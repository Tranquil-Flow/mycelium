// SPDX-License-Identifier: AGPL-3.0-only
//! A loopback fake of a route epoch's request gateway (`api::requests` routes): bearer check,
//! `GET /v1/qualification/current`, `POST /v1/inference` (202 with stream/cancel paths and the
//! owner token), the SSE event stream (`id: g:s` / `event:` / `data:` frames) and `DELETE` Stop.
//! Each stream follows the gateway's `Script`. Test code only; shared with node-service tests.
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, Ordering::SeqCst},
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};
/// The id the fake gives its `index`-th token event (`token_id` = this + index), as the route's
/// gateway gives every token event its id (`M16Requests`' `{token_id, text, token_index}`).
#[allow(dead_code)] // not every including test file reads the ids
pub const FIRST_TOKEN_ID: u64 = 7_000;
/// How a stream ends after its tokens.
#[derive(Clone, Debug)]
#[allow(dead_code)] // not every including test file uses every ending
pub enum End {
    Completed,
    Failed(&'static str),
    /// Waits until the request is cancelled (`cancelled`) or `release`d (`completed`).
    Hold,
    /// Sends nothing more and keeps the connection open until the gateway is dropped.
    Stall,
}
#[derive(Clone, Debug)]
pub struct Script {
    pub tokens: Vec<&'static str>,
    pub end: End,
    /// `false`: the qualification answers 503 (no qualified route).
    pub qualified: bool,
    /// `Some((status, code))`: `POST /v1/inference` refuses with `{"error": code}` (the route's
    /// refusal before any work, e.g. `joined_route_context_exceeds_worker`).
    pub refuse_post: Option<(u16, &'static str)>,
}
impl Script {
    pub fn completed(tokens: &[&'static str]) -> Self {
        Self {
            tokens: tokens.to_vec(),
            end: End::Completed,
            qualified: true,
            refuse_post: None,
        }
    }
    pub fn ending(tokens: &[&'static str], end: End) -> Self {
        Self {
            end,
            ..Self::completed(tokens)
        }
    }
}
/// What the gateway saw.
#[derive(Default, Debug)]
pub struct Seen {
    pub posts: Vec<Value>,
    pub deletes: usize,
    pub tokens_sent: usize,
    pub cancelled: Vec<String>,
    pub released: bool,
    pub unauthorized: usize,
}
/// Runs inside the gateway's `DELETE` handler, before its reply (a test observes what else holds
/// at the moment the cancel arrives).
pub type OnDelete = Box<dyn Fn() + Send + Sync>;
struct Shared {
    script: Mutex<Script>,
    seen: Mutex<Seen>,
    changed: Condvar,
    stop: AtomicBool,
    on_delete: Mutex<Option<OnDelete>>,
}
pub struct FakeGateway {
    addr: SocketAddr,
    bearer: String,
    shared: Arc<Shared>,
    accept: Option<JoinHandle<()>>,
    connections: Arc<Mutex<Vec<JoinHandle<()>>>>,
}
fn patience() -> Duration {
    Duration::from_millis(
        budgets::BudgetId::WorkerStartup
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis(),
    )
}
impl FakeGateway {
    pub fn start(script: Script) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let shared = Arc::new(Shared {
            script: Mutex::new(script),
            seen: Mutex::default(),
            changed: Condvar::new(),
            stop: AtomicBool::new(false),
            on_delete: Mutex::new(None),
        });
        let bearer = "fake-gateway-bearer-0123456789abcdef".to_owned();
        let connections: Arc<Mutex<Vec<JoinHandle<()>>>> = Arc::default();
        let accept = {
            let (shared, bearer, connections) =
                (shared.clone(), bearer.clone(), connections.clone());
            std::thread::spawn(move || {
                for stream in listener.incoming() {
                    if shared.stop.load(SeqCst) {
                        return;
                    }
                    let Ok(stream) = stream else { continue };
                    let (shared, bearer) = (shared.clone(), bearer.clone());
                    let handle = std::thread::spawn(move || serve(stream, &shared, &bearer));
                    connections.lock().unwrap().push(handle);
                }
            })
        };
        Self {
            addr,
            bearer,
            shared,
            accept: Some(accept),
            connections,
        }
    }
    pub fn addr(&self) -> SocketAddr {
        self.addr
    }
    pub fn bearer(&self) -> String {
        self.bearer.clone()
    }
    #[allow(dead_code)] // not every including test file swaps scripts
    pub fn script(&self, script: Script) {
        *self.shared.script.lock().unwrap() = script;
    }
    /// Run `observe` in every later `DELETE` handler, before the gateway answers it.
    #[allow(dead_code)] // not every including test file observes its cancels
    pub fn on_delete(&self, observe: OnDelete) {
        *self.shared.on_delete.lock().unwrap() = Some(observe);
    }
    pub fn seen<T>(&self, read: impl FnOnce(&Seen) -> T) -> T {
        read(&self.shared.seen.lock().unwrap())
    }
    /// Let a held stream complete.
    pub fn release(&self) {
        self.shared.seen.lock().unwrap().released = true;
        self.shared.changed.notify_all();
    }
    /// Wait (bounded) until `ready` holds for what the gateway saw.
    pub fn wait(&self, what: &str, ready: impl Fn(&Seen) -> bool) {
        let end = Instant::now() + patience();
        let mut seen = self.shared.seen.lock().unwrap();
        while !ready(&seen) {
            let left = end.saturating_duration_since(Instant::now());
            assert!(!left.is_zero(), "fake gateway: never saw {what}: {seen:?}");
            seen = self.shared.changed.wait_timeout(seen, left).unwrap().0;
        }
    }
}
impl Drop for FakeGateway {
    fn drop(&mut self) {
        self.shared.stop.store(true, SeqCst);
        self.shared.changed.notify_all();
        // Wake the accept loop once so it sees the stop.
        drop(TcpStream::connect(self.addr));
        if let Some(accept) = self.accept.take() {
            accept.join().unwrap();
        }
        for handle in std::mem::take(&mut *self.connections.lock().unwrap()) {
            handle.join().unwrap();
        }
    }
}
struct Request {
    method: String,
    path: String,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
}
impl Request {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers
            .iter()
            .find(|(k, _)| k.eq_ignore_ascii_case(name))
            .map(|(_, v)| v.as_str())
    }
}
fn read_request(reader: &mut BufReader<TcpStream>) -> Option<Request> {
    let mut line = String::new();
    reader.read_line(&mut line).ok()?;
    let mut parts = line.split_whitespace();
    let (method, path) = (parts.next()?.to_owned(), parts.next()?.to_owned());
    let mut headers = Vec::new();
    loop {
        line.clear();
        reader.read_line(&mut line).ok()?;
        let trimmed = line.trim_end();
        if trimmed.is_empty() {
            break;
        }
        let (k, v) = trimmed.split_once(':')?;
        headers.push((k.trim().to_owned(), v.trim().to_owned()));
    }
    let length = headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("content-length"))
        .and_then(|(_, v)| v.parse::<usize>().ok())
        .unwrap_or(0);
    let mut body = vec![0; length];
    reader.read_exact(&mut body).ok()?;
    Some(Request {
        method,
        path,
        headers,
        body,
    })
}
fn reply(stream: &mut TcpStream, status: u16, body: &Value) {
    let body = body.to_string();
    let _written = write!(
        stream,
        "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{body}",
        body.len()
    );
}
fn frame(stream: &mut TcpStream, id: &str, sequence: u64, kind: &str, extra: Value) -> bool {
    let mut event =
        json!({"request_id":id,"publisher_generation":1,"sequence":sequence,"type":kind});
    if let (Some(event), Some(extra)) = (event.as_object_mut(), extra.as_object()) {
        event.extend(extra.clone());
    }
    write!(stream, "id: 1:{sequence}\nevent: {kind}\ndata: {event}\n\n")
        .and_then(|()| stream.flush())
        .is_ok()
}
fn serve(stream: TcpStream, shared: &Shared, bearer: &str) {
    let mut reader = BufReader::new(stream.try_clone().unwrap());
    let mut stream = stream;
    let Some(request) = read_request(&mut reader) else {
        return;
    };
    if request.header("authorization") != Some(&format!("Bearer {bearer}")) {
        shared.seen.lock().unwrap().unauthorized += 1;
        reply(&mut stream, 401, &json!({"error":"unauthorized"}));
        return;
    }
    let script = shared.script.lock().unwrap().clone();
    match (request.method.as_str(), request.path.as_str()) {
        ("GET", "/v1/qualification/current") => {
            if script.qualified {
                reply(
                    &mut stream,
                    200,
                    &json!({"qualification_id":"fake-route","model_id":"Qwen/Qwen3-0.6B"}),
                );
            } else {
                reply(&mut stream, 503, &json!({"error":"route_not_ready"}));
            }
        }
        ("POST", "/v1/inference") => {
            let document: Value = serde_json::from_slice(&request.body).unwrap_or(Value::Null);
            let mut seen = shared.seen.lock().unwrap();
            seen.posts.push(document);
            let id = format!("fake-request-{:04}", seen.posts.len());
            shared.changed.notify_all();
            drop(seen);
            if let Some((status, code)) = script.refuse_post {
                reply(&mut stream, status, &json!({ "error": code }));
                return;
            }
            reply(
                &mut stream,
                202,
                &json!({"request_id":id,"stream_path":format!("/v1/inference/{id}/events"),
                    "cancel_path":format!("/v1/inference/{id}"),
                    "session_token":"fake-owner-session-token-0123456789abcdef"}),
            );
        }
        ("DELETE", path) => {
            if let Some(observe) = shared.on_delete.lock().unwrap().as_ref() {
                observe();
            }
            let id = path.trim_start_matches("/v1/inference/").to_owned();
            let mut seen = shared.seen.lock().unwrap();
            seen.deletes += 1;
            seen.cancelled.push(id.clone());
            shared.changed.notify_all();
            drop(seen);
            reply(
                &mut stream,
                202,
                &json!({"request_id":id,"status":"cancelling"}),
            );
        }
        ("GET", path) if path.ends_with("/events") => {
            let id = path
                .trim_start_matches("/v1/inference/")
                .trim_end_matches("/events")
                .to_owned();
            if write!(
                stream,
                "HTTP/1.1 200 OK\r\ncontent-type: text/event-stream; charset=utf-8\r\ncache-control: no-store\r\n\r\n"
            )
            .is_err()
            {
                return;
            }
            let mut sequence = 0;
            if !frame(&mut stream, &id, sequence, "accepted", json!({})) {
                return;
            }
            for (index, text) in script.tokens.iter().enumerate() {
                sequence += 1;
                if !frame(
                    &mut stream,
                    &id,
                    sequence,
                    "token",
                    json!({"token_id":FIRST_TOKEN_ID + index as u64,"text":text,"token_index":index}),
                ) {
                    return;
                }
                shared.seen.lock().unwrap().tokens_sent += 1;
                shared.changed.notify_all();
            }
            sequence += 1;
            match script.end {
                End::Completed => {
                    frame(&mut stream, &id, sequence, "completed", json!({}));
                }
                End::Failed(code) => {
                    frame(&mut stream, &id, sequence, "failed", json!({"code":code}));
                }
                End::Hold | End::Stall => {
                    let end = Instant::now() + patience() + patience();
                    let mut seen = shared.seen.lock().unwrap();
                    loop {
                        if shared.stop.load(SeqCst) || Instant::now() >= end {
                            return;
                        }
                        if matches!(script.end, End::Hold) {
                            if seen.cancelled.contains(&id) {
                                drop(seen);
                                frame(&mut stream, &id, sequence, "cancelled", json!({}));
                                return;
                            }
                            if seen.released {
                                drop(seen);
                                frame(&mut stream, &id, sequence, "completed", json!({}));
                                return;
                            }
                        }
                        let left = end.saturating_duration_since(Instant::now());
                        seen = shared.changed.wait_timeout(seen, left).unwrap().0;
                    }
                }
            }
        }
        _ => reply(&mut stream, 404, &json!({"error":"not_found"})),
    }
}
