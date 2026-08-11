"""Closed M20 speculative-decoding planning and privacy-reduced runtime evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


PLAN_PROTOCOL = "mycelium.m20_speculative_plan.v1"
RUNTIME_PROTOCOL = "mycelium.m20_speculative_runtime.v1"
_DIGEST_PREFIX = "sha256:"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(_canonical(value)).hexdigest()


def _closed(value: Mapping[str, Any], fields: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(code)
    return copy.deepcopy(dict(value))


def _text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(code)
    return value


def _number(value: Any, code: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(code)
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(code)
    return result


def _integer(value: Any, code: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(code)
    return value


def _sha256(value: Any, code: str) -> str:
    text = _text(value, code)
    if not text.startswith(_DIGEST_PREFIX) or len(text) != 71:
        raise ValueError(code)
    try:
        int(text.removeprefix(_DIGEST_PREFIX), 16)
    except ValueError as exc:
        raise ValueError(code) from exc
    return text


_BINDING_FIELDS = frozenset(
    {
        "deployment_id",
        "deployment_epoch",
        "graph_digest",
        "membership_generation",
        "model_id",
        "model_revision",
        "qualification_id",
        "qualification_digest",
    }
)


def _binding(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _closed(value, _BINDING_FIELDS, "m20_binding_invalid")
    for key in ("deployment_id", "model_id", "model_revision", "qualification_id"):
        _text(result[key], "m20_binding_invalid")
    _integer(result["deployment_epoch"], "m20_binding_invalid", minimum=1)
    _integer(result["membership_generation"], "m20_binding_invalid", minimum=1)
    _sha256(result["graph_digest"], "m20_binding_invalid")
    _sha256(result["qualification_digest"], "m20_binding_invalid")
    return result


_MODEL_FIELDS = frozenset(
    {
        "model_id",
        "model_revision",
        "tokenizer_digest",
        "vocabulary_size",
        "special_tokens_digest",
        "position_semantics_digest",
        "kv_schema_digest",
    }
)


def _model(value: Mapping[str, Any], code: str) -> dict[str, Any]:
    result = _closed(value, _MODEL_FIELDS, code)
    _text(result["model_id"], code)
    _text(result["model_revision"], code)
    _integer(result["vocabulary_size"], code, minimum=1)
    for key in (
        "tokenizer_digest",
        "special_tokens_digest",
        "position_semantics_digest",
        "kv_schema_digest",
    ):
        _sha256(result[key], code)
    return result


_COMPATIBILITY_FIELDS = frozenset(
    {
        "tokenizer",
        "vocabulary",
        "special_tokens",
        "position_semantics",
        "separate_kv_ownership",
        "batched_target_verification",
    }
)
_MEASUREMENT_FIELDS = frozenset(
    {
        "sample_count",
        "target_only_tpot_ms",
        "draft_tpot_ms",
        "verification_batch_ms",
        "proposal_transfer_ms",
        "observed_acceptance_fraction",
        "predicted_gain_fraction",
        "observed_gain_fraction",
    }
)
_DECISION_FIELDS = frozenset(
    {"state", "reason", "material_gain_threshold", "target_fallback"}
)
_PLAN_FIELDS = frozenset(
    {
        "protocol",
        "binding",
        "target",
        "draft",
        "workload_id",
        "proposal_width",
        "acceptance_distribution",
        "compatibility",
        "measurements",
        "decision",
        "privacy",
        "plan_digest",
    }
)


def build_speculative_plan(
    *,
    binding: Mapping[str, Any],
    target: Mapping[str, Any],
    draft: Mapping[str, Any],
    workload_id: str,
    proposal_width: int,
    acceptance_distribution: Sequence[float],
    compatibility: Mapping[str, bool],
    measurements: Mapping[str, Any],
    material_gain_threshold: float = 0.10,
) -> dict[str, Any]:
    target_doc = _model(target, "m20_target_invalid")
    draft_doc = _model(draft, "m20_draft_invalid")
    if (target_doc["model_id"], target_doc["model_revision"]) == (
        draft_doc["model_id"],
        draft_doc["model_revision"],
    ):
        raise ValueError("m20_draft_target_identity_equal")
    width = _integer(proposal_width, "m20_proposal_width_invalid", minimum=1)
    distribution = [
        _number(item, "m20_acceptance_distribution_invalid")
        for item in acceptance_distribution
    ]
    if len(distribution) != width + 1 or abs(sum(distribution) - 1.0) > 1e-9:
        raise ValueError("m20_acceptance_distribution_invalid")
    compat = _closed(compatibility, _COMPATIBILITY_FIELDS, "m20_compatibility_invalid")
    if any(type(value) is not bool for value in compat.values()):
        raise ValueError("m20_compatibility_invalid")
    measured = _closed(measurements, _MEASUREMENT_FIELDS, "m20_measurements_invalid")
    _integer(measured["sample_count"], "m20_measurements_invalid")
    for key in _MEASUREMENT_FIELDS - {"sample_count"}:
        value = measured[key]
        if value is not None:
            _number(value, "m20_measurements_invalid")
    threshold = _number(material_gain_threshold, "m20_gain_threshold_invalid")
    compatible = all(compat.values())
    gain = measured["observed_gain_fraction"]
    if not compat["batched_target_verification"]:
        state, reason = "disabled", "batched_target_verification_unavailable"
    elif not compatible:
        state, reason = "disabled", "draft_target_incompatible"
    elif measured["sample_count"] < 1 or gain is None:
        state, reason = "disabled", "insufficient_measurements"
    elif float(gain) < threshold:
        state, reason = "disabled", "material_gain_not_observed"
    else:
        state, reason = "qualified_enabled", "material_gain_and_parity_qualified"
    body: dict[str, Any] = {
        "protocol": PLAN_PROTOCOL,
        "binding": _binding(binding),
        "target": target_doc,
        "draft": draft_doc,
        "workload_id": _text(workload_id, "m20_workload_invalid"),
        "proposal_width": width,
        "acceptance_distribution": distribution,
        "compatibility": compat,
        "measurements": measured,
        "decision": {
            "state": state,
            "reason": reason,
            "material_gain_threshold": threshold,
            "target_fallback": True,
        },
        "privacy": "no prompts, decoded text, logits, token ids, tensors, credentials, or paths",
    }
    body["plan_digest"] = _digest(body)
    return validate_speculative_plan(body)


def validate_speculative_plan(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        result = _closed(document, _PLAN_FIELDS, "m20_plan_invalid")
        if result["protocol"] != PLAN_PROTOCOL:
            raise ValueError("m20_plan_invalid")
        _binding(result["binding"])
        _model(result["target"], "m20_plan_invalid")
        _model(result["draft"], "m20_plan_invalid")
        _text(result["workload_id"], "m20_plan_invalid")
        width = _integer(result["proposal_width"], "m20_plan_invalid", minimum=1)
        distribution = result["acceptance_distribution"]
        if not isinstance(distribution, list) or len(distribution) != width + 1:
            raise ValueError("m20_plan_invalid")
        if abs(sum(_number(item, "m20_plan_invalid") for item in distribution) - 1.0) > 1e-9:
            raise ValueError("m20_plan_invalid")
        compat = _closed(result["compatibility"], _COMPATIBILITY_FIELDS, "m20_plan_invalid")
        if any(type(value) is not bool for value in compat.values()):
            raise ValueError("m20_plan_invalid")
        measured = _closed(result["measurements"], _MEASUREMENT_FIELDS, "m20_plan_invalid")
        _integer(measured["sample_count"], "m20_plan_invalid")
        for key in _MEASUREMENT_FIELDS - {"sample_count"}:
            if measured[key] is not None:
                _number(measured[key], "m20_plan_invalid")
        decision = _closed(result["decision"], _DECISION_FIELDS, "m20_plan_invalid")
        if decision["state"] not in {"disabled", "qualified_enabled"}:
            raise ValueError("m20_plan_invalid")
        _text(decision["reason"], "m20_plan_invalid")
        _number(decision["material_gain_threshold"], "m20_plan_invalid")
        if decision["target_fallback"] is not True:
            raise ValueError("m20_plan_invalid")
        if result["privacy"] != "no prompts, decoded text, logits, token ids, tensors, credentials, or paths":
            raise ValueError("m20_plan_invalid")
        supplied = _sha256(result["plan_digest"], "m20_plan_invalid")
        unsigned = copy.deepcopy(result)
        del unsigned["plan_digest"]
        if supplied != _digest(unsigned):
            raise ValueError("m20_plan_invalid")
        return result
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "m20_plan_invalid":
            raise
        raise ValueError("m20_plan_invalid") from exc


@dataclass(frozen=True)
class DecodeResult:
    token_ids: tuple[int, ...]
    proposed: int
    verified: int
    accepted: int
    rejected: int
    rollback: int
    fallback: bool
    cancelled: bool


class SpeculativeDecoder:
    """Greedy target-authoritative speculative decoder over injected model callbacks."""

    def __init__(
        self,
        *,
        draft_step: Callable[[tuple[int, ...]], int],
        target_step: Callable[[tuple[int, ...]], int],
        target_verify: Callable[[tuple[int, ...], tuple[int, ...]], Sequence[int]],
        proposal_width: int,
    ) -> None:
        self._draft_step = draft_step
        self._target_step = target_step
        self._target_verify = target_verify
        self._width = _integer(proposal_width, "m20_proposal_width_invalid", minimum=1)

    def decode(
        self,
        prompt: Sequence[int],
        *,
        max_new_tokens: int,
        draft_available: Callable[[], bool] = lambda: True,
        cancel_requested: Callable[[], bool] = lambda: False,
    ) -> DecodeResult:
        limit = _integer(max_new_tokens, "m20_max_new_tokens_invalid", minimum=1)
        context = tuple(_integer(token, "m20_token_invalid") for token in prompt)
        output: list[int] = []
        proposed = verified = accepted = rejected = rollback = 0
        fallback = cancelled = False
        while len(output) < limit:
            if cancel_requested():
                cancelled = True
                break
            if not draft_available():
                fallback = True
                token = _integer(self._target_step(context), "m20_target_token_invalid")
                output.append(token)
                context += (token,)
                continue
            width = min(self._width, limit - len(output))
            proposal: list[int] = []
            draft_context = context
            for _ in range(width):
                token = _integer(self._draft_step(draft_context), "m20_draft_token_invalid")
                proposal.append(token)
                draft_context += (token,)
            proposed += len(proposal)
            target_tokens = tuple(
                _integer(token, "m20_target_token_invalid")
                for token in self._target_verify(context, tuple(proposal))
            )
            if len(target_tokens) != len(proposal):
                raise ValueError("m20_verification_width_invalid")
            verified += len(proposal)
            mismatch = next(
                (index for index, pair in enumerate(zip(proposal, target_tokens, strict=True)) if pair[0] != pair[1]),
                None,
            )
            if mismatch is None:
                accepted += len(proposal)
                output.extend(proposal)
                context += tuple(proposal)
                continue
            accepted += mismatch
            rejected += 1
            rollback += len(proposal) - mismatch
            committed = proposal[:mismatch] + [target_tokens[mismatch]]
            output.extend(committed)
            context += tuple(committed)
        return DecodeResult(
            token_ids=tuple(output),
            proposed=proposed,
            verified=verified,
            accepted=accepted,
            rejected=rejected,
            rollback=rollback,
            fallback=fallback,
            cancelled=cancelled,
        )


_REQUEST_FIELDS = frozenset(
    {
        "request_id",
        "proposal_width",
        "proposed_count",
        "target_verified_count",
        "accepted_count",
        "rejected_count",
        "rollback_count",
        "fallback_state",
        "terminal_state",
        "cleanup_complete",
    }
)
_RUNTIME_FIELDS = frozenset({"protocol", "binding", "mode", "requests", "runtime_digest"})


def build_speculative_runtime(
    *, binding: Mapping[str, Any], mode: str, requests: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "protocol": RUNTIME_PROTOCOL,
        "binding": _binding(binding),
        "mode": mode,
        "requests": [copy.deepcopy(dict(request)) for request in requests],
    }
    body["runtime_digest"] = _digest(body)
    return validate_speculative_runtime(body)


def validate_speculative_runtime(document: Mapping[str, Any]) -> dict[str, Any]:
    try:
        result = _closed(document, _RUNTIME_FIELDS, "m20_runtime_invalid")
        if result["protocol"] != RUNTIME_PROTOCOL or result["mode"] not in {
            "disabled",
            "speculative",
            "target_fallback",
        }:
            raise ValueError("m20_runtime_invalid")
        _binding(result["binding"])
        if not isinstance(result["requests"], list):
            raise ValueError("m20_runtime_invalid")
        for item in result["requests"]:
            request = _closed(item, _REQUEST_FIELDS, "m20_runtime_invalid")
            _text(request["request_id"], "m20_runtime_invalid")
            for key in (
                "proposal_width",
                "proposed_count",
                "target_verified_count",
                "accepted_count",
                "rejected_count",
                "rollback_count",
            ):
                _integer(request[key], "m20_runtime_invalid")
            if request["accepted_count"] + request["rejected_count"] > request["target_verified_count"]:
                raise ValueError("m20_runtime_invalid")
            if request["fallback_state"] not in {"none", "draft_lost", "policy_disabled", "verification_failed"}:
                raise ValueError("m20_runtime_invalid")
            if request["terminal_state"] not in {"completed", "cancelled", "aborted"}:
                raise ValueError("m20_runtime_invalid")
            if request["cleanup_complete"] is not True:
                raise ValueError("m20_runtime_invalid")
        supplied = _sha256(result["runtime_digest"], "m20_runtime_invalid")
        unsigned = copy.deepcopy(result)
        del unsigned["runtime_digest"]
        if supplied != _digest(unsigned):
            raise ValueError("m20_runtime_invalid")
        return result
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "m20_runtime_invalid":
            raise
        raise ValueError("m20_runtime_invalid") from exc


__all__ = [
    "DecodeResult",
    "PLAN_PROTOCOL",
    "RUNTIME_PROTOCOL",
    "SpeculativeDecoder",
    "build_speculative_plan",
    "build_speculative_runtime",
    "validate_speculative_plan",
    "validate_speculative_runtime",
]
