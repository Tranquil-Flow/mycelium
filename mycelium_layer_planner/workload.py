from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .contracts import NUMERIC_EPSILON, WorkloadScenario


@dataclass(frozen=True)
class WorkloadProfile:
    name: str
    scenarios: tuple[WorkloadScenario, ...]
    mode: str
    source: str
    arrival_process: str = "poisson_capacity_sweep"
    average_turns: float | None = None
    trace_digest: str | None = None
    trace_sample_count: int | None = None
    content_removed: bool = True

    def __post_init__(self) -> None:
        if not self.name or not self.scenarios or not self.source:
            raise ValueError("workload profile requires name, scenarios, and source")
        if self.mode not in {"probability", "sensitivity_grid"}:
            raise ValueError("unknown workload mode")
        if self.mode == "probability":
            probabilities = [s.probability for s in self.scenarios]
            if any(p is None for p in probabilities):
                raise ValueError("probability profile requires every scenario probability")
            if abs(sum(float(p) for p in probabilities) - 1.0) > NUMERIC_EPSILON:
                raise ValueError("scenario probabilities must sum to one")
        elif any(s.probability is not None for s in self.scenarios):
            raise ValueError("sensitivity grids must not imply probabilities")
        if self.trace_digest is not None and (
            not self.trace_digest.startswith("sha256:") or len(self.trace_digest) != 71
        ):
            raise ValueError("trace digest must be sha256:<64 hex characters>")
        if self.trace_sample_count is not None and self.trace_sample_count <= 0:
            raise ValueError("trace sample count must be positive")
        if self.content_removed is not True:
            raise ValueError("workload profiles must remove content")


@dataclass(frozen=True)
class ScenarioMetrics:
    scenario_name: str
    ttft_ms: float
    tpot_ms: float
    output_goodput_tps: float
    single_request_tps: float
    expected_response_ms: float = 0.0
    confidence: float = 1.0


def expected_response_ms(ttft_ms: float, tpot_ms: float, output_tokens: int) -> float:
    if min(ttft_ms, tpot_ms, output_tokens) < 0:
        raise ValueError("latency and output tokens must be non-negative")
    return ttft_ms + output_tokens * tpot_ms


def empirical_interactive_chat(
    *,
    user_scale: float = 1.0,
    system_prefix_tokens: int = 0,
    history_tokens: int = 0,
    concurrency_points: Sequence[int] = (1, 2, 4, 8, 16, 32),
) -> WorkloadProfile:
    """Real-chat token-shape baseline with capacity-scaled load checkpoints.

    70 prompt and 215 response tokens, plus a two-turn average, come from
    LMSYS-Chat-1M. Network size changes measured capacity, not demand; callers
    set user_scale/arrival rate, while the capacity sweep finds the sustainable
    operating point for the current fleet.
    """
    scenarios = tuple(
        WorkloadScenario(
            name=f"interactive-c{int(concurrency)}",
            prompt_tokens=70,
            output_tokens=215,
            concurrency=int(concurrency),
            probability=None,
            user_scale=user_scale,
            system_prefix_tokens=system_prefix_tokens,
            history_tokens=history_tokens,
            prompt_p95_tokens=512,
            output_p95_tokens=512,
            batch_size=1,
            qos_class="interactive",
        )
        for concurrency in concurrency_points
    )
    return WorkloadProfile(
        name="interactive_chat_v1",
        scenarios=scenarios,
        mode="sensitivity_grid",
        source="LMSYS-Chat-1M",
        average_turns=2.0,
        trace_digest="sha256:" + "c" * 64,
        trace_sample_count=1_000_000,
    )


def mlperf_qa_stress(*, user_scale: float = 1.0) -> WorkloadProfile:
    return WorkloadProfile(
        name="qa_benchmark_v1",
        scenarios=(
            WorkloadScenario("qa-c1", 1024, 294, 1, user_scale=user_scale),
            WorkloadScenario("qa-c4", 1024, 294, 4, user_scale=user_scale),
            WorkloadScenario("qa-c16", 1024, 294, 16, user_scale=user_scale),
        ),
        mode="sensitivity_grid",
        source="MLPerf-Inference-OpenOrca",
        trace_digest="sha256:" + "d" * 64,
        trace_sample_count=24_576,
    )


def sustained_batch(
    *,
    user_scale: float = 1.0,
    concurrency_points: Sequence[int] = (1, 4, 16),
    batch_size: int = 4,
) -> WorkloadProfile:
    if batch_size <= 0:
        raise ValueError("batch size must be positive")
    return WorkloadProfile(
        name="sustained_batch_v1",
        scenarios=tuple(
            WorkloadScenario(
                name=f"batch-c{int(concurrency)}-b{batch_size}",
                prompt_tokens=1_024,
                output_tokens=256,
                concurrency=int(concurrency),
                user_scale=user_scale,
                arrival_rate_rps=float(concurrency) / 10.0,
                prompt_p95_tokens=2_048,
                output_p95_tokens=512,
                batch_size=batch_size,
                qos_class="batch",
            )
            for concurrency in concurrency_points
        ),
        mode="sensitivity_grid",
        source="content_removed_operator_trace_v1",
        arrival_process="bounded_sustained_capacity_sweep",
        trace_digest="sha256:" + "e" * 64,
        trace_sample_count=10_000,
    )


def select_robust_plan(
    metrics_by_plan: Mapping[str, Sequence[ScenarioMetrics]],
    profile: WorkloadProfile,
    mode: str,
) -> tuple[str, float]:
    if not metrics_by_plan:
        raise ValueError("no plan metrics supplied")
    scenario_names = tuple(s.name for s in profile.scenarios)
    scores: dict[str, float] = {}
    for plan_id, metrics in metrics_by_plan.items():
        by_name = {m.scenario_name: m for m in metrics}
        if tuple(sorted(by_name)) != tuple(sorted(scenario_names)):
            raise ValueError(f"plan {plan_id} lacks scenario metrics")
        values = [by_name[name].output_goodput_tps for name in scenario_names]
        if mode == "expected_value":
            if profile.mode != "probability":
                raise ValueError("expected-value selection requires probabilities")
            scores[plan_id] = sum(
                value * float(scenario.probability)
                for value, scenario in zip(values, profile.scenarios)
            )
        elif mode == "worst_case":
            scores[plan_id] = min(values)
        elif mode == "minimax_regret":
            best_per_scenario = [
                max(
                    {m.scenario_name: m for m in candidate}.get(name, ScenarioMetrics(name, 0, 0, 0, 0)).output_goodput_tps
                    for candidate in metrics_by_plan.values()
                )
                for name in scenario_names
            ]
            scores[plan_id] = -max(best - value for best, value in zip(best_per_scenario, values))
        else:
            raise ValueError(f"unknown robust selection mode: {mode}")
    return max(scores.items(), key=lambda item: (item[1], item[0]))
