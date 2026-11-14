// SPDX-License-Identifier: AGPL-3.0-only
//! The serving step against the executed v0.1.4 `_step_serving` (`demo_chain.py:987-1106 @ f4e366e9`)
//! over labelled exchange/workflow doubles. The oracle's double takes the step's observational
//! branch, so the lifecycle-fenced commit is pinned by the source-cited tests at the end of this
//! file. No native, route or chat claim.
use owner_run::chain::{PortError, StepEnv, StepError};
use owner_run::machine::{OwnerRunError, Reason};
use owner_run::state::RunState;
use owner_run::steps::exchange::{Announcement, ClockSkew, MemberRow, ReportsPort};
use owner_run::steps::model::{ModelStepOutput, PreparedModel, PreparedSource};
use owner_run::steps::publish::{Fence, ServingIdentity, WorkPort};
use owner_run::steps::serving::run_serving_step;
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_serving_r19.json")).unwrap()
}
/// Lane SRV round 2: read-count status frames, an identity change after the wait, and the FENCED
/// branch over the real `ManagedServingWorkflow` and `RunExchange` (probe `serving_probe.py r20`).
fn fixture_r20() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_serving_r20.json")).unwrap()
}
#[derive(Default)]
struct Record {
    time: f64,
    calls: Vec<Value>,
    clocks: Vec<Value>,
    /// What the fenced commit published: the announcement's phase, limits and per-node rows.
    committed: Option<Value>,
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
fn frame(frames: &Value, t: f64) -> Value {
    frames
        .as_array()
        .unwrap()
        .iter()
        .rfind(|f| f[0].as_f64().unwrap() <= t)
        .map_or(Value::Null, |f| f[1].clone())
}
struct Exchange {
    spec: Value,
    rec: Arc<Mutex<Record>>,
}
impl ReportsPort for Exchange {
    fn forget_reports(&mut self) {
        panic!("cannot forget");
    }
    fn announce(&mut self, _: &Announcement) -> Result<(), StepError> {
        panic!("the serving announcement goes through the workflow fence");
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        let mut rec = self.rec.lock().unwrap();
        rec.calls.push(json!("reports"));
        let now = frame(&self.spec["reports"], rec.time);
        Ok(if now.is_null() {
            BTreeMap::new()
        } else {
            serde_json::from_value(now).unwrap()
        })
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
/// The oracle's workflow double plus the app attributes `current(workflow)` reads.
struct Workflow {
    spec: Value,
    rec: Arc<Mutex<Record>>,
    reads: usize,
    /// What the fenced commit answers (`None`: commits).
    fence: Option<&'static str>,
    /// `app._chain_epoch` reads after the `epoch_flip_after_status_read`-th status read.
    epoch_reads_after: usize,
}
fn is_int(v: &Value) -> bool {
    v.is_i64() || v.is_u64()
}
impl WorkPort for Workflow {
    fn install_cleanup_progress(&mut self) -> Result<(), StepError> {
        panic!("publish only");
    }
    fn start_serving(&mut self, _: &Value) -> Result<Value, PortError> {
        panic!("publish only");
    }
    fn workflow_state(&mut self) -> Result<Value, StepError> {
        if self.spec["no_workflow"] == true {
            return Ok(Value::Null);
        }
        let mut rec = self.rec.lock().unwrap();
        rec.calls.push(json!("workflow_state"));
        self.reads += 1;
        if self.spec["workflow_error_read"].as_u64() == Some(self.reads as u64) {
            return Err(StepError::Exception {
                class: "RuntimeError".into(),
            });
        }
        if self.spec["fenced"] == true {
            // The real ManagedServingWorkflow.status() (workflow.py:237-247), api_running.
            return Ok(json!({"state": "api_running", "error_code": null,
                "startup": self.spec["startup"], "challenge_progress": null,
                "readiness_source": "registry_api"}));
        }
        if let Some(reads) = self.spec["workflow_reads"].as_array() {
            return Ok(reads[self.reads.min(reads.len()) - 1].clone());
        }
        Ok(frame(&self.spec["workflow"], rec.time))
    }
    /// `needs_identity` and `current` exactly as `demo_chain.py:988-1004` evaluates them.
    fn serving_identity(&mut self, state: &RunState) -> Result<ServingIdentity, StepError> {
        let spec = &self.spec;
        let present = spec["no_workflow"] != true;
        let ctx_epoch = state
            .compile_state
            .as_ref()
            .map_or(Value::Null, |c| c["epoch"].clone());
        let compiled = &spec["app_compile_state"];
        let binding = &spec["progress_identity"];
        let needs = !ctx_epoch.is_null()
            || compiled.get("epoch").is_some()
            || (present && !binding.is_null());
        if !present {
            return Ok(if needs {
                ServingIdentity::Changed
            } else {
                ServingIdentity::Absent
            });
        }
        if !needs {
            return Ok(ServingIdentity::NotNeeded);
        }
        let mut app_epoch = frame(&spec["app_epoch"], self.rec.lock().unwrap().time);
        if let Some(flip) = spec["epoch_flip_after_status_read"].as_u64()
            && self.reads as u64 >= flip
        {
            self.epoch_reads_after += 1;
            if self.epoch_reads_after > 1 {
                app_epoch = json!(app_epoch.as_i64().unwrap() + 1);
            }
        }
        let current = is_int(&ctx_epoch)
            && is_int(&compiled["epoch"])
            && is_int(&app_epoch)
            && compiled["state"] == "compiled"
            && ctx_epoch == compiled["epoch"]
            && ctx_epoch == app_epoch
            && *binding == json!([state.run_id, state.deployment_id, ctx_epoch])
            && spec["workflow_stopped"] != true;
        Ok(if current {
            ServingIdentity::Current
        } else {
            ServingIdentity::Changed
        })
    }
    /// The re-checks and the announcement run inside the owner's Stop fence, as the oracle's
    /// `with app.guard, owner._lock, workflow._lock` scope does (`demo_chain.py:1073-1097`).
    fn commit_serving(
        &mut self,
        _: &RunState,
        a: &Announcement,
        work_limits: Option<&Value>,
        fence: Fence<'_>,
    ) -> Result<(), StepError> {
        fence(&mut || {
            if let Some(code) = self.fence {
                return Err(OwnerRunError::new(code).into());
            }
            let model = &self.spec["model"];
            assert_eq!(a.model_id, model["model_id"]);
            assert_eq!(a.resolved_commit, model["resolved_commit"]);
            let per_node = a
                .per_node
                .as_ref()
                .map(|rows| Value::Object(rows.iter().cloned().collect::<serde_json::Map<_, _>>()));
            self.rec.lock().unwrap().committed = Some(json!({"phase": a.phase,
                "work_limits": work_limits, "per_node": per_node.clone().unwrap_or(json!({}))}));
            self.rec.lock().unwrap().calls.push(json!(["announce", {
                "phase": a.phase, "run_id": a.run_id, "deployment_id": a.deployment_id,
                "model": model, "slice_mode": a.slice_mode, "manifest_digest": a.manifest_digest,
                "work_limits": work_limits, "per_node": per_node}]));
            Ok(())
        })
    }
}
fn state_of(spec: &Value) -> RunState {
    let s = &spec["state"];
    let m = &spec["model"];
    let mut state = RunState::new("run-1", "dep-1", Reason::Start);
    state.local_node = s["local_node"].as_str().map(str::to_owned);
    state.participants = serde_json::from_value(s["participants"].clone()).unwrap();
    state.compile_state = (!s["compile_state"].is_null()).then(|| s["compile_state"].clone());
    state.layers = serde_json::from_value(s["layers"].clone()).unwrap();
    let absent = |key: &str| {
        s[key].is_null()
            || spec["absent"]
                .as_array()
                .is_some_and(|a| a.contains(&json!(key)))
    };
    state.work_limits = (!absent("work_limits")).then(|| s["work_limits"].clone());
    state.work_nodes = (!absent("work_nodes")).then(|| s["work_nodes"].clone());
    state.registry_origin = (!s["registry_origin"].is_null()).then(|| s["registry_origin"].clone());
    let digest = s["manifest_digest"].as_str().unwrap();
    let prepared = PreparedSource {
        model: PreparedModel {
            model_id: m["model_id"].as_str().unwrap().into(),
            resolved_commit: m["resolved_commit"].as_str().unwrap().into(),
            architecture: "qwen3".into(),
            label: "Qwen3 0.6B".into(),
            choice: m["choice"].as_str().map(str::to_owned),
            shape_registered: true,
            owner_storage_need: 1,
        },
        manifest_digest: digest.into(),
        source_bytes: json!(123),
    };
    state.model = Some(ModelStepOutput {
        forced_slice: false,
        prepared,
        choice: m["choice"].as_str().map(str::to_owned),
        slice_mode: s["slice_mode"].as_bool().unwrap(),
        manifest_digest: digest.into(),
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
struct Ran {
    outcome: Value,
    env: Env,
    rec: Arc<Mutex<Record>>,
}
fn run(spec: &Value, fence: Option<&'static str>) -> Ran {
    let rec = Arc::new(Mutex::new(Record::default()));
    let mut env = Env {
        rec: rec.clone(),
        stop: spec["stop_at"].as_f64(),
        result: json!({}),
        progress: vec![],
        publishes: vec![],
    };
    let mut workflow = Workflow {
        spec: spec.clone(),
        rec: rec.clone(),
        reads: 0,
        fence,
        epoch_reads_after: 0,
    };
    let mut reports = Exchange {
        spec: spec.clone(),
        rec: rec.clone(),
    };
    let mut state = state_of(spec);
    let result = run_serving_step(
        &mut env,
        &mut reports,
        &mut workflow,
        &mut state,
        spec["interval"].as_f64().unwrap(),
    );
    let outcome = match result {
        Ok(()) => json!({"kind":"ok"}),
        Err(StepError::Stopped) => json!({"kind":"stopped"}),
        Err(StepError::Exception { class }) => json!({"kind":"exception","class":class}),
        Err(StepError::Refused(e)) => json!({"kind":"refused","code":e.code,"message":e.message}),
    };
    Ran { outcome, env, rec }
}
/// Proves: B-RUN-10 (the serving step waits, refuses and publishes as the executed v0.1.4 step does:
/// outcome, collaborator order and payloads, clock reads, progress lines, results and elapsed time).
#[test]
fn serving_matches_executed_oracle() {
    let fixture = fixture();
    assert_eq!(
        fixture["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let cases = fixture["cases"].as_object().unwrap();
    assert_eq!(cases.len(), 30);
    for (name, case) in cases {
        let ran = run(&case["spec"], None);
        assert_eq!(ran.outcome, case["outcome"], "{name}: executed outcome");
        assert_eq!(ran.env.result, case["result"], "{name}: result");
        assert_eq!(rle(&ran.env.progress), case["progress"], "{name}: progress");
        assert_eq!(
            rle(&ran.env.publishes),
            case["publishes"],
            "{name}: published participants"
        );
        let rec = ran.rec.lock().unwrap();
        assert_eq!(
            rle(&rec.calls),
            case["calls"],
            "{name}: collaborator order and payloads"
        );
        assert_eq!(rle(&rec.clocks), case["clocks"], "{name}: clock reads");
        assert_eq!(rec.time, case["time"].as_f64().unwrap(), "{name}: elapsed");
        println!("SERVING-CASE-PASS {name}");
    }
}
/// The fenced cases whose `at_guard` writer lands between the post-wait read and the fence: the
/// oracle's outcomes are pinned on the production fences, not on this double:
/// owner Stop / replaced context -> `stopped`, owner not running -> identity changed:
/// `owner-run` `runner::fence_tests` and `runner::replaced_tests`; workflow Stop, state, replacement
/// and live startup: `api` `joined_owner::work::serving_fence_tests`.
const FENCE_WRITERS: [&str; 7] = [
    "fenced-owner-stop-at-guard",
    "fenced-owner-replaced-at-guard",
    "fenced-owner-not-running-at-guard",
    "fenced-workflow-stop-at-guard",
    "fenced-workflow-stopped-at-guard",
    "fenced-startup-unqualified-at-guard",
    "fenced-workflow-replaced-at-guard",
];
/// Proves: B-RUN-10 (the serving step against the executed v0.1.4 step for the round-2 cases: a
/// present workflow whose status is not a mapping raises `AttributeError` on its first, waiting or
/// post-wait read (demo_chain.py:1007, 1017, 1030; ASRV-05); an identity that changes after the
/// wait's success fails the post-wait recheck (:1021-1023; SRV-5); and the FENCED branch over the
/// real ManagedServingWorkflow and RunExchange publishes `dict(work_limits)` and
/// `{node: dict(extra) for node, extra in (per_node or {}).items()}` with the source's exception
/// classes (run_exchange.py:167-176; ASRV-07), or nothing.)
#[test]
fn serving_matches_executed_oracle_round_two() {
    let fixture = fixture_r20();
    assert_eq!(
        fixture["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    let cases = fixture["cases"].as_object().unwrap();
    assert_eq!(cases.len(), 57);
    let mut replayed = 0;
    let mut differ = Vec::new();
    for (name, case) in cases {
        if FENCE_WRITERS.contains(&name.as_str()) {
            continue;
        }
        let ran = run(&case["spec"], None);
        let rec = ran.rec.lock().unwrap();
        let mut fields = vec![
            ("outcome", ran.outcome.clone(), case["outcome"].clone()),
            ("result", ran.env.result.clone(), case["result"].clone()),
            ("progress", rle(&ran.env.progress), case["progress"].clone()),
            (
                "publishes",
                rle(&ran.env.publishes),
                case["publishes"].clone(),
            ),
        ];
        if case["spec"]["fenced"] == true {
            let committed = rec
                .committed
                .clone()
                .unwrap_or(json!({"phase": "idle", "work_limits": null, "per_node": {}}));
            fields.push(("announcement", committed, case["announcement"].clone()));
        } else {
            fields.push(("calls", rle(&rec.calls), case["calls"].clone()));
            fields.push(("clocks", rle(&rec.clocks), case["clocks"].clone()));
            fields.push(("time", json!(rec.time), case["time"].clone()));
        }
        for (field, rust, oracle) in fields {
            if rust != oracle {
                differ.push(format!("{name}: {field}: rust {rust} oracle {oracle}"));
            }
        }
        replayed += 1;
        println!("SERVING-R20-CASE-RAN {name}");
    }
    assert_eq!(replayed, 50);
    assert!(
        differ.is_empty(),
        "{} differences:\n{}",
        differ.len(),
        differ.join("\n")
    );
}
/// Proves: B-RUN-10 (the fenced commit, `demo_chain.py:1081-1094`: a workflow that changed or lost its
/// qualification between the wait and the publication refuses; nothing is announced, no route origin
/// is published and the page is not told the route is ready).
#[test]
fn a_refused_fence_publishes_nothing() {
    let fixture = fixture();
    let spec = &fixture["cases"]["ok-immediate"]["spec"];
    for code in [
        "serving_workflow_identity_changed",
        "route_qualification_failed",
    ] {
        let ran = run(spec, Some(code));
        assert_eq!(
            ran.outcome,
            json!({"kind":"refused","code":code,"message":null})
        );
        assert_eq!(ran.env.result, json!({}), "{code}: nothing published");
        assert!(
            ran.env.progress.is_empty(),
            "{code}: {:?}",
            ran.env.progress
        );
        let rec = ran.rec.lock().unwrap();
        assert!(
            rec.calls.iter().all(|c| !c.is_array()),
            "{code}: no announcement"
        );
    }
}
/// Proves: B-RUN-10 (`demo_chain.py:1077-1080`: a Stop that lands after the wait ends the step as
/// stopped, not as a qualification failure, and before anything is published).
#[test]
fn a_stop_before_the_fence_ends_the_step_stopped() {
    let fixture = fixture();
    let mut spec = fixture["cases"]["ok-immediate"]["spec"].clone();
    spec["stop_at"] = json!(0.0);
    let rec = Arc::new(Mutex::new(Record::default()));
    // The wait's own first Stop check is `ctx.check_stopped()`: let it pass once, then stop.
    struct Late(Env, std::cell::Cell<u32>);
    impl StepEnv for Late {
        fn progress(&mut self, s: &str) {
            self.0.progress(s);
        }
        fn stopped(&self) -> bool {
            self.1.set(self.1.get() + 1);
            self.1.get() > 1
        }
        fn now(&mut self) -> f64 {
            self.0.now()
        }
        fn wall(&mut self) -> f64 {
            self.0.wall()
        }
        fn set_result(&mut self, k: &str, v: Value) {
            self.0.set_result(k, v);
        }
        fn record_exception(&mut self, c: &str, e: &str) {
            self.0.record_exception(c, e);
        }
        fn pause(&mut self, s: f64) -> bool {
            self.0.pause(s)
        }
    }
    let mut env = Late(
        Env {
            rec: rec.clone(),
            stop: None,
            result: json!({}),
            progress: vec![],
            publishes: vec![],
        },
        std::cell::Cell::new(0),
    );
    let mut workflow = Workflow {
        spec: spec.clone(),
        rec: rec.clone(),
        reads: 0,
        fence: None,
        epoch_reads_after: 0,
    };
    let mut reports = Exchange {
        spec: spec.clone(),
        rec: rec.clone(),
    };
    let mut state = state_of(&spec);
    let result = run_serving_step(&mut env, &mut reports, &mut workflow, &mut state, 0.5);
    assert!(matches!(result, Err(StepError::Stopped)), "{result:?}");
    assert_eq!(env.0.result, json!({}));
    assert!(rec.lock().unwrap().calls.iter().all(|c| !c.is_array()));
}
/// Proves: B-RUN-03 (a host that lends its workflow without wiring the serving step ends the run
/// with the tagged refusal, never with a route it did not check).
#[test]
fn a_host_without_the_serving_seam_is_refused_by_name() {
    struct Bare;
    impl WorkPort for Bare {
        fn install_cleanup_progress(&mut self) -> Result<(), StepError> {
            Ok(())
        }
        fn start_serving(&mut self, _: &Value) -> Result<Value, PortError> {
            Ok(json!({}))
        }
        fn workflow_state(&mut self) -> Result<Value, StepError> {
            Ok(
                json!({"state":"api_running","startup":{"startup_qualification_complete":true,
                "unavailable_deployment_count":0}}),
            )
        }
    }
    let fixture = fixture();
    let spec = &fixture["cases"]["ok-immediate"]["spec"];
    let rec = Arc::new(Mutex::new(Record::default()));
    let mut env = Env {
        rec: rec.clone(),
        stop: None,
        result: json!({}),
        progress: vec![],
        publishes: vec![],
    };
    let mut reports = Exchange {
        spec: spec.clone(),
        rec,
    };
    let mut state = state_of(spec);
    let Err(StepError::Refused(e)) =
        run_serving_step(&mut env, &mut reports, &mut Bare, &mut state, 0.5)
    else {
        panic!("refusal expected");
    };
    assert_eq!(e.code, owner_run::steps::NOT_PORTED_CODE);
    assert_eq!(env.result, json!({}));
}
