#!/usr/bin/env python3
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
"""Seal the A3 useful-model qualification gate from executed product evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


GATE_PROTOCOL = "mycelium.a3_useful_model_qualification.v1"
DECISION_PROTOCOL = "mycelium.model_representation_decision.v2"
PREPARATION_PROTOCOL = "mycelium.model_preparation.v1"
RECOVERY_AUTHORIZATION_PROTOCOL = (
    "mycelium.private_model_preparation_recovery_authorization.v1"
)
ACQUISITION_LEDGER_PROTOCOL = "mycelium.swarm_artifact_acquisition_ledger.v1"
ACQUISITION_PROTOCOL = "mycelium.swarm_artifact_acquisition.v1"
MEMORY_AB_PROTOCOLS = {
    "mycelium.a3_memory_tier_ab.v1",
    "mycelium.a3_product_memory_tier_ab.v1",
}
PARITY_PROTOCOL = "mycelium.model_reference_parity.v1"
RUNTIME_PROTOCOL = "mycelium.distributed_model_runtime_evidence.v1"
SELECTOR_PROTOCOL = "mycelium.model_selector_switchback.v1"
UI_PROTOCOL = "mycelium.a3_ui_verification.v1"
SELECTOR_NEGATIVE_CASES = frozenset(
    {"infeasible", "stale", "incomplete", "mismatched", "unqualified", "dead"}
)
QWEN3_MODEL_ID = "Qwen/Qwen3-8B"
QWEN3_REVISION = "b968826d9c46dd6066d109eabc6255188de91218"
WORKSPACES = (
    "inference",
    "device_lab",
    "network",
    "nodes",
    "plans",
    "readiness",
    "incidents",
    "settings",
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_DECISION_FIELDS = {
    "protocol",
    "model_id",
    "revision",
    "source_quantization",
    "serving_dtype",
    "serving_quantization",
    "representation_digest",
    "conversion_authorized",
    "source_artifact_digest",
    "quantizer",
    "download_authorized",
}


class GateError(RuntimeError):
    """A stable A3 gate input or execution failure."""


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError(f"invalid_json:{path.name}") from exc
    if not isinstance(value, dict):
        raise GateError(f"non_object_json:{path.name}")
    return value


def _write_private_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _valid_revision(value: object) -> bool:
    return isinstance(value, str) and _REVISION.fullmatch(value) is not None


def _decision_valid(
    decision: Mapping[str, Any],
    *,
    model_id: str,
    revision: str,
    representation: str,
) -> bool:
    return (
        set(decision) == _DECISION_FIELDS
        and decision.get("protocol") == DECISION_PROTOCOL
        and decision.get("model_id") == model_id
        and decision.get("revision") == revision
        and isinstance(decision.get("source_quantization"), str)
        and bool(decision.get("source_quantization"))
        and isinstance(decision.get("serving_dtype"), str)
        and bool(decision.get("serving_dtype"))
        and decision.get("serving_quantization") == representation
        and _valid_digest(decision.get("representation_digest"))
        and decision.get("conversion_authorized") is True
        and _valid_digest(decision.get("source_artifact_digest"))
        and isinstance(decision.get("quantizer"), str)
        and bool(decision.get("quantizer"))
        and decision.get("download_authorized") is False
    )


def _contiguous_stages(stages: object, *, num_layers: int) -> bool:
    if not isinstance(stages, list) or len(stages) < 2:
        return False
    cursor = 0
    nodes: set[str] = set()
    for index, stage in enumerate(stages):
        if not isinstance(stage, Mapping):
            return False
        start = stage.get("start_layer")
        end = stage.get("end_layer_exclusive")
        node_id = stage.get("node_id")
        if (
            stage.get("stage_index") != index
            or type(start) is not int
            or type(end) is not int
            or start != cursor
            or end <= start
            or not isinstance(node_id, str)
            or not node_id
            or node_id in nodes
        ):
            return False
        nodes.add(node_id)
        cursor = end
    return cursor == num_layers


def _frozen_stages(stages: object) -> list[dict[str, Any]] | None:
    fields = (
        "stage_index",
        "node_id",
        "start_layer",
        "end_layer_exclusive",
        "backend",
        "decode_mode",
        "assignment_files",
        "assignment_artifact_bytes",
    )
    if not isinstance(stages, list) or not all(
        isinstance(stage, Mapping) for stage in stages
    ):
        return None
    return [
        {field: copy.deepcopy(stage.get(field)) for field in fields} for stage in stages
    ]


def _recovery_source_authority(
    recovery: Mapping[str, Any] | None,
    decision: Mapping[str, Any],
    feasibility: Mapping[str, Any],
    preparation: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return the frozen build authority when additive recovery is exact."""

    if recovery is None:
        return None
    source = recovery.get("source_authority")
    current = recovery.get("recovery_authority")
    stages = _frozen_stages(feasibility.get("stages"))
    owner_digest = _digest(dict(decision))
    immutable = {
        "model_id": decision.get("model_id"),
        "revision": decision.get("revision"),
        "source_quantization": decision.get("source_quantization"),
        "serving_dtype": decision.get("serving_dtype"),
        "serving_quantization": decision.get("serving_quantization"),
        "representation_digest": decision.get("representation_digest"),
        "source_artifact_digest": decision.get("source_artifact_digest"),
        "quantizer": decision.get("quantizer"),
        "conversion_authorized": True,
        "download_authorized": False,
        "owner_decision_digest": owner_digest,
    }
    source_started = recovery.get("source_authorized_at_unix_ms")
    source_valid_until = recovery.get("source_evidence_valid_until_unix_ms")
    recovery_started = recovery.get("recovery_authorized_at_unix_ms")
    recovery_valid_until = recovery.get("recovery_evidence_valid_until_unix_ms")
    recovered_at = recovery.get("recovered_at_unix_ms")
    if not (
        recovery.get("protocol") == RECOVERY_AUTHORIZATION_PROTOCOL
        and isinstance(source, Mapping)
        and isinstance(current, Mapping)
        and stages is not None
        and source.get("protocol") == "mycelium.model_preparation_authorization.v2"
        and current.get("protocol") == "mycelium.model_preparation_authorization.v2"
        and all(source.get(field) == value for field, value in immutable.items())
        and all(current.get(field) == value for field, value in immutable.items())
        and source.get("stages") == stages
        and current.get("stages") == stages
        and recovery.get("source_authority_digest") == _digest(dict(source))
        and recovery.get("recovery_authority_digest") == _digest(dict(current))
        and recovery.get("source_preparation_binding_digest")
        == source.get("preparation_binding_digest")
        and current.get("feasibility_digest") == feasibility.get("feasibility_digest")
        and current.get("evidence_generation") == feasibility.get("evidence_generation")
        and recovery_valid_until == feasibility.get("evidence_valid_until_unix_ms")
        and type(source_started) is int
        and type(source_valid_until) is int
        and 0 < source_started <= source_valid_until
        and type(recovery_started) is int
        and type(recovery_valid_until) is int
        and 0 < recovery_started <= recovery_valid_until
        and preparation.get("started_at_unix_ms") == recovery_started
        and type(recovered_at) is int
        and recovery_started <= recovered_at
        and recovery.get("source_completed_phase")
        in {"candidate_challenged", "seed_bound", "artifacts_acquired", "peers_staged"}
        and recovery.get("resume_from_phase")
        == recovery.get("source_completed_phase")
    ):
        return None
    return source


def _preparation_valid(
    status: Mapping[str, Any],
    decision: Mapping[str, Any],
    feasibility: Mapping[str, Any],
    *,
    model_id: str,
    revision: str,
) -> bool:
    stages = feasibility.get("stages")
    started = status.get("started_at_unix_ms")
    completed = status.get("completed_at_unix_ms")
    transfer_bytes = status.get("transfer_bytes")
    verified_bytes = status.get("verified_bytes")
    feasibility_valid_until = feasibility.get("evidence_valid_until_unix_ms")
    return (
        status.get("protocol") == PREPARATION_PROTOCOL
        and status.get("state") == "succeeded"
        and status.get("phase") is None
        and status.get("model_id") == model_id
        and status.get("revision") == revision
        and status.get("representation_digest") == decision.get("representation_digest")
        and status.get("owner_decision_digest") == _digest(dict(decision))
        and isinstance(status.get("candidate_id"), str)
        and bool(status.get("candidate_id"))
        and isinstance(stages, list)
        and type(status.get("topology_size")) is int
        and status["topology_size"] == len(stages)
        and type(transfer_bytes) is int
        and transfer_bytes > 0
        and type(verified_bytes) is int
        and verified_bytes == transfer_bytes
        and status.get("reason_code") is None
        and type(started) is int
        and type(completed) is int
        and started > 0
        and completed >= started
        and type(feasibility_valid_until) is int
        and started <= feasibility_valid_until
        and status.get("download_authorized") is False
        and status.get("activation_started") is False
    )


def _ready_acquisition(
    value: object,
    *,
    model_id: str,
    revision: str,
    decision: Mapping[str, Any],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    total = value.get("total_bytes")
    chunks = value.get("chunk_count")
    verified_chunks = value.get("verified_chunk_count")
    return (
        value.get("protocol") == ACQUISITION_PROTOCOL
        and value.get("state") == "ready"
        and value.get("phase") is None
        and value.get("model_id") == model_id
        and value.get("model_revision") == revision
        and value.get("representation_digest") == decision.get("representation_digest")
        and _valid_digest(value.get("manifest_digest"))
        and _valid_digest(value.get("assignment_digest"))
        and _valid_digest(value.get("feasibility_digest"))
        and type(value.get("evidence_generation")) is int
        and type(value.get("layer_start")) is int
        and type(value.get("layer_end_exclusive")) is int
        and value["layer_end_exclusive"] > value["layer_start"]
        and type(total) is int
        and total > 0
        and type(value.get("cached_verified_bytes")) is int
        and type(value.get("transferred_verified_bytes")) is int
        and type(value.get("origin_bytes")) is int
        and type(value.get("missing_bytes")) is int
        and value["missing_bytes"] == 0
        and type(value.get("quarantined_bytes")) is int
        and value["quarantined_bytes"] == 0
        and type(chunks) is int
        and chunks > 0
        and verified_chunks == chunks
        and value.get("reason_code") is None
        and value.get("retryable") is False
        and type(value.get("terminal_at_unix_ms")) is int
    )


def _acquisitions_valid(
    ledger: Mapping[str, Any],
    decision: Mapping[str, Any],
    feasibility: Mapping[str, Any],
    *,
    model_id: str,
    revision: str,
    acquisition_authority: Mapping[str, Any] | None = None,
) -> bool:
    history = ledger.get("history")
    authority = feasibility if acquisition_authority is None else acquisition_authority
    stages = authority.get("stages")
    if (
        ledger.get("protocol") != ACQUISITION_LEDGER_PROTOCOL
        or ledger.get("current") is not None
        or not isinstance(history, list)
        or not isinstance(stages, list)
    ):
        return False
    ready = [
        value
        for value in history
        if _ready_acquisition(
            value,
            model_id=model_id,
            revision=revision,
            decision=decision,
        )
    ]
    for stage in stages:
        if not isinstance(stage, Mapping):
            return False
        stage_range = (stage.get("start_layer"), stage.get("end_layer_exclusive"))
        matching = [
            value
            for value in ready
            if (value.get("layer_start"), value.get("layer_end_exclusive"))
            == stage_range
        ]
        current = [
            value
            for value in matching
            if value.get("feasibility_digest") == authority.get("feasibility_digest")
            and value.get("evidence_generation") == authority.get("evidence_generation")
        ]
        cold = [
            value
            for value in matching
            if value.get("transferred_verified_bytes", 0) > 0
            or value.get("origin_bytes", 0) > 0
        ]
        warm = [
            value
            for value in matching
            if value.get("cached_verified_bytes") == value.get("total_bytes")
            and value.get("transferred_verified_bytes") == 0
            and value.get("origin_bytes") == 0
        ]
        if not current or not cold or not warm:
            return False
    return True


def _feasibility_valid(
    report: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    model_id: str,
    revision: str,
    representation: str,
    now_unix_ms: int,
) -> bool:
    valid_until = report.get("evidence_valid_until_unix_ms")
    stages = report.get("stages")
    num_layers = (
        stages[-1].get("end_layer_exclusive")
        if isinstance(stages, list) and stages and isinstance(stages[-1], Mapping)
        else None
    )
    return (
        report.get("protocol") == "mycelium.model_feasibility.v1"
        and report.get("model_id") == model_id
        and report.get("revision") == revision
        and report.get("state") == "feasible"
        and report.get("provisioning_authorized") is True
        and type(valid_until) is int
        and valid_until >= now_unix_ms
        and type(report.get("evidence_generation")) is int
        and _valid_digest(report.get("feasibility_digest"))
        and report.get("source_quantization") == decision.get("source_quantization")
        and report.get("serving_dtype") == decision.get("serving_dtype")
        and report.get("serving_quantization") == representation
        and report.get("representation_digest") == decision.get("representation_digest")
        and report.get("artifact_digest") == decision.get("source_artifact_digest")
        and type(num_layers) is int
        and num_layers > 0
        and _contiguous_stages(stages, num_layers=num_layers)
    )


def _allocation_ranges(value: object) -> list[tuple[str, int, int]] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None
    ranges: list[tuple[str, int, int]] = []
    cursor = 0
    for item in value:
        if not isinstance(item, Mapping):
            return None
        node_id = item.get("node_id")
        start = item.get("start_layer")
        end = item.get("end_layer_exclusive")
        if (
            not isinstance(node_id, str)
            or not node_id
            or type(start) is not int
            or type(end) is not int
            or start != cursor
            or end <= start
        ):
            return None
        ranges.append((node_id, start, end))
        cursor = end
    return ranges


def _memory_ab_valid(
    evidence: Mapping[str, Any],
    *,
    binding_digest: str,
) -> bool:
    baseline = _allocation_ranges(evidence.get("baseline_allocation"))
    candidate = _allocation_ranges(evidence.get("candidate_allocation"))
    changed_fields = evidence.get("changed_fields")
    memory_node = evidence.get("memory_tier_node_id")
    before = evidence.get("baseline_fast_memory_bytes")
    after = evidence.get("candidate_fast_memory_bytes")
    protocol = evidence.get("protocol")
    if not (
        protocol in MEMORY_AB_PROTOCOLS
        and evidence.get("binding_digest") == binding_digest
        and _valid_digest(evidence.get("invariant_inputs_digest"))
        and isinstance(changed_fields, list)
        and isinstance(memory_node, str)
        and bool(memory_node)
        and type(before) is int
        and type(after) is int
        and before > 0
        and after > 0
        and before != after
        and baseline is not None
        and candidate is not None
        and type(evidence.get("explored_allocation_count")) is int
        and evidence["explored_allocation_count"] > 0
        and isinstance(evidence.get("rejected_intervals"), list)
    ):
        return False
    if protocol == "mycelium.a3_memory_tier_ab.v1":
        if not (
            _valid_digest(evidence.get("baseline_snapshot_digest"))
            and _valid_digest(evidence.get("candidate_snapshot_digest"))
            and evidence.get("baseline_snapshot_digest")
            != evidence.get("candidate_snapshot_digest")
            and changed_fields == [f"nodes.{memory_node}.fast_memory_bytes"]
        ):
            return False
    elif not (
        _valid_digest(evidence.get("baseline_input_digest"))
        and _valid_digest(evidence.get("candidate_input_digest"))
        and evidence.get("baseline_input_digest")
        != evidence.get("candidate_input_digest")
        and _valid_digest(evidence.get("baseline_operation_digest"))
        and _valid_digest(evidence.get("candidate_operation_digest"))
        and _valid_digest(evidence.get("baseline_feasibility_digest"))
        and _valid_digest(evidence.get("candidate_feasibility_digest"))
        and evidence.get("planner") == "capability_aware_contiguous_exact_weight_dp"
        and changed_fields == [f"placement.nodes.{memory_node}.fast_allocatable_bytes"]
    ):
        return False
    allocation_changed = baseline != candidate
    if allocation_changed:
        rejected = evidence["rejected_intervals"]
        rejection_trace = any(
            isinstance(item, Mapping)
            and item.get("node_id") == memory_node
            and item.get("reason")
            in {"insufficient_fast_memory", "fast_memory_spill_pressure"}
            for item in rejected
        )
        pressure = evidence.get("counterfactual_baseline_allocation_pressure")
        product_trace = (
            isinstance(pressure, Mapping)
            and pressure.get("node_id") == memory_node
            and pressure.get("reason") == "fast_memory_spill_pressure"
            and type(pressure.get("required_memory_bytes")) is int
            and pressure["required_memory_bytes"] > after
            and pressure.get("candidate_fast_memory_bytes") == after
            and type(pressure.get("spill_bytes")) is int
            and pressure["spill_bytes"] == pressure["required_memory_bytes"] - after
        )
        return evidence.get("result") == "allocation_changed" and (
            rejection_trace or product_trace
        )
    proof = evidence.get("stability_proof")
    return (
        evidence.get("result") == "allocation_stable"
        and isinstance(proof, Mapping)
        and proof.get("kind") in {"unique_minimum", "all_alternatives_dominated"}
        and type(proof.get("evaluated_alternative_count")) is int
        and proof["evaluated_alternative_count"] > 0
        and _valid_digest(proof.get("proof_digest"))
    )


def _parity_valid(
    evidence: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    model_id: str,
    revision: str,
) -> bool:
    tolerance = evidence.get("logit_tolerance")
    relative_tolerance = evidence.get("relative_logit_tolerance")
    cases = evidence.get("cases")
    return (
        evidence.get("protocol") == PARITY_PROTOCOL
        and evidence.get("model_id") == model_id
        and evidence.get("revision") == revision
        and evidence.get("representation_digest")
        == decision.get("representation_digest")
        and evidence.get("source_artifact_digest")
        == decision.get("source_artifact_digest")
        and _valid_digest(evidence.get("tokenizer_digest"))
        and _valid_digest(evidence.get("policy_digest"))
        and evidence.get("reference_process_participates_in_route") is False
        and _valid_digest(evidence.get("reference_runtime_digest"))
        and _valid_digest(evidence.get("distributed_runtime_digest"))
        and isinstance(tolerance, (int, float))
        and not isinstance(tolerance, bool)
        and math.isfinite(float(tolerance))
        and tolerance >= 0
        and isinstance(relative_tolerance, (int, float))
        and not isinstance(relative_tolerance, bool)
        and math.isfinite(float(relative_tolerance))
        and relative_tolerance >= 0
        and isinstance(cases, list)
        and bool(cases)
        and all(
            isinstance(case, Mapping)
            and _valid_digest(case.get("prompt_tokens_digest"))
            and case.get("greedy_tokens_match") is True
            and case.get("finite_logits") is True
            and case.get("within_tolerance") is True
            and isinstance(case.get("maximum_logit_error"), (int, float))
            and not isinstance(case.get("maximum_logit_error"), bool)
            and math.isfinite(float(case["maximum_logit_error"]))
            and isinstance(case.get("maximum_relative_logit_error"), (int, float))
            and not isinstance(case.get("maximum_relative_logit_error"), bool)
            and math.isfinite(float(case["maximum_relative_logit_error"]))
            for case in cases
        )
    )


def _runtime_valid(
    evidence: Mapping[str, Any],
    decision: Mapping[str, Any],
    *,
    model_id: str,
    revision: str,
) -> bool:
    stages = evidence.get("stages")
    cleanup = evidence.get("cleanup")
    return (
        evidence.get("protocol") == RUNTIME_PROTOCOL
        and evidence.get("model_id") == model_id
        and evidence.get("revision") == revision
        and evidence.get("representation_digest")
        == decision.get("representation_digest")
        and evidence.get("simulated") is False
        and isinstance(stages, list)
        and len(stages) >= 2
        and all(
            isinstance(stage, Mapping)
            and stage.get("loaded_only_assignment_tensors") is True
            and type(stage.get("prefill_applied_operations")) is int
            and stage["prefill_applied_operations"] > 0
            and type(stage.get("decode_applied_operations")) is int
            and stage["decode_applied_operations"] > 0
            and type(stage.get("active_kv_bytes")) is int
            and stage["active_kv_bytes"] > 0
            and type(stage.get("frames_sent")) is int
            and stage["frames_sent"] > 0
            and type(stage.get("frames_received")) is int
            and stage["frames_received"] > 0
            for stage in stages
        )
        and isinstance(cleanup, Mapping)
        and all(
            isinstance(cleanup.get(kind), Mapping)
            and cleanup[kind].get("active_kv_states") == 0
            and cleanup[kind].get("active_kv_bytes") == 0
            for kind in ("completion", "cancellation", "shutdown")
        )
    )


def _route_advanced(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    before_counters = before.get("counters")
    after_counters = after.get("counters")
    if not isinstance(before_counters, Mapping) or not isinstance(
        after_counters, Mapping
    ):
        return False
    return (
        before.get("route_alive") is True
        and after.get("route_alive") is True
        and before.get("simulated") is False
        and after.get("simulated") is False
        and before.get("route_identity_digest") == after.get("route_identity_digest")
        and _valid_digest(after.get("route_identity_digest"))
        and before.get("deployment_id") == after.get("deployment_id")
        and isinstance(after.get("deployment_id"), str)
        and before_counters.get("fatal") is None
        and after_counters.get("fatal") is None
        and int(after_counters.get("frames_sent", 0))
        > int(before_counters.get("frames_sent", 0))
        and int(after_counters.get("frames_received", 0))
        > int(before_counters.get("frames_received", 0))
        and int(after_counters.get("applied_operation_count", 0))
        > int(before_counters.get("applied_operation_count", 0))
    )


def _selector_valid(
    evidence: Mapping[str, Any],
    *,
    deployment_id: object,
    model_id: str,
) -> bool:
    incumbent_id = evidence.get("incumbent_deployment_id")
    rejections = evidence.get("negative_rejections")
    if (
        not isinstance(incumbent_id, str)
        or not incumbent_id
        or not isinstance(rejections, list)
        or len(rejections) != len(SELECTOR_NEGATIVE_CASES)
    ):
        return False
    cases: set[str] = set()
    deployment_ids: set[str] = set()
    for rejection in rejections:
        if not isinstance(rejection, Mapping):
            return False
        case = rejection.get("case")
        rejected_id = rejection.get("deployment_id")
        selection_error = rejection.get("selection_error")
        model_blocker = rejection.get("model_blocker")
        if (
            not isinstance(case, str)
            or case not in SELECTOR_NEGATIVE_CASES
            or case in cases
            or not isinstance(rejected_id, str)
            or not rejected_id
            or rejected_id in {incumbent_id, deployment_id}
            or rejected_id in deployment_ids
            or rejection.get("request_path") != "/__mycelium/deployments/select"
            or rejection.get("http_status") != 409
            or selection_error not in {"deployment_unknown", "deployment_not_qualified"}
            or not isinstance(model_blocker, str)
            or not model_blocker
            or len(model_blocker) > 128
            or "/" in model_blocker
            or rejection.get("selected_deployment_id_before") != incumbent_id
            or rejection.get("selected_deployment_id_after") != incumbent_id
            or rejection.get("switching_allowed_before") is not True
            or rejection.get("switching_allowed_after") is not True
        ):
            return False
        cases.add(case)
        deployment_ids.add(rejected_id)
    return (
        evidence.get("protocol") == SELECTOR_PROTOCOL
        and evidence.get("candidate_deployment_id") == deployment_id
        and evidence.get("candidate_model_id") == model_id
        and evidence.get("candidate_currently_qualified") is True
        and evidence.get("candidate_was_selectable") is True
        and evidence.get("candidate_request_completed") is True
        and incumbent_id != deployment_id
        and evidence.get("incumbent_reselected") is True
        and evidence.get("incumbent_request_completed_after_switchback") is True
        and cases == SELECTOR_NEGATIVE_CASES
        and evidence.get("incumbent_request_completed_after_negative_gate") is True
        and evidence.get("selection_scope") == "future_requests"
    )


def _qwen3_honest(value: Mapping[str, Any], *, now_unix_ms: int) -> bool:
    valid_until = value.get("evidence_valid_until_unix_ms")
    return (
        value.get("model_id") == QWEN3_MODEL_ID
        and value.get("revision") == QWEN3_REVISION
        and value.get("catalog_state") == "compatible"
        and value.get("qualified") is False
        and value.get("selectable") is False
        and value.get("provisioning_started") is False
        and value.get("representation_authorized") is False
        and isinstance(value.get("reason"), str)
        and bool(value.get("reason"))
        and type(valid_until) is int
        and valid_until >= now_unix_ms
    )


def _ui_valid(value: Mapping[str, Any]) -> bool:
    workspaces = value.get("workspaces")
    return (
        value.get("protocol") == UI_PROTOCOL
        and isinstance(workspaces, Mapping)
        and set(workspaces) == set(WORKSPACES)
        and all(workspaces[name] is True for name in WORKSPACES)
        and value.get("refresh_reconstructed") is True
        and value.get("navigation_reconstructed") is True
        and value.get("back_forward_reconstructed") is True
        and value.get("reconnect_reconstructed") is True
        and value.get("second_session_isolated") is True
        and value.get("internal_milestone_labels_absent") is True
        and value.get("private_paths_absent") is True
    )


def evaluate(
    *,
    decision: Mapping[str, Any],
    feasibility: Mapping[str, Any],
    preparation: Mapping[str, Any],
    acquisitions: Mapping[str, Any],
    memory_ab: Mapping[str, Any],
    parity: Mapping[str, Any],
    runtime: Mapping[str, Any],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    inference: Mapping[str, Any],
    selector: Mapping[str, Any],
    qwen3: Mapping[str, Any],
    ui: Mapping[str, Any],
    model_id: str,
    revision: str,
    representation: str,
    now_unix_ms: int,
    recovery: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not model_id or not _valid_revision(revision) or not representation:
        raise GateError("expected_model_binding_invalid")
    detached_decision = copy.deepcopy(dict(decision))
    decision_digest = _digest(detached_decision)
    binding_digest = _digest(
        {
            "model_id": model_id,
            "revision": revision,
            "representation": representation,
            "representation_digest": decision.get("representation_digest"),
            "source_artifact_digest": decision.get("source_artifact_digest"),
            "quantizer": decision.get("quantizer"),
            "feasibility_digest": feasibility.get("feasibility_digest"),
            "evidence_generation": feasibility.get("evidence_generation"),
        }
    )
    recovery_source = _recovery_source_authority(
        recovery,
        decision,
        feasibility,
        preparation,
    )
    recovery_valid = recovery is None or recovery_source is not None
    checks = {
        "exact_representation_decision_is_authorized": _decision_valid(
            decision,
            model_id=model_id,
            revision=revision,
            representation=representation,
        ),
        "fresh_feasibility_binds_decision_and_contiguous_route": _feasibility_valid(
            feasibility,
            decision,
            model_id=model_id,
            revision=revision,
            representation=representation,
            now_unix_ms=(
                preparation["started_at_unix_ms"]
                if type(preparation.get("started_at_unix_ms")) is int
                else now_unix_ms
            ),
        ),
        "preparation_completed_under_exact_owner_authority": _preparation_valid(
            preparation,
            decision,
            feasibility,
            model_id=model_id,
            revision=revision,
        ),
        "durable_preparation_recovery_is_bound": recovery_valid,
        "cold_and_warm_assignment_acquisition_passed": _acquisitions_valid(
            acquisitions,
            decision,
            feasibility,
            model_id=model_id,
            revision=revision,
            acquisition_authority=recovery_source,
        ),
        "memory_tier_ab_is_executed_and_traceable": _memory_ab_valid(
            memory_ab,
            binding_digest=binding_digest,
        ),
        "independent_reference_parity_passed": _parity_valid(
            parity,
            decision,
            model_id=model_id,
            revision=revision,
        ),
        "distributed_runtime_and_kv_cleanup_passed": _runtime_valid(
            runtime,
            decision,
            model_id=model_id,
            revision=revision,
        ),
        "physical_route_advanced_without_fallback": _route_advanced(before, after),
        "browser_inference_streamed_real_output": (
            inference.get("terminal_state") == "completed"
            and isinstance(inference.get("request_id"), str)
            and bool(inference.get("request_id"))
            and isinstance(inference.get("output"), str)
            and bool(inference.get("output").strip())
            and type(inference.get("output_token_count")) is int
            and inference["output_token_count"] > 0
            and inference.get("backend_fallback") is False
        ),
        "qualified_selector_and_incumbent_switchback_passed": _selector_valid(
            selector,
            deployment_id=after.get("deployment_id"),
            model_id=model_id,
        ),
        "qwen3_is_truthfully_current_blocked": _qwen3_honest(
            qwen3,
            now_unix_ms=now_unix_ms,
        ),
        "all_product_workspaces_and_browser_reconstruction_passed": _ui_valid(ui),
    }
    result: dict[str, Any] = {
        "protocol": GATE_PROTOCOL,
        "passed": all(checks.values()),
        "model": {
            "model_id": model_id,
            "revision": revision,
            "serving_quantization": representation,
            "representation_digest": decision.get("representation_digest"),
            "source_artifact_digest": decision.get("source_artifact_digest"),
            "quantizer": decision.get("quantizer"),
            "owner_decision_digest": decision_digest,
        },
        "authority": {
            "binding_digest": binding_digest,
            "feasibility_digest": feasibility.get("feasibility_digest"),
            "evidence_generation": feasibility.get("evidence_generation"),
            "valid_until_unix_ms": feasibility.get("evidence_valid_until_unix_ms"),
            "preparation_digest": _digest(dict(preparation)),
            "acquisition_ledger_digest": _digest(dict(acquisitions)),
            "recovery_authorization_digest": (
                None if recovery is None else _digest(dict(recovery))
            ),
        },
        "route": {
            "deployment_id": after.get("deployment_id"),
            "route_identity_digest": after.get("route_identity_digest"),
            "stage_count": len(runtime.get("stages", []))
            if isinstance(runtime.get("stages"), list)
            else 0,
        },
        "inference": {
            "request_id": inference.get("request_id"),
            "output": inference.get("output"),
            "output_token_count": inference.get("output_token_count"),
        },
        "checks": checks,
    }
    result["evidence_digest"] = _digest(result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seal A3 from representation, planner, runtime, and browser evidence."
    )
    for name in (
        "decision",
        "feasibility",
        "preparation",
        "acquisitions",
        "memory-ab",
        "parity",
        "runtime",
        "before",
        "after",
        "inference",
        "selector",
        "qwen3",
        "ui",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--representation", required=True)
    parser.add_argument("--now-unix-ms", type=int, required=True)
    parser.add_argument("--recovery", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = evaluate(
        decision=_read_object(args.decision),
        feasibility=_read_object(args.feasibility),
        preparation=_read_object(args.preparation),
        acquisitions=_read_object(args.acquisitions),
        memory_ab=_read_object(args.memory_ab),
        parity=_read_object(args.parity),
        runtime=_read_object(args.runtime),
        before=_read_object(args.before),
        after=_read_object(args.after),
        inference=_read_object(args.inference),
        selector=_read_object(args.selector),
        qwen3=_read_object(args.qwen3),
        ui=_read_object(args.ui),
        model_id=args.model_id,
        revision=args.revision,
        representation=args.representation,
        now_unix_ms=args.now_unix_ms,
        recovery=None if args.recovery is None else _read_object(args.recovery),
    )
    _write_private_json(args.output, result)
    print(json.dumps({"output": str(args.output), **result}, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
