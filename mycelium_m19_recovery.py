# SPDX-License-Identifier: AGPL-3.0-or-later
"""Closed M19 liveness, replanning, and recovery projections.

The module deliberately carries no prompt, token, KV, endpoint, or device content.
It records only the fenced facts needed to prove whether recovery happened and which
explicit mode was used.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


LIVENESS_PROTOCOL = "mycelium.m19_liveness.v1"
PLAN_PROTOCOL = "mycelium.m19_recovery_plan.v1"
RUNTIME_PROTOCOL = "mycelium.m19_recovery_runtime.v1"

_PRIVATE_FIELDS = frozenset(
    {
        "prompt", "prompt_text", "response", "response_text", "token_ids", "tokens",
        "tensor", "tensors", "kv", "kv_bytes", "kv_content", "runtime_endpoint",
        "private_address", "hostname", "device_id", "credential", "secret",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "deployment_id", "deployment_epoch", "topology_version", "model_id",
        "model_revision", "representation_digest", "graph_digest",
        "membership_generation",
    }
)


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{name} must be a bounded non-empty string")
    return value


def _sha(value: object, name: str) -> str:
    value = _text(value, name)
    if len(value) != 71 or not value.startswith("sha256:") or any(
        char not in "0123456789abcdef" for char in value[7:]
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 reference")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _reject_private(value: object) -> None:
    if isinstance(value, Mapping):
        if _PRIVATE_FIELDS.intersection(str(key).lower() for key in value):
            raise ValueError("M19 projection contains private runtime content")
        for child in value.values():
            _reject_private(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_private(child)


def _json_copy(value: object) -> Any:
    _reject_private(value)
    try:
        return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("M19 projection must be finite JSON") from exc


def _binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _BINDING_FIELDS:
        raise ValueError("M19 binding shape is invalid")
    result = {key: _text(value[key], key) for key in ("deployment_id", "model_id", "model_revision")}
    for key in ("representation_digest", "graph_digest"):
        result[key] = _sha(value[key], key)
    for key in ("deployment_epoch", "topology_version", "membership_generation"):
        result[key] = _integer(value[key], key, minimum=1)
    return result


class TrafficAwareLivenessDetector:
    """Suppress transient misses while escalating verified active-path failures."""

    def __init__(self, binding: Mapping[str, Any], *, generated_at_unix_ms: int) -> None:
        self._binding = _binding(binding)
        self._generated_at = _integer(generated_at_unix_ms, "generated_at_unix_ms", minimum=1)
        self._subjects: dict[str, dict[str, Any]] = {}
        self._incidents: list[dict[str, Any]] = []
        self._sequence = 0

    def observe(self, subject_id: str, *, observed_at_unix_ms: int) -> None:
        subject_id = _text(subject_id, "subject_id")
        now = _integer(observed_at_unix_ms, "observed_at_unix_ms", minimum=1)
        record = self._subjects.get(subject_id)
        if record is None:
            if len(self._subjects) >= 4096:
                raise ValueError("liveness_subject_limit")
            self._subjects[subject_id] = {
                "subject_id": subject_id, "state": "fresh", "last_fresh_unix_ms": now,
                "last_observed_unix_ms": now, "consecutive_misses": 0,
                "consecutive_fresh": 1,
            }
            return
        if now < record["last_observed_unix_ms"]:
            raise ValueError("liveness_observation_stale")
        record["last_observed_unix_ms"] = now
        record["consecutive_misses"] = 0
        record["consecutive_fresh"] += 1
        record["last_fresh_unix_ms"] = now
        if record["state"] in {"quarantined", "failed"}:
            if record["consecutive_fresh"] >= 2:
                record["state"] = "recovered"
        else:
            record["state"] = "fresh"

    def miss(self, subject_id: str, *, observed_at_unix_ms: int, active_traffic: bool) -> None:
        subject_id = _text(subject_id, "subject_id")
        now = _integer(observed_at_unix_ms, "observed_at_unix_ms", minimum=1)
        record = self._subjects.get(subject_id)
        if record is None:
            raise ValueError("liveness_subject_unknown")
        if now < record["last_observed_unix_ms"]:
            raise ValueError("liveness_observation_stale")
        record["last_observed_unix_ms"] = now
        record["consecutive_misses"] += 1
        record["consecutive_fresh"] = 0
        stale_ms = now - record["last_fresh_unix_ms"]
        if active_traffic:
            self.active_disconnect(subject_id, observed_at_unix_ms=now, scope="request", affected_track_ids=())
        elif record["consecutive_misses"] >= 3 and stale_ms >= 15_000:
            record["state"] = "quarantined"
            self._incident(subject_id, now, "peer", (), "idle_stale", "quarantined")
        else:
            record["state"] = "suspect"

    def active_disconnect(
        self,
        subject_id: str,
        *,
        observed_at_unix_ms: int,
        scope: str,
        affected_track_ids: Sequence[str],
    ) -> None:
        if scope not in {"request", "edge", "placement", "peer", "deployment"}:
            raise ValueError("liveness_scope_invalid")
        subject_id = _text(subject_id, "subject_id")
        now = _integer(observed_at_unix_ms, "observed_at_unix_ms", minimum=1)
        record = self._subjects.get(subject_id)
        if record is None:
            raise ValueError("liveness_subject_unknown")
        record["state"] = "failed"
        record["last_observed_unix_ms"] = now
        record["consecutive_fresh"] = 0
        self._incident(subject_id, now, scope, affected_track_ids, "active_disconnect", "failed")

    def _incident(
        self, subject_id: str, now: int, scope: str, tracks: Sequence[str], reason: str, outcome: str
    ) -> None:
        self._sequence += 1
        self._incidents.append(
            {
                "incident_id": f"m19-incident-{self._sequence}", "subject_id": subject_id,
                "scope": scope, "detector_source": "traffic_aware_liveness",
                "reason": reason, "first_observed_unix_ms": now,
                "last_observed_unix_ms": now,
                "old_generation": self._binding["membership_generation"],
                "new_generation": self._binding["membership_generation"],
                "affected_track_ids": sorted({_text(item, "track_id") for item in tracks}),
                "action": "suppress" if outcome == "suspect" else "remove_from_admission",
                "terminal_outcome": outcome,
            }
        )
        self._incidents = self._incidents[-256:]

    def status(self) -> dict[str, Any]:
        body = {
            "protocol": LIVENESS_PROTOCOL, "generated_at_unix_ms": self._generated_at,
            "binding": copy.deepcopy(self._binding),
            "budgets": {
                "active_failure_detection_ms": 2_000, "idle_keepalive_ms": 5_000,
                "suspect_misses": 2, "quarantine_misses": 3,
                "quarantine_stale_ms": 15_000, "recovery_fresh_observations": 2,
            },
            "subjects": [copy.deepcopy(self._subjects[key]) for key in sorted(self._subjects)],
            "incidents": copy.deepcopy(self._incidents),
        }
        body["evidence_digest"] = _digest(body)
        return validate_liveness(body)


_LIVENESS_FIELDS = frozenset(
    {"protocol", "generated_at_unix_ms", "binding", "budgets", "subjects", "incidents", "evidence_digest"}
)


def validate_liveness(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if not isinstance(document, Mapping) or set(document) != _LIVENESS_FIELDS:
            raise ValueError
        result = _json_copy(document)
        if result["protocol"] != LIVENESS_PROTOCOL:
            raise ValueError
        _binding(result["binding"])
        _integer(result["generated_at_unix_ms"], "generated_at_unix_ms", minimum=1)
        _sha(result["evidence_digest"], "evidence_digest")
        body = dict(result)
        actual = body.pop("evidence_digest")
        if _digest(body) != actual or not isinstance(result["subjects"], list) or not isinstance(result["incidents"], list):
            raise ValueError
        if len(result["subjects"]) > 4096 or len(result["incidents"]) > 256:
            raise ValueError
        for record in result["subjects"]:
            if not isinstance(record, Mapping) or set(record) != {
                "subject_id", "state", "last_fresh_unix_ms", "last_observed_unix_ms",
                "consecutive_misses", "consecutive_fresh",
            } or record["state"] not in {"fresh", "suspect", "quarantined", "failed", "recovered"}:
                raise ValueError
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("liveness_invalid") from exc


def build_recovery_plan(
    binding: Mapping[str, Any], *, incumbent_track_ids: Sequence[str], failed_track_ids: Sequence[str],
    successors: Sequence[Mapping[str, Any]], equivalent_candidate_generations: int,
    candidate_first_seen_unix_ms: int, generated_at_unix_ms: int,
) -> dict[str, Any]:
    bound = _binding(binding)
    incumbents = sorted({_text(item, "track_id") for item in incumbent_track_ids})
    failed = sorted({_text(item, "track_id") for item in failed_track_ids})
    if not set(failed) <= set(incumbents):
        raise ValueError("recovery_failed_track_unknown")
    now = _integer(generated_at_unix_ms, "generated_at_unix_ms", minimum=1)
    first_seen = _integer(candidate_first_seen_unix_ms, "candidate_first_seen_unix_ms", minimum=1)
    generations = _integer(equivalent_candidate_generations, "equivalent_candidate_generations", minimum=1)
    surviving = sorted(set(incumbents) - set(failed))
    normalized_successors = [_normalize_successor(item) for item in successors]
    emergency = not surviving
    stable = generations >= 3 and now - first_seen >= 10_000
    state = "emergency_candidate" if emergency else ("stable_candidate" if stable else "hysteresis_pending")
    eligible = any(item["qualification_id"] and item["qualification_digest"] for item in normalized_successors)
    body = {
        "protocol": PLAN_PROTOCOL, "generated_at_unix_ms": now, "binding": bound,
        "incumbent_track_ids": incumbents, "failed_track_ids": failed,
        "surviving_track_ids": surviving, "successors": normalized_successors,
        "candidate_state": state, "equivalent_candidate_generations": generations,
        "candidate_first_seen_unix_ms": first_seen,
        "provisioning_allowed": bool(eligible and (emergency or stable)),
        "claim_boundary": "deterministic candidate intent only; runtime and qualification own recovery",
    }
    body["plan_digest"] = _digest(body)
    return validate_recovery_plan(body)


def _normalize_successor(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = {"track_id", "qualification_id", "qualification_digest", "decode_mode", "kv_compatibility", "kv_schema_digest", "failure_domain"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("recovery_successor_shape_invalid")
    compatibility = value["kv_compatibility"]
    if compatibility not in {"compatible", "incompatible", "unknown"}:
        raise ValueError("recovery_successor_kv_compatibility_invalid")
    schema = value["kv_schema_digest"]
    if schema is not None:
        schema = _sha(schema, "kv_schema_digest")
    if compatibility == "compatible" and schema is None:
        raise ValueError("recovery_successor_kv_schema_missing")
    return {
        "track_id": _text(value["track_id"], "track_id"),
        "qualification_id": _text(value["qualification_id"], "qualification_id"),
        "qualification_digest": _sha(value["qualification_digest"], "qualification_digest"),
        "decode_mode": _text(value["decode_mode"], "decode_mode"),
        "kv_compatibility": compatibility, "kv_schema_digest": schema,
        "failure_domain": _text(value["failure_domain"], "failure_domain"),
    }


_PLAN_FIELDS = frozenset(
    {"protocol", "generated_at_unix_ms", "binding", "incumbent_track_ids", "failed_track_ids", "surviving_track_ids", "successors", "candidate_state", "equivalent_candidate_generations", "candidate_first_seen_unix_ms", "provisioning_allowed", "claim_boundary", "plan_digest"}
)


def validate_recovery_plan(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if not isinstance(document, Mapping) or set(document) != _PLAN_FIELDS:
            raise ValueError
        result = _json_copy(document)
        if result["protocol"] != PLAN_PROTOCOL or result["candidate_state"] not in {"hysteresis_pending", "stable_candidate", "emergency_candidate"}:
            raise ValueError
        _binding(result["binding"])
        for successor in result["successors"]:
            _normalize_successor(successor)
        body = dict(result)
        actual = body.pop("plan_digest")
        if _sha(actual, "plan_digest") != actual or _digest(body) != actual:
            raise ValueError
        if set(result["failed_track_ids"]) - set(result["incumbent_track_ids"]):
            raise ValueError
        if result["surviving_track_ids"] != sorted(set(result["incumbent_track_ids"]) - set(result["failed_track_ids"])):
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("recovery_plan_invalid") from exc


class RecoveryLedger:
    """Generation-fenced logical request attempts with monotonic checkpoints."""

    def __init__(self, binding: Mapping[str, Any], *, maximum_recovery_attempts: int = 2) -> None:
        self._binding = _binding(binding)
        self._maximum = _integer(maximum_recovery_attempts, "maximum_recovery_attempts", minimum=1)
        self._requests: dict[str, dict[str, Any]] = {}
        self._successor_failures: list[int] = []
        self._breaker_open_until_unix_ms = 0
        self._reconciliation: dict[str, str] = {}

    @classmethod
    def restore(cls, document: Mapping[str, Any]) -> RecoveryLedger:
        """Restore one validated checkpoint without creating a second authority."""

        status = validate_recovery_runtime(document)
        ledger = cls(
            status["binding"],
            maximum_recovery_attempts=status["maximum_recovery_attempts"],
        )
        ledger._requests = {
            item["request_id"]: copy.deepcopy(item) for item in status["requests"]
        }
        ledger._successor_failures = list(status["breaker"]["failure_observations_unix_ms"])
        ledger._breaker_open_until_unix_ms = status["breaker"]["open_until_unix_ms"]
        ledger._reconciliation = dict(status["reconciliation"])
        return ledger

    def record_successor_failure(self, *, observed_at_unix_ms: int) -> None:
        now = _integer(observed_at_unix_ms, "observed_at_unix_ms", minimum=1)
        self._successor_failures = [
            item for item in self._successor_failures if now - item <= 60_000
        ]
        self._successor_failures.append(now)
        self._successor_failures = self._successor_failures[-3:]
        if len(self._successor_failures) >= 3:
            self._breaker_open_until_unix_ms = max(
                self._breaker_open_until_unix_ms, now + 30_000
            )

    def reconcile_after_restart(self) -> dict[str, str]:
        """Assign exactly one restart outcome to every retained request."""

        for request_id in sorted(self._requests):
            record = self._requests[request_id]
            if request_id in self._reconciliation:
                continue
            if record["terminal_state"] is not None:
                outcome = "already_terminal"
            elif record["attempt"] > 1:
                outcome = "resumed"
            else:
                record.update(
                    {
                        "terminal_state": "aborted",
                        "terminal_reason": "restart_without_recovery_checkpoint",
                        "cleanup_complete": True,
                    }
                )
                outcome = "aborted"
            self._reconciliation[request_id] = outcome
        return dict(self._reconciliation)

    def admit(self, request_id: str, *, path_id: str, track_id: str, qualification_id: str, qualification_digest: str) -> None:
        request_id = _text(request_id, "request_id")
        if request_id in self._requests:
            raise ValueError("recovery_duplicate_request")
        self._requests[request_id] = {
            "request_id": request_id, "attempt": 1, "path_id": _text(path_id, "path_id"),
            "track_id": _text(track_id, "track_id"),
            "qualification_id": _text(qualification_id, "qualification_id"),
            "qualification_digest": _sha(qualification_digest, "qualification_digest"),
            "committed_token_count": 0, "committed_token_digest": None,
            "recovery_mode": "none", "successor_track_id": None,
            "successor_path_id": None, "kv_outcome": "not_applicable",
            "replay_performed": False, "terminal_state": None, "terminal_reason": None,
            "cleanup_complete": False,
        }

    def _active(self, request_id: str) -> dict[str, Any]:
        record = self._requests.get(request_id)
        if record is None:
            raise ValueError("request_unknown")
        if record["terminal_state"] is not None:
            raise ValueError("request_already_terminal")
        return record

    def commit(self, request_id: str, *, committed_token_count: int, committed_token_digest: str) -> None:
        record = self._active(request_id)
        count = _integer(committed_token_count, "committed_token_count")
        digest = _sha(committed_token_digest, "committed_token_digest")
        if count < record["committed_token_count"]:
            raise ValueError("recovery_watermark_stale")
        if count == record["committed_token_count"] and record["committed_token_digest"] not in {None, digest}:
            raise ValueError("recovery_watermark_conflict")
        record["committed_token_count"] = count
        record["committed_token_digest"] = digest

    def recover(
        self, request_id: str, *, successor: Mapping[str, Any], expected_attempt: int,
        committed_token_count: int, committed_token_digest: str, recovery_mode: str,
        successor_path_id: str, replay_performed: bool,
        observed_at_unix_ms: int | None = None,
    ) -> None:
        record = self._active(request_id)
        successor = _normalize_successor(successor)
        expected_attempt = _integer(expected_attempt, "expected_attempt", minimum=2)
        if expected_attempt != record["attempt"] + 1:
            raise ValueError("recovery_attempt_stale")
        if expected_attempt - 1 > self._maximum:
            raise ValueError("recovery_circuit_breaker_open")
        if observed_at_unix_ms is not None:
            now = _integer(observed_at_unix_ms, "observed_at_unix_ms", minimum=1)
            if now < self._breaker_open_until_unix_ms:
                raise ValueError("recovery_circuit_breaker_open")
        count = _integer(committed_token_count, "committed_token_count")
        digest = _sha(committed_token_digest, "committed_token_digest")
        if count != record["committed_token_count"] or digest != record["committed_token_digest"]:
            raise ValueError("recovery_watermark_stale")
        if recovery_mode not in {"full_context_replay", "fenced_kv_successor"}:
            raise ValueError("recovery_mode_invalid")
        if recovery_mode == "fenced_kv_successor" and successor["kv_compatibility"] != "compatible":
            raise ValueError("kv_successor_incompatible")
        if recovery_mode == "full_context_replay" and not replay_performed:
            raise ValueError("full_context_replay_not_performed")
        if recovery_mode == "fenced_kv_successor" and replay_performed:
            raise ValueError("kv_successor_cannot_claim_replay")
        record.update(
            {
                "attempt": expected_attempt, "path_id": _text(successor_path_id, "successor_path_id"),
                "track_id": successor["track_id"], "qualification_id": successor["qualification_id"],
                "qualification_digest": successor["qualification_digest"], "recovery_mode": recovery_mode,
                "successor_track_id": successor["track_id"], "successor_path_id": successor_path_id,
                "kv_outcome": "resumed_from_checkpoint" if recovery_mode == "fenced_kv_successor" else "not_transferred",
                "replay_performed": replay_performed,
            }
        )

    def complete(self, request_id: str, *, committed_token_count: int, committed_token_digest: str) -> None:
        self.commit(request_id, committed_token_count=committed_token_count, committed_token_digest=committed_token_digest)
        record = self._active(request_id)
        record.update({"terminal_state": "completed", "terminal_reason": "completed", "cleanup_complete": True})

    def abort(self, request_id: str, *, reason: str) -> None:
        record = self._active(request_id)
        record.update({"terminal_state": "aborted", "terminal_reason": _text(reason, "reason"), "cleanup_complete": True})

    def status(self) -> dict[str, Any]:
        body = {
            "protocol": RUNTIME_PROTOCOL, "binding": copy.deepcopy(self._binding),
            "maximum_recovery_attempts": self._maximum,
            "requests": [copy.deepcopy(self._requests[key]) for key in sorted(self._requests)],
            "breaker": {
                "state": "open" if self._breaker_open_until_unix_ms else "closed",
                "failure_observations_unix_ms": list(self._successor_failures),
                "open_until_unix_ms": self._breaker_open_until_unix_ms,
            },
            "reconciliation": dict(sorted(self._reconciliation.items())),
        }
        body["runtime_digest"] = _digest(body)
        return validate_recovery_runtime(body)


_RUNTIME_FIELDS = frozenset({"protocol", "binding", "maximum_recovery_attempts", "requests", "breaker", "reconciliation", "runtime_digest"})
_REQUEST_FIELDS = frozenset(
    {"request_id", "attempt", "path_id", "track_id", "qualification_id", "qualification_digest", "committed_token_count", "committed_token_digest", "recovery_mode", "successor_track_id", "successor_path_id", "kv_outcome", "replay_performed", "terminal_state", "terminal_reason", "cleanup_complete"}
)


def validate_recovery_runtime(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        if not isinstance(document, Mapping) or set(document) != _RUNTIME_FIELDS:
            raise ValueError
        result = _json_copy(document)
        if result["protocol"] != RUNTIME_PROTOCOL or not isinstance(result["requests"], list):
            raise ValueError
        _binding(result["binding"])
        _integer(result["maximum_recovery_attempts"], "maximum_recovery_attempts", minimum=1)
        breaker = result["breaker"]
        if not isinstance(breaker, Mapping) or set(breaker) != {
            "state", "failure_observations_unix_ms", "open_until_unix_ms"
        } or breaker["state"] not in {"closed", "open"}:
            raise ValueError
        if not isinstance(breaker["failure_observations_unix_ms"], list) or len(breaker["failure_observations_unix_ms"]) > 3:
            raise ValueError
        for observed in breaker["failure_observations_unix_ms"]:
            _integer(observed, "failure_observation_unix_ms", minimum=1)
        open_until = _integer(breaker["open_until_unix_ms"], "open_until_unix_ms")
        if (breaker["state"] == "open") != (open_until > 0):
            raise ValueError
        reconciliation = result["reconciliation"]
        if not isinstance(reconciliation, Mapping) or any(
            outcome not in {"resumed", "aborted", "already_terminal"}
            for outcome in reconciliation.values()
        ):
            raise ValueError
        seen: set[str] = set()
        for record in result["requests"]:
            if not isinstance(record, Mapping) or set(record) != _REQUEST_FIELDS:
                raise ValueError
            request_id = _text(record["request_id"], "request_id")
            if request_id in seen:
                raise ValueError
            seen.add(request_id)
            _sha(record["qualification_digest"], "qualification_digest")
            if record["committed_token_digest"] is not None:
                _sha(record["committed_token_digest"], "committed_token_digest")
            if record["terminal_state"] not in {None, "completed", "aborted"}:
                raise ValueError
            if bool(record["cleanup_complete"]) != (record["terminal_state"] is not None):
                raise ValueError
        if set(reconciliation) - seen:
            raise ValueError
        body = dict(result)
        actual = body.pop("runtime_digest")
        if _sha(actual, "runtime_digest") != actual or _digest(body) != actual:
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("recovery_runtime_invalid") from exc
