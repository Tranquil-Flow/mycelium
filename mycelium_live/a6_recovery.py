# SPDX-License-Identifier: AGPL-3.0-only
"""Qualifier-owned A6 compatibility and physical successor challenge.

This module is used by the ordinary physical server.  It never consumes a
browser-authored success flag: eligibility is derived from an exact physical
startup challenge on the already-reserved successor placements.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import hmac
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any
import uuid

from mycelium_a6_m16 import recovery_successor_binding
from mycelium_m16_runtime import path_manifest_document_digest
from mycelium_a6_recovery import (
    ReplayCheckpoint,
    build_recovery_qualification,
    compatibility_matches,
    recovery_request_binding,
)
from mycelium_router.serialization import execution_graph_to_dict

#: Grant-gated qualifier faults the product-mounted A6 receiver may arm. Each
#: one makes the qualifier issue a qualification the recovery authority must
#: refuse; none can make an unqualified successor pass.
CONTROLLED_QUALIFICATION_FAULTS = (
    "incompatible",
    "stale",
    "foreign",
    "unexecuted",
    "mismatched_path_attempt",
)
_CONTROLLED_FAULT_GRANT = "FLEET GRANTED TO A6"
_MAX_RETAINED_OBSERVATIONS = 64
_MAX_DIAGNOSTIC_MESSAGE = 160
_LOGGER = logging.getLogger(__name__)


def _bounded_message(error: BaseException) -> str:
    """One-line, length-bounded exception text for operator diagnostics."""

    text = " ".join(str(error).split())
    return text[:_MAX_DIAGNOSTIC_MESSAGE]


def _challenge_sessions(route: Any, placement_ids: tuple[str, ...]) -> dict[str, Any]:
    """The control sessions of the nodes a successor challenge runs on.

    Resolved before the challenge starts, so a route the challenge's own
    failure retires is still the one inspected afterwards. Diagnostics only:
    any shape this cannot read yields no sessions.
    """

    try:
        current = getattr(route, "_current_route_or_raise", None)
        concrete = current() if callable(current) else route
        graph = concrete.execution_graph
        wanted = set(placement_ids)
        node_ids = sorted({
            placement.node_id
            for stage in graph.stages
            for placement in stage.placements
            if placement.placement_id in wanted
        })
        sessions = getattr(concrete, "_sessions", None)
        if not isinstance(sessions, Mapping):
            return {}
        return {node_id: sessions[node_id] for node_id in node_ids if node_id in sessions}
    except Exception:
        return {}


def _participant_exits(sessions: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Which challenged nodes' control processes exited, and how.

    For a node reached over ssh the control process is the local ssh client:
    exit status 255 with ``Timeout, server ... not responding.`` is a lost
    link (``ServerAliveCountMax``), not a node crash, and the remote worker
    may still be running.
    """

    exits: list[dict[str, Any]] = []
    for node_id, session in sorted(sessions.items()):
        try:
            returncode = session.returncode
        except Exception:
            continue
        if returncode is None:
            continue
        try:
            raw = session.stderr
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        except Exception:
            text = ""
        lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
        exits.append(
            {
                "node_id": node_id,
                "returncode": returncode if type(returncode) is int else None,
                "stderr_last_line": lines[-1][:_MAX_DIAGNOSTIC_MESSAGE] if lines else None,
            }
        )
    return exits


_TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "vocab.json",
    "merges.txt",
)


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def tokenizer_artifact_digest(deployment_dir: Path) -> str:
    """Bind every tokenizer artifact name, byte length, and exact SHA-256."""

    root = Path(deployment_dir)
    records: list[dict[str, object]] = []
    for name in _TOKENIZER_FILES:
        path = root / name
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("a6_tokenizer_artifact_unsafe")
        records.append(
            {"name": name, "size": path.stat().st_size, "digest": _file_digest(path)}
        )
    if not records or not any(
        record["name"] in {"tokenizer.json", "vocab.json"} for record in records
    ):
        raise ValueError("a6_tokenizer_artifact_unavailable")
    return _digest(records)


def _configuration(deployment_dir: Path) -> Mapping[str, Any]:
    path = Path(deployment_dir) / "config.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("a6_model_configuration_unsafe")
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("a6_model_configuration_invalid") from exc
    if not isinstance(document, Mapping):
        raise ValueError("a6_model_configuration_invalid")
    return document


def _graph_digest(graph: Any) -> str:
    try:
        return _digest(execution_graph_to_dict(graph))
    except (AttributeError, TypeError, ValueError):
        # Narrow fallback supports deterministic probes with reduced graph
        # doubles. Production ExecutionGraph objects always take the exact
        # serialization path above.
        return _digest(
            {
                "deployment_id": graph.deployment_id,
                "deployment_epoch": graph.deployment_epoch,
                "topology_version": graph.topology_version,
                "model_id": graph.model_id,
                "resolved_commit": graph.resolved_commit,
                "manifest_digest": graph.manifest_digest,
            }
        )


def _compatibility(
    *,
    graph: Any,
    checkpoint: ReplayCheckpoint,
    deployment_dir: Path,
    tokenizer_digest: str,
    parity_policy_digest: str,
    workload_envelope_digest: str,
) -> dict[str, Any]:
    config = _configuration(deployment_dir)
    tokenizer_inventory = tokenizer_artifact_digest(deployment_dir)
    special_policy = {
        key: config.get(key)
        for key in (
            "bos_token_id",
            "eos_token_id",
            "pad_token_id",
            "decoder_start_token_id",
            "tie_word_embeddings",
        )
    }
    context_policy = {
        key: config.get(key)
        for key in (
            "max_position_embeddings",
            "sliding_window",
            "use_sliding_window",
            "rope_scaling",
        )
    }
    return {
        "model_id": graph.model_id,
        "model_revision": graph.resolved_commit,
        "representation_digest": checkpoint.representation_digest,
        "manifest_digest": graph.manifest_digest,
        "tokenizer_digest": tokenizer_digest,
        "vocabulary_digest": tokenizer_inventory,
        "special_token_policy_digest": _digest(special_policy),
        "architecture_digest": _digest(config),
        "context_policy_digest": _digest(context_policy),
        "output_semantics_digest": _digest(
            {
                "decode": "gateway_contiguous_token_positions",
                "replay_prefix_publication": "suppressed",
                "sampling": "request_generation_settings_bound",
            }
        ),
        "generation_settings_digest": checkpoint.generation_settings_digest,
        "qualification_parity_policy_digest": parity_policy_digest,
        "workload_envelope_digest": workload_envelope_digest,
    }


class LiveA6RecoveryQualifier:
    """Freshly qualify one exact immutable successor reservation."""

    def __init__(
        self,
        *,
        graph: Any,
        deployment_dir: Path,
        tokenizer_digest: str,
        route: Any | None = None,
        physical_challenge: Callable[..., Mapping[str, Any]] | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if route is None and physical_challenge is None:
            raise ValueError("a6_physical_challenge_source_required")
        if not isinstance(tokenizer_digest, str) or not tokenizer_digest.startswith("sha256:"):
            raise ValueError("a6_tokenizer_digest_invalid")
        self._graph = graph
        self._deployment_dir = Path(deployment_dir)
        self._tokenizer_digest = tokenizer_digest
        self._route = route
        self._physical_challenge = physical_challenge or self._run_physical_challenge
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1_000))
        self._fault_lock = threading.Lock()
        self._armed_faults: dict[str, str] = {}
        self._issued: dict[str, dict[str, Any]] = {}
        self._challenge_outcomes: dict[str, dict[str, Any]] = {}

    def arm_controlled_fault(self, *, request_id: str, kind: str, grant: str) -> None:
        """Arm one qualification fault for one request; consumed by one issue."""

        if not isinstance(grant, str) or not hmac.compare_digest(
            grant.encode("utf-8"), _CONTROLLED_FAULT_GRANT.encode("ascii")
        ):
            raise PermissionError("explicit_fleet_grant_required")
        if kind not in CONTROLLED_QUALIFICATION_FAULTS:
            raise ValueError("a6_controlled_qualification_fault_unknown")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("a6_controlled_qualification_fault_request_invalid")
        with self._fault_lock:
            if request_id in self._armed_faults:
                raise ValueError("a6_controlled_qualification_fault_already_armed")
            self._armed_faults[request_id] = kind

    def issued_qualification_facts(self, request_id: str) -> dict[str, Any] | None:
        """What this qualifier last issued for one request, as measured facts."""

        with self._fault_lock:
            facts = self._issued.get(request_id)
            return None if facts is None else json.loads(json.dumps(facts))

    def physical_challenge_outcome(self, request_id: str) -> dict[str, Any] | None:
        """Why the last successor physical challenge for one request passed or failed."""

        with self._fault_lock:
            outcome = self._challenge_outcomes.get(request_id)
            return None if outcome is None else json.loads(json.dumps(outcome))

    def _record_challenge_outcome(self, request_id: str, outcome: Mapping[str, Any]) -> None:
        with self._fault_lock:
            self._challenge_outcomes.pop(request_id, None)
            self._challenge_outcomes[request_id] = dict(outcome)
            while len(self._challenge_outcomes) > _MAX_RETAINED_OBSERVATIONS:
                self._challenge_outcomes.pop(next(iter(self._challenge_outcomes)))
        document = json.dumps(
            {"event": "a6_successor_physical_challenge", "request_id": request_id, **outcome},
            sort_keys=True,
            separators=(",", ":"),
        )
        if outcome.get("passed") is True:
            _LOGGER.info("%s", document)
        else:
            _LOGGER.warning("%s", document)

    def _record_challenge_in_flight(self, request_id: str, outcome: Mapping[str, Any]) -> None:
        """Expose a started challenge before it ends, without logging it.

        A challenge whose node stalls can outlive the A6 seam's bounded wait;
        the seam then reports ``in_flight`` instead of no outcome at all.
        """

        with self._fault_lock:
            self._challenge_outcomes.pop(request_id, None)
            self._challenge_outcomes[request_id] = {**outcome, "outcome": "in_flight"}
            while len(self._challenge_outcomes) > _MAX_RETAINED_OBSERVATIONS:
                self._challenge_outcomes.pop(next(iter(self._challenge_outcomes)))

    def _consume_fault(self, request_id: str) -> str | None:
        with self._fault_lock:
            return self._armed_faults.pop(request_id, None)

    def _record_issue(self, request_id: str, facts: Mapping[str, Any]) -> None:
        with self._fault_lock:
            self._issued.pop(request_id, None)
            self._issued[request_id] = dict(facts)
            while len(self._issued) > _MAX_RETAINED_OBSERVATIONS:
                self._issued.pop(next(iter(self._issued)))

    def __call__(
        self,
        request_id: str,
        checkpoint: ReplayCheckpoint,
        successor_identity: Mapping[str, Any],
        successor_manifest: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if request_id != checkpoint.request_id:
            raise ValueError("successor_identity_mismatch")
        expected = {
            "request_id": request_id,
            "request_attempt": checkpoint.source_attempt + 1,
            "path_id": successor_manifest.path_id,
            "path_attempt": successor_manifest.path_attempt,
            "path_manifest_digest": path_manifest_document_digest(successor_manifest),
            "deployment_id": checkpoint.deployment_id,
            "deployment_epoch": checkpoint.deployment_epoch,
            "topology_generation": checkpoint.topology_generation,
        }
        if any(successor_identity.get(key) != value for key, value in expected.items()):
            raise ValueError("successor_identity_mismatch")
        if (
            successor_manifest.request_id != request_id
            or successor_manifest.deployment_id != checkpoint.deployment_id
            or successor_manifest.deployment_epoch != checkpoint.deployment_epoch
            or successor_manifest.topology_version != checkpoint.topology_generation
            or not successor_manifest.ordered_hops
        ):
            raise ValueError("successor_identity_mismatch")
        if (
            self._graph.deployment_id != checkpoint.deployment_id
            or self._graph.deployment_epoch != checkpoint.deployment_epoch
            or self._graph.topology_version != checkpoint.topology_generation
            or self._graph.model_id != checkpoint.model_id
            or self._graph.manifest_digest != checkpoint.representation_digest
            or checkpoint.tokenizer_digest != self._tokenizer_digest
        ):
            raise ValueError("source_compatibility_binding_mismatch")

        fault = self._consume_fault(request_id)
        placement_ids = tuple(
            hop.placement_id for hop in successor_manifest.ordered_hops
        )
        if fault == "unexecuted":
            # The physical challenge is deliberately never run; the issued
            # record says so and the authority must refuse it.
            prompt_digest = _digest({"challenge": "not_executed"})
            challenge = {
                "passed": True,
                "parity_policy_digest": prompt_digest,
                "workload_envelope_digest": prompt_digest,
            }
        else:
            challenge = self._physical_challenge(
                request_id=request_id,
                placement_ids=placement_ids,
                absolute_deadline_unix_ms=checkpoint.expiry_unix_ms,
            )
        if set(challenge) != {
            "passed",
            "parity_policy_digest",
            "workload_envelope_digest",
        } or not isinstance(challenge["passed"], bool):
            raise ValueError("a6_physical_challenge_result_invalid")
        for key in ("parity_policy_digest", "workload_envelope_digest"):
            value = challenge[key]
            if (
                not isinstance(value, str)
                or len(value) != 71
                or not value.startswith("sha256:")
            ):
                raise ValueError("a6_physical_challenge_result_invalid")

        source = _compatibility(
            graph=self._graph,
            checkpoint=checkpoint,
            deployment_dir=self._deployment_dir,
            tokenizer_digest=self._tokenizer_digest,
            parity_policy_digest=str(challenge["parity_policy_digest"]),
            workload_envelope_digest=str(challenge["workload_envelope_digest"]),
        )
        successor = dict(source)
        if fault == "incompatible":
            successor["architecture_digest"] = _digest(
                {"controlled_fault": "incompatible", "source": source["architecture_digest"]}
            )
        passed = bool(challenge["passed"]) and compatibility_matches(source, successor)
        now = self._clock_ms()
        expiry = min(checkpoint.expiry_unix_ms, now + 30_000)
        if expiry <= now:
            raise ValueError("recovery_qualification_expired")
        issued_at = now
        if fault == "stale":
            # A well-formed record whose validity window already closed.
            issued_at = max(1, now - 60_000)
            expiry = max(issued_at + 1, now - 30_000)
        binding = {
            "deployment_id": self._graph.deployment_id,
            "deployment_epoch": self._graph.deployment_epoch,
            "topology_generation": self._graph.topology_version,
            "model_id": self._graph.model_id,
            "model_revision": self._graph.resolved_commit,
            "representation_digest": self._graph.manifest_digest,
            "graph_digest": _graph_digest(self._graph),
        }
        request_binding = recovery_request_binding(checkpoint)
        if fault == "foreign":
            request_binding = dict(request_binding)
            request_binding["request_id"] = f"foreign-{uuid.uuid4().hex}"
        successor_binding = recovery_successor_binding(
            successor_identity,
            successor_manifest,
        )
        if fault == "mismatched_path_attempt":
            successor_binding = dict(successor_binding)
            successor_binding["path_attempt"] = successor_binding["path_attempt"] + 1
        # The physical challenge result is reported as measured; compatibility
        # is a separate fact the authority checks against the source.
        physical = "passed" if challenge["passed"] else "failed"
        if fault == "unexecuted":
            physical = "unexecuted"
        qualification = build_recovery_qualification(
            binding,
            qualification_id=f"a6-successor-{uuid.uuid4().hex}",
            request_binding=request_binding,
            successor_binding=successor_binding,
            compatibility=successor,
            compatible=passed,
            issued_at_unix_ms=issued_at,
            expiry_unix_ms=expiry,
            reason_codes=(
                ()
                if passed
                else ("successor_compatibility_mismatch",)
                if challenge["passed"]
                else ("successor_physical_challenge_failed",)
            ),
            physical_challenge=physical,
        )
        self._record_issue(
            request_id,
            {
                "controlled_fault": fault,
                "successor_placement_ids": list(placement_ids),
                "successor_request_attempt": successor_identity.get("request_attempt"),
                "successor_path_attempt": successor_manifest.path_attempt,
                "issued_path_attempt": qualification["successor_binding"]["path_attempt"],
                "compatibility_matches_source": compatibility_matches(source, successor),
                "request_binding_matches": (
                    qualification["request_binding"] == recovery_request_binding(checkpoint)
                ),
                "physical_challenge": qualification["physical_challenge"],
                "issued_at_unix_ms": qualification["issued_at_unix_ms"],
                "expiry_unix_ms": qualification["expiry_unix_ms"],
                "measured_at_unix_ms": now,
            },
        )
        return source, qualification

    def _run_physical_challenge(
        self,
        *,
        request_id: str,
        placement_ids: tuple[str, ...],
        absolute_deadline_unix_ms: int,
    ) -> Mapping[str, Any]:
        route = self._route
        assert route is not None
        prompt, expected = route.startup_challenge
        policy_digest = _digest(
            {
                "challenge": "exact_startup_token_parity",
                "expected_output_digest": _digest(list(expected)),
                "placement_count": len(placement_ids),
            }
        )
        workload_digest = _digest(
            {
                "prompt_token_count": len(prompt),
                "maximum_new_tokens": len(expected),
                "absolute_deadline_unix_ms": absolute_deadline_unix_ms,
            }
        )
        emitted: list[int] = []

        class Sink:
            def emit(self, token_index: int, token_id: int) -> None:
                if token_index != len(emitted) or type(token_id) is not int:
                    raise RuntimeError("a6_physical_challenge_noncontiguous")
                emitted.append(token_id)

        probe_id = f"a6-qual-{hashlib.sha256(request_id.encode()).hexdigest()[:16]}-{uuid.uuid4().hex[:8]}"
        passed = False
        outcome: dict[str, Any] = {
            "passed": False,
            "outcome": "not_started",
            "placement_ids": list(placement_ids),
            "expected_token_count": len(expected),
            "observed_token_count": None,
            "emitted_token_count": 0,
            "first_mismatch_index": None,
            "error_type": None,
            "error": None,
            "release_error_type": None,
            "release_error": None,
            "participant_exits": [],
        }
        sessions = _challenge_sessions(route, placement_ids)
        try:
            if self._clock_ms() >= absolute_deadline_unix_ms:
                outcome["outcome"] = "deadline_elapsed"
                return {
                    "passed": False,
                    "parity_policy_digest": policy_digest,
                    "workload_envelope_digest": workload_digest,
                }
            self._record_challenge_in_flight(request_id, outcome)
            result = route.infer(
                prompt,
                max_new_tokens=len(expected),
                request_id=probe_id,
                sink=Sink(),
                selected_placement_ids=placement_ids,
                # Like the startup renewal, the successor challenge is compared
                # to the exact controller-owned output. Product stop tokens
                # (e.g. Qwen ``<|im_end|>``) would truncate it and fail every
                # successor; the override is request-local.
                _stop_token_ids_override=frozenset(),
            )
            observed = tuple(result.token_ids)
            passed = observed == tuple(expected) and tuple(emitted) == tuple(expected)
            outcome["observed_token_count"] = len(observed)
            outcome["outcome"] = "passed" if passed else "output_mismatch"
            if not passed:
                outcome["first_mismatch_index"] = next(
                    (
                        index
                        for index, (left, right) in enumerate(zip(observed, expected))
                        if left != right
                    ),
                    min(len(observed), len(expected)),
                )
        except Exception as error:
            passed = False
            outcome["outcome"] = "infer_error"
            outcome["error_type"] = type(error).__name__
            outcome["error"] = _bounded_message(error)
            outcome["participant_exits"] = _participant_exits(sessions)
        finally:
            outcome["emitted_token_count"] = len(emitted)
            try:
                # A route owner must not cold-restart the degraded route this
                # probe ran on: the recovery that asked for it still runs its
                # successor there.
                release = getattr(route, "release_qualification_probe", None)
                if not callable(release):
                    release = route.release_request
                release(probe_id)
            except Exception as error:
                passed = False
                outcome["release_error_type"] = type(error).__name__
                outcome["release_error"] = _bounded_message(error)
                if outcome["outcome"] == "passed":
                    outcome["outcome"] = "release_error"
            outcome["passed"] = passed
            self._record_challenge_outcome(request_id, outcome)
        return {
            "passed": passed,
            "parity_policy_digest": policy_digest,
            "workload_envelope_digest": workload_digest,
        }


__all__ = [
    "CONTROLLED_QUALIFICATION_FAULTS",
    "LiveA6RecoveryQualifier",
    "tokenizer_artifact_digest",
]
