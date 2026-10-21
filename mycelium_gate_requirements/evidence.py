# SPDX-License-Identifier: AGPL-3.0-only
"""Host-free gate evidence: device classes and capability digests only.

The private role binding addresses members by node id so the harness can drive
them.  Anything that leaves the owner's machine as evidence goes through
``public_receipt`` (roles -> device class + capability digest + member digest)
and ``assert_host_free``, which scans every key and string for IP addresses,
``user@host`` targets, home/volume paths, mDNS/tailnet host names and any
caller-supplied forbidden value (node ids, endpoint ids/addresses, serials).
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .binding import BINDING_PROTOCOL, verify_binding
from .spec import GateSpec, digest_of

RECEIPT_PROTOCOL = "mycelium.gate_role_binding_receipt.v1"

_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Za-z:])[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![0-9A-Za-z:])")
_USER_AT_HOST = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+")
_HOME_PATH = re.compile(r"(?:/Users/|/home/|/Volumes/|[A-Za-z]:\\Users\\|~/)")
_HOST_SUFFIX = re.compile(r"[A-Za-z0-9-]+\.(?:local|ts\.net|lan|home\.arpa)\b", re.IGNORECASE)
_PATTERNS = (
    ("ip_address", _IPV4),
    ("user_at_host", _USER_AT_HOST),
    ("home_or_volume_path", _HOME_PATH),
    ("host_name", _HOST_SUFFIX),
)


class HostIdentifierLeak(ValueError):
    def __init__(self, findings: list[str]) -> None:
        self.findings = findings
        super().__init__("evidence_host_identifier_present")


def _ipv6(text: str) -> bool:
    for match in _IPV6.finditer(text):
        value = match.group(0)
        if "::" in value or value.count(":") >= 5:
            return True
    return False


def _strings(value: Any, path: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield f"{path}.<key>", str(key)
            yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def host_identifier_findings(document: Any, *, forbidden_values: Iterable[str] = ()) -> list[str]:
    forbidden = sorted({item.lower() for item in forbidden_values
                        if isinstance(item, str) and len(item) >= 3})
    findings: list[str] = []
    for path, text in _strings(document):
        for name, pattern in _PATTERNS:
            if pattern.search(text):
                findings.append(f"{name}:{path}")
        if _ipv6(text):
            findings.append(f"ip_address:{path}")
        lowered = text.lower()
        if any(value in lowered for value in forbidden):
            findings.append(f"forbidden_value:{path}")
    return sorted(set(findings))


def assert_host_free(document: Any, *, forbidden_values: Iterable[str] = ()) -> None:
    findings = host_identifier_findings(document, forbidden_values=forbidden_values)
    if findings:
        raise HostIdentifierLeak(findings)


def forbidden_values(members: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """Every host-identifying value the seed knows about these members."""

    values: set[str] = set()
    for member in members:
        for key in ("node_id", "endpoint_id", "incarnation"):
            if isinstance(member.get(key), str):
                values.add(member[key])
        for address in member.get("endpoint_addrs") or ():
            if isinstance(address, str):
                values.add(address)
                host = address.rsplit(":", 1)[0].strip("[]") if ":" in address else address
                values.add(host)
    return tuple(sorted(value for value in values if len(value) >= 3))


def public_receipt(binding: Mapping[str, Any]) -> dict[str, Any]:
    """Host-free projection of a verified binding: what a gate may publish."""

    binding = verify_binding(binding)
    body = {
        "protocol": RECEIPT_PROTOCOL,
        "gate_id": binding["gate_id"],
        "spec_digest": binding["spec_digest"],
        "binding_digest": binding["binding_digest"],
        "inventory_digest": binding["inventory_digest"],
        "roles": {
            role: [{"device_class": dict(item["device_class"]),
                    "capability_digest": item["capability_digest"],
                    "member_digest": item["member_digest"]} for item in members]
            for role, members in binding["roles"].items()
        },
        "cases": {case: dict(value) for case, value in binding["cases"].items()},
        "claim_boundary": ("role binding receipt: names device classes and capability digests "
                           "of the members bound to each role; not a gate result"),
    }
    assert_host_free(body)
    return {**body, "receipt_digest": digest_of(body)}


def verify_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, Mapping) or receipt.get("protocol") != RECEIPT_PROTOCOL:
        raise ValueError("gate_role_binding_receipt_invalid")
    body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    if receipt.get("receipt_digest") != digest_of(body):
        raise ValueError("gate_role_binding_receipt_digest_mismatch")
    assert_host_free(body)
    return dict(receipt)


def check_case_evidence(spec: GateSpec, record: Mapping[str, Any], *,
                        forbidden: Iterable[str] = ()) -> list[str]:
    """Missing spec-required fields and host identifiers in one case record."""

    problems: list[str] = []
    cases = {case["case_id"] for case in spec.document["cases"]}
    if record.get("case_id") not in cases:
        problems.append("case_evidence_case_unknown")
    for field in spec.document["evidence"]["per_case_fields"]:
        if field not in record:
            problems.append(f"case_evidence_field_missing:{field}")
    problems.extend(f"case_evidence_host_identifier:{item}"
                    for item in host_identifier_findings(record, forbidden_values=forbidden))
    return problems


__all__ = [
    "BINDING_PROTOCOL", "RECEIPT_PROTOCOL", "HostIdentifierLeak", "assert_host_free",
    "check_case_evidence", "forbidden_values", "host_identifier_findings", "public_receipt",
    "verify_receipt",
]
