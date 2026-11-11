// SPDX-License-Identifier: AGPL-3.0-only
//! The HTTP actions persist exactly as v0.1.4 (app.py:1356-1361, fast_mode.py:53-69,
//! app.py:855-868): `/api/fast-mode` and `/api/run-model` write the private preference files
//! through `PersistentBackend`; every other action is delegated untouched. Expected documents
//! come from the executed oracle fixture (`L10-evidence/r7-probe-run-model.py`) and the round-6
//! preference probe.
use api::{
    actions::{Action, Backend, Control},
    desktop::{Desktop, Service, Session},
    http::serve_connection,
    persistent::{Host, PersistentBackend},
    schema::{PublicError, Snapshot},
    status::PublishedStatus,
};
use budgets::BudgetId;
use journey_tests::{compare, duration, http::Client, project};
use serde_json::{Map, Value, json};
use std::{
    fs,
    net::{Ipv4Addr, TcpListener},
    os::unix::fs::MetadataExt,
    path::PathBuf,
    sync::{Arc, Mutex},
    thread,
    time::Instant,
};

const GIB: u64 = 1 << 30;
#[derive(Default)]
struct State {
    closed: bool,
    target: bool,
    run_active: bool,
    acquisition_active: bool,
    owner_run_active: bool,
    env: Vec<(String, String)>,
    memory: Option<u64>,
    trace: Vec<String>,
    /// This Mac's member node id (`authority.member.node_id`).
    member: Option<String>,
}
#[derive(Clone, Default)]
struct FakeHost(Arc<Mutex<State>>);
impl FakeHost {
    fn with<R>(&self, f: impl FnOnce(&mut State) -> R) -> R {
        f(&mut self.0.lock().unwrap())
    }
}
impl Host for FakeHost {
    fn closed(&self) -> bool {
        self.with(|s| s.closed)
    }
    fn owner(&self) -> bool {
        self.with(|s| !s.target)
    }
    fn environment(&self, name: &str) -> Option<String> {
        self.with(|s| {
            s.env
                .iter()
                .find(|(k, _)| k == name)
                .map(|(_, v)| v.clone())
        })
    }
    fn device_memory_bytes(&self) -> Option<u64> {
        self.with(|s| s.memory)
    }
    fn run_active(&self) -> bool {
        self.with(|s| s.run_active)
    }
    fn acquisition_active(&self) -> bool {
        self.with(|s| s.acquisition_active)
    }
    fn owner_run_active(&self) -> bool {
        self.with(|s| s.owner_run_active)
    }
    fn fast_mode_changed(&mut self) {
        self.with(|s| s.trace.push("host:fast_mode_changed".into()));
    }
    fn run_model_saved(&mut self) -> Result<(), PublicError> {
        self.with(|s| s.trace.push("host:run_model_saved".into()));
        Ok(())
    }
    fn cancel_acquisition(&mut self) {
        self.with(|s| s.trace.push("host:cancel_acquisition".into()));
    }
    fn membership_binding(&self) -> Option<api::member::Binding> {
        self.with(|s| {
            s.member.clone().map(|node_id| api::member::Binding {
                swarm_id: "swarm".into(),
                node_id,
                seed_key_digest: "digest".into(),
            })
        })
    }
}
struct Inner(Arc<Mutex<Vec<String>>>);
impl Backend for Inner {
    fn prepare(&mut self, action: Action, _: &Map<String, Value>) -> Result<(), PublicError> {
        self.0
            .lock()
            .unwrap()
            .push(format!("inner:prepare:{action:?}"));
        Ok(())
    }
    fn execute(&mut self, action: Action, body: &Map<String, Value>) -> Result<Value, PublicError> {
        self.0
            .lock()
            .unwrap()
            .push(format!("inner:execute:{action:?}"));
        // A body naming `refused` stands for an action its owner refuses (no effect).
        if body.contains_key("refused") {
            return Err(PublicError::new(409, "action_not_completed"));
        }
        Ok(json!({"state": "stopped", "route_ready": false}))
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        self.0.lock().unwrap().push("inner:publish".into());
        Ok(())
    }
}
fn root(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("l10-persist-{tag}-{}", std::process::id()));
    if dir.exists() {
        fs::remove_dir_all(&dir).unwrap();
    }
    fs::create_dir_all(&dir).unwrap();
    dir
}
fn body(v: Value) -> Map<String, Value> {
    v.as_object().unwrap().clone()
}
type Trace = Arc<Mutex<Vec<String>>>;
type App = Control<PersistentBackend<FakeHost, Inner>>;
fn build(tag: &str) -> (App, FakeHost, PathBuf, Trace) {
    let host = FakeHost::default();
    let dir = root(tag);
    let inner = Arc::new(Mutex::new(Vec::new()));
    let backend = PersistentBackend::new(dir.clone(), host.clone(), Inner(inner.clone()));
    (Control::new(backend), host, dir, inner)
}
fn oracle() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_run_model.json")).unwrap()
}
fn refused(r: Result<Value, PublicError>) -> (u16, &'static str) {
    let e = r.unwrap_err();
    (e.status, e.code)
}

#[test]
fn fast_mode_persists_the_canonical_private_file_and_refreshes_the_process_default() {
    let (c, host, dir, inner) = build("fast");
    assert_eq!(
        c.action("/api/fast-mode", &body(json!({"enabled": true})))
            .unwrap(),
        json!({"enabled": true, "source": "saved", "locked": false})
    );
    let file = dir.join("fast-mode.json");
    assert_eq!(
        fs::read(&file).unwrap(),
        br#"{"enabled":true,"protocol":"mycelium.a13_fast_mode.v1"}"#
    );
    assert_eq!(fs::metadata(&file).unwrap().mode() & 0o7777, 0o600);
    assert_eq!(
        c.action("/api/fast-mode", &body(json!({"enabled": false})))
            .unwrap(),
        json!({"enabled": false, "source": "saved", "locked": false})
    );
    assert_eq!(
        fs::read(&file).unwrap(),
        br#"{"enabled":false,"protocol":"mycelium.a13_fast_mode.v1"}"#
    );
    assert_eq!(
        host.with(|s| s.trace.clone()),
        ["host:fast_mode_changed", "host:fast_mode_changed"]
    );
    // The preference never reaches the delegate except for the status publication.
    assert_eq!(*inner.lock().unwrap(), ["inner:publish", "inner:publish"]);
}

#[test]
fn fast_mode_refusals_keep_the_oracle_order_status_and_write_nothing() {
    let (c, host, dir, _) = build("fast-refuse");
    let file = dir.join("fast-mode.json");
    let enable = body(json!({"enabled": true}));
    // application_closed is checked before the role, the role before the body shape.
    host.with(|s| {
        s.closed = true;
        s.target = true;
    });
    assert_eq!(
        refused(c.action("/api/fast-mode", &enable)),
        (400, "application_closed")
    );
    host.with(|s| s.closed = false);
    assert_eq!(
        refused(c.action("/api/fast-mode", &body(json!({"enabled": 1})))),
        (400, "owner_role_required")
    );
    host.with(|s| s.target = false);
    assert_eq!(
        refused(c.action("/api/fast-mode", &body(json!({"enabled": 1})))),
        (400, "invalid_request")
    );
    host.with(|s| s.run_active = true);
    assert_eq!(
        refused(c.action("/api/fast-mode", &enable)),
        (400, "fast_mode_run_active")
    );
    host.with(|s| {
        s.run_active = false;
        s.env = vec![("MYCELIUM_TOKEN_PATH".into(), "on".into())];
    });
    assert_eq!(
        refused(c.action("/api/fast-mode", &enable)),
        (400, "fast_mode_locked_by_environment")
    );
    host.with(|s| s.env = vec![("MYCELIUM_TOKEN_PATH".into(), "maybe".into())]);
    assert_eq!(
        refused(c.action("/api/fast-mode", &enable)),
        (409, "action_not_completed")
    );
    assert!(!file.exists());
    assert!(host.with(|s| s.trace.is_empty()));
}

#[test]
fn run_model_choice_persists_and_returns_the_oracle_document() {
    let (c, host, dir, inner) = build("run-model");
    host.with(|s| s.memory = Some(16 * GIB));
    let oracle = oracle();
    let case = oracle["choose_cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["label"] == "save_qwen3_8b")
        .unwrap();
    assert_eq!(case["device_memory_bytes"], json!(16 * GIB));
    assert_eq!(
        c.action("/api/run-model", &body(json!({"choice": "qwen3-8b"})))
            .unwrap(),
        case["ok"]
    );
    let file = dir.join("run-model.json");
    assert_eq!(
        fs::read_to_string(&file).unwrap(),
        case["file"].as_str().unwrap()
    );
    assert_eq!(fs::metadata(&file).unwrap().mode() & 0o7777, 0o600);
    assert_eq!(host.with(|s| s.trace.clone()), ["host:run_model_saved"]);
    assert_eq!(*inner.lock().unwrap(), ["inner:publish"]);
    // Choosing again over a saved choice is allowed while nothing runs, with no role check.
    host.with(|s| s.target = true);
    assert_eq!(
        c.action("/api/run-model", &body(json!({"choice": "a9-route"})))
            .unwrap()["selected"],
        "a9-route"
    );
}

#[test]
fn run_model_refusals_follow_choose_run_model_and_keep_the_saved_choice() {
    let (c, host, dir, inner) = build("run-model-refuse");
    c.action("/api/run-model", &body(json!({"choice": "qwen3-8b"})))
        .unwrap();
    inner.lock().unwrap().clear();
    host.with(|s| s.trace.clear());
    let ask = |choice: Value| c.action("/api/run-model", &body(json!({"choice": choice})));
    assert_eq!(
        refused(ask(json!("gpt"))),
        (400, "run_model_choice_invalid")
    );
    host.with(|s| s.acquisition_active = true);
    assert_eq!(
        refused(ask(json!("a9-route"))),
        (400, "acquisition_already_active")
    );
    host.with(|s| {
        s.acquisition_active = false;
        s.owner_run_active = true;
    });
    assert_eq!(refused(ask(json!("a9-route"))), (400, "owner_run_active"));
    host.with(|s| {
        s.owner_run_active = false;
        s.env = vec![("MYCELIUM_A13_RUN_MODEL".into(), "qwen3-14b".into())];
        s.acquisition_active = true;
    });
    assert_eq!(
        refused(ask(json!("a9-route"))),
        (400, "run_model_locked_by_environment")
    );
    host.with(|s| s.env = vec![("MYCELIUM_A13_RUN_MODEL".into(), "bogus".into())]);
    assert_eq!(
        refused(ask(json!("a9-route"))),
        (409, "action_not_completed")
    );
    host.with(|s| {
        s.closed = true;
        s.env.clear();
    });
    assert_eq!(refused(ask(json!("a9-route"))), (400, "application_closed"));
    assert_eq!(
        fs::read_to_string(dir.join("run-model.json")).unwrap(),
        r#"{"choice":"qwen3-8b","protocol":"mycelium.a13_run_model_choice.v1"}"#
    );
    assert!(host.with(|s| s.trace.is_empty()));
    assert!(inner.lock().unwrap().is_empty());
}

#[test]
fn a_failed_write_is_the_library_code_and_never_publishes() {
    let host = FakeHost::default();
    let inner = Arc::new(Mutex::new(Vec::new()));
    let missing = PathBuf::from("/nonexistent-l10-r7/state");
    let c = Control::new(PersistentBackend::new(
        missing,
        host.clone(),
        Inner(inner.clone()),
    ));
    assert_eq!(
        refused(c.action("/api/fast-mode", &body(json!({"enabled": true})))),
        (409, "authority_state_write_failed")
    );
    assert_eq!(
        refused(c.action("/api/run-model", &body(json!({"choice": "a9-route"})))),
        (409, "authority_state_write_failed")
    );
    assert!(inner.lock().unwrap().is_empty());
    assert!(host.with(|s| s.trace.is_empty()));
}

#[test]
fn other_actions_are_delegated_with_prepare_execute_publish() {
    let (c, host, _, inner) = build("delegate");
    // `/api/demo/*` belongs to the host's run (api::owner_driver, tests/demo_routes.rs): a host with no run
    // chain answers what the delegate always did, without touching the delegate.
    assert_eq!(
        refused(c.action("/api/demo/stop", &Map::new())),
        (409, "action_not_completed")
    );
    assert!(inner.lock().unwrap().is_empty());
    c.action("/api/serving/stop", &Map::new()).unwrap();
    assert_eq!(
        *inner.lock().unwrap(),
        [
            "inner:prepare:ServingStop",
            "inner:execute:ServingStop",
            "inner:publish"
        ]
    );
    assert!(host.with(|s| s.trace.is_empty()));
    assert_eq!(
        refused(c.action("/api/serving/stop", &body(json!({"x": 1})))),
        (400, "invalid_request")
    );
}

#[test]
fn preference_observations_persist_over_real_rust_http() {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let host_name = listener.local_addr().unwrap().to_string();
    let origin = format!("http://{host_name}");
    let status = Arc::new(PublishedStatus::new());
    status
        .publish(
            1,
            Snapshot::parse(
                serde_json::from_str(include_str!("../contracts/status-example.json")).unwrap(),
            )
            .unwrap(),
        )
        .unwrap();
    let session = Session::new(
        "launch".into(),
        "session".into(),
        Instant::now() + duration(BudgetId::WorkerStartup).unwrap(),
        duration(BudgetId::WorkerStartup).unwrap(),
    )
    .unwrap();
    let dir = root("http");
    let fake = FakeHost::default();
    let inner = Arc::new(Mutex::new(Vec::new()));
    let backend = PersistentBackend::new(dir.clone(), fake, Inner(inner));
    let app = Desktop::new(host_name, Arc::new(Control::new(backend)), status, session);
    let job = thread::spawn(move || {
        for _ in 0..4 {
            let (stream, _) = listener.accept().unwrap();
            serve_connection(
                stream,
                duration(BudgetId::ModelSelection).unwrap(),
                |r| app.preflight(r),
                |r| app.handle(r),
            )
            .unwrap();
        }
    });
    let mut c = Client::new(&origin).unwrap();
    c.sign_in("launch").unwrap();
    let replies = [
        (
            "choose_model_without_download",
            c.post("/api/run-model", &json!({"choice": "qwen3-0.6b"}))
                .unwrap(),
        ),
        (
            "fast_mode_saved",
            c.post("/api/fast-mode", &json!({"enabled": true})).unwrap(),
        ),
        (
            "normal_mode_saved",
            c.post("/api/fast-mode", &json!({"enabled": false}))
                .unwrap(),
        ),
    ];
    job.join().unwrap();
    let golden: Value = serde_json::from_str(include_str!(
        "../../../test-support/journeys/fixtures/python-v014.json"
    ))
    .unwrap();
    let mut expected = Vec::new();
    let mut actual = Vec::new();
    // Integrator ruling (a): the golden replay also compares the response BYTES with what the
    // released app sent (executed-oracle serialisation, L10-evidence/r8-probe-wire-routes.py).
    let wire: Value =
        serde_json::from_str(include_str!("fixtures/oracle_wire_routes.json")).unwrap();
    let want_bytes = [
        wire["routes"]["run_model_choose"]["golden_choose_0.6b_no_memory"].clone(),
        wire["routes"]["fast_mode"]["choose_true"].clone(),
        wire["routes"]["fast_mode"]["choose_false"].clone(),
    ];
    for ((id, reply), want) in replies.iter().zip(&want_bytes) {
        assert_eq!(
            reply.text().unwrap(),
            want.as_str().unwrap(),
            "bytes of {id}"
        );
    }
    for (id, reply) in replies {
        let old = golden["observations"]
            .as_array()
            .unwrap()
            .iter()
            .find(|v| v["id"] == id)
            .unwrap()
            .clone();
        let keys = old["fields"]
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>();
        let mut got = old.clone();
        got["status"] = json!(reply.status);
        got["fields"] = project(&reply.json().unwrap(), &keys).unwrap();
        expected.push(old);
        actual.push(got);
    }
    compare(&json!(expected), &json!(actual)).unwrap();
    assert_eq!(
        fs::read_to_string(dir.join("run-model.json")).unwrap(),
        r#"{"choice":"qwen3-0.6b","protocol":"mycelium.a13_run_model_choice.v1"}"#
    );
    assert_eq!(
        fs::read(dir.join("fast-mode.json")).unwrap(),
        br#"{"enabled":false,"protocol":"mycelium.a13_fast_mode.v1"}"#
    );
}

/// `/api/revoke-self` and `/api/revoke` of this Mac (app.py:1645-1660 @ f4e366e9): once the
/// revocation itself succeeded, `self._cancel_acquisition()` stops this Mac's download (a revoked
/// member keeps no grant to fetch with); revoking another member, or a revocation that was
/// refused, cancels nothing.
#[test]
fn revoking_this_mac_cancels_its_acquisition_after_the_revocation() {
    let (c, host, _dir, inner) = build("revoke");
    host.with(|s| s.member = Some("this-mac".into()));
    let cancels = |host: &FakeHost| {
        host.with(|s| {
            let seen = s
                .trace
                .iter()
                .filter(|t| *t == "host:cancel_acquisition")
                .count();
            s.trace.clear();
            seen
        })
    };
    c.action("/api/revoke-self", &body(json!({"confirm": true})))
        .unwrap();
    assert!(inner.lock().unwrap().contains(&"inner:execute:RevokeSelf".to_owned()));
    assert_eq!(cancels(&host), 1, "revoke-self did not cancel the download");
    c.action(
        "/api/revoke",
        &body(json!({"node_id": "another-mac", "generation": 1})),
    )
    .unwrap();
    assert_eq!(cancels(&host), 0, "revoking another member cancelled this Mac's download");
    c.action(
        "/api/revoke",
        &body(json!({"node_id": "this-mac", "generation": 1})),
    )
    .unwrap();
    assert_eq!(cancels(&host), 1, "revoking this Mac did not cancel its download");
    assert_eq!(
        refused(c.action(
            "/api/revoke-self",
            &body(json!({"confirm": true, "refused": true}))
        )),
        (409, "action_not_completed")
    );
    assert_eq!(cancels(&host), 0, "a refused revocation cancelled the download");
}
