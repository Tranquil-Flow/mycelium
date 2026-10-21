# SPDX-License-Identifier: AGPL-3.0-only
"""Planner-owned A6 recovery intent projection.

The Planner can nominate candidates but never authorizes route readiness.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from mycelium_a6_recovery import build_recovery_intent


def project_recovery_intent(
    binding: Mapping[str, Any],
    *,
    retained_path_ids: Sequence[str],
    candidate_path_ids: Sequence[str],
    equivalent_candidate_generations: int,
    candidate_first_seen_unix_ms: int,
    generated_at_unix_ms: int,
    rejection_reasons: Sequence[str] = (),
    breaker_open: bool = False,
) -> dict[str, Any]:
    """Publish one strict non-authorizing recovery-intent document."""

    return build_recovery_intent(
        binding,
        retained_path_ids=retained_path_ids,
        candidate_path_ids=candidate_path_ids,
        equivalent_candidate_generations=equivalent_candidate_generations,
        candidate_first_seen_unix_ms=candidate_first_seen_unix_ms,
        generated_at_unix_ms=generated_at_unix_ms,
        rejection_reasons=rejection_reasons,
        breaker_open=breaker_open,
    )
