// SPDX-License-Identifier: AGPL-3.0-only
//! S1 prepare on the packaged executable: `POST /api/model/download` and `/api/model/cancel`
//! (app.py:1551-1577 @ f4e366e9) run `api::acquisition` through the packaged host into v0.1.4's
//! model-source layout (`_open_acquisition`, app.py:649-672: `<state>/model-source-<rev12>/`
//! `models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/<rev>/`; acquisition.py:1096-1184 `_execute`:
//! download, verify, `prepare_local_source`). Preparing the model never makes a one-Mac chat
//! (lane CHAT, owner ruling 14 Nov: one chat path, as v0.1.4): before and after the snapshot is
//! prepared, and after a relaunch over it, the page's chat is the owner run's route, which serves
//! nothing until a run does (`chat_not_ready`, chat.py:317-318).
//!
//! No network and no real model. A loopback repository serves a tiny snapshot: the config and
//! safetensors of the accepted `qwen2_monolithic` layout of the executed manifest oracle
//! (`api/tests/fixtures/oracle_manifest.json`, as `api::model_routes_e2e` serves it) and a
//! tokenizer file. What lands on disk is compared with the executed acquisition oracle
//! (`api/tests/fixtures/oracle_acquisition.json`, scenario by scenario), the route answers with the
//! executed v0.1.4 process (`api/tests/fixtures/oracle_model_routes.json`).
//!
//! This Mac's resource observation is controlled (the reviewer's executed v0.1.4 sequences replace
//! `DemoApp._policy_observer` the same way: `status/moonweaver/workers/rv-s1-app-host-model-prepare/`
//! `evidence/oracle/drive_v014.py` in the program root, its answers in `v014-all.out` and
//! `v014-revoke_quit.out`), so no test depends on the power, thermal state or network of the Mac it
//! runs on.
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use api::{
    acquisition::{HttpTransport, Transport},
    resources::{Observation, Observer},
    schema::PublicError,
};
use budgets::{BudgetId as B, Entries};
use desktop::{
    channel::{ORIGIN, Request},
    core::Core,
    lifecycle::{Cleanup, Poll},
    services::{ServiceRequest, Services},
};
use node_service::{
    app_host::ModelSource,
    desktop_host::DesktopServices,
    packaged::{self, AppCleanup},
    service::{Driver, Job, NodeService},
};
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::TcpListener,
    num::NonZeroUsize,
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};

const MANIFEST_ORACLE: &str = include_str!("../../api/tests/fixtures/oracle_manifest.json");
const ACQUISITION_ORACLE: &str = include_str!("../../api/tests/fixtures/oracle_acquisition.json");
const ROUTES_ORACLE: &str = include_str!("../../api/tests/fixtures/oracle_model_routes.json");
/// The pinned run model's revision (`backend_mlx::admission::REVISION`).
const REVISION: &str = backend_mlx::admission::REVISION;
/// v0.1.4's owner acquisition root for that model (`'model-source-' + resolved_commit[:12]`).
const SOURCE: &str = "model-source-7ae557604adf";
const MODEL_DIR: &str = "models--Qwen--Qwen2.5-0.5B-Instruct";
/// The executed acquisition scenarios' grant (`max_download_bytes` = `storage_limit_bytes`).
const GRANT_BYTES: u64 = 10_000_000;
const TOKENIZER: &[u8] = br#"{"version":"1.0","model":{"type":"BPE","vocab":{},"merges":[]}}"#;

fn millis(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
/// How long an attempt may take to settle (the api model-route tests' own bound).
fn patience() -> Duration {
    millis(B::ModelSelection) * 4
}
fn poll() -> Duration {
    millis(B::OwnerPoll)
}
struct MustNotRun;
impl Driver for MustNotRun {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        panic!("a model action started native work")
    }
}
/// A test-owned directory, removed with its contents when the test ends, also when it fails
/// (REVIEW-lanePH1 finding 8). Bind it before anything that uses it, so it is dropped last.
struct TestRoot(std::path::PathBuf);
impl std::ops::Deref for TestRoot {
    type Target = std::path::Path;
    fn deref(&self) -> &std::path::Path {
        &self.0
    }
}
impl AsRef<std::path::Path> for TestRoot {
    fn as_ref(&self) -> &std::path::Path {
        &self.0
    }
}
impl Drop for TestRoot {
    fn drop(&mut self) {
        if let Err(e) = std::fs::remove_dir_all(&self.0)
            && e.kind() != std::io::ErrorKind::NotFound
        {
            eprintln!("test root {:?} not removed: {e}", self.0);
        }
    }
}
fn state_root() -> TestRoot {
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "packaged-model-prepare-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    TestRoot(root)
}
fn identity() -> Value {
    api::acquisition::model_identity(&api::model_catalog::registered()[1])
}
fn snapshot_dir(state: &Path, revision: &str) -> PathBuf {
    state
        .join(SOURCE)
        .join(MODEL_DIR)
        .join("snapshots")
        .join(revision)
}

// ---- this Mac's resource observation, controlled ----

/// The observation the packaged host's resource guard reads in place of the bundled
/// `native/resource_probe`; a test changes it between requests, or holds the probe inside its
/// observation (as a slow helper would be) until released.
#[derive(Clone)]
struct Observed {
    seen: Arc<Mutex<Observation>>,
    probe: Arc<(Mutex<Probe>, Condvar)>,
}
#[derive(Default)]
struct Probe {
    /// The next observation waits inside the probe until released.
    held: bool,
    /// An observation is waiting inside the held probe.
    inside: bool,
}
impl Observed {
    fn new(seen: Observation) -> Self {
        Self {
            seen: Arc::new(Mutex::new(seen)),
            probe: Arc::default(),
        }
    }
    fn set(&self, seen: Observation) {
        *self.seen.lock().unwrap() = seen;
    }
    fn hold(&self) {
        self.probe.0.lock().unwrap().held = true;
    }
    /// Wait until an observation is inside the held probe.
    fn wait_inside(&self) {
        let deadline = Instant::now() + patience();
        let (probe, changed) = &*self.probe;
        let mut probe = probe.lock().unwrap();
        while !probe.inside {
            let left = deadline.saturating_duration_since(Instant::now());
            assert!(
                !left.is_zero(),
                "nothing observed this Mac while the probe was held"
            );
            probe = changed.wait_timeout(probe, left).unwrap().0;
        }
    }
    fn release(&self) {
        self.probe.0.lock().unwrap().held = false;
        self.probe.1.notify_all();
    }
}
impl Observer for Observed {
    fn observe(&self) -> Observation {
        let (probe, changed) = &*self.probe;
        let mut held = probe.lock().unwrap();
        if held.held {
            held.inside = true;
            changed.notify_all();
            while held.held {
                held = changed.wait(held).unwrap();
            }
            held.inside = false;
        }
        drop(held);
        self.seen.lock().unwrap().clone()
    }
}
/// Wait until `done` holds (the api model-route tests' patience).
fn wait_for(what: &str, done: impl Fn() -> bool) {
    let deadline = Instant::now() + patience();
    while !done() {
        assert!(Instant::now() < deadline, "{what}");
        std::thread::sleep(poll());
    }
}
fn observation(power: &str, thermal: &str, network: &str) -> Observation {
    Observation {
        power: power.into(),
        thermal: thermal.into(),
        network: network.into(),
    }
}
/// What the window's consent (`ac_only`, `unmetered_only`, `stop_on_serious`) permits.
fn permissive() -> Observation {
    observation("external", "nominal", "unmetered")
}

// ---- the tiny snapshot and its loopback repository ----

type Files = Vec<(String, Vec<u8>)>;
/// `config.json`, `model.safetensors`, `tokenizer.json`: the config and the BF16 [1] tensors of
/// the manifest oracle's accepted `qwen2_monolithic` case. `layers` declares another layer count
/// than the tensors carry (the manifest compile then refuses what the byte checks accepted).
fn tiny_snapshot(layers: Option<u32>) -> Files {
    let doc: Value = serde_json::from_str(MANIFEST_ORACLE).unwrap();
    let case = doc["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["label"] == "qwen2_monolithic")
        .unwrap();
    let mut config = case["files"]["config.json"]["text"]
        .as_str()
        .unwrap()
        .to_owned();
    if let Some(layers) = layers {
        let declared = "\"num_hidden_layers\": 24";
        assert!(config.contains(declared));
        config = config.replace(declared, &format!("\"num_hidden_layers\": {layers}"));
    }
    let spec = &case["files"]["model.safetensors"];
    let mut tensors: Vec<&Value> = spec["tensors"].as_array().unwrap().iter().collect();
    tensors.sort_by(|a, b| a[0].as_str().cmp(&b[0].as_str()));
    let seed = spec["seed"].as_u64().unwrap() as usize;
    let mut header = serde_json::Map::new();
    let (mut offset, mut data) = (0usize, Vec::new());
    for (i, tensor) in tensors.iter().enumerate() {
        header.insert(
            tensor[0].as_str().unwrap().to_owned(),
            json!({"dtype": "BF16", "shape": [1], "data_offsets": [offset, offset + 2]}),
        );
        data.extend((0..2).map(|j| ((seed + i + j) % 251) as u8));
        offset += 2;
    }
    let header = serde_json::to_vec(&Value::Object(header)).unwrap();
    let mut weights = (header.len() as u64).to_le_bytes().to_vec();
    weights.extend(header);
    weights.extend(data);
    vec![
        ("config.json".into(), config.into_bytes()),
        ("model.safetensors".into(), weights),
        ("tokenizer.json".into(), TOKENIZER.to_vec()),
    ]
}
fn total(files: &Files) -> u64 {
    files.iter().map(|(_, bytes)| bytes.len() as u64).sum()
}
fn size_of(files: &Files, name: &str) -> u64 {
    files.iter().find(|(p, _)| p == name).unwrap().1.len() as u64
}
#[derive(Default)]
struct Gate {
    /// The file whose response waits for [`Repository::release`].
    held: Option<String>,
    /// The held file was requested and its response is waiting.
    waiting: bool,
    /// The file served with one byte changed (same size: only its digest differs).
    tamper: Option<String>,
}
struct Shared {
    files: Files,
    gate: Mutex<Gate>,
    changed: Condvar,
    stop: AtomicBool,
}
/// A one-thread loopback repository of the pinned model: the revision document (`?blobs=true`,
/// every sibling LFS with its sha256) and the `resolve` files.
struct Repository {
    origin: String,
    shared: Arc<Shared>,
    server: Option<JoinHandle<()>>,
}
impl Repository {
    fn start(files: Files) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let origin = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
        let shared = Arc::new(Shared {
            files,
            gate: Mutex::new(Gate::default()),
            changed: Condvar::new(),
            stop: AtomicBool::new(false),
        });
        let serving = shared.clone();
        let server = std::thread::spawn(move || serve(&listener, &serving));
        Self {
            origin,
            shared,
            server: Some(server),
        }
    }
    fn transport(&self) -> Arc<dyn Transport> {
        Arc::new(HttpTransport::loopback_for_tests(&self.origin))
    }
    fn tamper(&self, file: Option<&str>) {
        self.shared.gate.lock().unwrap().tamper = file.map(str::to_owned);
    }
    fn hold(&self, file: &str) {
        self.shared.gate.lock().unwrap().held = Some(file.to_owned());
    }
    /// Wait until the held file was requested; its response is not sent yet.
    fn wait_held(&self) {
        let deadline = Instant::now() + patience();
        let mut gate = self.shared.gate.lock().unwrap();
        while !gate.waiting {
            let left = deadline.saturating_duration_since(Instant::now());
            assert!(!left.is_zero(), "the held file was never requested");
            gate = self.shared.changed.wait_timeout(gate, left).unwrap().0;
        }
    }
    fn release(&self) {
        self.shared.gate.lock().unwrap().held = None;
        self.shared.changed.notify_all();
    }
}
impl Drop for Repository {
    fn drop(&mut self) {
        self.shared.stop.store(true, Ordering::SeqCst);
        self.release();
        if let Some(server) = self.server.take() {
            server.join().unwrap();
        }
    }
}
fn serve(listener: &TcpListener, shared: &Shared) {
    let model = &api::model_catalog::registered()[1];
    let (id, commit) = (model.model_id, model.resolved_commit);
    while !shared.stop.load(Ordering::SeqCst) {
        let Ok((mut stream, _)) = listener.accept() else {
            std::thread::sleep(poll() / 50);
            continue;
        };
        stream.set_nonblocking(false).unwrap();
        let mut head = Vec::new();
        let mut byte = [0u8; 1];
        while !head.ends_with(b"\r\n\r\n") {
            match stream.read(&mut byte) {
                Ok(1) => head.push(byte[0]),
                _ => break,
            }
        }
        let text = String::from_utf8_lossy(&head).into_owned();
        let target = text.split(' ').nth(1).unwrap_or_default().to_owned();
        let body = if target == format!("/api/models/{id}/revision/{commit}?blobs=true") {
            let siblings: Vec<Value> = shared
                .files
                .iter()
                .map(|(path, bytes)| {
                    let sha = protocol::canonical::sha256_prefixed(bytes)[7..].to_owned();
                    json!({"rfilename": path, "size": bytes.len(),
                        "lfs": {"size": bytes.len(), "sha256": sha}})
                })
                .collect();
            Some(
                json!({"id": id, "modelId": id, "sha": commit, "private": false, "siblings": siblings})
                    .to_string()
                    .into_bytes(),
            )
        } else if let Some(rest) = target.strip_prefix(&format!("/{id}/resolve/{commit}/")) {
            let path = rest.split('?').next().unwrap_or_default();
            shared
                .files
                .iter()
                .find(|(p, _)| p == path)
                .map(|(p, bytes)| {
                    let mut gate = shared.gate.lock().unwrap();
                    if gate.held.as_deref() == Some(p.as_str()) {
                        gate.waiting = true;
                        shared.changed.notify_all();
                        while gate.held.as_deref() == Some(p.as_str())
                            && !shared.stop.load(Ordering::SeqCst)
                        {
                            gate = shared.changed.wait(gate).unwrap();
                        }
                        gate.waiting = false;
                    }
                    let mut bytes = bytes.clone();
                    if gate.tamper.as_deref() == Some(p.as_str()) {
                        let last = bytes.len() - 1;
                        bytes[last] ^= 1;
                    }
                    bytes
                })
        } else {
            None
        };
        let reply = match body {
            Some(body) => {
                let mut out = format!(
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                )
                .into_bytes();
                out.extend(body);
                out
            }
            None => {
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_vec()
            }
        };
        stream.write_all(&reply).unwrap_or_default();
    }
}

// ---- the packaged window: the action root, or the whole packaged core ----

trait Window {
    fn call(&self, path: &str, body: Value) -> (u16, Value);
}
fn channel(path: &str) -> Vec<(String, String)> {
    // The window's channel header: chat pages send `X-Mycelium-Chat`, actions `X-Mycelium-Action`.
    let channel = if path.starts_with("/chat/") {
        "x-mycelium-chat"
    } else {
        "x-mycelium-action"
    };
    vec![
        (channel.into(), "1".into()),
        ("content-type".into(), "application/json".into()),
    ]
}
fn answer(status: u16, body: &[u8]) -> (u16, Value) {
    (status, serde_json::from_slice(body).unwrap())
}
impl Window for DesktopServices {
    fn call(&self, path: &str, body: Value) -> (u16, Value) {
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        self.submit(
            ServiceRequest {
                method: "POST".into(),
                target: path.into(),
                headers: channel(path),
                body: serde_json::to_vec(&body).unwrap(),
            },
            Box::new(move |reply| tx.send(reply).unwrap()),
        );
        let reply = rx.recv_timeout(millis(B::WorkerStartup)).unwrap();
        answer(reply.status, &reply.body)
    }
}
impl Window for Arc<dyn Core> {
    fn call(&self, path: &str, body: Value) -> (u16, Value) {
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        self.submit(
            Request {
                window: "main".into(),
                method: "POST".into(),
                uri: format!("{ORIGIN}{path}"),
                headers: channel(path),
                body: serde_json::to_vec(&body).unwrap(),
            },
            Box::new(move |reply| tx.send(reply).unwrap()),
        );
        let reply = rx.recv_timeout(millis(B::WorkerStartup)).unwrap();
        answer(reply.status, &reply.body)
    }
}
fn refusal(reply: &(u16, Value)) -> (u16, &str) {
    (
        reply.0,
        reply.1["error"].as_str().unwrap_or("<not a refusal>"),
    )
}
/// The consent form's body as the v0.1.4 window sends it (`consent_valid`).
fn consent() -> Value {
    json!({
        "peer_visibility": true, "power": "ac_only",
        "storage_bytes": api::model_catalog::storage_consent_ceiling(),
        "network": "unmetered_only", "background": false, "diagnostics": false,
        "thermal": "stop_on_serious",
    })
}
fn create(window: &impl Window) -> (u16, Value) {
    window.call("/api/create", json!({"consent": consent()}))
}
/// `/api/create` made this Mac the swarm's current member (what `/api/model/download` needs).
fn assert_created(window: &impl Window) {
    let created = create(window);
    assert_eq!(created.0, 200, "create: {}", created.1);
}
fn download(window: &impl Window) -> (u16, Value) {
    window.call(
        "/api/model/download",
        json!({"consent": {"download_acknowledged": true,
            "max_download_bytes": GRANT_BYTES, "storage_limit_bytes": GRANT_BYTES}}),
    )
}
fn cancel(window: &impl Window) -> (u16, Value) {
    window.call("/api/model/cancel", json!({}))
}
fn chat_state(window: &impl Window) -> (u16, Value) {
    window.call("/chat/api/state", json!({}))
}
/// The page's chat is the run route's and no run serves: the state answers unavailable with no
/// model and a send is refused `chat_not_ready` (chat.py:317-318, 477-503), whatever is on disk.
#[track_caller]
fn assert_no_run_chat(window: &impl Window) {
    let state = chat_state(window);
    assert_eq!(state.0, 200, "chat state: {}", state.1);
    assert_eq!(
        (&state.1["available"], &state.1["model_id"]),
        (&json!(false), &Value::Null),
        "{}",
        state.1
    );
    let sent = window.call(
        "/chat/api/send",
        json!({"prompt": "hi", "max_new_tokens": 8}),
    );
    assert_eq!(refusal(&sent), (503, "chat_not_ready"));
}
/// The pinned snapshot's two files are where v0.1.4's lookup (and the run model's) finds them.
fn located(state: &Path) -> bool {
    let snapshot = snapshot_dir(state, REVISION);
    snapshot.join("model.safetensors").is_file() && snapshot.join("tokenizer.json").is_file()
}
/// The download answered with the attempt it started (racy by nature: the status at the instant
/// the worker started, `api::model_routes_e2e`).
fn assert_started(started: &(u16, Value)) {
    assert_eq!(started.0, 200, "download: {}", started.1);
    let state = started.1["state"].as_str().unwrap_or_default();
    assert!(
        [
            "starting",
            "resolving_metadata",
            "checking_capacity",
            "downloading"
        ]
        .contains(&state),
        "download: {}",
        started.1
    );
    assert_eq!(started.1["model"], identity());
}
/// The run model's source in `state` over `transport`, this Mac observed through `observed`.
fn model_source(state: &Path, transport: Arc<dyn Transport>, observed: &Observed) -> ModelSource {
    ModelSource::new(state.to_owned(), transport).with_observer(Arc::new(observed.clone()))
}
/// The packaged action root (`PersistentBackend<PackagedHost, …>`) over `source` (a clone: the
/// test reads the same state), the node service's driver never started.
fn action_root(state: &Path, source: &ModelSource) -> DesktopServices {
    let dirs = packaged::prepare_dirs(state).unwrap();
    let node = NodeService::new(
        Arc::new(MustNotRun),
        "private-local-token".into(),
        node_service::native::development_qualification().unwrap(),
    );
    DesktopServices::with_model_source(
        node,
        NonZeroUsize::new(4).unwrap(),
        &dirs.private,
        source.clone(),
    )
}
/// The packaged executable's core over `state` (`packaged::core`, the repository and this Mac's
/// observation given).
fn packaged_core(
    state: &Path,
    repository: &Repository,
    observed: &Observed,
) -> (Arc<dyn Core>, AppCleanup) {
    let (core, mut cleanup) = packaged::core_with_network(
        state,
        repository.transport(),
        Arc::new(observed.clone()),
        macs::test_network(),
    )
    .unwrap();
    cleanup.bind_clock(Instant::now()).unwrap();
    (core, cleanup)
}
/// Quit: the window's cleanup reaches `Verified` within the quit allowance.
fn quit(core: Arc<dyn Core>, cleanup: AppCleanup) {
    quit_with(core, cleanup, || ());
}
/// Quit with `meanwhile` run once the cleanup has begun (the window's Quit returns at once; the
/// process then waits for its cleanup).
fn quit_with(core: Arc<dyn Core>, mut cleanup: AppCleanup, meanwhile: impl FnOnce()) {
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    cleanup.begin(allowance).unwrap();
    meanwhile();
    let deadline = Instant::now() + Duration::from_millis(allowance.millis());
    while cleanup.poll().unwrap() != Poll::Verified {
        assert!(
            Instant::now() < deadline,
            "the packaged core did not clean up"
        );
        std::thread::sleep(poll());
    }
    drop(core);
}

// ---- what the acquisition left: its persisted status and its tree ----

/// The acquisition's persisted status document (`status.json`), `null` before the first one.
fn acquisition_status(state: &Path) -> Value {
    match std::fs::read(state.join(SOURCE).join("status.json")) {
        Ok(bytes) => serde_json::from_slice(&bytes).unwrap(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Value::Null,
        Err(e) => panic!("status.json: {e}"),
    }
}
fn settled(state: &Path) -> Value {
    let deadline = Instant::now() + patience();
    loop {
        let status = acquisition_status(state);
        if ["prepared", "failed", "cancelled"]
            .iter()
            .any(|s| status["state"] == *s)
        {
            return status;
        }
        assert!(
            Instant::now() < deadline,
            "the acquisition never settled: {status}"
        );
        std::thread::sleep(poll());
    }
}
fn status_doc(state: &str, downloaded: u64, total: u64, error: Option<&str>) -> Value {
    json!({"state": state, "downloaded_bytes": downloaded, "total_bytes": total,
        "error_code": error, "model": identity()})
}
/// Everything under `root` as the acquisition oracle records it (`path`, `kind`, `mode`, `size`);
/// `status.json`'s size is its document's (compared by content instead).
fn tree(root: &Path) -> Vec<Value> {
    fn walk(dir: &Path, root: &Path, out: &mut Vec<Value>) {
        for entry in std::fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            let meta = std::fs::symlink_metadata(&path).unwrap();
            let relative = path
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            let mode = meta.mode() & 0o7777;
            if meta.is_dir() {
                out.push(json!({"path": relative, "kind": "dir", "mode": mode}));
                walk(&path, root, out);
            } else if relative == "status.json" {
                out.push(json!({"path": relative, "kind": "file", "mode": mode}));
            } else {
                out.push(
                    json!({"path": relative, "kind": "file", "mode": mode, "size": meta.len()}),
                );
            }
        }
    }
    let mut out = Vec::new();
    walk(root, root, &mut out);
    out.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
    out
}
/// The executed acquisition oracle's scenario `name`.
fn scenario(name: &str) -> Value {
    let doc: Value = serde_json::from_str(ACQUISITION_ORACLE).unwrap();
    assert_eq!(doc["model"]["commit"], REVISION);
    doc["scenarios"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["spec"]["name"] == name)
        .unwrap()["oracle"]
        .clone()
}
/// The oracle's tree for `name` in this snapshot's names and sizes: its `sub/tokenizer.json` is
/// this repository's `tokenizer.json` (so its `sub` directories have no counterpart), a file keeps
/// this snapshot's size and a `.partial` the oracle's.
fn oracle_tree(name: &str, files: &Files) -> Vec<Value> {
    let mut out: Vec<Value> = scenario(name)["tree"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| !(e["kind"] == "dir" && e["path"].as_str().unwrap().ends_with("/sub")))
        .map(|e| {
            let path = e["path"]
                .as_str()
                .unwrap()
                .replace("/sub/tokenizer.json", "/tokenizer.json");
            let name = path.rsplit('/').next().unwrap().to_owned();
            let mut entry = json!({"path": path, "kind": e["kind"], "mode": e["mode"]});
            if e["kind"] == "file" && name != "status.json" {
                entry["size"] = match files.iter().find(|(p, _)| *p == name) {
                    Some((_, bytes)) => json!(bytes.len()),
                    None => e["size"].clone(),
                };
            }
            entry
        })
        .collect();
    out.sort_by(|a, b| a["path"].as_str().cmp(&b["path"].as_str()));
    out
}
/// The oracle's final `(state, error_code)` for `name`.
fn oracle_outcome(name: &str) -> (Value, Value) {
    let oracle = scenario(name);
    let last = &oracle["ops"].as_array().unwrap().last().unwrap()["final_status"];
    (last["state"].clone(), last["error_code"].clone())
}
fn outcome(status: &Value) -> (Value, Value) {
    (status["state"].clone(), status["error_code"].clone())
}
fn entries(dir: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}
/// A snapshot written straight into the model-source layout (a restored or foreign copy).
fn plant(state: &Path, revision: &str, files: &Files) {
    let dir = snapshot_dir(state, revision);
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&dir)
        .unwrap();
    for (name, bytes) in files {
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(dir.join(name))
            .unwrap()
            .write_all(bytes)
            .unwrap();
    }
}

#[test]
fn packaged_model_prepare_download_reaches_prepared() {
    let state = state_root();
    let files = tiny_snapshot(None);
    let repository = Repository::start(files.clone());
    let observed = Observed::new(permissive());
    let source = model_source(&state, repository.transport(), &observed);
    let host = action_root(&state, &source);
    assert_created(&host);
    // `_resource_check` (app.py:1567-1570 -> 929-947, resources.py:152-181 @ f4e366e9) against
    // the window's consent (`ac_only`, `unmetered_only`): what this Mac reports is refused with the
    // guard's own code, kept as `acquisition_policy_error`, and nothing is fetched (the model
    // source holds only its lock). v0.1.4's answers: the reviewer's executed sequence `guard`
    // (`v014-all.out`: battery, metered, thermal serious, network unknown, observer raising ->
    // UNKNOWN_OBSERVATION); unknown thermal is the near miss of the thermal gate.
    let unprobed = Observation::unknown();
    for (label, seen, code) in [
        (
            "battery",
            observation("battery", "nominal", "unmetered"),
            "resource_power_policy_violation",
        ),
        (
            "metered",
            observation("external", "nominal", "metered"),
            "resource_network_policy_violation",
        ),
        (
            "thermal_serious",
            observation("external", "serious", "unmetered"),
            "resource_thermal_policy_violation",
        ),
        (
            "network_unknown",
            observation("external", "nominal", "unknown"),
            "resource_network_policy_violation",
        ),
        (
            "observer_fails",
            unprobed.clone(),
            "resource_power_policy_violation",
        ),
        (
            "thermal_unknown",
            observation("external", "unknown", "unmetered"),
            "resource_thermal_policy_violation",
        ),
    ] {
        observed.set(seen.clone());
        assert_eq!(refusal(&download(&host)), (400, code), "{label}");
        assert_eq!(source.policy_error(), Some(code), "{label}");
        assert_eq!(source.resource_observation(), seen, "{label}");
        assert_eq!(
            entries(&state.join(SOURCE)),
            ["acquisition.lock"],
            "{label}"
        );
    }
    // In production the observation is the helper bundled beside the executable
    // (`native/resource_probe`); a bundle without one observes nothing and is refused as an
    // observer failure is (fail closed), before anything is fetched.
    let exe = std::env::current_exe().unwrap();
    assert!(!exe.parent().unwrap().join("native/resource_probe").exists());
    let bare = state_root();
    let bundled = ModelSource::new(bare.to_path_buf(), repository.transport());
    let bare_host = action_root(&bare, &bundled);
    assert_created(&bare_host);
    assert_eq!(
        refusal(&download(&bare_host)),
        (400, "resource_power_policy_violation")
    );
    assert_eq!(bundled.resource_observation(), unprobed);
    assert_eq!(entries(&bare.join(SOURCE)), ["acquisition.lock"]);
    drop(bare_host);
    std::fs::remove_dir_all(&bare).unwrap_or_default();
    // What the consent permits: the download starts and the refusal is cleared (`guard`,
    // `download_good` -> `settled_good` prepared).
    observed.set(permissive());
    // `self.acquisition.start(grant)`: one background attempt, its status answered.
    assert_started(&download(&host));
    assert_eq!(source.policy_error(), None);
    assert_eq!(source.resource_observation(), permissive());
    let prepared = settled(&state);
    assert_eq!(
        prepared,
        status_doc("prepared", total(&files), total(&files), None)
    );
    assert_eq!(outcome(&prepared), oracle_outcome("happy_path"));
    // v0.1.4's layout under the state root: one `model-source-<rev12>` beside `v02-node`, its
    // lock, status and the verified snapshot of the pinned revision, private modes throughout.
    assert_eq!(entries(&state), [SOURCE, "v02-node"]);
    assert_eq!(tree(&state.join(SOURCE)), oracle_tree("happy_path", &files));
    let snapshot = snapshot_dir(&state, REVISION);
    for (name, bytes) in &files {
        assert_eq!(
            &std::fs::read(snapshot.join(name)).unwrap(),
            bytes,
            "{name}"
        );
    }
    // v0.1.4's lookup finds exactly that snapshot.
    assert!(located(&state));
    // Download twice: the attempt has ended, the source is already prepared and stays so
    // (oracle `run_twice_after_prepared`).
    assert_eq!(refusal(&download(&host)), (409, "source_already_prepared"));
    // Cancel after prepared: nothing runs, the prepared status is answered and kept.
    assert_eq!(cancel(&host), (200, prepared.clone()));
    assert_eq!(acquisition_status(&state), prepared);
    assert_eq!(tree(&state.join(SOURCE)), oracle_tree("happy_path", &files));
    drop(host);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

#[test]
fn packaged_model_prepare_never_makes_a_one_mac_chat() {
    let state = state_root();
    let files = tiny_snapshot(None);
    // A snapshot of another revision is not the pinned model (near miss).
    plant(&state, &"0".repeat(40), &files);
    let repository = Repository::start(files.clone());
    let observed = Observed::new(permissive());
    let (core, cleanup) = packaged_core(&state, &repository, &observed);
    // No model: the window opens and the chat is the run route's.
    assert_no_run_chat(&core);
    // The action root is the packaged one: the owner creates the swarm and asks for the model.
    assert_created(&core);
    assert_no_run_chat(&core);
    assert_started(&download(&core));
    assert_eq!(
        settled(&state),
        status_doc("prepared", total(&files), total(&files), None)
    );
    // Prepared: the chat is still the run route's (no run serves), and no one-Mac native driver
    // was composed over the snapshot (it would keep its member identity in the private root).
    assert_no_run_chat(&core);
    let private = state.join("v02-node/private");
    assert!(!private.join("member").exists(), "{:?}", entries(&private));
    quit(core, cleanup);
    // Relaunch after prepared: the window composes the located model at open. Quit closed the
    // acquisition (`DemoApp.close` -> `self.acquisition.close()`, app.py:1845-1847 @ f4e366e9:
    // cancel, join, `closed` persisted, acquisition.py:1524-1549), so the next process answers
    // v0.1.4's quit-path `closed` with the bytes kept: the reviewer's executed sequences
    // `compile_refused_relaunch` and `quit_running` (`cancel_after_relaunch` 200 `closed`) and the
    // acquisition oracle `close_when_idle` (the `new` step after `close`). A process that died
    // without closing recovers `failed prepared_source_revalidation_required` instead
    // (`happy_then_reverify`, the `new` step): the crash path, not Quit.
    let (core, cleanup) = packaged_core(&state, &repository, &observed);
    assert_no_run_chat(&core);
    let oracle = scenario("close_when_idle");
    let reopened = &oracle["ops"][2];
    assert_eq!(reopened["op"], "new");
    let answered = cancel(&core);
    assert_eq!(answered.0, 200, "{}", answered.1);
    assert_eq!(outcome(&answered.1), outcome(&reopened["status"]));
    assert_eq!(
        answered.1,
        status_doc("closed", total(&files), total(&files), None)
    );
    assert_eq!(acquisition_status(&state), answered.1);
    quit(core, cleanup);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

#[test]
fn packaged_model_prepare_bad_snapshot_refused() {
    // A file whose bytes miss the pinned digest: refused while downloading, nothing is prepared.
    let state = state_root();
    let files = tiny_snapshot(None);
    let repository = Repository::start(files.clone());
    repository.tamper(Some("model.safetensors"));
    let observed = Observed::new(permissive());
    let (core, cleanup) = packaged_core(&state, &repository, &observed);
    assert_created(&core);
    assert_started(&download(&core));
    let failed = settled(&state);
    // Only the config was verified and kept (the mismatched partial is removed).
    assert_eq!(
        failed,
        status_doc(
            "failed",
            size_of(&files, "config.json"),
            total(&files),
            Some("artifact_digest_mismatch")
        )
    );
    assert_eq!(outcome(&failed), oracle_outcome("digest_mismatch_lfs"));
    assert_eq!(
        tree(&state.join(SOURCE)),
        oracle_tree("digest_mismatch_lfs", &files)
    );
    assert!(!located(&state));
    assert_no_run_chat(&core);
    // Download after the failure, the source repaired: the attempt resumes from what verified
    // and prepares (oracle `run_after_failure_retries`); the chat stays the run route's.
    repository.tamper(None);
    assert_started(&download(&core));
    assert_eq!(
        settled(&state),
        status_doc("prepared", total(&files), total(&files), None)
    );
    assert_no_run_chat(&core);
    quit(core, cleanup);
    std::fs::remove_dir_all(&state).unwrap_or_default();

    // A snapshot every byte check accepts but the native manifest compile refuses: it is on disk
    // where v0.1.4's lookup lands, yet it was never prepared; the chat stays the run route's.
    let state = state_root();
    let files = tiny_snapshot(Some(25));
    let repository = Repository::start(files.clone());
    let (core, cleanup) = packaged_core(&state, &repository, &observed);
    assert_created(&core);
    assert_started(&download(&core));
    let refused = settled(&state);
    assert_eq!(
        refused,
        status_doc(
            "failed",
            total(&files),
            total(&files),
            Some("native_manifest_compile_failed")
        )
    );
    assert_eq!(outcome(&refused), oracle_outcome("compile_serving_error"));
    assert_eq!(
        tree(&state.join(SOURCE)),
        oracle_tree("compile_serving_error", &files)
    );
    assert!(located(&state));
    assert_no_run_chat(&core);
    quit(core, cleanup);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

#[test]
fn packaged_model_prepare_cancel_leaves_no_partial() {
    let state = state_root();
    let files = tiny_snapshot(None);
    let repository = Repository::start(files.clone());
    let observed = Observed::new(permissive());
    let (core, cleanup) = packaged_core(&state, &repository, &observed);
    let routes: Value = serde_json::from_str(ROUTES_ORACLE).unwrap();
    let step = |label: &str| {
        routes["steps"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["label"] == label)
            .unwrap()
            .clone()
    };
    // Cancel before anything started: a request body is refused, an empty one answers the idle
    // status (the executed v0.1.4 process's `cancel_with_body`, `cancel_fresh`).
    let with_body = core.call("/api/model/cancel", json!({"x": 1}));
    assert_eq!(
        (json!(with_body.0), with_body.1),
        (
            step("cancel_with_body")["status"].clone(),
            step("cancel_with_body")["body"].clone()
        )
    );
    let fresh = cancel(&core);
    assert_eq!(
        (json!(fresh.0), fresh.1),
        (
            step("cancel_fresh")["status"].clone(),
            step("cancel_fresh")["body"].clone()
        )
    );
    assert_created(&core);
    // Quit with nothing ever started: Quit closes the acquisition (`DemoApp.close` ->
    // `self.acquisition.close()`, app.py:1845-1847 @ f4e366e9), which persists `closed` over the
    // idle status, and the next process answers it (acquisition oracle `close_when_idle`: `close`,
    // then `new`).
    quit(core, cleanup);
    let idle_closed = status_doc("closed", 0, 0, None);
    let oracle = scenario("close_when_idle");
    assert_eq!(acquisition_status(&state), idle_closed);
    assert_eq!(outcome(&idle_closed), outcome(&oracle["ops"][0]["status"]));
    let (core, cleanup) = packaged_core(&state, &repository, &observed);
    assert_eq!(cancel(&core), (200, idle_closed.clone()));
    assert_eq!(outcome(&idle_closed), outcome(&oracle["ops"][2]["status"]));
    // A download the resource guard refuses after the relaunch fetches nothing and leaves the
    // closed status as it was (the reviewer's `guard`: `tree_after_battery`).
    observed.set(observation("battery", "nominal", "unmetered"));
    assert_eq!(
        refusal(&download(&core)),
        (400, "resource_power_policy_violation")
    );
    assert_eq!(acquisition_status(&state), idle_closed);
    assert_eq!(
        entries(&state.join(SOURCE)),
        ["acquisition.lock", "status.json"]
    );
    observed.set(permissive());
    // The weights' response is held: the attempt waits inside its request.
    repository.hold("model.safetensors");
    assert_started(&download(&core));
    repository.wait_held();
    // A second download while one runs (`_acquisition_active`).
    assert_eq!(
        refusal(&download(&core)),
        (400, "acquisition_already_active")
    );
    // Cancel while it runs: `self._cancel_acquisition(); return self.acquisition.status()`, the
    // status at that instant (the config is in, the weights requested).
    assert_eq!(
        cancel(&core),
        (
            200,
            status_doc(
                "downloading",
                size_of(&files, "config.json"),
                total(&files),
                None
            )
        )
    );
    repository.release();
    let cancelled = settled(&state);
    assert_eq!(
        cancelled,
        status_doc(
            "cancelled",
            size_of(&files, "config.json"),
            total(&files),
            None
        )
    );
    assert_eq!(outcome(&cancelled), oracle_outcome("cancel_before_file"));
    // No partial model source: the resumable stage stays where v0.1.4 keeps it, nothing reaches
    // the snapshot v0.1.4's lookup reads, and the chat stays the run route's.
    assert_eq!(
        tree(&state.join(SOURCE)),
        oracle_tree("cancel_before_file", &files)
    );
    assert!(!snapshot_dir(&state, REVISION).exists());
    assert!(!located(&state));
    assert_no_run_chat(&core);
    // Cancel with nothing running answers the cancelled status and changes nothing.
    assert_eq!(cancel(&core), (200, cancelled.clone()));
    // Quit while an attempt runs: Quit asks the attempt to stop at once and closes the
    // acquisition (cancel, join, `closed` persisted, acquisition.py:1524-1549); the stage stays
    // resumable and nothing reaches the snapshot. The response it waits in is released once Quit
    // has begun, as in the reviewer's executed v0.1.4 sequence `quit_running` (`status_after_close`:
    // `closed`, no error, the config's bytes; `cancel_after_relaunch`: 200 the same).
    repository.hold("model.safetensors");
    assert_started(&download(&core));
    repository.wait_held();
    quit_with(core, cleanup, || repository.release());
    let closed = status_doc(
        "closed",
        size_of(&files, "config.json"),
        total(&files),
        None,
    );
    assert_eq!(acquisition_status(&state), closed);
    assert_eq!(
        tree(&state.join(SOURCE)),
        oracle_tree("cancel_before_file", &files)
    );
    assert!(!snapshot_dir(&state, REVISION).exists());
    let (core, cleanup) = packaged_core(&state, &repository, &observed);
    assert_eq!(cancel(&core), (200, closed));
    // Download after the quit resumes the stage to prepared; the chat stays the run route's.
    assert_started(&download(&core));
    assert_eq!(
        settled(&state),
        status_doc("prepared", total(&files), total(&files), None)
    );
    assert_eq!(tree(&state.join(SOURCE)), oracle_tree("happy_path", &files));
    assert_no_run_chat(&core);
    quit(core, cleanup);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// The executed v0.1.4 process's `/api/model/*` sequence (`oracle_model_routes.json`: shape,
/// membership and grant refusals, cancel, the offline refusal of the public hub, the retry after
/// a failure, stop/start) replayed on the packaged action root, offline as the oracle ran. The
/// `status_view` blocks are compared on their `acquisition` (the persisted status) and, through
/// the model source, on `resource_observation` and `acquisition_policy_error`: this Mac is
/// observed as the oracle's own probe observed its Mac (`battery`, `nominal`, `unmetered`), which
/// the oracle's `battery_allowed`/`metered_allowed` policy permits. The packaged `/api/status`
/// projects neither, nor `running` or `membership_state` (declared, counted).
#[test]
fn packaged_model_prepare_routes_equal_the_executed_oracle() {
    let oracle: Value = serde_json::from_str(ROUTES_ORACLE).unwrap();
    let state = state_root();
    let observed = Observed::new(observation("battery", "nominal", "unmetered"));
    let source = model_source(
        &state,
        Arc::new(HttpTransport::hugging_face(true)),
        &observed,
    );
    let host = action_root(&state, &source);
    const GB: u64 = 1_000_000_000;
    let grant = json!({"download_acknowledged": true, "max_download_bytes": 2 * GB, "storage_limit_bytes": 4 * GB});
    let policy = json!({"peer_visibility": true, "power": "battery_allowed", "storage_bytes": 4 * GB,
        "network": "metered_allowed", "background": false, "diagnostics": false, "thermal": "stop_on_serious"});
    let (mut exact, mut declared) = (0, 0);
    for step in oracle["steps"].as_array().unwrap() {
        let label = step["label"].as_str().unwrap();
        if let Some(view) = step.get("status_view") {
            // `settled`: the oracle's view once its attempt had ended, read when the persisted
            // status is terminal (a condition, not a fixed wait).
            let status = if label.starts_with("after_download") || label == "settled" {
                settled(&state)
            } else {
                acquisition_status(&state)
            };
            // Before the first attempt nothing was persisted: the in-memory status is idle.
            let status = if status.is_null() {
                status_doc("idle", 0, 0, None)
            } else {
                status
            };
            assert_eq!(status, view["acquisition"], "status view at {label}");
            assert_eq!(
                source.resource_observation().to_value(),
                view["resource_observation"],
                "resource observation at {label}"
            );
            assert_eq!(
                json!(source.policy_error()),
                view["acquisition_policy_error"],
                "policy error at {label}"
            );
            exact += 3;
            declared += view.as_object().unwrap().len() - 3;
            continue;
        }
        let path = step["path"].as_str().unwrap();
        let request = if step["request"].is_null() {
            json!({})
        } else if label.starts_with("grant_") || label.starts_with("shape_") {
            step["request"].clone()
        } else if label == "create" {
            json!({"consent": policy})
        } else if label.starts_with("download") {
            json!({"consent": grant})
        } else {
            step["request"].clone()
        };
        let (status, body) = host.call(path, request);
        assert_eq!(json!(status), step["status"], "{label}: {body}");
        match label {
            // The status at the instant the worker had started (`api::model_routes_e2e`).
            "download_valid" | "download_again_after_failure" | "download_after_restart_valid" => {
                assert!(
                    ["starting", "resolving_metadata", "failed"]
                        .contains(&body["state"].as_str().unwrap_or_default()),
                    "{label}: {body}"
                );
                assert_eq!(body["model"], step["body"]["model"], "{label}");
                declared += 1;
            }
            // Their bodies are the authority's (compared in `packaged_owner_create`).
            "create" | "start" => declared += 1,
            _ => {
                assert_eq!(body, step["body"], "{label}");
                exact += 1;
            }
        }
    }
    println!("ORACLE-ROUTES exact={exact} declared={declared}");
    assert_eq!(entries(&state), [SOURCE, "v02-node"]);
    drop(host);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// A snapshot already on disk (a v0.1.4 copy) is re-verified by the download, never trusted: a
/// file whose bytes changed and a missing file are refused with the oracle's codes
/// (`snapshot_bad_digest`, `snapshot_missing_file`), and the snapshot is left as it was.
#[test]
fn packaged_model_prepare_present_snapshot_reverified() {
    for (name, file) in [
        ("snapshot_bad_digest", "model.safetensors"),
        ("snapshot_missing_file", "tokenizer.json"),
    ] {
        let state = state_root();
        let files = tiny_snapshot(None);
        let mut present = files.clone();
        if name == "snapshot_bad_digest" {
            let entry = present.iter_mut().find(|(p, _)| p == file).unwrap();
            let last = entry.1.len() - 1;
            entry.1[last] ^= 1;
        } else {
            present.retain(|(p, _)| p != file);
        }
        plant(&state, REVISION, &present);
        let repository = Repository::start(files.clone());
        let observed = Observed::new(permissive());
        let source = model_source(&state, repository.transport(), &observed);
        let host = action_root(&state, &source);
        assert_created(&host);
        assert_started(&download(&host));
        let failed = settled(&state);
        assert_eq!(outcome(&failed), oracle_outcome(name), "{name}: {failed}");
        assert_eq!(failed["total_bytes"], total(&files), "{name}");
        assert_eq!(failed["downloaded_bytes"], 0, "{name}");
        let snapshot = snapshot_dir(&state, REVISION);
        assert_eq!(entries(&snapshot).len(), present.len(), "{name}");
        for (path, bytes) in &present {
            assert_eq!(
                &std::fs::read(snapshot.join(path)).unwrap(),
                bytes,
                "{name}"
            );
        }
        drop(host);
        std::fs::remove_dir_all(&state).unwrap_or_default();
    }
}

// ---- Quit's close of the download, whatever else the cleanup met ----

/// A driver that returns without the request's terminal: the node service cannot prove its
/// cleanup, so the window's own Quit cleanup fails (`cleanup_unproven`, service.rs `poll_shutdown`).
struct EndsWithoutTerminal;
impl Driver for EndsWithoutTerminal {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        Ok(())
    }
}
/// The packaged core (`packaged::assemble`) over the action root composed with `driver`.
fn assembled(
    state: &Path,
    source: &ModelSource,
    driver: Arc<dyn Driver>,
) -> (Arc<dyn Core>, AppCleanup) {
    let (core, mut cleanup, _) = assembled_unbound(state, source, driver);
    cleanup.bind_clock(Instant::now()).unwrap();
    (core, cleanup)
}
/// [`assembled`] before the shell bound its clock, with the window's services (a clone of what
/// the core serves) for the test to call directly.
fn assembled_unbound(
    state: &Path,
    source: &ModelSource,
    driver: Arc<dyn Driver>,
) -> (Arc<dyn Core>, AppCleanup, DesktopServices) {
    let dirs = packaged::prepare_dirs(state).unwrap();
    let node = NodeService::new(
        driver,
        "private-local-token".into(),
        node_service::native::development_qualification().unwrap(),
    );
    let services = DesktopServices::with_model_source(
        node,
        packaged::CALLBACK_CAPACITY,
        &dirs.private,
        source.clone(),
    );
    let (core, cleanup) = packaged::assemble(services.clone(), source.clone());
    (core, cleanup, services)
}
/// Quit to its result: the cleanup's first non-pending answer within the quit allowance.
fn quit_result(cleanup: &mut AppCleanup) -> Result<Poll, desktop::lifecycle::CleanupError> {
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    let deadline = Instant::now() + Duration::from_millis(allowance.millis());
    loop {
        match cleanup.poll() {
            Ok(Poll::Pending) => {
                assert!(
                    Instant::now() < deadline,
                    "the packaged core did not clean up"
                );
                std::thread::sleep(poll());
            }
            other => return other,
        }
    }
}

/// REVIEW-laneR1-G2 finding 5: v0.1.4's `DemoApp.close` (app.py:1835-1847 @ f4e366e9) closes the
/// acquisition after an earlier close step failed (each step records `cleanup_error` and the next
/// one runs), so the next launch answers `closed`. Here the window's services cannot prove their
/// cleanup (a run that ended without its terminal): Quit still fails (exit 3, as v0.1.4's
/// `cleanup_incomplete`), and the idle acquisition is closed all the same (`close_when_idle`).
#[test]
fn packaged_quit_closes_the_download_when_the_window_cleanup_fails() {
    let state = state_root();
    let repository = Repository::start(tiny_snapshot(None));
    let observed = Observed::new(permissive());
    let source = model_source(&state, repository.transport(), &observed);
    let (core, mut cleanup) = assembled(&state, &source, Arc::new(EndsWithoutTerminal));
    assert_created(&core);
    let sent = core.call(
        "/chat/api/send",
        json!({"prompt": "hello", "max_new_tokens": 1}),
    );
    assert_eq!(sent.0, 200, "send: {}", sent.1);
    cleanup
        .begin(desktop::lifecycle::quit_allowance().unwrap())
        .unwrap();
    assert_eq!(
        quit_result(&mut cleanup),
        Err(desktop::lifecycle::CleanupError::Poll),
        "the window's cleanup is unproven: Quit reports it"
    );
    assert_eq!(
        acquisition_status(&state),
        status_doc("closed", 0, 0, None),
        "Quit closed the acquisition although the window's cleanup failed"
    );
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// Quit is verified only once the acquisition's `closed` is written (REVIEW-laneR1-G2 finding 11,
/// mutant Me): while the running attempt still waits inside its held response the close cannot
/// have finished, so Quit stays pending; once released, `Verified` means `closed` is on disk.
#[test]
fn packaged_quit_waits_for_the_close_to_be_written() {
    let state = state_root();
    let files = tiny_snapshot(None);
    let repository = Repository::start(files.clone());
    let observed = Observed::new(permissive());
    let (core, mut cleanup) = packaged_core(&state, &repository, &observed);
    assert_created(&core);
    repository.hold("model.safetensors");
    assert_started(&download(&core));
    repository.wait_held();
    cleanup
        .begin(desktop::lifecycle::quit_allowance().unwrap())
        .unwrap();
    // The window's services verify within the first polls (nothing runs); the close then waits
    // for the attempt the held response keeps.
    for _ in 0..6 {
        assert_eq!(
            cleanup.poll(),
            Ok(Poll::Pending),
            "Quit reported done while the close waits for the held attempt"
        );
        assert_ne!(acquisition_status(&state)["state"], "closed");
        std::thread::sleep(poll());
    }
    repository.release();
    assert_eq!(quit_result(&mut cleanup), Ok(Poll::Verified));
    assert_eq!(
        acquisition_status(&state),
        status_doc(
            "closed",
            size_of(&files, "config.json"),
            total(&files),
            None
        ),
        "Verified before the close was written"
    );
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// A close that fails is never reported as a clean Quit (REVIEW-laneR1-G2 finding 11, mutant Mg):
/// v0.1.4 records `cleanup_error = 'acquisition_stop_failed'` (app.py:1846-1847) and its launcher
/// exits `cleanup_incomplete`. The acquisition cannot persist `closed` (its directory is read-only).
#[test]
fn packaged_quit_reports_a_failed_close() {
    use std::os::unix::fs::PermissionsExt;
    let state = state_root();
    let repository = Repository::start(tiny_snapshot(None));
    let observed = Observed::new(permissive());
    let (core, mut cleanup) = packaged_core(&state, &repository, &observed);
    assert_created(&core);
    let sources = state.join(SOURCE);
    assert_eq!(entries(&sources), ["acquisition.lock"]);
    std::fs::set_permissions(&sources, std::fs::Permissions::from_mode(0o500)).unwrap();
    cleanup
        .begin(desktop::lifecycle::quit_allowance().unwrap())
        .unwrap();
    let result = quit_result(&mut cleanup);
    std::fs::set_permissions(&sources, std::fs::Permissions::from_mode(0o700)).unwrap();
    assert_eq!(
        result,
        Err(desktop::lifecycle::CleanupError::Poll),
        "a failed close was reported as a clean Quit"
    );
    assert_eq!(acquisition_status(&state), Value::Null);
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

// ---- REVIEW-lanePH1 finding 1: Quit closes the download when the window's services hang ----

/// A chat run that ignores cancellation and ends only when the test releases it (then failed, so
/// the node service can settle it: `FX-SLICE1-quit-after-failure`); until then the window's
/// services cannot finish their cleanup.
#[derive(Clone, Default)]
struct HeldRun(Arc<(Mutex<bool>, Condvar)>);
impl HeldRun {
    fn release(&self) {
        *self.0.0.lock().unwrap() = true;
        self.0.1.notify_all();
    }
}
impl Driver for HeldRun {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        let mut released = self.0.0.lock().unwrap();
        while !*released {
            released = self.0.1.wait(released).unwrap();
        }
        Err(PublicError::new(503, "worker_native_exited"))
    }
}
/// Releases the held run however the test ends.
struct ReleaseRun(HeldRun);
impl Drop for ReleaseRun {
    fn drop(&mut self) {
        self.0.release();
    }
}
/// Quit's first non-pending answer within the api model-route tests' patience (`None`: still
/// pending then), and whether the acquisition's `closed` was on disk while Quit still answered
/// pending.
fn quit_watching_the_close(
    cleanup: &mut AppCleanup,
    state: &Path,
) -> (Option<Result<Poll, desktop::lifecycle::CleanupError>>, bool) {
    let deadline = Instant::now() + patience();
    let mut closed_while_pending = false;
    loop {
        match cleanup.poll() {
            Ok(Poll::Pending) if Instant::now() < deadline => {
                closed_while_pending |= acquisition_status(state)["state"] == "closed";
                std::thread::sleep(poll());
            }
            Ok(Poll::Pending) => return (None, closed_while_pending),
            other => return (Some(other), closed_while_pending),
        }
    }
}
/// The window with a chat run its services wait for, and its Quit begun as the shell begins it,
/// with the shell's clock bound `elapsed` before now (how long the window has already waited).
fn quit_with_hung_services(
    state: &Path,
    elapsed: Duration,
) -> (Arc<dyn Core>, AppCleanup, ReleaseRun) {
    let repository = Repository::start(tiny_snapshot(None));
    let observed = Observed::new(permissive());
    let source = model_source(state, repository.transport(), &observed);
    let run = HeldRun::default();
    let (core, mut cleanup, _) = assembled_unbound(state, &source, Arc::new(run.clone()));
    assert_created(&core);
    let sent = core.call(
        "/chat/api/send",
        json!({"prompt": "hello", "max_new_tokens": 1}),
    );
    assert_eq!(sent.0, 200, "send: {}", sent.1);
    cleanup
        .bind_clock(Instant::now().checked_sub(elapsed).unwrap())
        .unwrap();
    cleanup
        .begin(desktop::lifecycle::quit_allowance().unwrap())
        .unwrap();
    (core, cleanup, ReleaseRun(run))
}
/// v0.1.4's `DemoApp.close` joins the run for at most its allowance (`run.join(60)`) and closes the
/// acquisition whatever the join found (app.py:1835-1847 @ f4e366e9), so the next launch answers
/// `closed`. Here the window's services stay pending (a chat run that never ends) and the shell's
/// clock says the allowance is nearly spent (less than the closes' own budget left): Quit closes
/// the download now, still within the allowance, and stays pending while the services do; once
/// the allowance ran out it reports the services' deadline (exit 0, as v0.1.4 ignores the join),
/// never `Verified`.
#[test]
fn packaged_quit_closes_the_download_in_time_when_the_window_services_hang() {
    let state = state_root();
    let allowance = Duration::from_millis(desktop::lifecycle::quit_allowance().unwrap().millis());
    let left = millis(B::AcquisitionCloseJoinSlack);
    let (core, mut cleanup, release) = quit_with_hung_services(&state, allowance - left);
    let (result, closed_while_pending) = quit_watching_the_close(&mut cleanup, &state);
    assert!(
        closed_while_pending,
        "Quit did not close the download while the window's services hung ({result:?}): {}",
        acquisition_status(&state)
    );
    assert_eq!(
        result,
        Some(Err(desktop::lifecycle::CleanupError::Deadline)),
        "the services never finished: Quit reports their deadline"
    );
    assert_eq!(acquisition_status(&state), status_doc("closed", 0, 0, None));
    drop(release);
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}
/// The same with the allowance already spent when the window polls its Quit: the closes still
/// run, and the services' deadline is reported only once `closed` is written.
#[test]
fn packaged_quit_reports_the_window_deadline_after_the_closes_ran() {
    let state = state_root();
    let allowance = Duration::from_millis(desktop::lifecycle::quit_allowance().unwrap().millis());
    let (core, mut cleanup, release) =
        quit_with_hung_services(&state, allowance + millis(B::AcquisitionCloseJoinSlack));
    let (result, _) = quit_watching_the_close(&mut cleanup, &state);
    assert_eq!(
        (result, acquisition_status(&state)),
        (
            Some(Err(desktop::lifecycle::CleanupError::Deadline)),
            status_doc("closed", 0, 0, None)
        ),
        "the window's deadline was not reported after the download was closed"
    );
    drop(release);
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}
/// A run that waits for its cancellation (bounded by the stage-cleanup budget) and says whether
/// it was cancelled.
#[derive(Clone, Default)]
struct EndsWhenCancelled(Arc<(Mutex<Option<bool>>, Condvar)>);
impl Driver for EndsWhenCancelled {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        let deadline = Instant::now() + millis(B::StageCleanup);
        while !job.cancelled() && Instant::now() < deadline {
            std::thread::yield_now();
        }
        *self.0.0.lock().unwrap() = Some(job.cancelled());
        self.0.1.notify_all();
        Err(PublicError::new(503, "request_cancelled"))
    }
}
/// REVIEW-lanePH1 finding 1b: when the window's services refuse Quit's begin (here the shell never
/// bound its clock), v0.1.4 still runs every later step of `DemoApp.close` (app.py:1815-1847):
/// the window stops admitting requests (`self.closed.set()`), its running chat is cancelled
/// (`owner_run.stop()`), the download is closed, and the refusal is reported last.
#[test]
fn packaged_quit_fences_and_cancels_when_the_services_refuse_its_begin() {
    let state = state_root();
    let repository = Repository::start(tiny_snapshot(None));
    let observed = Observed::new(permissive());
    let source = model_source(&state, repository.transport(), &observed);
    let run = EndsWhenCancelled::default();
    let (core, mut cleanup, services) = assembled_unbound(&state, &source, Arc::new(run.clone()));
    assert_created(&core);
    let sent = core.call(
        "/chat/api/send",
        json!({"prompt": "hello", "max_new_tokens": 1}),
    );
    assert_eq!(sent.0, 200, "send: {}", sent.1);
    cleanup
        .begin(desktop::lifecycle::quit_allowance().unwrap())
        .unwrap();
    let cancelled = {
        let (ended, changed) = &*run.0;
        let ended = changed
            .wait_timeout_while(ended.lock().unwrap(), millis(B::ModelSelection), |e| {
                e.is_none()
            })
            .unwrap()
            .0;
        *ended
    };
    assert_eq!(
        cancelled,
        Some(true),
        "Quit's begin was refused and the running chat was never cancelled"
    );
    let state_answer = services.call("/chat/api/state", json!({}));
    assert_eq!(
        refusal(&state_answer),
        (503, "application_closed"),
        "Quit's begin was refused and the window still admits requests"
    );
    assert_eq!(
        quit_result(&mut cleanup),
        Err(desktop::lifecycle::CleanupError::Clock)
    );
    assert_eq!(acquisition_status(&state), status_doc("closed", 0, 0, None));
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// v0.1.4's `DemoApp.close` runs `swarm_publish.close` and then `acquisition.close()`, keeping the
/// first failure (app.py:1824-1847 @ f4e366e9): a failed listing close is reported only once the
/// download's close ended (REVIEW-lanePH1 finding 5, mutant Mss). The listing state cannot be read
/// (its file is a directory), so the custody's close fails at once, while the download's close
/// waits for the attempt its held response keeps: Quit stays pending until that close was
/// written, then reports the failure (exit 3).
#[test]
fn packaged_quit_reports_a_failed_swarm_close_only_after_the_download_close_ended() {
    let state = state_root();
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(
            state
                .join("v02-node/private")
                .join(api::swarm::publisher::app::DB_FILE),
        )
        .unwrap();
    let files = tiny_snapshot(None);
    let repository = Repository::start(files.clone());
    let observed = Observed::new(permissive());
    let (core, mut cleanup) = packaged_core(&state, &repository, &observed);
    assert_created(&core);
    repository.hold("model.safetensors");
    assert_started(&download(&core));
    repository.wait_held();
    cleanup
        .begin(desktop::lifecycle::quit_allowance().unwrap())
        .unwrap();
    for _ in 0..6 {
        assert_eq!(
            cleanup.poll(),
            Ok(Poll::Pending),
            "Quit reported the listing's failed close while the download's close still ran"
        );
        assert_ne!(acquisition_status(&state)["state"], "closed");
        std::thread::sleep(poll());
    }
    repository.release();
    assert_eq!(
        quit_result(&mut cleanup),
        Err(desktop::lifecycle::CleanupError::Poll),
        "a failed swarm close was reported as a clean Quit"
    );
    assert_eq!(
        acquisition_status(&state),
        status_doc(
            "closed",
            size_of(&files, "config.json"),
            total(&files),
            None
        )
    );
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

// ---- `_policy_loop`: a running download follows this Mac and the saved consent ----

/// `DemoApp._policy_loop` (app.py:1007-1030 @ f4e366e9): every second, while an attempt is active,
/// this Mac is observed against the saved consent; when it stops satisfying it (on battery, a
/// metered network, serious thermal state) the real worker is cancelled, the guard's code kept as
/// `acquisition_policy_error` and the attempt is no longer active, so the next download starts
/// once this Mac satisfies the consent again (`_acquisition_active` cleared, not
/// `acquisition_already_active`). REVIEW-laneR1-G2 finding 6.
#[test]
fn packaged_policy_loop_cancels_a_download_when_this_mac_changes() {
    for (label, seen, code) in [
        (
            "battery",
            observation("battery", "nominal", "unmetered"),
            "resource_power_policy_violation",
        ),
        (
            "metered",
            observation("external", "nominal", "metered"),
            "resource_network_policy_violation",
        ),
        (
            "thermal_serious",
            observation("external", "serious", "unmetered"),
            "resource_thermal_policy_violation",
        ),
    ] {
        let state = state_root();
        let files = tiny_snapshot(None);
        let repository = Repository::start(files.clone());
        let observed = Observed::new(permissive());
        let source = model_source(&state, repository.transport(), &observed);
        let host = action_root(&state, &source);
        assert_created(&host);
        repository.hold("model.safetensors");
        assert_started(&download(&host));
        repository.wait_held();
        observed.set(seen.clone());
        wait_for(
            &format!("{label}: the running download was never cancelled"),
            || source.policy_error() == Some(code),
        );
        assert_eq!(source.resource_observation(), seen, "{label}");
        repository.release();
        assert_eq!(
            settled(&state),
            status_doc(
                "cancelled",
                size_of(&files, "config.json"),
                total(&files),
                None
            ),
            "{label}"
        );
        observed.set(permissive());
        assert_started(&download(&host));
        assert_eq!(
            settled(&state),
            status_doc("prepared", total(&files), total(&files), None),
            "{label}"
        );
        drop(host);
        std::fs::remove_dir_all(&state).unwrap_or_default();
    }
}

/// `_policy_loop`'s first gate (app.py:1018-1023 @ f4e366e9): an active attempt whose Mac is no
/// longer a running, current member of its swarm is cancelled with `membership_required`, before
/// this Mac is observed (REVIEW-lanePH1 finding 2, mutant Mmb). The packaged routes cannot lose
/// the membership with a download running (`/api/stop` cancels the attempt itself; revocation is
/// not ported), so the attempt is the run model's acquisition this Mac's state root keeps
/// (`ModelSource` clones share it) and the loop judging it is that of a second packaged host over
/// a private root whose owner never created a swarm (its authority is not running).
#[test]
fn packaged_policy_loop_cancels_a_download_when_the_membership_is_lost() {
    let state = state_root();
    let files = tiny_snapshot(None);
    let repository = Repository::start(files.clone());
    let observed = Observed::new(permissive());
    let source = model_source(&state, repository.transport(), &observed);
    let owner = action_root(&state, &source);
    assert_created(&owner);
    let unenrolled = state.join("unenrolled-private");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&unenrolled)
        .unwrap();
    let not_member = DesktopServices::with_model_source(
        NodeService::new(
            Arc::new(MustNotRun),
            "private-local-token".into(),
            node_service::native::development_qualification().unwrap(),
        ),
        NonZeroUsize::new(4).unwrap(),
        &unenrolled,
        source.clone(),
    );
    repository.hold("model.safetensors");
    assert_started(&download(&owner));
    repository.wait_held();
    wait_for(
        "the download of a Mac that is no member was never cancelled",
        || source.policy_error() == Some("membership_required"),
    );
    repository.release();
    assert_eq!(
        settled(&state),
        status_doc(
            "cancelled",
            size_of(&files, "config.json"),
            total(&files),
            None
        )
    );
    drop(not_member);
    drop(owner);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// The policy loop ends with the window: Quit stops it and verifies only once it has ended and
/// was joined (bounded by its one observation), never leaving it running past the process's
/// cleanup proof. Here the loop is inside a held probe when Quit begins.
#[test]
fn packaged_quit_joins_the_policy_loop() {
    let state = state_root();
    let files = tiny_snapshot(None);
    let repository = Repository::start(files.clone());
    let observed = Observed::new(permissive());
    let (core, mut cleanup) = packaged_core(&state, &repository, &observed);
    assert_created(&core);
    repository.hold("model.safetensors");
    assert_started(&download(&core));
    repository.wait_held();
    observed.hold();
    observed.wait_inside();
    cleanup
        .begin(desktop::lifecycle::quit_allowance().unwrap())
        .unwrap();
    repository.release();
    for _ in 0..6 {
        assert_eq!(
            cleanup.poll(),
            Ok(Poll::Pending),
            "Quit verified while its policy loop was still observing this Mac"
        );
        std::thread::sleep(poll());
    }
    observed.release();
    assert_eq!(quit_result(&mut cleanup), Ok(Poll::Verified));
    assert_eq!(
        acquisition_status(&state),
        status_doc(
            "closed",
            size_of(&files, "config.json"),
            total(&files),
            None
        )
    );
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

// ---- `/api/run-model`: the choice re-opens the run model's acquisition ----

/// `DemoApp._choose_run_model` (app.py:855-868 @ f4e366e9): the saved choice is followed by
/// `acquisition_policy_error = None; self._open_acquisition()`, which closes the open acquisition
/// (`closed` persisted) and opens the chosen model's in its own `model-source-<rev12>` directory
/// (app.py:649-672). When that open raises (`AcquisitionError`, here the chosen model's directory
/// is a file: `_private_root` -> `acquisition_state_root_unsafe`), the exception is the action's
/// answer (the handler's 409 `public_failure_code`, app.py:2012-2031), never a success.
#[test]
fn packaged_run_model_choice_reopens_the_acquisition_and_answers_an_open_failure() {
    const OTHER: &str = "model-source-c1899de289a0";
    let state = state_root();
    let repository = Repository::start(tiny_snapshot(None));
    let observed = Observed::new(permissive());
    let source = model_source(&state, repository.transport(), &observed);
    let host = action_root(&state, &source);
    let idle_closed = |model: &api::model_catalog::Model| {
        json!({"state": "closed", "downloaded_bytes": 0, "total_bytes": 0, "error_code": null,
               "model": api::acquisition::model_identity(model)})
    };
    let catalog = api::model_catalog::registered();
    // Qwen3-0.6B: the acquisition moves to its own directory; the starter's is closed.
    let chosen = host.call("/api/run-model", json!({"choice": "qwen3-0.6b"}));
    assert_eq!(chosen.0, 200, "run-model: {}", chosen.1);
    assert_eq!(entries(&state), [SOURCE, OTHER, "v02-node"]);
    assert_eq!(acquisition_status(&state), idle_closed(&catalog[1]));
    assert_eq!(entries(&state.join(OTHER)), ["acquisition.lock"]);
    // The starter again: back to its directory, the Qwen3 acquisition closed in turn.
    let chosen = host.call("/api/run-model", json!({"choice": "a9-route"}));
    assert_eq!(chosen.0, 200, "run-model: {}", chosen.1);
    let other_status: Value =
        serde_json::from_slice(&std::fs::read(state.join(OTHER).join("status.json")).unwrap())
            .unwrap();
    assert_eq!(other_status, idle_closed(&catalog[2]));
    // The chosen model's directory cannot be an acquisition root: the open's refusal answers.
    std::fs::remove_dir_all(state.join(OTHER)).unwrap();
    std::fs::write(state.join(OTHER), b"not a directory").unwrap();
    let refused = host.call("/api/run-model", json!({"choice": "qwen3-0.6b"}));
    assert_eq!(
        refusal(&refused),
        (409, "acquisition_state_root_unsafe"),
        "a failed acquisition open was answered as a success"
    );
    assert_eq!(
        std::fs::read(state.join(OTHER)).unwrap(),
        b"not a directory"
    );
    drop(host);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// The roots `state_root()` makes are removed with their contents when the test ends, also when it
/// fails (REVIEW-lanePH1 finding 8: they used to accumulate in the user's TMPDIR).
#[test]
fn a_test_root_is_removed_when_its_test_ends() {
    let root = state_root();
    let path = root.to_path_buf();
    std::fs::write(path.join("left.json"), b"{}").unwrap();
    drop(root);
    assert!(!path.exists(), "{path:?} was left behind");
    let mut failing = None;
    let unwound = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let root = state_root();
        failing = Some(root.to_path_buf());
        panic!("a failing test");
    }));
    assert!(unwound.is_err());
    let failing = failing.unwrap();
    assert!(
        !failing.exists(),
        "{failing:?} was left behind by a failing test"
    );
}
