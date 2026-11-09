// SPDX-License-Identifier: AGPL-3.0-only
//! The `compile` step (`demo_chain.py:792-850 @ f4e366e9`) against EXECUTED v0.1.4: FX-L7-chain-compile-r11
//! (907 cases recorded by L7 with `round11-capture-chain-compile.py`: the real `_step_compile` ->
//! `member_capability_records` -> `start_compile` incl. its compile thread -> ceiling admission -> wall
//! limits, on a stub app; fixture `crates/planner/tests/fixtures/chain-compile.json`). Here the whole step
//! runs over [`CompilePort`] with the fleet's recorded inputs, the planner behind it and owner-run's own
//! ceiling comparator; compared per case: outcome (code, message), progress lines, announcements,
//! retire/build calls, the builder request, `_member_capability_reports`, `_chain_compile_state`, and
//! every `ctx.state` / `ctx.result` field the step writes. Plus the two outcomes of the wait that the
//! done-when names: `execution_proposal_outside_ceiling` and `chain_compile_timeout`.
mod support;
use owner_run::chain::StepError;
use owner_run::machine::Reason;
use owner_run::state::RunState;
use owner_run::steps::compile::run_compile_step;
use owner_run::steps::exchange::{Announcement, ClockSkew, MemberRow, ReportsPort};
use owner_run::steps::model::{ModelStepOutput, PreparedModel, PreparedSource};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use support::compile_double::{Port, inputs_of};
use support::vworld::{VEnv, World};

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../planner/tests/fixtures/chain-compile.json"
    ))
    .expect("fixture parses")
}

struct Exchange {
    announced: Vec<String>,
}
impl ReportsPort for Exchange {
    fn forget_reports(&mut self) {}
    fn announce(&mut self, a: &Announcement) -> Result<(), StepError> {
        self.announced.push(a.phase.clone());
        Ok(())
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        Ok(BTreeMap::new())
    }
    fn members(&mut self) -> Vec<MemberRow> {
        Vec::new()
    }
    fn own_node_id(&mut self) -> Option<String> {
        None
    }
    fn member_identity(&mut self, _: &str) -> Value {
        Value::Null
    }
    fn clock_skew(&mut self, _: &[String]) -> Result<Option<ClockSkew>, StepError> {
        Ok(None)
    }
    fn tick_local(&mut self) {}
}

fn state_of(i: &Value) -> RunState {
    let mut st = RunState::new("run-1", i["deployment_id"].as_str().unwrap(), Reason::Start);
    st.local_node = i["local_node"].as_str().map(str::to_owned);
    st.execution_ceiling = (!i["ceiling"].is_null()).then(|| i["ceiling"].clone());
    let p = &i["prepared"];
    let model = PreparedModel {
        model_id: p["model_id"].as_str().unwrap().into(),
        resolved_commit: p["resolved_commit"].as_str().unwrap().into(),
        architecture: String::new(),
        label: String::new(),
        choice: None,
        shape_registered: true,
        owner_storage_need: 0,
    };
    let digest: String = p["manifest_digest"].as_str().unwrap().into();
    st.model = Some(ModelStepOutput {
        prepared: PreparedSource {
            model: model.clone(),
            manifest_digest: digest.clone(),
            source_bytes: p["source_bytes"].clone(),
        },
        slice_mode: i["slice_mode"].as_bool().unwrap(),
        manifest_digest: digest,
        choice: None,
        model_id: model.model_id,
        resolved_commit: model.resolved_commit,
    });
    st
}

fn interval() -> f64 {
    use budgets::{BudgetId, Entries};
    BudgetId::OwnerPoll
        .span(Entries::ZERO)
        .map_or(f64::NAN, |s| s.millis() as f64 / 1000.0)
}

/// Runs the step for one recorded case; `Err` describes the first difference from the oracle.
fn replay(name: &str, rec: &Value, delay: usize) -> Result<(), String> {
    let i = &rec["inputs"];
    let world = World::new(&json!({}));
    let mut env = VEnv::new(&world);
    let mut exchange = Exchange { announced: vec![] };
    let mut port = Port {
        inputs: inputs_of(i),
        adapter: i["adapter"].clone(),
        slice_mode: i["slice_mode"].as_bool().unwrap(),
        delay,
        polls: 0,
        retired: 0,
        requests: vec![],
    };
    let mut state = state_of(i);
    let result = run_compile_step(&mut env, &mut exchange, &mut port, &mut state, interval());
    let outcome = match &result {
        Ok(()) => json!({"ok": true}),
        Err(StepError::Refused(e)) => {
            json!({"refused": {"code": e.code, "message": e.message, "fix": e.fix}})
        }
        Err(other) => return Err(format!("{name}: unexpected {other:?}")),
    };
    let ok = outcome.get("ok").is_some();
    let layers: Value = state
        .layers
        .iter()
        .map(|(k, v)| (k.clone(), json!([v[0], v[1]])))
        .collect::<serde_json::Map<_, _>>()
        .into();
    // a failed admission leaves `ctx.state` untouched except for `proposal` after member_wall_limit_invalid
    let want_ctx = |k: &str| {
        if ok {
            rec["ctx_state"][k].clone()
        } else {
            Value::Null
        }
    };
    let got = json!({
        "outcome": outcome,
        "progress": env.progress,
        "announce": exchange.announced,
        "retire_calls": port.retired,
        "build_calls": port.requests.len(),
        "records": state.member_records.clone().unwrap_or(Value::Null),
        "compile_state": state.chain_compile_state.clone().unwrap_or(Value::Null),
        "published": env.publishes,
    });
    // a blocked builder is released only after the step gave up: the recorded state is the later one
    let compiled =
        rec["compile_state"]["state"] == "compiled" && i["adapter"]["block_build"] != true;
    let want = json!({
        "outcome": rec["outcome"],
        "progress": rec["progress"],
        "announce": rec["announce"],
        "retire_calls": rec["retire_calls"],
        "build_calls": rec["build_calls"].as_array().unwrap().len(),
        "records": rec["records"],
        // the step keeps the compile state only once the compile finished
        "compile_state": if compiled { rec["compile_state"].clone() } else { Value::Null },
        "published": rec["published"],
    });
    for key in [
        "outcome",
        "progress",
        "announce",
        "retire_calls",
        "build_calls",
        "records",
        "compile_state",
        "published",
    ] {
        if got[key] != want[key] {
            let clip = |v: &Value| v.to_string().chars().take(700).collect::<String>();
            return Err(format!(
                "{name}: {key} differs\n rust   {}\n oracle {}",
                clip(&got[key]),
                clip(&want[key])
            ));
        }
    }
    if ok {
        let got = json!({
            "proposal": state.proposal, "worker_wall_limits": state.worker_wall_limits,
            "wall_cap_fallback_nodes": state.wall_cap_fallback_nodes, "compile_state": state.compile_state,
            "layers": layers,
            "cache_hit": env.results.get("compilation_cache_hit"),
            "build_seconds": env.results.get("build_seconds"),
        });
        let want = json!({
            "proposal": want_ctx("proposal"), "worker_wall_limits": want_ctx("worker_wall_limits"),
            "wall_cap_fallback_nodes": want_ctx("wall_cap_fallback_nodes"), "compile_state": want_ctx("compile_state"),
            "layers": want_ctx("layers"),
            "cache_hit": rec["ctx_result"]["compilation_cache_hit"],
            "build_seconds": rec["ctx_result"]["build_seconds"],
        });
        if got != want {
            return Err(format!(
                "{name}: run context differs\n rust   {got}\n oracle {want}"
            ));
        }
    }
    Ok(())
}

fn group(label: &str, pick: impl Fn(&str, &Value) -> bool, at_least: usize, delay: usize) -> usize {
    let doc = fixture();
    let mut n = 0;
    let mut bad = Vec::new();
    for (name, rec) in doc["cases"].as_object().unwrap() {
        if !pick(name, rec) {
            continue;
        }
        n += 1;
        if let Err(e) = replay(name, rec, delay) {
            bad.push(e);
        }
    }
    assert!(n >= at_least, "{label}: only {n} cases (need {at_least})");
    assert!(
        bad.is_empty(),
        "{label}: {} of {n} cases differ from the oracle; first 3:\n{}",
        bad.len(),
        bad.iter().take(3).cloned().collect::<Vec<_>>().join("\n")
    );
    n
}

/// Proves: B-RUN-24 (the route is compiled from exactly the members that reported in; every refusal of the
/// preparation keeps its closed code and message; a failed build ends the run with `chain_compile_failed`),
/// B-CONSENT-07, B-CONSENT-08, B-CONSENT-09 (per-worker wall limits: unknown member 60 s, the owner's own
/// Mac its proposal, min(proposal, member cap)), B-PLAN-09, B-PLAN-10.
#[test]
fn the_compile_step_equals_the_oracle_for_all_907_recorded_cases() {
    let n = group("all", |_, _| true, 900, 0);
    assert_eq!(n, 907);
}

/// R34 paired original `_step_compile` trace: records -> announce -> one
/// start_compile refusal. Scripted inputs from the existing executed corpus;
/// no builder output, host identity or native success is fabricated here.
#[test]
fn compile_source_refusal_follows_announcement_without_a_second_input_read() {
    use owner_run::chain::PortError;
    use owner_run::steps::compile::{BuildPoll, CompileContext, CompileInputs, CompilePort};
    use planner::chain_compile::BuildRequest;
    use std::{cell::RefCell, rc::Rc};
    struct Refusing {
        input: CompileInputs,
        calls: Rc<RefCell<Vec<&'static str>>>,
    }
    impl CompilePort for Refusing {
        fn compile_context(&mut self) -> Result<CompileContext, PortError> {
            self.calls.borrow_mut().push("records");
            Ok(CompileContext {
                facts: self.input.facts.clone(),
                local: self.input.local.clone(),
                joined_reports: self.input.joined_reports.clone(),
                now: self.input.now,
            })
        }
        fn compile_inputs(&mut self) -> Result<CompileInputs, PortError> {
            self.calls.borrow_mut().push("inputs");
            Err(PortError::Failed {
                code: Some("source_not_prepared".into()),
                text: String::new(),
            })
        }
        fn retire_bootstrap(&mut self) -> Result<(), PortError> {
            panic!("source refusal must retain bootstrap")
        }
        fn start_build(&mut self, _: &BuildRequest) {
            panic!("source refusal must not start builder")
        }
        fn poll_build(&mut self) -> BuildPoll {
            panic!("source refusal must not poll")
        }
    }
    struct Announcing(Rc<RefCell<Vec<&'static str>>>);
    impl ReportsPort for Announcing {
        fn announce(&mut self, a: &Announcement) -> Result<(), StepError> {
            assert_eq!(a.phase, "compile");
            self.0.borrow_mut().push("announce:compile");
            Ok(())
        }
        fn forget_reports(&mut self) {
            panic!("unexpected forget")
        }
        fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
            panic!("unexpected reports")
        }
        fn members(&mut self) -> Vec<MemberRow> {
            panic!("unexpected members")
        }
        fn own_node_id(&mut self) -> Option<String> {
            panic!("unexpected own node")
        }
        fn member_identity(&mut self, _: &str) -> Value {
            panic!("unexpected identity")
        }
        fn clock_skew(&mut self, _: &[String]) -> Result<Option<ClockSkew>, StepError> {
            panic!("unexpected skew")
        }
        fn tick_local(&mut self) {
            panic!("unexpected tick")
        }
    }
    let fixture = fixture();
    let rec = &fixture["cases"]["build-never-finishes-timeout"];
    let calls = Rc::new(RefCell::new(Vec::new()));
    let mut port = Refusing {
        input: inputs_of(&rec["inputs"]),
        calls: calls.clone(),
    };
    let mut state = state_of(&rec["inputs"]);
    let world = World::new(&json!({}));
    let mut env = VEnv::new(&world);
    let result = run_compile_step(
        &mut env,
        &mut Announcing(calls.clone()),
        &mut port,
        &mut state,
        interval(),
    );
    assert!(matches!(result, Err(StepError::Refused(ref e)) if e.code == "source_not_prepared"));
    assert_eq!(*calls.borrow(), ["records", "announce:compile", "inputs"]);
    assert!(state.member_records.is_some());
    assert!(state.chain_compile_state.is_none());
    assert!(env.progress.is_empty());
}

/// The same 907 cases with the builder thread still running for the first polls of the step's wait.
#[test]
fn a_builder_that_finishes_a_few_polls_later_changes_nothing() {
    group(
        "delayed",
        |_, rec| rec["inputs"]["adapter"]["block_build"] != true,
        800,
        3,
    );
}

/// Proves: B-CONSENT-16 (a compiled proposal outside the owner's execution ceiling fails with
/// `execution_proposal_outside_ceiling` before any work is published; edges: each limit at, one above and
/// one below the compiled value).
#[test]
fn a_proposal_outside_the_ceiling_is_refused_before_publish() {
    let n = group("ceiling", |name, _| name.starts_with("ceiling-"), 39, 0);
    assert_eq!(n, 39);
    let doc = fixture();
    let refused = doc["cases"]
        .as_object()
        .unwrap()
        .iter()
        .filter(|(n, r)| {
            n.starts_with("ceiling-")
                && r["outcome"]["refused"]["code"] == "execution_proposal_outside_ceiling"
        })
        .count();
    assert!(
        refused >= 20,
        "only {refused} ceiling refusals in the fixture"
    );
}

/// Proves: B-RUN-07 (`chain_compile_timeout`: a builder that never finishes ends the compile step after the
/// Compile budget, 3,600 s, with the bare code; polled every OwnerPoll, 500 ms).
#[test]
fn a_build_that_never_finishes_times_out_after_the_compile_budget() {
    let doc = fixture();
    let rec = &doc["cases"]["build-never-finishes-timeout"];
    replay("build-never-finishes-timeout", rec, 0).expect("equals the oracle");
    // the virtual time it took: 3,600 s of polls (the wait's own clock), not a literal in the step
    let world = World::new(&json!({}));
    let mut env = VEnv::new(&world);
    let i = &rec["inputs"];
    let mut port = Port {
        inputs: inputs_of(i),
        adapter: i["adapter"].clone(),
        slice_mode: false,
        delay: 0,
        polls: 0,
        retired: 0,
        requests: vec![],
    };
    let mut state = state_of(i);
    let mut exchange = Exchange { announced: vec![] };
    let result = run_compile_step(&mut env, &mut exchange, &mut port, &mut state, interval());
    let Err(StepError::Refused(e)) = result else {
        panic!("a timeout is a refusal");
    };
    assert_eq!(
        (e.code.as_str(), e.message.as_deref()),
        ("chain_compile_timeout", None)
    );
    assert!(
        (world.t.get() - 3600.0).abs() <= interval(),
        "the budget is 3600 s: ended at {}",
        world.t.get()
    );
}

/// Proves: B-RUN-04 (a Stop during the compile wait ends it as Stopped, never as a timeout or a failure).
#[test]
fn a_stop_during_the_compile_wait_is_stopped() {
    let doc = fixture();
    let rec = &doc["cases"]["build-never-finishes-timeout"];
    let world = World::new(&json!({"stop_at": 30.0}));
    let mut env = VEnv::new(&world);
    let i = &rec["inputs"];
    let mut port = Port {
        inputs: inputs_of(i),
        adapter: i["adapter"].clone(),
        slice_mode: false,
        delay: 0,
        polls: 0,
        retired: 0,
        requests: vec![],
    };
    let mut state = state_of(i);
    let mut exchange = Exchange { announced: vec![] };
    let result = run_compile_step(&mut env, &mut exchange, &mut port, &mut state, interval());
    assert!(matches!(result, Err(StepError::Stopped)), "{result:?}");
    assert!(world.t.get() >= 30.0 && world.t.get() < 31.0);
}

/// Proves: a Stop that arrives while the host is still reading the source for `compile_inputs` reaches
/// that read (one check per chunk) and ends the step as Stopped, exactly like a Stop during the compile
/// wait: no build request, the bootstrap kept, nothing compiled. The port below models a 100-chunk source
/// scan on the virtual clock (1 s per chunk); the old read hashes every chunk before it can return.
#[test]
fn a_stop_during_the_source_preflight_is_stopped_before_any_build() {
    use owner_run::chain::PortError;
    use owner_run::steps::compile::{BuildPoll, CompileContext, CompileInputs, CompilePort};
    use planner::chain_compile::BuildRequest;
    use std::rc::Rc;
    struct Preflight {
        port: Port,
        world: Rc<World>,
        chunks: u32,
    }
    impl Preflight {
        fn chunk(&self) {
            self.world.t.set(self.world.t.get() + 1.0);
        }
    }
    impl CompilePort for Preflight {
        fn compile_context(&mut self) -> Result<CompileContext, PortError> {
            self.port.compile_context()
        }
        fn compile_inputs(&mut self) -> Result<CompileInputs, PortError> {
            for _ in 0..self.chunks {
                self.chunk();
            }
            self.port.compile_inputs()
        }
        fn compile_inputs_until(
            &mut self,
            stopped: &dyn Fn() -> bool,
        ) -> Result<CompileInputs, PortError> {
            for _ in 0..self.chunks {
                if stopped() {
                    return Err(PortError::Failed {
                        code: Some("compiler_stopped".into()),
                        text: String::new(),
                    });
                }
                self.chunk();
            }
            self.port.compile_inputs()
        }
        fn retire_bootstrap(&mut self) -> Result<(), PortError> {
            self.port.retire_bootstrap()
        }
        fn start_build(&mut self, request: &BuildRequest) {
            self.port.start_build(request);
        }
        fn poll_build(&mut self) -> BuildPoll {
            self.port.poll_build()
        }
    }
    let doc = fixture();
    let rec = &doc["cases"]["build-never-finishes-timeout"];
    let world = World::new(&json!({"stop_at": 30.0}));
    let mut env = VEnv::new(&world);
    let i = &rec["inputs"];
    let mut port = Preflight {
        port: Port {
            inputs: inputs_of(i),
            adapter: i["adapter"].clone(),
            slice_mode: false,
            delay: 0,
            polls: 0,
            retired: 0,
            requests: vec![],
        },
        world: world.clone(),
        chunks: 100,
    };
    let mut state = state_of(i);
    let mut exchange = Exchange { announced: vec![] };
    let result = run_compile_step(&mut env, &mut exchange, &mut port, &mut state, interval());
    assert!(matches!(result, Err(StepError::Stopped)), "{result:?}");
    assert!(
        world.t.get() >= 30.0 && world.t.get() < 31.0,
        "the read stops within one chunk of the Stop: ended at {}",
        world.t.get()
    );
    assert!(port.port.requests.is_empty(), "no build after a Stop");
    assert_eq!(port.port.retired, 0, "the bootstrap is kept");
    assert!(state.chain_compile_state.is_none());
}
