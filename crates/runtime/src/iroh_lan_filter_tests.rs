// SPDX-License-Identifier: AGPL-3.0-only
//! Fault injection for the direct-LAN defence in depth (lane B9). With the
//! pre-send filter in place no path outside the admission ever opens, so the
//! 250 ms path monitor is unreachable by real traffic; `distrust_for_test`
//! makes the monitor (only) treat the connection's own path as not admitted.
//! Each case proves the connection is watched at all and that the monitor
//! alone closes it (`lan_path_refused`, observed by the peer), including the
//! owner-side NodeControl dial (`iroh_control.rs`, REVIEW-laneB8f F1).
use crate::{
    iroh_control::{ControlRequest, NodeControlClient},
    iroh_host::{LAN_PATH_REFUSED, LanPolicy, LoopbackHost},
};
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use iroh::{Endpoint, EndpointAddr, RelayMode, SecretKey, endpoint::presets};
use std::{
    net::SocketAddr,
    sync::{Arc, atomic::Ordering},
    time::{Duration, Instant},
};
use transport::{Channel, Clock, ConnectionFence};

struct Mono(Instant);
impl Clock for Mono {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(
            ClockDomain(2),
            u64::try_from(self.0.elapsed().as_millis()).unwrap_or(u64::MAX),
        )
    }
    fn wall_seconds(&self) -> f64 {
        panic!("the LAN carrier must not consult wall time")
    }
}
fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap()
}
fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn deadline(clock: &Mono, id: B) -> MonoInstant {
    clock
        .monotonic()
        .checked_add(id.span(Entries::ZERO).unwrap())
        .unwrap()
}
fn lan_host<'a>(
    rt: &'a tokio::runtime::Runtime,
    clock: &Mono,
    channel: Channel,
) -> LoopbackHost<'a> {
    let loopback = "127.0.0.1".parse().unwrap();
    LoopbackHost::bind_lan(
        rt,
        clock,
        deadline(clock, B::RouterClose),
        SecretKey::generate(),
        &[channel],
        loopback,
        LanPolicy::with_explicit(vec![loopback]).unwrap(),
    )
    .unwrap()
}
fn raw_peer(rt: &tokio::runtime::Runtime, channel: Channel) -> (Endpoint, SocketAddr) {
    let endpoint = rt
        .block_on(async {
            Endpoint::builder(presets::Minimal)
                .relay_mode(RelayMode::Disabled)
                .clear_address_lookup()
                .clear_ip_transports()
                .bind_addr(SocketAddr::from(([127, 0, 0, 1], 0)))
                .unwrap()
                .alpns(vec![channel.alpn().to_vec()])
                .bind()
                .await
        })
        .unwrap();
    let socket = endpoint.bound_sockets()[0];
    (endpoint, socket)
}
fn refused_by_host(reason: &str) {
    assert!(
        reason.contains("lan_path_refused") && reason.contains(&LAN_PATH_REFUSED.to_string()),
        "the monitor did not close the connection: peer saw {reason}"
    );
}
#[test]
fn the_monitor_alone_closes_a_dialed_connection_whose_path_is_not_admitted() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let (peer, socket) = raw_peer(&rt, Channel::Router);
    let accepting = peer.clone();
    let accepted = rt.spawn(async move {
        let connection = accepting.accept().await.unwrap().await.unwrap();
        format!("{:?}", connection.closed().await)
    });
    let mut host = lan_host(&rt, &clock, Channel::Router);
    let ours = host
        .connect(
            &EndpointAddr::new(peer.id()).with_ip_addr(socket),
            &clock,
            deadline(&clock, B::SidecarLocalHandshake),
        )
        .unwrap();
    let admission = Arc::clone(host.lan_admission().unwrap());
    admission.distrust_for_test.store(true, Ordering::SeqCst);
    let reason = rt
        .block_on(async { tokio::time::timeout(span(B::RouterClose), accepted).await })
        .expect("connection kept")
        .unwrap();
    refused_by_host(&reason);
    drop(ours);
    admission.distrust_for_test.store(false, Ordering::SeqCst);
    host.close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    rt.block_on(peer.close());
}
#[test]
fn the_monitor_alone_closes_an_accepted_connection_whose_path_is_not_admitted() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let mut host = lan_host(&rt, &clock, Channel::Router);
    let addr = host.address().unwrap();
    let (peer, _) = raw_peer(&rt, Channel::Router);
    let dialing = peer.clone();
    let dialed = rt.spawn(async move {
        let connection = dialing.connect(addr, Channel::Router.alpn()).await.unwrap();
        format!("{:?}", connection.closed().await)
    });
    let ours = host
        .accept(&clock, deadline(&clock, B::SidecarLocalHandshake))
        .unwrap();
    let admission = Arc::clone(host.lan_admission().unwrap());
    admission.distrust_for_test.store(true, Ordering::SeqCst);
    let reason = rt
        .block_on(async { tokio::time::timeout(span(B::RouterClose), dialed).await })
        .expect("connection kept")
        .unwrap();
    refused_by_host(&reason);
    drop(ours);
    admission.distrust_for_test.store(false, Ordering::SeqCst);
    host.close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    rt.block_on(peer.close());
}
/// REVIEW-laneB8f F1: the owner side of the member control channel dials
/// through `NodeControlClient`; its connection must be confined too.
#[test]
fn the_monitor_alone_closes_an_owner_control_call_whose_path_is_not_admitted() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let (peer, socket) = raw_peer(&rt, Channel::NodeControl);
    let mut host = lan_host(&rt, &clock, Channel::NodeControl);
    let admission = Arc::clone(host.lan_admission().unwrap());
    let flag = Arc::clone(&admission);
    let serving = peer.clone();
    let served = rt.spawn(async move {
        let connection = serving.accept().await.unwrap().await.unwrap();
        let (_send, mut recv) = connection.accept_bi().await.unwrap();
        recv.read_to_end(4096).await.unwrap();
        // The call is in flight on a confined path; now it is not admitted.
        flag.distrust_for_test.store(true, Ordering::SeqCst);
        format!("{:?}", connection.closed().await)
    });
    let target = EndpointAddr::new(peer.id()).with_ip_addr(socket);
    let fence = ConnectionFence {
        endpoint_id: *peer.id().as_bytes(),
        local_generation: 1,
        remote_generation: 1,
    };
    let (_authority_tx, mut authority) = tokio::sync::watch::channel(Some(fence));
    let control = NodeControlClient::new(&host, &clock, Entries::new(1)).unwrap();
    let began = Instant::now();
    let result = control.call(
        ControlRequest {
            peer: &target,
            fence,
            command: b"b9-control-monitor",
            deadline: deadline(&clock, B::RouterClose),
        },
        &mut authority,
    );
    let reason = rt
        .block_on(async { tokio::time::timeout(span(B::RouterClose), served).await })
        .expect("control connection kept")
        .unwrap();
    refused_by_host(&reason);
    assert!(result.is_err(), "a refused control call must not succeed");
    assert!(
        began.elapsed() < span(B::RouterClose),
        "closed by the monitor, not the deadline"
    );
    drop(control);
    admission.distrust_for_test.store(false, Ordering::SeqCst);
    host.close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    rt.block_on(peer.close());
}
