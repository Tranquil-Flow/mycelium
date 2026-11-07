// SPDX-License-Identifier: AGPL-3.0-only
//! Shared pieces of the oracle-replay tests: the state directory as the oracle listed it, and a loopback TLS
//! seed that replays the recorded exchange (the recorded server certificate; the recorded CA is pinned by the client).
#![allow(dead_code)]
use rustls::{ServerConfig, ServerConnection, StreamOwned};
use rustls_pki_types::{CertificateDer, PrivateKeyDer, pem::PemObject};
use serde_json::{Map, Value, json};
use std::{
    collections::VecDeque,
    fs,
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream},
    os::unix::fs::PermissionsExt,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread,
};

pub fn poll() -> std::time::Duration {
    let millis = budgets::BudgetId::OwnerPoll
        .span(budgets::Entries::new(0))
        .unwrap()
        .millis();
    std::time::Duration::from_millis(millis)
}
pub fn secret(hex: &str) -> [u8; 32] {
    let mut out = [0u8; 32];
    for (i, b) in out.iter_mut().enumerate() {
        *b = u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).unwrap();
    }
    out
}
pub fn unhex(hex: &str) -> Vec<u8> {
    (0..hex.len() / 2)
        .map(|i| u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).unwrap())
        .collect()
}
/// The state directory exactly as the oracle listed it (files, `identity/` and modes).
pub fn lay_out(label: &str, files: &Value) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("l10r13-renew-{}-{label}", std::process::id()));
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
pub fn listing(dir: &Path) -> Value {
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

pub enum Reply {
    Body(String),
    Error(u16, String),
    Drop,
    /// A chunked body cut short: `http.client.IncompleteRead`, which no `except SeedHTTPError` catches.
    Cut,
}
type Seen = Arc<Mutex<Vec<(String, String, String)>>>;
/// One loopback TLS seed for a whole scenario (the client keeps the address it was built with, so the
/// address must outlive a step): each step queues the replies the oracle's seed gave, and collects the
/// requests it received. A connection that arrives with nothing queued is logged as `("", "", "")` and dropped.
pub struct Server {
    pub address: SocketAddr,
    queue: Arc<Mutex<VecDeque<Reply>>>,
    seen: Seen,
    stop: Arc<AtomicBool>,
    handle: Option<thread::JoinHandle<()>>,
}
impl Server {
    pub fn script(&self, replies: Vec<Reply>) {
        *self.queue.lock().unwrap() = replies.into();
    }
    /// The requests received since the last call, and whether the queued replies were all used.
    pub fn take(&self) -> (Vec<(String, String, String)>, bool) {
        let drained = std::mem::take(&mut *self.seen.lock().unwrap());
        (drained, self.queue.lock().unwrap().is_empty())
    }
    pub fn finish(mut self) {
        self.stop.store(true, Ordering::SeqCst);
        // Wake the blocking `accept` (no time literal, no polling).
        if let Err(error) = TcpStream::connect(self.address) {
            eprintln!("replay seed: wake-up connection failed: {error}");
        }
        if let Some(handle) = self.handle.take() {
            handle.join().unwrap();
        }
    }
}
pub fn serve(doc: &Value) -> Server {
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
    let seen: Seen = Arc::new(Mutex::new(Vec::new()));
    let log = seen.clone();
    let queue = Arc::new(Mutex::new(VecDeque::<Reply>::new()));
    let replies = queue.clone();
    let stop = Arc::new(AtomicBool::new(false));
    let halt = stop.clone();
    let handle = thread::spawn(move || {
        loop {
            let Ok((tcp, _)) = listener.accept() else {
                return;
            };
            if halt.load(Ordering::SeqCst) {
                return;
            }
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
            let Some(reply) = replies.lock().unwrap().pop_front() else {
                log.lock()
                    .unwrap()
                    .push((String::new(), String::new(), String::new()));
                continue;
            };
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
    Server {
        address,
        queue,
        seen,
        stop,
        handle: Some(handle),
    }
}

/// What the seed said for each recorded call, in order.
pub fn script_of(calls: &[Value]) -> Vec<Reply> {
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
pub fn first_counter(scenario: &Value) -> u32 {
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
