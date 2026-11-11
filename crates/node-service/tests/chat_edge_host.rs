// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "desktop-shell")]
//! v0.1.4's chat edge through the real `DesktopServices` request path (the one the packaged window
//! and the dev shell both use): the same `/chat/api/send` bodies the oracle was executed on
//! (FX-L11-chat-edge), the limits, the busy and rate answers, and the gateway's credential-theft
//! policy answer, with a recording driver instead of a model.
use api::schema::PublicError;
use budgets::{BudgetId as B, Entries};
use desktop::{
    lifecycle::Poll,
    services::{ServiceRequest, Services},
};
use node_service::{
    desktop_host::DesktopServices,
    service::{Driver, Job, NodeService},
};
use serde_json::{Value, json};
use std::{
    num::NonZeroUsize,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

fn millis(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
/// What the driver was asked to run: `(templated prompt, max_new_tokens)`.
type Seen = Arc<Mutex<Vec<(String, u64)>>>;
struct Capture(Seen);
impl Driver for Capture {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        self.0.lock().unwrap().push((
            job.document["prompt"].as_str().unwrap().to_owned(),
            job.document["max_new_tokens"].as_u64().unwrap(),
        ));
        Ok(())
    }
}
/// Holds the run until released, so the chat stays busy.
struct Hold(Arc<(Mutex<bool>, std::sync::Condvar)>);
impl Driver for Hold {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        let (flag, cv) = &*self.0;
        let mut released = flag.lock().unwrap();
        while !*released {
            released = cv.wait(released).unwrap();
        }
        Ok(())
    }
}
/// A test-owned directory, removed with its contents when the test ends, also when it fails
/// (REVIEW-lanePH1 finding 8). Bind it before anything that uses it, so it is dropped last.
struct TestRoot(std::path::PathBuf);
impl std::ops::Deref for TestRoot {
    type Target = std::path::Path;
    fn deref(&self) -> &std::path::Path {
        &self.0
    }
}
impl AsRef<std::path::Path> for TestRoot {
    fn as_ref(&self) -> &std::path::Path {
        &self.0
    }
}
impl Drop for TestRoot {
    fn drop(&mut self) {
        if let Err(e) = std::fs::remove_dir_all(&self.0)
            && e.kind() != std::io::ErrorKind::NotFound
        {
            eprintln!("test root {:?} not removed: {e}", self.0);
        }
    }
}
/// A fresh private state root for the packaged action host (these chat paths never write it).
fn private() -> TestRoot {
    use std::os::unix::fs::DirBuilderExt;
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "chat-edge-host-private-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    TestRoot(root)
}
fn host_with(driver: Arc<dyn Driver>) -> (TestRoot, DesktopServices) {
    let private = private();
    let host = DesktopServices::new(
        NodeService::new(
            driver,
            "private-local-token".into(),
            node_service::native::development_qualification().unwrap(),
        ),
        NonZeroUsize::new(4).unwrap(),
        &private,
    );
    (private, host)
}
fn call_raw(host: &DesktopServices, path: &str, body: Vec<u8>) -> (u16, Value) {
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    host.submit(
        ServiceRequest {
            method: "POST".into(),
            target: path.into(),
            headers: vec![
                ("x-mycelium-chat".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body,
        },
        Box::new(move |reply| tx.send(reply).unwrap()),
    );
    let reply = rx.recv_timeout(millis(B::WorkerStartup)).unwrap();
    (
        reply.status,
        serde_json::from_slice(&reply.body).unwrap_or(Value::Null),
    )
}
fn send(host: &DesktopServices, body: &Value) -> (u16, Value) {
    call_raw(host, "/chat/api/send", serde_json::to_vec(body).unwrap())
}
/// Poll `/chat/api/events` until the request is done; return every token text.
fn drain(host: &DesktopServices, request_id: &str) -> (Vec<String>, Value) {
    let mut after = 0;
    let mut tokens = Vec::new();
    let deadline = Instant::now() + millis(B::WorkerStartup);
    loop {
        let (status, doc) = call_raw(
            host,
            "/chat/api/events",
            serde_json::to_vec(&json!({"request_id": request_id, "after": after})).unwrap(),
        );
        assert_eq!(status, 200, "{doc}");
        for t in doc["tokens"].as_array().unwrap() {
            tokens.push(t.as_str().unwrap().to_owned());
        }
        after = doc["next"].as_u64().unwrap();
        if doc["done"] == true {
            return (tokens, doc);
        }
        assert!(Instant::now() < deadline, "request never finished: {doc}");
    }
}
fn shut(host: &DesktopServices) {
    host.begin_shutdown(Instant::now() + millis(B::StageCleanup))
        .unwrap();
    let deadline = Instant::now() + millis(B::StageCleanup);
    while Instant::now() < deadline {
        match host.poll_shutdown() {
            Ok(Poll::Pending) => std::thread::yield_now(),
            _ => return,
        }
    }
}

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/chat-edge-oracle.json")).unwrap()
}

#[test]
fn every_executed_send_case_gets_the_oracles_status_body_and_driver_request() {
    let fixture = fixture();
    for case in fixture["send_cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let seen: Seen = Arc::default();
        let (_private, host) = host_with(Arc::new(Capture(seen.clone())));
        let (status, mut body) = call_raw(
            &host,
            "/chat/api/send",
            case["raw"].as_str().unwrap().as_bytes().to_vec(),
        );
        assert_eq!(
            u64::from(status),
            case["status"].as_u64().unwrap(),
            "{name}: {body}"
        );
        if status == 200 {
            assert!(body["request_id"].is_string(), "{name}");
            body.as_object_mut().unwrap().remove("request_id");
        }
        let expected = &case["body"];
        for key in ["max_new_tokens", "clamped", "error"] {
            assert_eq!(body[key], expected[key], "{name}: {key} in {body}");
        }
        let wanted = case["client_saw"].as_array().unwrap();
        if status == 200 {
            let deadline = Instant::now() + millis(B::WorkerStartup);
            while seen.lock().unwrap().is_empty() {
                assert!(Instant::now() < deadline, "{name}: driver never ran");
                std::thread::yield_now();
            }
            let (prompt, tokens) = seen.lock().unwrap()[0].clone();
            assert_eq!(
                tokens,
                wanted[0]["max_new_tokens"].as_u64().unwrap(),
                "{name}"
            );
            assert_eq!(
                prompt,
                node_service::chat_template::qwen2_chat_prompt(
                    wanted[0]["prompt"].as_str().unwrap()
                ),
                "{name}"
            );
        } else {
            assert!(
                seen.lock().unwrap().is_empty(),
                "{name}: a refused request ran"
            );
        }
        shut(&host);
    }
}

#[test]
fn the_state_document_carries_the_oracles_limits() {
    let (_private, host) = host_with(Arc::new(Capture(Arc::default())));
    let (status, state) = call_raw(&host, "/chat/api/state", b"{}".to_vec());
    assert_eq!(status, 200);
    assert_eq!(state["limits"], fixture()["state_limits"]);
    shut(&host);
}

#[test]
fn a_second_send_while_one_runs_is_429_chat_busy_as_in_the_oracle() {
    let fixture = fixture();
    assert_eq!(fixture["busy"]["second"]["status"], 429);
    assert_eq!(fixture["busy"]["second"]["body"]["error"], "chat_busy");
    let gate = Arc::new((Mutex::new(false), std::sync::Condvar::new()));
    let (_private, host) = host_with(Arc::new(Hold(gate.clone())));
    let (first, _) = send(&host, &json!({"prompt": "one", "max_new_tokens": 2}));
    assert_eq!(first, 200);
    let (second, body) = send(&host, &json!({"prompt": "two", "max_new_tokens": 2}));
    assert_eq!((second, body["error"].as_str()), (429, Some("chat_busy")));
    *gate.0.lock().unwrap() = true;
    gate.1.notify_all();
    shut(&host);
}

#[test]
fn the_fifth_accepted_send_within_a_minute_is_429_and_refusals_are_not_charged() {
    let fixture = fixture();
    let (_private, host) = host_with(Arc::new(Capture(Arc::default())));
    // The oracle: six invalid sends are not charged; the fifth valid one is limited. The valid
    // sends are credential-theft prompts: accepted like any request, finished at once, so no run
    // (and no cleanup proof) is needed between them.
    let theft = fixture["policy"][0]["prompt"].as_str().unwrap();
    for step in fixture["rate_charged_only_when_accepted"]
        .as_array()
        .unwrap()
    {
        let invalid = step["step"].as_str().unwrap().starts_with("invalid");
        let body = if invalid {
            json!({"prompt": "", "max_new_tokens": 1})
        } else {
            json!({"prompt": theft, "max_new_tokens": 1})
        };
        let (status, doc) = send(&host, &body);
        assert_eq!(
            u64::from(status),
            step["status"].as_u64().unwrap(),
            "{}: {doc}",
            step["step"]
        );
        assert_eq!(doc["error"], step["body"]["error"], "{}", step["step"]);
        if status == 200 {
            drain(&host, doc["request_id"].as_str().unwrap());
        }
    }
    shut(&host);
}

#[test]
fn a_credential_theft_prompt_gets_the_gateways_refusal_text_without_a_model_run() {
    let fixture = fixture();
    let seen: Seen = Arc::default();
    let (_private, host) = host_with(Arc::new(Capture(seen.clone())));
    let refusal = fixture["policy_stream"]["emit_token"][1].as_str().unwrap();
    let prompt = fixture["policy"][0]["prompt"].as_str().unwrap();
    assert_eq!(fixture["policy"][0]["response"], refusal);
    let (status, accepted) = send(&host, &json!({"prompt": prompt, "max_new_tokens": 64}));
    assert_eq!(status, 200, "{accepted}");
    // v0.1.4 accepts it like any request; the gateway then emits token 0 = the refusal and completes.
    assert_eq!(accepted["max_new_tokens"], 64);
    let (tokens, last) = drain(&host, accepted["request_id"].as_str().unwrap());
    assert_eq!(tokens, [refusal]);
    assert_eq!(last["done"], true);
    assert_eq!(last["error"], Value::Null);
    assert!(
        seen.lock().unwrap().is_empty(),
        "the policy answer ran a model"
    );
    // The next, ordinary prompt still reaches the driver.
    let (status, next) = send(
        &host,
        &json!({"prompt": "How can I prevent phishing attacks?", "max_new_tokens": 3}),
    );
    assert_eq!(status, 200, "{next}");
    let deadline = Instant::now() + millis(B::WorkerStartup);
    while seen.lock().unwrap().is_empty() {
        assert!(Instant::now() < deadline, "driver never ran");
        std::thread::yield_now();
    }
    assert_eq!(seen.lock().unwrap()[0].1, 3);
    shut(&host);
}

/// The roots `private()` makes are removed with their contents when the test ends, also when it
/// fails (REVIEW-lanePH1 finding 8: they used to accumulate in the user's TMPDIR).
#[test]
fn a_test_root_is_removed_when_its_test_ends() {
    let root = private();
    let path = root.to_path_buf();
    std::fs::write(path.join("left.json"), b"{}").unwrap();
    drop(root);
    assert!(!path.exists(), "{path:?} was left behind");
    let mut failing = None;
    let unwound = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let root = private();
        failing = Some(root.to_path_buf());
        panic!("a failing test");
    }));
    assert!(unwound.is_err());
    let failing = failing.unwrap();
    assert!(
        !failing.exists(),
        "{failing:?} was left behind by a failing test"
    );
}
