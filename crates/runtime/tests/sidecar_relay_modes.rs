// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NET-09 (production-relay and --force-relay modes wait the ready-address window, name ready_address_incomplete and still publish ready, as the executed oracle under Seatbelt).
//!
//! The production-relay and `--force-relay` modes of `run_sidecar` against the EXECUTED v0.1.4
//! sidecar (`fixtures/sidecar-relay-oracle.json`, probe `L9-evidence/r14-relay-probe.rb`, release
//! profile, run under Seatbelt with every non-local outbound connection denied).
//!
//! What the oracle did with no relay address ever advertised: it waited the whole ready-address
//! window, logged `ready_address_incomplete` once, then published `ready` anyway (id and ALPN as in
//! local mode; `--force-relay`: an address with NO transport address at all; production: the direct
//! interface addresses, never a relay and never loopback), and stopped on SIGINT with exit 0 and no
//! socket left behind. Here the relay is one explicit unreachable local URL with no address lookup
//! (`Reach::Hermetic`), so nothing leaves the host; the window is a registry span passed by the
//! test (the shipped 3 s is `SidecarReadyAddressWindow`; the binary test in `sidecar_main_cli.rs`
//! waits it for real). The relay-ONLINE path (`Online`) needs
//! a relay server and stays covered by the gate's own tests; two physical hosts are NOT RUN.
#![cfg(all(feature = "iroh-adapter", unix))]
use budgets::{BudgetId as B, Entries, Span};
use runtime::{
    iroh_host::Reach,
    sidecar_main::{NoArtifactBackend, SidecarConfig, SidecarOutput, run_sidecar},
    sidecar_process::{BootstrapSecret, SidecarError},
};
use serde_json::Value;
use std::{
    num::NonZeroUsize,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    sync::{Arc, Mutex, mpsc},
    time::{Duration, Instant},
};

fn fixture() -> Value {
    serde_json::from_str(include_str!("../fixtures/sidecar-relay-oracle.json")).unwrap()
}
fn window() -> Span {
    B::SidecarLocalHandshake.span(Entries::ZERO).unwrap()
}
fn slice() -> Duration {
    Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis() * 2)
}
struct Lines {
    stdout: Mutex<mpsc::Sender<String>>,
    stderr: Mutex<Vec<String>>,
}
impl SidecarOutput for Lines {
    fn stdout_line(&self, line: &str) -> std::io::Result<()> {
        self.stdout
            .lock()
            .unwrap()
            .send(line.to_owned())
            .map_err(|_| std::io::ErrorKind::BrokenPipe.into())
    }
    fn stderr_line(&self, line: &str) {
        self.stderr.lock().unwrap().push(line.to_owned());
    }
}
struct Outcome {
    /// False when the sidecar had already ended (a refused mode): nobody was left to stop.
    stop_delivered: bool,
    ready: Value,
    ready_after: Duration,
    events: Vec<String>,
    result: Result<(), SidecarError>,
    socket_left: bool,
}
/// Runs the sidecar in a thread with the oracle's endpoint secret, stops it after `ready`.
fn run(force_relay: bool, ready_window: Option<Span>) -> Outcome {
    let base = std::env::var_os("TMPDIR").map_or_else(std::env::temp_dir, PathBuf::from);
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let serial = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let dir = base.join(format!("l9rm{}-{serial}-{force_relay}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    let socket = dir.join("s");
    let (sender, ready) = mpsc::channel();
    let output = Arc::new(Lines {
        stdout: Mutex::new(sender),
        stderr: Mutex::new(Vec::new()),
    });
    let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
    let started = Instant::now();
    let thread = {
        let output = output.clone();
        let socket = socket.clone();
        std::thread::spawn(move || {
            let runtime = tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .enable_all()
                .build()
                .unwrap();
            let config = SidecarConfig {
                // Nothing listens on port 1: the relay is never reachable.
                reach: Reach::Hermetic("http://127.0.0.1:1".parse().unwrap()),
                ready_window,
                socket,
                local_only: false,
                force_relay,
                queue_capacity: NonZeroUsize::new(4).unwrap(),
                endpoint_secret: Some(BootstrapSecret::from_bytes([0x44; 32])),
                artifact: None,
            };
            run_sidecar(
                &runtime,
                config,
                BootstrapSecret::from_bytes([0x4f; 32]),
                &NoArtifactBackend,
                output,
                async {
                    if stopped.await.is_err() {
                        eprintln!("the stop sender was dropped: stopping");
                    }
                },
            )
        })
    };
    // The thread ends by itself when the mode is refused: no ready line then.
    let poll = Duration::from_millis(B::RegistryPoll.span(Entries::ZERO).unwrap().millis());
    let line = loop {
        match ready.recv_timeout(poll) {
            Ok(line) => break Some(line),
            Err(mpsc::RecvTimeoutError::Disconnected) => break None,
            Err(mpsc::RecvTimeoutError::Timeout) => {
                if thread.is_finished() || started.elapsed() > slice() {
                    break None;
                }
            }
        }
    };
    let ready_after = started.elapsed();
    let ready_value = line.map_or(Value::Null, |line| serde_json::from_str(&line).unwrap());
    let stop_delivered = stop.send(()).is_ok();
    let result = thread.join().unwrap();
    let outcome = Outcome {
        stop_delivered,
        ready: ready_value,
        ready_after,
        events: output.stderr.lock().unwrap().clone(),
        result,
        socket_left: socket.exists(),
    };
    std::fs::remove_dir_all(&dir).unwrap();
    outcome
}

#[test]
fn the_fixture_is_the_executed_oracle_table() {
    let f = fixture();
    assert!(
        f["provenance"]["oracle"]
            .as_str()
            .unwrap()
            .contains("f4e366e98a1f705dfaf182897044500d290dbd7f")
    );
    assert!(
        f["provenance"]["sandbox_profile"]
            .as_str()
            .unwrap()
            .contains("deny network-outbound")
    );
    assert_eq!(f["cases"].as_array().unwrap().len(), 2);
}

#[test]
fn an_unreachable_relay_waits_the_window_names_it_and_still_publishes_ready_as_the_oracle() {
    let f = fixture();
    for case in f["cases"].as_array().unwrap() {
        let force_relay = case["flags"] == serde_json::json!(["--force-relay"]);
        let name = if force_relay {
            "force-relay"
        } else {
            "production"
        };
        let got = run(force_relay, Some(window()));
        assert_eq!(
            got.result,
            Ok(()),
            "{name}: a stop is a clean exit like the oracle's 0"
        );
        assert_eq!(case["exit"], 0);
        let expected: Vec<&str> = case["events"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e.as_str().unwrap())
            .collect();
        assert_eq!(got.events, expected, "{name}: events");
        assert_eq!(got.ready["event"], "ready", "{name}: {:?}", got.ready);
        assert_eq!(got.ready["alpn"], case["ready_alpn"], "{name}: alpn");
        assert_eq!(
            got.ready["endpoint_id"], case["endpoint_id"],
            "{name}: fixed secret, same id"
        );
        assert_eq!(got.ready["endpoint_addr"]["id"], got.ready["endpoint_id"]);
        let addrs = got.ready["endpoint_addr"]["addrs"].as_array().unwrap();
        let kinds: Vec<&str> = {
            let mut kinds: Vec<&str> = addrs
                .iter()
                .map(|a| a.as_object().unwrap().keys().next().unwrap().as_str())
                .collect();
            kinds.sort_unstable();
            kinds.dedup();
            kinds
        };
        let want: Vec<&str> = case["addr_kinds"]
            .as_array()
            .unwrap()
            .iter()
            .map(|k| k.as_str().unwrap())
            .collect();
        assert_eq!(kinds, want, "{name}: address kinds");
        if force_relay {
            assert_eq!(addrs.len() as u64, case["addr_count"].as_u64().unwrap());
        } else {
            assert!(
                !addrs.is_empty(),
                "{name}: the direct addresses are published"
            );
        }
        assert!(
            addrs
                .iter()
                .all(|a| !a["Ip"].as_str().unwrap_or("").starts_with("127.")),
            "{name}: no loopback address (oracle any_loopback = {})",
            case["any_loopback"]
        );
        // The ready line waits the whole window (the oracle: 3046 ms against its 3 s window).
        let waited = Duration::from_millis(window().millis());
        assert!(
            got.ready_after >= waited,
            "{name}: ready after {:?} < window {waited:?}",
            got.ready_after
        );
        assert!(
            got.ready_after < waited + slice(),
            "{name}: ready after {:?}",
            got.ready_after
        );
        assert_eq!(
            got.socket_left,
            case["socket_left_behind"].as_bool().unwrap(),
            "{name}: socket"
        );
    }
}

#[test]
fn without_a_registry_window_the_relay_modes_stay_refused_before_any_endpoint_exists() {
    for force_relay in [false, true] {
        let got = run(force_relay, None);
        assert!(!got.stop_delivered, "the sidecar had already ended");
        assert_eq!(got.result, Err(SidecarError::NetworkModeUnsupported));
        assert!(got.ready.is_null());
        assert!(!got.socket_left, "no socket was bound");
    }
}
