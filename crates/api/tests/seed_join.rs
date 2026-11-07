// SPDX-License-Identifier: AGPL-3.0-only
//! The seed half of `RemoteMacMember.accept_handoff` against the EXECUTED oracle
//! (`L10-evidence/r11-probe-seed-join.py` -> `fixtures/oracle_seed_join.json`, v0.1.4 f4e366e9): the real
//! Python member, a real loopback TLS seed (`DemoAuthority` + `LocalTLSSeedServer`, installation-local CA).
//! The probe recorded every seed call; this test replays those exchanges from a loopback TLS server
//! (the recorded server certificate, the recorded CA pinned by the client) and demands the Rust
//! member send the SAME request bytes, then end with the same answer or exception (class/module/code),
//! the same files, and - for a first acceptance - the live session's membership binding.
use api::{
    member::Binding,
    peer::{Accepted, JoinEnv, accept_handoff, membership_binding},
    seed::SeedOptions,
};
use protocol::identity::Identity;
use rustls::{ServerConfig, ServerConnection, StreamOwned};
use rustls_pki_types::{CertificateDer, PrivateKeyDer, pem::PemObject};
use serde_json::{Map, Value, json};
use std::{
    fs,
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpListener},
    os::unix::fs::PermissionsExt,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
};

fn poll() -> std::time::Duration {
    let millis = budgets::BudgetId::OwnerPoll
        .span(budgets::Entries::new(0))
        .unwrap()
        .millis();
    std::time::Duration::from_millis(millis)
}
fn fixture() -> Value {
    let doc: Value = serde_json::from_str(include_str!("fixtures/oracle_seed_join.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    doc
}
fn bytes(hex: &str) -> [u8; 32] {
    let mut out = [0u8; 32];
    for (i, b) in out.iter_mut().enumerate() {
        *b = u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).unwrap();
    }
    out
}
fn fresh(label: &str, files: &Value) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("l10r11-seed-{}-{label}", std::process::id()));
    fs::remove_dir_all(&dir).unwrap_or(());
    fs::create_dir_all(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
    for (name, file) in files.as_object().unwrap() {
        fs::write(dir.join(name), file["text"].as_str().unwrap()).unwrap();
        let mode = u32::from_str_radix(file["mode"].as_str().unwrap(), 8).unwrap();
        fs::set_permissions(dir.join(name), fs::Permissions::from_mode(mode)).unwrap();
    }
    dir
}
fn listing(dir: &Path) -> Value {
    let mut out = Map::new();
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_file() {
            out.insert(
                path.file_name().unwrap().to_string_lossy().into_owned(),
                json!({"text": fs::read_to_string(&path).unwrap(),
                       "mode": format!("{:04o}", fs::metadata(&path).unwrap().permissions().mode() & 0o777)}),
            );
        }
    }
    Value::Object(out)
}

/// What the replay seed saw and did: the requests it received, in order.
struct Replay {
    address: SocketAddr,
    seen: Arc<Mutex<Vec<(String, String, String)>>>,
    stop: Arc<AtomicBool>,
    handle: thread::JoinHandle<()>,
}
impl Replay {
    /// Stops the server (a call the member never made leaves it waiting) and observes its end.
    fn finish(self) -> Vec<(String, String, String)> {
        self.stop.store(true, Ordering::SeqCst);
        self.handle.join().unwrap();
        self.seen.lock().unwrap().clone()
    }
}
/// A TLS loopback seed that answers `calls` in order (a call marked `drop` is read and then
/// hung up on, the oracle's `seed_http_unreachable` after the commit).
fn replay(doc: &Value, calls: Vec<(Value, bool)>) -> Replay {
    let certs: Vec<CertificateDer<'static>> =
        CertificateDer::pem_slice_iter(doc["server_cert_pem"].as_str().unwrap().as_bytes())
            .map(Result::unwrap)
            .collect();
    let key =
        PrivateKeyDer::from_pem_slice(doc["server_key_pem"].as_str().unwrap().as_bytes()).unwrap();
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = Arc::new(
        ServerConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(certs, key)
            .unwrap(),
    );
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let address = listener.local_addr().unwrap();
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = seen.clone();
    let stop = Arc::new(AtomicBool::new(false));
    let halt = stop.clone();
    listener.set_nonblocking(true).unwrap();
    let handle = thread::spawn(move || {
        for (call, drop) in calls {
            let tcp = loop {
                match listener.accept() {
                    Ok((tcp, _)) => break tcp,
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        if halt.load(Ordering::SeqCst) {
                            return;
                        }
                        thread::sleep(poll());
                    }
                    Err(_) => return,
                }
            };
            tcp.set_nonblocking(false).unwrap();
            let mut stream = StreamOwned::new(ServerConnection::new(config.clone()).unwrap(), tcp);
            let mut raw = Vec::new();
            let mut chunk = [0u8; 4096];
            let (head_end, length) = loop {
                let Ok(n) = stream.read(&mut chunk) else {
                    break (0, 0);
                };
                if n == 0 {
                    break (0, 0);
                }
                raw.extend_from_slice(&chunk[..n]);
                if let Some(p) = raw.windows(4).position(|w| w == b"\r\n\r\n") {
                    let head = String::from_utf8_lossy(&raw[..p]).to_ascii_lowercase();
                    let length = head
                        .lines()
                        .find_map(|l| l.strip_prefix("content-length:"))
                        .and_then(|v| v.trim().parse::<usize>().ok())
                        .unwrap_or(0);
                    if raw.len() >= p + 4 + length {
                        break (p + 4, length);
                    }
                }
            };
            let head = String::from_utf8_lossy(&raw[..head_end.saturating_sub(4)]).into_owned();
            let first = head.lines().next().unwrap_or_default().to_owned();
            let mut parts = first.split(' ');
            let (method, path) = (
                parts.next().unwrap_or_default().to_owned(),
                parts.next().unwrap_or_default().to_owned(),
            );
            let body = String::from_utf8_lossy(&raw[head_end..head_end + length]).into_owned();
            log.lock().unwrap().push((method, path, body));
            if drop {
                continue;
            }
            let response = call["response"].as_str().unwrap().as_bytes();
            let head = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                response.len()
            );
            stream.write_all(head.as_bytes()).unwrap();
            stream.write_all(response).unwrap();
            stream.flush().unwrap();
            stream.conn.send_close_notify();
            stream.flush().unwrap();
        }
    });
    Replay {
        address,
        seen,
        stop,
        handle,
    }
}

#[test]
fn seed_half_of_accept_handoff_equals_the_executed_oracle() {
    let doc = fixture();
    let scenarios = doc["scenarios"].as_array().unwrap();
    assert!(scenarios.len() >= 5, "{} scenarios", scenarios.len());
    let mut failures = Vec::new();
    for scenario in scenarios {
        let label = scenario["label"].as_str().unwrap();
        let dir = fresh(label, &scenario["files_in"]);
        let signer = Identity::from_secret(
            scenario["signer_endpoint"].as_str().unwrap(),
            &bytes(scenario["signer_secret"].as_str().unwrap()),
        )
        .unwrap();
        let t0 = scenario["t0"].as_f64().unwrap();
        let drop_path = match label {
            "recovery_after_join_commit" => Some("/seed/join"),
            "identity_dropped" => Some("/seed/identity"),
            _ => None,
        };
        let mut live: Option<api::member::Session> = None;
        for (index, attempt) in scenario["attempts"].as_array().unwrap().iter().enumerate() {
            let calls: Vec<(Value, bool)> = attempt["calls"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|c| c.get("response").is_some())
                .map(|c| {
                    let last = index == 0 && Some(c["path"].as_str().unwrap()) == drop_path;
                    (c.clone(), last)
                })
                .collect();
            // `seed_unreachable`: the dial itself fails (nothing listens there).
            let server = (!calls.is_empty()).then(|| replay(scenario, calls));
            let address = server.as_ref().map_or_else(
                || {
                    let probe = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
                    probe.local_addr().unwrap()
                },
                |s| s.address,
            );
            let options = SeedOptions {
                ca_pem: Some(scenario["ca_pem"].as_str().unwrap().as_bytes().to_vec()),
                connect_to: Some(address),
                server_name: None,
            };
            let clock = move || t0;
            let random = |n: usize| match n {
                8 => "00112233445566ff".to_owned(),
                _ => "ab".repeat(n),
            };
            let env = JoinEnv {
                signer: &signer,
                clock: &clock,
                random_hex: &random,
                seed: &options,
            };
            let got = accept_handoff(&dir, true, &scenario["handoff"], &env);
            let mut problems = Vec::new();
            match (&got, attempt.get("answer"), attempt.get("error")) {
                (Ok(Accepted { answer, session }), Some(want), None) => {
                    if &answer.to_value() != want {
                        problems.push(format!("answer {} != {want}", answer.to_value()));
                    }
                    let _ = session;
                }
                (Err(e), None, Some(want)) => {
                    let (class, module) = e.raised.identity();
                    if e.code != want["code"].as_str().unwrap() {
                        problems.push(format!("code {} != {}", e.code, want["code"]));
                    }
                    if (class, module)
                        != (
                            want["type"].as_str().unwrap(),
                            want["module"].as_str().unwrap(),
                        )
                    {
                        problems.push(format!("raised {class}/{module} != {}", want["type"]));
                    }
                }
                other => problems.push(format!("outcome {other:?} vs {attempt}")),
            }
            if let (Ok(Accepted { session, .. }), true) = (got, attempt.get("answer").is_some()) {
                live = session;
            }
            if listing(&dir) != attempt["files"] {
                problems.push(format!(
                    "files {:?} != {:?}",
                    listing(&dir)
                        .as_object()
                        .unwrap()
                        .keys()
                        .collect::<Vec<_>>(),
                    attempt["files"]
                        .as_object()
                        .unwrap()
                        .keys()
                        .collect::<Vec<_>>()
                ));
                for (name, want) in attempt["files"].as_object().unwrap() {
                    if listing(&dir).get(name) != Some(want) {
                        problems.push(format!("file {name} differs"));
                    }
                }
            }
            // The exact requests the oracle sent.
            if let Some(server) = server {
                let seen = server.finish();
                let want: Vec<(String, String, String)> = attempt["calls"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .filter(|c| c.get("response").is_some())
                    .map(|c| {
                        (
                            c["method"].as_str().unwrap().to_owned(),
                            c["path"].as_str().unwrap().to_owned(),
                            c["request"].as_str().unwrap_or_default().to_owned(),
                        )
                    })
                    .collect();
                if seen != want {
                    problems.push(format!(
                        "requests differ: {} seen vs {} wanted",
                        seen.len(),
                        want.len()
                    ));
                    for (a, b) in seen.iter().zip(&want) {
                        if a != b {
                            problems.push(format!("  {} {} body equal: {}", a.0, a.1, a.2 == b.2));
                        }
                    }
                }
            }
            if !problems.is_empty() {
                failures.push(format!("{label}#{index}: {}", problems.join(" | ")));
            }
        }
        // A first acceptance leaves a live session whose binding names the swarm, node and seed key.
        if scenario["label"] == "first_join" {
            let files = &scenario["attempts"][0]["files"];
            let state: Value =
                serde_json::from_str(files["membership.state.json"]["text"].as_str().unwrap())
                    .unwrap();
            let binding = membership_binding(live.as_ref());
            let want = Binding {
                swarm_id: state["swarm_id"].as_str().unwrap().to_owned(),
                node_id: state["node_id"].as_str().unwrap().to_owned(),
                seed_key_digest: state["seed_key_digest"].as_str().unwrap().to_owned(),
            };
            if binding != Some(want) {
                failures.push(format!("{label}: membership_binding {binding:?}"));
            }
        }
        fs::remove_dir_all(&dir).unwrap_or(());
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
