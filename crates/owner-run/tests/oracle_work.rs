// SPDX-License-Identifier: AGPL-3.0-only
//! Executed v014 publish/targets over labelled exchange/workflow doubles; no native/serving claim.
use owner_run::chain::{PortError, StepEnv, StepError};
use owner_run::machine::{OwnerRunError, Reason};
use owner_run::state::RunState;
use owner_run::steps::exchange::{Announcement, ClockSkew, MemberRow, ReportsPort};
use owner_run::steps::model::{ModelStepOutput, PreparedModel, PreparedSource};
use owner_run::steps::publish::{WorkPort, run_publish_step};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_work_r15_v2.json")).unwrap()
}
#[derive(Default)]
struct Record {
    time: f64,
    calls: Vec<Value>,
    clocks: Vec<Value>,
}
struct Env {
    rec: Arc<Mutex<Record>>,
    stop: Option<f64>,
    result: Value,
    progress: Vec<Value>,
    publishes: Vec<Value>,
}
impl StepEnv for Env {
    fn progress(&mut self, s: &str) {
        self.progress.push(json!(s));
    }
    fn stopped(&self) -> bool {
        self.stop
            .is_some_and(|at| self.rec.lock().unwrap().time >= at)
    }
    fn now(&mut self) -> f64 {
        let mut rec = self.rec.lock().unwrap();
        let t = rec.time;
        rec.clocks.push(json!(["mono", t]));
        t
    }
    fn wall(&mut self) -> f64 {
        let mut rec = self.rec.lock().unwrap();
        let t = 1_800_000_000.0 + rec.time;
        rec.clocks.push(json!(["wall", t]));
        t
    }
    fn set_result(&mut self, k: &str, v: Value) {
        if k == "participants" {
            self.publishes.push(v.clone());
        }
        self.result[k] = v;
    }
    fn record_exception(&mut self, _: &str, _: &str) {
        panic!("unexpected cleanup exception");
    }
    fn pause(&mut self, s: f64) -> bool {
        self.rec.lock().unwrap().time += s;
        self.stopped()
    }
}
struct Exchange {
    spec: Value,
    rec: Arc<Mutex<Record>>,
}
fn frame(frames: &Value, t: f64) -> Value {
    frames
        .as_array()
        .unwrap()
        .iter()
        .rfind(|f| f[0].as_f64().unwrap() <= t)
        .map_or(Value::Null, |f| f[1].clone())
}
fn exception() -> StepError {
    StepError::Exception {
        class: "RuntimeError".into(),
    }
}
impl ReportsPort for Exchange {
    fn forget_reports(&mut self) {
        panic!("cannot forget");
    }
    fn announce(&mut self, _: &Announcement) -> Result<(), StepError> {
        panic!("cannot announce");
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        let mut rec = self.rec.lock().unwrap();
        rec.calls.push(json!("reports"));
        if self.spec["reports_error"] == true {
            return Err(exception());
        }
        Ok(serde_json::from_value(frame(&self.spec["reports"], rec.time)).unwrap())
    }
    fn members(&mut self) -> Vec<MemberRow> {
        self.rec.lock().unwrap().calls.push(json!("members"));
        self.spec["members"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| MemberRow {
                node_id: m["node_id"].as_str().unwrap().into(),
                lifecycle_state: m["lifecycle_state"].as_str().map(str::to_owned),
                lease_expires_at: m["lease_expires_at"].as_f64(),
            })
            .collect()
    }
    fn own_node_id(&mut self) -> Option<String> {
        Some("owner-authority".into())
    }
    fn member_identity(&mut self, _: &str) -> Value {
        panic!("no identity reread");
    }
    fn clock_skew(&mut self, _: &[String]) -> Result<Option<ClockSkew>, StepError> {
        panic!("no skew read");
    }
    fn tick_local(&mut self) {
        self.rec.lock().unwrap().calls.push(json!("tick"));
    }
}
struct Workflow {
    spec: Value,
    rec: Arc<Mutex<Record>>,
    reads: usize,
}
impl WorkPort for Workflow {
    fn install_cleanup_progress(&mut self) -> Result<(), StepError> {
        if self.spec["no_workflow"] != true {
            self.rec
                .lock()
                .unwrap()
                .calls
                .push(json!("install_cleanup_progress"));
            if self.spec["install_error"] == true {
                return Err(exception());
            }
        }
        Ok(())
    }
    fn start_serving(&mut self, proposal: &Value) -> Result<Value, PortError> {
        let mut rec = self.rec.lock().unwrap();
        rec.calls.push(json!(["start_serving", proposal]));
        if self.spec["cleanup_tick"] == true && self.spec["no_workflow"] != true {
            rec.calls.push(json!("tick"));
        }
        if let Some(e) = self.spec.get("start_error") {
            return Err(if e["kind"] == "refused" {
                PortError::Refused(OwnerRunError::with(
                    e["code"].as_str().unwrap(),
                    Some("private refusal message"),
                    None,
                ))
            } else {
                PortError::Failed {
                    code: e["code"].as_str().map(str::to_owned),
                    text: e["text"].as_str().unwrap().into(),
                }
            });
        }
        Ok(self.spec["started"].clone())
    }
    fn workflow_state(&mut self) -> Result<Value, StepError> {
        if self.spec["no_workflow"] == true {
            return Ok(Value::Null);
        }
        let mut rec = self.rec.lock().unwrap();
        rec.calls.push(json!("workflow_state"));
        self.reads += 1;
        if self.spec["workflow_error_read"].as_u64() == Some(self.reads as u64) {
            return Err(exception());
        }
        Ok(
            if let Some(seq) = self.spec["workflow_sequence"].as_array() {
                seq[(self.reads - 1).min(seq.len() - 1)].clone()
            } else {
                frame(&self.spec["workflow"], rec.time)
            },
        )
    }
}
fn state_of(spec: &Value) -> RunState {
    let s = &spec["state"];
    let m = &spec["model"];
    let mut state = RunState::new(
        "run-1",
        "dep-1",
        if spec["reason"] == "rollover" {
            Reason::Rollover
        } else {
            Reason::Start
        },
    );
    state.local_node = s["local_node"].as_str().map(str::to_owned);
    state.descriptors = serde_json::from_value(s["descriptors"].clone()).unwrap();
    state.rollover_binding = s.get("rollover_binding").cloned();
    state.participants = serde_json::from_value(s["participants"].clone()).unwrap();
    state.proposal = s.get("proposal").cloned();
    state.compile_state = (!s["compile_state"].is_null()).then(|| s["compile_state"].clone());
    state.worker_wall_limits = serde_json::from_value(s["worker_wall_limits"].clone()).unwrap();
    state.layers = serde_json::from_value(s["layers"].clone()).unwrap();
    let prepared = PreparedSource {
        model: PreparedModel {
            model_id: m["model_id"].as_str().unwrap().into(),
            resolved_commit: m["resolved_commit"].as_str().unwrap().into(),
            architecture: m["architecture"].as_str().unwrap().into(),
            label: m["label"].as_str().unwrap().into(),
            choice: m["choice"].as_str().map(str::to_owned),
            shape_registered: true,
            owner_storage_need: m["owner_storage_need"].as_u64().unwrap(),
        },
        manifest_digest: s["manifest_digest"].as_str().unwrap().into(),
        source_bytes: json!(123),
    };
    state.model = Some(ModelStepOutput {
        forced_slice: false,
        prepared,
        choice: m["choice"].as_str().map(str::to_owned),
        slice_mode: s["slice_mode"].as_bool().unwrap(),
        manifest_digest: s["manifest_digest"].as_str().unwrap().into(),
        model_id: m["model_id"].as_str().unwrap().into(),
        resolved_commit: m["resolved_commit"].as_str().unwrap().into(),
    });
    state.execution_clock_started_at = s["execution_clock_started_at"].as_f64();
    state.registry_origin = s.get("registry_origin").cloned();
    state
}
fn rle(xs: &[Value]) -> Value {
    let mut out: Vec<Value> = vec![];
    for x in xs {
        if let Some(last) = out.last_mut().filter(|v| v[0] == *x) {
            last[1] = json!(last[1].as_u64().unwrap() + 1);
        } else {
            out.push(json!([x, 1]));
        }
    }
    json!(out)
}
fn replay(name: &str, case: &Value) {
    let spec = &case["spec"];
    let rec = Arc::new(Mutex::new(Record::default()));
    let mut env = Env {
        rec: rec.clone(),
        stop: spec["stop_at"].as_f64(),
        result: json!({}),
        progress: vec![],
        publishes: vec![],
    };
    let mut source = Workflow {
        spec: spec.clone(),
        rec: rec.clone(),
        reads: 0,
    };
    let mut reports = Exchange {
        spec: spec.clone(),
        rec: rec.clone(),
    };
    let mut state = state_of(spec);
    let interval = budgets::BudgetId::OwnerPoll
        .span(budgets::Entries::ZERO)
        .unwrap()
        .millis() as f64
        / 1000.0;
    let result = if spec["step"] == "publish" {
        run_publish_step(&mut env, &mut reports, &mut source, &mut state, interval)
    } else {
        owner_run::steps::targets::run_targets_step(
            &mut env,
            &mut reports,
            &mut source,
            &mut state,
            interval,
        )
    };
    let outcome = match result {
        Ok(()) => json!({"kind":"ok"}),
        Err(StepError::Stopped) => json!({"kind":"stopped"}),
        Err(StepError::Exception { class }) => json!({"kind":"exception","class":class}),
        Err(StepError::Refused(e)) => json!({"kind":"refused","code":e.code,"message":e.message}),
    };
    assert_eq!(outcome, case["outcome"], "{name}: executed outcome");
    // Compare actual full context, not a copy of the expected unchanged inputs.
    let mut context = json!({"compile_state":state.compile_state,"descriptors":state.descriptors,
        "layers":state.layers,"local_node":state.local_node,
        "manifest_digest":state.model.as_ref().map(|m| &m.manifest_digest),
        "participants":state.participants,"proposal":state.proposal,"rollover_binding":state.rollover_binding,
        "slice_mode":state.model.as_ref().map(|m|m.slice_mode),"worker_wall_limits":state.worker_wall_limits});
    if state.proposal.is_none() {
        context.as_object_mut().unwrap().remove("proposal");
    }
    if let Some(clock) = state.execution_clock_started_at {
        context["execution_clock_started_at"] = json!(clock);
    }
    if let Some(origin) = &state.registry_origin {
        context["registry_origin"] = origin.clone();
    }
    assert_eq!(context, case["state"], "{name}: partial/final state");
    assert_eq!(
        rle(&rec.lock().unwrap().clocks),
        case["clocks"],
        "{name}: clock reads"
    );
    assert_eq!(env.result, case["result"], "{name}: result");
    assert_eq!(rle(&env.progress), case["progress"], "{name}: progress");
    assert_eq!(
        rle(&env.publishes),
        case["publishes"],
        "{name}: published participants"
    );
    assert_eq!(
        rle(&rec.lock().unwrap().calls),
        case["calls"],
        "{name}: collaborator order and payloads"
    );
    assert_eq!(
        rec.lock().unwrap().time,
        case["time"].as_f64().unwrap(),
        "{name}: source budget"
    );
    println!("WORK-DIRECT-CASE-PASS {name}");
}
#[test]
fn targets_matches_executed_oracle() {
    for (name, case) in fixture()["cases"].as_object().unwrap() {
        if name.starts_with("targets-") {
            replay(name, case);
        }
    }
}
#[test]
fn publish_matches_executed_oracle() {
    for (name, case) in fixture()["cases"].as_object().unwrap() {
        if name.starts_with("publish-") {
            replay(name, case);
        }
    }
}
