// SPDX-License-Identifier: AGPL-3.0-only
//! App-side listing custody (`AppPublisher`) against real loopback directories: the Rust
//! authority and the deployed Python directory (an unchanged copy, explicitly ignored here
//! because it needs an external checkout and interpreter). Owner-run documents are test inputs
//! with the `owner_run.py:188-214` keys the headless status carries; no route is claimed.
#[path = "support/swarm_publisher.rs"]
mod support;
use api::swarm::{
    docs,
    json::parse,
    limits::Limits,
    publisher::{
        DirectoryClient, DirectoryPort, Error,
        app::{AppPublisher, DB_FILE, KEY_FILE, OwnerFacts},
        heartbeat_seconds,
        host::Stop,
    },
    wire,
};
use serde_json::{Map, Value, json};
use std::{
    io::{Read, Write},
    net::TcpListener,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    thread::JoinHandle,
};
use support::{Scratch, Server, set_clock};

const NOW: i64 = 1_800_000_000;
const MODEL: &str = "Qwen/Qwen3-0.6B";
const ENTRY: &str = "https://entry.example.org";

fn body(directory: &str) -> Map<String, Value> {
    json!({"directory": directory, "name": "Café swarm", "access_mode": "request",
           "description": "Public text"})
    .as_object()
    .unwrap()
    .clone()
}
fn owner_run(state: &str, route_ready: bool) -> Value {
    json!({"protocol": "mycelium.owner_run.v1", "state": state, "route_ready": route_ready,
           "registry_origin": if route_ready { json!("http://127.0.0.1:9") } else { Value::Null },
           "participants": [{"node_id": "a"}, {"node_id": "b"}]})
}
fn facts(state: &str, route_ready: bool) -> OwnerFacts {
    OwnerFacts::from_owner_run(
        true,
        Some(&owner_run(state, route_ready)),
        Some(MODEL),
        Some(ENTRY),
    )
}
fn rows(c: &dyn DirectoryPort) -> Vec<Value> {
    c.call("GET", "/swarms", b"").unwrap()["swarms"]
        .as_array()
        .unwrap()
        .clone()
}
fn publisher_files(root: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(root)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with("swarm-publisher"))
        .collect();
    names.sort();
    names
}
/// The bound origin's real client, or a hostile/offline socket while `hostile` is set. The
/// directory origin bound in the durable state stays the real one throughout.
struct Switch {
    real: DirectoryClient,
    hostile: Mutex<Option<DirectoryClient>>,
    calls: AtomicUsize,
    sent: Mutex<Vec<(String, Vec<u8>)>>,
}
impl DirectoryPort for Switch {
    fn call(&self, method: &str, path: &str, body: &[u8]) -> Result<Value, Error> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if method == "POST" {
            self.sent.lock().unwrap().push((path.into(), body.to_vec()));
        }
        match &*self.hostile.lock().unwrap() {
            Some(hostile) => hostile.call(method, path, body),
            None => self.real.call(method, path, body),
        }
    }
}
fn custody(root: &Path, switch: &Arc<Switch>) -> AppPublisher {
    let switch = switch.clone();
    AppPublisher::with_connect(
        root,
        Box::new(move |_origin: &str| Ok(switch.clone() as Arc<dyn DirectoryPort>)),
    )
}
/// A real loopback socket that answers every request with a hostile, unparseable reply.
struct Hostile {
    origin: String,
    stop: Arc<AtomicBool>,
    join: Option<JoinHandle<()>>,
}
impl Hostile {
    fn start() -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = stop.clone();
        let join = std::thread::spawn(move || {
            for socket in listener.incoming() {
                let mut socket = socket.unwrap();
                if stopping.load(Ordering::SeqCst) {
                    break;
                }
                let mut head = [0u8; 4096];
                let read = socket.read(&mut head).unwrap();
                assert!(read > 0);
                socket
                    .write_all(
                        b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 9\r\n\r\n<html>no!",
                    )
                    .unwrap();
            }
        });
        Self {
            origin,
            stop,
            join: Some(join),
        }
    }
}
impl Drop for Hostile {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        std::net::TcpStream::connect(self.origin.strip_prefix("http://").unwrap()).unwrap();
        self.join.take().unwrap().join().unwrap();
    }
}

#[test]
fn opted_out_custody_creates_no_state_and_never_connects() {
    let root = Scratch::new();
    let connects = Arc::new(AtomicUsize::new(0));
    let counted = connects.clone();
    let p = AppPublisher::with_connect(
        &root.0,
        Box::new(
            move |_origin: &str| -> Result<Arc<dyn DirectoryPort>, Error> {
                counted.fetch_add(1, Ordering::SeqCst);
                Err(Error::new("swarm_directory_unreachable"))
            },
        ),
    );
    assert!(!p.resume().unwrap());
    for state in [("serving", true), ("running", false), ("stopped", false)] {
        assert!(p.tick(NOW, &facts(state.0, state.1)).unwrap().is_none());
    }
    assert!(p.stop(NOW, Stop::Quit).unwrap().is_none());
    assert_eq!(connects.load(Ordering::SeqCst), 0);
    assert!(publisher_files(&root.0).is_empty());
    assert_eq!(p.status_json(), json!({"enabled": false, "mode": null}));
}

#[test]
fn publish_refusals_are_specific_and_leave_no_state() {
    let root = Scratch::new();
    let p = AppPublisher::new(&root.0);
    let refuse = |owner: bool, b: Map<String, Value>| p.publish(owner, &b).unwrap_err().code;
    assert_eq!(
        refuse(false, body("http://127.0.0.1:9999")),
        "owner_role_required"
    );
    let mut extra = body("http://127.0.0.1:9999");
    extra.insert("prompt".into(), json!("never listed"));
    assert_eq!(refuse(true, extra), "invalid_request");
    let mut missing = body("http://127.0.0.1:9999");
    missing.remove("name");
    assert_eq!(refuse(true, missing), "invalid_request");
    let link = json!({"link": "mycelium-swarm:x"})
        .as_object()
        .unwrap()
        .clone();
    assert_eq!(refuse(true, link), "swarm_link_mode_unsupported");
    assert_eq!(
        refuse(true, body("http://directory.example.org")),
        "swarm_directory_origin_invalid"
    );
    let mut long = body("http://127.0.0.1:9999");
    long.insert("name".into(), json!("too long".repeat(20)));
    assert_eq!(refuse(true, long), "swarm_name_invalid");
    let mut open = body("http://127.0.0.1:9999");
    open.insert("access_mode".into(), json!("open"));
    assert_eq!(refuse(true, open), "swarm_open_quota_invalid");
    assert!(publisher_files(&root.0).is_empty());
    assert_eq!(
        p.unpublish(false, &Map::new()).unwrap_err().code,
        "owner_role_required"
    );
    let partial = json!({"delete": false}).as_object().unwrap().clone();
    assert_eq!(
        p.unpublish(true, &partial).unwrap_err().code,
        "invalid_request"
    );
}

fn lifecycle(python: bool) {
    let root = Scratch::new();
    let state = root.0.join("state");
    std::fs::create_dir(&state).unwrap();
    set_clock(&root.0, NOW);
    let server = Server::start(&root.0, python);
    let switch = Arc::new(Switch {
        real: DirectoryClient::new(&server.origin).unwrap(),
        hostile: Mutex::new(None),
        calls: AtomicUsize::new(0),
        sent: Mutex::new(Vec::new()),
    });
    let p = custody(&state, &switch);
    let pending = p.publish(true, &body(&server.origin)).unwrap();
    assert_eq!(pending["enabled"], true);
    assert_eq!(pending["phase"], "pending");
    assert_eq!(pending["directory"], server.origin.as_str());
    assert_eq!(publisher_files(&state), [KEY_FILE, DB_FILE]);
    // Opt-in alone publishes nothing: the run must be serving AND route-qualified.
    for (s, ready) in [("running", false), ("serving", false), ("stopped", false)] {
        p.tick(NOW, &facts(s, ready)).unwrap();
        assert!(rows(&switch.real).is_empty(), "{s}");
    }
    let unqualified = OwnerFacts::from_owner_run(
        true,
        Some(&owner_run("serving", true)),
        Some(MODEL),
        Some("http://127.0.0.1:7"),
    );
    p.tick(NOW, &unqualified).unwrap();
    assert!(
        rows(&switch.real).is_empty(),
        "plain-http relay is not a public entry"
    );
    let listed = p.tick(NOW, &facts("serving", true)).unwrap().unwrap();
    assert_eq!(listed.phase, api::swarm::publisher::Phase::Listed);
    let row = rows(&switch.real).remove(0);
    assert_eq!(row["status"], "serving");
    assert_eq!(row["members"], 2);
    let record = &row["record"];
    assert_eq!(record["protocol"], docs::RECORD);
    assert_eq!(record["model"], MODEL);
    assert_eq!(record["entry"]["origin"], ENTRY);
    assert_eq!(record.as_object().unwrap().len(), 11);
    // The directory returns the exact signed bytes; verify them independently here.
    let j = parse(&serde_json::to_vec(record).unwrap()).unwrap();
    docs::check_record(&j, &wire::ed25519_verify, false, false).unwrap();
    let key = std::fs::read(state.join(KEY_FILE)).unwrap();
    let public = ed25519_dalek::SigningKey::from_bytes(&key.try_into().unwrap()).verifying_key();
    assert_eq!(
        record["owner_key"],
        wire::key_text(&public.to_bytes()).unwrap()
    );
    let status = p.status_json();
    assert_eq!(status["phase"], "listed");
    assert_eq!(status["swarm_id"], record["swarm_id"]);
    // Refresh: renewed before the TTL, the listing outlives the first beat.
    let interval = heartbeat_seconds().unwrap();
    let ttl = Limits::source().unwrap().heartbeat_ttl;
    let mut now = NOW;
    while now < NOW + ttl {
        now += interval;
        set_clock(&root.0, now);
        p.tick(now, &facts("serving", true)).unwrap();
    }
    assert_eq!(rows(&switch.real)[0]["status"], "serving");
    // Stop (the run is no longer serving): the listing is deleted, not left offline.
    let stopped = p.tick(now, &facts("stopped", false)).unwrap().unwrap();
    // Still opted in, nothing listed: pending until the route is qualified again.
    assert_eq!(stopped.phase, api::swarm::publisher::Phase::Pending);
    assert!(stopped.swarm_id.is_none());
    assert!(rows(&switch.real).is_empty());
    let id = record["swarm_id"].as_str().unwrap();
    assert_eq!(
        switch
            .real
            .call("GET", &format!("/swarms/{id}"), b"")
            .unwrap_err()
            .code,
        "swarm_directory_swarm_unknown"
    );
    // Serving again republishes under a fresh id (a delete is a permanent tombstone).
    p.tick(now, &facts("serving", true)).unwrap();
    assert_ne!(
        rows(&switch.real)[0]["record"]["swarm_id"],
        record["swarm_id"]
    );
    // Explicit opt-out withdraws and stays out.
    let out = p.unpublish(true, &Map::new()).unwrap();
    assert_eq!(out["enabled"], false);
    p.tick(now, &facts("serving", true)).unwrap();
    assert!(rows(&switch.real).is_empty());
    // Opting in again, then the installation stops being the owner (it joined another swarm as
    // a member): the listing is withdrawn and the opt-in revoked, not carried into the new role.
    p.publish(true, &body(&server.origin)).unwrap();
    p.tick(now, &facts("serving", true)).unwrap();
    assert_eq!(rows(&switch.real).len(), 1);
    let member = OwnerFacts::from_owner_run(false, None, None, None);
    let left = p.tick(now, &member).unwrap().unwrap();
    assert!(!left.enabled);
    assert!(rows(&switch.real).is_empty());
    // Only the closed v1 documents left this host: no run facts, prompts or identities.
    let sent = switch.sent.lock().unwrap();
    for path in ["/records", "/heartbeat", "/delete"] {
        assert!(sent.iter().any(|(p, _)| p == path), "{path} was exercised");
    }
    for (path, bytes) in sent.iter() {
        let doc: Value = serde_json::from_slice(bytes).unwrap();
        let mut keys: Vec<&str> = doc
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        keys.sort_unstable();
        let expected: &[&str] = match path.as_str() {
            "/records" => &[
                "access_mode",
                "description",
                "entry",
                "model",
                "name",
                "open_quota",
                "owner_key",
                "protocol",
                "seq",
                "sig",
                "swarm_id",
            ],
            "/heartbeat" => &[
                "at",
                "members",
                "protocol",
                "served_model",
                "sig",
                "signer_key",
                "status",
                "swarm_id",
            ],
            "/delete" => &["owner_key", "protocol", "seq", "sig", "swarm_id"],
            other => panic!("unexpected document {other}"),
        };
        assert_eq!(keys, expected, "{path}");
        let text = String::from_utf8(bytes.clone()).unwrap();
        for leaked in [
            "node_id",
            "http://127.0.0.1:9\"",
            "participants",
            "registry",
        ] {
            assert!(!text.contains(leaked), "{path} leaked {leaked}");
        }
    }
}

fn debt(python: bool) {
    let root = Scratch::new();
    let state = root.0.join("state");
    std::fs::create_dir(&state).unwrap();
    set_clock(&root.0, NOW);
    let mut server = Server::start(&root.0, python);
    let switch = Arc::new(Switch {
        real: DirectoryClient::new(&server.origin).unwrap(),
        hostile: Mutex::new(None),
        calls: AtomicUsize::new(0),
        sent: Mutex::new(Vec::new()),
    });
    let p = custody(&state, &switch);
    p.publish(true, &body(&server.origin)).unwrap();
    p.tick(NOW, &facts("serving", true)).unwrap();
    assert_eq!(rows(&switch.real).len(), 1);
    // A hostile directory refuses with garbage: quit keeps durable withdrawal debt.
    let hostile = Hostile::start();
    *switch.hostile.lock().unwrap() = Some(DirectoryClient::new(&hostile.origin).unwrap());
    let quit = p.stop(NOW, Stop::Quit).unwrap().unwrap();
    assert_eq!(quit.phase, api::swarm::publisher::Phase::WithdrawPending);
    assert!(quit.error_code.is_some());
    assert_eq!(p.status_json()["phase"], "withdraw_pending");
    drop(p);
    *switch.hostile.lock().unwrap() = None;
    assert_eq!(rows(&switch.real).len(), 1, "the listing is still there");
    // Offline at the next start: the debt reopens from disk and waits for its retry slot.
    server.stop();
    let p = custody(&state, &switch);
    assert!(p.resume().unwrap());
    assert_eq!(p.status_json()["phase"], "withdraw_pending");
    let before = switch.calls.load(Ordering::SeqCst);
    p.tick(NOW, &facts("stopped", false)).unwrap();
    assert_eq!(
        switch.calls.load(Ordering::SeqCst),
        before,
        "bounded retry cadence"
    );
    let next = NOW + heartbeat_seconds().unwrap();
    let offline = p.tick(next, &facts("stopped", false)).unwrap().unwrap();
    assert_eq!(
        offline.error_code.as_deref(),
        Some("swarm_directory_unreachable")
    );
    // A republish is blocked while the debt exists.
    assert_eq!(
        p.publish(true, &body("http://127.0.0.1:9999"))
            .unwrap_err()
            .code,
        "swarm_withdrawal_pending"
    );
    drop(p);
    server.restart();
    let p = custody(&state, &switch);
    assert!(p.resume().unwrap());
    let later = next + heartbeat_seconds().unwrap();
    set_clock(&root.0, later);
    let done = p.tick(later, &facts("stopped", false)).unwrap().unwrap();
    assert_eq!(done.phase, api::swarm::publisher::Phase::Pending);
    assert!(done.swarm_id.is_none() && done.error_code.is_none());
    assert!(rows(&switch.real).is_empty());
    assert_eq!(p.status_json()["phase"], "pending");
}

#[test]
fn rust_authority_publish_refresh_and_withdraw_on_stop() {
    lifecycle(false);
}
#[test]
fn rust_authority_failed_withdrawal_keeps_debt_until_a_retry_succeeds() {
    debt(false);
}
#[test]
#[ignore = "requires the deployed Python directory copy + cryptography interpreter; explicit loopback qualification"]
fn deployed_python_directory_publish_refresh_and_withdraw_on_stop() {
    lifecycle(true);
}
#[test]
#[ignore = "requires the deployed Python directory copy + cryptography interpreter; explicit loopback qualification"]
fn deployed_python_directory_failed_withdrawal_keeps_debt_until_a_retry_succeeds() {
    debt(true);
}
