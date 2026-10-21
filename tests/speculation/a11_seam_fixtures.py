# SPDX-License-Identifier: AGPL-3.0-only
"""Shared deterministic fixtures for the A11 in-process seam suites.

Not a test module (no collected tests): it only builds the admissible plan and the
injected decision-source doubles both seam suites drive.  Everything here is
in-process and deterministic.
"""

from __future__ import annotations

import copy

import mycelium_speculative_decode as sd

#: Candidate token ids chosen far away from any record arithmetic, so a leaked raw
#: token id is unmistakable in a projection.
CANDIDATES = (900_001, 900_002, 900_003, 900_004)
CORRECTION_TOKENS = (800_000, 800_001, 800_002, 800_003)


def admissible_plan() -> dict:
    """The fixture plan promoted to a qualified-enabled, admissible plan."""

    plan = copy.deepcopy(sd.compatibility_fixtures()["speculative_decode_plan"])
    plan["verifier"]["status"] = "qualified"
    for key in plan["compatibility"]:
        if key not in {"generation_policy", "kv_alias", "kv_transfer"}:
            plan["compatibility"][key] = True
    plan["deadline_policy"]["warm_target_only_p95_tpot_ms"] = 80.0
    plan["observation"] = {
        "sample_count": 12,
        "observed_wall_clock_reduction_fraction": 0.14,
        "paired_lower_bound_fraction": 0.11,
        "ttft_regression_fraction": 0.02,
    }
    plan["decision"] = {
        "state": sd.ENABLED_DECISION,
        "reason": "material_gain_and_parity_qualified",
        "material_gain_threshold": sd.BENCHMARK_IMPROVEMENT_FRACTION,
        "target_only_fallback": True,
    }
    return sd.seal(plan)


def disabled_plan() -> dict:
    """The unmodified compatibility fixture: not enabled, so nothing is admitted."""

    return sd.compatibility_fixtures()["speculative_decode_plan"]


class CausalTarget:
    """Deterministic causal target-decision double (never reads candidates[i])."""

    def __init__(self, decide) -> None:
        self.decide = decide
        self.bounded_calls = 0
        self.bounded_widths: list[int] = []
        self.sequential_calls = 0

    def evaluate_bounded_positions(self, *, prefix_digest, candidates):
        self.bounded_calls += 1
        self.bounded_widths.append(len(candidates))
        return tuple(
            int(self.decide(prefix_digest, tuple(candidates[:index])))
            for index in range(len(candidates))
        )

    def sequential_decisions(self, *, prefix_digest, candidates):
        self.sequential_calls += len(candidates)
        return tuple(
            int(self.decide(prefix_digest, tuple(candidates[:index])))
            for index in range(len(candidates))
        )


def matching_target() -> CausalTarget:
    """A causal target that agrees with ``CANDIDATES`` at every position."""

    def decide(_prefix_digest: str, accepted: tuple[int, ...]) -> int:
        return CANDIDATES[len(accepted)]

    return CausalTarget(decide)


def non_causal_target() -> CausalTarget:
    """A cheating batched shortcut: the bounded call claims every position matched."""

    class NonCausal(CausalTarget):
        def evaluate_bounded_positions(self, *, prefix_digest, candidates):
            self.bounded_calls += 1
            self.bounded_widths.append(len(candidates))
            return tuple(candidates)

        def sequential_decisions(self, *, prefix_digest, candidates):
            self.sequential_calls += len(candidates)
            return tuple(700_000 + index for index in range(len(candidates)))

    return NonCausal(lambda _prefix_digest, _accepted: 0)


def refusing_target() -> CausalTarget:
    """A causal target that rejects every position from the first one on."""

    def decide(_prefix_digest: str, accepted: tuple[int, ...]) -> int:
        return CORRECTION_TOKENS[len(accepted)]

    return CausalTarget(decide)
