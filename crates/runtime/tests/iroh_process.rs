// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NET-09 (authenticated QUIC against an exact-owned child speaking the sidecar wire; partial: not the historical executable); B-NET-21 (partial).
//!
//! Real authenticated QUIC between this test process and an exact-owned Rust
//! child. The child is a source-derived one-attempt sidecar wire fixture, NOT
//! the historical executable or the complete owner/member application.
#![cfg(feature = "iroh-adapter")]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use iroh::{Endpoint, EndpointAddr, EndpointId, RelayMode, endpoint::presets};
use protocol::remote::{self, RemoteKind};
use runtime::{iroh::IrohTransport, iroh_host::LoopbackHost};
use std::{
    cell::Cell,
    io::{BufRead, BufReader, Write},
    net::{Ipv4Addr, SocketAddr, SocketAddrV4, UdpSocket},
    process::{Child, Command, Stdio},
    sync::mpsc::{self, Receiver},
    thread::JoinHandle,
    time::{Duration, Instant},
};
use transport::{Channel, Clock, ConnectionFence, Error, Exchange, Transport};

include!("support/iroh_corpus.rs");
include!("support/iroh_native_corpus.rs");
include!("support/iroh_control.rs");
include!("support/iroh_managed.rs");
#[path = "support/iroh_journal.rs"]
mod journal;
include!("support/iroh_artifact.rs");
include!("support/artifact_objects.rs");
include!("support/artifact_auth.rs");
include!("support/fork_boundary.rs");
include!("support/iroh_dispatch.rs");
fn allowance() -> Duration {
    Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis())
}
struct Time {
    start: Instant,
    offset: Cell<u64>,
    domain: Cell<ClockDomain>,
}
impl Time {
    fn new() -> Self {
        Self {
            start: Instant::now(),
            offset: Cell::new(0),
            domain: Cell::new(ClockDomain(9)),
        }
    }
    fn deadline(&self) -> MonoInstant {
        self.monotonic()
            .checked_add(B::RouterClose.span(Entries::ZERO).unwrap())
            .unwrap()
    }
}
impl Clock for Time {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(
            self.domain.get(),
            self.start.elapsed().as_millis() as u64 + self.offset.get(),
        )
    }
    fn wall_seconds(&self) -> f64 {
        panic!("transport must not consult wall time")
    }
}
fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
}

struct Peer {
    child: Child,
    lines: Receiver<String>,
    reader: Option<JoinHandle<()>>,
    address: EndpointAddr,
    socket: SocketAddr,
    reaped: bool,
}
impl Peer {
    fn spawn(client: EndpointId, mode: &str) -> Self {
        let mut child = spawn_fixture_process(
            Command::new(std::env::current_exe().unwrap())
                .args(["--exact", "iroh_child_fixture", "--nocapture"])
                .env("L9_IROH_CHILD", mode)
                .env("L9_EXPECTED_PEER", client.to_string())
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::inherit()),
        )
        .unwrap();
        let output = child.stdout.take().unwrap();
        let (tx, lines) = mpsc::channel();
        let reader = std::thread::spawn(move || {
            for line in BufReader::new(output).lines() {
                let line = line.expect("child stdout read");
                if tx.send(line).is_err() {
                    return;
                }
            }
        });
        // Construct cleanup owner before any readiness assertion can panic.
        let mut peer = Self {
            child,
            lines,
            reader: Some(reader),
            address: EndpointAddr::new(client),
            socket: SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0).into(),
            reaped: false,
        };
        let line = peer.line("L9_READY ");
        let value: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(value["pid"].as_u64().unwrap(), u64::from(peer.child.id()));
        assert_ne!(peer.child.id(), std::process::id());
        peer.socket = value["socket"].as_str().unwrap().parse().unwrap();
        assert!(peer.socket.ip().is_loopback());
        let id: EndpointId = value["id"].as_str().unwrap().parse().unwrap();
        peer.address = EndpointAddr::new(id).with_ip_addr(peer.socket);
        peer
    }
    fn line(&self, prefix: &str) -> String {
        let end = Instant::now() + allowance();
        loop {
            let line = self
                .lines
                .recv_timeout(end.saturating_duration_since(Instant::now()))
                .expect("child readiness/completion deadline");
            if let Some(value) = line.strip_prefix(prefix) {
                return value.to_owned();
            }
        }
    }
    fn finish(&mut self) {
        assert_eq!(self.line("L9_CLOSED "), "true");
        let status = self.child.wait().unwrap();
        self.reaped = true;
        self.reader.take().unwrap().join().unwrap();
        assert!(status.success(), "child failed: {status}");
        // Keep this exact socket-release assertion inside the same fork/socket
        // boundary as endpoint binds and fixture spawn (see fork_boundary.rs).
        assert_socket_released(self.socket, "child UDP socket leaked");
    }
}
impl Drop for Peer {
    fn drop(&mut self) {
        if !self.reaped {
            if self.child.try_wait().expect("child status").is_none() {
                self.child.kill().expect("kill exact owned fixture PID");
            }
            self.child.wait().expect("reap exact owned fixture PID");
        }
        if let Some(reader) = self.reader.take() {
            reader.join().expect("join child stdout reader");
        }
    }
}

#[test]
fn iroh_child_fixture() {
    let Ok(mode) = std::env::var("L9_IROH_CHILD") else {
        return;
    };
    let expected: EndpointId = std::env::var("L9_EXPECTED_PEER").unwrap().parse().unwrap();
    if mode.starts_with("journal-") {
        journal::child(expected, &mode);
        return;
    }
    if mode.starts_with("managed-") {
        managed_child(expected, &mode);
        return;
    }
    if mode.starts_with("artifact-") {
        artifact_child(expected, &mode);
        return;
    }
    if mode.starts_with("control-") {
        control_child(expected, &mode);
        return;
    }
    if mode == "native-corpus" {
        native_corpus_child(expected);
        return;
    }
    if mode == "dispatch" {
        dispatch_child(expected);
        return;
    }
    if mode == "corpus" {
        receiver_child(expected);
        return;
    }
    let rt = runtime();
    let sockets = rt.block_on(async {
        // Exact local-only builder in sidecar.rs:834-865 @ f4e366e9.
        let endpoint = Endpoint::builder(presets::Minimal)
            .relay_mode(RelayMode::Disabled).clear_address_lookup().clear_ip_transports()
            .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0)).unwrap()
            .alpns(vec![if mode == "alpn" { Channel::NodeControl } else { Channel::Router }.alpn().to_vec()])
            .bind().await.unwrap();
        let sockets = endpoint.bound_sockets();
        assert_eq!(sockets.len(), 1);
        println!("L9_READY {}", serde_json::json!({"id":endpoint.id().to_string(),"socket":sockets[0].to_string(),"pid":std::process::id()}));
        std::io::stdout().flush().unwrap();
        let incoming = tokio::time::timeout(allowance(), endpoint.accept()).await.unwrap().unwrap();
        let connection = tokio::time::timeout(allowance(), incoming).await.unwrap();
        if mode == "alpn" {
            assert!(connection.is_err(), "Router ALPN must not negotiate NodeControl");
        } else {
            let connection = connection.unwrap();
            assert_eq!(connection.remote_id(), expected, "TLS peer identity");
            assert_eq!(connection.alpn(), Channel::Router.alpn());
            loop {
                let streams = tokio::time::timeout(allowance(), connection.accept_bi()).await.unwrap();
                let Ok((mut send, mut recv)) = streams else { break; };
                let bytes = recv.read_to_end(remote::REMOTE_MAX_FRAME_BYTES).await.unwrap();
                let frame = remote::decode_remote_frame(&bytes).unwrap();
                assert_eq!(frame.kind, RemoteKind::Transfer);
                assert_eq!(&frame.payload[..remote::REMOTE_GENERATION_BYTES], &2u64.to_be_bytes(), "source generation prefix");
                assert_eq!(&frame.payload[remote::REMOTE_GENERATION_BYTES..], payload().as_slice(), "Router bytes unchanged");
                if mode == "hold" {
                    println!("L9_RECEIVED true");
                    std::io::stdout().flush().unwrap();
                    connection.closed().await;
                    break;
                }
                let mut id = frame.message_id;
                let mut kind = RemoteKind::Ack;
                let data: &[u8] = match mode.as_str() {
                    "nonempty" => b"invalid-ack-body",
                    "rotated" => { kind = RemoteKind::Error; b"peer_rotated" },
                    "replay" => { kind = RemoteKind::Error; b"replay_collision" },
                    "queue" => { kind = RemoteKind::Error; b"queue_full" },
                    "error" => { kind = RemoteKind::Error; b"invalid_frame" },
                    "wrong-id" => { id = [99; 16]; b"" },
                    _ => b"",
                };
                let mut response = remote::encode_remote_frame(kind, id, data).unwrap();
                match mode.as_str() {
                    "truncated" => { response.pop(); },
                    "trailing" => response.push(0),
                    "version" => response[0] = 0,
                    "kind" => response[1] = 0,
                    _ => {},
                }
                send.write_all(&response).await.unwrap();
                send.finish().unwrap();
                // Hold endpoint until the client actually retires the carrier.
            }
            drop(connection);
        }
        endpoint.close().await;
        drop(endpoint);
        sockets
    });
    // Endpoint::close joins drivers; runtime drop also reaps aborted internal
    // actor destructors. Keep the original OS-socket assertion after both.
    drop(rt);
    assert!(
        UdpSocket::bind(sockets[0]).is_ok(),
        "child endpoint socket retained"
    );
    println!("L9_CLOSED true");
}
#[test]
fn caller_owned_endpoint_identity_survives_close_and_rebind() {
    let secret = iroh::SecretKey::from_bytes(&[37; 32]);
    for _ in 0..2 {
        let rt = runtime();
        let clock = Time::new();
        let mut host =
            bind_host_with_secret(&rt, &clock, clock.deadline(), secret.clone()).unwrap();
        let address = host.address().unwrap();
        let socket = *address.ip_addrs().next().unwrap();
        let actual = address.id;
        host.close(&clock, clock.deadline()).unwrap();
        drop(host);
        drop(rt);
        assert_socket_released(socket, "old endpoint must release before reuse");
        assert_eq!(
            actual,
            secret.public(),
            "binding must retain caller endpoint identity"
        );
    }
}
#[test]
fn socket_creation_is_ordered_against_concurrent_fixture_spawn() {
    // Deterministic S23 control with no QUIC: a UDP socket created while other
    // threads continuously spawn fixture processes must be reusable right after
    // it is closed. Each spawned child lives long enough to hold an inherited
    // descriptor, which is exactly the leak the boundary has to exclude.
    struct Stop<'a>(&'a std::sync::atomic::AtomicBool);
    impl Drop for Stop<'_> {
        fn drop(&mut self) {
            self.0.store(true, std::sync::atomic::Ordering::SeqCst);
        }
    }
    let stop = std::sync::atomic::AtomicBool::new(false);
    let spawned = std::sync::atomic::AtomicUsize::new(0);
    std::thread::scope(|scope| {
        let _stop = Stop(&stop);
        let spawners: Vec<_> = (0..3)
            .map(|_| {
                scope.spawn(|| {
                    while !stop.load(std::sync::atomic::Ordering::SeqCst) {
                        // Without L9_IROH_CHILD this is the no-op fixture entry.
                        let mut child = spawn_fixture_process(
                            Command::new(std::env::current_exe().unwrap())
                                .args(["--exact", "iroh_child_fixture"])
                                .stdin(Stdio::null())
                                .stdout(Stdio::null())
                                .stderr(Stdio::null()),
                        )
                        .unwrap();
                        assert!(child.wait().unwrap().success());
                        spawned.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                    }
                })
            })
            .collect();
        // Bounded by spawn progress, not by elapsed time.
        while spawned.load(std::sync::atomic::Ordering::SeqCst) < 60 {
            assert!(
                spawners.iter().any(|spawner| !spawner.is_finished()),
                "every pressure spawner ended before the bound"
            );
            let socket =
                bind_udp_fixture(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0).into()).unwrap();
            let address = socket.local_addr().unwrap();
            drop(socket);
            assert_socket_released(
                address,
                "closed socket must be reusable under spawn pressure",
            );
        }
    });
}
fn payload() -> Vec<u8> {
    // Original oracle-recorded L5 HopHeader, not the encoder under test.
    let fixture: serde_json::Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continued-wire-vectors.json"
    ))
    .unwrap();
    let hex = fixture[0]["encoded_hex"].as_str().unwrap();
    (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
        .collect()
}
fn request(peer: &Peer, clock: &Time) -> Exchange {
    Exchange {
        peer: ConnectionFence {
            endpoint_id: *peer.address.id.as_bytes(),
            local_generation: 2,
            remote_generation: 3,
        },
        channel: Channel::Router,
        message_id: [5; 16],
        payload: payload(),
        deadline: clock.deadline(),
        maximum_response_bytes: 0,
    }
}
#[test]
fn real_quic_router_sidecar_ack_and_l5_fence_fault_matrix() {
    for (mode, expected) in [
        ("ack", Ok(())),
        ("wrong-id", Err(Error::Correlation)),
        ("nonempty", Err(Error::Unavailable)),
        ("rotated", Err(Error::Generation)),
        ("replay", Err(Error::Unavailable)),
        ("queue", Err(Error::Unavailable)),
        ("error", Err(Error::Unavailable)),
        ("truncated", Err(Error::Unavailable)),
        ("trailing", Err(Error::Unavailable)),
        ("version", Err(Error::Unavailable)),
        ("kind", Err(Error::Unavailable)),
    ] {
        let rt = runtime();
        let clock = Time::new();
        let mut host = bind_host(&rt, &clock, clock.deadline()).unwrap();
        let local = host.address().unwrap();
        let mut peer = Peer::spawn(local.id, mode);
        let r = request(&peer, &clock);
        let conn = host.connect(&peer.address, &clock, r.deadline).unwrap();
        let mut carrier = IrohTransport::new(conn, &rt, &clock, r.peer, || r.peer).unwrap();
        assert_eq!(
            transport::exchange(&mut carrier, &r, &clock).map(|bytes| assert!(bytes.is_empty())),
            expected,
            "{mode}"
        );
        carrier.close(clock.deadline()).unwrap();
        assert!(matches!(carrier.exchange(&r), Err(Error::Unavailable)));
        drop(carrier);
        host.close(&clock, clock.deadline()).unwrap();
        assert!(host.address().is_err());
        drop(host);
        drop(rt);
        for socket in local.ip_addrs() {
            assert_socket_released(*socket, "host socket leaked");
        }
        peer.finish();
    }
}
#[test]
fn real_quic_auth_generation_channel_deadline_cancel_and_close() {
    let rt = runtime();
    let clock = Time::new();
    let mut host = bind_host(&rt, &clock, clock.deadline()).unwrap();
    let mut peer = Peer::spawn(host.address().unwrap().id, "ack");
    let mut r = request(&peer, &clock);
    let conn = host.connect(&peer.address, &clock, r.deadline).unwrap();
    let original = r.peer;
    let mut wrong = original;
    wrong.endpoint_id = [8; 32];
    assert!(matches!(
        IrohTransport::new(conn.clone(), &rt, &clock, wrong, || wrong),
        Err(Error::Authentication)
    ));
    wrong = original;
    wrong.remote_generation += 1;
    assert!(matches!(
        IrohTransport::new(conn.clone(), &rt, &clock, original, || wrong),
        Err(Error::Generation)
    ));
    let current = Cell::new(original);
    let mut carrier = IrohTransport::new(conn, &rt, &clock, original, || current.get()).unwrap();
    for error in [
        Error::Authentication,
        Error::Generation,
        Error::Channel,
        Error::Expired,
        Error::ClockDomain,
    ] {
        r.peer = original;
        r.channel = Channel::Router;
        r.deadline = clock.deadline();
        match error {
            Error::Authentication => r.peer.endpoint_id = [8; 32],
            Error::Generation => r.peer.local_generation += 1,
            Error::Channel => r.channel = Channel::NodeControl,
            Error::Expired => r.deadline = clock.monotonic(),
            Error::ClockDomain => {
                r.deadline = MonoInstant::new(ClockDomain(10), r.deadline.ticks())
            }
            _ => unreachable!(),
        }
        assert!(matches!(carrier.exchange(&r), Err(e) if e == error));
    }
    r = request(&peer, &clock);
    assert!(
        transport::exchange(&mut carrier, &r, &clock)
            .unwrap()
            .is_empty()
    );
    carrier.cancel(r.message_id, clock.deadline()).unwrap();
    assert!(matches!(carrier.exchange(&r), Err(Error::Unavailable)));
    carrier.close(clock.deadline()).unwrap();
    drop(carrier);
    host.close(&clock, clock.deadline()).unwrap();
    peer.finish();
}
#[test]
fn real_quic_late_membership_change_retires_ambiguous_carrier() {
    let rt = runtime();
    let clock = Time::new();
    let mut host = bind_host(&rt, &clock, clock.deadline()).unwrap();
    let mut peer = Peer::spawn(host.address().unwrap().id, "ack");
    let r = request(&peer, &clock);
    let conn = host.connect(&peer.address, &clock, r.deadline).unwrap();
    let calls = Cell::new(0);
    let mut carrier = IrohTransport::new(conn, &rt, &clock, r.peer, || {
        let call = calls.get();
        calls.set(call + 1);
        let mut fence = r.peer;
        // Construction, send admission, then after real ACK: authority rotated.
        if call == 2 {
            fence.remote_generation += 1;
        }
        fence
    })
    .unwrap();
    assert!(matches!(carrier.exchange(&r), Err(Error::Generation)));
    // Authority returning to the old value must not revive an ambiguous carrier.
    assert!(
        matches!(carrier.exchange(&r), Err(Error::Unavailable)),
        "post-ACK fence failure must retire carrier"
    );
    carrier.close(clock.deadline()).unwrap();
    drop(carrier);
    host.close(&clock, clock.deadline()).unwrap();
    peer.finish();
}
#[test]
fn real_quic_inflight_cancellation_unblocks_without_ack_or_replay() {
    let rt = runtime();
    let clock = Time::new();
    let mut host = bind_host(&rt, &clock, clock.deadline()).unwrap();
    let mut peer = Peer::spawn(host.address().unwrap().id, "hold");
    let r = request(&peer, &clock);
    let conn = host.connect(&peer.address, &clock, r.deadline).unwrap();
    let mut carrier = IrohTransport::new(conn, &rt, &clock, r.peer, || r.peer).unwrap();
    let cancellation = carrier.cancellation_handle();
    std::thread::scope(|scope| {
        let lines = &mut peer.lines;
        let task = scope.spawn(move || {
            let end = Instant::now() + allowance();
            loop {
                let line = lines
                    .recv_timeout(end.saturating_duration_since(Instant::now()))
                    .unwrap();
                if line == "L9_RECEIVED true" {
                    break;
                }
            }
            cancellation.request_cancel();
        });
        assert!(
            matches!(carrier.exchange(&r), Err(Error::Cancelled)),
            "in-flight cancellation must not claim delivery"
        );
        task.join().unwrap();
    });
    assert!(matches!(carrier.exchange(&r), Err(Error::Unavailable)));
    carrier.close(clock.deadline()).unwrap();
    drop(carrier);
    host.close(&clock, clock.deadline()).unwrap();
    peer.finish();
}
#[test]
fn real_quic_rejects_other_alpn_and_host_nonlocal_addresses() {
    let rt = runtime();
    let clock = Time::new();
    let mut host = bind_host(&rt, &clock, clock.deadline()).unwrap();
    let mut peer = Peer::spawn(host.address().unwrap().id, "alpn");
    assert!(
        host.connect(&peer.address, &clock, clock.deadline())
            .is_err()
    );
    let nonlocal = EndpointAddr::new(peer.address.id)
        .with_ip_addr("192.0.2.1:40000".parse::<SocketAddr>().unwrap());
    assert!(matches!(
        host.connect(&nonlocal, &clock, clock.deadline()),
        Err(Error::Authentication)
    ));
    host.close(&clock, clock.deadline()).unwrap();
    peer.finish();
}
