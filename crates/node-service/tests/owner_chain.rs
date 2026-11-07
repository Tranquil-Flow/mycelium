// SPDX-License-Identifier: AGPL-3.0-only
//! The owner chain's first steps against the REAL collaborators that exist in the workspace: the api
//! crate's model catalog (`owner_storage_requirement`) and resource-policy reader, composed under
//! `owner-run`'s `model` step. Expected values: the owner-run executed-oracle fixture
//! FX-SLICE1-model-step (`crates/owner-run/tests/fixtures/oracle_model_step.json`).
use api::preferences::resource_policy;
use node_service::owner_chain::{catalog_owner_storage_need, step_exception, storage_consent_cap};
use owner_run::chain::{StepEnv, StepError};
use owner_run::steps::model::{
    ModelPort, PreparedModel, PreparedSource, Reverify, StepInput, run_model_step,
};
use serde_json::{Value, json};
use std::path::PathBuf;

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../owner-run/tests/fixtures/oracle_model_step.json"
    ))
    .expect("fixture")
}

struct Scratch(PathBuf);
impl Scratch {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("owner-chain-{}-{name}", std::process::id()));
        if dir.exists() {
            std::fs::remove_dir_all(&dir).expect("clear stale scratch");
        }
        std::fs::create_dir_all(&dir).expect("scratch dir");
        std::fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700))
            .expect("mode");
        Self(dir)
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        if let Err(error) = std::fs::remove_dir_all(&self.0) {
            eprintln!("scratch cleanup failed: {error}");
        }
    }
}

fn consent(storage: u64) -> Value {
    json!({"peer_visibility": true, "power": "ac_only", "storage_bytes": storage, "network": "unmetered_only",
           "background": false, "diagnostics": false, "thermal": "stop_on_serious"})
}

/// Proves: B-ONB-11 (the owner-storage need of every registered model is the oracle's number, computed by
/// the Rust catalog; an unregistered model has none).
#[test]
fn the_real_catalog_gives_the_oracle_owner_storage_need() {
    let fx = fixture();
    for m in fx["models"].as_array().expect("models") {
        let (id, commit) = (
            m["model_id"].as_str().expect("id"),
            m["resolved_commit"].as_str().expect("c"),
        );
        assert_eq!(
            catalog_owner_storage_need(id, commit),
            m["owner_storage_need"].as_u64(),
            "{id}"
        );
    }
    assert_eq!(
        catalog_owner_storage_need("nobody/nothing", "0".repeat(40).as_str()),
        None
    );
}

/// Proves: B-ONB-11 (the storage cap is read through the real resource-policy reader: absent -> no cap, a
/// policy -> its storage figure, an unsafe file -> the step fails as a DemoError exception).
#[test]
fn the_real_resource_policy_reader_feeds_the_storage_cap() {
    let dir = Scratch::new("policy");
    assert_eq!(storage_consent_cap(&dir.0).expect("absent"), None);
    resource_policy::write(&dir.0, &consent(500_000_000)).expect("write consent");
    assert_eq!(
        storage_consent_cap(&dir.0).expect("present"),
        Some(json!(500_000_000u64))
    );
    let path = dir.0.join(resource_policy::FILE);
    std::fs::set_permissions(&path, std::os::unix::fs::PermissionsExt::from_mode(0o644))
        .expect("loosen");
    assert_eq!(
        storage_consent_cap(&dir.0),
        Err(StepError::Exception {
            class: "DemoError".into()
        })
    );
    let e = resource_policy::read(&dir.0).expect_err("unsafe mode");
    assert_eq!(
        step_exception(e),
        StepError::Exception {
            class: "DemoError".into()
        }
    );
}

struct Env;
impl StepEnv for Env {
    fn progress(&mut self, _detail: &str) {}
    fn stopped(&self) -> bool {
        false
    }
    fn now(&mut self) -> f64 {
        0.0
    }
    fn wall(&mut self) -> f64 {
        unreachable!("the model step reads no wall clock")
    }
    fn set_result(&mut self, key: &str, _: Value) {
        panic!("the model step sets no result ({key})")
    }
    fn record_exception(&mut self, code: &str, _: &str) {
        panic!("the model step records no exception ({code})")
    }
    fn pause(&mut self, _seconds: f64) -> bool {
        false
    }
}

/// A prepared model, a real storage-consent file and the real catalog under the real `model` step.
struct Composed {
    root: PathBuf,
    model: PreparedModel,
}
impl ModelPort for Composed {
    fn owner_and_running(&self) -> bool {
        true
    }
    fn swarm_created(&self) -> bool {
        true
    }
    fn has_closing_thread(&self) -> bool {
        false
    }
    fn closing_thread_alive(&mut self) -> bool {
        false
    }
    fn acquisition_state(&mut self) -> String {
        "prepared".into()
    }
    fn reverify_available(&self) -> bool {
        false
    }
    fn reverify(&mut self) -> Reverify {
        Reverify::Done
    }
    fn prepared_source(&mut self) -> Result<PreparedSource, StepError> {
        Ok(PreparedSource {
            model: self.model.clone(),
            manifest_digest: format!("sha256:{}", "ab".repeat(32)),
            source_bytes: json!(1_000_000),
        })
    }
    fn storage_consent_cap(&mut self) -> Result<Option<Value>, StepError> {
        storage_consent_cap(&self.root)
    }
    fn check_rollover_members(&mut self, _binding: &Value) -> Result<(), StepError> {
        Ok(())
    }
}

/// Proves: B-ONB-11 and B-RUN-03 (a real storage consent below the real catalog's need refuses the step with
/// owner_storage_consent_below_model; at the need it passes), B-RUN-01 (the model step's result names the model).
#[test]
fn model_step_over_the_real_policy_file_and_catalog() {
    let fx = fixture();
    let row = &fx["models"][1];
    let need = catalog_owner_storage_need(
        row["model_id"].as_str().expect("id"),
        row["resolved_commit"].as_str().expect("c"),
    )
    .expect("registered");
    let model = |need: u64| PreparedModel {
        model_id: row["model_id"].as_str().expect("id").to_owned(),
        resolved_commit: row["resolved_commit"].as_str().expect("c").to_owned(),
        architecture: row["architecture"].as_str().expect("a").to_owned(),
        label: row["label"].as_str().expect("l").to_owned(),
        choice: row["choice"].as_str().map(str::to_owned),
        shape_registered: true,
        owner_storage_need: need,
    };
    // the consent form carries at most the catalog's ceiling and at least 100 MB; `need` is within it
    let dir = Scratch::new("compose");
    let ceiling = api::model_catalog::storage_consent_ceiling();
    assert!(
        need <= ceiling,
        "the oracle's need fits the consent ceiling"
    );
    resource_policy::write(&dir.0, &consent(need - 1)).expect("below");
    let mut port = Composed {
        root: dir.0.clone(),
        model: model(need),
    };
    match run_model_step(&mut Env, &mut port, &StepInput::default()) {
        Err(StepError::Refused(e)) => assert_eq!(e.code, "owner_storage_consent_below_model"),
        other => panic!("expected the storage refusal, got {other:?}"),
    }
    resource_policy::write(&dir.0, &consent(need)).expect("at need");
    let out =
        run_model_step(&mut Env, &mut port, &StepInput::default()).expect("passes at the need");
    assert_eq!(out.model_id, row["model_id"].as_str().expect("id"));
    assert!(!out.slice_mode);
}
