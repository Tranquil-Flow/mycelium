# SPDX-License-Identifier: AGPL-3.0-only
"""A11 target-authoritative speculative decoding: closed contracts plus the
deterministic verifier transaction half.

CAPABILITY-Named replacement for the unintegrated ``mycelium.m20_speculative_*``
records, per the A11 specification section 4.

Implemented here:

- closed, canonically digested validators for the four A11 records
  (``mycelium.speculative_decode_plan.v1``, ``..._runtime.v1``,
  ``..._benchmark.v1``, ``..._qualification.v1``);
- the frozen adaptive-deadline arithmetic and the frozen circuit-breaker
  decision;
- the **deterministic** half of the A11 runtime cycle (specification section 5):
  a target-verification transaction state machine with the three frozen modes
  (``rollback_to_prefix``, ``commit_verified_prefix``,
  ``commit_verified_prefix_plus_target_correction``), one bounded causal target
  call per transaction, prefix-parity checking against sequential target
  execution, an atomic/monotonic/idempotent commit ledger, fail-closed conflict
  handling, and deterministic request admission with sampling rejection.

The transaction half runs on an **injected decision source and an in-process
prefix ledger**. It is not the physically qualified A10 verifier, not a model
runtime, and it moves no proposal: nothing here loads a model, touches KV bytes,
transfers a proposal, runs a benchmark, publishes a decision, or qualifies a
deployment. Every remaining execution seam -- private proposal transfer,
live-route benchmark execution, physical qualification and browser projection --
raises :class:`SpeculativeDecodeUnavailable` with a closed reason code and
returns no document.

The A11 gate stays ``design_only``: this module earns no closure requirement and
must not be read as a physical, benefit, or qualification claim.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any


PLAN_PROTOCOL = "mycelium.speculative_decode_plan.v1"
RUNTIME_PROTOCOL = "mycelium.speculative_decode_runtime.v1"
BENCHMARK_PROTOCOL = "mycelium.speculative_decode_benchmark.v1"
QUALIFICATION_PROTOCOL = "mycelium.speculative_decode_qualification.v1"

ENABLED_DECISION = "qualified_enabled"
DISABLED_DECISION = "measured_disabled"
DISABLED_REASONS = frozenset(
    {
        "no_authorized_local_draft",
        "incompatible_token_semantics",
        "target_verifier_unavailable",
        "parity_failed",
        "insufficient_acceptance",
        "draft_deadline_exceeded",
        "material_gain_not_observed",
        "confidence_bound_not_met",
    }
)
# Capability reasons: the evaluation could not obtain a performance sample at
# all. Missing measurements stay ``None``; they never become zero.
NO_SAMPLE_REASONS = frozenset({"no_authorized_local_draft", "target_verifier_unavailable"})
# Correctness reasons: evaluation stopped on a correctness gate, so no
# performance measurement may be attached.
CORRECTNESS_REASONS = frozenset({"incompatible_token_semantics", "parity_failed"})
# Performance reasons: a real measured window must back the decision.
SAMPLE_REQUIRED_REASONS = frozenset(
    {"insufficient_acceptance", "draft_deadline_exceeded", "material_gain_not_observed", "confidence_bound_not_met"}
)

DEADLINE_MINIMUM_MS = 5.0
DEADLINE_MAXIMUM_MS = 100.0
DEADLINE_WARM_P95_MULTIPLIER = 0.75
DEADLINE_UNKNOWN_REASON = "warm_target_only_p95_tpot_unknown"

CIRCUIT_BREAKER_CONSECUTIVE_DEADLINES = 3
CIRCUIT_BREAKER_ROLLING_WINDOW_CYCLES = 20
CIRCUIT_BREAKER_COOLDOWN_SECONDS = 300
CIRCUIT_BREAKER_STATES = frozenset({"closed", "open_target_only", "revoked_requalification_required"})

BENCHMARK_MEASURED_WINDOWS = 6
BENCHMARK_PAIRED_ORDER = ("target_only", "speculative", "target_only")
BENCHMARK_WINDOW_MODE_ORDER = (
    "target_only",
    "speculative",
    "speculative",
    "target_only",
    "target_only",
    "speculative",
)
BENCHMARK_WARMUPS_PER_MODE = 1
BENCHMARK_MINIMUM_COMPLETED_REQUESTS = 12
BENCHMARK_MINIMUM_COMMITTED_TOKENS = 384
BENCHMARK_MINIMUM_BUCKETS = 2
BENCHMARK_IMPROVEMENT_FRACTION = 0.10
BENCHMARK_PAIRED_LOWER_BOUND_FRACTION = 0.10
BENCHMARK_MAXIMUM_TTFT_REGRESSION_FRACTION = 0.10
BENCHMARK_CONFIDENCE = 0.95
BENCHMARK_INVALIDATION_REASONS = frozenset(
    {
        "route_churn",
        "stale_qualification",
        "incarnation_change",
        "thermal_or_power_drift",
        "missing_samples",
        "configuration_mismatch",
    }
)
BENCHMARK_BLOCKING_CONDITIONS = frozenset(
    {
        "material_gain_below_threshold",
        "paired_lower_bound_below_threshold",
        "ttft_regression_above_limit",
        "benchmark_pair_invalid",
    }
)

PLAN_PRIVACY_STATEMENT = (
    "no prompts, output text, raw token ids, logits, tensors, activations, kv bytes, "
    "sampler state, credentials, keys, raw endpoints, addresses, usernames, hostnames, "
    "private paths, command lines, or exception strings"
)

PROPOSAL_MAXIMUM_WIDTH_BOUND = 16
PROPOSAL_MAXIMUM_COUNT_BOUND = 512
PROPOSAL_MAXIMUM_BYTES_BOUND = 8192

# The only qualified generation policy (specification section 3). An unsupported or
# unknown policy fails speculative admission; it is never silently changed.
QUALIFIED_GENERATION_POLICIES = frozenset({"deterministic_greedy"})
# The three frozen transaction modes of the target verification operation.
TRANSACTION_MODES = (
    "rollback_to_prefix",
    "commit_verified_prefix",
    "commit_verified_prefix_plus_target_correction",
)
# Internal deterministic state record of the in-process transaction state machine.
TRANSACTION_STATE_KIND = "mycelium.speculative_decode_transaction_state"
TRANSACTION_COMMIT_STATES = (
    "open",
    "verified_prefix_ready",
    "target_correction_ready",
    "committed",
    "committed_with_correction",
    "rolled_back",
)
TRANSACTION_FAILURES = frozenset({"parity_failed"})

SCAFFOLD_STAGE = "scaffold"
IMPLEMENTED_CLAIM = (
    "closed contract validation, policy arithmetic and the deterministic in-process "
    "verifier transaction semantics only"
)
CLAIM_BOUNDARY = (
    "closed contract validation, policy arithmetic and deterministic in-process "
    "transaction/admission semantics only; no physical target verification, no proposal "
    "transfer, no speculative decoding on a live route, no benchmark execution, no "
    "benefit, no enabled decision, no measured-disabled evaluation, no qualification claim"
)
DETERMINISTIC_VERIFIER_CLAIM = (
    "deterministic in-process transaction and admission semantics only; no physical "
    "target verification, no proposal transfer, no benchmark execution, no qualification claim"
)

DIGEST_FIELDS = {
    PLAN_PROTOCOL: "plan_digest",
    RUNTIME_PROTOCOL: "runtime_digest",
    BENCHMARK_PROTOCOL: "benchmark_digest",
    QUALIFICATION_PROTOCOL: "qualification_digest",
}

_PRIVATE_KEY_MARKERS = (
    "prompt",
    "output_text",
    "raw_token",
    "token_id",
    "logit",
    "tensor",
    "activation",
    "kv_bytes",
    "sampler",
    "credential",
    "api_key",
    "secret",
    "private_path",
    "command_line",
    "hostname",
    "username",
    "address",
    "exception",
)


class SpeculativeDecodeContractError(ValueError):
    """A closed A11 contract was violated. ``code`` is the closed reason."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")

    def __str__(self) -> str:  # pragma: no cover - exercised through message equality
        return self.code if not self.detail else f"{self.code}: {self.detail}"


class SpeculativeDecodeUnavailable(RuntimeError):
    """An A11 execution seam is not implemented. Never a silent success."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")

    def __str__(self) -> str:  # pragma: no cover - exercised through message equality
        return self.code if not self.detail else f"{self.code}: {self.detail}"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _closed(value: Any, fields: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(code)
    return copy.deepcopy(dict(value))


def _text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(code)
    return value


def _bool_or_none(value: Any, code: str) -> bool | None:
    if value is not None and type(value) is not bool:
        raise ValueError(code)
    return value


def _integer(value: Any, code: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(code)
    if maximum is not None and value > maximum:
        raise ValueError(code)
    return value


def _number(value: Any, code: str, *, minimum: float = 0.0, nullable: bool = False) -> float | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(code)
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(code)
    return result


def _sha256(value: Any, code: str) -> str:
    text = _text(value, code)
    if not text.startswith("sha256:") or len(text) != 71:
        raise ValueError(code)
    try:
        int(text.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise ValueError(code) from exc
    return text


def _exact(value: Any, expected: Any, code: str) -> Any:
    if value != expected:
        raise ValueError(code)
    return value


def seal(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a closed A11 record with its canonical self-digest reset."""

    result = copy.deepcopy(dict(document))
    protocol = result.get("protocol")
    if protocol not in DIGEST_FIELDS:
        raise SpeculativeDecodeContractError("unknown_protocol", str(protocol))
    result.pop(DIGEST_FIELDS[protocol], None)
    result[DIGEST_FIELDS[protocol]] = _digest(result)
    return result


def _verify_self_digest(document: dict[str, Any], protocol: str, code: str) -> None:
    digest_field = DIGEST_FIELDS[protocol]
    supplied = _sha256(document.get(digest_field), code)
    unsigned = copy.deepcopy(document)
    del unsigned[digest_field]
    if supplied != _digest(unsigned):
        raise ValueError(code)


# ---------------------------------------------------------------------------
# Adaptive deadline and circuit breaker (frozen policy arithmetic)
# ---------------------------------------------------------------------------


def admit_proposal_deadline(
    *,
    warm_target_only_p95_tpot_ms: float | None,
    remaining_request_deadline_ms: float | None,
) -> dict[str, Any]:
    """Compute the admitted proposal deadline: ``min(100, max(5, 0.75 * p95))``.

    An unknown warm baseline is never replaced by a default: the result is
    inadmissible and carries ``DEADLINE_UNKNOWN_REASON``.
    """

    if warm_target_only_p95_tpot_ms is None:
        return {
            "admitted_deadline_ms": None,
            "clamp_reason": DEADLINE_UNKNOWN_REASON,
            "admissible": False,
        }
    baseline = _number(warm_target_only_p95_tpot_ms, "warm_target_only_p95_tpot_invalid")
    if remaining_request_deadline_ms is None:
        return {
            "admitted_deadline_ms": None,
            "clamp_reason": "request_deadline_unknown",
            "admissible": False,
        }
    remaining = _number(remaining_request_deadline_ms, "remaining_request_deadline_invalid")
    if remaining <= 0.0:
        return {"admitted_deadline_ms": None, "clamp_reason": "request_deadline_expired", "admissible": False}
    raw = DEADLINE_WARM_P95_MULTIPLIER * baseline
    if raw < DEADLINE_MINIMUM_MS:
        admitted, reason = DEADLINE_MINIMUM_MS, "floor_applied"
    elif raw > DEADLINE_MAXIMUM_MS:
        admitted, reason = DEADLINE_MAXIMUM_MS, "ceiling_applied"
    else:
        admitted, reason = raw, "within_bounds"
    if remaining < admitted:
        return {"admitted_deadline_ms": float(remaining), "clamp_reason": "request_deadline_clamp", "admissible": True}
    return {"admitted_deadline_ms": float(admitted), "clamp_reason": reason, "admissible": True}


_CYCLE_FIELDS = frozenset({"deadline_missed", "speculative_cycle_ms", "equivalent_target_only_ms"})


def circuit_breaker_decision(
    *,
    parity_violation: bool = False,
    compatibility_violation: bool = False,
    cycles: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Frozen A11 circuit breaker: three consecutive deadlines, any compatibility
    or parity violation, or a 20-cycle rolling window without improvement."""

    if type(parity_violation) is not bool or type(compatibility_violation) is not bool:
        raise SpeculativeDecodeContractError("circuit_breaker_input_invalid", "violation flags must be bool")
    observed: list[dict[str, Any]] = []
    for index, cycle in enumerate(cycles):
        try:
            entry = _closed(cycle, _CYCLE_FIELDS, "circuit_breaker_cycle_invalid")
            if type(entry["deadline_missed"]) is not bool:
                raise ValueError("circuit_breaker_cycle_invalid")
            entry["speculative_cycle_ms"] = _number(
                entry["speculative_cycle_ms"], "circuit_breaker_cycle_invalid", nullable=True
            )
            entry["equivalent_target_only_ms"] = _number(
                entry["equivalent_target_only_ms"], "circuit_breaker_cycle_invalid", nullable=True
            )
        except ValueError as exc:
            raise SpeculativeDecodeContractError("circuit_breaker_cycle_invalid", f"index={index}") from exc
        observed.append(entry)

    if parity_violation or compatibility_violation:
        return {
            "state": "revoked_requalification_required",
            "reason": "compatibility_or_parity_violation",
            "cooldown_seconds": None,
        }

    tail = observed[-CIRCUIT_BREAKER_CONSECUTIVE_DEADLINES:]
    if len(tail) == CIRCUIT_BREAKER_CONSECUTIVE_DEADLINES and all(item["deadline_missed"] for item in tail):
        return {
            "state": "open_target_only",
            "reason": "consecutive_proposal_deadlines",
            "cooldown_seconds": CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        }

    window = observed[-CIRCUIT_BREAKER_ROLLING_WINDOW_CYCLES:]
    if len(window) == CIRCUIT_BREAKER_ROLLING_WINDOW_CYCLES:
        pairs = [
            (item["speculative_cycle_ms"], item["equivalent_target_only_ms"])
            for item in window
            if item["speculative_cycle_ms"] is not None and item["equivalent_target_only_ms"] is not None
        ]
        if len(pairs) < CIRCUIT_BREAKER_ROLLING_WINDOW_CYCLES:
            return {"state": "closed", "reason": "insufficient_performance_observations", "cooldown_seconds": None}
        speculative_mean = sum(pair[0] for pair in pairs) / len(pairs)
        equivalent_mean = sum(pair[1] for pair in pairs) / len(pairs)
        if speculative_mean >= equivalent_mean:
            return {
                "state": "open_target_only",
                "reason": "rolling_performance_window_not_improved",
                "cooldown_seconds": CIRCUIT_BREAKER_COOLDOWN_SECONDS,
            }

    return {"state": "closed", "reason": "within_policy", "cooldown_seconds": None}


# ---------------------------------------------------------------------------
# Plan contract
# ---------------------------------------------------------------------------

_PLAN_FIELDS = frozenset(
    {
        "protocol",
        "decision",
        "target",
        "draft",
        "verifier",
        "compatibility",
        "workload",
        "proposal_bounds",
        "deadline_policy",
        "circuit_breaker_policy",
        "resource_envelope",
        "observation",
        "target_only_fallback",
        "benchmark_threshold",
        "privacy",
        "plan_digest",
    }
)
_IDENTITY_FIELDS = frozenset({"role", "identity_digest", "representation_digest", "runtime_incarnation"})
_COMPATIBILITY_FIELDS = frozenset(
    {
        "tokenizer_digest_match",
        "canonical_token_mapping_match",
        "special_token_semantics_match",
        "position_semantics_match",
        "attention_mask_match",
        "context_truncation_match",
        "generation_policy",
        "kv_alias",
        "kv_transfer",
    }
)
_COMPATIBILITY_MATCH_FIELDS = (
    "tokenizer_digest_match",
    "canonical_token_mapping_match",
    "special_token_semantics_match",
    "position_semantics_match",
    "attention_mask_match",
    "context_truncation_match",
)
_VERIFIER_FIELDS = frozenset({"capability", "qualified_width", "target_runtime_binding_digest", "status"})
_WORKLOAD_FIELDS = frozenset({"workload_id", "route_digest", "session_generation"})
_PROPOSAL_BOUNDS_FIELDS = frozenset({"maximum_width", "maximum_count", "maximum_bytes"})
_DEADLINE_POLICY_FIELDS = frozenset(
    {
        "maximum_deadline_ms",
        "minimum_deadline_ms",
        "warm_p95_multiplier",
        "warm_target_only_p95_tpot_ms",
        "remaining_request_deadline_ms",
        "source",
    }
)
_CIRCUIT_BREAKER_POLICY_FIELDS = frozenset({"consecutive_deadlines", "rolling_window_cycles", "cooldown_seconds"})
_RESOURCE_FIELDS = frozenset(
    {
        "target_kv_bytes",
        "draft_kv_bytes",
        "verifier_workspace_bytes",
        "proposal_buffer_bytes",
        "transport_buffer_bytes",
        "runtime_batch_slots",
        "independent_reservations",
    }
)
_OBSERVATION_FIELDS = frozenset(
    {
        "sample_count",
        "observed_wall_clock_reduction_fraction",
        "paired_lower_bound_fraction",
        "ttft_regression_fraction",
    }
)
_DECISION_FIELDS = frozenset({"state", "reason", "material_gain_threshold", "target_only_fallback"})
_BENCHMARK_THRESHOLD_FIELDS = frozenset(
    {
        "improvement_fraction",
        "paired_lower_bound_fraction",
        "confidence",
        "maximum_ttft_regression_fraction",
    }
)

_GENERATION_POLICY = "deterministic_greedy"
_MATERIAL_THRESHOLD = BENCHMARK_IMPROVEMENT_FRACTION


def _identity(value: Any, role: str, code: str) -> dict[str, Any]:
    result = _closed(value, _IDENTITY_FIELDS, code)
    if result["role"] != role:
        raise ValueError(code)
    _sha256(result["identity_digest"], code)
    _sha256(result["representation_digest"], code)
    _sha256(result["runtime_incarnation"], code)
    return result


def _validate_plan_decision(
    decision: dict[str, Any],
    *,
    verifier: dict[str, Any],
    compatibility: dict[str, Any],
    observation: dict[str, Any],
) -> None:
    state = decision["state"]
    if state not in {ENABLED_DECISION, DISABLED_DECISION}:
        raise ValueError("speculative_decode_plan_invalid")
    _text(decision["reason"], "speculative_decode_plan_invalid")
    _exact(decision["material_gain_threshold"], _MATERIAL_THRESHOLD, "speculative_decode_plan_invalid")
    if decision["target_only_fallback"] is not True:
        raise ValueError("speculative_decode_plan_invalid")

    for key in _COMPATIBILITY_MATCH_FIELDS:
        _bool_or_none(compatibility[key], "speculative_decode_plan_invalid")
    if compatibility["kv_alias"] is not False or compatibility["kv_transfer"] is not False:
        raise ValueError("speculative_decode_plan_invalid")
    policy = _text(compatibility["generation_policy"], "speculative_decode_plan_invalid")

    if policy != _GENERATION_POLICY:
        if state == ENABLED_DECISION or decision["reason"] != "incompatible_token_semantics":
            raise SpeculativeDecodeContractError("unsupported_generation_policy", policy)

    sample_count = _integer(observation["sample_count"], "speculative_decode_plan_invalid")
    measurements = {
        key: _number(observation[key], "speculative_decode_plan_invalid", nullable=True)
        for key in _OBSERVATION_FIELDS
        if key != "sample_count"
    }
    reduction = measurements["observed_wall_clock_reduction_fraction"]
    lower_bound = measurements["paired_lower_bound_fraction"]
    ttft = measurements["ttft_regression_fraction"]

    if state == ENABLED_DECISION:
        if verifier["status"] != "qualified":
            raise ValueError("speculative_decode_plan_invalid")
        if any(compatibility[key] is not True for key in _COMPATIBILITY_MATCH_FIELDS):
            raise ValueError("speculative_decode_plan_invalid")
        if sample_count < 1 or reduction is None or lower_bound is None or ttft is None:
            raise SpeculativeDecodeContractError(
                "measurement_required_for_performance_reason", "enabled decision needs a complete measured window"
            )
        if reduction < _MATERIAL_THRESHOLD or lower_bound < BENCHMARK_PAIRED_LOWER_BOUND_FRACTION:
            raise SpeculativeDecodeContractError("enabled_decision_below_material_threshold")
        if ttft > BENCHMARK_MAXIMUM_TTFT_REGRESSION_FRACTION:
            raise SpeculativeDecodeContractError("enabled_decision_below_material_threshold", "ttft regression")
        return

    reason = decision["reason"]
    if reason not in DISABLED_REASONS:
        raise ValueError("speculative_decode_plan_invalid")
    if reason in NO_SAMPLE_REASONS:
        if sample_count != 0 or any(value is not None for value in measurements.values()):
            raise SpeculativeDecodeContractError("capability_reason_may_not_carry_measurement", reason)
    elif reason in CORRECTNESS_REASONS:
        if any(value is not None for value in measurements.values()):
            raise SpeculativeDecodeContractError("capability_reason_may_not_carry_measurement", reason)
    else:
        if sample_count < 1:
            raise SpeculativeDecodeContractError("measurement_required_for_performance_reason", reason)
        if reason == "material_gain_not_observed" and (reduction is None or reduction >= _MATERIAL_THRESHOLD):
            raise SpeculativeDecodeContractError("measurement_required_for_performance_reason", reason)
        if reason == "confidence_bound_not_met" and (lower_bound is None or lower_bound >= _MATERIAL_THRESHOLD):
            raise SpeculativeDecodeContractError("measurement_required_for_performance_reason", reason)


def validate_speculative_decode_plan(document: Mapping[str, Any]) -> dict[str, Any]:
    """Closed validation of ``mycelium.speculative_decode_plan.v1``."""

    try:
        result = _closed(document, _PLAN_FIELDS, "speculative_decode_plan_invalid")
        if result["protocol"] != PLAN_PROTOCOL:
            raise ValueError("speculative_decode_plan_invalid")
        target = _identity(result["target"], "target", "speculative_decode_plan_invalid")
        draft = _identity(result["draft"], "draft", "speculative_decode_plan_invalid")
        if target["identity_digest"] == draft["identity_digest"] or (
            target["representation_digest"] == draft["representation_digest"]
        ):
            raise SpeculativeDecodeContractError("duplicate_model_identity")

        verifier = _closed(result["verifier"], _VERIFIER_FIELDS, "speculative_decode_plan_invalid")
        if verifier["capability"] != "a10_target_verifier":
            raise ValueError("speculative_decode_plan_invalid")
        _integer(verifier["qualified_width"], "speculative_decode_plan_invalid", minimum=1)
        _sha256(verifier["target_runtime_binding_digest"], "speculative_decode_plan_invalid")
        if verifier["status"] not in {"qualified", "unqualified", "unknown"}:
            raise ValueError("speculative_decode_plan_invalid")

        compatibility = _closed(result["compatibility"], _COMPATIBILITY_FIELDS, "speculative_decode_plan_invalid")
        workload = _closed(result["workload"], _WORKLOAD_FIELDS, "speculative_decode_plan_invalid")
        _text(workload["workload_id"], "speculative_decode_plan_invalid")
        _sha256(workload["route_digest"], "speculative_decode_plan_invalid")
        _integer(workload["session_generation"], "speculative_decode_plan_invalid", minimum=1)

        bounds = _closed(result["proposal_bounds"], _PROPOSAL_BOUNDS_FIELDS, "speculative_decode_plan_invalid")
        width = _integer(
            bounds["maximum_width"], "speculative_decode_plan_invalid", minimum=1, maximum=PROPOSAL_MAXIMUM_WIDTH_BOUND
        )
        count = _integer(
            bounds["maximum_count"], "speculative_decode_plan_invalid", minimum=width, maximum=PROPOSAL_MAXIMUM_COUNT_BOUND
        )
        _integer(
            bounds["maximum_bytes"], "speculative_decode_plan_invalid", minimum=count, maximum=PROPOSAL_MAXIMUM_BYTES_BOUND
        )

        deadline = _closed(result["deadline_policy"], _DEADLINE_POLICY_FIELDS, "speculative_decode_plan_invalid")
        _exact(deadline["maximum_deadline_ms"], DEADLINE_MAXIMUM_MS, "speculative_decode_plan_invalid")
        _exact(deadline["minimum_deadline_ms"], DEADLINE_MINIMUM_MS, "speculative_decode_plan_invalid")
        _exact(deadline["warm_p95_multiplier"], DEADLINE_WARM_P95_MULTIPLIER, "speculative_decode_plan_invalid")
        _number(deadline["warm_target_only_p95_tpot_ms"], "speculative_decode_plan_invalid", nullable=True)
        _number(deadline["remaining_request_deadline_ms"], "speculative_decode_plan_invalid")
        if deadline["source"] != "measured_warm":
            raise SpeculativeDecodeContractError("planner_prediction_not_authoritative", str(deadline["source"]))

        breaker = _closed(
            result["circuit_breaker_policy"], _CIRCUIT_BREAKER_POLICY_FIELDS, "speculative_decode_plan_invalid"
        )
        _exact(breaker["consecutive_deadlines"], CIRCUIT_BREAKER_CONSECUTIVE_DEADLINES, "speculative_decode_plan_invalid")
        _exact(breaker["rolling_window_cycles"], CIRCUIT_BREAKER_ROLLING_WINDOW_CYCLES, "speculative_decode_plan_invalid")
        _exact(breaker["cooldown_seconds"], CIRCUIT_BREAKER_COOLDOWN_SECONDS, "speculative_decode_plan_invalid")

        resources = _closed(result["resource_envelope"], _RESOURCE_FIELDS, "speculative_decode_plan_invalid")
        for key in _RESOURCE_FIELDS - {"independent_reservations"}:
            _integer(resources[key], "speculative_decode_plan_invalid", minimum=1)
        if resources["independent_reservations"] is not True:
            raise ValueError("speculative_decode_plan_invalid")

        observation = _closed(result["observation"], _OBSERVATION_FIELDS, "speculative_decode_plan_invalid")
        decision = _closed(result["decision"], _DECISION_FIELDS, "speculative_decode_plan_invalid")
        _validate_plan_decision(
            decision, verifier=verifier, compatibility=compatibility, observation=observation
        )

        if result["target_only_fallback"] is not True:
            raise ValueError("speculative_decode_plan_invalid")
        threshold = _closed(
            result["benchmark_threshold"], _BENCHMARK_THRESHOLD_FIELDS, "speculative_decode_plan_invalid"
        )
        _exact(threshold["improvement_fraction"], _MATERIAL_THRESHOLD, "speculative_decode_plan_invalid")
        _exact(
            threshold["paired_lower_bound_fraction"],
            BENCHMARK_PAIRED_LOWER_BOUND_FRACTION,
            "speculative_decode_plan_invalid",
        )
        _exact(threshold["confidence"], BENCHMARK_CONFIDENCE, "speculative_decode_plan_invalid")
        _exact(
            threshold["maximum_ttft_regression_fraction"],
            BENCHMARK_MAXIMUM_TTFT_REGRESSION_FRACTION,
            "speculative_decode_plan_invalid",
        )
        _exact(result["privacy"], PLAN_PRIVACY_STATEMENT, "speculative_decode_plan_invalid")
        _verify_self_digest(result, PLAN_PROTOCOL, "speculative_decode_plan_invalid")
        return result
    except SpeculativeDecodeContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SpeculativeDecodeContractError("speculative_decode_plan_invalid") from exc


# ---------------------------------------------------------------------------
# Runtime contract
# ---------------------------------------------------------------------------

_RUNTIME_FIELDS = frozenset({"protocol", "binding", "mode", "requests", "runtime_digest"})
_RUNTIME_BINDING_FIELDS = frozenset(
    {
        "deployment_id",
        "deployment_epoch",
        "graph_digest",
        "membership_generation",
        "qualification_digest",
        "target_runtime_incarnation",
        "draft_runtime_incarnation",
    }
)
_RUNTIME_REQUEST_FIELDS = frozenset(
    {
        "request_id",
        "mode",
        "proposal_width",
        "proposed_count",
        "verified_count",
        "accepted_count",
        "rejected_count",
        "target_corrected_count",
        "rollback_count",
        "committed_count",
        "target_committed_output_tokens",
        "deadline_outcome",
        "draft_queue_ms",
        "draft_compute_ms",
        "transfer_ms",
        "fallback_reason",
        "cancelled",
        "circuit_breaker_state",
        "cleanup_complete",
        "terminal_state",
    }
)
_DEADLINE_OUTCOMES = frozenset(
    {"none", "proposal_arrived", "proposal_deadline_missed", "request_deadline_clamp", "warm_baseline_unknown"}
)
_FALLBACK_REASONS = frozenset(
    {
        "none",
        "draft_unavailable",
        "draft_lost",
        "proposal_deadline_missed",
        "low_acceptance",
        "thermal_throttle",
        "verifier_parity_failure",
        "invalid_proposal",
        "compatibility_violation",
    }
)
_TERMINAL_STATES = frozenset({"completed", "cancelled", "aborted", "target_lost_explicit"})


def _validate_runtime_binding(value: Any) -> dict[str, Any]:
    result = _closed(value, _RUNTIME_BINDING_FIELDS, "speculative_decode_runtime_invalid")
    _text(result["deployment_id"], "speculative_decode_runtime_invalid")
    _integer(result["deployment_epoch"], "speculative_decode_runtime_invalid", minimum=1)
    _sha256(result["graph_digest"], "speculative_decode_runtime_invalid")
    _integer(result["membership_generation"], "speculative_decode_runtime_invalid", minimum=1)
    _sha256(result["qualification_digest"], "speculative_decode_runtime_invalid")
    _sha256(result["target_runtime_incarnation"], "speculative_decode_runtime_invalid")
    _sha256(result["draft_runtime_incarnation"], "speculative_decode_runtime_invalid")
    return result


def _validate_runtime_request(value: Any) -> dict[str, Any]:
    # Owner amendment 2026-10-07: an optional ``target_bonus_count`` -- the target's
    # own decision committed after an all-match span.  Absent means zero.
    fields = _RUNTIME_REQUEST_FIELDS
    if isinstance(value, Mapping) and "target_bonus_count" in value:
        fields = _RUNTIME_REQUEST_FIELDS | {"target_bonus_count"}
    request = _closed(value, fields, "speculative_decode_runtime_invalid")
    _text(request["request_id"], "speculative_decode_runtime_invalid")
    if request["mode"] not in {"target_only", "speculative_requested", "speculative_admitted"}:
        raise ValueError("speculative_decode_runtime_invalid")
    for key in (
        "proposal_width",
        "proposed_count",
        "verified_count",
        "accepted_count",
        "rejected_count",
        "target_corrected_count",
        "rollback_count",
        "committed_count",
        "target_committed_output_tokens",
    ):
        _integer(request[key], "speculative_decode_runtime_invalid")
    bonus = request.get("target_bonus_count", 0)
    _integer(bonus, "speculative_decode_runtime_invalid")

    if request["mode"] == "target_only":
        if request["proposal_width"] != 0:
            raise ValueError("speculative_decode_runtime_invalid")
        if any(
            request[key] != 0
            for key in (
                "proposed_count",
                "verified_count",
                "accepted_count",
                "rejected_count",
                "target_corrected_count",
                "rollback_count",
                "committed_count",
                "target_committed_output_tokens",
            )
        ) or bonus != 0:
            raise ValueError("speculative_decode_runtime_invalid")
        if request["deadline_outcome"] != "none" or request["fallback_reason"] != "none":
            raise ValueError("speculative_decode_runtime_invalid")
        if any(request[key] is not None for key in ("draft_queue_ms", "draft_compute_ms", "transfer_ms")):
            raise ValueError("speculative_decode_runtime_invalid")
    elif request["proposal_width"] < 1:
        raise ValueError("speculative_decode_runtime_invalid")

    for key in ("draft_queue_ms", "draft_compute_ms", "transfer_ms"):
        _number(request[key], "speculative_decode_runtime_invalid", nullable=True)

    proposed = request["proposed_count"]
    verified = request["verified_count"]
    accepted = request["accepted_count"]
    rejected = request["rejected_count"]
    corrected = request["target_corrected_count"]
    committed = request["committed_count"]
    rollback = request["rollback_count"]
    if accepted + rejected > verified or verified > proposed:
        raise SpeculativeDecodeContractError("impossible_runtime_counts", request["request_id"])
    if corrected > rejected or rollback > proposed - accepted:
        raise SpeculativeDecodeContractError("impossible_runtime_counts", request["request_id"])
    # Each bonus follows an all-match span of at least one accepted position.
    if bonus > accepted:
        raise SpeculativeDecodeContractError("impossible_runtime_counts", request["request_id"])
    if committed != accepted + corrected + bonus or request["target_committed_output_tokens"] != committed:
        raise SpeculativeDecodeContractError("committed_positions_not_target_authoritative", request["request_id"])

    if request["deadline_outcome"] not in _DEADLINE_OUTCOMES:
        raise ValueError("speculative_decode_runtime_invalid")
    if request["fallback_reason"] == "target_loss":
        raise SpeculativeDecodeContractError("target_loss_is_not_draft_fallback", request["request_id"])
    if request["fallback_reason"] not in _FALLBACK_REASONS:
        raise ValueError("speculative_decode_runtime_invalid")
    if request["fallback_reason"] == "proposal_deadline_missed" and request["deadline_outcome"] != "proposal_deadline_missed":
        raise ValueError("speculative_decode_runtime_invalid")
    if type(request["cancelled"]) is not bool:
        raise ValueError("speculative_decode_runtime_invalid")
    if request["terminal_state"] not in _TERMINAL_STATES:
        raise ValueError("speculative_decode_runtime_invalid")
    if request["cancelled"] is not (request["terminal_state"] == "cancelled"):
        raise ValueError("speculative_decode_runtime_invalid")
    if request["cleanup_complete"] is not True:
        raise ValueError("speculative_decode_runtime_invalid")
    if request["circuit_breaker_state"] not in CIRCUIT_BREAKER_STATES:
        raise ValueError("speculative_decode_runtime_invalid")
    return request


def validate_speculative_decode_runtime(document: Mapping[str, Any]) -> dict[str, Any]:
    """Closed validation of ``mycelium.speculative_decode_runtime.v1``."""

    try:
        result = _closed(document, _RUNTIME_FIELDS, "speculative_decode_runtime_invalid")
        if result["protocol"] != RUNTIME_PROTOCOL:
            raise ValueError("speculative_decode_runtime_invalid")
        _validate_runtime_binding(result["binding"])
        if result["mode"] not in {"disabled", "target_only", "speculative"}:
            raise ValueError("speculative_decode_runtime_invalid")
        if not isinstance(result["requests"], list) or not result["requests"]:
            raise ValueError("speculative_decode_runtime_invalid")

        requests = [_validate_runtime_request(item) for item in result["requests"]]
        if result["mode"] == "target_only" and any(item["mode"] != "target_only" for item in requests):
            raise SpeculativeDecodeContractError("target_only_mode_carries_proposals")
        _verify_self_digest(result, RUNTIME_PROTOCOL, "speculative_decode_runtime_invalid")
        result["requests"] = requests
        return result
    except SpeculativeDecodeContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SpeculativeDecodeContractError("speculative_decode_runtime_invalid") from exc


# ---------------------------------------------------------------------------
# Benchmark contract
# ---------------------------------------------------------------------------

_BENCHMARK_FIELDS = frozenset(
    {
        "protocol",
        "plan_digest",
        "executed",
        "paired_order",
        "warmups",
        "windows",
        "confidence",
        "valid",
        "invalidation_reasons",
        "decision",
        "benchmark_digest",
    }
)
_BENCHMARK_WINDOW_FIELDS = frozenset(
    {
        "mode",
        "completed_requests",
        "target_committed_output_tokens",
        "prompt_length_buckets",
        "output_length_buckets",
        "wall_clock_ms",
        "ttft_p95_ms",
        "error_timeout_cancellation_fallback_count",
    }
)
_BENCHMARK_DECISION_FIELDS = frozenset(
    {
        "state",
        "reason",
        "observed_reduction_fraction",
        "paired_lower_bound_fraction",
        "ttft_regression_fraction",
        "blocking_conditions",
    }
)
_TOLERANCE = 1e-9


def _validate_benchmark_window(value: Any) -> dict[str, Any]:
    window = _closed(value, _BENCHMARK_WINDOW_FIELDS, "speculative_decode_benchmark_invalid")
    if window["mode"] not in {"target_only", "speculative"}:
        raise ValueError("speculative_decode_benchmark_invalid")
    requests = _integer(window["completed_requests"], "benchmark_window_undersized", minimum=1)
    tokens = _integer(window["target_committed_output_tokens"], "benchmark_window_undersized", minimum=1)
    prompt_buckets = _integer(window["prompt_length_buckets"], "benchmark_window_undersized", minimum=1)
    output_buckets = _integer(window["output_length_buckets"], "benchmark_window_undersized", minimum=1)
    if requests < BENCHMARK_MINIMUM_COMPLETED_REQUESTS:
        raise SpeculativeDecodeContractError("benchmark_window_undersized", "completed_requests")
    if tokens < BENCHMARK_MINIMUM_COMMITTED_TOKENS:
        raise SpeculativeDecodeContractError("benchmark_window_undersized", "target_committed_output_tokens")
    if prompt_buckets < BENCHMARK_MINIMUM_BUCKETS or output_buckets < BENCHMARK_MINIMUM_BUCKETS:
        raise SpeculativeDecodeContractError("benchmark_window_undersized", "length_buckets")
    _number(window["wall_clock_ms"], "speculative_decode_benchmark_invalid")
    _number(window["ttft_p95_ms"], "speculative_decode_benchmark_invalid")
    _integer(window["error_timeout_cancellation_fallback_count"], "speculative_decode_benchmark_invalid")
    return window


def validate_speculative_decode_benchmark(document: Mapping[str, Any]) -> dict[str, Any]:
    """Closed validation of ``mycelium.speculative_decode_benchmark.v1``."""

    try:
        result = _closed(document, _BENCHMARK_FIELDS, "speculative_decode_benchmark_invalid")
        if result["protocol"] != BENCHMARK_PROTOCOL:
            raise ValueError("speculative_decode_benchmark_invalid")
        _sha256(result["plan_digest"], "speculative_decode_benchmark_invalid")
        if type(result["executed"]) is not bool or type(result["valid"]) is not bool:
            raise ValueError("speculative_decode_benchmark_invalid")
        _exact(result["paired_order"], list(BENCHMARK_PAIRED_ORDER), "speculative_decode_benchmark_invalid")
        warmups = _closed(result["warmups"], frozenset({"target_only", "speculative"}), "speculative_decode_benchmark_invalid")
        _exact(warmups["target_only"], BENCHMARK_WARMUPS_PER_MODE, "speculative_decode_benchmark_invalid")
        _exact(warmups["speculative"], BENCHMARK_WARMUPS_PER_MODE, "speculative_decode_benchmark_invalid")
        _exact(result["confidence"], BENCHMARK_CONFIDENCE, "speculative_decode_benchmark_invalid")

        reasons = result["invalidation_reasons"]
        if (
            not isinstance(reasons, list)
            or any(not isinstance(item, str) for item in reasons)
            or reasons != sorted(set(reasons))
            or any(item not in BENCHMARK_INVALIDATION_REASONS for item in reasons)
        ):
            raise ValueError("speculative_decode_benchmark_invalid")
        if result["valid"] is not (not reasons):
            raise ValueError("speculative_decode_benchmark_invalid")

        if not isinstance(result["windows"], list):
            raise ValueError("speculative_decode_benchmark_invalid")
        windows = [_validate_benchmark_window(item) for item in result["windows"]]

        decision = _closed(result["decision"], _BENCHMARK_DECISION_FIELDS, "speculative_decode_benchmark_invalid")
        if decision["state"] not in {ENABLED_DECISION, DISABLED_DECISION}:
            raise ValueError("speculative_decode_benchmark_invalid")
        blocking = decision["blocking_conditions"]
        if (
            not isinstance(blocking, list)
            or any(not isinstance(item, str) for item in blocking)
            or blocking != sorted(set(blocking))
            or any(item not in BENCHMARK_BLOCKING_CONDITIONS for item in blocking)
        ):
            raise ValueError("speculative_decode_benchmark_invalid")
        measurements = {
            key: _number(decision[key], "speculative_decode_benchmark_invalid", nullable=True)
            for key in (
                "observed_reduction_fraction",
                "paired_lower_bound_fraction",
                "ttft_regression_fraction",
            )
        }

        if not result["executed"]:
            if windows or result["invalidation_reasons"] != ["missing_samples"] or result["valid"]:
                raise ValueError("speculative_decode_benchmark_invalid")
            if any(value is not None for value in measurements.values()):
                raise SpeculativeDecodeContractError("benchmark_decision_inconsistent", "unexecuted claims a sample")
            if (
                decision["state"] != DISABLED_DECISION
                or decision["reason"] != "confidence_bound_not_met"
                or blocking != ["benchmark_pair_invalid"]
            ):
                raise SpeculativeDecodeContractError("benchmark_decision_inconsistent", "unexecuted decision")
        elif len(windows) != BENCHMARK_MEASURED_WINDOWS:
            raise SpeculativeDecodeContractError("benchmark_window_order_invalid", f"windows={len(windows)}")
        else:
            modes = tuple(window["mode"] for window in windows)
            if modes != BENCHMARK_WINDOW_MODE_ORDER:
                raise SpeculativeDecodeContractError("benchmark_window_order_invalid", str(modes))

        if result["executed"] and result["valid"]:
            target_walls = [w["wall_clock_ms"] for w in windows if w["mode"] == "target_only"]
            speculative_walls = [w["wall_clock_ms"] for w in windows if w["mode"] == "speculative"]
            target_ttfts = [w["ttft_p95_ms"] for w in windows if w["mode"] == "target_only"]
            speculative_ttfts = [w["ttft_p95_ms"] for w in windows if w["mode"] == "speculative"]
            reduction = 1.0 - (sum(speculative_walls) / len(speculative_walls)) / (
                sum(target_walls) / len(target_walls)
            )
            ttft_regression = (sum(speculative_ttfts) / len(speculative_ttfts)) / (
                sum(target_ttfts) / len(target_ttfts)
            ) - 1.0
            lower_bound = measurements["paired_lower_bound_fraction"]
            if (
                measurements["observed_reduction_fraction"] is None
                or abs(measurements["observed_reduction_fraction"] - reduction) > _TOLERANCE
                or measurements["ttft_regression_fraction"] is None
                or abs(measurements["ttft_regression_fraction"] - ttft_regression) > _TOLERANCE
                or lower_bound is None
                or lower_bound > reduction + _TOLERANCE
            ):
                raise SpeculativeDecodeContractError("benchmark_decision_inconsistent", "declared measurements")
            expected_blocking: list[str] = []
            if reduction < BENCHMARK_IMPROVEMENT_FRACTION - _TOLERANCE:
                expected_blocking.append("material_gain_below_threshold")
            if lower_bound < BENCHMARK_PAIRED_LOWER_BOUND_FRACTION - _TOLERANCE:
                expected_blocking.append("paired_lower_bound_below_threshold")
            if ttft_regression > BENCHMARK_MAXIMUM_TTFT_REGRESSION_FRACTION + _TOLERANCE:
                expected_blocking.append("ttft_regression_above_limit")
            if not expected_blocking:
                expected_state, expected_reason = ENABLED_DECISION, decision["reason"]
            elif "material_gain_below_threshold" in expected_blocking:
                expected_state, expected_reason = DISABLED_DECISION, "material_gain_not_observed"
            else:
                expected_state, expected_reason = DISABLED_DECISION, "confidence_bound_not_met"
            if (
                decision["state"] != expected_state
                or decision["reason"] != expected_reason
                or blocking != sorted(expected_blocking)
                or (expected_state == DISABLED_DECISION and not expected_blocking)
            ):
                raise SpeculativeDecodeContractError("benchmark_decision_inconsistent", "decision")
        elif result["executed"] and not result["valid"]:
            if any(value is not None for value in measurements.values()):
                raise SpeculativeDecodeContractError("benchmark_decision_inconsistent", "invalid pair claims a sample")
            if (
                decision["state"] != DISABLED_DECISION
                or decision["reason"] != "confidence_bound_not_met"
                or blocking != ["benchmark_pair_invalid"]
            ):
                raise SpeculativeDecodeContractError("benchmark_decision_inconsistent", "invalid pair decision")

        _verify_self_digest(result, BENCHMARK_PROTOCOL, "speculative_decode_benchmark_invalid")
        result["windows"] = windows
        return result
    except SpeculativeDecodeContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SpeculativeDecodeContractError("speculative_decode_benchmark_invalid") from exc


# ---------------------------------------------------------------------------
# Qualification contract and public projection
# ---------------------------------------------------------------------------

_QUALIFICATION_FIELDS = frozenset(
    {
        "protocol",
        "plan_digest",
        "benchmark_digest",
        "executed",
        "result",
        "browser_workspaces",
        "negative_gates",
        "projection",
        "qualification_digest",
    }
)


def validate_public_projection(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Privacy-reduced projection: private material cannot pass this boundary."""

    if not isinstance(projection, Mapping):
        raise SpeculativeDecodeContractError("public_projection_invalid", "not a mapping")
    for key in projection:
        if not isinstance(key, str) or not key:
            raise SpeculativeDecodeContractError("public_projection_invalid", "key")
        lowered = key.lower()
        if any(marker in lowered for marker in _PRIVATE_KEY_MARKERS):
            raise SpeculativeDecodeContractError("public_projection_carries_private_fields", key)
    return copy.deepcopy(dict(projection))


def validate_speculative_decode_qualification(document: Mapping[str, Any]) -> dict[str, Any]:
    """Closed validation of ``mycelium.speculative_decode_qualification.v1``.

    The scaffold can only represent an unexecuted qualification record. A record
    claiming execution requires the physical A11 pipeline and is refused here
    rather than validated into existence.
    """

    try:
        result = _closed(document, _QUALIFICATION_FIELDS, "speculative_decode_qualification_invalid")
        if result["protocol"] != QUALIFICATION_PROTOCOL:
            raise ValueError("speculative_decode_qualification_invalid")
        _sha256(result["plan_digest"], "speculative_decode_qualification_invalid")
        _sha256(result["benchmark_digest"], "speculative_decode_qualification_invalid")
        if type(result["executed"]) is not bool:
            raise ValueError("speculative_decode_qualification_invalid")
        if result["executed"]:
            raise SpeculativeDecodeContractError("qualification_execution_requires_physical_pipeline")
        if result["result"] != "not_executed":
            raise ValueError("speculative_decode_qualification_invalid")
        if (
            result["browser_workspaces"] is not None
            or result["negative_gates"] is not None
            or result["projection"] is not None
        ):
            raise ValueError("speculative_decode_qualification_invalid")
        _verify_self_digest(result, QUALIFICATION_PROTOCOL, "speculative_decode_qualification_invalid")
        return result
    except SpeculativeDecodeContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SpeculativeDecodeContractError("speculative_decode_qualification_invalid") from exc


# ---------------------------------------------------------------------------
# Compatibility fixtures (claim nothing)
# ---------------------------------------------------------------------------

_PLACEHOLDER = "sha256:" + "0" * 64
_FIXTURE_DIGESTS = {index: "sha256:" + f"{index:064x}" for index in range(1, 10)}


def compatibility_fixtures() -> dict[str, dict[str, Any]]:
    """Closed, self-bound fixtures that each claim no evaluation result."""

    document = _FIXTURE_DIGESTS

    plan = {
        "protocol": PLAN_PROTOCOL,
        "decision": {
            "state": DISABLED_DECISION,
            "reason": "no_authorized_local_draft",
            "material_gain_threshold": _MATERIAL_THRESHOLD,
            "target_only_fallback": True,
        },
        "target": {
            "role": "target",
            "identity_digest": document[1],
            "representation_digest": document[2],
            "runtime_incarnation": document[3],
        },
        "draft": {
            "role": "draft",
            "identity_digest": document[4],
            "representation_digest": document[5],
            "runtime_incarnation": document[6],
        },
        "verifier": {
            "capability": "a10_target_verifier",
            "qualified_width": 4,
            "target_runtime_binding_digest": document[7],
            "status": "unknown",
        },
        "compatibility": {
            "tokenizer_digest_match": None,
            "canonical_token_mapping_match": None,
            "special_token_semantics_match": None,
            "position_semantics_match": None,
            "attention_mask_match": None,
            "context_truncation_match": None,
            "generation_policy": _GENERATION_POLICY,
            "kv_alias": False,
            "kv_transfer": False,
        },
        "workload": {"workload_id": "fixture-workload", "route_digest": document[8], "session_generation": 1},
        "proposal_bounds": {"maximum_width": 4, "maximum_count": 32, "maximum_bytes": 512},
        "deadline_policy": {
            "maximum_deadline_ms": DEADLINE_MAXIMUM_MS,
            "minimum_deadline_ms": DEADLINE_MINIMUM_MS,
            "warm_p95_multiplier": DEADLINE_WARM_P95_MULTIPLIER,
            "warm_target_only_p95_tpot_ms": None,
            "remaining_request_deadline_ms": 500.0,
            "source": "measured_warm",
        },
        "circuit_breaker_policy": {
            "consecutive_deadlines": CIRCUIT_BREAKER_CONSECUTIVE_DEADLINES,
            "rolling_window_cycles": CIRCUIT_BREAKER_ROLLING_WINDOW_CYCLES,
            "cooldown_seconds": CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        },
        "resource_envelope": {
            "target_kv_bytes": 1024,
            "draft_kv_bytes": 1024,
            "verifier_workspace_bytes": 2048,
            "proposal_buffer_bytes": 512,
            "transport_buffer_bytes": 512,
            "runtime_batch_slots": 2,
            "independent_reservations": True,
        },
        "observation": {
            "sample_count": 0,
            "observed_wall_clock_reduction_fraction": None,
            "paired_lower_bound_fraction": None,
            "ttft_regression_fraction": None,
        },
        "target_only_fallback": True,
        "benchmark_threshold": {
            "improvement_fraction": BENCHMARK_IMPROVEMENT_FRACTION,
            "paired_lower_bound_fraction": BENCHMARK_PAIRED_LOWER_BOUND_FRACTION,
            "confidence": BENCHMARK_CONFIDENCE,
            "maximum_ttft_regression_fraction": BENCHMARK_MAXIMUM_TTFT_REGRESSION_FRACTION,
        },
        "privacy": PLAN_PRIVACY_STATEMENT,
    }

    runtime = {
        "protocol": RUNTIME_PROTOCOL,
        "binding": {
            "deployment_id": "fixture-deployment",
            "deployment_epoch": 1,
            "graph_digest": document[1],
            "membership_generation": 1,
            "qualification_digest": document[2],
            "target_runtime_incarnation": document[3],
            "draft_runtime_incarnation": document[6],
        },
        "mode": "target_only",
        "requests": [
            {
                "request_id": "fixture-request",
                "mode": "target_only",
                "proposal_width": 0,
                "proposed_count": 0,
                "verified_count": 0,
                "accepted_count": 0,
                "rejected_count": 0,
                "target_corrected_count": 0,
                "rollback_count": 0,
                "committed_count": 0,
                "target_committed_output_tokens": 0,
                "deadline_outcome": "none",
                "draft_queue_ms": None,
                "draft_compute_ms": None,
                "transfer_ms": None,
                "fallback_reason": "none",
                "cancelled": False,
                "circuit_breaker_state": "closed",
                "cleanup_complete": True,
                "terminal_state": "completed",
            }
        ],
    }

    benchmark = {
        "protocol": BENCHMARK_PROTOCOL,
        "plan_digest": _PLACEHOLDER,
        "executed": False,
        "paired_order": list(BENCHMARK_PAIRED_ORDER),
        "warmups": {"target_only": BENCHMARK_WARMUPS_PER_MODE, "speculative": BENCHMARK_WARMUPS_PER_MODE},
        "windows": [],
        "confidence": BENCHMARK_CONFIDENCE,
        "valid": False,
        "invalidation_reasons": ["missing_samples"],
        "decision": {
            "state": DISABLED_DECISION,
            "reason": "confidence_bound_not_met",
            "observed_reduction_fraction": None,
            "paired_lower_bound_fraction": None,
            "ttft_regression_fraction": None,
            "blocking_conditions": ["benchmark_pair_invalid"],
        },
    }

    qualification = {
        "protocol": QUALIFICATION_PROTOCOL,
        "plan_digest": _PLACEHOLDER,
        "benchmark_digest": _PLACEHOLDER,
        "executed": False,
        "result": "not_executed",
        "browser_workspaces": None,
        "negative_gates": None,
        "projection": None,
    }

    return {
        "speculative_decode_plan": validate_speculative_decode_plan(seal(plan)),
        "speculative_decode_runtime": validate_speculative_decode_runtime(seal(runtime)),
        "speculative_decode_benchmark": validate_speculative_decode_benchmark(seal(benchmark)),
        "speculative_decode_qualification": validate_speculative_decode_qualification(seal(qualification)),
    }


# ---------------------------------------------------------------------------
# Deterministic verifier transactions (specification section 5, in-process model)
#
# One transaction evaluates every bounded candidate position in ONE causal target
# call, compares every per-position decision with sequential target execution, and
# then rolls back, commits the verified prefix, or commits the verified prefix plus
# one target-authoritative correction. Commits are atomic, strictly monotonic and
# idempotent for an identical cycle digest; a conflicting duplicate, a stale prefix
# or a parity failure fails closed to the previous target watermark.
#
# The decision source is injected and the watermark is an in-process ledger. Neither
# is the physically qualified A10 verifier, and no proposal moves: these records
# carry digests and counts, never raw tokens.
# ---------------------------------------------------------------------------

_TRANSACTION_FIELDS = frozenset(
    {
        "kind",
        "plan_digest",
        "cycle_ref",
        "idempotency_digest",
        "generation_policy",
        "proposal_width",
        "evaluated_position_count",
        "verified_count",
        "accepted_prefix_length",
        "rejected_count",
        "target_corrected_count",
        "rollback_count",
        "committed_position_count",
        "committed_count",
        "target_prefix_position",
        "target_prefix_digest",
        "target_watermark_position",
        "target_watermark_digest",
        "commit_state",
        "transaction_mode",
        "parity",
        "deadline_ms",
        "deadline_clamp_reason",
        "accepted_prefix_digest",
        "committed_digest",
        "correction_token_digest",
        "failure_reason",
        "claim",
    }
)
_TRANSACTION_COUNT_FIELDS = (
    "proposal_width",
    "evaluated_position_count",
    "verified_count",
    "accepted_prefix_length",
    "rejected_count",
    "target_corrected_count",
    "rollback_count",
    "committed_position_count",
    "committed_count",
    "target_prefix_position",
    "target_watermark_position",
)
_TRANSACTION_DIGEST_FIELDS = (
    "target_prefix_digest",
    "target_watermark_digest",
    "accepted_prefix_digest",
    "committed_digest",
    "correction_token_digest",
)
_PARITY_STATES = ("not_checked", "matched", "failed")
_COMMITTED_STATES = ("committed", "committed_with_correction")
_COMMITTABLE_STATES = ("verified_prefix_ready", "target_correction_ready")
_REQUEST_PREFERENCES = ("target_only_requested", "speculative_requested")
_GENESIS_DIGEST = "sha256:" + "0" * 64


def _contract(code: str, detail: Any = None) -> SpeculativeDecodeContractError:
    return SpeculativeDecodeContractError(code, None if detail is None else str(detail))


def _token(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value >= 1 << 31:
        raise _contract(code, value)
    return value


def _token_sequence(value: Any, code: str) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not len(value):
        raise _contract(code, value)
    return tuple(_token(item, code) for item in value)


def _digest_or_none(value: Any, code: str) -> str | None:
    if value is None:
        return None
    try:
        return _sha256(value, code)
    except ValueError as exc:
        raise _contract(code, value) from exc


def _prefix_digest(*, parent_digest: str, position: int, tokens: Sequence[int]) -> str:
    """Digest of a target-owned prefix: parent digest, position and committed tokens."""

    return _digest({"parent": parent_digest, "position": int(position), "tokens": [int(item) for item in tokens]})


def _token_digest(token: int) -> str:
    return _digest({"token": int(token)})


class TargetPrefixLedger:
    """Deterministic in-process model of the target-owned committed prefix.

    This is not a target runtime, not KV storage and not a model: it holds one
    position/digest watermark and the closed commit arbitration rules of
    specification section 5. A commit is one atomic assignment; the watermark is
    strictly monotonic; an identical cycle digest is idempotent and never advances
    twice; a conflicting duplicate cycle fails closed and leaves the watermark
    untouched.
    """

    def __init__(self, *, position: int = 0, digest: str | None = None, generation: int = 1) -> None:
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise _contract("invalid_committed_prefix_position", position)
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise _contract("invalid_prefix_generation", generation)
        if digest is None:
            digest = _prefix_digest(parent_digest=_GENESIS_DIGEST, position=position, tokens=())
        else:
            digest = _digest_or_none(digest, "invalid_committed_prefix_digest")
        if digest is None:
            raise _contract("invalid_committed_prefix_digest")
        self._position = position
        self._digest = digest
        self._generation = generation
        self._commit_count = 0
        self._rollback_count = 0
        self._cycles: dict[str, dict[str, Any]] = {}
        self._digests: dict[str, tuple[str, int]] = {}

    @property
    def position(self) -> int:
        return self._position

    @property
    def digest(self) -> str:
        return self._digest

    @property
    def generation(self) -> int:
        return self._generation

    def snapshot(self) -> dict[str, Any]:
        """The current watermark, its generation and the arbitration counters."""

        return {
            "position": self._position,
            "digest": self._digest,
            "generation": self._generation,
            "commit_count": self._commit_count,
            "rollback_count": self._rollback_count,
            "cycles": sorted(self._cycles),
        }

    def commit(self, *, cycle_ref: Any, idempotency_digest: Any, position: Any, digest: Any) -> dict[str, Any]:
        try:
            reference = _text(cycle_ref, "invalid_cycle_ref")
            idempotency = _sha256(idempotency_digest, "invalid_idempotency_digest")
            target_digest = _sha256(digest, "invalid_committed_prefix_digest")
        except ValueError as exc:
            raise _contract(exc.args[0], exc.args[1] if len(exc.args) > 1 else None) from exc
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise _contract("invalid_committed_prefix_position", position)

        previous = self._cycles.get(reference)
        if previous is not None:
            if previous["idempotency_digest"] != idempotency:
                raise _contract("conflicting_duplicate_cycle", reference)
            record = dict(previous["record"])
            record["idempotent_duplicate"] = True
            record["advanced"] = False
            return record

        seen = self._digests.get(idempotency)
        if seen is not None:
            recorded_ref, recorded_position = seen
            if recorded_position != position:
                raise _contract("conflicting_duplicate_cycle", reference)
            # The identical operation presented under a second cycle reference is
            # still the same commit: idempotent, and never a second advance.
            record = dict(self._cycles[recorded_ref]["record"])
            record["idempotent_duplicate"] = True
            record["advanced"] = False
            return record

        if position < self._position:
            raise _contract("non_monotonic_commit", f"position={position} watermark={self._position}")
        if position == self._position:
            # A commit advances the watermark; the same position carrying other
            # content is a conflicting cycle, never a silent no-op advance.
            raise _contract("conflicting_duplicate_cycle", reference)

        # One atomic assignment: a commit that cannot complete fully never runs.
        advance = position - self._position
        self._position = position
        self._digest = target_digest
        self._generation += 1
        self._commit_count += 1
        record = {
            "cycle_ref": reference,
            "idempotency_digest": idempotency,
            "target_watermark_position": self._position,
            "target_watermark_digest": self._digest,
            "committed_position_count": advance,
            "generation": self._generation,
            "advanced": True,
            "idempotent_duplicate": False,
        }
        self._cycles[reference] = {"idempotency_digest": idempotency, "record": record}
        self._digests[idempotency] = (reference, position)
        return dict(record)

    def rollback(self, *, cycle_ref: Any) -> dict[str, Any]:
        """Discard tentative state; the committed watermark is untouched."""

        try:
            reference = _text(cycle_ref, "invalid_cycle_ref")
        except ValueError as exc:
            raise _contract("invalid_cycle_ref", exc.args[1] if len(exc.args) > 1 else None) from exc
        self._rollback_count += 1
        return {
            "cycle_ref": reference,
            "rolled_back": True,
            "target_watermark_position": self._position,
            "target_watermark_digest": self._digest,
            "generation": self._generation,
        }


def _transaction_state(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": TRANSACTION_STATE_KIND,
        "plan_digest": _GENESIS_DIGEST,
        "cycle_ref": "",
        "idempotency_digest": None,
        "generation_policy": _GENERATION_POLICY,
        "proposal_width": 0,
        "evaluated_position_count": 0,
        "verified_count": 0,
        "accepted_prefix_length": 0,
        "rejected_count": 0,
        "target_corrected_count": 0,
        "rollback_count": 0,
        "committed_position_count": 0,
        "committed_count": 0,
        "target_prefix_position": 0,
        "target_prefix_digest": None,
        "target_watermark_position": 0,
        "target_watermark_digest": None,
        "commit_state": "open",
        "transaction_mode": None,
        "parity": "not_checked",
        "deadline_ms": None,
        "deadline_clamp_reason": None,
        "accepted_prefix_digest": None,
        "committed_digest": None,
        "correction_token_digest": None,
        "failure_reason": None,
        "claim": DETERMINISTIC_VERIFIER_CLAIM,
    }
    record.update(overrides)
    return record


def _validate_transaction_state(value: Any) -> dict[str, Any]:
    try:
        record = _closed(value, _TRANSACTION_FIELDS, "verification_transaction_invalid")
        if record["kind"] != TRANSACTION_STATE_KIND:
            raise ValueError("verification_transaction_invalid")
        _sha256(record["plan_digest"], "verification_transaction_invalid")
        _text(record["cycle_ref"], "verification_transaction_invalid")
        if record["idempotency_digest"] is not None:
            _sha256(record["idempotency_digest"], "verification_transaction_invalid")
        if record["generation_policy"] not in QUALIFIED_GENERATION_POLICIES:
            raise ValueError("verification_transaction_invalid")
        for key in _TRANSACTION_COUNT_FIELDS:
            _integer(record[key], "verification_transaction_invalid")
        if record["commit_state"] not in TRANSACTION_COMMIT_STATES:
            raise ValueError("verification_transaction_invalid")
        if record["transaction_mode"] is not None and record["transaction_mode"] not in TRANSACTION_MODES:
            raise ValueError("verification_transaction_invalid")
        if record["parity"] not in _PARITY_STATES:
            raise ValueError("verification_transaction_invalid")
        if record["failure_reason"] is not None and record["failure_reason"] not in TRANSACTION_FAILURES:
            raise ValueError("verification_transaction_invalid")
        _number(record["deadline_ms"], "verification_transaction_invalid", nullable=True)
        for key in _TRANSACTION_DIGEST_FIELDS:
            _digest_or_none(record[key], "verification_transaction_invalid")
        if record["claim"] != DETERMINISTIC_VERIFIER_CLAIM:
            raise ValueError("verification_transaction_invalid")

        width = record["proposal_width"]
        evaluated = record["evaluated_position_count"]
        accepted = record["accepted_prefix_length"]
        rejected = record["rejected_count"]
        corrected = record["target_corrected_count"]
        committed = record["committed_position_count"]
        if evaluated > width or accepted + rejected != evaluated or corrected > 1 or corrected > rejected:
            raise ValueError("verification_transaction_invalid")
        if record["rollback_count"] > width - accepted:
            raise ValueError("verification_transaction_invalid")
        if committed != record["committed_count"] or committed > accepted + corrected:
            raise ValueError("verification_transaction_invalid")
        if record["commit_state"] == "open":
            if any(record[key] for key in _TRANSACTION_COUNT_FIELDS if key != "proposal_width"):
                raise ValueError("verification_transaction_invalid")
            if record["committed_digest"] is not None or record["accepted_prefix_digest"] is not None:
                raise ValueError("verification_transaction_invalid")
        else:
            if evaluated != width:
                raise ValueError("verification_transaction_invalid")
            if record["committed_digest"] is None or record["accepted_prefix_digest"] is None:
                raise ValueError("verification_transaction_invalid")
        if record["commit_state"] not in _COMMITTED_STATES and committed != 0:
            raise ValueError("verification_transaction_invalid")
        if record["commit_state"] == "committed_with_correction" and (
            corrected != 1 or record["transaction_mode"] != TRANSACTION_MODES[2]
        ):
            raise ValueError("verification_transaction_invalid")
        if record["commit_state"] == "committed" and (
            corrected != 0 or record["transaction_mode"] != TRANSACTION_MODES[1]
        ):
            raise ValueError("verification_transaction_invalid")
        return record
    except SpeculativeDecodeContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _contract("verification_transaction_invalid") from exc


def _require_decision_source(target: Any) -> Any:
    if not callable(getattr(target, "evaluate_bounded_positions", None)):
        raise _contract("target_decision_source_invalid", type(target).__name__)
    return target


def _bounded_call(target: Any, *, prefix_digest: str, candidates: tuple[int, ...]) -> tuple[int, ...]:
    source = _require_decision_source(target)
    try:
        raw = source.evaluate_bounded_positions(prefix_digest=prefix_digest, candidates=candidates)
    except SpeculativeDecodeContractError:
        raise
    except Exception as exc:  # a decision source may not surface an arbitrary failure
        raise _contract("target_decision_source_failed", type(exc).__name__) from exc
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise _contract("target_decision_invalid", type(raw).__name__)
    return tuple(_token(item, "target_decision_invalid") for item in raw)


def sequential_target_decisions(
    *, target: Any, prefix_digest: Any, candidates: Sequence[int]
) -> tuple[int, ...]:
    """Run the target sequentially on the same prefix: exactly one decision per position.

    When the decision source declares ``sequential_decisions`` (the qualified
    reference oracle), that execution is used. Otherwise the causal single-position
    form of ``evaluate_bounded_positions`` is used: position ``i`` is the last
    decision of a call over ``candidates[: i + 1]``, which a causal target answers
    without consulting ``candidates[i]``.
    """

    source = _require_decision_source(target)
    digest = _digest_or_none(prefix_digest, "invalid_committed_prefix_digest")
    if digest is None:
        raise _contract("invalid_committed_prefix_digest", prefix_digest)
    tokens = _token_sequence(candidates, "invalid_candidate_tokens")
    oracle = getattr(source, "sequential_decisions", None)
    if callable(oracle):
        try:
            raw = oracle(prefix_digest=digest, candidates=tokens)
        except SpeculativeDecodeContractError:
            raise
        except Exception as exc:
            raise _contract("target_decision_source_failed", type(exc).__name__) from exc
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise _contract("target_decision_invalid", type(raw).__name__)
        decisions = tuple(_token(item, "target_decision_invalid") for item in raw)
        if len(decisions) != len(tokens):
            raise _contract("target_decision_invalid", f"sequential_width={len(decisions)}")
        return decisions
    return tuple(
        _bounded_call(source, prefix_digest=digest, candidates=tokens[: index + 1])[-1]
        for index in range(len(tokens))
    )


def check_prefix_parity(
    *,
    target: Any,
    prefix_digest: Any,
    candidates: Sequence[int],
    decisions: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Compare one bounded call's per-position decisions with sequential execution.

    Every position decision of the bounded multi-position call must equal the
    decision sequential target execution produces for the same prefix and the same
    accepted tokens. ``first_divergence`` names the first position that does not.
    """

    digest = _digest_or_none(prefix_digest, "invalid_committed_prefix_digest")
    if digest is None:
        raise _contract("invalid_committed_prefix_digest", prefix_digest)
    tokens = _token_sequence(candidates, "invalid_candidate_tokens")
    if decisions is None:
        bounded = _bounded_call(target, prefix_digest=digest, candidates=tokens)
    else:
        if isinstance(decisions, (str, bytes)) or not isinstance(decisions, Sequence):
            raise _contract("target_decision_invalid", type(decisions).__name__)
        bounded = tuple(_token(item, "target_decision_invalid") for item in decisions)
    if len(bounded) != len(tokens):
        raise _contract("target_decision_invalid", f"bounded_width={len(bounded)}")
    sequential = sequential_target_decisions(target=target, prefix_digest=digest, candidates=tokens)
    divergence = next(
        (index for index, (left, right) in enumerate(zip(bounded, sequential)) if left != right), None
    )
    return {
        "parity": "failed" if divergence is not None else "matched",
        "positions_compared": len(tokens),
        "first_divergence": divergence,
        "sequential_calls": len(tokens),
        "claim": DETERMINISTIC_VERIFIER_CLAIM,
    }


def _admit_transaction_deadline(plan: Mapping[str, Any], deadline_ms: Any) -> tuple[float | None, str | None]:
    policy = plan["deadline_policy"]
    remaining = policy["remaining_request_deadline_ms"]
    if deadline_ms is None:
        admitted = admit_proposal_deadline(
            warm_target_only_p95_tpot_ms=policy["warm_target_only_p95_tpot_ms"],
            remaining_request_deadline_ms=remaining,
        )
        return admitted["admitted_deadline_ms"], admitted["clamp_reason"]
    try:
        observed = _number(deadline_ms, "verification_deadline_invalid", minimum=0.0)
    except ValueError as exc:
        raise _contract("verification_deadline_invalid", deadline_ms) from exc
    if observed is None:  # a non-nullable number never returns None
        raise _contract("verification_deadline_invalid", deadline_ms)
    requested = observed
    if requested < DEADLINE_MINIMUM_MS:
        value, reason = DEADLINE_MINIMUM_MS, "floor_applied"
    elif requested > DEADLINE_MAXIMUM_MS:
        value, reason = DEADLINE_MAXIMUM_MS, "ceiling_applied"
    else:
        value, reason = float(requested), "within_bounds"
    if remaining is not None and float(remaining) < value:
        return float(remaining), "request_deadline_clamp"
    return float(value), reason


def open_target_verification_transaction(
    *,
    plan: Mapping[str, Any],
    width: int | None = None,
    deadline_ms: float | None = None,
    proposal_width: int | None = None,
    cycle_ref: str | None = None,
    request_id: str | None = None,
    committed_prefix_position: int = 0,
    committed_prefix_digest: str | None = None,
    idempotency_digest: str | None = None,
) -> dict[str, Any]:
    """Open one deterministic transaction at the exact committed target prefix.

    The admitted deadline follows the frozen formula: floor/ceiling clamp first,
    then the request-remaining-deadline clamp. An unsupported generation policy
    fails admission here and is never coerced to greedy.
    """

    document = validate_speculative_decode_plan(plan)
    policy = document["compatibility"]["generation_policy"]
    if policy not in QUALIFIED_GENERATION_POLICIES:
        raise _contract("unsupported_generation_policy", policy)

    requested_width = width if width is not None else proposal_width
    if isinstance(requested_width, bool) or not isinstance(requested_width, int) or requested_width < 1:
        raise _contract("proposal_width_out_of_bounds", requested_width)
    maximum = min(
        int(document["proposal_bounds"]["maximum_width"]),
        int(document["verifier"]["qualified_width"]),
    )
    if requested_width > maximum:
        raise _contract("proposal_width_out_of_bounds", requested_width)

    if isinstance(committed_prefix_position, bool) or not isinstance(committed_prefix_position, int) or (
        committed_prefix_position < 0
    ):
        raise _contract("invalid_committed_prefix_position", committed_prefix_position)
    if committed_prefix_digest is None:
        prefix_digest = _prefix_digest(
            parent_digest=_GENESIS_DIGEST, position=committed_prefix_position, tokens=()
        )
    else:
        prefix_digest = _digest_or_none(committed_prefix_digest, "invalid_committed_prefix_digest")

    reference = cycle_ref if cycle_ref is not None else request_id
    if reference is None:
        reference = "cycle:" + document["plan_digest"].split(":")[1][:16]
    try:
        reference = _text(reference, "invalid_cycle_ref")
    except ValueError as exc:
        raise _contract("invalid_cycle_ref", reference) from exc

    admitted_ms, clamp_reason = _admit_transaction_deadline(document, deadline_ms)

    if idempotency_digest is None:
        idempotency = _digest(
            {
                "plan": document["plan_digest"],
                "cycle": reference,
                "width": requested_width,
                "prefix": [committed_prefix_position, prefix_digest],
                "deadline_ms": admitted_ms,
            }
        )
    else:
        idempotency = _digest_or_none(idempotency_digest, "invalid_idempotency_digest")
    if idempotency is None:
        raise _contract("invalid_idempotency_digest")

    return _transaction_state(
        plan_digest=document["plan_digest"],
        cycle_ref=reference,
        idempotency_digest=idempotency,
        proposal_width=requested_width,
        target_prefix_position=committed_prefix_position,
        target_prefix_digest=prefix_digest,
        target_watermark_position=committed_prefix_position,
        target_watermark_digest=prefix_digest,
        deadline_ms=admitted_ms,
        deadline_clamp_reason=clamp_reason,
    )


def verify_transaction_positions(
    *,
    transaction: Mapping[str, Any],
    candidate_tokens: Sequence[int],
    target: Any,
) -> dict[str, Any]:
    """Evaluate every bounded position in ONE causal call and decide the mode.

    All positions matching -> ``commit_verified_prefix``. First mismatch -> the
    common prefix plus one target-authoritative correction
    (``commit_verified_prefix_plus_target_correction``). A parity failure or a
    non-causal decision source -> ``rollback_to_prefix``, which commits nothing and
    leaves the exact original prefix in place.
    """

    record = _validate_transaction_state(transaction)
    if record["commit_state"] != "open":
        raise _contract("transaction_not_open", record["commit_state"])
    source = _require_decision_source(target)
    tokens = _token_sequence(candidate_tokens, "invalid_candidate_tokens")
    if len(tokens) != record["proposal_width"]:
        raise _contract("proposal_width_mismatch", f"{len(tokens)}!={record['proposal_width']}")

    prefix_digest = record["target_prefix_digest"]
    decisions = _bounded_call(source, prefix_digest=prefix_digest, candidates=tokens)
    if len(decisions) != len(tokens):
        raise _contract("target_decision_invalid", f"{len(decisions)}!={len(tokens)}")
    parity = check_prefix_parity(
        target=source, prefix_digest=prefix_digest, candidates=tokens, decisions=decisions
    )

    width = len(tokens)
    updated = dict(record)
    updated.update({"evaluated_position_count": width, "verified_count": width, "parity": parity["parity"]})
    if parity["parity"] != "matched":
        updated.update(
            {
                "accepted_prefix_length": 0,
                "rejected_count": width,
                "target_corrected_count": 0,
                "rollback_count": 1,
                "commit_state": "rolled_back",
                "transaction_mode": TRANSACTION_MODES[0],
                "failure_reason": "parity_failed",
                "accepted_prefix_digest": prefix_digest,
                "committed_digest": prefix_digest,
                "correction_token_digest": None,
                "committed_position_count": 0,
                "committed_count": 0,
            }
        )
        return updated

    accepted = next(
        (index for index, (candidate, decision) in enumerate(zip(tokens, decisions)) if candidate != decision),
        width,
    )
    rejected = width - accepted
    corrected = 0 if accepted == width else 1
    if corrected:
        mode = TRANSACTION_MODES[2]
        state = "target_correction_ready"
        committed_tokens = tokens[:accepted] + (int(decisions[accepted]),)
        correction_digest = _token_digest(int(decisions[accepted]))
    else:
        mode = TRANSACTION_MODES[1]
        state = "verified_prefix_ready"
        committed_tokens = tokens
        correction_digest = None
    updated.update(
        {
            "accepted_prefix_length": accepted,
            "rejected_count": rejected,
            "target_corrected_count": corrected,
            "rollback_count": corrected,
            "commit_state": state,
            "transaction_mode": mode,
            "failure_reason": None,
            "accepted_prefix_digest": _prefix_digest(
                parent_digest=prefix_digest,
                position=record["target_prefix_position"] + accepted,
                tokens=tokens[:accepted],
            ),
            "committed_digest": _prefix_digest(
                parent_digest=prefix_digest,
                position=record["target_prefix_position"] + accepted + corrected,
                tokens=committed_tokens,
            ),
            "correction_token_digest": correction_digest,
            "committed_position_count": 0,
            "committed_count": 0,
        }
    )
    return updated


def commit_verified_prefix(
    *,
    plan: Mapping[str, Any] | None = None,
    transaction: Mapping[str, Any] | None = None,
    accepted_prefix_length: int | None = None,
    correction_token: int | None = None,
    ledger: TargetPrefixLedger | None = None,
    committed_prefix_position: int = 0,
    cycle_ref: str | None = None,
    idempotency_digest: str | None = None,
) -> dict[str, Any]:
    """Commit a verified prefix, or a verified prefix plus one target correction.

    Two entry points, one arbitration rule:

    * ``transaction=`` -- the verified transaction produced by
      :func:`verify_transaction_positions`. The commit is applied to ``ledger`` (when
      given) atomically, monotonically and idempotently per cycle digest; a
      rolled-back, parity-failed or otherwise uncommittable transaction is refused.
    * ``plan=`` with ``accepted_prefix_length=`` -- the deterministic commit
      *arithmetic* over a caller-supplied accepted prefix. It never claims that a
      proposal was verified here, and it never touches a ledger.
    """

    if transaction is None:
        if plan is None:
            raise _contract("verification_transaction_required")
        if ledger is not None:
            raise _contract("commit_ledger_requires_transaction")
        document = validate_speculative_decode_plan(plan)
        policy = document["compatibility"]["generation_policy"]
        if policy not in QUALIFIED_GENERATION_POLICIES:
            raise _contract("unsupported_generation_policy", policy)
        try:
            accepted = _integer(accepted_prefix_length, "invalid_accepted_prefix_length")
        except ValueError as exc:
            raise _contract("invalid_accepted_prefix_length", accepted_prefix_length) from exc
        if isinstance(committed_prefix_position, bool) or not isinstance(committed_prefix_position, int) or (
            committed_prefix_position < 0
        ):
            raise _contract("invalid_committed_prefix_position", committed_prefix_position)
        corrections = 0 if correction_token is None else 1
        correction_digest = (
            None if correction_token is None else _token_digest(_token(correction_token, "invalid_correction_token"))
        )
        count = accepted + corrections
        return {
            "kind": TRANSACTION_STATE_KIND,
            "plan_digest": document["plan_digest"],
            "cycle_ref": cycle_ref,
            "idempotency_digest": idempotency_digest,
            "generation_policy": _GENERATION_POLICY,
            "transaction_mode": TRANSACTION_MODES[2] if corrections else TRANSACTION_MODES[1],
            "commit_state": "committed_with_correction" if corrections else "committed",
            "accepted_prefix_length": accepted,
            "target_corrected_count": corrections,
            "committed_position_count": count,
            "committed_count": count,
            "target_prefix_position": committed_prefix_position,
            "target_watermark_position": committed_prefix_position + count,
            "target_watermark_digest": None,
            "committed_digest": None,
            "correction_token_digest": correction_digest,
            "verification": "caller_supplied_accepted_prefix",
            "idempotent_duplicate": False,
            "ledger_applied": False,
            "claim": DETERMINISTIC_VERIFIER_CLAIM,
        }

    record = _validate_transaction_state(transaction)
    if record["commit_state"] not in _COMMITTABLE_STATES:
        raise _contract("transaction_not_committable", record["commit_state"])
    accepted = record["accepted_prefix_length"]
    corrected = record["target_corrected_count"]
    if correction_token is not None:
        supplied = _token_digest(_token(correction_token, "invalid_correction_token"))
        if supplied != record["correction_token_digest"]:
            raise _contract("correction_token_mismatch", record["correction_token_digest"])
    if ledger is not None and not isinstance(ledger, TargetPrefixLedger):
        raise _contract("invalid_prefix_ledger", type(ledger).__name__)

    count = accepted + corrected
    position = record["target_prefix_position"] + count
    committed = dict(record)
    committed.update(
        {
            "commit_state": "committed_with_correction" if corrected else "committed",
            "committed_position_count": count,
            "committed_count": count,
            "target_watermark_position": position,
            "target_watermark_digest": record["committed_digest"],
            "idempotent_duplicate": False,
            "advanced": True,
            "ledger_applied": ledger is not None,
        }
    )
    if ledger is not None:
        watermark = ledger.commit(
            cycle_ref=record["cycle_ref"],
            idempotency_digest=record["idempotency_digest"],
            position=position,
            digest=record["committed_digest"],
        )
        committed.update(
            {
                "advanced": watermark["advanced"],
                "idempotent_duplicate": watermark["idempotent_duplicate"],
                "target_watermark_position": watermark["target_watermark_position"],
                "target_watermark_digest": watermark["target_watermark_digest"],
                "ledger_generation": watermark["generation"],
            }
        )
    return committed


def rollback_to_prefix(
    *,
    transaction: Mapping[str, Any] | None = None,
    ledger: TargetPrefixLedger | None = None,
    plan: Mapping[str, Any] | None = None,
    committed_prefix_position: int = 0,
    committed_prefix_digest: str | None = None,
) -> dict[str, Any]:
    """Roll a transaction back to the exact original target prefix.

    Nothing is committed and the target watermark is untouched; a ledger, when
    given, records the rollback and never moves.
    """

    if transaction is None:
        if plan is None:
            raise _contract("verification_transaction_required")
        if ledger is not None:
            raise _contract("commit_ledger_requires_transaction")
        document = validate_speculative_decode_plan(plan)
        policy = document["compatibility"]["generation_policy"]
        if policy not in QUALIFIED_GENERATION_POLICIES:
            raise _contract("unsupported_generation_policy", policy)
        if isinstance(committed_prefix_position, bool) or not isinstance(committed_prefix_position, int) or (
            committed_prefix_position < 0
        ):
            raise _contract("invalid_committed_prefix_position", committed_prefix_position)
        if committed_prefix_digest is None:
            prefix_digest = _prefix_digest(
                parent_digest=_GENESIS_DIGEST, position=committed_prefix_position, tokens=()
            )
        else:
            prefix_digest = _digest_or_none(committed_prefix_digest, "invalid_committed_prefix_digest")
        return _transaction_state(
            plan_digest=document["plan_digest"],
            cycle_ref="cycle:rollback",
            target_prefix_position=committed_prefix_position,
            target_prefix_digest=prefix_digest,
            target_watermark_position=committed_prefix_position,
            target_watermark_digest=prefix_digest,
            accepted_prefix_digest=prefix_digest,
            committed_digest=prefix_digest,
            commit_state="rolled_back",
            transaction_mode=TRANSACTION_MODES[0],
            rollback_count=1,
        )

    record = _validate_transaction_state(transaction)
    if record["commit_state"] in _COMMITTED_STATES:
        raise _contract("transaction_not_rollbackable", record["commit_state"])
    if ledger is not None:
        if not isinstance(ledger, TargetPrefixLedger):
            raise _contract("invalid_prefix_ledger", type(ledger).__name__)
        ledger.rollback(cycle_ref=record["cycle_ref"])
    rolled = dict(record)
    rolled.update(
        {
            "commit_state": "rolled_back",
            "transaction_mode": TRANSACTION_MODES[0],
            "rollback_count": max(1, record["rollback_count"]),
            "committed_position_count": 0,
            "committed_count": 0,
            "target_watermark_position": record["target_prefix_position"],
            "target_watermark_digest": record["target_prefix_digest"],
        }
    )
    return rolled


def integrate_request_admission(
    *,
    plan: Mapping[str, Any],
    request_id: str,
    preference: str,
    generation_policy: str | None = None,
    revoked: bool = False,
) -> dict[str, Any]:
    """Project the deterministic admission of one future request.

    Speculation is off by default and a preference applies to a future request only.
    An unsupported or unknown generation policy fails speculative admission, uses
    target-only mode, and is never silently changed to greedy.
    """

    document = validate_speculative_decode_plan(plan)
    try:
        reference = _text(request_id, "request_id_invalid")
    except ValueError as exc:
        raise _contract("request_id_invalid", request_id) from exc
    if preference not in _REQUEST_PREFERENCES:
        raise _contract("request_preference_invalid", preference)
    if type(revoked) is not bool:
        raise _contract("request_admission_invalid", revoked)
    requested_policy = (
        generation_policy if generation_policy is not None else document["compatibility"]["generation_policy"]
    )
    try:
        requested_policy = _text(requested_policy, "unsupported_generation_policy")
    except ValueError as exc:
        raise _contract("unsupported_generation_policy", requested_policy) from exc

    decision = document["decision"]
    if requested_policy not in QUALIFIED_GENERATION_POLICIES:
        overlay_disabled_reason = "incompatible_token_semantics"
    elif revoked:
        overlay_disabled_reason = "parity_failed"
    elif decision["state"] != ENABLED_DECISION:
        overlay_disabled_reason = decision["reason"]
    else:
        overlay_disabled_reason = None

    if preference == "target_only_requested":
        mode, fallback, admission, effective = ("target_only", None, "target_only_requested", None)
    elif revoked:
        mode, fallback, admission, effective = (
            "target_only",
            "parity_failed",
            "revoked_requalification_required",
            None,
        )
    elif overlay_disabled_reason is not None:
        mode, fallback, admission, effective = (
            "target_only",
            overlay_disabled_reason,
            "target_only_fallback",
            None,
        )
    else:
        mode, fallback, admission, effective = (
            "speculative_admitted",
            None,
            "speculative_admitted",
            _GENERATION_POLICY,
        )
    return {
        "request_id": reference,
        "plan_digest": document["plan_digest"],
        "requested_preference": preference,
        "requested_generation_policy": requested_policy,
        "effective_generation_policy": effective,
        "mode": mode,
        "admission": admission,
        "fallback_reason": fallback,
        "overlay_disabled_reason": overlay_disabled_reason,
        "revoked": revoked,
        "off_by_default": True,
        "applies_to": "future_requests_only",
        "target_only_available": True,
        "claim": DETERMINISTIC_VERIFIER_CLAIM,
    }


# ---------------------------------------------------------------------------
# Honest not-implemented boundaries
# ---------------------------------------------------------------------------

BOUNDARY_CODES = frozenset(
    {
        "private_proposal_transfer_not_implemented",
        "live_route_benchmark_not_implemented",
        "physical_qualification_not_implemented",
        "product_projection_not_implemented",
    }
)
_BOUNDARY_NOTE = "A11 scaffold stage: this execution seam is not implemented and never silently succeeds"


def _unavailable(code: str, **context: Any) -> None:
    detail = _BOUNDARY_NOTE
    if context:
        detail = f"{detail} ({', '.join(sorted(context))})"
    raise SpeculativeDecodeUnavailable(code, detail)


def transfer_proposal_envelope(**context: Any) -> dict[str, Any]:
    """Boundary: requires the authenticated private runtime transport."""

    _unavailable("private_proposal_transfer_not_implemented", **context)


def run_paired_product_benchmark(**context: Any) -> dict[str, Any]:
    """Boundary: requires the ordinary same-route product benchmark session."""

    _unavailable("live_route_benchmark_not_implemented", **context)


def execute_qualification_bundle(**context: Any) -> dict[str, Any]:
    """Boundary: requires the physical positive/negative and browser gates."""

    _unavailable("physical_qualification_not_implemented", **context)


def project_workspace_state(**context: Any) -> dict[str, Any]:
    """Boundary: requires the eight-workspace private browser product path."""

    _unavailable("product_projection_not_implemented", **context)


__all__ = [
    "BENCHMARK_BLOCKING_CONDITIONS",
    "BENCHMARK_CONFIDENCE",
    "BENCHMARK_IMPROVEMENT_FRACTION",
    "BENCHMARK_INVALIDATION_REASONS",
    "BENCHMARK_MAXIMUM_TTFT_REGRESSION_FRACTION",
    "BENCHMARK_MEASURED_WINDOWS",
    "BENCHMARK_MINIMUM_BUCKETS",
    "BENCHMARK_MINIMUM_COMMITTED_TOKENS",
    "BENCHMARK_MINIMUM_COMPLETED_REQUESTS",
    "BENCHMARK_PAIRED_LOWER_BOUND_FRACTION",
    "BENCHMARK_PAIRED_ORDER",
    "BENCHMARK_PROTOCOL",
    "BENCHMARK_WARMUPS_PER_MODE",
    "BENCHMARK_WINDOW_MODE_ORDER",
    "BOUNDARY_CODES",
    "CIRCUIT_BREAKER_COOLDOWN_SECONDS",
    "CIRCUIT_BREAKER_CONSECUTIVE_DEADLINES",
    "CIRCUIT_BREAKER_ROLLING_WINDOW_CYCLES",
    "CIRCUIT_BREAKER_STATES",
    "CLAIM_BOUNDARY",
    "CORRECTNESS_REASONS",
    "DEADLINE_MAXIMUM_MS",
    "DEADLINE_MINIMUM_MS",
    "DEADLINE_UNKNOWN_REASON",
    "DEADLINE_WARM_P95_MULTIPLIER",
    "DETERMINISTIC_VERIFIER_CLAIM",
    "DISABLED_DECISION",
    "DISABLED_REASONS",
    "ENABLED_DECISION",
    "IMPLEMENTED_CLAIM",
    "NO_SAMPLE_REASONS",
    "PLAN_PRIVACY_STATEMENT",
    "PLAN_PROTOCOL",
    "PROPOSAL_MAXIMUM_BYTES_BOUND",
    "PROPOSAL_MAXIMUM_COUNT_BOUND",
    "PROPOSAL_MAXIMUM_WIDTH_BOUND",
    "QUALIFIED_GENERATION_POLICIES",
    "QUALIFICATION_PROTOCOL",
    "RUNTIME_PROTOCOL",
    "SAMPLE_REQUIRED_REASONS",
    "SCAFFOLD_STAGE",
    "SpeculativeDecodeContractError",
    "SpeculativeDecodeUnavailable",
    "TRANSACTION_COMMIT_STATES",
    "TRANSACTION_FAILURES",
    "TRANSACTION_MODES",
    "TRANSACTION_STATE_KIND",
    "TargetPrefixLedger",
    "admit_proposal_deadline",
    "check_prefix_parity",
    "circuit_breaker_decision",
    "commit_verified_prefix",
    "compatibility_fixtures",
    "execute_qualification_bundle",
    "integrate_request_admission",
    "open_target_verification_transaction",
    "project_workspace_state",
    "rollback_to_prefix",
    "run_paired_product_benchmark",
    "seal",
    "sequential_target_decisions",
    "transfer_proposal_envelope",
    "validate_public_projection",
    "validate_speculative_decode_benchmark",
    "validate_speculative_decode_plan",
    "validate_speculative_decode_qualification",
    "validate_speculative_decode_runtime",
    "verify_transaction_positions",
]
