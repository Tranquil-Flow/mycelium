#!/usr/bin/env python3
"""Produce and verify unsigned A5 interrupted-attempt recovery accounting."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "mycelium.a5_interrupted_attempt_reconciliation.v1"
INPUT_PROTOCOL = "mycelium.a5_interrupted_attempt_recovery_operator_input.v1"
VERIFY_PROTOCOL = "mycelium.a5_interrupted_attempt_reconciliation_verification.v1"
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
GIT_RE = re.compile(r"[0-9a-f]{40}")
CYCLE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
MAXIMUM_INPUT_BYTES = 16 * 1024 * 1024


class RecoveryError(RuntimeError):
    """Stable fail-closed recovery producer/verifier rejection."""


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise RecoveryError("duplicate_json_key")
        document[key] = value
    return document


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise RecoveryError("schema_invalid") from error


def _sha_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_regular_bytes(
    path: Path,
    *,
    missing_reason: str,
    allow_empty: bool = False,
) -> tuple[bytes, os.stat_result]:
    named = _regular_metadata(path, missing_reason=missing_reason, allow_empty=allow_empty)
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise RecoveryError(missing_reason) from error
    except OSError as error:
        raise RecoveryError("input_not_regular") from error
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != named.st_dev
            or opened.st_ino != named.st_ino
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_size != named.st_size
        ):
            raise RecoveryError("bound_artifact_drift")
        chunks: list[bytes] = []
        remaining = MAXIMUM_INPUT_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        final = os.fstat(descriptor)
        if (
            len(payload) != opened.st_size
            or final.st_size != opened.st_size
            or final.st_mtime_ns != opened.st_mtime_ns
        ):
            raise RecoveryError("bound_artifact_drift")
        return payload, opened
    finally:
        os.close(descriptor)


def _absolute(path: str | Path, *, reason: str = "path_invalid") -> Path:
    supplied = os.fspath(path)
    if not supplied or not os.path.isabs(supplied):
        raise RecoveryError(reason)
    absolute = Path(os.path.abspath(supplied))
    if os.path.normpath(supplied) != supplied:
        raise RecoveryError(reason)
    return absolute


def _regular_metadata(
    path: Path, *, missing_reason: str, allow_empty: bool = False,
) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise RecoveryError(missing_reason) from error
    except OSError as error:
        raise RecoveryError("input_not_regular") from error
    if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
        raise RecoveryError("input_not_regular")
    try:
        if path.resolve(strict=True) != path:
            raise RecoveryError("input_not_regular")
    except OSError as error:
        raise RecoveryError("input_not_regular") from error
    if metadata.st_size < (0 if allow_empty else 1) or metadata.st_size > MAXIMUM_INPUT_BYTES:
        raise RecoveryError("input_not_regular")
    return metadata


def _load_json_path(
    path: Path,
    *,
    missing_reason: str = "bound_artifact_missing",
) -> tuple[dict[str, Any], bytes, os.stat_result]:
    try:
        payload, metadata = _read_regular_bytes(path, missing_reason=missing_reason)
        value = json.loads(payload, object_pairs_hook=_reject_duplicate_pairs)
    except RecoveryError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RecoveryError("json_invalid") from error
    if not isinstance(value, dict):
        raise RecoveryError("schema_invalid")
    return value, payload, metadata


def _keys(value: Mapping[str, Any], expected: set[str], reason: str = "schema_invalid") -> None:
    if set(value) != expected:
        raise RecoveryError(reason)


def _sha(value: Any, *, reason: str = "schema_invalid") -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise RecoveryError(reason)
    return value


def _git(value: Any, *, reason: str = "schema_invalid") -> str:
    if not isinstance(value, str) or GIT_RE.fullmatch(value) is None:
        raise RecoveryError(reason)
    return value


def _cycle(value: Any) -> str:
    if not isinstance(value, str) or CYCLE_RE.fullmatch(value) is None:
        raise RecoveryError("schema_invalid")
    return value


def _integer(value: Any, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RecoveryError("schema_invalid")
    return value


def _artifact_ref(value: Any, *, allow_empty: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecoveryError("schema_invalid")
    _keys(value, {"path", "sha256", "size_bytes"})
    path = _absolute(value["path"])
    expected_sha = _sha(value["sha256"])
    expected_size = _integer(value["size_bytes"], minimum=0 if allow_empty else 1)
    payload, metadata = _read_regular_bytes(
        path, missing_reason="bound_artifact_missing", allow_empty=allow_empty,
    )
    actual_sha = _sha_bytes(payload)
    if metadata.st_size != expected_size or actual_sha != expected_sha:
        raise RecoveryError("bound_artifact_drift")
    return {
        "exists": True,
        "path": os.fspath(path),
        "sha256": actual_sha,
        "size_bytes": metadata.st_size,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
    }


def _artifact_ref_from_path(path: Path) -> dict[str, Any]:
    payload, metadata = _read_regular_bytes(
        path, missing_reason="bound_artifact_missing"
    )
    return {
        "exists": True,
        "path": os.fspath(path),
        "sha256": _sha_bytes(payload),
        "size_bytes": metadata.st_size,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
    }


def _verify_bound_artifact(value: Any, *, allow_empty: bool = False) -> None:
    if not isinstance(value, dict):
        raise RecoveryError("schema_invalid")
    _keys(value, {"exists", "path", "sha256", "size_bytes", "mode"})
    if value["exists"] is not True:
        raise RecoveryError("schema_invalid")
    path = _absolute(value["path"])
    payload, metadata = _read_regular_bytes(
        path, missing_reason="bound_artifact_missing", allow_empty=allow_empty,
    )
    if (
        _sha_bytes(payload) != _sha(value["sha256"])
        or metadata.st_size != _integer(value["size_bytes"], minimum=0 if allow_empty else 1)
        or f"{stat.S_IMODE(metadata.st_mode):04o}" != value["mode"]
    ):
        raise RecoveryError("bound_artifact_drift")


def _load_bound_json(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact = _artifact_ref(value)
    document, _, _ = _load_json_path(Path(artifact["path"]))
    return artifact, document


def _identity(value: Any) -> None:
    if not isinstance(value, dict):
        raise RecoveryError("start_observation_invalid")
    _keys(
        value,
        {
            "pid",
            "parent_pid",
            "session_id",
            "start_identity",
            "start_identity_source",
            "observed_at_unix_ns",
        },
        "start_observation_invalid",
    )
    _integer(value["pid"], minimum=1)
    if value["parent_pid"] is not None:
        _integer(value["parent_pid"])
    if value["session_id"] is not None:
        _integer(value["session_id"])
    if not isinstance(value["start_identity"], str) or not value["start_identity"]:
        raise RecoveryError("start_observation_invalid")
    if value["start_identity_source"] not in {
        "os_ps_lstart",
        "supervisor_observation_only",
    }:
        raise RecoveryError("start_observation_invalid")
    _integer(value["observed_at_unix_ns"], minimum=1)


def _historical_candidate(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise RecoveryError("schema_invalid")
    _keys(
        value,
        {"cycle_id", "candidate_commit", "candidate_tree", "source_manifest_sha256"},
    )
    return {
        "cycle_id": _cycle(value["cycle_id"]),
        "candidate_commit": _git(value["candidate_commit"]),
        "candidate_tree": _git(value["candidate_tree"]),
        "source_manifest_sha256": _sha(value["source_manifest_sha256"]),
    }


def _validate_authorization(
    value: Any,
    candidate: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    artifact, authorization = _load_bound_json(value)
    if authorization.get("protocol") != "mycelium.integration_requalification_cycle_authorization.v23":
        raise RecoveryError("authorization_protocol_invalid")
    source_manifest = authorization.get("source_manifest")
    if not isinstance(source_manifest, dict):
        raise RecoveryError("authorization_candidate_mismatch")
    observed = {
        "cycle_id": authorization.get("cycle_id"),
        "candidate_commit": authorization.get("candidate_commit"),
        "candidate_tree": authorization.get("candidate_tree"),
        "source_manifest_sha256": source_manifest.get("sha256"),
    }
    if observed != dict(candidate):
        raise RecoveryError("authorization_candidate_mismatch")
    if authorization.get("qualification_claim") is not False:
        raise RecoveryError("authorization_claim_invalid")
    if authorization.get("promotion_authorized") is not False:
        raise RecoveryError("authorization_claim_invalid")
    return artifact, authorization


def _validate_start_observation(
    value: Any,
    *,
    candidate: Mapping[str, str],
    authorization_sha256: str,
) -> dict[str, Any]:
    if value is None:
        return {"status": "unknown", "claim": None, "started": None}
    if not isinstance(value, dict):
        raise RecoveryError("start_observation_partial")
    _keys(value, {"claim", "started"}, "start_observation_partial")
    if not isinstance(value["claim"], dict) or not isinstance(value["started"], dict):
        raise RecoveryError("start_observation_partial")
    claim_artifact, claim = _load_bound_json(value["claim"])
    started_artifact, started = _load_bound_json(value["started"])
    _keys(
        claim,
        {
            "protocol",
            "state",
            "attempt_id",
            "attempt_binding",
            "claim_owner_identity",
            "claimed_at_unix_ms",
            "detached_requested",
            "qualification_claim",
            "promotion_authorized",
        },
        "start_observation_invalid",
    )
    if claim["protocol"] not in {"mycelium.a5_benchmark_attempt_claim.v1", "mycelium.a5_benchmark_attempt_claim.v2"} or claim["state"] != "claimed":
        raise RecoveryError("start_observation_invalid")
    _sha(claim["attempt_id"], reason="start_observation_invalid")
    binding = claim["attempt_binding"]
    if not isinstance(binding, dict):
        raise RecoveryError("start_observation_invalid")
    _keys(
        binding,
        {
            "authorization_sha256",
            "candidate_tree",
            "child_argv_digest",
            "cycle_id",
            "expected_source_manifest_digest",
            "source_manifest_digest",
            "terminal_path_binding_digest",
        } | ({"maximum_lifetime_seconds"} if claim["protocol"].endswith(".v2") else set()),
        "start_observation_invalid",
    )
    if claim["protocol"].endswith(".v2"):
        lifetime = binding["maximum_lifetime_seconds"]
        if type(lifetime) not in (int, float) or not 0 < lifetime <= 86400:
            raise RecoveryError("start_observation_invalid")
    if _sha_bytes(_canonical_bytes(binding)) != claim["attempt_id"]:
        raise RecoveryError("start_observation_invalid")
    if (
        binding["authorization_sha256"] != authorization_sha256
        or binding["candidate_tree"] != candidate["candidate_tree"]
        or binding["cycle_id"] != candidate["cycle_id"]
        or binding["expected_source_manifest_digest"]
        != candidate["source_manifest_sha256"]
        or binding["source_manifest_digest"] != candidate["source_manifest_sha256"]
    ):
        raise RecoveryError("start_authorization_binding_mismatch")
    _sha(binding["child_argv_digest"], reason="start_observation_invalid")
    _sha(binding["terminal_path_binding_digest"], reason="start_observation_invalid")
    _identity(claim["claim_owner_identity"])
    _integer(claim["claimed_at_unix_ms"])
    if not isinstance(claim["detached_requested"], bool):
        raise RecoveryError("start_observation_invalid")
    if claim["qualification_claim"] is not False or claim["promotion_authorized"] is not False:
        raise RecoveryError("start_observation_invalid")

    _keys(
        started,
        {
            "protocol",
            "state",
            "attempt_id",
            "attempt_claim_sha256",
            "supervisor_identity",
            "child_identity",
            "started_at_unix_ms",
            "qualification_claim",
            "promotion_authorized",
        },
        "start_observation_invalid",
    )
    if (
        started["protocol"] != "mycelium.a5_benchmark_attempt_started.v1"
        or started["state"] != "workload_started"
        or started["attempt_id"] != claim["attempt_id"]
        or started["attempt_claim_sha256"] != claim_artifact["sha256"]
        or started["qualification_claim"] is not False
        or started["promotion_authorized"] is not False
    ):
        raise RecoveryError("start_observation_invalid")
    _identity(started["supervisor_identity"])
    _identity(started["child_identity"])
    _integer(started["started_at_unix_ms"])
    return {
        "status": "source_bound_observed",
        "claim": claim_artifact,
        "started": started_artifact,
    }


def _terminal_artifacts(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise RecoveryError("schema_invalid")
    expected = {"success", "failure", "supervisor_receipt", "sentinel"}
    _keys(value, expected)
    result: dict[str, dict[str, Any]] = {}
    paths: set[Path] = set()
    for name in sorted(expected):
        path = _absolute(value[name])
        if path in paths:
            raise RecoveryError("terminal_artifact_path_collision")
        paths.add(path)
        if os.path.lexists(path):
            raise RecoveryError("historical_terminal_artifact_present")
        result[name] = {
            "exists": False,
            "path": os.fspath(path),
            "sha256": None,
            "size_bytes": 0,
        }
    return result


def _roots(
    retained_value: Any,
    runtime_value: Any,
) -> dict[str, list[dict[str, Any]]]:
    if (
        not isinstance(retained_value, list)
        or not retained_value
        or not isinstance(runtime_value, list)
        or not runtime_value
    ):
        raise RecoveryError("schema_invalid")
    retained: list[Path] = []
    for supplied in retained_value:
        path = _absolute(supplied)
        try:
            metadata = path.lstat()
        except OSError as error:
            raise RecoveryError("retained_evidence_root_missing") from error
        if path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
            raise RecoveryError("retained_evidence_root_invalid")
        retained.append(path)
    runtime = [_absolute(supplied) for supplied in runtime_value]
    if len(set(retained)) != len(retained) or len(set(runtime)) != len(runtime):
        raise RecoveryError("schema_invalid")
    for evidence_root in retained:
        for runtime_root in runtime:
            if evidence_root == runtime_root or evidence_root in runtime_root.parents or runtime_root in evidence_root.parents:
                raise RecoveryError("evidence_runtime_root_overlap")
    return {
        "retained_evidence_roots": [
            {
                "path": os.fspath(path),
                "exists": True,
                "kind": "directory",
                "preservation_required": True,
            }
            for path in retained
        ],
        "removable_runtime_stage_roots": [
            {
                "path": os.fspath(path),
                "exists_at_recording": os.path.lexists(path),
                "removal_performed_by_producer": False,
            }
            for path in runtime
        ],
    }


def _cleanup_ack(value: Any, host_id: str) -> dict[str, Any]:
    artifact, document = _load_bound_json(value)
    _keys(document, {"protocol", "node_id", "staging_root", "removed"})
    if (
        document["protocol"] != "mycelium.controller_remote_cleanup_ack.v1"
        or document["node_id"] != host_id
        or not isinstance(document["staging_root"], str)
        or not document["staging_root"]
        or document["removed"] is not True
    ):
        raise RecoveryError("cleanup_acknowledgment_invalid")
    artifact["protocol"] = document["protocol"]
    artifact["node_id"] = host_id
    artifact["staging_root"] = document["staging_root"]
    artifact["removed"] = True
    return artifact


def _later_observation(value: Any, *, recorded_at_unix_ms: int) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RecoveryError("later_observation_invalid")
    _keys(
        value,
        {
            "observed_at_unix_ms",
            "maximum_age_ms",
            "expected_host_ids",
            "collector",
            "hosts",
        },
        "later_observation_invalid",
    )
    observed_at = _integer(value["observed_at_unix_ms"])
    maximum_age = _integer(value["maximum_age_ms"], minimum=1)
    if observed_at > recorded_at_unix_ms or recorded_at_unix_ms - observed_at > maximum_age:
        raise RecoveryError("later_census_stale")
    expected_hosts = value["expected_host_ids"]
    if (
        not isinstance(expected_hosts, list)
        or not expected_hosts
        or any(not isinstance(host, str) or not host for host in expected_hosts)
        or expected_hosts != sorted(set(expected_hosts))
    ):
        raise RecoveryError("later_host_roster_invalid")
    collector = value["collector"]
    if not isinstance(collector, dict):
        raise RecoveryError("later_observation_invalid")
    _keys(collector, {"tool_commit", "tool_tree", "source"}, "later_observation_invalid")
    collector_output = {
        "tool_commit": _git(collector["tool_commit"], reason="later_observation_invalid"),
        "tool_tree": _git(collector["tool_tree"], reason="later_observation_invalid"),
        "source": _artifact_ref(collector["source"]),
    }
    hosts = value["hosts"]
    if not isinstance(hosts, list) or len(hosts) != len(expected_hosts):
        raise RecoveryError("later_host_roster_incomplete")
    by_host: dict[str, Any] = {}
    for host in hosts:
        if not isinstance(host, dict):
            raise RecoveryError("later_host_observation_invalid")
        _keys(
            host,
            {
                "host_id",
                "status",
                "sampled_at_unix_ms",
                "ownership_marker",
                "cleanup_acknowledgment",
                "post_cleanup",
            },
            "later_host_observation_invalid",
        )
        host_id = host["host_id"]
        if host_id in by_host:
            raise RecoveryError("later_host_roster_invalid")
        by_host[host_id] = host
    if sorted(by_host) != expected_hosts:
        raise RecoveryError("later_host_roster_incomplete")

    output_hosts = []
    complete = True
    statuses = {"observed", "inaccessible", "missing", "stale", "failed"}
    for host_id in expected_hosts:
        host = by_host[host_id]
        status_value = host["status"]
        if status_value not in statuses:
            raise RecoveryError("later_host_observation_invalid")
        if status_value != "observed":
            if any(
                host[name] is not None
                for name in (
                    "sampled_at_unix_ms",
                    "ownership_marker",
                    "cleanup_acknowledgment",
                    "post_cleanup",
                )
            ):
                raise RecoveryError("unknown_resource_state_coerced")
            complete = False
            output_hosts.append(
                {
                    "host_id": host_id,
                    "status": status_value,
                    "sampled_at_unix_ms": None,
                    "ownership_marker": None,
                    "cleanup_acknowledgment": None,
                    "resource_state": None,
                }
            )
            continue
        sampled_at = _integer(host["sampled_at_unix_ms"])
        if sampled_at > observed_at or recorded_at_unix_ms - sampled_at > maximum_age:
            raise RecoveryError("later_census_stale")
        marker = host["ownership_marker"]
        if not isinstance(marker, dict):
            raise RecoveryError("ownership_marker_invalid")
        _keys(marker, {"matched", "sha256"}, "ownership_marker_invalid")
        if marker["matched"] is not True:
            raise RecoveryError("ownership_marker_invalid")
        marker_output = {"matched": True, "sha256": _sha(marker["sha256"])}
        cleanup = _cleanup_ack(host["cleanup_acknowledgment"], host_id)
        state = host["post_cleanup"]
        if not isinstance(state, dict):
            raise RecoveryError("post_cleanup_observation_invalid")
        _keys(
            state,
            {
                "process_count",
                "listener_count",
                "socket_count",
                "runtime_stage_root_exists",
            },
            "post_cleanup_observation_invalid",
        )
        if (
            _integer(state["process_count"]) != 0
            or _integer(state["listener_count"]) != 0
            or _integer(state["socket_count"]) != 0
            or state["runtime_stage_root_exists"] is not False
        ):
            complete = False
        output_hosts.append(
            {
                "host_id": host_id,
                "status": "observed",
                "sampled_at_unix_ms": sampled_at,
                "ownership_marker": marker_output,
                "cleanup_acknowledgment": cleanup,
                "resource_state": dict(state),
            }
        )
    return {
        "evidence_class": "unsigned_operator_observation",
        "authenticated_live_measurement": False,
        "observed_at_unix_ms": observed_at,
        "maximum_age_ms": maximum_age,
        "expected_host_ids": expected_hosts,
        "collector": collector_output,
        "hosts": output_hosts,
        "observation_complete": complete,
        "fleet_released": False,
        "historical_signed_request_cleanup_established": False,
    }


def _producer_revision() -> dict[str, Any]:
    source_path = Path(__file__).resolve()
    try:
        relative_source = source_path.relative_to(ROOT)
        commit = subprocess.check_output(
            [
                "git",
                "-C",
                os.fspath(ROOT),
                "log",
                "-1",
                "--format=%H",
                "--",
                os.fspath(relative_source),
            ],
            text=True,
        ).strip()
        if not commit:
            raise RecoveryError("producer_source_not_committed")
        tree = subprocess.check_output(
            ["git", "-C", os.fspath(ROOT), "rev-parse", f"{commit}^{{tree}}"],
            text=True,
        ).strip()
        committed_source = subprocess.check_output(
            [
                "git",
                "-C",
                os.fspath(ROOT),
                "show",
                f"{commit}:{relative_source.as_posix()}",
            ]
        )
    except RecoveryError:
        raise
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        raise RecoveryError("producer_revision_unavailable") from error
    source = _artifact_ref_from_path(source_path)
    if _sha_bytes(committed_source) != source["sha256"]:
        raise RecoveryError("producer_source_not_committed")
    return {
        "commit": _git(commit),
        "tree": _git(tree),
        "source": source,
    }


def _record_digest(document: Mapping[str, Any]) -> str:
    unsigned = dict(document)
    unsigned.pop("record_sha256", None)
    return _sha_bytes(_canonical_bytes(unsigned))


def _build_record(input_path: Path, *, recorded_at_unix_ms: int) -> dict[str, Any]:
    operator_input, payload, metadata = _load_json_path(
        input_path,
        missing_reason="input_missing",
    )
    _keys(
        operator_input,
        {
            "protocol",
            "historical_candidate",
            "authorization",
            "start_observation",
            "terminal_artifact_paths",
            "frozen_inputs",
            "retained_evidence_roots",
            "removable_runtime_stage_roots",
            "historical_unknowns",
            "later_resource_release_observation",
        },
    )
    if operator_input["protocol"] != INPUT_PROTOCOL:
        raise RecoveryError("input_protocol_invalid")
    candidate = _historical_candidate(operator_input["historical_candidate"])
    authorization, _ = _validate_authorization(operator_input["authorization"], candidate)
    start = _validate_start_observation(
        operator_input["start_observation"],
        candidate=candidate,
        authorization_sha256=authorization["sha256"],
    )
    terminals = _terminal_artifacts(operator_input["terminal_artifact_paths"])
    frozen_value = operator_input["frozen_inputs"]
    if not isinstance(frozen_value, list) or not frozen_value:
        raise RecoveryError("schema_invalid")
    # Opaque retained evidence may prove emptiness; required JSON stays nonempty.
    frozen = [_artifact_ref(value, allow_empty=True) for value in frozen_value]
    roots = _roots(
        operator_input["retained_evidence_roots"],
        operator_input["removable_runtime_stage_roots"],
    )
    unknowns = operator_input["historical_unknowns"]
    if not isinstance(unknowns, dict):
        raise RecoveryError("schema_invalid")
    _keys(
        unknowns,
        {
            "exit_code",
            "exit_time_utc",
            "signal",
            "cause",
            "historical_signed_request_cleanup",
        },
    )
    if any(value is not None for value in unknowns.values()):
        raise RecoveryError("historical_unknown_coerced")
    producer = _producer_revision()
    if producer["commit"] == candidate["candidate_commit"] and producer["tree"] == candidate["candidate_tree"]:
        raise RecoveryError("candidate_producer_identity_conflated")
    later = _later_observation(
        operator_input["later_resource_release_observation"],
        recorded_at_unix_ms=recorded_at_unix_ms,
    )
    record: dict[str, Any] = {
        "protocol": PROTOCOL,
        "record_sha256": None,
        "recorded_at_unix_ms": recorded_at_unix_ms,
        "evidence_class": "unsigned_recovery_accounting_not_benchmark_or_release",
        "authority": {
            "authenticated_live_measurement": False,
            "signed": False,
            "trust_boundary": "unsigned_operator_recovery_accounting_only",
        },
        "operator_input": {
            "exists": True,
            "path": os.fspath(input_path),
            "sha256": _sha_bytes(payload),
            "size_bytes": metadata.st_size,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        },
        "historical_candidate": candidate,
        "authorization": authorization,
        "start_observation": start,
        "terminal_artifacts": terminals,
        "frozen_inputs": frozen,
        "evidence_roots": roots,
        "historical_observation": {
            "workload_started": True if start["status"] == "source_bound_observed" else "unknown",
            **dict(unknowns),
        },
        "later_resource_release": later,
        "disposition": {
            "outcome": "invalid_no_result",
            "attempt_consumed": True,
            "resumable": False,
            "retry_authorized": False,
            "qualification_claim": False,
            "benchmark_pass": False,
            "promotion_authorized": False,
            "fresh_run_authorized": False,
        },
        "producer": producer,
    }
    record["record_sha256"] = _record_digest(record)
    return record


def _verify_missing_artifact(value: Any) -> None:
    if not isinstance(value, dict):
        raise RecoveryError("schema_invalid")
    _keys(value, {"exists", "path", "sha256", "size_bytes"})
    path = _absolute(value["path"])
    if value != {
        "exists": False,
        "path": os.fspath(path),
        "sha256": None,
        "size_bytes": 0,
    }:
        raise RecoveryError("terminal_artifact_invalid")
    if os.path.lexists(path):
        raise RecoveryError("bound_terminal_artifact_drift")


def _input_ref_from_bound(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecoveryError("schema_invalid")
    required = {"path", "sha256", "size_bytes"}
    if not required.issubset(value):
        raise RecoveryError("schema_invalid")
    return {name: value[name] for name in sorted(required)}


def _verify_later(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise RecoveryError("later_observation_invalid")
    _keys(
        value,
        {
            "evidence_class",
            "authenticated_live_measurement",
            "observed_at_unix_ms",
            "maximum_age_ms",
            "expected_host_ids",
            "collector",
            "hosts",
            "observation_complete",
            "fleet_released",
            "historical_signed_request_cleanup_established",
        },
        "later_observation_invalid",
    )
    if (
        value["evidence_class"] != "unsigned_operator_observation"
        or value["authenticated_live_measurement"] is not False
        or value["fleet_released"] is not False
        or value["historical_signed_request_cleanup_established"] is not False
    ):
        raise RecoveryError("later_release_authority_invalid")
    collector = value["collector"]
    if not isinstance(collector, dict):
        raise RecoveryError("later_observation_invalid")
    _keys(collector, {"tool_commit", "tool_tree", "source"}, "later_observation_invalid")
    _git(collector["tool_commit"], reason="later_observation_invalid")
    _git(collector["tool_tree"], reason="later_observation_invalid")
    _verify_bound_artifact(collector["source"])
    expected = value["expected_host_ids"]
    hosts = value["hosts"]
    if not isinstance(expected, list) or not isinstance(hosts, list):
        raise RecoveryError("later_observation_invalid")
    if expected != sorted(set(expected)) or len(hosts) != len(expected):
        raise RecoveryError("later_host_roster_invalid")
    observed_complete = True
    observed_ids = []
    for host in hosts:
        if not isinstance(host, dict):
            raise RecoveryError("later_host_observation_invalid")
        _keys(
            host,
            {
                "host_id",
                "status",
                "sampled_at_unix_ms",
                "ownership_marker",
                "cleanup_acknowledgment",
                "resource_state",
            },
            "later_host_observation_invalid",
        )
        observed_ids.append(host["host_id"])
        if host["status"] != "observed":
            observed_complete = False
            if any(host[name] is not None for name in ("sampled_at_unix_ms", "ownership_marker", "cleanup_acknowledgment", "resource_state")):
                raise RecoveryError("unknown_resource_state_coerced")
            continue
        marker = host["ownership_marker"]
        if not isinstance(marker, dict) or marker.get("matched") is not True:
            raise RecoveryError("ownership_marker_invalid")
        _sha(marker.get("sha256"), reason="ownership_marker_invalid")
        cleanup = host["cleanup_acknowledgment"]
        if not isinstance(cleanup, dict):
            raise RecoveryError("cleanup_acknowledgment_invalid")
        _verify_bound_artifact(
            {key: cleanup[key] for key in ("exists", "path", "sha256", "size_bytes", "mode")}
        )
        state = host["resource_state"]
        if not isinstance(state, dict):
            raise RecoveryError("post_cleanup_observation_invalid")
        if (
            state.get("process_count") != 0
            or state.get("listener_count") != 0
            or state.get("socket_count") != 0
            or state.get("runtime_stage_root_exists") is not False
        ):
            observed_complete = False
    if observed_ids != expected or value["observation_complete"] is not observed_complete:
        raise RecoveryError("later_host_roster_invalid")


def _verify_record(document: dict[str, Any]) -> None:
    _keys(
        document,
        {
            "protocol",
            "record_sha256",
            "recorded_at_unix_ms",
            "evidence_class",
            "authority",
            "operator_input",
            "historical_candidate",
            "authorization",
            "start_observation",
            "terminal_artifacts",
            "frozen_inputs",
            "evidence_roots",
            "historical_observation",
            "later_resource_release",
            "disposition",
            "producer",
        },
    )
    if document["protocol"] != PROTOCOL:
        raise RecoveryError("record_protocol_invalid")
    if document["record_sha256"] != _record_digest(document):
        raise RecoveryError("record_digest_mismatch")
    if document["evidence_class"] != "unsigned_recovery_accounting_not_benchmark_or_release":
        raise RecoveryError("authority_claim_invalid")
    if document["authority"] != {
        "authenticated_live_measurement": False,
        "signed": False,
        "trust_boundary": "unsigned_operator_recovery_accounting_only",
    }:
        raise RecoveryError("authority_claim_invalid")
    candidate = _historical_candidate(document["historical_candidate"])
    producer = document["producer"]
    if not isinstance(producer, dict):
        raise RecoveryError("schema_invalid")
    _keys(producer, {"commit", "tree", "source"})
    producer_commit = _git(producer["commit"])
    producer_tree = _git(producer["tree"])
    if producer_commit == candidate["candidate_commit"] and producer_tree == candidate["candidate_tree"]:
        raise RecoveryError("candidate_producer_identity_conflated")
    _verify_bound_artifact(producer["source"])
    _verify_bound_artifact(document["operator_input"])
    operator_input, _, _ = _load_json_path(Path(document["operator_input"]["path"]))
    _keys(
        operator_input,
        {
            "protocol",
            "historical_candidate",
            "authorization",
            "start_observation",
            "terminal_artifact_paths",
            "frozen_inputs",
            "retained_evidence_roots",
            "removable_runtime_stage_roots",
            "historical_unknowns",
            "later_resource_release_observation",
        },
    )
    if operator_input["protocol"] != INPUT_PROTOCOL:
        raise RecoveryError("input_protocol_invalid")
    if _historical_candidate(operator_input["historical_candidate"]) != candidate:
        raise RecoveryError("authorization_candidate_mismatch")
    authorization, _ = _validate_authorization(
        operator_input["authorization"], candidate
    )
    if authorization != document["authorization"]:
        raise RecoveryError("authorization_binding_mismatch")
    start = document["start_observation"]
    if not isinstance(start, dict):
        raise RecoveryError("start_observation_invalid")
    _keys(start, {"status", "claim", "started"}, "start_observation_invalid")
    expected_start = _validate_start_observation(
        operator_input["start_observation"],
        candidate=candidate,
        authorization_sha256=authorization["sha256"],
    )
    if start != expected_start:
        raise RecoveryError("start_observation_invalid")
    if start["status"] == "unknown":
        expected_started: bool | str = "unknown"
    elif start["status"] == "source_bound_observed":
        expected_started = True
    else:
        raise RecoveryError("start_observation_invalid")
    terminals = document["terminal_artifacts"]
    if not isinstance(terminals, dict):
        raise RecoveryError("schema_invalid")
    _keys(terminals, {"success", "failure", "supervisor_receipt", "sentinel"})
    expected_terminals = _terminal_artifacts(
        operator_input["terminal_artifact_paths"]
    )
    if terminals != expected_terminals:
        raise RecoveryError("terminal_artifact_invalid")
    for artifact in terminals.values():
        _verify_missing_artifact(artifact)
    frozen = document["frozen_inputs"]
    if not isinstance(frozen, list) or not frozen:
        raise RecoveryError("schema_invalid")
    expected_frozen = [
        _artifact_ref(value, allow_empty=True) for value in operator_input["frozen_inputs"]
    ]
    if frozen != expected_frozen:
        raise RecoveryError("bound_artifact_drift")
    for artifact in frozen:
        _verify_bound_artifact(artifact, allow_empty=True)
    roots = document["evidence_roots"]
    if not isinstance(roots, dict):
        raise RecoveryError("schema_invalid")
    _keys(roots, {"retained_evidence_roots", "removable_runtime_stage_roots"})
    if (
        not isinstance(roots["retained_evidence_roots"], list)
        or not isinstance(roots["removable_runtime_stage_roots"], list)
        or any(not isinstance(item, dict) for item in roots["retained_evidence_roots"])
        or any(
            not isinstance(item, dict)
            for item in roots["removable_runtime_stage_roots"]
        )
    ):
        raise RecoveryError("schema_invalid")
    expected_retained_paths = [
        os.fspath(_absolute(path)) for path in operator_input["retained_evidence_roots"]
    ]
    expected_runtime_paths = [
        os.fspath(_absolute(path))
        for path in operator_input["removable_runtime_stage_roots"]
    ]
    if [item.get("path") for item in roots["retained_evidence_roots"]] != expected_retained_paths:
        raise RecoveryError("evidence_root_binding_mismatch")
    if [item.get("path") for item in roots["removable_runtime_stage_roots"]] != expected_runtime_paths:
        raise RecoveryError("evidence_root_binding_mismatch")
    for retained in roots["retained_evidence_roots"]:
        if not isinstance(retained, dict):
            raise RecoveryError("schema_invalid")
        _keys(retained, {"path", "exists", "kind", "preservation_required"})
        path = _absolute(retained["path"])
        if retained != {
            "path": os.fspath(path),
            "exists": True,
            "kind": "directory",
            "preservation_required": True,
        } or not path.is_dir() or path.is_symlink():
            raise RecoveryError("retained_evidence_root_missing")
    for runtime in roots["removable_runtime_stage_roots"]:
        _keys(
            runtime,
            {"path", "exists_at_recording", "removal_performed_by_producer"},
        )
        _absolute(runtime["path"])
        if (
            not isinstance(runtime["exists_at_recording"], bool)
            or runtime["removal_performed_by_producer"] is not False
        ):
            raise RecoveryError("evidence_root_binding_mismatch")
    observation = document["historical_observation"]
    if not isinstance(observation, dict):
        raise RecoveryError("schema_invalid")
    _keys(
        observation,
        {
            "workload_started",
            "exit_code",
            "exit_time_utc",
            "signal",
            "cause",
            "historical_signed_request_cleanup",
        },
    )
    if observation["workload_started"] != expected_started or any(
        observation[name] is not None
        for name in (
            "exit_code",
            "exit_time_utc",
            "signal",
            "cause",
            "historical_signed_request_cleanup",
        )
    ):
        raise RecoveryError("historical_unknown_coerced")
    if document["disposition"] != {
        "outcome": "invalid_no_result",
        "attempt_consumed": True,
        "resumable": False,
        "retry_authorized": False,
        "qualification_claim": False,
        "benchmark_pass": False,
        "promotion_authorized": False,
        "fresh_run_authorized": False,
    }:
        raise RecoveryError("disposition_invalid")
    expected_later = _later_observation(
        operator_input["later_resource_release_observation"],
        recorded_at_unix_ms=_integer(document["recorded_at_unix_ms"]),
    )
    if document["later_resource_release"] != expected_later:
        raise RecoveryError("later_observation_binding_mismatch")
    _verify_later(document["later_resource_release"])


def _exclusive_write(path: Path, document: Mapping[str, Any]) -> None:
    path = _absolute(path, reason="output_path_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    payload = json.dumps(
        document,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as error:
        raise RecoveryError("output_exists") from error
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise RecoveryError("output_write_failed")
            offset += written
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)
    try:
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except OSError:
        pass


def _produce(args: argparse.Namespace) -> int:
    input_path = _absolute(args.input, reason="input_path_invalid")
    output_path = _absolute(args.output, reason="output_path_invalid")
    recorded_at = _integer(args.recorded_at_unix_ms)
    record = _build_record(input_path, recorded_at_unix_ms=recorded_at)
    _exclusive_write(output_path, record)
    print(
        json.dumps(
            {
                "protocol": PROTOCOL,
                "record_sha256": record["record_sha256"],
                "written": os.fspath(output_path),
            },
            sort_keys=True,
        )
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    record_path = _absolute(args.record, reason="record_path_invalid")
    document, _, metadata = _load_json_path(
        record_path, missing_reason="record_missing"
    )
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise RecoveryError("record_mode_invalid")
    _verify_record(document)
    print(
        json.dumps(
            {
                "protocol": VERIFY_PROTOCOL,
                "record_sha256": document["record_sha256"],
                "valid": True,
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    produce = subparsers.add_parser("produce", allow_abbrev=False)
    produce.add_argument("--input", required=True)
    produce.add_argument("--output", required=True)
    produce.add_argument("--recorded-at-unix-ms", required=True, type=int)
    verify = subparsers.add_parser("verify", allow_abbrev=False)
    verify.add_argument("--record", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return _produce(args) if args.command == "produce" else _verify(args)
    except RecoveryError as error:
        payload: dict[str, Any] = {
            "protocol": (
                "mycelium.a5_interrupted_attempt_reconciliation_production_error.v1"
                if args.command == "produce"
                else VERIFY_PROTOCOL
            ),
            "reason_code": str(error),
        }
        if args.command == "verify":
            payload["valid"] = False
        print(json.dumps(payload, sort_keys=True))
        return 2
    except OSError:
        payload: dict[str, Any] = {
            "protocol": (
                "mycelium.a5_interrupted_attempt_reconciliation_production_error.v1"
                if args.command == "produce"
                else VERIFY_PROTOCOL
            ),
            "reason_code": "io_error",
        }
        if args.command == "verify":
            payload["valid"] = False
        print(json.dumps(payload, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
