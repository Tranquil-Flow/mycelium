#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Validate one source-owned A6 physical recovery observation.

The operator plan supplies placement and run identity only.  It cannot supply a
passing assertion or an executable.  A live product/fault controller writes one
closed production-observation envelope below the physical runner's private
evidence directory; this runner independently validates that envelope and
derives the privacy-reduced case record consumed by ``run_a6_recovery_gate``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from mycelium_physical_runner import load_operator_plan

EXACT_FLEET_GRANT = "FLEET GRANTED TO A6"
OBSERVATION_PROTOCOL = "mycelium.a6_source_owned_case_observation.v1"
RECORD_PROTOCOL = "mycelium.a6_physical_recovery_case.v1"
AUTHORITY = "ordinary_product_gateway_and_router"
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}$")

REQUIRED_CASES = (
    "ordinary_browser_source_failure_recovery",
    "incompatible_successor",
    "stale_qualification",
    "foreign_qualification",
    "unexecuted_qualification",
    "mismatched_path_attempt",
    "no_successor",
    "successor_prefill_failure",
    "cancellation_before_recovery",
    "cancellation_during_prefill",
    "timeout_during_recovery",
    "exhausted_recovery_attempts",
    "stale_source_output_after_fencing",
    "cleanup_reservation_release",
    "cleanup_authority_failure",
    "shutdown_during_recovery",
)
REQUIRED_CLEANUP_CHECKS = (
    "local_product_port",
    "physical_processes",
    "reservations",
)

_POSITIVE_FIELDS = frozenset(
    {
        "case_id",
        "executed",
        "ordinary_product_path",
        "browser_originated",
        "private_material_exposed",
        "source_failure_observed",
        "checkpoint_frozen",
        "successor_qualified",
        "source_fenced",
        "successor_prefill",
        "publication_generation_cas_count",
        "duplicated_token_count",
        "omitted_committed_token_count",
        "late_source_publication_count",
        "logical_stream_count",
        "terminal_event_count",
        "terminal_state",
        "process_cleanup_complete",
        "stale_reservation_count",
    }
)
_CLEANUP_AUTHORITY_FIELDS = frozenset(
    {
        "case_id",
        "executed",
        "ordinary_product_path",
        "private_material_exposed",
        "rejection_observed",
        "fail_closed",
        "terminal_state",
        "terminal_event_count",
        "unpublishable_suffix_discarded",
        "process_cleanup_complete",
        "stale_reservation_count",
    }
)
_NEGATIVE_FIELDS = frozenset(
    {
        "case_id",
        "executed",
        "ordinary_product_path",
        "private_material_exposed",
        "rejection_observed",
        "fail_closed",
        "terminal_state",
        "terminal_event_count",
        "process_cleanup_complete",
        "stale_reservation_count",
    }
)
_FAIL_CLOSED_TERMINAL_STATES = frozenset(
    {"aborted", "cancelled", "timed_out", "shutdown", "TERMINAL_BLOCKED"}
)
# Owner ruling (2026-10-05): the late old-attempt output of the fenced source is
# "rejected without advancing the stream" (spec section 6); the request itself
# may still complete on its successor. For this case only, ``fail_closed``
# means the late source publication was refused and ``completed`` is admitted.
_NEGATIVE_TERMINAL_STATES = {
    "stale_source_output_after_fencing": _FAIL_CLOSED_TERMINAL_STATES | {"completed"},
}
_CLEANUP_RELEASE_FIELDS = frozenset(
    {
        "case_id",
        "executed",
        "ordinary_product_path",
        "private_material_exposed",
        "release_observed",
        "release_count",
        "terminal_event_count",
        "process_cleanup_complete",
        "stale_reservation_count",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "protocol",
        "authority",
        "case_id",
        "bindings",
        "observation",
        "observation_digest",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "source_digest",
        "spec_digest",
        "contract_digest",
        "operator_plan_digest",
        "run_id",
        "deployment_id",
        "deployment_epoch",
        "request_id",
        "source_path_id",
        "source_attempt",
        "source_path_attempt",
        "source_path_generation",
        "successor_path_id",
        "successor_attempt",
        "successor_path_attempt",
        "successor_path_generation",
        "successor_qualification_generation",
        "committed_token_count",
        "source_absolute_deadline_unix_ms",
        "successor_absolute_deadline_unix_ms",
    }
)
_PRIVATE_MARKERS = (
    "encoded_prompt_token_ids",
    "committed_prefix_tokens",
    "checkpoint_path",
    "node_secret",
    "ssh_identity",
    "private_key",
)


def _fail(code: str) -> None:
    raise ValueError(code)


def _literal_bool(value: object) -> bool:
    return type(value) is bool


def _nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _plain_mapping(value: object, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail("physical_observation_invalid")
    return dict(value)


def _base_observation_valid(value: Mapping[str, Any], case_id: str) -> bool:
    return (
        value.get("case_id") == case_id
        and value.get("executed") is True
        and value.get("ordinary_product_path") is True
        and value.get("private_material_exposed") is False
        and value.get("process_cleanup_complete") is True
        and value.get("stale_reservation_count") == 0
        and _nonnegative_int(value.get("terminal_event_count"))
    )


def validate_case_observation(
    case_id: str, observation: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate one exact case shape and derive success from observations.

    Deliberately no ``result`` or ``passed`` field is accepted.  A caller must
    prove the case-specific facts; this function derives acceptance from them.
    """

    if case_id not in REQUIRED_CASES:
        _fail("physical_observation_invalid")
    if case_id == "ordinary_browser_source_failure_recovery":
        value = _plain_mapping(observation, _POSITIVE_FIELDS)
        if not _base_observation_valid(value, case_id):
            _fail("physical_observation_invalid")
        if not all(
            value.get(field) is True
            for field in (
                "browser_originated",
                "source_failure_observed",
                "checkpoint_frozen",
                "successor_qualified",
                "source_fenced",
                "successor_prefill",
            )
        ):
            _fail("physical_observation_invalid")
        if (
            value.get("publication_generation_cas_count") != 1
            or value.get("duplicated_token_count") != 0
            or value.get("omitted_committed_token_count") != 0
            or value.get("late_source_publication_count") != 0
            or value.get("logical_stream_count") != 1
            or value.get("terminal_event_count") != 1
            or value.get("terminal_state") != "completed"
        ):
            _fail("physical_observation_invalid")
        return value

    if case_id == "cleanup_authority_failure":
        value = _plain_mapping(observation, _CLEANUP_AUTHORITY_FIELDS)
        if not _base_observation_valid(value, case_id):
            _fail("physical_observation_invalid")
        if (
            value.get("rejection_observed") is not True
            or value.get("fail_closed") is not True
            or value.get("terminal_state") != "TERMINAL_BLOCKED"
            or value.get("terminal_event_count") != 0
            or value.get("unpublishable_suffix_discarded") is not True
        ):
            _fail("physical_observation_invalid")
        return value

    if case_id == "cleanup_reservation_release":
        value = _plain_mapping(observation, _CLEANUP_RELEASE_FIELDS)
        if not _base_observation_valid(value, case_id):
            _fail("physical_observation_invalid")
        if value.get("release_observed") is not True or not _positive_int(
            value.get("release_count")
        ):
            _fail("physical_observation_invalid")
        return value

    value = _plain_mapping(observation, _NEGATIVE_FIELDS)
    if not _base_observation_valid(value, case_id):
        _fail("physical_observation_invalid")
    if value.get("rejection_observed") is not True or value.get("fail_closed") is not True:
        _fail("physical_observation_invalid")
    if value.get("terminal_state") not in _NEGATIVE_TERMINAL_STATES.get(
        case_id, _FAIL_CLOSED_TERMINAL_STATES
    ):
        _fail("physical_observation_invalid")
    if value.get("terminal_event_count") not in {0, 1}:
        _fail("physical_observation_invalid")
    return value


def _validate_bindings(value: object, *, positive: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _BINDING_FIELDS:
        _fail("physical_observation_binding_invalid")
    result = dict(value)
    for field in (
        "source_digest",
        "spec_digest",
        "contract_digest",
        "operator_plan_digest",
    ):
        if not isinstance(result[field], str) or DIGEST.fullmatch(result[field]) is None:
            _fail("physical_observation_binding_invalid")
    for field in (
        "run_id",
        "deployment_id",
        "request_id",
        "source_path_id",
    ):
        if not isinstance(result[field], str) or IDENTIFIER.fullmatch(result[field]) is None:
            _fail("physical_observation_binding_invalid")
    for field in (
        "deployment_epoch",
        "source_attempt",
        "source_path_generation",
        "source_absolute_deadline_unix_ms",
    ):
        if not _positive_int(result[field]):
            _fail("physical_observation_binding_invalid")
    if not _nonnegative_int(result["source_path_attempt"]):
        _fail("physical_observation_binding_invalid")
    successor_fields = (
        "successor_path_id",
        "successor_attempt",
        "successor_path_attempt",
        "successor_path_generation",
        "successor_qualification_generation",
        "successor_absolute_deadline_unix_ms",
    )
    if positive:
        if (
            not isinstance(result["successor_path_id"], str)
            or IDENTIFIER.fullmatch(result["successor_path_id"]) is None
            or not all(_positive_int(result[field]) for field in successor_fields[1:])
            or result["successor_attempt"] <= result["source_attempt"]
            or result["successor_path_generation"] <= result["source_path_generation"]
            or result["successor_absolute_deadline_unix_ms"]
            != result["source_absolute_deadline_unix_ms"]
            or not _positive_int(result["committed_token_count"])
        ):
            _fail("physical_observation_binding_invalid")
    else:
        for field in successor_fields:
            item = result[field]
            if item is not None and not (
                isinstance(item, str) and field == "successor_path_id" and IDENTIFIER.fullmatch(item)
            ) and not (field != "successor_path_id" and _nonnegative_int(item)):
                _fail("physical_observation_binding_invalid")
        if not _nonnegative_int(result["committed_token_count"]):
            _fail("physical_observation_binding_invalid")
    return result


def validate_source_owned_observation(
    document: Mapping[str, Any], *, case_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(document, Mapping) or set(document) != _OBSERVATION_FIELDS:
        _fail("physical_source_observation_invalid")
    value = dict(document)
    if (
        value["protocol"] != OBSERVATION_PROTOCOL
        or value["authority"] != AUTHORITY
        or value["case_id"] != case_id
    ):
        _fail("physical_source_observation_invalid")
    body = dict(value)
    actual_digest = body.pop("observation_digest")
    if not isinstance(actual_digest, str) or _sha256(_canonical(body)) != actual_digest:
        _fail("physical_source_observation_invalid")
    serialized = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if any(marker in serialized for marker in _PRIVATE_MARKERS):
        _fail("physical_source_observation_private_material")
    observation = validate_case_observation(case_id, value["observation"])
    bindings = _validate_bindings(
        value["bindings"],
        positive=case_id == "ordinary_browser_source_failure_recovery",
    )
    return bindings, observation


def _read_private_json(path: Path) -> dict[str, Any]:
    root = path.parent
    root_info = root.stat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_IMODE(root_info.st_mode) != 0o700
        or root_info.st_uid != os.getuid()
    ):
        _fail("physical_source_observation_root_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or info.st_size <= 0
            or info.st_size > 1024 * 1024
        ):
            _fail("physical_source_observation_file_invalid")
        raw = os.read(fd, info.st_size + 1)
    finally:
        os.close(fd)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("physical_source_observation_invalid") from exc
    if not isinstance(value, dict):
        _fail("physical_source_observation_invalid")
    return value


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    root = path.parent
    info = root.stat()
    if stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.getuid():
        _fail("physical_case_evidence_root_invalid")
    payload = json.dumps(value, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    temporary = root / f".{path.name}.tmp-{os.getpid()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def _operator_plan_digest(path: Path) -> str:
    return _sha256(path.read_bytes())


def _case_main(case_id: str, operator_plan: Path) -> int:
    config = load_operator_plan(operator_plan)
    source = Path(config.evidence_output_dir) / "a6-production-observations" / f"{case_id}.json"
    bindings, observation = validate_source_owned_observation(
        _read_private_json(source), case_id=case_id
    )
    expected_plan_digest = _operator_plan_digest(operator_plan)
    if bindings["operator_plan_digest"] != expected_plan_digest or bindings["run_id"] != config.run_id:
        _fail("physical_source_observation_plan_binding_mismatch")
    output_raw = os.environ.get("MYCELIUM_A6_CASE_EVIDENCE_FILE")
    if not output_raw or not Path(output_raw).is_absolute():
        _fail("physical_case_evidence_path_required")
    output = Path(output_raw)
    record = {
        "protocol": RECORD_PROTOCOL,
        "case_id": case_id,
        "executed": True,
        "result": "passed",
        "source_digest": bindings["source_digest"],
        "spec_digest": bindings["spec_digest"],
        "contract_digest": bindings["contract_digest"],
        "run_id": bindings["run_id"],
        "deployment_id": bindings["deployment_id"],
        "request_id": bindings["request_id"],
        "observation_digest": _sha256(_canonical(observation)),
    }
    _write_private_json(output, record)
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def _cleanup_main(check_id: str, operator_plan: Path) -> int:
    config = load_operator_plan(operator_plan)
    path = Path(config.evidence_output_dir) / "a6-production-observations" / "cleanup.json"
    value = _read_private_json(path)
    expected = {
        "protocol",
        "authority",
        "run_id",
        "local_product_port_listener_count",
        "physical_process_count",
        "reservation_count",
    }
    if (
        set(value) != expected
        or value.get("protocol") != "mycelium.a6_source_owned_cleanup_observation.v1"
        or value.get("authority") != AUTHORITY
        or value.get("run_id") != config.run_id
    ):
        _fail("physical_cleanup_observation_invalid")
    field = {
        "local_product_port": "local_product_port_listener_count",
        "physical_processes": "physical_process_count",
        "reservations": "reservation_count",
    }[check_id]
    if value.get(field) != 0:
        _fail(f"physical_cleanup_failed:{check_id}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--case", choices=REQUIRED_CASES)
    action.add_argument("--cleanup-check", choices=REQUIRED_CLEANUP_CHECKS)
    parser.add_argument("--operator-plan", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    # Authorization is checked before plan or evidence access.
    if os.environ.get("MYCELIUM_A6_FLEET_GRANT") != EXACT_FLEET_GRANT:
        print("explicit_fleet_grant_required", file=sys.stderr)
        return 2
    args = _parser().parse_args(argv)
    try:
        if args.case is not None:
            return _case_main(args.case, args.operator_plan)
        return _cleanup_main(args.cleanup_check, args.operator_plan)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
