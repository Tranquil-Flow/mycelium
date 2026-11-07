// SPDX-License-Identifier: AGPL-3.0-only
//! The facts and adapters the owner run chain needs from this crate (SE-L10-MODEL-FACTS): registered-model
//! label / architecture / picker choice and the run identity stamp equal EXECUTED v0.1.4 (probe
//! `r19-probe-model-facts.py`, fixture `oracle_model_facts.json`), and the real chain over fake host ports
//! runs the ported steps (`model`), then ends at the first step whose port is not wired with a tagged
//! refusal that names its seam (never a success). The acquisition's `prepared_source` refusals are the
//! source's (`acquisition.py:1492-1514`).
use api::{
    acquisition::{Acquisition, Options, PrepareError, Preparer},
    actions::Action,
    model_catalog,
    owner_chain::{ChainPorts, HostChain},
    owner_driver::{DemoRuns, Timing, fresh_identity, utc_stamp},
};
use owner_run::{
    action::DemoFacts,
    chain::StepError,
    steps::model::{NOT_PREPARED_MESSAGE, PreparedSource, Reverify},
};
use serde_json::{Map, Value, json};
use std::{
    path::Path,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

const FIXTURE: &str = include_str!("fixtures/oracle_model_facts.json");

fn secs(n: u64) -> Duration {
    Duration::from_secs(n)
}

/// Proves: B-MODEL-01 (the five registered models' label, architecture, picker choice and owner storage,
/// executed), partial B-MODEL-07 (the run names the model it pins from these facts).
#[test]
fn model_facts_equal_the_executed_oracle() {
    let fx: Value = serde_json::from_str(FIXTURE).expect("fixture");
    assert_eq!(fx["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    let rows = fx["models"].as_array().expect("models");
    assert_eq!(rows.len(), model_catalog::registered().len());
    for row in rows {
        let id = row["model_id"].as_str().unwrap();
        let commit = row["resolved_commit"].as_str().unwrap();
        let model = model_catalog::find(id, commit).expect("registered");
        assert_eq!(row["label"], model.label(), "{id}");
        assert_eq!(row["architecture"], model.architecture(), "{id}");
        assert_eq!(
            row["choice"],
            model_catalog::choice_for_model(model).map_or(Value::Null, Value::from),
            "{id}"
        );
        assert_eq!(
            row["owner_storage"].as_u64(),
            Some(model_catalog::owner_storage_requirement(model)),
            "{id}"
        );
    }
}

/// Proves: B-RUN-14 (a run gets `a13-<6 hex>-qwen-<UTC stamp>` and a version-4 uuid deployment id, fresh each time).
#[test]
fn run_identity_stamps_equal_executed_strftime() {
    let fx: Value = serde_json::from_str(FIXTURE).expect("fixture");
    for row in fx["stamps"].as_array().expect("stamps") {
        let epoch = row["epoch"].as_f64().unwrap();
        assert_eq!(row["stamp"], utc_stamp(epoch), "epoch {epoch}");
    }
    let id = fresh_identity(1_793_231_734.0).expect("identity");
    assert!(id.run_id.starts_with("a13-"), "{}", id.run_id);
    assert!(
        id.run_id.ends_with("-qwen-20261028T235534Z"),
        "{}",
        id.run_id
    );
    assert_eq!(id.run_id.len(), "a13-".len() + 6 + "-qwen-".len() + 16);
    let parts: Vec<&str> = id.deployment_id.split('-').collect();
    assert_eq!(
        parts.iter().map(|p| p.len()).collect::<Vec<_>>(),
        [8, 4, 4, 4, 12]
    );
    assert!(
        parts[2].starts_with('4'),
        "uuid version 4: {}",
        id.deployment_id
    );
    assert_ne!(
        fresh_identity(1.0).unwrap().deployment_id,
        fresh_identity(1.0).unwrap().deployment_id,
        "fresh randomness per run"
    );
}

struct Facts;
impl DemoFacts for Facts {
    fn owner(&self) -> bool {
        true
    }
    fn ensure_exchange(&mut self) -> bool {
        true
    }
    fn members_joined_without_relay(&self) -> Option<u64> {
        Some(0)
    }
}

struct FakePorts {
    state: &'static str,
    cap: Option<Value>,
    calls: Arc<Mutex<Vec<&'static str>>>,
}
impl ChainPorts for FakePorts {
    fn owner_and_running(&self) -> bool {
        true
    }
    fn swarm_created(&self) -> bool {
        true
    }
    fn acquisition_state(&self) -> String {
        self.calls.lock().unwrap().push("state");
        self.state.to_owned()
    }
    fn reverify(&self) -> Reverify {
        self.calls.lock().unwrap().push("reverify");
        Reverify::Failed
    }
    fn prepared_source(&self) -> Result<PreparedSource, StepError> {
        self.calls.lock().unwrap().push("prepared_source");
        let model = &model_catalog::registered()[1];
        Ok(api::owner_chain::prepared_source_from(
            model,
            "sha256:".to_owned() + &"ab".repeat(32),
            json!(999_604_126u64),
        ))
    }
    fn storage_consent_cap(&self) -> Result<Option<Value>, StepError> {
        Ok(self.cap.clone())
    }
    fn check_rollover_members(&self, _: &Value) -> Result<(), StepError> {
        Ok(())
    }
    fn note_model(&mut self, choice: Option<&str>) {
        assert_eq!(
            choice,
            model_catalog::choice_for_model(&model_catalog::registered()[1])
        );
        self.calls.lock().unwrap().push("note_model");
    }
}

fn run_until_failed(ports: FakePorts) -> Value {
    let mut ports = Some(ports);
    let mut runs = DemoRuns::new(
        Box::new(move || Box::new(HostChain::new(ports.take().expect("one chain")))),
        Box::new(|| fresh_identity(1_793_231_734.0).expect("identity")),
        Timing {
            poll: secs(0),
            ..Timing::registered()
        },
    );
    runs.demo_action(Action::DemoStart, &Map::new(), &mut Facts)
        .expect("admitted");
    let deadline = Instant::now() + secs(20);
    let status = loop {
        let status = runs.status().expect("installed").to_value();
        if status["state"] == "failed" && runs.quiescent() {
            break status;
        }
        assert!(Instant::now() < deadline, "the run never failed: {status}");
        std::thread::yield_now();
    };
    runs.close();
    status
}

/// Proves: B-MODEL-07 (the model step reports "whole snapshot per Mac" for a small model), B-RUN-03 (a failing
/// step ends the run with a closed code and the failed step name; a step this host does not wire ends with a
/// tagged refusal naming its seam, never a success).
#[test]
fn a_prepared_model_passes_the_model_step_and_the_run_stops_at_the_first_unwired_step() {
    let calls = Arc::new(Mutex::new(Vec::new()));
    let status = run_until_failed(FakePorts {
        state: "prepared",
        cap: None,
        calls: calls.clone(),
    });
    assert_eq!(status["failed_step"], "contributor", "{status}");
    assert_eq!(
        status["error_code"], "owner_run_step_not_ported",
        "{status}"
    );
    assert!(
        status["error_message"]
            .as_str()
            .unwrap()
            .contains("NP-L10-contributor"),
        "{status}"
    );
    assert_eq!(status["steps"][0]["state"], "done");
    assert_eq!(
        status["steps"][0]["detail"],
        "Qwen/Qwen2.5-0.5B-Instruct · whole snapshot per Mac"
    );
    // The oracle reads the state four times before it accepts `prepared` (three checks and the wait).
    let log = calls.lock().unwrap().clone();
    assert_eq!(log.iter().filter(|c| **c == "state").count(), 4, "{log:?}");
    assert_eq!(log.iter().filter(|c| **c == "prepared_source").count(), 1);
    assert_eq!(
        log.last(),
        Some(&"note_model"),
        "only after the model step accepts"
    );
    assert!(
        !log.contains(&"reverify"),
        "a prepared model is not re-hashed: {log:?}"
    );
}

/// Proves: B-RUN-03 (a failing step carries a closed code, message and fix), B-MODEL-03 (a model that is not
/// prepared is never downloaded by Start: the owner is told to download it).
#[test]
fn an_unprepared_model_stops_the_run_at_the_model_step_with_the_download_instruction() {
    let status = run_until_failed(FakePorts {
        state: "closed",
        cap: None,
        calls: Arc::default(),
    });
    assert_eq!(status["failed_step"], "model", "{status}");
    assert_eq!(status["error_code"], "owner_model_not_prepared");
    assert_eq!(status["error_message"], NOT_PREPARED_MESSAGE);
    assert_eq!(
        status["error_fix"],
        json!({"action": "prepare_model", "target": "acquisition"})
    );
}

/// Proves: B-MODEL-09 (the owner's storage consent must hold the selected model; refused up front with the numbers).
#[test]
fn a_storage_consent_below_the_model_refuses_with_the_oracle_text() {
    let status = run_until_failed(FakePorts {
        state: "prepared",
        cap: Some(json!(1_000_000_000u64)),
        calls: Arc::default(),
    });
    assert_eq!(
        status["error_code"], "owner_storage_consent_below_model",
        "{status}"
    );
    assert_eq!(
        status["error_message"],
        "This Mac's storage limit is 1.0 GB, but Qwen2.5-0.5B needs 1.1 GB here (its pinned download plus headroom). \
         Choose a larger storage limit or a smaller model."
    );
}

struct NoFacts;
impl Preparer for NoFacts {
    fn prepare(&self, _: &Path, _: &model_catalog::Model) -> Result<(), PrepareError> {
        Ok(())
    }
}
fn open(dir: &Path) -> Acquisition {
    Acquisition::open(
        dir,
        &model_catalog::registered()[1],
        Options {
            transport: Arc::new(api::acquisition::HttpTransport::loopback_for_tests(
                "http://127.0.0.1:1",
            )),
            preparer: Arc::new(NoFacts),
            disk_free: Box::new(|_| Ok(u64::MAX)),
            clock_unix_ms: Box::new(api::acquisition::system_clock_ms),
            observer: None,
        },
    )
    .expect("open")
}

/// Proves: partial B-MODEL-04 (a source that is not prepared is refused by name before anything is served from
/// it; a closed acquisition says so).
#[test]
fn prepared_source_refuses_like_the_oracle_before_and_after_close() {
    // `_private_root` refuses a non-canonical state root (macOS `TMPDIR` is under the `/var` symlink).
    let dir = std::fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("l10-prepsrc-{}", std::process::id()));
    std::fs::remove_dir_all(&dir).unwrap_or_default();
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
    let acquisition = open(&dir);
    assert_eq!(
        acquisition.prepared_source().unwrap_err().code(),
        "source_not_prepared"
    );
    acquisition.close().expect("close");
    assert_eq!(
        acquisition.prepared_source().unwrap_err().code(),
        "acquisition_closed"
    );
    std::fs::remove_dir_all(&dir).unwrap_or_default();
}
