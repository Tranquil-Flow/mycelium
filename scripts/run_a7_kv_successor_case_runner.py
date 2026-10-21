#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""A7 KV-successor physical case runner.

The A7 gate driver invokes this script once per frozen case.  The runner
performs the closed-record contract checks and enforces the owner fleet grant
before any physical action can run.  Without the grant it emits a fail-closed
refusal record that the offline gate evaluator must reject.  With the grant it
hands the case to the owner-authorised physical measurement plug-in
(``mycelium_a7_physical_measurement.measure_case``, configured by the file
named in ``MYCELIUM_A7_PHYSICAL_CONFIG``); a measured record is written only
from what that plug-in observed, and any plug-in refusal becomes a refusal
record with the plug-in's stable reason and a non-zero exit.  The plug-in's
privacy-reduced observations are written beside the record as
``<case>.measurement.json``.

The gate's case record schema is closed by ``run_a7_kv_successor_gate._CASE_FIELDS``.
The owner-accepted known limit is therefore written beside the record as
``<case>.note.json`` instead of extending the digest-bound case body.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, NoReturn, cast

_GATE_PATH = Path(__file__).resolve().with_name("run_a7_kv_successor_gate.py")
_GATE_SPEC = importlib.util.spec_from_file_location("run_a7_kv_successor_gate", _GATE_PATH)
if _GATE_SPEC is None or _GATE_SPEC.loader is None:  # pragma: no cover - importlib defensive guard
    raise RuntimeError("a7_case_runner_gate_import_failed")
gate = importlib.util.module_from_spec(_GATE_SPEC)
_GATE_SPEC.loader.exec_module(gate)

FLEET_GRANT_ENV = "MYCELIUM_A7_FLEET_GRANT"
FLEET_GRANT_VALUE = gate.EXACT_FLEET_GRANT
PHYSICAL_NOT_IMPLEMENTED = "physical_measurement_runner_not_implemented"
PHYSICAL_MEASUREMENT = "physical_measurement"
MEASUREMENT_CONFIG_ENV = "MYCELIUM_A7_PHYSICAL_CONFIG"
_BINDING_FIELDS = ("protocol", "case", "candidate_commit", "plan_digest", "evidence_digest")
FLEET_GRANT_MISSING = "fleet_grant_missing"
KNOWN_LIMIT = (
    "a7 completion-step residual race; ACCEPTED 2026-10-02; "
    "mitigate via a7_source_lost accept-first. See "
    "runs/v2/pm-a7/evidence-known-limit/completion-step-residual-race.md."
)
NOTE_PROTOCOL = "mycelium.a7_physical_case_note.v1"
_STRUCTURAL_FAILURES = {
    "record_fields",
    "record_protocol_or_case",
    "record_digest",
    "record_not_bound_to_plan",
    "record_counts_invalid",
    "record_measurements_invalid",
}


class ContractError(ValueError):
    """Plan, filesystem, or case-contract validation failed before measurement."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _read_json(path: Path) -> Any:
    with path.open("rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _write_private(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_canonical(document) + b"\n")


def _note_path(record_out: Path) -> Path:
    if record_out.suffix == ".json":
        return record_out.with_name(f"{record_out.stem}.note.json")
    return record_out.with_name(f"{record_out.name}.note.json")


def _fail(code: str) -> NoReturn:
    raise ContractError(code)


def _verify_case(case: str) -> None:
    if case not in gate.CASES:
        _fail("a7_case_runner_unknown_case")


def _verify_output_paths(record_out: Path) -> Path:
    if record_out.exists() or record_out.is_symlink():
        _fail("a7_case_runner_record_exists")
    note = _note_path(record_out)
    if note.exists() or note.is_symlink():
        _fail("a7_case_runner_note_exists")
    return note


def _verify_plan_file(plan_path: Path) -> dict[str, Any]:
    if not plan_path.is_file() or plan_path.is_symlink():
        _fail("a7_case_runner_plan_missing")
    try:
        verified = gate._verify_plan(_read_json(plan_path))
    except ValueError as exc:
        _fail(str(exc) or "a7_case_runner_plan_invalid")
    if not isinstance(verified, Mapping):
        _fail("a7_case_runner_plan_invalid")
    plan = dict(cast(Mapping[str, Any], verified))

    # Bind to the gate driver's current frozen plan generator, not merely to a
    # syntactically self-consistent JSON object.
    try:
        expected = gate.build_plan(plan["candidate_commit"])
    except ValueError as exc:
        _fail(str(exc) or "a7_case_runner_candidate_invalid")
    if plan.get("plan_digest") != expected["plan_digest"]:
        _fail("a7_case_runner_plan_digest_not_cached")

    _read_every_threshold(plan)
    return plan


def _read_every_threshold(plan: Mapping[str, Any]) -> dict[str, Any]:
    thresholds = plan.get("thresholds")
    if not isinstance(thresholds, Mapping):
        _fail("a7_case_runner_thresholds_missing")
    if set(thresholds) != set(gate.THRESHOLDS):
        _fail("a7_case_runner_thresholds_incomplete")
    # Deliberately touch each frozen value before constructing a record.  The
    # current shell does not consume the numbers for measurement yet, but this
    # keeps the runner coupled to the same closed contract as the gate.
    return {name: thresholds[name] for name in gate.THRESHOLDS}


def _refusal_record(plan: Mapping[str, Any], case: str, reason: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "protocol": gate.CASE_PROTOCOL,
        "case": case,
        "candidate_commit": plan["candidate_commit"],
        "plan_digest": plan["plan_digest"],
        "ordinary_browser_request": True,
        "fault": "source_process_kill",
        "outcome": "unknown",
        "reason": reason,
        "kv_continuity_claimed": False,
        "committed_before_failure": 0,
        "acknowledged_before_failure": 0,
        "successor_recovery_prefill_operations": 0,
        "prefix_tokens_sent_to_successor": 0,
        "reference_parity": False,
        "duplicate_tokens": 0,
        "missing_tokens": 0,
        "publishers": 0,
        "terminal_results": 0,
        "successor_decode_operations": 0,
        "stale_source_publications_accepted": 0,
        "interruption_ms": 0.0,
        "cutover_ms": 0.0,
        "checkpoint_bytes_total": 0,
        "checkpoint_count": 0,
        "max_ack_wait_ms": 0.0,
        "baseline_tpot_ms": 0.0,
        "candidate_tpot_ms": 0.0,
        "cleanup": {},
        "workspaces_checked": [],
    }
    record = {**body, "evidence_digest": gate._sha(body)}
    _validate_record_contract(record, plan)
    return record


def _validate_record_contract(record: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    if set(record) != set(gate._CASE_FIELDS):
        _fail("a7_case_runner_record_fields")
    body = {key: value for key, value in record.items() if key != "evidence_digest"}
    if record.get("evidence_digest") != gate._sha(body):
        _fail("a7_case_runner_record_digest")
    failures = gate.evaluate_case(record, plan)
    if any(failure in _STRUCTURAL_FAILURES for failure in failures):
        _fail("a7_case_runner_record_contract")


def _known_limit_note(plan: Mapping[str, Any], case: str, record: Mapping[str, Any]) -> dict[str, Any]:
    body = {
        "protocol": NOTE_PROTOCOL,
        "case": case,
        "candidate_commit": plan["candidate_commit"],
        "plan_digest": plan["plan_digest"],
        "record_evidence_digest": record["evidence_digest"],
        "known_limit": KNOWN_LIMIT,
        "claim_boundary": (
            "Known-limit metadata is intentionally outside the gate's closed "
            "_CASE_FIELDS body; extending the case record would fail protocol equality."
        ),
    }
    return {**body, "note_digest": gate._sha(body)}


def _physical_exit_reason() -> tuple[str, int]:
    if os.environ.get(FLEET_GRANT_ENV) != FLEET_GRANT_VALUE:
        # Direct case-runner invocations may still need a closed refusal record
        # for offline evaluation.  The gate coordinator itself refuses earlier
        # when this grant is absent, so no physical action can occur.
        return FLEET_GRANT_MISSING, 0
    # The owner-authorised physical measurement plug-in (authorised
    # 2026-10-03): main() hands the case to it; every refusal it raises is
    # still written as a closed refusal record with a non-zero exit.
    return PHYSICAL_MEASUREMENT, 0


def _measurement_path(record_out: Path) -> Path:
    if record_out.suffix == ".json":
        return record_out.with_name(f"{record_out.stem}.measurement.json")
    return record_out.with_name(f"{record_out.name}.measurement.json")


def _measured_record(plan: Mapping[str, Any], case: str, fields: Mapping[str, Any]) -> dict[str, Any]:
    """Bind the plug-in's measured fields to the plan and validate the closed contract."""

    if set(fields) | set(_BINDING_FIELDS) != set(gate._CASE_FIELDS) or set(fields) & set(_BINDING_FIELDS):
        _fail("a7_case_runner_measurement_fields")
    body: dict[str, Any] = {
        "protocol": gate.CASE_PROTOCOL,
        "case": case,
        "candidate_commit": plan["candidate_commit"],
        "plan_digest": plan["plan_digest"],
        **fields,
    }
    record = {**body, "evidence_digest": gate._sha(body)}
    _validate_record_contract(record, plan)
    return record


def _physical_measurement(plan: Mapping[str, Any], case: str, record_out: Path) -> tuple[str, int, dict[str, Any] | None]:
    """Run the plug-in; returns (reason, exit code, measured record or None)."""

    observations_path = _measurement_path(record_out)
    if observations_path.exists() or observations_path.is_symlink():
        _fail("a7_case_runner_measurement_exists")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from mycelium_a7_physical_measurement import MeasurementRefused, measure_case

    try:
        fields, observations = measure_case(
            plan=plan,
            case=case,
            grant=os.environ.get(FLEET_GRANT_ENV),
            config_path=os.environ.get(MEASUREMENT_CONFIG_ENV),
        )
    except MeasurementRefused as refused:
        if refused.observations is not None:
            _write_private(observations_path, {"case": case, "refusal": refused.code, **refused.observations})
        return refused.code, 1, None
    record = _measured_record(plan, case, fields)
    _write_private(observations_path, {"case": case, "record_evidence_digest": record["evidence_digest"], **observations})
    return "measured", 0, record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--record-out", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        _verify_case(args.case)
        note_path = _verify_output_paths(args.record_out)
        plan = _verify_plan_file(args.plan)
        reason, exit_code = _physical_exit_reason()
        measured = None
        if reason == PHYSICAL_MEASUREMENT:
            reason, exit_code, measured = _physical_measurement(plan, args.case, args.record_out)
        record = measured if measured is not None else _refusal_record(plan, args.case, reason)
        note = _known_limit_note(plan, args.case, record)
        _write_private(note_path, note)
        _write_private(args.record_out, record)
        print(json.dumps({"case": args.case, "record": str(args.record_out), "reason": reason}, sort_keys=True))
        return exit_code
    except (ContractError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
