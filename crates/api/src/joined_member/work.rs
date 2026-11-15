// SPDX-License-Identifier: AGPL-3.0-only
//! The joined member's work service: receive a stage shard, prepare the native worker, serve route
//! epochs, leave, and the member's own standing-consent decision and status. Moved unchanged out of
//! the operator example `examples/shard_member` (its `control.rs`, `remote.rs`, `serve.rs`,
//! `leave.rs` and the command handlers of `shard_member.rs`), which now calls this module, so the
//! example and the packaged member run one copy.
//!
//! Operator control is independent of blocking receive/native work. Origins (the private stdin of
//! the example, and the owner's signed remote control channel) feed ONE serial operation queue
//! ([`Dispatcher`]); a strictly increasing operation_id binds cancellation and result retrieval to
//! one operation; long operations must carry one. The member side of the signed owner<->member
//! control channel ([`control_loop`]) accepts ONLY the pinned owner control endpoint, verifies every
//! signed command (pin, member endpoint, sequence, window), feeds the same queue, and signs replies
//! with the member's app (enrolled) key. `status` and `result` answer immediately, even while a long
//! operation runs.
//!
//! Network choice: this module binds nothing and reads no interface address. The caller binds the
//! carrier and control hosts and passes the carrier choice in as one value (the LAN policy, or none
//! for loopback), which is handed through unchanged.
use crate::{
    app_host,
    compiler_inputs::Error,
    joined_shard::{self, JoinedShard, native::NativeShard, serve as stage},
    leave::{ComputeCleanup, Steps},
    managed_carrier::HostClock,
    member::Session,
    member_control,
    preferences::{resource_policy, standing_consent},
    process_identity::{Incarnation, Liveness},
    run_exchange::owner_pin::{OwnerPin, PinRefusal, VerifiedAnnouncement},
};
use budgets::{BudgetId as B, Entries, MonoInstant};
use protocol::{artifact_grant::ChunkRequest, identity::Identity};
use runtime::{
    iroh_control_receive::NodeControlReceiver,
    iroh_host::{LanPolicy, LoopbackHost, NetworkMode},
    sidecar_artifact::serve_limits,
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
    sync::{
        Arc, LockResult, Mutex, MutexGuard,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
use transport::{Channel, Clock, ConnectionFence};
pub type Result<T> = std::result::Result<T, String>;

// ---- The serial operation queue (from examples/shard_member/control.rs) ----

/// Operations that block the member for long: each needs its own operation_id.
pub const LONG: &[&str] = &["receive", "prepare_native", "native_step", "serve"];
/// Finished remote results kept for retrieval (oldest dropped first).
const KEPT_RESULTS: usize = 16;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Origin {
    Stdin,
    Remote,
}
type Active = Arc<Mutex<Option<(Option<u64>, Arc<AtomicBool>)>>>;
/// Cancels the active operation when dropped (the local operator's reader ended).
pub struct CancelOnExit(Active);
impl Drop for CancelOnExit {
    fn drop(&mut self) {
        match self.0.lock() {
            Ok(active) => {
                if let Some((_, cancel)) = active.as_ref() {
                    cancel.store(true, Ordering::SeqCst);
                }
            }
            Err(_) => eprintln!("operator_state_poisoned"),
        }
    }
}
pub struct Operation {
    pub request: Value,
    pub cancelled: Arc<AtomicBool>,
    pub deadline: Instant,
    pub origin: Origin,
    pub id: Option<u64>,
}
impl Operation {
    pub fn check(&self) -> Result<()> {
        if self.cancelled.load(Ordering::SeqCst) {
            Err("member_operation_cancelled".into())
        } else if Instant::now() >= self.deadline {
            Err("member_operation_deadline".into())
        } else {
            Ok(())
        }
    }
}
/// Queue admission shared by both origins.
pub struct Dispatcher {
    /// Dropped on stdin EOF when stdin is the only origin (owner gone: exit).
    tx: Mutex<Option<mpsc::SyncSender<Result<Operation>>>>,
    active: Active,
    last_id: Mutex<u64>,
    results: Mutex<BTreeMap<u64, Value>>,
    wall: Duration,
    closing: AtomicBool,
}
impl Dispatcher {
    /// The queue (one slot) and its receiving end; every operation's wall is the member's
    /// operation wall.
    pub fn new() -> Result<(Arc<Self>, mpsc::Receiver<Result<Operation>>)> {
        let wall = Duration::from_millis(
            B::MemberChosenWall
                .span(Entries::ZERO)
                .map_err(|_| "budget_unavailable")?
                .millis(),
        );
        let (tx, requests) = mpsc::sync_channel(1);
        Ok((
            Arc::new(Self {
                tx: Mutex::new(Some(tx)),
                active: Arc::new(Mutex::new(None)),
                last_id: Mutex::new(0),
                results: Mutex::new(BTreeMap::new()),
                wall,
                closing: AtomicBool::new(false),
            }),
            requests,
        ))
    }
    /// Cancellation of the active operation with exactly this id.
    pub fn cancel(&self, id: Option<u64>) -> Result<Value> {
        let current = self.active.lock().map_err(|_| "operator_state_poisoned")?;
        let matched = id.is_some() && current.as_ref().is_some_and(|(held, _)| *held == id);
        if let Some((_, flag)) = current.as_ref().filter(|_| matched) {
            flag.store(true, Ordering::SeqCst);
        }
        Ok(json!({"operation_id":id,"cancel_requested":matched,"cleanup_proven":false}))
    }
    /// Admit one operation; `Err(code)` is an immediate refusal reply.
    pub fn admit(&self, request: Value, origin: Origin) -> std::result::Result<(), &'static str> {
        let command = request["command"]
            .as_str()
            .ok_or("operator_command_invalid")?;
        if self.closing.load(Ordering::SeqCst) {
            return Err("operator_closing");
        }
        let mut current = self.active.lock().map_err(|_| "operator_state_poisoned")?;
        if current.is_some() {
            return Err("operator_busy");
        }
        let id = request.get("operation_id").map(|v| v.as_u64());
        // Long operations must be individually cancellable (F6); remote results
        // are retrieved by id, so every remote operation carries one.
        if id.is_none() && (LONG.contains(&command) || origin == Origin::Remote) {
            return Err("operator_operation_id_required");
        }
        let mut last = self.last_id.lock().map_err(|_| "operator_state_poisoned")?;
        if id.is_some_and(|id| id.is_none_or(|id| id <= *last)) {
            return Err("operator_operation_id_invalid");
        }
        let id = id.flatten();
        if let Some(id) = id {
            *last = id;
        }
        let cancelled = Arc::new(AtomicBool::new(false));
        *current = Some((id, cancelled.clone()));
        if command == "close" {
            self.closing.store(true, Ordering::SeqCst);
        }
        self.tx
            .lock()
            .map_err(|_| "operator_state_poisoned")?
            .as_ref()
            .ok_or("operator_closing")?
            .try_send(Ok(Operation {
                request,
                cancelled,
                deadline: Instant::now() + self.wall,
                origin,
                id,
            }))
            .map_err(|_| "operator_dispatch_failed")
    }
    /// Remote result retrieval: the finished reply (removed), or pending.
    pub fn result(&self, id: u64) -> Result<Value> {
        let mut results = self.results.lock().map_err(|_| "operator_state_poisoned")?;
        Ok(match results.remove(&id) {
            Some(reply) => json!({"operation_id":id,"finished":true,"reply":reply}),
            None => json!({"operation_id":id,"finished":false}),
        })
    }
    pub fn active_id(&self) -> Option<u64> {
        self.active
            .lock()
            .ok()
            .and_then(|a| a.as_ref().and_then(|(id, _)| *id))
    }
    /// A guard that cancels the active operation when the local operator's reader ends.
    pub fn cancel_on_exit(&self) -> CancelOnExit {
        CancelOnExit(self.active.clone())
    }
    /// Cancel whatever operation is active (the local operator is gone).
    pub fn cancel_active(&self) -> Result<()> {
        if let Some((_, cancel)) = self
            .active
            .lock()
            .map_err(|_| "operator_state_poisoned")?
            .as_ref()
        {
            cancel.store(true, Ordering::SeqCst);
        }
        Ok(())
    }
    /// The local operator is gone: end the operation loop.
    pub fn close_input(&self) -> Result<()> {
        self.tx
            .lock()
            .map_err(|_| "operator_state_poisoned")?
            .take();
        Ok(())
    }
    /// `close` was admitted.
    pub fn closing(&self) -> bool {
        self.closing.load(Ordering::SeqCst)
    }
    /// Deliver an operation's reply to its origin (`local` answers the stdin origin), then free
    /// the queue slot.
    pub fn complete(
        &self,
        operation: &Operation,
        out: Value,
        local: impl FnOnce(&Value) -> Result<()>,
    ) -> Result<()> {
        if operation.origin == Origin::Remote {
            let mut results = self.results.lock().map_err(|_| "operator_state_poisoned")?;
            if let Some(id) = operation.id {
                results.insert(id, out);
            }
            while results.len() > KEPT_RESULTS {
                results.pop_first();
            }
        } else {
            local(&out)?;
        }
        self.active
            .lock()
            .map_err(|_| "operator_state_poisoned")?
            .take();
        Ok(())
    }
}

// ---- Member side of the signed control channel (from examples/shard_member/remote.rs) ----

/// `now + budget` on the member's monotonic clock.
pub fn until(clock: &HostClock, id: B) -> Result<MonoInstant> {
    clock
        .monotonic()
        .checked_add(id.span(Entries::ZERO).map_err(|_| "budget_unavailable")?)
        .map_err(|_| "deadline_invalid".into())
}
/// Live (non-blocking) view of member state for `status`.
pub type Status = Arc<Live>;
/// The status view plus the exact worker incarnations THIS member started.
pub struct Live {
    view: Mutex<Value>,
    workers: Mutex<BTreeMap<u32, Incarnation>>,
}
impl Live {
    pub fn new(view: Value) -> Status {
        Arc::new(Self {
            view: Mutex::new(view),
            workers: Mutex::new(BTreeMap::new()),
        })
    }
    pub fn lock(&self) -> LockResult<MutexGuard<'_, Value>> {
        self.view.lock()
    }
    /// Bind `pid` to its incarnation. Call only while this member still owns
    /// the unreaped worker child, so the pid cannot name another process yet.
    pub fn record_worker(&self, pid: Option<u32>) {
        let Some(pid) = pid else { return };
        match Incarnation::of(pid) {
            Ok(Some(now)) => {
                if let Ok(mut workers) = self.workers.lock() {
                    workers.insert(pid, now);
                }
            }
            // A worker that already exited is gone; nothing to bind.
            Ok(None) => {}
            Err(e) => eprintln!("member_worker_identity_unavailable: {e}"),
        }
    }
    /// `true`/`false` only for a worker incarnation this member started;
    /// `null` for any other pid (not ours: no claim) or an unanswerable query.
    fn liveness(&self, pid: u64) -> Value {
        let recorded = u32::try_from(pid)
            .ok()
            .and_then(|p| self.workers.lock().ok()?.get(&p).copied());
        match recorded.map(|i| i.liveness()) {
            Some(Liveness::Alive) => json!(true),
            Some(Liveness::Gone) => json!(false),
            Some(Liveness::Unknown) | None => Value::Null,
        }
    }
}
/// Whom the member's control endpoint obeys.
pub enum OwnerTrust {
    /// The operator's out-of-band pin (`shard_member --owner-control`), like a join link.
    Operator(iroh::EndpointId),
    /// The pin of the latest owner-signed run announcement this member verified.
    Announced(Pins),
}
/// The member's current announced owner pin, shared between its announcement reader and its
/// control endpoint.
#[derive(Clone, Default)]
pub struct Pins(Arc<Mutex<Option<OwnerPin>>>);
impl Pins {
    pub fn new() -> Self {
        Self::default()
    }
    /// Take the pin of a newly verified announcement; a verified announcement without a valid pin
    /// clears the held one (fail closed) and names why.
    pub fn repin(
        &self,
        verified: &VerifiedAnnouncement,
    ) -> std::result::Result<OwnerPin, PinRefusal> {
        let pin = OwnerPin::from_announcement(verified);
        *self.held() = pin.as_ref().ok().cloned();
        pin
    }
    /// The pin held now, if any.
    pub fn current(&self) -> Option<OwnerPin> {
        self.held().clone()
    }
    fn held(&self) -> MutexGuard<'_, Option<OwnerPin>> {
        // A pin is replaced whole; a panicked holder cannot leave a half-written one.
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}
impl OwnerTrust {
    /// The owner control key obeyed now; `owner_pin_missing` when no announced pin is held.
    pub fn owner(&self) -> Result<iroh::EndpointId> {
        match self {
            Self::Operator(owner) => Ok(*owner),
            Self::Announced(pins) => pins
                .current()
                .map(|pin| pin.endpoint())
                .ok_or_else(|| "owner_pin_missing".into()),
        }
    }
}
/// Admission of one signed command that arrived on a control connection from `remote`: the
/// connection's TLS-pinned key and the command's signing key must both be the owner key obeyed
/// now (`member_control::verify_command`'s codes; `owner_pin_missing` without a pin).
pub fn admit_command(
    trust: &OwnerTrust,
    remote: iroh::EndpointId,
    envelope: &Value,
    me: &iroh::EndpointId,
    last: u64,
    now: f64,
) -> Result<(u64, Value)> {
    let owner = trust.owner()?;
    if remote != owner {
        return Err("member_control_owner_unpinned".into());
    }
    member_control::verify_command(envelope, &owner, me, last, now).map_err(|e| e.code.to_owned())
}
/// The accept/verify/dispatch loop of the member's control endpoint, on a host the caller bound
/// (and, in direct-LAN mode, admitted the operator's owner key on). Runs until `stop` is set.
#[allow(clippy::too_many_arguments)]
pub fn control_loop(
    rt: &tokio::runtime::Runtime,
    clock: &HostClock,
    host: &mut LoopbackHost<'_>,
    trust: &OwnerTrust,
    me: iroh::EndpointId,
    dispatcher: &Dispatcher,
    signer: &Identity,
    status: &Status,
    stop: &AtomicBool,
) -> Result<()> {
    // Lane FLK: the member says when its control endpoint starts accepting, refuses a
    // connection, is away from its accept long enough for an owner's connect to time out, and
    // stops accepting (one line each, with the reason; public short ids only).
    let short = me.fmt_short();
    match host.address() {
        Ok(address) => eprintln!(
            "member_control_accepting endpoint={short} mode={:?} sockets={:?}",
            host.mode(),
            address.ip_addrs().collect::<Vec<_>>()
        ),
        Err(e) => eprintln!(
            "member_control_accepting endpoint={short} mode={:?} address={e:?}",
            host.mode()
        ),
    }
    let ended = control_loop_inner(rt, clock, host, trust, me, dispatcher, signer, status, stop);
    match &ended {
        Ok(()) => eprintln!("member_control_stopped endpoint={short} reason=stop_requested"),
        Err(code) => eprintln!("member_control_stopped endpoint={short} reason={code}"),
    }
    ended
}
/// A served connection that kept the loop away from its accept for longer than one owner poll
/// (the owner's connect allowance is `SidecarLocalHandshake`): what it was doing, phase by phase.
struct Away {
    command: String,
    receive: Duration,
    handle: Duration,
    respond: Duration,
    close_wait: Duration,
    closed_by_peer: bool,
}
#[allow(clippy::too_many_arguments)]
fn control_loop_inner(
    rt: &tokio::runtime::Runtime,
    clock: &HostClock,
    host: &mut LoopbackHost<'_>,
    trust: &OwnerTrust,
    me: iroh::EndpointId,
    dispatcher: &Dispatcher,
    signer: &Identity,
    status: &Status,
    stop: &AtomicBool,
) -> Result<()> {
    let short = me.fmt_short();
    let report_after = Duration::from_millis(
        B::OwnerPoll
            .span(Entries::ZERO)
            .map_err(|_| "budget_unavailable")?
            .millis(),
    );
    let mut last = 0u64;
    let mut admitted: Option<iroh::EndpointId> = None;
    // Direct LAN: the receive filter's drops and the pre-send filter's refusals, reported when
    // they change (an owner Initial dropped before iroh is a connect that can only time out).
    let mut filtered = (0u64, 0u64);
    while !stop.load(Ordering::SeqCst) {
        if let (Some(dropped), Some(refused)) =
            (host.lan_dropped_datagrams(), host.lan_refused_datagrams())
            && (dropped, refused) != filtered
        {
            eprintln!(
                "member_control_filter endpoint={short} dropped_in={dropped} (+{}) refused_out={refused} (+{}) learned={:?}",
                dropped.saturating_sub(filtered.0),
                refused.saturating_sub(filtered.1),
                host.lan_learned_hosts()
            );
            filtered = (dropped, refused);
        }
        // Direct LAN: the transport admits only the announced key; a new pin replaces it. A
        // cleared pin keeps the last key at the transport while no command is admitted.
        if let OwnerTrust::Announced(_) = trust
            && host.mode() == NetworkMode::DirectLan
            && let Ok(owner) = trust.owner()
            && admitted != Some(owner)
        {
            host.admit_peer_ids(&[owner])
                .map_err(|e| format!("member_control_admission_failed:{e:?}"))?;
            admitted = Some(owner);
        }
        // Bounded accept so the stop flag is observed within one owner poll.
        let connection = match host.accept(clock, until(clock, B::OwnerPoll)?) {
            Ok(connection) => connection,
            Err(transport::Error::Expired) => continue,
            Err(e) => {
                eprintln!("member_control_refused endpoint={short} reason=accept_{e:?}");
                continue;
            }
        };
        let served = Instant::now();
        let Ok(owner) = trust.owner() else {
            eprintln!(
                "member_control_refused endpoint={short} from={} reason=no_owner_pin",
                connection.remote_id().fmt_short()
            );
            connection.close(2u32.into(), b"no owner pin");
            continue;
        };
        if connection.remote_id() != owner {
            eprintln!(
                "member_control_refused endpoint={short} from={} reason=unpinned_control_owner",
                connection.remote_id().fmt_short()
            );
            connection.close(2u32.into(), b"unpinned control owner");
            continue;
        }
        let fence = ConnectionFence {
            endpoint_id: *owner.as_bytes(),
            local_generation: 0,
            remote_generation: 0,
        };
        let Ok(mut receiver) =
            NodeControlReceiver::new(connection.clone(), rt, clock, fence, || fence)
        else {
            eprintln!(
                "member_control_refused endpoint={short} from={} reason=control_channel_refused",
                connection.remote_id().fmt_short()
            );
            connection.close(2u32.into(), b"control channel refused");
            continue;
        };
        let mut away = Away {
            command: String::new(),
            receive: Duration::ZERO,
            handle: Duration::ZERO,
            respond: Duration::ZERO,
            close_wait: Duration::ZERO,
            closed_by_peer: false,
        };
        // The owner client makes one connection per call.
        let received = receiver.receive(until(clock, B::RouterClose)?);
        away.receive = served.elapsed();
        if let Ok(delivery) = received {
            let command: Value = serde_json::from_slice(delivery.bytes()).unwrap_or(Value::Null);
            let handling = Instant::now();
            let answer = match admit_command(
                trust,
                connection.remote_id(),
                &command,
                &me,
                last,
                app_host::now(),
            ) {
                Ok((sequence, request)) => {
                    last = sequence;
                    away.command = request["command"].as_str().unwrap_or_default().to_owned();
                    handle(dispatcher, status, request)
                }
                Err(code) => {
                    away.command = format!("refused:{code}");
                    json!({"ok":false,"code":code})
                }
            };
            away.handle = handling.elapsed();
            let responding = Instant::now();
            match member_control::sign_reply(signer, &command, answer)
                .ok()
                .and_then(|r| serde_json::to_vec(&r).ok())
            {
                Some(bytes) => {
                    if let Err(e) = delivery.respond(&bytes) {
                        eprintln!("member_control_respond_failed:{e}");
                    }
                }
                None => eprintln!("member_control_reply_unsigned"),
            }
            away.respond = responding.elapsed();
        }
        let wait = Duration::from_millis(
            B::RouterClose
                .span(Entries::ZERO)
                .map_err(|_| "budget_unavailable")?
                .millis(),
        );
        // The client closes after reading the response; bounded wait, then close.
        let closing = Instant::now();
        if rt
            .block_on(async { tokio::time::timeout(wait, connection.closed()).await })
            .is_err()
        {
            connection.close(0u32.into(), b"control call served");
        } else {
            away.closed_by_peer = true;
        }
        away.close_wait = closing.elapsed();
        if served.elapsed() > report_after {
            eprintln!(
                "member_control_away endpoint={short} from={} command={} away_ms={} receive_ms={} handle_ms={} respond_ms={} close_wait_ms={} closed_by_peer={}",
                connection.remote_id().fmt_short(),
                away.command,
                served.elapsed().as_millis(),
                away.receive.as_millis(),
                away.handle.as_millis(),
                away.respond.as_millis(),
                away.close_wait.as_millis(),
                away.closed_by_peer
            );
        }
    }
    Ok(())
}
fn handle(dispatcher: &Dispatcher, status: &Status, request: Value) -> Value {
    let command = request["command"].as_str().unwrap_or_default();
    let id = request["operation_id"].as_u64();
    let result = match command {
        "status" => status
            .lock()
            .map(|s| {
                let mut s = s.clone();
                // Exact-incarnation liveness on THIS host, even mid-operation:
                // the owned worker, plus up to 8 pids the owner asks about (e.g.
                // workers named in this member's signed proofs). Bound to the
                // pid + start time + owner this member recorded for its own
                // workers, so a reused pid never reads alive and another user's
                // process never reads as our worker's death.
                let alive = |p: u64| status.liveness(p);
                s["worker_alive"] = s["worker_pid"].as_u64().map_or(Value::Null, alive);
                let probes: serde_json::Map<String, Value> = request["probe_pids"]
                    .as_array()
                    .map(|a| {
                        a.iter()
                            .take(8)
                            .filter_map(Value::as_u64)
                            .collect::<Vec<_>>()
                    })
                    .unwrap_or_default()
                    .into_iter()
                    .map(|p| (p.to_string(), alive(p)))
                    .collect();
                s["probe_alive"] = Value::Object(probes);
                s["active_operation_id"] = json!(dispatcher.active_id());
                s["pid"] = json!(std::process::id());
                s
            })
            .map_err(|_| "member_status_poisoned".to_owned()),
        "result" => match id {
            Some(id) => dispatcher.result(id),
            None => Err("operator_operation_id_required".into()),
        },
        "cancel_operation" => dispatcher.cancel(id.filter(|id| *id > 0)),
        _ => dispatcher
            .admit(request.clone(), Origin::Remote)
            .map(|()| json!({"accepted":true,"operation_id":id}))
            .map_err(str::to_owned),
    };
    match result {
        Ok(value) => json!({"ok":true,"result":value}),
        Err(code) => json!({"ok":false,"code":code}),
    }
}

// ---- The member's membership, as the work service needs it ----

/// The joined member's live membership: the renewed session (shared with its renewal loop) and the
/// seed-current shard identity. The example's `Membership` implements it.
pub trait Member: Sync {
    /// The session the renewal loop renews in place. Never hold it over network I/O.
    fn session(&self) -> &Arc<Mutex<Option<Session>>>;
    /// This member's current shard identity, or the refusal (renewal failed, lease expired).
    fn current(&self, signer: &Identity) -> std::result::Result<Value, Error>;
}

// ---- Standing consent decision (from examples/shard_member.rs) ----

/// The device memory bounding the per-run memory cap: the decision's stated
/// value, else this Mac's measured physical memory (v0.1.4 always measures,
/// `app.py:1754` `host_memory_bytes`); `None` only when it cannot be read.
pub fn device_memory(body: &Value) -> Option<u64> {
    body["device_memory_bytes"].as_u64().or_else(|| {
        let out = std::process::Command::new("/usr/sbin/sysctl")
            .args(["-n", "hw.memsize"])
            .output()
            .ok()?;
        String::from_utf8(out.stdout)
            .ok()?
            .trim()
            .parse()
            .ok()
            .filter(|m| *m > 0)
    })
}
/// The member-side decision is one this Mac can honour: an offered resource
/// policy (`consent_valid`, else `consent_required`) and grant caps within
/// that policy and the device memory. Nothing is written.
pub fn usable_decision(body: &Value) -> Result<()> {
    if !resource_policy::consent_valid(&body["policy"]) {
        return Err("consent_required".into());
    }
    standing_consent::decision_caps(&body["consent"], &body["policy"], device_memory(body))
        .map(|_| ())
        .map_err(|e| e.0.to_owned())
}
/// Record a member-side decision (local stdin operator or `--consent-file`):
/// its resource policy and the unbound standing grant. v0.1.4 grants before
/// the pair request and binds after the accept.
pub fn grant_decision(root: &Path, body: &Value) -> Result<()> {
    usable_decision(body)?;
    resource_policy::write(root, &body["policy"]).map_err(|e| e.code.to_owned())?;
    standing_consent::grant(
        root,
        &body["consent"],
        &body["policy"],
        app_host::now(),
        device_memory(body),
    )
    .map(|_| ())
    .map_err(|e| e.0.to_owned())
}
/// Grant and bind standing consent from a member-side decision to the swarm
/// this member joined.
pub fn grant_consent(root: &Path, membership: &impl Member, body: &Value) -> Result<Value> {
    grant_decision(root, body)?;
    bind_consent(root, membership)
}
/// Bind the granted standing consent to the swarm this member joined.
pub fn bind_consent(root: &Path, membership: &impl Member) -> Result<Value> {
    let held = membership
        .session()
        .lock()
        .map_err(|_| "session_poisoned")?;
    let session = held.as_ref().ok_or("not_joined")?;
    let binding = session.binding().ok_or("not_joined")?;
    standing_consent::bind(root, &json!({"swarm_id":binding.swarm_id,"node_id":binding.node_id,"seed_key_digest":binding.seed_key_digest}), app_host::now()).map_err(|e| e.0.to_owned())?;
    Ok(json!({"consent":"granted"}))
}
/// The member's own standing consent and resource policy, for the owner to check before any
/// offer (signed on the remote channel).
pub fn consent_status(root: &Path) -> Result<Value> {
    Ok(json!({
        "consent": standing_consent::load(root).map_err(|e| e.0.to_owned())?,
        "policy": resource_policy::read(root).map_err(|e| e.code.to_owned())?,
        "route_ready": false}))
}

// ---- The member's work (from the command handlers of examples/shard_member.rs) ----

/// The carrier side of one member process: the host the caller bound (loopback or direct LAN),
/// its runtime and clock, this carrier's endpoint id and the caller's network choice, handed
/// through unchanged.
pub struct Carrier<'a, 'r> {
    pub host: &'a LoopbackHost<'r>,
    pub runtime: &'a tokio::runtime::Runtime,
    pub clock: &'a HostClock,
    pub endpoint: iroh::EndpointId,
    /// The caller's network choice: a direct-LAN policy, or `None` for loopback.
    pub lan: Option<&'a LanPolicy>,
}
/// `now + RouterClose` on the member's monotonic clock.
fn router_close(clock: &HostClock) -> Result<MonoInstant> {
    clock
        .monotonic()
        .checked_add(
            B::RouterClose
                .span(Entries::ZERO)
                .map_err(|_| "budget_unavailable")?,
        )
        .map_err(|_| "deadline_invalid".to_owned())
}
/// What this member owns: inert received custody, its native worker, the last resident proof
/// it produced, and the leave fence.
pub struct Work {
    pub root: PathBuf,
    pub native: Option<NativeShard>,
    pub shard: Option<JoinedShard>,
    /// Digest of the last signed resident proof THIS process produced.
    pub proof_digest: Option<String>,
    pub left: bool,
    /// The latest owner-signed run announcement this member verified (its share), if any.
    announced: Option<VerifiedAnnouncement>,
    /// Native work needs the announced share of the offer's own run (an announced owner);
    /// `false` for the operator-driven member (`Work::operator`).
    require_share: bool,
    /// First prepare plus this member's then-consented wall; never renewed by another prepare.
    consent_deadline: Option<Instant>,
}
fn consent_end(root: &Path, began: Instant) -> Result<Instant> {
    let consent = standing_consent::load(root)
        .map_err(|_| "joined_shard_consent_required")?
        .ok_or("joined_shard_consent_required")?;
    let wall = consent["caps"]["max_wall_seconds"]
        .as_u64()
        .filter(|w| *w > 0)
        .ok_or("joined_native_consent_invalid")?;
    began
        .checked_add(Duration::from_secs(wall))
        .ok_or_else(|| "joined_native_consent_invalid".into())
}
impl Work {
    pub fn new(root: PathBuf) -> Self {
        Self {
            root,
            native: None,
            shard: None,
            proof_digest: None,
            left: false,
            announced: None,
            require_share: true,
            consent_deadline: None,
        }
    }
    /// A member driven by its local operator (the example's CLI-pinned owner, no announcement):
    /// its native prepare is bounded by its own consent caps only.
    pub fn operator(root: PathBuf) -> Self {
        Self {
            require_share: false,
            ..Self::new(root)
        }
    }
    /// Take the share (`work_limits`) of a newly verified announcement for this member; work is
    /// then admitted only within it.
    pub fn announced(&mut self, verified: &VerifiedAnnouncement) {
        self.announced = Some(verified.clone());
    }
    /// The share work is admitted within: the last verified announcement's `work_limits`.
    pub fn share(&self) -> Option<&Value> {
        self.announced
            .as_ref()
            .and_then(VerifiedAnnouncement::work_limits)
    }
    /// Announced work is admitted only from an announcement verified against this membership's
    /// seed key (`owner_pin_announcement_unverified`), under standing consent granted AND bound to
    /// this membership (`joined_shard_consent_required`), and within the member's caps: the
    /// announced share through `standing_limits` (`member_wall_limit_invalid`,
    /// `member_wall_cap_below_share`, `member_resources_invalid`, `member_memory_cap_below_share`,
    /// `member_storage_cap_below_share`, `worker_limits_invalid`), v0.1.4 standing_consent.py:230-261.
    /// `Ok(None)`: nothing was announced (operator path) or no share for this member; otherwise
    /// the worker limits of the share.
    pub fn admit_share(&self, session: Option<&impl Member>) -> Result<Option<Value>> {
        match self.announced.as_ref() {
            Some(verified) => Self::admit(verified, &self.root, session),
            None => Ok(None),
        }
    }
    /// The share of the offer's OWN owner run (REVIEW-lanePH3B-S4AF F1): only the held
    /// announcement whose `run_id` is the run the offer's transfer belongs to
    /// (`joined_owner::work::transfer_run`) decides; another run's announcement never does.
    /// `required` (native prepare of an announced owner's work): no announced share for this run
    /// refuses `member_share_unannounced` (fail closed; v0.1.4 checks the share of the CURRENT
    /// announcement before accepting work, demo_target.py:516-519, 531-538).
    pub fn admit_share_for(
        &self,
        session: Option<&impl Member>,
        offer: &Value,
        required: bool,
    ) -> Result<Option<Value>> {
        let run = offer["message"]["run"]
            .as_str()
            .and_then(crate::joined_owner::work::transfer_owner_run);
        let verified = self
            .announced
            .as_ref()
            .filter(|v| run.is_some() && v.announcement()["run_id"].as_str() == run);
        match verified {
            Some(v) if !required || v.work_limits().is_some() => {
                Self::admit(v, &self.root, session)
            }
            _ if required => Err("member_share_unannounced".into()),
            _ => Ok(None),
        }
    }
    fn admit(
        verified: &VerifiedAnnouncement,
        root: &Path,
        session: Option<&impl Member>,
    ) -> Result<Option<Value>> {
        let binding = session
            .ok_or("not_joined")?
            .session()
            .lock()
            .map_err(|_| "session_poisoned")?
            .as_ref()
            .and_then(Session::binding)
            .ok_or("not_joined")?;
        if verified.seed_key_digest() != binding.seed_key_digest {
            return Err("owner_pin_announcement_unverified".into());
        }
        let consent = standing_consent::load(root)
            .map_err(|_| "joined_shard_consent_required")?
            .ok_or("joined_shard_consent_required")?;
        if consent["state"] != "granted"
            || consent["binding"]
                != json!({"swarm_id":binding.swarm_id,"node_id":binding.node_id,
                    "seed_key_digest":binding.seed_key_digest})
        {
            return Err("joined_shard_consent_required".into());
        }
        match verified.work_limits() {
            None => Ok(None),
            Some(share) => crate::standing_acceptor::standing_limits(&consent, Some(share))
                .map(Some)
                .map_err(|e| e.0.to_owned()),
        }
    }
    /// `receive`: one seed-signed shard offer over the artifact carrier into retained custody.
    #[allow(clippy::too_many_arguments)]
    pub fn receive(
        &mut self,
        session: Option<&impl Member>,
        carrier_side: &Carrier<'_, '_>,
        signer: &Identity,
        carrier: &Identity,
        operation: &Operation,
    ) -> Result<Value> {
        let request = &operation.request;
        let (host, rt, clock) = (carrier_side.host, carrier_side.runtime, carrier_side.clock);
        let policy = carrier_side.lan;
        operation.check()?;
        // Item 3 / F1: this run's share, when already announced, fits the member's bound consent
        // before custody or dial (the owner pushes before the run's `work` announcement: an
        // unknown share is checked at `prepare_native`; another run's share never applies).
        self.admit_share_for(session, &request["offer"], false)?;
        let remaining = operation
            .deadline
            .saturating_duration_since(std::time::Instant::now());
        let end = clock
            .monotonic()
            .checked_add(budgets::Span::from_millis(
                remaining
                    .as_millis()
                    .try_into()
                    .map_err(|_| "deadline_invalid")?,
            ))
            .map_err(|_| "deadline_invalid")?;
        if self.shard.is_some() || self.native.is_some() {
            return Err("shard_already_owned".into());
        }
        self.consent_deadline = None;
        let session = session.ok_or("not_joined")?;
        let offer = &request["offer"];
        let source: iroh::EndpointId = request["source_id"]
            .as_str()
            .ok_or("source_id_missing")?
            .parse()
            .map_err(|_| "source_id_invalid")?;
        let socket: Option<std::net::SocketAddr> =
            serde_json::from_value(request["source_socket"].clone())
                .map_err(|_| "source_socket_invalid")?;
        if policy.is_none() && socket.is_none_or(|s| !s.ip().is_loopback()) {
            return Err("source_not_loopback".into());
        }
        // Retain custody BEFORE any fallible carrier work; failures keep debt.
        self.shard = Some(
            JoinedShard::begin(
                &self.root,
                session
                    .session()
                    .lock()
                    .map_err(|_| "session_poisoned")?
                    .as_ref()
                    .ok_or("not_joined")?,
                signer,
                carrier,
                offer,
                app_host::now(),
            )
            .map_err(|e| e.0.to_owned())?,
        );
        let receive = self.shard.as_mut().ok_or("shard_required")?;
        // LAN: dial the source ONLY at the seed-signed grant hints of the
        // admitted offer; an operator-supplied address must be one of them.
        let dial: Vec<std::net::SocketAddr> = match policy {
            Some(policy) => {
                let signed =
                    joined_shard::carrier::signed_source_addrs(offer, &source.to_string(), policy);
                if signed.is_empty() {
                    return Err("source_address_unsigned".into());
                }
                if socket.is_some_and(|s| !signed.contains(&s)) {
                    return Err("source_address_unexpected".into());
                }
                signed
            }
            None => socket.into_iter().collect(),
        };
        let connection = host
            .connect_channel(
                &dial
                    .iter()
                    .fold(iroh::EndpointAddr::new(source), |a, s| a.with_ip_addr(*s)),
                Channel::ArtifactChunk,
                clock,
                end.earliest(router_close(clock)?)
                    .map_err(|_| "deadline_invalid")?,
            )
            .map_err(|e| format!("{e:?}"))?;
        let mut link = runtime::iroh_artifact::ChunkSession::new(
            connection,
            rt,
            clock,
            carrier_side.endpoint,
            serve_limits().map_err(|_| "budget_unavailable")?.wire,
        )
        .map_err(|e| e.0)?;
        let size = offer["message"]["transfer"]["size_bytes"]
            .as_u64()
            .ok_or("size_invalid")?;
        let cap = B::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63
            .bytes()
            .map_err(|_| "budget_unavailable")?
            .get();
        let mut offset = 0;
        while offset < size {
            operation.check()?;
            // Never hold membership's renewal mutex over network I/O.
            let snapshot = session
                .session()
                .lock()
                .map_err(|_| "session_poisoned")?
                .as_ref()
                .ok_or("not_joined")?
                .clone();
            let length = cap
                .min(protocol::artifact_grant::MAX_CHUNK_BYTES)
                .min(size - offset);
            let chunk = ChunkRequest {
                request_id: format!("chunk-{offset}"),
                source_endpoint_id: source.to_string(),
                object_digest: offer["message"]["transfer"]["content_digest"]
                    .as_str()
                    .ok_or("digest_invalid")?
                    .into(),
                offset_bytes: offset,
                length_bytes: length,
            };
            receive
                .fetch_chunk_checked(
                    &mut link,
                    &chunk,
                    &snapshot,
                    signer,
                    carrier,
                    &app_host::now,
                    end,
                    &|| {
                        operation.check().map_err(|_| {
                            Error(if operation.cancelled.load(Ordering::SeqCst) {
                                "member_operation_cancelled"
                            } else {
                                "member_operation_deadline"
                            })
                        })?;
                        session.current(signer).map(drop)
                    },
                )
                .map_err(|e| e.0)?;
            offset += length;
        }
        link.finish();
        // Each bounded verification read admits against the CURRENT
        // renewed session, never a pre-verify snapshot (F5).
        let live = || -> std::result::Result<Session, Error> {
            session
                .session()
                .lock()
                .map_err(|_| Error("session_poisoned"))?
                .as_ref()
                .ok_or(Error("not_joined"))
                .cloned()
        };
        receive
            .verify_live(&live, signer, carrier, &app_host::now, &|| {
                operation
                    .check()
                    .map_err(|_| Error("member_operation_interrupted"))?;
                session.current(signer).map(drop)
            })
            .map_err(|e| e.0.to_owned())?;
        Ok(
            json!({"received_bytes":size,"path":receive.path(),"node_id":session.current(signer).map_err(|e| e.0.to_owned())?["node_id"],"generation":session.current(signer).map_err(|e| e.0.to_owned())?["generation"],"route_ready":false,"pid":std::process::id()}),
        )
    }
    /// `prepare_native`: hand the received custody to a native worker and return its signed
    /// resident proof.
    pub fn prepare_native(
        &mut self,
        session: Option<&impl Member>,
        signer: &Identity,
        operation: &Operation,
    ) -> Result<Value> {
        let request = &operation.request;
        if self.native.is_none() {
            // A refused custody hand-over keeps the received shard owned (F4).
            match NativeShard::try_new(self.shard.take().ok_or("shard_required")?) {
                Ok(compute) => self.native = Some(compute),
                Err(refused) => {
                    let (error, received) = *refused;
                    self.shard = Some(received);
                    return Err(error.0.to_owned());
                }
            }
        }
        // F1: the share announced for the offer's own run fits this member's bound caps, before
        // any native worker (custody stays owned on a refusal).
        let offer = self
            .native
            .as_ref()
            .ok_or("native_required")?
            .offer()
            .clone();
        self.admit_share_for(session, &offer, self.require_share)?;
        let member = session.ok_or("not_joined")?;
        let range =
            serde_json::from_value(request["range"].clone()).map_err(|_| "range_invalid")?;
        let tokens = request["tokens"].as_u64().ok_or("tokens_invalid")? as usize;
        let nonce = request["nonce"].as_str().ok_or("nonce_invalid")?;
        let source = PathBuf::from(
            request["forbidden_source"]
                .as_str()
                .ok_or("source_invalid")?,
        );
        if self.consent_deadline.is_none() {
            self.consent_deadline = Some(consent_end(&self.root, Instant::now())?);
        }
        let signed = self
            .native
            .as_mut()
            .ok_or("native_required")?
            .prepare_until(
                range,
                tokens,
                nonce,
                signer,
                &|| member.current(signer),
                operation.cancelled.clone(),
                &source,
                operation.deadline,
            )
            .map_err(|e| e.0.to_owned())?;
        self.proof_digest =
            Some(joined_shard::route::proof_digest(&signed).map_err(|e| e.0.to_owned())?);
        Ok(signed)
    }
    /// `native_step`: one operator-driven step of the prepared worker.
    pub fn native_step(
        &mut self,
        session: Option<&impl Member>,
        signer: &Identity,
        operation: &Operation,
    ) -> Result<Value> {
        use base64::Engine;
        let request = &operation.request;
        let member = session.ok_or("not_joined")?;
        let input: Option<Vec<u32>> =
            serde_json::from_value(request["tokens"].clone()).map_err(|_| "tokens_invalid")?;
        let payload = base64::engine::general_purpose::STANDARD
            .decode(request["payload"].as_str().ok_or("payload_invalid")?)
            .map_err(|_| "payload_invalid")?;
        let bytes = self
            .native
            .as_mut()
            .ok_or("native_required")?
            .step_until(
                input.as_deref(),
                &payload,
                &|| member.current(signer),
                operation.cancelled.clone(),
                operation.deadline,
            )
            .map_err(|e| e.0.to_owned())?;
        Ok(json!({"payload":base64::engine::general_purpose::STANDARD.encode(bytes)}))
    }
    /// `native_close`: graceful close of the worker.
    pub fn native_close(&mut self) -> Result<Value> {
        self.native
            .as_mut()
            .ok_or("native_required")?
            .close()
            .map_err(|e| e.0.to_owned())
    }
    /// `discard`: retire owned custody (never reported when nothing was owned).
    pub fn discard(&mut self) -> Result<Value> {
        if self.native.is_none() && self.shard.is_none() {
            // Nothing owned: never report a discard that did not happen (F4).
            return Ok(
                json!({"inert_bytes_discarded":false,"nothing_owned":true,"route_ready":false}),
            );
        }
        if let Some(compute) = self.native.as_mut() {
            compute.discard().map_err(|e| e.0.to_owned())?;
        }
        self.native = None;
        if let Some(receive) = self.shard.as_mut() {
            receive.discard().map_err(|e| e.0.to_owned())?;
        }
        self.shard = None;
        self.consent_deadline = None;
        Ok(json!({"inert_bytes_discarded":true,"route_ready":false}))
    }
    /// `serve`: one owner-signed route epoch through the library stage server
    /// (`joined_shard::serve`) with this process's native custody. The caller recycles its
    /// carrier afterwards when its network choice needs it.
    pub fn serve(
        &mut self,
        session: Option<&impl Member>,
        carrier_side: &Carrier<'_, '_>,
        signer: &Identity,
        operation: &Operation,
    ) -> Result<Value> {
        let member = session.ok_or("not_joined")?;
        let native = self.native.as_mut().ok_or("native_required")?;
        let proof_digest = self
            .proof_digest
            .as_deref()
            .ok_or("native_proof_required")?;
        let current = || member.current(signer);
        // One epoch per run, but never beyond this member's own wall. Carrier setup polls
        // cancellation/live consent too; retain this independent bound throughout the serve.
        // Re-preparing cannot extend the first prepare's wall.
        let consented = match self.consent_deadline {
            Some(end) => end,
            None => consent_end(&self.root, Instant::now())?,
        };
        let close = budgets::BudgetId::RouterClose
            .span(budgets::Entries::ZERO)
            .map_err(|_| "budget_unavailable")?;
        // Verify the peer's envelope and its binding BEFORE deriving any local deadline.
        // The carrier rechecks live membership at entry; this check protects the conversion.
        let wall_now = app_host::now();
        let (_, message) = joined_shard::route::verify_member_epoch(
            &operation.request["route"],
            &current().map_err(|e| e.0.to_owned())?,
            native.offer(),
            proof_digest,
            wall_now,
        )
        .map_err(|_| "route_invalid")?;
        let remaining = message["expires_at"]
            .as_f64()
            .filter(|e| e.is_finite() && *e > wall_now)
            .ok_or("route_invalid")?
            - wall_now;
        let remaining = Duration::try_from_secs_f64(remaining).map_err(|_| "route_invalid")?;
        let expiry = Instant::now()
            .checked_add(remaining)
            .and_then(|at| at.checked_add(Duration::from_millis(close.millis())))
            .ok_or("route_invalid")?;
        stage::serve(
            &stage::Serve {
                host: carrier_side.host,
                runtime: carrier_side.runtime,
                clock: carrier_side.clock,
                current: &current,
                cancelled: operation.cancelled.clone(),
                deadline: expiry.min(consented),
            },
            native,
            &operation.request["route"],
            proof_digest,
        )
    }
    /// `leave`: the product `api::leave::MemberLeave` state machine over this member's steps.
    pub fn leave<M: Member>(
        &mut self,
        session: &mut Option<M>,
        signer: &Identity,
        operation: &Operation,
    ) -> Result<Value> {
        let mode = operation.request["mode"]
            .as_str()
            .ok_or("leave_mode_invalid")?
            .to_owned();
        let began = std::time::Instant::now();
        let root = self.root.clone();
        let mut steps = Leave {
            root: root.clone(),
            signer,
            native: &mut self.native,
            shard: &mut self.shard,
            session,
            fenced: &mut self.left,
            began,
            withdrawal: None,
            unload: json!(null),
            timings: Vec::new(),
            refunded: false,
        };
        let record = crate::leave::MemberLeave::new(&root, crate::leave::Limits::default())
            .run(&mode, &mut steps, &mut crate::leave::SystemClock::default())
            .map_err(|e| e.code.to_owned())?;
        Ok(
            json!({"record":record,"withdrawal":steps.withdrawal,"unload":steps.unload,
            "steps":steps.timings,"refunded":steps.refunded,
            "elapsed_ms":began.elapsed().as_secs_f64()*1000.0,"route_ready":false}),
        )
    }
    /// After every operation: the status view the owner reads (`status`), with the owned worker
    /// bound to its incarnation while it is still unreaped.
    pub fn record(
        &self,
        status: &Status,
        joined: bool,
        command: &str,
        operation: &Operation,
        out: &Value,
    ) {
        // The worker child is still owned (unreaped) here: bind its pid now.
        let worker_pid = self.native.as_ref().and_then(NativeShard::worker_pid);
        status.record_worker(worker_pid);
        if let Ok(mut s) = status.lock() {
            s["joined"] = json!(joined);
            s["left"] = json!(self.left);
            s["worker_pid"] = json!(worker_pid);
            s["native_owned"] = json!(self.native.is_some());
            s["shard_owned"] = json!(self.shard.is_some());
            s["last_operation"] =
                json!({"command":command,"operation_id":operation.id,"ok":out["ok"]});
            // A finished serve's teardown evidence (member-signed with the reply).
            if command == "serve" {
                s["last_serve"] = out.clone();
            }
            if command == "leave" {
                s["last_leave"] = out.clone();
            }
        }
    }
}

// ---- Member leave steps (from examples/shard_member/leave.rs) ----

/// Member leave through the product `api::leave::MemberLeave` state machine:
/// fence, drain, unload (native close/reap), publish a member-signed withdrawal,
/// withdraw standing consent, disconnect (stop renewal), retire+refund custody.
pub struct Leave<'a, M: Member> {
    pub root: PathBuf,
    pub signer: &'a Identity,
    pub native: &'a mut Option<NativeShard>,
    pub shard: &'a mut Option<JoinedShard>,
    pub session: &'a mut Option<M>,
    pub fenced: &'a mut bool,
    pub began: Instant,
    pub withdrawal: Option<Value>,
    pub unload: Value,
    pub timings: Vec<Value>,
    pub refunded: bool,
}
impl<M: Member> Leave<'_, M> {
    fn run(&mut self, name: &str) -> std::result::Result<(), String> {
        match name {
            "fence" => *self.fenced = true,
            "unload" => {
                if let Some(n) = self.native.as_mut()
                    && n.worker_pid().is_some()
                {
                    let pid = n.worker_pid();
                    self.unload = match n.close() {
                        Ok(stats) => {
                            json!({"worker_pid":pid,"graceful":true,"context_closed":stats["context_closed"]})
                        }
                        Err(_) => {
                            json!({"worker_pid":pid,"graceful":false,"reap":n.abort().map_err(|e| e.0.to_owned())?})
                        }
                    };
                }
            }
            "publish_withdrawal" => {
                let offer = match (self.native.as_ref(), self.shard.as_ref()) {
                    (Some(n), _) => n.offer().clone(),
                    _ => return Ok(()),
                };
                let member = self.session.as_ref().ok_or("member_session_missing")?;
                let held = member.session().lock().map_err(|_| "session_poisoned")?;
                self.withdrawal = Some(
                    joined_shard::withdrawal(
                        held.as_ref().ok_or("member_session_missing")?,
                        self.signer,
                        &offer,
                        &self.unload,
                        app_host::now(),
                    )
                    .map_err(|e| e.0.to_owned())?,
                );
            }
            "withdraw_consent" => {
                standing_consent::withdraw(&self.root, app_host::now())
                    .map_err(|e| e.0.to_owned())?;
            }
            // Dropping the membership stops AND joins the renewal thread: the lease ages out.
            "disconnect" => *self.session = None,
            "cancel_work" => {
                if let Some(n) = self.native.as_mut() {
                    n.discard().map_err(|e| e.0.to_owned())?;
                    self.refunded = true;
                }
                *self.native = None;
                if let Some(s) = self.shard.as_mut() {
                    s.discard().map_err(|e| e.0.to_owned())?;
                    self.refunded = true;
                }
                *self.shard = None;
            }
            _ => {}
        }
        Ok(())
    }
}
impl<M: Member> Steps for Leave<'_, M> {
    fn step(&mut self, name: &str) -> std::result::Result<(), Option<String>> {
        let result = self.run(name);
        self.timings.push(json!({"step":name,"ok":result.is_ok(),
            "at_ms":self.began.elapsed().as_secs_f64()*1000.0}));
        result.map_err(Some)
    }
    fn inflight(&mut self) -> std::result::Result<i64, crate::leave::Unobservable> {
        // The operator channel is serial: no route epoch can be serving now.
        Ok(0)
    }
    fn compute_cleanup(&mut self) -> Option<std::result::Result<ComputeCleanup, String>> {
        Some(
            if self.native.is_none() && self.shard.is_none() && self.refunded {
                Ok(ComputeCleanup::Proven)
            } else if self.native.is_none() && self.shard.is_none() {
                Ok(ComputeCleanup::NoneOwned)
            } else {
                Err("member_compute_cleanup_unavailable".into())
            },
        )
    }
}
