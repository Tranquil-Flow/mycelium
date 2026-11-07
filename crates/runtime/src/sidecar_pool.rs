// SPDX-License-Identifier: AGPL-3.0-only
//! Deterministic port of the shipped v0.1.4 sidecar Router state machine:
//! pooled outbound retry scheduling, per-item cancellation and terminals, and
//! inbound history/inflight redelivery. Source: `native/iroh_transport/src/
//! sidecar.rs @ f4e366e9` (cited per item below).
//!
//! This is the *scheduler and history*, with no socket, task or clock of its
//! own: the QUIC driver (connect/open/write/read with the 60-s stream and 5-s
//! connect caps) calls it with explicit [`MonoInstant`]s and reports each
//! attempt's outcome. It adds no recovery the sidecar does not have: there is
//! no retry-count limit (the item's enqueue-time lifetime is the only bound),
//! no membership recovery on peer rotation, and no replay persistence (history
//! is ephemeral process state, FIFO-bounded by the seen limit).
//!
//! Every numeric bound is supplied by the caller in [`PoolLimits`]; native L1
//! registry IDs for the sidecar bounds are still an open seam request, so no
//! equal-valued default is invented here.
use crate::iroh_wire::SendAttempt;
use budgets::{MonoInstant, Span};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

pub type MessageId = [u8; 16];
pub type EndpointKey = [u8; 32];
pub type Digest = [u8; 32];
/// QUIC application close/reset code for stream cancellation (`CANCEL_CODE`,
/// sidecar.rs:106). Cancels reset send and stop receive; the shared pooled
/// connection stays open.
pub const CANCEL_CODE: u32 = 7;
/// QUIC close code for pool eviction/rotation/duplicate (`REJECT_CODE`).
pub const REJECT_CODE: u32 = 8;
pub const REASON_RETRY: &[u8] = b"retry";
pub const REASON_DUPLICATE: &[u8] = b"duplicate";
pub const REASON_IDENTITY: &[u8] = b"identity";
pub const REASON_PEER_ROTATED: &[u8] = b"peer_rotated";

#[derive(Clone, Copy, Debug)]
pub struct PoolLimits {
    /// Outbound and inbound queue capacity (`DEFAULT_QUEUE_CAPACITY` shape).
    pub capacity: usize,
    /// `seen_limit = max(capacity.saturating_mul(seen_factor), seen_floor)`
    /// (sidecar.rs:1715: factor 8, floor 64 in the shipped constants).
    pub seen_factor: usize,
    pub seen_floor: usize,
    /// First retry delay; also the unbound-primary wait tick.
    pub initial_retry: Span,
    /// Retry delay ceiling; the delay doubles saturating up to this.
    pub max_retry: Span,
    /// One enqueue-time lifetime per item (`OUTBOUND_DELIVERY_DEADLINE`).
    pub lifetime: Span,
}
impl PoolLimits {
    pub fn seen_limit(&self) -> usize {
        self.capacity
            .saturating_mul(self.seen_factor)
            .max(self.seen_floor)
    }
}

/// Retry delay sequence: the current delay is used, then doubled saturating and
/// clamped to the maximum (sidecar.rs:2562-2706). The first delay is NOT
/// clamped, exactly like the source.
#[derive(Clone, Copy, Debug)]
pub struct Backoff {
    current: Span,
    max: Span,
}
impl Backoff {
    pub fn new(limits: &PoolLimits) -> Self {
        Self {
            current: limits.initial_retry,
            max: limits.max_retry,
        }
    }
    pub fn next_wait(&mut self) -> Span {
        let wait = self.current;
        let doubled = Span::from_millis(self.current.millis().saturating_mul(2));
        self.current = doubled.min(self.max);
        wait
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PeerBinding {
    pub endpoint: EndpointKey,
    pub generation: u64,
}
#[derive(Default)]
struct PeerTable {
    bindings: BTreeMap<EndpointKey, PeerBinding>,
    primary: Option<EndpointKey>,
}
impl PeerTable {
    fn primary(&self) -> Option<PeerBinding> {
        self.primary.and_then(|e| self.bindings.get(&e).copied())
    }
    fn matches(&self, endpoint: EndpointKey, generation: u64) -> bool {
        self.bindings
            .get(&endpoint)
            .is_some_and(|b| b.generation == generation)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OutboundTerminal {
    Delivered,
    PeerRotated,
    Cancelled,
    ReplayCollision,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub struct OutboundHandle(u64);
/// One outbound frame. `destination` names a routed peer (unknown fails
/// closed); `None` uses the primary binding.
#[derive(Clone, Debug)]
pub struct EnqueueRequest {
    pub message_id: MessageId,
    pub digest: Digest,
    pub payload: Vec<u8>,
    pub expected_generation: Option<u64>,
    pub source_generation: Option<u64>,
    pub destination: Option<EndpointKey>,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EnqueueOutcome {
    Queued(OutboundHandle),
    /// Same id and same digest shares the active control.
    Duplicate(OutboundHandle),
    ReplayCollision,
    PeerRotated,
    Full,
    /// Clock arithmetic/domain refusal (the source would panic on overflow).
    Refused,
}
struct OutboundControl {
    digest: Digest,
    cancelled: bool,
    permit: bool,
    generation: u64,
    terminal: Option<OutboundTerminal>,
    deadline: MonoInstant,
}
impl OutboundControl {
    /// First wins (sidecar.rs:1633 compare-exchange).
    fn finish(&mut self, terminal: OutboundTerminal) {
        if self.terminal.is_none() {
            self.terminal = Some(terminal);
        }
    }
}
#[derive(Clone, Debug)]
pub struct OutboundItem {
    pub message_id: MessageId,
    pub payload: Vec<u8>,
    pub source_generation: Option<u64>,
    pub target: Option<PeerBinding>,
    pub handle: OutboundHandle,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InboundTerminal {
    Acknowledged,
    PeerRotated,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub struct InboundHandle(u64);
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AdmissionOutcome {
    Admitted(InboundHandle),
    PendingDuplicate(InboundHandle),
    CompletedDuplicate,
    ReplayCollision,
    PeerRotated,
    Full,
}
struct InboundControl {
    digest: Digest,
    terminal: Option<InboundTerminal>,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct InboundDelivery {
    pub message_id: MessageId,
    pub source_endpoint: Option<EndpointKey>,
    pub generation: u64,
    pub payload: Vec<u8>,
}
struct InboundItem {
    delivery: InboundDelivery,
    control: InboundHandle,
}
#[derive(Default)]
struct InboundState {
    pending: VecDeque<InboundItem>,
    inflight: BTreeMap<u64, VecDeque<InboundItem>>,
    active: BTreeMap<MessageId, InboundHandle>,
    completed: BTreeMap<MessageId, Digest>,
    completed_order: VecDeque<MessageId>,
    rotated: BTreeSet<MessageId>,
    rotated_order: VecDeque<MessageId>,
}
impl InboundState {
    fn remember_completed(&mut self, id: MessageId, digest: Digest, limit: usize) {
        if self.completed.insert(id, digest).is_none() {
            self.completed_order.push_back(id);
        }
        while self.completed_order.len() > limit {
            if let Some(expired) = self.completed_order.pop_front() {
                self.completed.remove(&expired);
            }
        }
    }
    fn remember_rotated(&mut self, id: MessageId, limit: usize) {
        if self.rotated.insert(id) {
            self.rotated_order.push_back(id);
        }
        while self.rotated_order.len() > limit {
            if let Some(expired) = self.rotated_order.pop_front() {
                self.rotated.remove(&expired);
            }
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ConfigureRefusal {
    Empty,
    DuplicateEndpoint,
    /// An existing endpoint was re-bound at a generation not above its own.
    StaleGeneration,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ConfigureOutcome {
    /// False when an identical set was reinstalled (no fencing, no publish).
    pub changed: bool,
    /// First binding's generation, published to waiting connections/workers.
    pub generation: u64,
    /// Pooled connections the driver must close with [`REJECT_CODE`] and
    /// [`REASON_PEER_ROTATED`] (rotation fence; not a reconnect grant).
    pub closed_pool: Vec<(EndpointKey, u64)>,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PoolRegistration {
    Inserted,
    /// An existing live entry wins; the driver closes the NEW connection with
    /// [`REJECT_CODE`]/[`REASON_DUPLICATE`] and uses the existing one.
    Duplicate,
}

pub struct SidecarState {
    limits: PoolLimits,
    peers: PeerTable,
    next_handle: u64,
    controls: BTreeMap<OutboundHandle, OutboundControl>,
    tokens: BTreeMap<MessageId, OutboundHandle>,
    queue: VecDeque<OutboundItem>,
    outbound_free: usize,
    pool: BTreeSet<(EndpointKey, u64)>,
    inbound: InboundState,
    inbound_controls: BTreeMap<InboundHandle, InboundControl>,
    inbound_free: usize,
    published_generation: u64,
}
impl SidecarState {
    pub fn new(limits: PoolLimits) -> Self {
        Self {
            limits,
            peers: PeerTable::default(),
            next_handle: 0,
            controls: BTreeMap::new(),
            tokens: BTreeMap::new(),
            queue: VecDeque::new(),
            outbound_free: limits.capacity,
            pool: BTreeSet::new(),
            inbound: InboundState::default(),
            inbound_controls: BTreeMap::new(),
            inbound_free: limits.capacity,
            published_generation: 0,
        }
    }
    pub fn limits(&self) -> &PoolLimits {
        &self.limits
    }
    pub fn outbound_free(&self) -> usize {
        self.outbound_free
    }
    pub fn inbound_free(&self) -> usize {
        self.inbound_free
    }
    pub fn published_generation(&self) -> u64 {
        self.published_generation
    }
    pub fn peer_matches(&self, endpoint: EndpointKey, generation: u64) -> bool {
        self.peers.matches(endpoint, generation)
    }
    /// Current binding for one routed peer (the inbound admission lookup,
    /// sidecar.rs:2741-2750).
    pub fn peer_binding(&self, endpoint: EndpointKey) -> Option<PeerBinding> {
        self.peers.bindings.get(&endpoint).copied()
    }
    /// The primary binding (first of the last installed set) and every binding.
    pub fn peer_snapshot(&self) -> (Option<PeerBinding>, Vec<PeerBinding>) {
        (
            self.peers.primary(),
            self.peers.bindings.values().copied().collect(),
        )
    }
    /// False until the first successful `configure_peers`.
    pub fn peers_configured(&self) -> bool {
        !self.peers.bindings.is_empty()
    }
    fn handle(&mut self) -> u64 {
        self.next_handle += 1;
        self.next_handle
    }

    /// sidecar.rs:1897-1940. Atomic: any refusal leaves the table untouched.
    pub fn configure_peers(
        &mut self,
        ordered: Vec<PeerBinding>,
    ) -> Result<ConfigureOutcome, ConfigureRefusal> {
        let Some(first) = ordered.first().copied() else {
            return Err(ConfigureRefusal::Empty);
        };
        let mut seen = BTreeSet::new();
        for binding in &ordered {
            if !seen.insert(binding.endpoint) {
                return Err(ConfigureRefusal::DuplicateEndpoint);
            }
        }
        let occupied = !self.peers.bindings.is_empty();
        if occupied {
            let unchanged = self.peers.bindings.len() == ordered.len()
                && self.peers.primary() == Some(first)
                && ordered
                    .iter()
                    .all(|b| self.peers.bindings.get(&b.endpoint) == Some(b));
            if unchanged {
                return Ok(ConfigureOutcome {
                    changed: false,
                    generation: first.generation,
                    closed_pool: Vec::new(),
                });
            }
            for binding in &ordered {
                if let Some(existing) = self.peers.bindings.get(&binding.endpoint)
                    && binding.generation <= existing.generation
                {
                    return Err(ConfigureRefusal::StaleGeneration);
                }
            }
        }
        self.peers.primary = Some(first.endpoint);
        self.peers.bindings = ordered.into_iter().map(|b| (b.endpoint, b)).collect();
        let mut closed_pool = Vec::new();
        if occupied {
            self.fence_outbound_for_rotation();
            self.fence_inbound_for_rotation();
            closed_pool = std::mem::take(&mut self.pool).into_iter().collect();
        }
        self.published_generation = first.generation;
        Ok(ConfigureOutcome {
            changed: true,
            generation: first.generation,
            closed_pool,
        })
    }

    // ---- outbound (sidecar.rs:2137-2243) ---------------------------------
    pub fn enqueue(&mut self, request: EnqueueRequest, now: MonoInstant) -> EnqueueOutcome {
        let EnqueueRequest {
            message_id,
            digest,
            payload,
            expected_generation,
            source_generation,
            destination,
        } = request;
        let routed = match destination {
            Some(endpoint) => match self.peers.bindings.get(&endpoint) {
                Some(binding) => Some(*binding),
                None => return EnqueueOutcome::PeerRotated,
            },
            None => self.peers.primary(),
        };
        if expected_generation.is_some_and(|g| routed.map(|b| b.generation) != Some(g)) {
            return EnqueueOutcome::PeerRotated;
        }
        if let Some(handle) = self.tokens.get(&message_id).copied() {
            return match self.controls.get(&handle) {
                Some(control) if control.digest == digest => EnqueueOutcome::Duplicate(handle),
                _ => EnqueueOutcome::ReplayCollision,
            };
        }
        if self.outbound_free == 0 {
            return EnqueueOutcome::Full;
        }
        let Ok(deadline) = now.checked_add(self.limits.lifetime) else {
            return EnqueueOutcome::Refused;
        };
        self.outbound_free -= 1;
        let handle = OutboundHandle(self.handle());
        self.controls.insert(
            handle,
            OutboundControl {
                digest,
                cancelled: false,
                permit: true,
                generation: routed.map_or(0, |b| b.generation),
                terminal: None,
                deadline,
            },
        );
        self.tokens.insert(message_id, handle);
        self.queue.push_back(OutboundItem {
            message_id,
            payload,
            source_generation,
            target: routed,
            handle,
        });
        EnqueueOutcome::Queued(handle)
    }
    fn release_permit(&mut self, handle: OutboundHandle) {
        if let Some(control) = self.controls.get_mut(&handle)
            && control.permit
        {
            control.permit = false;
            self.outbound_free += 1;
        }
    }
    /// sidecar.rs:2196-2218: removes the exact token and queued item, makes
    /// `Cancelled` terminal (first wins), signals cancellation and releases the
    /// capacity permit before any worker traversal.
    pub fn cancel(&mut self, message_id: MessageId) -> bool {
        self.cancel_handle(message_id).is_some()
    }
    /// [`Self::cancel`], naming the cancelled control so a driver can wake the
    /// exact worker attempt that observes it.
    pub fn cancel_handle(&mut self, message_id: MessageId) -> Option<OutboundHandle> {
        let handle = self.tokens.remove(&message_id)?;
        if let Some(position) = self.queue.iter().position(|i| i.handle == handle) {
            self.queue.remove(position);
        }
        if let Some(control) = self.controls.get_mut(&handle) {
            control.finish(OutboundTerminal::Cancelled);
            control.cancelled = true;
        }
        self.release_permit(handle);
        Some(handle)
    }
    pub fn next_outbound(&mut self) -> Option<OutboundItem> {
        self.queue.pop_front()
    }
    pub fn terminal(&self, handle: OutboundHandle) -> Option<OutboundTerminal> {
        self.controls.get(&handle).and_then(|c| c.terminal)
    }
    pub fn is_cancelled(&self, handle: OutboundHandle) -> bool {
        self.controls.get(&handle).is_some_and(|c| c.cancelled)
    }
    fn finish_outbound(&mut self, item: &OutboundItem, terminal: OutboundTerminal) {
        if let Some(control) = self.controls.get_mut(&item.handle) {
            control.finish(terminal);
        }
        if self.tokens.get(&item.message_id) == Some(&item.handle) {
            self.tokens.remove(&item.message_id);
        }
        self.release_permit(item.handle);
    }
    fn bind_outbound(&mut self, handle: OutboundHandle) -> Option<PeerBinding> {
        let binding = self.peers.primary()?;
        let control = self.controls.get_mut(&handle)?;
        if control.generation == 0 {
            control.generation = binding.generation;
            Some(binding)
        } else if control.generation == binding.generation {
            Some(binding)
        } else {
            None
        }
    }
    /// sidecar.rs:1948-1966.
    fn fence_outbound_for_rotation(&mut self) {
        let stale: Vec<(MessageId, OutboundHandle)> =
            std::mem::take(&mut self.tokens).into_iter().collect();
        let ids: BTreeSet<MessageId> = stale.iter().map(|(id, _)| *id).collect();
        self.queue.retain(|item| !ids.contains(&item.message_id));
        for (_, handle) in stale {
            if let Some(control) = self.controls.get_mut(&handle) {
                control.finish(OutboundTerminal::PeerRotated);
                control.cancelled = true;
            }
            self.release_permit(handle);
        }
    }

    // ---- pooled connections keyed by (EndpointId, generation) -------------
    pub fn pool_contains(&self, key: (EndpointKey, u64)) -> bool {
        self.pool.contains(&key)
    }
    /// Diagnostic: the registered pool keys (the driver's connection map must hold the
    /// same keys at every quiescent point; `iroh_pool_tests` checks it).
    pub fn pool_keys(&self) -> Vec<(EndpointKey, u64)> {
        self.pool.iter().copied().collect()
    }
    /// sidecar.rs:2043-2050: a concurrent duplicate loses to the existing entry.
    pub fn pool_register(&mut self, key: (EndpointKey, u64)) -> PoolRegistration {
        if self.pool.insert(key) {
            PoolRegistration::Inserted
        } else {
            PoolRegistration::Duplicate
        }
    }
    /// True when an entry existed; the driver then closes that connection with
    /// [`REJECT_CODE`]/[`REASON_RETRY`].
    pub fn pool_evict(&mut self, key: (EndpointKey, u64)) -> bool {
        self.pool.remove(&key)
    }

    // ---- inbound (sidecar.rs:2244-2407) -----------------------------------
    pub fn admit_inbound(
        &mut self,
        origin: Option<(EndpointKey, u64)>,
        message_id: MessageId,
        digest: Digest,
        payload: Vec<u8>,
    ) -> AdmissionOutcome {
        let limit = self.limits.seen_limit();
        if let Some((endpoint, generation)) = origin
            && !self.peers.matches(endpoint, generation)
        {
            self.inbound.remember_rotated(message_id, limit);
            return AdmissionOutcome::PeerRotated;
        }
        if self.inbound.rotated.contains(&message_id) {
            return AdmissionOutcome::PeerRotated;
        }
        if let Some(handle) = self.inbound.active.get(&message_id).copied() {
            let same = self
                .inbound_controls
                .get(&handle)
                .is_some_and(|c| c.digest == digest);
            return if same {
                AdmissionOutcome::PendingDuplicate(handle)
            } else {
                AdmissionOutcome::ReplayCollision
            };
        }
        if let Some(completed) = self.inbound.completed.get(&message_id) {
            return if *completed == digest {
                AdmissionOutcome::CompletedDuplicate
            } else {
                AdmissionOutcome::ReplayCollision
            };
        }
        if self.inbound_free == 0 {
            return AdmissionOutcome::Full;
        }
        self.inbound_free -= 1;
        let handle = InboundHandle(self.handle());
        self.inbound_controls.insert(
            handle,
            InboundControl {
                digest,
                terminal: None,
            },
        );
        self.inbound.active.insert(message_id, handle);
        self.inbound.pending.push_back(InboundItem {
            delivery: InboundDelivery {
                message_id,
                source_endpoint: origin.map(|(e, _)| e),
                generation: origin.map_or(0, |(_, g)| g),
                payload,
            },
            control: handle,
        });
        AdmissionOutcome::Admitted(handle)
    }
    pub fn inbound_terminal(&self, handle: InboundHandle) -> Option<InboundTerminal> {
        self.inbound_controls.get(&handle).and_then(|c| c.terminal)
    }
    pub fn inbound_pending(&self) -> usize {
        self.inbound.pending.len()
    }
    /// Moves the pending front to this local session's inflight queue.
    pub fn receive(&mut self, session: u64) -> Option<InboundDelivery> {
        let item = self.inbound.pending.pop_front()?;
        let delivery = item.delivery.clone();
        self.inbound
            .inflight
            .entry(session)
            .or_default()
            .push_back(item);
        Some(delivery)
    }
    /// Only the session whose inflight queue holds the id may acknowledge it.
    pub fn ack(&mut self, session: u64, message_id: MessageId) -> bool {
        let limit = self.limits.seen_limit();
        let Some(items) = self.inbound.inflight.get_mut(&session) else {
            return false;
        };
        let Some(position) = items
            .iter()
            .position(|i| i.delivery.message_id == message_id)
        else {
            return false;
        };
        let Some(item) = items.remove(position) else {
            return false;
        };
        if items.is_empty() {
            self.inbound.inflight.remove(&session);
        }
        if self.inbound.active.get(&message_id) == Some(&item.control) {
            self.inbound.active.remove(&message_id);
        }
        if let Some(control) = self.inbound_controls.get_mut(&item.control) {
            let digest = control.digest;
            if control.terminal.is_none() {
                control.terminal = Some(InboundTerminal::Acknowledged);
            }
            self.inbound.remember_completed(message_id, digest, limit);
        }
        self.inbound_free += 1;
        true
    }
    /// Local hangup: this session's inflight items return to the FRONT of the
    /// pending queue in their original order (sidecar.rs:2397-2406).
    pub fn redeliver_session(&mut self, session: u64) {
        let Some(mut inflight) = self.inbound.inflight.remove(&session) else {
            return;
        };
        while let Some(item) = inflight.pop_back() {
            self.inbound.pending.push_front(item);
        }
    }
    /// sidecar.rs:1968-1984.
    fn fence_inbound_for_rotation(&mut self) {
        let limit = self.limits.seen_limit();
        let active: Vec<(MessageId, InboundHandle)> = std::mem::take(&mut self.inbound.active)
            .into_iter()
            .collect();
        let completed: Vec<MessageId> = std::mem::take(&mut self.inbound.completed)
            .into_keys()
            .collect();
        self.inbound.completed_order.clear();
        let dropped = self.inbound.pending.len()
            + self
                .inbound
                .inflight
                .values()
                .map(VecDeque::len)
                .sum::<usize>();
        self.inbound.pending.clear();
        self.inbound.inflight.clear();
        self.inbound_free = (self.inbound_free + dropped).min(self.limits.capacity);
        for (id, _) in &active {
            self.inbound.remember_rotated(*id, limit);
        }
        for id in completed {
            self.inbound.remember_rotated(id, limit);
        }
        for (_, handle) in active {
            if let Some(control) = self.inbound_controls.get_mut(&handle)
                && control.terminal.is_none()
            {
                control.terminal = Some(InboundTerminal::PeerRotated);
            }
        }
    }
}

/// Result of one send attempt as reported by the QUIC driver.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AttemptResult {
    Delivered,
    /// The item's cancellation fired during connect/open/write/read. Write/
    /// read cancellation resets send and stops receive with [`CANCEL_CODE`]
    /// and leaves the shared connection open.
    Cancelled,
    PeerRotated,
    ReplayCollision,
    Retry {
        evict_connection: bool,
    },
}
impl AttemptResult {
    /// Connect, open, write, FIN, read and timeout failures evict the pool
    /// entry and retry (sidecar.rs:2578-2664).
    pub const fn io_failure() -> Self {
        Self::Retry {
            evict_connection: true,
        }
    }
}
impl From<SendAttempt> for AttemptResult {
    fn from(value: SendAttempt) -> Self {
        match value {
            SendAttempt::Delivered => Self::Delivered,
            SendAttempt::PeerRotated => Self::PeerRotated,
            SendAttempt::ReplayCollision => Self::ReplayCollision,
            SendAttempt::Retry { evict_connection } => Self::Retry { evict_connection },
        }
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Step {
    /// The worker has finished the item; capacity is released and the token
    /// removed. The value is the control's (first-wins) terminal.
    Finish(OutboundTerminal),
    /// Try one attempt on this binding's pooled connection. The attempt itself
    /// is bounded by the item's deadline (`timeout_at`); report expiry with
    /// [`OutboundRun::attempt_deadline_elapsed`].
    Attempt(PeerBinding),
    /// No usable primary binding: wake on peer change, cancellation, `wake_at`
    /// or the item's deadline, then call [`OutboundRun::advance`].
    WaitForBinding { wake_at: MonoInstant },
    /// Retry wait (doubling): wake on peer change, cancellation, `wake_at` or
    /// the deadline, then call [`OutboundRun::advance`].
    Backoff { wake_at: MonoInstant },
}

/// One outbound worker item (`outbound_worker`, sidecar.rs:2530-2594).
pub struct OutboundRun {
    item: OutboundItem,
    backoff: Backoff,
    bound: Option<PeerBinding>,
    deadline: MonoInstant,
    attempting: Option<PeerBinding>,
    initial_retry: Span,
}
impl OutboundRun {
    pub fn start(state: &SidecarState, item: OutboundItem) -> Option<Self> {
        let deadline = state.controls.get(&item.handle)?.deadline;
        Some(Self {
            bound: item.target,
            backoff: Backoff::new(&state.limits),
            deadline,
            attempting: None,
            initial_retry: state.limits.initial_retry,
            item,
        })
    }
    pub fn item(&self) -> &OutboundItem {
        &self.item
    }
    /// The ENQUEUE-time deadline bounding every attempt of this item.
    pub fn deadline(&self) -> MonoInstant {
        self.deadline
    }
    fn finish(&self, state: &mut SidecarState, terminal: OutboundTerminal) -> Step {
        state.finish_outbound(&self.item, terminal);
        Step::Finish(state.terminal(self.item.handle).unwrap_or(terminal))
    }
    fn earliest(&self, now: MonoInstant, wait: Span) -> MonoInstant {
        now.checked_add(wait)
            .and_then(|at| at.earliest(self.deadline))
            .unwrap_or(self.deadline)
    }
    /// Top of the worker loop. The deadline is the ENQUEUE-time one: a retry
    /// never renews it.
    pub fn advance(&mut self, state: &mut SidecarState, now: MonoInstant) -> Step {
        if state.is_cancelled(self.item.handle) {
            return self.finish(state, OutboundTerminal::Cancelled);
        }
        if self.deadline.remaining(now).is_err() {
            return self.finish(state, OutboundTerminal::Cancelled);
        }
        let peer = match self.bound {
            Some(peer) => peer,
            None => match state.bind_outbound(self.item.handle) {
                Some(peer) => {
                    self.bound = Some(peer);
                    peer
                }
                None => {
                    return Step::WaitForBinding {
                        wake_at: self.earliest(now, self.initial_retry),
                    };
                }
            },
        };
        if !state.peer_matches(peer.endpoint, peer.generation) {
            return self.finish(state, OutboundTerminal::PeerRotated);
        }
        self.attempting = Some(peer);
        Step::Attempt(peer)
    }
    /// The outer `timeout_at(deadline, attempt)` elapsed.
    pub fn attempt_deadline_elapsed(&mut self, state: &mut SidecarState) -> Step {
        self.attempting = None;
        self.finish(state, OutboundTerminal::Cancelled)
    }
    pub fn attempted(
        &mut self,
        state: &mut SidecarState,
        now: MonoInstant,
        result: AttemptResult,
    ) -> Step {
        let peer = self.attempting.take();
        match result {
            AttemptResult::Delivered => self.finish(state, OutboundTerminal::Delivered),
            AttemptResult::Cancelled => self.finish(state, OutboundTerminal::Cancelled),
            AttemptResult::PeerRotated => self.finish(state, OutboundTerminal::PeerRotated),
            AttemptResult::ReplayCollision => self.finish(state, OutboundTerminal::ReplayCollision),
            AttemptResult::Retry { evict_connection } => {
                if evict_connection && let Some(peer) = peer {
                    state.pool_evict((peer.endpoint, peer.generation));
                }
                let wait = self.backoff.next_wait();
                Step::Backoff {
                    wake_at: self.earliest(now, wait),
                }
            }
        }
    }
}
