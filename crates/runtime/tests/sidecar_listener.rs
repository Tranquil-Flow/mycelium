// SPDX-License-Identifier: AGPL-3.0-only
//! Listener loops of the local channel: an owner-only Unix socket and the pid-authorized
//! loopback channel, with the 16-session cap semantics (a slot is taken before accepting),
//! peer rejection before any handshake byte, and two-process runs (an exact-owned child of
//! this test binary is the local client; another receives the bootstrap secret on its
//! inherited stdin pipe). The listener loops were written before these tests; their evidence
//! is these tests plus the recorded mutants (`r9-listener-mutations.txt`).
#![cfg(all(feature = "iroh-adapter", unix))]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant, Span};
use runtime::{
    iroh_host::LoopbackHost,
    iroh_pool::{DriverLimits, PooledRouter, Services},
    sidecar_listener::{LocalServer, serve_loopback_listener, serve_unix_listener},
    sidecar_local::{
        ControlCallLimits, RecordKind, SequenceGuard, client_hello, decode_record,
        derive_session_keys, encode_record, verify_server_hello,
    },
    sidecar_pool::PoolLimits,
    sidecar_process::{
        BootstrapSecret, ConnectionOwnerTable, LoopbackListener, TcpOwnerRow, bind_secure_uds,
        parse_bootstrap_secret, read_bootstrap_secret,
    },
    sidecar_session::SessionLimits,
};
use std::{
    io::{BufRead, BufReader, Write},
    net::{Ipv4Addr, SocketAddr, SocketAddrV4},
    os::unix::fs::PermissionsExt,
    process::{Command, Stdio},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt},
    net::{TcpSocket, UnixStream},
};
use transport::Clock;

include!("support/fork_lock.rs");

const SECRET: [u8; 32] = [0x6b; 32];
const ENDPOINT: &str = "listener-endpoint";

struct Mono(Instant);
impl Clock for Mono {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(ClockDomain(34), self.0.elapsed().as_millis() as u64)
    }
    fn wall_seconds(&self) -> f64 {
        panic!("the listener must not consult wall time")
    }
}
fn ms(value: u64) -> Span {
    Span::from_millis(value)
}
fn slice() -> Duration {
    Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis() * 4)
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
fn session_limits() -> SessionLimits {
    SessionLimits {
        handshake: ms(2_000),
        receive_poll: ms(250),
        control: ControlCallLimits {
            ceiling_millis: 60_000,
            max_command_bytes: 4 * 1024 * 1024,
        },
    }
}
fn driver_limits() -> DriverLimits {
    DriverLimits {
        pool: PoolLimits {
            capacity: 2,
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
fn scratch(label: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("sl{}-{label}", std::process::id()));
    observed("std::fs::remove_dir_all", std::fs::remove_dir_all(&dir));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

struct Rig {
    rt: tokio::runtime::Runtime,
    clock: Arc<Mono>,
}
impl Rig {
    fn new() -> Self {
        Self {
            rt: runtime(),
            clock: Arc::new(Mono(Instant::now())),
        }
    }
    fn deadline(&self) -> MonoInstant {
        self.clock.monotonic().checked_add(ms(60_000)).unwrap()
    }
    fn server(
        &self,
        host: &LoopbackHost<'_>,
        max_sessions: usize,
        events: Arc<Mutex<Vec<&'static str>>>,
    ) -> LocalServer<Mono> {
        LocalServer {
            router: Arc::new(
                PooledRouter::start_with(
                    host,
                    self.clock.clone(),
                    driver_limits(),
                    Services::default(),
                )
                .unwrap(),
            ),
            secret: parse_bootstrap_secret(std::io::Cursor::new(SECRET)).unwrap(),
            endpoint_id: ENDPOINT.to_owned(),
            limits: session_limits(),
            max_sessions,
            events: Arc::new(move |event| events.lock().unwrap().push(event)),
        }
    }
}

async fn write_prefixed<S: AsyncWrite + Unpin>(stream: &mut S, bytes: &[u8]) {
    stream.write_u32(bytes.len() as u32).await.unwrap();
    stream.write_all(bytes).await.unwrap();
}
async fn read_prefixed<S: AsyncRead + Unpin>(stream: &mut S) -> Option<Vec<u8>> {
    let length = stream.read_u32().await.ok()? as usize;
    let mut bytes = vec![0; length];
    stream.read_exact(&mut bytes).await.ok()?;
    Some(bytes)
}
/// A client that sent its hello and may still be waiting for the sidecar's reply.
struct Pending {
    client_nonce: [u8; 32],
    reply: Option<Vec<u8>>,
}
async fn send_hello<S: AsyncWrite + Unpin>(stream: &mut S) -> Pending {
    let client_nonce = [0x21_u8; 32];
    write_prefixed(stream, &client_hello(&SECRET, &client_nonce).unwrap()).await;
    Pending {
        client_nonce,
        reply: None,
    }
}
/// Waits up to `within` for the hello reply; true once the session answers a Ping with an Ack.
async fn finish<S: AsyncRead + AsyncWrite + Unpin>(
    stream: &mut S,
    pending: &mut Pending,
    within: Duration,
) -> bool {
    if pending.reply.is_none() {
        match tokio::time::timeout(within, read_prefixed(stream)).await {
            Ok(Some(reply)) => pending.reply = Some(reply),
            _ => return false,
        }
    }
    let reply = pending.reply.as_ref().unwrap();
    let (server_nonce, endpoint) =
        verify_server_hello(reply, &SECRET, &pending.client_nonce).unwrap();
    assert_eq!(endpoint, ENDPOINT);
    let keys = derive_session_keys(&SECRET, &pending.client_nonce, &server_nonce).unwrap();
    let ping = encode_record(RecordKind::Ping, 0, [9; 16], b"", &keys.client_to_sidecar).unwrap();
    write_prefixed(stream, &ping).await;
    let reply = read_prefixed(stream).await.unwrap();
    let record = decode_record(&reply, &keys.sidecar_to_client, &mut SequenceGuard::new()).unwrap();
    record.kind == RecordKind::Ack && record.message_id == [9; 16]
}
async fn handshake_and_ping<S: AsyncRead + AsyncWrite + Unpin>(
    stream: &mut S,
    within: Duration,
) -> bool {
    let mut pending = send_hello(stream).await;
    finish(stream, &mut pending, within).await
}

#[test]
fn a_unix_socket_session_runs_and_the_session_cap_holds_back_the_next_connection_until_a_slot_frees()
 {
    let rig = Rig::new();
    let mut host = LoopbackHost::bind(&rig.rt, &*rig.clock, rig.deadline()).unwrap();
    let events = Arc::new(Mutex::new(Vec::new()));
    let server = Arc::new(rig.server(&host, 1, events.clone()));
    let dir = scratch("uds");
    let socket = dir.join("s");
    let _guard = rig.rt.enter();
    let listener = bind_secure_uds(&socket).unwrap();
    assert_eq!(
        std::fs::symlink_metadata(&socket)
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    let (stop_tx, stop_rx) = tokio::sync::oneshot::channel::<()>();
    let loop_server = server.clone();
    let task = rig.rt.spawn(async move {
        serve_unix_listener(&listener, &loop_server, async {
            observed("stop_rx.await", stop_rx.await);
        })
        .await
    });
    rig.rt.block_on(async {
        // Session A holds the only slot (it stays connected after its ping).
        let mut a = UnixStream::connect(&socket).await.unwrap();
        assert!(
            handshake_and_ping(&mut a, slice()).await,
            "session A served"
        );
        // Session B is accepted by the kernel (backlog) but the loop does not serve it: it
        // takes a slot BEFORE accepting, so B's hello stays unanswered.
        let mut b = UnixStream::connect(&socket).await.unwrap();
        let mut pending = send_hello(&mut b).await;
        assert!(
            !finish(&mut b, &mut pending, pause(600)).await,
            "B must not be served while A holds the only slot"
        );
        // A hangs up: the freed slot lets the loop accept B and answer its waiting hello.
        drop(a);
        assert!(
            finish(&mut b, &mut pending, slice()).await,
            "B served once A freed the slot"
        );
    });
    stop_tx.send(()).unwrap();
    assert_eq!(rig.rt.block_on(task).unwrap(), Ok(()));
    assert!(
        events.lock().unwrap().is_empty(),
        "same-uid peers are never rejected"
    );
    drop(_guard);
    rig.rt.block_on(server.router.shutdown());
    drop(server);
    host.close(&*rig.clock, rig.deadline()).unwrap();
    observed("std::fs::remove_dir_all", std::fs::remove_dir_all(dir));
}

struct OwnerOf(Mutex<Vec<TcpOwnerRow>>);
impl ConnectionOwnerTable for OwnerOf {
    fn rows(&self) -> std::io::Result<Vec<TcpOwnerRow>> {
        Ok(self.0.lock().unwrap().clone())
    }
}

#[test]
fn the_loopback_channel_serves_only_the_allowed_pid_and_rejects_everything_else_before_any_byte() {
    let rig = Rig::new();
    let mut host = LoopbackHost::bind(&rig.rt, &*rig.clock, rig.deadline()).unwrap();
    let events = Arc::new(Mutex::new(Vec::new()));
    let server = Arc::new(rig.server(&host, 4, events.clone()));
    let _guard = rig.rt.enter();
    let listener = Arc::new(LoopbackListener::bind().unwrap());
    let server_addr = listener.local_addr();
    let table = Arc::new(OwnerOf(Mutex::new(Vec::new())));
    let (stop_tx, stop_rx) = tokio::sync::oneshot::channel::<()>();
    let task = {
        let (listener, server, table) = (listener.clone(), server.clone(), table.clone());
        rig.rt.spawn(async move {
            serve_loopback_listener(&listener, table, 4242, &server, async {
                observed("stop_rx.await", stop_rx.await);
            })
            .await
        })
    };
    let connect = |pid: Option<u32>| {
        let table = table.clone();
        async move {
            let socket = TcpSocket::new_v4().unwrap();
            socket
                .bind(SocketAddr::V4(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0)))
                .unwrap();
            let SocketAddr::V4(local) = socket.local_addr().unwrap() else {
                unreachable!()
            };
            *table.0.lock().unwrap() = pid
                .map(|pid| {
                    vec![TcpOwnerRow {
                        local,
                        remote: server_addr,
                        owning_pid: pid,
                    }]
                })
                .unwrap_or_default();
            socket.connect(SocketAddr::V4(server_addr)).await.unwrap()
        }
    };
    rig.rt.block_on(async {
        let mut allowed = connect(Some(4242)).await;
        assert!(
            handshake_and_ping(&mut allowed, slice()).await,
            "allowed pid served"
        );
        drop(allowed);
        for (label, pid) in [("foreign pid", Some(7)), ("unknown owner", None)] {
            let mut rejected = connect(pid).await;
            let hello = client_hello(&SECRET, &[0x21; 32]).unwrap();
            // The server may already have dropped the connection: a failed write is fine.
            let framed = [(hello.len() as u32).to_be_bytes().as_slice(), &hello].concat();
            if rejected.write_all(&framed).await.is_err() {
                // Rejected before a single byte was read, which is the point.
            }
            // The server drops the connection without a single handshake byte.
            let mut byte = [0_u8; 1];
            let read = tokio::time::timeout(slice(), rejected.read(&mut byte))
                .await
                .unwrap_or_else(|_| panic!("{label}: connection not dropped"));
            assert!(
                matches!(read, Ok(0) | Err(_)),
                "{label}: no byte may be answered, got {read:?}"
            );
        }
        // A later allowed client is still served (rejections hold no slot).
        let mut again = connect(Some(4242)).await;
        assert!(handshake_and_ping(&mut again, slice()).await);
    });
    stop_tx.send(()).unwrap();
    assert_eq!(rig.rt.block_on(task).unwrap(), Ok(()));
    assert_eq!(
        *events.lock().unwrap(),
        ["local_peer_rejected", "local_peer_rejected"]
    );
    drop(_guard);
    rig.rt.block_on(server.router.shutdown());
    drop(server);
    drop(listener);
    host.close(&*rig.clock, rig.deadline()).unwrap();
}

// ---- two-process ----------------------------------------------------------------------

#[test]
fn listener_child_fixture() {
    let Ok(mode) = std::env::var("SL_CHILD") else {
        return;
    };
    match mode.as_str() {
        "uds-client" => {
            let socket = std::env::var("SL_SOCKET").unwrap();
            let rt = runtime();
            rt.block_on(async {
                let mut stream = UnixStream::connect(&socket).await.unwrap();
                let served = handshake_and_ping(&mut stream, slice()).await;
                println!("SL_CLIENT served={served} pid={}", std::process::id());
                std::io::stdout().flush().unwrap();
            });
        }
        "bootstrap-stdin" => {
            // stdin is the inherited pipe: the bootstrap secret never touches argv or env.
            use std::os::fd::AsFd as _;
            let inherited = std::io::stdin().as_fd().try_clone_to_owned().unwrap();
            let secret = read_bootstrap_secret(inherited);
            let line = match secret {
                Ok(secret) => format!(
                    "SL_BOOTSTRAP ok {}",
                    secret
                        .as_bytes()
                        .iter()
                        .map(|b| format!("{b:02x}"))
                        .collect::<String>()
                ),
                Err(error) => format!("SL_BOOTSTRAP err {}", error.code()),
            };
            println!("{line}");
            std::io::stdout().flush().unwrap();
        }
        other => panic!("unknown child mode {other}"),
    }
}

fn spawn_child(mode: &str, env: &[(&str, String)]) -> std::process::Child {
    let mut command = Command::new(std::env::current_exe().unwrap());
    command
        .args(["--exact", "listener_child_fixture", "--nocapture"])
        .env("SL_CHILD", mode)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    for (key, value) in env {
        command.env(key, value);
    }
    node::process::confine_descriptors(&mut command).unwrap();
    let _boundary = fixture_spawn_boundary();
    command.spawn().unwrap()
}
fn child_line(child: &mut std::process::Child, prefix: &str) -> String {
    let output = child.stdout.take().unwrap();
    let mut found = None;
    // Drain to EOF: closing the pipe early would make the child's later prints fail.
    for line in BufReader::new(output).lines() {
        let line = line.unwrap();
        if found.is_none() {
            found = line.strip_prefix(prefix).map(str::to_owned);
        }
    }
    found.unwrap_or_else(|| panic!("child ended without a `{prefix}` line"))
}

#[test]
fn an_exact_owned_child_process_is_served_over_the_owner_only_socket() {
    let rig = Rig::new();
    let mut host = LoopbackHost::bind(&rig.rt, &*rig.clock, rig.deadline()).unwrap();
    let events = Arc::new(Mutex::new(Vec::new()));
    let server = Arc::new(rig.server(&host, 2, events.clone()));
    let dir = scratch("child");
    let socket = dir.join("s");
    let _guard = rig.rt.enter();
    let listener = bind_secure_uds(&socket).unwrap();
    let (stop_tx, stop_rx) = tokio::sync::oneshot::channel::<()>();
    let loop_server = server.clone();
    let task = rig.rt.spawn(async move {
        serve_unix_listener(&listener, &loop_server, async {
            observed("stop_rx.await", stop_rx.await);
        })
        .await
    });
    let mut child = spawn_child("uds-client", &[("SL_SOCKET", socket.display().to_string())]);
    let child_pid = child.id();
    assert_ne!(child_pid, std::process::id());
    let reported = child_line(&mut child, "SL_CLIENT ");
    assert_eq!(reported, format!("served=true pid={child_pid}"));
    assert!(child.wait().unwrap().success());
    stop_tx.send(()).unwrap();
    assert_eq!(rig.rt.block_on(task).unwrap(), Ok(()));
    drop(_guard);
    rig.rt.block_on(server.router.shutdown());
    drop(server);
    host.close(&*rig.clock, rig.deadline()).unwrap();
    assert!(
        !socket.exists() || std::fs::symlink_metadata(&socket).is_ok(),
        "the test removes its directory below"
    );
    observed("std::fs::remove_dir_all", std::fs::remove_dir_all(dir));
}

#[test]
fn the_bootstrap_secret_travels_over_the_childs_inherited_stdin_pipe_only() {
    // 32 bytes then EOF is accepted; anything else is refused with the oracle's code.
    for (label, bytes, expected) in [
        (
            "exact",
            vec![0x44_u8; 32],
            "ok ".to_owned() + &"44".repeat(32),
        ),
        (
            "short",
            vec![0x44_u8; 31],
            "err invalid_bootstrap_length".to_owned(),
        ),
        (
            "long",
            vec![0x44_u8; 33],
            "err invalid_bootstrap_length".to_owned(),
        ),
    ] {
        let mut child = spawn_child("bootstrap-stdin", &[]);
        let mut stdin = child.stdin.take().unwrap();
        stdin.write_all(&bytes).unwrap();
        drop(stdin);
        assert_eq!(child_line(&mut child, "SL_BOOTSTRAP "), expected, "{label}");
        assert!(child.wait().unwrap().success());
    }
    // The secret type never prints its bytes.
    let secret: BootstrapSecret = parse_bootstrap_secret(std::io::Cursor::new([1u8; 32])).unwrap();
    assert!(!format!("{secret:?}").contains('1'));
}

/// A result whose failure is tolerated by design (a cleanup, a peer that already ended the
/// call): the outcome is printed to the test log, never dropped.
fn observed<T, E: std::fmt::Debug>(what: &str, result: Result<T, E>) {
    if let Err(error) = result {
        eprintln!("tolerated {what}: {error:?}");
    }
}
