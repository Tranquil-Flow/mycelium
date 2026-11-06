// SPDX-License-Identifier: AGPL-3.0-only
//! Controlled path cancellation over exact-subject pending sends (`runtime::subject_cancel`)
//! against v0.1.4 mycelium_router/transports/iroh.py @ f4e366e9: the public entry
//! `apply_controlled_path_cancellation` :4290-4335 and its per-subject apply :4337-4438,
//! `_cancel_pending_scope_locked` :2824-2850, the reservation :2704-2719, the delivery-cancel
//! worker's deadline :2959-2976, admission check :2981-2990 and `finally` :3086-3101, and the
//! cleanup projection's `pending_delivery_count` :1101-1132, with
//! `_DELIVERY_CANCEL_ACK_TIMEOUT_SECONDS = 1.0` (:75). Every instant, window, flag, count and
//! trace below is pinned by hand from those lines on a fake clock; none is computed by the core or
//! read through its budget accessor.
use budgets::{ClockDomain, MonoInstant, Span};
use runtime::prefill_watch::MonotonicClock;
use runtime::sidecar_pool::{
    EnqueueOutcome, EnqueueRequest, MessageId, OutboundHandle, OutboundTerminal, PoolLimits,
    SidecarState,
};
use runtime::subject_cancel::{
    ApplyError, ApplyFault, ApplyReceipt, CancelResult, CancelWindow, Gate, OwnerRouter,
    PathCancellation, PendingSend, Reservation, RouterAnswer, RunningDeliveryCancel, SendSweep,
    SidecarCancel, SubjectCancel, SubjectTrace, Sweep, WorkerStart, WorkerView,
};
use std::cell::Cell;
use std::collections::VecDeque;
use std::convert::Infallible;
use std::fmt::Debug;

const NODE: &str = "local-node";

/// A settable monotonic clock (seconds).
struct Clock(Cell<f64>);

impl Clock {
    fn at(seconds: f64) -> Self {
        Self(Cell::new(seconds))
    }
    fn set(&self, seconds: f64) {
        self.0.set(seconds);
    }
}

impl MonotonicClock for Clock {
    fn monotonic(&self) -> f64 {
        self.0.get()
    }
}

/// A Router with the owner-control entry point that accepts every subject (the oracle's
/// `_OwnerCancellationRouter`, tests/path_cancellation_adversarial/test_iroh_transport.py:49-53).
#[derive(Default)]
struct Router {
    owner_calls: Vec<PathCancellation>,
}

impl OwnerRouter for Router {
    type Error = Infallible;
    fn apply_controlled_path_cancellation(
        &mut self,
        cancellation: &PathCancellation,
    ) -> Option<Result<bool, Infallible>> {
        self.owner_calls.push(cancellation.clone());
        Some(Ok(true))
    }
    fn receive_path_cancellation(
        &mut self,
        cancellation: &PathCancellation,
        source_node_id: &str,
    ) -> Result<bool, Infallible> {
        panic!(
            "an owner-control Router is never asked to receive {cancellation:?} from {source_node_id}"
        )
    }
}

/// The Router's own error (a Router that raises; the physical node retries it,
/// physical_inference_node.py:5635-5645).
#[derive(Debug, PartialEq)]
struct RouterRaised;

/// An owner-control Router that answers each call from its script, in call order.
struct ScriptedRouter {
    script: VecDeque<Result<bool, RouterRaised>>,
    owner_calls: Vec<PathCancellation>,
}

impl ScriptedRouter {
    fn new(script: impl IntoIterator<Item = Result<bool, RouterRaised>>) -> Self {
        Self {
            script: script.into_iter().collect(),
            owner_calls: Vec::new(),
        }
    }
}

impl OwnerRouter for ScriptedRouter {
    type Error = RouterRaised;
    fn apply_controlled_path_cancellation(
        &mut self,
        cancellation: &PathCancellation,
    ) -> Option<Result<bool, RouterRaised>> {
        self.owner_calls.push(cancellation.clone());
        let answer = self.script.pop_front();
        Some(answer.unwrap_or_else(|| panic!("no scripted Router answer for {cancellation:?}")))
    }
    fn receive_path_cancellation(
        &mut self,
        cancellation: &PathCancellation,
        source_node_id: &str,
    ) -> Result<bool, RouterRaised> {
        panic!(
            "an owner-control Router is never asked to receive {cancellation:?} from {source_node_id}"
        )
    }
}

fn subject(request: &str, path: &str, attempt: u64) -> PathCancellation {
    PathCancellation {
        request_id: request.to_owned(),
        path_id: path.to_owned(),
        path_attempt: attempt,
        topology_version: 3,
    }
}

/// The transport state of `NODE` with queue capacity 128 and the given delivery timeout (s).
fn transport(delivery_s: f64) -> SubjectCancel {
    match SubjectCancel::new(NODE, 128, delivery_s) {
        Ok(core) => core,
        Err(error) => panic!("a valid transport configuration is refused: {error:?}"),
    }
}

/// A subject this node entered and participates in, as a sender's manifest registers it.
fn register(core: &mut SubjectCancel, registered: &PathCancellation) {
    core.set_entry_node(&registered.request_id, NODE);
    core.set_participants(&registered.path_id, [NODE, "peer-node"]);
    core.set_path_subject(registered.clone());
}

fn id(n: u8) -> MessageId {
    [n; 16]
}

/// An ordinary send (`cancellable_forward=False`) that has started sidecar admission.
fn send(core: &mut SubjectCancel, n: u8, request: &str, path: &str, attempt: u64) -> MessageId {
    let mut pending = PendingSend::new(Some(request), Some(path), Some(attempt));
    pending.admission_started = true;
    core.register_pending(id(n), pending);
    id(n)
}

/// A deferred path forward (`cancellable_forward=True`) that has started sidecar admission.
fn forward_send(
    core: &mut SubjectCancel,
    n: u8,
    request: &str,
    path: &str,
    attempt: u64,
) -> MessageId {
    let mut pending = PendingSend::new(Some(request), Some(path), Some(attempt));
    pending.cancellable_forward = true;
    pending.admission_started = true;
    core.register_pending(id(n), pending);
    id(n)
}

/// The public apply must be an accepted success.
fn accepted<E: Debug>(result: Result<ApplyReceipt, ApplyError<E>>) -> ApplyReceipt {
    assert!(
        result.as_ref().is_ok_and(|receipt| receipt.accepted),
        "the public apply must be an accepted success, got {:?}",
        result
            .as_ref()
            .map(|receipt| receipt.accepted)
            .map_err(|error| &error.fault),
    );
    result.unwrap_or_else(|error| panic!("checked above: {error:?}"))
}

fn reserved(message_id: MessageId, cleanup_deadline: Option<f64>) -> SendSweep {
    SendSweep {
        message_id,
        sweep: Sweep::ExactSubject,
        reservation: Reservation::Reserved { cleanup_deadline },
    }
}

/// An exact-subject send whose cancellation had already started: no second worker.
fn already_started(message_id: MessageId) -> SendSweep {
    SendSweep {
        message_id,
        sweep: Sweep::ExactSubject,
        reservation: Reservation::AlreadyStarted,
    }
}

/// A subject that passed every gate and was accepted by the owner-control Router.
fn applied(applied: &PathCancellation, sends: Vec<SendSweep>) -> SubjectTrace {
    SubjectTrace {
        subject: applied.clone(),
        gate: Gate::Admitted,
        sends,
        router: Some(RouterAnswer::OwnerControl(true)),
        accepted: true,
    }
}

/// `(cancelled, reason, cancel_started, cancel_confirmed)` of a live send.
fn flags(core: &SubjectCancel, message_id: MessageId) -> (bool, &'static str, bool, bool) {
    let pending = core
        .pending(&message_id)
        .unwrap_or_else(|| panic!("send {message_id:?} is no longer pending"));
    (
        pending.cancelled(),
        pending.reason(),
        pending.cancel_started(),
        pending.cancel_confirmed(),
    )
}

const RESERVED: (bool, &str, bool, bool) = (true, "path_cancelled", true, false);
const CONFIRMED: (bool, &str, bool, bool) = (true, "path_cancelled", true, true);
const UNTOUCHED: (bool, &str, bool, bool) = (false, "", false, false);

fn on_sidecar(started: WorkerStart) -> RunningDeliveryCancel {
    match started {
        WorkerStart::Sidecar(running) => running,
        other => panic!("an admitted send's worker must ask the sidecar, got {other:?}"),
    }
}

/// The (message id, cleanup deadline) of each registered worker, in registration order.
fn worker_deadlines(receipt: &ApplyReceipt) -> Vec<(MessageId, Option<f64>)> {
    receipt
        .dispatched
        .iter()
        .map(|d| (d.message_id(), d.cleanup_deadline()))
        .collect()
}

/// The node ids registered as a path's participants.
fn participant_ids<'a>(core: &'a SubjectCancel, path_id: &str) -> Option<Vec<&'a str>> {
    core.participants(path_id)
        .map(|nodes| nodes.iter().map(String::as_str).collect())
}

/// A worker started at 100 s under the supplied absolute 107 s: the deadline verbatim and
/// `max(0, 107 - 100)` left (:2975-2976).
const AT_100_UNTIL_107: CancelWindow = CancelWindow {
    started_at: 100.0,
    deadline: 107.0,
    ack_window: 7.0,
};
/// The same deadline for a worker started at 102 s: 5 s left.
const AT_102_UNTIL_107: CancelWindow = CancelWindow {
    started_at: 102.0,
    deadline: 107.0,
    ack_window: 5.0,
};
/// The same deadline for a retry worker started at 101 s: 6 s left.
const AT_101_UNTIL_107: CancelWindow = CancelWindow {
    started_at: 101.0,
    deadline: 107.0,
    ack_window: 6.0,
};
/// A worker with no cleanup deadline started at 100 s, delivery timeout 5 s: the
/// `min(1.0, 5.0)` ACK window and 100 + 1 (:2960-2964).
const AT_100_ACK_1S: CancelWindow = CancelWindow {
    started_at: 100.0,
    deadline: 101.0,
    ack_window: 1.0,
};

/// A sidecar holding one queued outbound item per message id.
fn sidecar(ids: &[MessageId]) -> (SidecarState, Vec<OutboundHandle>) {
    let mut state = SidecarState::new(PoolLimits {
        capacity: 4,
        seen_factor: 8,
        seen_floor: 64,
        initial_retry: Span::from_millis(50),
        max_retry: Span::from_millis(1_000),
        lifetime: Span::from_millis(60_000),
    });
    let handles = ids
        .iter()
        .map(|message_id| {
            let request = EnqueueRequest {
                message_id: *message_id,
                digest: [7; 32],
                payload: b"frame".to_vec(),
                expected_generation: None,
                source_generation: None,
                destination: None,
            };
            match state.enqueue(request, MonoInstant::new(ClockDomain(5), 100_000)) {
                EnqueueOutcome::Queued(handle) => handle,
                other => panic!("the sidecar must queue {message_id:?}, got {other:?}"),
            }
        })
        .collect();
    (state, handles)
}

#[test]
fn subject_cancel_supplied_deadline_reaches_every_matching_delivery() {
    let clock = Clock::at(100.0);
    let mut core = transport(5.0);
    let rp1 = subject("R", "P", 1);
    register(&mut core, &rp1);
    let first = send(&mut core, 1, "R", "P", 1);
    let second = send(&mut core, 2, "R", "P", 1);
    let mut router = Router::default();

    let receipt = accepted(core.apply_controlled_path_cancellation(
        Some(&mut router),
        &rp1,
        true,
        Some(107.0),
    ));

    assert_eq!(receipt.gate, Gate::Admitted);
    assert_eq!(
        receipt.subjects,
        vec![applied(
            &rp1,
            vec![reserved(first, Some(107.0)), reserved(second, Some(107.0))]
        )]
    );
    let dispatched: Vec<(MessageId, Option<f64>)> = receipt
        .dispatched
        .iter()
        .map(|d| (d.message_id(), d.cleanup_deadline()))
        .collect();
    assert_eq!(
        dispatched,
        vec![(first, Some(107.0)), (second, Some(107.0))]
    );
    assert_eq!(router.owner_calls, vec![rp1.clone()]);
    assert_eq!(flags(&core, first), RESERVED);
    assert_eq!(flags(&core, second), RESERVED);
    assert_eq!(core.last_cancellation(), Some(&rp1));
    assert!(core.cancellation_observed("R", "P", 1));
    assert!(core.request_controlled_cancelled("R"));
    // `_forget_path` retired the subject, its participants and the request's entry.
    assert_eq!(core.path_subjects(), &[] as &[PathCancellation]);
    assert_eq!(core.participants("P"), None);
    assert_eq!(core.entry_node("R"), None);

    let mut workers = receipt.dispatched.into_iter();
    let at_100 = workers
        .next()
        .map(|d| core.start_delivery_cancel(d, &clock));
    clock.set(102.0);
    let at_102 = workers
        .next()
        .map(|d| core.start_delivery_cancel(d, &clock));
    let (at_100, at_102) = match (at_100, at_102) {
        (Some(a), Some(b)) => (on_sidecar(a), on_sidecar(b)),
        other => panic!("two registered workers, got {other:?}"),
    };
    assert_eq!(at_100.message_id(), first);
    assert_eq!(at_100.window(), AT_100_UNTIL_107);
    assert_eq!(at_102.message_id(), second);
    assert_eq!(at_102.window(), AT_102_UNTIL_107);
    assert_eq!(
        core.delivery_cancel_worker(&first),
        Some(WorkerView {
            cleanup_deadline: Some(107.0),
            window: Some(AT_100_UNTIL_107),
        })
    );
    assert_eq!(
        core.delivery_cancel_worker(&second),
        Some(WorkerView {
            cleanup_deadline: Some(107.0),
            window: Some(AT_102_UNTIL_107),
        })
    );
    assert_eq!(core.delivery_cancel_workers(), 2);
    assert_eq!(core.pending_delivery_count("R", "P", Some(1)), 2);

    // A deferred forward of the same subject is met first, by the forward sweep (:4386,
    // :2761-2780), and its worker gets no cleanup deadline (:2780): the 1 s ACK window even
    // under the supplied 107. The exact sweep (:4391-4393) then reserves the ordinary send with
    // 107 and finds the forward already started.
    let clock = Clock::at(100.0);
    let mut core = transport(5.0);
    register(&mut core, &rp1);
    let ordinary = send(&mut core, 1, "R", "P", 1);
    let forward = forward_send(&mut core, 2, "R", "P", 1);
    let mut router = Router::default();

    let receipt = accepted(core.apply_controlled_path_cancellation(
        Some(&mut router),
        &rp1,
        true,
        Some(107.0),
    ));

    assert_eq!(
        receipt.subjects,
        vec![applied(
            &rp1,
            vec![
                SendSweep {
                    message_id: forward,
                    sweep: Sweep::CancellableForward,
                    reservation: Reservation::Reserved {
                        cleanup_deadline: None
                    },
                },
                reserved(ordinary, Some(107.0)),
                already_started(forward),
            ]
        )]
    );
    assert_eq!(
        worker_deadlines(&receipt),
        vec![(forward, None), (ordinary, Some(107.0))]
    );
    assert_eq!(router.owner_calls, vec![rp1.clone()]);
    assert_eq!(flags(&core, ordinary), RESERVED);
    assert_eq!(flags(&core, forward), RESERVED);
    let windows: Vec<(MessageId, CancelWindow)> = receipt
        .dispatched
        .into_iter()
        .map(|d| on_sidecar(core.start_delivery_cancel(d, &clock)))
        .map(|running| (running.message_id(), running.window()))
        .collect();
    assert_eq!(
        windows,
        vec![(forward, AT_100_ACK_1S), (ordinary, AT_100_UNTIL_107)]
    );
    assert_eq!(
        core.delivery_cancel_worker(&forward),
        Some(WorkerView {
            cleanup_deadline: None,
            window: Some(AT_100_ACK_1S),
        })
    );
}

#[test]
fn subject_cancel_none_uses_min_of_ack_and_delivery_timeout() {
    // (delivery timeout, ACK window, absolute deadline) for a worker started at 100 s:
    // min(1.0, delivery) (:2960-2963) and start + window (:2964).
    let cases = [(0.25, 0.25, 100.25), (5.0, 1.0, 101.0)];
    for (delivery_s, ack_s, absolute_s) in cases {
        let clock = Clock::at(100.0);
        let mut core = transport(delivery_s);
        let rp1 = subject("R", "P", 1);
        register(&mut core, &rp1);
        let only = send(&mut core, 1, "R", "P", 1);
        let mut router = Router::default();

        let receipt =
            accepted(core.apply_controlled_path_cancellation(Some(&mut router), &rp1, true, None));

        assert_eq!(receipt.gate, Gate::Admitted, "delivery {delivery_s}");
        assert_eq!(
            receipt.subjects,
            vec![applied(&rp1, vec![reserved(only, None)])],
            "delivery {delivery_s}"
        );
        assert_eq!(flags(&core, only), RESERVED, "delivery {delivery_s}");
        let mut workers = receipt.dispatched.into_iter();
        let started = workers
            .next()
            .map(|d| core.start_delivery_cancel(d, &clock));
        assert!(
            workers.next().is_none(),
            "delivery {delivery_s}: one worker"
        );
        let running = match started {
            Some(started) => on_sidecar(started),
            None => panic!("delivery {delivery_s}: no worker registered"),
        };
        assert_eq!(
            running.window(),
            CancelWindow {
                started_at: 100.0,
                deadline: absolute_s,
                ack_window: ack_s,
            },
            "delivery {delivery_s}"
        );
        assert_eq!(
            core.delivery_cancel_worker(&only),
            Some(WorkerView {
                cleanup_deadline: None,
                window: Some(CancelWindow {
                    started_at: 100.0,
                    deadline: absolute_s,
                    ack_window: ack_s,
                }),
            }),
            "delivery {delivery_s}"
        );
    }
}

#[test]
fn subject_cancel_non_matching_subject_untouched() {
    let clock = Clock::at(100.0);
    let mut core = transport(5.0);
    let rp1 = subject("R", "P", 1);
    register(&mut core, &rp1);
    let matching = send(&mut core, 1, "R", "P", 1);
    let other_request = send(&mut core, 2, "S", "P", 1);
    let other_path = send(&mut core, 3, "R", "Q", 1);
    let other_attempt = send(&mut core, 4, "R", "P", 2);
    let mut router = Router::default();

    let receipt = accepted(core.apply_controlled_path_cancellation(
        Some(&mut router),
        &rp1,
        true,
        Some(107.0),
    ));

    // Q is no registered subject of R: only (R,P,1) is applied, and only its send is met.
    assert_eq!(receipt.gate, Gate::Admitted);
    assert_eq!(
        receipt.subjects,
        vec![applied(&rp1, vec![reserved(matching, Some(107.0))])]
    );
    assert_eq!(router.owner_calls, vec![rp1.clone()]);
    assert_eq!(flags(&core, matching), RESERVED);
    for untouched in [other_request, other_path, other_attempt] {
        assert_eq!(flags(&core, untouched), UNTOUCHED, "{untouched:?}");
        assert_eq!(
            core.delivery_cancel_worker(&untouched),
            None,
            "{untouched:?}"
        );
    }
    assert_eq!(core.delivery_cancel_workers(), 1);
    // The owner-cancelled request's projection is request-wide (:1101-1120): every
    // unconfirmed send of R. S is projected for its exact path and attempt.
    assert_eq!(core.pending_delivery_count("R", "P", Some(1)), 3);
    assert_eq!(core.pending_delivery_count("S", "P", Some(1)), 1);

    let mut workers = receipt.dispatched.into_iter();
    let started = workers
        .next()
        .map(|d| core.start_delivery_cancel(d, &clock));
    assert!(workers.next().is_none(), "one worker");
    let running = match started {
        Some(started) => on_sidecar(started),
        None => panic!("the matching send has no worker"),
    };
    assert_eq!(running.message_id(), matching);
    assert_eq!(running.window(), AT_100_UNTIL_107);
}

#[test]
fn subject_cancel_successor_lineage_included() {
    let clock = Clock::at(100.0);
    let mut core = transport(5.0);
    let original = subject("R", "P", 1);
    let successor = subject("R", "Q", 2);
    register(&mut core, &original);
    register(&mut core, &successor);
    let on_original = send(&mut core, 1, "R", "P", 1);
    let on_successor = send(&mut core, 2, "R", "Q", 2);
    let other_request = send(&mut core, 3, "S", "Q", 2);
    let mut router = Router::default();

    let receipt = accepted(core.apply_controlled_path_cancellation(
        Some(&mut router),
        &original,
        true,
        Some(107.0),
    ));

    assert_eq!(receipt.gate, Gate::Admitted);
    assert_eq!(
        receipt.subjects,
        vec![
            applied(&original, vec![reserved(on_original, Some(107.0))]),
            applied(&successor, vec![reserved(on_successor, Some(107.0))]),
        ]
    );
    assert_eq!(
        router.owner_calls,
        vec![original.clone(), successor.clone()]
    );
    let dispatched: Vec<(MessageId, Option<f64>)> = receipt
        .dispatched
        .iter()
        .map(|d| (d.message_id(), d.cleanup_deadline()))
        .collect();
    assert_eq!(
        dispatched,
        vec![(on_original, Some(107.0)), (on_successor, Some(107.0))]
    );
    assert_eq!(flags(&core, on_original), RESERVED);
    assert_eq!(flags(&core, on_successor), RESERVED);
    assert_eq!(flags(&core, other_request), UNTOUCHED);
    assert_eq!(core.delivery_cancel_worker(&other_request), None);
    assert!(core.cancellation_observed("R", "Q", 2));
    assert!(!core.cancellation_observed("S", "Q", 2));
    assert_eq!(core.path_subjects(), &[] as &[PathCancellation]);
    assert_eq!(core.entry_node("R"), None);

    let windows: Vec<(MessageId, CancelWindow)> = receipt
        .dispatched
        .into_iter()
        .map(|d| on_sidecar(core.start_delivery_cancel(d, &clock)))
        .map(|running| (running.message_id(), running.window()))
        .collect();
    assert_eq!(
        windows,
        vec![
            (on_original, AT_100_UNTIL_107),
            (on_successor, AT_100_UNTIL_107),
        ]
    );

    // The lineage is the named request's only (:4317-4319): a registered subject of request S
    // is not applied, not forgotten, its send stays untouched and the Router never sees it. A
    // same-request successor whose path excludes this node is refused by its own gate
    // (:4368-4373) and stays registered, so R keeps its entry; the public result is the OR of
    // the subjects (:4334), accepted although the last one was refused.
    let clock = Clock::at(100.0);
    let mut core = transport(5.0);
    let other = subject("S", "X", 1);
    let elsewhere = subject("R", "T", 4);
    register(&mut core, &original);
    register(&mut core, &successor);
    register(&mut core, &other);
    core.set_participants("T", ["peer-node"]);
    core.set_path_subject(elsewhere.clone());
    let on_original = send(&mut core, 1, "R", "P", 1);
    let on_successor = send(&mut core, 2, "R", "Q", 2);
    let on_other = send(&mut core, 3, "S", "X", 1);
    let on_elsewhere = send(&mut core, 4, "R", "T", 4);
    let mut router = Router::default();

    let receipt = accepted(core.apply_controlled_path_cancellation(
        Some(&mut router),
        &original,
        true,
        Some(107.0),
    ));

    assert_eq!(receipt.gate, Gate::Admitted);
    assert_eq!(
        receipt.subjects,
        vec![
            applied(&original, vec![reserved(on_original, Some(107.0))]),
            applied(&successor, vec![reserved(on_successor, Some(107.0))]),
            SubjectTrace {
                subject: elsewhere.clone(),
                gate: Gate::NotParticipant,
                sends: vec![],
                router: None,
                accepted: false,
            },
        ]
    );
    assert_eq!(
        router.owner_calls,
        vec![original.clone(), successor.clone()]
    );
    assert_eq!(
        worker_deadlines(&receipt),
        vec![(on_original, Some(107.0)), (on_successor, Some(107.0))]
    );
    assert_eq!(flags(&core, on_original), RESERVED);
    assert_eq!(flags(&core, on_successor), RESERVED);
    for untouched in [on_other, on_elsewhere] {
        assert_eq!(flags(&core, untouched), UNTOUCHED, "{untouched:?}");
        assert_eq!(
            core.delivery_cancel_worker(&untouched),
            None,
            "{untouched:?}"
        );
    }
    assert_eq!(core.delivery_cancel_workers(), 2);
    assert_eq!(core.path_subjects(), &[other.clone(), elsewhere.clone()]);
    assert_eq!(core.entry_node("R"), Some(NODE));
    assert_eq!(core.entry_node("S"), Some(NODE));
    assert_eq!(participant_ids(&core, "P"), None);
    assert_eq!(participant_ids(&core, "Q"), None);
    assert_eq!(participant_ids(&core, "X"), Some(vec![NODE, "peer-node"]));
    assert_eq!(participant_ids(&core, "T"), Some(vec!["peer-node"]));
    assert!(core.cancellation_observed("R", "P", 1));
    assert!(core.cancellation_observed("R", "Q", 2));
    assert!(!core.cancellation_observed("S", "X", 1));
    assert!(!core.cancellation_observed("R", "T", 4));
    assert!(core.request_controlled_cancelled("R"));
    assert!(!core.request_controlled_cancelled("S"));
    assert_eq!(core.pending_delivery_count("R", "P", Some(1)), 3);
    assert_eq!(core.pending_delivery_count("S", "X", Some(1)), 1);
    let windows: Vec<(MessageId, CancelWindow)> = receipt
        .dispatched
        .into_iter()
        .map(|d| on_sidecar(core.start_delivery_cancel(d, &clock)))
        .map(|running| (running.message_id(), running.window()))
        .collect();
    assert_eq!(
        windows,
        vec![
            (on_original, AT_100_UNTIL_107),
            (on_successor, AT_100_UNTIL_107),
        ]
    );

    // A Router raising on the successor: the error propagates with everything applied before
    // it, and the successor, forgotten by its `finally` (:4426-4438), is registered again for
    // owner retry (:4327-4332). The retry re-applies it; this time the Router answers False for
    // it, and its own fence is observed, so its trace is still accepted (:4419-4424).
    let clock = Clock::at(101.0);
    let mut core = transport(5.0);
    register(&mut core, &original);
    register(&mut core, &successor);
    let on_original = send(&mut core, 1, "R", "P", 1);
    let on_successor = send(&mut core, 2, "R", "Q", 2);
    let mut router = ScriptedRouter::new([Ok(true), Err(RouterRaised)]);

    let raised = match core.apply_controlled_path_cancellation(
        Some(&mut router),
        &original,
        true,
        Some(107.0),
    ) {
        Err(error) => error,
        Ok(receipt) => panic!(
            "the Router's error must propagate, got accepted={}",
            receipt.accepted
        ),
    };
    assert!(
        matches!(raised.fault, ApplyFault::Router(RouterRaised)),
        "{:?}",
        raised.fault
    );
    assert_eq!(raised.fault.code(), None);
    assert!(raised.partial.accepted);
    assert_eq!(raised.partial.gate, Gate::Admitted);
    assert_eq!(
        raised.partial.subjects,
        vec![
            applied(&original, vec![reserved(on_original, Some(107.0))]),
            SubjectTrace {
                subject: successor.clone(),
                gate: Gate::Admitted,
                sends: vec![reserved(on_successor, Some(107.0))],
                router: None,
                accepted: false,
            },
        ]
    );
    assert_eq!(
        worker_deadlines(&raised.partial),
        vec![(on_original, Some(107.0)), (on_successor, Some(107.0))]
    );
    assert_eq!(
        router.owner_calls,
        vec![original.clone(), successor.clone()]
    );
    assert_eq!(core.path_subjects(), &[successor.clone()]);
    assert_eq!(core.entry_node("R"), None);
    assert_eq!(participant_ids(&core, "Q"), None);
    assert!(core.cancellation_observed("R", "Q", 2));
    assert_eq!(flags(&core, on_original), RESERVED);
    assert_eq!(flags(&core, on_successor), RESERVED);

    router.script.extend([Ok(true), Ok(false)]);
    let retry = accepted(core.apply_controlled_path_cancellation(
        Some(&mut router),
        &original,
        true,
        Some(107.0),
    ));
    assert_eq!(
        retry.subjects,
        vec![
            applied(&original, vec![already_started(on_original)]),
            SubjectTrace {
                subject: successor.clone(),
                gate: Gate::Admitted,
                sends: vec![already_started(on_successor)],
                router: Some(RouterAnswer::OwnerControl(false)),
                accepted: true,
            },
        ]
    );
    assert_eq!(retry.dispatched.len(), 0);
    assert!(router.script.is_empty());
    assert_eq!(
        router.owner_calls,
        vec![
            original.clone(),
            successor.clone(),
            original.clone(),
            successor.clone()
        ]
    );
    assert_eq!(core.path_subjects(), &[] as &[PathCancellation]);
    assert_eq!(core.pending_delivery_count("R", "P", Some(1)), 2);
    for d in raised.partial.dispatched {
        let running = on_sidecar(core.start_delivery_cancel(d, &clock));
        assert_eq!(running.window(), AT_101_UNTIL_107);
        core.finish_delivery_cancel(running, CancelResult::Confirmed);
    }
    assert_eq!(flags(&core, on_original), CONFIRMED);
    assert_eq!(flags(&core, on_successor), CONFIRMED);
    assert_eq!(core.pending_delivery_count("R", "P", Some(1)), 0);
    assert_eq!(core.delivery_cancel_workers(), 0);
}

#[test]
fn subject_cancel_already_reserved_is_reported() {
    let clock = Clock::at(100.0);
    let mut core = transport(5.0);
    let rp1 = subject("R", "P", 1);
    register(&mut core, &rp1);
    let only = send(&mut core, 1, "R", "P", 1);
    let (mut sidecar, handles) = sidecar(&[only]);
    let mut router = Router::default();

    let first = accepted(core.apply_controlled_path_cancellation(
        Some(&mut router),
        &rp1,
        true,
        Some(107.0),
    ));
    assert_eq!(first.gate, Gate::Admitted);
    assert_eq!(
        first.subjects,
        vec![applied(&rp1, vec![reserved(only, Some(107.0))])]
    );
    let mut workers = first.dispatched.into_iter();
    let started = workers
        .next()
        .map(|d| core.start_delivery_cancel(d, &clock));
    assert!(workers.next().is_none(), "exactly one worker");
    let held = match started {
        Some(started) => on_sidecar(started),
        None => panic!("no worker registered"),
    };
    assert_eq!(held.window(), AT_100_UNTIL_107);
    // The sidecar cancels the message; its answer has not reached the worker yet.
    assert_eq!(
        held.cancel_in_sidecar(&mut sidecar),
        SidecarCancel::Cancelled(handles[0])
    );
    assert_eq!(
        sidecar.terminal(handles[0]),
        Some(OutboundTerminal::Cancelled)
    );

    clock.set(103.0);
    let second = accepted(core.apply_controlled_path_cancellation(
        Some(&mut router),
        &rp1,
        true,
        Some(109.0),
    ));
    // Already reserved: reported in the trace, no duplicate worker, no new error.
    assert_eq!(second.gate, Gate::Admitted);
    assert_eq!(
        second.subjects,
        vec![applied(
            &rp1,
            vec![SendSweep {
                message_id: only,
                sweep: Sweep::ExactSubject,
                reservation: Reservation::AlreadyStarted,
            }]
        )]
    );
    assert_eq!(second.dispatched.len(), 0);
    assert_eq!(router.owner_calls, vec![rp1.clone(), rp1.clone()]);
    assert_eq!(core.delivery_cancel_workers(), 1);
    assert_eq!(
        core.delivery_cancel_worker(&only),
        Some(WorkerView {
            cleanup_deadline: Some(107.0),
            window: Some(AT_100_UNTIL_107),
        })
    );
    assert_eq!(flags(&core, only), RESERVED);
    assert_eq!(core.pending_delivery_count("R", "P", Some(1)), 1);

    core.finish_delivery_cancel(held, CancelResult::Confirmed);
    assert_eq!(flags(&core, only), CONFIRMED);
    assert_eq!(core.pending_delivery_count("R", "P", Some(1)), 0);
    assert_eq!(core.delivery_cancel_workers(), 0);
}

#[test]
fn subject_cancel_no_success_before_cancel_result() {
    let clock = Clock::at(100.0);
    let mut core = transport(5.0);
    let rp1 = subject("R", "P", 1);
    register(&mut core, &rp1);
    let only = send(&mut core, 1, "R", "P", 1);
    let (mut sidecar, handles) = sidecar(&[only]);
    let mut router = Router::default();

    let first = accepted(core.apply_controlled_path_cancellation(
        Some(&mut router),
        &rp1,
        true,
        Some(107.0),
    ));
    assert_eq!(first.gate, Gate::Admitted);
    assert_eq!(
        first.subjects,
        vec![applied(&rp1, vec![reserved(only, Some(107.0))])]
    );
    // Accepted, not cleaned up: nothing is confirmed before the cancel's result.
    assert_eq!(flags(&core, only), RESERVED);
    assert_eq!(core.pending_delivery_count("R", "P", Some(1)), 1);
    let mut workers = first.dispatched.into_iter();
    let started = workers
        .next()
        .map(|d| core.start_delivery_cancel(d, &clock));
    assert!(workers.next().is_none(), "exactly one worker");
    let failing = match started {
        Some(started) => on_sidecar(started),
        None => panic!("no worker registered"),
    };
    assert_eq!(failing.window(), AT_100_UNTIL_107);
    assert_eq!(flags(&core, only), RESERVED);
    assert_eq!(core.pending_delivery_count("R", "P", Some(1)), 1);

    // The answer never confirms (the sidecar call failed before it cancelled anything).
    core.finish_delivery_cancel(failing, CancelResult::Unconfirmed);
    assert_eq!(flags(&core, only), (true, "path_cancelled", false, false));
    assert_eq!(core.pending_delivery_count("R", "P", Some(1)), 1);
    assert_eq!(core.delivery_cancel_workers(), 0);
    assert_eq!(sidecar.terminal(handles[0]), None);

    clock.set(101.0);
    let retry = accepted(core.apply_controlled_path_cancellation(
        Some(&mut router),
        &rp1,
        true,
        Some(107.0),
    ));
    assert_eq!(retry.gate, Gate::Admitted);
    assert_eq!(
        retry.subjects,
        vec![applied(&rp1, vec![reserved(only, Some(107.0))])]
    );
    let mut workers = retry.dispatched.into_iter();
    let started = workers
        .next()
        .map(|d| core.start_delivery_cancel(d, &clock));
    assert!(workers.next().is_none(), "exactly one retry worker");
    let retrying = match started {
        Some(started) => on_sidecar(started),
        None => panic!("no retry worker registered"),
    };
    assert_eq!(retrying.window(), AT_101_UNTIL_107);
    // The sidecar's own cancel is not the transport's confirmation.
    assert_eq!(
        retrying.cancel_in_sidecar(&mut sidecar),
        SidecarCancel::Cancelled(handles[0])
    );
    assert_eq!(flags(&core, only), RESERVED);
    assert_eq!(core.pending_delivery_count("R", "P", Some(1)), 1);

    core.finish_delivery_cancel(retrying, CancelResult::Confirmed);
    assert_eq!(flags(&core, only), CONFIRMED);
    assert_eq!(core.pending_delivery_count("R", "P", Some(1)), 0);
    assert_eq!(core.delivery_cancel_workers(), 0);
}
