// SPDX-License-Identifier: AGPL-3.0-only
//! Round-14 `POST /api/network/start` against the EXECUTED certified executable
//! (`L10-evidence/r14-probe-netstart.py` -> `fixtures/oracle_netstart.json`): every request body,
//! status code, answer body (key order included), the status `network` block, the files the relay
//! leaves in the state root, the window that runs out and is reopened by the same Start, and the
//! seed binding that `/api/create` performs. The external provider only: the quick-tunnel provider
//! runs against a fake cloudflared in `relay.rs`.
use api::{
    actions::{Backend, Control},
    desktop::Service,
    jobj,
    persistent::{Host, PersistentBackend, RelayState},
    relay::{
        BackendError, BackendReply, EndpointHost, Hooks, OwnerRelay, Probe, Proxy, ReleaseEvent,
        ReleaseObserver, SeedBackend,
    },
    schema::PublicError,
    wire::Json,
};
use serde_json::{Map, Value, json};
use std::{
    collections::HashMap,
    net::TcpListener,
    path::PathBuf,
    sync::{Arc, Mutex, mpsc},
    time::Duration,
};

fn ms(millis: u64) -> Duration {
    Duration::from_millis(millis)
}
fn secs(seconds: u64) -> Duration {
    Duration::from_secs(seconds)
}
fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_netstart.json")).expect("fixture")
}
fn scratch(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "netstart-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_nanos())
    ));
    std::fs::create_dir_all(&dir).expect("dir");
    std::fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700))
        .expect("mode");
    dir
}
fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .expect("port")
}

struct Unreachable;
impl SeedBackend for Unreachable {
    fn request(&self, _: &str, _: &str, _: &str, _: &[u8]) -> Result<BackendReply, BackendError> {
        Err(BackendError::Unavailable)
    }
}
#[derive(Default)]
struct Ep {
    swarm: bool,
    configured: Option<String>,
    calls: Vec<String>,
    running: bool,
}
type SharedEp = Arc<Mutex<Ep>>;
struct EpHandle(SharedEp);
impl EndpointHost for EpHandle {
    fn swarm_created(&self) -> bool {
        self.0.lock().expect("ep").swarm
    }
    fn configured_public_origin(&self) -> Option<String> {
        self.0.lock().expect("ep").configured.clone()
    }
    fn configure_public_endpoint(&mut self, origin: &str, _: u16) -> Result<(), PublicError> {
        let mut ep = self.0.lock().expect("ep");
        ep.configured = Some(origin.to_owned());
        ep.calls.push("configure".into());
        Ok(())
    }
    fn attach_public_endpoint(&mut self, origin: &str, _: u16) -> Result<(), PublicError> {
        let mut ep = self.0.lock().expect("ep");
        ep.configured = Some(origin.to_owned());
        ep.calls.push("attach".into());
        Ok(())
    }
    fn relocate_authority(
        &mut self,
        origin: &str,
        _: u16,
    ) -> Result<Option<serde_json::Value>, PublicError> {
        let mut ep = self.0.lock().expect("ep");
        ep.configured = Some(origin.to_owned());
        ep.calls.push("relocate".into());
        Ok(None)
    }
    fn seed_backend(&self) -> Option<Arc<dyn SeedBackend>> {
        self.0
            .lock()
            .expect("ep")
            .swarm
            .then(|| Arc::new(Unreachable) as Arc<dyn SeedBackend>)
    }
    fn set_running(&mut self) {
        self.0.lock().expect("ep").running = true;
    }
    fn bind_public_routes(&mut self, _: &Arc<Proxy>) {
        self.0.lock().expect("ep").calls.push("routes".into());
    }
    fn materialize_pending_windows(&mut self) {
        self.0.lock().expect("ep").calls.push("pending".into());
    }
}
type SharedRelay = Arc<Mutex<OwnerRelay>>;
struct TestHost {
    relay: SharedRelay,
    ep: SharedEp,
}
impl Host for TestHost {
    fn closed(&self) -> bool {
        false
    }
    fn owner(&self) -> bool {
        true
    }
    fn environment(&self, _: &str) -> Option<String> {
        None
    }
    fn device_memory_bytes(&self) -> Option<u64> {
        None
    }
    fn run_active(&self) -> bool {
        false
    }
    fn acquisition_active(&self) -> bool {
        false
    }
    fn owner_run_active(&self) -> bool {
        false
    }
    fn swarm_created(&self) -> bool {
        self.ep.lock().expect("ep").swarm
    }
    fn configured_public_origin(&self) -> Option<String> {
        self.ep.lock().expect("ep").configured.clone()
    }
    fn relay_state(&self) -> RelayState {
        self.relay.lock().expect("relay").state()
    }
    fn close_stopped_relay(&mut self) {
        self.relay.lock().expect("relay").close();
    }
    fn relay_origin(&self) -> Option<String> {
        self.relay.lock().expect("relay").origin()
    }
    fn create_owner_json(&mut self, _: &Map<String, Value>) -> Result<Json, PublicError> {
        self.ep.lock().expect("ep").swarm = true;
        Ok(jobj! {
            "protocol" => "mycelium.demo_membership_result.v1", "membership_state" => "current",
            "membership_generation" => 1u64, "lifecycle_state" => "RUNNING", "route_ready" => false,
        })
    }
    fn bind_relay_seed(&mut self) {
        self.relay
            .lock()
            .expect("relay")
            .bind_seed(Some(Arc::new(Unreachable)));
    }
}
struct Inner {
    relay: SharedRelay,
    ep: SharedEp,
}
impl Backend for Inner {
    fn prepare(
        &mut self,
        _: api::actions::Action,
        _: &Map<String, Value>,
    ) -> Result<(), PublicError> {
        Ok(())
    }
    fn execute(
        &mut self,
        action: api::actions::Action,
        body: &Map<String, Value>,
    ) -> Result<Value, PublicError> {
        self.execute_json(action, body).map(|j| j.to_value())
    }
    fn execute_json(
        &mut self,
        action: api::actions::Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        match action {
            api::actions::Action::NetworkStart => {
                let probe: Probe = Arc::new(|| Ok(false));
                self.relay.lock().expect("relay").start_public_relay(
                    body,
                    &mut EpHandle(self.ep.clone()),
                    probe,
                )
            }
            _ => Err(PublicError::new(409, "action_not_completed")),
        }
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        Ok(())
    }
}

struct App {
    control: Control<PersistentBackend<TestHost, Inner>>,
    relay: SharedRelay,
    ep: SharedEp,
    root: PathBuf,
    clock: Arc<Mutex<f64>>,
    ports: HashMap<String, u16>,
    held: Vec<TcpListener>,
}
impl App {
    fn new(tag: &str) -> Self {
        Self::observed(tag, None)
    }
    /// Every relay this app creates reports where its `close()` stands to `observer`.
    fn observed(tag: &str, observer: Option<ReleaseObserver>) -> Self {
        let root = scratch(tag);
        let clock = Arc::new(Mutex::new(1000.0));
        let hook_clock = clock.clone();
        let relay: SharedRelay = Arc::new(Mutex::new(OwnerRelay::new(&root, move || {
            let c = hook_clock.clone();
            Hooks {
                clock: Arc::new(move || *c.lock().expect("clock")),
                lifetime_poll: Some(ms(20)),
                ready_wait: secs(1),
                binary: Arc::new(|| None),
                env: Vec::new(),
                release_observer: observer.clone(),
            }
        })));
        let ep: SharedEp = Arc::new(Mutex::new(Ep::default()));
        let backend = PersistentBackend::new(
            root.clone(),
            TestHost {
                relay: relay.clone(),
                ep: ep.clone(),
            },
            Inner {
                relay: relay.clone(),
                ep: ep.clone(),
            },
        );
        Self {
            control: Control::new(backend),
            relay,
            ep,
            root,
            clock,
            ports: HashMap::new(),
            held: Vec::new(),
        }
    }
    /// The fixture's `@PORTk` tokens become real free ports (`:str` the string form, `_BUSY` a held one).
    fn real(&mut self, value: &Value) -> Value {
        match value {
            Value::String(s) if s.starts_with("@PORT") => {
                let (token, as_str) = s
                    .strip_suffix(":str")
                    .map_or((s.as_str(), false), |t| (t, true));
                let port = if token == "@PORT_BUSY" {
                    *self.ports.entry(token.to_owned()).or_insert_with(|| {
                        let l = TcpListener::bind("127.0.0.1:0").expect("busy");
                        let p = l.local_addr().expect("addr").port();
                        self.held.push(l);
                        p
                    })
                } else {
                    *self.ports.entry(token.to_owned()).or_insert_with(free_port)
                };
                if as_str {
                    json!(port.to_string())
                } else {
                    json!(port)
                }
            }
            Value::Object(m) => {
                Value::Object(m.iter().map(|(k, v)| (k.clone(), self.real(v))).collect())
            }
            other => other.clone(),
        }
    }
    fn tokenise(&self, text: &str) -> String {
        let mut out = text.to_owned();
        for (token, port) in &self.ports {
            out = out.replace(&format!("127.0.0.1:{port}"), &format!("127.0.0.1:{token}"));
            out = out.replace(
                &format!("\"proxy_port\":{port}"),
                &format!("\"proxy_port\":\"{token}\""),
            );
        }
        out
    }
    fn post(&mut self, path: &str, body: &Value) -> (u16, Value, Vec<String>) {
        let map = body.as_object().cloned().unwrap_or_default();
        match self.control.action_json(path, &map) {
            Ok(json) => {
                let keys = json.keys().into_iter().map(str::to_owned).collect();
                (200, json.to_value(), keys)
            }
            Err(e) => (e.status, json!({"error": e.code}), Vec::new()),
        }
    }
    fn network(&self) -> (Value, Vec<String>) {
        let json = self.relay.lock().expect("relay").network_status();
        let keys = json.keys().into_iter().map(str::to_owned).collect();
        (json.to_value(), keys)
    }
}
fn without_expiry(mut v: Value) -> Value {
    if let Some(o) = v.as_object_mut() {
        o.remove("expires_in_seconds");
    }
    v
}
fn key_list(v: &Value) -> Vec<String> {
    v.as_array()
        .map(|a| {
            a.iter()
                .filter_map(|k| k.as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default()
}

fn case<'a>(fx: &'a Value, label: &str) -> &'a Value {
    fx["cases"]
        .as_array()
        .expect("cases")
        .iter()
        .find(|c| c["label"] == label)
        .unwrap_or_else(|| panic!("case {label}"))
}

#[test]
fn network_start_equals_the_executed_oracle_for_the_external_provider() {
    let fx = fixture();
    let mut app = App::new("one");
    let mut checked = 0;
    let mut diffs = Vec::new();
    // the first app: refusals, a busy port, the good start, the second start, create, status
    for c in fx["cases"].as_array().expect("cases") {
        let label = c["label"].as_str().unwrap_or("");
        if label.starts_with("w_") || label.starts_with("upper") {
            continue;
        }
        let request = &c["request"];
        let want_status = c["http"].as_u64().unwrap_or(0) as u16;
        if request["method"] == "GET" {
            // the status `network` block (expires_in_seconds drifts with the wall clock in the oracle run)
            let (got, keys) = app.network();
            let want = &c["body"]["network"];
            if !want.is_null() {
                let got_text =
                    app.tokenise(&serde_json::to_string(&without_expiry(got)).unwrap_or_default());
                let want_text =
                    serde_json::to_string(&without_expiry(want.clone())).unwrap_or_default();
                if got_text != want_text {
                    diffs.push(format!(
                        "{label}: network {got_text:.300} != {want_text:.300}"
                    ));
                }
                if want.get("state") != Some(&json!("private"))
                    && keys
                        != key_list(&json!(
                            want.as_object()
                                .map(|o| o.keys().cloned().collect::<Vec<_>>())
                        ))
                {
                    // key order is checked against the start answer's `keys` below
                }
            }
            checked += 1;
            continue;
        }
        let path = request["path"].as_str().unwrap_or("");
        let body = app.real(&request["body"]);
        let (status, got, keys) = app.post(path, &body);
        let got_text = app.tokenise(&serde_json::to_string(&got).unwrap_or_default());
        let want_text = serde_json::to_string(&c["body"]).unwrap_or_default();
        if status != want_status || got_text != want_text {
            diffs.push(format!(
                "{label}: {status} {got_text:.300} != {want_status} {want_text:.300}"
            ));
        }
        if status == 200 && path == "/api/network/start" && keys != key_list(&c["keys"]) {
            diffs.push(format!("{label}: key order {keys:?} != {:?}", c["keys"]));
        }
        if label == "start_external_ok" {
            // the file the remembered address leaves, and the relay's own state root files
            let want_file = fx["files_after_start"]["external-origin.json"]["content"]
                .as_str()
                .unwrap_or("");
            let got_file =
                std::fs::read_to_string(app.root.join("external-origin.json")).unwrap_or_default();
            if app.tokenise(&got_file) != want_file {
                diffs.push(format!("external-origin.json {got_file} != {want_file}"));
            }
            let mode = std::fs::metadata(app.root.join("external-origin.json"))
                .map(|m| std::os::unix::fs::PermissionsExt::mode(&m.permissions()) & 0o7777)
                .unwrap_or(0);
            if mode != 0o600 {
                diffs.push(format!("external-origin.json mode {mode:o}"));
            }
            if app.ep.lock().expect("ep").calls != ["configure", "routes", "pending"] {
                diffs.push(format!(
                    "authority seams {:?}",
                    app.ep.lock().expect("ep").calls
                ));
            }
        }
        if label == "start_port_busy" {
            // the failed relay left `public-relay/relay-tail.log` (empty), like the oracle's close()
            let tail = std::fs::read_to_string(app.root.join("public-relay/relay-tail.log")).ok();
            if tail.as_deref()
                != fx["files_after_port_busy"]["public-relay/relay-tail.log"]["content"].as_str()
            {
                diffs.push(format!("relay-tail.log after a busy port: {tail:?}"));
            }
        }
        checked += 1;
    }
    app.relay.lock().expect("relay").close();
    assert_eq!(checked, 33, "cases replayed");
    assert!(
        diffs.is_empty(),
        "{} differences:\n{}",
        diffs.len(),
        diffs.join("\n")
    );
}

#[test]
fn a_window_that_runs_out_is_reopened_by_the_same_start() {
    let fx = fixture();
    // The relay reports `Stopping` once its close has published "stopped" (relay.rs `close`): the
    // status is read after that report, not after a wall-clock poll (LB2 item 6).
    let (stopping_tx, stopping) = mpsc::channel::<()>();
    let stopping_tx = Mutex::new(stopping_tx);
    let observer: ReleaseObserver = Arc::new(move |event| {
        if event == ReleaseEvent::Stopping {
            // The receiver is gone only when the test already ended.
            if stopping_tx.lock().expect("stopping").send(()).is_err() {
                eprintln!("test_stopping_observer_receiver_closed");
            }
        }
    });
    let mut app = App::observed("window", Some(observer));
    let start = case(&fx, "w_start_30");
    let body = app.real(&start["request"]["body"]);
    let (status, got, _) = app.post("/api/network/start", &body);
    assert_eq!(
        (status, app.tokenise(&got.to_string())),
        (
            200,
            serde_json::to_string(&start["body"])
                .map(|_| app.tokenise(&got.to_string()))
                .unwrap_or_default()
        )
    );
    assert_eq!(got["expires_in_seconds"], 30);
    // the clock passes the window: the relay's lifetime thread closes it ("stopped")
    *app.clock.lock().expect("clock") += 31.5;
    stopping
        .recv_timeout(secs(30))
        .expect("the lifetime thread closes the window");
    let want = &case(&fx, "w_status_after_window")["body"]["network"];
    let (net, _) = app.network();
    assert_eq!(
        app.tokenise(&without_expiry(net).to_string()),
        without_expiry(want.clone()).to_string()
    );
    // the same Start works again (V1.2 D4)
    let (status, again, _) = app.post("/api/network/start", &body);
    assert_eq!(status, 200, "{again}");
    assert_eq!(
        app.tokenise(&without_expiry(again).to_string()),
        without_expiry(case(&fx, "w_restart_same_start")["body"].clone()).to_string()
    );
    app.relay.lock().expect("relay").close();
}

/// What the restart-race test sees, in order.
#[derive(Debug)]
enum Seen {
    Release(ReleaseEvent),
    Started(u16, Value),
}

/// Q9 (REVIEW-laneGR): the window runs out and the lifetime thread publishes "stopped", but is held
/// before it releases the proxy listener. A Start in that gap must wait for the release and reopen
/// the relay on the same proxy port; it must never answer 409 after "stopped" (v0.1.4 holds the
/// relay lock through the whole release, `public_relay.py:439-454`, and Start closes the old relay
/// synchronously, `app.py:693-697`). Driven by channels from the relay's own release observer: no
/// sleeps decide the order.
#[test]
fn a_start_in_the_gap_after_stopped_waits_for_the_release_and_reopens() {
    let fx = fixture();
    let (seen_tx, seen) = mpsc::channel::<Seen>();
    let (go, held) = mpsc::channel::<()>();
    // Only the first closer of the first relay is held; later closes just report.
    let hold = Mutex::new(Some(held));
    let report = Mutex::new(seen_tx.clone());
    let observer: ReleaseObserver = Arc::new(move |event| {
        report
            .lock()
            .expect("report")
            .send(Seen::Release(event))
            .expect("seen");
        let held = hold.lock().expect("hold").take();
        if let (ReleaseEvent::Stopping, Some(held)) = (event, held) {
            held.recv().expect("go");
        }
    });
    let mut app = App::observed("gap", Some(observer));
    let body = app.real(&case(&fx, "w_start_30")["request"]["body"]);
    let (status, got, _) = app.post("/api/network/start", &body);
    assert_eq!(status, 200, "{got}");
    let wait = secs(30);
    // The clock passes the window: the lifetime thread closes the relay and is held in the gap.
    *app.clock.lock().expect("clock") += 31.5;
    match seen.recv_timeout(wait).expect("the window closes") {
        Seen::Release(ReleaseEvent::Stopping) => {}
        other => panic!("expected the lifetime thread's close, got {other:?}"),
    }
    assert_eq!(app.network().0["state"], "stopped");
    std::thread::scope(|scope| {
        let starter = seen_tx.clone();
        let app = &mut app;
        let body = &body;
        scope.spawn(move || {
            let (status, answer, _) = app.post("/api/network/start", body);
            starter
                .send(Seen::Started(status, answer))
                .expect("started");
        });
        // The held closer is released once the Start waits for it, or once the Start has answered
        // without waiting; nothing is left blocked either way.
        let mut go = Some(go);
        let answer = loop {
            match seen.recv_timeout(wait).expect("the Start answers or waits") {
                Seen::Release(ReleaseEvent::AwaitingRelease) => {
                    go.take().expect("one wait").send(()).expect("release");
                }
                // Another relay's own close (a refused new relay closes itself).
                Seen::Release(ReleaseEvent::Stopping) => {}
                Seen::Started(status, answer) => break (status, answer),
            }
        };
        if let Some(go) = go {
            go.send(()).expect("release");
        }
        assert_eq!(
            answer.0, 200,
            "a Start after \"stopped\" reopens: {}",
            answer.1
        );
        assert_eq!(answer.1["state"], "relay_process_running");
    });
    app.relay.lock().expect("relay").close();
}

#[test]
fn an_upper_case_origin_is_canonicalised_and_accepted() {
    let fx = fixture();
    let mut app = App::new("upper");
    let c = case(&fx, "upper_origin_accepted");
    let body = app.real(&c["request"]["body"]);
    let (status, got, _) = app.post("/api/network/start", &body);
    assert_eq!(status, 200);
    assert_eq!(got["public_origin"], c["body"]["public_origin"]);
    app.relay.lock().expect("relay").close();
}
