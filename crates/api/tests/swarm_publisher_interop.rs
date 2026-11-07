// SPDX-License-Identifier: AGPL-3.0-only
//! Live TCP qualification: identical Rust publisher scenarios against both real authorities.
#[path = "support/swarm_publisher.rs"]
mod support;
use api::swarm::{
    docs,
    json::parse,
    limits::Limits,
    publisher::{
        DirectoryClient, DirectoryPort, Error, Listing, OwnerState, Phase, SwarmPublisher,
        heartbeat_seconds,
    },
    wire,
};
use ed25519_dalek::{Signer as _, SigningKey};
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use support::{Scratch, Server, set_clock};
const NOW: i64 = 1_800_000_000;
fn options() -> Listing {
    Listing {
        name: "Café swarm".into(),
        description: "Public text".into(),
        access_mode: "request".into(),
        open_quota: None,
    }
}
fn serving() -> OwnerState {
    OwnerState::Serving {
        model: "Qwen/Qwen3-0.6B".into(),
        members: 2,
        entry_origin: "https://entry.example.org".into(),
    }
}
fn rows(c: &dyn DirectoryPort) -> Vec<Value> {
    c.call("GET", "/swarms", b"").unwrap()["swarms"]
        .as_array()
        .unwrap()
        .clone()
}
fn sign(mut doc: Value) -> Vec<u8> {
    doc.as_object_mut().unwrap().remove("sig");
    let j = parse(&serde_json::to_vec(&doc).unwrap()).unwrap();
    let sig = SigningKey::from_bytes(&[7; 32]).sign(&wire::signing_bytes(&j).unwrap());
    wire::canonical(&j.with(
        "sig",
        api::swarm::json::J::str(&wire::b64url(&sig.to_bytes())),
    ))
    .unwrap()
}
// Inject only the outgoing fault; the refusal is produced by the real server.
struct FaultPort {
    client: Arc<DirectoryClient>,
    fault: Mutex<Option<&'static str>>,
    sent: Mutex<Vec<(String, Value)>>,
}
impl DirectoryPort for FaultPort {
    fn call(&self, method: &str, path: &str, body: &[u8]) -> Result<Value, Error> {
        let mut body = body.to_vec();
        if method == "POST" {
            let mut doc: Value = serde_json::from_slice(&body).unwrap();
            match *self.fault.lock().unwrap() {
                Some("version") if path == "/records" => {
                    doc["protocol"] = json!("mycelium.swarm.record.v3");
                    body = sign(doc.clone());
                }
                Some("signature") if path == "/heartbeat" => {
                    doc["members"] = json!(63);
                    body = serde_json::to_vec(&doc).unwrap();
                }
                Some("expired") if path == "/heartbeat" => {
                    doc["at"] = json!(0);
                    body = sign(doc.clone());
                }
                Some("oversized") => {
                    body = vec![b' '; Limits::source().unwrap().max_body + 1];
                }
                _ => {}
            }
            self.sent.lock().unwrap().push((path.into(), doc));
        }
        self.client.call(method, path, &body)
    }
}
fn lifecycle(python: bool) {
    let root = Scratch::new();
    set_clock(&root.0, NOW);
    let mut server = Server::start(&root.0, python);
    let client = Arc::new(DirectoryClient::new(&server.origin).unwrap());
    let capture = Arc::new(FaultPort {
        client: client.clone(),
        fault: Mutex::new(None),
        sent: Mutex::new(Vec::new()),
    });
    let path = root.0.join("publisher.sqlite3");
    let mut p = SwarmPublisher::open(
        &path,
        SigningKey::from_bytes(&[7; 32]),
        capture.clone(),
        &server.origin,
    )
    .unwrap();
    p.reconcile(NOW, &serving()).unwrap();
    assert!(rows(&*client).is_empty());
    p.set_listing(Some(options())).unwrap();
    assert_eq!(
        p.reconcile(NOW, &OwnerState::Warming { members: 2 })
            .unwrap()
            .phase,
        Phase::Pending
    );
    assert!(rows(&*client).is_empty());
    assert_eq!(p.reconcile(NOW, &serving()).unwrap().phase, Phase::Listed);
    let first = rows(&*client).remove(0);
    assert_eq!(first["record"]["protocol"], docs::RECORD);
    assert_eq!(first["status"], "serving");
    assert_eq!(first["members"], 2);
    assert_eq!(first["record"]["name"], "Café swarm");
    assert_eq!(first["record"].as_object().unwrap().len(), 11);
    assert_eq!(p.status().protocol_version, 1);
    let interval = heartbeat_seconds().unwrap();
    let now = NOW + interval;
    set_clock(&root.0, now);
    p.reconcile(now, &serving()).unwrap();
    let ttl = Limits::source().unwrap().heartbeat_ttl;
    set_clock(&root.0, NOW + ttl); // first beat would expire; renewed beat remains live
    assert_eq!(rows(&*client)[0]["status"], "serving");
    set_clock(&root.0, now);
    assert_eq!(
        p.reconcile(now, &OwnerState::Warming { members: 3 })
            .unwrap()
            .phase,
        Phase::Listed
    );
    assert_eq!(rows(&*client)[0]["status"], "warming");
    let changed = OwnerState::Serving {
        model: "Qwen/Qwen3-8B".into(),
        members: 4,
        entry_origin: "https://new-entry.example.org".into(),
    };
    let mut opts = options();
    opts.access_mode = "invite".into();
    p.set_listing(Some(opts)).unwrap();
    p.reconcile(now, &changed).unwrap();
    let updated = rows(&*client).remove(0);
    assert_eq!(updated["record"]["model"], "Qwen/Qwen3-8B");
    assert_eq!(
        updated["record"]["entry"]["origin"],
        "https://new-entry.example.org"
    );
    assert_eq!(updated["access_mode"], "invite");
    assert_eq!(updated["members"], 4);
    assert!(updated["record"]["seq"].as_u64().unwrap() > first["record"]["seq"].as_u64().unwrap());
    for (path, doc) in capture.sent.lock().unwrap().iter() {
        if path == "/heartbeat" {
            assert_eq!(doc["protocol"], docs::HEARTBEAT);
            let keys = doc
                .as_object()
                .unwrap()
                .keys()
                .map(String::as_str)
                .collect::<Vec<_>>();
            assert_eq!(
                keys,
                [
                    "at",
                    "members",
                    "protocol",
                    "served_model",
                    "sig",
                    "signer_key",
                    "status",
                    "swarm_id"
                ]
            );
        }
        assert!(doc.get("participants").is_none());
        assert!(doc.get("prompt").is_none());
        assert!(doc.get("registry_origin").is_none());
        assert!(doc.get("local_path").is_none());
    }
    server.stop();
    p.set_listing(None).unwrap();
    let pending = p.reconcile(now, &OwnerState::Quit).unwrap();
    assert_eq!(pending.phase, Phase::WithdrawPending);
    assert!(!pending.enabled);
    assert_eq!(
        pending.error_code.as_deref(),
        Some("swarm_directory_unreachable")
    );
    drop(p);
    let mut p = SwarmPublisher::open(
        &path,
        SigningKey::from_bytes(&[7; 32]),
        capture.clone(),
        &server.origin,
    )
    .unwrap();
    assert_eq!(p.status().phase, Phase::WithdrawPending);
    let count = capture.sent.lock().unwrap().len();
    p.reconcile(now, &OwnerState::Quit).unwrap();
    assert_eq!(capture.sent.lock().unwrap().len(), count);
    server.restart();
    let now = now + interval;
    set_clock(&root.0, now);
    assert_eq!(
        p.reconcile(now, &OwnerState::Quit).unwrap().phase,
        Phase::Unlisted
    );
    assert!(rows(&*client).is_empty());
    p.set_listing(Some(options())).unwrap();
    p.reconcile(now, &serving()).unwrap();
    assert_ne!(
        rows(&*client)[0]["record"]["swarm_id"],
        first["record"]["swarm_id"]
    );
    p.reconcile(now, &OwnerState::Left).unwrap();
    assert!(rows(&*client).is_empty());
}
fn refusals(python: bool) {
    for (fault, expected) in [
        ("signature", "swarm_directory_signature_invalid"),
        ("expired", "swarm_directory_clock_skew"),
        ("oversized", "swarm_directory_body_too_large"),
        ("version", "swarm_directory_version_unsupported"),
    ] {
        let root = Scratch::new();
        set_clock(&root.0, NOW);
        let server = Server::start(&root.0, python);
        let client = Arc::new(DirectoryClient::new(&server.origin).unwrap());
        let port = Arc::new(FaultPort {
            client: client.clone(),
            fault: Mutex::new(Some(fault)),
            sent: Mutex::new(Vec::new()),
        });
        let mut p = SwarmPublisher::memory(SigningKey::from_bytes(&[7; 32]), port.clone()).unwrap();
        p.set_listing(Some(options())).unwrap();
        let state = p.reconcile(NOW, &serving()).unwrap();
        assert_eq!(state.phase, Phase::Failed, "{fault}");
        assert_eq!(state.error_code.as_deref(), Some(expected), "{fault}");
        let before = port.sent.lock().unwrap().len();
        p.reconcile(NOW, &serving()).unwrap();
        assert_eq!(port.sent.lock().unwrap().len(), before);
        *port.fault.lock().unwrap() = None;
        let now = NOW + heartbeat_seconds().unwrap();
        set_clock(&root.0, now);
        assert_eq!(p.reconcile(now, &serving()).unwrap().phase, Phase::Listed);
        assert_eq!(rows(&*client)[0]["status"], "serving");
        p.set_listing(None).unwrap();
        p.reconcile(now, &OwnerState::Quit).unwrap();
        assert!(rows(&*client).is_empty());
    }
    let root = Scratch::new();
    set_clock(&root.0, NOW);
    let server = Server::start(&root.0, python);
    let client = Arc::new(DirectoryClient::new(&server.origin).unwrap());
    for _ in 0..Limits::source().unwrap().writes_per_ip_per_minute {
        assert_eq!(
            client.call("POST", "/records", b"{}").unwrap_err().code,
            "swarm_directory_version_unsupported"
        );
    }
    let mut p = SwarmPublisher::memory(SigningKey::from_bytes(&[7; 32]), client.clone()).unwrap();
    p.set_listing(Some(options())).unwrap();
    assert_eq!(
        p.reconcile(NOW, &serving()).unwrap().error_code.as_deref(),
        Some("swarm_directory_rate_limited_ip")
    );
    let window = budgets::BudgetId::PyDirectoryRateWindow
        .span(budgets::Entries::ZERO)
        .unwrap()
        .millis()
        / 1000;
    let now = NOW + window as i64;
    set_clock(&root.0, now);
    assert_eq!(p.reconcile(now, &serving()).unwrap().phase, Phase::Listed);
    p.set_listing(None).unwrap();
    p.reconcile(now, &OwnerState::Quit).unwrap();
    assert!(rows(&*client).is_empty());
}
#[test]
fn rust_authority_lifecycle_over_tcp() {
    lifecycle(false);
}
#[test]
fn rust_authority_refusals_over_tcp() {
    refusals(false);
}
#[test]
#[ignore = "requires read-only Python oracle checkout + cryptography interpreter; explicit loopback qualification"]
fn python_directory_lifecycle_over_tcp() {
    lifecycle(true);
}
#[test]
#[ignore = "requires read-only Python oracle checkout + cryptography interpreter; explicit loopback qualification"]
fn python_directory_refusals_over_tcp() {
    refusals(true);
}
