// SPDX-License-Identifier: AGPL-3.0-only
//! Executed activation step over labelled host IO. ChainOps scheduling witnesses
//! in the fixture are source evidence only, not Rust host/native qualification.
use owner_run::chain::{PortError, StepEnv, StepError};
use owner_run::machine::{OwnerRunError, Reason};
use owner_run::state::RunState;
use owner_run::steps::activate::{ActivationPort, run_activate_step};
use serde_json::{Value, json};

#[derive(Default)]
struct Env {
    progress: Vec<String>,
}
impl StepEnv for Env {
    fn progress(&mut self, s: &str) {
        self.progress.push(s.into());
    }
    fn stopped(&self) -> bool {
        panic!("activation never polls Stop");
    }
    fn now(&mut self) -> f64 {
        panic!("activation never reads clock");
    }
    fn wall(&mut self) -> f64 {
        panic!("activation never reads wall clock");
    }
    fn set_result(&mut self, _: &str, _: Value) {
        panic!("no activation result");
    }
    fn record_exception(&mut self, _: &str, _: &str) {
        panic!("no activation diagnostic");
    }
    fn pause(&mut self, _: f64) -> bool {
        panic!("no activation wait");
    }
}
struct Port {
    spec: Value,
    calls: Vec<Value>,
}
impl ActivationPort for Port {
    fn activate(&mut self, proposal: &Value) -> Result<(), PortError> {
        self.calls.push(json!(["activate", proposal]));
        if let Some(e) = self.spec.get("error") {
            return Err(if e["kind"] == "refused" {
                PortError::Refused(OwnerRunError::with(
                    e["code"].as_str().unwrap(),
                    Some("private message"),
                    Some(e["fix"].as_object().unwrap().clone()),
                ))
            } else {
                PortError::Failed {
                    code: e["code"].as_str().map(str::to_owned),
                    text: e["text"].as_str().unwrap().into(),
                }
            });
        }
        // The host port intentionally drops ChainOps' return document; the
        // source step never interprets it as qualification or readiness.
        Ok(())
    }
}
#[test]
fn activation_step_matches_executed_oracle() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/oracle_activate_map_r18.json")).unwrap();
    let cases = fixture["cases"].as_object().unwrap();
    assert!(!cases.is_empty());
    for (name, case) in cases {
        let spec = &case["spec"];
        let mut state = RunState::new("run-1", "dep-1", Reason::Start);
        state.proposal = spec["state"].get("proposal").cloned();
        state.registry_origin = spec["state"].get("registry_origin").cloned();
        state.execution_clock_started_at = spec["state"]["execution_clock_started_at"].as_f64();
        let before = format!("{state:?}");
        let mut env = Env::default();
        let mut port = Port {
            spec: spec.clone(),
            calls: vec![],
        };
        let outcome = match run_activate_step(&mut env, &mut port, &state) {
            Ok(()) => json!({"kind":"ok"}),
            Err(StepError::Refused(e)) => {
                json!({"kind":"refused","code":e.code,"message":e.message,"fix":e.fix})
            }
            Err(StepError::Stopped) => json!({"kind":"stopped"}),
            Err(StepError::Exception { class }) => json!({"kind":"exception","class":class}),
        };
        assert_eq!(
            outcome, case["outcome"],
            "{name}: full outcome including privacy"
        );
        assert_eq!(
            json!(port.calls),
            case["calls"],
            "{name}: exact calls and opaque payload"
        );
        assert_eq!(
            json!(env.progress),
            case["progress"],
            "{name}: progress order"
        );
        assert_eq!(
            format!("{state:?}"),
            before,
            "{name}: no partial private-state mutation"
        );
        let mut actual = json!({});
        if let Some(p) = &state.proposal {
            actual["proposal"] = p.clone();
        }
        if let Some(p) = &state.registry_origin {
            actual["registry_origin"] = p.clone();
        }
        if let Some(p) = state.execution_clock_started_at {
            actual["execution_clock_started_at"] = json!(p);
        }
        assert_eq!(actual, case["state"], "{name}: complete source state");
        assert_eq!(
            case["result"],
            json!({}),
            "{name}: oracle publishes no result"
        );
        println!("ORACLE-ACTIVATE-R17 {name}");
    }
}
