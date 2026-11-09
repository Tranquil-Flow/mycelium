// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-side test configuration of the signed remote member channel; the
//! channel itself is product code (`api::joined_owner::remote`, B16), which
//! delivers replies in the same shape as the private stdin channel.
//! Environment (all optional):
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
/// A running owner-side link (PRODUCT `api::joined_owner::remote`, B16);
/// replies arrive on the receiver given at `start`.
pub struct Link(api::joined_owner::remote::RemoteChannel);
impl Link {
    pub fn start(member: iroh::EndpointAddr) -> (Self, mpsc::Receiver<Value>) {
        let (channel, replies) =
            api::joined_owner::remote::RemoteChannel::start(owner_secret().clone(), member, lan())
                .unwrap();
        (Self(channel), replies)
    }
    pub fn send(&self, command: Value) {
        self.0.send(command).unwrap();
    }
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
    probe(member, enrolled, &[])
}
/// Signed status asking the member about exact pids on its own host.
pub fn probe(
    member: &iroh::EndpointAddr,
    enrolled: Option<String>,
    pids: &[i32],
) -> Result<Value, String> {
    with_owner(owner_secret(), |owner, client, until| {
        Remote {
            addr: member.clone(),
            enrolled,
        }
        .call(
            owner,
            client,
            json!({"command":"status","probe_pids":pids}),
            until(),
            api::app_host::now(),
        )
        .map_err(|e| e.code.to_owned())
    })
}
