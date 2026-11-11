// SPDX-License-Identifier: AGPL-3.0-only
//! Slice S5: the packaged host's `/chat/*` over the owner run's route gateway. While the run
//! serves, the page's chat goes through the shipped `ChatService` (api::chat_service) instead of
//! the SLICE-1 native path: chat.js:59-84 -> `/chat/api/send` + `/chat/api/events` ->
//! `DemoApp.chat_route` app.py:1926 -> `chat.handle` chat.py:553 -> `ChatService` :300-474;
//! `/api/demo/stop` -> `OwnerRun.stop` owner_run.py:275-282 -> teardown demo_chain.py:1322-1335
//! `chat.cancel_all('chat_cancelled')` -> the page shows `[stopped: chat_cancelled]` (chat.js:37-41).
#![cfg(feature = "desktop-shell")]
#[path = "../../api/tests/support/fake_route_gateway.rs"]
mod fake_route_gateway;
use api::{
    chat_service::{HttpRouteGateway, RouteGateway, RunRoute},
    schema::PublicError,
};
use budgets::{BudgetId as B, Entries};
use desktop::{
    lifecycle::Poll,
    services::{ServiceRequest, Services},
};
use fake_route_gateway::{End, FakeGateway, Script};
use node_service::{
    desktop_host::DesktopServices,
    service::{Driver, Job, NodeService},
};
use serde_json::{Value, json};
use std::{
    num::NonZeroUsize,
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};
fn millis(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
/// The SLICE-1 native path must not run while the owner run serves the chat.
struct MustNotRun;
impl Driver for MustNotRun {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        panic!("a chat the owner run serves reached the native driver")
    }
}
struct TestRoot(PathBuf);
impl Drop for TestRoot {
    fn drop(&mut self) {
        if let Err(e) = std::fs::remove_dir_all(&self.0)
            && e.kind() != std::io::ErrorKind::NotFound
        {
            eprintln!("test root {:?} not removed: {e}", self.0);
        }
    }
}
fn root() -> TestRoot {
    use std::os::unix::fs::DirBuilderExt;
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "packaged-chat-route-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    TestRoot(root)
}
/// The owner run serving `run-1` over the fake gateway (test double of the run seam).
struct ServingRun(Arc<dyn RouteGateway>);
impl RunRoute for ServingRun {
    fn status(&self) -> Option<Value> {
        Some(
            json!({"run_id":"run-1","state":"serving","step":"serving","error_code":null,
            "rollover":{"state":"idle"},
            "participants":[
                {"label":"This Mac (owner)","node_id":"a".repeat(64),"layers":[0,14]},
                {"label":"Mac B","node_id":"b".repeat(64),"layers":[14,28]}]}),
        )
    }
    fn gateway(&self, _: &str) -> Option<Arc<dyn RouteGateway>> {
        Some(self.0.clone())
    }
    fn model_id(&self) -> Option<String> {
        Some("Qwen/Qwen3-0.6B".into())
    }
}
struct Host {
    _root: TestRoot,
    timing: PathBuf,
    services: DesktopServices,
}
fn host(fake: &FakeGateway) -> Host {
    let root = root();
    let private = root.0.join("private");
    std::fs::create_dir(&private).unwrap();
    let evidence = root.0.join("evidence");
    std::fs::create_dir(&evidence).unwrap();
    let timing = evidence.join("chat-timing.jsonl");
    let services = DesktopServices::new(
        NodeService::new(
            Arc::new(MustNotRun),
            "private-local-token".into(),
            node_service::native::development_qualification().unwrap(),
        ),
        NonZeroUsize::new(4).unwrap(),
        &private,
    )
    .with_chat_run(
        Arc::new(ServingRun(Arc::new(HttpRouteGateway::new(
            fake.addr(),
            fake.bearer(),
        )))),
        Some(timing.clone()),
    )
    .unwrap();
    Host {
        _root: root,
        timing,
        services,
    }
}
fn call(host: &DesktopServices, path: &str, body: Value) -> (u16, Value) {
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    let chat = path.starts_with("/chat/");
    host.submit(
        ServiceRequest {
            method: "POST".into(),
            target: path.into(),
            headers: vec![
                (
                    if chat {
                        "x-mycelium-chat"
                    } else {
                        "x-mycelium-action"
                    }
                    .into(),
                    "1".into(),
                ),
                ("content-type".into(), "application/json".into()),
            ],
            body: serde_json::to_vec(&body).unwrap(),
        },
        Box::new(move |reply| tx.send(reply).unwrap_or_default()),
    );
    let reply = rx.recv_timeout(millis(B::WorkerStartup)).unwrap();
    (
        reply.status,
        serde_json::from_slice(&reply.body).unwrap_or(Value::Null),
    )
}
/// chat.js's loop: events until done; returns (text, last page).
fn until_done(host: &DesktopServices, id: &Value) -> (String, Value) {
    let end = Instant::now() + millis(B::WorkerStartup);
    let (mut after, mut text) = (json!(0), String::new());
    loop {
        let (status, page) = call(
            host,
            "/chat/api/events",
            json!({"request_id":id,"after":after}),
        );
        assert_eq!(status, 200, "{page}");
        for t in page["tokens"].as_array().unwrap() {
            text.push_str(t.as_str().unwrap());
        }
        after = page["next"].clone();
        if page["done"] == true {
            return (text, page);
        }
        assert!(Instant::now() < end, "the chat never ended: {page}");
        std::thread::yield_now();
    }
}
fn quit(host: &DesktopServices) {
    host.begin_shutdown(Instant::now() + millis(B::StageCleanup))
        .unwrap();
    let deadline = Instant::now() + millis(B::StageCleanup);
    let verdict = loop {
        match host.poll_shutdown() {
            Ok(Poll::Pending) if Instant::now() < deadline => std::thread::yield_now(),
            other => break other,
        }
    };
    assert_eq!(verdict, Ok(Poll::Verified));
}
fn timing_lines(path: &std::path::Path) -> Vec<Value> {
    match std::fs::read_to_string(path) {
        Ok(text) => text
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(e) => panic!("timing file unreadable: {e}"),
    }
}

/// A loopback fake gateway streams SSE tokens into `/chat/api/events`; the chat state names the
/// run's model and Macs (not the SLICE-1 hard-coded Qwen2.5-0.5B), and the completed chat leaves
/// one timing line in the owner's evidence folder.
#[test]
fn the_runs_route_gateway_streams_its_tokens_into_the_pages_chat_events() {
    let fake = FakeGateway::start(Script::completed(&["Hel", "lo"]));
    let host = host(&fake);
    let (status, state) = call(&host.services, "/chat/api/state", json!({}));
    assert_eq!(status, 200, "{state}");
    let served = json!([{"label":"This Mac (owner)","node":"a".repeat(12),"layers":[0,14]},
        {"label":"Mac B","node":"b".repeat(12),"layers":[14,28]}]);
    assert_eq!(
        (&state["available"], &state["model_id"], &state["served_by"]),
        (&json!(true), &json!("Qwen/Qwen3-0.6B"), &served)
    );
    let (status, sent) = call(
        &host.services,
        "/chat/api/send",
        json!({"prompt":"hi","max_new_tokens":2}),
    );
    assert_eq!(status, 200, "{sent}");
    assert_eq!(sent["served_by"], served);
    let (text, last) = until_done(&host.services, &sent["request_id"]);
    assert_eq!(text, "Hello");
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("completed"), &Value::Null)
    );
    assert_eq!(fake.seen(|s| s.posts.len()), 1);
    let lines = timing_lines(&host.timing);
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert_eq!(lines[0]["run_id"], "run-1");
    quit(&host.services);
}

/// One chat at a time on the route too (chat.py:319-321).
#[test]
fn a_second_send_while_the_route_chat_runs_is_chat_busy() {
    let fake = FakeGateway::start(Script::ending(&["a"], End::Hold));
    let host = host(&fake);
    let (status, sent) = call(
        &host.services,
        "/chat/api/send",
        json!({"prompt":"one","max_new_tokens":4}),
    );
    assert_eq!(status, 200, "{sent}");
    let (status, busy) = call(
        &host.services,
        "/chat/api/send",
        json!({"prompt":"two","max_new_tokens":4}),
    );
    assert_eq!((status, &busy["error"]), (429, &json!("chat_busy")));
    fake.release();
    until_done(&host.services, &sent["request_id"]);
    quit(&host.services);
}

/// `/api/demo/stop` while the route streams: the page's events end `cancelled` with
/// `chat_cancelled` (chat.py:367-369 via demo_chain.py:1322-1335), and no timing line is written.
#[test]
fn a_demo_stop_ends_the_route_chat_chat_cancelled() {
    let fake = FakeGateway::start(Script::ending(&["a", "b"], End::Hold));
    let host = host(&fake);
    let (status, sent) = call(
        &host.services,
        "/chat/api/send",
        json!({"prompt":"hi","max_new_tokens":8}),
    );
    assert_eq!(status, 200, "{sent}");
    fake.wait("both tokens sent", |s| s.tokens_sent == 2);
    let (status, stopped) = call(&host.services, "/api/demo/stop", json!({}));
    assert_eq!(status, 200, "{stopped}");
    let (text, last) = until_done(&host.services, &sent["request_id"]);
    assert_eq!(text, "ab");
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &json!("chat_cancelled"))
    );
    assert_eq!(fake.seen(|s| s.deletes), 1);
    assert_eq!(timing_lines(&host.timing), Vec::<Value>::new());
    quit(&host.services);
}
