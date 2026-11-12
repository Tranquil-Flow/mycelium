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
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering::SeqCst},
        mpsc,
    },
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
    host_over(
        Arc::new(ServingRun(Arc::new(HttpRouteGateway::new(
            fake.addr(),
            fake.bearer(),
        )))),
        Arc::new(MustNotRun),
    )
}
/// The packaged chat host over `run` (composed with `with_chat_run`) and the native `driver`.
fn host_over(run: Arc<dyn RunRoute>, driver: Arc<dyn Driver>) -> Host {
    let root = root();
    let private = root.0.join("private");
    std::fs::create_dir(&private).unwrap();
    let evidence = root.0.join("evidence");
    std::fs::create_dir(&evidence).unwrap();
    let timing = evidence.join("chat-timing.jsonl");
    let services = DesktopServices::new(
        NodeService::new(
            driver,
            "private-local-token".into(),
            node_service::native::development_qualification().unwrap(),
        ),
        NonZeroUsize::new(4).unwrap(),
        &private,
    )
    .with_chat_run(run, Some(timing.clone()))
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

// ---- lane S5F: REVIEW-lanePH3B-S5 F1, F3 and the run's handle (F2/F5) ----

/// A native driver run that holds until it is cancelled or released (bounded by the stage-cleanup
/// budget); it counts its runs, so a test sees whether a chat reached the SLICE-1 native path.
#[derive(Clone, Default)]
struct Holds {
    runs: Arc<AtomicUsize>,
    released: Arc<AtomicBool>,
}
impl Holds {
    fn runs(&self) -> usize {
        self.runs.load(SeqCst)
    }
    fn release(&self) {
        self.released.store(true, SeqCst);
    }
}
impl Driver for Holds {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        self.runs.fetch_add(1, SeqCst);
        let deadline = Instant::now() + millis(B::StageCleanup);
        while !job.cancelled() && !self.released.load(SeqCst) && Instant::now() < deadline {
            std::thread::yield_now();
        }
        Err(PublicError::new(503, "native_driver_released"))
    }
}
type Hold = (usize, mpsc::Sender<()>, mpsc::Receiver<()>);
/// The owner run as the chat sees it, scripted by the test: its status (`None`: no run was started
/// yet, `owner_status` is then the idle checklist, demo_app.py:144-149), one route gateway, and an
/// optional hold inside one status read.
struct ScriptedRun {
    status: Mutex<Option<Value>>,
    gateway: Arc<dyn RouteGateway>,
    reads: AtomicUsize,
    hold: Mutex<Option<Hold>>,
}
impl ScriptedRun {
    fn new(gateway: Arc<dyn RouteGateway>, status: Option<Value>) -> Arc<Self> {
        Arc::new(Self {
            status: Mutex::new(status),
            gateway,
            reads: AtomicUsize::new(0),
            hold: Mutex::new(None),
        })
    }
    fn set(&self, status: Option<Value>) {
        *self.status.lock().unwrap() = status;
    }
    /// Hold the `n`-th status read from now until released: (entered, release).
    fn hold_read(&self, n: usize) -> (mpsc::Receiver<()>, mpsc::Sender<()>) {
        let (entered_tx, entered) = mpsc::channel();
        let (release, release_rx) = mpsc::channel();
        *self.hold.lock().unwrap() = Some((self.reads.load(SeqCst) + n, entered_tx, release_rx));
        (entered, release)
    }
    fn disarm(&self) {
        self.hold.lock().unwrap().take();
    }
}
impl RunRoute for ScriptedRun {
    fn status(&self) -> Option<Value> {
        let read = self.reads.fetch_add(1, SeqCst) + 1;
        let held = {
            let mut hold = self.hold.lock().unwrap();
            if hold.as_ref().is_some_and(|(at, _, _)| *at == read) {
                hold.take()
            } else {
                None
            }
        };
        if let Some((_, entered, release)) = held {
            entered.send(()).unwrap_or_default();
            release
                .recv_timeout(millis(B::WorkerStartup))
                .unwrap_or_default();
        }
        self.status.lock().unwrap().clone()
    }
    fn gateway(&self, _: &str) -> Option<Arc<dyn RouteGateway>> {
        Some(self.gateway.clone())
    }
    fn model_id(&self) -> Option<String> {
        Some("Qwen/Qwen3-0.6B".into())
    }
}
fn http(fake: &FakeGateway) -> Arc<dyn RouteGateway> {
    Arc::new(HttpRouteGateway::new(fake.addr(), fake.bearer()))
}
fn serving_status() -> Value {
    ServingRun(Arc::new(MustNotRunGateway)).status().unwrap()
}
/// `ServingRun`'s status needs a gateway value only; this one is never asked to stream.
struct MustNotRunGateway;
impl RouteGateway for MustNotRunGateway {
    fn stream(
        &self,
        _: &str,
        _: u64,
        _: &api::chat_service::RequestCancel,
        _: &mut dyn FnMut(&str),
    ) -> Result<api::chat_service::Streamed, api::chat_service::GatewayError> {
        panic!("this gateway only builds a status")
    }
}
/// Activating or qualifying its route (owner_run.py STEPS, chat.py:47 `_WARMING_STEPS`).
fn warming() -> Value {
    json!({"run_id":"run-1","state":"running","step":"activate","error_code":null,
        "rollover":{"state":"idle"},"participants":null})
}
/// Switching to a fresh run (owner_run.py:252-257, 382, 402).
fn switching() -> Value {
    json!({"run_id":"run-1","state":"running","step":"descriptors","error_code":null,
        "rollover":{"state":"switching"},"participants":null})
}
fn submit_async(host: &DesktopServices, path: &str, body: Value) -> mpsc::Receiver<(u16, Value)> {
    let (tx, rx) = mpsc::channel();
    host.submit(
        ServiceRequest {
            method: "POST".into(),
            target: path.into(),
            headers: vec![
                ("x-mycelium-chat".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: serde_json::to_vec(&body).unwrap(),
        },
        Box::new(move |reply| {
            tx.send((
                reply.status,
                serde_json::from_slice(&reply.body).unwrap_or(Value::Null),
            ))
            .unwrap_or_default()
        }),
    );
    rx
}

/// F1: with a run composed but not serving (no run started, warming up, switching runs), every
/// chat route goes to the shipped `ChatService`, which answers the not-ready codes itself; the
/// SLICE-1 native path never answers while a run is composed. The shipped app has no other path:
/// chat.js `call` -> `_Handler.do_POST` app.py:1936-1950 -> `DemoApp.chat_route` app.py:1926-1934 ->
/// `chat.handle` chat.py:553-584 -> `ChatService.state` :476-504 / `send` :300-318 (`_serving`
/// :228-233; no owner run -> idle status, demo_app.py:144-149 -> `chat_not_ready` :317).
/// What chat.js shows (chat.js:46-53, 81-82): the state line is MESSAGES.chat_not_ready /
/// chat_warming_up / chat_switching_runs, Send is disabled (`!state.available`), and a send that
/// still arrives shows the same MESSAGES text for its 503 code.
#[test]
fn with_a_run_composed_every_chat_route_answers_the_shipped_not_ready_codes() {
    let fake = FakeGateway::start(Script::completed(&["Hel", "lo"]));
    let driver = Holds::default();
    let run = ScriptedRun::new(http(&fake), None);
    let host = host_over(run.clone(), Arc::new(driver.clone()));
    // `retry_after_s=5` (chat.py:315).
    let retry = millis(B::PyFinalMyceliumA13MacosChatRetryAfterSL315C60).as_secs();
    for (status, (switching_runs, warming_up), refusal) in [
        (None, (false, false), json!({"error":"chat_not_ready"})),
        (
            Some(warming()),
            (false, true),
            json!({"error":"chat_warming_up"}),
        ),
        (
            Some(switching()),
            (true, false),
            json!({"error":"chat_switching_runs","retry_after_s":retry}),
        ),
    ] {
        run.set(status.clone());
        let (code, state) = call(&host.services, "/chat/api/state", json!({}));
        assert_eq!(code, 200, "{state}");
        assert_eq!(
            (
                &state["available"],
                &state["switching_runs"],
                &state["warming_up"],
                &state["model_id"],
                &state["served_by"]
            ),
            (
                &json!(false),
                &json!(switching_runs),
                &json!(warming_up),
                &Value::Null,
                &json!([])
            ),
            "{status:?}"
        );
        let (code, sent) = call(
            &host.services,
            "/chat/api/send",
            json!({"prompt":"hi","max_new_tokens":2}),
        );
        assert_eq!((code, &sent), (503, &refusal), "{status:?}");
    }
    assert_eq!(
        driver.runs(),
        0,
        "a chat reached the SLICE-1 native path while a run is composed"
    );
    assert_eq!(fake.seen(|s| s.posts.len()), 0);
    // No record: the service's own 404 (chat.py:460-462).
    let (code, unknown) = call(
        &host.services,
        "/chat/api/events",
        json!({"request_id":"A".repeat(24),"after":0}),
    );
    assert_eq!(
        (code, &unknown["error"]),
        (404, &json!("chat_request_unknown"))
    );
    // Once the run serves, the same routes chat over its route gateway.
    run.set(Some(serving_status()));
    let (code, sent) = call(
        &host.services,
        "/chat/api/send",
        json!({"prompt":"hi","max_new_tokens":2}),
    );
    assert_eq!(code, 200, "{sent}");
    let (text, last) = until_done(&host.services, &sent["request_id"]);
    assert_eq!(
        (text.as_str(), &last["terminal"]),
        ("Hello", &json!("completed"))
    );
    assert_eq!(driver.runs(), 0);
    quit(&host.services);
}

/// F3: a native chat and a route chat are never admitted together. At 5963027 the route send
/// checked the native chat and released that lock before `ChatService.send` marked itself busy, so
/// a native send in that gap (the run not serving for a moment) was admitted too. The hold sits in
/// the route send's own admission read (`ChatService.send` -> `_serving`, chat.py:311; the second
/// status read of that send at 5963027, the first being the host's routing read). v0.1.4 admits
/// one chat at a time (chat.py:319-321, one `_busy` under one lock).
#[test]
fn a_native_chat_and_a_route_chat_are_never_admitted_together() {
    let fake = FakeGateway::start(Script::ending(&["a"], End::Hold));
    let driver = Holds::default();
    let run = ScriptedRun::new(http(&fake), Some(serving_status()));
    let host = host_over(run.clone(), Arc::new(driver.clone()));
    let (entered, release) = run.hold_read(2);
    let first = submit_async(
        &host.services,
        "/chat/api/send",
        json!({"prompt":"one","max_new_tokens":4}),
    );
    let deadline = Instant::now() + millis(B::WorkerStartup);
    let mut answered = None;
    let held = loop {
        if entered.try_recv().is_ok() {
            break true;
        }
        if let Ok(reply) = first.try_recv() {
            answered = Some(reply);
            break false;
        }
        assert!(Instant::now() < deadline, "the first send never answered");
        std::thread::yield_now();
    };
    if !held {
        run.disarm();
    }
    // The run leaves `serving` for a moment (warm-up of a fresh run) and the page sends again.
    run.set(Some(warming()));
    let (second_code, second) = call(
        &host.services,
        "/chat/api/send",
        json!({"prompt":"two","max_new_tokens":4}),
    );
    run.set(Some(serving_status()));
    release.send(()).unwrap_or_default();
    let (first_code, first) = answered.unwrap_or_else(|| {
        first
            .recv_timeout(millis(B::WorkerStartup))
            .expect("the first send never answered")
    });
    assert!(
        !(first_code == 200 && second_code == 200),
        "two chats admitted at once: {first} and {second}"
    );
    assert_eq!(
        driver.runs(),
        0,
        "a chat reached the SLICE-1 native path while a run is composed"
    );
    assert_eq!(
        (first_code, second_code, &second["error"]),
        (200, 503, &json!("chat_warming_up"))
    );
    fake.release();
    until_done(&host.services, &first["request_id"]);
    driver.release();
    quit(&host.services);
}

/// F2/F5 (lane S5F item 4): the one handle the owner run's ports hold (`DesktopServices::run_chats`)
/// ends the page's route chat with the shipped code for every teardown, driven against a serving
/// fake run: Quit (`DemoApp.close` app.py:1818-1823 -> `demo_app.close` -> `OwnerRun.stop`
/// owner_run.py:275-282 -> `chain.teardown(failed=False)` :450), Stop (`/api/demo/stop`, the host's
/// own `cancel_all`, then the same teardown), rollover (owner_run.py:256 `rollover_requested`),
/// the ceiling (demo_chain.py:1157-1164, its own cancel off the caller's thread) and a failed run
/// (the monitor's dead-member code demo_chain.py:1179-1181, else `owner_run_failed`); the code is
/// demo_chain.py:1332-1335 -> chat.py:418-451 -> :366-369 -> chat.js:37-41.
#[test]
fn the_runs_handle_ends_the_route_chat_with_the_shipped_code_for_every_teardown() {
    for (case, page) in [
        ("quit", "chat_cancelled"),
        ("stop", "chat_cancelled"),
        ("rollover", "chat_switching_runs"),
        ("ceiling", "execution_ceiling_reached"),
        ("dead member", "joined_route_member_stale"),
        ("failed run", "owner_run_failed"),
    ] {
        let fake = FakeGateway::start(Script::ending(&["a", "b"], End::Hold));
        let host = host(&fake);
        let chats = host
            .services
            .run_chats()
            .expect("with_chat_run builds the run's handle");
        chats.new_run();
        let (status, sent) = call(
            &host.services,
            "/chat/api/send",
            json!({"prompt":"hi","max_new_tokens":8}),
        );
        assert_eq!(status, 200, "{case}: {sent}");
        fake.wait("both tokens sent", |s| s.tokens_sent == 2);
        match case {
            "quit" => assert_eq!(chats.teardown(false), 1, "{case}"),
            "stop" => {
                let (status, stopped) = call(&host.services, "/api/demo/stop", json!({}));
                assert_eq!(status, 200, "{stopped}");
                assert_eq!(
                    chats.teardown(false),
                    0,
                    "the host's Stop cancelled it first"
                );
            }
            "rollover" => {
                chats.rollover_requested();
                assert_eq!(chats.teardown(false), 1, "{case}");
            }
            "ceiling" => {
                assert!(chats.ceiling_reached(), "the first ceiling probe cancels");
                assert!(!chats.ceiling_reached(), "once per run");
            }
            "dead member" => {
                chats.dead_member("joined_route_member_stale");
                assert_eq!(chats.teardown(true), 1, "{case}");
            }
            _ => assert_eq!(chats.teardown(true), 1, "{case}"),
        }
        let (text, last) = until_done(&host.services, &sent["request_id"]);
        assert_eq!(text, "ab", "{case}");
        assert_eq!(
            (&last["terminal"], &last["error"]),
            (&json!("cancelled"), &json!(page)),
            "{case}"
        );
        if case == "ceiling" {
            assert_eq!(chats.teardown(true), 0, "the ceiling cancelled it first");
        }
        assert_eq!(fake.seen(|s| s.deletes), 1, "{case}");
        assert_eq!(timing_lines(&host.timing), Vec::<Value>::new(), "{case}");
        quit(&host.services);
    }
}
