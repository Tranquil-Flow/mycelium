// SPDX-License-Identifier: AGPL-3.0-only
//! The seed HTTPS client against the EXECUTED oracle (FX-L10-SEED-HTTP:
//! `L10-evidence/r12-probe-seed-http.py` -> `fixtures/oracle_seed_http.json`, v0.1.4 f4e366e9).
//! The real Python `SeedHTTPClient` (pinned-CA adapter) ran every case against a scripted loopback TLS
//! server; this test serves the SAME wire bytes to the Rust client and demands the same outcome:
//! `/seed/join` error statuses (JOIN_ROUTE_ERROR_STATUSES), error-body/content-type/framing rules,
//! identity verification branches, rotation transitions and the `/seed/resume` transport.
use api::seed::{SeedClient, SeedError, SeedOptions};
use rustls::{ServerConfig, ServerConnection, StreamOwned};
use rustls_pki_types::{CertificateDer, PrivateKeyDer, pem::PemObject};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpListener},
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
    let doc: Value = serde_json::from_str(include_str!("fixtures/oracle_seed_http.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    doc
}

/// `wire()` of the probe: the scripted response as bytes.
fn wire(entry: &Value) -> Vec<u8> {
    if let Some(hex) = entry.get("raw_hex").and_then(Value::as_str) {
        return (0..hex.len() / 2)
            .map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).unwrap())
            .collect();
    }
    let body: Vec<u8> = if let Some(hex) = entry.get("body_hex").and_then(Value::as_str) {
        (0..hex.len() / 2)
            .map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).unwrap())
            .collect()
    } else if let Some(n) = entry.get("filler").and_then(Value::as_u64) {
        let head = b"{\"a\":\"";
        let mut out = head.to_vec();
        out.resize(n as usize - 2, b'x');
        out.extend_from_slice(b"\"}");
        out
    } else {
        entry
            .get("body")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .as_bytes()
            .to_vec()
    };
    let mut lines = vec![format!("HTTP/1.1 {} Status", entry["status"])];
    for header in entry["headers"].as_array().into_iter().flatten() {
        lines.push(format!(
            "{}: {}",
            header[0].as_str().unwrap(),
            header[1].as_str().unwrap()
        ));
    }
    let payload = if entry.get("chunked").and_then(Value::as_bool) == Some(true) {
        lines.push("Transfer-Encoding: chunked".into());
        let half = body.len() / 2;
        let mut out = Vec::new();
        for part in [&body[..half], &body[half..]] {
            if !part.is_empty() {
                out.extend_from_slice(format!("{:x}\r\n", part.len()).as_bytes());
                out.extend_from_slice(part);
                out.extend_from_slice(b"\r\n");
            }
        }
        out.extend_from_slice(b"0\r\n\r\n");
        out
    } else {
        let length = entry
            .get("content_length")
            .and_then(Value::as_u64)
            .map_or(body.len(), |n| n as usize);
        lines.push(format!("Content-Length: {length}"));
        body
    };
    lines.push("Connection: close".into());
    let mut out = (lines.join("\r\n") + "\r\n\r\n").into_bytes();
    out.extend_from_slice(&payload);
    out
}

struct Scripted {
    address: SocketAddr,
    seen: Arc<Mutex<Vec<(String, String, String)>>>,
    stop: Arc<AtomicBool>,
    handle: thread::JoinHandle<()>,
}
impl Scripted {
    fn finish(self) -> Vec<(String, String, String)> {
        self.stop.store(true, Ordering::SeqCst);
        self.handle.join().unwrap();
        self.seen.lock().unwrap().clone()
    }
}
fn serve(doc: &Value, script: Vec<Vec<u8>>) -> Scripted {
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
        for response in script {
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
            let body = String::from_utf8_lossy(&raw[head_end..head_end + length]).into_owned();
            log.lock().unwrap().push((method, path, body));
            let delivered = stream
                .write_all(&response)
                .and_then(|()| stream.flush())
                .and_then(|()| {
                    stream.conn.send_close_notify();
                    stream.flush()
                });
            // A client that gave up (a scripted early hang-up) is an expected end, but never silent.
            if let Err(error) = delivered {
                eprintln!("scripted seed: client closed early: {error}");
            }
        }
    });
    Scripted {
        address,
        seen,
        stop,
        handle,
    }
}

/// The fixture's outcome shape for a Rust result.
fn digest_of(value: &Value) -> Value {
    let raw = protocol::canonical::to_bytes(value).unwrap();
    serde_json::json!({"ok_digest": hex(&Sha256::digest(&raw)), "bytes": raw.len()})
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn outcome(result: Result<Value, SeedError>) -> Value {
    match result {
        Ok(value) => {
            let size = protocol::canonical::to_bytes(&value).unwrap().len();
            if size > 4096 {
                digest_of(&value)
            } else {
                serde_json::json!({"ok": value})
            }
        }
        Err(e) if e.raw.is_some() => {
            serde_json::json!({"raised": {"type": e.raw, "module": "http.client"}})
        }
        Err(e) => serde_json::json!({"error": {
            "code": e.code, "status": e.status,
            "window": e.window.map(|w| serde_json::json!({
                "now": w.now, "issued_at": w.issued_at, "expires_at": w.expires_at}))}}),
    }
}
/// The comparable part of a fixture error window (the oracle also records derived fields).
fn narrowed(expected: &Value) -> Value {
    let mut out = expected.clone();
    if let Some(window) = out
        .pointer_mut("/error/window")
        .and_then(Value::as_object_mut)
    {
        window.retain(|k, _| matches!(k.as_str(), "now" | "issued_at" | "expires_at"));
    }
    out
}

#[test]
fn seed_http_client_equals_the_executed_oracle() {
    let doc = fixture();
    let cases = doc["cases"].as_array().unwrap();
    assert!(cases.len() >= 290, "{} cases", cases.len());
    let mut failures = Vec::new();
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let script: Vec<Vec<u8>> = case["script"]
            .as_array()
            .unwrap()
            .iter()
            .map(wire)
            .collect();
        let server = serve(&doc, script);
        let options = SeedOptions {
            ca_pem: Some(doc["ca_pem"].as_str().unwrap().as_bytes().to_vec()),
            connect_to: Some(server.address),
            server_name: None,
        };
        let mut client = SeedClient::new(
            doc["seed_url"].as_str().unwrap(),
            doc["swarm_id"].as_str().unwrap(),
            case["digest"].as_str().unwrap(),
            std::slice::from_ref(&case["record"]),
            &options,
        )
        .unwrap();
        let mut problems = Vec::new();
        for (index, op) in case["ops"].as_array().unwrap().iter().enumerate() {
            let now = op["now"].as_f64();
            let result = match op["op"].as_str().unwrap() {
                "join" => client.join(op["token"].as_str().unwrap(), &op["envelope"]),
                "resume" => client.resume(&op["envelope"]),
                "identity" => client.identity(&|| now.unwrap()),
                "rotation" => client
                    .rotation(now.unwrap())
                    .map(|found| found.unwrap_or(Value::Null)),
                "accepted" => client
                    .accepted_seed_key_digest(&op["envelope"], now.unwrap())
                    .map(Value::from),
                other => panic!("unknown op {other}"),
            };
            let got = outcome(result);
            let want = narrowed(&case["results"][index]);
            if got != want {
                let (g, w) = (got.to_string(), want.to_string());
                problems.push(format!(
                    "op {index} ({}): {} != {}",
                    op["op"],
                    &g[..g.len().min(160)],
                    &w[..w.len().min(160)]
                ));
            }
        }
        let seen = server.finish();
        let wanted: Vec<(String, String, String)> = case["requests"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| {
                (
                    r[0].as_str().unwrap().to_owned(),
                    r[1].as_str().unwrap().to_owned(),
                    r[2].as_str().unwrap().to_owned(),
                )
            })
            .collect();
        if seen != wanted {
            problems.push(format!(
                "requests {:?} != {:?}",
                seen.iter().map(|s| (&s.0, &s.1)).collect::<Vec<_>>(),
                wanted.iter().map(|s| (&s.0, &s.1)).collect::<Vec<_>>()
            ));
        }
        if !problems.is_empty() {
            failures.push(format!("{name}: {}", problems.join(" | ")));
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} cases differ:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}
