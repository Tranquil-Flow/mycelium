#!/usr/bin/env python3
"""Fail closed on cross-component contract hash or namespace drift."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

if __package__:
    from .contract_io import list_names_under_root, read_under_root
    from .contract_registry import CONTRACT_SPECS, EXPECTED_FIXTURE_NAMES
else:
    from contract_io import list_names_under_root, read_under_root
    from contract_registry import CONTRACT_SPECS, EXPECTED_FIXTURE_NAMES

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "contracts" / "contract-manifest.v1.json"


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def strict_json_loads(content: str | bytes) -> Any:
    return json.loads(content, object_pairs_hook=_strict_object)


def _canonical_relative(relative_path: Any) -> str:
    if not isinstance(relative_path, str) or not relative_path or Path(relative_path).is_absolute():
        raise ValueError("pinned path must be a non-empty relative path")
    parts = Path(relative_path).parts
    if ".." in parts:
        raise ValueError(f"pinned path escapes canonical root: {relative_path}")
    normalized = Path(relative_path).as_posix()
    if normalized != relative_path or any(part in {"", "."} for part in parts):
        raise ValueError(f"pinned path is not a canonical relative path: {relative_path}")
    return normalized


def resolve_inside_root(relative_path: Any) -> Path:
    normalized = _canonical_relative(relative_path)
    candidate = ROOT / normalized
    read_under_root(ROOT, candidate)
    return candidate


def _read_pin(pin: Any, label: str) -> tuple[list[str], bytes | None]:
    if not isinstance(pin, dict):
        return [f"{label}: pin must be an object"], None
    relative_path = pin.get("path")
    try:
        normalized = _canonical_relative(relative_path)
        content = read_under_root(ROOT, ROOT / normalized)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return [f"{label}: {exc}"], None
    errors: list[str] = []
    if pin.get("size_bytes") != len(content):
        errors.append(f"{label}: size drift for {relative_path}")
    digest = hashlib.sha256(content).hexdigest()
    if pin.get("sha256") != digest:
        errors.append(f"{label}: sha256 drift for {relative_path}")
    return errors, content


def verify_pin(pin: Any, label: str) -> list[str]:
    errors, _ = _read_pin(pin, label)
    return errors


def audit(manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    drift: list[str] = []
    try:
        manifest_root = Path(os.path.abspath(os.fspath(manifest_path))).parent
        payload = strict_json_loads(read_under_root(manifest_root, manifest_path))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {"checked": 0, "drift": [f"manifest: {exc}"], "ok": False}
    if not isinstance(payload, dict) or payload.get("protocol") != "mycelium.contract_manifest.v1":
        return {"checked": 0, "drift": ["manifest: wrong protocol"], "ok": False}
    contracts = payload.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        return {"checked": 0, "drift": ["manifest: contracts must be a non-empty list"], "ok": False}

    expected_by_path = {
        f"contracts/compatibility-fixtures/{spec.fixture_name}": spec for spec in CONTRACT_SPECS
    }
    expected_pairs = {(path, spec.protocol) for path, spec in expected_by_path.items()}
    observed_pairs: set[tuple[str, str]] = set()
    for contract in contracts:
        if not isinstance(contract, dict):
            continue
        fixture = contract.get("fixture")
        protocol = contract.get("protocol")
        if not isinstance(fixture, dict):
            continue
        fixture_path = fixture.get("path")
        if isinstance(fixture_path, str) and isinstance(protocol, str):
            observed_pairs.add((fixture_path, protocol))
    if len(contracts) != len(CONTRACT_SPECS) or observed_pairs != expected_pairs:
        drift.append("manifest: contract registry mismatch")

    fixture_dir = ROOT / "contracts" / "compatibility-fixtures"
    try:
        present_fixtures = {
            name for name in list_names_under_root(ROOT, fixture_dir) if name.endswith(".json")
        }
    except (OSError, ValueError) as exc:
        drift.append(f"manifest: cannot inspect fixture directory: {exc}")
    else:
        if present_fixtures != EXPECTED_FIXTURE_NAMES:
            drift.append("manifest: checked-in fixture registry mismatch")

    seen_protocols: set[str] = set()
    seen_fixtures: set[str] = set()
    for index, contract in enumerate(contracts):
        label = f"contracts[{index}]"
        if not isinstance(contract, dict):
            drift.append(f"{label}: contract must be an object")
            continue
        protocol = contract.get("protocol")
        if not isinstance(protocol, str) or not protocol.startswith("mycelium."):
            drift.append(f"{label}: invalid protocol")
        elif protocol in seen_protocols:
            drift.append(f"{label}: duplicate protocol owner {protocol}")
        else:
            seen_protocols.add(protocol)

        fixture = contract.get("fixture")
        fixture_errors, fixture_content = _read_pin(fixture, f"{label}.fixture")
        drift.extend(fixture_errors)
        fixture_path = fixture.get("path") if isinstance(fixture, dict) else None
        if isinstance(fixture_path, str):
            if fixture_path in seen_fixtures:
                drift.append(f"{label}: duplicate fixture owner {fixture_path}")
            seen_fixtures.add(fixture_path)
            try:
                if fixture_content is None:
                    raise ValueError("fixture could not be read safely")
                fixture_payload = strict_json_loads(fixture_content)
                if not isinstance(fixture_payload, dict) or fixture_payload.get("protocol") != protocol:
                    drift.append(f"{label}: fixture protocol does not match owner")
            except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                drift.append(f"{label}: invalid fixture JSON: {exc}")

        sources = contract.get("owner_sources")
        if not isinstance(sources, list) or not sources:
            drift.append(f"{label}: owner_sources must be non-empty")
        else:
            source_paths: set[str] = set()
            for source_index, source in enumerate(sources):
                source_label = f"{label}.owner_sources[{source_index}]"
                drift.extend(verify_pin(source, source_label))
                source_path = source.get("path") if isinstance(source, dict) else None
                if isinstance(source_path, str):
                    if source_path in source_paths:
                        drift.append(f"{label}: duplicate owner source {source_path}")
                    source_paths.add(source_path)

            spec = expected_by_path.get(fixture_path) if isinstance(fixture_path, str) else None
            if spec is not None and (
                source_paths != set(spec.owner_sources) or len(sources) != len(spec.owner_sources)
            ):
                drift.append(f"{label}: owner source registry mismatch")

    return {"checked": len(contracts), "drift": sorted(set(drift)), "ok": not drift}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = audit(args.manifest)
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    elif result["ok"]:
        print(f"contract audit OK: {result['checked']} contracts")
    else:
        for error in result["drift"]:
            print(error, file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
