"""TDD specification for deterministic trace generation and minimization."""

import json

from mycelium_conformance.router_model import RouterModel
from mycelium_conformance.trace_generator import (
    DEFAULT_ACTIONS,
    TraceAction,
    generate_bounded_traces,
    minimize_trace,
    run_reference_trace,
    trace_to_json,
)


def machine():
    return RouterModel(
        prompt_tokens=(11, 12),
        maximum_new_tokens=2,
        path_width=3,
        maximum_recovery_attempts=1,
    )


def test_default_generator_is_deterministic_bounded_and_duplicate_free():
    first = generate_bounded_traces(maximum_tail_depth=3)
    second = generate_bounded_traces(maximum_tail_depth=3)

    assert first == second
    assert len(first) == 4_385
    assert len(set(first)) == len(first)
    assert max(map(len, first)) == 4
    assert first[0] == (TraceAction("admit"),)
    assert len(DEFAULT_ACTIONS) == 16


def test_generator_covers_every_required_lifecycle_and_replay_class():
    names = {action.name for trace in generate_bounded_traces() for action in trace}

    assert {
        "admit",
        "duplicate_admit",
        "token_next",
        "token_exact_replay",
        "token_conflicting_replay",
        "token_future_sequence",
        "token_stale_attempt",
        "token_future_attempt",
        "token_non_final",
        "token_off_path",
        "failure_current",
        "failure_stale_sequence",
        "failure_future_attempt",
        "failure_non_owner",
        "failure_off_path",
        "failure_recovery_fails",
        "cancel",
    } <= names


def test_trace_minimizer_returns_deterministic_one_minimal_subsequence():
    trace = tuple(
        TraceAction(name)
        for name in ("noise-a", "admit", "token_next", "cancel", "noise-b")
    )

    def disagrees(candidate):
        names = tuple(action.name for action in candidate)
        required = iter(names)
        return all(item in required for item in ("admit", "token_next", "cancel"))

    first = minimize_trace(trace, disagrees)
    second = minimize_trace(trace, disagrees)

    assert first == second
    assert tuple(action.name for action in first) == (
        "admit",
        "token_next",
        "cancel",
    )
    for index in range(len(first)):
        assert not disagrees(first[:index] + first[index + 1 :])


def test_trace_json_is_stable_and_directly_reproducible():
    trace = (TraceAction("admit"), TraceAction("token_future_sequence"))

    encoded = trace_to_json(trace)

    assert encoded == '[{"name":"admit"},{"name":"token_future_sequence"}]'
    assert json.loads(encoded) == [
        {"name": "admit"},
        {"name": "token_future_sequence"},
    ]


def test_every_bounded_trace_has_deterministic_final_state_and_taxonomy():
    for trace in generate_bounded_traces(maximum_tail_depth=3):
        first = run_reference_trace(machine(), trace)
        second = run_reference_trace(machine(), trace)

        assert first == second
        assert len(first.codes) == len(trace)
        assert len(first.states) == len(trace) + 1


def test_reference_trace_resolves_exact_and_conflicting_replays_distinctly():
    exact = run_reference_trace(
        machine(),
        (
            TraceAction("admit"),
            TraceAction("token_next"),
            TraceAction("token_exact_replay"),
        ),
    )
    conflict = run_reference_trace(
        machine(),
        (
            TraceAction("admit"),
            TraceAction("token_next"),
            TraceAction("token_conflicting_replay"),
        ),
    )

    assert exact.codes[-1] == "idempotent_duplicate"
    assert conflict.codes[-1] == "conflicting_duplicate"
    assert exact.final_state.emitted_tokens == (101,)
    assert conflict.final_state.emitted_tokens == (101,)
