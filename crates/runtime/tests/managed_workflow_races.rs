// SPDX-License-Identifier: AGPL-3.0-only
// Deterministic held-collaborator controls, SOURCE_SOFTWARE only.
use runtime::managed_workflow::*;
use serde_json::{Value, json};
use std::sync::{Arc, Mutex, mpsc};
use std::time::Duration;
#[derive(Clone)]
struct Held {
    at: &'static str,
    view: WorkflowView,
    entered: mpsc::Sender<()>,
    release: Arc<Mutex<mpsc::Receiver<()>>>,
}
impl Held {
    fn hold(&self, at: &str) {
        if self.at == at {
            self.entered.send(()).unwrap();
            self.release.lock().unwrap().recv().unwrap();
        }
    }
}
impl Authority for Held {
    fn publish(
        &mut self,
        _: &mut dyn Controller,
        _: &str,
        _: &str,
        _: &WorkflowView,
    ) -> Result<Vec<Value>> {
        self.hold("publish");
        Ok(vec![json!({"offer_id":"offer","expires_at":1300.0})])
    }
    fn request_cleanup(&mut self, _: &Value) -> Result<()> {
        Ok(())
    }
}
impl Controller for Held {
    fn run_identity(&self) -> Result<(String, String)> {
        Ok(("run".into(), "deployment".into()))
    }
    fn prepare(&mut self, _: &mut dyn Authority, _: &[Value], _: &WorkflowView) -> Result<()> {
        self.hold("prepare");
        Ok(())
    }
}
impl Activation for Held {
    fn wait(&mut self, _: Duration) -> Result<bool> {
        self.hold("activation");
        Ok(true)
    }
    fn wall_time(&self) -> f64 {
        1000.0
    }
}
impl Launch for Held {
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
        _: &WorkflowView,
        hooks: &mut LaunchHooks<'_>,
    ) -> Result<Value> {
        self.hold("before-started");
        if self.at == "teardown" {
            hooks.startup_failure("deployment", "native_refused");
            self.hold("teardown");
            return Err(WorkflowError::refused("secondary_failure"));
        }
        hooks.started(json!({"registry_origin":"fixture"}))?;
        // A later start cannot overwrite the committed historical document.
        hooks.started(json!({"registry_origin":"late"}))?;
        Ok(json!({"stopped":true}))
    }
}
fn held(at: &'static str) {
    let w = ManagedServingWorkflow::new();
    let view = w.view();
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let ports = Held {
        at,
        view: view.clone(),
        entered: entered_tx,
        release: Arc::new(Mutex::new(release_rx)),
    };
    let child = std::thread::spawn(move || {
        let (mut a, mut c, mut l, mut activation) =
            (ports.clone(), ports.clone(), ports.clone(), ports.clone());
        let mut callback = |_: Value| {
            assert_eq!(ports.view.status()["state"], "api_running");
            ports.hold("callback");
            Ok(())
        };
        w.run(
            WorkflowPorts {
                authority: &mut a,
                controller: &mut c,
                launch: &mut l,
            },
            &json!({}),
            50000,
            if at == "activation" {
                Some(&mut activation)
            } else {
                None
            },
            Some(&mut callback),
        )
    });
    entered_rx.recv().unwrap();
    let before = view.status();
    view.stop();
    let stopped = view.is_stopped();
    // Release/join BEFORE assertions, so a failing expectation cannot strand IO.
    release_tx.send(()).unwrap();
    let result = child.join().unwrap();
    assert!(stopped);
    match at {
        "publish" | "prepare" | "activation" => {
            assert_eq!(result.unwrap_err().code, "managed_workflow_stopped");
            assert!(view.status()["startup"].is_null());
        }
        "before-started" => {
            assert!(result.is_ok());
            assert!(view.status()["startup"].is_null());
        }
        "callback" => {
            assert!(result.is_ok());
            assert_eq!(before["state"], "api_running");
            assert_eq!(view.status()["startup"]["registry_origin"], "fixture");
        }
        "teardown" => {
            assert_eq!(before["state"], "failed");
            assert_eq!(before["error_code"], "native_refused");
            assert_eq!(result.unwrap_err().code, "secondary_failure");
            assert_eq!(view.status()["error_code"], "native_refused");
        }
        _ => panic!("unknown held phase"),
    }
}
#[test]
fn stop_held_publish() {
    held("publish")
}
#[test]
fn stop_held_prepare() {
    held("prepare")
}
#[test]
fn stop_held_activation() {
    held("activation")
}
#[test]
fn stop_before_startup_commit() {
    held("before-started")
}
#[test]
fn callback_is_outside_mutex_and_stop_keeps_historical_startup() {
    held("callback")
}
#[test]
fn startup_refusal_published_before_remote_teardown() {
    held("teardown")
}
#[test]
fn dropping_non_owning_views_does_not_stop_workflow() {
    let w = ManagedServingWorkflow::new();
    drop(w.view());
    assert!(!w.view().is_stopped());
}
