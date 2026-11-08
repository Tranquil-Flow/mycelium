// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-side driver of a member over the signed remote control channel
//! (`api::member_control` over NodeControl). One owned thread per member link
//! holds the owner control endpoint, admits each operator command remotely and
//! polls its member-signed result, delivering replies in the same shape as the
//! private stdin channel. Environment (all optional):
//! - `B8_MEMBER_CONTROL=remote`: drive spawned members over this channel only.
//! - `B8_LAN_IP=<this host's private LAN IP>` (+ `B8_LAN_ALLOW=ip,ip`): direct-LAN
//!   carriers, seed forwarder and source; otherwise LocalOnly loopback.
//! - `B8_OWNER_CONTROL_KEY=<file with the 64-hex owner control secret>`.
//! - `B8_REMOTE_MEMBER=<control endpoint id>@<ip:port>`: attach member 1 that an
//!   operator started on another Mac instead of spawning it.
use api::member_control::{Owner, Remote};
use budgets::{BudgetId as B, Entries, MonoInstant};
use runtime::{
    iroh_control::NodeControlClient,
    iroh_host::{LanPolicy, LoopbackHost},
};
use serde_json::{Value, json};
use std::{
    net::{IpAddr, SocketAddr},
    sync::{OnceLock, mpsc},
    time::{Duration, Instant},
};
use transport::{Channel, Clock};
pub fn remote_mode() -> bool {
    std::env::var("B8_MEMBER_CONTROL").as_deref() == Ok("remote")
}
/// Explicit direct-LAN configuration, if any (never inferred).
pub fn lan() -> Option<(IpAddr, LanPolicy)> {
    let ip: IpAddr = std::env::var("B8_LAN_IP").ok()?.parse().expect("B8_LAN_IP");
    let extra = std::env::var("B8_LAN_ALLOW")
        .map(|v| {
            v.split(',')
                .filter(|s| !s.is_empty())
                .map(|s| s.parse().expect("B8_LAN_ALLOW"))
                .collect()
        })
        .unwrap_or_default();
    Some((ip, LanPolicy::with_explicit(extra).expect("B8_LAN_ALLOW")))
}
/// Member CLI flags for the carrier network and the owner pin.
pub fn member_args() -> Vec<String> {
    let mut args = vec![
        "--owner-control".to_owned(),
        owner_secret().public().to_string(),
    ];
    if let Some((ip, _)) = lan() {
        args.extend(["--lan".to_owned(), ip.to_string()]);
        for extra in std::env::var("B8_LAN_ALLOW").unwrap_or_default().split(',') {
            if !extra.is_empty() {
                args.extend(["--lan-allow".to_owned(), extra.to_owned()]);
            }
        }
    }
    args
}
pub fn owner_secret() -> &'static iroh::SecretKey {
    static KEY: OnceLock<iroh::SecretKey> = OnceLock::new();
    KEY.get_or_init(|| match std::env::var_os("B8_OWNER_CONTROL_KEY") {
        Some(path) => {
            let text = std::fs::read_to_string(path).expect("owner control key file");
            let bytes: Vec<u8> = (0..64)
                .step_by(2)
                .map(|i| u8::from_str_radix(&text.trim()[i..i + 2], 16).expect("hex key"))
                .collect();
            iroh::SecretKey::from_bytes(&bytes.try_into().expect("32-byte key"))
        }
        None => iroh::SecretKey::generate(),
    })
}
/// `<endpoint id>@<ip:port>[,<ip:port>]` -> dial address.
pub fn parse_remote(text: &str) -> iroh::EndpointAddr {
    let (id, addrs) = text.split_once('@').expect("<endpoint id>@<ip:port>");
    addrs.split(',').fold(
        iroh::EndpointAddr::new(id.parse().expect("control endpoint id")),
        |a, s| a.with_ip_addr(s.parse::<SocketAddr>().expect("ip:port")),
    )
}
fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
/// A running owner-side link; replies arrive on the receiver given at `start`.
pub struct Link {
    commands: Option<mpsc::Sender<Value>>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl Link {
    pub fn start(member: iroh::EndpointAddr) -> (Self, mpsc::Receiver<Value>) {
        let (tx, commands) = mpsc::channel::<Value>();
        let (reply_tx, replies) = mpsc::channel();
        let thread = std::thread::spawn(move || drive(member, commands, reply_tx));
        (
            Self {
                commands: Some(tx),
                thread: Some(thread),
            },
            replies,
        )
    }
    pub fn send(&self, command: Value) {
        self.commands.as_ref().unwrap().send(command).unwrap();
    }
}
impl Drop for Link {
    fn drop(&mut self) {
        self.commands.take();
        if let Some(t) = self.thread.take() {
            t.join().unwrap();
        }
    }
}
fn drive(
    member: iroh::EndpointAddr,
    commands: mpsc::Receiver<Value>,
    replies: mpsc::Sender<Value>,
) {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let clock = api::managed_carrier::HostClock::default();
    let until = |d: Duration| -> MonoInstant {
        clock
            .monotonic()
            .checked_add(budgets::Span::from_millis(d.as_millis() as u64))
            .unwrap()
    };
    let secret = owner_secret().clone();
    let mut host = match lan() {
        Some((ip, policy)) => LoopbackHost::bind_lan(
            &rt,
            &clock,
            until(span(B::RouterClose)),
            secret.clone(),
            &[Channel::NodeControl],
            ip,
            policy,
        ),
        None => LoopbackHost::bind_protocols(
            &rt,
            &clock,
            until(span(B::RouterClose)),
            secret.clone(),
            &[Channel::NodeControl],
        ),
    }
    .unwrap();
    let owner = Owner::new(&secret).unwrap();
    let client = NodeControlClient::new(
        &host,
        &clock,
        B::NodeControlCallConcurrency.count().unwrap(),
    )
    .unwrap();
    let mut remote = Remote {
        addr: member,
        enrolled: None,
    };
    // One signed call, retried within the call ceiling (a stalled member makes a
    // single call time out; the operation result is still pending on it).
    let call = |remote: &Remote, request: Value| -> Result<Value, String> {
        remote
            .call(
                &owner,
                &client,
                request,
                until(span(B::SidecarLocalHandshake)),
                api::app_host::now(),
            )
            .map_err(|e| e.code.to_owned())
    };
    let poll = span(B::PyMyceliumA13MacosManagedControlHttpRetryInitialSecondsL317);
    let mut pending: std::collections::VecDeque<(u64, String, Instant)> = Default::default();
    loop {
        let next = if pending.is_empty() {
            commands.recv().ok()
        } else {
            match commands.recv_timeout(poll) {
                Ok(c) => Some(c),
                Err(mpsc::RecvTimeoutError::Timeout) => None,
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        };
        if let Some(command) = next {
            let name = command["command"].as_str().unwrap_or_default().to_owned();
            match call(&remote, command.clone()) {
                Ok(reply) if name == "cancel_operation" => replies.send(reply).unwrap(),
                // The member stops its control channel once it closes: admission is the reply.
                Ok(reply) if name == "close" && reply["ok"] == true => replies
                    .send(json!({"ok":true,"result":{"closing":true}}))
                    .unwrap(),
                Ok(reply) if reply["ok"] == true => pending.push_back((
                    command["operation_id"].as_u64().unwrap(),
                    name,
                    Instant::now(),
                )),
                Ok(refusal) => replies.send(refusal).unwrap(),
                Err(code) => replies
                    .send(json!({"ok":false,"code":format!("control_call_failed:{code}")}))
                    .unwrap(),
            }
        } else if pending.is_empty() {
            break;
        }
        if let Some((id, name, began)) = pending.front().cloned() {
            match call(&remote, json!({"command":"result","operation_id":id})) {
                Ok(r) if r["result"]["finished"] == true => {
                    let reply = r["result"]["reply"].clone();
                    if name == "accept" && reply["ok"] == true {
                        // Paired: later replies must be signed by the enrolled key
                        // (the registration's member row, which the owner checks
                        // against the seed row before issuing any offer).
                        remote.enrolled =
                            reply["result"]["message"]["member"]["verification_key_digest"]
                                .as_str()
                                .map(str::to_owned);
                        assert!(remote.enrolled.is_some(), "registration names its key");
                    }
                    pending.pop_front();
                    replies.send(reply).unwrap();
                }
                Ok(_) => {}
                Err(code) => eprintln!("member result poll {id} ({name}): {code}"),
            }
            assert!(
                began.elapsed() < span(B::MemberChosenWall),
                "remote operation {name} exceeded the member wall"
            );
        }
    }
    host.close(&clock, until(span(B::RouterClose))).unwrap();
}
/// One owner endpoint (keyed by `secret`) for a bounded set of calls.
pub fn with_owner<T>(
    secret: &iroh::SecretKey,
    f: impl FnOnce(
        &Owner,
        &NodeControlClient<'_, '_, api::managed_carrier::HostClock>,
        &dyn Fn() -> MonoInstant,
    ) -> T,
) -> T {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let clock = api::managed_carrier::HostClock::default();
    let until = |id: B| {
        clock
            .monotonic()
            .checked_add(id.span(Entries::ZERO).unwrap())
            .unwrap()
    };
    let mut host = match lan() {
        Some((ip, policy)) => LoopbackHost::bind_lan(
            &rt,
            &clock,
            until(B::RouterClose),
            secret.clone(),
            &[Channel::NodeControl],
            ip,
            policy,
        ),
        None => LoopbackHost::bind_protocols(
            &rt,
            &clock,
            until(B::RouterClose),
            secret.clone(),
            &[Channel::NodeControl],
        ),
    }
    .unwrap();
    let owner = Owner::new(secret).unwrap();
    let client = NodeControlClient::new(
        &host,
        &clock,
        B::NodeControlCallConcurrency.count().unwrap(),
    )
    .unwrap();
    let out = f(&owner, &client, &|| until(B::SidecarLocalHandshake));
    drop(client);
    host.close(&clock, until(B::RouterClose)).unwrap();
    out
}
/// Raw bytes over the pinned transport (negative tests: replay, misbinding).
pub fn raw(
    client: &NodeControlClient<'_, '_, api::managed_carrier::HostClock>,
    member: &iroh::EndpointAddr,
    bytes: &[u8],
    deadline: MonoInstant,
) -> Result<Value, String> {
    use runtime::iroh_control::ControlRequest;
    let fence = transport::ConnectionFence {
        endpoint_id: *member.id.as_bytes(),
        local_generation: 0,
        remote_generation: 0,
    };
    let (_keep, mut authority) = tokio::sync::watch::channel(Some(fence));
    client
        .call(
            ControlRequest {
                peer: member,
                fence,
                command: bytes,
                deadline,
            },
            &mut authority,
        )
        .map(|r| serde_json::from_slice(&r).unwrap())
        .map_err(|e| e.0.to_owned())
}
/// Signed status over the channel (immediate, also while an operation runs).
pub fn status(member: &iroh::EndpointAddr, enrolled: Option<String>) -> Result<Value, String> {
    with_owner(owner_secret(), |owner, client, until| {
        Remote {
            addr: member.clone(),
            enrolled,
        }
        .call(
            owner,
            client,
            json!({"command":"status"}),
            until(),
            api::app_host::now(),
        )
        .map_err(|e| e.code.to_owned())
    })
}
