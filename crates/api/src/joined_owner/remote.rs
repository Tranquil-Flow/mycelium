// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's side of the signed member control channel as product code
//! (moved from the B8 owner test driver). One owned thread per member holds
//! the owner control endpoint (LocalOnly loopback, or direct LAN on exactly one
//! interface), admits each operator command with one signed call and polls its
//! member-signed result by operation id. After a successful `accept`, replies
//! must be signed by the member's enrolled key (`member_control::Remote`).
//! Every failure is a reply with a typed code; nothing panics or waits
//! unbounded: a single call is bounded by the local handshake budget and an
//! operation by the member's chosen wall.
use super::link::{MemberLink, Refusal};
use crate::member_control::{Owner, Remote};
use budgets::{BudgetId as B, MonoInstant};
use runtime::{
    iroh_control::NodeControlClient,
    iroh_host::{LanPolicy, LoopbackHost},
};
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    net::IpAddr,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
use transport::{Channel, Clock};

// Test-only transport/elapsed-time seams; absent from ordinary library builds.
#[cfg(test)]
#[path = "../../tests/support/remote_tracking_hooks.rs"]
pub(crate) mod tracking_hooks;

fn span(id: B) -> Result<Duration, Refusal> {
    super::span(id).map_err(Refusal::from)
}
/// A queued command and whether its reply is dropped instead of delivered
/// (the owner's own `cancel_operation`, which no caller waits on).
type Queued = (Value, bool);
/// The command sender, shared with cancellers; taken (closing the driver)
/// when the channel drops.
type Commands = Arc<Mutex<Option<mpsc::SyncSender<Queued>>>>;
/// Command sender + owned driver thread; replies arrive on the receiver given
/// at `start`, in the member channel's reply shape (`{ok, result | code}`).
pub struct RemoteChannel {
    commands: Commands,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl RemoteChannel {
    pub fn start(
        owner: iroh::SecretKey,
        member: iroh::EndpointAddr,
        lan: Option<(IpAddr, LanPolicy)>,
    ) -> Result<(Self, mpsc::Receiver<Value>), Refusal> {
        Self::start_remote(
            owner,
            Remote {
                addr: member,
                enrolled: None,
            },
            lan,
        )
    }
    fn start_remote(
        owner: iroh::SecretKey,
        member: Remote,
        lan: Option<(IpAddr, LanPolicy)>,
    ) -> Result<(Self, mpsc::Receiver<Value>), Refusal> {
        let budgets = Budgets {
            close: span(B::RouterClose)?,
            call: span(B::SidecarLocalHandshake)?,
            poll: span(B::PyMyceliumA13MacosManagedControlHttpRetryInitialSecondsL317)?,
            wall: span(B::MemberChosenWall)?,
            pending: usize::try_from(
                B::PyMyceliumA13MacosManagedControlMaxPendingCommandsL106
                    .count()
                    .map_err(|_| Refusal("budget_unavailable".into()))?
                    .get(),
            )
            .map_err(|_| Refusal("budget_unavailable".into()))?,
        };
        // Bounded both ways (REVIEW-laneB16 F3): queued commands and admitted
        // operations awaiting their result; past either, a typed refusal.
        let (tx, commands) = mpsc::sync_channel::<Queued>(budgets.pending);
        let (reply_tx, replies) = mpsc::channel();
        let (ready_tx, ready) = mpsc::channel();
        let thread = std::thread::Builder::new()
            .name("joined-owner-member-control".into())
            .spawn(move || drive(owner, member, lan, budgets, commands, reply_tx, ready_tx))
            .map_err(|_| Refusal("member_control_thread_failed".into()))?;
        let channel = Self {
            commands: Arc::new(Mutex::new(Some(tx))),
            thread: Some(thread),
        };
        match ready.recv_timeout(budgets.wall) {
            Ok(Ok(())) => Ok((channel, replies)),
            Ok(Err(e)) => Err(e),
            Err(_) => Err(Refusal("member_control_start_timeout".into())),
        }
    }
    pub fn send(&self, command: Value) -> Result<(), Refusal> {
        queue(&self.commands, (command, false))
    }
}
fn queue(commands: &Commands, queued: Queued) -> Result<(), Refusal> {
    commands
        .lock()
        .map_err(|_| Refusal("member_control_closed".into()))?
        .as_ref()
        .ok_or_else(|| Refusal("member_control_closed".into()))?
        .try_send(queued)
        .map_err(|e| {
            Refusal(
                match e {
                    mpsc::TrySendError::Full(_) => "member_control_busy",
                    mpsc::TrySendError::Disconnected(_) => "member_control_closed",
                }
                .into(),
            )
        })
}
impl Drop for RemoteChannel {
    fn drop(&mut self) {
        match self.commands.lock() {
            Ok(mut commands) => drop(commands.take()),
            Err(poisoned) => drop(poisoned.into_inner().take()),
        }
        if let Some(t) = self.thread.take()
            && t.join().is_err()
        {
            eprintln!("member control driver panicked");
        }
    }
}
#[derive(Clone, Copy)]
struct Budgets {
    close: Duration,
    call: Duration,
    poll: Duration,
    wall: Duration,
    /// Most queued commands and most admitted operations awaiting a result.
    pending: usize,
}
fn drive(
    secret: iroh::SecretKey,
    mut remote: Remote,
    lan: Option<(IpAddr, LanPolicy)>,
    budgets: Budgets,
    commands: mpsc::Receiver<Queued>,
    replies: mpsc::Sender<Value>,
    ready: mpsc::Sender<Result<(), Refusal>>,
) {
    let refuse = |code: String| {
        if ready.send(Err(Refusal(code))).is_err() {
            eprintln!("member control starter gone");
        }
    };
    let Ok(rt) = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
    else {
        return refuse("runtime_failed".into());
    };
    let clock = crate::managed_carrier::HostClock::default();
    let until = |d: Duration| -> Option<MonoInstant> {
        clock
            .monotonic()
            .checked_add(budgets::Span::from_millis(
                u64::try_from(d.as_millis()).ok()?,
            ))
            .ok()
    };
    let Some(bind_deadline) = until(budgets.close) else {
        return refuse("deadline_invalid".into());
    };
    let bound = match lan {
        Some((ip, policy)) => LoopbackHost::bind_lan(
            &rt,
            &clock,
            bind_deadline,
            secret.clone(),
            &[Channel::NodeControl],
            ip,
            policy,
        ),
        None => LoopbackHost::bind_protocols(
            &rt,
            &clock,
            bind_deadline,
            secret.clone(),
            &[Channel::NodeControl],
        ),
    };
    let mut host = match bound {
        Ok(host) => host,
        Err(e) => return refuse(format!("member_control_bind_failed:{e:?}")),
    };
    let owner = match Owner::new(&secret) {
        Ok(owner) => owner,
        Err(e) => return refuse(e.code.to_owned()),
    };
    let concurrency = match B::NodeControlCallConcurrency.count() {
        Ok(n) => n,
        Err(_) => return refuse("budget_unavailable".into()),
    };
    let client = match NodeControlClient::new(&host, &clock, concurrency) {
        Ok(client) => client,
        Err(e) => return refuse(format!("member_control_client_failed:{e:?}")),
    };
    if ready.send(Ok(())).is_err() {
        eprintln!("member control starter gone");
    }
    // One signed call, bounded by the local handshake budget.
    let call = |remote: &Remote, request: Value| -> Result<Value, String> {
        let deadline = until(budgets.call).ok_or("deadline_invalid")?;
        #[cfg(test)]
        if let Some(reply) = tracking_hooks::call(remote.addr.id, &request) {
            return reply;
        }
        remote
            .call(&owner, &client, request, deadline, crate::app_host::now())
            .map_err(|e| e.code.to_owned())
    };
    // Long operations are admitted only while the owner can track one more.
    let call_unless_full = |remote: &Remote,
                            command: Value,
                            name: &str,
                            tracked: usize|
     -> Option<Result<Value, String>> {
        let immediate = matches!(name, "cancel_operation" | "status" | "close");
        (immediate || tracked < budgets.pending).then(|| call(remote, command))
    };
    let send = |reply: Value| {
        if replies.send(reply).is_err() {
            eprintln!("member control reply receiver gone");
        }
    };
    let mut pending: VecDeque<(u64, String, Instant)> = VecDeque::new();
    // Result polls sent per pending operation (a failed poll's line names its attempt).
    let mut polls: std::collections::BTreeMap<u64, u64> = std::collections::BTreeMap::new();
    // Failed result polls per operation, rate-limited (lane FLK, FLK-05): the first at once, then
    // at most one summary per period, and one outcome line when the operation ends.
    let mut failed_polls = runtime::repeats::Repeats::<u64>::new(runtime::repeats::period());
    let member = remote.addr.id.fmt_short().to_string();
    let poll_outcome =
        |failed: &mut runtime::repeats::Repeats<u64>, id: u64, name: &str, outcome: &str| {
            if let Some(line) = failed.outcome_line(
                &id,
                Instant::now(),
                &format!("member result poll {id} ({name}) member={member}"),
                outcome,
            ) {
                eprintln!("{line}");
            }
        };
    loop {
        let next = if pending.is_empty() {
            commands.recv().ok()
        } else {
            match commands.recv_timeout(budgets.poll) {
                Ok(c) => Some(c),
                Err(mpsc::RecvTimeoutError::Timeout) => None,
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        };
        if let Some((command, quiet)) = next {
            let name = command["command"].as_str().unwrap_or_default().to_owned();
            let operation = command["operation_id"].as_u64();
            match call_unless_full(&remote, command, &name, pending.len()) {
                // The owner's own cancel: nobody waits on its answer (an
                // unanswered cancel shows as the operation's own reply).
                Some(Ok(reply)) if quiet => {
                    if reply["ok"] != true {
                        eprintln!("member cancel refused: {}", reply["code"]);
                    }
                }
                Some(Err(code)) if quiet => eprintln!("member cancel failed: {code}"),
                None if quiet => eprintln!("member cancel not sent: owner busy"),
                // Not sent: the member never starts what the owner cannot track.
                None => send(json!({"ok":false,"code":"member_control_busy"})),
                // Immediate member answers (never queued on the member).
                Some(Ok(reply)) if name == "cancel_operation" || name == "status" => send(reply),
                // The member stops its control channel once it closes: admission is the reply.
                Some(Ok(reply)) if name == "close" && reply["ok"] == true => {
                    send(json!({"ok":true,"result":{"closing":true}}))
                }
                Some(Ok(reply)) if reply["ok"] == true => match operation {
                    Some(id) => pending.push_back((id, name, Instant::now())),
                    None => send(json!({"ok":false,"code":"operator_operation_id_required"})),
                },
                Some(Ok(refusal)) => send(refusal),
                Some(Err(code)) => {
                    // Lane FLK: which member, which command, after how long (the reply's
                    // `control_call_failed:` code alone names none of them).
                    eprintln!(
                        "member control call {name} (op {operation:?}) member={}: {code}",
                        remote.addr.id.fmt_short()
                    );
                    send(json!({"ok":false,"code":format!("control_call_failed:{code}")}))
                }
            }
        } else if pending.is_empty() {
            break;
        }
        #[cfg(test)]
        tracking_hooks::age(remote.addr.id, &mut pending);
        if let Some((id, name, began)) = pending.front().cloned() {
            let attempt = {
                let n = polls.entry(id).or_insert(0);
                *n += 1;
                *n
            };
            match call(&remote, json!({"command":"result","operation_id":id})) {
                Ok(r) if r["result"]["finished"] == true => {
                    let reply = r["result"]["reply"].clone();
                    if name == "accept" && reply["ok"] == true {
                        // Paired: later replies must be signed by the enrolled
                        // key named in the registration; `link::admit` checks
                        // that claim against the seed row before the owner uses
                        // this link (a refused link is dropped).
                        remote.enrolled =
                            reply["result"]["message"]["member"]["verification_key_digest"]
                                .as_str()
                                .map(str::to_owned);
                    }
                    pending.pop_front();
                    polls.remove(&id);
                    poll_outcome(&mut failed_polls, id, &name, "finished");
                    send(
                        if name == "accept" && reply["ok"] == true && remote.enrolled.is_none() {
                            json!({"ok":false,"code":"member_registration_key_missing"})
                        } else {
                            reply
                        },
                    );
                }
                Ok(_) => {}
                Err(code) => {
                    if let Some(line) = failed_polls.line(
                        id,
                        Instant::now(),
                        &format!("member result poll {id} ({name}): {code}"),
                        &format!(
                            "member={} attempt={attempt} operation_ms={}",
                            remote.addr.id.fmt_short(),
                            began.elapsed().as_millis()
                        ),
                    ) {
                        eprintln!("{line}");
                    }
                    if began.elapsed() >= budgets.wall {
                        polls.remove(&id);
                        pending.pop_front();
                        let code = format!("control_call_failed:{code}");
                        poll_outcome(&mut failed_polls, id, &name, &code);
                        send(json!({"ok":false,"code":code}));
                        continue;
                    }
                }
            }
            if pending.front().is_some_and(|p| p.0 == id) && began.elapsed() >= budgets.wall {
                polls.remove(&id);
                pending.pop_front();
                poll_outcome(
                    &mut failed_polls,
                    id,
                    &name,
                    "member_operation_wall_exceeded",
                );
                send(json!({"ok":false,"code":"member_operation_wall_exceeded"}));
            }
        }
    }
    drop(client);
    if let Some(deadline) = until(budgets.close)
        && let Err(e) = host.close(&clock, deadline)
    {
        eprintln!("member control close: {e:?}");
    }
}
/// A remote member as a [`MemberLink`]: every command gets the next strictly
/// increasing operation id (remote results are retrieved by id).
pub struct RemoteMember {
    channel: RemoteChannel,
    replies: mpsc::Receiver<Value>,
    /// The last operation id sent (the member runs one operation at a time).
    last_op: Arc<AtomicU64>,
}
impl RemoteMember {
    /// Adopt a verified current registration without calling pairing or accept.
    pub fn already_joined(
        owner: iroh::SecretKey,
        coordinator: &seed::Coordinator,
        verified: &crate::member_control::joined::Verified,
        lan: Option<(IpAddr, LanPolicy)>,
    ) -> Result<Self, Refusal> {
        if owner.public() != verified.owner() {
            return Err(Refusal("member_work_run_changed".into()));
        }
        crate::joined_shard::verify_registration(coordinator, verified.registration())?;
        let key = verified.registration()["message"]["member"]["verification_key_digest"]
            .as_str()
            .ok_or_else(|| Refusal("member_registration_key_missing".into()))?
            .to_owned();
        let (channel, replies) = RemoteChannel::start_remote(
            owner,
            Remote {
                addr: verified.address().clone(),
                enrolled: Some(key),
            },
            lan,
        )?;
        Ok(Self {
            channel,
            replies,
            last_op: Arc::new(AtomicU64::new(0)),
        })
    }
    pub fn start(
        owner: iroh::SecretKey,
        member: iroh::EndpointAddr,
        lan: Option<(IpAddr, LanPolicy)>,
    ) -> Result<Self, Refusal> {
        let (channel, replies) = RemoteChannel::start(owner, member, lan)?;
        Ok(Self {
            channel,
            replies,
            last_op: Arc::new(AtomicU64::new(0)),
        })
    }
    /// Ask the member to cancel the operation this link sent last
    /// (`cancel_operation`, member_control v1; the member cancels only its
    /// admitted operation with exactly that id). Callable from any thread; its
    /// reply is not queued for `finish`: the cancelled operation answers.
    pub fn canceller(&self) -> impl Fn() + Send + Sync + 'static {
        let (commands, last) = (self.channel.commands.clone(), self.last_op.clone());
        move || {
            let id = last.load(Ordering::SeqCst);
            if id == 0 {
                return;
            }
            let cancel = json!({"command":"cancel_operation","operation_id":id});
            if let Err(e) = queue(&commands, (cancel, true)) {
                eprintln!("member cancel not queued: {}", e.0);
            }
        }
    }
}
impl MemberLink for RemoteMember {
    fn begin(&mut self, mut command: Value) -> Result<(), Refusal> {
        match command["operation_id"].as_u64() {
            Some(id) => {
                self.last_op.fetch_max(id, Ordering::SeqCst);
            }
            None if command["command"] != "cancel_operation" => {
                let id = self.last_op.fetch_add(1, Ordering::SeqCst) + 1;
                command["operation_id"] = json!(id);
            }
            None => {}
        }
        self.channel.send(command)
    }
    fn finish(&mut self, within: Duration) -> Result<Value, Refusal> {
        let v = self
            .replies
            .recv_timeout(within)
            .map_err(|_| Refusal("member_reply_timeout".into()))?;
        if v["ok"] == true {
            Ok(v["result"].clone())
        } else {
            Err(Refusal(
                v["code"].as_str().unwrap_or("member_refused").to_owned(),
            ))
        }
    }
}
