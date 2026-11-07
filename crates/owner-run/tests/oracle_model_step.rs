// SPDX-License-Identifier: AGPL-3.0-only
//! The `model` and `contributor` steps (`demo_chain.py:442-530 @ f4e366e9`) against EXECUTED v0.1.4:
//! FX-SLICE1-model-step (probe `r11-probe-model-step.py`: the real `AppDemoChain._step_model` /
//! `_step_contributor` on a stub app; 106 model-step cases, 7 contributor cases). Compared exactly:
//! the outcome (closed code, message, fix), the progress details in order, the order and number of
//! every collaborator call, and the number of clock readings (= polls of the waits).
use owner_run::chain::{StepEnv, StepError};
use owner_run::steps::contributor::{ContributorPort, run_contributor_step};
use owner_run::steps::model::{
    ModelPort, PreparedModel, PreparedSource, Reverify, StepInput, run_model_step,
};
use serde_json::{Value, json};
use std::cell::Cell;
use std::rc::Rc;

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_model_step.json")).expect("fixture parses")
}

/// The oracle's `ctx.wait_until` clock: a reading advances one second.
struct Env {
    clock: f64,
    stop: Rc<Cell<bool>>,
    progress: Vec<String>,
}

impl StepEnv for Env {
    fn progress(&mut self, detail: &str) {
        self.progress.push(detail.to_owned());
    }
    fn stopped(&self) -> bool {
        self.stop.get()
    }
    fn now(&mut self) -> f64 {
        self.clock += 1.0;
        self.clock
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
        self.stop.get()
    }
}

struct Port<'a> {
    cfg: &'a Value,
    model: PreparedModel,
    log: Vec<String>,
    states: Vec<String>,
    n: usize,
    closing_left: Option<i64>,
    stop: Rc<Cell<bool>>,
}

impl ModelPort for Port<'_> {
    fn owner_and_running(&self) -> bool {
        self.cfg.get("role").is_none_or(|r| r == "owner")
            && self.cfg.get("running").is_none_or(|r| r == true)
    }
    fn swarm_created(&self) -> bool {
        self.cfg.get("swarm").is_none_or(|s| s == true)
    }
    fn has_closing_thread(&self) -> bool {
        self.closing_left.is_some()
    }
    fn closing_thread_alive(&mut self) -> bool {
        self.log.push("closing_alive".into());
        let left = self.closing_left.as_mut().expect("a closing thread");
        *left -= 1;
        *left >= 0
    }
    fn acquisition_state(&mut self) -> String {
        let state = self.states[self.n.min(self.states.len() - 1)].clone();
        self.n += 1;
        self.log.push(format!("status:{state}"));
        if let Some(at) = self.cfg["stop_at_call"].as_u64()
            && self.n as u64 >= at
        {
            self.stop.set(true);
        }
        state
    }
    fn reverify_available(&self) -> bool {
        matches!(
            self.cfg["reverify"].as_str(),
            Some("makes_prepared" | "raises" | "noop")
        )
    }
    fn reverify(&mut self) -> Reverify {
        match self.cfg["reverify"].as_str() {
            Some("makes_prepared") => {
                self.log.push("reverify".into());
                self.states = vec!["prepared".into()];
                self.n = 0;
                Reverify::Done
            }
            Some("raises") => {
                self.log.push("reverify".into());
                Reverify::Failed
            }
            Some("noop") => {
                self.log.push("reverify".into());
                Reverify::Done
            }
            other => panic!("reverify called while unavailable ({other:?})"),
        }
    }
    fn prepared_source(&mut self) -> Result<PreparedSource, StepError> {
        self.log.push("prepared_source".into());
        if self.cfg.get("prepared_source_raises").is_some() {
            return Err(StepError::Exception {
                class: "AcquisitionError".into(),
            });
        }
        Ok(PreparedSource {
            model: self.model.clone(),
            manifest_digest: format!("sha256:{}", "ab".repeat(32)),
            source_bytes: self
                .cfg
                .get("source_bytes")
                .cloned()
                .unwrap_or(json!(1_000_000)),
        })
    }
    fn storage_consent_cap(&mut self) -> Result<Option<Value>, StepError> {
        match self.cfg.get("storage") {
            None => Ok(None),
            Some(Value::String(s)) if s == "none" => Ok(None),
            Some(v) => Ok(Some(v.clone())),
        }
    }
    fn check_rollover_members(&mut self, _binding: &Value) -> Result<(), StepError> {
        self.log.push("rollover_members".into());
        Ok(())
    }
}

fn model_of(row: &Value) -> PreparedModel {
    PreparedModel {
        model_id: row["model_id"].as_str().expect("id").to_owned(),
        resolved_commit: row["resolved_commit"].as_str().expect("commit").to_owned(),
        architecture: row["architecture"].as_str().expect("arch").to_owned(),
        label: row["label"].as_str().expect("label").to_owned(),
        choice: row["choice"].as_str().map(str::to_owned),
        shape_registered: true,
        owner_storage_need: row["owner_storage_need"].as_u64().expect("need"),
    }
}

fn run(
    case: &Value,
) -> (
    Result<owner_run::steps::model::ModelStepOutput, StepError>,
    Env,
    Vec<String>,
) {
    let cfg = &case["cfg"];
    let stop = Rc::new(Cell::new(false));
    let mut env = Env {
        clock: 0.0,
        stop: stop.clone(),
        progress: Vec::new(),
    };
    let mut port = Port {
        cfg,
        model: model_of(&case["model"]),
        log: Vec::new(),
        states: cfg["states"]
            .as_array()
            .map(|a| {
                a.iter()
                    .filter_map(|s| s.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_else(|| vec!["prepared".into()]),
        n: 0,
        closing_left: cfg["closing"].as_i64(),
        stop,
    };
    let input = StepInput {
        rollover: cfg["reason"] == "rollover",
        rollover_binding: cfg.get("binding").filter(|b| !b.is_null()).cloned(),
    };
    let result = run_model_step(&mut env, &mut port, &input);
    (result, env, port.log)
}

/// Proves: B-RUN-02 (owner_role_required, owner_swarm_required), B-RUN-03 (owner_model_not_prepared with the
/// download message and the prepare_model fix, other refusals carry only their code), B-RUN-04 (a Stop ends the
/// waiting step as a stop, never a failure), B-MODEL-08 (a relaunch re-hashes the snapshot already on disk before
/// asking for a download), B-CONSENT-15 (a rollover run refuses a changed model), B-ONB-11 (owner storage
/// consent below the model's need), B-RUN-01 (the step's progress detail).
#[test]
fn model_step_matches_the_executed_oracle_on_every_case() {
    let fx = fixture();
    let cases = fx["cases"].as_object().expect("cases");
    assert_eq!(cases.len(), 106);
    let (mut ok, mut refused, mut stopped) = (0, 0, 0);
    for (name, case) in cases {
        let (result, env, log) = run(case);
        let want = &case["outcome"];
        match result {
            Ok(out) => {
                ok += 1;
                assert_eq!(want["ok"], true, "{name}: port succeeded, oracle {want}");
                let state = &case["state"];
                assert_eq!(state["slice_mode"], out.slice_mode, "{name}: slice mode");
                assert_eq!(state["manifest_digest"], out.manifest_digest, "{name}");
                assert_eq!(
                    state["model"],
                    json!({"choice": out.choice, "model_id": out.model_id, "resolved_commit": out.resolved_commit}),
                    "{name}: model"
                );
                assert_eq!(
                    case["chain_slice_fetch"], out.slice_mode,
                    "{name}: app._chain_slice_fetch"
                );
            }
            Err(StepError::Refused(e)) => {
                refused += 1;
                let r = &want["refused"];
                assert_eq!(r["code"], e.code, "{name}: code");
                assert_eq!(
                    r["message"].as_str(),
                    e.message.as_deref(),
                    "{name}: message"
                );
                let fix: Option<Value> = e.fix.map(Value::Object);
                assert_eq!(
                    r.get("fix").filter(|f| !f.is_null()).cloned(),
                    fix,
                    "{name}: fix"
                );
                assert!(
                    case["chain_slice_fetch"].is_null(),
                    "{name}: slice flag set before the refusal"
                );
            }
            Err(StepError::Stopped) => {
                stopped += 1;
                assert_eq!(want["stopped"], true, "{name}: port stopped, oracle {want}");
            }
            Err(StepError::Exception { class }) => panic!("{name}: unexpected exception {class}"),
        }
        let progress: Vec<&str> = case["progress"]
            .as_array()
            .expect("progress")
            .iter()
            .filter_map(Value::as_str)
            .collect();
        assert_eq!(progress, env.progress, "{name}: progress details");
        let calls: Vec<&str> = case["calls"]
            .as_array()
            .expect("calls")
            .iter()
            .filter_map(Value::as_str)
            .collect();
        assert_eq!(calls, log, "{name}: collaborator calls");
        assert!(
            (case["clock_reads"].as_f64().expect("reads") - env.clock).abs() < f64::EPSILON,
            "{name}: clock readings"
        );
    }
    assert_eq!((ok, refused, stopped), (71, 34, 1));
}

struct Contributor<'a> {
    cfg: &'a Value,
    log: Vec<String>,
    made: Vec<String>,
}

impl ContributorPort for Contributor<'_> {
    fn ensure_local_contributor(&mut self) -> Result<String, StepError> {
        if self.cfg["already"] != true {
            self.log.push("start_local_contributor".into());
            if self.cfg["start_raises"] == true {
                return Err(StepError::Exception {
                    class: "OSError".into(),
                });
            }
        }
        Ok(self.cfg["node"]
            .as_str()
            .unwrap_or("remote-mac-0123456789abcdef01234567")
            .to_owned())
    }
    fn exchange_available(&self) -> bool {
        self.cfg.get("exchange").is_none_or(|e| e == true)
    }
    fn attach_agent(&mut self, local: &str) -> Result<(), StepError> {
        self.log.push("agent_factory".into());
        self.made.push(local.to_owned());
        Ok(())
    }
}

/// Proves: B-RUN-01 (this Mac joins the run: its node id is the run's local node, the progress detail names its
/// first twelve characters), B-RUN-03 (an unexpected start failure keeps only its class; a missing run
/// exchange is owner_exchange_unavailable, after the contributor started).
#[test]
fn contributor_step_matches_the_executed_oracle() {
    let fx = fixture();
    let cases = fx["contributor"].as_object().expect("contributor");
    assert_eq!(cases.len(), 7);
    for (name, case) in cases {
        let cfg = &case["cfg"];
        let mut env = Env {
            clock: 0.0,
            stop: Rc::new(Cell::new(false)),
            progress: Vec::new(),
        };
        let mut port = Contributor {
            cfg,
            log: Vec::new(),
            made: Vec::new(),
        };
        let result = run_contributor_step(&mut env, &mut port);
        let want = &case["outcome"];
        match result {
            Ok(local) => {
                assert_eq!(want["ok"], true, "{name}");
                assert_eq!(case["local_node"], local, "{name}: local node");
                let made: Vec<Value> = case["agent_made"]
                    .as_array()
                    .expect("made")
                    .iter()
                    .map(|m| m[0].clone())
                    .collect();
                assert_eq!(made, port.made, "{name}: agent attached to the local node");
            }
            Err(StepError::Refused(e)) => {
                assert_eq!(want["refused"]["code"], e.code, "{name}");
                assert!(port.made.is_empty(), "{name}: no agent after the refusal");
            }
            Err(StepError::Exception { class }) => assert_eq!(want["exception"], class, "{name}"),
            Err(StepError::Stopped) => panic!("{name}: stopped"),
        }
        let progress: Vec<&str> = case["progress"]
            .as_array()
            .expect("progress")
            .iter()
            .filter_map(Value::as_str)
            .collect();
        assert_eq!(progress, env.progress, "{name}: progress");
        // the guard lines belong to the host's lock, not to the step; the rest of the order is the step's
        let calls: Vec<&str> = case["calls"]
            .as_array()
            .expect("calls")
            .iter()
            .filter_map(Value::as_str)
            .filter(|c| !c.starts_with("guard_"))
            .collect();
        assert_eq!(calls, port.log, "{name}: call order");
    }
}

/// Proves: B-RUN-01 (the registered models the step knows are the oracle's, in order).
#[test]
fn fixture_models_are_the_registered_catalog_in_order() {
    let fx = fixture();
    let ids: Vec<&str> = fx["models"]
        .as_array()
        .expect("models")
        .iter()
        .filter_map(|m| m["model_id"].as_str())
        .collect();
    assert_eq!(ids.len(), 5);
}

/// Proves: B-RUN-03 (an acquisition that cannot give its prepared source fails the step with only the
/// exception class `AcquisitionError`, after the "checking" progress line and after the four state reads;
/// nothing else is called). Expected values: `r12-probe-prepared-source-raises.py` executed against
/// v0.1.4 (FX-SLICE1-prepared-source-raises, 1 case):
/// `{"exception": "AcquisitionError"}`, progress `["checking the pinned model on this Mac"]`, calls
/// `status:prepared` x4 then `prepared_source`.
#[test]
fn a_prepared_source_that_raises_fails_the_step_with_its_class_only() {
    let case = json!({"cfg": {"model": 1, "prepared_source_raises": true}, "model": fixture()["models"][1]});
    let (outcome, env, log) = run(&case);
    assert_eq!(
        outcome.err(),
        Some(StepError::Exception {
            class: "AcquisitionError".into()
        })
    );
    assert_eq!(env.progress, ["checking the pinned model on this Mac"]);
    assert_eq!(
        log,
        [
            "status:prepared",
            "status:prepared",
            "status:prepared",
            "status:prepared",
            "prepared_source"
        ]
    );
}
