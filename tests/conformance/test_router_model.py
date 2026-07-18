"""TDD specification for the independent Router lifecycle automaton."""

from dataclasses import replace

import pytest

from mycelium_conformance.router_model import ModelEvent, RouterModel


def model() -> RouterModel:
    return RouterModel(
        prompt_tokens=(11, 12),
        maximum_new_tokens=2,
        path_width=3,
        maximum_recovery_attempts=1,
    )


def apply(state, machine, event):
    result = machine.apply(state, event)
    return result.state, result


def ready(machine: RouterModel):
    state = machine.initial_state()
    expected = (
        (ModelEvent("ADMIT"), "ADMITTING"),
        (ModelEvent("BEGIN_PREFILL", path_id="path-0", path_attempt=0), "PREFILL"),
        (ModelEvent("LOCK", path_id="path-0", path_attempt=0), "LOCKED"),
        (
            ModelEvent("PREFILL_COMPLETE", path_id="path-0", path_attempt=0),
            "DECODING",
        ),
    )
    for event, phase in expected:
        state, result = apply(state, machine, event)
        assert result.accepted
        assert state.phase == phase
    return state


def token(
    *,
    sequence=0,
    token_id=101,
    path_attempt=0,
    path_id="path-0",
    peer="final",
):
    return ModelEvent(
        "TOKEN",
        path_id=path_id,
        path_attempt=path_attempt,
        sequence=sequence,
        peer=peer,
        payload=(token_id,),
    )


def failure(
    *,
    sequence=0,
    path_attempt=0,
    path_id="path-0",
    peer="path",
    recovery="success",
):
    return ModelEvent(
        "FAILURE",
        path_id=path_id,
        path_attempt=path_attempt,
        sequence=sequence,
        peer=peer,
        payload=(recovery,),
    )


def test_documented_admission_prefill_lock_decode_order():
    machine = model()
    state = ready(machine)

    assert state.phase == "DECODING"
    assert state.path_attempt == 0
    assert state.reservations == 3
    assert state.next_sequence == 0


@pytest.mark.parametrize(
    ("phase_builder", "event"),
    [
        (lambda machine: machine.initial_state(), token()),
        (
            lambda machine: apply(
                machine.initial_state(), machine, ModelEvent("ADMIT")
            )[0],
            token(),
        ),
        (
            lambda machine: apply(
                apply(machine.initial_state(), machine, ModelEvent("ADMIT"))[0],
                machine,
                ModelEvent("BEGIN_PREFILL", path_id="path-0", path_attempt=0),
            )[0],
            token(),
        ),
    ],
)
def test_invalid_transition_rejects_without_mutating_state(phase_builder, event):
    machine = model()
    before = phase_builder(machine)

    result = machine.apply(before, event)

    assert not result.accepted
    assert not result.mutated
    assert result.code in {"unknown_request", "invalid_phase"}
    assert result.state is before


def test_path_attempt_advances_once_and_rejects_skip_or_regression():
    machine = model()
    state = ready(machine)

    skipped = machine.apply(
        state,
        failure(path_id="path-2", path_attempt=2),
    )
    assert skipped.code == "future_path_attempt"
    assert skipped.state is state

    recovered = machine.apply(state, failure())
    assert recovered.accepted
    assert recovered.state.path_attempt == 1
    assert recovered.state.path_id == "path-1"
    assert recovered.state.recovery_count == 1

    regressed = machine.apply(recovered.state, failure())
    assert regressed.code == "stale_path_attempt"
    assert regressed.state is recovered.state


def test_exact_token_duplicate_is_idempotent_but_conflict_fails_closed():
    machine = model()
    state = ready(machine)
    event = token()

    accepted = machine.apply(state, event)
    duplicate = machine.apply(accepted.state, event)
    conflict = machine.apply(accepted.state, replace(event, payload=(999,)))

    assert accepted.accepted
    assert duplicate.code == "idempotent_duplicate"
    assert duplicate.state is accepted.state
    assert conflict.code == "conflicting_duplicate"
    assert conflict.state is accepted.state
    assert accepted.state.emitted_tokens == (101,)


def test_stale_old_attempt_events_cannot_emit_complete_mutate_or_recover():
    machine = model()
    recovered = machine.apply(ready(machine), failure()).state
    before = recovered

    stale_token = machine.apply(before, token(path_attempt=0, path_id="path-0"))
    stale_failure = machine.apply(before, failure(path_attempt=0, path_id="path-0"))

    assert stale_token.code == "stale_path_attempt"
    assert stale_failure.code == "stale_path_attempt"
    assert stale_token.state is before
    assert stale_failure.state is before
    assert before.emitted_tokens == ()
    assert before.terminal_count == 0
    assert before.recovery_count == 1


def test_future_sequence_path_and_attempt_events_cannot_advance():
    machine = model()
    state = ready(machine)

    cases = (
        (token(sequence=1), "future_sequence"),
        (token(path_id="path-future"), "path_mismatch"),
        (token(path_id="path-1", path_attempt=1), "future_path_attempt"),
    )
    for event, code in cases:
        result = machine.apply(state, event)
        assert result.code == code
        assert result.state is state


def test_terminal_completion_cancel_and_failure_happen_exactly_once():
    machine = model()

    completed = machine.apply(ready(machine), token()).state
    completed = machine.apply(completed, token(sequence=1, token_id=102)).state
    after_completion = machine.apply(completed, ModelEvent("CANCEL"))
    assert completed.phase == "COMPLETED"
    assert completed.terminal_count == 1
    assert completed.release_count == 1
    assert completed.reservations == 0
    assert after_completion.code == "terminal_state"
    assert after_completion.state is completed

    cancelled = machine.apply(ready(machine), ModelEvent("CANCEL"))
    duplicate_cancel = machine.apply(cancelled.state, ModelEvent("CANCEL"))
    assert cancelled.state.phase == "CANCELLED"
    assert cancelled.state.terminal_count == 1
    assert cancelled.state.release_count == 1
    assert duplicate_cancel.code == "idempotent_duplicate"
    assert duplicate_cancel.state is cancelled.state

    failed = machine.apply(ready(machine), failure(recovery="failure"))
    replay_failure = machine.apply(failed.state, failure(recovery="failure"))
    assert failed.state.phase == "FAILED"
    assert failed.state.terminal_count == 1
    assert failed.state.release_count == 2
    assert failed.state.reservations == 0
    assert replay_failure.code == "terminal_state"
    assert replay_failure.state is failed.state


def test_non_final_and_off_path_peers_cannot_mutate_request():
    machine = model()
    state = ready(machine)

    for event, code in (
        (token(peer="non_final"), "non_final_peer"),
        (token(peer="off_path"), "off_path_peer"),
        (failure(peer="non_owner"), "non_owner_peer"),
        (failure(peer="off_path"), "off_path_peer"),
    ):
        result = machine.apply(state, event)
        assert result.code == code
        assert result.state is state


def test_recovery_preserves_prompt_plus_committed_tokens_not_kv_continuity():
    machine = model()
    with_token = machine.apply(ready(machine), token()).state

    recovered = machine.apply(
        with_token,
        failure(sequence=1),
    )

    assert recovered.accepted
    assert recovered.state.phase == "DECODING"
    assert recovered.state.path_attempt == 1
    assert recovered.state.preserved_context == (11, 12, 101)
    assert recovered.state.recovery_uses_transferred_kv is False
    assert recovered.state.release_count == 1
    assert recovered.state.reservations == 3


def test_recovery_resource_release_then_terminal_cleanup_is_exact():
    machine = model()
    recovered = machine.apply(ready(machine), failure()).state
    cancelled = machine.apply(recovered, ModelEvent("CANCEL"))

    assert recovered.release_count == 1
    assert recovered.reservations == 3
    assert cancelled.state.release_count == 2
    assert cancelled.state.reservations == 0


def test_hop_replay_model_accepts_exact_duplicate_and_rejects_payload_conflict():
    machine = model()
    state = ready(machine)
    hop = ModelEvent(
        "HOP",
        path_id="path-0",
        path_attempt=0,
        sequence=0,
        peer="entry",
        hop_index=0,
        payload=(1, 2),
    )

    first = machine.apply(state, hop)
    duplicate = machine.apply(first.state, hop)
    conflict = machine.apply(first.state, replace(hop, payload=(9, 9)))

    assert first.accepted
    assert first.state.hop_executions == 1
    assert duplicate.code == "idempotent_duplicate"
    assert duplicate.state is first.state
    assert conflict.code == "conflicting_duplicate"
    assert conflict.state is first.state


def test_progressive_prefill_replay_uses_same_exact_identity_rule():
    machine = model()
    state = machine.apply(machine.initial_state(), ModelEvent("ADMIT")).state
    state = machine.apply(
        state,
        ModelEvent("BEGIN_PREFILL", path_id="path-0", path_attempt=0),
    ).state
    delivery = ModelEvent(
        "PREFILL_HOP",
        path_id="path-0",
        path_attempt=0,
        sequence=-1,
        peer="entry",
        hop_index=0,
        payload=(1, 2),
    )

    first = machine.apply(state, delivery)
    duplicate = machine.apply(first.state, delivery)
    conflict = machine.apply(first.state, replace(delivery, payload=(9, 9)))

    assert first.accepted
    assert duplicate.code == "idempotent_duplicate"
    assert duplicate.state is first.state
    assert conflict.code == "conflicting_duplicate"
    assert conflict.state is first.state


def test_pending_hop_replay_distinguishes_exact_and_conflicting_payloads():
    machine = model()
    state = ready(machine)
    queued = ModelEvent(
        "ENQUEUE_HOP",
        path_id="path-0",
        path_attempt=0,
        sequence=0,
        peer="entry",
        hop_index=0,
        payload=(1, 2),
    )

    first = machine.apply(state, queued)
    duplicate = machine.apply(first.state, queued)
    conflict = machine.apply(first.state, replace(queued, payload=(9, 9)))

    assert first.accepted
    assert first.state.hop_executions == 0
    assert duplicate.code == "idempotent_duplicate"
    assert duplicate.state is first.state
    assert conflict.code == "conflicting_duplicate"
    assert conflict.state is first.state
