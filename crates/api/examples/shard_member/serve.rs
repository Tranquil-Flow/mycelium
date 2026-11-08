// SPDX-License-Identifier: AGPL-3.0-only
//! One owner-signed route epoch over the Router ALPN. The owner dials a data
//! connection (hops/results) then a control connection (PathCancellation). This
//! member validates every hop against the signed route, steps only its own
//! native worker, and ACKs a PathCancellation only after that worker was closed
//! (graceful) or killed and reaped (interrupted step). No route_ready claim.
use super::{Result, control::Operation, membership::Membership};
use api::joined_shard::{native::NativeShard, route};
use budgets::{BudgetId as B, Entries, MonoInstant};
use protocol::{identity::Identity, router_wire};
use runtime::{
    iroh::IrohTransport,
    iroh_host::LoopbackHost,
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
type HostClock = api::managed_carrier::HostClock;
enum Message {
    Hop(Value, Vec<u8>),
    Teardown(
        Value,
        Instant,
        mpsc::SyncSender<std::result::Result<(), String>>,
        mpsc::Receiver<()>,
    ),
    Closed(&'static str),
}
fn until(clock: &HostClock, operation: &Operation) -> Result<MonoInstant> {
    let left = operation.deadline.saturating_duration_since(Instant::now());
    clock
        .monotonic()
        .checked_add(budgets::Span::from_millis(
            left.as_millis()
                .try_into()
                .map_err(|_| "deadline_invalid")?,
        ))
        .map_err(|_| "deadline_invalid".into())
}
fn budget(id: B) -> Result<Duration> {
    Ok(Duration::from_millis(
        id.span(Entries::ZERO)
            .map_err(|_| "budget_unavailable")?
            .millis(),
    ))
}
#[allow(clippy::too_many_arguments)]
pub fn serve(
    host: &LoopbackHost<'_>,
    rt: &tokio::runtime::Runtime,
    clock: &HostClock,
    native: &mut NativeShard,
    member: &Membership,
    signer: &Identity,
    route_envelope: &Value,
    proof_digest: &str,
    operation: &Operation,
) -> Result<Value> {
    let began = Instant::now();
    let live = member.current(signer).map_err(|e| e.0.to_owned())?;
    let (index, message) = route::verify_member(
        route_envelope,
        &live,
        native.offer(),
        proof_digest,
        api::app_host::now(),
    )
    .map_err(|e| e.0.to_owned())?;
    let owner: iroh::EndpointId = message["owner_endpoint_id"]
        .as_str()
        .ok_or("route_owner_invalid")?
        .parse()
        .map_err(|_| "route_owner_invalid")?;
    let generation = live["generation"].as_u64().ok_or("generation_invalid")?;
    let epoch = message["topology_version"]
        .as_u64()
        .ok_or("route_invalid")?;
    let worker_pid = native.worker_pid();
    let mut connections = Vec::new();
    for _ in 0..2 {
        let c = host
            .accept(clock, until(clock, operation)?)
            .map_err(|e| format!("route_accept_failed:{e:?}"))?;
        if c.remote_id() != owner {
            c.close(2u32.into(), b"unpinned route owner");
            return Err("route_carrier_unpinned".into());
        }
        connections.push(c);
    }
    let base = ConnectionFence {
        endpoint_id: *owner.as_bytes(),
        local_generation: generation,
        remote_generation: epoch,
    };
    let current = move || ConnectionFence {
        local_generation: member
            .current(signer)
            .ok()
            .and_then(|m| m["generation"].as_u64())
            .unwrap_or(u64::MAX),
        ..base
    };
    let mut sender = IrohTransport::new(connections[0].clone(), rt, clock, base, current)
        .map_err(|e| format!("route_sender:{e:?}"))?;
    let data_cancel = sender.cancellation_handle();
    let receivers: Vec<_> = connections
        .iter()
        .map(|c| RouterReceiver::new(c.clone(), rt, clock, base, current))
        .collect::<std::result::Result<_, _>>()
        .map_err(|e| format!("route_receiver:{e:?}"))?;
    let cancel = Arc::new(AtomicBool::new(false));
    let (tx, rx) = mpsc::channel();
    let limit = until(clock, operation)?;
    let poll = budget(B::OwnerPoll)?;
    let hop_budget = |t: u64| {
        budget(if t == 0 {
            B::WorkerStartup
        } else {
            B::PyFinalScriptsRunA11RouteBenchmarkTargetTpotMsL398C12
        })
    };
    let stages = message["stages"].as_array().ok_or("route_invalid")?.len();
    let report = std::thread::scope(|scope| -> Result<Value> {
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
                                eprintln!("serve.rs: route already settled at receiver close");
                            }
                            return;
                        }
                    };
                    if delivery.message_id() != expected.to_be_bytes() {
                        if let Err(e) = delivery.reject(DispatchRefusal::ReplayCollision) {
                            eprintln!("serve.rs: delivery.reject failed: {e:?}");
                        }
                        continue;
                    }
                    expected += 1;
                    let Ok(frame) = router_wire::decode_frame(delivery.payload()) else {
                        if let Err(e) = delivery.reject(DispatchRefusal::InvalidFrame) {
                            eprintln!("serve.rs: delivery.reject failed: {e:?}");
                        }
                        continue;
                    };
                    match frame.message_type() {
                        "HopHeader" if role == 0 => {
                            // Single owner-serial dispatch slot: ACK = queued for native.
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
                            // ACK only after native close/reap actually completed.
                            match stopped.recv() {
                                Ok(Ok(())) => {
                                    if let Err(e) = delivery.acknowledge() {
                                        eprintln!("serve.rs: delivery.acknowledge failed: {e:?}");
                                    }
                                }
                                _ => {
                                    if let Err(e) = delivery.reject(DispatchRefusal::QueueFull) {
                                        eprintln!("serve.rs: delivery.reject failed: {e:?}");
                                    }
                                }
                            }
                            if let Err(e) = acked_tx.send(()) {
                                eprintln!("serve.rs: acked_tx.send failed: {e:?}");
                            }
                            return;
                        }
                        _ => {
                            if let Err(e) = delivery.reject(DispatchRefusal::InvalidFrame) {
                                eprintln!("serve.rs: delivery.reject failed: {e:?}");
                            }
                        }
                    }
                }
            });
        }
        drop(tx);
        let outcome = (|| -> Result<Value> {
            let mut path: Option<route::Path> = None;
            let mut t: u64 = 0;
            let mut sent: u128 = 0;
            let mut interrupted = false;
            let mut failure: Option<String> = None;
            let mut step_ms = Vec::new();
            let stop = |native: &mut NativeShard,
                        interrupted: bool|
             -> std::result::Result<Value, String> {
                if native.worker_pid().is_none() {
                    return Ok(json!({"worker":"absent"}));
                }
                if !interrupted && let Ok(stats) = native.close() {
                    return Ok(
                        json!({"context_closed":stats["context_closed"],"graceful":true,"stats":stats}),
                    );
                }
                native
                    .abort()
                    .map(|e| json!({"graceful":false,"reap":e}))
                    .map_err(|e| e.0.to_owned())
            };
            loop {
                let received = match rx.recv_timeout(poll) {
                    Ok(m) => m,
                    Err(mpsc::RecvTimeoutError::Timeout) => {
                        if operation.check().is_err() {
                            cancel.store(true, Ordering::SeqCst);
                            data_cancel.request_cancel();
                            let native_stop = stop(native, interrupted)?;
                            return Ok(
                                json!({"teardown":"operator_cancelled","native":native_stop}),
                            );
                        }
                        continue;
                    }
                    Err(mpsc::RecvTimeoutError::Disconnected) => {
                        let native_stop = stop(native, interrupted)?;
                        return Ok(
                            json!({"teardown":"carrier_lost","native":native_stop,"tokens_served":t}),
                        );
                    }
                };
                match received {
                    Message::Hop(body, payload) => {
                        if cancel.load(Ordering::SeqCst) || failure.is_some() {
                            continue;
                        }
                        if path.is_none() && t == 0 && body["phase"] == "PREFILL" {
                            path = Some(route::Path {
                                request_id: body["request_id"]
                                    .as_str()
                                    .unwrap_or_default()
                                    .to_owned(),
                                path_id: body["path_id"].as_str().unwrap_or_default().to_owned(),
                                path_attempt: body["path_attempt"].as_u64().unwrap_or(u64::MAX),
                            });
                        }
                        let Some(p) = path.as_ref() else {
                            failure = Some("route_hop_unexpected".into());
                            continue;
                        };
                        let expected =
                            route::hop(&message, p, t, index).map_err(|e| e.0.to_owned())?;
                        let normal = router_wire::encode_frame("HopHeader", &body, &[])
                            .and_then(|b| router_wire::decode_frame(&b));
                        if !normal.is_ok_and(|f| route::body_matches(&f, "HopHeader", &expected)) {
                            failure = Some("route_hop_binding".into());
                            continue;
                        }
                        let tokens = if index == 0 {
                            match route::entry_tokens(&payload) {
                                Ok(ids) => Some(ids),
                                Err(e) => {
                                    failure = Some(e.0.into());
                                    continue;
                                }
                            }
                        } else {
                            None
                        };
                        let step_began = Instant::now();
                        let deadline = Instant::now() + hop_budget(t)?;
                        let output = native.step_until(
                            tokens.as_deref(),
                            if tokens.is_some() { &[] } else { &payload },
                            &|| member.current(signer),
                            cancel.clone(),
                            deadline,
                        );
                        let bytes = match output {
                            Ok(bytes) => bytes,
                            Err(e) => {
                                // A cancelled/expired socket exchange leaves framing unusable.
                                interrupted = true;
                                if !cancel.load(Ordering::SeqCst) {
                                    failure = Some(e.0.into());
                                }
                                continue;
                            }
                        };
                        step_ms.push(step_began.elapsed().as_secs_f64() * 1000.0);
                        let frame = if index + 1 == stages {
                            let token = api::joined_shard::native::greedy(&bytes)
                                .map_err(|e| e.0.to_owned())?;
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
                            continue;
                        }
                        let request = Exchange {
                            peer: base,
                            channel: Channel::Router,
                            message_id: sent.to_be_bytes(),
                            payload: frame,
                            deadline: clock
                                .monotonic()
                                .checked_add(budgets::Span::from_millis(
                                    hop_budget(t)?
                                        .as_millis()
                                        .try_into()
                                        .map_err(|_| "deadline_invalid")?,
                                ))
                                .map_err(|_| "deadline_invalid")?,
                            maximum_response_bytes: protocol::remote::REMOTE_MAX_FRAME_BYTES,
                        };
                        if let Err(e) = transport::exchange(&mut sender, &request, clock) {
                            if !cancel.load(Ordering::SeqCst) {
                                failure = Some(format!("route_send_failed:{e:?}"));
                            }
                            continue;
                        }
                        sent += 1;
                        t += 1;
                    }
                    Message::Teardown(body, received, done, acked) => {
                        let matches = path
                            .as_ref()
                            .is_none_or(|p| body == route::cancellation(&message, p))
                            && body["topology_version"] == message["topology_version"];
                        let native_stop = stop(native, interrupted);
                        let ok = native_stop.is_ok();
                        if let Err(e) =
                            done.send(native_stop.as_ref().map(drop).map_err(Clone::clone))
                        {
                            eprintln!("serve.rs: done.send failed: {e:?}");
                        }
                        if let Err(e) = acked.recv_timeout(budget(B::RouterClose)?) {
                            eprintln!("serve.rs: acked.recv_timeout failed: {e:?}");
                        }
                        // The owner closes after reading every ACK; closing first could
                        // discard the queued ACK bytes. Wait (bounded) for its close.
                        let wait = budget(B::RouterClose)?;
                        for c in &connections {
                            // Elapsed means the owner never closed: recorded, still bounded.
                            if rt
                                .block_on(async { tokio::time::timeout(wait, c.closed()).await })
                                .is_err()
                            {
                                eprintln!("serve.rs: owner close wait elapsed");
                            }
                        }
                        let native_stop = native_stop?;
                        return Ok(
                            json!({"teardown":"path_cancellation","binding_matched":matches,
                        "stopped_ms":received.elapsed().as_secs_f64()*1000.0,"native":native_stop,
                        "native_stopped":ok,"tokens_served":t,"interrupted_step":interrupted,
                        "failure":failure,"request_id":path.as_ref().map(|p| p.request_id.clone()),
                        "stage_index":index,"step_ms":step_ms}),
                        );
                    }
                    Message::Closed(which) => {
                        if which == "route_control_closed" {
                            // No owner can deliver teardown any more: release compute now.
                            cancel.store(true, Ordering::SeqCst);
                            data_cancel.request_cancel();
                            let native_stop = stop(native, interrupted)?;
                            return Ok(
                                json!({"teardown":"control_lost","native":native_stop,"tokens_served":t,
                            "failure":failure,"stage_index":index}),
                            );
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
    let mut report = report?;
    report["worker_pid"] = json!(worker_pid);
    report["route_ready"] = json!(false);
    report["served_ms"] = json!(began.elapsed().as_secs_f64() * 1000.0);
    report["pid"] = json!(std::process::id());
    Ok(report)
}
