#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bind the A0 literal verification record to one executed candidate.

The record is committed as ``prepared_not_executed``. After the final candidate's
obligations have actually run, this tool writes ``executed_on_candidate``: the
candidate commit and clean tree, and for every obligation its real result plus
the receipt's name and sha256 digest. Owner-private receipt paths never enter the
record. It refuses unless HEAD is the candidate on a clean tree, refuses partial
result sets, and records a failed obligation as failed. It never resolves owner
ruling R4 (the atomic-commit reading) and never claims A0 complete.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

RECORD_PATH = Path("docs") / "handover" / "a0-literal-verification-record.v1.json"
REPO_ROOT = Path(__file__).resolve().parents[1]
PREPARED = "prepared_not_executed"
EXECUTED = "executed_on_candidate"
RESULTS = frozenset({"passed", "failed"})
COMMIT = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PREPARED_CLAIM_BOUNDARY = (
    "Structure only. Nothing in this record has been executed on any candidate; every "
    "result is null. It does not claim A0 is complete, does not amend the A0 specification "
    "or the governing plan, adds no register row, and changes no audit, ledger, manifest or "
    "checklist."
)
PREPARED_REMAINING = (
    "freeze the final candidate and bind candidate.commit/clean_tree",
    "execute 7.4 negative repository copies on that candidate and bind results",
    "execute 7.5 three-engine browser closeout on that candidate and bind receipts",
    "bind L2 GREEN for that candidate",
    "resolve R4 and bind the closing commit",
)
EXECUTED_CLAIM_BOUNDARY = (
    "Every A0 literal obligation was executed on the one bound candidate, with its real "
    "result and the receipt's name and digest. It does not claim A0 is complete (owner "
    "ruling R4 and the closing commit remain), does not amend the A0 specification or the "
    "governing plan, adds no register row, and changes no audit, ledger, manifest or "
    "checklist."
)
EXECUTED_REMAINING = ("resolve R4 and bind the closing commit",)


def _fail(code: str) -> None:
    raise ValueError(code)


def _validate_prepared(record: Mapping[str, Any]) -> None:
    if (
        record["claim_boundary"] != PREPARED_CLAIM_BOUNDARY
        or list(record["remaining_before_complete"]) != list(PREPARED_REMAINING)
    ):
        _fail("a0_record_claim_boundary_stale")
    candidate = record["candidate"]
    if candidate["commit"] is not None or candidate["clean_tree"] is not None:
        _fail("a0_record_prepared_state_claims_a_candidate")
    for item in record["obligations"]:
        if item["executed_on_candidate"] is not False or item["result"] is not None:
            _fail("a0_record_prepared_state_claims_execution")
        if item["evidence"] is not None:
            _fail("a0_record_prepared_state_claims_execution")


def _validate_executed(record: Mapping[str, Any]) -> None:
    if (
        record["claim_boundary"] != EXECUTED_CLAIM_BOUNDARY
        or list(record["remaining_before_complete"]) != list(EXECUTED_REMAINING)
    ):
        _fail("a0_record_claim_boundary_stale")
    candidate = record["candidate"]
    if not isinstance(candidate.get("commit"), str) or not COMMIT.fullmatch(candidate["commit"]):
        _fail("a0_record_candidate_invalid")
    if candidate.get("clean_tree") is not True:
        _fail("a0_record_candidate_tree_not_clean")
    if type(candidate.get("bound_at_unix_ms")) is not int or candidate["bound_at_unix_ms"] <= 0:
        _fail("a0_record_candidate_binding_time_invalid")
    for item in record["obligations"]:
        if item["executed_on_candidate"] is not True:
            _fail("a0_record_obligation_not_executed")
        if item["result"] not in RESULTS:
            _fail("a0_record_result_invalid")
        evidence = item["evidence"]
        if (
            not isinstance(evidence, Mapping)
            or set(evidence) != {"receipt", "digest"}
            or not isinstance(evidence["receipt"], str)
            or not evidence["receipt"]
            or "/" in evidence["receipt"]
            or not isinstance(evidence["digest"], str)
            or not DIGEST.fullmatch(evidence["digest"])
        ):
            _fail("a0_record_evidence_invalid")


def validate_record(record: Mapping[str, Any]) -> None:
    """Admit exactly the prepared structure or a fully executed, candidate-bound record."""

    if record.get("state") == PREPARED:
        _validate_prepared(record)
    elif record.get("state") == EXECUTED:
        _validate_executed(record)
    else:
        _fail("a0_record_state_invalid")
    atomic = record["atomic_commit"]
    if (atomic["reading"] is None) != (atomic["commit"] is None):
        _fail("a0_record_atomic_commit_half_bound")


def prepared_form(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return the prepared structure of a record in either state (for re-checking the fill)."""

    prepared = copy.deepcopy(dict(record))
    prepared["state"] = PREPARED
    prepared["claim_boundary"] = PREPARED_CLAIM_BOUNDARY
    prepared["remaining_before_complete"] = list(PREPARED_REMAINING)
    prepared["candidate"] = {
        **prepared["candidate"],
        "commit": None,
        "clean_tree": None,
        "bound_at_unix_ms": None,
    }
    for item in prepared["obligations"]:
        item["executed_on_candidate"] = False
        item["result"] = None
        item["evidence"] = None
    return prepared


def fill_record(
    record: Mapping[str, Any],
    *,
    candidate: object,
    results: Mapping[str, Mapping[str, Any]],
    bound_at_unix_ms: int,
) -> dict[str, Any]:
    if record.get("state") != PREPARED:
        _fail("a0_record_not_prepared")
    if not isinstance(candidate, str) or not COMMIT.fullmatch(candidate):
        _fail("a0_record_candidate_invalid")
    obligation_ids = [item["obligation_id"] for item in record["obligations"]]
    if set(results) != set(obligation_ids):
        _fail("a0_record_results_incomplete")
    filled = copy.deepcopy(dict(record))
    filled["state"] = EXECUTED
    filled["claim_boundary"] = EXECUTED_CLAIM_BOUNDARY
    filled["remaining_before_complete"] = list(EXECUTED_REMAINING)
    filled["candidate"] = {
        **filled["candidate"],
        "commit": candidate,
        "clean_tree": True,
        "bound_at_unix_ms": bound_at_unix_ms,
    }
    for item in filled["obligations"]:
        entry = results[item["obligation_id"]]
        if entry.get("result") not in RESULTS:
            _fail("a0_record_result_invalid")
        receipt = Path(str(entry.get("evidence", "")))
        if not receipt.is_file():
            _fail("a0_record_evidence_missing")
        item["executed_on_candidate"] = True
        item["result"] = entry["result"]
        item["evidence"] = {
            "receipt": receipt.name,
            "digest": "sha256:" + hashlib.sha256(receipt.read_bytes()).hexdigest(),
        }
    validate_record(filled)
    return filled


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args], check=True, capture_output=True, text=True
    ).stdout


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, help="the final candidate commit (40 hex)")
    parser.add_argument(
        "--results",
        type=Path,
        required=True,
        help="JSON: {obligation_id: {result: passed|failed, evidence: <receipt path>}}",
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        head = _git(args.repo_root, "rev-parse", "HEAD").strip()
        dirty = _git(args.repo_root, "status", "--porcelain").strip()
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"a0_record_git_unavailable: {error}", file=sys.stderr)
        return 2
    if head != args.candidate:
        print("a0_record_head_is_not_the_candidate", file=sys.stderr)
        return 2
    if dirty:
        print("a0_record_tree_not_clean", file=sys.stderr)
        return 2
    record_file = args.repo_root / RECORD_PATH
    try:
        record = json.loads(record_file.read_text("utf-8"))
        results = json.loads(args.results.read_text("utf-8"))
        filled = fill_record(
            record,
            candidate=args.candidate,
            results=results,
            bound_at_unix_ms=int(time.time() * 1000),
        )
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    record_file.write_text(json.dumps(filled, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"record": str(RECORD_PATH), "state": EXECUTED, "candidate": args.candidate}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
