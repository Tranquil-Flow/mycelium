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
};
use protocol::identity::Identity;
use serde_json::{Value, json};
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
