// SPDX-License-Identifier: AGPL-3.0-only
//! `api::acquisition` equals the executed oracle (`SelectedModelAcquisition` + `_HTTPRepositoryTransport`,
//! f4e366e9): 123 scenarios from `L10-evidence/r18-probe-acquisition.py`
//! (sha256 0dc794a7e7398702c9562615a33d2d32ac2ef6156aa0f0cd1e15cb7b29a1a258) recorded in
//! `fixtures/oracle_acquisition.json` (sha256 8c9aacbc7e69da0d94a77cbb58b10331c38a112e901fe46f52e5626872143fc0).
//! Per scenario the Rust class is run against a scripted raw-HTTP loopback server and must produce the same
//! status sequence (every persisted status), requests, files left on disk (path, kind, mode, size, sha256),
//! `status.json` bytes, refusal codes and `prepare_local_source` calls.
use api::acquisition::{Acquisition, HttpTransport, Options, PrepareError, Preparer, Status};
use api::model_catalog;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, VecDeque},
    fs,
    io::{Read, Write},
    net::{Shutdown, TcpListener, TcpStream},
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    thread::JoinHandle,
    time::{Duration, SystemTime},
};

const FIXTURE: &str = include_str!("fixtures/oracle_acquisition.json");

fn poll() -> Duration {
    let millis = budgets::BudgetId::OwnerPoll
        .span(budgets::Entries::new(0))
        .unwrap()
        .millis();
    Duration::from_millis(millis)
}
fn secs(n: u64) -> Duration {
    Duration::from_secs(n)
}
fn prng_bytes(path: &str, n: usize) -> Vec<u8> {
    let mut out = Vec::new();
    let mut i = 0;
    while out.len() < n {
        out.extend_from_slice(&Sha256::digest(format!("{path}/{i}").as_bytes()));
        i += 1;
    }
    out.truncate(n);
    out
}
fn unhex(text: &str) -> Vec<u8> {
    (0..text.len() / 2)
        .map(|i| u8::from_str_radix(&text[2 * i..2 * i + 2], 16).unwrap())
        .collect()
}
fn lock<T>(m: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    m.lock().unwrap_or_else(std::sync::PoisonError::into_inner)
}

/// A scripted server: per request target a list of canned responses (the last repeats).
struct Server {
    port: u16,
    requests: Arc<Mutex<Vec<Value>>>,
    stop: Arc<std::sync::atomic::AtomicBool>,
    handle: Option<JoinHandle<()>>,
}
impl Server {
    fn start(routes: &Value, on_cancel: Arc<dyn Fn() + Send + Sync>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        listener.set_nonblocking(true).unwrap();
        let requests = Arc::new(Mutex::new(Vec::new()));
        let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let routes: Arc<BTreeMap<String, Vec<Value>>> = Arc::new(
            routes
                .as_object()
                .unwrap()
                .iter()
                .map(|(k, v)| (k.clone(), v.as_array().unwrap().clone()))
                .collect(),
        );
        let counts: Arc<Mutex<BTreeMap<String, usize>>> = Arc::new(Mutex::new(BTreeMap::new()));
        let (r2, s2) = (requests.clone(), stop.clone());
        let handle = std::thread::spawn(move || {
            let mut workers: Vec<JoinHandle<()>> = Vec::new();
            while !s2.load(std::sync::atomic::Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        stream.set_nonblocking(false).unwrap();
                        let (routes, counts, requests, cancel) = (
                            routes.clone(),
                            counts.clone(),
                            r2.clone(),
                            on_cancel.clone(),
                        );
                        workers.push(std::thread::spawn(move || {
                            serve(stream, port, &routes, &counts, &requests, &*cancel);
                        }));
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(poll());
                    }
                    Err(_) => break,
                }
            }
            for worker in workers {
                worker.join().unwrap();
            }
        });
        Self {
            port,
            requests,
            stop,
            handle: Some(handle),
        }
    }
    fn finish(mut self) -> Vec<Value> {
        self.stop.store(true, std::sync::atomic::Ordering::SeqCst);
        self.handle.take().unwrap().join().unwrap();
        lock(&self.requests).clone()
    }
}
fn serve(
    mut stream: TcpStream,
    port: u16,
    routes: &BTreeMap<String, Vec<Value>>,
    counts: &Mutex<BTreeMap<String, usize>>,
    requests: &Mutex<Vec<Value>>,
    cancel: &(dyn Fn() + Send + Sync),
) {
    let mut head = Vec::new();
    let mut byte = [0u8; 1];
    while !head.ends_with(b"\r\n\r\n") {
        match stream.read(&mut byte) {
            Ok(1) => head.push(byte[0]),
            _ => return,
        }
    }
    let text = String::from_utf8_lossy(&head).into_owned();
    let mut lines = text.split("\r\n");
    let first = lines.next().unwrap();
    let mut parts = first.splitn(3, ' ');
    let (method, target) = (parts.next().unwrap(), parts.next().unwrap().to_owned());
    let mut headers = BTreeMap::new();
    for line in lines {
        if let Some((k, v)) = line.split_once(':') {
            headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_owned());
        }
    }
    let host = headers.remove("host").unwrap_or_default();
    headers.remove("connection");
    let n = {
        let mut counts = lock(counts);
        let n = counts.get(&target).copied().unwrap_or(0);
        counts.insert(target.clone(), n + 1);
        n
    };
    lock(requests).push(json!({
        "method": method, "target": target, "headers": headers,
        "host_ok": host == format!("127.0.0.1:{port}"),
    }));
    let response = match routes.get(&target) {
        Some(sequence) => sequence[n.min(sequence.len() - 1)].clone(),
        None => json!({"status": 404, "headers": [["Content-Length", "0"]], "body_hex": ""}),
    };
    if response["on_serve"] == "cancel" {
        cancel();
    }
    let body = if let Some(gen_spec) = response.get("body_gen") {
        let (rel, off, len) = (
            gen_spec[0].as_str().unwrap(),
            gen_spec[1].as_u64().unwrap() as usize,
            gen_spec[2].as_u64().unwrap() as usize,
        );
        let mut body = prng_bytes(rel, off + len)[off..].to_vec();
        body.extend(unhex(gen_spec[3].as_str().unwrap()));
        body
    } else {
        unhex(response["body_hex"].as_str().unwrap())
    };
    let mut wire = format!("HTTP/1.1 {} X\r\n", response["status"]);
    for pair in response["headers"].as_array().unwrap() {
        wire.push_str(&format!(
            "{}: {}\r\n",
            pair[0].as_str().unwrap(),
            pair[1].as_str().unwrap()
        ));
    }
    let chunked = response["chunked"] == true;
    if chunked {
        wire.push_str("Transfer-Encoding: chunked\r\n");
    }
    wire.push_str("\r\n");
    let mut bytes = wire.into_bytes();
    if chunked {
        for piece in body.chunks(7) {
            bytes.extend(format!("{:x}\r\n", piece.len()).into_bytes());
            bytes.extend_from_slice(piece);
            bytes.extend_from_slice(b"\r\n");
        }
        bytes.extend_from_slice(b"0\r\n\r\n");
    } else {
        let cut = response["truncate"]
            .as_u64()
            .map_or(body.len(), |t| t as usize);
        bytes.extend_from_slice(&body[..cut.min(body.len())]);
    }
    // The client may hang up first (a refused response): that is not a server failure.
    if stream.write_all(&bytes).is_ok() {
        stream.flush().unwrap_or(());
    }
    stream.shutdown(Shutdown::Both).unwrap_or(());
}

struct RecordingPreparer {
    mode: String,
    calls: Mutex<Vec<Value>>,
    root: PathBuf,
    cancel: Arc<dyn Fn() + Send + Sync>,
}
impl Preparer for RecordingPreparer {
    fn prepare(&self, snapshot: &Path, model: &model_catalog::Model) -> Result<(), PrepareError> {
        lock(&self.calls).push(json!({
            "snapshot": snapshot.strip_prefix(&self.root).unwrap().to_string_lossy(),
            "model_id": model.model_id,
        }));
        match self.mode.as_str() {
            "serving_error" => Err(PrepareError::Serving("source_architecture_mismatch".into())),
            "other_error" => Err(PrepareError::Other),
            "cancel" => {
                (self.cancel)();
                Ok(())
            }
            _ => Ok(()),
        }
    }
}

struct World {
    root: PathBuf,
    log: Arc<Mutex<Vec<Value>>>,
    disk: Arc<Mutex<(Option<Value>, VecDeque<u64>)>>,
    clock: Arc<Mutex<Option<i64>>>,
    current: Arc<Mutex<Option<Acquisition>>>,
}

fn status_json(s: &Status) -> Value {
    json!({
        "state": s.state, "downloaded_bytes": s.downloaded_bytes, "total_bytes": s.total_bytes,
        "error_code": s.error_code,
    })
}
fn open(
    world: &World,
    origin: Option<&str>,
    offline: bool,
    preparer: &Arc<RecordingPreparer>,
    model: &'static model_catalog::Model,
) -> Result<Acquisition, api::acquisition::AcqError> {
    let transport: Arc<dyn api::acquisition::Transport> = match origin {
        Some(origin) => Arc::new(HttpTransport::loopback_for_tests(origin)),
        None => Arc::new(HttpTransport::hugging_face(offline)),
    };
    let (disk, clock, log) = (world.disk.clone(), world.clock.clone(), world.log.clone());
    let acquisition = Acquisition::open(
        &world.root,
        model,
        Options {
            transport,
            preparer: preparer.clone(),
            disk_free: Box::new(move |_| {
                let mut disk = lock(&disk);
                if matches!(disk.0, Some(Value::String(ref s)) if s == "raise") {
                    return Err(api::acquisition::DiskProbeFailed);
                }
                if disk.1.len() > 1 {
                    return Ok(disk.1.pop_front().unwrap());
                }
                if let Some(last) = disk.1.front() {
                    return Ok(*last);
                }
                Ok(disk
                    .0
                    .as_ref()
                    .and_then(Value::as_u64)
                    .unwrap_or(1_000_000_000_000))
            }),
            clock_unix_ms: Box::new(move || {
                lock(&clock).unwrap_or_else(|| {
                    SystemTime::now()
                        .duration_since(SystemTime::UNIX_EPOCH)
                        .unwrap()
                        .as_millis() as i64
                })
            }),
            observer: Some(Box::new(move |s| lock(&log).push(status_json(s)))),
        },
    )?;
    *lock(&world.current) = Some(acquisition.clone());
    Ok(acquisition)
}

fn put(base: &Path, rel: &str, spec: &Value, files: &BTreeMap<&str, usize>) {
    let path = base.join(rel);
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path.parent().unwrap())
        .unwrap();
    let len = spec["len"].as_u64().unwrap() as usize;
    let data = match spec.get("of") {
        Some(of) => prng_bytes(of.as_str().unwrap(), len),
        None => unhex(spec["fill"].as_str().unwrap()).repeat(len),
    };
    let _ = files;
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(
            spec.get("mode")
                .and_then(Value::as_u64)
                .map_or(0o600, |m| m as u32),
        )
        .open(&path)
        .unwrap();
    file.write_all(&data).unwrap();
    fs::set_permissions(
        &path,
        fs::Permissions::from_mode(
            spec.get("mode")
                .and_then(Value::as_u64)
                .map_or(0o600, |m| m as u32),
        ),
    )
    .unwrap();
    let age = spec.get("age_s").and_then(Value::as_u64).unwrap_or(0);
    file.set_modified(SystemTime::now() - secs(age)).unwrap();
}
fn fixture_pre(root: &Path, pre: &Value, model: &model_catalog::Model) {
    let files: BTreeMap<&str, usize> = BTreeMap::from([
        ("config.json", 300),
        ("model.safetensors", 70000),
        ("sub/tokenizer.json", 1500),
    ]);
    let snapshots = root
        .join(format!("models--{}", model.model_id.replace('/', "--")))
        .join("snapshots");
    let stage = snapshots.join(format!(".{}.acquiring", model.resolved_commit));
    let snap = snapshots.join(model.resolved_commit);
    let mkdir = |p: &Path| {
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(p)
            .unwrap();
        fs::set_permissions(p, fs::Permissions::from_mode(0o700)).unwrap();
    };
    if pre.get("stage_files").is_some() || pre.get("stage_dirs").is_some() {
        mkdir(&stage);
        fs::set_permissions(
            snapshots.parent().unwrap(),
            fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        for d in pre
            .get("stage_dirs")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            if d != "." {
                mkdir(&stage.join(d.as_str().unwrap()));
            }
        }
        for (rel, spec) in pre
            .get("stage_files")
            .and_then(Value::as_object)
            .into_iter()
            .flatten()
        {
            put(&stage, rel, spec, &files);
        }
    }
    if let Some(selection) = pre.get("snapshot_files").filter(|v| !v.is_null()) {
        mkdir(&snap);
        fs::set_permissions(
            snapshots.parent().unwrap(),
            fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        let names: Vec<String> = if selection == "all" {
            files.keys().map(|k| (*k).to_owned()).collect()
        } else {
            selection
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_owned())
                .collect()
        };
        for rel in &names {
            put(
                &snap,
                rel,
                &json!({"of": rel, "len": files[rel.as_str()]}),
                &files,
            );
        }
        if let Some(rel) = pre.get("snapshot_corrupt").and_then(Value::as_str) {
            fs::remove_file(snap.join(rel)).unwrap();
            put(
                &snap,
                rel,
                &json!({"fill": "ee", "len": files[rel]}),
                &files,
            );
        }
        for (rel, n) in pre
            .get("snapshot_extra")
            .and_then(Value::as_object)
            .into_iter()
            .flatten()
        {
            put(&snap, rel, &json!({"fill": "01", "len": n}), &files);
        }
    }
    // The harness writes exactly the bytes the probe wrote (`pre.status_text`), mode `status_mode`.
    if let Some(text) = pre.get("status_text").and_then(Value::as_str) {
        let path = root.join("status.json");
        fs::write(&path, text).unwrap();
        let mode = pre
            .get("status_mode")
            .and_then(Value::as_u64)
            .map_or(0o600, |m| m as u32);
        fs::set_permissions(&path, fs::Permissions::from_mode(mode)).unwrap();
    }
}
fn tree(root: &Path) -> Value {
    fn walk(dir: &Path, root: &Path, out: &mut Vec<Value>) {
        let mut entries: Vec<_> = fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().path())
            .collect();
        entries.sort();
        for path in entries {
            let meta = fs::symlink_metadata(&path).unwrap();
            let rel = path
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            let is_dir = meta.is_dir();
            out.push(json!({
                "path": rel, "kind": if is_dir { "dir" } else { "file" }, "mode": meta.mode() & 0o7777,
                "size": if is_dir { Value::Null } else { json!(meta.len()) },
                "sha256": if is_dir { Value::Null } else {
                    json!(format!("{:x}", Sha256::digest(fs::read(&path).unwrap())))
                },
            }));
            if is_dir {
                walk(&path, root, out);
            }
        }
    }
    let mut all = Vec::new();
    walk(root, root, &mut all);
    // Python sorts `Path` objects: component by component, not as strings.
    all.sort_by(|a, b| {
        let parts = |v: &Value| {
            v["path"]
                .as_str()
                .unwrap()
                .split('/')
                .map(str::to_owned)
                .collect::<Vec<_>>()
        };
        parts(a).cmp(&parts(b))
    });
    Value::Array(all)
}

fn run_scenario(spec: &Value) -> Value {
    let canonical_tmp = fs::canonicalize(std::env::temp_dir()).unwrap();
    let root = canonical_tmp.join(format!(
        "r18acq-{}-{}",
        std::process::id(),
        spec["name"].as_str().unwrap()
    ));
    if root.exists() {
        fs::remove_dir_all(&root).unwrap();
    }
    fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let model = &model_catalog::registered()[spec["model_index"].as_u64().unwrap() as usize];
    let world = World {
        root: root.clone(),
        log: Arc::new(Mutex::new(Vec::new())),
        disk: Arc::new(Mutex::new((None, VecDeque::new()))),
        clock: Arc::new(Mutex::new(None)),
        current: Arc::new(Mutex::new(None)),
    };
    let current = world.current.clone();
    let cancel: Arc<dyn Fn() + Send + Sync> = Arc::new(move || {
        if let Some(a) = lock(&current).as_ref() {
            a.cancel();
        }
    });
    let server = Server::start(&spec["routes"], cancel.clone());
    let origin = format!("http://127.0.0.1:{}", server.port);
    fixture_pre(&root, &spec["pre"], model);
    let offline = spec["offline"] == true;
    let preparer = Arc::new(RecordingPreparer {
        mode: spec["compile"].as_str().unwrap().to_owned(),
        calls: Mutex::new(Vec::new()),
        root: root.clone(),
        cancel,
    });
    let mut out_ops = Vec::new();
    let mut initial = Value::Null;
    let mut acq = match open(&world, Some(&origin), offline, &preparer, model) {
        Ok(a) => {
            initial = a.status_value();
            Some(a)
        }
        Err(e) => {
            out_ops.push(json!({"op": "new", "raised": e.code()}));
            None
        }
    };
    let patience = secs(60);
    for op in spec["ops"].as_array().unwrap() {
        let kind = op["op"].as_str().unwrap();
        let mut rec = serde_json::Map::new();
        rec.insert("op".into(), kind.into());
        let log0 = lock(&world.log).len();
        let calls0 = lock(&preparer.calls).len();
        let requests0 = lock(&server.requests).len();
        let mut raised: Option<String> = None;
        match kind {
            "run" => {
                {
                    let mut disk = lock(&world.disk);
                    disk.0 = op.get("disk_free").filter(|v| !v.is_null()).cloned();
                    disk.1 = op
                        .get("disk_free_script")
                        .and_then(Value::as_array)
                        .map(|a| a.iter().map(|v| v.as_u64().unwrap()).collect())
                        .unwrap_or_default();
                }
                *lock(&world.clock) = op.get("clock").and_then(Value::as_i64);
                if op["hf_origin"] == true {
                    acq.as_ref().unwrap().close().unwrap();
                    acq = Some(open(&world, None, offline, &preparer, model).unwrap());
                }
                if let Err(e) = acq.as_ref().unwrap().run_blocking(&op["consent"]) {
                    raised = Some(e.code().to_owned());
                }
            }
            "reverify" => {
                let a = acq.as_ref().unwrap();
                if let Err(e) = a.reverify_closed_snapshot(op["storage"].as_u64().unwrap()) {
                    raised = Some(e.code().to_owned());
                }
                assert!(a.wait(patience));
            }
            "new" => {
                acq.as_ref().unwrap().release_lock_for_test();
                match open(&world, Some(&origin), offline, &preparer, model) {
                    Ok(a) => {
                        rec.insert("status".into(), a.status_value());
                        acq = Some(a);
                    }
                    Err(e) => raised = Some(e.code().to_owned()),
                }
            }
            "new_alongside" => {
                if let Err(e) = open(&world, Some(&origin), offline, &preparer, model) {
                    raised = Some(e.code().to_owned());
                }
                *lock(&world.current) = acq.clone();
            }
            "close" => {
                acq.as_ref().unwrap().close().unwrap();
                rec.insert("status".into(), acq.as_ref().unwrap().status_value());
            }
            "status_only" => {
                rec.insert(
                    "status".into(),
                    acq.as_ref().map_or(Value::Null, Acquisition::status_value),
                );
            }
            other => panic!("unknown op {other}"),
        }
        if let Some(code) = raised {
            rec.insert("raised".into(), code.into());
        }
        rec.insert(
            "status_log".into(),
            Value::Array(lock(&world.log)[log0..].to_vec()),
        );
        rec.insert(
            "request_count".into(),
            (lock(&server.requests).len() - requests0).into(),
        );
        rec.insert(
            "prepare_calls".into(),
            Value::Array(lock(&preparer.calls)[calls0..].to_vec()),
        );
        rec.insert(
            "final_status".into(),
            acq.as_ref().map_or(Value::Null, Acquisition::status_value),
        );
        out_ops.push(Value::Object(rec));
    }
    let requests = server.finish();
    let result = json!({
        "ops": out_ops, "requests": requests, "tree": tree(&root),
        "status_file": fs::read_to_string(root.join("status.json")).ok().map(|t| t.chars().take(400).collect::<String>()),
        "initial": initial,
    });
    if let Some(a) = &acq {
        a.close().unwrap_or(());
    }
    drop(acq);
    fs::remove_dir_all(&root).unwrap();
    result
}

/// The oracle record without the fields that only exist for some scenarios.
fn comparable(oracle: &Value, ours: &Value) -> Vec<String> {
    let mut problems = Vec::new();
    let o_ops = oracle["ops"].as_array().unwrap();
    let r_ops = ours["ops"].as_array().unwrap();
    if o_ops.len() != r_ops.len() {
        problems.push(format!("op count {} vs {}", o_ops.len(), r_ops.len()));
        return problems;
    }
    for (i, (o, r)) in o_ops.iter().zip(r_ops).enumerate() {
        for key in [
            "op",
            "raised",
            "status",
            "status_log",
            "request_count",
            "prepare_calls",
            "final_status",
        ] {
            // `raised` only appears when something was raised; `result` is the same as `final_status`.
            if o.get(key) != r.get(key) {
                problems.push(format!(
                    "op {i} {key}: oracle {:?} ours {:?}",
                    o.get(key),
                    r.get(key)
                ));
            }
        }
    }
    if oracle.get("initial").is_some_and(|v| !v.is_null()) && oracle["initial"] != ours["initial"] {
        problems.push(format!(
            "initial: oracle {} ours {}",
            oracle["initial"], ours["initial"]
        ));
    }
    if oracle["requests"] != ours["requests"] {
        problems.push(format!(
            "requests: oracle {} ours {}",
            oracle["requests"], ours["requests"]
        ));
    }
    if oracle["tree"] != ours["tree"] {
        problems.push(format!(
            "tree: oracle {} ours {}",
            oracle["tree"], ours["tree"]
        ));
    }
    if oracle["status_file"] != ours["status_file"] {
        problems.push(format!(
            "status_file: oracle {} ours {}",
            oracle["status_file"], ours["status_file"]
        ));
    }
    problems
}

#[test]
fn the_acquisition_equals_the_executed_oracle_scenario_by_scenario() {
    let document: Value = serde_json::from_str(FIXTURE).unwrap();
    assert_eq!(
        document["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let scenarios = document["scenarios"].as_array().unwrap();
    assert_eq!(scenarios.len(), 123);
    let mut failures = Vec::new();
    for scenario in scenarios {
        let ours = run_scenario(&scenario["spec"]);
        let problems = comparable(&scenario["oracle"], &ours);
        if !problems.is_empty() {
            let text: String = problems.join(" | ").chars().take(900).collect();
            failures.push(format!("{}: {text}", scenario["spec"]["name"]));
        }
    }
    assert!(
        failures.is_empty(),
        "{} scenarios differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
