from dataclasses import replace

import pytest

from mycelium_request_conformance.model import (
    Action,
    Authority,
    GatewayModel,
    Phase,
)


CURRENT = Authority(
    deployment="deploy-a",
    epoch=7,
    path="path-a",
    evidence="evidence-a",
    qualification="qualification-a",
    ready=True,
)


def _admitted(*, buffer_capacity: int = 4):
    model = GatewayModel(current=CURRENT, buffer_capacity=buffer_capacity)
    admitted = model.apply(Action.admit(CURRENT, payload="prompt-a"))
    assert admitted.code == "admitted"
    started = model.apply(Action.start(), state=admitted.state)
    assert started.code == "started"
    return model, started.state


def test_admission_requires_exact_current_qualification():
    model = GatewayModel(current=CURRENT)

    accepted = model.apply(Action.admit(CURRENT, payload="prompt-a"))
    assert accepted.code == "admitted"
    assert accepted.state.phase is Phase.ADMITTED
    assert accepted.state.counters.runtime_starts == 0
    started = model.apply(Action.start(), state=accepted.state)
    assert started.state.phase is Phase.STREAMING
    assert started.state.counters.runtime_starts == 1
    assert started.state.counters.capacity_acquires == 1
    assert started.state.counters.kv_acquires == 1

    variants = (
        (replace(CURRENT, deployment="deploy-old"), "deployment_changed"),
        (replace(CURRENT, epoch=6), "epoch_changed"),
        (replace(CURRENT, path="path-old"), "path_changed"),
        (replace(CURRENT, evidence="evidence-old"), "evidence_changed"),
        (replace(CURRENT, qualification="qualification-old"), "qualification_changed"),
        (replace(CURRENT, ready=False), "route_not_ready"),
    )
    for stale, expected_code in variants:
        rejected = GatewayModel(current=CURRENT).apply(
            Action.admit(stale, payload="prompt-a")
        )
        assert rejected.code == expected_code
        assert rejected.state.phase is Phase.NEW
        assert rejected.state.counters.runtime_starts == 0
        assert rejected.state.events == ()


def test_continuous_revalidation_covers_all_authority_dimensions():
    variants = (
        ("deployment", "deploy-b", "deployment_changed"),
        ("epoch", 8, "epoch_changed"),
        ("path", "path-b", "path_changed"),
        ("evidence", "evidence-b", "evidence_changed"),
        ("qualification", "qualification-b", "qualification_changed"),
        ("ready", False, "route_not_ready"),
    )
    for field, value, expected_code in variants:
        model, state = _admitted()
        changed = model.apply(Action.change_authority(field, value), state=state)
        terminal = model.apply(Action.complete(), state=changed.state)

        assert terminal.code == expected_code
        assert terminal.state.phase is Phase.FAILED
        assert terminal.state.terminal_count == 1
        assert terminal.state.counters.capacity_releases == 1
        assert terminal.state.counters.kv_cleanups == 1


def test_tokens_are_ordered_and_exact_token_replay_is_side_effect_free():
    model, state = _admitted()
    first = model.apply(Action.token(0, "alpha"), state=state)
    before = first.state

    replay = model.apply(Action.token(0, "alpha"), state=before)

    assert replay.code == "exact_token_replay"
    assert replay.state == before
    assert replay.state.counters.token_events == 1
    assert [event.text for event in replay.state.events if event.kind == "token"] == [
        "alpha"
    ]


@pytest.mark.parametrize(
    "action, code",
    (
        (Action.token(0, "different"), "token_order_violation"),
        (Action.token(2, "future"), "token_order_violation"),
    ),
)
def test_invalid_token_transition_fails_once_without_duplicate_token_event(action, code):
    model, state = _admitted()
    first = model.apply(Action.token(0, "alpha"), state=state)

    failed = model.apply(action, state=first.state)
    repeated = model.apply(Action.complete(), state=failed.state)

    assert failed.code == code
    assert failed.state.phase is Phase.FAILED
    assert failed.state.terminal_count == 1
    assert failed.state.counters.token_events == 1
    assert failed.state.counters.failures == 1
    assert failed.state.counters.capacity_releases == 1
    assert failed.state.counters.kv_cleanups == 1
    assert repeated.state == failed.state
    assert repeated.code == "already_terminal"


def test_exact_request_replay_is_idempotent_and_conflict_precedes_side_effects():
    model, state = _admitted()
    before = state

    exact = model.apply(Action.admit(CURRENT, payload="prompt-a"), state=state)
    conflict = model.apply(Action.admit(CURRENT, payload="prompt-b"), state=state)

    assert exact.code == "exact_request_replay"
    assert exact.state == before
    assert conflict.code == "conflicting_request_replay"
    assert conflict.state == before


def test_cancel_disconnect_reconnect_and_terminal_cleanup_are_idempotent():
    model, state = _admitted()
    connected = model.apply(Action.reconnect(-1), state=state)
    disconnected = model.apply(Action.disconnect(), state=connected.state)
    reconnected = model.apply(Action.reconnect(-1), state=disconnected.state)
    cancelled = model.apply(Action.cancel(), state=reconnected.state)
    again = model.apply(Action.cancel(), state=cancelled.state)

    assert connected.state.attached is True
    assert disconnected.state.attached is False
    assert disconnected.state.phase is Phase.STREAMING
    assert reconnected.state.attached is True
    assert cancelled.state.phase is Phase.CANCELLED
    assert cancelled.state.terminal_count == 1
    assert cancelled.state.counters.backend_cancels == 1
    assert cancelled.state.counters.capacity_releases == 1
    assert cancelled.state.counters.kv_cleanups == 1
    assert again.code == "already_terminal"
    assert again.state == cancelled.state


def test_bounded_buffer_reserves_terminal_slot_and_ack_releases_backpressure():
    model, state = _admitted(buffer_capacity=3)
    first = model.apply(Action.token(0, "alpha"), state=state)
    blocked = model.apply(Action.token(1, "beta"), state=first.state)

    assert blocked.code == "backpressured"
    assert blocked.state == first.state
    assert blocked.state.counters.maximum_buffered == 2

    connected = model.apply(Action.reconnect(-1), state=blocked.state)
    delivered_accepted = model.apply(Action.next_event(), state=connected.state)
    acked_accepted = model.apply(Action.ack(0), state=delivered_accepted.state)
    delivered_token = model.apply(Action.next_event(), state=acked_accepted.state)
    acknowledged = model.apply(Action.ack(1), state=delivered_token.state)
    second = model.apply(Action.token(1, "beta"), state=acknowledged.state)
    completed = model.apply(Action.complete(), state=second.state)

    assert second.code == "token_accepted"
    assert completed.state.phase is Phase.COMPLETED
    assert completed.state.terminal_count == 1
    assert len(completed.state.events) <= 3
    assert completed.state.counters.maximum_buffered <= 3


def test_expired_replay_cursor_fails_without_mutation():
    model, state = _admitted(buffer_capacity=3)
    first = model.apply(Action.token(0, "alpha"), state=state)
    connected = model.apply(Action.reconnect(-1), state=first.state)
    delivered_accepted = model.apply(Action.next_event(), state=connected.state)
    acked_accepted = model.apply(Action.ack(0), state=delivered_accepted.state)
    delivered_token = model.apply(Action.next_event(), state=acked_accepted.state)
    acknowledged = model.apply(Action.ack(1), state=delivered_token.state)
    disconnected = model.apply(Action.disconnect(), state=acknowledged.state)

    expired = model.apply(Action.reconnect(-1), state=disconnected.state)

    assert expired.code == "resume_cursor_expired"
    assert expired.state == disconnected.state


def test_cancel_before_start_has_no_runtime_capacity_or_cleanup_effects():
    model = GatewayModel(current=CURRENT)
    admitted = model.apply(Action.admit(CURRENT, payload="prompt-a"))
    cancelled = model.apply(Action.cancel(), state=admitted.state)
    late_start = model.apply(Action.start(), state=cancelled.state)

    assert cancelled.state.phase is Phase.CANCELLED
    assert cancelled.state.counters.runtime_starts == 0
    assert cancelled.state.counters.backend_cancels == 0
    assert cancelled.state.counters.capacity_acquires == 0
    assert cancelled.state.counters.capacity_releases == 0
    assert cancelled.state.counters.kv_acquires == 0
    assert cancelled.state.counters.kv_cleanups == 0
    assert late_start.state == cancelled.state


def test_non_utf8_token_text_fails_as_invalid_backend_token():
    model, state = _admitted()

    failed = model.apply(Action.token(0, "\ud800"), state=state)

    assert failed.code == "invalid_backend_token"
    assert failed.state.phase is Phase.FAILED
    assert failed.state.counters.token_events == 0
    assert failed.state.counters.failures == 1
    assert failed.state.counters.capacity_releases == 1
    assert failed.state.counters.kv_cleanups == 1


def test_errors_and_events_never_echo_payload_or_token_text_in_codes():
    model = GatewayModel(current=CURRENT)
    rejected = model.apply(
        Action.admit(replace(CURRENT, evidence="private-canary"), payload="secret-prompt")
    )
    _, state = _admitted()
    conflict = model.apply(Action.token(0, "secret-token"), state=state)
    conflict = model.apply(Action.token(0, "different-secret"), state=conflict.state)

    assert "private-canary" not in rejected.code
    assert "secret-prompt" not in rejected.code
    assert "secret-token" not in conflict.code
    assert "different-secret" not in conflict.code
