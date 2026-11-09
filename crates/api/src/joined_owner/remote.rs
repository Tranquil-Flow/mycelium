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
    sync::mpsc,
    time::{Duration, Instant},
};
use transport::{Channel, Clock};

fn span(id: B) -> Result<Duration, Refusal> {
    super::span(id).map_err(Refusal::from)
}
/// Command sender + owned driver thread; replies arrive on the receiver given
/// at `start`, in the member channel's reply shape (`{ok, result | code}`).
pub struct RemoteChannel {
    commands: Option<mpsc::Sender<Value>>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl RemoteChannel {
    pub fn start(
        owner: iroh::SecretKey,
        member: iroh::EndpointAddr,
        lan: Option<(IpAddr, LanPolicy)>,
    ) -> Result<(Self, mpsc::Receiver<Value>), Refusal> {
        let budgets = Budgets {
            close: span(B::RouterClose)?,
            call: span(B::SidecarLocalHandshake)?,
            poll: span(B::PyMyceliumA13MacosManagedControlHttpRetryInitialSecondsL317)?,
            wall: span(B::MemberChosenWall)?,
        };
        let (tx, commands) = mpsc::channel::<Value>();
        let (reply_tx, replies) = mpsc::channel();
        let (ready_tx, ready) = mpsc::channel();
        let thread = std::thread::Builder::new()
            .name("joined-owner-member-control".into())
            .spawn(move || drive(owner, member, lan, budgets, commands, reply_tx, ready_tx))
            .map_err(|_| Refusal("member_control_thread_failed".into()))?;
        let channel = Self {
            commands: Some(tx),
            thread: Some(thread),
        };
        match ready.recv_timeout(budgets.wall) {
            Ok(Ok(())) => Ok((channel, replies)),
            Ok(Err(e)) => Err(e),
            Err(_) => Err(Refusal("member_control_start_timeout".into())),
        }
    }
    pub fn send(&self, command: Value) -> Result<(), Refusal> {
        self.commands
            .as_ref()
            .ok_or_else(|| Refusal("member_control_closed".into()))?
            .send(command)
            .map_err(|_| Refusal("member_control_closed".into()))
    }
}
impl Drop for RemoteChannel {
    fn drop(&mut self) {
        self.commands.take();
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
}
fn drive(
    secret: iroh::SecretKey,
    member: iroh::EndpointAddr,
    lan: Option<(IpAddr, LanPolicy)>,
    budgets: Budgets,
    commands: mpsc::Receiver<Value>,
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
    let mut remote = Remote {
        addr: member,
        enrolled: None,
    };
    // One signed call, bounded by the local handshake budget.
    let call = |remote: &Remote, request: Value| -> Result<Value, String> {
        let deadline = until(budgets.call).ok_or("deadline_invalid")?;
        remote
            .call(&owner, &client, request, deadline, crate::app_host::now())
            .map_err(|e| e.code.to_owned())
    };
    let send = |reply: Value| {
        if replies.send(reply).is_err() {
            eprintln!("member control reply receiver gone");
        }
    };
    let mut pending: VecDeque<(u64, String, Instant)> = VecDeque::new();
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
        if let Some(command) = next {
            let name = command["command"].as_str().unwrap_or_default().to_owned();
            match call(&remote, command.clone()) {
                // Immediate member answers (never queued on the member).
                Ok(reply) if name == "cancel_operation" || name == "status" => send(reply),
                // The member stops its control channel once it closes: admission is the reply.
                Ok(reply) if name == "close" && reply["ok"] == true => {
                    send(json!({"ok":true,"result":{"closing":true}}))
                }
                Ok(reply) if reply["ok"] == true => match command["operation_id"].as_u64() {
                    Some(id) => pending.push_back((id, name, Instant::now())),
                    None => send(json!({"ok":false,"code":"operator_operation_id_required"})),
                },
                Ok(refusal) => send(refusal),
                Err(code) => send(json!({"ok":false,"code":format!("control_call_failed:{code}")})),
            }
        } else if pending.is_empty() {
            break;
        }
        if let Some((id, name, began)) = pending.front().cloned() {
            match call(&remote, json!({"command":"result","operation_id":id})) {
                Ok(r) if r["result"]["finished"] == true => {
                    let reply = r["result"]["reply"].clone();
                    if name == "accept" && reply["ok"] == true {
                        // Paired: later replies must be signed by the enrolled
                        // key named in the registration (the owner checks that
                        // registration against the seed row before any offer).
                        remote.enrolled =
                            reply["result"]["message"]["member"]["verification_key_digest"]
                                .as_str()
                                .map(str::to_owned);
                    }
                    pending.pop_front();
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
                    eprintln!("member result poll {id} ({name}): {code}");
                    if began.elapsed() >= budgets.wall {
                        pending.pop_front();
                        send(json!({"ok":false,"code":format!("control_call_failed:{code}")}));
                        continue;
                    }
                }
            }
            if pending.front().is_some_and(|p| p.0 == id) && began.elapsed() >= budgets.wall {
                pending.pop_front();
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
    last_op: u64,
}
impl RemoteMember {
    pub fn start(
        owner: iroh::SecretKey,
        member: iroh::EndpointAddr,
        lan: Option<(IpAddr, LanPolicy)>,
    ) -> Result<Self, Refusal> {
        let (channel, replies) = RemoteChannel::start(owner, member, lan)?;
        Ok(Self {
            channel,
            replies,
            last_op: 0,
        })
    }
}
impl MemberLink for RemoteMember {
    fn begin(&mut self, mut command: Value) -> Result<(), Refusal> {
        match command["operation_id"].as_u64() {
            Some(id) => self.last_op = self.last_op.max(id),
            None if command["command"] != "cancel_operation" => {
                self.last_op += 1;
                command["operation_id"] = json!(self.last_op);
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
