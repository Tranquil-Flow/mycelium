// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XB patch 4 (REVIEW XB3-02): which joined Mac's named reach refusal may end the owner's
//! steps before compose. The SHARED owner steps run here as they do in a run (`run_descriptors_step`,
//! `run_compile_step`) under the real [`ReachWatch`] (`watched_step`, what `Composed::run_step`
//! calls); only the exchange (reports, members) and the compile's host side are test doubles.
//! Model-free, no network.
use super::*;
use owner_run::steps::compile::{BuildPoll, CompileContext, CompileInputs, CompilePort};
use owner_run::steps::exchange::{Announcement, ClockSkew, MemberRow, ReportsPort};
use owner_run::steps::model::{ModelStepOutput, PreparedModel, PreparedSource};
use owner_run::steps::observe::ObservePort;
use std::{cell::RefCell, collections::BTreeMap, rc::Rc};

const RUN: &str = "run-xb4";
const DEPLOYMENT: &str = "deployment-xb4";
/// The owner's own contributor (the run's `local_node`).
const OWNER: &str = "owner-mac-local-contributor";
/// The joined Mac the run selects.
const JOINED: &str = "joined-mac-selected-b";
/// An enrolled Mac that is not one of this run's participants (it came online after collect
/// began, or the collect did not select it), yet reports into the same exchange for this run.
const LATE: &str = "joined-mac-late-c";

type Reports = Rc<RefCell<BTreeMap<String, Value>>>;

/// The run's exchange: every report it holds (keyed by node) and the live joined members.
/// `arrives` lands in it at the step's first local tick (`tick_local` is the first call of a
/// progress wait's predicate, right after the wait's Stop check).
struct Exchange {
    reports: Reports,
    members: Vec<String>,
    arrives: Option<(String, Value)>,
}
impl ReportsPort for Exchange {
    fn forget_reports(&mut self) {}
    fn announce(&mut self, _: &Announcement) -> Result<(), StepError> {
        Ok(())
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        Ok(self.reports.borrow().clone())
    }
    fn members(&mut self) -> Vec<MemberRow> {
        self.members
            .iter()
            .map(|node| MemberRow {
                node_id: node.clone(),
                lifecycle_state: None,
                lease_expires_at: Some(1e12),
            })
            .collect()
    }
    fn own_node_id(&mut self) -> Option<String> {
        Some("owner-authority-node".into())
    }
    fn member_identity(&mut self, _: &str) -> Value {
        json!([])
    }
    fn clock_skew(&mut self, _: &[String]) -> Result<Option<ClockSkew>, StepError> {
        Ok(None)
    }
    fn tick_local(&mut self) {
        if let Some((node, report)) = self.arrives.take() {
            self.reports.borrow_mut().insert(node, report);
        }
    }
}

/// The run's environment: a clock that advances by each pause, never stopped by the owner.
#[derive(Default)]
struct Env {
    now: f64,
    pauses: usize,
}
impl StepEnv for Env {
    fn progress(&mut self, _: &str) {}
    fn stopped(&self) -> bool {
        false
    }
    fn now(&mut self) -> f64 {
        self.now
    }
    fn wall(&mut self) -> f64 {
        1_000.0 + self.now
    }
    fn set_result(&mut self, _: &str, _: Value) {}
    fn record_exception(&mut self, _: &str, _: &str) {}
    fn pause(&mut self, seconds: f64) -> bool {
        self.now += seconds;
        self.pauses += 1;
        false
    }
}

fn state() -> RunState {
    let mut state = RunState::new(RUN, DEPLOYMENT, owner_run::machine::Reason::Start);
    state.local_node = Some(OWNER.into());
    let model = PreparedModel {
        model_id: "Qwen/Qwen3-0.6B".into(),
        resolved_commit: "c".repeat(40),
        architecture: "qwen3".into(),
        label: "Qwen3-0.6B".into(),
        choice: None,
        shape_registered: true,
        owner_storage_need: 1,
    };
    state.model = Some(ModelStepOutput {
        prepared: PreparedSource {
            model,
            manifest_digest: "d".repeat(64),
            source_bytes: json!(1),
        },
        slice_mode: false,
        forced_slice: false,
        manifest_digest: "d".repeat(64),
        choice: None,
        model_id: "Qwen/Qwen3-0.6B".into(),
        resolved_commit: "c".repeat(40),
    });
    state
}

/// A Mac's report with a signed descriptor for this run (it is ready to be selected).
fn ready(node: &str) -> Value {
    json!({"run_id": RUN, "deployment_id": DEPLOYMENT, "state": "ready", "detail": null,
        "descriptor": {"statement": {"target_node_id": node, "expires_at": 1e12}}})
}
/// A joined Mac's named reach refusal (what `refusal_report` sends), for `run` / `deployment`.
fn refusal(run: &str, deployment: Value) -> Value {
    json!({"run_id": run, "deployment_id": deployment, "state": "error",
        "detail": "work_network_unavailable", "descriptor": null, "progress": null})
}

fn code(outcome: &Result<(), StepError>) -> Option<String> {
    match outcome {
        Err(StepError::Refused(e)) => Some(e.code.clone()),
        _ => None,
    }
}

/// The real collect step (`descriptors`) of a run under the real watch, as `Composed::run_step`
/// runs it: the run state's participants when the step starts (none: a new run).
fn collect(reports: &Reports, members: &[&str]) -> (Result<(), StepError>, RunState, Env) {
    let mut exchange = Exchange {
        reports: reports.clone(),
        members: members.iter().map(|m| (*m).to_owned()).collect(),
        arrives: None,
    };
    let mut state = state();
    let mut env = Env::default();
    let read = || Value::Object(reports.borrow().clone().into_iter().collect());
    let start = state.participants.clone();
    let outcome = watched_step(
        &mut env,
        &read,
        (RUN.into(), DEPLOYMENT.into()),
        start,
        |watch| {
            owner_run::steps::descriptors::run_descriptors_step(
                watch,
                &mut exchange,
                &mut state,
                1.0,
            )
        },
    );
    (outcome, state, env)
}

/// REVIEW XB3-02: A + B collect while C (enrolled, online only after the collect's candidate set
/// was taken) reports this run's reach refusal. C is not a participant: the collect selects A and
/// B and the run goes on. Patch 3 read every report in the exchange and ended the run by C's name.
#[test]
fn an_unselected_macs_reach_refusal_does_not_stop_the_run() {
    let reports: Reports = Rc::default();
    reports.borrow_mut().insert(OWNER.into(), ready(OWNER));
    reports.borrow_mut().insert(JOINED.into(), ready(JOINED));
    reports
        .borrow_mut()
        .insert(LATE.into(), refusal(RUN, json!(DEPLOYMENT)));
    let (outcome, state, _) = collect(&reports, &[JOINED]);
    assert!(
        outcome.is_ok(),
        "an unselected Mac's refusal stopped the run: {outcome:?}"
    );
    assert_eq!(
        state.participants,
        vec![JOINED.to_owned(), OWNER.to_owned()]
    );
}

/// The intended joined Mac (a collect candidate) still ends the collect promptly by name.
#[test]
fn a_participants_reach_refusal_still_ends_the_collect_promptly_by_name() {
    let reports: Reports = Rc::default();
    reports.borrow_mut().insert(OWNER.into(), ready(OWNER));
    reports
        .borrow_mut()
        .insert(JOINED.into(), refusal(RUN, json!(DEPLOYMENT)));
    let (outcome, _, env) = collect(&reports, &[JOINED]);
    assert_eq!(
        code(&outcome).as_deref(),
        Some("joined_mac_network_refused"),
        "{outcome:?}"
    );
    assert!(
        env.now <= 2.0,
        "the refusal waited {} s of the collect",
        env.now
    );
}

/// Only exactly this run AND deployment count: no deployment (`null`), another deployment or
/// another run is not this run's refusal, even from a participant.
#[test]
fn only_a_participants_report_for_this_run_and_deployment_counts() {
    let participants = [OWNER.to_owned(), JOINED.to_owned()];
    let with = |report: Value| {
        let mut reports = serde_json::Map::new();
        reports.insert(JOINED.into(), report);
        member_reach_refusal_in(&Value::Object(reports), RUN, DEPLOYMENT, &participants)
    };
    assert!(with(refusal(RUN, json!(DEPLOYMENT))).is_some());
    assert!(with(refusal(RUN, Value::Null)).is_none(), "null deployment");
    assert!(
        with(refusal(RUN, json!("other"))).is_none(),
        "other deployment"
    );
    assert!(
        with(refusal("prior-run", json!(DEPLOYMENT))).is_none(),
        "prior run"
    );
    let mut only_late = serde_json::Map::new();
    only_late.insert(LATE.into(), refusal(RUN, json!(DEPLOYMENT)));
    assert!(
        member_reach_refusal_in(&Value::Object(only_late), RUN, DEPLOYMENT, &participants)
            .is_none(),
        "a non-participant"
    );
}

/// The compile step's host side: its source read is where the step consults Stop
/// (`compile_inputs_until`); the refusal named by `arrives` lands in the exchange just before.
struct Compile {
    reports: Reports,
    arrives: (String, Value),
}
impl CompilePort for Compile {
    fn compile_context(&mut self) -> Result<CompileContext, owner_run::chain::PortError> {
        Ok(CompileContext {
            facts: [
                (OWNER.to_owned(), json!({})),
                (JOINED.to_owned(), json!({})),
            ]
            .into_iter()
            .collect(),
            local: None,
            joined_reports: json!({}),
            now: 1_000.0,
        })
    }
    fn compile_inputs(&mut self) -> Result<CompileInputs, owner_run::chain::PortError> {
        unreachable!("the step reads its inputs through compile_inputs_until")
    }
    fn compile_inputs_until(
        &mut self,
        stopped: &dyn Fn() -> bool,
    ) -> Result<CompileInputs, owner_run::chain::PortError> {
        let (node, report) = self.arrives.clone();
        self.reports.borrow_mut().insert(node, report);
        Err(owner_run::chain::PortError::Failed {
            code: Some(
                if stopped() {
                    "stopped"
                } else {
                    "test_compile_inputs_end"
                }
                .into(),
            ),
            text: String::new(),
        })
    }
    fn retire_bootstrap(&mut self) -> Result<(), owner_run::chain::PortError> {
        unreachable!("the test ends at the source read")
    }
    fn start_build(&mut self, _: &planner::chain_compile::BuildRequest) {
        unreachable!("the test ends at the source read")
    }
    fn poll_build(&mut self) -> BuildPoll {
        unreachable!("the test ends at the source read")
    }
}

/// The real compile step of a run whose participants are the owner and the joined Mac, under the
/// real watch; `arrives` reports during the step's source read.
fn compile(arrives: (&str, Value)) -> Result<(), StepError> {
    let reports: Reports = Rc::default();
    let mut exchange = Exchange {
        reports: reports.clone(),
        members: vec![JOINED.into()],
        arrives: None,
    };
    let mut port = Compile {
        reports: reports.clone(),
        arrives: (arrives.0.to_owned(), arrives.1),
    };
    let mut state = state();
    state.participants = vec![JOINED.into(), OWNER.into()];
    let mut env = Env::default();
    let read = || Value::Object(reports.borrow().clone().into_iter().collect());
    let start = state.participants.clone();
    watched_step(
        &mut env,
        &read,
        (RUN.into(), DEPLOYMENT.into()),
        start,
        |watch| {
            owner_run::steps::compile::run_compile_step(
                watch,
                &mut exchange,
                &mut port,
                &mut state,
                1.0,
            )
        },
    )
}

/// Compile phase: the selected joined Mac's refusal ends the step by name; the same refusal from a
/// Mac outside the run leaves the step's own outcome untouched.
#[test]
fn in_compile_only_a_selected_participants_refusal_is_named() {
    let selected = compile((JOINED, refusal(RUN, json!(DEPLOYMENT))));
    assert_eq!(
        code(&selected).as_deref(),
        Some("joined_mac_network_refused"),
        "{selected:?}"
    );
    let outsider = compile((LATE, refusal(RUN, json!(DEPLOYMENT))));
    assert!(
        !code(&outsider).is_some_and(|c| c.starts_with("joined_mac_")),
        "an unselected Mac's refusal named the compile's failure: {outsider:?}"
    );
}

/// The observation context of a run whose facts nodes are the owner and the joined Mac.
struct Observe;
impl ObservePort for Observe {
    fn prepare(
        &mut self,
        _: &str,
        _: &str,
        _: &[Value],
    ) -> Result<(), owner_run::chain::PortError> {
        Ok(())
    }
    fn rebuild(
        &mut self,
        _: &str,
        _: &str,
        _: &[Value],
    ) -> Result<String, owner_run::chain::PortError> {
        unreachable!("a Start run never rebuilds")
    }
    fn facts_nodes(&mut self) -> Vec<String> {
        vec![JOINED.into(), OWNER.into()]
    }
    fn offers(&mut self) -> Vec<(String, String)> {
        vec![(JOINED.into(), "offer-b".into())]
    }
    fn collect(&mut self) -> Result<(), owner_run::chain::PortError> {
        unreachable!("the test ends in the bootstrap wait")
    }
    fn observed_hosts(&mut self) -> Vec<Option<String>> {
        unreachable!("the test ends in the bootstrap wait")
    }
    fn retire_bootstrap(&mut self) -> Result<(), owner_run::chain::PortError> {
        unreachable!("the test ends in the bootstrap wait")
    }
}

/// REVIEW XB3-04: in observe the selected joined Mac's refusal arrives AFTER the wait's Stop check
/// and before the step reads the reports (`all_in` -> `raise_member_refusal`), so the shared step
/// itself returns the joined Mac's raw code with no further check of the watch. The owner still
/// gets the joined Mac's named refusal, not the raw `work_network_unavailable`.
#[test]
fn a_refusal_that_observe_returns_itself_is_still_named() {
    let reports: Reports = Rc::default();
    let mut exchange = Exchange {
        reports: reports.clone(),
        members: vec![JOINED.into()],
        arrives: Some((JOINED.into(), refusal(RUN, json!(DEPLOYMENT)))),
    };
    let mut state = state();
    state.participants = vec![JOINED.into(), OWNER.into()];
    let mut env = Env::default();
    let read = || Value::Object(reports.borrow().clone().into_iter().collect());
    let start = state.participants.clone();
    let outcome = watched_step(
        &mut env,
        &read,
        (RUN.into(), DEPLOYMENT.into()),
        start,
        |watch| {
            owner_run::steps::observe::run_observe_step(
                watch,
                &mut exchange,
                &mut Observe,
                &mut state,
                1.0,
            )
        },
    );
    assert_eq!(
        code(&outcome).as_deref(),
        Some("joined_mac_network_refused"),
        "{outcome:?}"
    );
    assert_eq!(env.pauses, 0, "the refusal came from the step's own read");
}

/// The same raw outcome from a Mac outside the run is not this run's refusal: unchanged. (A
/// shared step only returns a participant's code; this pins that the re-check is scoped too.)
#[test]
fn a_raw_member_code_without_a_participants_report_passes_unchanged() {
    let reports: Reports = Rc::default();
    reports
        .borrow_mut()
        .insert(LATE.into(), refusal(RUN, json!(DEPLOYMENT)));
    let mut env = Env::default();
    let read = || Value::Object(reports.borrow().clone().into_iter().collect());
    let outcome = watched_step(
        &mut env,
        &read,
        (RUN.into(), DEPLOYMENT.into()),
        vec![JOINED.into(), OWNER.into()],
        |_| Err(owner_run::OwnerRunError::new("work_network_unavailable").into()),
    );
    assert_eq!(code(&outcome).as_deref(), Some("work_network_unavailable"));
    let stopped = watched_step(
        &mut env,
        &read,
        (RUN.into(), DEPLOYMENT.into()),
        vec![LATE.into()],
        |_| Err(StepError::Stopped),
    );
    assert!(
        matches!(stopped, Err(StepError::Stopped)),
        "a genuine Stop stays a Stop: {stopped:?}"
    );
}
