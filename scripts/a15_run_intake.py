#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded A15 executed-artifact intake CLI over REAL retained artifacts.

Non-authoritative. Reads the declared artifact bytes, verifies declared digests,
protocols, source binding, provenance class and evidence expiry, and writes an
explicitly not-ready report naming missing mandatory evidence and missing verifier
contracts. It never emits a readiness or completion claim and never writes to the
artifacts it reads.

Exit codes
----------
0  intake executed and a report was produced (the report may -- and for A15 does --
   contain findings; findings are the deliverable, not a failure of the tool)
2  the intake refused to run (fail closed): a declaration/shape error, a declaration
   that cannot be opened, or an evaluation instant that is absent/naive/non-UTC or not
   ISO-8601. No report is written and no claim is emitted on this path.
3  report could not be written

Usage
-----
  python3 -B scripts/a15_run_intake.py --declaration <path> --now <iso8601Z> --report <path>
  python3 -B scripts/a15_run_intake.py --run-dir <dir> --repo-root <dir> --report <path>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mycelium_a15_run_intake import (  # noqa: E402
    CLAIM_BOUNDARY,
    PROTOCOL,
    intake_run,
    load_declaration,
)

DEFAULT_RUN_DIR = (
    os.path.expanduser("~/Documents/mycelium-core-program/runs/wp8-02-observational-p84r1")
)
DEFAULT_EVIDENCE_ROOTS = (os.path.expanduser("~/mycelium-physical-run"),)
DEFAULT_SOURCE_COMMIT = "fb0314cf1cc5a57d14a2096226878267506b27f6"
DEFAULT_ARTIFACTS = (
    {
        "artifact_id": "readiness_gate",
        "path": "readiness-gate.json",
        "declared_digest": "sha256:a9983be6aeed2c8c99f214aa034e6f201759156e0a41546d06996abbf4af95d5",
        "protocol": "mycelium.pilot_readiness_gate.v1",
        "provenance": "live",
        "required": True,
        "fresh_until": None,
    },
    {
        "artifact_id": "package",
        "path": "PACKAGE.json",
        "declared_digest": "sha256:79adcfaf9355ff140873b571d121f137681eed56ed070c1e5ecfe098417ca22c",
        "protocol": "mycelium.pilot_readiness_gate.package.v1",
        "provenance": "live",
        "required": True,
        "fresh_until": None,
    },
    {
        "artifact_id": "gate_execution",
        "path": "gate-execution.json",
        "declared_digest": "sha256:d9e3b6fcd302ba9428556801a2469dfb645b5823bf41e73980c7cd8a28ed3d0a",
        "protocol": None,
        "provenance": "live",
        "required": True,
        "fresh_until": None,
    },
    {
        "artifact_id": "hypothesis",
        "path": "HYPOTHESIS.md",
        "declared_digest": "sha256:2a76aa59a83c0c6a0a5d1ce5a438a52987af80a00534fd9bb215f1418bba5bff",
        "protocol": None,
        "provenance": "source",
        "required": True,
        "fresh_until": None,
    },
)
DEFAULT_COMPLETION_RECORDS = (
    {"gate_id": "A3", "path": "docs/handover/a3-completion-record.v1.json"},
    {"gate_id": "A4", "path": "docs/handover/a4-completion-record.v1.json"},
    {"gate_id": "A5", "path": "docs/handover/a5-completion-record.v1.json"},
    {"gate_id": "A8", "path": "docs/handover/a8-completion-record.v1.json"},
)
DEFAULT_CHECKLIST = {
    "path": "docs/handover/mycelium-completion-checklist.v1.json",
    "declared_digest": "sha256:9aa2614696a2925eb0305713233393fe1491dfc17f2c609e21a4ca3ab48b7a0c",
}


def builtin_declaration(run_dir: str, repo_root: str, evidence_roots: list[str]) -> dict:
    return {
        "protocol": PROTOCOL,
        "claim_boundary": CLAIM_BOUNDARY,
        "run_dir": run_dir,
        "evidence_roots": evidence_roots,
        "repo_root": repo_root,
        "source": {"commit": DEFAULT_SOURCE_COMMIT, "branch": "integration/core-pilot"},
        "artifacts": [dict(item) for item in DEFAULT_ARTIFACTS],
        "completion_records": [dict(item) for item in DEFAULT_COMPLETION_RECORDS],
        "checklist": dict(DEFAULT_CHECKLIST),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--declaration", help="path to an intake declaration JSON document")
    parser.add_argument(
        "--run-dir",
        default=os.environ.get("MYCELIUM_A15_RUN_DIR", DEFAULT_RUN_DIR),
        help="retained run directory (used when --declaration is not given)",
    )
    parser.add_argument(
        "--repo-root", help="audited repository/worktree root (default: this script's checkout)"
    )
    parser.add_argument(
        "--evidence-root",
        action="append",
        default=None,
        help="additional off-repo evidence root (repeatable)",
    )
    parser.add_argument("--now", help="evaluation instant, ISO-8601 with Z (default: current UTC)")
    parser.add_argument("--report", help="write the intake report JSON to this path")
    return parser.parse_args(argv)


def parse_now(text: str) -> datetime:
    """Parse an ISO-8601 evaluation instant, refusing anything that is not UTC.

    Every refusal is the same ``ValueError`` token ``intake_run`` would raise, so the
    CLI always exits 2 (never a raw traceback) on an unparseable or non-UTC ``--now``.
    """
    token = "a15_run_intake_now_must_be_utc"
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(token) from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(token)
    return parsed


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    repo_root = args.repo_root or str(Path(__file__).resolve().parents[1])
    if args.evidence_root is not None:
        evidence_roots = list(args.evidence_root)
    else:
        env_roots = os.environ.get("MYCELIUM_A15_EVIDENCE_ROOTS", "")
        evidence_roots = (
            [item for item in env_roots.split(os.pathsep) if item]
            if env_roots
            else list(DEFAULT_EVIDENCE_ROOTS)
        )
    evidence_roots = [item for item in evidence_roots if item]

    try:
        now = parse_now(args.now) if args.now else datetime.now(timezone.utc)
        if args.declaration:
            declaration = load_declaration(args.declaration)
            declaration_for_report = json.loads(
                Path(args.declaration).read_bytes().decode("utf-8")
            )
        else:
            declaration = builtin_declaration(args.run_dir, repo_root, evidence_roots)
            declaration_for_report = declaration
        report = intake_run(declaration, now=now, repo_root=repo_root)
    except OSError as exc:
        # An unreadable declaration or retained input is a refusal, not a crash: same
        # fail-closed exit code as any other refused input, no report and no claim.
        print(
            json.dumps(
                {
                    "error": "a15_run_intake_input_unreadable",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "intake_refused": True,
                }
            ),
            file=sys.stderr,
        )
        return 2
    except ValueError as exc:
        print(json.dumps({"error": str(exc), "intake_refused": True}), file=sys.stderr)
        return 2

    report["declaration"] = declaration_for_report
    if args.report:
        try:
            Path(args.report).parent.mkdir(parents=True, exist_ok=True)
            Path(args.report).write_text(
                json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            print(json.dumps({"error": str(exc), "report_write_failed": True}), file=sys.stderr)
            return 3

    summary = {
        "protocol": report["protocol"],
        "authoritative": report["authoritative"],
        "production_verifier": report["production_verifier"],
        "release_ready": report["release_ready"],
        "gate_state": report["gate_state"],
        "intake_ok": report["intake_ok"],
        "finding_count": len(report["findings"]),
        "findings": report["findings"],
        "missing_evidence_count": report["missing_evidence_count"],
        "missing_verifier_contract_count": len(report["missing_verifier_contracts"]),
        "report_digest": report["report_digest"],
        "report_path": args.report,
    }
    print(json.dumps(summary, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
