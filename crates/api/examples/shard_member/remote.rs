// SPDX-License-Identifier: AGPL-3.0-only
//! Member side of the signed owner<->member control channel
//! (`api::member_control`). An owned thread with its own runtime and NodeControl
//! endpoint accepts ONLY the operator-pinned owner control endpoint, verifies
//! every signed command (pin, member endpoint, sequence, window), and feeds the
//! same serial operation queue as stdin. Replies are signed with the member's
//! app (enrolled) key. `status` and `result` answer immediately, even while a
//! long operation (receive/prepare/serve) runs on the main loop.
use super::control::{Dispatcher, Origin};
use api::{
    managed_carrier::HostClock,
    member_control,
    process_identity::{Incarnation, Liveness},
};
use budgets::{BudgetId as B, Entries, MonoInstant};
use protocol::identity::Identity;
use runtime::{
    iroh_control_receive::NodeControlReceiver,
    iroh_host::{LanPolicy, LoopbackHost},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    net::{IpAddr, SocketAddr},
    sync::{
        Arc, LockResult, Mutex, MutexGuard,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
};
use transport::{Channel, Clock, ConnectionFence};
type Result<T> = std::result::Result<T, String>;
pub struct Remote {
    pub endpoint: iroh::EndpointId,
    pub addrs: Vec<SocketAddr>,
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl Remote {
    pub fn stop(mut self) -> Result<()> {
        self.stop.store(true, Ordering::SeqCst);
        self.thread
            .take()
            .ok_or("member_control_missing")?
            .join()
            .map_err(|_| "member_control_panicked".into())
    }
}
fn until(clock: &HostClock, id: B) -> Result<MonoInstant> {
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
pub fn start(
    owner: iroh::EndpointId,
    lan: Option<(IpAddr, LanPolicy)>,
    dispatcher: Arc<Dispatcher>,
    signer: Identity,
    status: Status,
) -> Result<Remote> {
    let stop = Arc::new(AtomicBool::new(false));
    let (ready_tx, ready_rx) = mpsc::sync_channel(1);
    let flag = stop.clone();
    let thread = std::thread::spawn(move || {
        if let Err(code) = serve(owner, lan, dispatcher, signer, status, flag, ready_tx) {
            eprintln!("member_control_failed:{code}");
        }
    });
    let wait = std::time::Duration::from_millis(
        B::WorkerStartup
            .span(Entries::ZERO)
            .map_err(|_| "budget_unavailable")?
            .millis(),
    );
    let (endpoint, addrs) = ready_rx
        .recv_timeout(wait)
        .map_err(|_| "member_control_bind_failed")??;
    Ok(Remote {
        endpoint,
        addrs,
        stop,
        thread: Some(thread),
    })
}
type Ready = mpsc::SyncSender<Result<(iroh::EndpointId, Vec<SocketAddr>)>>;
fn serve(
    owner: iroh::EndpointId,
    lan: Option<(IpAddr, LanPolicy)>,
    dispatcher: Arc<Dispatcher>,
    signer: Identity,
    status: Status,
    stop: Arc<AtomicBool>,
    ready: Ready,
) -> Result<()> {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .map_err(|_| "runtime_failed")?;
    let clock = HostClock::default();
    let secret = iroh::SecretKey::generate();
    let bound = match lan {
        Some((ip, policy)) => LoopbackHost::bind_lan(
            &rt,
            &clock,
            until(&clock, B::RouterClose)?,
            secret,
            &[Channel::NodeControl],
            ip,
            policy,
        ),
        None => LoopbackHost::bind_protocols(
            &rt,
            &clock,
            until(&clock, B::RouterClose)?,
            secret,
            &[Channel::NodeControl],
        ),
    };
    let mut host = match bound {
        Ok(host) => host,
        Err(e) => {
            let code = format!("member_control_bind_failed:{e:?}");
            ready
                .send(Err(code.clone()))
                .map_err(|_| "member_control_ready")?;
            return Err(code);
        }
    };
    let addr = host.address().map_err(|e| format!("{e:?}"))?;
    let me = addr.id;
    ready
        .send(Ok((me, addr.ip_addrs().copied().collect())))
        .map_err(|_| "member_control_ready")?;
    let fence = ConnectionFence {
        endpoint_id: *owner.as_bytes(),
        local_generation: 0,
        remote_generation: 0,
    };
    let mut last = 0u64;
    while !stop.load(Ordering::SeqCst) {
        // Bounded accept so the stop flag is observed within one owner poll.
        let Ok(connection) = host.accept(&clock, until(&clock, B::OwnerPoll)?) else {
            continue;
        };
        if connection.remote_id() != owner {
            connection.close(2u32.into(), b"unpinned control owner");
            continue;
        }
        let Ok(mut receiver) =
            NodeControlReceiver::new(connection.clone(), &rt, &clock, fence, || fence)
        else {
            connection.close(2u32.into(), b"control channel refused");
            continue;
        };
        // The owner client makes one connection per call.
        if let Ok(delivery) = receiver.receive(until(&clock, B::RouterClose)?) {
            let command: Value = serde_json::from_slice(delivery.bytes()).unwrap_or(Value::Null);
            let answer = match member_control::verify_command(
                &command,
                &owner,
                &me,
                last,
                api::app_host::now(),
            ) {
                Ok((sequence, request)) => {
                    last = sequence;
                    handle(&dispatcher, &status, request)
                }
                Err(e) => json!({"ok":false,"code":e.code}),
            };
            match member_control::sign_reply(&signer, &command, answer)
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
        }
        let wait = std::time::Duration::from_millis(
            B::RouterClose
                .span(Entries::ZERO)
                .map_err(|_| "budget_unavailable")?
                .millis(),
        );
        // The client closes after reading the response; bounded wait, then close.
        if rt
            .block_on(async { tokio::time::timeout(wait, connection.closed()).await })
            .is_err()
        {
            connection.close(0u32.into(), b"control call served");
        }
    }
    host.close(&clock, until(&clock, B::RouterClose)?)
        .map_err(|e| format!("{e:?}"))?;
    // `host` drops before `rt` (reverse declaration order): sockets are reaped.
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
