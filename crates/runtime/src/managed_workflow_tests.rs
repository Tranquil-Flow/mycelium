// SPDX-License-Identifier: AGPL-3.0-only
// White-box checks inspect private metadata but obtain hooks only via Launch::run.
use super::*;
use budgets::table::{Provenance, Purpose, Value as BudgetValue};

#[test]
fn consumer_uses_exact_workflow206_count_policy_not_equal_valued_alias() {
    assert_eq!(
        VERIFIED_TOKEN_LIMIT.name(),
        "PyManagedWorkflowVerifiedChallengeTokenLimitL206C66",
        "wrong_verified_token_policy_identity"
    );
    let entry = VERIFIED_TOKEN_LIMIT.entry();
    assert_eq!(entry.value, BudgetValue::Count(budgets::Entries::new(4096)));
    assert_eq!(entry.purpose, Purpose::Policy);
    assert_eq!(
        entry.provenance,
        Provenance::Source("mycelium_a13_macos/workflow.py:206 @ f4e366e9")
    );
}
struct Collaborator;
impl Controller for Collaborator {
    fn run_identity(&self) -> Result<(String, String)> {
        Ok(("run".into(), "dep".into()))
    }
    fn prepare(&mut self, _: &mut dyn Authority, _: &[Value], _: &WorkflowView) -> Result<()> {
        Ok(())
    }
}
impl Authority for Collaborator {
    fn publish(
        &mut self,
        _: &mut dyn Controller,
        _: &str,
        _: &str,
        _: &WorkflowView,
    ) -> Result<Vec<Value>> {
        Ok(vec![])
    }
    fn request_cleanup(&mut self, _: &Value) -> Result<()> {
        Ok(())
    }
}
struct LaunchFixture {
    mode: &'static str,
}
impl Launch for LaunchFixture {
    fn validate_approval(&self, _: &Value) -> Result<()> {
        Ok(())
    }
    fn validate_port(&self, _: u16) -> Result<()> {
        Ok(())
    }
    fn run(
        &mut self,
        _: &Value,
        _: u16,
        v: &WorkflowView,
        h: &mut LaunchHooks<'_>,
    ) -> Result<Value> {
        let request = "startup-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        h.verified_challenge_event("dep", request, "begin", &json!(-1), &json!(3));
        h.verified_challenge_event("dep", request, "token", &json!(0), &json!(3));
        assert_eq!(v.inner.lock().unwrap().challenge.as_ref().unwrap().count, 1);
        match self.mode {
            "stop" => v.stop(),
            "failure" => h.startup_failure("dep", "refused"),
            "error" => return Err(WorkflowError::refused("launch_error")),
            "return" => return Ok(Value::Null),
            _ => panic!("unknown test mode"),
        }
        assert!(
            v.inner.lock().unwrap().challenge.is_none(),
            "terminal_must_clear_private_request_and_count"
        );
        Ok(Value::Null)
    }
}
fn cleared(mode: &'static str) {
    let w = ManagedServingWorkflow::new();
    w.bind_progress_identity("run", "dep", 0).unwrap();
    let result = w.run(
        WorkflowPorts {
            authority: &mut Collaborator,
            controller: &mut Collaborator,
            launch: &mut LaunchFixture { mode },
        },
        &json!({}),
        50000,
        None,
        None,
    );
    assert_eq!(result.is_err(), mode == "error");
    assert!(
        w.view.inner.lock().unwrap().challenge.is_none(),
        "return_must_clear_private_request_and_count"
    );
}
#[test]
fn stop_erases_private_request_and_count() {
    cleared("stop")
}
#[test]
fn startup_failure_erases_private_request_and_count() {
    cleared("failure")
}
#[test]
fn launch_error_erases_private_request_and_count() {
    cleared("error")
}
#[test]
fn normal_return_erases_private_request_and_count() {
    cleared("return")
}
