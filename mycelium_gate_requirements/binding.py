# SPDX-License-Identifier: AGPL-3.0-only
"""Bind gate roles to eligible seed members, deterministically, or refuse by name.

Inputs are the seed's membership records (``SeedCoordinator.members()``
projections or ``seed_members`` rows, see ``members_from_seed_database``) and
the members' signed capability reports.  A member is a candidate only when it
is current (``NEW``/``CONFIGURED``/``RUNNING`` with a fresh lease) and its
latest report verifies against the key the seed holds for it, names its node
and generation, and is fresh.  Two report families are understood:

* ``mycelium.a13_capability_link_report.v1`` (A13 join flow): Mac RAM, free
  disk, MLX probe, power/thermal and the member->owner relay link;
* ``mycelium.membership.capability_report.v2`` (platform-neutral): only
  ``native_observed`` memory/storage/thermal/power families count.

Binding is a bounded backtracking search over roles in spec order; candidates
are ranked by usable memory (desc), link RTT (asc) and member digest, so the
result depends only on the inventory, never on input order.  When it fails the
refusal names ``requirements_unsatisfied:<role>`` with every member's reasons,
or ``requirements_unsatisfied:joint_assignment`` when each role alone has
candidates but distinctness makes them jointly impossible.
"""
from __future__ import annotations

import itertools
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .spec import GateSpec, GateSpecError, canonical_bytes, digest_of

BINDING_PROTOCOL = "mycelium.gate_role_binding.v1"
A13_REPORT = "mycelium.a13_capability_link_report.v1"
V2_REPORT = "mycelium.membership.capability_report.v2"
CURRENT_LIFECYCLE = frozenset({"NEW", "CONFIGURED", "RUNNING"})
GIB = 1024**3
_SEARCH_BUDGET = 200_000

#: Reasons, in the fixed order they are reported for one member.
REQUIREMENT_REASONS = (
    "peer_class_not_allowed",
    "os_family_not_allowed",
    "architecture_not_allowed",
    "backend_not_allowed",
    "report_protocol_not_allowed",
    "usable_memory_below_requirement",
    "free_disk_below_requirement",
    "link_measurement_missing",
    "link_rtt_above_bound",
    "link_throughput_below_bound",
    "thermal_state_not_allowed",
    "power_source_not_allowed",
)

_OS_BY_PREFIX = (("mac_", "macos"), ("android_", "android"), ("linux_", "linux"),
                 ("windows_", "windows"))
_ARCH_BY_CLASS = {"mac_mlx_iroh": "arm64", "windows_native_iroh": "x86_64"}
_POWER = {"external": "external", "ac": "external", "battery": "battery"}


class GateBindingError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class GateRequirementsUnsatisfied(GateBindingError):
    """Refusal with named reasons; ``detail`` is keyed by member digest only."""

    def __init__(self, reasons: list[str], detail: dict[str, Any], members: dict[str, str]) -> None:
        self.reasons = list(reasons)
        self.detail = detail
        #: member digest -> node id, for the operator; not part of ``detail``.
        self.members = dict(members)
        super().__init__(",".join(self.reasons))

    def __str__(self) -> str:
        return ",".join(self.reasons)


# -- seed membership ---------------------------------------------------------

def members_from_seed_database(database: str | Path) -> list[dict[str, Any]]:
    """Read ``seed_members`` read-only; returns ``SeedCoordinator.members()``-shaped records."""

    uri = Path(database).resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        raise GateBindingError("seed_membership_unavailable") from None
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT node_id, endpoint_id, endpoint_addrs_json, peer_class, runtime_capability_json, "
            "verification_key_digest, incarnation, generation, lease_expires_at, lifecycle_state "
            "FROM seed_members ORDER BY node_id").fetchall()
    except sqlite3.Error:
        raise GateBindingError("seed_membership_unavailable") from None
    finally:
        connection.close()
    members = []
    for row in rows:
        members.append({
            "node_id": row["node_id"], "endpoint_id": row["endpoint_id"],
            "endpoint_addrs": list(json.loads(row["endpoint_addrs_json"])),
            "peer_class": row["peer_class"],
            "runtime_capability": dict(json.loads(row["runtime_capability_json"])),
            "verification_key_digest": row["verification_key_digest"],
            "incarnation": row["incarnation"], "generation": int(row["generation"]),
            "lease_expires_at": float(row["lease_expires_at"]),
            "lifecycle_state": row["lifecycle_state"],
        })
    return members


def member_digest(member: Mapping[str, Any]) -> str:
    """Stable, host-free member handle: binds node id to its seed-held key digest."""

    return digest_of({"node_id": member["node_id"],
                      "verification_key_digest": member["verification_key_digest"]})


def _os_family(peer_class: str) -> str:
    for prefix, family in _OS_BY_PREFIX:
        if peer_class.startswith(prefix):
            return family
    return "unknown"


def _report_node(report: Any) -> str | None:
    if not isinstance(report, Mapping):
        return None
    if report.get("protocol") == V2_REPORT and isinstance(report.get("report"), Mapping):
        value = report["report"].get("node_id")
    else:
        value = report.get("node_id")
    return value if isinstance(value, str) else None


def _verify_a13(report: Mapping[str, Any], member: Mapping[str, Any], now: float,
                max_age: float) -> dict[str, Any]:
    from mycelium_a13_macos.capability import verify_capability_link_report

    try:
        statement = verify_capability_link_report(
            dict(report), now=now, expected_key_digest=member["verification_key_digest"],
            node_id=member["node_id"])
    except Exception as exc:  # PairingError carries the stable code
        code = getattr(exc, "code", None)
        raise GateBindingError(code if isinstance(code, str) and code.startswith("capability_report_")
                               else "capability_report_invalid") from None
    if now - float(statement["observed_at"]) > max_age:
        raise GateBindingError("capability_report_expired")
    if statement["generation"] != member["generation"]:
        raise GateBindingError("capability_report_generation_mismatch")
    device, link = statement["device"], statement["link"]
    return {
        "report_protocol": A13_REPORT,
        "observed_at": float(statement["observed_at"]),
        "architecture": "arm64",
        "backends": ["mlx"] if device["mlx"]["available"] else [],
        "chip": device["chip"],
        "memory_bytes": int(device["memory_bytes"]),
        "sustainable_memory_bytes": None,
        "free_disk_bytes": int(device["available_storage_bytes"]),
        "link": {"rtt_ms": float(link["rtt_ms_median"]),
                 "bytes_per_second": float(min(link["upload_bytes_per_second"],
                                               link["download_bytes_per_second"])),
                 "path_kind": link["path_kind"]},
        "thermal": device["thermal"],
        "power": _POWER.get(device["power"], "unknown"),
    }


def _verify_v2(report: Mapping[str, Any], member: Mapping[str, Any], now: float,
               max_age: float) -> dict[str, Any]:
    from mycelium_platform_neutral.capability_report import verify_capability_report

    try:
        verified = verify_capability_report(
            report, now=now, expected_key_digest=member["verification_key_digest"])
    except Exception as exc:
        code = getattr(exc, "code", None) or (exc.args[0] if exc.args else None)
        if code == "capability_report_expired":
            raise GateBindingError("capability_report_expired") from None
        raise GateBindingError("capability_report_signature_invalid"
                               if code in {"capability_report_key_mismatch",
                                           "capability_report_signature_invalid"}
                               else "capability_report_invalid") from None
    if verified.node_id != member["node_id"]:
        raise GateBindingError("capability_report_signature_invalid")
    if now - verified.issued_at > max_age:
        raise GateBindingError("capability_report_expired")
    if verified.membership_generation != member["generation"]:
        raise GateBindingError("capability_report_generation_mismatch")

    def native(family: str) -> Mapping[str, Any]:
        observation = verified.family(family)
        return observation.values if observation.satisfies("native_observed") else {}

    memory, storage = native("memory"), native("storage")
    thermal, power = native("thermal"), native("power")
    sustainable = memory.get("sustainable_bytes") or memory.get("available_bytes")
    backend = member["runtime_capability"].get("runtime_backend")
    return {
        "report_protocol": V2_REPORT,
        "observed_at": float(verified.issued_at),
        "architecture": _ARCH_BY_CLASS.get(member["peer_class"],
                                           "arm64" if _os_family(member["peer_class"]) == "android"
                                           else "unknown"),
        "backends": [backend] if isinstance(backend, str) and backend not in {"tbd", ""} else [],
        "chip": None,
        "memory_bytes": None,
        "sustainable_memory_bytes": int(sustainable) if sustainable is not None else None,
        "free_disk_bytes": int(storage["free_bytes"]) if storage.get("free_bytes") is not None else None,
        "link": None,
        "thermal": thermal.get("state") or "unknown",
        "power": _POWER.get(power.get("source"), "unknown"),
    }


def _member_current(member: Mapping[str, Any], now: float) -> bool:
    return (member.get("lifecycle_state") in CURRENT_LIFECYCLE
            and isinstance(member.get("lease_expires_at"), (int, float))
            and float(member["lease_expires_at"]) > now)


def member_facts(spec: GateSpec, member: Mapping[str, Any], reports: Sequence[Mapping[str, Any]],
                 *, now: float) -> dict[str, Any]:
    """Normalise one member; ``facts['refusal']`` names why it can bind nothing."""

    peer_class = str(member["peer_class"])
    base = {
        "node_id": member["node_id"],
        "generation": member["generation"],
        "peer_class": peer_class,
        "member_digest": member_digest(member),
        "os_family": _os_family(peer_class),
        "refusal": None,
    }
    if not _member_current(member, now):
        return {**base, "refusal": "member_not_current"}
    candidates = [report for report in reports if _report_node(report) == member["node_id"]]
    if not candidates:
        return {**base, "refusal": "capability_report_missing"}
    max_age = float(spec.document["freshness"]["max_report_age_seconds"])
    verified: list[tuple[float, str, dict[str, Any]]] = []
    failures: list[str] = []
    for report in candidates:
        try:
            if report.get("protocol") == A13_REPORT:
                facts = _verify_a13(report, member, now, max_age)
            elif report.get("protocol") == V2_REPORT:
                facts = _verify_v2(report, member, now, max_age)
            else:
                raise GateBindingError("capability_report_protocol_unsupported")
        except GateBindingError as exc:
            failures.append(exc.code)
            continue
        capability_digest = digest_of(json.loads(canonical_bytes(report)))
        verified.append((facts["observed_at"], capability_digest, facts))
    if not verified:
        return {**base, "refusal": sorted(failures)[0]}
    _, capability_digest, facts = max(verified, key=lambda item: (item[0], item[1]))
    policy = spec.document["resource_policy"]
    if facts["memory_bytes"] is not None:
        usable = int(facts["memory_bytes"] * float(policy["usable_memory_fraction"]))
        usable -= int(policy["memory_reserve_bytes"])
    else:
        usable = facts["sustainable_memory_bytes"]
    memory_total = facts["memory_bytes"] or facts["sustainable_memory_bytes"]
    device_class = {
        "peer_class": peer_class,
        "os_family": base["os_family"],
        "architecture": facts["architecture"],
        "backends": sorted(facts["backends"]),
        "chip": facts["chip"],
        "memory_class_gib": int(memory_total // GIB) if memory_total else None,
        "report_protocol": facts["report_protocol"],
    }
    return {
        **base,
        "capability_digest": capability_digest,
        "report_protocol": facts["report_protocol"],
        "architecture": facts["architecture"],
        "backends": sorted(facts["backends"]),
        "usable_memory_bytes": max(usable, 0) if usable is not None else None,
        "free_disk_bytes": facts["free_disk_bytes"],
        "link": facts["link"],
        "thermal": facts["thermal"],
        "power": facts["power"],
        "device_class": device_class,
    }


def role_reasons(spec: GateSpec, role: Mapping[str, Any], facts: Mapping[str, Any]) -> list[str]:
    """Every unmet requirement of ``role`` for one member, in fixed order."""

    if facts["refusal"] is not None:
        return [facts["refusal"]]
    need = role["requirements"]
    found: list[str] = []
    if "peer_classes" in need and facts["peer_class"] not in need["peer_classes"]:
        found.append("peer_class_not_allowed")
    if "os_families" in need and facts["os_family"] not in need["os_families"]:
        found.append("os_family_not_allowed")
    if "architectures" in need and facts["architecture"] not in need["architectures"]:
        found.append("architecture_not_allowed")
    if "backends" in need and not set(facts["backends"]) & set(need["backends"]):
        found.append("backend_not_allowed")
    if "report_protocols" in need and facts["report_protocol"] not in need["report_protocols"]:
        found.append("report_protocol_not_allowed")
    memory = spec.required_memory_bytes(role)
    if memory and (facts["usable_memory_bytes"] is None or facts["usable_memory_bytes"] < memory):
        found.append("usable_memory_below_requirement")
    disk = spec.required_disk_bytes(role)
    if disk and (facts["free_disk_bytes"] is None or facts["free_disk_bytes"] < disk):
        found.append("free_disk_below_requirement")
    if "max_link_rtt_ms" in need or "min_link_bytes_per_second" in need:
        link = facts["link"]
        if link is None:
            found.append("link_measurement_missing")
        else:
            if "max_link_rtt_ms" in need and link["rtt_ms"] > float(need["max_link_rtt_ms"]):
                found.append("link_rtt_above_bound")
            if ("min_link_bytes_per_second" in need
                    and link["bytes_per_second"] < float(need["min_link_bytes_per_second"])):
                found.append("link_throughput_below_bound")
    if "thermal_states" in need and facts["thermal"] not in need["thermal_states"]:
        found.append("thermal_state_not_allowed")
    if "power_sources" in need and facts["power"] not in need["power_sources"]:
        found.append("power_source_not_allowed")
    return found


def _rank(facts: Mapping[str, Any]) -> tuple:
    link = facts.get("link")
    return (-(facts.get("usable_memory_bytes") or 0),
            link["rtt_ms"] if link else math.inf,
            facts["member_digest"])


def _search(spec: GateSpec, eligible: dict[str, list[Mapping[str, Any]]]
            ) -> tuple[dict[str, list] | None, str | None]:
    roles = spec.roles
    budget = [_SEARCH_BUDGET]
    deepest = [0]
    chosen: dict[str, list[Mapping[str, Any]]] = {}

    def allowed(role: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
        for other, members in chosen.items():
            if any(item["member_digest"] == candidate["member_digest"] for item in members):
                other_role = spec.role(other)
                if (other not in role["may_share_member_with"]
                        and role["role"] not in other_role["may_share_member_with"]):
                    return False
        same = role["same_backend_as"]
        if same is not None and same in chosen:
            backends = set().union(*(set(item["backends"]) for item in chosen[same]))
            if not set(candidate["backends"]) & backends:
                return False
        return True

    def visit(index: int) -> bool:
        deepest[0] = max(deepest[0], index)
        if index == len(roles):
            return True
        role = roles[index]
        pool = [item for item in eligible[role["role"]] if allowed(role, item)]
        # Prefer members not already bound to another role, then the ranking.
        used = {item["member_digest"] for members in chosen.values() for item in members}
        pool.sort(key=lambda item: (item["member_digest"] in used, _rank(item)))
        for size in range(min(role["count"]["max"], len(pool)), role["count"]["min"] - 1, -1):
            for combo in itertools.combinations(pool, size):
                budget[0] -= 1
                if budget[0] < 0:
                    raise GateBindingError("binding_search_budget_exhausted")
                chosen[role["role"]] = list(combo)
                if visit(index + 1):
                    return True
                del chosen[role["role"]]
        return False

    if visit(0):
        return dict(chosen), None
    return None, roles[min(deepest[0], len(roles) - 1)]["role"]


def _binding_body(spec: GateSpec, chosen: Mapping[str, list], inventory_digest: str,
                  now: float) -> dict[str, Any]:
    roles = {}
    placements: list[str] = []
    for role in spec.roles:
        bound = []
        for item in chosen.get(role["role"], []):
            bound.append({
                "node_id": item["node_id"], "generation": item["generation"],
                "peer_class": item["peer_class"], "member_digest": item["member_digest"],
                "capability_digest": item["capability_digest"],
                "device_class": item["device_class"],
            })
            if item["node_id"] not in placements:
                placements.append(item["node_id"])
        roles[role["role"]] = bound
    cases = {}
    for case in spec.document["cases"]:
        reason = None
        for name, minimum in case["requires_roles"].items():
            if len(roles[name]) < minimum:
                reason = f"requires_roles:{name}>={minimum}"
                break
        cases[case["case_id"]] = {"bindable": reason is None, "reason": reason}
    return {
        "protocol": BINDING_PROTOCOL,
        "gate_id": spec.gate_id,
        "spec_digest": spec.digest,
        "inventory_digest": inventory_digest,
        "bound_at": float(now),
        "roles": roles,
        "placements": placements,
        "cases": cases,
    }


def bind_roles(spec: GateSpec, *, members: Iterable[Mapping[str, Any]],
               reports: Iterable[Mapping[str, Any]], now: float) -> dict[str, Any]:
    members = sorted((dict(item) for item in members), key=lambda item: str(item["node_id"]))
    if len({item["node_id"] for item in members}) != len(members):
        raise GateBindingError("seed_membership_duplicate_node")
    reports = [dict(item) for item in reports]
    facts = [member_facts(spec, member, reports, now=now) for member in members]
    digests = {item["member_digest"]: item["node_id"] for item in facts}
    inventory_digest = digest_of(sorted(
        ({k: v for k, v in item.items() if k != "node_id"} for item in facts),
        key=lambda item: item["member_digest"]))

    eligible: dict[str, list[Mapping[str, Any]]] = {}
    reasons: list[str] = []
    detail: dict[str, Any] = {}
    for role in spec.roles:
        rejected: dict[str, list[str]] = {}
        accepted = []
        for item in facts:
            problems = role_reasons(spec, role, item)
            if problems:
                rejected[item["member_digest"]] = problems
            else:
                accepted.append(item)
        eligible[role["role"]] = sorted(accepted, key=_rank)
        if len(accepted) < role["count"]["min"]:
            reasons.append(f"requirements_unsatisfied:{role['role']}")
            detail[role["role"]] = {"needed": role["count"]["min"], "eligible": len(accepted),
                                    "rejected": dict(sorted(rejected.items()))}
    if reasons:
        raise GateRequirementsUnsatisfied(reasons, detail, digests)
    chosen, unfillable = _search(spec, eligible)
    if chosen is None:
        raise GateRequirementsUnsatisfied(
            ["requirements_unsatisfied:joint_assignment"],
            {"joint_assignment": {
                "roles": [role["role"] for role in spec.roles],
                "eligible": {role["role"]: len(eligible[role["role"]]) for role in spec.roles},
                "first_unfillable_role": unfillable,
                "reason": "distinct_members_or_backend_match_unavailable"}},
            digests)
    body = _binding_body(spec, chosen, inventory_digest, now)
    return {**body, "binding_digest": digest_of(body)}


def verify_binding(document: Any, *, spec: GateSpec | None = None) -> dict[str, Any]:
    if not isinstance(document, Mapping) or document.get("protocol") != BINDING_PROTOCOL:
        raise GateBindingError("gate_role_binding_invalid")
    body = {key: value for key, value in document.items() if key != "binding_digest"}
    try:
        computed = digest_of(body)
    except (TypeError, ValueError):
        raise GateBindingError("gate_role_binding_invalid") from None
    if document.get("binding_digest") != computed:
        raise GateBindingError("gate_role_binding_digest_mismatch")
    if spec is not None and (document.get("spec_digest") != spec.digest
                             or document.get("gate_id") != spec.gate_id):
        raise GateBindingError("gate_role_binding_spec_mismatch")
    roles = document.get("roles")
    if not isinstance(roles, Mapping) or not isinstance(document.get("placements"), list):
        raise GateBindingError("gate_role_binding_invalid")
    return dict(document)


def load_binding(path: str | Path, *, gate_id: str | None = None) -> dict[str, Any]:
    """Read and verify a binding file; with ``gate_id`` also pin it to the shipped spec."""

    from .spec import load_pinned_spec

    try:
        document = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        raise GateBindingError("gate_role_binding_unreadable") from None
    spec = None
    if gate_id is not None:
        try:
            spec = load_pinned_spec(gate_id)
        except GateSpecError as exc:
            raise GateBindingError(exc.code) from None
    return verify_binding(document, spec=spec)


def role_node_ids(binding: Mapping[str, Any], role: str) -> list[str]:
    members = binding.get("roles", {}).get(role)
    if members is None:
        raise GateBindingError("gate_role_binding_role_unknown")
    return [item["node_id"] for item in members]


def role_device_classes(binding: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {role: [dict(item["device_class"]) for item in members]
            for role, members in binding["roles"].items()}
