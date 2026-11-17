// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XB2 (ruling 29 A+B): two endpoints on DIFFERENT networks. The same-network carrier
//! (`NetworkMode::DirectLan`, `LanPolicy::private_only`) refuses a peer whose signed address is
//! outside the private-LAN class before any packet; the cross-network choice
//! (`LanPolicy::across_networks`) registers at Mycelium's relay, lets iroh use relayed and
//! hole-punched direct paths, and relaxes ONLY that address class: peer keys stay admitted from
//! signed data, an unadmitted key is refused (and counted), unusable addresses are refused before
//! any packet (and counted).
//!
//! Test double for "another network": 127.0.0.1 is outside the private-LAN class the same-network
//! rule admits (it stands for the public / CGNAT address a Mac on a hotspot has), yet both
//! endpoints can reach it without any network. The relay is a loopback port nothing listens on
//! (`http://127.0.0.1:1`), so nothing leaves the host: these runs prove the address rule and the
//! peer protections, not the relayed path itself (the ignored `probe_` test and the hotspot run do).
#![cfg(all(feature = "iroh-adapter", unix))]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use iroh::{EndpointAddr, SecretKey};
use runtime::iroh_host::{
    LanPolicy, LoopbackHost, NetworkMode, RELAY_UNREACHABLE, relay_reachable, validate_lan_peer,
};
use std::{
    net::{IpAddr, Ipv4Addr},
    time::Instant,
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
        panic!("the carrier must not consult wall time")
    }
}
fn ip(text: &str) -> IpAddr {
    text.parse().unwrap()
}
const LOOPBACK: IpAddr = IpAddr::V4(Ipv4Addr::LOCALHOST);
/// Nothing listens on port 1: the relay is never reachable, so nothing leaves the host.
fn dead_relay() -> iroh::RelayUrl {
    "http://127.0.0.1:1".parse().unwrap()
}
fn across() -> LanPolicy {
    LanPolicy::private_only().across_networks(dead_relay())
}
fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
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
fn bind<'a>(
    rt: &'a tokio::runtime::Runtime,
    clock: &Mono,
    key: &SecretKey,
    policy: LanPolicy,
) -> Result<LoopbackHost<'a>, Error> {
    LoopbackHost::bind_lan(
        rt,
        clock,
        deadline(clock, B::RouterClose),
        key.clone(),
        &[Channel::Router],
        LOOPBACK,
        policy,
    )
}
fn close(host: &mut LoopbackHost<'_>, clock: &Mono) {
    host.close(clock, deadline(clock, B::RouterClose)).unwrap();
}

/// The per-run choice and its status name; the same-network rule is exactly today's.
#[test]
fn the_cross_network_choice_relaxes_only_the_address_class() {
    let same = LanPolicy::private_only();
    assert_eq!(same.reach(), "same_network");
    assert_eq!(same.relay(), None);
    let across = across();
    assert_eq!(across.reach(), "relay");
    assert_eq!(across.relay(), Some(&dead_relay()));
    for public in [
        "8.8.8.8",
        "100.84.252.4",
        "203.0.113.9",
        "127.0.0.1",
        "2001:db8::1",
    ] {
        assert!(!same.allows(ip(public)), "same network: {public}");
        assert!(across.allows(ip(public)), "across networks: {public}");
    }
    for private in ["10.1.2.3", "172.20.10.4", "192.168.1.5", "fd12:3456::1"] {
        assert!(
            same.allows(ip(private)) && across.allows(ip(private)),
            "{private}"
        );
    }
    // Never an address, on any network.
    for never in ["0.0.0.0", "255.255.255.255", "224.0.0.1", "::", "ff02::1"] {
        assert!(!across.allows(ip(never)), "{never}");
    }
    // A whole signed address is still checked: a port 0 entry or a relay entry in the SIGNED
    // address refuses it (the relay comes from this host's own choice, never from the peer).
    let key = SecretKey::from_bytes(&[3; 32]);
    let at = |socket: &str| EndpointAddr::new(key.public()).with_ip_addr(socket.parse().unwrap());
    assert!(validate_lan_peer(&at("203.0.113.9:4433"), &across).is_ok());
    assert_eq!(
        validate_lan_peer(&at("203.0.113.9:4433"), &same),
        Err(Error::Authentication)
    );
    for refused in [
        at("203.0.113.9:0"),
        at("0.0.0.0:4433"),
        EndpointAddr::new(key.public()),
        at("203.0.113.9:4433").with_relay_url(dead_relay()),
    ] {
        assert_eq!(
            validate_lan_peer(&refused, &across),
            Err(Error::Authentication)
        );
    }
}

/// RED before XB2: the same-network carrier refuses a peer on another network; the cross-network
/// choice reaches it, under the admitted key, and carries bytes both ways.
#[test]
fn a_peer_on_another_network_is_refused_on_the_same_network_rule_and_reached_across_networks() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    // Today's rule: an endpoint cannot even bind outside the private-LAN class ...
    assert_eq!(
        bind(
            &rt,
            &clock,
            &SecretKey::generate(),
            LanPolicy::private_only()
        )
        .err(),
        Some(Error::Authentication)
    );
    let (server_key, client_key) = (SecretKey::generate(), SecretKey::generate());
    let mut server = bind(&rt, &clock, &server_key, across()).expect("bound across networks");
    assert_eq!(server.mode(), NetworkMode::CrossNetwork);
    server.admit_peer_ids(&[client_key.public()]).unwrap();
    let address = server.address().unwrap();
    assert_eq!(address.id, server_key.public());
    assert_eq!(
        address.relay_urls().cloned().collect::<Vec<_>>(),
        vec![dead_relay()],
        "the address names this host's relay"
    );
    let signed = EndpointAddr::from_parts(
        address.id,
        address.ip_addrs().map(|s| iroh::TransportAddr::Ip(*s)),
    );
    assert!(signed.ip_addrs().all(|s| s.ip() == LOOPBACK));
    // ... and a same-network dialer refuses the signed address of the peer on the other network.
    assert_eq!(
        validate_lan_peer(&signed, &LanPolicy::private_only()),
        Err(Error::Authentication)
    );
    let mut client = bind(&rt, &clock, &client_key, across()).unwrap();
    std::thread::scope(|scope| {
        let accepted = scope.spawn(|| {
            let c = server
                .accept(&clock, deadline(&clock, B::SidecarLocalHandshake))
                .expect("the admitted key is accepted");
            assert_eq!(c.remote_id(), client_key.public());
            rt.block_on(async {
                let (mut send, mut recv) = c.accept_bi().await.unwrap();
                let chunk = recv.read_to_end(1 << 20).await.unwrap();
                send.write_all(&chunk[..32]).await.unwrap();
                send.finish().unwrap();
                send.stopped().await.ok();
                chunk.len()
            })
        });
        let c = client
            .connect(&signed, &clock, deadline(&clock, B::SidecarLocalHandshake))
            .expect("reached across networks");
        assert_eq!(c.remote_id(), server_key.public());
        let echoed = rt.block_on(async {
            let (mut send, mut recv) = c.open_bi().await.unwrap();
            send.write_all(&[7u8; 256 * 1024]).await.unwrap();
            send.finish().unwrap();
            recv.read_to_end(64).await.unwrap()
        });
        assert_eq!(echoed, vec![7u8; 32]);
        assert_eq!(
            accepted.join().unwrap(),
            256 * 1024,
            "a shard-sized chunk arrived"
        );
        c.close(0u32.into(), b"done");
    });
    close(&mut client, &clock);
    close(&mut server, &clock);
}

/// The peer protections stay: an unadmitted key is refused by the host and counted; a dial to an
/// unusable signed address is refused before any packet and counted (`refused_out`).
#[test]
fn an_unadmitted_peer_is_still_refused_across_networks_and_counted() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let (server_key, admitted, stranger) = (
        SecretKey::generate(),
        SecretKey::generate(),
        SecretKey::generate(),
    );
    let mut server = bind(&rt, &clock, &server_key, across()).unwrap();
    server.admit_peer_ids(&[admitted.public()]).unwrap();
    assert_eq!(
        server.lan_dropped_datagrams(),
        Some(0),
        "no refused handshake yet"
    );
    let address = server.address().unwrap();
    let signed = EndpointAddr::from_parts(
        address.id,
        address.ip_addrs().map(|s| iroh::TransportAddr::Ip(*s)),
    );
    let mut intruder = bind(&rt, &clock, &stranger, across()).unwrap();
    std::thread::scope(|scope| {
        let refused = scope.spawn(|| {
            server
                .accept(&clock, deadline(&clock, B::SidecarLocalHandshake))
                .err()
        });
        // The handshake itself completes (TLS cannot know membership); the host closes it.
        if let Ok(c) = intruder.connect(&signed, &clock, deadline(&clock, B::SidecarLocalHandshake))
        {
            let reason = rt.block_on(c.closed());
            assert!(
                format!("{reason:?}").contains("lan_key_refused"),
                "closed by the host's key check: {reason:?}"
            );
        }
        assert_eq!(refused.join().unwrap(), Some(Error::Authentication));
    });
    assert_eq!(
        server.lan_dropped_datagrams(),
        Some(1),
        "the refused handshake is counted"
    );
    // Unusable signed addresses: refused before any packet, counted as refused_out.
    assert_eq!(intruder.lan_refused_datagrams(), Some(0));
    for unusable in [
        EndpointAddr::new(server_key.public()).with_ip_addr("0.0.0.0:4433".parse().unwrap()),
        EndpointAddr::new(server_key.public()).with_ip_addr("203.0.113.9:0".parse().unwrap()),
        EndpointAddr::new(server_key.public()).with_relay_url(dead_relay()),
    ] {
        assert_eq!(
            intruder
                .connect(
                    &unusable,
                    &clock,
                    deadline(&clock, B::SidecarLocalHandshake)
                )
                .err(),
            Some(Error::Authentication)
        );
    }
    assert_eq!(intruder.lan_refused_datagrams(), Some(3));
    close(&mut intruder, &clock);
    close(&mut server, &clock);
}

/// Lane XB3: across networks the relay is the rendezvous. With it unreachable the run fails
/// closed with the explicit `relay_unreachable` code, inside its bound, and never falls back to
/// another relay or to n0 (no other relay and no address lookup is configured at all).
#[test]
fn an_unreachable_relay_fails_closed_with_relay_unreachable() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let mut host = bind(&rt, &clock, &SecretKey::generate(), across()).unwrap();
    let window = B::SidecarReadyAddressWindow.span(Entries::ZERO).unwrap();
    let began = Instant::now();
    assert_eq!(
        host.relay_ready(&clock, deadline(&clock, B::SidecarReadyAddressWindow)),
        Err(RELAY_UNREACHABLE)
    );
    let waited = began.elapsed().as_millis();
    assert!(
        waited + 50 >= u128::from(window.millis()) && waited < u128::from(window.millis()) + 2_000,
        "bounded by the window: {waited} ms"
    );
    // Nothing else to fall back to: the address names exactly the one (dead) relay.
    assert_eq!(
        host.address()
            .unwrap()
            .relay_urls()
            .cloned()
            .collect::<Vec<_>>(),
        vec![dead_relay()]
    );
    close(&mut host, &clock);
    // A same-network run needs no relay: ready at once.
    let mut lan = LoopbackHost::bind_lan(
        &rt,
        &clock,
        deadline(&clock, B::RouterClose),
        SecretKey::generate(),
        &[Channel::Router],
        LOOPBACK,
        LanPolicy::with_explicit(vec![LOOPBACK]).unwrap(),
    )
    .unwrap();
    assert_eq!(lan.mode(), NetworkMode::DirectLan);
    assert_eq!(
        lan.relay_ready(&clock, deadline(&clock, B::SidecarReadyAddressWindow)),
        Ok(())
    );
    close(&mut lan, &clock);
}

/// Lane XB3 at a run's start (the packaged owner run asks before anything is dialed): an
/// unreachable relay refuses the run by name; a same-network run asks nothing.
#[test]
fn a_run_start_across_networks_refuses_an_unreachable_relay() {
    assert_eq!(relay_reachable(LOOPBACK, &across()), Err(RELAY_UNREACHABLE));
    assert_eq!(
        relay_reachable(LOOPBACK, &LanPolicy::with_explicit(vec![LOOPBACK]).unwrap()),
        Ok(())
    );
}

/// Lane XB patch 2 (REVIEW XB-10): across networks the host's admitted-key table gates the DIAL
/// too, not only the accept. A host that admitted its peers' keys (from signed data) refuses to
/// dial any other key before anything is sent: the stranger's address (a plain UDP socket here)
/// receives ZERO datagrams (no QUIC Initial, no path probe), and the refusal is counted
/// (`refused_out`). Without admitted keys the caller verifies the key it dials (the verified-
/// caller boundary every product caller already meets: endpoint ids from signed documents).
#[test]
fn an_unadmitted_key_is_never_dialled_across_networks_zero_outbound_probes() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let (key, admitted, stranger) = (
        SecretKey::generate(),
        SecretKey::generate(),
        SecretKey::generate(),
    );
    let mut host = bind(&rt, &clock, &key, across()).unwrap();
    host.admit_peer_ids(&[admitted.public()]).unwrap();
    let witness = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
    witness
        .set_read_timeout(Some(std::time::Duration::from_millis(400)))
        .unwrap();
    let stranger_at =
        EndpointAddr::new(stranger.public()).with_ip_addr(witness.local_addr().unwrap());
    let before = host.lan_refused_datagrams().unwrap();
    let dialled = host.connect(
        &stranger_at,
        &clock,
        deadline(&clock, B::SidecarLocalHandshake),
    );
    assert_eq!(dialled.err(), Some(Error::Authentication), "refused by key");
    assert_eq!(
        host.lan_refused_datagrams(),
        Some(before + 1),
        "the refused dial is counted"
    );
    let mut buf = [0u8; 2048];
    let received = witness.recv_from(&mut buf);
    assert!(
        received.is_err(),
        "zero outbound probes to the unadmitted key's address: {received:?}"
    );
    close(&mut host, &clock);
}

/// Lane XB patch 3 (REVIEW XB2-04): LIVE path evidence. Every connection a host on another Mac's
/// network dials or accepts is registered (`runtime::iroh_paths`) for its lifetime and sampled
/// when status reads it: here two endpoints across networks reach each other over a direct IP
/// path (the loopback stands for another network, the relay is dead), so BOTH ends report
/// `direct`, under the run's reach `relay`, with their direction; once the connection is closed
/// it is no longer reported. (A relayed path needs a live relay: the packaged status test uses a
/// relay-only double for it, and the probe / hotspot run show the real one.)
#[test]
fn a_live_connection_reports_its_selected_path_on_both_ends_until_it_closes() {
    use runtime::iroh_paths::snapshot;
    let rt = runtime();
    let clock = Mono(Instant::now());
    let (server_key, client_key) = (SecretKey::generate(), SecretKey::generate());
    let short = |key: &SecretKey| {
        key.public()
            .to_string()
            .chars()
            .take(12)
            .collect::<String>()
    };
    let mine = |key: &SecretKey| {
        snapshot()
            .into_iter()
            .filter(|sample| sample.remote == short(key))
            .collect::<Vec<_>>()
    };
    let mut server = bind(&rt, &clock, &server_key, across()).unwrap();
    server.admit_peer_ids(&[client_key.public()]).unwrap();
    let address = server.address().unwrap();
    let signed = EndpointAddr::from_parts(
        address.id,
        address.ip_addrs().map(|s| iroh::TransportAddr::Ip(*s)),
    );
    let mut client = bind(&rt, &clock, &client_key, across()).unwrap();
    std::thread::scope(|scope| {
        let accepted = scope.spawn(|| {
            server
                .accept(&clock, deadline(&clock, B::SidecarLocalHandshake))
                .unwrap()
        });
        let dialled = client
            .connect(&signed, &clock, deadline(&clock, B::SidecarLocalHandshake))
            .unwrap();
        let inbound = accepted.join().unwrap();
        // Sampled when read: wait (bounded) for iroh to have selected the path.
        let began = Instant::now();
        let (out, into) = loop {
            let (out, into) = (mine(&server_key), mine(&client_key));
            let selected = |samples: &[runtime::iroh_paths::PathSample]| {
                samples.iter().any(|s| s.path != "none")
            };
            if (selected(&out) && selected(&into)) || began.elapsed().as_secs() >= 5 {
                break (out, into);
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        };
        assert_eq!(out.len(), 1, "{out:?}");
        assert_eq!(
            (out[0].direction, out[0].reach, out[0].path),
            ("out", "relay", "direct"),
            "{out:?}"
        );
        assert_eq!(into.len(), 1, "{into:?}");
        assert_eq!(
            (into[0].direction, into[0].reach, into[0].path),
            ("in", "relay", "direct"),
            "{into:?}"
        );
        dialled.close(0u32.into(), b"done");
        rt.block_on(inbound.closed());
        drop((dialled, inbound));
    });
    assert!(mine(&server_key).is_empty(), "{:?}", mine(&server_key));
    assert!(mine(&client_key).is_empty(), "{:?}", mine(&client_key));
    close(&mut client, &clock);
    close(&mut server, &clock);
}

/// Lane XB patch 3 (REVIEW XB2-04), the LAN double: two direct-LAN endpoints on the same network
/// (loopback under an explicit-peer policy, as the packaged LAN tests run) report `direct` under
/// the reach `same_network` while connected, and nothing once closed.
#[test]
fn a_live_same_network_connection_reports_a_direct_path() {
    use runtime::iroh_paths::snapshot;
    let rt = runtime();
    let clock = Mono(Instant::now());
    let lan = || LanPolicy::with_explicit(vec![LOOPBACK]).unwrap();
    let (member_key, owner_key) = (SecretKey::generate(), SecretKey::generate());
    let short = |key: &SecretKey| {
        key.public()
            .to_string()
            .chars()
            .take(12)
            .collect::<String>()
    };
    let mine = |key: &SecretKey| {
        snapshot()
            .into_iter()
            .filter(|sample| sample.remote == short(key))
            .collect::<Vec<_>>()
    };
    let mut member = bind(&rt, &clock, &member_key, lan()).unwrap();
    assert_eq!(member.mode(), NetworkMode::DirectLan);
    member.admit_peer_ids(&[owner_key.public()]).unwrap();
    let address = member.address().unwrap();
    let mut owner = bind(&rt, &clock, &owner_key, lan()).unwrap();
    std::thread::scope(|scope| {
        let accepting = scope.spawn(|| {
            let window = deadline(&clock, B::WorkerStartup);
            loop {
                match member.accept(&clock, window) {
                    Err(Error::Authentication) => continue,
                    other => return other.unwrap(),
                }
            }
        });
        let dialled = owner
            .connect(&address, &clock, deadline(&clock, B::WorkerStartup))
            .unwrap();
        let inbound = accepting.join().unwrap();
        let began = Instant::now();
        let (out, into) = loop {
            let (out, into) = (mine(&member_key), mine(&owner_key));
            let selected = |samples: &[runtime::iroh_paths::PathSample]| {
                samples.iter().any(|s| s.path != "none")
            };
            if (selected(&out) && selected(&into)) || began.elapsed().as_secs() >= 5 {
                break (out, into);
            }
            std::thread::sleep(std::time::Duration::from_millis(50));
        };
        assert_eq!(
            out.iter()
                .map(|s| (s.direction, s.reach, s.path))
                .collect::<Vec<_>>(),
            vec![("out", "same_network", "direct")],
            "{out:?}"
        );
        assert_eq!(
            into.iter()
                .map(|s| (s.direction, s.reach, s.path))
                .collect::<Vec<_>>(),
            vec![("in", "same_network", "direct")],
            "{into:?}"
        );
        dialled.close(0u32.into(), b"done");
        rt.block_on(inbound.closed());
        drop((dialled, inbound));
    });
    assert!(mine(&member_key).is_empty() && mine(&owner_key).is_empty());
    close(&mut owner, &clock);
    close(&mut member, &clock);
}

/// Lane XB patch 3 (REVIEW XB2-09): what the inbound gate across networks guarantees, and no more.
/// An unadmitted key's handshake COMPLETES (TLS authenticates a key, not membership); the host
/// then closes the connection (`lan_key_refused`) and counts it BEFORE any caller sees it. So the
/// stranger gets no application stream (whatever it opens and writes is never answered: its read
/// ends in the host's close), the host's caller is never handed the connection (its accept answers
/// `Authentication`, and the next accept window ends empty), and every attempt is counted. NOT
/// shown (and not claimed): what the stranger's handshake itself sent or caused before that
/// post-handshake check (the transport's pre-admission traffic of a handshake).
#[test]
fn an_unadmitted_inbound_peer_gets_no_application_stream_and_is_counted() {
    let rt = runtime();
    let clock = Mono(Instant::now());
    let (server_key, admitted, stranger) = (
        SecretKey::generate(),
        SecretKey::generate(),
        SecretKey::generate(),
    );
    let mut server = bind(&rt, &clock, &server_key, across()).unwrap();
    server.admit_peer_ids(&[admitted.public()]).unwrap();
    let address = server.address().unwrap();
    let signed = EndpointAddr::from_parts(
        address.id,
        address.ip_addrs().map(|s| iroh::TransportAddr::Ip(*s)),
    );
    let mut intruder = bind(&rt, &clock, &stranger, across()).unwrap();
    for attempt in 1..=2u64 {
        std::thread::scope(|scope| {
            let refused = scope.spawn(|| {
                server
                    .accept(&clock, deadline(&clock, B::SidecarLocalHandshake))
                    .err()
            });
            if let Ok(c) =
                intruder.connect(&signed, &clock, deadline(&clock, B::SidecarLocalHandshake))
            {
                let answer = rt.block_on(async {
                    tokio::time::timeout(std::time::Duration::from_secs(10), async {
                        let (mut send, mut recv) = c.open_bi().await.map_err(|e| e.to_string())?;
                        send.write_all(b"model bytes, please")
                            .await
                            .map_err(|e| e.to_string())?;
                        send.finish().map_err(|e| e.to_string())?;
                        recv.read_to_end(64).await.map_err(|e| e.to_string())
                    })
                    .await
                });
                assert!(
                    !matches!(answer, Ok(Ok(_))),
                    "attempt {attempt}: the stranger got an application answer: {answer:?}"
                );
                let reason = rt.block_on(c.closed());
                assert!(
                    format!("{reason:?}").contains("lan_key_refused"),
                    "attempt {attempt}: closed by the host's key check: {reason:?}"
                );
            }
            assert_eq!(
                refused.join().unwrap(),
                Some(Error::Authentication),
                "attempt {attempt}: the caller is told of a refusal, never handed it"
            );
        });
        assert_eq!(
            server.lan_dropped_datagrams(),
            Some(attempt),
            "every refused attempt is counted"
        );
    }
    // Nothing was handed on: the next accept window ends empty.
    let empty = clock
        .monotonic()
        .checked_add(budgets::Span::from_millis(500))
        .unwrap();
    assert_eq!(server.accept(&clock, empty).err(), Some(Error::Expired));
    close(&mut intruder, &clock);
    close(&mut server, &clock);
}
