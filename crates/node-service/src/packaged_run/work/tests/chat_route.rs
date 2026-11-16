// SPDX-License-Identifier: AGPL-3.0-only
//! Lane CHAT (ledger row 17, owner ruling 14 Nov: one chat path, as v0.1.4): the packaged chat
//! over the owner run's route, through the real packaged run (`OwnerRun` -> `DemoRuns` ->
//! `Runner` -> `Composed`), the production route (`chat::RunChatRoute`, `chat::chat_run`) and the
//! shipped `ChatService` behind `DesktopServices`' `/chat/*` routes.
//!
//! v0.1.4 @ f4e366e9: chat.js -> `chat.handle` (chat.py:553-584) -> `ChatService.state` (:477-503)
//! / `send` (:300-340) / `events` (:460-475); `_serving` (:229-233) takes the route only while the
//! run serves; `_warming_up` (:244-248) while the run is in `activate` / `serving`; the run's
//! teardown cancels every chat FIRST (demo_chain.py:1322-1335) with `chat_switching_runs` after a
//! rollover request, the monitor's dead-member code or `owner_run_failed` for a failed run, else
//! `chat_cancelled`; the execution ceiling cancels its chats once (:1155-1163).
//!
//! SERVING STAND-IN (named as such): a real packaged run cannot reach `serving` on one Mac without
//! a qualified route (lane Q is porting the startup challenge in parallel, ledger row 16), so a
//! serving run here is the stand-in the lane SRV tests use (`serving_r2::ServingComposed`): every
//! step is answered by the test while new_run, teardown and the serving probes are the REAL
//! `Composed` chain over the packaged host; the `activate` step's open epoch is a loopback fake
//! request gateway (`api/tests/support/fake_route_gateway.rs`), put where the real work puts its
//! epoch (the host custody's route source). Once lane Q merges, `the_page_chats_through_the_run_
//! route_before_during_and_after_a_run` must be re-pointed at the real qualified route.
use super::serving_r2::run_state;
use super::*;
use crate::desktop_host::DesktopServices;
use api::{
    actions::Action,
    chat_service::{HttpRouteGateway, RouteGateway, RunRoute},
};
use desktop::{
    lifecycle::Poll,
    services::{ServiceRequest, Services},
};
use owner_run::{
    action::DemoFacts,
    machine::{CeilingProbe, EXECUTION_CEILING_CODE, MonitorProbe},
    owner_chain::ChainPorts as _,
};
use std::{num::NonZeroUsize, sync::mpsc};
#[allow(dead_code)] // the shared fake has endings this file does not use
#[path = "../../../../../api/tests/support/fake_route_gateway.rs"]
mod fake_route_gateway;
use fake_route_gateway::{End, FakeGateway, Script as Fake};

/// The fake gateway's bearer: no reply, log line, status or timing record may carry it.
const BEARER: &str = "fake-gateway-bearer-0123456789abcdef";

fn patience() -> Duration {
    span(BudgetId::WorkerStartup).unwrap()
}
struct Facts;
impl DemoFacts for Facts {
    fn owner(&self) -> bool {
        true
    }
    fn ensure_exchange(&mut self) -> bool {
        true
    }
    fn members_joined_without_relay(&self) -> Option<u64> {
        Some(0)
    }
}

/// What the test switches in the stand-in while the run goes on.
#[derive(Default)]
struct Flags {
    /// Hold every step until the test releases it (the test hears which step entered).
    hold: AtomicBool,
    /// Answer the next serving `rollover_due` with `true` (the run's wall clock stands in).
    rollover: AtomicBool,
    /// Let the serving monitor be the real `Composed` one (none resident: it fails the run).
    real_monitor: AtomicBool,
}
/// The serving stand-in (see the module comment).
struct StandIn {
    inner: Composed,
    host: PackagedRun,
    gateway: Value,
    run_id: String,
    flags: Arc<Flags>,
    entered: mpsc::Sender<String>,
    release: mpsc::Receiver<()>,
}
impl Chain for StandIn {
    fn new_run(
        &mut self,
        run: &str,
        deployment: &str,
        reason: owner_run::Reason,
        ceiling: Option<&Value>,
        binding: Option<&Value>,
    ) {
        self.run_id = run.to_owned();
        self.inner
            .new_run(run, deployment, reason, ceiling, binding);
    }
    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        if step == "activate" {
            // The epoch the activation opens: where `Work::compose` puts the real one.
            let gateway = self.gateway.clone();
            lock(&self.host.custody).route = Some(crate::packaged_run::chat::RouteSource {
                run_id: self.run_id.clone(),
                read: Arc::new(move || Some(gateway.clone())),
            });
        }
        if self.flags.hold.load(Ordering::SeqCst) {
            assert!(self.entered.send(step.to_owned()).is_ok(), "test listens");
            let poll = span(BudgetId::OwnerPoll).unwrap();
            loop {
                match self.release.recv_timeout(poll) {
                    Ok(()) => break,
                    Err(mpsc::RecvTimeoutError::Timeout) if !env.stopped() => {}
                    Err(_) => return Err(StepError::Stopped),
                }
            }
        }
        Ok(())
    }
    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        self.inner.teardown(failed, env)
    }
    fn rollover_binding(&self) -> Option<Value> {
        self.inner.rollover_binding()
    }
    fn rollover_due(&mut self) -> Option<bool> {
        let real = self.inner.rollover_due();
        if self.flags.rollover.swap(false, Ordering::SeqCst) {
            Some(true)
        } else {
            real
        }
    }
    fn ceiling_reached(&mut self) -> CeilingProbe {
        self.inner.ceiling_reached()
    }
    fn monitor(&mut self) -> MonitorProbe {
        if self.flags.real_monitor.load(Ordering::SeqCst) {
            self.inner.monitor()
        } else {
            MonitorProbe::Code(None)
        }
    }
}

/// One page request: `(status, body)`; every reply's bytes are kept for the bearer check.
fn post(
    services: &DesktopServices,
    replies: &Mutex<Vec<String>>,
    path: &str,
    body: Value,
) -> (u16, Value) {
    let (tx, rx) = mpsc::sync_channel(1);
    services.submit(
        ServiceRequest {
            method: "POST".into(),
            target: path.into(),
            headers: vec![
                ("x-mycelium-chat".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: serde_json::to_vec(&body).unwrap(),
        },
        Box::new(move |reply| tx.send(reply).unwrap_or_default()),
    );
    let reply = rx
        .recv_timeout(patience())
        .expect("the page's request answers");
    lock(replies).push(String::from_utf8_lossy(&reply.body).into_owned());
    (
        reply.status,
        serde_json::from_slice(&reply.body).unwrap_or(Value::Null),
    )
}
/// chat.js's loop: events until done; returns (tokens, last page).
fn until_done(
    services: &DesktopServices,
    replies: &Mutex<Vec<String>>,
    id: &Value,
) -> (Vec<String>, Value) {
    let end = Instant::now() + patience();
    let (mut after, mut tokens) = (json!(0), Vec::new());
    loop {
        let (status, page) = post(
            services,
            replies,
            "/chat/api/events",
            json!({"request_id":id,"after":after}),
        );
        assert_eq!(status, 200, "{page}");
        for t in page["tokens"].as_array().unwrap() {
            tokens.push(t.as_str().unwrap().to_owned());
        }
        after = page["next"].clone();
        if page["done"] == true {
            return (tokens, page);
        }
        assert!(Instant::now() < end, "the chat never ended: {page}");
        std::thread::yield_now();
    }
}
fn quit(services: &DesktopServices) {
    services
        .begin_shutdown(Instant::now() + patience())
        .unwrap();
    let deadline = Instant::now() + patience();
    let verdict = loop {
        match services.poll_shutdown() {
            Ok(Poll::Pending) if Instant::now() < deadline => std::thread::yield_now(),
            other => break other,
        }
    };
    assert_eq!(verdict, Ok(Poll::Verified));
}
fn timing_lines(path: &Path) -> Vec<Value> {
    match fs::read_to_string(path) {
        Ok(text) => text
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect(),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Vec::new(),
        Err(e) => panic!("timing file unreadable: {e}"),
    }
}

/// The packaged owner with its run installed over the stand-in, and the page's chat composed by
/// the production `chat_run` over that run (`<state>/v02-node/evidence/chat-timing.jsonl`).
struct Page {
    services: DesktopServices,
    run: OwnerRun,
    config: RunConfig,
    state: PathBuf,
    timing: PathBuf,
    flags: Arc<Flags>,
    entered: mpsc::Receiver<String>,
    release: mpsc::Sender<()>,
    replies: Mutex<Vec<String>>,
    f: Fixture,
}
impl Page {
    fn new(fake: &FakeGateway) -> Self {
        let f = Fixture::new();
        let flags = Arc::new(Flags::default());
        let (entered_tx, entered) = mpsc::channel();
        let (release, release_rx) = mpsc::channel();
        let mut chain: Option<Box<dyn Chain>> = Some(Box::new(StandIn {
            inner: Composed::new(&f.host, None, None),
            host: f.host.clone(),
            gateway: json!({"addr":fake.addr().to_string(),"bearer":fake.bearer()}),
            run_id: String::new(),
            flags: flags.clone(),
            entered: entered_tx,
            release: release_rx,
        }));
        let config = RunConfig::default();
        let run = OwnerRun::install_with(
            f.host.clone(),
            &config,
            None,
            Box::new(move || chain.take().expect("one runner")),
        );
        let state = f.root.join("app-state");
        let dirs = crate::packaged::prepare_dirs(&state).unwrap();
        let node = NodeService::new(
            Arc::new(NoNative),
            "chat-route-page".into(),
            crate::native::development_qualification().unwrap(),
        );
        // The packaged composition publishes the node's first status before the window's first
        // request (`DesktopServices::packaged_with_run`); the page's status reads it.
        api::actions::Backend::publish(&mut node.clone()).unwrap();
        let services = DesktopServices::new(node, NonZeroUsize::new(4).unwrap(), &dirs.private);
        let services = chat_run(services, &config, dirs.chat_timing.clone()).unwrap();
        Self {
            services,
            run,
            config,
            state,
            timing: dirs.chat_timing,
            flags,
            entered,
            release,
            replies: Mutex::default(),
            f,
        }
    }
    fn post(&self, path: &str, body: Value) -> (u16, Value) {
        post(&self.services, &self.replies, path, body)
    }
    fn send(&self, prompt: &str) -> (u16, Value) {
        self.post(
            "/chat/api/send",
            json!({"prompt":prompt,"max_new_tokens":8}),
        )
    }
    fn chat_state(&self) -> Value {
        let (status, state) = self.post("/chat/api/state", json!({}));
        assert_eq!(status, 200, "{state}");
        state
    }
    fn until_done(&self, id: &Value) -> (Vec<String>, Value) {
        until_done(&self.services, &self.replies, id)
    }
    fn start(&self) {
        lock(&self.run.runs)
            .demo_action(Action::DemoStart, &Map::new(), &mut Facts)
            .expect("start");
    }
    fn stop(&self) {
        lock(&self.run.runs)
            .demo_action(Action::DemoStop, &Map::new(), &mut Facts)
            .expect("stop");
    }
    fn status(&self) -> Value {
        self.config
            .observed()
            .and_then(|view| view.status())
            .map_or(Value::Null, |s| s.to_value())
    }
    /// The run's snapshot once `done` holds for it (bounded).
    fn until(&self, what: &str, done: impl Fn(&Value) -> bool) -> Value {
        let end = Instant::now() + patience();
        loop {
            let status = self.status();
            if done(&status) {
                return status;
            }
            assert!(Instant::now() < end, "{what}: {status}");
            std::thread::yield_now();
        }
    }
    fn serving(&self) -> Value {
        self.until("the run serves", |s| s["state"] == "serving")
    }
    /// The next held step's name.
    fn entered(&self) -> String {
        self.entered
            .recv_timeout(patience())
            .expect("a held step entered")
    }
    fn close(self) {
        quit(&self.services);
        lock(&self.run.runs).close();
        let replies = lock(&self.replies).join("\n");
        assert!(!replies.contains(BEARER), "a page reply carried the bearer");
        assert!(
            !fs::read_to_string(&self.timing)
                .unwrap_or_default()
                .contains(BEARER),
            "the timing file carried the bearer"
        );
        drop(self.f);
    }
}
fn not_ready(page: &Page, code: &str) {
    let state = page.chat_state();
    assert_eq!(
        (&state["available"], &state["model_id"], &state["served_by"]),
        (&json!(false), &Value::Null, &json!([])),
        "{state}"
    );
    assert_eq!(page.send("hi"), (503, json!({"error":code})), "{state}");
}

/// Proves: ledger row 17 (`/chat/api/state|send|events` through the production run route before a
/// run, in every step 1 to 10, while the run serves, and after Stop). Before the first Start no
/// run exists (`chat_not_ready`, chat.py:317-318); in steps 1-8 the run is `running` outside the
/// warming steps (`chat_not_ready`), in `activate` and `serving` it warms up (`chat_warming_up`,
/// chat.py:47, 244-248) and the gateway the run already holds is NOT offered (it does not serve,
/// chat.py:229-233); serving, the page receives exactly the tokens the route's gateway streams
/// and the completed chat leaves one timing line under the app's state root
/// (`<state>/v02-node/evidence/chat-timing.jsonl`); Stop ends a streaming chat `cancelled` with
/// `chat_cancelled` (demo_chain.py:1335, chat.py:367-369) through ONE gateway `DELETE`, and the
/// stopped run is `chat_not_ready` again.
#[test]
fn the_page_chats_through_the_run_route_before_during_and_after_a_run() {
    let fake = FakeGateway::start(Fake::completed(&["Hel", "lo"]));
    let page = Page::new(&fake);
    let route = RunChatRoute::new(page.config.clone());
    not_ready(&page, "chat_not_ready");
    page.flags.hold.store(true, Ordering::SeqCst);
    page.start();
    for (index, (step, _)) in owner_run::machine::STEPS.iter().enumerate() {
        assert_eq!(&page.entered(), step, "step {}", index + 1);
        let warming = matches!(*step, "activate" | "serving");
        let state = page.chat_state();
        assert_eq!(
            (
                &state["available"],
                &state["warming_up"],
                &state["switching_runs"]
            ),
            (&json!(false), &json!(warming), &json!(false)),
            "{step}: {state}"
        );
        let (code, refused) = page.send("hi");
        let expected = if warming {
            "chat_warming_up"
        } else {
            "chat_not_ready"
        };
        assert_eq!(
            (code, &refused),
            (503, &json!({"error":expected})),
            "{step}"
        );
        let run_id = page.status()["run_id"].as_str().unwrap().to_owned();
        assert!(
            route.gateway(&run_id).is_none(),
            "{step}: the route was offered while the run does not serve"
        );
        page.release.send(()).unwrap();
    }
    let serving = page.serving();
    let run_id = serving["run_id"].as_str().unwrap().to_owned();
    assert!(route.gateway(&run_id).is_some(), "the serving run's route");
    assert!(route.gateway("another-run").is_none());
    let state = page.chat_state();
    assert_eq!(state["available"], true, "{state}");
    assert_eq!(
        state["model_id"].as_str(),
        route.model_id().as_deref(),
        "{state}"
    );
    assert!(state["model_id"].is_string(), "{state}");
    let (code, sent) = page.send("hi");
    assert_eq!(code, 200, "{sent}");
    let (tokens, last) = page.until_done(&sent["request_id"]);
    assert_eq!(tokens, ["Hel", "lo"], "exactly the gateway's tokens");
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("completed"), &Value::Null)
    );
    let lines = timing_lines(&page.timing);
    assert_eq!(lines.len(), 1, "{lines:?}");
    assert_eq!(lines[0]["run_id"], json!(run_id));
    assert!(
        page.timing.starts_with(&page.state),
        "under the app's state root"
    );
    // Stop while a chat streams.
    fake.script(Fake::ending(&["a", "b"], End::Hold));
    let (code, held) = page.send("again");
    assert_eq!(code, 200, "{held}");
    fake.wait("both tokens sent", |s| s.tokens_sent == 4);
    page.stop();
    let (tokens, last) = page.until_done(&held["request_id"]);
    assert_eq!(tokens, ["a", "b"]);
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &json!("chat_cancelled"))
    );
    assert_eq!(fake.seen(|s| s.deletes), 1);
    page.until("the run stopped", |s| s["state"] == "stopped");
    assert!(route.gateway(&run_id).is_none(), "a stopped run's route");
    not_ready(&page, "chat_not_ready");
    assert_eq!(
        timing_lines(&page.timing).len(),
        1,
        "a cancelled chat is not timed"
    );
    page.close();
}

/// Proves: ledger row 17 (a failed run: demo_chain.py:1332-1334 `monitor_error_code or
/// 'owner_run_failed'`; the real monitor's `managed_workflow_missing`, :1174-1176, is not a
/// dead-member code, so the page's chat ends `cancelled` with `owner_run_failed` and the failed
/// run is `chat_not_ready`).
#[test]
fn a_failed_run_ends_the_route_chat_owner_run_failed() {
    let fake = FakeGateway::start(Fake::ending(&["a"], End::Hold));
    let page = Page::new(&fake);
    page.start();
    page.serving();
    let (code, sent) = page.send("hi");
    assert_eq!(code, 200, "{sent}");
    fake.wait("the token sent", |s| s.tokens_sent == 1);
    page.flags.real_monitor.store(true, Ordering::SeqCst);
    let (tokens, last) = page.until_done(&sent["request_id"]);
    assert_eq!(tokens, ["a"]);
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &json!("owner_run_failed"))
    );
    let failed = page.until("the run failed", |s| s["state"] == "failed");
    assert_eq!(failed["error_code"], "managed_workflow_missing", "{failed}");
    assert_eq!(fake.seen(|s| s.deletes), 1);
    not_ready(&page, "chat_not_ready");
    page.close();
}

/// The page's own `GET /api/status` (what run.js renders the Chat box from).
fn page_status(services: &DesktopServices) -> Value {
    let (tx, rx) = mpsc::sync_channel(1);
    services.submit(
        ServiceRequest {
            method: "GET".into(),
            target: "/api/status".into(),
            headers: Vec::new(),
            body: Vec::new(),
        },
        Box::new(move |reply| tx.send(reply).unwrap_or_default()),
    );
    let reply = rx.recv_timeout(patience()).expect("the status answers");
    assert_eq!(
        reply.status,
        200,
        "{}",
        String::from_utf8_lossy(&reply.body)
    );
    serde_json::from_slice(&reply.body).unwrap()
}

/// Lane CHATS item 1 (ledger row 106 a; v0.1.4 `chat.chat_status`, chat.py:521-529 @ f4e366e9):
/// while the run serves, the owner's status says the chat is available, from the SAME
/// `ChatService::state()` the chat page reads, with the page's `local_url`; no relay runs, so no
/// `share_url`. Before the run and after its Stop the block says not available. This is what
/// run.js turns into "Open the chat on this Mac" (run.js:128-137).
#[test]
fn the_owner_status_says_the_chat_is_available_while_the_run_serves() {
    let fake = FakeGateway::start(Fake::completed(&["Hel", "lo"]));
    let page = Page::new(&fake);
    let idle = page_status(&page.services);
    assert_eq!(idle["installation_role"], "owner", "{idle}");
    assert_eq!(idle["chat"]["available"], false, "{idle}");
    page.start();
    page.serving();
    let serving = page_status(&page.services);
    let chat = &serving["chat"];
    assert_eq!(chat["available"], true, "{serving}");
    assert_eq!(chat["busy"], false, "{serving}");
    assert_eq!(
        chat["available"],
        page.chat_state()["available"],
        "the status and the chat page read one service"
    );
    assert!(
        chat["local_url"]
            .as_str()
            .is_some_and(|u| u.ends_with("/chat")),
        "{serving}"
    );
    assert_eq!(chat["share_url"], Value::Null, "no relay: {serving}");
    page.stop();
    page.until("the run stopped", |s| s["state"] == "stopped");
    let stopped = page_status(&page.services);
    assert_eq!(stopped["chat"]["available"], false, "{stopped}");
    page.close();
}

/// Lane CHATS item 2 (v0.1.4 `_bind_public_routes`, app.py:1073-1077 @ f4e366e9): the handler the
/// relay's `/chat/*` call is the run's own chat service with the relay's client key, so a room
/// member's chat goes through the serving run's route, as the owner's page does: the state says
/// available, a send streams exactly the gateway's tokens through the relay's events, and a
/// stopped run answers the service's own `chat_not_ready`.
#[test]
fn the_relay_chat_reaches_the_serving_runs_route() {
    let fake = FakeGateway::start(Fake::completed(&["Hel", "lo"]));
    let page = Page::new(&fake);
    let relay = page
        .services
        .network()
        .relay_chat()
        .expect("the composition binds the relay's chat");
    let call = |path: &str, body: Value| {
        let answer = relay(
            "POST",
            path,
            &[
                ("X-Mycelium-Chat".into(), "1".into()),
                ("Content-Type".into(), "application/json".into()),
                ("Cf-Connecting-Ip".into(), "203.0.113.7".into()),
            ],
            &serde_json::to_vec(&body).unwrap(),
        )
        .expect("the relay chat answers");
        assert_eq!(answer.kind, "application/json");
        (
            u16::try_from(answer.status).unwrap(),
            serde_json::from_slice::<Value>(&answer.raw).unwrap(),
        )
    };
    let (code, idle) = call("/chat/api/state", json!({}));
    assert_eq!((code, &idle["available"]), (200, &json!(false)), "{idle}");
    page.start();
    page.serving();
    let (code, serving) = call("/chat/api/state", json!({}));
    assert_eq!(
        (code, &serving["available"]),
        (200, &json!(true)),
        "{serving}"
    );
    let (code, sent) = call(
        "/chat/api/send",
        json!({"prompt": "hi", "max_new_tokens": 8}),
    );
    assert_eq!(code, 200, "{sent}");
    let end = Instant::now() + patience();
    let (mut after, mut tokens) = (json!(0), Vec::new());
    loop {
        let (code, page_events) = call(
            "/chat/api/events",
            json!({"request_id": sent["request_id"], "after": after}),
        );
        assert_eq!(code, 200, "{page_events}");
        for t in page_events["tokens"].as_array().unwrap() {
            tokens.push(t.as_str().unwrap().to_owned());
        }
        after = page_events["next"].clone();
        if page_events["done"] == true {
            break;
        }
        assert!(Instant::now() < end, "the relay chat never ended");
        std::thread::yield_now();
    }
    assert_eq!(tokens, ["Hel", "lo"], "exactly the gateway's tokens");
    page.stop();
    page.until("the run stopped", |s| s["state"] == "stopped");
    assert_eq!(
        call("/chat/api/send", json!({"prompt": "hi"})),
        (503, json!({"error": "chat_not_ready"}))
    );
    page.close();
}

/// Proves: ledger row 17 (a rollover: `ctx.state['rollover_requested']` (owner_run.py:256) makes
/// the teardown cancel `chat_switching_runs` (demo_chain.py:1333); while the fresh run starts the
/// page answers `chat_switching_runs` with `retry_after_s` (chat.py:239-242, 314-315,
/// owner_run.py:382, 402); the fresh run serves its own route and chats again.)
#[test]
fn a_rollover_ends_the_route_chat_chat_switching_runs_and_the_next_run_chats() {
    let fake = FakeGateway::start(Fake::ending(&["a"], End::Hold));
    let page = Page::new(&fake);
    page.start();
    let first = page.serving()["run_id"].as_str().unwrap().to_owned();
    let (code, sent) = page.send("hi");
    assert_eq!(code, 200, "{sent}");
    fake.wait("the token sent", |s| s.tokens_sent == 1);
    page.flags.hold.store(true, Ordering::SeqCst);
    page.flags.rollover.store(true, Ordering::SeqCst);
    let (tokens, last) = page.until_done(&sent["request_id"]);
    assert_eq!(tokens, ["a"]);
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &json!("chat_switching_runs"))
    );
    assert_eq!(page.entered(), "model", "the fresh run starts");
    let state = page.chat_state();
    assert_eq!(
        (&state["available"], &state["switching_runs"]),
        (&json!(false), &json!(true)),
        "{state}"
    );
    let retry = span(BudgetId::PyFinalMyceliumA13MacosChatRetryAfterSL315C60)
        .unwrap()
        .as_secs();
    assert_eq!(
        page.send("hi"),
        (
            503,
            json!({"error":"chat_switching_runs","retry_after_s":retry})
        )
    );
    page.flags.hold.store(false, Ordering::SeqCst);
    page.release.send(()).unwrap();
    let second = page.until("the fresh run serves", |s| {
        s["state"] == "serving" && s["run_id"].as_str() != Some(first.as_str())
    });
    fake.script(Fake::completed(&["Hel", "lo"]));
    let (code, sent) = page.send("hi");
    assert_eq!(code, 200, "{sent}");
    let (tokens, last) = page.until_done(&sent["request_id"]);
    assert_eq!(tokens, ["Hel", "lo"]);
    assert_eq!(last["terminal"], "completed");
    assert_eq!(timing_lines(&page.timing)[0]["run_id"], second["run_id"]);
    page.stop();
    page.until("the run stopped", |s| s["state"] == "stopped");
    page.close();
}

/// Proves: REVIEW-laneCHAT F2 (each run starts with fresh chat facts: a new `RunContext`,
/// owner_run.py:392-399, whose `ctx.state` has no `rollover_requested`): the run after a rollover
/// is begun by the packaged chain's own `new_run` (`Composed`, `chat::new_run`), so a plain Stop
/// during its chat ends that chat `chat_cancelled` (demo_chain.py:1335), not with the previous
/// run's `chat_switching_runs` (:1333).
#[test]
fn a_stop_on_the_run_after_a_rollover_ends_its_chat_chat_cancelled() {
    let fake = FakeGateway::start(Fake::ending(&["a"], End::Hold));
    let page = Page::new(&fake);
    page.start();
    let first = page.serving()["run_id"].as_str().unwrap().to_owned();
    let (code, sent) = page.send("hi");
    assert_eq!(code, 200, "{sent}");
    fake.wait("the token sent", |s| s.tokens_sent == 1);
    page.flags.rollover.store(true, Ordering::SeqCst);
    let (_, last) = page.until_done(&sent["request_id"]);
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &json!("chat_switching_runs"))
    );
    page.until("the fresh run serves", |s| {
        s["state"] == "serving" && s["run_id"].as_str() != Some(first.as_str())
    });
    fake.script(Fake::ending(&["b"], End::Hold));
    let (code, held) = page.send("again");
    assert_eq!(code, 200, "{held}");
    fake.wait("the fresh run's token sent", |s| s.tokens_sent == 2);
    page.stop();
    let (tokens, last) = page.until_done(&held["request_id"]);
    assert_eq!(tokens, ["b"]);
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &json!("chat_cancelled")),
        "the fresh run kept the previous run's rollover request"
    );
    assert_eq!(fake.seen(|s| s.deletes), 2);
    page.until("the run stopped", |s| s["state"] == "stopped");
    page.close();
}

/// Proves: ledger row 17 (two pages chatting over the one route: one chat in flight for the whole
/// swarm, chat.py:319-321 `chat_busy`; each page reads exactly its own chat's tokens, chat.py:460-475).
#[test]
fn two_pages_chat_over_the_route_one_at_a_time_each_reading_its_own_tokens() {
    let fake = FakeGateway::start(Fake::ending(&["a", "b"], End::Hold));
    let page = Page::new(&fake);
    page.start();
    page.serving();
    let (code, one) = page.send("one");
    assert_eq!(code, 200, "{one}");
    fake.wait("both tokens sent", |s| s.tokens_sent == 2);
    let (code, busy) = page.send("two");
    assert_eq!((code, &busy["error"]), (429, &json!("chat_busy")));
    fake.release();
    let (tokens, last) = page.until_done(&one["request_id"]);
    assert_eq!(
        (tokens, &last["terminal"]),
        (vec!["a".to_owned(), "b".to_owned()], &json!("completed"))
    );
    fake.script(Fake::completed(&["Hel", "lo"]));
    let (code, two) = page.send("two");
    assert_eq!(code, 200, "{two}");
    let (tokens, _) = page.until_done(&two["request_id"]);
    assert_eq!(tokens, ["Hel", "lo"]);
    // Read again from the start, the first page's chat holds its own tokens only.
    let (tokens, again) = page.until_done(&one["request_id"]);
    assert_eq!(tokens, ["a", "b"], "{again}");
    assert_eq!(fake.seen(|s| s.posts.len()), 2);
    page.stop();
    page.until("the run stopped", |s| s["state"] == "stopped");
    page.close();
}

/// The bearer check's child (see the test below): a completed chat, a failed stream and a chat
/// cancelled by Stop over the route.
fn bearer_scenario() {
    let fake = FakeGateway::start(Fake::completed(&["Hel", "lo"]));
    let page = Page::new(&fake);
    page.start();
    page.serving();
    let (_, sent) = page.send("hi");
    page.until_done(&sent["request_id"]);
    fake.script(Fake::ending(&["x"], End::Failed("route_not_open")));
    let (_, sent) = page.send("hi");
    let (_, failed) = page.until_done(&sent["request_id"]);
    assert_eq!(failed["error"], "route_not_open");
    fake.script(Fake::ending(&["y"], End::Hold));
    let (_, sent) = page.send("hi");
    fake.wait("the held token", |s| s.tokens_sent == 4);
    page.stop();
    page.until_done(&sent["request_id"]);
    page.until("the run stopped", |s| s["state"] == "stopped");
    assert!(
        !page.status().to_string().contains(BEARER),
        "the run's status carried the bearer"
    );
    assert_eq!(timing_lines(&page.timing).len(), 1);
    page.close();
}
/// Proves: ledger row 17 (the route's bearer stays inside the gateway object: no page reply, no
/// run status, no timing record and no line this process writes to stdout or stderr carries it).
/// The scenario runs as a child of this test binary so that its whole output is captured.
#[test]
fn the_route_bearer_never_reaches_a_reply_a_log_line_or_the_timing_file() {
    const CHILD: &str = "MYCELIUM_CHAT_BEARER_CHILD";
    if std::env::var_os(CHILD).is_some() {
        bearer_scenario();
        return;
    }
    let name = "packaged_run::work::tests::chat_route::the_route_bearer_never_reaches_a_reply_a_log_line_or_the_timing_file";
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args([name, "--exact", "--nocapture", "--test-threads", "1"])
        .env(CHILD, "1")
        .output()
        .unwrap();
    let text = format!(
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(output.status.success(), "the scenario failed: {text}");
    assert!(
        text.contains("1 passed"),
        "the scenario did not run: {text}"
    );
    assert!(
        !text.contains(BEARER),
        "a log line carried the bearer: {text}"
    );
}

// ---- the run's ports over a resident owner workflow (real `PackagedChainPorts`) ----

/// The owner run as a chat service admits it (`serving` on the fake gateway): the ports below
/// cancel through the host's attached `RunChats`, whatever served the chat.
struct ScriptedServing(Arc<dyn RouteGateway>);
impl RunRoute for ScriptedServing {
    fn status(&self) -> Option<Value> {
        Some(
            json!({"run_id":"run-ports","state":"serving","step":"serving",
            "error_code":null,"rollover":{"state":"idle"},"participants":[]}),
        )
    }
    fn gateway(&self, _: &str) -> Option<Arc<dyn RouteGateway>> {
        Some(self.0.clone())
    }
    fn model_id(&self) -> Option<String> {
        Some("Qwen/Qwen3-0.6B".into())
    }
}
/// The real ports with a composed resident owner workflow, a page chat streaming on the fake
/// gateway (held), and the page's `RunChats` attached to the host.
struct Ported {
    ports: PackagedChainPorts,
    state: RunState,
    services: DesktopServices,
    replies: Mutex<Vec<String>>,
    chat: Value,
    f: Fixture,
}
fn ported(fake: &FakeGateway, run: &str) -> Ported {
    let mut f = Fixture::new();
    let ports = PackagedChainPorts::new(&f.host, None, None);
    let state = run_state(&mut f, &ports.work, run, &format!("dep-{run}"));
    let mut env = Env { at: Instant::now() };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    let dirs = crate::packaged::prepare_dirs(&f.root.join("app-state")).unwrap();
    let services = DesktopServices::new(
        NodeService::new(
            Arc::new(NoNative),
            "chat-ports-page".into(),
            crate::native::development_qualification().unwrap(),
        ),
        NonZeroUsize::new(4).unwrap(),
        &dirs.private,
    )
    .with_chat_run(
        Arc::new(ScriptedServing(Arc::new(HttpRouteGateway::new(
            fake.addr(),
            fake.bearer(),
        )))),
        Some(dirs.chat_timing),
    )
    .unwrap();
    crate::packaged_run::chat::attach_chats(&f.host, services.run_chats().unwrap());
    let replies = Mutex::default();
    let (code, chat) = post(
        &services,
        &replies,
        "/chat/api/send",
        json!({"prompt":"hi","max_new_tokens":8}),
    );
    assert_eq!(code, 200, "{chat}");
    fake.wait("the token sent", |s| s.tokens_sent == 1);
    Ported {
        ports,
        state,
        services,
        replies,
        chat,
        f,
    }
}
impl Ported {
    fn ended(&self) -> Value {
        until_done(&self.services, &self.replies, &self.chat["request_id"]).1
    }
    fn close(mut self) {
        // Idempotent after a teardown the test already ran.
        assert!(self.ports.close_work().is_ok(), "the work closes");
        quit(&self.services);
        drop(self.f);
    }
}

/// Proves: ledger row 17 (the teardown cancels every chat FIRST, while the route still takes
/// `DELETE` (demo_chain.py:1326-1335: "Closing the listener first leaves the proxy waiting on its
/// SSE read"): the gateway's `DELETE` arrives while the run's owner workflow is still resident,
/// before the work closes; ordered by observation inside the `DELETE`, not by time).
#[test]
fn the_teardown_cancels_the_page_chat_before_the_work_closes() {
    let fake = FakeGateway::start(Fake::ending(&["a"], End::Hold));
    let mut p = ported(&fake, "run-order");
    let resident = p.ports.work.resident.clone();
    let (seen_tx, seen) = mpsc::channel();
    let seen_tx = Mutex::new(seen_tx);
    fake.on_delete(Box::new(move || {
        let open = resident.try_lock().ok().map(|r| r.work.is_some());
        lock(&seen_tx).send(open).unwrap_or_default();
    }));
    let mut env = Env { at: Instant::now() };
    let torn = p.ports.teardown(false, &mut env);
    let at_cancel = seen.recv_timeout(patience());
    assert_eq!(torn, Ok(()));
    assert_eq!(
        at_cancel,
        Ok(Some(true)),
        "the chat's cancel must reach the route before the work closes"
    );
    assert!(
        lock(&p.ports.work.resident).work.is_none(),
        "the work closed"
    );
    let last = p.ended();
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &json!("chat_cancelled"))
    );
    assert_eq!(fake.seen(|s| s.deletes), 1);
    p.close();
}

/// A serving state of `p`'s run whose execution clock is far past its wall ceiling.
fn past_the_ceiling(p: &Ported) -> RunState {
    let wall = BudgetId::MemberChosenWall
        .span(Entries::ZERO)
        .unwrap()
        .millis()
        / 1000;
    let mut serving = p.state.clone();
    serving.work_limits = Some(json!({ "max_wall_seconds": wall }));
    serving.execution_clock_started_at = Some(-(wall as f64) * 10.0);
    serving
}

/// Proves: ledger row 17 (the execution ceiling cancels the page's chats with its code ONCE per
/// run, off the probe's thread (demo_chain.py:1155-1163 `execution_ceiling_cancelled`): the
/// chat has ended when `ceiling_reached` has answered, before any monitor ran; neither a second
/// `ceiling_reached`, the monitor's own ceiling check (:1166-1168) nor the failed run's teardown
/// cancels again).
#[test]
fn the_ceiling_cancels_the_page_chat_once_with_its_code() {
    let fake = FakeGateway::start(Fake::ending(&["a"], End::Hold));
    let mut p = ported(&fake, "run-ceiling");
    let serving = past_the_ceiling(&p);
    let ceiling = Some(EXECUTION_CEILING_CODE.to_owned());
    assert_eq!(
        p.ports.ceiling_reached(&serving),
        CeilingProbe::Code(ceiling.clone())
    );
    let last = p.ended();
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &json!(EXECUTION_CEILING_CODE))
    );
    assert_eq!(
        p.ports.ceiling_reached(&serving),
        CeilingProbe::Code(ceiling.clone())
    );
    assert_eq!(p.ports.monitor(&serving), MonitorProbe::Code(ceiling));
    let mut env = Env { at: Instant::now() };
    assert_eq!(p.ports.teardown(true, &mut env), Ok(()));
    assert_eq!(fake.seen(|s| s.deletes), 1, "one cancel per run");
    p.close();
}

/// Proves: REVIEW-laneCHAT F2 (`execution_ceiling_cancelled` is a fact of the run's `ctx`,
/// demo_chain.py:1159-1160, and every run gets a fresh one, owner_run.py:392-399): after the
/// ceiling cancelled the first run's chat, the next run begun by the packaged chain's own
/// `new_run` (`Composed`, `chat::new_run`) still has its chat cancelled by its own ceiling, with
/// the ceiling's code, through the real `ceiling_reached` port.
#[test]
fn the_next_runs_ceiling_still_cancels_its_page_chat() {
    let fake = FakeGateway::start(Fake::ending(&["a"], End::Hold));
    let mut p = ported(&fake, "run-ceiling-first");
    let ceiling = Some(EXECUTION_CEILING_CODE.to_owned());
    let serving = past_the_ceiling(&p);
    assert_eq!(
        p.ports.ceiling_reached(&serving),
        CeilingProbe::Code(ceiling.clone())
    );
    let last = p.ended();
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &json!(EXECUTION_CEILING_CODE))
    );
    let mut env = Env { at: Instant::now() };
    assert_eq!(p.ports.teardown(true, &mut env), Ok(()));
    assert_eq!(fake.seen(|s| s.deletes), 1);
    // The next run, begun as the runner begins every run: the packaged chain's `new_run`.
    Composed::new(&p.f.host, None, None).new_run(
        "run-ceiling-next",
        "dep-run-ceiling-next",
        owner_run::Reason::Start,
        None,
        None,
    );
    fake.script(Fake::ending(&["b"], End::Hold));
    let (code, next) = post(
        &p.services,
        &p.replies,
        "/chat/api/send",
        json!({"prompt":"hi","max_new_tokens":8}),
    );
    assert_eq!(code, 200, "{next}");
    fake.wait("the next run's token sent", |s| s.tokens_sent == 2);
    let mut serving = past_the_ceiling(&p);
    serving.run_id = "run-ceiling-next".into();
    assert_eq!(
        p.ports.ceiling_reached(&serving),
        CeilingProbe::Code(ceiling)
    );
    fake.wait("the next run's ceiling cancel", |s| s.deletes == 2);
    let (tokens, last) = until_done(&p.services, &p.replies, &next["request_id"]);
    assert_eq!(tokens, ["b"]);
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &json!(EXECUTION_CEILING_CODE))
    );
    p.close();
}

/// Proves: ledger row 17 (the monitor's ceiling check, demo_chain.py:1166-1168, cancels the
/// page's chats with the ceiling's code when no `ceiling_reached` probe ran first, once).
#[test]
fn the_monitor_ceiling_cancels_the_page_chat_once_with_its_code() {
    let fake = FakeGateway::start(Fake::ending(&["a"], End::Hold));
    let mut p = ported(&fake, "run-monitor-ceiling");
    let serving = past_the_ceiling(&p);
    let ceiling = Some(EXECUTION_CEILING_CODE.to_owned());
    assert_eq!(
        p.ports.monitor(&serving),
        MonitorProbe::Code(ceiling.clone())
    );
    let last = p.ended();
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &json!(EXECUTION_CEILING_CODE))
    );
    assert_eq!(p.ports.monitor(&serving), MonitorProbe::Code(ceiling));
    let mut env = Env { at: Instant::now() };
    assert_eq!(p.ports.teardown(true, &mut env), Ok(()));
    assert_eq!(fake.seen(|s| s.deletes), 1, "one cancel per run");
    p.close();
}

/// Proves: ledger row 17 (a dead member: the monitor's dead-member code is `monitor_error_code`
/// (demo_chain.py:1179-1181) and the failed run's teardown cancels with it (:1334); v0.2's dead
/// member is a participant whose seed lease ran out, `joined_route_member_stale`).
#[test]
fn a_dead_member_ends_the_page_chat_with_its_code() {
    let fake = FakeGateway::start(Fake::ending(&["a"], End::Hold));
    let mut p = ported(&fake, "run-dead");
    let seed = p.f.host.with(|c| c.authority.seed.clone().unwrap());
    let leases = api::joined_owner::epoch::seed_members(&seed.coordinator);
    {
        let mut resident = lock(&p.ports.work.resident);
        let work = resident.work.take().unwrap();
        resident.work = Some(work.with_seed_view(Arc::new(move |node: &str| {
            leases(node).map(|(row, now)| (row, now + 1e9))
        })));
    }
    assert_eq!(
        p.ports.monitor(&p.state),
        MonitorProbe::Code(Some("joined_route_member_stale".into()))
    );
    let mut env = Env { at: Instant::now() };
    assert_eq!(p.ports.teardown(true, &mut env), Ok(()));
    let last = p.ended();
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &json!("joined_route_member_stale"))
    );
    assert_eq!(fake.seen(|s| s.deletes), 1);
    p.close();
}

/// One request to a relay's loopback proxy as the public front forwards it: `(status, body)`.
/// `None` once nothing listens there any more (the relay closed).
fn relay_call(
    port: u16,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: &Value,
) -> Option<(u16, Value)> {
    use std::io::{Read, Write};
    let mut stream = std::net::TcpStream::connect(("127.0.0.1", port)).ok()?;
    stream.set_read_timeout(Some(patience())).unwrap();
    let body = serde_json::to_vec(body).unwrap();
    let mut head = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Length: {}\r\n",
        body.len()
    );
    for (name, value) in headers {
        head.push_str(&format!("{name}: {value}\r\n"));
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes()).ok()?;
    stream.write_all(&body).ok()?;
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).ok()?;
    let split = raw.windows(4).position(|w| w == b"\r\n\r\n")?;
    let status = String::from_utf8_lossy(&raw[..split])
        .split(' ')
        .nth(1)?
        .parse()
        .ok()?;
    Some((
        status,
        serde_json::from_slice(&raw[split + 4..]).unwrap_or(Value::Null),
    ))
}
/// A room member's chat request through the relay (chat.js `call`, the edge's client address).
fn room(port: u16, path: &str, body: &Value) -> Option<(u16, Value)> {
    relay_call(
        port,
        "POST",
        path,
        &[
            ("Content-Type", "application/json"),
            ("X-Mycelium-Chat", "1"),
            ("Cf-Connecting-Ip", "203.0.113.9"),
        ],
        body,
    )
}

/// Lane CHATPAGE item 4 (REVIEW-laneCHATS-astra CHATS-02): Quit while a room member's chat
/// streams through the relay and the serving run's route. The relay is the composition's own
/// (`/api/network/start`, external provider on a loopback proxy port: `bind_public_routes` binds
/// the run's chat behind it), the member's page polls `/chat/api/events` through it as chat.js
/// does, the route's stream is held after its first token, and the owner's Stop through the relay
/// is refused while the run keeps serving. Then the window's real Quit (`packaged::assemble` with
/// the run closed first, `close_run_first`): it ends `Verified` within the Quit allowance (no
/// hang), the held chat is cancelled through ONE gateway `DELETE` with `chat_cancelled`, the run
/// is stopped, the relay no longer listens, the member's poller ends, and the relay's handler
/// refuses a new send `application_closed` (Quit's gate is closing).
#[test]
fn quit_completes_while_a_relay_chat_streams_through_the_serving_run() {
    let fake = FakeGateway::start(Fake::ending(&["a"], End::Hold));
    let page = Page::new(&fake);
    let port = std::net::TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap();
    let (tx, rx) = mpsc::sync_channel(1);
    page.services.submit(
        ServiceRequest {
            method: "POST".into(),
            target: "/api/network/start".into(),
            headers: vec![
                ("x-mycelium-action".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: serde_json::to_vec(&json!({"relay_acknowledged": true, "max_seconds": 1800,
                "provider": "external", "public_origin": "https://v02-chatpage.example.org",
                "proxy_port": port}))
            .unwrap(),
        },
        Box::new(move |reply| tx.send(reply).unwrap_or_default()),
    );
    let started = rx
        .recv_timeout(patience())
        .expect("the relay start answers");
    assert_eq!(
        started.status,
        200,
        "{}",
        String::from_utf8_lossy(&started.body)
    );
    page.start();
    let run_id = page.serving()["run_id"].as_str().unwrap().to_owned();
    let (code, sent) = room(
        port,
        "/chat/api/send",
        &json!({"prompt": "hi", "max_new_tokens": 8}),
    )
    .expect("the relay answers");
    assert_eq!(code, 200, "{sent}");
    fake.wait("the route streams its first token", |s| s.tokens_sent == 1);
    // The member's page keeps polling through the relay until its chat ends or the relay goes.
    let id = sent["request_id"].clone();
    let poller = std::thread::spawn(move || {
        let (mut after, mut tokens, mut last) = (json!(0), Vec::new(), Value::Null);
        loop {
            let Some((code, events)) = room(
                port,
                "/chat/api/events",
                &json!({"request_id": id, "after": after}),
            ) else {
                return (tokens, last, false);
            };
            if code != 200 {
                return (tokens, events, false);
            }
            for t in events["tokens"].as_array().unwrap() {
                tokens.push(t.as_str().unwrap().to_owned());
            }
            after = events["next"].clone();
            if events["done"] == true {
                return (tokens, events, true);
            }
            last = events;
            std::thread::sleep(Duration::from_millis(50));
        }
    });
    // The owner's Stop through the relay: refused before any handler, and the run serves on.
    let refused = relay_call(
        port,
        "POST",
        "/api/demo/stop",
        &[
            ("Content-Type", "application/json"),
            ("X-Mycelium-Action", "1"),
        ],
        &json!({}),
    );
    assert_eq!(
        refused,
        Some((404, json!({"error": {"code": "relay_route_unknown"}})))
    );
    assert_eq!(page.status()["state"], "serving", "a relay Stop acted");
    assert_eq!(fake.seen(|s| s.deletes), 0);
    // The window's Quit, as the packaged composition assembles it.
    let Page {
        services,
        run,
        config,
        f,
        ..
    } = page;
    let relay = services
        .network()
        .relay_chat()
        .expect("the relay's chat handler");
    let (_core, mut cleanup) = crate::packaged::assemble(
        services,
        crate::app_host::ModelSource::hub(f.root.join("model-sources")),
    );
    crate::packaged::close_run_first(&mut cleanup, config.clone());
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    let began = Instant::now();
    use desktop::lifecycle::Cleanup as _;
    cleanup.bind_clock(began).unwrap();
    cleanup.begin(allowance).unwrap();
    let deadline = began + Duration::from_millis(allowance.millis());
    let verdict = loop {
        match cleanup.poll() {
            Ok(Poll::Pending) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(20))
            }
            other => break other,
        }
    };
    let took = began.elapsed();
    eprintln!(
        "CHATPAGE_QUIT_WHILE_RELAY_CHAT_STREAMS verdict={verdict:?} took_ms={}",
        took.as_millis()
    );
    assert_eq!(verdict, Ok(Poll::Verified), "Quit after {took:?}");
    assert!(took < Duration::from_millis(allowance.millis()), "{took:?}");
    let (tokens, last, done) = poller.join().expect("the member's poller ended");
    assert_eq!(tokens, ["a"], "{last}");
    if done {
        assert_eq!(
            (&last["terminal"], &last["error"]),
            (&json!("cancelled"), &json!("chat_cancelled")),
            "{last}"
        );
    }
    assert_eq!(
        fake.seen(|s| s.deletes),
        1,
        "the held chat's route call was cancelled once"
    );
    assert!(
        std::net::TcpStream::connect(("127.0.0.1", port)).is_err(),
        "Quit left the relay listening"
    );
    let stopped = config
        .observed()
        .and_then(|view| view.status())
        .map_or(Value::Null, |s| s.to_value());
    assert_eq!(stopped["state"], "stopped", "{stopped}");
    assert_eq!(stopped["run_id"], json!(run_id), "{stopped}");
    // The relay's handler itself, after Quit: the held chat ended `chat_cancelled`, a new send is
    // refused by Quit's gate.
    let call = |path: &str, body: Value| {
        let answer = relay(
            "POST",
            path,
            &[
                ("X-Mycelium-Chat".into(), "1".into()),
                ("Content-Type".into(), "application/json".into()),
            ],
            &serde_json::to_vec(&body).unwrap(),
        )
        .expect("the relay chat answers");
        (
            u16::try_from(answer.status).unwrap(),
            serde_json::from_slice::<Value>(&answer.raw).unwrap(),
        )
    };
    let (code, events) = call(
        "/chat/api/events",
        json!({"request_id": sent["request_id"], "after": 0}),
    );
    assert_eq!(code, 200, "{events}");
    assert_eq!(
        (&events["done"], &events["terminal"], &events["error"]),
        (&json!(true), &json!("cancelled"), &json!("chat_cancelled")),
        "{events}"
    );
    assert_eq!(
        call("/chat/api/send", json!({"prompt": "hi"})),
        (503, json!({"error": "application_closed"}))
    );
    lock(&run.runs).close();
    drop(f);
}
