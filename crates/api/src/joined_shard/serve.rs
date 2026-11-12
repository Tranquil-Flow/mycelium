// SPDX-License-Identifier: AGPL-3.0-only
//! Member-side Router stage server for ONE owner-signed route epoch.
//!
//! A `mycelium.joined_route.v2` epoch (lane EP1) serves many requests, one at a
//! time, on the same two connections: a PathCancellation is request-scoped. The
//! in-flight native step always finishes (never interrupted) and its result is
//! dropped; then the compute's session is reset and only then ACKed, and the
//! member serves the next PREFILL. A settled request id is refused (bounded
//! set). The epoch ends with the compute closed when the owner closes it, at
//! the operator's cancel or deadline, at the route's signed expiry, when the
//! member's own consent ends (all checked on every loop pass, whatever the
//! traffic; lane EPF), or after a
//! failure; a reset that fails is never ACKed (compute closed, delivery
//! rejected, a bounded owner re-dial acknowledged instead).
//!
//! The owner dials a data connection (hops/results) then a control connection
//! (PathCancellation). The member validates every hop against the signed route,
//! steps only its own stage compute, and ACKs a PathCancellation only after that
//! compute was closed (graceful) or killed and reaped (interrupted step). Any
//! hop-processing failure releases the compute at once and still waits for the
//! owner's teardown, so the owner can settle. When the owner's teardown cannot be
//! delivered (control lost, or the ACK write failed) the member keeps accepting a
//! bounded owner re-dial to acknowledge the already-released compute. No
//! route_ready claim; the native worker is the only producer of model bytes.
use super::{native::NativeShard, route};
use crate::{compiler_inputs::Error, managed_carrier::HostClock};
use budgets::{BudgetId as B, Entries, MonoInstant};
use protocol::router_wire;
use runtime::{
    iroh::IrohTransport,
    iroh_host::{LoopbackHost, NetworkMode},
    iroh_receive::{DispatchRefusal, RouterReceiver},
};
use serde_json::{Value, json};
use std::{
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
use transport::{Channel, Clock, ConnectionFence, Exchange};
type Outcome<T> = std::result::Result<T, String>;
/// Live member row (seed-current generation) for fences and route binding.
pub type Current<'a> = &'a (dyn Fn() -> Result<Value, Error> + Sync);

/// The one stage computation a member serves. `NativeShard` is the product
/// implementation; tests may supply a controlled implementation to exercise the
/// carrier (never presented as model output).
pub trait StageCompute {
    fn offer(&self) -> &Value;
    fn worker_pid(&self) -> Option<u32>;
    fn step(
        &mut self,
        tokens: Option<&[u32]>,
        payload: &[u8],
        current: Current<'_>,
        cancel: Arc<AtomicBool>,
        deadline: Instant,
    ) -> Result<Vec<u8>, Error>;
    /// Graceful close; on failure the compute stays owned for `abort`.
    fn close(&mut self) -> Result<Value, Error>;
    /// Kill and reap; evidence only after the OS reported the exit.
    fn abort(&mut self) -> Result<Value, Error>;
    /// Release one request's session state and keep the compute for the next
    /// request of a multi-request epoch (lane EP0/EP1). Never interrupts a
    /// step; Ok only when the state was released. A compute that cannot reset
    /// refuses, and the member then ends its epoch with the compute closed.
    fn reset(&mut self, _current: Current<'_>) -> Result<Value, Error> {
        Err(Error("joined_native_reset_unsupported"))
    }
    /// The member's own consent, checked WITHOUT spending anything (lane
    /// EPF, REVIEW-laneEP R1): membership current, consent granted and
    /// unchanged, its wall (from the first prepare) and command allowance not
    /// spent. A v2 serve checks it on every loop pass, whatever the traffic.
    fn live(&self, current: Current<'_>) -> Result<(), Error>;
}
impl StageCompute for NativeShard {
    fn offer(&self) -> &Value {
        NativeShard::offer(self)
    }
    fn worker_pid(&self) -> Option<u32> {
        NativeShard::worker_pid(self)
    }
    fn step(
        &mut self,
        tokens: Option<&[u32]>,
        payload: &[u8],
        current: Current<'_>,
        cancel: Arc<AtomicBool>,
        deadline: Instant,
    ) -> Result<Vec<u8>, Error> {
        self.step_until(tokens, payload, &|| current(), cancel, deadline)
    }
    fn close(&mut self) -> Result<Value, Error> {
        NativeShard::close(self)
    }
    fn abort(&mut self) -> Result<Value, Error> {
        NativeShard::abort(self)
    }
    fn reset(&mut self, current: Current<'_>) -> Result<Value, Error> {
        NativeShard::reset(self, &|| current())
    }
    fn live(&self, current: Current<'_>) -> Result<(), Error> {
        NativeShard::live(self, &|| current())
    }
}
/// Serving context: the member's carrier host and the operator operation bounds.
pub struct Serve<'a, 'r> {
    pub host: &'a LoopbackHost<'r>,
    pub runtime: &'a tokio::runtime::Runtime,
    pub clock: &'a HostClock,
    pub current: Current<'a>,
    /// Operator cancellation of the serve operation.
    pub cancelled: Arc<AtomicBool>,
    /// Whole-operation deadline.
    pub deadline: Instant,
}
impl Serve<'_, '_> {
    fn check(&self) -> Outcome<()> {
        if self.cancelled.load(Ordering::SeqCst) {
            Err("member_operation_cancelled".into())
        } else if Instant::now() >= self.deadline {
            Err("member_operation_deadline".into())
        } else {
            Ok(())
        }
    }
    fn until(&self, cap: Option<Duration>) -> Outcome<MonoInstant> {
        let mut left = self.deadline.saturating_duration_since(Instant::now());
        if let Some(cap) = cap {
            left = left.min(cap);
        }
        mono(self.clock, left)
    }
}
fn mono(clock: &HostClock, span: Duration) -> Outcome<MonoInstant> {
    clock
        .monotonic()
        .checked_add(budgets::Span::from_millis(
            span.as_millis()
                .try_into()
                .map_err(|_| "deadline_invalid")?,
        ))
        .map_err(|_| "deadline_invalid".into())
}
/// v2 (lane EPF2, REVIEW-laneEPF F11): a request id this member may keep for
/// its epoch is one the owner could have admitted: the shipped gateway's
/// grammar, at most 128 bytes (v0.1.4 mycelium_request_gateway contracts.py:27,
/// admission service.py:468; v0.2 owner m16_requests.rs:496).
fn valid_request_id(id: &Value) -> bool {
    id.as_str().is_some_and(crate::registry::valid_request_id)
}
fn budget(id: B) -> Outcome<Duration> {
    Ok(Duration::from_millis(
        id.span(Entries::ZERO)
            .map_err(|_| "budget_unavailable")?
            .millis(),
    ))
}
enum Message {
    /// One bounded, body-free refusal notification per receiver. Invalid peer
    /// IDs/payloads never enter the work queue, even after compute release.
    InvalidId {
        cancellation: bool,
    },
    Hop(Value, Vec<u8>),
    /// PathCancellation body, receipt time, native-stop result, ACK-delivered report.
    Teardown(
        Value,
        Instant,
        mpsc::SyncSender<std::result::Result<(), String>>,
        mpsc::Receiver<bool>,
    ),
    Closed(&'static str),
}
/// Release compute exactly once: graceful close unless the framing was
/// interrupted (or close fails), then kill+reap.
fn stop(compute: &mut dyn StageCompute, interrupted: bool) -> Outcome<Value> {
    if compute.worker_pid().is_none() {
        return Ok(json!({"worker":"absent"}));
    }
    if !interrupted && let Ok(stats) = compute.close() {
        return Ok(json!({"context_closed":stats["context_closed"],"graceful":true,"stats":stats}));
    }
    compute
        .abort()
        .map(|e| json!({"graceful":false,"reap":e}))
        .map_err(|e| e.0.to_owned())
}
/// Serve one owner-signed route epoch with this member's verified compute.
pub fn serve(
    s: &Serve<'_, '_>,
    compute: &mut dyn StageCompute,
    route_envelope: &Value,
    proof_digest: &str,
) -> Outcome<Value> {
    let began = Instant::now();
    let live = (s.current)().map_err(|e| e.0.to_owned())?;
    let (index, message) = route::verify_member_epoch(
        route_envelope,
        &live,
        compute.offer(),
        proof_digest,
        crate::app_host::now(),
    )
    .map_err(|e| e.0.to_owned())?;
    let owner: iroh::EndpointId = message["owner_endpoint_id"]
        .as_str()
        .ok_or("route_owner_invalid")?
        .parse()
        .map_err(|_| "route_owner_invalid")?;
    // Direct LAN: only the verified route's owner key is accepted; any other
    // key is refused by the host and unpinned at once (REVIEW-laneB9 F2).
    if s.host.mode() == NetworkMode::DirectLan
        && let Err(e) = s.host.admit_peer_ids(&[owner])
    {
        stop(compute, false)?;
        return Err(format!("route_owner_admission_failed:{e:?}"));
    }
    let generation = live["generation"].as_u64().ok_or("generation_invalid")?;
    let epoch = message["topology_version"]
        .as_u64()
        .ok_or("route_invalid")?;
    let multi = route::multi_request(&message);
    let expires_at = message["expires_at"].as_f64().ok_or("route_invalid")?;
    // v2: settled request ids, bounded by the epoch's lifetime admissions.
    let capacity = usize::try_from(
        B::JoinedEpochMaxSessions
            .count()
            .map_err(|_| "budget_unavailable")?
            .get(),
    )
    .map_err(|_| "budget_unavailable")?;
    let worker_pid = compute.worker_pid();
    let mut connections: Vec<iroh::endpoint::Connection> = Vec::new();
    let setup = (|| -> Outcome<()> {
        let poll = budget(B::OwnerPoll)?;
        loop {
            // Recheck on EVERY pass, even after refused handshakes/unpinned
            // traffic and after the second successful accept. The short accept
            // window observes Stop; it never renews the route or consent wall.
            s.check()?;
            compute.live(s.current).map_err(|e| e.0.to_owned())?;
            if crate::app_host::now() >= expires_at {
                return Err("route_expired".into());
            }
            if connections.len() == 2 {
                return Ok(());
            }
            let c = match s.host.accept(s.clock, s.until(Some(poll))?) {
                Ok(c) => c,
                Err(transport::Error::Expired | transport::Error::Authentication) => continue,
                Err(e) => return Err(format!("route_accept_failed:{e:?}")),
            };
            // Authentication is not membership: an unpinned dialer cannot
            // become a remote kill switch for the signed owner's route.
            if c.remote_id() != owner {
                c.close(2u32.into(), b"unpinned route owner");
                continue;
            }
            connections.push(c);
        }
    })();
    if let Err(code) = setup {
        for c in &connections {
            c.close(0u32.into(), b"route setup ended");
        }
        // No request started. Abort/reap rather than waiting for the native
        // graceful-close command's independent (longer) allowance.
        stop(compute, true)?;
        return Err(code);
    }
    let base = ConnectionFence {
        endpoint_id: *owner.as_bytes(),
        local_generation: generation,
        remote_generation: epoch,
    };
    let current = s.current;
    let fence = move || ConnectionFence {
        local_generation: current()
            .ok()
            .and_then(|m| m["generation"].as_u64())
            .unwrap_or(u64::MAX),
        ..base
    };
    let rt = s.runtime;
    let mut sender = IrohTransport::new(connections[0].clone(), rt, s.clock, base, fence)
        .map_err(|e| format!("route_sender:{e:?}"))?;
    let data_cancel = sender.cancellation_handle();
    let receivers: Vec<_> = connections
        .iter()
        .map(|c| RouterReceiver::new(c.clone(), rt, s.clock, base, fence))
        .collect::<std::result::Result<_, _>>()
        .map_err(|e| format!("route_receiver:{e:?}"))?;
    let cancel = Arc::new(AtomicBool::new(false));
    // v2: the owner cancelled the current request (sends only; never a step).
    let request_cancel = Arc::new(AtomicBool::new(false));
    let (tx, rx) = mpsc::channel();
    let limit = s.until(None)?;
    let poll = budget(B::OwnerPoll)?;
    let hop_budget = |t: u64| {
        budget(if t == 0 {
            B::WorkerStartup
        } else {
            B::PyFinalScriptsRunA11RouteBenchmarkTargetTpotMsL398C12
        })
    };
    // v2 (lane EPF2, REVIEW-laneEPF F10): a step and its forwarding exchange
    // also end by the route's signed expiry (the consent wall caps the step
    // in native.rs).
    let hop_wait = |t: u64| -> Outcome<Duration> {
        let wait = hop_budget(t)?;
        if !multi {
            return Ok(wait);
        }
        let left = Duration::try_from_secs_f64((expires_at - crate::app_host::now()).max(0.0))
            .unwrap_or(Duration::ZERO);
        Ok(wait.min(left))
    };
    let stages = message["stages"].as_array().ok_or("route_invalid")?.len();
    // v2: settled request ids (refused if replayed) and per-request summaries.
    let mut served: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    // v2 (lane EPF): ids seen only as an idle cancellation (no request ran
    // here): settled tombstones, counted with `served` against the bound.
    let mut skipped: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    let mut requests: Vec<Value> = Vec::new();
    let report = std::thread::scope(|scope| -> Outcome<Value> {
        // The queue ends with this closure, before the scope joins the
        // receiver threads (lane EPF2, REVIEW-laneEPF N1): a PathCancellation
        // still queued when the epoch ends drops its verdict sender with it,
        // so the receiver waiting on that verdict refuses it and returns.
        let rx = rx;
        for (role, mut receiver) in receivers.into_iter().enumerate() {
            let (tx, cancel, data_cancel) = (tx.clone(), cancel.clone(), &data_cancel);
            let request_cancel = request_cancel.clone();
            scope.spawn(move || {
                let mut expected: u128 = 0;
                let mut reported_invalid_id = false;
                loop {
                    let delivery = match receiver.receive(limit) {
                        Ok(d) => d,
                        Err(_) => {
                            let closed = Message::Closed(if role == 0 {
                                "route_data_closed"
                            } else {
                                "route_control_closed"
                            });
                            if tx.send(closed).is_err() {
                                eprintln!("serve: route already settled at receiver close");
                            }
                            return;
                        }
                    };
                    if delivery.message_id() != expected.to_be_bytes() {
                        if let Err(e) = delivery.reject(DispatchRefusal::ReplayCollision) {
                            eprintln!("serve: delivery.reject failed: {e:?}");
                        }
                        continue;
                    }
                    expected += 1;
                    let Ok(frame) = router_wire::decode_frame(delivery.payload()) else {
                        if let Err(e) = delivery.reject(DispatchRefusal::InvalidFrame) {
                            eprintln!("serve: delivery.reject failed: {e:?}");
                        }
                        continue;
                    };
                    // EP2H C2: every v2 phase, before copies, queueing, cancel
                    // flags or ACK. One constant notification per receiver keeps
                    // invalid traffic from retaining attacker-controlled state.
                    if multi
                        && matches!(frame.message_type(), "HopHeader" | "PathCancellation")
                        && !valid_request_id(&frame.body()["request_id"])
                    {
                        if !reported_invalid_id {
                            reported_invalid_id = true;
                            if tx
                                .send(Message::InvalidId {
                                    cancellation: frame.message_type() == "PathCancellation",
                                })
                                .is_err()
                            {
                                return;
                            }
                        }
                        if let Err(e) = delivery.reject(DispatchRefusal::InvalidFrame) {
                            eprintln!("serve: invalid request id reject failed: {e:?}");
                        }
                        continue;
                    }
                    match frame.message_type() {
                        "HopHeader" if role == 0 => {
                            // Single owner-serial dispatch slot: ACK = queued for compute.
                            if tx
                                .send(Message::Hop(frame.body().clone(), frame.payload().to_vec()))
                                .is_err()
                            {
                                return;
                            }
                            if delivery.acknowledge().is_err() {
                                return;
                            }
                        }
                        "PathCancellation" => {
                            let received = Instant::now();
                            if multi {
                                // Request-scoped: the step in flight finishes; the
                                // epoch's data connection stays usable.
                                request_cancel.store(true, Ordering::SeqCst);
                            } else {
                                cancel.store(true, Ordering::SeqCst);
                                // Unblock a pending result send; framing there is moot.
                                data_cancel.request_cancel();
                            }
                            let (done, stopped) = mpsc::sync_channel(1);
                            let (acked_tx, acked) = mpsc::sync_channel(1);
                            if tx
                                .send(Message::Teardown(
                                    frame.body().clone(),
                                    received,
                                    done,
                                    acked,
                                ))
                                .is_err()
                            {
                                return;
                            }
                            // ACK only after compute close/reap actually completed.
                            let delivered = match stopped.recv() {
                                Ok(Ok(())) => delivery.acknowledge().is_ok(),
                                _ => {
                                    if let Err(e) = delivery.reject(DispatchRefusal::QueueFull) {
                                        eprintln!("serve: delivery.reject failed: {e:?}");
                                    }
                                    false
                                }
                            };
                            if let Err(e) = acked_tx.send(delivered) {
                                eprintln!("serve: acked_tx.send failed: {e:?}");
                            }
                            if !multi {
                                return;
                            }
                            // v2: the epoch's next request ends with its own.
                        }
                        _ => {
                            if let Err(e) = delivery.reject(DispatchRefusal::InvalidFrame) {
                                eprintln!("serve: delivery.reject failed: {e:?}");
                            }
                        }
                    }
                }
            });
        }
        drop(tx);
        let (served, skipped, requests) = (&mut served, &mut skipped, &mut requests);
        let outcome = (|| -> Outcome<Value> {
            let mut path: Option<route::Path> = None;
            // v2: tokens of the requests already settled on this epoch.
            let mut total: u64 = 0;
            let mut t: u64 = 0;
            let mut sent: u128 = 0;
            let mut interrupted = false;
            let mut failure: Option<String> = None;
            // Compute already released after a local failure (exactly once).
            let mut released: Option<Value> = None;
            let mut step_ms = Vec::new();
            loop {
                let received = match rx.recv_timeout(poll) {
                    Ok(m) => Some(m),
                    Err(mpsc::RecvTimeoutError::Timeout) => None,
                    Err(mpsc::RecvTimeoutError::Disconnected) => {
                        let native_stop = match released.take() {
                            Some(v) => v,
                            None => stop(compute, interrupted)?,
                        };
                        return Ok(
                            json!({"teardown":"carrier_lost","native":native_stop,"tokens_served":t,"failure":failure}),
                        );
                    }
                };
                // v1: the operation bounds when no message came (unchanged).
                // v2 (lane EPF, REVIEW-laneEP R1): on EVERY pass, whatever the
                // owner sends, the operation bounds, the route's signed expiry
                // and the member's own consent (non-consuming: wall, commands,
                // membership current, consent unchanged) end the epoch with
                // the compute closed (v0.1.4 worker.py:985-1000, :1453-1459,
                // :800-818: the worker ends at its wall whatever the traffic).
                let ending: Option<(&'static str, Option<String>)> =
                    if s.check().is_err() && (multi || received.is_none()) {
                        Some(("operator_cancelled", None))
                    } else if multi && crate::app_host::now() >= expires_at {
                        Some(("route_expired", None))
                    } else if multi && released.is_none() {
                        compute
                            .live(s.current)
                            .err()
                            .map(|e| ("member_consent_ended", Some(e.0.to_owned())))
                    } else {
                        None
                    };
                if let Some((teardown, code)) = ending {
                    cancel.store(true, Ordering::SeqCst);
                    data_cancel.request_cancel();
                    let native_stop = match released.take() {
                        Some(v) => v,
                        None => stop(compute, interrupted)?,
                    };
                    if failure.is_none() {
                        failure = code;
                    }
                    if teardown == "operator_cancelled" {
                        return Ok(
                            json!({"teardown":"operator_cancelled","native":native_stop,"failure":failure}),
                        );
                    }
                    // A request still open is settled by the owner's re-dial
                    // against the closed compute.
                    return Ok(json!({"teardown":teardown,"native":native_stop,
                        "tokens_served":total + t,"failure":failure,"stage_index":index,
                        "deferred_ack":path.is_some()}));
                }
                let Some(received) = received else {
                    continue;
                };
                match received {
                    Message::InvalidId { cancellation } => {
                        let already_released = released.is_some();
                        if !already_released {
                            released = Some(stop(compute, interrupted)?);
                        }
                        failure.get_or_insert_with(|| {
                            if cancellation {
                                "route_cancellation_invalid".to_owned()
                            } else {
                                "route_request_invalid".to_owned()
                            }
                        });
                        // Preserve the idle-invalid-cancel refusal. An admitted
                        // or already-released request may still deliver valid
                        // cleanup on the same connection; invalid IDs never bind it.
                        if cancellation && path.is_none() && !already_released {
                            return Ok(json!({"teardown":"path_cancellation",
                                "failure":failure,"native":released.take(),
                                "tokens_served":total + t,"stage_index":index}));
                        }
                    }
                    Message::Hop(body, payload) => {
                        if cancel.load(Ordering::SeqCst)
                            || failure.is_some()
                            || request_cancel.load(Ordering::SeqCst)
                        {
                            continue;
                        }
                        let handled = (|| -> Outcome<()> {
                            if path.is_none() && t == 0 && body["phase"] == "PREFILL" {
                                if multi {
                                    // A new request only on a clean session (path
                                    // unbound), never a settled one, within bounds.
                                    if !valid_request_id(&body["request_id"]) {
                                        return Err("route_request_invalid".into());
                                    }
                                    let id = body["request_id"].as_str().unwrap_or_default();
                                    if served.contains(id) || skipped.contains(id) {
                                        return Err("route_request_settled".into());
                                    }
                                    if served.len() + skipped.len() >= capacity {
                                        return Err("route_request_capacity".into());
                                    }
                                    if crate::app_host::now() >= expires_at {
                                        return Err("route_expired".into());
                                    }
                                }
                                path = Some(route::Path {
                                    request_id: body["request_id"]
                                        .as_str()
                                        .unwrap_or_default()
                                        .to_owned(),
                                    path_id: body["path_id"]
                                        .as_str()
                                        .unwrap_or_default()
                                        .to_owned(),
                                    path_attempt: body["path_attempt"].as_u64().unwrap_or(u64::MAX),
                                });
                            }
                            let p = path.as_ref().ok_or("route_hop_unexpected")?;
                            let expected =
                                route::hop(&message, p, t, index).map_err(|e| e.0.to_owned())?;
                            let normal = router_wire::encode_frame("HopHeader", &body, &[])
                                .and_then(|b| router_wire::decode_frame(&b));
                            if !normal
                                .is_ok_and(|f| route::body_matches(&f, "HopHeader", &expected))
                            {
                                return Err("route_hop_binding".into());
                            }
                            let tokens = if index == 0 {
                                Some(route::entry_tokens(&payload).map_err(|e| e.0.to_owned())?)
                            } else {
                                None
                            };
                            let step_began = Instant::now();
                            let deadline = Instant::now() + hop_wait(t)?;
                            let bytes = compute
                                .step(
                                    tokens.as_deref(),
                                    if tokens.is_some() { &[] } else { &payload },
                                    s.current,
                                    // v2: only the operator interrupts a step; the
                                    // owner's request cancellation never does.
                                    if multi {
                                        s.cancelled.clone()
                                    } else {
                                        cancel.clone()
                                    },
                                    deadline,
                                )
                                .map_err(|e| {
                                    // A cancelled/expired exchange leaves framing unusable.
                                    interrupted = true;
                                    e.0.to_owned()
                                })?;
                            step_ms.push(step_began.elapsed().as_secs_f64() * 1000.0);
                            let frame = if index + 1 == stages {
                                let token =
                                    super::native::greedy(&bytes).map_err(|e| e.0.to_owned())?;
                                router_wire::encode_frame(
                                    "TokenEvent",
                                    &route::token_event(p, t, token),
                                    &[],
                                )
                            } else {
                                router_wire::encode_frame(
                                    "HopHeader",
                                    &route::hop(&message, p, t, index + 1)
                                        .map_err(|e| e.0.to_owned())?,
                                    &bytes,
                                )
                            }
                            .map_err(|_| "route_frame_invalid")?;
                            if cancel.load(Ordering::SeqCst)
                                || request_cancel.load(Ordering::SeqCst)
                            {
                                // Cancelled while the step ran: its result is dropped.
                                return Ok(());
                            }
                            let request = Exchange {
                                peer: base,
                                channel: Channel::Router,
                                message_id: sent.to_be_bytes(),
                                payload: frame,
                                deadline: mono(s.clock, hop_wait(t)?)?,
                                maximum_response_bytes: protocol::remote::REMOTE_MAX_FRAME_BYTES,
                            };
                            transport::exchange(&mut sender, &request, s.clock)
                                .map_err(|e| format!("route_send_failed:{e:?}"))?;
                            sent += 1;
                            t += 1;
                            Ok(())
                        })();
                        if let Err(code) = handled
                            && (multi || !cancel.load(Ordering::SeqCst))
                        {
                            // Release compute now; keep waiting for the owner's
                            // teardown so it can settle against an ACK.
                            failure = Some(code);
                            released = Some(stop(compute, interrupted)?);
                        }
                    }
                    Message::Teardown(body, received, done, acked) if multi => {
                        let matches = body["topology_version"] == message["topology_version"]
                            && path
                                .as_ref()
                                .is_none_or(|p| body == route::cancellation(&message, p));
                        let request_id = body["request_id"].as_str().map(str::to_owned);
                        let reset_began = Instant::now();
                        // A request this member admitted (its path is bound).
                        let admitted = path.is_some();
                        // An idle cancellation (no request ran here) is not free
                        // (lane EPF, REVIEW-laneEP R1): its id becomes a settled
                        // tombstone counted against the epoch's lifetime bound,
                        // and a repeat, a malformed id or one past the bound is
                        // refused (v0.1.4 relay.py:718-743 records a pending
                        // cancellation of an unseen path and refuses one when
                        // its table is full). Refusing ends this epoch.
                        let idle_refusal = match request_id.as_deref() {
                            _ if admitted || released.is_some() => None,
                            None | Some("") => Some("route_cancellation_invalid"),
                            // Lane EPF2 (F11): kept ids are bounded in length too.
                            Some(_) if !valid_request_id(&body["request_id"]) => {
                                Some("route_cancellation_invalid")
                            }
                            Some(id) if served.contains(id) || skipped.contains(id) => {
                                Some("route_request_settled")
                            }
                            Some(_) if served.len() + skipped.len() >= capacity => {
                                Some("route_request_capacity")
                            }
                            Some(_) => None,
                        };
                        // ACK means released. A reset keeps this member serving;
                        // anything else closes the compute and ends its epoch, and
                        // only a release the owner already expects is ACKed.
                        let (native_stop, ack, serving) = match released.take() {
                            Some(v) => (Ok(v), true, false),
                            None if !matches || interrupted => {
                                (stop(compute, interrupted), false, false)
                            }
                            None if idle_refusal.is_some() => {
                                failure = idle_refusal.map(str::to_owned);
                                (stop(compute, false), false, false)
                            }
                            None if path.is_none() => (Ok(json!({"session":"clean"})), true, true),
                            None => match compute.reset(s.current) {
                                Ok(v) => (Ok(json!({"reset":v})), true, true),
                                Err(e) => {
                                    failure = Some(format!("session_reset_failed:{}", e.0));
                                    (stop(compute, false), false, false)
                                }
                            },
                        };
                        let ok = native_stop.is_ok();
                        let verdict = if ack && ok {
                            Ok(())
                        } else {
                            Err("member_epoch_ended".to_owned())
                        };
                        if let Err(e) = done.send(verdict) {
                            eprintln!("serve: done.send failed: {e:?}");
                        }
                        let delivered =
                            acked.recv_timeout(budget(B::RouterClose)?).unwrap_or(false);
                        let reset_ms = reset_began.elapsed().as_secs_f64() * 1000.0;
                        if serving && delivered {
                            // Only an admitted request is served (and reported).
                            if let Some(id) = request_id.clone() {
                                if admitted {
                                    served.insert(id);
                                } else {
                                    skipped.insert(id);
                                }
                            }
                            if admitted {
                                requests.push(json!({"request_id":request_id,"tokens_served":t,
                                    "reset_ms":reset_ms,"step_ms":step_ms}));
                            }
                            total += t;
                            path = None;
                            t = 0;
                            step_ms = Vec::new();
                            request_cancel.store(false, Ordering::SeqCst);
                            continue;
                        }
                        // This member's epoch ends: a reset compute is closed now.
                        let native_stop = if serving {
                            stop(compute, false)
                        } else {
                            native_stop
                        };
                        let ok = native_stop.is_ok();
                        let wait = budget(B::RouterClose)?;
                        for c in &connections {
                            if rt
                                .block_on(async { tokio::time::timeout(wait, c.closed()).await })
                                .is_err()
                            {
                                eprintln!("serve: owner close wait elapsed");
                            }
                        }
                        let native_stop = native_stop?;
                        // A refused idle cancellation: no request of the owner's
                        // waits on this member, so there is nothing to re-dial.
                        let redial = if (delivered && ack) || !ok || idle_refusal.is_some() {
                            None
                        } else {
                            linger(s, owner, epoch, base, &fence, multi)?
                        };
                        return Ok(
                            json!({"teardown":"path_cancellation","binding_matched":matches,
                            "stopped_ms":received.elapsed().as_secs_f64()*1000.0,"native":native_stop,
                            "native_stopped":ok,"ack_delivered":delivered,"redial_ack_ms":redial,
                            "tokens_served":total + t,"interrupted_step":interrupted,
                            "failure":failure,"request_id":request_id,"stage_index":index,
                            "step_ms":step_ms}),
                        );
                    }
                    Message::Teardown(body, received, done, acked) => {
                        let matches = path
                            .as_ref()
                            .is_none_or(|p| body == route::cancellation(&message, p))
                            && body["topology_version"] == message["topology_version"];
                        let native_stop = match released.take() {
                            Some(v) => Ok(v),
                            None => stop(compute, interrupted),
                        };
                        let ok = native_stop.is_ok();
                        if let Err(e) =
                            done.send(native_stop.as_ref().map(drop).map_err(Clone::clone))
                        {
                            eprintln!("serve: done.send failed: {e:?}");
                        }
                        let delivered =
                            acked.recv_timeout(budget(B::RouterClose)?).unwrap_or(false);
                        // The owner closes after reading every ACK; closing first could
                        // discard the queued ACK bytes. Wait (bounded) for its close.
                        let wait = budget(B::RouterClose)?;
                        for c in &connections {
                            // Elapsed means the owner never closed: recorded, still bounded.
                            if rt
                                .block_on(async { tokio::time::timeout(wait, c.closed()).await })
                                .is_err()
                            {
                                eprintln!("serve: owner close wait elapsed");
                            }
                        }
                        let native_stop = native_stop?;
                        let redial = if delivered || !ok {
                            None
                        } else {
                            linger(s, owner, epoch, base, &fence, multi)?
                        };
                        return Ok(
                            json!({"teardown":"path_cancellation","binding_matched":matches,
                            "stopped_ms":received.elapsed().as_secs_f64()*1000.0,"native":native_stop,
                            "native_stopped":ok,"ack_delivered":delivered,"redial_ack_ms":redial,
                            "tokens_served":t,"interrupted_step":interrupted,
                            "failure":failure,"request_id":path.as_ref().map(|p| p.request_id.clone()),
                            "stage_index":index,"step_ms":step_ms}),
                        );
                    }
                    Message::Closed(which) => {
                        if which == "route_control_closed" {
                            // No owner can deliver teardown on this connection: release
                            // compute now, then accept a bounded owner re-dial.
                            cancel.store(true, Ordering::SeqCst);
                            data_cancel.request_cancel();
                            // v2: the owner closed an idle epoch whose every request
                            // was settled: end with the compute closed, no re-dial.
                            let clean = multi && path.is_none() && released.is_none();
                            let native_stop = match released.take() {
                                Some(v) => v,
                                None => stop(compute, interrupted)?,
                            };
                            if clean {
                                return Ok(json!({"teardown":"owner_closed","native":native_stop,
                                    "tokens_served":total,"failure":failure,"stage_index":index}));
                            }
                            return Ok(json!({"teardown":"control_lost","native":native_stop,
                                "tokens_served":t,"failure":failure,"stage_index":index,
                                "deferred_ack":true}));
                        }
                    }
                }
            }
        })();
        // Local close unblocks both receiver threads before the scope joins them.
        for c in &connections {
            c.close(0u32.into(), b"route_served");
        }
        outcome
    });
    // A refusal raised outside the hop handler must never leave compute owned.
    let mut report = match report {
        Ok(r) => r,
        Err(code) => {
            if compute.worker_pid().is_some() {
                let evidence = stop(compute, true);
                eprintln!("serve: released compute after {code}: {evidence:?}");
            }
            return Err(code);
        }
    };
    if report["deferred_ack"] == true {
        report["redial_ack_ms"] = json!(linger(s, owner, epoch, base, &fence, multi)?);
    }
    if multi {
        report["multi_request"] = json!(true);
        report["requests_served"] = json!(served.len());
        report["requests"] = json!(requests);
        report["connections_accepted"] = json!(connections.len());
    }
    report["worker_pid"] = json!(worker_pid);
    report["route_ready"] = json!(false);
    report["served_ms"] = json!(began.elapsed().as_secs_f64() * 1000.0);
    report["pid"] = json!(std::process::id());
    Ok(report)
}
/// After compute was released without a delivered ACK, accept the pinned
/// owner's re-dialed control connection for a bounded window and acknowledge
/// its PathCancellation for this epoch. `None` = no re-dial arrived in time.
fn linger(
    s: &Serve<'_, '_>,
    owner: iroh::EndpointId,
    epoch: u64,
    base: ConnectionFence,
    fence: &(dyn Fn() -> ConnectionFence + Sync),
    multi: bool,
) -> Outcome<Option<f64>> {
    let began = Instant::now();
    let window = budget(B::WorkerStartup)?;
    loop {
        let left = window.saturating_sub(began.elapsed());
        if left.is_zero() {
            return Ok(None);
        }
        let c = match s.host.accept(s.clock, mono(s.clock, left)?) {
            Ok(c) => c,
            // One failed inbound handshake (an abandoned or stale dial queued
            // ahead of the owner's re-dial, or a refused LAN path) must not end
            // the window: keep accepting until it elapses.
            Err(transport::Error::Authentication) => continue,
            Err(_) => return Ok(None),
        };
        if c.remote_id() != owner {
            c.close(2u32.into(), b"unpinned route owner");
            continue;
        }
        let mut receiver = RouterReceiver::new(c.clone(), s.runtime, s.clock, base, fence)
            .map_err(|e| format!("route_receiver:{e:?}"))?;
        let deadline = mono(s.clock, budget(B::RouterClose)?)?;
        let Ok(delivery) = receiver.receive(deadline) else {
            c.close(0u32.into(), b"route_served");
            continue;
        };
        let frame = router_wire::decode_frame(delivery.payload());
        let valid = frame.as_ref().is_ok_and(|f| {
            f.message_type() == "PathCancellation"
                && f.body()["topology_version"] == epoch
                && (!multi || valid_request_id(&f.body()["request_id"]))
        });
        if !valid {
            if let Err(e) = delivery.reject(DispatchRefusal::InvalidFrame) {
                eprintln!("serve: linger reject failed: {e:?}");
            }
            c.close(0u32.into(), b"route_served");
            continue;
        }
        let acknowledged = delivery.acknowledge().is_ok();
        let wait = budget(B::RouterClose)?;
        if s.runtime
            .block_on(async { tokio::time::timeout(wait, c.closed()).await })
            .is_err()
        {
            eprintln!("serve: owner close wait elapsed after re-dial");
        }
        if acknowledged {
            return Ok(Some(began.elapsed().as_secs_f64() * 1000.0));
        }
    }
}
