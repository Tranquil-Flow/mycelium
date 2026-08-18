#!/usr/bin/env python3
"""Audit the executable Mycelium claim ledger and product-action authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


PROTOCOL = "mycelium.governance_audit.v1"
LEDGER_PROTOCOL = "mycelium.governance_ledger.v1"
LEDGER_PATH = "contracts/governance-ledger.v1.json"
_STATE_ORDER = (
    "absent",
    "design_only",
    "implemented_unintegrated",
    "partial",
    "qualified",
)
_LEDGER_FIELDS = {
    "protocol",
    "state_order",
    "governing_plan",
    "architecture_ledger",
    "contract_manifest",
    "capabilities",
    "milestones",
    "authorized_product_actions",
    "read_only_boundary_protocols",
    "release_exclusions",
}
_ACTION_FIELDS = {"client", "methods", "endpoints", "protocols", "consent"}
_MILESTONE_ROW = re.compile(
    r"^\| (M(?:17|18|19|20|21|22|23)) \| `([^`]+)` \|", re.MULTILINE
)
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_CONST_PATH = re.compile(
    r"\b(?:export\s+)?const\s+([A-Z][A-Z0-9_]*)\s*=\s*(['\"])(/[^'\"]+)\2"
)
_DIRECT_MUTATION = re.compile(
    r"(?:\bfetch|\.request|\brequest)\s*\(\s*"
    r"(?P<target>[A-Z][A-Z0-9_]*|(?P<quote>['\"])(?P<endpoint>/[^'\"]+)(?P=quote))\s*,"
    r"(?P<arguments>(?:(?!\)\s*;).){0,700}?)\bmethod\s*:\s*(['\"])"
    r"(?P<method>POST|PUT|PATCH|DELETE)['\"]",
    re.DOTALL,
)
_DIRECT_POST = re.compile(r"\.post\s*\(\s*(['\"])(/[^'\"]+)\1")
_PATH_MEMBER_MUTATION = re.compile(
    r"this\.request\s*\(\s*this\.paths\.([a-zA-Z0-9_]+)\s*,"
    r".{0,500}?\bmethod\s*:\s*(['\"])(POST|PUT|PATCH|DELETE)\2",
    re.DOTALL,
)
_PATH_MEMBER_DEFAULT = re.compile(
    r"([a-zA-Z0-9_]+)\s*:\s*sameOriginPath\([^)]*?\?\?\s*"
    r"(['\"])(/[^'\"]+)\2\s*\)"
)
_BOOTSTRAP_MUTATION = re.compile(
    r"#mutate\s*\(\s*bootstrap\.api\.([a-zA-Z0-9_]+)"
)
_PRODUCT_API_PATH = re.compile(
    r"^\s*([a-zA-Z0-9_]+)\s*:\s*(['\"])(/[^'\"]+)\2\s*,?\s*$",
    re.MULTILINE,
)
_EXACT_BOOTSTRAP_MUTATION = re.compile(
    r"exactSameOriginPath\(\s*bootstrap\.api\.([a-zA-Z0-9_]+)\s*,"
    r"\s*PRODUCT_API_PATHS\.[a-zA-Z0-9_]+\s*,?\s*\).*?"
    r"\bmethod\s*:\s*(['\"])(GET|POST|PUT|PATCH|DELETE)\2",
    re.DOTALL,
)
_DYNAMIC_CANCEL = re.compile(
    r"accepted\.cancel_path\s*,\s*\{.{0,300}?"
    r"\bmethod\s*:\s*(['\"])DELETE\1",
    re.DOTALL,
)
_CANCEL_TEMPLATE = re.compile(
    r"const\s+expectedCancelPath\s*=\s*`(/[^`]+)`"
)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _canonical_path(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("path_invalid")
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or candidate.as_posix() != value
        or ".." in candidate.parts
    ):
        raise ValueError("path_invalid")
    return value


def _read(root: Path, relative: object) -> bytes:
    path = root / _canonical_path(relative)
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise ValueError("path_not_regular")
    return resolved.read_bytes()


def _load(root: Path, relative: object) -> Mapping[str, Any]:
    document = json.loads(_read(root, relative), object_pairs_hook=_strict_object)
    if not isinstance(document, dict):
        raise ValueError("document_not_object")
    return document


def _product_api_paths(root: Path) -> dict[str, str]:
    source = _read(root, "ui/web/src/app/contracts.ts").decode("utf-8")
    return {key: endpoint for key, _, endpoint in _PRODUCT_API_PATH.findall(source)}


def _implemented_action_endpoints(root: Path, client: str) -> set[str]:
    """Extract the mutating same-origin endpoints exercised by one governed client."""

    source = _read(root, client).decode("utf-8")
    constants = {name: endpoint for name, _, endpoint in _CONST_PATH.findall(source)}
    endpoints = set(_DIRECT_POST.findall(source))
    implemented = {endpoint for _, endpoint in endpoints}

    for match in _DIRECT_MUTATION.finditer(source):
        target = match.group("target")
        if target.startswith(("'", '"')):
            implemented.add(match.group("endpoint"))
        elif target in constants:
            implemented.add(constants[target])

    member_defaults = {
        key: endpoint for key, _, endpoint in _PATH_MEMBER_DEFAULT.findall(source)
    }
    for key, _, _ in _PATH_MEMBER_MUTATION.findall(source):
        if key not in member_defaults:
            raise ValueError("implemented_endpoint_unresolved")
        implemented.add(member_defaults[key])

    api_keys = set(_BOOTSTRAP_MUTATION.findall(source))
    api_keys.update(
        key
        for key, _, method in _EXACT_BOOTSTRAP_MUTATION.findall(source)
        if method in _MUTATING_METHODS
    )
    if api_keys:
        product_paths = _product_api_paths(root)
        for key in api_keys:
            if key not in product_paths:
                raise ValueError("implemented_endpoint_unresolved")
            implemented.add(product_paths[key])

    if _DYNAMIC_CANCEL.search(source) is not None:
        contracts = _read(root, "ui/web/src/app/contracts.ts").decode("utf-8")
        match = _CANCEL_TEMPLATE.search(contracts)
        if match is None:
            raise ValueError("implemented_endpoint_unresolved")
        template = re.sub(r"\$\{[^}]+\}", "{request_id}", match.group(1))
        implemented.add(template)

    if not implemented:
        raise ValueError("implemented_endpoint_set_empty")
    return implemented


def audit(repo_root: str | Path) -> dict[str, Any]:
    findings: list[str] = []
    try:
        root = Path(repo_root).resolve(strict=True)
        ledger = _load(root, LEDGER_PATH)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "checked_actions": 0,
            "checked_capabilities": 0,
            "checked_milestones": 0,
            "findings": [f"ledger:{type(exc).__name__}"],
            "ledger_digest": None,
            "ok": False,
            "protocol": PROTOCOL,
            "release_ready": False,
        }

    if set(ledger) != _LEDGER_FIELDS or ledger.get("protocol") != LEDGER_PROTOCOL:
        findings.append("ledger_shape_invalid")
    if tuple(ledger.get("state_order", ())) != _STATE_ORDER:
        findings.append("state_order_invalid")
    ranks = {state: index for index, state in enumerate(_STATE_ORDER)}

    capabilities_value = ledger.get("capabilities")
    capabilities: dict[str, str] = {}
    if not isinstance(capabilities_value, list):
        findings.append("capabilities_invalid")
        capabilities_value = []
    for item in capabilities_value:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "state"}
            or not isinstance(item.get("id"), str)
            or item.get("state") not in ranks
            or item["id"] in capabilities
        ):
            findings.append("capability_entry_invalid")
            continue
        capabilities[item["id"]] = item["state"]
    if set(capabilities) != {f"4.{index}" for index in range(1, 16)}:
        findings.append("capability_set_invalid")

    milestones_value = ledger.get("milestones")
    milestones: dict[str, str] = {}
    if not isinstance(milestones_value, list):
        findings.append("milestones_invalid")
        milestones_value = []
    for item in milestones_value:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "state", "capability_claims"}
            or not isinstance(item.get("id"), str)
            or item.get("state") not in ranks
            or item["id"] in milestones
            or not isinstance(item.get("capability_claims"), dict)
        ):
            findings.append("milestone_entry_invalid")
            continue
        milestones[item["id"]] = item["state"]
        for capability_id, claim_state in item["capability_claims"].items():
            current_state = capabilities.get(capability_id)
            if claim_state not in ranks or current_state is None:
                findings.append(
                    f"milestone_capability_unknown:{item['id']}:{capability_id}"
                )
            elif ranks[claim_state] > ranks[current_state]:
                findings.append(
                    f"milestone_promotion_unsupported:{item['id']}:{capability_id}"
                )
    if set(milestones) != {f"M{index}" for index in range(17, 24)}:
        findings.append("milestone_set_invalid")

    for field in ("governing_plan", "architecture_ledger"):
        try:
            _read(root, ledger.get(field))
        except (OSError, ValueError):
            findings.append(f"{field}_unavailable")
    try:
        architecture = _read(root, ledger.get("architecture_ledger")).decode("utf-8")
        architecture_rows = dict(_MILESTONE_ROW.findall(architecture))
    except (OSError, UnicodeDecodeError, ValueError):
        architecture_rows = {}
    if architecture_rows != milestones:
        findings.append("architecture_milestone_ledger_mismatch")

    manifest_value = ledger.get("contract_manifest")
    if not isinstance(manifest_value, dict) or set(manifest_value) != {
        "path",
        "protocol",
    }:
        findings.append("contract_manifest_binding_invalid")
    else:
        try:
            manifest = _load(root, manifest_value["path"])
            if manifest.get("protocol") != manifest_value["protocol"]:
                findings.append("contract_manifest_protocol_mismatch")
        except (OSError, ValueError, json.JSONDecodeError):
            findings.append("contract_manifest_unavailable")

    actions_value = ledger.get("authorized_product_actions")
    action_clients: set[str] = set()
    pinned_protocols: set[str] = set()
    if not isinstance(actions_value, list):
        findings.append("product_actions_invalid")
        actions_value = []
    for action in actions_value:
        if not isinstance(action, dict) or set(action) != _ACTION_FIELDS:
            findings.append("product_action_shape_invalid")
            continue
        try:
            client = _canonical_path(action["client"])
            _read(root, client)
        except (OSError, ValueError):
            findings.append("product_action_client_invalid")
            continue
        methods = action.get("methods")
        endpoints = action.get("endpoints")
        protocols = action.get("protocols")
        consent = action.get("consent")
        if (
            client in action_clients
            or not client.startswith("ui/web/src/")
            or not isinstance(methods, list)
            or not methods
            or any(
                method not in {"POST", "PUT", "PATCH", "DELETE"} for method in methods
            )
            or not isinstance(endpoints, list)
            or not endpoints
            or any(
                not isinstance(endpoint, str)
                or not endpoint.startswith("/")
                or endpoint.startswith("//")
                for endpoint in endpoints
            )
            or not isinstance(protocols, list)
            or not protocols
            or any(
                not isinstance(protocol, str) or not protocol.startswith("mycelium.")
                for protocol in protocols
            )
            or not isinstance(consent, str)
            or not consent
        ):
            findings.append("product_action_authority_invalid")
            continue
        try:
            implemented_endpoints = _implemented_action_endpoints(root, client)
        except (OSError, UnicodeError, ValueError):
            findings.append(f"product_action_implementation_invalid:{client}")
            continue
        if set(endpoints) != implemented_endpoints:
            findings.append(f"product_action_endpoint_set_mismatch:{client}")
            continue
        action_clients.add(client)
        pinned_protocols.update(protocols)
    read_only_protocols = ledger.get("read_only_boundary_protocols")
    if (
        not isinstance(read_only_protocols, list)
        or not read_only_protocols
        or any(
            not isinstance(protocol, str) or not protocol.startswith("mycelium.")
            for protocol in read_only_protocols
        )
        or len(set(read_only_protocols)) != len(read_only_protocols)
    ):
        findings.append("read_only_boundary_protocols_invalid")
    else:
        pinned_protocols.update(read_only_protocols)
    if not pinned_protocols:
        findings.append("boundary_protocols_unpinned")

    exclusions = ledger.get("release_exclusions")
    if (
        not isinstance(exclusions, list)
        or not exclusions
        or any(not isinstance(item, str) or not item for item in exclusions)
    ):
        findings.append("release_exclusions_invalid")

    ledger_bytes = _read(root, LEDGER_PATH)
    return {
        "checked_actions": len(action_clients),
        "checked_capabilities": len(capabilities),
        "checked_milestones": len(milestones),
        "findings": sorted(set(findings)),
        "ledger_digest": "sha256:" + hashlib.sha256(ledger_bytes).hexdigest(),
        "ok": not findings,
        "protocol": PROTOCOL,
        "release_ready": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = audit(args.repo_root)
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    elif result["ok"]:
        print(
            "governance audit OK: "
            f"{result['checked_capabilities']} capabilities, "
            f"{result['checked_milestones']} milestones, "
            f"{result['checked_actions']} product-action clients"
        )
    else:
        for finding in result["findings"]:
            print(finding, file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
