// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-RUN-25. The real owner-run step -> adapter -> runtime observation context;
//! only signed-session/mailbox IO and report/time inputs are scripted. No physical native proof.
use super::*;
#[path = "observe_runner.rs"]
mod runner_composition;
use owner_run::{
    chain::{PortError, StepEnv, StepError},
    machine::Reason,
    state::RunState,
    steps::{
        exchange::{Announcement, ClockSkew, MemberRow, ReportsPort},
        observe::{ObservePort, run_observe_step},
    },
};
use runtime::observe::ObservationContext;
use runtime::observe_owner::{ObservationRebuild, ObserveAdapter};
use std::collections::BTreeMap;

impl ObservationRebuild for Host {
    fn rebuild_observation(
        &mut self,
        _context: &mut Option<ObservationContext>,
        _run: &str,
        _deployment: &str,
        _cards: &[Value],
    ) -> Result<String, PortError> {
        self.log.lock().unwrap().push("rebuild".into());
        Err(PortError::Failed {
            code: Some("rebuild_stop_superseded".into()),
            text: String::new(),
        })
    }
}
fn cards() -> Vec<Value> {
    vec![
        json!({"statement":{"target_node_id":"n1"}}),
        json!({"statement":{"target_node_id":"n2"}}),
    ]
}
fn adapter(
    spec: &Value,
    log: &Log,
) -> ObserveAdapter<Host, impl FnMut() -> f64 + use<>, impl FnMut() -> String + use<>> {
    let start = fixture()["provenance"]["run_start_unix"].as_f64().unwrap();
    let reads = Arc::new(Mutex::new(0));
    let clock = clock_of(vec![json!(start + 5.0), json!(start + 6.0)], reads);
    let calls = log.clone();
    let token = move || {
        format!(
            "{:016x}",
            calls
                .lock()
                .unwrap()
                .iter()
                .filter(|c| c.starts_with("send:"))
                .count()
                + 1
        )
    };
    ObserveAdapter::new(new_host(spec, log), clock, token)
}

#[test]
fn adapter_preserves_executed_context_outcomes_and_cleanup_order() {
    let f = fixture();
    for case in f["cases"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["kind"] == "post")
    {
        let spec = &case["spec"];
        let log = Log::default();
        let mut port = adapter(spec, &log);
        port.prepare(RUN, DEP, &cards()).unwrap();
        assert_eq!(port.facts_nodes(), vec!["n1", "n2"]);
        assert_eq!(port.offers().len(), cards().len());
        log.lock().unwrap().clear();
        for (step, expected) in strings(spec.get("steps"))
            .iter()
            .zip(case["result"].as_array().unwrap())
        {
            let got = match step.as_str() {
                "collect_ok" | "collect_fail" => {
                    *port.host_mut().fail_send.lock().unwrap() =
                        (step == "collect_fail").then(|| "n2".into());
                    port.collect()
                }
                "retire" => port.retire_bootstrap(),
                _ => panic!("unknown oracle operation"),
            };
            match got {
                Ok(()) => assert!(expected.get("ok").is_some(), "{}: {expected}", spec["name"]),
                Err(PortError::Failed { code, text: _ }) => {
                    assert_eq!(json!(code), expected["error"], "{}", spec["name"])
                }
                Err(e) => panic!("unexpected error representation: {e:?}"),
            }
        }
        assert_eq!(
            json!(*log.lock().unwrap()),
            case["calls"],
            "{}",
            spec["name"]
        );
        assert_eq!(
            json!(port.context().unwrap().observations.is_some()),
            case["observations_set"]
        );
        if let Some(doc) = &port.context().unwrap().observations {
            let want = doc
                .observations
                .iter()
                .map(|o| Some(o.host_id.clone()))
                .collect::<Vec<_>>();
            assert_eq!(port.observed_hosts(), want);
        }
    }
}

#[test]
fn adapter_refuses_missing_context_and_does_not_replace_an_existing_run() {
    let oracle: Value = serde_json::from_str(include_str!(
        "../../fixtures/observe-adapter-admission-oracle.json"
    ))
    .unwrap();
    let code = |name: &str| {
        oracle["cases"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["name"] == name)
            .unwrap()["result"]["error"]
            .as_str()
            .unwrap()
    };
    let log = Log::default();
    let mut port = adapter(&json!({}), &log);
    assert_eq!(
        port.collect().unwrap_err().code_or("default"),
        code("missing_context")
    );
    assert_eq!(
        port.retire_bootstrap().unwrap_err().code_or("default"),
        code("missing_context")
    );
    port.prepare(RUN, DEP, &cards()).unwrap();
    let calls = log.lock().unwrap().clone();
    assert_eq!(
        port.prepare(RUN, "dep-2", &cards())
            .unwrap_err()
            .code_or("default"),
        code("existing_context")
    );
    assert_eq!(*log.lock().unwrap(), calls);
    assert_eq!(port.context().unwrap().deployment_id, DEP);
    assert_eq!(
        port.rebuild(RUN, "dep-2", &cards())
            .unwrap_err()
            .code_or("default"),
        "rebuild_stop_superseded"
    );
    assert_eq!(port.context().unwrap().deployment_id, DEP);
    port.host_mut().spec["role"] = json!("target");
    assert_eq!(
        port.prepare(RUN, DEP, &cards())
            .unwrap_err()
            .code_or("default"),
        code("nonowner_before_existing_context")
    );
}

#[derive(Default)]
struct Env {
    now: f64,
    progress: Vec<String>,
    results: BTreeMap<String, Value>,
}
impl StepEnv for Env {
    fn progress(&mut self, detail: &str) {
        self.progress.push(detail.into());
    }
    fn stopped(&self) -> bool {
        false
    }
    fn now(&mut self) -> f64 {
        self.now
    }
    fn wall(&mut self) -> f64 {
        fixture()["provenance"]["run_start_unix"].as_f64().unwrap() + self.now
    }
    fn set_result(&mut self, key: &str, value: Value) {
        self.results.insert(key.into(), value);
    }
    fn record_exception(&mut self, code: &str, class: &str) {
        panic!("unexpected exception {code}/{class}");
    }
    fn pause(&mut self, seconds: f64) -> bool {
        self.now += seconds;
        false
    }
}
#[derive(Default)]
struct Reports {
    announcements: Vec<Announcement>,
}
impl ReportsPort for Reports {
    fn forget_reports(&mut self) {
        panic!("observe must not forget reports");
    }
    fn announce(&mut self, a: &Announcement) -> Result<(), StepError> {
        self.announcements.push(a.clone());
        Ok(())
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        Ok(["n1", "n2"]
            .into_iter()
            .map(|n| {
                (
                    n.into(),
                    json!({"run_id":RUN,"deployment_id":DEP,"state":"bootstrap_running"}),
                )
            })
            .collect())
    }
    fn members(&mut self) -> Vec<MemberRow> {
        Vec::new()
    }
    fn own_node_id(&mut self) -> Option<String> {
        None
    }
    fn member_identity(&mut self, _node: &str) -> Value {
        Value::Null
    }
    fn clock_skew(&mut self, _nodes: &[String]) -> Result<Option<ClockSkew>, StepError> {
        Ok(None)
    }
    fn tick_local(&mut self) {}
}

#[test]
fn owner_run_step_collects_and_retires_the_real_runtime_context() {
    let log = Log::default();
    let mut port = adapter(&json!({}), &log);
    let mut state = RunState::new(RUN, DEP, Reason::Start);
    state.descriptors = cards();
    state.model = Some(owner_run::steps::model::ModelStepOutput {
            forced_slice: false,
        model_id: "model".into(),
        resolved_commit: "revision".into(),
        slice_mode: false,
        manifest_digest: DEFAULT_DIGEST.into(),
        choice: None,
        prepared: owner_run::steps::model::PreparedSource {
            model: owner_run::steps::model::PreparedModel {
                model_id: "model".into(),
                resolved_commit: "revision".into(),
                architecture: "qwen".into(),
                label: "model".into(),
                choice: None,
                shape_registered: false,
                owner_storage_need: 0,
            },
            manifest_digest: DEFAULT_DIGEST.into(),
            source_bytes: json!(0),
        },
    });
    let mut env = Env::default();
    let mut reports = Reports::default();
    let interval = budgets::BudgetId::OwnerPoll
        .span(budgets::Entries::ZERO)
        .unwrap()
        .millis() as f64
        / 1000.0;
    run_observe_step(&mut env, &mut reports, &mut port, &mut state, interval).unwrap();
    assert_eq!(state.participants, vec!["n1", "n2"]);
    assert_eq!(reports.announcements.len(), 1);
    assert_eq!(
        reports.announcements[0].per_node,
        Some(
            port.offers()
                .into_iter()
                .map(|(n, o)| (n, json!({"bootstrap_offer_id":o})))
                .collect()
        )
    );
    let f = fixture();
    let case = f["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["kind"] == "post" && c["spec"]["steps"] == json!(["collect_ok", "retire"]))
        .unwrap();
    let calls = log.lock().unwrap();
    let first = calls.iter().position(|c| c.starts_with("open:")).unwrap();
    assert_eq!(json!(&calls[first..]), case["calls"]);
    assert!(
        env.progress
            .iter()
            .any(|p| p == "2 Macs observed, 2 distinct hosts")
    );
    assert!(port.context().unwrap().observations.is_some());
}

#[test]
fn adapter_can_move_to_the_owner_run_thread_with_its_collected_context() {
    let log = Log::default();
    let mut port = adapter(&json!({}), &log);
    port.prepare(RUN, DEP, &cards()).unwrap();
    let handle = std::thread::spawn(move || {
        port.collect().unwrap();
        port.retire_bootstrap().unwrap();
        port
    });
    let port = handle.join().unwrap();
    assert!(port.context().unwrap().observations.is_some());
}
