// SPDX-License-Identifier: AGPL-3.0-only
// Genuine borrowed workflow hooks with scripted launch, not native verification.
use runtime::managed_workflow::*;
use serde_json::{Value, json};
use std::sync::{Arc, Mutex, mpsc};

const REQUEST: &str = "startup-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
struct Fixture;
impl Controller for Fixture {
    fn run_identity(&self) -> Result<(String, String)> {
        Ok(("run".into(), "dep".into()))
    }
    fn prepare(&mut self, _: &mut dyn Authority, _: &[Value], _: &WorkflowView) -> Result<()> {
        Ok(())
    }
}
impl Authority for Fixture {
    fn publish(
        &mut self,
        _: &mut dyn Controller,
        _: &str,
        _: &str,
        _: &WorkflowView,
    ) -> Result<Vec<Value>> {
        Ok(vec![json!({"offer_id":"one"}), json!({"offer_id":"two"})])
    }
    fn request_cleanup(&mut self, _: &Value) -> Result<()> {
        Ok(())
    }
}
struct Script<F>(F);
impl<F> Launch for Script<F>
where
    F: FnMut(&WorkflowView, &mut LaunchHooks<'_>) -> Result<Value>,
{
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
        view: &WorkflowView,
        hooks: &mut LaunchHooks<'_>,
    ) -> Result<Value> {
        (self.0)(view, hooks)
    }
}
fn workflow() -> ManagedServingWorkflow {
    let w = ManagedServingWorkflow::new();
    w.bind_progress_identity("run", "dep", 0).unwrap();
    w
}
fn run<F>(w: &ManagedServingWorkflow, f: F) -> Result<Value>
where
    F: FnMut(&WorkflowView, &mut LaunchHooks<'_>) -> Result<Value>,
{
    w.run(
        WorkflowPorts {
            authority: &mut Fixture,
            controller: &mut Fixture,
            launch: &mut Script(f),
        },
        &json!({}),
        50000,
        None,
        None,
    )
}
fn event(h: &LaunchHooks<'_>, kind: &str, index: Value, limit: Value) {
    h.verified_challenge_event("dep", REQUEST, kind, &index, &limit);
}
fn progress(v: &WorkflowView, count: u64) {
    assert_eq!(
        v.status(),
        json!({"state":"starting", "error_code":null, "startup":null,
        "challenge_progress":if count == 0 {Value::Null} else {json!({"verified_token_count":count})},
        "readiness_source":"registry_api"}),
        "private_count_only_status"
    );
}
#[test]
fn exact_cap_allows_4096_verified_indices_but_not_the_next() {
    run(&workflow(), |v, h| {
        event(h, "begin", json!(-1), json!(4096));
        for index in 0..4096 {
            event(h, "token", json!(index), json!(4096));
            progress(v, index + 1);
        }
        event(h, "token", json!(4096), json!(4096));
        progress(v, 4096);
        Ok(json!(null))
    })
    .unwrap();
}
#[test]
fn all_request_format_refusals_leave_begin_available() {
    run(&workflow(), |v, h| {
        for request in [
            "startup-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "startup-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaag",
            "startup-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "startup-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "startUP-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "startup-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
            "startup-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaé",
        ] {
            h.verified_challenge_event("dep", request, "begin", &json!(-1), &json!(3));
            h.verified_challenge_event("dep", request, "token", &json!(0), &json!(3));
            progress(v, 0);
        }
        event(h, "begin", json!(-1), json!(3));
        event(h, "token", json!(0), json!(3));
        progress(v, 1);
        Ok(json!(null))
    })
    .unwrap();
}
#[test]
fn count_only_status_never_contains_request_binding_or_unverified_values() {
    run(&workflow(), |v, h| {
        event(h, "begin", json!(-1), json!(3));
        event(h, "token", json!({"token":"secret-token-value"}), json!(3));
        progress(v, 0);
        event(h, "token", json!(0), json!({"token":"secret-limit-value"}));
        progress(v, 0);
        event(h, "token", json!(0), json!(3));
        progress(v, 1);
        let snapshot = v.status().to_string();
        for private in [
            REQUEST,
            "run",
            "dep",
            "epoch",
            "secret-token-value",
            "secret-limit-value",
        ] {
            assert!(!snapshot.contains(private), "privacy_leak:{private}");
        }
        Ok(json!(null))
    })
    .unwrap();
}
#[test]
fn limits_refuse_zero_negative_bool_float_string_null_and_overflow() {
    for limit in [
        json!(0),
        json!(-1),
        json!(true),
        json!(false),
        json!(1.0),
        json!(4096.0),
        json!("3"),
        json!(null),
        json!(4097),
        json!(u64::MAX),
    ] {
        run(&workflow(), |v, h| {
            event(h, "begin", json!(-1), limit.clone());
            event(h, "token", json!(0), limit.clone());
            progress(v, 0);
            event(h, "begin", json!(-1), json!(1));
            event(h, "token", json!(0), json!(1));
            progress(v, 1);
            Ok(json!(null))
        })
        .unwrap();
    }
}
#[test]
fn first_begin_owns_request_and_limit_without_resetting_count() {
    run(&workflow(), |v, h| {
        event(h, "begin", json!(-1), json!(3));
        event(h, "token", json!(0), json!(3));
        event(h, "begin", json!(-1), json!(4));
        event(h, "token", json!(1), json!(4));
        progress(v, 1);
        h.verified_challenge_event(
            "dep",
            "startup-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "begin",
            &json!(-1),
            &json!(3),
        );
        h.verified_challenge_event(
            "dep",
            "startup-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "token",
            &json!(1),
            &json!(3),
        );
        progress(v, 1);
        event(h, "begin", json!(-1.0), json!(3));
        event(h, "token", json!(1), json!(3));
        progress(v, 2);
        Ok(json!(null))
    })
    .unwrap();
}

fn held_launch(mode: &'static str) {
    let w = workflow();
    let view = w.view();
    let (tx, rx) = mpsc::channel();
    let (release, wait) = mpsc::channel();
    let child = std::thread::spawn(move || {
        run(&w, |v, h| {
            if mode != "before" {
                event(h, "begin", json!(-1), json!(3));
                event(h, "token", json!(0), json!(3));
            }
            if mode == "failure" {
                h.startup_failure("dep", "native_refused");
            }
            if mode == "started" {
                h.started(json!({"origin":"fixture"}))?;
            }
            tx.send(()).unwrap();
            wait.recv().unwrap();
            event(h, "begin", json!(-1), json!(3));
            event(h, "token", json!(1), json!(3));
            h.started(json!({"origin":"late"}))?;
            assert!(
                v.status()["challenge_progress"].is_null(),
                "stop_or_terminal_fences_progress"
            );
            if mode == "failure" {
                Err(WorkflowError::refused("secondary_failure"))
            } else {
                Ok(json!(null))
            }
        })
    });
    rx.recv().unwrap();
    let before = view.status();
    view.stop();
    let after = view.status();
    release.send(()).unwrap();
    let result = child.join().unwrap();
    assert!(after["challenge_progress"].is_null());
    if mode == "after" {
        assert_eq!(
            before["challenge_progress"],
            json!({"verified_token_count":1})
        );
    }
    if mode == "started" {
        assert_eq!(before["startup"], json!({"origin":"fixture"}));
        assert_eq!(view.status()["startup"], before["startup"]);
    } else {
        assert!(view.status()["startup"].is_null());
    }
    if mode == "failure" {
        assert_eq!(before["error_code"], "native_refused");
        assert_eq!(result.unwrap_err().code, "secondary_failure");
        assert_eq!(view.status()["error_code"], "native_refused");
    } else {
        result.unwrap();
    }
}
#[test]
fn held_stop_before_progress_fences_begin_tokens_and_started() {
    held_launch("before")
}
#[test]
fn held_stop_after_progress_clears_and_fences_late_tokens() {
    held_launch("after")
}
#[test]
fn held_failure_clears_progress_before_slow_teardown() {
    held_launch("failure")
}
#[test]
fn held_start_hides_progress_and_retains_historical_startup() {
    held_launch("started")
}

struct HeldCleanup {
    view: WorkflowView,
    tx: mpsc::Sender<()>,
    wait: mpsc::Receiver<()>,
    calls: Arc<Mutex<Vec<String>>>,
}
impl Authority for HeldCleanup {
    fn publish(
        &mut self,
        c: &mut dyn Controller,
        r: &str,
        d: &str,
        v: &WorkflowView,
    ) -> Result<Vec<Value>> {
        Fixture.publish(c, r, d, v)
    }
    fn request_cleanup(&mut self, offer: &Value) -> Result<()> {
        self.calls
            .lock()
            .unwrap()
            .push(offer["offer_id"].as_str().unwrap().into());
        // Reentrant status + stop is permitted: external cleanup is outside lock.
        assert_eq!(self.view.status()["state"], "failed");
        assert!(self.view.status()["challenge_progress"].is_null());
        self.view.stop();
        if offer["offer_id"] == "one" {
            self.tx.send(()).unwrap();
            self.wait.recv().unwrap();
        }
        Err(WorkflowError::new("/private/cleanup", "CleanupError"))
    }
}
#[test]
fn launch_error_clears_progress_before_held_cleanup_and_preserves_primary_error() {
    let w = workflow();
    let view = w.view();
    let calls = Arc::new(Mutex::new(vec![]));
    let (tx, rx) = mpsc::channel();
    let (release, wait) = mpsc::channel();
    let mut a = HeldCleanup {
        view: view.clone(),
        tx,
        wait,
        calls: calls.clone(),
    };
    let child = std::thread::spawn(move || {
        let mut l = Script(|v: &WorkflowView, h: &mut LaunchHooks<'_>| {
            event(h, "begin", json!(-1), json!(3));
            event(h, "token", json!(0), json!(3));
            progress(v, 1);
            Err(WorkflowError::refused("/private/launch"))
        });
        w.run(
            WorkflowPorts {
                authority: &mut a,
                controller: &mut Fixture,
                launch: &mut l,
            },
            &json!({}),
            50000,
            None,
            None,
        )
    });
    rx.recv().unwrap();
    let during = view.status();
    view.stop();
    release.send(()).unwrap();
    let err = child.join().unwrap().unwrap_err();
    assert_eq!(err.code, "/private/launch");
    assert_eq!(during["error_code"], "managed_workflow_failed");
    assert!(during["challenge_progress"].is_null());
    assert!(during["startup"].is_null());
    assert_eq!(*calls.lock().unwrap(), ["one", "two"]);
    assert_eq!(
        view.status()["cleanup_errors"],
        json!([{"code":"workflow_cleanup_request_failed","exception_class":"CleanupError","count":2}])
    );
    assert!(!view.status().to_string().contains("/private"));
}
