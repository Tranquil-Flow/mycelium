# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the immutable implementation-independent adapter automaton."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import inspect

import pytest

from mycelium_iroh_conformance import (
    AdapterAction,
    IrohAdapterModel,
    REQUIRED_REQUIREMENTS,
    REQUIRED_SCENARIOS,
    TraceAction,
    generate_bounded_traces,
    minimize_trace,
    run_reference_trace,
    state_to_json,
    trace_from_json,
    trace_to_json,
)
from mycelium_iroh_conformance import model as model_module


def _machine() -> IrohAdapterModel:
    return IrohAdapterModel(queue_capacity=2, initial_generation=7, seen_limit=8)


def _run(*names: str):
    return run_reference_trace(_machine(), (AdapterAction(name) for name in names))


def test_reference_model_has_no_production_import_boundary() -> None:
    source = inspect.getsource(model_module)
    assert "mycelium_router" not in source
    assert "mycelium_iroh_sidecar" not in source
    assert "IrohTransport" not in source
    assert "SidecarClient" not in source


def test_state_actions_and_transitions_are_immutable() -> None:
    machine = _machine()
    state = machine.initial_state()
    action = AdapterAction("bind_router")
    transition = machine.apply(state, action)

    with pytest.raises(FrozenInstanceError):
        state.lifecycle = "RUNNING"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        action.name = "close"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        transition.code = "changed"  # type: ignore[misc]


def test_every_rejection_preserves_exact_state_object() -> None:
    machine = _machine()
    state = machine.initial_state()
    for action in (
        AdapterAction("start"),
        AdapterAction("queue_send"),
        AdapterAction("receive_frame"),
        AdapterAction("rotate_peer"),
        AdapterAction("unknown-action"),
    ):
        transition = machine.apply(state, action)
        assert transition.accepted is False
        assert transition.mutated is False
        assert transition.state is state


def test_start_close_restart_is_irreversible_and_idempotent() -> None:
    run = _run("bind_router", "start", "start", "close", "close", "restart")
    assert run.codes == (
        "router_bound",
        "started",
        "already_running",
        "closed",
        "already_closed",
        "transport_closed",
    )
    assert run.final_state.lifecycle == "CLOSED"
    assert run.final_state.closed_client_count == 3
    assert run.final_state.installed_client_roles == ()


def test_queue_exhaustion_and_completion_release_exactly_one_permit() -> None:
    run = _run(
        "bind_router",
        "start",
        "queue_send",
        "queue_send",
        "queue_send",
        "send_confirmed",
        "send_confirmed",
    )
    assert run.codes[4] == "adapter_queue_full"
    assert run.states[4].queue_permits == 0
    assert run.states[5] is run.states[4]
    assert run.final_state.queue_permits == 2
    assert run.final_state.pending_receipt_ids == ()
    assert run.final_state.evidence.sent == 2


def test_deadline_then_late_confirmation_cancels_once_and_releases_once() -> None:
    run = _run(
        "bind_router",
        "start",
        "queue_send",
        "deadline",
        "send_confirmed",
    )
    assert run.codes[-2:] == ("delivery_deadline_exceeded", "delivery_deadline_exceeded")
    assert run.final_state.cancellation_count == 1
    assert run.final_state.queue_permits == 2
    assert run.final_state.evidence.sent == 0


def test_rotation_fences_pending_generation_without_retargeting() -> None:
    run = _run(
        "bind_router",
        "start",
        "queue_send",
        "send_begin",
        "rotate_peer",
        "finish_cancelled",
    )
    assert run.states[4].pending_sends[0].generation == 7
    assert run.states[5].generation == 8
    assert run.states[5].pending_sends[0].cancel_reason == "peer_rotated"
    assert run.final_state.queue_permits == 2
    assert run.final_state.cancellation_count == 1


def test_exact_replay_acks_twice_but_dispatches_once() -> None:
    run = _run(
        "bind_router",
        "start",
        "receive_frame",
        "dispatch",
        "ack",
        "receive_exact_replay",
        "ack",
    )
    state = run.final_state
    assert state.dispatch_count == 1
    assert state.ack_count == 2
    assert state.evidence.received == 1
    assert state.evidence.dispatched == 1
    assert state.evidence.duplicates == 1


def test_same_id_different_payload_is_fatal_without_second_dispatch_or_ack() -> None:
    run = _run(
        "bind_router",
        "start",
        "receive_frame",
        "dispatch",
        "ack",
        "receive_collision",
    )
    state = run.final_state
    assert state.lifecycle == "FATAL"
    assert state.fatal_error == "replay_collision"
    assert state.dispatch_count == 1
    assert state.ack_count == 1


@pytest.mark.parametrize(
    ("action", "fatal"),
    [
        ("receive_stale_sequence", "sequence_gap"),
        ("receive_future_sequence", "sequence_gap"),
        ("receive_stale_generation", "peer_rotated"),
        ("receive_future_generation", "peer_rotated"),
        ("receive_malformed_frame", "malformed_router_frame"),
        ("receive_truncated_frame", "malformed_router_frame"),
    ],
)
def test_invalid_authenticated_ingress_fails_closed(action: str, fatal: str) -> None:
    run = _run("bind_router", "start", action)
    assert run.final_state.lifecycle == "FATAL"
    assert run.final_state.fatal_error == fatal
    assert run.final_state.ack_count == 0
    assert run.final_state.dispatch_count == 0


def test_named_scenario_catalog_covers_every_required_requirement() -> None:
    assert {scenario.requirement for scenario in REQUIRED_SCENARIOS} == REQUIRED_REQUIREMENTS
    assert len({scenario.name for scenario in REQUIRED_SCENARIOS}) == len(REQUIRED_SCENARIOS)
    for scenario in REQUIRED_SCENARIOS:
        assert scenario.actions
        run = run_reference_trace(_machine(), scenario.actions)
        assert len(run.states) == len(scenario.actions) + 1


def test_bounded_generation_is_stable_complete_and_duplicate_free() -> None:
    alphabet = tuple(TraceAction(name) for name in ("close", "queue_send", "rotate_peer"))
    first = generate_bounded_traces(maximum_tail_depth=2, actions=alphabet)
    second = generate_bounded_traces(maximum_tail_depth=2, actions=alphabet)
    assert first == second
    assert len(first) == 16
    assert len(set(first)) == len(first)
    assert first[0] == (TraceAction("bind_router"), TraceAction("start"))
    assert first[-3:] == tuple((action,) for action in alphabet)


def test_counterexample_json_is_canonical_strict_and_round_trips() -> None:
    trace = (
        AdapterAction("queue_send", message_id="send-a", generation=7),
        AdapterAction("rotate_peer", generation=8),
    )
    encoded = trace_to_json(trace)
    assert encoded == (
        '[{"generation":7,"message_id":"send-a","name":"queue_send"},'
        '{"generation":8,"name":"rotate_peer"}]'
    )
    assert trace_from_json(encoded) == trace
    assert state_to_json(_machine().initial_state()).startswith('{"ack_count":0,')

    with pytest.raises(ValueError, match="unknown_fields"):
        trace_from_json('[{"name":"start","surprise":true}]')


def test_deletion_minimizer_is_deterministic_and_deletion_one_minimal() -> None:
    trace = tuple(AdapterAction(name) for name in ("noise-a", "bind_router", "noise-b", "start"))

    def reproduces(candidate: tuple[AdapterAction, ...]) -> bool:
        names = tuple(action.name for action in candidate)
        return "bind_router" in names and "start" in names

    minimal = minimize_trace(trace, reproduces)
    assert minimal == (AdapterAction("bind_router"), AdapterAction("start"))
    assert reproduces(minimal)
    assert all(
        not reproduces(minimal[:index] + minimal[index + 1 :])
        for index in range(len(minimal))
    )


def test_minimizer_rejects_non_reproducing_input() -> None:
    with pytest.raises(ValueError, match="trace_does_not_reproduce"):
        minimize_trace((AdapterAction("start"),), lambda _trace: False)
