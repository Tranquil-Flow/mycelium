// SPDX-License-Identifier: AGPL-3.0-only
//! Characterization of the shipped sidecar scheduler/history
//! (`sidecar.rs @ f4e366e9`). Ported oracle unit tests are named after their
//! source tests; the numeric values are the shipped constants used as explicit
//! vectors (sidecar.rs:54-106,1715), not defaults of the module under test.
use budgets::{ClockDomain, MonoInstant, Span};
use runtime::{
    iroh_wire::SendAttempt,
    sidecar_pool::{
        AdmissionOutcome, AttemptResult, Backoff, ConfigureRefusal, EnqueueOutcome, EnqueueRequest,
        InboundTerminal, OutboundRun, OutboundTerminal, PeerBinding, PoolLimits, PoolRegistration,
        SidecarState, Step,
    },
};

fn ms(value: u64) -> Span {
    Span::from_millis(value)
}
fn at(value: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(9), value)
}
fn limits(capacity: usize) -> PoolLimits {
    PoolLimits {
        capacity,
        seen_factor: 8,
        seen_floor: 64,
        initial_retry: ms(100),
        max_retry: ms(5_000),
        lifetime: ms(300_000),
    }
}
fn peer(seed: u8, generation: u64) -> PeerBinding {
    PeerBinding {
        endpoint: [seed; 32],
        generation,
    }
}
fn request(id: u8, digest: u8) -> EnqueueRequest {
    EnqueueRequest {
        message_id: [id; 16],
        digest: [digest; 32],
        payload: vec![id, digest],
        expected_generation: None,
        source_generation: None,
        destination: None,
    }
}
fn configured(capacity: usize) -> SidecarState {
    let mut state = SidecarState::new(limits(capacity));
    state.configure_peers(vec![peer(1, 1)]).unwrap();
    state
}
fn queued(state: &mut SidecarState, id: u8, now: u64) -> runtime::sidecar_pool::OutboundHandle {
    match state.enqueue(request(id, id), at(now)) {
        EnqueueOutcome::Queued(handle) => handle,
        other => panic!("expected Queued, got {other:?}"),
    }
}

// ---------------------------------------------------------------- backoff
#[test]
fn retry_delay_starts_at_initial_doubles_saturating_and_caps() {
    let mut backoff = Backoff::new(&limits(1));
    let waits: Vec<u64> = (0..9).map(|_| backoff.next_wait().millis()).collect();
    assert_eq!(
        waits,
        [100, 200, 400, 800, 1_600, 3_200, 5_000, 5_000, 5_000]
    );
}
#[test]
fn first_retry_delay_is_not_clamped_but_later_ones_are() {
    // The source waits `retry_delay` first and clamps only the doubled value.
    let mut tight = limits(1);
    tight.initial_retry = ms(900);
    tight.max_retry = ms(500);
    let mut backoff = Backoff::new(&tight);
    assert_eq!(backoff.next_wait().millis(), 900);
    assert_eq!(backoff.next_wait().millis(), 500);
    let mut huge = limits(1);
    huge.initial_retry = ms(u64::MAX);
    huge.max_retry = ms(u64::MAX);
    let mut backoff = Backoff::new(&huge);
    assert_eq!(backoff.next_wait().millis(), u64::MAX);
    assert_eq!(backoff.next_wait().millis(), u64::MAX);
}
#[test]
fn seen_limit_is_capacity_times_factor_with_a_floor() {
    assert_eq!(limits(1).seen_limit(), 64);
    assert_eq!(limits(8).seen_limit(), 64);
    assert_eq!(limits(9).seen_limit(), 72);
    assert_eq!(limits(usize::MAX).seen_limit(), usize::MAX);
}

// ------------------------------------------------- outbound scheduling
fn worker_run(state: &mut SidecarState) -> OutboundRun {
    let item = state.next_outbound().expect("queued item");
    OutboundRun::start(state, item).expect("control exists")
}
#[test]
fn retry_waits_double_and_never_renew_the_enqueue_time_lifetime() {
    let mut state = configured(2);
    let handle = queued(&mut state, 1, 1_000);
    let mut run = worker_run(&mut state);
    let mut now = 1_000;
    let mut waits = Vec::new();
    for _ in 0..9 {
        let Step::Attempt(binding) = run.advance(&mut state, at(now)) else {
            panic!("attempt expected")
        };
        assert_eq!(binding, peer(1, 1));
        // A valid-but-not-ack response (queue_full etc.) retries WITHOUT eviction.
        let Step::Backoff { wake_at } = run.attempted(
            &mut state,
            at(now),
            AttemptResult::Retry {
                evict_connection: false,
            },
        ) else {
            panic!("backoff expected")
        };
        waits.push(wake_at.ticks() - now);
        now = wake_at.ticks();
    }
    assert_eq!(
        waits,
        [100, 200, 400, 800, 1_600, 3_200, 5_000, 5_000, 5_000]
    );
    // The deadline is the ENQUEUE one (1_000 + 300_000), not renewed per retry.
    let before = at(1_000 + 300_000 - 1);
    assert!(matches!(run.advance(&mut state, before), Step::Attempt(_)));
    // One millisecond before the enqueue-time deadline the wait is clipped to it.
    assert_eq!(
        run.attempted(
            &mut state,
            before,
            AttemptResult::Retry {
                evict_connection: false,
            },
        ),
        Step::Backoff {
            wake_at: at(1_000 + 300_000)
        }
    );
    assert_eq!(
        run.advance(&mut state, at(1_000 + 300_000)),
        Step::Finish(OutboundTerminal::Cancelled)
    );
    assert_eq!(state.terminal(handle), Some(OutboundTerminal::Cancelled));
}
#[test]
fn a_wait_never_extends_past_the_item_deadline() {
    let mut state = configured(1);
    queued(&mut state, 1, 0);
    let mut run = worker_run(&mut state);
    let near = 300_000 - 50;
    assert!(matches!(
        run.advance(&mut state, at(near)),
        Step::Attempt(_)
    ));
    assert_eq!(
        run.attempted(&mut state, at(near), AttemptResult::io_failure()),
        Step::Backoff {
            wake_at: at(300_000)
        }
    );
}
#[test]
fn a_send_whose_cancellation_is_lost_still_ends_at_the_deadline() {
    // Oracle: a_send_whose_cancellation_is_lost_still_ends_at_the_deadline.
    let mut state = configured(1);
    let handle = queued(&mut state, 5, 0);
    let mut run = worker_run(&mut state);
    assert!(matches!(run.advance(&mut state, at(0)), Step::Attempt(_)));
    // The outer timeout_at(deadline, attempt) elapses inside the attempt.
    assert_eq!(
        run.attempt_deadline_elapsed(&mut state),
        Step::Finish(OutboundTerminal::Cancelled)
    );
    assert_eq!(state.terminal(handle), Some(OutboundTerminal::Cancelled));
    // Slot released and the id forgotten.
    assert_eq!(state.outbound_free(), 1);
    assert!(!state.cancel([5; 16]));
}
#[test]
fn cancellation_is_explicit_and_releases_capacity_before_worker_traversal() {
    // Oracle: cancellation_is_explicit_and_releases_capacity_before_worker_traversal.
    let mut state = configured(1);
    assert!(!state.cancel([9; 16]));
    let handle = queued(&mut state, 1, 0);
    assert_eq!(state.outbound_free(), 0);
    assert!(state.cancel([1; 16]));
    assert!(!state.cancel([1; 16]));
    assert_eq!(state.outbound_free(), 1);
    assert_eq!(state.terminal(handle), Some(OutboundTerminal::Cancelled));
    assert!(
        state.next_outbound().is_none(),
        "cancel removed the exact queued item"
    );
    assert!(matches!(
        state.enqueue(request(2, 2), at(0)),
        EnqueueOutcome::Queued(_)
    ));
}
#[test]
fn terminal_is_first_wins_in_both_orders() {
    // Cancel first, a late Delivered cannot overwrite it.
    let mut state = configured(2);
    let first = queued(&mut state, 1, 0);
    let mut run = worker_run(&mut state);
    assert!(matches!(run.advance(&mut state, at(0)), Step::Attempt(_)));
    assert!(state.cancel([1; 16]));
    assert_eq!(
        run.attempted(&mut state, at(1), AttemptResult::Delivered),
        Step::Finish(OutboundTerminal::Cancelled)
    );
    assert_eq!(state.terminal(first), Some(OutboundTerminal::Cancelled));
    // Delivered first, a later cancel finds no token and changes nothing.
    let second = queued(&mut state, 2, 0);
    let mut run = worker_run(&mut state);
    assert!(matches!(run.advance(&mut state, at(0)), Step::Attempt(_)));
    assert_eq!(
        run.attempted(&mut state, at(1), AttemptResult::Delivered),
        Step::Finish(OutboundTerminal::Delivered)
    );
    assert!(!state.cancel([2; 16]));
    assert_eq!(state.terminal(second), Some(OutboundTerminal::Delivered));
    assert_eq!(state.outbound_free(), 2);
}
#[test]
fn cancel_of_an_item_in_flight_ends_it_without_evicting_the_shared_connection() {
    let mut state = configured(2);
    let key = ([1; 32], 1);
    assert_eq!(state.pool_register(key), PoolRegistration::Inserted);
    queued(&mut state, 1, 0);
    queued(&mut state, 2, 0);
    let mut cancelled = worker_run(&mut state);
    let mut sibling = worker_run(&mut state);
    assert!(matches!(
        cancelled.advance(&mut state, at(0)),
        Step::Attempt(_)
    ));
    assert!(matches!(
        sibling.advance(&mut state, at(0)),
        Step::Attempt(_)
    ));
    assert!(state.cancel([1; 16]));
    // Write/read cancellation resets only this stream (code 7); the pooled
    // connection stays, and the sibling is undisturbed.
    assert_eq!(
        cancelled.attempted(&mut state, at(1), AttemptResult::Cancelled),
        Step::Finish(OutboundTerminal::Cancelled)
    );
    assert!(state.pool_contains(key));
    assert_eq!(
        sibling.attempted(&mut state, at(1), AttemptResult::Delivered),
        Step::Finish(OutboundTerminal::Delivered)
    );
    assert!(state.pool_contains(key));
    assert_eq!(runtime::sidecar_pool::CANCEL_CODE, 7);
}
#[test]
fn io_failures_evict_the_pool_entry_but_valid_retry_responses_keep_it() {
    let mut state = configured(1);
    let key = ([1; 32], 1);
    state.pool_register(key);
    queued(&mut state, 1, 0);
    let mut run = worker_run(&mut state);
    assert!(matches!(run.advance(&mut state, at(0)), Step::Attempt(_)));
    // queue_full style response: retry, connection retained.
    run.attempted(
        &mut state,
        at(0),
        AttemptResult::Retry {
            evict_connection: false,
        },
    );
    assert!(state.pool_contains(key));
    assert!(matches!(run.advance(&mut state, at(100)), Step::Attempt(_)));
    // connect/open/write/FIN/read/decode/wrong-ID failure: evict then retry.
    assert!(matches!(
        run.attempted(&mut state, at(100), AttemptResult::io_failure()),
        Step::Backoff { .. }
    ));
    assert!(!state.pool_contains(key));
    assert_eq!(runtime::sidecar_pool::REJECT_CODE, 8);
}
#[test]
fn wire_classification_drives_eviction_and_terminals() {
    // The response classifier stays in iroh_wire; this binds it to the scheduler.
    assert_eq!(
        AttemptResult::from(SendAttempt::Retry {
            evict_connection: true
        }),
        AttemptResult::io_failure()
    );
    assert_eq!(
        AttemptResult::from(SendAttempt::PeerRotated),
        AttemptResult::PeerRotated
    );
    assert_eq!(
        AttemptResult::from(SendAttempt::ReplayCollision),
        AttemptResult::ReplayCollision
    );
    let mut state = configured(2);
    queued(&mut state, 1, 0);
    let mut run = worker_run(&mut state);
    assert!(matches!(run.advance(&mut state, at(0)), Step::Attempt(_)));
    assert_eq!(
        run.attempted(&mut state, at(0), SendAttempt::ReplayCollision.into()),
        Step::Finish(OutboundTerminal::ReplayCollision)
    );
    queued(&mut state, 2, 0);
    let mut run = worker_run(&mut state);
    assert!(matches!(run.advance(&mut state, at(0)), Step::Attempt(_)));
    assert_eq!(
        run.attempted(&mut state, at(0), SendAttempt::PeerRotated.into()),
        Step::Finish(OutboundTerminal::PeerRotated)
    );
}
#[test]
fn pool_keeps_one_connection_per_endpoint_and_generation() {
    let mut state = configured(1);
    assert_eq!(
        state.pool_register(([1; 32], 1)),
        PoolRegistration::Inserted
    );
    assert_eq!(
        state.pool_register(([1; 32], 1)),
        PoolRegistration::Duplicate
    );
    assert_eq!(
        state.pool_register(([1; 32], 2)),
        PoolRegistration::Inserted
    );
    assert_eq!(
        state.pool_register(([2; 32], 1)),
        PoolRegistration::Inserted
    );
    assert!(state.pool_evict(([1; 32], 1)));
    assert!(!state.pool_evict(([1; 32], 1)));
    assert_eq!(runtime::sidecar_pool::REASON_DUPLICATE, b"duplicate");
    assert_eq!(runtime::sidecar_pool::REASON_RETRY, b"retry");
}
#[test]
fn a_missing_primary_binding_waits_on_the_initial_tick_and_binds_later() {
    let mut state = SidecarState::new(limits(1));
    assert!(matches!(
        state.enqueue(request(1, 1), at(0)),
        EnqueueOutcome::Queued(_)
    ));
    let mut run = worker_run(&mut state);
    assert_eq!(
        run.advance(&mut state, at(10)),
        Step::WaitForBinding { wake_at: at(110) }
    );
    state.configure_peers(vec![peer(1, 4)]).unwrap();
    assert_eq!(run.advance(&mut state, at(110)), Step::Attempt(peer(1, 4)));
}
#[test]
fn an_item_bound_to_a_generation_ends_peer_rotated_when_the_set_rotates() {
    let mut state = SidecarState::new(limits(2));
    state.enqueue(request(2, 2), at(0));
    let item = state.next_outbound().unwrap();
    let mut run = OutboundRun::start(&state, item).unwrap();
    // The first install of an empty table fences nothing; the item then binds.
    state.configure_peers(vec![peer(1, 4)]).unwrap();
    assert_eq!(run.advance(&mut state, at(0)), Step::Attempt(peer(1, 4)));
    // Rotation fences the in-flight token: PeerRotated is the first terminal,
    // so the worker's own Cancelled loses (first wins).
    state.configure_peers(vec![peer(1, 5)]).unwrap();
    assert_eq!(
        run.advance(&mut state, at(1)),
        Step::Finish(OutboundTerminal::PeerRotated)
    );
    assert_eq!(state.outbound_free(), 2);
}
#[test]
fn queues_are_bounded_and_duplicates_do_not_consume_slots() {
    let mut state = configured(1);
    let handle = queued(&mut state, 1, 0);
    assert_eq!(
        state.enqueue(request(1, 1), at(0)),
        EnqueueOutcome::Duplicate(handle)
    );
    assert_eq!(
        state.enqueue(request(1, 9), at(0)),
        EnqueueOutcome::ReplayCollision
    );
    assert_eq!(state.enqueue(request(2, 2), at(0)), EnqueueOutcome::Full);
    assert_eq!(state.outbound_free(), 0);
}
#[test]
fn routed_send_fails_closed_and_checks_the_named_generation() {
    // Oracle: routed_send_* tests.
    let mut state = SidecarState::new(limits(4));
    state.configure_peers(vec![peer(1, 3), peer(2, 7)]).unwrap();
    let mut routed = request(1, 1);
    routed.destination = Some([2; 32]);
    assert!(matches!(
        state.enqueue(routed.clone(), at(0)),
        EnqueueOutcome::Queued(_)
    ));
    let item = state.next_outbound().unwrap();
    assert_eq!(item.target, Some(peer(2, 7)));
    routed.message_id = [2; 16];
    routed.destination = Some([9; 32]);
    assert_eq!(
        state.enqueue(routed.clone(), at(0)),
        EnqueueOutcome::PeerRotated
    );
    routed.destination = Some([2; 32]);
    routed.expected_generation = Some(6);
    assert_eq!(
        state.enqueue(routed.clone(), at(0)),
        EnqueueOutcome::PeerRotated
    );
    routed.expected_generation = Some(7);
    assert!(matches!(
        state.enqueue(routed, at(0)),
        EnqueueOutcome::Queued(_)
    ));
}

// -------------------------------------------------- peer configuration
#[test]
fn configure_peers_fences_displaced_inflight_work() {
    // Oracle: configure_peers_fences_displaced_inflight_work.
    let mut state = configured(4);
    let handle = queued(&mut state, 1, 0);
    state.pool_register(([1; 32], 1));
    let outcome = state.configure_peers(vec![peer(1, 2), peer(2, 2)]).unwrap();
    assert!(outcome.changed);
    assert_eq!(outcome.generation, 2);
    assert_eq!(outcome.closed_pool, vec![([1; 32], 1)]);
    assert_eq!(state.terminal(handle), Some(OutboundTerminal::PeerRotated));
    assert!(state.is_cancelled(handle));
    assert!(state.peer_matches([2; 32], 2));
    assert_eq!(state.outbound_free(), 4);
    assert!(state.next_outbound().is_none());
    assert_eq!(state.published_generation(), 2);
}
#[test]
fn configure_peers_is_atomic_and_idempotent() {
    let mut state = SidecarState::new(limits(2));
    assert_eq!(state.configure_peers(vec![]), Err(ConfigureRefusal::Empty));
    assert_eq!(
        state.configure_peers(vec![peer(1, 5), peer(1, 5)]),
        Err(ConfigureRefusal::DuplicateEndpoint)
    );
    state.configure_peers(vec![peer(1, 5), peer(2, 5)]).unwrap();
    let handle = queued(&mut state, 1, 0);
    let same = state.configure_peers(vec![peer(1, 5), peer(2, 5)]).unwrap();
    assert!(!same.changed);
    assert_eq!(
        state.terminal(handle),
        None,
        "an identical set fences nothing"
    );
    // One stale entry rejects the whole set (oracle ..._when_one_entry_is_stale).
    assert_eq!(
        state.configure_peers(vec![peer(1, 6), peer(2, 5)]),
        Err(ConfigureRefusal::StaleGeneration)
    );
    assert!(state.peer_matches([1; 32], 5));
    assert!(state.peer_matches([2; 32], 5));
}

// ------------------------------------------------------- inbound history
fn admit(state: &mut SidecarState, id: u8, digest: u8) -> AdmissionOutcome {
    state.admit_inbound(None, [id; 16], [digest; 32], vec![id])
}
#[test]
fn inbound_disconnect_redelivers_until_ack() {
    // Oracle: inbound_disconnect_redelivers_until_ack.
    let mut state = SidecarState::new(limits(1));
    let AdmissionOutcome::Admitted(handle) = admit(&mut state, 1, 1) else {
        panic!("admitted")
    };
    let first = state.receive(7).unwrap();
    assert_eq!(first.message_id, [1; 16]);
    state.redeliver_session(7);
    let again = state.receive(8).unwrap();
    assert_eq!(again, first);
    assert!(!state.ack(7, [1; 16]), "the old session no longer holds it");
    assert_eq!(state.inbound_terminal(handle), None);
    assert!(state.ack(8, [1; 16]));
    assert_eq!(
        state.inbound_terminal(handle),
        Some(InboundTerminal::Acknowledged)
    );
    assert!(state.receive(8).is_none());
    assert_eq!(state.inbound_free(), 1);
}
#[test]
fn redelivery_restores_original_order_ahead_of_newer_pending_items() {
    let mut state = SidecarState::new(limits(8));
    for id in 1..=4 {
        admit(&mut state, id, id);
    }
    assert_eq!(state.receive(7).unwrap().message_id, [1; 16]);
    assert_eq!(state.receive(7).unwrap().message_id, [2; 16]);
    state.redeliver_session(7);
    let order: Vec<u8> = std::iter::from_fn(|| state.receive(9))
        .map(|d| d.message_id[0])
        .collect();
    assert_eq!(order, [1, 2, 3, 4]);
}
#[test]
fn ack_requires_the_session_that_holds_the_item_inflight() {
    let mut state = SidecarState::new(limits(2));
    admit(&mut state, 1, 1);
    assert!(!state.ack(7, [1; 16]), "still pending, not inflight");
    state.receive(7).unwrap();
    assert!(!state.ack(8, [1; 16]), "another session cannot ack it");
    assert!(!state.ack(7, [2; 16]), "unknown id");
    assert!(state.ack(7, [1; 16]));
    assert!(!state.ack(7, [1; 16]));
}
#[test]
fn inbound_queue_is_bounded_and_duplicates_do_not_consume_slots() {
    // Oracle: queues_are_bounded_and_duplicates_do_not_consume_slots.
    let mut state = SidecarState::new(limits(1));
    assert!(matches!(
        admit(&mut state, 1, 1),
        AdmissionOutcome::Admitted(_)
    ));
    assert!(matches!(
        admit(&mut state, 1, 1),
        AdmissionOutcome::PendingDuplicate(_)
    ));
    assert_eq!(admit(&mut state, 1, 9), AdmissionOutcome::ReplayCollision);
    assert_eq!(admit(&mut state, 2, 2), AdmissionOutcome::Full);
}
#[test]
fn completed_duplicates_ack_and_changed_digests_collide() {
    let mut state = SidecarState::new(limits(2));
    admit(&mut state, 1, 1);
    state.receive(7).unwrap();
    assert!(state.ack(7, [1; 16]));
    assert_eq!(
        admit(&mut state, 1, 1),
        AdmissionOutcome::CompletedDuplicate
    );
    assert_eq!(admit(&mut state, 1, 2), AdmissionOutcome::ReplayCollision);
}
#[test]
fn active_inbound_id_is_never_evicted_by_completed_history() {
    // Oracle: active_inbound_id_is_never_evicted_by_completed_history.
    let mut state = SidecarState::new(limits(2));
    assert!(matches!(
        admit(&mut state, 1, 1),
        AdmissionOutcome::Admitted(_)
    ));
    assert!(state.receive(7).is_some());
    for id in 2_u8..=70 {
        assert!(matches!(
            admit(&mut state, id, id),
            AdmissionOutcome::Admitted(_)
        ));
        assert!(state.receive(8).is_some());
        assert!(state.ack(8, [id; 16]));
    }
    assert!(matches!(
        admit(&mut state, 1, 1),
        AdmissionOutcome::PendingDuplicate(_)
    ));
}
#[test]
fn completed_history_is_fifo_bounded_by_the_seen_limit() {
    let mut state = SidecarState::new(limits(1));
    let bound = state.limits().seen_limit();
    let total = u8::try_from(bound + 3).unwrap();
    for id in 1..=total {
        assert!(matches!(
            admit(&mut state, id, id),
            AdmissionOutcome::Admitted(_)
        ));
        state.receive(1).unwrap();
        assert!(state.ack(1, [id; 16]));
    }
    // The three oldest completed ids aged out: they are admitted anew, while
    // the newest still answer as completed duplicates.
    assert!(matches!(
        admit(&mut state, 1, 1),
        AdmissionOutcome::Admitted(_)
    ));
    assert_eq!(
        admit(&mut state, total, total),
        AdmissionOutcome::CompletedDuplicate
    );
}
#[test]
fn rotation_fence_clears_inbound_work_and_marks_ids_rotated() {
    let mut state = configured(4);
    let origin = Some(([1; 32], 1));
    let AdmissionOutcome::Admitted(active) = state.admit_inbound(origin, [1; 16], [1; 32], vec![1])
    else {
        panic!("admitted")
    };
    state.admit_inbound(origin, [2; 16], [2; 32], vec![2]);
    state.receive(7).unwrap();
    state.admit_inbound(origin, [3; 16], [3; 32], vec![3]);
    state.receive(7).unwrap();
    state.receive(7).unwrap();
    assert!(state.ack(7, [3; 16]));
    state.configure_peers(vec![peer(1, 2)]).unwrap();
    assert_eq!(
        state.inbound_terminal(active),
        Some(InboundTerminal::PeerRotated)
    );
    assert_eq!(state.inbound_pending(), 0);
    assert!(state.receive(7).is_none());
    assert_eq!(state.inbound_free(), 4);
    // Active and completed ids are now rotated, ahead of capacity admission.
    for id in 1..=3 {
        assert_eq!(
            state.admit_inbound(Some(([1; 32], 2)), [id; 16], [id; 32], vec![id]),
            AdmissionOutcome::PeerRotated
        );
    }
}
#[test]
fn stale_origin_is_rotated_and_remembered_before_capacity_admission() {
    let mut state = configured(1);
    // A full queue does not change a stale-origin verdict.
    assert!(matches!(
        admit(&mut state, 1, 1),
        AdmissionOutcome::Admitted(_)
    ));
    assert_eq!(
        state.admit_inbound(Some(([1; 32], 9)), [5; 16], [5; 32], vec![5]),
        AdmissionOutcome::PeerRotated
    );
    // The id is remembered rotated even with a now-valid origin.
    assert_eq!(
        state.admit_inbound(Some(([1; 32], 1)), [5; 16], [5; 32], vec![5]),
        AdmissionOutcome::PeerRotated
    );
    assert_eq!(
        state.admit_inbound(Some(([9; 32], 1)), [6; 16], [6; 32], vec![6]),
        AdmissionOutcome::PeerRotated
    );
}
