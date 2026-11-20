// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's side of the signed member control channel as product code
//! (moved from the B8 owner test driver). One owned thread per member drives
//! its calls through the run's ONE owner control endpoint per owner key ([`Hub`]:
//! LocalOnly loopback, direct LAN or across networks on exactly one interface), admits each operator command with one signed call and polls its
//! member-signed result by operation id. After a successful `accept`, replies
//! must be signed by the member's enrolled key (`member_control::Remote`).
//! Every control call is bounded by the local handshake budget. Finite operations
//! are tracked up to the member wall; a resident `serve` is tracked until its real
//! completion or channel shutdown, not ended by the owner's observation timeout.
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
            // Across networks the first call's connect runs through the relay (see
            // `runtime::iroh_host::control_handshake`); on the LAN it stays 2 s.
            call: runtime::iroh_host::control_handshake(
                lan.as_ref()
                    .is_some_and(|(_, policy)| policy.relay().is_some()),
            )
            .map_err(|_| Refusal("budget_unavailable".into()))?,
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
    // ONE owner control endpoint per owner key, shared by every member channel of the run
    // (see [`Hub`]): two endpoints under one key at the relay lose each other's replies.
    let hub = match Hub::acquire(&secret, lan, budgets.close) {
        Ok(hub) => hub,
        Err(code) => return refuse(code),
    };
    if ready.send(Ok(())).is_err() {
        eprintln!("member control starter gone");
    }
    // One signed call, bounded by the control handshake budget (relay-sized across networks).
    let call = |remote: &Remote, request: Value| -> Result<Value, String> {
        #[cfg(test)]
        if let Some(reply) = tracking_hooks::call(remote.addr.id, &request) {
            return reply;
        }
        hub.call(remote, request, budgets.call)
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
    // Once per resident serve, even if every earlier result poll succeeded.
    let mut tracking_timeouts = std::collections::BTreeSet::new();
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
                    tracking_timeouts.remove(&id);
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
                    if name != "serve" && began.elapsed() >= budgets.wall {
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
                if name == "serve" {
                    // Observation expiry is not evidence that the member ended its epoch.
                    // Keep the same operation and poll cadence; signed expiry, consent and
                    // the real member reply/carrier still govern the route and its cleanup.
                    if tracking_timeouts.insert(id) {
                        eprintln!(
                            "member_serve_tracking_timeout member={member} operation={id} elapsed_ms={} action=continue_polling",
                            began.elapsed().as_millis()
                        );
                    }
                    continue;
                }
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
    drop(hub);
}
/// One signed control call handed to the [`Hub`]: the member, the request, its allowance and
/// where the reply goes.
type Job = (Remote, Value, Duration, mpsc::Sender<Result<Value, String>>);
/// The run's ONE owner control endpoint for an owner key and network (lane XC), shared by every
/// member channel ([`RemoteChannel`]) of that key. Each channel used to bind its own endpoint
/// under the same key; across networks both registered at the relay, which keeps only the
/// NEWEST connection per key active (`iroh-relay-1.0.2/src/server/clients.rs:84-98`), so a
/// member's reply to the other endpoint was lost (rv52f hotspot: two owner endpoints
/// `id=3cda344136`, one per joined Mac; `control_read_timeout` on the first call). On the LAN
/// the duplicate was invisible (direct IP paths). The hub's thread binds the endpoint once and
/// runs calls on `NodeControlCallConcurrency` workers; the last channel's drop closes it.
struct Hub {
    key: (iroh::EndpointId, Option<(IpAddr, LanPolicy)>),
    jobs: Mutex<Option<mpsc::Sender<Job>>>,
    thread: Mutex<Option<std::thread::JoinHandle<()>>>,
}
/// The live hubs (weak: a hub lives as long as a channel holds it).
static HUBS: Mutex<Vec<std::sync::Weak<Hub>>> = Mutex::new(Vec::new());
impl Hub {
    fn acquire(
        secret: &iroh::SecretKey,
        lan: Option<(IpAddr, LanPolicy)>,
        close: Duration,
    ) -> Result<Arc<Self>, String> {
        let key = (secret.public(), lan);
        let mut hubs = HUBS.lock().map_err(|_| "member_control_hub_poisoned")?;
        hubs.retain(|h| h.strong_count() > 0);
        if let Some(hub) = hubs
            .iter()
            .filter_map(std::sync::Weak::upgrade)
            .find(|h| h.key == key)
        {
            return Ok(hub);
        }
        let (jobs, inbox) = mpsc::channel::<Job>();
        let (ready_tx, ready) = mpsc::channel();
        let (secret, lan) = (secret.clone(), key.1.clone());
        let thread = std::thread::Builder::new()
            .name("joined-owner-control-endpoint".into())
            .spawn(move || serve_hub(secret, lan, close, inbox, ready_tx))
            .map_err(|_| "member_control_thread_failed")?;
        match ready.recv() {
            Ok(Ok(())) => {}
            Ok(Err(code)) => {
                thread.join().ok();
                return Err(code);
            }
            Err(_) => return Err("member_control_thread_failed".into()),
        }
        let hub = Arc::new(Self {
            key,
            jobs: Mutex::new(Some(jobs)),
            thread: Mutex::new(Some(thread)),
        });
        hubs.push(Arc::downgrade(&hub));
        Ok(hub)
    }
    /// One signed call through the shared endpoint, bounded by `within`.
    fn call(&self, remote: &Remote, request: Value, within: Duration) -> Result<Value, String> {
        let (reply_tx, reply) = mpsc::channel();
        self.jobs
            .lock()
            .map_err(|_| "member_control_closed")?
            .as_ref()
            .ok_or("member_control_closed")?
            .send((remote.clone(), request, within, reply_tx))
            .map_err(|_| "member_control_closed")?;
        reply
            .recv()
            .map_err(|_| "member_control_closed".to_owned())?
    }
    #[cfg(test)]
    fn live(owner: iroh::EndpointId) -> usize {
        HUBS.lock()
            .unwrap()
            .iter()
            .filter_map(std::sync::Weak::upgrade)
            .filter(|h| h.key.0 == owner)
            .count()
    }
}
impl Drop for Hub {
    fn drop(&mut self) {
        match self.jobs.lock() {
            Ok(mut jobs) => drop(jobs.take()),
            Err(poisoned) => drop(poisoned.into_inner().take()),
        }
        let thread = match self.thread.lock() {
            Ok(mut t) => t.take(),
            Err(poisoned) => poisoned.into_inner().take(),
        };
        if let Some(t) = thread
            && t.join().is_err()
        {
            eprintln!("member control endpoint panicked");
        }
    }
}
fn serve_hub(
    secret: iroh::SecretKey,
    lan: Option<(IpAddr, LanPolicy)>,
    close: Duration,
    inbox: mpsc::Receiver<Job>,
    ready: mpsc::Sender<Result<(), String>>,
) {
    let refuse = |code: String| {
        if ready.send(Err(code)).is_err() {
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
    let Some(bind_deadline) = until(close) else {
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
    let workers = match B::NodeControlCallConcurrency.count() {
        Ok(n) => n,
        Err(_) => return refuse("budget_unavailable".into()),
    };
    {
        let client = match NodeControlClient::new(&host, &clock, workers) {
            Ok(client) => client,
            Err(e) => return refuse(format!("member_control_client_failed:{e:?}")),
        };
        if ready.send(Ok(())).is_err() {
            eprintln!("member control starter gone");
        }
        let inbox = Mutex::new(inbox);
        let workers = usize::try_from(workers.get()).unwrap_or(1).max(1);
        std::thread::scope(|scope| {
            for _ in 0..workers {
                scope.spawn(|| {
                    loop {
                        let job = match inbox.lock() {
                            Ok(inbox) => inbox.recv(),
                            Err(_) => return,
                        };
                        let Ok((remote, request, within, reply)) = job else {
                            return;
                        };
                        let outcome = until(within)
                            .ok_or_else(|| "deadline_invalid".to_owned())
                            .and_then(|deadline| {
                                remote
                                    .call(
                                        &owner,
                                        &client,
                                        request,
                                        deadline,
                                        crate::app_host::now(),
                                    )
                                    .map_err(|e| e.code.to_owned())
                            });
                        if reply.send(outcome).is_err() {
                            eprintln!("member control caller gone");
                        }
                    }
                });
            }
        });
    }
    if let Some(deadline) = until(close)
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

#[cfg(test)]
mod hub_tests {
    use super::*;
    /// Lane XC: every member channel of one owner key shares ONE owner control endpoint (two
    /// endpoints under one key at the relay lose each other's replies); the last drop closes it.
    #[test]
    fn member_channels_of_one_owner_share_one_control_endpoint() {
        let owner = iroh::SecretKey::generate();
        let other = iroh::SecretKey::generate();
        let member =
            |n: u8| iroh::EndpointAddr::new(iroh::SecretKey::from_bytes(&[n; 32]).public());
        assert_eq!(Hub::live(owner.public()), 0);
        let (a, _ra) = RemoteChannel::start(owner.clone(), member(1), None).unwrap();
        let (b, _rb) = RemoteChannel::start(owner.clone(), member(2), None).unwrap();
        let (c, _rc) = RemoteChannel::start(other.clone(), member(3), None).unwrap();
        assert_eq!(
            Hub::live(owner.public()),
            1,
            "one endpoint for both members of one owner"
        );
        assert_eq!(
            Hub::live(other.public()),
            1,
            "another owner key binds its own"
        );
        drop(a);
        assert_eq!(
            Hub::live(owner.public()),
            1,
            "still held by the second channel"
        );
        drop(b);
        assert_eq!(
            Hub::live(owner.public()),
            0,
            "the last channel's drop closes it"
        );
        drop(c);
        assert_eq!(Hub::live(other.public()), 0);
    }
}
