// SPDX-License-Identifier: AGPL-3.0-only
//! The installed headless host serves the product evidence routes of v0.1.4's product gateway
//! (`GET /api/v1/product/{snapshot,events,export}`, `mycelium_ui_gateway/app.py:348-352, 591-670` over
//! `mycelium_product_spine/app.py` @ f4e366e9) for an owner whose seed coordinator runs: one
//! snapshot per GET, ordered resumable events, the stale-cursor refusal, a bounded replay window kept
//! across a restart, and no private identifiers. The membership source is the owner's own seed
//! (`current_members()`); this host has no live route, qualification, assignment or recovery source
//! yet (gap rows 3/6), so those stay `missing`/`unsupported` and `route_ready` stays false.
use budgets::{BudgetId, Entries};
use journey_tests::http::{Client, Response};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs,
    io::{BufRead, BufReader},
    net::{Ipv4Addr, TcpListener},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

const ORIGIN: &str = "https://v02-product-e2e.example.org";
const SNAPSHOT: &str = "/api/v1/product/snapshot";
const EVENTS: &str = "/api/v1/product/events";
const EXPORT: &str = "/api/v1/product/export";
/// `ProductEvidenceApplication(replay_limit=128)` (`app.py:57`).
const REPLAY_LIMIT: usize = 128;

fn profile_dir() -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    exe.parent().unwrap().parent().unwrap().to_path_buf()
}
/// Under nextest the `headless-release` setup script builds the release launcher once and names it
/// here; plain `cargo test` builds this profile's launcher once per process.
const PREBUILT: &str = "MYCELIUM_HEADLESS_RELEASE";
fn launcher() -> PathBuf {
    static BUILT: std::sync::OnceLock<PathBuf> = std::sync::OnceLock::new();
    BUILT
        .get_or_init(|| match std::env::var_os(PREBUILT) {
            Some(path) => prebuilt(Path::new(&path)),
            None => nested_build(),
        })
        .clone()
}
fn prebuilt(path: &Path) -> PathBuf {
    let expected = profile_dir()
        .with_file_name("release")
        .join("examples/headless");
    match (fs::canonicalize(path), fs::canonicalize(&expected)) {
        (Ok(found), Ok(expected)) if found == expected => found,
        other => panic!("{PREBUILT} is not this target's release launcher: {other:?}"),
    }
}
fn nested_build() -> PathBuf {
    let profile = profile_dir();
    let target = profile.parent().unwrap();
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../Cargo.toml");
    let mut command = Command::new(std::env::var("CARGO").unwrap_or_else(|_| "cargo".into()));
    command.args(["build", "--offline", "--locked", "-j", "2", "-p", "api"]);
    command.args(["--example", "headless"]);
    command.arg("--manifest-path").arg(manifest);
    command.arg("--target-dir").arg(target);
    if profile.file_name().is_some_and(|name| name == "release") {
        command.arg("--release");
    }
    let output = command.output().expect("cargo is runnable");
    assert!(
        output.status.success(),
        "nested build failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
    profile.join("examples/headless")
}
fn span(id: BudgetId) -> Duration {
    Duration::from_millis(id.span(Entries::new(0)).unwrap().millis())
}
fn patience() -> Duration {
    span(BudgetId::ModelSelection) * 4
}
fn free_port() -> u16 {
    TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}
fn consent() -> Value {
    json!({
        "peer_visibility": true, "power": "ac_only", "storage_bytes": 4_000_000_000u64,
        "network": "unmetered_only", "background": false, "diagnostics": false,
        "thermal": "stop_on_serious",
    })
}

/// One headless process; dropped = killed and reaped by exact pid if it did not exit.
struct Headless {
    child: Child,
    client: Client,
}
impl Headless {
    fn launch(root: &Path) -> Self {
        let mut child = Command::new(launcher())
            .args(["--headless", "--offline", "--state-root"])
            .arg(root)
            .env("HOME", root)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("the headless example was built");
        let stdout = child.stdout.take().unwrap();
        let (send, receive) = mpsc::channel();
        let reader = thread::spawn(move || {
            let mut line = String::new();
            BufReader::new(stdout).read_line(&mut line).unwrap();
            send.send(line).unwrap();
        });
        let line = receive.recv_timeout(patience()).expect("first line");
        reader.join().unwrap();
        assert!(line.contains("launcher_started"), "{line:?}");
        let form = fs::read_to_string(root.join("open-demo.html")).expect("launch form");
        let attribute = |name: &str| {
            let key = format!("{name}=\"");
            form.split_once(&key)
                .and_then(|(_, rest)| rest.split_once('"'))
                .map(|(value, _)| value.to_owned())
                .expect("form attribute")
        };
        let origin = attribute("action")
            .strip_suffix("/session")
            .unwrap()
            .to_owned();
        let mut client = Client::new(&origin).unwrap();
        client.sign_in(&attribute("value")).unwrap();
        Self { child, client }
    }
    fn get(&self, path: &str, last_event_id: Option<u64>) -> Response {
        let id = last_event_id.map(|v| v.to_string());
        let headers: Vec<(&str, &str)> = id
            .as_deref()
            .map(|v| vec![("Last-Event-ID", v)])
            .unwrap_or_default();
        self.client
            .request("GET", path, &[], &headers, true, BudgetId::ModelSelection)
            .unwrap()
    }
    fn anonymous(&self, path: &str) -> Response {
        self.client
            .request("GET", path, &[], &[], false, BudgetId::ModelSelection)
            .unwrap()
    }
    fn post(&self, path: &str, body: Value) -> Value {
        let reply = self.client.post(path, &body).unwrap();
        let value = reply.json().unwrap();
        assert_eq!(reply.status, 200, "{path}: {value}");
        value
    }
    fn quit(mut self) {
        self.post("/api/quit", json!({}));
        let deadline = Instant::now() + patience();
        while self.child.try_wait().unwrap().is_none() {
            assert!(
                Instant::now() < deadline,
                "headless did not exit after quit"
            );
            thread::sleep(span(BudgetId::OwnerPoll) / 10);
        }
    }
}
impl Drop for Headless {
    fn drop(&mut self) {
        if self.child.try_wait().unwrap().is_none() {
            self.child.kill().unwrap();
            self.child.wait().unwrap();
        }
    }
}

fn refusal(reply: &Response) -> (u16, Value) {
    (reply.status, reply.json().unwrap())
}
fn unavailable(code: &str) -> (u16, Value) {
    (
        503,
        json!({"protocol": "mycelium.product_ui.error.v1", "code": code, "retryable": true}),
    )
}
/// The frames of a finite product event stream, each a valid `mycelium.product_event.v1`.
fn frames(reply: &Response) -> Vec<Value> {
    assert_eq!(reply.status, 200, "{:?}", reply.text());
    let text = reply.text().unwrap();
    text.split_terminator("\n\n")
        .map(|frame| {
            let lines: Vec<&str> = frame.split('\n').collect();
            assert_eq!(lines.len(), 3, "{frame}");
            assert_eq!(lines[1], "event: product_snapshot");
            let event: Value =
                serde_json::from_str(lines[2].strip_prefix("data: ").unwrap()).unwrap();
            protocol::product::event(&event).expect("a valid product event");
            assert_eq!(lines[0], format!("id: {}", event["cursor"]));
            event
        })
        .collect()
}
fn cursors(events: &[Value]) -> Vec<u64> {
    events
        .iter()
        .map(|e| e["cursor"].as_u64().unwrap())
        .collect()
}
fn devices(snapshot: &Value) -> Vec<Value> {
    snapshot["entities"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| e["kind"] == "device")
        .cloned()
        .collect()
}
fn source<'a>(snapshot: &'a Value, id: &str) -> &'a Value {
    snapshot["source_states"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["source_id"] == id)
        .unwrap()
}
/// Nothing that names a Mac or its addresses leaves the product routes.
fn private_free(body: &[u8], raw_ids: &BTreeSet<String>) {
    let text = String::from_utf8(body.to_vec()).unwrap();
    for raw in raw_ids {
        assert!(!text.contains(raw.as_str()), "raw node id {raw} leaked");
    }
    for private in [
        "endpoint_addrs",
        "endpoint_id",
        "verification_key",
        "127.0.0.1",
    ] {
        assert!(!text.contains(private), "{private} leaked");
    }
}
fn raw_members(app: &Headless) -> BTreeSet<String> {
    let status = app.client.get("/api/status").unwrap().json().unwrap();
    status["authority"]["members"]
        .as_array()
        .map(|members| {
            members
                .iter()
                .filter_map(|m| m["node_id"].as_str().map(str::to_owned))
                .collect()
        })
        .unwrap_or_default()
}

#[test]
fn the_owner_product_snapshot_and_events_follow_the_swarm_through_a_restart() {
    let scratch = std::env::temp_dir().join(format!("b13-headless-product-{}", std::process::id()));
    fs::remove_dir_all(&scratch).unwrap_or(());
    fs::create_dir_all(&scratch).unwrap();
    let base = fs::canonicalize(&scratch).unwrap();
    let root = base.join("owner");
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let app = Headless::launch(&root);

    // No swarm yet: the routes exist, need the session, and have no product application.
    assert_eq!(app.anonymous(SNAPSHOT).status, 401);
    assert_eq!(app.anonymous(EVENTS).status, 401);
    assert_eq!(
        refusal(&app.get(SNAPSHOT, None)),
        unavailable("product_snapshot_unavailable")
    );
    assert_eq!(
        refusal(&app.get(EVENTS, None)),
        unavailable("product_events_unavailable")
    );
    assert_eq!(
        refusal(&app.get(EXPORT, None)),
        unavailable("product_export_unavailable")
    );

    // The owner creates its swarm: its own seed member is the one current device.
    app.post(
        "/api/network/start",
        json!({"relay_acknowledged": true, "max_seconds": 1800, "provider": "external",
               "public_origin": ORIGIN, "proxy_port": free_port()}),
    );
    app.post("/api/create", json!({"consent": consent()}));
    let mut seen = raw_members(&app);
    assert_eq!(seen.len(), 1, "{seen:?}");
    let first = app.get(SNAPSHOT, None);
    assert_eq!(first.status, 200);
    private_free(&first.body, &seen);
    let first = first.json().unwrap();
    protocol::product::snapshot(&first).unwrap();
    assert_eq!(first["publication"]["cursor"], 1);
    assert_eq!(first["publication"]["source_mode"], "degraded");
    let owner = devices(&first);
    assert_eq!(owner.len(), 1, "{first}");
    assert_eq!(owner[0]["attributes"]["lifecycle"], "running");
    assert_eq!(owner[0]["attributes"]["lease_freshness"], "fresh");
    assert_eq!(source(&first, "membership-source")["status"], "current");
    assert_eq!(
        source(&first, "route-source")["reason_code"],
        "route_source_missing"
    );
    assert!(
        !first["entities"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e["kind"] == "route" || e["attributes"]["route_ready"] == true),
        "no route here"
    );
    let all = frames(&app.get(EVENTS, None));
    assert_eq!(cursors(&all), [1, 2]);
    assert_eq!(all[0]["snapshot"], first);
    let mut last = 2;

    // The owner's local contributor joins the run (the run's contributor step): a second device.
    app.post("/api/local-contributor/start", json!({}));
    seen.extend(raw_members(&app));
    assert_eq!(seen.len(), 2, "{seen:?}");
    let joined = app.get(EVENTS, Some(last));
    private_free(&joined.body, &seen);
    let joined = frames(&joined);
    assert_eq!(cursors(&joined), [last + 1]);
    last += 1;
    let two = devices(&joined[0]["snapshot"]);
    assert_eq!(two.len(), 2, "{}", joined[0]);
    assert!(
        two.iter()
            .all(|d| d["attributes"]["lease_freshness"] == "fresh")
    );

    // The contributor stops: its member is fenced out, which the next snapshot shows.
    app.post("/api/local-contributor/stop", json!({}));
    let stopped = frames(&app.get(EVENTS, Some(last)));
    assert_eq!(cursors(&stopped), [last + 1]);
    last += 1;
    // The seed keeps the stopped contributor's row (lifecycle NEW, lease still running): it is no
    // longer running nor activation-eligible; the owner's own device is unchanged.
    let after_stop = devices(&stopped[0]["snapshot"]);
    let lifecycles: BTreeSet<String> = after_stop
        .iter()
        .map(|d| d["attributes"]["lifecycle"].as_str().unwrap().to_owned())
        .collect();
    eprintln!("b13-observed contributor-stop lifecycles {lifecycles:?}");
    assert_eq!(after_stop.len(), 2, "{after_stop:?}");
    let running: Vec<&Value> = after_stop
        .iter()
        .filter(|d| d["attributes"]["lifecycle"] == "running")
        .collect();
    assert_eq!(
        running.len(),
        1,
        "only the owner still runs: {after_stop:?}"
    );
    assert_eq!(running[0]["entity_id"], owner[0]["entity_id"]);
    let contributor = after_stop
        .iter()
        .find(|d| d["entity_id"] != owner[0]["entity_id"])
        .unwrap();
    assert_eq!(contributor["attributes"]["activation_eligible"], false);

    // The owner stops its authority: no seed, no product application; it comes back on start
    // and the sequence continues where it was.
    app.post("/api/stop", json!({}));
    assert_eq!(
        refusal(&app.get(SNAPSHOT, None)),
        unavailable("product_snapshot_unavailable")
    );
    assert_eq!(
        refusal(&app.get(EVENTS, Some(last))),
        unavailable("product_events_unavailable")
    );
    app.post("/api/start", json!({}));
    let back = frames(&app.get(EVENTS, Some(last)));
    assert_eq!(cursors(&back), [last + 1]);
    last += 1;

    // A restart keeps the window: a client resumes with the cursor it had.
    app.quit();
    let state = root.join("serving/product/product-evidence-state.v1.json");
    assert_eq!(
        fs::metadata(&state).unwrap().permissions().mode() & 0o7777,
        0o600
    );
    let app = Headless::launch(&root);
    let resumed = app.get(EVENTS, Some(last - 1));
    private_free(&resumed.body, &seen);
    let resumed = frames(&resumed);
    assert_eq!(cursors(&resumed), [last, last + 1]);
    last += 1;
    let owner_after = devices(&resumed[1]["snapshot"]);
    eprintln!(
        "b13-observed restart cursors {:?} devices {}",
        cursors(&resumed),
        serde_json::to_string(&owner_after).unwrap()
    );
    // The owner's own device (same pseudonym across the restart) renewed its membership at each
    // authority start: create 1, /api/start 2, relaunch 3.
    let owner_now = owner_after
        .iter()
        .find(|d| d["entity_id"] == owner[0]["entity_id"])
        .expect("the owner keeps its pseudonym across the restart");
    assert_eq!(owner_now["attributes"]["membership_generation"], 3);
    assert_eq!(owner_now["attributes"]["lifecycle"], "running");

    // The window is bounded: after more publications than it holds, an old cursor is stale and
    // the stored window holds exactly the replay limit.
    for _ in 0..REPLAY_LIMIT {
        assert_eq!(app.get(SNAPSHOT, None).status, 200);
    }
    let stale = app.get(EVENTS, Some(last));
    assert_eq!(
        refusal(&stale),
        (
            409,
            json!({"protocol": "mycelium.product_ui.error.v1", "code": "product_cursor_stale",
                   "retryable": false})
        )
    );
    let stored: Value = serde_json::from_slice(&fs::read(&state).unwrap()).unwrap();
    assert_eq!(stored["events"].as_array().unwrap().len(), REPLAY_LIMIT);
    let tail = frames(&app.get(EVENTS, Some(last + REPLAY_LIMIT as u64)));
    assert_eq!(tail.len(), 2, "the remaining tail after a recent cursor");
    let malformed = app
        .client
        .request(
            "GET",
            EVENTS,
            &[],
            &[("Last-Event-ID", "x1")],
            true,
            BudgetId::ModelSelection,
        )
        .unwrap();
    assert_eq!(refusal(&malformed).1["code"], "invalid_last_event_id");
    app.quit();
    fs::remove_dir_all(&base).unwrap_or(());
}

const STATE: &str = "serving/product/product-evidence-state.v1.json";

/// A private owner state root under a per-test scratch directory, removed when the test ends.
struct OwnerRoot {
    base: PathBuf,
    root: PathBuf,
}
impl OwnerRoot {
    fn new(label: &str) -> Self {
        let scratch = std::env::temp_dir().join(format!(
            "b13f-headless-product-{label}-{}",
            std::process::id()
        ));
        fs::remove_dir_all(&scratch).unwrap_or(());
        fs::create_dir_all(&scratch).unwrap();
        let base = fs::canonicalize(&scratch).unwrap();
        let root = base.join("owner");
        fs::create_dir(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        Self { base, root }
    }
    fn swarm_id(&self) -> String {
        let record: Value =
            serde_json::from_slice(&fs::read(self.root.join("owner-swarm.json")).unwrap()).unwrap();
        record["swarm_id"].as_str().unwrap().to_owned()
    }
    /// The files next to the window other than the window itself (what was set aside).
    fn set_aside(&self) -> Vec<PathBuf> {
        let dir = self.root.join("serving/product");
        let mut found: Vec<PathBuf> = fs::read_dir(&dir)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|path| path.file_name().unwrap() != "product-evidence-state.v1.json")
            .collect();
        found.sort();
        found
    }
}
impl Drop for OwnerRoot {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.base).unwrap_or(());
    }
}
fn create_swarm(app: &Headless) {
    app.post(
        "/api/network/start",
        json!({"relay_acknowledged": true, "max_seconds": 1800, "provider": "external",
               "public_origin": ORIGIN, "proxy_port": free_port()}),
    );
    app.post("/api/create", json!({"consent": consent()}));
}
fn mode(path: &Path) -> u32 {
    fs::symlink_metadata(path).unwrap().permissions().mode() & 0o7777
}

/// B13f F2: the persisted window belongs to the swarm it was published for. v0.1.4 kept it per serving
/// run (`serving-runs/<run_id>/registry/product`, `workflow.py:489,506`), so a window never outlived its
/// swarm. The installed host has no action that dissolves a swarm, so a second swarm in the same state
/// root exists only after the swarm record is gone (a manual reset, or a later owner-leave): that new
/// swarm must start its own window, and the old one is set aside unchanged, never restored or deleted.
#[test]
fn a_new_swarm_in_the_same_state_root_starts_its_own_window() {
    let owner = OwnerRoot::new("new-swarm");
    let app = Headless::launch(&owner.root);
    create_swarm(&app);
    let swarm_a = owner.swarm_id();
    for _ in 0..3 {
        assert_eq!(app.get(SNAPSHOT, None).status, 200);
    }
    let a = frames(&app.get(EVENTS, None));
    assert_eq!(cursors(&a), [1, 2, 3, 4]);
    app.quit();
    let window_a = fs::read(owner.root.join(STATE)).unwrap();

    // The swarm's own state goes away: its record, its seed store (which refuses another swarm id,
    // `seed_state_identity_mismatch`) and the owner's membership of it (`pairing_handoff_changed`
    // otherwise). The same root then creates another swarm; the rest of the root (node identity,
    // network settings, serving/) stays, as a reset of the swarm leaves it.
    fs::create_dir(owner.base.join("swarm-a")).unwrap();
    for name in ["owner-swarm.json", "seed-rs", "authority/member"] {
        let aside = owner.base.join("swarm-a").join(name.replace('/', "-"));
        fs::rename(owner.root.join(name), aside).unwrap();
    }
    let app = Headless::launch(&owner.root);
    create_swarm(&app);
    let swarm_b = owner.swarm_id();
    assert_ne!(swarm_a, swarm_b);
    let b = frames(&app.get(EVENTS, None));
    assert_eq!(
        cursors(&b),
        [1],
        "swarm B's window starts empty: {:?}",
        cursors(&b)
    );
    let a_ids: BTreeSet<&str> = a
        .iter()
        .map(|e| {
            e["snapshot"]["publication"]["snapshot_id"]
                .as_str()
                .unwrap()
        })
        .collect();
    assert!(
        !a_ids.contains(
            b[0]["snapshot"]["publication"]["snapshot_id"]
                .as_str()
                .unwrap()
        )
    );
    // Swarm A's window was set aside as it was: same bytes, still owner-only.
    let aside = owner.set_aside();
    assert_eq!(aside.len(), 1, "{aside:?}");
    assert_eq!(fs::read(&aside[0]).unwrap(), window_a);
    assert_eq!(mode(&aside[0]), 0o600);
    // B's own window survives B's restart as before.
    app.quit();
    let app = Headless::launch(&owner.root);
    assert_eq!(cursors(&frames(&app.get(EVENTS, Some(1)))), [2]);
    assert_eq!(owner.set_aside().len(), 1);
    app.quit();
}

/// B13f F3: a stored window that cannot be read is refused (v0.1.4 failed the serving start on it,
/// `state.py:315-350`) with its typed reason in the 503 body, and stays refused, never rewritten, until
/// the owner resets it: the reset moves the file aside (0600, same bytes) and a fresh window starts.
#[test]
fn a_corrupt_window_is_refused_with_its_reason_until_the_owner_resets_it() {
    let owner = OwnerRoot::new("corrupt");
    let app = Headless::launch(&owner.root);
    create_swarm(&app);
    assert_eq!(app.get(SNAPSHOT, None).status, 200);
    app.quit();
    let state = owner.root.join(STATE);
    let corrupt = b"{\"protocol\": \"mycelium.product_evidence_state.v1\", \"events\": [".to_vec();
    fs::write(&state, &corrupt).unwrap();
    assert_eq!(mode(&state), 0o600);

    let app = Headless::launch(&owner.root);
    for (path, code) in [
        (SNAPSHOT, "product_snapshot_unavailable"),
        (EVENTS, "product_events_unavailable"),
        (EXPORT, "product_export_unavailable"),
    ] {
        assert_eq!(
            refusal(&app.get(path, None)),
            (
                503,
                json!({"protocol": "mycelium.product_ui.error.v1", "code": code,
                       "retryable": false, "reason": "product_evidence_state_corrupt"})
            ),
            "{path}"
        );
    }
    assert_eq!(fs::read(&state).unwrap(), corrupt, "never rewritten");
    assert!(owner.set_aside().is_empty());

    // The reset is an owner action: session, origin and the action header are required.
    let reset = "/api/v1/product/reset";
    let anonymous = app
        .client
        .request(
            "POST",
            reset,
            b"{}",
            &[("Content-Type", "application/json")],
            false,
            BudgetId::ModelSelection,
        )
        .unwrap();
    assert_eq!(anonymous.status, 401);
    let cross = app
        .client
        .request(
            "POST",
            reset,
            b"{}",
            &[
                ("Content-Type", "application/json"),
                ("Origin", "https://elsewhere.example"),
            ],
            true,
            BudgetId::ModelSelection,
        )
        .unwrap();
    assert_eq!(cross.status, 403);
    assert_eq!(fs::read(&state).unwrap(), corrupt);

    let done = app.post(reset, json!({}));
    let aside = owner.set_aside();
    assert_eq!(aside.len(), 1, "{aside:?}");
    assert_eq!(
        done,
        json!({"protocol": "mycelium.product_evidence_reset.v1",
               "reason": "product_evidence_state_corrupt",
               "set_aside_as": aside[0].file_name().unwrap().to_str().unwrap()})
    );
    assert_eq!(fs::read(&aside[0]).unwrap(), corrupt);
    assert_eq!(mode(&aside[0]), 0o600);
    // A fresh window: publication starts again and is stored.
    let fresh = frames(&app.get(EVENTS, None));
    assert_eq!(cursors(&fresh), [1]);
    assert_eq!(mode(&state), 0o600);
    // A readable window is never reset.
    let again = app.client.post(reset, &json!({})).unwrap();
    assert_eq!(
        refusal(&again),
        (
            409,
            json!({"protocol": "mycelium.product_ui.error.v1",
                   "code": "product_evidence_state_not_refused", "retryable": false})
        )
    );
    assert_eq!(owner.set_aside().len(), 1);
    app.quit();
}
