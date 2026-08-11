from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import SpeculativeConfig


@dataclass(frozen=True)
class SpeculativeScore:
    enabled: bool
    reason: str
    expected_accepted_tokens: float
    target_only_tps: float
    speculative_tps: float
    use_speculative: bool
    target_fallback: bool
    draft_kv_owner: str
    target_kv_owner: str
    proposal_payload_bytes: int
    expected_committed_tokens: float
    predicted_gain_fraction: float
    material_gain_threshold: float


def score_speculative(
    config: SpeculativeConfig,
    target_decode_ms: float,
    draft_decode_ms: float,
    verification_ms: float,
    proposal_transfer_ms: float,
    *,
    runtime_supported: bool = True,
    proposal_payload_bytes: int = 16,
    material_gain_threshold: float = 0.10,
) -> SpeculativeScore:
    if min(target_decode_ms, draft_decode_ms, verification_ms, proposal_transfer_ms) < 0:
        raise ValueError("speculative timing inputs must be non-negative")
    if proposal_payload_bytes <= 0:
        raise ValueError("proposal payload must be positive")
    if not 0 <= material_gain_threshold < 1:
        raise ValueError("material gain threshold must be in [0, 1)")
    target_tps = 1000.0 / target_decode_ms if target_decode_ms > 0 else float("inf")
    expected = sum(
        accepted_count * probability
        for accepted_count, probability in enumerate(config.accepted_count_distribution)
    )
    expected_committed = sum(
        (
            accepted_count
            if accepted_count == config.proposal_width
            else accepted_count + 1
        )
        * probability
        for accepted_count, probability in enumerate(
            config.accepted_count_distribution
        )
    )
    cycle_ms = (
        config.proposal_width * draft_decode_ms
        + verification_ms
        + proposal_transfer_ms
    )
    speculative_tps = (
        expected_committed * 1000.0 / cycle_ms if cycle_ms > 0 else float("inf")
    )
    predicted_gain = (
        (speculative_tps / target_tps) - 1.0
        if math.isfinite(target_tps) and target_tps > 0
        else float("-inf")
    )
    enabled = runtime_supported
    use_speculative = enabled and predicted_gain >= material_gain_threshold
    reason = (
        "beneficial"
        if use_speculative
        else ("runtime_unsupported" if not enabled else "material_gain_not_predicted")
    )
    return SpeculativeScore(
        enabled=enabled,
        reason=reason,
        expected_accepted_tokens=expected,
        target_only_tps=target_tps,
        speculative_tps=speculative_tps,
        use_speculative=use_speculative,
        target_fallback=True,
        draft_kv_owner=f"draft:{config.draft_model_id}@{config.draft_revision}",
        target_kv_owner="target_model_track",
        proposal_payload_bytes=proposal_payload_bytes,
        expected_committed_tokens=expected_committed,
        predicted_gain_fraction=predicted_gain,
        material_gain_threshold=material_gain_threshold,
    )
