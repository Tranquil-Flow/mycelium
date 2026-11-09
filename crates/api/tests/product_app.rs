// SPDX-License-Identifier: AGPL-3.0-only
//! The product evidence application (`mycelium_product_spine/app.py` @ f4e366e9) and its browser
//! routes (`mycelium_ui_gateway/app.py:348-352, 591-670`) over test sources: publication per GET,
//! ordered and resumable events (`Last-Event-ID`), the stale-cursor refusal, the bounded replay
//! window (memory and disk), restart continuity, source degradation and privacy. The serving inputs
//! are the protocol crate's contract fixtures (test inputs: no Rust host produces a live route yet).
use api::{
    http::{Reply, Request},
    product_app::{
        self, DEFAULT_REPLAY_LIMIT, EVENTS_PATH, EXPORT_PATH, Events, ProductApp, SNAPSHOT_PATH,
        STATE_FILE, SourceError, Sources,
    },
    product_projector::ProductProjector,
};
use serde_json::{Map, Value, json};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
};

const NOW: u64 = 1_900_000_000_000;
const RAW_NODE: &str = "owner-node-raw-id";

fn contract(name: &str) -> Value {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../protocol/fixtures/contracts")
        .join(name);
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}

fn member(node: &str, state: &str) -> Map<String, Value> {
    json!({
        "node_id": node, "endpoint_addrs": ["192.168.1.20:4433"], "peer_class": "mac_mlx_iroh",
        "runtime_capability": {"runtime_backend": "mlx", "transport": "iroh",
                               "activation_protocol": "mycelium.router_wire.v1"},
        "activation_eligible": true, "incarnation": "incarnation-1", "generation": 1,
        "lease_expires_at": (NOW / 1000 + 300) as f64, "last_liveness_at": (NOW / 1000) as f64,
        "lifecycle_state": state, "verification_key_digest": format!("sha256:{}", "1".repeat(64)),
    })
    .as_object()
    .unwrap()
    .clone()
}

/// Test sources whose answers the test changes between publications.
struct Fake {
    members: Mutex<Option<Vec<Map<String, Value>>>>,
    route: Mutex<Result<Option<Value>, SourceError>>,
    qualification: Mutex<Option<Value>>,
    recovery: Mutex<Option<Value>>,
}
impl Fake {
    /// What the headless owner has right after creating its swarm: one member, nothing serving.
    fn owner() -> Arc<Self> {
        Arc::new(Self {
            members: Mutex::new(Some(vec![member(RAW_NODE, "RUNNING")])),
            route: Mutex::new(Ok(None)),
            qualification: Mutex::new(None),
            recovery: Mutex::new(None),
        })
    }
    fn serve(&self) {
        let mut route = contract("live-route-status-v1.json");
        route["route_alive"] = json!(true);
        let mut qualification = contract("route-qualification-v1.json");
        for key in ["deployment_id", "model_id", "topology_version"] {
            qualification[key] = route[key].clone();
        }
        qualification["route_ready"] = json!(true);
        *self.members.lock().unwrap() = Some(vec![
            member("node-a", "RUNNING"),
            member("node-b", "RUNNING"),
        ]);
        *self.route.lock().unwrap() = Ok(Some(route));
        *self.qualification.lock().unwrap() = Some(qualification);
        *self.recovery.lock().unwrap() = Some(contract("full-context-recovery-runtime-v1.json"));
    }
}
impl Sources for Fake {
    fn members(&self) -> Result<Vec<Map<String, Value>>, SourceError> {
        self.members
            .lock()
            .unwrap()
            .clone()
            .ok_or(SourceError::Failed)
    }
    fn route_status(&self) -> Result<Option<Value>, SourceError> {
        self.route.lock().unwrap().clone()
    }
    fn qualification(&self) -> Result<Option<Value>, SourceError> {
        Ok(self.qualification.lock().unwrap().clone())
    }
    fn recovery(&self) -> Result<Option<Value>, SourceError> {
        Ok(self.recovery.lock().unwrap().clone())
    }
}

fn clock() -> (Arc<AtomicU64>, Box<dyn Fn() -> u64 + Send + Sync>) {
    let now = Arc::new(AtomicU64::new(NOW));
    let read = now.clone();
    (now, Box::new(move || read.fetch_add(1, Ordering::SeqCst)))
}

fn app(sources: Arc<Fake>, limit: usize, root: Option<&Path>) -> ProductApp {
    ProductApp::new(
        ProductProjector::new(&[3; 32]).unwrap(),
        sources,
        clock().1,
        limit,
        root,
    )
    .unwrap()
}

fn get(path: &str, last_event_id: &[&str]) -> Request {
    Request {
        method: "GET".into(),
        target: path.into(),
        headers: last_event_id
            .iter()
            .map(|v| ("Last-Event-ID".to_owned(), (*v).to_owned()))
            .collect(),
        body: Vec::new(),
    }
}

fn json_of(reply: &Reply) -> Value {
    serde_json::from_slice(&reply.body).unwrap()
}

/// The frames of a finite product event stream: `(id, event)`.
fn frames(reply: &Reply) -> Vec<(u64, Value)> {
    assert_eq!(
        reply.header("content-type"),
        Some("text/event-stream; charset=utf-8")
    );
    let text = std::str::from_utf8(&reply.body).unwrap();
    assert!(text.is_empty() || text.ends_with("\n\n"));
    text.split_terminator("\n\n")
        .map(|frame| {
            let lines: Vec<&str> = frame.split('\n').collect();
            assert_eq!(lines.len(), 3, "{frame}");
            assert_eq!(lines[1], "event: product_snapshot");
            let id: u64 = lines[0].strip_prefix("id: ").unwrap().parse().unwrap();
            let event: Value =
                serde_json::from_str(lines[2].strip_prefix("data: ").unwrap()).unwrap();
            protocol::product::event(&event).expect("a valid product event");
            assert_eq!(event["cursor"], json!(id));
            (id, event)
        })
        .collect()
}

fn private_dir(base: &Path, name: &str) -> PathBuf {
    let root = base.join(name);
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    root
}

struct Scratch(PathBuf);
impl Scratch {
    fn new(label: &str) -> Self {
        let base = std::env::temp_dir().join(format!("b13-product-{label}-{}", std::process::id()));
        fs::remove_dir_all(&base).unwrap_or(());
        fs::create_dir_all(&base).unwrap();
        Self(fs::canonicalize(base).unwrap())
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap_or(());
    }
}

#[test]
fn without_an_application_every_route_is_unavailable_and_retryable() {
    for (path, code) in [
        (SNAPSHOT_PATH, "product_snapshot_unavailable"),
        (EXPORT_PATH, "product_export_unavailable"),
        (EVENTS_PATH, "product_events_unavailable"),
    ] {
        let reply = product_app::handle(None, &get(path, &[])).unwrap();
        assert_eq!(reply.status, 503);
        assert_eq!(
            json_of(&reply),
            json!({"protocol": "mycelium.product_ui.error.v1", "code": code, "retryable": true})
        );
    }
    let mut post = get(SNAPSHOT_PATH, &[]);
    post.method = "POST".into();
    let reply = product_app::handle(None, &post).unwrap();
    assert_eq!((reply.status, reply.header("allow")), (405, Some("GET")));
}

#[test]
fn every_get_publishes_the_next_snapshot_and_events_resume_after_the_cursor() {
    let fake = Fake::owner();
    let app = app(fake.clone(), DEFAULT_REPLAY_LIMIT, None);
    let first = product_app::handle(Some(&app), &get(SNAPSHOT_PATH, &[])).unwrap();
    assert_eq!(first.status, 200);
    let first = json_of(&first);
    protocol::product::snapshot(&first).unwrap();
    assert_eq!(first["publication"]["cursor"], 1);
    let export = json_of(&product_app::handle(Some(&app), &get(EXPORT_PATH, &[])).unwrap());
    assert_eq!(export["publication"]["cursor"], 2);
    // No Last-Event-ID: every retained event, then the one this GET published, in order.
    let all = frames(&product_app::handle(Some(&app), &get(EVENTS_PATH, &[])).unwrap());
    assert_eq!(all.iter().map(|f| f.0).collect::<Vec<_>>(), [1, 2, 3]);
    assert_eq!(all[0].1["snapshot"], first);
    for (id, event) in &all {
        assert_eq!(event["previous_cursor"], json!(id - 1));
        assert_eq!(event["event_kind"], "snapshot_published");
    }
    // Resume after the last seen cursor: only what is new.
    let resumed = frames(&product_app::handle(Some(&app), &get(EVENTS_PATH, &["3"])).unwrap());
    assert_eq!(resumed.iter().map(|f| f.0).collect::<Vec<_>>(), [4]);
    // A cursor ahead of the sequence: nothing yet (the publication still happened).
    let ahead = product_app::handle(Some(&app), &get(EVENTS_PATH, &["99"])).unwrap();
    assert_eq!((ahead.status, frames(&ahead).len()), (200, 0));
    assert_eq!(app.retained(), 5);
    // Source transitions show up in the next publication: the route starts serving.
    fake.serve();
    let serving = frames(&product_app::handle(Some(&app), &get(EVENTS_PATH, &["5"])).unwrap());
    let snapshot = &serving[0].1["snapshot"];
    assert_eq!(snapshot["publication"]["source_mode"], "live");
    let kinds: Vec<&str> = snapshot["entities"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|e| e["kind"].as_str())
        .collect();
    for kind in [
        "route",
        "stage",
        "qualification",
        "recovery",
        "directed_link",
        "device",
    ] {
        assert!(kinds.contains(&kind), "{kind}: {kinds:?}");
    }
    // ... and stops: the route source fails with a bounded public reason.
    *fake.route.lock().unwrap() = Err(SourceError::Unavailable("route_cold_restart"));
    *fake.qualification.lock().unwrap() = None;
    let stopped = json_of(&product_app::handle(Some(&app), &get(SNAPSHOT_PATH, &[])).unwrap());
    let route = stopped["source_states"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["source_id"] == "route-source")
        .unwrap();
    assert_eq!(
        (&route["status"], &route["reason_code"]),
        (&json!("missing"), &json!("route_cold_restart"))
    );
    assert_eq!(stopped["publication"]["source_mode"], "degraded");
}

#[test]
fn a_cursor_older_than_the_window_is_stale_and_the_window_is_bounded() {
    let app = app(Fake::owner(), DEFAULT_REPLAY_LIMIT, None);
    let publications = DEFAULT_REPLAY_LIMIT * 3;
    for _ in 0..publications {
        app.snapshot().unwrap();
    }
    assert_eq!(
        app.retained(),
        DEFAULT_REPLAY_LIMIT,
        "memory stays at the cap"
    );
    // The events GET publishes cursor N+1; the window then holds N+1-127 ..= N+1.
    let next = publications as u64 + 1;
    let floor = next + 1 - DEFAULT_REPLAY_LIMIT as u64;
    match app.events(Some(floor - 2)).unwrap() {
        Events::Stale => {}
        Events::After(events) => panic!("expected stale, got {}", events.len()),
    }
    // `floor - 1` was complete before this GET; its own publication moves the floor past it.
    let reply =
        product_app::handle(Some(&app), &get(EVENTS_PATH, &[&(floor - 1).to_string()])).unwrap();
    assert_eq!(
        reply.status, 409,
        "the floor moved by this GET's publication"
    );
    assert_eq!(json_of(&reply)["code"], "product_cursor_stale");
    // `cursor == floor - 1` is still complete: the whole window replays.
    let edge = frames(
        &product_app::handle(Some(&app), &get(EVENTS_PATH, &[&(floor + 1).to_string()])).unwrap(),
    );
    assert_eq!(edge.len(), DEFAULT_REPLAY_LIMIT);
    assert_eq!(edge[0].0, floor + 2);
    assert_eq!(app.retained(), DEFAULT_REPLAY_LIMIT);
}

#[test]
fn a_malformed_last_event_id_is_refused_before_anything_is_published() {
    let app = app(Fake::owner(), DEFAULT_REPLAY_LIMIT, None);
    for values in [
        &["abc"][..],
        &[""],
        &["-1"],
        &["1.0"],
        &["٣"],
        &["9007199254740992"],
        &["1", "2"],
    ] {
        let reply = product_app::handle(Some(&app), &get(EVENTS_PATH, values)).unwrap();
        assert_eq!(reply.status, 400, "{values:?}");
        assert_eq!(json_of(&reply)["code"], "invalid_last_event_id");
    }
    assert_eq!(app.retained(), 0);
    let largest =
        product_app::handle(Some(&app), &get(EVENTS_PATH, &["9007199254740991"])).unwrap();
    assert_eq!(largest.status, 200);
}

#[test]
fn the_window_survives_a_restart_and_the_sequence_continues() {
    let scratch = Scratch::new("restart");
    let root = private_dir(&scratch.0, "product");
    let limit = 4;
    {
        let app = app(Fake::owner(), limit, Some(&root));
        for _ in 0..6 {
            app.snapshot().unwrap();
        }
    }
    let path = root.join(STATE_FILE);
    let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o7777;
    assert_eq!(mode, 0o600);
    let stored: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    assert_eq!(stored["protocol"], "mycelium.product_evidence_state.v1");
    assert_eq!(stored["replay_limit"], json!(limit));
    let cursors: Vec<u64> = stored["events"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e["cursor"].as_u64().unwrap())
        .collect();
    assert_eq!(cursors, [3, 4, 5, 6], "only the window is stored");
    // A new process restores the window: a client resumes with its old cursor.
    let reopened = app(Fake::owner(), limit, Some(&root));
    let resumed = frames(&product_app::handle(Some(&reopened), &get(EVENTS_PATH, &["5"])).unwrap());
    assert_eq!(resumed.iter().map(|f| f.0).collect::<Vec<_>>(), [6, 7]);
    assert_eq!(resumed[1].1["snapshot"]["publication"]["generation"], 7);
    // A stored window for another replay limit, or a tampered one, is refused, never rewritten.
    let before = fs::read(&path).unwrap();
    let refused = ProductApp::new(
        ProductProjector::new(&[3; 32]).unwrap(),
        Fake::owner(),
        clock().1,
        limit + 1,
        Some(&root),
    );
    assert_eq!(refused.err().unwrap().0, "product_evidence_state_corrupt");
    let mut tampered: Value = serde_json::from_slice(&before).unwrap();
    tampered["events"][1]["cursor"] = json!(40);
    fs::write(&path, serde_json::to_vec(&tampered).unwrap()).unwrap();
    let refused = ProductApp::new(
        ProductProjector::new(&[3; 32]).unwrap(),
        Fake::owner(),
        clock().1,
        limit,
        Some(&root),
    );
    assert_eq!(refused.err().unwrap().0, "product_evidence_state_corrupt");
    // A world-readable root is refused (owner-only state).
    let open = private_dir(&scratch.0, "open");
    fs::set_permissions(&open, fs::Permissions::from_mode(0o755)).unwrap();
    let refused = ProductApp::new(
        ProductProjector::new(&[3; 32]).unwrap(),
        Fake::owner(),
        clock().1,
        limit,
        Some(&open),
    );
    assert_eq!(
        refused.err().unwrap().0,
        "product_evidence_state_root_invalid"
    );
}

#[test]
fn a_failing_membership_source_degrades_and_a_failing_projection_is_unavailable() {
    let fake = Fake::owner();
    *fake.members.lock().unwrap() = None;
    let app = app(fake.clone(), DEFAULT_REPLAY_LIMIT, None);
    let snapshot = json_of(&product_app::handle(Some(&app), &get(SNAPSHOT_PATH, &[])).unwrap());
    let membership = &snapshot["source_states"][0];
    assert_eq!(membership["reason_code"], "membership_source_failed");
    assert_eq!(snapshot["publication"]["source_mode"], "degraded");
    // A route document the projector cannot read fails the whole publication (oracle: the
    // exception escapes `_publish`): 503, and the sequence does not advance.
    *fake.members.lock().unwrap() = Some(vec![member(RAW_NODE, "RUNNING")]);
    let mut route = contract("live-route-status-v1.json");
    route["topology_version"] = json!("not a number");
    *fake.route.lock().unwrap() = Ok(Some(route));
    let reply = product_app::handle(Some(&app), &get(SNAPSHOT_PATH, &[])).unwrap();
    assert_eq!(reply.status, 503);
    assert_eq!(json_of(&reply)["code"], "product_snapshot_unavailable");
    *fake.route.lock().unwrap() = Ok(None);
    let next = json_of(&product_app::handle(Some(&app), &get(SNAPSHOT_PATH, &[])).unwrap());
    assert_eq!(next["publication"]["cursor"], 2);
}

#[test]
fn nothing_private_reaches_a_snapshot_or_an_event() {
    let fake = Fake::owner();
    let app = app(fake.clone(), DEFAULT_REPLAY_LIMIT, None);
    let mut bodies = vec![
        product_app::handle(Some(&app), &get(SNAPSHOT_PATH, &[]))
            .unwrap()
            .body,
    ];
    fake.serve();
    bodies.push(
        product_app::handle(Some(&app), &get(EVENTS_PATH, &[]))
            .unwrap()
            .body,
    );
    for body in bodies {
        let text = String::from_utf8(body).unwrap();
        for private in [
            RAW_NODE,
            "node-a",
            "192.168.1.20",
            "endpoint_addrs",
            "verification_key",
        ] {
            assert!(!text.contains(private), "{private} leaked");
        }
        for key in ["\"prompt\"", "\"tokens\"", "\"completion\"", "\"message\""] {
            assert!(!text.contains(key), "{key}");
        }
    }
}

#[test]
fn a_replay_limit_outside_the_oracle_bounds_is_refused() {
    for limit in [0, product_app::MAX_REPLAY_LIMIT + 1] {
        let refused = ProductApp::new(
            ProductProjector::new(&[3; 32]).unwrap(),
            Fake::owner(),
            clock().1,
            limit,
            None,
        );
        assert_eq!(refused.err().unwrap().0, "product_replay_limit_invalid");
    }
}
