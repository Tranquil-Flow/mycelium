// SPDX-License-Identifier: AGPL-3.0-only
//! Direct-LAN carrier mode (`NetworkMode::DirectLan`): explicit interface bind,
//! no relay or address lookup, allow-listed peers only. The loopback-explicit
//! cases run everywhere (127.0.0.1 is allowed only because it is explicitly
//! listed); the real-interface cases need this host's private LAN address in
//! `MYCELIUM_LAN_IP` and never contact another host.
#![cfg(all(feature = "iroh-adapter", unix))]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use iroh::{
    Endpoint, EndpointAddr, RelayMode, SecretKey, TransportAddr,
    endpoint::{BindOpts, Connection, presets},
};
use runtime::iroh_host::{LanPolicy, LoopbackHost, NetworkMode, private_lan, validate_lan_peer};
use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr, UdpSocket},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
use transport::{Channel, Clock, Error};

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
fn at(key: &SecretKey, socket: &str) -> EndpointAddr {
    EndpointAddr::new(key.public()).with_ip_addr(socket.parse::<SocketAddr>().unwrap())
}
#[test]
fn policy_admits_only_private_ranges_unless_explicitly_configured() {
    let lan = LanPolicy::private_only();
    for allowed in [
        "10.1.2.3",
        "172.20.0.39",
        "192.168.1.5",
        "169.254.10.1",
        "fd12:3456::1",
        "fe80::1",
    ] {
        assert!(
            private_lan(ip(allowed)) && lan.allows(ip(allowed)),
            "{allowed}"
        );
    }
    for refused in [
        "8.8.8.8",
        "100.84.252.4", // CGNAT / tailnet: not a LAN range
        "172.32.0.1",
        "127.0.0.1",
        "2001:db8::1",
        "0.0.0.0",
        "255.255.255.255",
        "224.0.0.1",
        "::",
    ] {
        assert!(!lan.allows(ip(refused)), "{refused}");
    }
    let explicit = LanPolicy::with_explicit(vec![ip("100.84.252.4"), ip("127.0.0.1")]).unwrap();
    assert!(explicit.allows(ip("100.84.252.4")) && explicit.allows(ip("127.0.0.1")));
    assert!(!explicit.allows(ip("8.8.8.8")));
    for never in ["0.0.0.0", "255.255.255.255", "224.0.0.1", "::"] {
        assert_eq!(
            LanPolicy::with_explicit(vec![ip(never)]).unwrap_err(),
            Error::Authentication,
            "{never}"
        );
    }
}
#[test]
fn whole_address_is_refused_when_any_entry_is_outside_the_allow_list() {
    let key = SecretKey::from_bytes(&[3; 32]);
    let lan = LanPolicy::private_only();
    assert!(validate_lan_peer(&at(&key, "192.168.1.5:4433"), &lan).is_ok());
    for refused in [
        at(&key, "8.8.8.8:4433"),
        at(&key, "192.168.1.5:0"),
        at(&key, "192.168.1.5:4433").with_ip_addr("203.0.113.9:4433".parse().unwrap()),
        EndpointAddr::new(key.public()),
        EndpointAddr::new(key.public()).with_relay_url("http://127.0.0.1:1".parse().unwrap()),
    ] {
        assert_eq!(
            validate_lan_peer(&refused, &lan),
            Err(Error::Authentication)
        );
    }
}
fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap()
}
fn deadline(clock: &Mono, id: B) -> MonoInstant {
    clock
        .monotonic()
        .checked_add(id.span(Entries::ZERO).unwrap())
        .unwrap()
}
#[test]
fn bind_refuses_an_interface_outside_the_allow_list() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    for interface in [
        ip("127.0.0.1"),
        ip("8.8.8.8"),
        IpAddr::V4(Ipv4Addr::UNSPECIFIED),
    ] {
        let refused = LoopbackHost::bind_lan(
            &rt,
            &clock,
            deadline(&clock, B::RouterClose),
            SecretKey::generate(),
            &[Channel::Router],
            interface,
            LanPolicy::private_only(),
        );
        assert!(matches!(refused, Err(Error::Authentication)), "{interface}");
    }
    // The generic network binder never builds a direct-LAN endpoint without its config.
    assert!(matches!(
        LoopbackHost::bind_network(
            &rt,
            &clock,
            deadline(&clock, B::RouterClose),
            SecretKey::generate(),
            &[Channel::Router],
            NetworkMode::DirectLan,
            &runtime::iroh_host::Reach::Shipped,
        ),
        Err(Error::Unavailable)
    ));
}
/// Dial the real server address with the right key, a MITM key, and refused
/// addresses; the accept side admits only its allow-listed inbound paths.
fn exercise(interface: IpAddr, server_policy: LanPolicy, client_policy: LanPolicy) {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let server_key = SecretKey::generate();
    let mut server = LoopbackHost::bind_lan(
        &rt,
        &clock,
        deadline(&clock, B::RouterClose),
        server_key.clone(),
        &[Channel::Router],
        interface,
        server_policy,
    )
    .unwrap();
    assert_eq!(server.mode(), NetworkMode::DirectLan);
    let addr = server.address().unwrap();
    let socket = *addr.ip_addrs().next().unwrap();
    assert_eq!(
        socket.ip(),
        interface,
        "bound to exactly the configured interface"
    );
    assert_eq!(
        addr.ip_addrs().count(),
        1,
        "no wildcard or discovered addresses"
    );
    let mut client = LoopbackHost::bind_lan(
        &rt,
        &clock,
        deadline(&clock, B::RouterClose),
        SecretKey::generate(),
        &[Channel::Router],
        interface,
        client_policy,
    )
    .unwrap();
    let client_id = client.address().unwrap().id;
    std::thread::scope(|scope| {
        let accepted = scope.spawn(|| {
            let c = server
                .accept(&clock, deadline(&clock, B::SidecarLocalHandshake))
                .unwrap();
            assert_eq!(c.remote_id(), client_id, "authenticated dialer identity");
            c
        });
        let c = client
            .connect(&addr, &clock, deadline(&clock, B::SidecarLocalHandshake))
            .unwrap();
        assert_eq!(c.remote_id(), server_key.public());
        let s = accepted.join().unwrap();
        c.close(0u32.into(), b"done");
        s.close(0u32.into(), b"done");
    });
    // MITM: the advertised address answers with a different key -> no session.
    let mitm = EndpointAddr::new(SecretKey::generate().public()).with_ip_addr(socket);
    assert!(
        client
            .connect(&mitm, &clock, deadline(&clock, B::SidecarLocalHandshake))
            .is_err(),
        "a key mismatch at the advertised address must fail the handshake"
    );
    // Public and unexpected-range addresses are refused before any packet.
    let began = Instant::now();
    for refused in [
        EndpointAddr::new(server_key.public()).with_ip_addr("8.8.8.8:4433".parse().unwrap()),
        EndpointAddr::new(server_key.public()).with_ip_addr("100.84.252.4:4433".parse().unwrap()),
        EndpointAddr::new(server_key.public())
            .with_relay_url("http://127.0.0.1:1".parse().unwrap()),
    ] {
        assert_eq!(
            client
                .connect(&refused, &clock, deadline(&clock, B::SidecarLocalHandshake))
                .unwrap_err(),
            Error::Authentication
        );
    }
    assert!(
        began.elapsed().as_millis()
            < u128::from(B::OwnerPoll.span(Entries::ZERO).unwrap().millis())
    );
    client
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    server
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    drop(client);
    drop(server);
    drop(rt);
    // Socket released: the exact address is bindable again.
    assert!(std::net::UdpSocket::bind(socket).is_ok());
}
#[test]
fn explicit_loopback_lan_dials_pinned_keys_and_refuses_mitm_and_public_peers() {
    let explicit = || LanPolicy::with_explicit(vec![ip("127.0.0.1")]).unwrap();
    exercise(ip("127.0.0.1"), explicit(), explicit());
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
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn real_lan_interface_dials_pinned_keys_and_refuses_mitm_and_public_peers() {
    exercise(
        lan_ip(),
        LanPolicy::private_only(),
        LanPolicy::private_only(),
    );
}
fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
/// L1: an allow-listed dialer that sends its first QUIC Initial and then goes
/// silent must not hold the accept window. A black-hole relay forwards only the
/// staller's first datagram; once the server is answering that handshake (so it
/// is the one being awaited), a pinned dialer connects directly and must be
/// accepted in the same window.
#[test]
fn a_stalled_inbound_handshake_does_not_hold_the_accept_window() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let explicit = || LanPolicy::with_explicit(vec![ip("127.0.0.1")]).unwrap();
    let mut server = LoopbackHost::bind_lan(
        &rt,
        &clock,
        deadline(&clock, B::RouterClose),
        SecretKey::generate(),
        &[Channel::Router],
        ip("127.0.0.1"),
        explicit(),
    )
    .unwrap();
    let addr = server.address().unwrap();
    let target = *addr.ip_addrs().next().unwrap();
    let relay = UdpSocket::bind("127.0.0.1:0").unwrap();
    relay
        .set_read_timeout(Some(span(B::SidecarReceivePollWait)))
        .unwrap();
    let via_relay = EndpointAddr::new(addr.id).with_ip_addr(relay.local_addr().unwrap());
    let mut staller = LoopbackHost::bind_lan(
        &rt,
        &clock,
        deadline(&clock, B::RouterClose),
        SecretKey::generate(),
        &[Channel::Router],
        ip("127.0.0.1"),
        explicit(),
    )
    .unwrap();
    let mut pinned = LoopbackHost::bind_lan(
        &rt,
        &clock,
        deadline(&clock, B::RouterClose),
        SecretKey::generate(),
        &[Channel::Router],
        ip("127.0.0.1"),
        explicit(),
    )
    .unwrap();
    let pinned_id = pinned.address().unwrap().id;
    let done = AtomicBool::new(false);
    let (answering, answered) = mpsc::channel();
    std::thread::scope(|scope| {
        scope.spawn(|| {
            let (mut forwarded, mut told) = (false, false);
            let mut buffer = [0u8; 2048];
            while !done.load(Ordering::SeqCst) {
                let Ok((n, from)) = relay.recv_from(&mut buffer) else {
                    continue;
                };
                if from == target {
                    // The server is now running the staller's handshake.
                    if !told {
                        told = true;
                        answering.send(()).unwrap();
                    }
                } else if !forwarded {
                    forwarded = true;
                    relay.send_to(&buffer[..n], target).unwrap();
                }
            }
        });
        let window = scope.spawn(|| {
            let end = deadline(&clock, B::RouterClose);
            loop {
                match server.accept(&clock, end) {
                    Ok(c) if c.remote_id() == pinned_id => return Ok(c),
                    Ok(c) => c.close(2u32.into(), b"unpinned"),
                    Err(Error::Authentication) => continue,
                    Err(e) => return Err(e),
                }
            }
        });
        let stalled =
            scope.spawn(|| staller.connect(&via_relay, &clock, deadline(&clock, B::RouterClose)));
        answered
            .recv_timeout(span(B::RouterClose))
            .expect("the server never started the staller's handshake");
        let dialed = pinned.connect(&addr, &clock, deadline(&clock, B::RouterClose));
        let accepted = window.join().unwrap();
        done.store(true, Ordering::SeqCst);
        assert!(
            stalled.join().unwrap().is_err(),
            "the staller never completes"
        );
        let accepted = accepted.expect("a stalled handshake ended the accept window");
        assert_eq!(accepted.remote_id(), pinned_id);
        let dialed = dialed.unwrap();
        dialed.close(0u32.into(), b"done");
        accepted.close(0u32.into(), b"done");
    });
    for host in [&mut pinned, &mut staller, &mut server] {
        host.close(&clock, deadline(&clock, B::RouterClose))
            .unwrap();
    }
}
/// A raw (non-`LoopbackHost`) peer bound to the LAN interface AND loopback: an
/// authenticated peer that advertises a second candidate outside the allow-list.
fn two_socket_peer(rt: &tokio::runtime::Runtime, lan: IpAddr) -> (Endpoint, SocketAddr) {
    let endpoint = rt
        .block_on(async {
            Endpoint::builder(presets::Minimal)
                .relay_mode(RelayMode::Disabled)
                .clear_address_lookup()
                .clear_ip_transports()
                // Non-default socket first: iroh refuses any bind after a default one.
                .bind_addr_with_opts(
                    SocketAddr::new(ip("127.0.0.1"), 0),
                    BindOpts::default()
                        .set_prefix_len(8)
                        .set_is_default_route(false),
                )
                .unwrap()
                .bind_addr(SocketAddr::new(lan, 0))
                .unwrap()
                .alpns(vec![Channel::Router.alpn().to_vec()])
                .bind()
                .await
        })
        .unwrap();
    let on_lan = endpoint
        .bound_sockets()
        .into_iter()
        .find(|s| s.ip() == lan)
        .unwrap();
    (endpoint, on_lan)
}
fn outside(connection: &Connection, policy: &LanPolicy) -> Option<SocketAddr> {
    connection
        .paths()
        .iter()
        .find_map(|path| match path.remote_addr() {
            TransportAddr::Ip(a) if !policy.allows(a.ip()) => Some(*a),
            _ => None,
        })
}
/// L2: after the handshake, iroh 1.0.2 holepunching (QUIC NAT traversal plus
/// multipath) may open a path to a candidate the authenticated peer advertised.
/// A direct-LAN host must not keep a connection that runs over a path outside
/// its allow-list: the peer observes the host closing it (`lan_path_refused`).
fn assert_extra_path_refused(our_side: &Connection, peer_closed: &mpsc::Receiver<String>) {
    let policy = LanPolicy::private_only();
    let end = Instant::now() + span(B::RouterClose);
    let mut seen = None;
    while Instant::now() < end && our_side.close_reason().is_none() {
        if seen.is_none() {
            seen = outside(our_side, &policy);
        }
        std::thread::yield_now();
    }
    let reason = peer_closed.recv_timeout(span(B::RouterClose));
    assert!(
        reason
            .as_ref()
            .is_ok_and(|r| r.contains("lan_path_refused")),
        "extra path {seen:?} outside the allow-list was kept: peer close {reason:?}, \
         open paths {:?}",
        our_side
            .paths()
            .iter()
            .map(|p| p.remote_addr().clone())
            .collect::<Vec<_>>()
    );
}
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn real_lan_dialer_drops_a_connection_that_gains_a_path_outside_the_allow_list() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let (peer, on_lan) = two_socket_peer(&rt, lan_ip());
    let (tx, peer_closed) = mpsc::channel();
    let accepting = peer.clone();
    rt.spawn(async move {
        let connection = accepting.accept().await.unwrap().await.unwrap();
        tx.send(format!("{:?}", connection.closed().await)).unwrap();
    });
    let mut client = LoopbackHost::bind_lan(
        &rt,
        &clock,
        deadline(&clock, B::RouterClose),
        SecretKey::generate(),
        &[Channel::Router],
        lan_ip(),
        LanPolicy::private_only(),
    )
    .unwrap();
    let ours = client
        .connect(
            &EndpointAddr::new(peer.id()).with_ip_addr(on_lan),
            &clock,
            deadline(&clock, B::SidecarLocalHandshake),
        )
        .unwrap();
    assert_extra_path_refused(&ours, &peer_closed);
    drop(ours);
    client
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    rt.block_on(peer.close());
}
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn real_lan_acceptor_drops_a_connection_that_gains_a_path_outside_the_allow_list() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let mut server = LoopbackHost::bind_lan(
        &rt,
        &clock,
        deadline(&clock, B::RouterClose),
        SecretKey::generate(),
        &[Channel::Router],
        lan_ip(),
        LanPolicy::private_only(),
    )
    .unwrap();
    let addr = server.address().unwrap();
    let (peer, _) = two_socket_peer(&rt, lan_ip());
    let (tx, peer_closed) = mpsc::channel();
    let dialing = peer.clone();
    rt.spawn(async move {
        let connection = dialing.connect(addr, Channel::Router.alpn()).await.unwrap();
        tx.send(format!("{:?}", connection.closed().await)).unwrap();
    });
    let ours = server
        .accept(&clock, deadline(&clock, B::SidecarLocalHandshake))
        .unwrap();
    assert_extra_path_refused(&ours, &peer_closed);
    drop(ours);
    server
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    rt.block_on(peer.close());
}
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn real_lan_interface_refuses_an_inbound_path_outside_its_allow_list() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let mut server = LoopbackHost::bind_lan(
        &rt,
        &clock,
        deadline(&clock, B::RouterClose),
        SecretKey::generate(),
        &[Channel::Router],
        lan_ip(),
        LanPolicy::private_only(),
    )
    .unwrap();
    // A dialer whose source path is loopback (explicitly allowed on ITS side)
    // reaches the LAN socket of the same host; the server must refuse it.
    let mut client = LoopbackHost::bind_lan(
        &rt,
        &clock,
        deadline(&clock, B::RouterClose),
        SecretKey::generate(),
        &[Channel::Router],
        ip("127.0.0.1"),
        LanPolicy::with_explicit(vec![ip("127.0.0.1"), lan_ip()]).unwrap(),
    )
    .unwrap();
    let addr = server.address().unwrap();
    std::thread::scope(|scope| {
        let refused =
            scope.spawn(|| server.accept(&clock, deadline(&clock, B::SidecarLocalHandshake)));
        let dialed = client.connect(&addr, &clock, deadline(&clock, B::SidecarLocalHandshake));
        assert!(
            dialed.is_err(),
            "refused inbound path never yields a session"
        );
        assert_eq!(refused.join().unwrap().unwrap_err(), Error::Authentication);
    });
    client
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
    server
        .close(&clock, deadline(&clock, B::RouterClose))
        .unwrap();
}
