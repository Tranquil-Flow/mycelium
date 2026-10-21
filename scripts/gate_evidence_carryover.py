#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Decide, per physical A-gate, whether GREEN evidence carries to the final candidate.

Owner decision D2 (2026-10-13): GREEN evidence obtained on the evidence
candidate STANDS on the final candidate when the gate's pinned source set
(``scripts/gate_source_sets.py``) is byte-identical between the two commits;
any change in a pinned file forces a rerun.

The pinned set is computed at BOTH commits and the union is compared byte for
byte (sha256 of the blob at each commit), so a file that only the final
candidate pulls in (a new import, a new module shadowing a third-party one)
also forces a rerun.  The output is a JSON receipt with a self digest (no
signing keys: it binds commits, trees, tool digests, per-file digests and
verdicts) that can be attached to the gate evidence.

Out-of-repo run-day drivers are not versioned in Git: the receipt records
their current sha256 and flags them.  When a generator manifest captured at
evidence time is supplied (``--evidence-externals``), a changed driver forces
RERUN as well.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.gate_source_sets import (  # noqa: E402
    DEFINITIONS_VERSION,
    GATE_DEFINITIONS,
    GENERATED_PIN_FILES,
    GateDefinition,
    GitTree,
    canonical_bytes,
    compute_gate_source_set,
    definitions_digest,
    gate_manifest,
    runday_root_default,
)

RECEIPT_PROTOCOL = "mycelium.gate_evidence_carryover.v1"
DECISION_RULE = (
    "D2 (owner, 2026-10-13): a physical A-gate's GREEN evidence obtained on the evidence candidate "
    "STANDS on the final candidate when the gate's pinned source set is byte-identical between the two "
    "commits; any change in a pinned file forces a rerun. R1 (owner, 2026-10-13): a change confined to the "
    "generated pin manifests (A6 source manifest, contract manifest) carries; it is labelled "
    "generated_pins_only."
)
CLAIM_BOUNDARY = (
    "Source-identity check only: CARRY says the pinned in-repo bytes are identical; it does not re-verify "
    "the evidence itself, device state, model artifacts, or out-of-repo run-day drivers (recorded, not "
    "versioned in Git)."
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _tool_digests() -> dict[str, str]:
    here = Path(__file__).resolve()
    return {
        "scripts/gate_source_sets.py": _sha256((here.parent / "gate_source_sets.py").read_bytes()),
        "scripts/gate_evidence_carryover.py": _sha256(here.read_bytes()),
    }


def _blob_sha(tree: GitTree, path: str) -> str | None:
    return tree.sha256(path) if tree.is_file(path) else None


def _external_status(
    gate: str,
    current: Sequence[Mapping[str, Any]],
    baseline: Mapping[str, Any] | None,
) -> tuple[str, list[dict[str, Any]]]:
    if baseline is None:
        return "unversioned_current_digest_only", []
    recorded = {
        item["path"]: item.get("sha256")
        for item in baseline.get("gates", {}).get(gate, {}).get("externals", [])
    }
    changed = [
        {"path": item["path"], "evidence_sha256": recorded.get(item["path"]), "final_sha256": item.get("sha256")}
        for item in current
        if recorded.get(item["path"]) != item.get("sha256")
    ]
    return ("changed" if changed else "identical"), changed


def evaluate_carryover(
    repo: Path | str,
    evidence_commit: str,
    final_commit: str,
    *,
    definitions: Iterable[GateDefinition] = GATE_DEFINITIONS,
    gates: Iterable[str] | None = None,
    external_root: Path | None | str = None,
    evidence_externals: Mapping[str, Any] | None = None,
    attachments: Mapping[str, Sequence[str]] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Return the D2 carry-over receipt for ``evidence_commit`` -> ``final_commit``."""

    if external_root == "default":
        external_root = runday_root_default()
    evidence = GitTree(repo, evidence_commit)
    final = GitTree(repo, final_commit)
    selected = sorted(definitions, key=lambda d: d.gate)
    if gates is not None:
        wanted = set(gates)
        unknown = wanted - {d.gate for d in selected}
        if unknown:
            raise ValueError(f"gate_carryover_unknown_gate:{','.join(sorted(unknown))}")
        selected = [d for d in selected if d.gate in wanted]
    results: dict[str, Any] = {}
    for definition in selected:
        at_evidence = compute_gate_source_set(evidence, definition, external_root=external_root)
        at_final = compute_gate_source_set(final, definition, external_root=external_root)
        manifest_e = gate_manifest(evidence, at_evidence, definition)
        manifest_f = gate_manifest(final, at_final, definition)
        union = sorted(set(at_evidence.files) | set(at_final.files))
        evidence.prefetch(union)
        final.prefetch(union)
        changed: list[dict[str, Any]] = []
        for path in union:
            before = _blob_sha(evidence, path)
            after = _blob_sha(final, path)
            if before == after:
                continue
            status = "modified" if before and after else ("added" if after else "removed")
            changed.append({"path": path, "status": status, "evidence_sha256": before, "final_sha256": after})
        external_status, external_changed = _external_status(
            definition.gate, manifest_f["externals"], evidence_externals
        )
        changed_paths = {item["path"] for item in changed}
        generated_only = bool(changed) and changed_paths <= GENERATED_PIN_FILES
        reasons: list[str] = []
        if generated_only:
            # Owner ruling R1: a regenerated pin manifest is metadata about code, not code.
            reasons.append("generated_pins_only")
        elif changed:
            reasons.append("pinned_source_changed")
        if external_status == "changed":
            reasons.append("external_driver_changed")
        if not at_final.externals_scanned and definition.external:
            reasons.append("externals_not_scanned")
        verdict = "RERUN" if ("pinned_source_changed" in reasons or "external_driver_changed" in reasons) else "CARRY"
        results[definition.gate] = {
            "verdict": verdict,
            "reasons": reasons,
            "changed_files": changed,
            "changed_only_generated_pins": generated_only,
            "evidence_source_digest": manifest_e["source_digest"],
            "final_source_digest": manifest_f["source_digest"],
            "pinned_file_count_evidence": manifest_e["file_count"],
            "pinned_file_count_final": manifest_f["file_count"],
            "pinned_union_count": len(union),
            "missing_entries_evidence": manifest_e["missing_entries"],
            "missing_entries_final": manifest_f["missing_entries"],
            "externals_scanned": at_final.externals_scanned,
            "external_status": external_status,
            "external_changed": external_changed,
            "externals": manifest_f["externals"],
            "external_digest": manifest_f["external_digest"],
            "attachments": sorted((attachments or {}).get(definition.gate, [])),
        }
    body = {
        "protocol": RECEIPT_PROTOCOL,
        "decision_rule": DECISION_RULE,
        "claim_boundary": CLAIM_BOUNDARY,
        "repo": str(Path(repo).resolve()),
        "evidence_commit": evidence.commit,
        "evidence_tree": evidence.tree_oid,
        "final_commit": final.commit,
        "final_tree": final.tree_oid,
        "definitions_version": DEFINITIONS_VERSION,
        "definitions_digest": definitions_digest(selected),
        "tool_digests": _tool_digests(),
        "external_root": None if external_root is None else str(external_root),
        "generated_at": generated_at,
        "summary": {gate: item["verdict"] for gate, item in results.items()},
        "gates": results,
    }
    return {**body, "receipt_digest": _sha256(canonical_bytes(body))}


def verify_receipt(receipt: Mapping[str, Any]) -> str:
    if receipt.get("protocol") != RECEIPT_PROTOCOL:
        raise ValueError("gate_carryover_receipt_protocol_invalid")
    body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    digest = _sha256(canonical_bytes(body))
    if digest != receipt.get("receipt_digest"):
        raise ValueError("gate_carryover_receipt_digest_mismatch")
    for gate, item in receipt.get("gates", {}).items():
        if receipt.get("summary", {}).get(gate) != item.get("verdict"):
            raise ValueError(f"gate_carryover_receipt_summary_mismatch:{gate}")
    return digest


def _parse_attachments(values: Sequence[str] | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for value in values or ():
        gate, sep, ref = value.partition("=")
        if not sep or not gate or not ref:
            raise SystemExit(f"--attach expects GATE=PATH, got {value!r}")
        out.setdefault(gate, []).append(ref)
    return out


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--evidence", help="commit the GREEN evidence was obtained on")
    parser.add_argument("--final", help="final candidate commit")
    parser.add_argument("--verify-receipt", type=Path, help="only verify an existing receipt's digest")
    parser.add_argument("--gate", action="append", help="limit to this gate (repeatable)")
    parser.add_argument("--runday-root", type=Path, help="out-of-repo run-day root")
    parser.add_argument("--no-externals", action="store_true", help="skip out-of-repo drivers (receipt says so)")
    parser.add_argument("--evidence-externals", type=Path, help="gate_source_sets manifest captured at evidence time")
    parser.add_argument("--attach", action="append", help="GATE=PATH evidence reference recorded in the receipt")
    parser.add_argument("--output", type=Path, help="write the receipt JSON here")
    parser.add_argument("--fail-on-rerun", action="store_true", help="exit 3 when any gate is RERUN")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.verify_receipt:
        digest = verify_receipt(json.loads(args.verify_receipt.read_text()))
        print(f"receipt verified {digest}")
        return 0
    if not args.evidence or not args.final:
        parser.error("--evidence and --final are required")
    if args.no_externals:
        external: Path | None | str = None
    elif args.runday_root:
        external = args.runday_root
    else:
        external = runday_root_default()
        if external is None:
            raise SystemExit("run-day root not found: pass --runday-root or --no-externals")
    baseline = json.loads(args.evidence_externals.read_text()) if args.evidence_externals else None
    receipt = evaluate_carryover(
        args.repo,
        args.evidence,
        args.final,
        gates=args.gate,
        external_root=external,
        evidence_externals=baseline,
        attachments=_parse_attachments(args.attach),
        generated_at=_dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(f"evidence {receipt['evidence_commit']} -> final {receipt['final_commit']}")
    for gate, item in receipt["gates"].items():
        detail = ""
        if item["changed_files"]:
            names = [c["path"] for c in item["changed_files"]]
            detail = f" changed={len(names)}: " + ", ".join(names[:8]) + (" ..." if len(names) > 8 else "")
        print(f"{gate:>4} {item['verdict']:<5} pinned={item['pinned_union_count']}{detail}")
    print(f"receipt {receipt['receipt_digest']}")
    if args.fail_on_rerun and any(item["verdict"] == "RERUN" for item in receipt["gates"].values()):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
