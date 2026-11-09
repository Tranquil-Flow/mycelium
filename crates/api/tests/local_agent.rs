// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's own Mac as a member of its run, against EXECUTED v0.1.4 (FX-L10-LOCAL-AGENT: probe
//! `r20-probe-local-agent.py` runs the REAL `DemoTargetAgent(local=True)` and the REAL
//! `installation_descriptor.installation_descriptor` on a stub app with a fixed Ed25519 key and a scripted clock;
//! two identical executions). Compared: every descriptor card byte for byte (signature included), and per tick the
//! agent status and the exact reports the owner's inbox receives. The slice scenario touches the file system, so
//! its cards are compared without the signature (the signed statement names the scratch directory).
use api::{
    installation_descriptor::{self, Failure, Member, Paths, Source},
    local_agent::{AgentHost, LocalAgent},
    slice_fetch::{self, SliceError, SliceTransport, SnapshotRangeTransport},
};
use protocol::identity::Identity;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    os::unix::fs::{DirBuilderExt, PermissionsExt},
    path::{Path, PathBuf},
};

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_local_agent.json")).expect("fixture parses")
}

fn hex_bytes(text: &str) -> [u8; 32] {
    let mut out = [0u8; 32];
    for (i, byte) in out.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&text[2 * i..2 * i + 2], 16).expect("hex");
    }
    out
}

struct Rig {
    signer: Identity,
    member: Member,
    swarm: String,
    python: String,
    sidecar: String,
}
impl Rig {
    fn new(fx: &Value) -> Self {
        let signer = Identity::from_secret(
            fx["endpoint_id"].as_str().expect("endpoint"),
            &hex_bytes(fx["secret_hex"].as_str().expect("secret")),
        )
        .expect("identity");
        let m = &fx["member"];
        let member = Member {
            node_id: m["node_id"].as_str().expect("node").to_owned(),
            endpoint_id: m["endpoint_id"].as_str().expect("endpoint").to_owned(),
            verification_key_digest: m["verification_key_digest"]
                .as_str()
                .expect("digest")
                .to_owned(),
            generation: m["generation"].as_u64().expect("generation"),
            incarnation: m["incarnation"].as_str().expect("incarnation").to_owned(),
        };
        Self {
            signer,
            member,
            swarm: fx["swarm_id"].as_str().expect("swarm").to_owned(),
            python: fx["python"].as_str().expect("python").to_owned(),
            sidecar: fx["sidecar"].as_str().expect("sidecar").to_owned(),
        }
    }
    fn paths(&self, state_root: &str) -> Paths {
        Paths {
            state_root: state_root.to_owned(),
            endpoint_secret_file: format!("{state_root}/identity/node.key"),
            python_executable: self.python.clone(),
            sidecar_binary: self.sidecar.clone(),
        }
    }
}

#[test]
fn published_bootstrap_cannot_silently_remain_reported_without_a_native_handler() {
    let fx = fixture();
    let rig = Rig::new(&fx);
    let mut host = Scripted {
        rig: &rig,
        root: PathBuf::from("/v02/agent-root"),
        sink: Vec::new(),
        t: 1000.0,
        announcement: Some(
            json!({"phase":"observe", "run_id":"a13-test-qwen-20261029T100000Z", "deployment_id":"dep-1",
            "model":{"model_id":"Qwen/Qwen2.5-0.5B-Instruct", "resolved_commit":"7ae557604adf67be50417f59c2c2f167def9a775"}, "bootstrap_offer_id":"stage-real"}),
        ),
        active: true,
        check: None,
        source_error: false,
        sidecar: true,
        member: true,
        prepared: (
            "Qwen/Qwen2.5-0.5B-Instruct".into(),
            "7ae557604adf67be50417f59c2c2f167def9a775".into(),
        ),
    };
    let status = LocalAgent::new().tick(&mut host);
    assert_eq!(
        status["state"], "error",
        "a published offer requires a real bootstrap handler"
    );
    assert_eq!(status["detail"], "native_bootstrap_unavailable");
    assert_eq!(host.sink.last().unwrap()["state"], "error");
}

fn failure_pair(f: &Failure) -> Value {
    json!({"class": f.class, "text": f.code})
}

/// Proves: B-RUN-08 (the owner's contributor describes itself with a signed installation descriptor: consent,
/// the ids, the slice source and the binding are checked in the oracle's order, every card equals the executed one).
#[test]
fn installation_descriptors_equal_the_executed_oracle() {
    let fx = fixture();
    let rig = Rig::new(&fx);
    let cases = fx["descriptors"].as_array().expect("cases");
    assert_eq!(cases.len(), 6, "every recorded descriptor case is replayed");
    for entry in cases {
        let case = &entry["case"];
        let name = case["name"].as_str().expect("name");
        let source = match case["slice"].as_object() {
            None => Source::Whole {
                source_root: "/v02/fixed/source".into(),
                manifest_digest: format!("sha256:{}", "ab".repeat(32)),
            },
            Some(slice) => Source::Slice {
                source_root: slice["source_root"].as_str().expect("root").into(),
                manifest_digest: slice["manifest_digest"].as_str().expect("digest").into(),
            },
        };
        let got = installation_descriptor::installation_descriptor(
            &rig.signer,
            &rig.member,
            &rig.swarm,
            case["run"].as_str().expect("run"),
            case["dep"].as_str().expect("dep"),
            case["ack"].as_bool().unwrap_or(true),
            &source,
            &rig.paths("/v02/fixed/root/local-contributor"),
            1000.0,
        );
        match (&entry["result"]["ok"], &entry["result"]["error"]) {
            (ok, _) if !ok.is_null() => {
                assert_eq!(got.as_ref().expect(name), ok, "{name}: the card");
            }
            (_, error) => {
                let failure = got.expect_err(name);
                assert_eq!(failure_pair(&failure), *error, "{name}: the refusal");
            }
        }
    }
}

struct Scripted<'a> {
    rig: &'a Rig,
    root: PathBuf,
    sink: Vec<Value>,
    t: f64,
    announcement: Option<Value>,
    active: bool,
    check: Option<String>,
    source_error: bool,
    sidecar: bool,
    member: bool,
    prepared: (String, String),
}
impl Scripted<'_> {
    fn prepare_slice_root(&self, run_id: &str) -> Result<String, Failure> {
        let base = self.root.join("model-source").join("slices");
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&base)
            .expect("slices base");
        for entry in fs::read_dir(&base).expect("read") {
            let entry = entry.expect("entry");
            let is_dir = entry.file_type().is_ok_and(|t| t.is_dir());
            if entry.file_name() != run_id && is_dir {
                fs::remove_dir_all(entry.path()).unwrap_or_default();
            }
        }
        let root = base.join(run_id);
        if !root.exists() {
            fs::DirBuilder::new()
                .mode(0o700)
                .create(&root)
                .expect("slice root");
        }
        Ok(root.to_string_lossy().into_owned())
    }
}
impl AgentHost for Scripted<'_> {
    fn announcement(&mut self) -> Result<Option<Value>, api::local_agent::ExchangeUnavailable> {
        if self.source_error {
            return Err(api::local_agent::ExchangeUnavailable);
        }
        Ok(self.announcement.clone())
    }
    fn member(&mut self) -> Option<String> {
        self.member.then(|| self.rig.member.node_id.clone())
    }
    fn run_active(&mut self) -> bool {
        self.active
    }
    fn resource_check(&mut self) -> Option<String> {
        self.check.clone()
    }
    fn prepared_model(&mut self) -> Result<(String, String), Failure> {
        Ok(self.prepared.clone())
    }
    fn describe(
        &mut self,
        run_id: &str,
        deployment_id: &str,
        slice_manifest_digest: Option<&str>,
    ) -> Result<Value, Failure> {
        if !self.sidecar {
            return Err(Failure::new(
                "ValueError",
                "native_sidecar_installation_invalid",
            ));
        }
        let root = self.root.to_string_lossy().into_owned();
        let source = match slice_manifest_digest {
            Some(digest) => Source::Slice {
                source_root: self.prepare_slice_root(run_id)?,
                manifest_digest: digest.to_owned(),
            },
            None => Source::Whole {
                source_root: format!("{root}/model-source/snapshot"),
                manifest_digest: format!("sha256:{}", "ab".repeat(32)),
            },
        };
        installation_descriptor::installation_descriptor(
            &self.rig.signer,
            &self.rig.member,
            &self.rig.swarm,
            run_id,
            deployment_id,
            true,
            &source,
            &self.rig.paths(&format!("{root}/local-contributor")),
            self.t,
        )
    }
    fn sink(&mut self, report: &Value) -> Result<(), api::local_agent::ExchangeUnavailable> {
        self.sink.push(report.clone());
        Ok(())
    }
    fn clock(&mut self) -> f64 {
        self.t
    }
}

/// The recorded text with the scratch tokens replaced; `signature` fields dropped when the statement names a
/// scratch directory chosen by the run.
fn expand(value: &Value, root: &Path, python: &str, drop_signature: bool) -> Value {
    let text = value
        .to_string()
        .replace("<ROOT>", &root.to_string_lossy())
        .replace("<PYTHON>", python);
    let mut out: Value = serde_json::from_str(&text).expect("json");
    if drop_signature {
        strip_signatures(&mut out);
    }
    out
}
fn strip_signatures(value: &mut Value) {
    match value {
        Value::Object(map) => {
            map.remove("signature");
            map.remove("signed_statement_digest");
            map.values_mut().for_each(strip_signatures);
        }
        Value::Array(items) => items.iter_mut().for_each(strip_signatures),
        _ => {}
    }
}

fn run(rig: &Rig, scenario: &Value, python: &str) {
    let name = scenario["name"].as_str().expect("name");
    let real_fs = scenario["real_fs"].as_bool().expect("real_fs");
    let root = if real_fs {
        let dir = std::env::temp_dir().join(format!("l10r20-agent-{}-{name}", std::process::id()));
        fs::remove_dir_all(&dir).unwrap_or_default();
        for rel in scenario["spec"]["pre_dirs"]
            .as_array()
            .into_iter()
            .flatten()
        {
            fs::create_dir_all(dir.join(rel.as_str().expect("rel"))).expect("pre dir");
        }
        fs::canonicalize(&dir).unwrap_or(dir)
    } else {
        PathBuf::from("/v02/agent-root")
    };
    let mut host = Scripted {
        rig,
        root: root.clone(),
        sink: Vec::new(),
        t: 0.0,
        announcement: None,
        active: true,
        check: scenario["spec"]["check"].as_str().map(str::to_owned),
        source_error: false,
        sidecar: true,
        member: scenario["spec"]["member"].as_bool().unwrap_or(true),
        prepared: (
            "Qwen/Qwen2.5-0.5B-Instruct".into(),
            "7ae557604adf67be50417f59c2c2f167def9a775".into(),
        ),
    };
    let mut agent = LocalAgent::new();
    let mut ticks = scenario["log"].as_array().expect("log").iter();
    for op in scenario["spec"]["ops"].as_array().expect("ops") {
        match op["op"].as_str().expect("op") {
            "time" => host.t = op["t"].as_f64().expect("t"),
            "announce" => host.announcement = (!op["value"].is_null()).then(|| op["value"].clone()),
            "active" => host.active = op["value"].as_bool().expect("bool"),
            "check" => host.check = op["value"].as_str().map(str::to_owned),
            "source_error" => host.source_error = op["value"].as_bool().expect("bool"),
            "sidecar" => host.sidecar = op["value"].as_bool().expect("bool"),
            "tick" => {
                let want = ticks.next().expect("a recorded tick");
                let before = host.sink.len();
                let status = agent.tick(&mut host);
                let at = format!("{name} @ {}", want["t"]);
                assert_eq!(
                    status,
                    expand(&want["status"], &root, python, false),
                    "{at}: status"
                );
                let reports = Value::Array(host.sink[before..].to_vec());
                let want_reports = expand(&want["reports"], &root, python, real_fs);
                let mut got_reports = reports;
                if real_fs {
                    strip_signatures(&mut got_reports);
                }
                assert_eq!(got_reports, want_reports, "{at}: reports");
                if real_fs {
                    let base = root.join("model-source").join("slices");
                    let mut dirs: Vec<String> = fs::read_dir(&base)
                        .map(|d| {
                            d.filter_map(Result::ok)
                                .map(|e| e.file_name().to_string_lossy().into_owned())
                                .collect()
                        })
                        .unwrap_or_default();
                    dirs.sort();
                    assert_eq!(json!(dirs), want["slice_dirs"], "{at}: slice dirs");
                    for dir in &dirs {
                        let mode = fs::metadata(base.join(dir))
                            .expect("meta")
                            .permissions()
                            .mode()
                            & 0o777;
                        assert_eq!(
                            json!(format!("0o{mode:o}")),
                            want["slice_mode_bits"][dir],
                            "{at}: mode of {dir}"
                        );
                    }
                }
            }
            other => panic!("unknown op {other}"),
        }
    }
    assert!(
        ticks.next().is_none(),
        "{name}: every recorded tick is replayed"
    );
    if real_fs {
        fs::remove_dir_all(&root).unwrap_or_default();
    }
}

/// Proves: B-RUN-08 (the owner's Mac joins its own run as a contributor: it reports idle/stopped/reported with a
/// descriptor once per run, re-signs before half its life is gone, reports a refusal or a failure by name, and
/// consents only while the owner's Start is active), B-RUN-07 (a refusing joined Mac surfaces its code).
#[test]
fn the_local_agent_equals_the_executed_oracle_tick_by_tick() {
    let fx = fixture();
    let rig = Rig::new(&fx);
    let scenarios = fx["scenarios"].as_array().expect("scenarios");
    assert_eq!(scenarios.len(), 12, "every recorded scenario is replayed");
    let mut ticks = 0;
    for scenario in scenarios {
        ticks += scenario["log"].as_array().expect("log").len();
        run(&rig, scenario, &rig.python);
    }
    assert_eq!(ticks, 29, "every recorded tick is replayed");
}

// ---- the work phase (FX-B18-MEMBER-WORK: probe `probe_member_work.py` runs the REAL v0.1.4
// `DemoTargetAgent(local=True)` work phase with the REAL `fetch_node_slice` + `SnapshotRangeTransport` on a
// stub actor/route/consent app and a scripted clock; two executions byte-identical). Compared per tick: the
// agent status, the reports the owner's inbox receives, the routes the agent invoked and, once no fetch is
// running, every published slice file with its digest. The descriptor is a fixed marker here (its bytes are
// FX-L10-LOCAL-AGENT above).

fn work_fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_member_work.json")).expect("fixture parses")
}

const SOURCES: [(&str, usize); 2] = [
    ("model-00001.safetensors", 5000),
    ("model-00002.safetensors", 3000),
];
/// The probe's deterministic snapshot bytes: an iterated sha256 chain seeded by the file name.
fn source_bytes(name: &str, size: usize) -> Vec<u8> {
    let mut seed: Vec<u8> = Sha256::digest(name.as_bytes()).to_vec();
    let mut out = Vec::new();
    while out.len() < size {
        seed = Sha256::digest(&seed).to_vec();
        out.extend_from_slice(&seed);
    }
    out.truncate(size);
    out
}
fn sha_hex(path: &Path) -> String {
    format!("sha256:{:x}", Sha256::digest(fs::read(path).expect("read")))
}

struct WorkHost {
    root: PathBuf,
    t: f64,
    announcement: Option<Value>,
    active: bool,
    check: Option<String>,
    native: Value,
    cleanup: Option<Value>,
    offer: Option<Value>,
    caps: Value,
    transport: bool,
    sink: Vec<Value>,
    actions: Vec<Value>,
}
impl WorkHost {
    fn new(root: PathBuf) -> Self {
        let snapshot = root.join("snapshot");
        fs::create_dir_all(&snapshot).expect("snapshot");
        for (name, size) in SOURCES {
            fs::write(snapshot.join(name), source_bytes(name, size)).expect("source");
        }
        Self {
            root,
            t: 1000.0,
            announcement: None,
            active: true,
            check: None,
            native: json!({}),
            cleanup: None,
            offer: None,
            caps: json!({}),
            transport: true,
            sink: Vec::new(),
            actions: Vec::new(),
        }
    }
    fn slices(&self) -> PathBuf {
        self.root.join("model-source").join("slices")
    }
    fn action(&mut self, path: &str, body: Value) {
        self.actions.push(json!([path, body]));
    }
}
impl AgentHost for WorkHost {
    fn announcement(&mut self) -> Result<Option<Value>, api::local_agent::ExchangeUnavailable> {
        Ok(self.announcement.clone())
    }
    fn member(&mut self) -> Option<String> {
        Some("node-local".into())
    }
    fn run_active(&mut self) -> bool {
        self.active
    }
    fn resource_check(&mut self) -> Option<String> {
        self.check.clone()
    }
    fn prepared_model(&mut self) -> Result<(String, String), Failure> {
        let model = &self.announcement.as_ref().expect("announced")["model"];
        Ok((
            model["model_id"].as_str().expect("id").into(),
            model["resolved_commit"].as_str().expect("commit").into(),
        ))
    }
    /// The probe's descriptor marker; slice mode creates the per-run root as `_prepare_slice_root` does.
    fn describe(&mut self, run: &str, _: &str, slice: Option<&str>) -> Result<Value, Failure> {
        if slice.is_some() {
            fs::create_dir_all(self.slices().join(run)).expect("slice root");
        }
        Ok(json!({"card": run, "slice": slice.is_some(),
            "statement": {"issued_at": self.t, "expires_at": self.t + 600.0}}))
    }
    fn pending_cleanup(&mut self) -> Result<Option<Value>, Failure> {
        Ok(self.cleanup.clone())
    }
    fn cleanup(&mut self, request: &Value) -> Result<(), Failure> {
        self.action("/api/control/stop", json!({}));
        self.native = json!({"state": "stopped"});
        self.action(
            "/api/staging/cleanup/accept",
            json!({"offer_id": request["offer_id"], "archive_digest": request["archive_digest"],
                "cleanup_acknowledged": true}),
        );
        self.cleanup = None;
        Ok(())
    }
    fn stop_native(&mut self) -> Result<(), Failure> {
        self.action("/api/control/stop", json!({}));
        self.native = json!({"state": "stopped"});
        Ok(())
    }
    fn native_control(&mut self) -> Result<Value, Failure> {
        Ok(self.native.clone())
    }
    fn pending_offer(&mut self) -> Result<Option<Value>, Failure> {
        Ok(self.offer.clone())
    }
    /// The oracle's `/api/staging/accept` + `/api/control/start` for the bootstrap offer, as one
    /// recorded route (no recorded scenario announces a bootstrap offer).
    fn bootstrap(&mut self, _run: &str, _deployment: &str, offer: &str) -> Result<(), Failure> {
        self.action("bootstrap", json!({"offer_id": offer}));
        self.native = json!({"state": "running"});
        Ok(())
    }
    fn accept_work(&mut self, limits: &Value) -> Result<(), Failure> {
        self.action(
            "/api/work/preview",
            json!({"limits": limits, "stage_acknowledged": true}),
        );
        self.action(
            "/api/work/accept",
            json!({"limits": limits, "preview": {"preview": "work-preview"}, "work_acknowledged": true}),
        );
        self.native = json!({"state": "running", "work_admitted": true});
        self.offer = None;
        Ok(())
    }
    fn member_caps(&mut self) -> Result<Value, Failure> {
        Ok(self.caps.clone())
    }
    fn storage_cap(&mut self) -> Result<u64, Failure> {
        Ok(10_000_000)
    }
    fn slice_root(&mut self, run: &str) -> Result<PathBuf, Failure> {
        Ok(self.slices().join(run))
    }
    fn slice_transport(&mut self, a: &Value) -> Option<Box<dyn SliceTransport>> {
        self.transport.then(|| {
            Box::new(SnapshotRangeTransport::new(
                self.root.join("snapshot"),
                a["model"]["model_id"].as_str().unwrap_or_default(),
                a["model"]["resolved_commit"].as_str().unwrap_or_default(),
                None,
            )) as Box<dyn SliceTransport>
        })
    }
    fn sink(&mut self, report: &Value) -> Result<(), api::local_agent::ExchangeUnavailable> {
        self.sink.push(report.clone());
        Ok(())
    }
    fn clock(&mut self) -> f64 {
        self.t
    }
}

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("b18-agent-{}-{name}", std::process::id()));
    fs::remove_dir_all(&dir).unwrap_or_default();
    fs::create_dir_all(&dir).expect("scratch");
    fs::canonicalize(&dir).expect("canonical")
}

fn published(slices: &Path) -> Value {
    let mut files = serde_json::Map::new();
    fn walk(dir: &Path, base: &Path, files: &mut serde_json::Map<String, Value>) {
        for entry in fs::read_dir(dir).into_iter().flatten().flatten() {
            let path = entry.path();
            if path.is_dir() {
                walk(&path, base, files);
            } else {
                let rel = path
                    .strip_prefix(base)
                    .expect("under")
                    .to_string_lossy()
                    .into_owned();
                files.insert(rel, json!(sha_hex(&path)));
            }
        }
    }
    walk(slices, slices, &mut files);
    Value::Object(files)
}

fn run_work(scenario: &Value) {
    let name = scenario["name"].as_str().expect("name");
    let root = scratch(name);
    let mut host = WorkHost::new(root.clone());
    let mut agent = LocalAgent::new();
    let mut ticks = scenario["log"].as_array().expect("log").iter();
    for op in scenario["ops"].as_array().expect("ops") {
        let value = || op["value"].clone();
        match op["op"].as_str().expect("op") {
            "announce" => host.announcement = Some(value()),
            "time" => host.t = op["t"].as_f64().expect("t"),
            "native" => host.native = value(),
            "cleanup" => host.cleanup = Some(value()),
            "offer" => host.offer = Some(value()),
            "caps" => host.caps = value(),
            "refuse" => {
                (host.active, host.check) = match op["value"].as_str() {
                    None => (true, None),
                    Some("owner_run_not_active") => (false, None),
                    Some(other) => (true, Some(other.to_owned())),
                };
            }
            "join" => agent.join_slice(),
            "tick" => {
                let want = ticks.next().expect("a recorded tick");
                let at = format!("{name} @ {}", want["t"]);
                let (reports, actions) = (host.sink.len(), host.actions.len());
                let status = agent.tick(&mut host);
                assert_eq!(status, want["status"], "{at}: status");
                assert_eq!(
                    json!(host.sink[reports..]),
                    want["reports"],
                    "{at}: reports"
                );
                assert_eq!(
                    json!(host.actions[actions..]),
                    want["actions"],
                    "{at}: routes"
                );
                if status["state"] != "downloading_slice" {
                    assert_eq!(
                        published(&host.slices()),
                        want["slice_files"],
                        "{at}: slice files"
                    );
                }
            }
            other => panic!("unknown op {other}"),
        }
    }
    assert!(
        ticks.next().is_none(),
        "{name}: every recorded tick is replayed"
    );
    drop(agent);
    fs::remove_dir_all(&root).unwrap_or_default();
}

/// Proves: B-RUN-09 (each member reports its own work phase: an owner-requested cleanup runs first, a share its
/// caps cannot hold is refused by name before any fetch, its slice is fetched and verified (`downloading_slice`
/// -> `slice_ready`), a tampered or oversized slice is refused by name, the owner's contributor accepts its work
/// offer, and `work_running`/`serving`/a blocked worker's code/what it waits for follow from its worker).
#[test]
fn the_work_phase_equals_the_executed_oracle_tick_by_tick() {
    let fx = work_fixture();
    let scenarios = fx["scenarios"].as_array().expect("scenarios");
    assert_eq!(scenarios.len(), 7, "every recorded scenario is replayed");
    let ticks: usize = scenarios
        .iter()
        .map(|s| s["log"].as_array().map_or(0, Vec::len))
        .sum();
    assert_eq!(ticks, 36, "every recorded tick is replayed");
    for scenario in scenarios {
        run_work(scenario);
    }
}

fn work_announcement(fx: &Value) -> Value {
    fx["scenarios"][0]["ops"]
        .as_array()
        .expect("ops")
        .iter()
        .find(|op| op["op"] == "announce" && op["value"]["phase"] == "work")
        .expect("work announcement")["value"]
        .clone()
}

/// Proves: in the work phase the agent starts its bootstrap worker only when the offer it holds IS the
/// announced bootstrap offer (`demo_target.py:495-497`): an agent whose descriptor is fresh in the work phase
/// (nothing bootstrapped yet) holding the WORK offer accepts its work and never re-runs the bootstrap.
#[test]
fn the_work_phase_bootstraps_only_for_the_bootstrap_offer_it_holds() {
    let fx = work_fixture();
    let whole = fx["scenarios"]
        .as_array()
        .expect("scenarios")
        .iter()
        .find(|s| s["name"] == "whole_snapshot_work_waits_by_name")
        .expect("whole-snapshot scenario");
    let mut work = whole["ops"]
        .as_array()
        .expect("ops")
        .iter()
        .find(|op| op["op"] == "announce" && op["value"]["phase"] == "work")
        .expect("work announcement")["value"]
        .clone();
    work["bootstrap_offer_id"] = json!("bootstrap-offer-1");
    let offer = |id: &str| {
        json!({"archive_digest": format!("sha256:{}", "3".repeat(64)),
        "offer_id": id, "run_id": work["run_id"]})
    };
    for (held, bootstraps) in [("work-offer-1", false), ("bootstrap-offer-1", true)] {
        let root = scratch(&format!("bootstrap-{held}"));
        let mut host = WorkHost::new(root.clone());
        let mut agent = LocalAgent::new();
        host.native = json!({"state": "stopped"});
        host.offer = Some(offer(held));
        host.announcement = Some(work.clone());
        let status = agent.tick(&mut host);
        let routes: Vec<&Value> = host.actions.iter().map(|a| &a[0]).collect();
        if bootstraps {
            assert_eq!(status["state"], "bootstrap_running", "{held}: {status}");
            assert_eq!(routes, [&json!("bootstrap")], "{held}");
            // Once per descriptor: the next tick does not start it again.
            host.native = json!({"state": "stopped"});
            agent.tick(&mut host);
            assert_eq!(host.actions.len(), 1, "{held}: {:?}", host.actions);
        } else {
            assert_eq!(status["state"], "work_running", "{held}: {status}");
            assert_eq!(
                routes,
                [&json!("/api/work/preview"), &json!("/api/work/accept")],
                "{held}: the work offer is accepted, the bootstrap is not re-run"
            );
        }
        drop(agent);
        fs::remove_dir_all(&root).unwrap_or_default();
    }
}

/// Proves: the HTTP repository fallback (S1 model download) is never used for a slice: without an owner-served
/// source the member refuses by name and reads no byte.
#[test]
fn a_member_without_an_owner_served_source_refuses_by_name() {
    let fx = work_fixture();
    let work = work_announcement(&fx);
    let root = scratch("no-source");
    let mut host = WorkHost::new(root.clone());
    host.transport = false;
    let mut agent = LocalAgent::new();
    host.announcement = Some(json!({"phase": "collect", "run_id": work["run_id"],
        "deployment_id": work["deployment_id"], "model": work["model"], "slice_mode": true,
        "manifest_digest": work["manifest_digest"]}));
    assert_eq!(agent.tick(&mut host)["state"], "reported");
    host.announcement = Some(work);
    for _ in 0..2 {
        let status = agent.tick(&mut host);
        assert_eq!(status["state"], "error", "{status}");
        assert_eq!(
            status["detail"],
            slice_fetch::REPOSITORY_FALLBACK_NOT_PORTED
        );
    }
    assert_eq!(published(&host.slices()), json!({}), "nothing fetched");
    assert_eq!(
        host.sink.last().expect("reported")["detail"],
        "slice_repository_fallback_not_ported"
    );
    drop(agent);
    fs::remove_dir_all(&root).unwrap_or_default();
}

/// A transport whose reads wait for the test to release them, one at a time.
struct Gated {
    inner: SnapshotRangeTransport,
    gate: std::sync::mpsc::Receiver<()>,
    reads: std::sync::Arc<std::sync::atomic::AtomicUsize>,
}
impl SliceTransport for Gated {
    fn read_range(
        &mut self,
        m: &str,
        c: &str,
        p: &str,
        o: u64,
        l: u64,
    ) -> Result<Vec<u8>, SliceError> {
        self.reads.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        self.gate
            .recv()
            .map_err(|_| SliceError("slice_test_gate_closed".into()))?;
        self.inner.read_range(m, c, p, o, l)
    }
}

/// Proves: Stop reaches a running slice fetch: the owner's `stopped` announcement ends it at the next segment,
/// nothing partial is published, and the same run then reports the cancellation by name.
#[test]
fn stop_cancels_a_running_slice_fetch_between_segments() {
    let fx = work_fixture();
    let work = work_announcement(&fx);
    let root = scratch("stop");
    let mut host = WorkHost::new(root.clone());
    let (release, gate) = std::sync::mpsc::channel();
    let reads = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    struct Once(Option<Gated>);
    let mut gated = Once(Some(Gated {
        inner: SnapshotRangeTransport::new(
            root.join("snapshot"),
            work["model"]["model_id"].as_str().expect("id"),
            work["model"]["resolved_commit"].as_str().expect("commit"),
            None,
        ),
        gate,
        reads: reads.clone(),
    }));
    struct StopHost<'a>(&'a mut WorkHost, &'a mut Once);
    impl AgentHost for StopHost<'_> {
        fn announcement(&mut self) -> Result<Option<Value>, api::local_agent::ExchangeUnavailable> {
            self.0.announcement()
        }
        fn member(&mut self) -> Option<String> {
            self.0.member()
        }
        fn run_active(&mut self) -> bool {
            self.0.run_active()
        }
        fn resource_check(&mut self) -> Option<String> {
            self.0.resource_check()
        }
        fn prepared_model(&mut self) -> Result<(String, String), Failure> {
            self.0.prepared_model()
        }
        fn describe(&mut self, r: &str, d: &str, s: Option<&str>) -> Result<Value, Failure> {
            self.0.describe(r, d, s)
        }
        fn member_caps(&mut self) -> Result<Value, Failure> {
            self.0.member_caps()
        }
        fn storage_cap(&mut self) -> Result<u64, Failure> {
            self.0.storage_cap()
        }
        fn slice_root(&mut self, r: &str) -> Result<PathBuf, Failure> {
            self.0.slice_root(r)
        }
        fn slice_transport(&mut self, _: &Value) -> Option<Box<dyn SliceTransport>> {
            self.1
                .0
                .take()
                .map(|g| Box::new(g) as Box<dyn SliceTransport>)
        }
        fn sink(&mut self, r: &Value) -> Result<(), api::local_agent::ExchangeUnavailable> {
            self.0.sink(r)
        }
        fn clock(&mut self) -> f64 {
            self.0.clock()
        }
    }
    let mut agent = LocalAgent::new();
    let mut collect = work.clone();
    collect["phase"] = json!("collect");
    host.announcement = Some(collect);
    assert_eq!(
        agent.tick(&mut StopHost(&mut host, &mut gated))["state"],
        "reported"
    );
    host.announcement = Some(work.clone());
    let status = agent.tick(&mut StopHost(&mut host, &mut gated));
    assert_eq!(status["state"], "downloading_slice", "{status}");
    while reads.load(std::sync::atomic::Ordering::SeqCst) == 0 {
        std::thread::yield_now();
    }
    let mut stopped = work.clone();
    stopped["phase"] = json!("stopped");
    host.announcement = Some(stopped);
    assert_eq!(
        agent.tick(&mut StopHost(&mut host, &mut gated))["state"],
        "stopped"
    );
    release.send(()).expect("release the in-flight read");
    agent.join_slice();
    assert_eq!(
        reads.load(std::sync::atomic::Ordering::SeqCst),
        1,
        "no segment after Stop"
    );
    let run = work["run_id"].as_str().expect("run");
    let files = published(&host.slices());
    assert!(
        files
            .as_object()
            .expect("map")
            .keys()
            .all(|k| !k.starts_with(&format!("{run}/stage-0"))),
        "nothing partial is published: {files}"
    );
    host.announcement = Some(work);
    let status = agent.tick(&mut StopHost(&mut host, &mut gated));
    assert_eq!(status["state"], "error");
    assert_eq!(status["detail"], "slice_fetch_cancelled");
    drop(agent);
    fs::remove_dir_all(&root).unwrap_or_default();
}

fn secs(n: u64) -> std::time::Duration {
    std::time::Duration::from_secs(n)
}
/// Signals once its fetch thread has fully exited (thread-local destructors run after the thread's
/// result is stored, so the agent's `JoinHandle::is_finished` is already true).
struct OnExit(std::sync::mpsc::Sender<()>);
impl Drop for OnExit {
    fn drop(&mut self) {
        // The receiver is gone only when the test already failed.
        self.0.send(()).unwrap_or_default();
    }
}
thread_local! {
    static ON_EXIT: std::cell::RefCell<Option<OnExit>> = const { std::cell::RefCell::new(None) };
}
/// A source that fails its first read once the test releases it.
struct Gone {
    release: std::sync::mpsc::Receiver<()>,
    exited: std::sync::mpsc::Sender<()>,
}
impl SliceTransport for Gone {
    fn read_range(
        &mut self,
        _: &str,
        _: &str,
        _: &str,
        _: u64,
        _: u64,
    ) -> Result<Vec<u8>, SliceError> {
        ON_EXIT.with(|e| *e.borrow_mut() = Some(OnExit(self.exited.clone())));
        // A failing test drops the sender: the read then fails at once.
        self.release.recv_timeout(secs(30)).unwrap_or_default();
        Err(SliceError("slice_test_source_gone".into()))
    }
}

/// Proves: a fetch that ends while the agent is mid-step is collected, never started again: its
/// refusal is reported on that same tick and the source is asked for exactly once
/// (`_drive_slice` reads the thread's result before deciding to start one).
#[test]
fn a_fetch_that_ends_mid_step_is_reported_not_started_again() {
    let fx = work_fixture();
    let work = work_announcement(&fx);
    let root = scratch("ends-mid-step");
    let mut host = WorkHost::new(root.clone());
    let (release, gone_release) = std::sync::mpsc::channel();
    let (exited_tx, exited) = std::sync::mpsc::channel();
    struct MidStep<'a> {
        host: &'a mut WorkHost,
        gone: Option<Gone>,
        transports: usize,
        armed: bool,
        release: std::sync::mpsc::Sender<()>,
        exited: std::sync::mpsc::Receiver<()>,
    }
    impl AgentHost for MidStep<'_> {
        fn announcement(&mut self) -> Result<Option<Value>, api::local_agent::ExchangeUnavailable> {
            self.host.announcement()
        }
        fn member(&mut self) -> Option<String> {
            self.host.member()
        }
        fn run_active(&mut self) -> bool {
            self.host.run_active()
        }
        fn resource_check(&mut self) -> Option<String> {
            self.host.resource_check()
        }
        fn prepared_model(&mut self) -> Result<(String, String), Failure> {
            self.host.prepared_model()
        }
        fn describe(&mut self, r: &str, d: &str, s: Option<&str>) -> Result<Value, Failure> {
            self.host.describe(r, d, s)
        }
        fn native_control(&mut self) -> Result<Value, Failure> {
            self.host.native_control()
        }
        fn pending_offer(&mut self) -> Result<Option<Value>, Failure> {
            self.host.pending_offer()
        }
        /// Called after the step collected nothing and before the slice is driven: the running fetch
        /// ends exactly here.
        fn member_caps(&mut self) -> Result<Value, Failure> {
            if std::mem::take(&mut self.armed) {
                self.release.send(()).expect("release the read");
                self.exited
                    .recv_timeout(secs(30))
                    .expect("the fetch thread exited");
            }
            self.host.member_caps()
        }
        fn storage_cap(&mut self) -> Result<u64, Failure> {
            self.host.storage_cap()
        }
        fn slice_root(&mut self, r: &str) -> Result<PathBuf, Failure> {
            self.host.slice_root(r)
        }
        fn slice_transport(&mut self, _: &Value) -> Option<Box<dyn SliceTransport>> {
            self.transports += 1;
            self.gone
                .take()
                .map(|g| Box::new(g) as Box<dyn SliceTransport>)
        }
        fn sink(&mut self, r: &Value) -> Result<(), api::local_agent::ExchangeUnavailable> {
            self.host.sink(r)
        }
        fn clock(&mut self) -> f64 {
            self.host.clock()
        }
    }
    let mut collect = work.clone();
    collect["phase"] = json!("collect");
    host.announcement = Some(collect);
    let mut agent = LocalAgent::new();
    let mut mid = MidStep {
        host: &mut host,
        gone: Some(Gone {
            release: gone_release,
            exited: exited_tx,
        }),
        transports: 0,
        armed: false,
        release,
        exited,
    };
    assert_eq!(agent.tick(&mut mid)["state"], "reported");
    mid.host.announcement = Some(work);
    let status = agent.tick(&mut mid);
    assert_eq!(status["state"], "downloading_slice", "{status}");
    mid.armed = true;
    let status = agent.tick(&mut mid);
    let transports = mid.transports;
    agent.join_slice();
    assert_eq!(
        (status["state"].clone(), status["detail"].clone()),
        (json!("error"), json!("slice_test_source_gone")),
        "{status}"
    );
    assert_eq!(transports, 1, "the ended fetch was started again");
    drop(agent);
    fs::remove_dir_all(&root).unwrap_or_default();
}

/// A transport that serves a wrong byte for the first read of each segment and counts rejects.
struct Flaky {
    inner: SnapshotRangeTransport,
    corrupted: std::collections::BTreeSet<(String, u64)>,
    rejects: usize,
}
impl SliceTransport for Flaky {
    fn read_range(
        &mut self,
        m: &str,
        c: &str,
        p: &str,
        o: u64,
        l: u64,
    ) -> Result<Vec<u8>, SliceError> {
        let mut data = self.inner.read_range(m, c, p, o, l)?;
        if self.corrupted.insert((p.to_owned(), o)) {
            data[0] ^= 0xff;
        }
        Ok(data)
    }
    fn reject(&mut self, _: &str, _: u64, _: u64) -> bool {
        self.rejects += 1;
        true
    }
}

/// Proves: the slice fetcher publishes only digest-verified bytes: a corrupted segment is re-fetched after the
/// source rejects it, a fetch resumes from its journal, a mismatching existing file is a conflict, and the
/// download / disk caps refuse before any read.
#[test]
fn the_slice_fetcher_publishes_only_verified_bytes() {
    let fx = work_fixture();
    let work = work_announcement(&fx);
    let plan = &work["node_slice_plan"];
    let digest = plan["node_plan_digest"].as_str().expect("digest");
    let root = scratch("fetcher");
    WorkHost::new(root.clone());
    let snapshot = || {
        SnapshotRangeTransport::new(
            root.join("snapshot"),
            plan["model_id"].as_str().expect("id"),
            plan["resolved_commit"].as_str().expect("commit"),
            None,
        )
    };
    let dest = root.join("dest");
    fs::create_dir_all(&dest).expect("dest");
    let never = || false;
    let mut flaky = Flaky {
        inner: snapshot(),
        corrupted: Default::default(),
        rejects: 0,
    };
    let report =
        slice_fetch::fetch_node_slice(plan, digest, &dest, &mut flaky, 1 << 20, 1 << 20, &never)
            .expect("verified");
    assert_eq!(flaky.rejects, 3, "each corrupted segment was rejected once");
    assert_eq!(report["range_requests"], 6);
    assert_eq!(report["state"], "slice_verified_not_loaded");
    for file in plan["files"].as_array().expect("files") {
        let path = dest.join(file["path"].as_str().expect("path"));
        assert_eq!(json!(sha_hex(&path)), file["content_digest"]);
        assert_eq!(
            fs::metadata(&path).expect("meta").permissions().mode() & 0o777,
            0o600
        );
    }
    assert!(!dest.join(".slice-work").exists(), "work files are removed");
    // Idempotent: every byte is reused, no read.
    let again = slice_fetch::fetch_node_slice(
        plan,
        digest,
        &dest,
        &mut snapshot(),
        1 << 20,
        1 << 20,
        &never,
    )
    .expect("reuse");
    assert_eq!(
        (
            again["range_requests"].clone(),
            again["reused_bytes"].clone()
        ),
        (json!(0), json!(2223))
    );
    // A different existing file is a conflict, never overwritten.
    let a = dest.join(plan["files"][0]["path"].as_str().expect("path"));
    fs::write(&a, b"other bytes").expect("write");
    let refused = slice_fetch::fetch_node_slice(
        plan,
        digest,
        &dest,
        &mut snapshot(),
        1 << 20,
        1 << 20,
        &never,
    );
    assert_eq!(
        refused.expect_err("conflict").0,
        "slice_destination_conflict"
    );
    // Caps refuse before reading.
    for (download, disk, code) in [
        (2199, 1 << 20, "slice_download_limit_exceeded"),
        (1 << 20, 2222, "slice_disk_limit_exceeded"),
    ] {
        let mut gate = Flaky {
            inner: snapshot(),
            corrupted: Default::default(),
            rejects: 0,
        };
        let refused =
            slice_fetch::fetch_node_slice(plan, digest, &dest, &mut gate, download, disk, &never);
        assert_eq!(refused.expect_err(code).0, code);
        assert!(gate.corrupted.is_empty(), "{code}: no byte read");
    }
    // Resume: a journal of verified segments is reused, only the rest is fetched.
    let resume = root.join("resume");
    fs::create_dir_all(&resume).expect("resume");
    let stop_after_first = std::sync::atomic::AtomicUsize::new(0);
    // Calls: one per file before cloning, then one per segment: stop before a.bin's second segment.
    let stop = || stop_after_first.fetch_add(1, std::sync::atomic::Ordering::SeqCst) >= 3;
    let cancelled = slice_fetch::fetch_node_slice(
        plan,
        digest,
        &resume,
        &mut snapshot(),
        1 << 20,
        1 << 20,
        &stop,
    );
    assert_eq!(cancelled.expect_err("stopped").0, "slice_fetch_cancelled");
    let progress = slice_fetch::slice_progress(plan, &resume).expect("progress");
    assert_eq!(progress, json!({"done_bytes": 1000, "total_bytes": 2223}));
    let done = slice_fetch::fetch_node_slice(
        plan,
        digest,
        &resume,
        &mut snapshot(),
        1 << 20,
        1 << 20,
        &never,
    )
    .expect("resumed");
    assert_eq!(
        (done["reused_bytes"].clone(), done["range_requests"].clone()),
        (json!(1000), json!(2))
    );
    fs::remove_dir_all(&root).unwrap_or_default();
}
