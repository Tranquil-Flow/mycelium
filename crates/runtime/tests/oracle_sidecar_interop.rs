// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NET-09 (the REAL v0.1.4 sidecar binary interoperates with the Rust PooledRouter, local only); B-NET-21 (partial).
//!
//! Interop of the REAL v0.1.4 sidecar binary (`native/iroh_transport`
//! `mycelium-iroh-sidecar @ f4e366e9`, production build, no `loopback-test`) with the Rust
//! `PooledRouter`, local only (127.0.0.1, no relay, no address lookup, no public endpoint).
//!
//! Topology: the oracle sidecar is an exact-owned child process, started the shipped way
//! (`--uds <path> --bootstrap-fd <n> --local-only`, the 32-byte secret in a pipe handed over the
//! spawn boundary's pass list). The Rust half is a `PooledRouter` on a `LoopbackHost` with the
//! `serve_unix_listener` local server on its own owner-only socket. One Rust local-IPC client
//! (`LocalClient`, built on the `sidecar_local` codec) drives BOTH sidecars, so every scenario is
//! run in both directions and the local replies of the two sidecars are compared byte for byte.
//!
//! The oracle binary is built by the lane from a pristine `git archive` of the oracle commit
//! (`L9-evidence/r12-oracle-sidecar-build.log`); the tests need `L9_ORACLE_SIDECAR=<path>` and are
//! `#[ignore]`d otherwise (visible in the counts, never a silent pass).
//!
//! Expected values: the oracle's own replies (executed) and the recorded wire vectors
//! (`protocol/fixtures/continued-wire-vectors.json`, accepted by the oracle's ingress).
#![cfg(all(feature = "iroh-adapter", unix))]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant, Span};
use iroh::EndpointAddr;
use protocol::router_wire;
use runtime::{
    iroh_host::LoopbackHost,
    iroh_pool::{DriverLimits, PooledRouter, Services},
    sidecar_listener::{LocalServer, serve_unix_listener},
    sidecar_local::{
        ControlCallLimits, Record, RecordKind, SequenceGuard, SessionKeys, client_hello,
        decode_record, derive_session_keys, encode_record, verify_server_hello,
    },
    sidecar_pool::PoolLimits,
    sidecar_process::{bind_secure_uds, parse_bootstrap_secret},
    sidecar_session::SessionLimits,
};
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Write},
    os::fd::AsRawFd,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex, mpsc},
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::UnixStream,
};
use transport::Clock;

include!("support/fork_lock.rs");

const ORACLE_SECRET: [u8; 32] = [0x4f; 32];
const RUST_SECRET: [u8; 32] = [0x52; 32];
const NEEDS: &str =
    "needs L9_ORACLE_SIDECAR=<oracle mycelium-iroh-sidecar @ f4e366e9>; run with --ignored";

struct Mono(Instant);
impl Clock for Mono {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(ClockDomain(36), self.0.elapsed().as_millis() as u64)
    }
    fn wall_seconds(&self) -> f64 {
        panic!("the pooled driver must not consult wall time")
    }
}
fn ms(value: u64) -> Span {
    Span::from_millis(value)
}
/// Every bounded wait is twice the registry close budget; scenario pauses are parameters.
fn slice() -> Duration {
    Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis() * 2)
}
fn pause(units: u64) -> Duration {
    Duration::from_millis(units)
}
fn runtime() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap()
}
fn id(n: u8) -> [u8; 16] {
    [n; 16]
}
fn unhex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}
fn valid_frames() -> (Vec<u8>, Vec<u8>) {
    let fixture: Value = serde_json::from_str(include_str!(
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
fn scratch(label: &str) -> PathBuf {
    // One directory per call: the in-process and entry-point variants of a scenario share
    // labels and run concurrently.
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let serial = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("oi{}-{serial}-{label}", std::process::id()));
    if let Err(error) = std::fs::remove_dir_all(&dir) {
        assert_eq!(
            error.kind(),
            std::io::ErrorKind::NotFound,
            "stale scratch {}",
            dir.display()
        );
    }
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
    dir
}
fn oracle_binary() -> PathBuf {
    let path = PathBuf::from(
        std::env::var("L9_ORACLE_SIDECAR").expect("L9_ORACLE_SIDECAR must name the oracle binary"),
    );
    assert!(path.is_file(), "{} is not a file", path.display());
    path
}

// ---- the local-IPC client (shared by both sidecars) -------------------------------------

struct LocalClient {
    stream: UnixStream,
    keys: SessionKeys,
    send: u64,
    receive: SequenceGuard,
    endpoint: String,
}
impl LocalClient {
    /// `None` when the sidecar closes instead of answering the hello.
    async fn try_connect(socket: &Path, secret: &[u8; 32]) -> Option<Self> {
        let mut stream = UnixStream::connect(socket).await.ok()?;
        let nonce = [0x21_u8; 32];
        let hello = client_hello(secret, &nonce).unwrap();
        stream.write_u32(hello.len() as u32).await.ok()?;
        stream.write_all(&hello).await.ok()?;
        let reply = tokio::time::timeout(slice(), read_prefixed(&mut stream))
            .await
            .ok()??;
        let (server_nonce, endpoint) = verify_server_hello(&reply, secret, &nonce).ok()?;
        Some(Self {
            stream,
            keys: derive_session_keys(secret, &nonce, &server_nonce).unwrap(),
            send: 0,
            receive: SequenceGuard::new(),
            endpoint,
        })
    }
    async fn connect(socket: &Path, secret: &[u8; 32]) -> Self {
        Self::try_connect(socket, secret)
            .await
            .expect("sidecar answered the hello")
    }
    async fn call(&mut self, kind: RecordKind, message_id: [u8; 16], payload: &[u8]) -> Record {
        let encoded = encode_record(
            kind,
            self.send,
            message_id,
            payload,
            &self.keys.client_to_sidecar,
        )
        .unwrap();
        self.send += 1;
        self.stream.write_u32(encoded.len() as u32).await.unwrap();
        self.stream.write_all(&encoded).await.unwrap();
        let reply = tokio::time::timeout(slice() * 2, read_prefixed(&mut self.stream))
            .await
            .expect("reply within the bound")
            .expect("sidecar replied");
        decode_record(&reply, &self.keys.sidecar_to_client, &mut self.receive).unwrap()
    }
    /// Polls `kind` (Receive / ReceiveFrom) until a delivery arrives, within the bound.
    async fn delivery(&mut self, kind: RecordKind) -> Record {
        let limit = Instant::now() + slice();
        loop {
            let record = self.call(kind, id(0xee), b"").await;
            if matches!(record.kind, RecordKind::Delivery | RecordKind::DeliveryFrom) {
                return record;
            }
            assert_eq!(record.payload, br#"{"code":"empty"}"#, "{record:?}");
            assert!(Instant::now() < limit, "no delivery within the bound");
        }
    }
}
async fn read_prefixed(stream: &mut UnixStream) -> Option<Vec<u8>> {
    let length = stream.read_u32().await.ok()? as usize;
    let mut bytes = vec![0; length];
    stream.read_exact(&mut bytes).await.ok()?;
    Some(bytes)
}
fn code(record: &Record) -> String {
    assert_eq!(record.kind, RecordKind::Error, "{record:?}");
    let value: Value = serde_json::from_slice(&record.payload).unwrap();
    value["code"].as_str().unwrap().to_owned()
}
fn confirmed(frame: &[u8]) -> Vec<u8> {
    [
        1_u64.to_be_bytes().as_slice(),
        1_u64.to_be_bytes().as_slice(),
        frame,
    ]
    .concat()
}
fn peers(list: &[(&EndpointAddr, u64)]) -> Vec<u8> {
    let peers: Vec<Value> = list
        .iter()
        .map(|(address, generation)| {
            json!({"endpoint_id": address.id.to_string(), "endpoint_addr": address, "generation": generation})
        })
        .collect();
    serde_json::to_vec(&json!({ "peers": peers })).unwrap()
}

// ---- the two sidecars ----------------------------------------------------------------

/// What a test needs to know about either sidecar.
struct Side {
    socket: PathBuf,
    secret: [u8; 32],
    address: EndpointAddr,
    name: &'static str,
}

/// The real oracle binary as an exact-owned child, reaped on every path.
struct OracleProcess {
    child: Option<Child>,
    readers: Vec<std::thread::JoinHandle<()>>,
    stderr: Arc<Mutex<Vec<String>>>,
    side: Side,
}
impl OracleProcess {
    fn start(label: &str) -> Self {
        Self::start_binary(label, oracle_binary(), ORACLE_SECRET, "oracle")
    }
    /// Any sidecar binary started the shipped way (`--uds`, `--bootstrap-fd`, `--local-only`).
    fn start_binary(label: &str, binary: PathBuf, secret: [u8; 32], name: &'static str) -> Self {
        let dir = scratch(label);
        let socket = dir.join("s");
        let (reader, writer) = rustix::pipe::pipe().unwrap();
        let mut writer = std::fs::File::from(writer);
        writer.write_all(&secret).unwrap();
        drop(writer);
        let number = reader.as_raw_fd();
        let mut command = Command::new(&binary);
        command
            .arg("--uds")
            .arg(&socket)
            .arg("--bootstrap-fd")
            .arg(number.to_string())
            .arg("--local-only")
            .current_dir(&dir)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        node::fds::confine_descriptors_except(&mut command, &[number]).expect("pass list accepted");
        let mut child = {
            let _boundary = fixture_spawn_boundary();
            command.spawn().expect("oracle sidecar spawned")
        };
        drop(reader);
        let stdout = child.stdout.take().unwrap();
        let (lines, ready) = mpsc::channel();
        let stdout_reader = std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if lines.send(line).is_err() {
                    return;
                }
            }
        });
        let stderr_lines = Arc::new(Mutex::new(Vec::new()));
        let sink = stderr_lines.clone();
        let stderr = child.stderr.take().unwrap();
        let stderr_reader = std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                sink.lock().unwrap().push(line);
            }
        });
        let mut process = Self {
            child: Some(child),
            readers: vec![stdout_reader, stderr_reader],
            stderr: stderr_lines,
            side: Side {
                socket,
                secret,
                address: EndpointAddr::new(iroh::SecretKey::from_bytes(&[1; 32]).public()),
                name,
            },
        };
        let line = ready
            .recv_timeout(slice())
            .unwrap_or_else(|e| panic!("oracle ready record: {e}; stderr {:?}", process.stderr));
        let ready: Value = serde_json::from_str(&line).expect("ready record is JSON");
        assert_eq!(ready["event"], "ready", "{ready}");
        // Executed: the ready record names the Router ALPN of the transport crate.
        assert_eq!(
            ready["alpn"].as_str().map(str::as_bytes),
            Some(transport::Channel::Router.alpn()),
            "{ready}"
        );
        process.side.address = serde_json::from_value(ready["endpoint_addr"].clone()).unwrap();
        assert_eq!(process.side.address.id.to_string(), ready["endpoint_id"]);
        process
    }
    fn pid(&self) -> u32 {
        self.child.as_ref().expect("running").id()
    }
    /// SIGINT to the exact pid (the stop both sidecars handle: `ctrl_c`), then its exit code
    /// and whether the socket file is gone.
    fn interrupt(mut self) -> (Option<i32>, bool) {
        let mut child = self.child.take().expect("running");
        let pid = child.id();
        rustix::process::kill_process(
            rustix::process::Pid::from_raw(i32::try_from(pid).unwrap()).unwrap(),
            rustix::process::Signal::INT,
        )
        .expect("SIGINT the exact pid");
        let started = Instant::now();
        let status = loop {
            if let Some(status) = child.try_wait().expect("wait") {
                break Some(status);
            }
            if started.elapsed() > slice() {
                child.kill().expect("kill the stuck sidecar");
                child.wait().expect("reap");
                break None;
            }
            std::thread::sleep(pause(10));
        };
        assert!(gone(pid), "sidecar {pid} outlived its reap");
        for reader in std::mem::take(&mut self.readers) {
            reader.join().expect("reader thread");
        }
        (status.and_then(|s| s.code()), !self.side.socket.exists())
    }
    /// Stops the exact pid and proves it gone.
    fn finish(mut self) -> Vec<String> {
        let mut child = self.child.take().expect("running");
        let pid = child.id();
        child.kill().expect("kill the exact oracle pid");
        child.wait().expect("reap the oracle");
        assert!(gone(pid), "oracle {pid} outlived its reap");
        // The pipes closed with the process: both readers end.
        for reader in std::mem::take(&mut self.readers) {
            reader.join().expect("reader thread");
        }
        self.stderr.lock().unwrap().clone()
    }
}
impl Drop for OracleProcess {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            if let Err(error) = child.kill() {
                eprintln!("oracle {} not killed on drop: {error}", child.id());
            }
            if let Err(error) = child.wait() {
                eprintln!("oracle {} not reaped on drop: {error}", child.id());
            }
        }
        for reader in std::mem::take(&mut self.readers) {
            if reader.join().is_err() {
                eprintln!("oracle reader thread panicked");
            }
        }
    }
}
fn gone(pid: u32) -> bool {
    let pid = rustix::process::Pid::from_raw(i32::try_from(pid).unwrap()).unwrap();
    matches!(
        rustix::process::test_kill_process(pid),
        Err(rustix::io::Errno::SRCH)
    )
}

/// The Rust sidecar: `PooledRouter` + the Unix-socket local server, in this process.
struct RustSidecar<'a> {
    host: LoopbackHost<'a>,
    server: Arc<LocalServer<Mono>>,
    stop: Option<tokio::sync::oneshot::Sender<()>>,
    task: Option<tokio::task::JoinHandle<()>>,
    side: Side,
    dir: PathBuf,
}
fn limits() -> DriverLimits {
    DriverLimits {
        pool: PoolLimits {
            capacity: 4,
            seen_factor: 8,
            seen_floor: 64,
            initial_retry: ms(100),
            max_retry: ms(5_000),
            lifetime: ms(300_000),
        },
        connect: ms(5_000),
        stream_io: ms(60_000),
    }
}
impl<'a> RustSidecar<'a> {
    fn start(rt: &'a tokio::runtime::Runtime, clock: &Arc<Mono>, label: &str) -> Self {
        let deadline = clock.monotonic().checked_add(ms(60_000)).unwrap();
        let host = LoopbackHost::bind(rt, &**clock, deadline).unwrap();
        let address = host.address().unwrap();
        let server = Arc::new(LocalServer {
            router: Arc::new(
                PooledRouter::start_with(&host, clock.clone(), limits(), Services::default())
                    .unwrap(),
            ),
            secret: parse_bootstrap_secret(std::io::Cursor::new(RUST_SECRET)).unwrap(),
            endpoint_id: address.id.to_string(),
            limits: SessionLimits {
                handshake: ms(2_000),
                receive_poll: ms(250),
                control: ControlCallLimits {
                    ceiling_millis: 60_000,
                    max_command_bytes: 4 * 1024 * 1024,
                },
            },
            max_sessions: 16,
            events: Arc::new(|_| {}),
        });
        let dir = scratch(label);
        let socket = dir.join("s");
        let _guard = rt.enter();
        let listener = bind_secure_uds(&socket).unwrap();
        let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
        let served = server.clone();
        let task = rt.spawn(async move {
            let result = serve_unix_listener(&listener, &served, async {
                if stopped.await.is_err() {
                    eprintln!("tolerated: stop sender dropped");
                }
            })
            .await;
            assert!(result.is_ok());
        });
        Self {
            host,
            server,
            stop: Some(stop),
            task: Some(task),
            side: Side {
                socket,
                secret: RUST_SECRET,
                address,
                name: "rust",
            },
            dir,
        }
    }
    fn finish(mut self, rt: &tokio::runtime::Runtime, clock: &Mono) {
        self.stop.take().unwrap().send(()).unwrap();
        rt.block_on(self.task.take().unwrap()).unwrap();
        rt.block_on(self.server.router.shutdown());
        let deadline = clock.monotonic().checked_add(ms(60_000)).unwrap();
        let Self {
            mut host,
            server,
            dir,
            ..
        } = self;
        drop(server);
        host.close(clock, deadline).unwrap();
        if let Err(error) = std::fs::remove_dir_all(dir) {
            eprintln!("tolerated remove_dir_all {error:?}");
        }
    }
}

/// Which Rust sidecar answers: assembled in this process, or the `mycelium-rs-sidecar` entry
/// point started as an exact child exactly like the oracle binary.
#[derive(Clone, Copy, Debug)]
enum Mode {
    InProcess,
    EntryPoint,
}
enum RustHalf<'a> {
    InProcess(RustSidecar<'a>),
    Child(OracleProcess),
}
impl<'a> RustHalf<'a> {
    fn start(mode: Mode, rt: &'a tokio::runtime::Runtime, clock: &Arc<Mono>, label: &str) -> Self {
        match mode {
            Mode::InProcess => Self::InProcess(RustSidecar::start(rt, clock, label)),
            Mode::EntryPoint => Self::Child(OracleProcess::start_binary(
                label,
                PathBuf::from(env!("CARGO_BIN_EXE_mycelium-rs-sidecar")),
                RUST_SECRET,
                "rust",
            )),
        }
    }
    fn side(&self) -> &Side {
        match self {
            Self::InProcess(rust) => &rust.side,
            Self::Child(child) => &child.side,
        }
    }
    fn finish(self, rt: &tokio::runtime::Runtime, clock: &Mono) {
        match self {
            Self::InProcess(rust) => rust.finish(rt, clock),
            Self::Child(child) => {
                let pid = child.pid();
                let stderr = child.finish();
                assert!(gone(pid), "entry point {pid} outlived its reap");
                assert!(
                    stderr.iter().all(|line| !line.contains("fatal")),
                    "entry point stderr {stderr:?}"
                );
            }
        }
    }
}

// ---- scenarios -------------------------------------------------------------------------

fn the_oracle_binary_serves_the_rust_client_codec_and_refuses_what_the_rust_server_refuses_body(
    mode: Mode,
) {
    let _ = NEEDS;
    let rt = runtime();
    let clock = Arc::new(Mono(Instant::now()));
    let oracle = OracleProcess::start("hs-o");
    let rust = RustHalf::start(mode, &rt, &clock, "hs-r");
    rt.block_on(async {
        for side in [&oracle.side, rust.side()] {
            let mut client = LocalClient::connect(&side.socket, &side.secret).await;
            assert_eq!(
                client.endpoint,
                side.address.id.to_string(),
                "{}",
                side.name
            );
            let ack = client.call(RecordKind::Ping, id(9), b"").await;
            assert_eq!(
                (ack.kind, ack.message_id),
                (RecordKind::Ack, id(9)),
                "{}",
                side.name
            );
            assert!(ack.payload.is_empty(), "{}", side.name);
            // A wrong secret never receives a server hello (both close the connection).
            assert!(
                LocalClient::try_connect(&side.socket, &[0x77; 32])
                    .await
                    .is_none(),
                "{} answered a wrong proof",
                side.name
            );
            // A record signed with the wrong key ends the session without an answer.
            let mut forged = LocalClient::connect(&side.socket, &side.secret).await;
            forged.keys.client_to_sidecar = [0x13; 32];
            let encoded = encode_record(
                RecordKind::Ping,
                0,
                id(1),
                b"",
                &forged.keys.client_to_sidecar,
            )
            .unwrap();
            forged.stream.write_u32(encoded.len() as u32).await.unwrap();
            forged.stream.write_all(&encoded).await.unwrap();
            assert!(
                tokio::time::timeout(slice(), read_prefixed(&mut forged.stream))
                    .await
                    .unwrap()
                    .is_none(),
                "{} answered a forged record",
                side.name
            );
        }
    });
    let pid = oracle.pid();
    let stderr = oracle.finish();
    assert!(gone(pid));
    assert!(
        stderr.iter().all(|line| !line.contains("fatal")),
        "oracle stderr {stderr:?}"
    );
    rust.finish(&rt, &clock);
}

/// The unconfigured-sidecar battery: the same local requests, identical replies.
fn battery() -> Vec<(RecordKind, [u8; 16], Vec<u8>)> {
    let (frame, _) = valid_frames();
    let other = RecordKind::Delivery;
    vec![
        (RecordKind::Ping, id(1), vec![]),
        (RecordKind::Send, id(2), b"junk".to_vec()),
        (RecordKind::Send, id(3), frame.clone()),
        (RecordKind::SendConfirmed, id(4), vec![0; 15]),
        (
            RecordKind::SendConfirmed,
            id(5),
            [
                0_u64.to_be_bytes().as_slice(),
                1_u64.to_be_bytes().as_slice(),
                &frame,
            ]
            .concat(),
        ),
        (
            RecordKind::SendConfirmed,
            id(6),
            [
                1_u64.to_be_bytes().as_slice(),
                0_u64.to_be_bytes().as_slice(),
                &frame,
            ]
            .concat(),
        ),
        (RecordKind::SendConfirmed, id(7), confirmed(b"junk")),
        (RecordKind::SendRouted, id(8), vec![0; 10]),
        (
            RecordKind::SendRouted,
            id(9),
            [vec![0xff; 32], confirmed(&frame)].concat(),
        ),
        (RecordKind::Receive, id(10), vec![]),
        (RecordKind::ReceiveFrom, id(11), vec![]),
        (RecordKind::Ack, id(12), vec![]),
        (RecordKind::Cancel, id(13), vec![]),
        (RecordKind::ConfigurePeer, id(14), b"{}".to_vec()),
        (RecordKind::ConfigurePeer, id(15), b"not json".to_vec()),
        (
            RecordKind::ConfigurePeers,
            id(16),
            b"{\"peers\":[]}".to_vec(),
        ),
        (
            RecordKind::ConfigurePeers,
            id(17),
            b"{\"peers\":[{\"endpoint_id\":\"x\"}]}".to_vec(),
        ),
        (
            RecordKind::ConfigurePeers,
            id(18),
            b"{\"peers\":[],\"extra\":1}".to_vec(),
        ),
        (other, id(19), vec![]),
        (RecordKind::TransportObservations, id(20), vec![]),
        (RecordKind::GetTransportObservations, id(21), vec![]),
        (RecordKind::GetAdmissionCounters, id(22), vec![]),
        (RecordKind::GetAdmissionCounters, id(23), vec![0; 32]),
        (RecordKind::ControlCall, id(24), b"junk".to_vec()),
        (RecordKind::ControlCall, id(25), vec![]),
        (RecordKind::Ping, id(26), vec![]),
    ]
}

fn local_replies_of_the_oracle_and_the_rust_sidecar_are_identical_for_the_same_requests_body(
    mode: Mode,
) {
    let rt = runtime();
    let clock = Arc::new(Mono(Instant::now()));
    let oracle = OracleProcess::start("bat-o");
    let rust = RustHalf::start(mode, &rt, &clock, "bat-r");
    let mut transcripts = Vec::new();
    rt.block_on(async {
        for side in [&oracle.side, rust.side()] {
            let mut client = LocalClient::connect(&side.socket, &side.secret).await;
            let mut transcript = Vec::new();
            for (kind, message_id, payload) in battery() {
                let reply = client.call(kind, message_id, &payload).await;
                transcript.push((
                    kind,
                    message_id,
                    reply.kind,
                    reply.message_id,
                    reply.payload,
                ));
            }
            transcripts.push(transcript);
        }
    });
    let (oracle_replies, rust_replies) = (&transcripts[0], &transcripts[1]);
    assert_eq!(oracle_replies.len(), battery().len());
    for (index, (o, r)) in oracle_replies.iter().zip(rust_replies).enumerate() {
        assert_eq!(
            o,
            r,
            "request {index} ({:?}): oracle reply {:?}/{:?} vs rust {:?}/{:?}",
            o.0,
            o.2,
            String::from_utf8_lossy(&o.4),
            r.2,
            String::from_utf8_lossy(&r.4)
        );
    }
    for (kind, _, reply, _, payload) in oracle_replies {
        eprintln!(
            "L9_BATTERY {kind:?} -> {reply:?} {}",
            String::from_utf8_lossy(payload)
        );
    }
    oracle.finish();
    rust.finish(&rt, &clock);
}

/// `sender` -> `receiver` over QUIC, driven only by local-IPC records.
async fn cross(sender: &Side, receiver: &Side, receive: RecordKind, frame: &[u8], n: u8) {
    let mut from = LocalClient::connect(&sender.socket, &sender.secret).await;
    let mut to = LocalClient::connect(&receiver.socket, &receiver.secret).await;
    let mut waiting = LocalClient::connect(&sender.socket, &sender.secret).await;
    let sent = frame.to_vec();
    let pending = tokio::spawn(async move {
        waiting
            .call(RecordKind::SendConfirmed, id(n), &confirmed(&sent))
            .await
    });
    let delivery = to.delivery(receive).await;
    assert_eq!(
        delivery.message_id,
        id(n),
        "{} -> {}",
        sender.name,
        receiver.name
    );
    let (head, body) = match receive {
        RecordKind::Receive => (8, 0),
        _ => (32 + 8, 32),
    };
    assert_eq!(
        &delivery.payload[body..head],
        1_u64.to_be_bytes(),
        "the receiver's table generation of the sender"
    );
    assert_eq!(
        &delivery.payload[head..],
        frame,
        "frame bytes are untouched"
    );
    if receive == RecordKind::ReceiveFrom {
        assert_eq!(
            &delivery.payload[..32],
            sender.address.id.as_bytes(),
            "source endpoint"
        );
    }
    // The sender's confirmed send must not complete before the receiver's local ack.
    assert!(
        tokio::time::timeout(pause(500), async {
            while !pending.is_finished() {
                tokio::task::yield_now().await;
            }
        })
        .await
        .is_err(),
        "{} -> {}: confirmed before the local ack",
        sender.name,
        receiver.name
    );
    let ack = to.call(RecordKind::Ack, id(n), b"").await;
    assert_eq!(ack.kind, RecordKind::Ack, "{ack:?}");
    let done = tokio::time::timeout(slice(), pending)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        (done.kind, done.message_id),
        (RecordKind::Ack, id(n)),
        "{} -> {}: {:?}",
        sender.name,
        receiver.name,
        String::from_utf8_lossy(&done.payload)
    );
    // An acknowledged id is not deliverable twice.
    let again = to.call(RecordKind::Ack, id(n), b"").await;
    assert_eq!(code(&again), "unknown_delivery");
    drop(from.call(RecordKind::Ping, id(0), b"").await);
}

fn router_frames_cross_between_the_oracle_binary_and_the_pooled_router_in_both_directions_body(
    mode: Mode,
) {
    let (frame, second) = valid_frames();
    let rt = runtime();
    let clock = Arc::new(Mono(Instant::now()));
    let oracle = OracleProcess::start("x-o");
    let rust = RustHalf::start(mode, &rt, &clock, "x-r");
    rt.block_on(async {
        for (side, other) in [(&oracle.side, rust.side()), (rust.side(), &oracle.side)] {
            let mut client = LocalClient::connect(&side.socket, &side.secret).await;
            let reply = client
                .call(
                    RecordKind::ConfigurePeers,
                    id(1),
                    &peers(&[(&other.address, 1)]),
                )
                .await;
            assert_eq!(reply.kind, RecordKind::Ack, "{}: {:?}", side.name, reply);
        }
        cross(
            &oracle.side,
            rust.side(),
            RecordKind::ReceiveFrom,
            &frame,
            10,
        )
        .await;
        cross(rust.side(), &oracle.side, RecordKind::Receive, &frame, 11).await;
        cross(&oracle.side, rust.side(), RecordKind::Receive, &second, 12).await;
        cross(
            rust.side(),
            &oracle.side,
            RecordKind::ReceiveFrom,
            &second,
            13,
        )
        .await;
        // Both sides report the traffic as transport observations for the other endpoint.
        let mut documents = Vec::new();
        for (side, other) in [(&oracle.side, rust.side()), (rust.side(), &oracle.side)] {
            let mut client = LocalClient::connect(&side.socket, &side.secret).await;
            let reply = client
                .call(RecordKind::GetTransportObservations, id(2), b"")
                .await;
            assert_eq!(
                reply.kind,
                RecordKind::TransportObservations,
                "{}",
                side.name
            );
            let document: Value = serde_json::from_slice(&reply.payload).unwrap();
            eprintln!("L9_OBSERVATIONS {} {document}", side.name);
            let text = document.to_string();
            assert!(
                text.contains(&other.address.id.to_string()),
                "{} observations name the peer: {text}",
                side.name
            );
            documents.push(document);
        }
        let shape = |value: &Value| -> Vec<String> {
            fn walk(value: &Value, path: String, out: &mut Vec<String>) {
                match value {
                    Value::Object(map) => {
                        for (key, inner) in map {
                            // Peer ids differ per side; the shape is the structure below them.
                            let key = if key.len() == 64 { "<endpoint>" } else { key };
                            walk(inner, format!("{path}/{key}"), out);
                        }
                    }
                    Value::Array(items) => {
                        for inner in items {
                            walk(inner, format!("{path}[]"), out);
                        }
                    }
                    other => out.push(format!(
                        "{path}:{}",
                        match other {
                            Value::String(_) => "string",
                            Value::Number(_) => "number",
                            Value::Bool(_) => "bool",
                            _ => "null",
                        }
                    )),
                }
            }
            let mut out = Vec::new();
            walk(value, String::new(), &mut out);
            out.sort();
            out.dedup();
            out
        };
        assert_eq!(
            shape(&documents[0]),
            shape(&documents[1]),
            "the observation document has the oracle's structure"
        );
    });
    oracle.finish();
    rust.finish(&rt, &clock);
}

/// A confirmed send still waiting for the receiver's local ack ends `peer_rotated` when its
/// sender republishes the peer with a new generation (and the dead generation is refused).
async fn rotation(sender: &Side, receiver: &Side, frame: &[u8], n: u8) {
    let mut control = LocalClient::connect(&sender.socket, &sender.secret).await;
    let mut waiting = LocalClient::connect(&sender.socket, &sender.secret).await;
    let sent = frame.to_vec();
    let pending = tokio::spawn(async move {
        waiting
            .call(RecordKind::SendConfirmed, id(n), &confirmed(&sent))
            .await
    });
    // The frame reaches the receiver, which never acknowledges it.
    let mut to = LocalClient::connect(&receiver.socket, &receiver.secret).await;
    let delivery = to.delivery(RecordKind::Receive).await;
    assert_eq!(delivery.message_id, id(n));
    assert!(!pending.is_finished());
    let rotated = control
        .call(
            RecordKind::ConfigurePeers,
            id(40),
            &peers(&[(&receiver.address, 2)]),
        )
        .await;
    assert_eq!(rotated.kind, RecordKind::Ack, "{rotated:?}");
    let done = tokio::time::timeout(slice(), pending)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        code(&done),
        "peer_rotated",
        "{} -> {}",
        sender.name,
        receiver.name
    );
    // The old generation is no longer routable.
    let stale = control
        .call(RecordKind::SendConfirmed, id(n + 1), &confirmed(frame))
        .await;
    assert_eq!(
        code(&stale),
        "peer_rotated",
        "{} -> {}",
        sender.name,
        receiver.name
    );
}

fn a_rotation_ends_a_pending_confirmed_send_with_the_same_code_on_both_sidecars_body(mode: Mode) {
    let (frame, _) = valid_frames();
    let rt = runtime();
    let clock = Arc::new(Mono(Instant::now()));
    // A fresh pair per direction: peer generations only grow.
    for oracle_sends in [true, false] {
        let label = if oracle_sends { "a" } else { "b" };
        let oracle = OracleProcess::start(&format!("rot-o{label}"));
        let rust = RustHalf::start(mode, &rt, &clock, &format!("rot-r{label}"));
        rt.block_on(async {
            for (side, other) in [(&oracle.side, rust.side()), (rust.side(), &oracle.side)] {
                let mut client = LocalClient::connect(&side.socket, &side.secret).await;
                let reply = client
                    .call(
                        RecordKind::ConfigurePeers,
                        id(1),
                        &peers(&[(&other.address, 1)]),
                    )
                    .await;
                assert_eq!(reply.kind, RecordKind::Ack);
            }
            if oracle_sends {
                rotation(&oracle.side, rust.side(), &frame, 50).await;
            } else {
                rotation(rust.side(), &oracle.side, &frame, 50).await;
            }
        });
        oracle.finish();
        rust.finish(&rt, &clock);
    }
}

/// One id, one frame, three senders: the duplicate shares the pending item (both confirm after
/// the single local ack), and the same id with a different frame is a `replay_collision`.
async fn duplicates(sender: &Side, receiver: &Side, first: &[u8], other: &[u8], n: u8) {
    let spawn_send = |frame: Vec<u8>| {
        let (socket, secret) = (sender.socket.clone(), sender.secret);
        tokio::spawn(async move {
            let mut client = LocalClient::connect(&socket, &secret).await;
            client
                .call(RecordKind::SendConfirmed, id(n), &confirmed(&frame))
                .await
        })
    };
    let mut to = LocalClient::connect(&receiver.socket, &receiver.secret).await;
    let one = spawn_send(first.to_vec());
    let delivery = to.delivery(RecordKind::Receive).await;
    assert_eq!(delivery.message_id, id(n));
    let two = spawn_send(first.to_vec());
    let mut other_client = LocalClient::connect(&sender.socket, &sender.secret).await;
    let collision = other_client
        .call(RecordKind::SendConfirmed, id(n), &confirmed(other))
        .await;
    assert_eq!(
        code(&collision),
        "replay_collision",
        "{} -> {}",
        sender.name,
        receiver.name
    );
    assert!(!one.is_finished() && !two.is_finished());
    assert_eq!(
        to.call(RecordKind::Ack, id(n), b"").await.kind,
        RecordKind::Ack
    );
    for pending in [one, two] {
        let done = tokio::time::timeout(slice(), pending)
            .await
            .unwrap()
            .unwrap();
        assert_eq!(
            done.kind,
            RecordKind::Ack,
            "{} -> {}: {:?}",
            sender.name,
            receiver.name,
            String::from_utf8_lossy(&done.payload)
        );
    }
}

fn a_duplicate_shares_the_pending_item_and_a_different_frame_under_its_id_collides_on_both_sidecars_body(
    mode: Mode,
) {
    let (frame, second) = valid_frames();
    let rt = runtime();
    let clock = Arc::new(Mono(Instant::now()));
    let oracle = OracleProcess::start("dup-o");
    let rust = RustHalf::start(mode, &rt, &clock, "dup-r");
    rt.block_on(async {
        for (side, other) in [(&oracle.side, rust.side()), (rust.side(), &oracle.side)] {
            let mut client = LocalClient::connect(&side.socket, &side.secret).await;
            let reply = client
                .call(
                    RecordKind::ConfigurePeers,
                    id(1),
                    &peers(&[(&other.address, 1)]),
                )
                .await;
            assert_eq!(reply.kind, RecordKind::Ack);
        }
        duplicates(&oracle.side, rust.side(), &frame, &second, 60).await;
        duplicates(rust.side(), &oracle.side, &frame, &second, 61).await;
    });
    oracle.finish();
    rust.finish(&rt, &clock);
}

#[test]
#[ignore = "needs L9_ORACLE_SIDECAR=<oracle mycelium-iroh-sidecar @ f4e366e9>; run with --ignored"]
fn the_oracle_binary_serves_the_rust_client_codec_and_refuses_what_the_rust_server_refuses() {
    the_oracle_binary_serves_the_rust_client_codec_and_refuses_what_the_rust_server_refuses_body(
        Mode::InProcess,
    );
}
#[test]
#[ignore = "needs L9_ORACLE_SIDECAR=<oracle mycelium-iroh-sidecar @ f4e366e9>; run with --ignored"]
fn the_oracle_binary_serves_the_rust_client_codec_and_refuses_what_the_rust_server_refuses_with_the_entry_point_binary()
 {
    the_oracle_binary_serves_the_rust_client_codec_and_refuses_what_the_rust_server_refuses_body(
        Mode::EntryPoint,
    );
}

#[test]
#[ignore = "needs L9_ORACLE_SIDECAR=<oracle mycelium-iroh-sidecar @ f4e366e9>; run with --ignored"]
fn local_replies_of_the_oracle_and_the_rust_sidecar_are_identical_for_the_same_requests() {
    local_replies_of_the_oracle_and_the_rust_sidecar_are_identical_for_the_same_requests_body(
        Mode::InProcess,
    );
}
#[test]
#[ignore = "needs L9_ORACLE_SIDECAR=<oracle mycelium-iroh-sidecar @ f4e366e9>; run with --ignored"]
fn local_replies_of_the_oracle_and_the_rust_sidecar_are_identical_for_the_same_requests_with_the_entry_point_binary()
 {
    local_replies_of_the_oracle_and_the_rust_sidecar_are_identical_for_the_same_requests_body(
        Mode::EntryPoint,
    );
}

#[test]
#[ignore = "needs L9_ORACLE_SIDECAR=<oracle mycelium-iroh-sidecar @ f4e366e9>; run with --ignored"]
fn router_frames_cross_between_the_oracle_binary_and_the_pooled_router_in_both_directions() {
    router_frames_cross_between_the_oracle_binary_and_the_pooled_router_in_both_directions_body(
        Mode::InProcess,
    );
}
#[test]
#[ignore = "needs L9_ORACLE_SIDECAR=<oracle mycelium-iroh-sidecar @ f4e366e9>; run with --ignored"]
fn router_frames_cross_between_the_oracle_binary_and_the_pooled_router_in_both_directions_with_the_entry_point_binary()
 {
    router_frames_cross_between_the_oracle_binary_and_the_pooled_router_in_both_directions_body(
        Mode::EntryPoint,
    );
}

#[test]
#[ignore = "needs L9_ORACLE_SIDECAR=<oracle mycelium-iroh-sidecar @ f4e366e9>; run with --ignored"]
fn a_rotation_ends_a_pending_confirmed_send_with_the_same_code_on_both_sidecars() {
    a_rotation_ends_a_pending_confirmed_send_with_the_same_code_on_both_sidecars_body(
        Mode::InProcess,
    );
}
#[test]
#[ignore = "needs L9_ORACLE_SIDECAR=<oracle mycelium-iroh-sidecar @ f4e366e9>; run with --ignored"]
fn a_rotation_ends_a_pending_confirmed_send_with_the_same_code_on_both_sidecars_with_the_entry_point_binary()
 {
    a_rotation_ends_a_pending_confirmed_send_with_the_same_code_on_both_sidecars_body(
        Mode::EntryPoint,
    );
}

#[test]
#[ignore = "needs L9_ORACLE_SIDECAR=<oracle mycelium-iroh-sidecar @ f4e366e9>; run with --ignored"]
fn a_duplicate_shares_the_pending_item_and_a_different_frame_under_its_id_collides_on_both_sidecars()
 {
    a_duplicate_shares_the_pending_item_and_a_different_frame_under_its_id_collides_on_both_sidecars_body(Mode::InProcess);
}
#[test]
#[ignore = "needs L9_ORACLE_SIDECAR=<oracle mycelium-iroh-sidecar @ f4e366e9>; run with --ignored"]
fn a_duplicate_shares_the_pending_item_and_a_different_frame_under_its_id_collides_on_both_sidecars_with_the_entry_point_binary()
 {
    a_duplicate_shares_the_pending_item_and_a_different_frame_under_its_id_collides_on_both_sidecars_body(Mode::EntryPoint);
}

#[test]
#[ignore = "needs L9_ORACLE_SIDECAR=<oracle mycelium-iroh-sidecar @ f4e366e9>; run with --ignored"]
fn sigint_stops_the_oracle_and_the_entry_point_with_exit_0_and_no_socket_left() {
    let oracle = OracleProcess::start("sigint-o");
    let rust = OracleProcess::start_binary(
        "sigint-r",
        PathBuf::from(env!("CARGO_BIN_EXE_mycelium-rs-sidecar")),
        RUST_SECRET,
        "rust",
    );
    for (name, process) in [("oracle", oracle), ("rust entry point", rust)] {
        assert!(
            process.side.socket.exists(),
            "{name}: socket bound while serving"
        );
        let (code, socket_gone) = process.interrupt();
        assert_eq!(code, Some(0), "{name}: clean stop");
        assert!(socket_gone, "{name}: the socket file was removed");
    }
}
