// SPDX-License-Identifier: AGPL-3.0-only
//! S1 prepare on the packaged executable: `POST /api/model/download` and `/api/model/cancel`
//! (app.py:1551-1577 @ f4e366e9) run `api::acquisition` through the packaged host into v0.1.4's
//! model-source layout (`_open_acquisition`, app.py:649-672: `<state>/model-source-<rev12>/`
//! `models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/<rev>/`; acquisition.py:1096-1184 `_execute`:
//! download, verify, `prepare_local_source`), and the packaged core leaves `model_not_installed`
//! once that snapshot is prepared, without a relaunch.
//!
//! No network and no real model. A loopback repository serves a tiny snapshot: the config and
//! safetensors of the accepted `qwen2_monolithic` layout of the executed manifest oracle
//! (`api/tests/fixtures/oracle_manifest.json`, as `api::model_routes_e2e` serves it) and a
//! tokenizer file. What lands on disk is compared with the executed acquisition oracle
//! (`api/tests/fixtures/oracle_acquisition.json`, scenario by scenario), the route answers with the
//! executed v0.1.4 process (`api/tests/fixtures/oracle_model_routes.json`).
#![cfg(feature = "desktop-shell")]
use api::{
    acquisition::{HttpTransport, Transport},
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
const NOT_INSTALLED: (u16, &str) = (503, "model_not_installed");
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
fn state_root() -> PathBuf {
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "packaged-model-prepare-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    root
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
/// The packaged action root (`PersistentBackend<PackagedHost, …>`) with its model source over
/// `transport`, the node service's driver never started.
fn action_root(state: &Path, transport: Arc<dyn Transport>) -> DesktopServices {
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
        ModelSource::new(state.to_owned(), transport),
    )
}
/// The packaged executable's core over `state` (`packaged::core`, the repository given).
fn packaged_core(state: &Path, repository: &Repository) -> (Arc<dyn Core>, AppCleanup) {
    let (core, mut cleanup) = packaged::core_with(state, repository.transport()).unwrap();
    cleanup.bind_clock(Instant::now()).unwrap();
    (core, cleanup)
}
/// Quit: the window's cleanup reaches `Verified` within the quit allowance.
fn quit(core: Arc<dyn Core>, mut cleanup: AppCleanup) {
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    cleanup.begin(allowance).unwrap();
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
    let host = action_root(&state, repository.transport());
    assert_created(&host);
    // `self.acquisition.start(grant)`: one background attempt, its status answered.
    assert_started(&download(&host));
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
    // The packaged core's lookup finds exactly that snapshot.
    let located = packaged::locate_model(&state).expect("the prepared snapshot is located");
    assert_eq!(located.checkpoint, snapshot.join("model.safetensors"));
    assert_eq!(located.tokenizer, snapshot.join("tokenizer.json"));
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
fn packaged_model_prepare_core_leaves_model_not_installed() {
    let state = state_root();
    let files = tiny_snapshot(None);
    // A snapshot of another revision is not the pinned model (near miss).
    plant(&state, &"0".repeat(40), &files);
    let repository = Repository::start(files.clone());
    let (core, cleanup) = packaged_core(&state, &repository);
    // No model: the window opens and the native service answers `model_not_installed`.
    assert_eq!(refusal(&chat_state(&core)), NOT_INSTALLED);
    // The action root is the packaged one: the owner creates the swarm and asks for the model.
    assert_created(&core);
    assert_eq!(refusal(&chat_state(&core)), NOT_INSTALLED);
    assert_started(&download(&core));
    assert_eq!(
        settled(&state),
        status_doc("prepared", total(&files), total(&files), None)
    );
    // Prepared: without a relaunch, the native service is composed over the located snapshot
    // and answers.
    let composed = chat_state(&core);
    assert_eq!(composed.0, 200, "chat state: {}", composed.1);
    assert_eq!(composed.1["available"], true);
    assert_eq!(composed.1["busy"], false);
    assert_eq!(composed.1["model_id"], "Qwen2.5-0.5B-Instruct");
    // The native driver was composed on the private root (its member identity and the local
    // membership `configured_driver` keeps there).
    let private = state.join("v02-node/private");
    assert!(private.join("member").is_dir(), "{:?}", entries(&private));
    quit(core, cleanup);
    // Relaunch after prepared: the window composes the located model at open, and the
    // acquisition recovers v0.1.4's `failed prepared_source_revalidation_required`
    // (oracle `happy_then_reverify`, the `new` step).
    let (core, cleanup) = packaged_core(&state, &repository);
    assert_eq!(chat_state(&core).0, 200);
    let oracle = scenario("happy_then_reverify");
    let recovered = &oracle["ops"][1]["final_status"];
    let answered = cancel(&core);
    assert_eq!(answered.0, 200, "{}", answered.1);
    assert_eq!(outcome(&answered.1), outcome(recovered));
    assert_eq!(
        answered.1,
        status_doc(
            "failed",
            total(&files),
            total(&files),
            Some("prepared_source_revalidation_required")
        )
    );
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
    let (core, cleanup) = packaged_core(&state, &repository);
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
    assert!(packaged::locate_model(&state).is_none());
    assert_eq!(refusal(&chat_state(&core)), NOT_INSTALLED);
    // Download after the failure, the source repaired: the attempt resumes from what verified
    // and prepares (oracle `run_after_failure_retries`); then the core answers.
    repository.tamper(None);
    assert_started(&download(&core));
    assert_eq!(
        settled(&state),
        status_doc("prepared", total(&files), total(&files), None)
    );
    assert_eq!(chat_state(&core).0, 200);
    quit(core, cleanup);
    std::fs::remove_dir_all(&state).unwrap_or_default();

    // A snapshot every byte check accepts but the native manifest compile refuses: it is on disk
    // where the packaged lookup lands, yet it was never prepared, so the core keeps refusing.
    let state = state_root();
    let files = tiny_snapshot(Some(25));
    let repository = Repository::start(files.clone());
    let (core, cleanup) = packaged_core(&state, &repository);
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
    assert!(packaged::locate_model(&state).is_some());
    assert_eq!(refusal(&chat_state(&core)), NOT_INSTALLED);
    quit(core, cleanup);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

#[test]
fn packaged_model_prepare_cancel_leaves_no_partial() {
    let state = state_root();
    let files = tiny_snapshot(None);
    let repository = Repository::start(files.clone());
    let (core, cleanup) = packaged_core(&state, &repository);
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
    // the snapshot the packaged lookup reads, and the core keeps refusing.
    assert_eq!(
        tree(&state.join(SOURCE)),
        oracle_tree("cancel_before_file", &files)
    );
    assert!(!snapshot_dir(&state, REVISION).exists());
    assert!(packaged::locate_model(&state).is_none());
    assert_eq!(refusal(&chat_state(&core)), NOT_INSTALLED);
    // Cancel with nothing running answers the cancelled status and changes nothing.
    assert_eq!(cancel(&core), (200, cancelled.clone()));
    // Download after cancel resumes the stage to prepared; then the core answers.
    assert_started(&download(&core));
    assert_eq!(
        settled(&state),
        status_doc("prepared", total(&files), total(&files), None)
    );
    assert_eq!(tree(&state.join(SOURCE)), oracle_tree("happy_path", &files));
    assert_eq!(chat_state(&core).0, 200);
    quit(core, cleanup);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// The executed v0.1.4 process's `/api/model/*` sequence (`oracle_model_routes.json`: shape,
/// membership and grant refusals, cancel, the offline refusal of the public hub, the retry after
/// a failure, stop/start) replayed on the packaged action root, offline as the oracle ran. The
/// `status_view` blocks are compared on their `acquisition` (the persisted status); the packaged
/// `/api/status` does not project `acquisition_policy_error`, `resource_observation`, `running`
/// or `membership_state` (declared, counted).
#[test]
fn packaged_model_prepare_routes_equal_the_executed_oracle() {
    let oracle: Value = serde_json::from_str(ROUTES_ORACLE).unwrap();
    let state = state_root();
    let host = action_root(&state, Arc::new(HttpTransport::hugging_face(true)));
    const GB: u64 = 1_000_000_000;
    let grant = json!({"download_acknowledged": true, "max_download_bytes": 2 * GB, "storage_limit_bytes": 4 * GB});
    let policy = json!({"peer_visibility": true, "power": "battery_allowed", "storage_bytes": 4 * GB,
        "network": "metered_allowed", "background": false, "diagnostics": false, "thermal": "stop_on_serious"});
    let (mut exact, mut declared) = (0, 0);
    for step in oracle["steps"].as_array().unwrap() {
        let label = step["label"].as_str().unwrap();
        if let Some(view) = step.get("status_view") {
            let status = if label.starts_with("after_download") {
                settled(&state)
            } else {
                if label == "settled" {
                    std::thread::sleep(poll() * 12);
                }
                acquisition_status(&state)
            };
            // Before the first attempt nothing was persisted: the in-memory status is idle.
            let status = if status.is_null() {
                status_doc("idle", 0, 0, None)
            } else {
                status
            };
            assert_eq!(status, view["acquisition"], "status view at {label}");
            exact += 1;
            declared += view.as_object().unwrap().len() - 1;
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
        let host = action_root(&state, repository.transport());
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
