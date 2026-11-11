// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-fanned controlled path cancellation of the v0.1.4 iroh transport's pending sends
//! (`mycelium_router/transports/iroh.py @ f4e366e9`) as a pure, clock-injected core:
//!
//! - [`SubjectCancel::apply_controlled_path_cancellation`] is the public entry (:4290-4335) over
//!   the per-subject `_apply_controlled_path_cancellation` (:4337-4438): the participant and
//!   entry gates, the exact cancellation fence (:4383-4385), the cancellable-forward sweep
//!   (:4386, :2761-2780), the exact-subject pending sweep `_cancel_pending_scope_locked`
//!   (:2824-2850), the Router hand-off with its `cancellation_observed` fallback (:4402-4425)
//!   and `_forget_path` in its `finally` (:4426-4438, :4712-4719);
//! - a reservation is `_reserve_pending_cancellation_locked` (:2704-2719), and a newly reserved
//!   send registers exactly one delivery-cancel worker (`_start_delivery_cancel_locked`,
//!   :2898-2922);
//! - [`SubjectCancel::start_delivery_cancel`] is that worker's head (:2959-2990): its deadline
//!   and remaining ACK window at its own clock read, and the local confirmation of a send that
//!   never reached admission; [`SubjectCancel::finish_delivery_cancel`] is its `finally`
//!   (:3086-3101);
//! - [`SubjectCancel::pending_delivery_count`] is `pending_delivery_count` of the cleanup
//!   projection `_cancellation_cleanup_state_locked` (:1101-1132).
//!
//! Accepted is not cleanup. An apply returns once the sends are reserved and their workers
//! registered; it never waits for a sidecar answer. A send stays a cleanup blocker until its
//! worker finishes with [`CancelResult::Confirmed`]. The sidecar's own cancel
//! ([`RunningDeliveryCancel::cancel_in_sidecar`], [`SidecarState::cancel_handle`]) is not that
//! confirmation.
//!
//! Successor subjects come only from the registered `_path_subjects` of the same request, never
//! from the caller. The public apply gates on the named subject; each subject is then applied
//! with its own gates. When one raises, that subject is registered again for owner retry and the
//! error propagates with the partial receipt (:4321-4333); the workers it already registered
//! stay registered, as the oracle's started threads do.
//!
//! Not here (the transport wiring owns them): the state lock, `_require_running`, the worker
//! threads, the cancellation-client pool and the worker's attempt loop (:2991-3085), the
//! deferred forward queue with its `_cancelled_forward_scopes` fence and the active forward
//! client interruption (:2727-2759, :2782-2786, :4395), the path graphs `_path_graphs` (forgotten
//! with the path), and the runtime type check of `entry_cancelled`, which is a Rust `bool`.
//!
//! Times are the clock's floats, as `time.monotonic()` returns them, and a supplied cleanup
//! deadline is the caller's f64 as it is: past, NaN and ±inf deadlines are used verbatim and
//! compared exactly as Python does, never refused, renewed or capped. The ACK cap is
//! `PyMyceliumRouterTransportsIrohDeliveryCancelAckTimeoutSecondsL75` (1 s, :75). The delivery
//! timeout and the history capacity (`queue_capacity`, :946) are the transport's constructor
//! values and are refused as its constructor refuses them (:859-866).
//!
//! A subject's fields are the values the registry holds, as a peer's `ManifestLocked` or the
//! owner's command delivers them, each a [`PyJson`]. A peer's frame types none of them
//! (mycelium_router/wire.py:263-270 copies them, :459-475 only compares them, relay.py:988-1100
//! only compares them), so an attempt arrives as any `int` (`2**64`, `-1`), a `float` (`1.0`,
//! `inf`), a `bool`, a `str` or `None`, a request id as a non-empty `str` (lone surrogates
//! included) or a nonzero number, a path id as a `str`, a number or `None`, and a topology
//! version as any value. Upstream refuses only a falsy request id and a NaN attempt or id (it
//! differs from its own manifest copy, wire.py:461-468). A pending send's scope is the one
//! `_frame_scope` keeps (:1245-1249): a `str` id or `None`, an `int` attempt of any sign and
//! size (never a `bool`) or `None`. Every comparison is Python's. `==` and `!=` are
//! [`py_json_eq`] (`1 == 1.0 == True`, `-0.0 == 0`, `None == None`, `"1" != 1`, a NaN equals
//! nothing), as in both sweeps (:2762-2767, :2833-2837), the same-request lineage (:4318-4319,
//! :4716-4717) and the cleanup projection (:1108-1119). A dict or tuple key (`_path_subjects`,
//! `_entry_nodes`, `_participant_nodes_by_path`, `_controlled_cancelled_requests`,
//! `_cancellations_by_subject` and the probe of `cancellation_observed`, :1263-1292) is looked
//! up as CPython's dict looks it up, by [`py_json_same`]: `hash(1) == hash(1.0) == hash(True)`,
//! so Python-equal keys are one key, and a key keeps its place when it is set again.
//!
//! Declared difference, unreachable: a list or dict as a request id, path id or attempt is
//! unhashable, so the oracle raises `TypeError` at its first dict use of it. No path delivers
//! one: every registration hashes the subject's `(request_id, path_id, path_attempt)` first
//! (:4018-4023, :4141-4147, :4208-4213), and the owner's attempt is an `int`
//! (physical_inference_node.py:3596-3597). The core never refuses one and compares it as above.
use crate::m16_router_contracts::{PyJson, PyReal, py_json_eq, py_json_same};
use crate::prefill_watch::MonotonicClock;
use crate::sidecar_pool::{MessageId, OutboundHandle, SidecarState};
use budgets::{BudgetId, Entries, Refusal};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

/// The reservation reason of an owner-fanned cancellation (:2772, :2842).
const PATH_CANCELLED: &str = "path_cancelled";

/// `PathCancellation` (contracts.py:361-365), each field the value the registry holds.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PathCancellation {
    pub request_id: PyJson,
    pub path_id: PyJson,
    pub path_attempt: PyJson,
    pub topology_version: PyJson,
}

impl PathCancellation {
    /// Its `(request_id, path_id, path_attempt)` is the dict key `(request_id, path_id,
    /// path_attempt)` (:1270, :1275-1279): a tuple key compares element by element.
    fn has_key(&self, request_id: &PyJson, path_id: &PyJson, path_attempt: &PyJson) -> bool {
        py_json_same(&self.request_id, request_id)
            && py_json_same(&self.path_id, path_id)
            && py_json_same(&self.path_attempt, path_attempt)
    }
}

/// The fields of `_PendingSend` (:749-765) this core reads or writes. The send path owns the
/// public ones; its scope is fixed at construction, and the cancellation flags are written only
/// by this core.
#[derive(Clone, Debug, PartialEq)]
pub struct PendingSend {
    request_id: PyJson,
    path_id: PyJson,
    path_attempt: PyJson,
    pub cancellable_forward: bool,
    pub admission_started: bool,
    pub completed: bool,
    cancelled: bool,
    reason: &'static str,
    cancel_started: bool,
    cancel_confirmed: bool,
}

impl PendingSend {
    /// `_PendingSend(request_id=..., path_id=..., path_attempt=...)` over the scope
    /// `_frame_scope` keeps of a frame's values (:1245-1249, `None` for an absent one): a `str`
    /// id, else `None`; an attempt whose type is `int` (a `bool` or `float` is not), else
    /// `None`. Every flag false and an empty reason.
    pub fn new(request_id: PyJson, path_id: PyJson, path_attempt: PyJson) -> Self {
        let str_or_none = |value: PyJson| match value {
            PyJson::Str(_) => value,
            _ => PyJson::None,
        };
        let path_attempt = match path_attempt {
            PyJson::Num(PyReal::Int { .. }) => path_attempt,
            _ => PyJson::None,
        };
        Self {
            request_id: str_or_none(request_id),
            path_id: str_or_none(path_id),
            path_attempt,
            cancellable_forward: false,
            admission_started: false,
            completed: false,
            cancelled: false,
            reason: "",
            cancel_started: false,
            cancel_confirmed: false,
        }
    }
    /// A `str` or `None`.
    pub fn request_id(&self) -> &PyJson {
        &self.request_id
    }
    /// A `str` or `None`.
    pub fn path_id(&self) -> &PyJson {
        &self.path_id
    }
    /// An `int` or `None`.
    pub fn path_attempt(&self) -> &PyJson {
        &self.path_attempt
    }
    pub fn cancelled(&self) -> bool {
        self.cancelled
    }
    pub fn reason(&self) -> &'static str {
        self.reason
    }
    pub fn cancel_started(&self) -> bool {
        self.cancel_started
    }
    pub fn cancel_confirmed(&self) -> bool {
        self.cancel_confirmed
    }
    /// The exact (request, path, attempt) equality of both sweeps (:2762-2767, :2833-2837):
    /// Python `==`, so an attempt `1.0` or `True` meets the `int` 1 and `None` meets `None`.
    fn exact(&self, subject: &PathCancellation) -> bool {
        py_json_eq(&self.request_id, &subject.request_id)
            && py_json_eq(&self.path_id, &subject.path_id)
            && py_json_eq(&self.path_attempt, &subject.path_attempt)
    }
}

/// A refusal of the core's construction.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SubjectCancelError {
    /// The transport constructor's `ValueError(message)` (:859-866).
    Value(&'static str),
    /// The ACK-cap budget row is not a duration.
    Budget(Refusal),
}

/// Why an apply raised.
#[derive(Debug)]
pub enum ApplyFault<E> {
    /// `IrohTransportError("delivery_cancel_worker_collision")` (:2906-2907): a reserved send's
    /// message id still has a registered worker. Raised under the lock, before the Router
    /// hand-off, so the path is not forgotten.
    WorkerCollision,
    /// The Router raised (:4402-4418); the path was forgotten all the same (:4426-4438).
    Router(E),
}

impl<E> ApplyFault<E> {
    /// The oracle's error code, `None` for the Router's own error.
    pub fn code(&self) -> Option<&'static str> {
        match self {
            Self::WorkerCollision => Some("delivery_cancel_worker_collision"),
            Self::Router(_) => None,
        }
    }
}

/// A raised apply: the fault and everything applied before it. The failing subject was
/// registered again for owner retry (:4327-4332).
#[derive(Debug)]
pub struct ApplyError<E> {
    pub fault: ApplyFault<E>,
    pub partial: ApplyReceipt,
}

/// One gate's verdict, for the public entry and for each subject.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Gate {
    /// Every gate passed: the subject was fenced, swept and handed to the Router.
    Admitted,
    /// `self._router is None` (:4308-4309, :4366-4367).
    NoRouter,
    /// The entry is known and this node is not a participant of the path (:4310-4313,
    /// :4368-4373).
    NotParticipant,
    /// A non-entry node was told `entry_cancelled=True` (:4314-4315, :4377-4378).
    EntryCancelOnNonEntry,
}

/// Which sweep met a send.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Sweep {
    /// `_cancel_forward_scope_locked` (:2761-2780), cancellable forwards only. Its workers get no
    /// cleanup deadline (:2780).
    CancellableForward,
    /// `_cancel_pending_scope_locked` (:2832-2850), every exact-subject send, with the cleanup
    /// deadline of the apply.
    ExactSubject,
}

/// What a sweep did to one exact-subject send (:2704-2719).
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Reservation {
    /// Newly reserved: `cancelled`, reason `path_cancelled` and `cancel_started` set, and one
    /// worker registered with this cleanup deadline.
    Reserved { cleanup_deadline: Option<f64> },
    /// Not reserved: the send has completed (:2712).
    Completed,
    /// Not reserved: its cancellation already started. No second worker; the running one keeps
    /// its deadline (:2713).
    AlreadyStarted,
    /// Reserved, but its message id still has a registered worker (:2906-2907): the apply raises
    /// [`ApplyFault::WorkerCollision`].
    WorkerCollision { cleanup_deadline: Option<f64> },
}

/// One send met by one sweep.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct SendSweep {
    pub message_id: MessageId,
    pub sweep: Sweep,
    pub reservation: Reservation,
}

/// The Router's answer (:4402-4418).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RouterAnswer {
    /// `router.apply_controlled_path_cancellation(cancellation)`.
    OwnerControl(bool),
    /// `router.receive_path_cancellation(cancellation, source_node_id=entry)`.
    EntryReceive(bool),
    /// Neither: no owner-control entry point and no known entry (:4417-4418).
    NoEntryPoint,
}

impl RouterAnswer {
    fn accepted(self) -> bool {
        match self {
            Self::OwnerControl(accepted) | Self::EntryReceive(accepted) => accepted,
            Self::NoEntryPoint => false,
        }
    }
}

/// One subject's apply.
#[derive(Clone, Debug, PartialEq)]
pub struct SubjectTrace {
    pub subject: PathCancellation,
    pub gate: Gate,
    /// The sends both sweeps met, in sweep then registration order.
    pub sends: Vec<SendSweep>,
    /// `None` when a gate refused or the Router raised.
    pub router: Option<RouterAnswer>,
    /// The Router's answer, or else exact `cancellation_observed` evidence (:4419-4424).
    pub accepted: bool,
}

/// A public apply: its result, the public gate, each subject in apply order and the workers it
/// registered. `accepted` says the owner request was taken, never that any cleanup finished.
#[derive(Debug)]
pub struct ApplyReceipt {
    pub accepted: bool,
    pub gate: Gate,
    pub subjects: Vec<SubjectTrace>,
    pub dispatched: Vec<DeliveryCancelDispatch>,
}

/// One registered delivery-cancel worker that has not started: the oracle's
/// `threading.Thread(args=(control, message_id, pending, cleanup_deadline_monotonic_s))`.
#[derive(Debug)]
pub struct DeliveryCancelDispatch {
    message_id: MessageId,
    record: u64,
    token: u64,
    cleanup_deadline: Option<f64>,
}

impl DeliveryCancelDispatch {
    pub fn message_id(&self) -> MessageId {
        self.message_id
    }
    pub fn cleanup_deadline(&self) -> Option<f64> {
        self.cleanup_deadline
    }
}

/// A worker's absolute deadline and remaining ACK window at its start (:2959-2976).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CancelWindow {
    pub started_at: f64,
    pub deadline: f64,
    pub ack_window: f64,
}

/// A started worker.
#[derive(Debug)]
pub enum WorkerStart {
    /// The send never reached admission: confirmed locally, no sidecar call (:2981-2990).
    LocallyConfirmed(CancelWindow),
    /// The send reached admission: the worker cancels its message id over the sidecar and
    /// reports the answer to [`SubjectCancel::finish_delivery_cancel`].
    Sidecar(RunningDeliveryCancel),
}

/// A worker that is cancelling one message id over the sidecar.
#[derive(Debug)]
pub struct RunningDeliveryCancel {
    message_id: MessageId,
    token: u64,
    window: CancelWindow,
}

impl RunningDeliveryCancel {
    pub fn message_id(&self) -> MessageId {
        self.message_id
    }
    pub fn window(&self) -> CancelWindow {
        self.window
    }
    /// The sidecar's half of this per-message cancel, through [`SidecarState::cancel_handle`]
    /// (sidecar.rs:2196-2218, which serves the worker's `control.cancel(message_id)`): it removes
    /// the exact token and queued item, makes `Cancelled` terminal and releases the permit, or
    /// finds no such message. This is the sidecar's action, never the transport's
    /// confirmation: the send stays a cleanup blocker until that answer reaches the worker and
    /// it finishes with [`CancelResult::Confirmed`].
    pub fn cancel_in_sidecar(&self, sidecar: &mut SidecarState) -> SidecarCancel {
        match sidecar.cancel_handle(self.message_id) {
            Some(handle) => SidecarCancel::Cancelled(handle),
            None => SidecarCancel::UnknownMessage,
        }
    }
}

/// The sidecar's own cancel outcome.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SidecarCancel {
    Cancelled(OutboundHandle),
    /// The sidecar's `unknown_message` (the worker's `"retry"`, :3141-3142).
    UnknownMessage,
}

/// The cancel answer that reached the worker (`confirmed = outcome is True`, :3021).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CancelResult {
    Confirmed,
    Unconfirmed,
}

/// A registered worker as the transport sees it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct WorkerView {
    pub cleanup_deadline: Option<f64>,
    /// `None` until the worker starts.
    pub window: Option<CancelWindow>,
}

/// The node Router the apply hands each fenced subject to.
pub trait OwnerRouter {
    type Error;
    /// `router.apply_controlled_path_cancellation(cancellation)` (:4403-4409), `None` when the
    /// Router has no such entry point.
    fn apply_controlled_path_cancellation(
        &mut self,
        cancellation: &PathCancellation,
    ) -> Option<Result<bool, Self::Error>>;
    /// `router.receive_path_cancellation(cancellation, source_node_id=entry)` (:4410-4416).
    fn receive_path_cancellation(
        &mut self,
        cancellation: &PathCancellation,
        source_node_id: &str,
    ) -> Result<bool, Self::Error>;
}

/// `_delivery_cancel_threads` entry.
struct Worker {
    token: u64,
    /// The `_PendingSend` object the thread was given, live or since removed.
    record: u64,
    cleanup_deadline: Option<f64>,
    window: Option<CancelWindow>,
}

/// A worker collision raised by a sweep.
struct Collision;

/// The transport state the controlled cancellation reads and writes.
pub struct SubjectCancel {
    node_id: String,
    delivery_timeout_seconds: f64,
    ack_cap_seconds: f64,
    history_capacity: usize,
    /// `_entry_nodes`: request id to entry node, in dict order.
    entry_nodes: Vec<(PyJson, String)>,
    /// `_participant_nodes_by_path`: path id to its nodes, in dict order.
    participants: Vec<(PyJson, BTreeSet<String>)>,
    /// `_path_subjects`, keyed by path id, in dict order.
    path_subjects: Vec<PathCancellation>,
    last_cancellation: Option<PathCancellation>,
    /// `_cancellations_by_subject`, oldest first.
    cancellations_by_subject: VecDeque<PathCancellation>,
    /// `_controlled_cancelled_requests`, oldest first.
    controlled_requests: VecDeque<PyJson>,
    /// Every `_PendingSend` object still reachable, live or held by an unstarted worker.
    records: BTreeMap<u64, PendingSend>,
    /// `_pending`: message id to its live object, in dict order.
    pending: Vec<(MessageId, u64)>,
    /// `_delivery_cancel_threads`, in registration order.
    workers: Vec<(MessageId, Worker)>,
    next_id: u64,
}

impl SubjectCancel {
    /// The transport's state for `node_id`, refused as its constructor refuses (:859-866): an
    /// empty node id, a zero queue capacity, or a delivery timeout `<= 0` (NaN and +inf pass, as
    /// they pass that comparison).
    pub fn new(
        node_id: &str,
        queue_capacity: usize,
        delivery_timeout_seconds: f64,
    ) -> Result<Self, SubjectCancelError> {
        if node_id.is_empty() {
            return Err(SubjectCancelError::Value(
                "node and expected endpoint ids must not be empty",
            ));
        }
        if queue_capacity == 0 {
            return Err(SubjectCancelError::Value("queue_capacity must be positive"));
        }
        if delivery_timeout_seconds <= 0.0 {
            return Err(SubjectCancelError::Value("timeouts must be positive"));
        }
        let cap = BudgetId::PyMyceliumRouterTransportsIrohDeliveryCancelAckTimeoutSecondsL75
            .span(Entries::ZERO)
            .map_err(SubjectCancelError::Budget)?;
        Ok(Self {
            node_id: node_id.to_owned(),
            delivery_timeout_seconds,
            // An integral millisecond count over 1000 rounds once, to the double nearest the
            // oracle's decimal literal (1000 / 1000 = 1.0 exactly).
            ack_cap_seconds: cap.millis() as f64 / 1000.0,
            history_capacity: queue_capacity,
            entry_nodes: Vec::new(),
            participants: Vec::new(),
            path_subjects: Vec::new(),
            last_cancellation: None,
            cancellations_by_subject: VecDeque::new(),
            controlled_requests: VecDeque::new(),
            records: BTreeMap::new(),
            pending: Vec::new(),
            workers: Vec::new(),
            next_id: 0,
        })
    }

    // ---- registries the transport's frame paths write ---------------------------------------
    /// `_entry_nodes[request_id] = node_id`: a Python-equal request id keeps its key and place.
    pub fn set_entry_node(&mut self, request_id: PyJson, node_id: &str) {
        set_key(&mut self.entry_nodes, request_id, node_id.to_owned());
    }
    /// `_entry_nodes.get(request_id)`.
    pub fn entry_node(&self, request_id: &PyJson) -> Option<&str> {
        get_key(&self.entry_nodes, request_id).map(String::as_str)
    }
    /// `_participant_nodes_by_path[path_id] = frozenset(nodes)`: a Python-equal path id keeps
    /// its key and place.
    pub fn set_participants<'a>(
        &mut self,
        path_id: PyJson,
        nodes: impl IntoIterator<Item = &'a str>,
    ) {
        set_key(
            &mut self.participants,
            path_id,
            nodes.into_iter().map(str::to_owned).collect(),
        );
    }
    /// `_participant_nodes_by_path.get(path_id)`.
    pub fn participants(&self, path_id: &PyJson) -> Option<&BTreeSet<String>> {
        get_key(&self.participants, path_id)
    }
    /// `_path_subjects[subject.path_id] = subject`: a Python-equal path id keeps its place.
    pub fn set_path_subject(&mut self, subject: PathCancellation) {
        match self
            .path_subjects
            .iter_mut()
            .find(|registered| py_json_same(&registered.path_id, &subject.path_id))
        {
            Some(registered) => *registered = subject,
            None => self.path_subjects.push(subject),
        }
    }
    /// The registered subjects, in registration order.
    pub fn path_subjects(&self) -> &[PathCancellation] {
        &self.path_subjects
    }
    /// `_pending[message_id] = send`: an existing id keeps its place and its previous object is
    /// detached (a worker that holds it still reads it).
    pub fn register_pending(&mut self, message_id: MessageId, send: PendingSend) {
        let record = self.next_id();
        self.records.insert(record, send);
        match self.pending.iter_mut().find(|(id, _)| *id == message_id) {
            Some((_, live)) => {
                let previous = std::mem::replace(live, record);
                self.release_record(previous);
            }
            None => self.pending.push((message_id, record)),
        }
    }
    pub fn pending(&self, message_id: &MessageId) -> Option<&PendingSend> {
        self.live(message_id)
            .and_then(|record| self.records.get(&record))
    }
    /// The live send for the send path's in-place admission/completion updates.
    pub fn pending_mut(&mut self, message_id: &MessageId) -> Option<&mut PendingSend> {
        let record = self.live(message_id)?;
        self.records.get_mut(&record)
    }
    /// `_pending.pop(message_id, None)`: an absent id is a no-op `None`.
    pub fn remove_pending(&mut self, message_id: &MessageId) -> Option<PendingSend> {
        let at = self.pending.iter().position(|(id, _)| id == message_id)?;
        let (_, record) = self.pending.remove(at);
        let send = self.records.get(&record).cloned();
        self.release_record(record);
        send
    }

    // ---- observers ---------------------------------------------------------------------------
    /// `cancellation_cleanup_state(...)["pending_delivery_count"]` (:1101-1132): the
    /// unconfirmed sends of the subject. Once the request is owner-cancelled the projection is
    /// request-wide, every path and attempt of it. A `None` attempt on either side matches any
    /// attempt; every other comparison is Python `==` (:1108-1119).
    pub fn pending_delivery_count(
        &self,
        request_id: &PyJson,
        path_id: &PyJson,
        path_attempt: &PyJson,
    ) -> usize {
        let request_wide = self.request_controlled_cancelled(request_id);
        self.pending
            .iter()
            .filter_map(|(_, record)| self.records.get(record))
            .filter(|send| {
                py_json_eq(&send.request_id, request_id)
                    && (request_wide || py_json_eq(&send.path_id, path_id))
                    && (request_wide
                        || *path_attempt == PyJson::None
                        || send.path_attempt == PyJson::None
                        || py_json_eq(&send.path_attempt, path_attempt))
                    && !send.cancel_confirmed
            })
            .count()
    }
    /// `cancellation_observed` (:1263-1272): exact bounded evidence, the tuple's dict lookup.
    pub fn cancellation_observed(
        &self,
        request_id: &PyJson,
        path_id: &PyJson,
        path_attempt: &PyJson,
    ) -> bool {
        self.cancellations_by_subject
            .iter()
            .any(|c| c.has_key(request_id, path_id, path_attempt))
    }
    /// `_request_controlled_cancelled_locked` (:1294-1298): never for a `None` request id.
    pub fn request_controlled_cancelled(&self, request_id: &PyJson) -> bool {
        *request_id != PyJson::None
            && self
                .controlled_requests
                .iter()
                .any(|r| py_json_same(r, request_id))
    }
    /// `_last_cancellation`.
    pub fn last_cancellation(&self) -> Option<&PathCancellation> {
        self.last_cancellation.as_ref()
    }
    /// The registered worker of a message id.
    pub fn delivery_cancel_worker(&self, message_id: &MessageId) -> Option<WorkerView> {
        self.workers
            .iter()
            .find(|(id, _)| id == message_id)
            .map(|(_, worker)| WorkerView {
                cleanup_deadline: worker.cleanup_deadline,
                window: worker.window,
            })
    }
    pub fn delivery_cancel_workers(&self) -> usize {
        self.workers.len()
    }

    // ---- the controlled cancellation ---------------------------------------------------------
    /// `apply_controlled_path_cancellation(cancellation, *, entry_cancelled,
    /// cleanup_deadline_monotonic_s=None)` (:4290-4335). Refused (`accepted` false, nothing
    /// changed) without a Router, when the entry is known and this node is not a participant of
    /// the path, or when a non-entry node is told `entry_cancelled`. Otherwise the request is
    /// remembered as owner-cancelled and each subject is applied: the named one, then every
    /// registered subject of the same request (a registered subject of the named path replaces
    /// the named one in its place). `accepted` is the OR of the subjects.
    pub fn apply_controlled_path_cancellation<R: OwnerRouter>(
        &mut self,
        mut router: Option<&mut R>,
        cancellation: &PathCancellation,
        entry_cancelled: bool,
        cleanup_deadline_monotonic_s: Option<f64>,
    ) -> Result<ApplyReceipt, ApplyError<R::Error>> {
        let mut receipt = ApplyReceipt {
            accepted: false,
            gate: Gate::Admitted,
            subjects: Vec::new(),
            dispatched: Vec::new(),
        };
        if router.is_none() {
            receipt.gate = Gate::NoRouter;
            return Ok(receipt);
        }
        if let Some(gate) = self.refused(cancellation, entry_cancelled) {
            receipt.gate = gate;
            return Ok(receipt);
        }
        self.remember_controlled(&cancellation.request_id);
        let mut subjects = vec![cancellation.clone()];
        for registered in &self.path_subjects {
            if !py_json_eq(&registered.request_id, &cancellation.request_id) {
                continue;
            }
            match subjects
                .iter_mut()
                .find(|subject| py_json_same(&subject.path_id, &registered.path_id))
            {
                Some(subject) => *subject = registered.clone(),
                None => subjects.push(registered.clone()),
            }
        }
        for subject in subjects {
            let (trace, applied) = self.apply_subject(
                router.as_deref_mut(),
                &subject,
                entry_cancelled,
                cleanup_deadline_monotonic_s,
                &mut receipt.dispatched,
            );
            match applied {
                Ok(()) => {
                    receipt.accepted = trace.accepted || receipt.accepted;
                    receipt.subjects.push(trace);
                }
                Err(fault) => {
                    receipt.subjects.push(trace);
                    // Retain the admitted subject for owner retry (:4327-4332).
                    self.set_path_subject(subject);
                    return Err(ApplyError {
                        fault,
                        partial: receipt,
                    });
                }
            }
        }
        Ok(receipt)
    }

    /// `_apply_controlled_path_cancellation` (:4337-4438) for one subject: its trace, and the
    /// fault when it raised.
    fn apply_subject<R: OwnerRouter>(
        &mut self,
        router: Option<&mut R>,
        subject: &PathCancellation,
        entry_cancelled: bool,
        cleanup_deadline: Option<f64>,
        dispatched: &mut Vec<DeliveryCancelDispatch>,
    ) -> (SubjectTrace, Result<(), ApplyFault<R::Error>>) {
        let mut trace = SubjectTrace {
            subject: subject.clone(),
            gate: Gate::Admitted,
            sends: Vec::new(),
            router: None,
            accepted: false,
        };
        let entry_node = get_key(&self.entry_nodes, &subject.request_id).cloned();
        let Some(router) = router else {
            trace.gate = Gate::NoRouter;
            return (trace, Ok(()));
        };
        if let Some(gate) = self.refused(subject, entry_cancelled) {
            trace.gate = gate;
            return (trace, Ok(()));
        }
        // The exact cancellation fence, published before the sweeps (:4379-4385).
        self.last_cancellation = Some(subject.clone());
        self.remember_cancellation(subject);
        self.remember_controlled(&subject.request_id);
        let swept = self
            .sweep(
                subject,
                Sweep::CancellableForward,
                None,
                &mut trace.sends,
                dispatched,
            )
            .and_then(|()| {
                self.sweep(
                    subject,
                    Sweep::ExactSubject,
                    cleanup_deadline,
                    &mut trace.sends,
                    dispatched,
                )
            });
        if swept.is_err() {
            return (trace, Err(ApplyFault::WorkerCollision));
        }
        let answer = match router.apply_controlled_path_cancellation(subject) {
            Some(result) => result.map(RouterAnswer::OwnerControl),
            None => match entry_node.as_deref() {
                Some(entry) => router
                    .receive_path_cancellation(subject, entry)
                    .map(RouterAnswer::EntryReceive),
                None => Ok(RouterAnswer::NoEntryPoint),
            },
        };
        let observed = self.cancellation_observed(
            &subject.request_id,
            &subject.path_id,
            &subject.path_attempt,
        );
        // `finally`: the registries are retired even when the Router raised (:4426-4438).
        self.forget_path(&subject.request_id, &subject.path_id);
        match answer {
            Ok(answer) => {
                trace.router = Some(answer);
                trace.accepted = answer.accepted() || observed;
                (trace, Ok(()))
            }
            Err(error) => (trace, Err(ApplyFault::Router(error))),
        }
    }

    /// The gates shared by the public entry and each subject (:4310-4315, :4363-4378); none
    /// applies while the request's entry is unknown.
    fn refused(&self, subject: &PathCancellation, entry_cancelled: bool) -> Option<Gate> {
        let entry = get_key(&self.entry_nodes, &subject.request_id)?;
        if get_key(&self.participants, &subject.path_id)
            .is_some_and(|nodes| !nodes.contains(&self.node_id))
        {
            return Some(Gate::NotParticipant);
        }
        if *entry != self.node_id && entry_cancelled {
            return Some(Gate::EntryCancelOnNonEntry);
        }
        None
    }

    /// One sweep over a snapshot of `_pending` (:2761-2780 with `cancellable_forward`,
    /// :2832-2850 without): reserve each exact-subject send (:2704-2719) and register one worker
    /// for each newly reserved one (:2898-2922).
    fn sweep(
        &mut self,
        subject: &PathCancellation,
        sweep: Sweep,
        cleanup_deadline: Option<f64>,
        sends: &mut Vec<SendSweep>,
        dispatched: &mut Vec<DeliveryCancelDispatch>,
    ) -> Result<(), Collision> {
        let live = self.pending.clone();
        for (message_id, record) in live {
            let Some(send) = self.records.get_mut(&record) else {
                continue;
            };
            if (sweep == Sweep::CancellableForward && !send.cancellable_forward)
                || !send.exact(subject)
            {
                continue;
            }
            let reservation = if send.completed {
                Reservation::Completed
            } else if send.cancel_started {
                Reservation::AlreadyStarted
            } else {
                send.cancelled = true;
                send.reason = PATH_CANCELLED;
                send.cancel_started = true;
                if self.workers.iter().any(|(id, _)| *id == message_id) {
                    sends.push(SendSweep {
                        message_id,
                        sweep,
                        reservation: Reservation::WorkerCollision { cleanup_deadline },
                    });
                    return Err(Collision);
                }
                let token = self.next_id();
                self.workers.push((
                    message_id,
                    Worker {
                        token,
                        record,
                        cleanup_deadline,
                        window: None,
                    },
                ));
                dispatched.push(DeliveryCancelDispatch {
                    message_id,
                    record,
                    token,
                    cleanup_deadline,
                });
                Reservation::Reserved { cleanup_deadline }
            };
            sends.push(SendSweep {
                message_id,
                sweep,
                reservation,
            });
        }
        Ok(())
    }

    // ---- delivery-cancel workers -------------------------------------------------------------
    /// The worker's head (:2959-2990), at its own clock read. With no cleanup deadline the ACK
    /// window is `min(ACK cap, delivery timeout)` and the deadline is the start plus it; a
    /// supplied deadline is the absolute deadline verbatim, with window `max(0, deadline -
    /// start)`. A send that never reached admission is confirmed locally and its worker
    /// unregistered; any other asks the sidecar.
    pub fn start_delivery_cancel(
        &mut self,
        dispatch: DeliveryCancelDispatch,
        clock: &impl MonotonicClock,
    ) -> WorkerStart {
        let started_at = clock.monotonic();
        let (deadline, ack_window) = match dispatch.cleanup_deadline {
            None => {
                // Python's `min(cap, timeout)`: the timeout only when strictly smaller.
                let window = if self.delivery_timeout_seconds < self.ack_cap_seconds {
                    self.delivery_timeout_seconds
                } else {
                    self.ack_cap_seconds
                };
                (started_at + window, window)
            }
            Some(deadline) => {
                // Python's `max(0.0, remaining)`: the remaining time only when strictly larger.
                let remaining = deadline - started_at;
                (deadline, if remaining > 0.0 { remaining } else { 0.0 })
            }
        };
        let window = CancelWindow {
            started_at,
            deadline,
            ack_window,
        };
        let current = self.current_worker(dispatch.message_id, dispatch.token);
        if let Some(at) = current {
            self.workers[at].1.window = Some(window);
        }
        let started = match self.records.get_mut(&dispatch.record) {
            Some(send) if !send.admission_started => {
                send.cancel_confirmed = true;
                if let Some(at) = current {
                    self.workers.remove(at);
                }
                WorkerStart::LocallyConfirmed(window)
            }
            _ => WorkerStart::Sidecar(RunningDeliveryCancel {
                message_id: dispatch.message_id,
                token: dispatch.token,
                window,
            }),
        };
        self.release_record(dispatch.record);
        started
    }

    /// The worker's `finally` (:3086-3101): a confirmed answer confirms the live send of the
    /// message id; the worker unregisters if it is still the registered one, and an unconfirmed
    /// answer then makes the live send eligible for a later sweep again (`cancel_started`
    /// false), still a cleanup blocker. A send removed meanwhile is not an error.
    pub fn finish_delivery_cancel(&mut self, running: RunningDeliveryCancel, result: CancelResult) {
        let confirmed = result == CancelResult::Confirmed;
        let live = self.live(&running.message_id);
        if confirmed && let Some(send) = live.and_then(|record| self.records.get_mut(&record)) {
            send.cancel_confirmed = true;
        }
        if let Some(at) = self.current_worker(running.message_id, running.token) {
            self.workers.remove(at);
            if !confirmed && let Some(send) = live.and_then(|record| self.records.get_mut(&record))
            {
                send.cancel_started = false;
            }
        }
    }

    // ---- internals ---------------------------------------------------------------------------
    fn next_id(&mut self) -> u64 {
        let id = self.next_id;
        self.next_id = self.next_id.wrapping_add(1);
        id
    }
    fn live(&self, message_id: &MessageId) -> Option<u64> {
        self.pending
            .iter()
            .find(|(id, _)| id == message_id)
            .map(|(_, record)| *record)
    }
    /// `self._delivery_cancel_threads.get(message_id) is current`.
    fn current_worker(&self, message_id: MessageId, token: u64) -> Option<usize> {
        self.workers
            .iter()
            .position(|(id, worker)| *id == message_id && worker.token == token)
    }
    /// Drops an object neither `_pending` nor an unstarted worker still holds.
    fn release_record(&mut self, record: u64) {
        let held = self.pending.iter().any(|(_, live)| *live == record)
            || self
                .workers
                .iter()
                .any(|(_, worker)| worker.record == record && worker.window.is_none());
        if !held {
            self.records.remove(&record);
        }
    }
    /// `_remember_cancellation_locked` (:1274-1283).
    fn remember_cancellation(&mut self, cancellation: &PathCancellation) {
        self.cancellations_by_subject.retain(|c| {
            !c.has_key(
                &cancellation.request_id,
                &cancellation.path_id,
                &cancellation.path_attempt,
            )
        });
        self.cancellations_by_subject
            .push_back(cancellation.clone());
        while self.cancellations_by_subject.len() > self.history_capacity {
            self.cancellations_by_subject.pop_front();
        }
    }
    /// `_remember_controlled_cancellation_locked` (:1285-1292).
    fn remember_controlled(&mut self, request_id: &PyJson) {
        self.controlled_requests
            .retain(|r| !py_json_same(r, request_id));
        self.controlled_requests.push_back(request_id.clone());
        while self.controlled_requests.len() > self.history_capacity {
            self.controlled_requests.pop_front();
        }
    }
    /// `_forget_path` (:4712-4719), without the wiring's path graphs.
    fn forget_path(&mut self, request_id: &PyJson, path_id: &PyJson) {
        self.participants
            .retain(|(key, _)| !py_json_same(key, path_id));
        self.path_subjects
            .retain(|subject| !py_json_same(&subject.path_id, path_id));
        if !self
            .path_subjects
            .iter()
            .any(|subject| py_json_eq(&subject.request_id, request_id))
        {
            self.entry_nodes
                .retain(|(key, _)| !py_json_same(key, request_id));
        }
    }
}

/// `d[key] = value` on a dict of decoded keys: a Python-equal key keeps its first spelling and
/// its place.
fn set_key<V>(entries: &mut Vec<(PyJson, V)>, key: PyJson, value: V) {
    match entries
        .iter_mut()
        .find(|(held, _)| py_json_same(held, &key))
    {
        Some((_, held)) => *held = value,
        None => entries.push((key, value)),
    }
}

/// `d.get(key)` on a dict of decoded keys.
fn get_key<'a, V>(entries: &'a [(PyJson, V)], key: &PyJson) -> Option<&'a V> {
    entries
        .iter()
        .find(|(held, _)| py_json_same(held, key))
        .map(|(_, value)| value)
}
