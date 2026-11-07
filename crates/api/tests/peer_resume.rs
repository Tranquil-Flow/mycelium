// SPDX-License-Identifier: AGPL-3.0-only
//! `RemoteMacMember.start()` -> `_resume` and the seed-refusal / contract-error branches of `accept_handoff`
//! against the EXECUTED oracle (FX-L10-PEER-RESUME: `L10-evidence/r12-probe-peer.py` ->
//! `fixtures/oracle_peer_resume.json`, v0.1.4 f4e366e9): the real Python member against a real loopback TLS
//! seed. Every seed exchange was recorded; this test replays them from a loopback TLS server and demands the
//! same request BYTES, the same answer or exception (class/module/code), the same `_started`/live-session
//! outcome, the same membership projection and binding, and byte-equal state files, for 53 steps in 25 scenarios.
use api::{
    member::Binding,
    peer::{Accepted, JoinEnv, accept_handoff, membership_binding, membership_result_live, start},
    seed::SeedOptions,
};
use protocol::identity::Identity;
use rustls::{ServerConfig, ServerConnection, StreamOwned};
use rustls_pki_types::{CertificateDer, PrivateKeyDer, pem::PemObject};
use serde_json::{Map, Value, json};
use std::{
    cell::Cell,
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
    let doc: Value =
        serde_json::from_str(include_str!("fixtures/oracle_peer_resume.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    doc
}
fn secret(hex: &str) -> [u8; 32] {
    let mut out = [0u8; 32];
    for (i, b) in out.iter_mut().enumerate() {
        *b = u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).unwrap();
    }
    out
}
fn unhex(hex: &str) -> Vec<u8> {
    (0..hex.len() / 2)
        .map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).unwrap())
        .collect()
}
/// The state directory exactly as the oracle listed it (files, `identity/` and modes).
fn lay_out(label: &str, files: &Value) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("l10r12-peer-{}-{label}", std::process::id()));
    fs::remove_dir_all(&dir).unwrap_or(());
    fs::create_dir_all(dir.join("identity")).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
    for (name, file) in files.as_object().unwrap() {
        let bytes = file.get("text").and_then(Value::as_str).map_or_else(
            || unhex(file["hex"].as_str().unwrap()),
            |t| t.as_bytes().to_vec(),
        );
        fs::write(dir.join(name), bytes).unwrap();
        let mode = u32::from_str_radix(file["mode"].as_str().unwrap(), 8).unwrap();
        fs::set_permissions(dir.join(name), fs::Permissions::from_mode(mode)).unwrap();
    }
    dir
}
fn listing(dir: &Path) -> Value {
    let mut out = Map::new();
    let mut visit = |path: &Path, name: String| {
        let raw = fs::read(path).unwrap();
        let mode = format!(
            "{:04o}",
            fs::metadata(path).unwrap().permissions().mode() & 0o777
        );
        let entry = match String::from_utf8(raw.clone()) {
            Ok(text) => json!({"text": text, "mode": mode}),
            Err(_) => {
                json!({"hex": raw.iter().map(|b| format!("{b:02x}")).collect::<String>(), "mode": mode})
            }
        };
        out.insert(name, entry);
    };
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_file() {
            visit(
                &path,
                path.file_name().unwrap().to_string_lossy().into_owned(),
            );
        }
    }
    if let Ok(entries) = fs::read_dir(dir.join("identity")) {
        for entry in entries {
            let path = entry.unwrap().path();
            visit(
                &path,
                format!("identity/{}", path.file_name().unwrap().to_string_lossy()),
            );
        }
    }
    Value::Object(out)
}

enum Reply {
    Body(String),
    Error(u16, String),
    Drop,
    /// A chunked body cut short: `http.client.IncompleteRead`, which no `except SeedHTTPError` catches.
    Cut,
}
struct Replay {
    address: SocketAddr,
    seen: Arc<Mutex<Vec<(String, String, String)>>>,
    stop: Arc<AtomicBool>,
    handle: thread::JoinHandle<()>,
}
impl Replay {
    fn finish(self) -> Vec<(String, String, String)> {
        self.stop.store(true, Ordering::SeqCst);
        self.handle.join().unwrap();
        self.seen.lock().unwrap().clone()
    }
}
fn replay(doc: &Value, script: Vec<Reply>) -> Replay {
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
    listener.set_nonblocking(true).unwrap();
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = seen.clone();
    let stop = Arc::new(AtomicBool::new(false));
    let halt = stop.clone();
    let handle = thread::spawn(move || {
        for reply in script {
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
            let mut chunk = [0u8; 8192];
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
            log.lock().unwrap().push((
                method,
                path,
                String::from_utf8_lossy(&raw[head_end..head_end + length]).into_owned(),
            ));
            let (status, body) = match reply {
                Reply::Drop => continue,
                Reply::Cut => {
                    let wire = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n7\r\n{\"a\":1}\r\n";
                    let delivered = stream.write_all(wire).and_then(|()| stream.flush());
                    if let Err(error) = delivered {
                        eprintln!("replay seed: client closed early: {error}");
                    }
                    continue;
                }
                Reply::Body(body) => (200, body),
                Reply::Error(status, code) => (
                    status,
                    format!(
                        "{{\"error\":{{\"code\":\"{code}\"}},\"protocol\":\"mycelium.seed.http_error.v1\"}}"
                    ),
                ),
            };
            let head = format!(
                "HTTP/1.1 {status} Status\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            );
            let delivered = stream
                .write_all(head.as_bytes())
                .and_then(|()| stream.write_all(body.as_bytes()))
                .and_then(|()| stream.flush())
                .and_then(|()| {
                    stream.conn.send_close_notify();
                    stream.flush()
                });
            // A client that gave up (a scripted early hang-up) is an expected end, but never silent.
            if let Err(error) = delivered {
                eprintln!("replay seed: client closed early: {error}");
            }
        }
    });
    Replay {
        address,
        seen,
        stop,
        handle,
    }
}

/// What the seed said for each recorded call, in order.
fn script_of(calls: &[Value]) -> Vec<Reply> {
    calls
        .iter()
        .map(|call| {
            if call.get("dropped").is_some() {
                Reply::Drop
            } else if call.get("replaced_by_raw").is_some() {
                Reply::Cut
            } else if let Some(e) = call.get("replaced_by_error") {
                Reply::Error(
                    e["status"].as_u64().unwrap() as u16,
                    e["code"].as_str().unwrap().to_owned(),
                )
            } else if let Some(e) = call.get("error") {
                Reply::Error(
                    e["status"].as_u64().unwrap() as u16,
                    e["code"].as_str().unwrap().to_owned(),
                )
            } else if let Some(served) = call.get("response_served") {
                Reply::Body(served.as_str().unwrap().to_owned())
            } else {
                Reply::Body(call["response"].as_str().unwrap().to_owned())
            }
        })
        .collect()
}

/// The suffix the oracle's first `uuid4()` of a scenario produced (`join-message-<30 x ab><nn>`).
fn first_counter(scenario: &Value) -> u32 {
    let request = scenario["steps"][0]["calls"]
        .as_array()
        .unwrap()
        .iter()
        .find_map(|c| {
            c["request"]
                .as_str()
                .filter(|r| r.contains("join-message-"))
        });
    // A scenario that is refused at `/seed/identity` never draws an id (the counter then starts at 0).
    let Some(request) = request else {
        return 1;
    };
    let at = request.find("join-message-").unwrap() + "join-message-".len();
    u32::from_str_radix(&request[at + 30..at + 32], 16).unwrap()
}

#[test]
fn start_resume_and_seed_refusals_equal_the_executed_oracle() {
    let doc = fixture();
    let scenarios = doc["scenarios"].as_array().unwrap();
    assert!(scenarios.len() >= 25, "{} scenarios", scenarios.len());
    let mut failures = Vec::new();
    let mut steps_run = 0usize;
    for scenario in scenarios {
        let label = scenario["label"].as_str().unwrap();
        let signer = Identity::from_secret(
            scenario["signer_endpoint"].as_str().unwrap(),
            &secret(scenario["signer_secret"].as_str().unwrap()),
        )
        .unwrap();
        let counter = Cell::new(first_counter(scenario) - 1);
        let mut live: Option<api::member::Session> = None;
        for step in scenario["steps"].as_array().unwrap() {
            steps_run += 1;
            let name = step["name"].as_str().unwrap();
            let dir = lay_out(&format!("{label}-{name}"), &step["files_in"]);
            let calls = step["calls"].as_array().unwrap();
            let server = (!calls.is_empty()).then(|| replay(scenario, script_of(calls)));
            let address = server.as_ref().map_or_else(
                || {
                    TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
                        .unwrap()
                        .local_addr()
                        .unwrap()
                },
                |s| s.address,
            );
            let options = SeedOptions {
                ca_pem: Some(doc_ca(scenario)),
                connect_to: Some(address),
                server_name: None,
            };
            let t0 = step["t0"].as_f64().unwrap();
            let clock = move || t0;
            let random = |n: usize| -> String {
                if n == 8 {
                    return "00112233445566ff".to_owned();
                }
                counter.set(counter.get() + 1);
                format!("{}{:02x}", "ab".repeat(n - 1), counter.get())
            };
            let env = JoinEnv {
                signer: &signer,
                clock: &clock,
                random_hex: &random,
                seed: &options,
            };
            let mut problems = Vec::new();
            let want = &step["outcome"];
            let (error, session, started): (
                Option<(String, String, String)>,
                Option<api::member::Session>,
                bool,
            );
            if name == "accept" {
                match accept_handoff(&dir, true, &scenario["handoff"], &env) {
                    Ok(Accepted { answer, session: s }) => {
                        if Some(&answer.to_value()) != want.get("answer") {
                            problems.push(format!(
                                "answer {} != {:?}",
                                answer.to_value(),
                                want.get("answer")
                            ));
                        }
                        (error, session, started) = (None, s, true);
                    }
                    Err(e) => {
                        let (class, module) = e.raised.identity();
                        (error, session, started) = (
                            Some((class.into(), module.into(), e.code.into())),
                            None,
                            true,
                        );
                    }
                }
            } else {
                let identity_present = step["files_in"].get("identity/node.key").is_some();
                match start(&dir, &signer, identity_present, &env) {
                    Ok(done) => (error, session, started) = (None, done.session, true),
                    Err(failed) => {
                        let (class, module) = failed.error.raised.identity();
                        (error, session, started) = (
                            Some((class.into(), module.into(), failed.error.code.into())),
                            None,
                            failed.started,
                        );
                    }
                }
            }
            match (&error, want.get("error")) {
                (None, None) => {}
                (Some((class, module, code)), Some(w)) => {
                    // An `http.client` exception has no closed code: its text carries byte counts.
                    let raw = w["module"] == "http.client";
                    if (class.as_str(), module.as_str())
                        != (w["type"].as_str().unwrap(), w["module"].as_str().unwrap())
                        || (!raw && code.as_str() != w["code"].as_str().unwrap())
                    {
                        problems.push(format!("raised {class}/{module}/{code} != {w}"));
                    }
                }
                (got, w) => problems.push(format!("outcome {got:?} vs {w:?}")),
            }
            if started != want["_started"] {
                problems.push(format!("_started {started} != {}", want["_started"]));
            }
            if session.is_some() != want["has_member"] {
                problems.push(format!(
                    "live session {} != {}",
                    session.is_some(),
                    want["has_member"]
                ));
            }
            // The membership projection `status()` gives now, and the binding a live session offers.
            match (
                membership_result_live(&dir, session.as_ref(), t0),
                want.get("membership_result"),
            ) {
                (Ok(got), Some(w)) if &got.to_value() == w => {}
                // `status()` itself raises on unreadable state: the oracle recorded that exception.
                (Err(e), None) => {
                    let (class, module) = e.raised.identity();
                    let w = &want["membership_result_error"];
                    if (class, module, e.code)
                        != (
                            w["type"].as_str().unwrap_or_default(),
                            w["module"].as_str().unwrap_or_default(),
                            w["code"].as_str().unwrap_or_default(),
                        )
                    {
                        problems.push(format!(
                            "membership_result error {class}/{module}/{} != {w}",
                            e.code
                        ));
                    }
                }
                (got, w) => problems.push(format!("membership_result {got:?} != {w:?}")),
            }
            if let Some(binding) = want.get("binding").filter(|b| !b.is_null()) {
                let expect = Binding {
                    swarm_id: binding["swarm_id"].as_str().unwrap().into(),
                    node_id: binding["node_id"].as_str().unwrap().into(),
                    seed_key_digest: binding["seed_key_digest"].as_str().unwrap().into(),
                };
                if membership_binding(session.as_ref()) != Some(expect) {
                    problems.push("membership_binding differs".into());
                }
            }
            if let (Some(s), Some(g)) = (&session, want.get("session_generation"))
                && s.generation().map(Value::from).as_ref() != Some(g)
            {
                problems.push(format!("generation {:?} != {g}", s.generation()));
            }
            if listing(&dir) != step["files"] {
                let got = listing(&dir);
                let mut names: Vec<&String> = got.as_object().unwrap().keys().collect();
                names.sort();
                problems.push(format!("files differ: {names:?}"));
                for (file, w) in step["files"].as_object().unwrap() {
                    if got.get(file) != Some(w) {
                        problems.push(format!("  {file} differs"));
                    }
                }
            }
            if let Some(server) = server {
                let seen = server.finish();
                let wanted: Vec<(String, String, String)> = calls
                    .iter()
                    .map(|c| {
                        (
                            c["method"].as_str().unwrap().into(),
                            c["path"].as_str().unwrap().into(),
                            c["request"].as_str().unwrap_or_default().into(),
                        )
                    })
                    .collect();
                if seen != wanted {
                    problems.push(format!(
                        "requests {:?} != {:?}",
                        seen.iter().map(|s| &s.1).collect::<Vec<_>>(),
                        wanted.iter().map(|s| &s.1).collect::<Vec<_>>()
                    ));
                    for (a, b) in seen.iter().zip(&wanted) {
                        if a != b {
                            problems.push(format!("  {} body equal: {}", a.1, a.2 == b.2));
                        }
                    }
                }
            }
            live = session.or(live);
            if !problems.is_empty() {
                failures.push(format!("{label}/{name}: {}", problems.join(" | ")));
            }
            fs::remove_dir_all(&dir).unwrap_or(());
        }
        drop(live);
    }
    assert!(steps_run >= 53, "{steps_run} steps");
    assert!(
        failures.is_empty(),
        "{} steps differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
}

fn doc_ca(scenario: &Value) -> Vec<u8> {
    scenario["ca_pem"].as_str().unwrap().as_bytes().to_vec()
}
