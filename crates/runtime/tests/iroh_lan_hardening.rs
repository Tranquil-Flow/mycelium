// SPDX-License-Identifier: AGPL-3.0-only
//! Product-LAN hardening of the direct-LAN carrier (lane B9).
//!
//! An authenticated peer can advertise extra NAT-traversal candidates
//! (`Builder::external_addr`, iroh 1.0.2 `socket.rs:1873-1876`) that this host
//! would otherwise probe. Each candidate here is a test-owned UDP "tap" that only
//! counts datagrams arriving from this host's socket: the property is that NO
//! datagram is ever sent to a destination the host did not pin, not merely that
//! the connection is closed afterwards. Loopback cases use an explicitly allowed
//! 127.0.0.1; the real-interface cases need this host's private LAN address in
//! `MYCELIUM_LAN_IP` and never contact another host.
#![cfg(all(feature = "iroh-adapter", unix))]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use iroh::{Endpoint, EndpointAddr, RelayMode, SecretKey, endpoint::presets};
use runtime::{
    iroh_control::{ControlRequest, NodeControlClient},
    iroh_host::{LanPolicy, LoopbackHost, private_lan},
};
use std::{
    net::{IpAddr, SocketAddr, UdpSocket},
    sync::{
        Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
use transport::{Channel, Clock, ConnectionFence, Error};

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
fn ip(text: &str) -> IpAddr {
    text.parse().unwrap()
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
fn loopback() -> LanPolicy {
    LanPolicy::with_explicit(vec![ip("127.0.0.1")]).unwrap()
}
fn lan_ip() -> IpAddr {
    let value = std::env::var("MYCELIUM_LAN_IP").expect("MYCELIUM_LAN_IP = this host's LAN IP");
    let address = ip(&value);
    assert!(
        private_lan(address),
        "{address} is not a private LAN address"
    );
    address
}
fn host<'a>(
    rt: &'a tokio::runtime::Runtime,
    clock: &Mono,
    interface: IpAddr,
    policy: LanPolicy,
    channel: Channel,
) -> LoopbackHost<'a> {
    LoopbackHost::bind_lan(
        rt,
        clock,
        deadline(clock, B::RouterClose),
        SecretKey::generate(),
        &[channel],
        interface,
        policy,
    )
    .unwrap()
}
/// A test-owned UDP socket an authenticated peer advertises as a candidate.
struct Tap {
    socket: UdpSocket,
    addr: SocketAddr,
}
impl Tap {
    fn bind(at: IpAddr) -> Self {
        let socket = UdpSocket::bind(SocketAddr::new(at, 0)).unwrap();
        socket
            .set_read_timeout(Some(span(B::SidecarReceivePollWait)))
            .unwrap();
        let addr = socket.local_addr().unwrap();
        Self { socket, addr }
    }
    /// Every datagram that arrives from `from` until `until`.
    fn watch(&self, from: SocketAddr, until: Instant, seen: &Mutex<Vec<usize>>) {
        let mut buffer = [0u8; 2048];
        while Instant::now() < until {
            if let Ok((n, source)) = self.socket.recv_from(&mut buffer)
                && source == from
            {
                seen.lock().unwrap().push(n);
            }
        }
    }
}
/// A raw (non-`LoopbackHost`) authenticated peer bound to `at` that advertises
/// `candidates` as extra direct addresses.
fn raw_peer(
    rt: &tokio::runtime::Runtime,
    at: IpAddr,
    candidates: &[SocketAddr],
    channel: Channel,
) -> (Endpoint, SocketAddr) {
    let endpoint = rt
        .block_on(async {
            let mut builder = Endpoint::builder(presets::Minimal)
                .portmapper_config(iroh::endpoint::PortmapperConfig::Disabled)
                .relay_mode(RelayMode::Disabled)
                .clear_address_lookup()
                .clear_ip_transports()
                .bind_addr(SocketAddr::new(at, 0))
                .unwrap();
            for candidate in candidates {
                builder = builder.external_addr(*candidate);
            }
            builder.alpns(vec![channel.alpn().to_vec()]).bind().await
        })
        .unwrap();
    let socket = endpoint.bound_sockets()[0];
    (endpoint, socket)
}
fn our_socket(host: &LoopbackHost<'_>) -> SocketAddr {
    *host.address().unwrap().ip_addrs().next().unwrap()
}
fn assert_nothing_reached(taps: &[(&Tap, Vec<usize>)]) {
    let reached: Vec<_> = taps
        .iter()
        .filter(|(_, seen)| !seen.is_empty())
        .map(|(tap, seen)| format!("{} datagram(s) {seen:?} to {}", seen.len(), tap.addr))
        .collect();
    assert!(
        reached.is_empty(),
        "datagrams reached unpinned candidates: {}",
        reached.join("; ")
    );
}
/// Our host DIALS the peer; observe the taps for one handshake budget after the
/// connection is up, while the connection stays open.
fn dialer_case(interface: IpAddr, policy: LanPolicy, tap_ips: &[IpAddr]) {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let taps: Vec<Tap> = tap_ips.iter().map(|at| Tap::bind(*at)).collect();
    let addrs: Vec<_> = taps.iter().map(|t| t.addr).collect();
    let (peer, peer_socket) = raw_peer(&rt, interface, &addrs, Channel::Router);
    let accepting = peer.clone();
    let accepted = rt.spawn(async move {
        let incoming = accepting.accept().await.unwrap();
        let connection = incoming.await.unwrap();
        connection.closed().await
    });
    let mut client = host(&rt, &clock, interface, policy, Channel::Router);
    let from = our_socket(&client);
    let ours = client
        .connect(
            &EndpointAddr::new(peer.id()).with_ip_addr(peer_socket),
            &clock,
            deadline(&clock, B::SidecarLocalHandshake),
        )
        .unwrap();
    let until = Instant::now() + span(B::SidecarLocalHandshake);
    let seen: Vec<_> = taps.iter().map(|_| Mutex::new(Vec::new())).collect();
    std::thread::scope(|scope| {
        for (tap, seen) in taps.iter().zip(&seen) {
            scope.spawn(move || tap.watch(from, until, seen));
        }
    });
    let observed: Vec<_> = taps
        .iter()
        .zip(seen)
        .map(|(t, s)| (t, s.into_inner().unwrap()))
        .collect();
    ours.close(0u32.into(), b"done");
    drop(ours);
    client
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    rt.block_on(peer.close());
    accepted.abort();
    assert_nothing_reached(&observed);
}
/// Our host ACCEPTS the peer's dial.
fn acceptor_case(interface: IpAddr, policy: LanPolicy, tap_ips: &[IpAddr]) {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let taps: Vec<Tap> = tap_ips.iter().map(|at| Tap::bind(*at)).collect();
    let addrs: Vec<_> = taps.iter().map(|t| t.addr).collect();
    let mut server = host(&rt, &clock, interface, policy, Channel::Router);
    let from = our_socket(&server);
    let addr = server.address().unwrap();
    let (peer, _) = raw_peer(&rt, interface, &addrs, Channel::Router);
    let dialing = peer.clone();
    let dialed = rt.spawn(async move {
        let connection = dialing.connect(addr, Channel::Router.alpn()).await.unwrap();
        connection.closed().await
    });
    let ours = server
        .accept(&clock, deadline(&clock, B::SidecarLocalHandshake))
        .unwrap();
    let until = Instant::now() + span(B::SidecarLocalHandshake);
    let seen: Vec<_> = taps.iter().map(|_| Mutex::new(Vec::new())).collect();
    std::thread::scope(|scope| {
        for (tap, seen) in taps.iter().zip(&seen) {
            scope.spawn(move || tap.watch(from, until, seen));
        }
    });
    let observed: Vec<_> = taps
        .iter()
        .zip(seen)
        .map(|(t, s)| (t, s.into_inner().unwrap()))
        .collect();
    ours.close(0u32.into(), b"done");
    drop(ours);
    server
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    rt.block_on(peer.close());
    dialed.abort();
    assert_nothing_reached(&observed);
}
/// Exact pins (the default): an in-range address the peer merely advertised is
/// not one this host dialed, so nothing is sent there.
#[test]
fn exact_pins_send_nothing_to_an_in_range_candidate_the_dialer_never_pinned() {
    dialer_case(ip("127.0.0.1"), loopback(), &[ip("127.0.0.1")]);
}
#[test]
fn exact_pins_send_nothing_to_an_in_range_candidate_the_acceptor_never_pinned() {
    acceptor_case(ip("127.0.0.1"), loopback(), &[ip("127.0.0.1")]);
}
/// Real interface: a loopback candidate (outside the private allow-list) and an
/// in-range candidate on this host's LAN address (never pinned) both get nothing.
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn real_lan_dialer_sends_no_datagram_to_any_unpinned_candidate() {
    dialer_case(
        lan_ip(),
        LanPolicy::private_only(),
        &[ip("127.0.0.1"), lan_ip()],
    );
}
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn real_lan_acceptor_sends_no_datagram_to_any_unpinned_candidate() {
    acceptor_case(
        lan_ip(),
        LanPolicy::private_only(),
        &[ip("127.0.0.1"), lan_ip()],
    );
}
/// The owner side of the member control channel (`NodeControlClient`) over a
/// direct-LAN host: the call dials through the host (pinned, confined), still
/// completes, and nothing reaches the candidate the peer advertised. The peer
/// holds its reply for one handshake budget while the taps watch.
fn control_case(interface: IpAddr, policy: LanPolicy, tap_ips: &[IpAddr]) {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let taps: Vec<Tap> = tap_ips.iter().map(|at| Tap::bind(*at)).collect();
    let addrs: Vec<_> = taps.iter().map(|t| t.addr).collect();
    let (peer, peer_socket) = raw_peer(&rt, interface, &addrs, Channel::NodeControl);
    let (release_tx, release) = tokio::sync::oneshot::channel::<()>();
    let serving = peer.clone();
    let served = rt.spawn(async move {
        let connection = serving.accept().await.unwrap().await.unwrap();
        let (mut send, mut recv) = connection.accept_bi().await.unwrap();
        let command = recv.read_to_end(4096).await.unwrap();
        // Hold the reply until the tap window ends (or the host closes us).
        tokio::select! {
            held = release => held.unwrap(),
            reason = connection.closed() => return format!("{reason:?}"),
        }
        send.write_all(&command).await.unwrap();
        send.finish().unwrap();
        format!("{:?}", connection.closed().await)
    });
    let mut client = host(&rt, &clock, interface, policy, Channel::NodeControl);
    let from = our_socket(&client);
    let target = EndpointAddr::new(peer.id()).with_ip_addr(peer_socket);
    let fence = ConnectionFence {
        endpoint_id: *peer.id().as_bytes(),
        local_generation: 1,
        remote_generation: 1,
    };
    let (_authority_tx, mut authority) = tokio::sync::watch::channel(Some(fence));
    let control = NodeControlClient::new(&client, &clock, Entries::new(1)).unwrap();
    let seen: Vec<_> = taps.iter().map(|_| Mutex::new(Vec::new())).collect();
    let result = std::thread::scope(|scope| {
        let until = Instant::now() + span(B::SidecarLocalHandshake);
        let watchers: Vec<_> = taps
            .iter()
            .zip(&seen)
            .map(|(tap, seen)| scope.spawn(move || tap.watch(from, until, seen)))
            .collect();
        let releaser = scope.spawn(move || {
            for watcher in watchers {
                watcher.join().unwrap();
            }
            release_tx.send(()).unwrap();
        });
        let result = control.call(
            ControlRequest {
                peer: &target,
                fence,
                command: b"b9-control-probe",
                deadline: deadline(&clock, B::RouterClose),
            },
            &mut authority,
        );
        releaser.join().unwrap();
        result
    });
    let closed = rt.block_on(async { tokio::time::timeout(span(B::RouterClose), served).await });
    let observed: Vec<_> = taps
        .iter()
        .zip(seen)
        .map(|(t, s)| (t, s.into_inner().unwrap()))
        .collect();
    drop(control);
    client
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    rt.block_on(peer.close());
    assert_nothing_reached(&observed);
    assert_eq!(
        result.as_deref(),
        Ok(&b"b9-control-probe"[..]),
        "the LAN control call must complete through the host (peer: {closed:?})"
    );
}
#[test]
fn owner_control_dial_is_pinned_and_sends_nothing_to_an_unpinned_candidate() {
    control_case(ip("127.0.0.1"), loopback(), &[ip("127.0.0.1")]);
}
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn real_lan_owner_control_dial_is_confined_to_its_pins() {
    control_case(
        lan_ip(),
        LanPolicy::private_only(),
        &[ip("127.0.0.1"), lan_ip()],
    );
}
/// Concurrent accept: several allow-listed dialers that each send one QUIC
/// Initial (through a black-hole relay that forwards only that first datagram)
/// and then go silent must not serialise the accept. A pinned dialer arriving
/// after all of them is accepted within ONE handshake budget, not N of them.
#[test]
fn stalled_handshakes_do_not_serialise_the_accept() {
    const STALLERS: usize = 3;
    let rt = runtime();
    let clock = Mono(Instant::now());
    let mut server = host(&rt, &clock, ip("127.0.0.1"), loopback(), Channel::Router);
    let addr = server.address().unwrap();
    let target = *addr.ip_addrs().next().unwrap();
    let relays: Vec<UdpSocket> = (0..STALLERS)
        .map(|_| {
            let relay = UdpSocket::bind("127.0.0.1:0").unwrap();
            relay
                .set_read_timeout(Some(span(B::SidecarReceivePollWait)))
                .unwrap();
            relay
        })
        .collect();
    let mut stallers: Vec<_> = (0..STALLERS)
        .map(|_| host(&rt, &clock, ip("127.0.0.1"), loopback(), Channel::Router))
        .collect();
    let mut pinned = host(&rt, &clock, ip("127.0.0.1"), loopback(), Channel::Router);
    let pinned_id = pinned.address().unwrap().id;
    let done = AtomicBool::new(false);
    let (forwarded_tx, forwarded) = mpsc::channel();
    std::thread::scope(|scope| {
        for relay in &relays {
            let (done, forwarded_tx) = (&done, forwarded_tx.clone());
            scope.spawn(move || {
                let mut sent = false;
                let mut buffer = [0u8; 2048];
                while !done.load(Ordering::SeqCst) {
                    let Ok((n, from)) = relay.recv_from(&mut buffer) else {
                        continue;
                    };
                    if from != target && !sent {
                        sent = true;
                        relay.send_to(&buffer[..n], target).unwrap();
                        forwarded_tx.send(()).unwrap();
                    }
                }
            });
        }
        let window = scope.spawn(|| {
            let end = deadline(&clock, B::RouterClose);
            loop {
                match server.accept(&clock, end) {
                    Ok(c) if c.remote_id() == pinned_id => return Ok((c, Instant::now())),
                    Ok(c) => c.close(2u32.into(), b"unpinned"),
                    Err(Error::Authentication) => continue,
                    Err(e) => return Err(e),
                }
            }
        });
        let stalled: Vec<_> = stallers
            .iter()
            .zip(&relays)
            .map(|(staller, relay)| {
                let via = EndpointAddr::new(addr.id).with_ip_addr(relay.local_addr().unwrap());
                let clock = &clock;
                scope.spawn(move || staller.connect(&via, clock, deadline(clock, B::RouterClose)))
            })
            .collect();
        for _ in 0..STALLERS {
            forwarded
                .recv_timeout(span(B::RouterClose))
                .expect("a staller's first Initial never reached the server");
        }
        let began = Instant::now();
        let dialed = pinned.connect(&addr, &clock, deadline(&clock, B::RouterClose));
        let accepted = window.join().unwrap();
        done.store(true, Ordering::SeqCst);
        for staller in stalled {
            assert!(
                staller.join().unwrap().is_err(),
                "a staller never completes"
            );
        }
        let (accepted, at) = accepted.expect("stalled handshakes ended the accept window");
        let waited = at.duration_since(began);
        assert!(
            waited < span(B::SidecarLocalHandshake),
            "the pinned dialer waited {waited:?} behind {STALLERS} stalled handshakes \
             (one handshake budget is {:?})",
            span(B::SidecarLocalHandshake)
        );
        assert_eq!(accepted.remote_id(), pinned_id);
        let dialed = dialed.unwrap();
        dialed.close(0u32.into(), b"done");
        accepted.close(0u32.into(), b"done");
    });
    for host in stallers.iter_mut() {
        host.close(&clock, deadline(&clock, B::RouterClose))
            .unwrap();
    }
    for host in [&mut pinned, &mut server] {
        host.close(&clock, deadline(&clock, B::RouterClose))
            .unwrap();
    }
}
/// Range form (opt-in): still nothing outside the allow-list, and no IP
/// candidate is probed at all (a direct-LAN endpoint has no IP transport).
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn real_lan_range_form_still_sends_nothing_outside_the_allow_list() {
    dialer_case(
        lan_ip(),
        LanPolicy::private_only().range_form(),
        &[ip("127.0.0.1"), lan_ip()],
    );
}
/// Exact peer hosts (from signed pairing/offer data): an allow-listed dialer
/// from any other host is ignored before its handshake; admitting its host
/// later (signed data arrived) accepts the same dialer.
#[test]
fn peer_host_pins_admit_inbound_handshakes_only_from_the_signed_hosts() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let elsewhere = ip("10.255.255.1");
    let policy = LanPolicy::with_explicit(vec![ip("127.0.0.1")])
        .unwrap()
        .with_peer_hosts(vec![elsewhere])
        .unwrap();
    assert!(
        LanPolicy::private_only()
            .with_peer_hosts(vec![ip("8.8.8.8")])
            .is_err(),
        "a pinned host must itself be allow-listed"
    );
    let mut server = host(&rt, &clock, ip("127.0.0.1"), policy, Channel::Router);
    let addr = server.address().unwrap();
    let mut client = host(&rt, &clock, ip("127.0.0.1"), loopback(), Channel::Router);
    let client_id = client.address().unwrap().id;
    std::thread::scope(|scope| {
        let refused =
            scope.spawn(|| server.accept(&clock, deadline(&clock, B::SidecarLocalHandshake)));
        let dialed = client.connect(&addr, &clock, deadline(&clock, B::SidecarLocalHandshake));
        assert!(dialed.is_err(), "an unpinned host never gets a session");
        // B9f: dropped by the receive filter before iroh (was `Authentication`
        // from `Incoming::ignore`): the accept is never woken.
        assert_eq!(refused.join().unwrap().unwrap_err(), Error::Expired);
    });
    assert!(
        server.lan_dropped_datagrams().unwrap() > 0,
        "the unpinned host's handshake never reached the receive filter"
    );
    assert_eq!(
        server.admit_peer_hosts(&[ip("8.8.8.8")]),
        Err(Error::Authentication)
    );
    server.admit_peer_hosts(&[ip("127.0.0.1")]).unwrap();
    std::thread::scope(|scope| {
        let accepted = scope.spawn(|| {
            loop {
                match server.accept(&clock, deadline(&clock, B::RouterClose)) {
                    Err(Error::Authentication) => continue,
                    other => return other,
                }
            }
        });
        let dialed = client
            .connect(&addr, &clock, deadline(&clock, B::SidecarLocalHandshake))
            .unwrap();
        let accepted = accepted.join().unwrap().unwrap();
        assert_eq!(accepted.remote_id(), client_id);
        dialed.close(0u32.into(), b"done");
        accepted.close(0u32.into(), b"done");
    });
    for host in [&mut client, &mut server] {
        host.close(&clock, deadline(&clock, B::RouterClose))
            .unwrap();
    }
}
/// The sender-side filter itself: a peer whose packets start arriving from a
/// NEW, unpinned source (a UDP relay switches the socket it forwards from, like
/// a NAT rebinding or a peer moving address) must never make this host send
/// there. Only the socket it accepted from (pinned) ever hears from it.
#[test]
fn a_peer_address_change_is_never_followed_to_an_unpinned_address() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let mut server = host(&rt, &clock, ip("127.0.0.1"), loopback(), Channel::Router);
    let ours = our_socket(&server);
    let bind = || {
        let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
        socket
            .set_read_timeout(Some(span(B::SidecarReceivePollWait)))
            .unwrap();
        socket
    };
    // Peer-facing ingress, then the two host-facing sockets: A (pinned at
    // accept) and B (the source after the switch, never pinned).
    let (ingress, first, second) = (bind(), bind(), bind());
    let via =
        EndpointAddr::new(server.address().unwrap().id).with_ip_addr(ingress.local_addr().unwrap());
    let (peer, _) = raw_peer(&rt, ip("127.0.0.1"), &[], Channel::Router);
    let switched = AtomicBool::new(false);
    let done = AtomicBool::new(false);
    let peer_at = Mutex::new(None::<SocketAddr>);
    let heard_on_second = Mutex::new(Vec::new());
    std::thread::scope(|scope| {
        // Peer -> host, through A until the switch, then through B.
        scope.spawn(|| {
            let mut buffer = [0u8; 2048];
            while !done.load(Ordering::SeqCst) {
                let Ok((n, from)) = ingress.recv_from(&mut buffer) else {
                    continue;
                };
                *peer_at.lock().unwrap() = Some(from);
                let out = if switched.load(Ordering::SeqCst) {
                    &second
                } else {
                    &first
                };
                out.send_to(&buffer[..n], ours).unwrap();
            }
        });
        // Host -> peer, from whichever host-facing socket the host used.
        for (socket, is_second) in [(&first, false), (&second, true)] {
            let (done, peer_at, ingress, heard) = (&done, &peer_at, &ingress, &heard_on_second);
            scope.spawn(move || {
                let mut buffer = [0u8; 2048];
                while !done.load(Ordering::SeqCst) {
                    let Ok((n, from)) = socket.recv_from(&mut buffer) else {
                        continue;
                    };
                    if from != ours {
                        continue;
                    }
                    if is_second {
                        heard.lock().unwrap().push(n);
                    }
                    if let Some(peer) = *peer_at.lock().unwrap() {
                        ingress.send_to(&buffer[..n], peer).unwrap();
                    }
                }
            });
        }
        let dialing = peer.clone();
        let (opened_tx, opened) = mpsc::channel();
        let (stop_tx, stop) = tokio::sync::oneshot::channel::<()>();
        let talker = rt.spawn(async move {
            let connection = dialing.connect(via, Channel::Router.alpn()).await.unwrap();
            let mut send = connection.open_uni().await.unwrap();
            send.write_all(b"before").await.unwrap();
            opened_tx.send(()).unwrap();
            // Keep traffic flowing so packets arrive from the new source.
            let ticker = async {
                let mut pace = tokio::time::interval(span(B::SidecarReceivePollWait) / 5);
                loop {
                    pace.tick().await;
                    if send.write_all(b"after").await.is_err() {
                        return;
                    }
                }
            };
            tokio::select! {
                _ = stop => {}
                () = ticker => {}
            }
            connection.close(0u32.into(), b"done");
        });
        let accepted = server
            .accept(&clock, deadline(&clock, B::SidecarLocalHandshake))
            .unwrap();
        opened
            .recv_timeout(span(B::RouterClose))
            .expect("the peer never opened its stream");
        switched.store(true, Ordering::SeqCst);
        // Observe one handshake budget after the switch (the relay threads
        // record); an idle tap is a bounded wait without sleeping.
        let until = Instant::now() + span(B::SidecarLocalHandshake);
        Tap::bind(ip("127.0.0.1")).watch(ours, until, &Mutex::new(Vec::new()));
        stop_tx.send(()).unwrap();
        rt.block_on(async { tokio::time::timeout(span(B::RouterClose), talker).await })
            .unwrap()
            .unwrap();
        done.store(true, Ordering::SeqCst);
        accepted.close(0u32.into(), b"done");
    });
    server
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    rt.block_on(peer.close());
    let heard = heard_on_second.into_inner().unwrap();
    assert!(
        heard.is_empty(),
        "the host followed the peer to its unpinned new source: {} datagram(s) {heard:?}",
        heard.len()
    );
}
/// A peer key seen before at another (since released) address: the address
/// book still lists the old address, which was legitimately admitted then. A
/// new connection from the same key at a new address must be kept (the
/// owner re-dials every route epoch from a fresh socket with the same key).
#[test]
fn a_returning_peer_at_a_new_address_is_not_refused_for_its_old_one() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let mut server = host(&rt, &clock, ip("127.0.0.1"), loopback(), Channel::Router);
    let addr = server.address().unwrap();
    let key = SecretKey::generate();
    let dial = |peer: &LoopbackHost<'_>| {
        std::thread::scope(|scope| {
            let accepted =
                scope.spawn(|| server.accept(&clock, deadline(&clock, B::SidecarLocalHandshake)));
            let dialed = peer
                .connect(&addr, &clock, deadline(&clock, B::SidecarLocalHandshake))
                .unwrap();
            (dialed, accepted.join().unwrap().unwrap())
        })
    };
    let lan_host = || {
        LoopbackHost::bind_lan(
            &rt,
            &clock,
            deadline(&clock, B::RouterClose),
            key.clone(),
            &[Channel::Router],
            ip("127.0.0.1"),
            loopback(),
        )
        .unwrap()
    };
    let mut first = lan_host();
    let old = our_socket(&first);
    let (dialed, accepted) = dial(&first);
    dialed.close(0u32.into(), b"epoch over");
    rt.block_on(async { tokio::time::timeout(span(B::RouterClose), accepted.closed()).await })
        .unwrap();
    drop((dialed, accepted));
    first
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    // The old address's exact pin is released after its close drain.
    let end = Instant::now() + span(B::RouterClose) * 2;
    while server.lan_admits(old) == Some(true) {
        assert!(Instant::now() < end, "the old pin was never released");
        std::thread::yield_now();
    }
    let mut second = lan_host();
    assert_ne!(our_socket(&second), old);
    let (dialed, accepted) = dial(&second);
    // Several monitor ticks must pass without the host refusing it.
    let kept = rt.block_on(async {
        tokio::time::timeout(span(B::SidecarLocalHandshake), accepted.closed()).await
    });
    assert!(
        kept.is_err(),
        "the returning peer's new connection was closed: {kept:?}"
    );
    dialed.close(0u32.into(), b"done");
    accepted.close(0u32.into(), b"done");
    for host in [&mut second, &mut server] {
        host.close(&clock, deadline(&clock, B::RouterClose))
            .unwrap();
    }
}
