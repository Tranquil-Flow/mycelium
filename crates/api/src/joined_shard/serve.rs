// SPDX-License-Identifier: AGPL-3.0-only
//! Member-side Router stage server for ONE owner-signed route epoch.
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
    fn reset(&mut self, current: Current<'_>) -> Result<Value, Error> {
        let _ = current;
        Err(Error("joined_native_reset_unsupported"))
    }
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
fn budget(id: B) -> Outcome<Duration> {
    Ok(Duration::from_millis(
        id.span(Entries::ZERO)
            .map_err(|_| "budget_unavailable")?
            .millis(),
    ))
}
enum Message {
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
    let worker_pid = compute.worker_pid();
    let mut connections = Vec::new();
    let setup = s.until(None)?;
    while connections.len() < 2 {
        // Within the one setup deadline: a failed inbound handshake (stale or
        // refused path) or an unpinned dialer is dropped, never served and
        // never allowed to abort the owner's route (no remote kill switch).
        let c = match s.host.accept(s.clock, setup) {
            Ok(c) => c,
            Err(transport::Error::Authentication) => continue,
            Err(e) => {
                // Compute is still owned here: release it before refusing.
                stop(compute, false)?;
                return Err(format!("route_accept_failed:{e:?}"));
            }
        };
        if c.remote_id() != owner {
            c.close(2u32.into(), b"unpinned route owner");
            continue;
        }
        connections.push(c);
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
    let stages = message["stages"].as_array().ok_or("route_invalid")?.len();
    let report = std::thread::scope(|scope| -> Outcome<Value> {
        for (role, mut receiver) in receivers.into_iter().enumerate() {
            let (tx, cancel, data_cancel) = (tx.clone(), cancel.clone(), &data_cancel);
            scope.spawn(move || {
                let mut expected: u128 = 0;
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
                            cancel.store(true, Ordering::SeqCst);
                            // Unblock a pending result send; framing there is moot.
                            data_cancel.request_cancel();
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
                            return;
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
        let outcome = (|| -> Outcome<Value> {
            let mut path: Option<route::Path> = None;
            let mut t: u64 = 0;
            let mut sent: u128 = 0;
            let mut interrupted = false;
            let mut failure: Option<String> = None;
            // Compute already released after a local failure (exactly once).
            let mut released: Option<Value> = None;
            let mut step_ms = Vec::new();
            loop {
                let received = match rx.recv_timeout(poll) {
                    Ok(m) => m,
                    Err(mpsc::RecvTimeoutError::Timeout) => {
                        if s.check().is_err() {
                            cancel.store(true, Ordering::SeqCst);
                            data_cancel.request_cancel();
                            let native_stop = match released.take() {
                                Some(v) => v,
                                None => stop(compute, interrupted)?,
                            };
                            return Ok(
                                json!({"teardown":"operator_cancelled","native":native_stop,"failure":failure}),
                            );
                        }
                        continue;
                    }
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
                match received {
                    Message::Hop(body, payload) => {
                        if cancel.load(Ordering::SeqCst) || failure.is_some() {
                            continue;
                        }
                        let handled = (|| -> Outcome<()> {
                            if path.is_none() && t == 0 && body["phase"] == "PREFILL" {
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
                            let deadline = Instant::now() + hop_budget(t)?;
                            let bytes = compute
                                .step(
                                    tokens.as_deref(),
                                    if tokens.is_some() { &[] } else { &payload },
                                    s.current,
                                    cancel.clone(),
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
                            if cancel.load(Ordering::SeqCst) {
                                return Ok(());
                            }
                            let request = Exchange {
                                peer: base,
                                channel: Channel::Router,
                                message_id: sent.to_be_bytes(),
                                payload: frame,
                                deadline: mono(s.clock, hop_budget(t)?)?,
                                maximum_response_bytes: protocol::remote::REMOTE_MAX_FRAME_BYTES,
                            };
                            transport::exchange(&mut sender, &request, s.clock)
                                .map_err(|e| format!("route_send_failed:{e:?}"))?;
                            sent += 1;
                            t += 1;
                            Ok(())
                        })();
                        if let Err(code) = handled
                            && !cancel.load(Ordering::SeqCst)
                        {
                            // Release compute now; keep waiting for the owner's
                            // teardown so it can settle against an ACK.
                            failure = Some(code);
                            released = Some(stop(compute, interrupted)?);
                        }
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
                            linger(s, owner, epoch, base, &fence)?
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
                            let native_stop = match released.take() {
                                Some(v) => v,
                                None => stop(compute, interrupted)?,
                            };
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
        report["redial_ack_ms"] = json!(linger(s, owner, epoch, base, &fence)?);
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
            f.message_type() == "PathCancellation" && f.body()["topology_version"] == epoch
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
