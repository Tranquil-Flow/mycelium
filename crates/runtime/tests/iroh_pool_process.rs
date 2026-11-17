// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NET-09 (real two-process QUIC carrier: fences, wrong ALPN, backpressure and cancel_ack, artifact and control ALPNs); B-NET-21 (rotation delivers no stale frame, cancel, bounded connect).
//!
//! Two-process QUIC tests for the pooled Router driver (`runtime::iroh_pool`).
//!
//! The parent test process hosts a real `PooledRouter` on a loopback-only endpoint
//! (direct IPv4 addresses, no relay, no address lookup). An exact-owned child
//! process of this same test binary is the wire peer: a scripted raw acceptor
//! (observes streams, connections, close codes and stream-stop codes), a scripted
//! raw sender (observes the replies and resets the receive side produces), or a
//! mute endpoint that never accepts. Bounds are explicit per test (native L1 IDs
//! for the sidecar bounds do not exist yet); the shipped values are 5 s connect and
//! 60 s stream, and the 60-s dispatch-wait reset is exercised with the same code
//! path at a caller-supplied 1.5 s. Source: `sidecar.rs @ f4e366e9`.
#![cfg(feature = "iroh-adapter")]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant, Span};
use iroh::{
    Endpoint, EndpointAddr, EndpointId, RelayMode,
    endpoint::{ConnectionError, ReadError, ReadToEndError, SendStream, presets},
};
use protocol::{
    remote::{self, RemoteKind},
    router_wire,
};
use runtime::{
    iroh_host::LoopbackHost,
    iroh_pool::{ConnectionService, DriverLimits, EventSink, PooledRouter, Services},
    sidecar_pool::{EnqueueOutcome, EnqueueRequest, OutboundHandle, OutboundTerminal, PoolLimits},
    sidecar_telemetry::TransportEvent,
};
use std::{
    io::{BufRead, BufReader, Write},
    net::{Ipv4Addr, SocketAddr, SocketAddrV4, UdpSocket},
    process::{Child, Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};
use tokio::sync::mpsc::{UnboundedReceiver, unbounded_channel};
use transport::{Channel, Clock};

include!("support/fork_lock.rs");

/// Scenario gate. Every scenario owns a loopback endpoint, a child process and
/// several runtimes; with all 22 in parallel on a loaded host (the lanes run at
/// `nice 19`) each is starved of CPU and a registry-bounded wall-clock wait expires
/// although the product is correct (reproduced: 3 of 22 failed in one full run at
/// load 32, each passes alone). The gate bounds how many run at once; it adds no
/// time to any wait (a scenario's own bounds start when it starts).
const SCENARIOS_AT_ONCE: usize = 3;
struct Gate {
    running: std::sync::Mutex<usize>,
    changed: std::sync::Condvar,
    limit: usize,
}
struct Entered<'a>(&'a Gate);
impl Gate {
    const fn new(limit: usize) -> Self {
        Self {
            running: std::sync::Mutex::new(0),
            changed: std::sync::Condvar::new(),
            limit,
        }
    }
    fn enter(&self) -> Entered<'_> {
        let mut running = self
            .running
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        while *running >= self.limit {
            running = self
                .changed
                .wait(running)
                .unwrap_or_else(std::sync::PoisonError::into_inner);
        }
        *running += 1;
        Entered(self)
    }
}
impl Drop for Entered<'_> {
    fn drop(&mut self) {
        *self
            .0
            .running
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) -= 1;
        self.0.changed.notify_one();
    }
}
static GATE: Gate = Gate::new(SCENARIOS_AT_ONCE);
fn scenario() -> Entered<'static> {
    GATE.enter()
}
/// The ALPN verdict line for `name` among the log entries after the first `seen`
/// (a dial's own verdict, never an earlier dial's).
fn fresh_verdict(log: &[String], seen: usize, name: &str) -> Option<String> {
    for (prefix, label) in [
        ("L9_ALPN_CONNECTED ", "CONNECTED "),
        ("L9_ALPN_FAILED ", "FAILED "),
    ] {
        if let Some(found) = log[seen.min(log.len())..]
            .iter()
            .filter_map(|line| line.strip_prefix(prefix))
            .rfind(|line| line.starts_with(name))
        {
            return Some(format!("{label}{found}"));
        }
    }
    None
}

/// Every wait for a child line or a terminal is bounded by twice the registry
/// close budget; scenario pauses below are explicit scenario parameters.
fn slice() -> Duration {
    Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis() * 2)
}
fn pause(units: u64) -> Duration {
    Duration::from_millis(units)
}

/// Child-side task registry: spawned tasks are retained and reaped at exit.
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

struct Mono(Instant);
impl Clock for Mono {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(ClockDomain(31), self.0.elapsed().as_millis() as u64)
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
fn limits(
    initial: u64,
    max: u64,
    lifetime: u64,
    connect: u64,
    stream_io: u64,
    capacity: usize,
) -> DriverLimits {
    DriverLimits {
        pool: PoolLimits {
            capacity,
            seen_factor: 8,
            seen_floor: 64,
            initial_retry: ms(initial),
            max_retry: ms(max),
            lifetime: ms(lifetime),
        },
        connect: ms(connect),
        stream_io: ms(stream_io),
    }
}
fn id(n: u8) -> [u8; 16] {
    [n; 16]
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
/// Two distinct accepted L5 inner Router frames (oracle-recorded vectors).
fn valid_frames() -> (Vec<u8>, Vec<u8>) {
    let fixture: serde_json::Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continued-wire-vectors.json"
    ))
    .unwrap();
    let frame = |i: usize| unhex(fixture[i]["encoded_hex"].as_str().unwrap());
    let (first, second) = (frame(0), frame(1));
    assert_ne!(first, second);
    assert!(router_wire::decode_frame(&first).is_ok());
    assert!(router_wire::decode_frame(&second).is_ok());
    (first, second)
}
fn deadline(clock: &Mono) -> MonoInstant {
    clock.monotonic().checked_add(ms(10_000)).unwrap()
}

/// Inet socket names (`127.0.0.1:62768`, `*:62768`, `a:1->b:2`) in `pid`'s OWN descriptor
/// table, read from the kernel (`lsof -p`). Unlike a rebind probe this cannot be fooled by an
/// unrelated process that takes a freed ephemeral port on this shared host (a measured false
/// positive: `r9-leak-foreign-squatter-experiment.txt`, and the round-6 finding).
fn inet_socket_names(pid: u32) -> Vec<String> {
    let output = std::process::Command::new("/usr/sbin/lsof")
        .args(["-nP", "-a", "-i", "-Fn", "-p", &pid.to_string()])
        .output()
        .expect("lsof runs");
    // lsof exits 1 with no output when the pid holds no inet descriptor.
    assert!(output.status.success() || output.stdout.is_empty());
    String::from_utf8(output.stdout)
        .unwrap()
        .lines()
        .filter_map(|line| line.strip_prefix('n').map(str::to_owned))
        .collect()
}
/// The local end of an lsof socket name (`a:1->b:2` -> `a:1`).
fn local_end(name: &str) -> &str {
    name.split("->").next().unwrap_or(name)
}
fn holds_port(pid: u32, port: u16) -> bool {
    inet_socket_names(pid)
        .iter()
        .any(|name| local_end(name).rsplit(':').next() == Some(&port.to_string()))
}
/// This process no longer holds any of `sockets`: its own descriptor table says so (one
/// `lsof` call, however many sockets).
fn assert_all_released(sockets: &[SocketAddr], what: &str) {
    let held = inet_socket_names(std::process::id());
    for socket in sockets {
        assert!(
            !held
                .iter()
                .any(|name| local_end(name).rsplit(':').next() == Some(&socket.port().to_string())),
            "{what}: this process still holds {socket}: {held:?}"
        );
    }
}
fn assert_released(socket: SocketAddr, what: &str) {
    assert_all_released(&[socket], what);
}

/// Exact checks over the driver's own attempt records (its injected clock), shared by the
/// timing tests: no wall-clock sleep or child-side timestamp is involved, so host load can
/// only lengthen waits, never fail these relations.
fn assert_decisions(decisions: &[runtime::iroh_pool::AttemptDecision], backoffs: &[u64]) {
    assert!(!decisions.is_empty(), "no attempt was recorded");
    let deadline = decisions[0].deadline;
    for decision in decisions {
        assert_eq!(decision.deadline, deadline, "a retry renewed the lifetime");
        assert!(
            decision.deadline.remaining(decision.decided_at).is_ok(),
            "an attempt was decided at or after the enqueue-time deadline: {decisions:?}"
        );
    }
    // The planned waits are exactly the pool's doubling schedule (clamped at the max).
    let planned: Vec<Option<u64>> = decisions[1..]
        .iter()
        .map(|d| d.backoff_before.map(Span::millis))
        .collect();
    let expected: Vec<Option<u64>> = (0..planned.len())
        .map(|i| Some(*backoffs.get(i).or(backoffs.last()).unwrap()))
        .collect();
    assert_eq!(planned, expected, "planned backoffs: {decisions:?}");
    // A timer never fires early: each decision follows the previous by at least its wait.
    for pair in decisions.windows(2) {
        let gap = pair[1]
            .decided_at
            .elapsed_since(pair[0].decided_at)
            .unwrap()
            .millis();
        assert!(
            gap >= pair[1].backoff_before.unwrap().millis(),
            "decided {gap} ms after the previous attempt: {decisions:?}"
        );
    }
}

/// Parent side: host + driver, torn down in the order the driver requires.
struct Parent<'a> {
    host: Option<LoopbackHost<'a>>,
    router: Option<PooledRouter<Mono>>,
    clock: Arc<Mono>,
    sockets: Vec<SocketAddr>,
}
impl<'a> Parent<'a> {
    fn new(rt: &'a tokio::runtime::Runtime, driver: DriverLimits) -> Self {
        Self::with(rt, driver, &[Channel::Router], Services::default())
    }
    fn with(
        rt: &'a tokio::runtime::Runtime,
        driver: DriverLimits,
        channels: &[Channel],
        services: Services,
    ) -> Self {
        let clock = Arc::new(Mono(Instant::now()));
        let host = {
            let _boundary = fixture_spawn_boundary();
            LoopbackHost::bind_protocols(
                rt,
                &*clock,
                deadline(&clock),
                iroh::SecretKey::from_bytes(&[41; 32]),
                channels,
            )
            .unwrap()
        };
        let sockets = host.address().unwrap().ip_addrs().copied().collect();
        let router = PooledRouter::start_with(&host, clock.clone(), driver, services).unwrap();
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
    /// Shut the driver down (inside the runtime), drop it, then close the host.
    fn teardown(mut self, rt: &tokio::runtime::Runtime) -> Vec<SocketAddr> {
        let router = self.router.take().unwrap();
        rt.block_on(router.shutdown());
        drop(router);
        let mut host = self.host.take().unwrap();
        host.close(&*self.clock, deadline(&self.clock)).unwrap();
        drop(host);
        self.sockets.clone()
    }
}

struct Peer {
    child: Child,
    rx: UnboundedReceiver<String>,
    log: Vec<String>,
    reader: Option<JoinHandle<()>>,
    address: EndpointAddr,
    socket: SocketAddr,
    reaped: bool,
}
impl Peer {
    fn spawn(mode: &str, env: &[(&str, String)]) -> Self {
        let peer = Self::spawn_with(mode, env, true);
        // Exact and host-independent: the fixture's own descriptor table holds its one
        // announced socket and nothing inherited from this process.
        assert_eq!(
            peer.foreign_sockets(),
            Vec::<String>::new(),
            "child inherited sockets"
        );
        peer
    }
    /// `confined == false` is the bare `Command::spawn` exposure (used only to prove the
    /// descriptor-table check is not vacuous).
    fn spawn_with(mode: &str, env: &[(&str, String)], confined: bool) -> Self {
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args(["--exact", "pool_child_fixture", "--nocapture"])
            .env("L9_POOL_CHILD", mode)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit());
        for (key, value) in env {
            command.env(key, value);
        }
        // The canonical boundary (L6) plus the S23 ordering lock around the fork.
        if confined {
            node::process::confine_descriptors(&mut command).unwrap();
        }
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
        let end = Instant::now() + slice();
        let ready = loop {
            match peer.rx.try_recv() {
                Ok(line) => {
                    if let Some(rest) = line.strip_prefix("L9_READY ") {
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
    /// Inet sockets in the live child's descriptor table other than its own announced one.
    fn foreign_sockets(&self) -> Vec<String> {
        let own = self.socket.to_string();
        inet_socket_names(self.child.id())
            .into_iter()
            .filter(|name| local_end(name) != own)
            .collect()
    }
    fn command(&mut self, line: &str) {
        let stdin = self.child.stdin.as_mut().unwrap();
        writeln!(stdin, "{line}").unwrap();
        stdin.flush().unwrap();
    }
    /// Awaits (driving QUIC on the caller's runtime) the next line with `prefix`.
    async fn expect(&mut self, prefix: &str) -> String {
        self.expect_within(prefix, slice()).await
    }
    async fn expect_within(&mut self, prefix: &str, within: Duration) -> String {
        let end = tokio::time::Instant::now() + within;
        loop {
            let line = tokio::time::timeout_at(end, self.rx.recv())
                .await
                .unwrap_or_else(|_| {
                    panic!("no `{prefix}` line within {within:?}\nlog: {:#?}", self.log)
                })
                .expect("child output closed");
            if let Some(rest) = line.strip_prefix(prefix) {
                let rest = rest.to_owned();
                self.log.push(line);
                return rest;
            }
            self.log.push(line);
        }
    }
    /// Pump the runtime for `span` and collect whatever the child printed.
    async fn settle(&mut self, span: Duration) {
        let end = tokio::time::Instant::now() + span;
        while let Ok(Some(line)) = tokio::time::timeout_at(end, self.rx.recv()).await {
            self.log.push(line);
        }
    }
    fn lines(&self, prefix: &str) -> Vec<String> {
        self.log
            .iter()
            .filter_map(|l| l.strip_prefix(prefix).map(str::to_owned))
            .collect()
    }
    /// `L9_STREAM conn idx ms id action` records.
    fn streams(&self) -> Vec<(u32, u64, String, String)> {
        self.lines("L9_STREAM ")
            .iter()
            .map(|l| {
                let f: Vec<&str> = l.split(' ').collect();
                (
                    f[0].parse().unwrap(),
                    f[2].parse().unwrap(),
                    f[3].to_owned(),
                    f[4].to_owned(),
                )
            })
            .collect()
    }
    fn finish(&mut self, rt: &tokio::runtime::Runtime) {
        self.command("quit");
        rt.block_on(self.expect("L9_CLOSED "));
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

// ---------------------------------------------------------------------------
// Child fixtures (this same test binary, selected by L9_POOL_CHILD).
// ---------------------------------------------------------------------------
fn child_endpoint_builder() -> iroh::endpoint::Builder {
    Endpoint::builder(presets::Minimal)
        .portmapper_config(iroh::endpoint::PortmapperConfig::Disabled)
        .relay_mode(RelayMode::Disabled)
        .clear_address_lookup()
        .clear_ip_transports()
        .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
        .unwrap()
        .alpns(vec![Channel::Router.alpn().to_vec()])
}
fn announce(endpoint: &Endpoint) -> SocketAddr {
    let sockets = endpoint.bound_sockets();
    assert_eq!(sockets.len(), 1);
    println!(
        "L9_READY {}",
        serde_json::json!({"id":endpoint.id().to_string(),"socket":sockets[0].to_string(),"pid":std::process::id()})
    );
    std::io::stdout().flush().unwrap();
    sockets[0]
}
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
    // The reader blocks on stdin until the parent closes it at child exit, so it is
    // retained here and ends with the process rather than being joined mid-run.
    STDIN_READER
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .push(reader);
    rx
}
static STDIN_READER: std::sync::Mutex<Vec<std::thread::JoinHandle<()>>> =
    std::sync::Mutex::new(Vec::new());
fn line(text: String) {
    println!("{text}");
    std::io::stdout().flush().unwrap();
}
fn close_text(error: &ConnectionError) -> String {
    match error {
        ConnectionError::ApplicationClosed(close) => format!(
            "code={} reason={}",
            close.error_code.into_inner(),
            String::from_utf8_lossy(&close.reason)
        ),
        other => format!("other={other}"),
    }
}
fn finish_child(rt: tokio::runtime::Runtime, socket: SocketAddr) {
    drop(rt);
    assert!(
        !holds_port(std::process::id(), socket.port()),
        "child endpoint socket retained"
    );
    println!("L9_CLOSED true");
}

/// Scripted raw acceptor: one action per accepted stream (last repeats).
fn acceptor_child() {
    let script: Vec<String> = std::env::var("L9_SCRIPT")
        .unwrap()
        .split(',')
        .map(str::to_owned)
        .collect();
    let rt = runtime();
    let socket = rt.block_on(async {
        let endpoint = child_endpoint_builder().bind().await.unwrap();
        let socket = announce(&endpoint);
        let start = Instant::now();
        let streams = Arc::new(AtomicUsize::new(0));
        let mut connections = 0_u32;
        let mut commands = stdin_commands();
        loop {
            tokio::select! {
                incoming = endpoint.accept() => {
                    let Some(incoming) = incoming else { break };
                    connections += 1;
                    let number = connections;
                    let (script, streams) = (script.clone(), streams.clone());
                    keep(async move {
                        let Ok(connection) = incoming.await else { return };
                        line(format!("L9_CONN {number}"));
                        let watcher = connection.clone();
                        keep(async move {
                            let error = watcher.closed().await;
                            line(format!("L9_CONN_CLOSED {number} {}", close_text(&error)));
                        });
                        while let Ok((mut send, mut recv)) = connection.accept_bi().await {
                            let Ok(bytes) = recv.read_to_end(remote::REMOTE_MAX_FRAME_BYTES).await else { continue };
                            let frame = remote::decode_remote_frame(&bytes).unwrap();
                            assert_eq!(frame.kind, RemoteKind::Transfer);
                            let index = streams.fetch_add(1, Ordering::SeqCst);
                            let action = script[index.min(script.len() - 1)].clone();
                            let name = hex(&frame.message_id);
                            line(format!(
                                "L9_STREAM {number} {index} {} {name} {action}",
                                start.elapsed().as_millis()
                            ));
                            match action.as_str() {
                                "ack" => {
                                    let r = remote::encode_remote_frame(RemoteKind::Ack, frame.message_id, b"").unwrap();
                                    send.write_all(&r).await.unwrap();
                                    send.finish().unwrap();
                                }
                                "queue_full" => {
                                    let r = remote::encode_remote_frame(RemoteKind::Error, frame.message_id, b"queue_full").unwrap();
                                    send.write_all(&r).await.unwrap();
                                    send.finish().unwrap();
                                }
                                "garbage" => {
                                    send.write_all(b"xx").await.unwrap();
                                    send.finish().unwrap();
                                }
                                "hold" => {
                                    keep(async move {
                                        let code = match send.stopped().await {
                                            Ok(Some(code)) => code.into_inner().to_string(),
                                            other => format!("{other:?}"),
                                        };
                                        line(format!("L9_STOPPED {name} {code}"));
                                    });
                                }
                                other => panic!("unknown action {other}"),
                            }
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

/// An endpoint that completes no handshake: it never calls `accept`.
fn mute_child() {
    let rt = runtime();
    let socket = rt.block_on(async {
        let endpoint = child_endpoint_builder().bind().await.unwrap();
        let socket = announce(&endpoint);
        let mut commands = stdin_commands();
        while let Some(command) = commands.recv().await {
            if command == "quit" {
                break;
            }
        }
        endpoint.close().await;
        socket
    });
    finish_child(rt, socket);
}

/// An unrelated process that takes a specific (just freed) UDP port and sits on it: the
/// "another process took the ephemeral port" case a rebind probe mistakes for a leak.
fn squatter_child() {
    let port: u16 = std::env::var("L9_SQUAT_PORT").unwrap().parse().unwrap();
    let socket = UdpSocket::bind((Ipv4Addr::LOCALHOST, port)).unwrap();
    println!(
        "L9_READY {}",
        serde_json::json!({
            "id": iroh::SecretKey::from_bytes(&[9; 32]).public().to_string(),
            "socket": socket.local_addr().unwrap().to_string(),
            "pid": std::process::id()
        })
    );
    std::io::stdout().flush().unwrap();
    // Hold the port until the parent kills this exact pid.
    let mut line = String::new();
    while std::io::stdin().read_line(&mut line).is_ok_and(|n| n > 0) {
        line.clear();
    }
}

/// Scripted raw sender towards the parent's pooled router.
fn sender_child() {
    let parent_id: EndpointId = std::env::var("L9_PARENT_ID").unwrap().parse().unwrap();
    let parent_socket: SocketAddr = std::env::var("L9_PARENT_SOCKET").unwrap().parse().unwrap();
    let (valid, valid2) = valid_frames();
    let rt = runtime();
    let socket = rt.block_on(async {
        let endpoint = child_endpoint_builder().bind().await.unwrap();
        let socket = announce(&endpoint);
        let mut commands = stdin_commands();
        let mut connections: std::collections::BTreeMap<String, iroh::endpoint::Connection> =
            std::collections::BTreeMap::new();
        while let Some(command) = commands.recv().await {
            let parts: Vec<&str> = command.split(' ').collect();
            match parts[0] {
                "quit" => break,
                "connect" => {
                    let target = EndpointAddr::new(parent_id).with_ip_addr(parent_socket);
                    let conn = endpoint
                        .connect(target, Channel::Router.alpn())
                        .await
                        .unwrap();
                    line("L9_CONNECTED".to_owned());
                    let watcher = conn.clone();
                    keep(async move {
                        let error = watcher.closed().await;
                        line(format!("L9_CONN_CLOSED {}", close_text(&error)));
                    });
                    connections.insert(parts[1].to_owned(), conn);
                }
                "send" => {
                    let number: u8 = parts[2].parse().unwrap();
                    let generation: u64 = parts[3].parse().unwrap();
                    let inner = match parts[4] {
                        "valid" => valid.clone(),
                        "valid2" => valid2.clone(),
                        "garbage" => b"not-a-router-frame".to_vec(),
                        other => panic!("unknown frame kind {other}"),
                    };
                    let conn = connections.get(parts[1]).cloned().expect("connect first");
                    keep(async move {
                        let started = Instant::now();
                        let (mut send, mut recv) = conn.open_bi().await.unwrap();
                        let payload = [generation.to_be_bytes().as_slice(), &inner].concat();
                        let frame =
                            remote::encode_remote_frame(RemoteKind::Transfer, id(number), &payload)
                                .unwrap();
                        send.write_all(&frame).await.unwrap();
                        send.finish().unwrap();
                        let result = match recv.read_to_end(remote::REMOTE_MAX_FRAME_BYTES).await {
                            Ok(bytes) => match remote::decode_remote_frame(&bytes) {
                                Ok(f) if f.kind == RemoteKind::Ack && f.payload.is_empty() => {
                                    "ack".to_owned()
                                }
                                Ok(f) => format!("err:{}", String::from_utf8_lossy(&f.payload)),
                                Err(error) => format!("undecodable:{error:?}"),
                            },
                            Err(ReadToEndError::Read(ReadError::Reset(code))) => {
                                format!("reset:{}", code.into_inner())
                            }
                            Err(other) => format!("readerr:{other}"),
                        };
                        line(format!(
                            "L9_RESP {} {} {result}",
                            hex(&id(number)),
                            started.elapsed().as_millis()
                        ));
                    });
                }
                other => panic!("unknown command {other}"),
            }
        }
        for conn in connections.values() {
            conn.close(0u32.into(), b"done");
        }
        reap_kept().await;
        endpoint.close().await;
        socket
    });
    finish_child(rt, socket);
}

/// Artifact-ALPN client: opens N connections on `mycelium.artifact_chunk.v1`, each
/// with one half-open request stream, and can write a Router transfer frame on one.
fn artifact_child() {
    let parent_id: EndpointId = std::env::var("L9_PARENT_ID").unwrap().parse().unwrap();
    let parent_socket: SocketAddr = std::env::var("L9_PARENT_SOCKET").unwrap().parse().unwrap();
    let (valid, _) = valid_frames();
    let rt = runtime();
    let socket = rt.block_on(async {
        let endpoint = child_endpoint_builder().bind().await.unwrap();
        let socket = announce(&endpoint);
        let mut commands = stdin_commands();
        let mut held: Vec<(iroh::endpoint::Connection, SendStream)> = Vec::new();
        let mut alpn_held: Vec<iroh::endpoint::Connection> = Vec::new();
        while let Some(command) = commands.recv().await {
            let parts: Vec<&str> = command.split(' ').collect();
            match parts[0] {
                "quit" => break,
                "alpn" => {
                    // `alpn <router|control|artifact|unknown>`: one dial on that ALPN; report the
                    // handshake verdict and, if it connected, the close the parent applies.
                    let name = parts[1].to_owned();
                    let alpn: &[u8] = match parts[1] {
                        "router" => Channel::Router.alpn(),
                        "control" => Channel::NodeControl.alpn(),
                        "artifact" => Channel::ArtifactChunk.alpn(),
                        _ => b"mycelium.unknown.v1",
                    };
                    let target = EndpointAddr::new(parent_id).with_ip_addr(parent_socket);
                    match endpoint.connect(target, alpn).await {
                        Ok(connection) => {
                            line(format!("L9_ALPN_CONNECTED {name}"));
                            let watcher = connection.clone();
                            keep(async move {
                                let error = watcher.closed().await;
                                line(format!("L9_ALPN_CLOSED {name} {}", close_text(&error)));
                            });
                            alpn_held.push(connection);
                        }
                        Err(error) => line(format!("L9_ALPN_FAILED {name} {error}")),
                    }
                }
                "art" => {
                    let count: usize = parts[1].parse().unwrap();
                    for _ in 0..count {
                        let number = held.len();
                        let target = EndpointAddr::new(parent_id).with_ip_addr(parent_socket);
                        let connection = match endpoint
                            .connect(target, Channel::ArtifactChunk.alpn())
                            .await
                        {
                            Ok(connection) => connection,
                            Err(error) => {
                                line(format!("L9_ART_FAILED {number} {error}"));
                                continue;
                            }
                        };
                        let watcher = connection.clone();
                        keep(async move {
                            let error = watcher.closed().await;
                            line(format!("L9_ART_CLOSED {number} {}", close_text(&error)));
                        });
                        let mut sent = false;
                        if let Ok((mut send, _recv)) = connection.open_bi().await {
                            let payload = if parts.get(2) == Some(&"frame") {
                                let body = [2_u64.to_be_bytes().as_slice(), &valid].concat();
                                remote::encode_remote_frame(RemoteKind::Transfer, id(77), &body)
                                    .unwrap()
                            } else {
                                b"{\"partial\":".to_vec()
                            };
                            sent = send.write_all(&payload).await.is_ok();
                            held.push((connection.clone(), send));
                        }
                        line(format!(
                            "L9_ART {number} {}",
                            if sent { "OPEN" } else { "UNUSED" }
                        ));
                    }
                }
                other => panic!("unknown command {other}"),
            }
        }
        for (connection, _) in &held {
            connection.close(0u32.into(), b"done");
        }
        reap_kept().await;
        endpoint.close().await;
        socket
    });
    finish_child(rt, socket);
}

#[test]
fn pool_child_fixture() {
    let Ok(mode) = std::env::var("L9_POOL_CHILD") else {
        return;
    };
    match mode.as_str() {
        "acceptor" => acceptor_child(),
        "mute" => mute_child(),
        "sender" => sender_child(),
        "artifact" => artifact_child(),
        "squatter" => squatter_child(),
        other => panic!("unknown child mode {other}"),
    }
}

// ---------------------------------------------------------------------------
// Parent scenarios.
// ---------------------------------------------------------------------------
fn request(n: u8, payload: &[u8]) -> EnqueueRequest {
    EnqueueRequest {
        message_id: id(n),
        digest: runtime::iroh_pool::frame_digest(payload),
        payload: payload.to_vec(),
        expected_generation: None,
        source_generation: None,
        destination: None,
    }
}
fn queued(outcome: EnqueueOutcome) -> OutboundHandle {
    match outcome {
        EnqueueOutcome::Queued(handle) => handle,
        other => panic!("expected Queued, got {other:?}"),
    }
}
async fn terminal(router: &PooledRouter<Mono>, handle: OutboundHandle) -> OutboundTerminal {
    tokio::time::timeout(slice(), router.wait_terminal(handle))
        .await
        .expect("terminal within slice")
}

#[test]
fn one_pooled_connection_serves_every_frame_and_valid_retries_back_off_without_eviction() {
    let _scenario = scenario();
    let (frame, _) = valid_frames();
    let rt = runtime();
    let parent = Parent::new(&rt, limits(200, 400, 20_000, 3_000, 5_000, 4));
    let mut peer = Peer::spawn(
        "acceptor",
        &[("L9_SCRIPT", "queue_full,queue_full,ack".into())],
    );
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), 2)])
        .unwrap();
    rt.block_on(async {
        let first = queued(parent.router().enqueue(request(1, &frame)));
        let second = queued(parent.router().enqueue(request(2, &frame)));
        assert_eq!(
            terminal(parent.router(), first).await,
            OutboundTerminal::Delivered
        );
        assert_eq!(
            terminal(parent.router(), second).await,
            OutboundTerminal::Delivered
        );
        peer.settle(pause(300)).await;
    });
    let streams = peer.streams();
    assert_eq!(
        streams.len(),
        4,
        "3 attempts for #1 (2 queue_full), 1 for #2: {:#?}",
        peer.log
    );
    assert!(
        streams.iter().all(|s| s.0 == 1),
        "every stream rode connection 1"
    );
    assert_eq!(
        peer.lines("L9_CONN ").len(),
        1,
        "one dial for the whole pool"
    );
    assert!(
        peer.lines("L9_CONN_CLOSED ").is_empty(),
        "queue_full must not evict"
    );
    assert_eq!(parent.router().pooled_connections(), 1);
    // Doubling clamped at the cap: the planned waits are initial then max, exactly, on the
    // driver's own clock (item 1 has 3 attempts; item 2 starts a fresh run).
    let decisions = parent.router().attempt_decisions();
    assert_eq!(decisions.len(), 4, "{decisions:?}");
    assert_decisions(&decisions[..3], &[200, 400]);
    assert_eq!(decisions[3].backoff_before, None, "{decisions:?}");
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn io_failure_evicts_the_pooled_connection_with_code_8_retry_and_redials() {
    let _scenario = scenario();
    let (frame, _) = valid_frames();
    let rt = runtime();
    let parent = Parent::new(&rt, limits(200, 400, 20_000, 3_000, 5_000, 4));
    let mut peer = Peer::spawn("acceptor", &[("L9_SCRIPT", "garbage,ack".into())]);
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), 2)])
        .unwrap();
    rt.block_on(async {
        let handle = queued(parent.router().enqueue(request(1, &frame)));
        assert_eq!(
            terminal(parent.router(), handle).await,
            OutboundTerminal::Delivered
        );
        let closed = peer.expect("L9_CONN_CLOSED 1 ").await;
        assert_eq!(closed, "code=8 reason=retry");
        peer.settle(pause(200)).await;
    });
    assert_eq!(
        peer.lines("L9_CONN ").len(),
        2,
        "redialled after the eviction"
    );
    let streams = peer.streams();
    assert_eq!((streams[0].0, streams[1].0), (1, 2));
    assert_eq!(parent.router().pooled_connections(), 1);
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn cancel_resets_one_stream_with_code_7_releases_the_permit_and_keeps_the_pooled_connection() {
    let _scenario = scenario();
    let (frame, _) = valid_frames();
    let rt = runtime();
    let parent = Parent::new(&rt, limits(200, 400, 20_000, 3_000, 5_000, 2));
    let mut peer = Peer::spawn("acceptor", &[("L9_SCRIPT", "hold,ack".into())]);
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), 2)])
        .unwrap();
    rt.block_on(async {
        let router = parent.router();
        let held = queued(router.enqueue(request(1, &frame)));
        peer.expect("L9_STREAM ").await;
        let sibling = queued(router.enqueue(request(2, &frame)));
        assert_eq!(router.outbound_free(), 0);
        assert!(router.cancel(id(1)));
        // Terminal and permit release happen before any worker traversal.
        assert_eq!(
            router.outbound_terminal(held),
            Some(OutboundTerminal::Cancelled)
        );
        assert_eq!(router.outbound_free(), 1);
        assert!(!router.cancel(id(1)), "the token is gone");
        let stopped = peer.expect("L9_STOPPED ").await;
        assert_eq!(stopped, format!("{} 7", hex(&id(1))), "STOP_SENDING code 7");
        assert_eq!(terminal(router, sibling).await, OutboundTerminal::Delivered);
        assert_eq!(terminal(router, held).await, OutboundTerminal::Cancelled);
        peer.settle(pause(300)).await;
    });
    let streams = peer.streams();
    assert_eq!(streams.len(), 2);
    assert!(
        streams.iter().all(|s| s.0 == 1),
        "the sibling used the same connection"
    );
    assert!(
        peer.lines("L9_CONN_CLOSED ").is_empty(),
        "cancel must not close the pool"
    );
    assert_eq!(peer.lines("L9_CONN ").len(), 1);
    assert_eq!(parent.router().pooled_connections(), 1);
    assert_eq!(parent.router().outbound_free(), 2);
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn lifetime_is_the_enqueue_time_bound_and_no_attempt_follows_it() {
    let _scenario = scenario();
    let (frame, _) = valid_frames();
    let rt = runtime();
    let parent = Parent::new(&rt, limits(200, 400, 1_500, 3_000, 5_000, 4));
    let mut peer = Peer::spawn("acceptor", &[("L9_SCRIPT", "queue_full".into())]);
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), 2)])
        .unwrap();
    let started = Instant::now();
    let (elapsed, at_terminal) = rt.block_on(async {
        let handle = queued(parent.router().enqueue(request(1, &frame)));
        assert_eq!(
            terminal(parent.router(), handle).await,
            OutboundTerminal::Cancelled
        );
        let elapsed = started.elapsed();
        let at_terminal = parent.router().attempt_decisions();
        peer.settle(pause(900)).await;
        (elapsed, at_terminal)
    });
    // Host load can lengthen the item but never end it before its lifetime.
    assert!(elapsed >= pause(1_499), "ended early: {elapsed:?}");
    let decisions = parent.router().attempt_decisions();
    assert!(decisions.len() >= 3, "retries ran: {decisions:?}");
    // 200 ms then the 400 ms cap, every decision strictly inside the ENQUEUE-time deadline,
    // which no retry renewed, and no attempt decided after the terminal.
    assert_decisions(&decisions, &[200, 400]);
    assert!(
        decisions[0]
            .deadline
            .checked_sub(ms(1_500))
            .unwrap()
            .ticks()
            <= decisions[0].decided_at.ticks(),
        "the deadline is enqueue + lifetime: {decisions:?}"
    );
    assert_eq!(
        decisions, at_terminal,
        "an attempt was decided after the terminal"
    );
    assert_eq!(parent.router().outbound_free(), 4);
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn a_dial_is_capped_by_the_connect_bound_not_the_stream_bound() {
    let _scenario = scenario();
    let (frame, _) = valid_frames();
    let rt = runtime();
    // connect 300 ms, stream 5 s, lifetime 1.7 s, fixed 200-ms retry: a capped
    // dial fails every ~500 ms (3-4 failures); an uncapped one would still be
    // in its first dial when the lifetime ends.
    let parent = Parent::new(&rt, limits(200, 200, 1_700, 300, 5_000, 4));
    let mut peer = Peer::spawn("mute", &[]);
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), 2)])
        .unwrap();
    rt.block_on(async {
        let handle = queued(parent.router().enqueue(request(1, &frame)));
        assert_eq!(
            terminal(parent.router(), handle).await,
            OutboundTerminal::Cancelled
        );
    });
    let stats = parent.router().stats();
    assert!(
        (3..=4).contains(&stats.dial_failures),
        "dial failures: {stats:?}"
    );
    assert_eq!(parent.router().pooled_connections(), 0);
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn rotation_closes_the_pool_with_code_8_peer_rotated_and_ends_the_held_item() {
    let _scenario = scenario();
    let (frame, _) = valid_frames();
    let rt = runtime();
    let parent = Parent::new(&rt, limits(200, 400, 20_000, 3_000, 5_000, 2));
    let mut peer = Peer::spawn("acceptor", &[("L9_SCRIPT", "hold".into())]);
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), 2)])
        .unwrap();
    rt.block_on(async {
        let router = parent.router();
        let held = queued(router.enqueue(request(1, &frame)));
        peer.expect("L9_STREAM ").await;
        let outcome = router
            .configure_peers(vec![(peer.address.clone(), 3)])
            .unwrap();
        assert!(outcome.changed);
        assert_eq!(outcome.closed_pool, vec![(*peer.address.id.as_bytes(), 2)]);
        assert_eq!(terminal(router, held).await, OutboundTerminal::PeerRotated);
        let closed = peer.expect("L9_CONN_CLOSED 1 ").await;
        assert_eq!(closed, "code=8 reason=peer_rotated");
        peer.settle(pause(600)).await;
    });
    assert_eq!(peer.streams().len(), 1, "no stale delivery after rotation");
    assert_eq!(parent.router().pooled_connections(), 0);
    assert_eq!(parent.router().outbound_free(), 2);
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

fn sender_peer(parent: &Parent<'_>) -> Peer {
    let address = parent.address();
    Peer::spawn(
        "sender",
        &[
            ("L9_PARENT_ID", address.id.to_string()),
            (
                "L9_PARENT_SOCKET",
                address.ip_addrs().next().unwrap().to_string(),
            ),
        ],
    )
}
async fn response(peer: &mut Peer, n: u8) -> String {
    let rest = peer.expect(&format!("L9_RESP {} ", hex(&id(n)))).await;
    rest.split_once(' ').unwrap().1.to_owned()
}

#[test]
fn inbound_replies_follow_the_sidecar_order_and_ack_only_after_the_local_ack() {
    let _scenario = scenario();
    let (frame, other) = valid_frames();
    let rt = runtime();
    let parent = Parent::new(&rt, limits(200, 400, 20_000, 3_000, 2_000, 1));
    let mut peer = sender_peer(&parent);
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), 2)])
        .unwrap();
    rt.block_on(async {
        let router = parent.router();
        peer.command("connect a");
        peer.expect("L9_CONNECTED").await;
        // Wrong generation answers peer_rotated BEFORE the inner frame is judged.
        peer.command("send a 1 3 valid");
        assert_eq!(response(&mut peer, 1).await, "err:peer_rotated");
        peer.command("send a 2 3 garbage");
        assert_eq!(response(&mut peer, 2).await, "err:peer_rotated");
        peer.command("send a 3 2 garbage");
        assert_eq!(response(&mut peer, 3).await, "err:invalid_frame");
        // Admitted but not yet dispatched: no reply of any kind.
        peer.command("send a 4 2 valid");
        let delivery = router.receive(1, ms(3_000)).await.expect("admitted item");
        assert_eq!(delivery.message_id, id(4));
        assert_eq!(delivery.payload, frame);
        assert_eq!(delivery.generation, 2);
        assert_eq!(delivery.source_endpoint, Some(*peer.address.id.as_bytes()));
        peer.settle(pause(500)).await;
        assert!(
            peer.lines("L9_RESP ").len() == 3,
            "reply before the local ack: {:#?}",
            peer.log
        );
        assert!(!router.ack(2, id(4)), "another session cannot acknowledge");
        peer.settle(pause(200)).await;
        assert_eq!(peer.lines("L9_RESP ").len(), 3, "{:#?}", peer.log);
        assert!(router.ack(1, id(4)));
        assert_eq!(response(&mut peer, 4).await, "ack");
        // Completed history: same bytes re-ACK, changed bytes collide.
        peer.command("send a 4 2 valid");
        assert_eq!(response(&mut peer, 4).await, "ack");
        peer.command("send a 4 2 valid2");
        assert_eq!(response(&mut peer, 4).await, "err:replay_collision");
        // Streams on one connection are served strictly serially (as in v0.1.4):
        // item 5 is admitted (capacity 1) and its dispatch wait expires, but the
        // item stays pending and keeps the capacity, so the next admission is full.
        peer.command("send a 5 2 valid");
        assert_eq!(response(&mut peer, 5).await, "reset:7");
        peer.command("send a 6 2 valid");
        assert_eq!(response(&mut peer, 6).await, "err:queue_full");
        assert_eq!(router.try_receive(1).map(|d| d.message_id), Some(id(5)));
        assert_ne!(frame, other);
        assert!(peer.lines("L9_CONN_CLOSED").is_empty());
    });
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn dispatch_wait_expiry_resets_the_stream_with_code_7_never_acks_and_a_retransmit_joins_the_pending_item()
 {
    let _scenario = scenario();
    let rt = runtime();
    // The shipped wait is 60 s; this exercises the same code path at 1.5 s.
    let parent = Parent::new(&rt, limits(200, 400, 20_000, 3_000, 1_500, 4));
    let mut peer = sender_peer(&parent);
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), 2)])
        .unwrap();
    rt.block_on(async {
        let router = parent.router();
        peer.command("connect a");
        peer.expect("L9_CONNECTED").await;
        peer.command("send a 7 2 valid");
        let reset = response(&mut peer, 7).await;
        let (elapsed, result) = {
            let record = peer.lines("L9_RESP ").pop().unwrap();
            let fields: Vec<&str> = record.split(' ').collect();
            (fields[1].parse::<u64>().unwrap(), fields[2].to_owned())
        };
        assert_eq!(reset, "reset:7");
        assert_eq!(result, "reset:7");
        assert!(
            (1_500..4_000).contains(&elapsed),
            "reset after {elapsed} ms"
        );
        assert!(
            peer.lines("L9_CONN_CLOSED").is_empty(),
            "the connection survives the reset"
        );
        // Nothing was performed or acknowledged: the item is still pending, once.
        peer.command("send a 7 2 valid");
        let delivery = router.receive(1, ms(3_000)).await.expect("still pending");
        assert_eq!(delivery.message_id, id(7));
        assert!(
            router.try_receive(1).is_none(),
            "the retransmit must not duplicate the item"
        );
        assert!(router.ack(1, id(7)));
        assert_eq!(response(&mut peer, 7).await, "ack");
    });
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn unconfigured_and_unknown_remotes_are_closed_with_code_8() {
    let _scenario = scenario();
    let rt = runtime();
    let parent = Parent::new(&rt, limits(200, 400, 20_000, 3_000, 3_000, 2));
    let mut peer = sender_peer(&parent);
    rt.block_on(async {
        peer.command("connect a");
        peer.expect("L9_CONNECTED").await;
        assert_eq!(
            peer.expect("L9_CONN_CLOSED ").await,
            "code=8 reason=unconfigured"
        );
        // Configure a different identity: the connecting child is now unknown.
        let stranger = EndpointAddr::new(iroh::SecretKey::from_bytes(&[9; 32]).public())
            .with_ip_addr(SocketAddr::from((Ipv4Addr::LOCALHOST, 9)));
        parent
            .router()
            .configure_peers(vec![(stranger, 2)])
            .unwrap();
        peer.command("connect a");
        peer.expect("L9_CONNECTED").await;
        assert_eq!(
            peer.expect("L9_CONN_CLOSED ").await,
            "code=8 reason=identity"
        );
    });
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn inbound_rotation_fences_inflight_work_and_closes_the_connection_with_code_8() {
    let _scenario = scenario();
    let rt = runtime();
    let parent = Parent::new(&rt, limits(200, 400, 20_000, 3_000, 3_000, 2));
    let mut peer = sender_peer(&parent);
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), 2)])
        .unwrap();
    rt.block_on(async {
        let router = parent.router();
        peer.command("connect a");
        peer.expect("L9_CONNECTED").await;
        peer.command("send a 8 2 valid");
        let delivery = router.receive(1, ms(3_000)).await.expect("admitted item");
        assert_eq!(delivery.message_id, id(8));
        router
            .configure_peers(vec![(peer.address.clone(), 3)])
            .unwrap();
        assert_eq!(
            peer.expect("L9_CONN_CLOSED ").await,
            "code=8 reason=peer_rotated"
        );
        peer.settle(pause(300)).await;
        let acked = peer.lines("L9_RESP ").iter().any(|l| l.ends_with(" ack"));
        assert!(!acked, "rotation must never acknowledge: {:#?}", peer.log);
        assert!(!router.ack(1, id(8)), "rotation cleared the inflight item");
        assert_eq!(router.inbound_free(), 2);
    });
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn an_idle_inbound_connection_is_closed_with_code_8_when_the_peer_table_is_republished() {
    let _scenario = scenario();
    // v0.1.4 `handle_remote_connection` selects `accept_bi` against the peer
    // generation watch (sidecar.rs:2778-2786): an idle connection, which never
    // reaches the loop-top `peer_matches` check, is closed the moment the table
    // changes.
    let rt = runtime();
    let parent = Parent::new(&rt, limits(200, 400, 20_000, 3_000, 3_000, 2));
    let mut peer = sender_peer(&parent);
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), 2)])
        .unwrap();
    rt.block_on(async {
        peer.command("connect a");
        peer.expect("L9_CONNECTED").await;
        peer.settle(pause(300)).await;
        assert!(peer.lines("L9_CONN_CLOSED").is_empty(), "idle but admitted");
        parent
            .router()
            .configure_peers(vec![(peer.address.clone(), 3)])
            .unwrap();
        assert_eq!(
            peer.expect("L9_CONN_CLOSED ").await,
            "code=8 reason=peer_rotated"
        );
    });
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

// ---------------------------------------------------------------------------
// Round 8: artifact-ALPN branch and sidecar telemetry (sidecar.rs:2731-2744,
// 2062-2108, 2756-2769, 2859).
// ---------------------------------------------------------------------------
/// Holds every artifact connection open and counts them (stands in for the artifact
/// source's own serve loop, which has its own limits).
struct HoldService {
    served: AtomicUsize,
}
impl ConnectionService for HoldService {
    fn serve(
        self: Arc<Self>,
        connection: iroh::endpoint::Connection,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>> {
        Box::pin(async move {
            self.served.fetch_add(1, Ordering::SeqCst);
            drop(connection.closed().await);
        })
    }
}
fn artifact_peer(parent: &Parent<'_>) -> Peer {
    let address = parent.address();
    Peer::spawn(
        "artifact",
        &[
            ("L9_PARENT_ID", address.id.to_string()),
            (
                "L9_PARENT_SOCKET",
                address.ip_addrs().next().unwrap().to_string(),
            ),
        ],
    )
}
async fn until(what: &str, mut ready: impl FnMut() -> bool) {
    let end = tokio::time::Instant::now() + slice();
    while !ready() {
        assert!(tokio::time::Instant::now() < end, "timed out: {what}");
        tokio::time::sleep(pause(10)).await;
    }
}
fn json(text: &str) -> serde_json::Value {
    serde_json::from_str(text).unwrap()
}

#[test]
fn artifact_connections_go_to_the_artifact_service_and_never_hold_router_slots_or_frames() {
    let _scenario = scenario();
    let (frame, _) = valid_frames();
    let rt = runtime();
    let service = Arc::new(HoldService {
        served: AtomicUsize::new(0),
    });
    let handler: Arc<dyn ConnectionService> = service.clone();
    // Two Router connection slots in total, as in the sidecar's starvation test.
    let parent = Parent::with(
        &rt,
        limits(200, 400, 20_000, 3_000, 3_000, 2),
        &[Channel::Router, Channel::ArtifactChunk],
        Services {
            artifact: Some(handler),
            control: None,
            events: None,
        },
    );
    let mut sender = sender_peer(&parent);
    let mut holder = artifact_peer(&parent);
    // Only the sender is a Router peer: the artifact client is NOT in the table.
    parent
        .router()
        .configure_peers(vec![(sender.address.clone(), 2)])
        .unwrap();
    rt.block_on(async {
        let router = parent.router();
        // Six artifact connections each with a half-open request stream (more than
        // the two Router slots).
        holder.command("art 6");
        for _ in 0..6 {
            holder.expect("L9_ART ").await;
        }
        until("six artifact connections served", || {
            service.served.load(Ordering::SeqCst) == 6
        })
        .await;
        assert_eq!(router.inbound_free(), 2, "artifact load holds no Router slot");
        // A Router transfer frame written on an artifact stream is never admitted.
        holder.command("art 1 frame");
        holder.expect("L9_ART ").await;
        until("seventh artifact connection served", || {
            service.served.load(Ordering::SeqCst) == 7
        })
        .await;
        sender.settle(pause(200)).await;
        assert!(router.try_receive(1).is_none(), "no Router delivery from artifact");
        assert_eq!(router.inbound_free(), 2);
        assert!(json(&router.admission_counters_json(*sender.address.id.as_bytes()))
            ["inbound_frames_admitted"]
            == 0);
        // A configured Router peer is still admitted promptly under that load.
        sender.command("connect a");
        sender.expect("L9_CONNECTED").await;
        sender.command("send a 1 2 valid");
        let delivery = router.receive(1, ms(3_000)).await.expect("router frame");
        assert_eq!((delivery.message_id, delivery.payload), (id(1), frame));
        assert!(router.ack(1, id(1)));
        assert_eq!(response(&mut sender, 1).await, "ack");
        assert!(holder.lines("L9_ART_CLOSED").is_empty(), "artifact connections stay open");
    });
    let sockets = parent.teardown(&rt);
    holder.finish(&rt);
    sender.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn the_artifact_alpn_with_no_service_is_closed_with_code_8_alpn() {
    let _scenario = scenario();
    let rt = runtime();
    let parent = Parent::with(
        &rt,
        limits(200, 400, 20_000, 3_000, 3_000, 2),
        &[Channel::Router, Channel::ArtifactChunk],
        Services::default(),
    );
    let mut holder = artifact_peer(&parent);
    rt.block_on(async {
        holder.command("art 1");
        assert_eq!(
            holder.expect("L9_ART_CLOSED 0 ").await,
            "code=8 reason=alpn"
        );
    });
    let sockets = parent.teardown(&rt);
    holder.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

// ---------------------------------------------------------------------------
// Round 9: NodeControl hook (not a v0.1.4 behaviour: its sidecar never served NodeControl
// inbound) and the ALPN-negotiation proof for the non-Router ALPN code-8 branch.
// ---------------------------------------------------------------------------
fn alpn_dial(rt: &tokio::runtime::Runtime, peer: &mut Peer, name: &str) -> String {
    rt.block_on(async {
        let seen = peer.log.len();
        peer.command(&format!("alpn {name}"));
        let end = tokio::time::Instant::now() + slice();
        loop {
            peer.settle(pause(50)).await;
            if let Some(verdict) = fresh_verdict(&peer.log, seen, name) {
                return verdict;
            }
            assert!(tokio::time::Instant::now() < end, "no verdict for {name}");
        }
    })
}
/// The close text the parent applied to the caller's `name` connection.
fn alpn_closed(rt: &tokio::runtime::Runtime, peer: &mut Peer, name: &str) -> String {
    rt.block_on(async {
        let end = tokio::time::Instant::now() + slice();
        let prefix = format!("L9_ALPN_CLOSED {name} ");
        loop {
            peer.settle(pause(50)).await;
            if let Some(text) = peer.lines(&prefix).into_iter().next() {
                return text;
            }
            assert!(tokio::time::Instant::now() < end, "no close for {name}");
        }
    })
}

#[test]
fn control_connections_go_to_the_control_service_with_no_router_peer_and_no_router_slot() {
    let _scenario = scenario();
    let rt = runtime();
    let service = Arc::new(HoldService {
        served: AtomicUsize::new(0),
    });
    let handler: Arc<dyn ConnectionService> = service.clone();
    // One Router slot in total; no peer table at all (the control caller is the owner,
    // not a Router peer).
    let parent = Parent::with(
        &rt,
        limits(200, 400, 20_000, 3_000, 3_000, 1),
        &[Channel::Router, Channel::NodeControl],
        Services {
            artifact: None,
            control: Some(handler),
            events: None,
        },
    );
    let mut caller = artifact_peer(&parent);
    let verdict = alpn_dial(&rt, &mut caller, "control");
    assert_eq!(verdict, "CONNECTED control");
    rt.block_on(until("the control service has the connection", || {
        service.served.load(Ordering::SeqCst) == 1
    }));
    // Router capacity is untouched and the connection stays open.
    assert_eq!(parent.router().inbound_free(), 1);
    rt.block_on(caller.settle(pause(300)));
    assert!(caller.lines("L9_ALPN_CLOSED").is_empty());
    // A second control connection is served too (the slot was not held).
    let second = alpn_dial(&rt, &mut caller, "control");
    assert_eq!(second, "CONNECTED control");
    rt.block_on(until("second control connection", || {
        service.served.load(Ordering::SeqCst) == 2
    }));
    let sockets = parent.teardown(&rt);
    caller.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn a_registered_but_unserved_control_alpn_is_closed_with_code_8_alpn() {
    let _scenario = scenario();
    let rt = runtime();
    let parent = Parent::with(
        &rt,
        limits(200, 400, 20_000, 3_000, 3_000, 2),
        &[Channel::Router, Channel::NodeControl],
        Services::default(),
    );
    let mut caller = artifact_peer(&parent);
    assert_eq!(alpn_dial(&rt, &mut caller, "control"), "CONNECTED control");
    assert_eq!(
        alpn_closed(&rt, &mut caller, "control"),
        "code=8 reason=alpn"
    );
    let sockets = parent.teardown(&rt);
    caller.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

/// Ruling (3): v0.1.4 can only ever see Router and (when configured) artifact ALPNs because
/// the endpoint registers nothing else, so its "admit non-Router ALPN" path is unreachable on
/// the wire; `r9-oracle-dump.json` `alpn_negotiation` records the oracle failing the TLS
/// handshake for NodeControl/artifact/unknown on a sidecar-bound endpoint. A host that
/// registers only Router gets the same.
#[test]
fn an_alpn_the_host_did_not_register_fails_the_handshake_exactly_like_the_v014_endpoint() {
    let _scenario = scenario();
    let oracle: serde_json::Value =
        serde_json::from_str(include_str!("../fixtures/sidecar-oracle-vectors.json")).unwrap();
    let router_only = oracle["alpn_negotiation"]["rows"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["target"] == "router_only" && row.get("dial").is_some())
        .map(|row| {
            (
                row["alpn"].as_str().unwrap().to_owned(),
                row["dial"].as_str().unwrap().to_owned(),
            )
        })
        .collect::<Vec<_>>();
    assert_eq!(router_only.len(), 4, "the oracle probed four ALPNs");
    let rt = runtime();
    let parent = Parent::with(
        &rt,
        limits(200, 400, 20_000, 3_000, 3_000, 2),
        &[Channel::Router],
        Services::default(),
    );
    let mut caller = artifact_peer(&parent);
    for (oracle_name, oracle_dial) in router_only {
        let name = match oracle_name.as_str() {
            "node_control" => "control",
            "artifact_chunk" => "artifact",
            other => other,
        };
        let verdict = alpn_dial(&rt, &mut caller, name);
        if oracle_dial == "connected" {
            assert_eq!(verdict, format!("CONNECTED {name}"));
        } else {
            assert!(
                oracle_dial.contains("cryptographic handshake failed"),
                "{oracle_dial}"
            );
            assert!(
                verdict.starts_with(&format!("FAILED {name} "))
                    && verdict.contains("cryptographic handshake failed"),
                "{name}: {verdict}"
            );
        }
    }
    let sockets = parent.teardown(&rt);
    caller.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn observations_count_attempts_failures_reconnects_frames_and_a_direct_path() {
    let _scenario = scenario();
    let (frame, _) = valid_frames();
    let rt = runtime();
    let events = Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink_log = events.clone();
    let sink: EventSink = Arc::new(move |event, remote, generation| {
        sink_log
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push((event, remote, generation));
    });
    let parent = Parent::with(
        &rt,
        limits(200, 400, 20_000, 3_000, 5_000, 4),
        &[Channel::Router],
        Services {
            artifact: None,
            control: None,
            events: Some(sink),
        },
    );
    let mut peer = Peer::spawn("acceptor", &[("L9_SCRIPT", "garbage,ack,ack".into())]);
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), 2)])
        .unwrap();
    let remote = peer.address.id.to_string();
    // Never used yet: zeros and `unknown`.
    let idle = json(&parent.router().transport_observations_json());
    assert_eq!(
        idle["protocol"],
        "mycelium.iroh_sidecar.transport_observations.v1"
    );
    assert_eq!(idle["observations"][0]["path_class"], "unknown");
    assert_eq!(idle["observations"][0]["connections_opened"], 0);
    assert_eq!(
        idle["observations"][0]["remote_endpoint_id"],
        remote.as_str()
    );
    assert_eq!(idle["observations"][0]["connection_generation"], 2);
    rt.block_on(async {
        for n in [1, 2] {
            let handle = queued(parent.router().enqueue(request(n, &frame)));
            assert_eq!(
                terminal(parent.router(), handle).await,
                OutboundTerminal::Delivered
            );
        }
    });
    let document = json(&parent.router().transport_observations_json());
    let o = &document["observations"][0];
    // attempt 1 garbage (io failure, evicted), attempt 2 ack on a new connection,
    // attempt 3 ack on the same connection.
    assert_eq!(o["connections_opened"], 2);
    assert_eq!(o["reconnect_count"], 1);
    assert_eq!(o["frames_sent"], 2);
    assert!(
        (o["loss_ratio"].as_f64().unwrap() - 1.0 / 3.0).abs() < 1e-12,
        "{o}"
    );
    assert_eq!(o["path_class"], "direct");
    assert_eq!(o["relay_identity"], serde_json::Value::Null);
    assert_eq!(o["selected_path_changes"], 1);
    assert!(o["sample_count"].as_u64().unwrap() >= 2);
    assert!(o["observed_goodput_bps"].as_f64().unwrap() > 0.0);
    assert!(o["measured_at_unix_ms"].as_u64().unwrap() > 1_700_000_000_000);
    let log = events
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .clone();
    let key = *peer.address.id.as_bytes();
    assert_eq!(
        log,
        vec![
            (TransportEvent::ConnectionOpened, key, 2),
            (TransportEvent::ConnectionOpened, key, 2)
        ]
    );
    assert_eq!(
        log[0].0.line(&remote, 2),
        format!(
            "{{\"connection_generation\":2,\"event\":\"transport_connection_opened\",\"remote_endpoint_id\":\"{remote}\"}}"
        )
    );
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn a_dial_over_the_connect_bound_names_the_deadline_event_and_counts_failures() {
    let _scenario = scenario();
    let (frame, _) = valid_frames();
    let rt = runtime();
    let events = Arc::new(std::sync::Mutex::new(Vec::new()));
    let sink_log = events.clone();
    let sink: EventSink = Arc::new(move |event, _, generation| {
        sink_log
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .push((event, generation));
    });
    let parent = Parent::with(
        &rt,
        limits(200, 200, 1_300, 300, 5_000, 4),
        &[Channel::Router],
        Services {
            artifact: None,
            control: None,
            events: Some(sink),
        },
    );
    let mut peer = Peer::spawn("mute", &[]);
    parent
        .router()
        .configure_peers(vec![(peer.address.clone(), 2)])
        .unwrap();
    rt.block_on(async {
        let handle = queued(parent.router().enqueue(request(1, &frame)));
        assert_eq!(
            terminal(parent.router(), handle).await,
            OutboundTerminal::Cancelled
        );
    });
    let o = json(&parent.router().transport_observations_json())["observations"][0].clone();
    assert_eq!(o["connections_opened"], 0);
    assert_eq!(o["frames_sent"], 0);
    // Every attempt that ended failed; the last one is cut off by the item's own
    // lifetime before it can fail (attempts counted at start, failures at eviction),
    // so the ratio is below 1 but above one half with 3-4 attempts.
    let loss = o["loss_ratio"].as_f64().unwrap();
    assert!((0.5..1.0).contains(&loss), "loss ratio {loss}: {o}");
    let log = events
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .clone();
    assert!(!log.is_empty());
    assert!(
        log.iter()
            .all(|e| *e == (TransportEvent::PeerConnectDeadlineExceeded, 2)),
        "{log:?}"
    );
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

#[test]
fn admission_counters_count_identity_rejections_per_remote_and_first_admissions_only() {
    let _scenario = scenario();
    let (frame, _) = valid_frames();
    let rt = runtime();
    let parent = Parent::new(&rt, limits(200, 400, 20_000, 3_000, 3_000, 2));
    let mut peer = sender_peer(&parent);
    let remote = *peer.address.id.as_bytes();
    let stranger_key = *iroh::SecretKey::from_bytes(&[9; 32]).public().as_bytes();
    rt.block_on(async {
        let router = parent.router();
        let counters = |candidate| json(&router.admission_counters_json(candidate));
        // Unconfigured closes are not identity rejections.
        peer.command("connect a");
        peer.expect("L9_CONNECTED").await;
        assert_eq!(
            peer.expect("L9_CONN_CLOSED ").await,
            "code=8 reason=unconfigured"
        );
        assert_eq!(counters(remote)["inbound_identity_rejections"], 0);
        let stranger = EndpointAddr::new(iroh::SecretKey::from_bytes(&[9; 32]).public())
            .with_ip_addr(SocketAddr::from((Ipv4Addr::LOCALHOST, 9)));
        router.configure_peers(vec![(stranger, 2)]).unwrap();
        peer.command("connect b");
        peer.expect("L9_CONNECTED").await;
        assert_eq!(
            peer.expect("L9_CONN_CLOSED ").await,
            "code=8 reason=identity"
        );
        let after = counters(remote);
        assert_eq!(
            after["protocol"],
            "mycelium.iroh_sidecar.inbound_admission.v1"
        );
        assert_eq!(after["inbound_identity_rejections"], 1);
        assert_eq!(after["candidate_identity_rejections"], 1);
        assert_eq!(counters(stranger_key)["candidate_identity_rejections"], 0);
        assert_eq!(after["inbound_frames_admitted"], 0);
        // Now admit the sender and send the same message twice: one admission.
        router
            .configure_peers(vec![(peer.address.clone(), 3)])
            .unwrap();
        peer.command("connect c");
        peer.expect("L9_CONNECTED").await;
        peer.command("send c 7 3 valid");
        let delivery = router.receive(1, ms(3_000)).await.expect("admitted");
        assert_eq!(delivery.payload, frame);
        // A second connection retransmits the same message while it is still pending:
        // it joins the pending item and is not a second admission.
        peer.command("connect d");
        peer.expect("L9_CONNECTED").await;
        peer.command("send d 7 3 valid");
        peer.settle(pause(300)).await;
        assert_eq!(counters(remote)["inbound_frames_admitted"], 1);
        assert!(router.ack(1, id(7)));
        assert_eq!(response(&mut peer, 7).await, "ack");
        assert_eq!(response(&mut peer, 7).await, "ack");
        assert_eq!(counters(remote)["inbound_frames_admitted"], 1);
    });
    let sockets = parent.teardown(&rt);
    peer.finish(&rt);
    drop(rt);
    assert_all_released(&sockets, "parent socket leaked");
}

// ---------------------------------------------------------------------------
// The descriptor-table check itself (round 9). The old probe bound the freed port and read
// a failure as "leaked"; on this shared host an unrelated process taking the port failed
// `parent socket leaked` in 2 of 20 loaded runs with no holder left to find.
// ---------------------------------------------------------------------------

/// A parent socket in the creation gap: created, close-on-exec not yet applied.
fn gap_socket() -> (UdpSocket, SocketAddr) {
    let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
    let address = socket.local_addr().unwrap();
    rustix::io::fcntl_setfd(&socket, rustix::io::FdFlags::empty()).unwrap();
    (socket, address)
}

#[test]
fn an_unconfined_spawn_leaks_a_parent_socket_and_the_child_table_check_sees_it() {
    let _scenario = scenario();
    let (socket, address) = gap_socket();
    // RED shape: a bare spawn inherits the descriptor, and the check names it.
    let bare = Peer::spawn_with("mute", &[], false);
    let leaked = bare.foreign_sockets();
    assert!(
        leaked
            .iter()
            .any(|name| local_end(name).ends_with(&format!(":{}", address.port()))),
        "unconfined child must hold the parent's socket, table: {leaked:?}"
    );
    // The canonical boundary: same open gap socket, nothing inherited.
    let confined = Peer::spawn("mute", &[]);
    assert!(confined.foreign_sockets().is_empty());
    drop(bare);
    drop(confined);
    drop(socket);
    assert_released(address, "gap socket");
}

#[test]
fn the_old_rebind_probe_called_a_squatter_a_leak_but_the_table_check_does_not() {
    let _scenario = scenario();
    let port = {
        let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
        socket.local_addr().unwrap().port()
    };
    // An unrelated process (no descendant of ours holds a copy of anything) takes the port.
    let squatter = Peer::spawn("squatter", &[("L9_SQUAT_PORT", port.to_string())]);
    assert_eq!(squatter.socket.port(), port);
    assert!(
        UdpSocket::bind(("127.0.0.1", port)).is_err(),
        "the old probe reports the port as held: its false positive"
    );
    // The kernel table of THIS process says it holds nothing on that port.
    assert_released(SocketAddr::from((Ipv4Addr::LOCALHOST, port)), "not ours");
    drop(squatter);
}

#[test]
fn the_scenario_gate_never_lets_more_than_the_allowed_scenarios_run_together() {
    // A gate of its own: the shared one is also held by the scenarios running beside this test.
    let gate = Gate::new(SCENARIOS_AT_ONCE);
    let live = AtomicUsize::new(0);
    let peak = AtomicUsize::new(0);
    std::thread::scope(|scope| {
        for _ in 0..4 * SCENARIOS_AT_ONCE {
            scope.spawn(|| {
                let _scenario = gate.enter();
                let now = live.fetch_add(1, Ordering::SeqCst) + 1;
                peak.fetch_max(now, Ordering::SeqCst);
                std::thread::sleep(pause(30));
                live.fetch_sub(1, Ordering::SeqCst);
            });
        }
    });
    assert_eq!(live.load(Ordering::SeqCst), 0);
    assert!(
        peak.load(Ordering::SeqCst) <= SCENARIOS_AT_ONCE,
        "{} scenarios ran at once",
        peak.load(Ordering::SeqCst)
    );
    assert!(
        peak.load(Ordering::SeqCst) >= 2,
        "the gate must not serialize everything"
    );
}
#[test]
fn a_dials_verdict_is_never_an_earlier_dials_stale_line() {
    let mut log = vec![
        "running 1 test".to_owned(),
        "L9_ALPN_CONNECTED control".to_owned(),
    ];
    // The second dial of the same name has not answered yet.
    assert_eq!(fresh_verdict(&log, 2, "control"), None);
    // The first dial's own verdict is still visible from its own start.
    assert_eq!(
        fresh_verdict(&log, 0, "control").as_deref(),
        Some("CONNECTED control")
    );
    log.push("L9_ALPN_FAILED control handshake refused".to_owned());
    assert_eq!(
        fresh_verdict(&log, 2, "control").as_deref(),
        Some("FAILED control handshake refused")
    );
    assert_eq!(fresh_verdict(&log, 2, "artifact"), None);
}
