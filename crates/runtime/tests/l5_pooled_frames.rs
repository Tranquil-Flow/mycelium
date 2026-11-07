// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NET-09 (the L5 Router frame families over PooledRouter between two OS processes).
//!
//! L5 Router frame families carried over L9's `PooledRouter` between two OS processes.
//!
//! The parent hosts a real `PooledRouter` on a loopback-only endpoint; an exact-owned
//! child process of this same test binary is the wire peer (raw acceptor or raw
//! sender). Every one of the 18 oracle-recorded `mycelium.router_wire.v1` message
//! families (`continued-wire-vectors.json`, positive vectors) must cross byte-exact in
//! both directions: the outer stream bytes the child observes equal an independently
//! framed `Transfer` (generation prefix + the oracle's encoded frame), and a frame sent
//! by the child is delivered to the parent's session unchanged and answered with the
//! exact empty-`Ack` frame. Not a physical split and not a whole-application test.
#![cfg(feature = "iroh-adapter")]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant, Span};
use iroh::{
    Endpoint, EndpointAddr, EndpointId, RelayMode,
    endpoint::{ReadToEndError, presets},
};
use protocol::{
    artifact_grant as ag,
    identity::Identity,
    remote::{self, RemoteKind},
    router_wire,
};
use runtime::{
    iroh_artifact::{ArtifactError, ArtifactWireLimits, RESPONSE_PROTOCOL, frame_response},
    iroh_artifact_source::{AdmittedChunk, ArtifactObjects, ArtifactServeLimits, ArtifactService},
    iroh_host::LoopbackHost,
    iroh_pool::{DriverLimits, PooledRouter, Services},
    sidecar_pool::{EnqueueOutcome, EnqueueRequest, OutboundTerminal, PoolLimits},
};
use std::{
    io::{BufRead, BufReader, Write},
    net::{Ipv4Addr, SocketAddr, SocketAddrV4, UdpSocket},
    process::{Child, Command, Stdio},
    sync::Arc,
    sync::atomic::Ordering,
    time::{Duration, Instant},
};
use tokio::sync::mpsc::{UnboundedReceiver, unbounded_channel};
use transport::{Channel, Clock};

include!("support/fork_lock.rs");

const FAMILIES: usize = 18;
/// The pinned peer-table generation for both processes' view of the parent.
const GENERATION: u64 = 2;

struct Mono(Instant);
impl Clock for Mono {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(ClockDomain(51), self.0.elapsed().as_millis() as u64)
    }
    fn wall_seconds(&self) -> f64 {
        panic!("the pooled driver must not consult wall time")
    }
}
fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
}
fn ms(value: u64) -> Span {
    Span::from_millis(value)
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn unhex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}
fn id(n: usize) -> [u8; 16] {
    [u8::try_from(n).unwrap(); 16]
}
/// The 18 oracle-recorded router message families: (name, encoded frame).
fn families() -> Vec<(String, Vec<u8>)> {
    let fixture: Vec<serde_json::Value> = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continued-wire-vectors.json"
    ))
    .unwrap();
    let out: Vec<(String, Vec<u8>)> = fixture
        .iter()
        .map(|row| {
            (
                row["name"].as_str().unwrap().to_owned(),
                unhex(row["encoded_hex"].as_str().unwrap()),
            )
        })
        .collect();
    assert_eq!(out.len(), FAMILIES);
    for (name, frame) in &out {
        assert!(router_wire::decode_frame(frame).is_ok(), "{name}");
    }
    out
}
fn driver() -> DriverLimits {
    DriverLimits {
        pool: PoolLimits {
            capacity: FAMILIES,
            seen_factor: 8,
            seen_floor: 64,
            initial_retry: ms(200),
            max_retry: ms(400),
            lifetime: ms(60_000),
        },
        connect: ms(5_000),
        stream_io: ms(10_000),
    }
}
/// Socket release is checked under the S23 fork/socket ordering lock (a sibling test's
/// fork must not hold a freshly closed descriptor open across the probe).
fn assert_released(socket: SocketAddr, what: &str) {
    let _boundary = fixture_spawn_boundary();
    assert!(UdpSocket::bind(socket).is_ok(), "{what}");
}
/// Every wait for a child line or a terminal is bounded by twice the registry close budget.
fn wait() -> Duration {
    Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis() * 2)
}
fn pause(units: u64) -> Duration {
    Duration::from_millis(units)
}

// ---------------------------------------------------------------- child side
static KEPT: std::sync::Mutex<Vec<tokio::task::JoinHandle<()>>> = std::sync::Mutex::new(Vec::new());
fn keep(future: impl std::future::Future<Output = ()> + Send + 'static) {
    KEPT.lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .push(tokio::spawn(future));
}
async fn reap_kept() {
    let all = std::mem::take(
        &mut *KEPT
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner),
    );
    for task in &all {
        task.abort();
    }
    for task in all {
        if task.await.is_err() {
            // Aborted by design.
        }
    }
}
fn child_endpoint() -> iroh::endpoint::Builder {
    Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Disabled)
        .clear_address_lookup()
        .clear_ip_transports()
        .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
        .unwrap()
        .alpns(vec![
            Channel::Router.alpn().to_vec(),
            Channel::NodeControl.alpn().to_vec(),
        ])
}
fn line(text: String) {
    println!("{text}");
    std::io::stdout().flush().unwrap();
}
fn announce(endpoint: &Endpoint) -> SocketAddr {
    let sockets = endpoint.bound_sockets();
    assert_eq!(sockets.len(), 1);
    line(format!(
        "L5_READY {}",
        serde_json::json!({"id":endpoint.id().to_string(),"socket":sockets[0].to_string(),"pid":std::process::id()})
    ));
    sockets[0]
}
static STDIN_READER: std::sync::Mutex<Vec<std::thread::JoinHandle<()>>> =
    std::sync::Mutex::new(Vec::new());
fn stdin_commands() -> UnboundedReceiver<String> {
    let (tx, rx) = unbounded_channel();
    let reader = std::thread::spawn(move || {
        for line in std::io::stdin().lines() {
            let Ok(line) = line else { return };
            if tx.send(line).is_err() {
                return;
            }
        }
    });
    STDIN_READER
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .push(reader);
    rx
}
fn finish_child(rt: tokio::runtime::Runtime, socket: SocketAddr) {
    drop(rt);
    assert!(UdpSocket::bind(socket).is_ok(), "child socket retained");
    line("L5_CLOSED true".to_owned());
}

/// Raw acceptor: prints each received stream's exact bytes and answers an empty Ack.
fn acceptor_child() {
    let rt = runtime();
    let socket = rt.block_on(async {
        let endpoint = child_endpoint().bind().await.unwrap();
        let socket = announce(&endpoint);
        let mut commands = stdin_commands();
        loop {
            tokio::select! {
                incoming = endpoint.accept() => {
                    let Some(incoming) = incoming else { break };
                    keep(async move {
                        let Ok(connection) = incoming.await else { return };
                        while let Ok((mut send, mut recv)) = connection.accept_bi().await {
                            let Ok(bytes) = recv.read_to_end(remote::REMOTE_MAX_FRAME_BYTES).await else { continue };
                            let frame = remote::decode_remote_frame(&bytes).unwrap();
                            line(format!("L5_STREAM {} {}", hex(&frame.message_id), hex(&bytes)));
                            let ack = remote::encode_remote_frame(RemoteKind::Ack, frame.message_id, b"").unwrap();
                            send.write_all(&ack).await.unwrap();
                            send.finish().unwrap();
                        }
                    });
                }
                command = commands.recv() => {
                    if command.as_deref() == Some("quit") || command.is_none() { break; }
                }
            }
        }
        reap_kept().await;
        endpoint.close().await;
        socket
    });
    finish_child(rt, socket);
}

/// Raw sender: frames an oracle frame as a Transfer and prints the exact reply bytes.
fn sender_child() {
    let parent_id: EndpointId = std::env::var("L5_PARENT_ID").unwrap().parse().unwrap();
    let parent_socket: SocketAddr = std::env::var("L5_PARENT_SOCKET").unwrap().parse().unwrap();
    let rt = runtime();
    let socket = rt.block_on(async {
        let endpoint = child_endpoint().bind().await.unwrap();
        let socket = announce(&endpoint);
        let mut commands = stdin_commands();
        let mut connection = None;
        while let Some(command) = commands.recv().await {
            let parts: Vec<&str> = command.split(' ').collect();
            match parts[0] {
                "quit" => break,
                "connect" => {
                    let target = EndpointAddr::new(parent_id).with_ip_addr(parent_socket);
                    connection = Some(
                        endpoint
                            .connect(target, Channel::Router.alpn())
                            .await
                            .unwrap(),
                    );
                    line("L5_CONNECTED".to_owned());
                }
                "send" => {
                    let number: usize = parts[1].parse().unwrap();
                    let inner = unhex(parts[2]);
                    let conn = connection.clone().expect("connect first");
                    keep(async move {
                        let (mut send, mut recv) = conn.open_bi().await.unwrap();
                        let payload = [GENERATION.to_be_bytes().as_slice(), &inner].concat();
                        let frame =
                            remote::encode_remote_frame(RemoteKind::Transfer, id(number), &payload)
                                .unwrap();
                        send.write_all(&frame).await.unwrap();
                        send.finish().unwrap();
                        let result = match recv.read_to_end(remote::REMOTE_MAX_FRAME_BYTES).await {
                            Ok(bytes) => hex(&bytes),
                            Err(ReadToEndError::Read(error)) => format!("readerr:{error}"),
                            Err(other) => format!("readerr:{other}"),
                        };
                        line(format!("L5_RESP {number} {result}"));
                    });
                }
                other => panic!("unknown command {other}"),
            }
        }
        if let Some(conn) = connection {
            conn.close(0u32.into(), b"done");
        }
        reap_kept().await;
        endpoint.close().await;
        socket
    });
    finish_child(rt, socket);
}

/// Raw artifact-ALPN client: writes one request per stream, prints the exact response bytes.
fn artifact_client_child() {
    let parent_id: EndpointId = std::env::var("L5_PARENT_ID").unwrap().parse().unwrap();
    let parent_socket: SocketAddr = std::env::var("L5_PARENT_SOCKET").unwrap().parse().unwrap();
    let rt = runtime();
    let socket = rt.block_on(async {
        let endpoint = child_endpoint().bind().await.unwrap();
        let socket = announce(&endpoint);
        let mut commands = stdin_commands();
        let mut connection = None;
        while let Some(command) = commands.recv().await {
            let parts: Vec<&str> = command.split(' ').collect();
            match parts[0] {
                "quit" => break,
                "connect" => {
                    let target = EndpointAddr::new(parent_id).with_ip_addr(parent_socket);
                    connection = Some(
                        endpoint
                            .connect(target, Channel::ArtifactChunk.alpn())
                            .await
                            .unwrap(),
                    );
                    line("L5_CONNECTED".to_owned());
                }
                "fetch" => {
                    let number: usize = parts[1].parse().unwrap();
                    let request = unhex(parts[2]);
                    let conn = connection.clone().expect("connect first");
                    let (mut send, mut recv) = conn.open_bi().await.unwrap();
                    send.write_all(&request).await.unwrap();
                    send.finish().unwrap();
                    let result = match recv.read_to_end(ARTIFACT_RESPONSE_CAP).await {
                        Ok(bytes) => hex(&bytes),
                        Err(error) => format!("readerr:{error}"),
                    };
                    line(format!("L5_RESP {number} {result}"));
                }
                other => panic!("unknown command {other}"),
            }
        }
        if let Some(conn) = connection {
            conn.close(0u32.into(), b"done");
        }
        reap_kept().await;
        endpoint.close().await;
        socket
    });
    finish_child(rt, socket);
}
const ARTIFACT_RESPONSE_CAP: usize = 1 << 20;

/// NodeControl receiver: prints each request's exact bytes and answers one of two canned
/// responses (even calls the first, odd calls the second).
fn control_acceptor_child() {
    let responses = [
        unhex(&std::env::var("L5_CONTROL_FIRST").unwrap()),
        unhex(&std::env::var("L5_CONTROL_SECOND").unwrap()),
    ];
    let rt = runtime();
    let socket = rt.block_on(async {
        let endpoint = child_endpoint().bind().await.unwrap();
        let socket = announce(&endpoint);
        let mut commands = stdin_commands();
        let served = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        loop {
            tokio::select! {
                incoming = endpoint.accept() => {
                    let Some(incoming) = incoming else { break };
                    let (responses, served) = (responses.clone(), served.clone());
                    keep(async move {
                        let Ok(connection) = incoming.await else { return };
                        assert_eq!(connection.alpn(), Channel::NodeControl.alpn());
                        let Ok((mut send, mut recv)) = connection.accept_bi().await else { return };
                        let Ok(request) = recv.read_to_end(ARTIFACT_RESPONSE_CAP).await else { return };
                        let n = served.fetch_add(1, Ordering::SeqCst);
                        line(format!("L5_CONTROL {n} {}", hex(&request)));
                        send.write_all(&responses[n % 2]).await.unwrap();
                        send.finish().unwrap();
                        // The caller closes the connection (code 7) once it has the response.
                        connection.closed().await;
                    });
                }
                command = commands.recv() => {
                    if command.as_deref() == Some("quit") || command.is_none() { break; }
                }
            }
        }
        reap_kept().await;
        endpoint.close().await;
        socket
    });
    finish_child(rt, socket);
}

#[test]
fn l5_child_fixture() {
    let Ok(mode) = std::env::var("L5_POOLED_CHILD") else {
        return;
    };
    match mode.as_str() {
        "acceptor" => acceptor_child(),
        "sender" => sender_child(),
        "artifact_client" => artifact_client_child(),
        "control_acceptor" => control_acceptor_child(),
        other => panic!("unknown child mode {other}"),
    }
}

// --------------------------------------------------------------- parent side
struct Peer {
    child: Child,
    rx: UnboundedReceiver<String>,
    log: Vec<String>,
    reader: Option<std::thread::JoinHandle<()>>,
    address: EndpointAddr,
    socket: SocketAddr,
    reaped: bool,
}
impl Peer {
    fn spawn(mode: &str, env: &[(&str, String)]) -> Self {
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args(["--exact", "l5_child_fixture", "--nocapture"])
            .env("L5_POOLED_CHILD", mode)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        for (key, value) in env {
            command.env(key, value);
        }
        node::process::confine_descriptors(&mut command).unwrap();
        let mut child = {
            let _boundary = fixture_spawn_boundary();
            command.spawn().unwrap()
        };
        let output = child.stdout.take().unwrap();
        let (tx, rx) = unbounded_channel();
        let reader = std::thread::spawn(move || {
            for line in BufReader::new(output).lines() {
                let Ok(line) = line else { return };
                if tx.send(line).is_err() {
                    return;
                }
            }
        });
        let mut peer = Self {
            child,
            rx,
            log: Vec::new(),
            reader: Some(reader),
            address: EndpointAddr::new(iroh::SecretKey::from_bytes(&[7; 32]).public()),
            socket: SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0).into(),
            reaped: false,
        };
        let end = Instant::now() + wait();
        let ready = loop {
            match peer.rx.try_recv() {
                Ok(line) => {
                    if let Some(rest) = line.strip_prefix("L5_READY ") {
                        break rest.to_owned();
                    }
                    peer.log.push(line);
                }
                Err(_) => {
                    assert!(Instant::now() < end, "child readiness deadline");
                    std::thread::sleep(pause(10));
                }
            }
        };
        let value: serde_json::Value = serde_json::from_str(&ready).unwrap();
        assert_eq!(value["pid"].as_u64().unwrap(), u64::from(peer.child.id()));
        assert_ne!(peer.child.id(), std::process::id());
        peer.socket = value["socket"].as_str().unwrap().parse().unwrap();
        assert!(peer.socket.ip().is_loopback());
        let peer_id: EndpointId = value["id"].as_str().unwrap().parse().unwrap();
        peer.address = EndpointAddr::new(peer_id).with_ip_addr(peer.socket);
        peer
    }
    fn command(&mut self, text: &str) {
        let stdin = self.child.stdin.as_mut().unwrap();
        writeln!(stdin, "{text}").unwrap();
        stdin.flush().unwrap();
    }
    async fn expect(&mut self, prefix: &str) -> String {
        let end = tokio::time::Instant::now() + wait();
        loop {
            let line = tokio::time::timeout_at(end, self.rx.recv())
                .await
                .unwrap_or_else(|_| panic!("no `{prefix}` line\nlog: {:#?}", self.log))
                .expect("child output closed");
            if let Some(rest) = line.strip_prefix(prefix) {
                let rest = rest.to_owned();
                self.log.push(line);
                return rest;
            }
            self.log.push(line);
        }
    }
    fn finish(&mut self, rt: &tokio::runtime::Runtime) {
        self.command("quit");
        rt.block_on(self.expect("L5_CLOSED "));
        let status = self.child.wait().unwrap();
        self.reaped = true;
        self.reader.take().unwrap().join().unwrap();
        assert!(status.success(), "child failed: {status}");
        assert_released(self.socket, "child UDP socket leaked");
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

struct Parent<'a> {
    host: Option<LoopbackHost<'a>>,
    router: Option<PooledRouter<Mono>>,
    clock: Arc<Mono>,
    sockets: Vec<SocketAddr>,
}
impl<'a> Parent<'a> {
    fn new(rt: &'a tokio::runtime::Runtime) -> Self {
        Self::with_services(rt, &[Channel::Router], |_| Services::default())
    }
    fn with_services(
        rt: &'a tokio::runtime::Runtime,
        channels: &[Channel],
        services: impl FnOnce(&EndpointAddr) -> Services,
    ) -> Self {
        let clock = Arc::new(Mono(Instant::now()));
        let host = {
            let _boundary = fixture_spawn_boundary();
            LoopbackHost::bind_protocols(
                rt,
                &*clock,
                clock.monotonic().checked_add(ms(10_000)).unwrap(),
                iroh::SecretKey::from_bytes(&[41; 32]),
                channels,
            )
            .unwrap()
        };
        let sockets = host.address().unwrap().ip_addrs().copied().collect();
        let services = services(&host.address().unwrap());
        let router = PooledRouter::start_with(&host, clock.clone(), driver(), services).unwrap();
        Self {
            host: Some(host),
            router: Some(router),
            clock,
            sockets,
        }
    }
    fn router(&self) -> &PooledRouter<Mono> {
        self.router.as_ref().unwrap()
    }
    fn address(&self) -> EndpointAddr {
        self.host.as_ref().unwrap().address().unwrap()
    }
    fn teardown(mut self, rt: &tokio::runtime::Runtime) -> Vec<SocketAddr> {
        let router = self.router.take().unwrap();
        rt.block_on(router.shutdown());
        drop(router);
        let mut host = self.host.take().unwrap();
        host.close(
            &*self.clock,
            self.clock.monotonic().checked_add(ms(10_000)).unwrap(),
        )
        .unwrap();
        drop(host);
        self.sockets.clone()
    }
}

#[test]
fn every_router_family_leaves_the_pool_as_the_exact_transfer_frame_and_is_acked_by_a_peer_process()
{
    let families = families();
    let rt = runtime();
    let parent = Parent::new(&rt);
    let mut peer = Peer::spawn("acceptor", &[]);
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), GENERATION)])
        .unwrap();
    rt.block_on(async {
        for (n, (name, frame)) in families.iter().enumerate() {
            let n = n + 1;
            let outcome = parent.router().enqueue(EnqueueRequest {
                message_id: id(n),
                digest: runtime::iroh_pool::frame_digest(frame),
                payload: frame.clone(),
                expected_generation: None,
                source_generation: None,
                destination: None,
            });
            let EnqueueOutcome::Queued(handle) = outcome else {
                panic!("{name}: {outcome:?}")
            };
            let terminal = tokio::time::timeout(wait(), parent.router().wait_terminal(handle))
                .await
                .expect("terminal within wait");
            assert_eq!(terminal, OutboundTerminal::Delivered, "{name}");
            let rest = peer.expect(&format!("L5_STREAM {} ", hex(&id(n)))).await;
            let transfer = [GENERATION.to_be_bytes().as_slice(), frame].concat();
            let want = remote::encode_remote_frame(RemoteKind::Transfer, id(n), &transfer).unwrap();
            assert_eq!(rest, hex(&want), "{name}: outer stream bytes");
        }
    });
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    sockets
        .iter()
        .for_each(|s| assert_released(*s, "parent socket leaked"));
}

#[test]
fn every_router_family_sent_by_a_peer_process_is_delivered_unchanged_and_acked_with_the_exact_frame()
 {
    let families = families();
    let rt = runtime();
    let parent = Parent::new(&rt);
    let address = parent.address();
    let mut peer = Peer::spawn(
        "sender",
        &[
            ("L5_PARENT_ID", address.id.to_string()),
            (
                "L5_PARENT_SOCKET",
                address.ip_addrs().next().unwrap().to_string(),
            ),
        ],
    );
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), GENERATION)])
        .unwrap();
    rt.block_on(async {
        peer.command("connect");
        peer.expect("L5_CONNECTED").await;
        for (n, (name, frame)) in families.iter().enumerate() {
            let n = n + 1;
            peer.command(&format!("send {n} {}", hex(frame)));
            let delivery = parent
                .router()
                .receive(1, ms(20_000))
                .await
                .unwrap_or_else(|| panic!("{name}: nothing delivered"));
            assert_eq!(delivery.message_id, id(n), "{name}");
            assert_eq!(delivery.generation, GENERATION, "{name}");
            assert_eq!(&delivery.payload, frame, "{name}: payload bytes");
            assert_eq!(
                delivery.source_endpoint,
                Some(*peer.address.id.as_bytes()),
                "{name}"
            );
            assert!(parent.router().ack(1, id(n)), "{name}");
            let reply = peer.expect(&format!("L5_RESP {n} ")).await;
            let ack = remote::encode_remote_frame(RemoteKind::Ack, id(n), b"").unwrap();
            assert_eq!(reply, hex(&ack), "{name}: reply bytes");
        }
    });
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    sockets
        .iter()
        .for_each(|s| assert_released(*s, "parent socket leaked"));
}

// ------------------------------------------------------------- artifact family
fn artifact_fixture() -> serde_json::Value {
    serde_json::from_str(include_str!("../../protocol/fixtures/artifact_grant.json")).unwrap()
}
fn artifact_now() -> u64 {
    artifact_fixture()["now_unix_ms"].as_u64().unwrap()
}
fn grant_limits() -> ag::GrantLimits {
    let span = |id: B| id.span(Entries::ZERO).unwrap().millis();
    ag::GrantLimits {
        max_grant_ttl_ms: span(B::PyMyceliumMobileArtifactGrantMaxGrantTtlMsL38),
        max_request_ttl_ms: span(B::PyMyceliumMobileArtifactGrantMaxRequestTtlMsL39),
        request_ttl_ms: span(B::PyFinalMyceliumMobileArtifactGrantTtlMsL309C18),
        clock_skew_ms: span(B::PyMyceliumMobileArtifactGrantClockSkewMsL40),
    }
}
fn artifact_wire() -> ArtifactWireLimits {
    // Input-derived test cap (the shipped native defaults have no L1 id yet).
    let cap = include_str!("../../protocol/fixtures/artifact_grant.json").len() as u64;
    ArtifactWireLimits {
        request_bytes: budgets::Bytes::new(cap),
        header_bytes: budgets::Bytes::new(cap),
        io: B::RouterClose.span(Entries::ZERO).unwrap(),
    }
}
const ARTIFACT_DATA: &[u8] = b"real artifact bytes: \0\xff\x01, not a Router frame";
fn seed() -> Identity {
    Identity::from_secret("seed-endpoint-1", &[0x22; 32]).unwrap()
}
/// In-memory content store: one object, a replay fence, no disk.
struct Objects {
    fence: std::sync::Mutex<std::collections::BTreeSet<String>>,
}
impl ArtifactObjects for Objects {
    fn verified_size(&self, digest: &str) -> Option<u64> {
        (ag::range_digest(ARTIFACT_DATA) == digest).then_some(ARTIFACT_DATA.len() as u64)
    }
    fn admit_and_read(
        &self,
        grant: &ag::Grant,
        request: &ag::ChunkRequest,
        nonce: &str,
        _expiry: u64,
        _now: u64,
    ) -> Result<AdmittedChunk, ArtifactError> {
        let fence = format!("{}\0{}\0{nonce}", grant.grant_id, request.request_id);
        if !self.fence.lock().unwrap().insert(fence) {
            return Err(ArtifactError("artifact_chunk_request_replay".into()));
        }
        let (from, len) = (request.offset_bytes as usize, request.length_bytes as usize);
        Ok(AdmittedChunk {
            bytes: ARTIFACT_DATA[from..from + len].to_vec(),
            pace: budgets::Span::ZERO,
        })
    }
}
fn artifact_service(source: Identity) -> Services {
    let span = B::RouterClose.span(Entries::ZERO).unwrap();
    let service = ArtifactService::new(
        source,
        seed().public_key_record(),
        grant_limits(),
        Arc::new(Objects {
            fence: Default::default(),
        }),
        ArtifactServeLimits {
            wire: artifact_wire(),
            connections: Entries::new(2),
            per_peer: Entries::new(2),
            blocking: Entries::new(2),
            read: span,
            idle: span,
            maximum_pace: span,
        },
        Arc::new(artifact_now),
    )
    .unwrap();
    Services {
        artifact: Some(Arc::new(service)),
        ..Services::default()
    }
}
fn oracle_code(vectors: &serde_json::Value, kind: &str, name: &str) -> String {
    let case = vectors["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["kind"] == kind && c["name"] == name)
        .unwrap_or_else(|| panic!("{kind} {name}"));
    assert_eq!(case["oracle"]["ok"], false, "{kind} {name}");
    case["oracle"]["error"].as_str().unwrap().to_owned()
}

#[test]
fn artifact_chunk_frames_cross_the_pooled_host_byte_exact_and_refusals_carry_the_oracle_codes() {
    let vectors: serde_json::Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/round6-artifact-grant-vectors.json"
    ))
    .unwrap();
    let rt = runtime();
    let source_secret = [0x33; 32];
    let parent =
        Parent::with_services(&rt, &[Channel::Router, Channel::ArtifactChunk], |address| {
            artifact_service(
                Identity::from_secret(&address.id.to_string(), &source_secret).unwrap(),
            )
        });
    let address = parent.address();
    let source = Identity::from_secret(&address.id.to_string(), &source_secret).unwrap();
    let mut peer = Peer::spawn(
        "artifact_client",
        &[
            ("L5_PARENT_ID", address.id.to_string()),
            (
                "L5_PARENT_SOCKET",
                address.ip_addrs().next().unwrap().to_string(),
            ),
        ],
    );
    let member = Identity::from_secret(&peer.address.id.to_string(), &[0x11; 32]).unwrap();
    let object = ag::range_digest(ARTIFACT_DATA);
    let mut message = artifact_fixture()["grant"]["message"].clone();
    message["recipient_endpoint_id"] = member.endpoint_id().into();
    message["recipient_key_digest"] = member.verification_key_digest().into();
    message["authorized_sources"] = serde_json::json!([{"endpoint_id": source.endpoint_id(),
        "verification_key_digest": source.verification_key_digest(), "direct_addrs": []}]);
    message["allowed_object_digests"] = serde_json::json!([object]);
    let envelope = serde_json::json!({"protocol": protocol::membership::SIGNED_MESSAGE_PROTOCOL,
        "signature": seed().sign(&message).unwrap(), "message": message, "verification_key": seed().public_key_record()});
    let grant = ag::verify_grant(
        &envelope,
        &seed().public_key_record(),
        artifact_now(),
        grant_limits(),
    )
    .unwrap();
    let length = ARTIFACT_DATA.len() as u64;
    let signed = |id: &str, digest: &str, offset: u64, len: u64| {
        ag::sign_chunk_request(
            &member,
            &grant,
            id,
            id,
            source.endpoint_id(),
            digest,
            offset,
            len,
            artifact_now(),
            grant_limits(),
        )
        .unwrap()
    };
    let wire = |v: &serde_json::Value| protocol::canonical::to_bytes(v).unwrap();
    rt.block_on(async {
        peer.command("connect");
        peer.expect("L5_CONNECTED").await;
        // 1. a granted chunk: exact response frame = u32-BE header + receipt-bound bytes.
        let request = signed("chunk-1", &object, 0, length);
        peer.command(&format!("fetch 1 {}", hex(&wire(&request))));
        let got = peer.expect("L5_RESP 1 ").await;
        let checked = ag::ChunkRequest {
            request_id: "chunk-1".into(),
            source_endpoint_id: source.endpoint_id().into(),
            object_digest: object.clone(),
            offset_bytes: 0,
            length_bytes: length,
        };
        let receipt = ag::sign_chunk_receipt(&source, &checked, &grant, ARTIFACT_DATA, artifact_now()).unwrap();
        let header = serde_json::json!({"protocol": RESPONSE_PROTOCOL, "status": "ok", "receipt": receipt});
        let want = frame_response(&header, ARTIFACT_DATA, artifact_wire().header_bytes).unwrap();
        assert_eq!(got, hex(&want), "granted chunk frame");
        // 2. refusals: the wire code is the one the executed v0.1.4 verifier produced.
        let refused = |code: &str| {
            let header = serde_json::json!({"protocol": RESPONSE_PROTOCOL, "status": "refused", "code": code});
            hex(&frame_response(&header, b"", artifact_wire().header_bytes).unwrap())
        };
        let mut tampered = signed("chunk-2", &object, 0, length);
        tampered["message"]["request_id"] = "tampered".into();
        peer.command(&format!("fetch 2 {}", hex(&wire(&tampered))));
        assert_eq!(
            peer.expect("L5_RESP 2 ").await,
            refused(&oracle_code(&vectors, "request", "envelope:message_tampered")),
            "tampered request"
        );
        let missing = signed("chunk-3", &format!("sha256:{}", "0".repeat(64)), 0, length);
        peer.command(&format!("fetch 3 {}", hex(&wire(&missing))));
        assert_eq!(
            peer.expect("L5_RESP 3 ").await,
            refused(&oracle_code(&vectors, "request", "object_not_in_grant")),
            "object outside the grant"
        );
        let beyond = signed("chunk-4", &object, 1, length);
        peer.command(&format!("fetch 4 {}", hex(&wire(&beyond))));
        assert_eq!(
            peer.expect("L5_RESP 4 ").await,
            refused(&oracle_code(&vectors, "request", "ctx:object_too_small")),
            "range beyond the object"
        );
    });
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    sockets
        .iter()
        .for_each(|s| assert_released(*s, "parent socket leaked"));
}

// ------------------------------------------------------------ NodeControl family
#[test]
fn every_native_command_crosses_the_control_carrier_and_its_response_returns_byte_exact() {
    let fixture: Vec<serde_json::Value> = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continuation-native-vectors.json"
    ))
    .unwrap();
    let canonical = |kind: &str, name: Option<&str>| -> Vec<(String, Vec<u8>)> {
        fixture
            .iter()
            .filter(|r| r["kind"] == kind && name.is_none_or(|n| r["name"] == n))
            .map(|r| {
                (
                    r["name"].as_str().unwrap().to_owned(),
                    r["canonical_utf8"].as_str().unwrap().as_bytes().to_vec(),
                )
            })
            .collect()
    };
    let commands = canonical("native_command", None);
    assert_eq!(commands.len(), 27);
    for row in fixture.iter().filter(|r| r["kind"] == "native_command") {
        // The oracle-recorded commands are exactly what the L5 grammar accepts.
        let value: serde_json::Value =
            serde_json::from_slice(row["canonical_utf8"].as_str().unwrap().as_bytes()).unwrap();
        protocol::native::validate_command(&value).unwrap();
    }
    let first = canonical("native_response", Some("completed")).remove(0).1;
    let second = canonical("native_response", Some("failed")).remove(0).1;
    let rt = runtime();
    let parent = Parent::with_services(&rt, &[Channel::Router, Channel::NodeControl], |_| {
        Services::default()
    });
    let mut peer = Peer::spawn(
        "control_acceptor",
        &[
            ("L5_CONTROL_FIRST", hex(&first)),
            ("L5_CONTROL_SECOND", hex(&second)),
        ],
    );
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), GENERATION)])
        .unwrap();
    parent
        .router()
        .enable_control_carrier(runtime::iroh_pool::ControlCarrierLimits {
            concurrency: 1,
            max_response_bytes: ARTIFACT_RESPONSE_CAP,
        });
    rt.block_on(async {
        for (n, (name, command)) in commands.iter().enumerate() {
            let response = parent
                .router()
                .control_call(*peer.address.id.as_bytes(), wait(), command.clone())
                .await
                .unwrap_or_else(|code| panic!("{name}: {code}"));
            let want = if n % 2 == 0 { &first } else { &second };
            assert_eq!(&response, want, "{name}: response bytes");
            let seen = peer.expect(&format!("L5_CONTROL {n} ")).await;
            assert_eq!(seen, hex(command), "{name}: request bytes");
        }
    });
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    sockets
        .iter()
        .for_each(|s| assert_released(*s, "parent socket leaked"));
}
