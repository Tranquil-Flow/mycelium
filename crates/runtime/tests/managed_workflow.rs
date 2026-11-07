// SPDX-License-Identifier: AGPL-3.0-only
// f4e366e9 workflow.py:165-351; SOURCE_SOFTWARE scripted collaborators only.
// Paired external oracle capture SHA256:
// 36dacb357aea190f7814283f7c1a6bd6a2d77163d32fe02c8f7b99b887589908.
use runtime::managed_workflow::*;
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use std::time::Duration;
#[derive(Clone)]
struct Script {
    case: String,
    view: WorkflowView,
    trace: Arc<Mutex<(Vec<String>, Vec<Value>)>>,
}
impl Script {
    fn mark(&self, call: &str) {
        let snapshot = self.view.status();
        let mut t = self.trace.lock().unwrap();
        t.0.push(call.into());
        t.1.push(snapshot);
    }
}
impl Controller for Script {
    fn run_identity(&self) -> Result<(String, String)> {
        Ok(("run-1".into(), "dep-1".into()))
    }
    fn prepare(&mut self, _: &mut dyn Authority, _: &[Value], _: &WorkflowView) -> Result<()> {
        self.mark("prepare");
        if self.case == "prepare-refused" {
            return Err(WorkflowError::refused("prepare_refused"));
        }
        if self.case == "stop-prepare" {
            self.view.stop();
        }
        Ok(())
    }
}
impl Authority for Script {
    fn publish(
        &mut self,
        _: &mut dyn Controller,
        _: &str,
        _: &str,
        _: &WorkflowView,
    ) -> Result<Vec<Value>> {
        self.mark("publish");
        if self.case == "publish-refused" {
            return Err(WorkflowError::refused("publication_refused"));
        }
        if self.case == "stop-publish" {
            self.view.stop();
        }
        Ok(vec![
            json!({"offer_id":"offer","expires_at":if self.case=="activation-expired" {999.0} else {1300.0}}),
        ])
    }
    fn request_cleanup(&mut self, offer: &Value) -> Result<()> {
        self.mark(&format!("cleanup:{}", offer["offer_id"].as_str().unwrap()));
        if self.case == "cleanup-refused" {
            return Err(WorkflowError::new(
                "private /secret/cleanup",
                "RuntimeError",
            ));
        }
        Ok(())
    }
}
impl Launch for Script {
    fn validate_approval(&self, _: &Value) -> Result<()> {
        self.mark("approval");
        match self.case.as_str() {
            "approval-refused" => Err(WorkflowError::refused("model_execution_approval_required")),
            "approval-binding-refused" => {
                Err(WorkflowError::refused("managed_workflow_approval_mismatch"))
            }
            _ => Ok(()),
        }
    }
    fn validate_port(&self, _: u16) -> Result<()> {
        self.mark("port");
        if self.case == "port-refused" {
            Err(WorkflowError::refused("dynamic_loopback_port_unavailable"))
        } else {
            Ok(())
        }
    }
    fn run(
        &mut self,
        _: &Value,
        _: u16,
        _: &WorkflowView,
        hooks: &mut LaunchHooks<'_>,
    ) -> Result<Value> {
        self.mark("launch");
        if self.case == "startup-refused" {
            hooks.startup_failure("wrong", "ignored");
            hooks.startup_failure("dep-1", "native_refused");
            self.mark("teardown");
            return Err(WorkflowError::refused("later_failure"));
        }
        if self.case == "private-refused" {
            return Err(WorkflowError::refused("/secret/prompt"));
        }
        if self.case == "stop-before-started" {
            self.view.stop();
        }
        hooks.started(json!({"registry_origin":"http://127.0.0.1:50000"}))?;
        Ok(json!({"outcome":"stopped"}))
    }
}
impl Activation for Script {
    fn wait(&mut self, interval: Duration) -> Result<bool> {
        assert_eq!(interval,Duration::from_millis(budgets::BudgetId::PyMyceliumA13MacosWorkflowManagedservingworkflowRunActivationEventWaitL323.span(budgets::Entries::ZERO).unwrap().millis()));
        self.mark("activation");
        if self.case == "stop-activation" {
            self.view.stop();
            Ok(false)
        } else {
            Ok(true)
        }
    }
    fn wall_time(&self) -> f64 {
        1000.0
    }
}
fn fixture() -> Value {
    serde_json::from_str(include_str!("managed_workflow_oracle.json")).unwrap()
}
fn replay(case: &str) {
    let w = ManagedServingWorkflow::new();
    let view = w.view();
    let s = Script {
        case: case.into(),
        view: view.clone(),
        trace: Arc::new(Mutex::new((vec![], vec![]))),
    };
    let (mut authority, mut controller, mut launch, mut activation) =
        (s.clone(), s.clone(), s.clone(), s.clone());
    if case == "identity-changed" {
        w.bind_progress_identity("other", "dep-1", 1).unwrap();
    }
    if case == "stop-before" {
        view.stop();
    }
    let mut started = |_: Value| {
        s.mark("started");
        if case == "started-stop" {
            view.stop();
        }
        match case {
            "callback-refused" => Err(WorkflowError::refused("callback_refused")),
            "callback-private-refused" => Err(WorkflowError::refused("/secret/callback")),
            _ => Ok(()),
        }
    };
    let activation_port = if ["activation", "activation-expired", "stop-activation"].contains(&case)
    {
        Some(&mut activation as &mut dyn Activation)
    } else {
        None
    };
    let result = w.run(
        WorkflowPorts {
            authority: &mut authority,
            controller: &mut controller,
            launch: &mut launch,
        },
        &json!({}),
        50000,
        activation_port,
        Some(&mut started),
    );
    let (result, error) = match result {
        Ok(v) => (v, Value::Null),
        Err(e) => (Value::Null, json!(e.code)),
    };
    let trace = s.trace.lock().unwrap();
    let actual = json!({"calls":trace.0,"states":trace.1,"result":result,"error":error,"status":view.status(),"stop":view.is_stopped()});
    assert_eq!(
        actual,
        fixture()["cases"][case],
        "source workflow mismatch: {case}"
    );
}
macro_rules! replay_test {
    ($name:ident,$case:literal) => {
        #[test]
        fn $name() {
            replay($case)
        }
    };
}
replay_test!(success, "success");
replay_test!(callback_refused, "callback-refused");
replay_test!(callback_private_refused, "callback-private-refused");
replay_test!(approval_refused, "approval-refused");
replay_test!(approval_binding_refused, "approval-binding-refused");
replay_test!(port_refused, "port-refused");
replay_test!(stop_before, "stop-before");
replay_test!(identity_changed, "identity-changed");
replay_test!(publish_refused, "publish-refused");
replay_test!(prepare_refused, "prepare-refused");
replay_test!(stop_publish, "stop-publish");
replay_test!(stop_prepare, "stop-prepare");
replay_test!(activation, "activation");
replay_test!(activation_expired, "activation-expired");
replay_test!(stop_activation, "stop-activation");
replay_test!(startup_refused, "startup-refused");
replay_test!(private_refused, "private-refused");
replay_test!(cleanup_refused, "cleanup-refused");
replay_test!(started_stop, "started-stop");
replay_test!(stop_before_started, "stop-before-started");
#[test]
fn admitted_progress_policy_reaches_borrowed_launch_without_inventing_progress() {
    let w = ManagedServingWorkflow::new();
    w.bind_progress_identity("run-1", "dep-1", 0).unwrap();
    let view = w.view();
    let s = Script {
        case: "success".into(),
        view: view.clone(),
        trace: Arc::new(Mutex::new((vec![], vec![]))),
    };
    let (mut a, mut c, mut l) = (s.clone(), s.clone(), s.clone());
    let result = w
        .run(
            WorkflowPorts {
                authority: &mut a,
                controller: &mut c,
                launch: &mut l,
            },
            &json!({}),
            50000,
            None,
            None,
        )
        .unwrap();
    assert_eq!(result, json!({"outcome":"stopped"}));
    assert_eq!(
        view.status()["startup"]["registry_origin"],
        "http://127.0.0.1:50000"
    );
    assert!(view.status()["challenge_progress"].is_null());
    assert_eq!(
        s.trace.lock().unwrap().0,
        [
            "approval",
            "port",
            "publish",
            "prepare",
            "launch",
            "cleanup:offer"
        ]
    );
}
#[test]
fn close_wait_is_source_sum_not_cleanup_ack_or_a_new_deadline() {
    // Twice-executed original close_wait_seconds, source-bound imports only.
    let w = ManagedServingWorkflow::new();
    for (peers, millis) in [(0, 13500), (1, 1903500), (2, 3793500), (3, 5683500)] {
        assert_eq!(
            w.close_wait(budgets::Entries::new(peers))
                .unwrap()
                .as_millis(),
            millis
        );
    }
    assert!(w.close_wait(budgets::Entries::new(u64::MAX)).is_err());
}
#[test]
fn constructor_and_binding_are_private_and_one_shot() {
    let w = ManagedServingWorkflow::new();
    assert_eq!(w.view().status(), fixture()["constructor"]);
    assert_eq!(
        w.bind_progress_identity("", "dep", 0).unwrap_err().code,
        "managed_progress_identity_invalid"
    );
    assert_eq!(
        w.bind_progress_identity("run", "", 0).unwrap_err().code,
        "managed_progress_identity_invalid"
    );
    w.bind_progress_identity("run", "dep", 0).unwrap();
    assert_eq!(
        w.bind_progress_identity("run", "dep", 1).unwrap_err().code,
        "managed_progress_identity_reused"
    );
    assert_eq!(w.view().status(), fixture()["constructor"]);
}
