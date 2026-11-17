// SPDX-License-Identifier: AGPL-3.0-only
//! Executed v014 slices over labelled exchange/workflow doubles; no fetch/device claim.
use owner_run::chain::{StepEnv, StepError};
use owner_run::machine::{OwnerRunError, Reason};
use owner_run::state::RunState;
use owner_run::steps::exchange::{Announcement, ClockSkew, MemberRow, ReportsPort};
use owner_run::steps::model::{ModelStepOutput, PreparedModel, PreparedSource};
use owner_run::steps::slices::{SlicesPort, run_slices_step};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_slices_r14.json")).unwrap()
}
#[derive(Default)]
struct Record {
    time: f64,
    calls: Vec<Value>,
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
        self.rec.lock().unwrap().time
    }
    fn wall(&mut self) -> f64 {
        1_800_000_000.0 + self.now()
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
struct Source {
    spec: Value,
    rec: Arc<Mutex<Record>>,
    reads: usize,
}
impl SlicesPort for Source {
    fn node_slice_plans(&mut self) -> Result<Option<BTreeMap<String, Value>>, StepError> {
        self.rec.lock().unwrap().calls.push(json!("plans"));
        if self.spec["plans_error"] == true {
            return Err(StepError::Exception {
                class: "RuntimeError".into(),
            });
        }
        Ok(self.spec["plans"]
            .as_object()
            .map(|m| m.iter().map(|(k, v)| (k.clone(), v.clone())).collect()))
    }
    fn storage_policy(&mut self) -> Result<Option<Value>, StepError> {
        self.rec.lock().unwrap().calls.push(json!("policy"));
        self.reads += 1;
        if self.spec["policy_error"].as_u64() == Some(self.reads as u64) {
            return Err(OwnerRunError::new("policy_read_failed").into());
        }
        let ps = self.spec["policies"].as_array().unwrap();
        Ok(ps[(self.reads - 1).min(ps.len() - 1)]
            .get("storage_bytes")
            .cloned())
    }
}
struct Exchange {
    spec: Value,
    rec: Arc<Mutex<Record>>,
}
impl ReportsPort for Exchange {
    fn forget_reports(&mut self) {
        panic!("slices cannot forget");
    }
    fn announce(&mut self, _: &Announcement) -> Result<(), StepError> {
        panic!("work must retain limits");
    }
    fn announce_work(&mut self, a: &Announcement, limits: &Value) -> Result<(), StepError> {
        let per: serde_json::Map<String, Value> = a.per_node.clone().unwrap().into_iter().collect();
        self.rec.lock().unwrap().calls.push(json!(["announce",{
            "phase":a.phase,"run_id":a.run_id,"deployment_id":a.deployment_id,
            "model":{"model_id":a.model_id,"resolved_commit":a.resolved_commit},
            "slice_mode":a.slice_mode,"manifest_digest":a.manifest_digest,
            "per_node":per,"work_limits":limits
        }]));
        if self.spec["announce_error"] == true {
            return Err(OwnerRunError::new("work_announce_failed").into());
        }
        Ok(())
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        let mut rec = self.rec.lock().unwrap();
        rec.calls.push(json!("reports"));
        let mut out = BTreeMap::new();
        for frame in self.spec["reports"].as_array().unwrap() {
            if frame[0].as_f64().unwrap() <= rec.time {
                out = frame[1]
                    .as_object()
                    .unwrap()
                    .iter()
                    .map(|(k, v)| (k.clone(), v.clone()))
                    .collect();
            }
        }
        Ok(out)
    }
    fn members(&mut self) -> Vec<MemberRow> {
        panic!("slices does not read membership");
    }
    fn own_node_id(&mut self) -> Option<String> {
        panic!("slices does not read own identity");
    }
    fn member_identity(&mut self, _: &str) -> Value {
        panic!("slices does not reread identities");
    }
    fn clock_skew(&mut self, _: &[String]) -> Result<Option<ClockSkew>, StepError> {
        panic!("slices does not read skew");
    }
    fn tick_local(&mut self) {
        self.rec.lock().unwrap().calls.push(json!("tick"));
    }
}
fn state_forced(spec: &Value, forced_slice: bool) -> RunState {
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
    state.participants = serde_json::from_value(s["participants"].clone()).unwrap();
    state.proposal = Some(s["proposal"].clone());
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
        forced_slice,
        prepared,
        choice: m["choice"].as_str().map(str::to_owned),
        slice_mode: s["slice_mode"].as_bool().unwrap(),
        manifest_digest: s["manifest_digest"].as_str().unwrap().into(),
        model_id: m["model_id"].as_str().unwrap().into(),
        resolved_commit: m["resolved_commit"].as_str().unwrap().into(),
    });
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
    replay_as(name, case, false, &case["progress"]);
}
fn replay_as(name: &str, case: &Value, forced_slice: bool, progress: &Value) {
    let spec = &case["spec"];
    let rec = Arc::new(Mutex::new(Record::default()));
    let mut env = Env {
        rec: rec.clone(),
        stop: spec["stop_at"].as_f64(),
        result: json!({}),
        progress: vec![],
        publishes: vec![],
    };
    let mut source = Source {
        spec: spec.clone(),
        rec: rec.clone(),
        reads: 0,
    };
    let mut reports = Exchange {
        spec: spec.clone(),
        rec: rec.clone(),
    };
    let mut state = state_forced(spec, forced_slice);
    let interval = budgets::BudgetId::OwnerPoll
        .span(budgets::Entries::ZERO)
        .unwrap()
        .millis() as f64
        / 1000.0;
    let result = run_slices_step(&mut env, &mut reports, &mut source, &mut state, interval);
    let outcome = match result {
        Ok(()) => json!({"kind":"ok"}),
        Err(StepError::Stopped) => json!({"kind":"stopped"}),
        Err(StepError::Exception { class }) => json!({"kind":"exception","class":class}),
        Err(StepError::Refused(e)) => json!({"kind":"refused","code":e.code,"message":e.message}),
    };
    assert_eq!(outcome, case["outcome"], "{name}: executed outcome");
    assert_eq!(
        json!({"work_limits":state.work_limits,"work_nodes":state.work_nodes}),
        case["state"],
        "{name}: partial/final state"
    );
    assert_eq!(env.result, case["result"], "{name}: result");
    assert_eq!(rle(&env.progress), *progress, "{name}: progress");
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
}
#[test]
fn slices_matches_executed_oracle() {
    for (name, case) in fixture()["cases"].as_object().unwrap() {
        replay(name, case);
    }
}

/// B11(b) (ledger rows 84 a, 93 (4 a), owner ruling 16 Nov): on the packaged two-Mac path the
/// host forces stage slicing (`ModelStepOutput::forced_slice`) and the owner pushes each Mac its
/// own stage shard inside the slices port before it answers "no slice plan"
/// (`OwnerWork::node_slice_plans`), so the oracle's `whole snapshots: no slice to fetch`
/// (`demo_chain.py:880`, a run whose members already hold the whole snapshot) told the owner that
/// nothing moved while the stage shards did. Such a run reports the owner's shard push instead;
/// everything else the oracle recorded for the case (calls, announcement, limits, state, result,
/// budget) is unchanged, and a run that is not force-sliced keeps the oracle's text (the executed
/// cases above). Recorded divergence (text for `api/contracts/divergences.json`, lane A's file):
/// REPORT-laneC-release.md, B11(b).
#[test]
fn a_force_sliced_run_reports_the_owner_s_shard_push_not_whole_snapshots() {
    const ORACLE: &str = "whole snapshots: no slice to fetch";
    const PUSHED: &str = "stage shards pushed: every Mac holds only its own layers";
    let cases = fixture();
    for name in ["whole-none", "whole-empty"] {
        let case = &cases["cases"][name];
        let oracle = case["progress"].to_string();
        assert!(
            oracle.contains(ORACLE),
            "{name}: the oracle's own text: {oracle}"
        );
        let pushed: Value = serde_json::from_str(&oracle.replace(ORACLE, PUSHED)).unwrap();
        replay_as(name, case, true, &pushed);
    }
}
