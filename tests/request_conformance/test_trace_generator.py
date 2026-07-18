import json

from mycelium_request_conformance.model import Action, Authority, GatewayModel, Phase
from mycelium_request_conformance.trace import (
    RACE_ACTIONS,
    TAIL_ACTIONS,
    generate_bounded_traces,
    generate_race_traces,
    minimize_trace,
    run_trace,
    trace_to_json,
)


CURRENT = Authority(
    deployment="deploy-a",
    epoch=7,
    path="path-a",
    evidence="evidence-a",
    qualification="qualification-a",
    ready=True,
)


def test_bounded_generation_is_exhaustive_for_declared_alphabet_and_deterministic():
    first = generate_bounded_traces(CURRENT, maximum_tail_depth=2)
    second = generate_bounded_traces(CURRENT, maximum_tail_depth=2)

    assert len(TAIL_ACTIONS) == 12
    assert len(first) == 1 + 12 + 12**2 + 12
    assert first == second
    assert len({trace_to_json(trace) for trace in first}) == len(first)
    assert max(len(trace) for trace in first) == 3


def test_race_generation_enumerates_every_ordering_once():
    traces = generate_race_traces(CURRENT)

    assert len(RACE_ACTIONS) == 5
    assert len(traces) == 120
    assert len({trace_to_json(trace) for trace in traces}) == 120
    assert all(len(trace) == 6 for trace in traces)


def test_reference_replay_is_byte_deterministic_including_side_effect_counters():
    for trace in (*generate_bounded_traces(CURRENT), *generate_race_traces(CURRENT)):
        first = run_trace(GatewayModel(current=CURRENT), trace)
        second = run_trace(GatewayModel(current=CURRENT), trace)
        assert first == second


def test_trace_json_has_stable_schema_and_never_serializes_payload_text():
    trace = (
        Action.admit(CURRENT, payload="private-prompt"),
        Action.token(0, "private-token"),
        Action.complete(),
    )

    encoded = trace_to_json(trace)
    document = json.loads(encoded)

    assert [item["kind"] for item in document] == ["admit", "token", "complete"]
    assert "private-prompt" not in encoded
    assert "private-token" not in encoded


def test_minimizer_returns_deletion_one_minimal_counterexample_with_counters():
    trace = (
        Action.disconnect(),
        Action.admit(CURRENT, payload="private-prompt"),
        Action.token(0, "alpha"),
        Action.token(0, "different"),
        Action.change_authority("epoch", 8),
    )

    def failure(candidate):
        result = run_trace(GatewayModel(current=CURRENT), candidate)
        state = result.state
        return (
            state.phase is Phase.FAILED
            and state.outcome == "conflicting_token_replay"
            and state.counters.token_events == 1
            and state.counters.failures == 1
            and state.counters.capacity_releases == 1
            and state.counters.kv_cleanups == 1
        )

    minimized = minimize_trace(trace, failure)

    assert [action.kind for action in minimized] == ["admit", "token", "token"]
    assert failure(minimized)
    for index in range(len(minimized)):
        assert not failure(minimized[:index] + minimized[index + 1 :])
