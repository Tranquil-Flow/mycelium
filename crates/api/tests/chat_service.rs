// SPDX-License-Identifier: AGPL-3.0-only
//! Slice S5: the shipped `ChatService` (mycelium_a13_macos/chat.py:211-500 @ f4e366e9) over the
//! run's route gateway. Chain: `_Handler.do_POST` app.py:1936-1950 -> `DemoApp.chat_route`
//! app.py:1926 -> `chat.handle` chat.py:553 -> `ChatService.send` :300-340 -> `_work` :342-398 ->
//! `RegistryClient.stream` :106-147; the run's teardown demo_chain.py:1322-1335 -> `cancel_all`
//! :442-445. A loopback fake gateway streams SSE; the run is a test double of the `RunRoute` seam.
#[path = "support/fake_route_gateway.rs"]
mod fake_route_gateway;
use api::chat_service::{
    ChatService, ChatTeardown, Clock, GatewayError, HttpRouteGateway, MonotonicClock, Prompt,
    RequestCancel, RouteGateway, RunRoute, Streamed, Teardown,
};
use budgets::{BudgetId as B, Entries};
use fake_route_gateway::{End, FakeGateway, Script};
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, AtomicUsize, Ordering::SeqCst},
    },
    time::{Duration, Instant},
};
fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
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
    let path = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "chat-service-{}",
        api::app_host::hex(&api::app_host::random(16).unwrap())
    ));
    std::fs::create_dir(&path).unwrap();
    TestRoot(path)
}
/// The owner run as a test double of the seam: its status (owner_run.py:188-214 fields the chat
/// reads) and the route gateway it hands out, counting how often it was asked.
struct FakeRun {
    status: Mutex<Option<Value>>,
    gateway: Arc<dyn RouteGateway>,
    asked: AtomicUsize,
}
impl RunRoute for FakeRun {
    fn status(&self) -> Option<Value> {
        self.status.lock().unwrap().clone()
    }
    fn gateway(&self, _run_id: &str) -> Option<Arc<dyn RouteGateway>> {
        self.asked.fetch_add(1, SeqCst);
        Some(self.gateway.clone())
    }
    fn model_id(&self) -> Option<String> {
        Some("Qwen/Qwen3-0.6B".into())
    }
}
fn serving(run_id: &str) -> Value {
    json!({"run_id":run_id,"state":"serving","step":"serving","error_code":null,
        "rollover":{"state":"idle","completed":0,"consecutive_failures":0},
        "participants":[
            {"label":"This Mac (owner)","node_id":"a".repeat(64),"layers":[0,14]},
            {"label":"Mac B","node_id":"b".repeat(64),"layers":[14,28]}]})
}
fn run_over(gateway: Arc<dyn RouteGateway>, status: Option<Value>) -> Arc<FakeRun> {
    Arc::new(FakeRun {
        status: Mutex::new(status),
        gateway,
        asked: AtomicUsize::new(0),
    })
}
fn http(fake: &FakeGateway) -> Arc<dyn RouteGateway> {
    Arc::new(HttpRouteGateway::new(fake.addr(), fake.bearer()))
}
fn service(run: Arc<FakeRun>, timing: Option<PathBuf>) -> ChatService {
    ChatService::new(run, Arc::new(MonotonicClock::default()), timing).unwrap()
}
fn prompt(text: &str, max_new_tokens: u64) -> Prompt {
    Prompt {
        text: text.into(),
        max_new_tokens,
        clamped: false,
    }
}
fn id_of(sent: &Value) -> String {
    sent["request_id"].as_str().unwrap().to_owned()
}
/// Poll the chat's events (as chat.js does) until it is done; returns (tokens, last page).
fn until_done(chat: &ChatService, id: &str) -> (Vec<String>, Value) {
    let end = Instant::now() + span(B::WorkerStartup);
    let (mut after, mut tokens) = (0u64, Vec::new());
    loop {
        let page = chat
            .events(&json!({"request_id":id,"after":after}))
            .unwrap_or_else(|e| panic!("events refused: {:?}", e.error));
        for t in page["tokens"].as_array().unwrap() {
            tokens.push(t.as_str().unwrap().to_owned());
        }
        after = page["next"].as_u64().unwrap();
        if page["done"] == true {
            return (tokens, page);
        }
        assert!(Instant::now() < end, "the chat never ended: {page}");
        std::thread::yield_now();
    }
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

#[test]
fn a_chat_streams_the_gateways_tokens_into_its_events_with_the_runs_served_by() {
    let root = root();
    let timing = root.0.join("chat-timing.jsonl");
    let fake = FakeGateway::start(Script::completed(&["Hel", "lo", "!"]));
    let run = run_over(http(&fake), Some(serving("run-1")));
    let chat = service(run, Some(timing.clone()));
    assert!(chat.serving());
    let sent = chat
        .send("local", prompt("hi there", 3))
        .unwrap_or_else(|e| panic!("send refused: {:?}", e.error));
    let served = json!([{"label":"This Mac (owner)","node":"a".repeat(12),"layers":[0,14]},
        {"label":"Mac B","node":"b".repeat(12),"layers":[14,28]}]);
    assert_eq!(
        (
            &sent["max_new_tokens"],
            &sent["clamped"],
            &sent["served_by"]
        ),
        (&json!(3), &json!(false), &served)
    );
    let (tokens, last) = until_done(&chat, &id_of(&sent));
    assert_eq!(tokens, ["Hel", "lo", "!"]);
    assert_eq!(
        (&last["terminal"], &last["error"], &last["served_by"]),
        (&json!("completed"), &Value::Null, &served)
    );
    assert!(last["seconds"].is_number(), "{last}");
    let post = fake.seen(|s| s.posts.clone());
    assert_eq!(post.len(), 1);
    assert_eq!(
        (
            &post[0]["protocol"],
            &post[0]["prompt"],
            &post[0]["max_new_tokens"],
            &post[0]["qualification"]["qualification_id"],
            &post[0]["workload_profile_id"],
            &post[0]["qos_class"]
        ),
        (
            &json!("mycelium.request_gateway.v2"),
            &json!("hi there"),
            &json!(3),
            &json!("fake-route"),
            &json!("interactive_chat_v1"),
            &json!("interactive")
        )
    );
    // v0.2-only per-chat timing record (fields = chat.py:392-395), one line per completed chat.
    let lines = timing_lines(&timing);
    assert_eq!(lines.len(), 1, "{lines:?}");
    let line = lines[0].as_object().unwrap();
    let mut keys: Vec<_> = line.keys().cloned().collect();
    keys.sort();
    assert_eq!(
        keys,
        ["decode_millitokens_s", "request_ms", "run_id", "ttft_ms"]
    );
    assert_eq!(line["run_id"], "run-1");
    assert!(
        line["ttft_ms"].is_u64() && line["request_ms"].is_u64(),
        "{line:?}"
    );
}

#[test]
fn one_chat_at_a_time_a_second_send_is_chat_busy() {
    let fake = FakeGateway::start(Script::ending(&["a"], End::Hold));
    let chat = service(run_over(http(&fake), Some(serving("run-1"))), None);
    let first = chat.send("local", prompt("one", 4)).unwrap();
    let second = chat.send("local", prompt("two", 4)).err().map(|e| e.error);
    assert_eq!(second.map(|e| (e.status, e.code)), Some((429, "chat_busy")));
    assert!(chat.busy());
    fake.release();
    let (_, last) = until_done(&chat, &id_of(&first));
    assert_eq!(last["terminal"], "completed");
    assert!(!chat.busy());
    assert_eq!(fake.seen(|s| s.posts.len()), 1);
}

#[test]
fn two_chats_of_one_run_use_the_gateway_the_run_handed_out_once() {
    let fake = FakeGateway::start(Script::completed(&["x"]));
    let run = run_over(http(&fake), Some(serving("run-1")));
    let chat = service(run.clone(), None);
    for text in ["first", "second"] {
        let sent = chat.send("local", prompt(text, 1)).unwrap();
        assert_eq!(until_done(&chat, &id_of(&sent)).1["terminal"], "completed");
    }
    assert_eq!(fake.seen(|s| s.posts.len()), 2);
    assert_eq!(
        run.asked.load(SeqCst),
        1,
        "the run's gateway is resolved per run, not per chat (EP-PLAN U7)"
    );
    // A new run (rollover) is asked again.
    *run.status.lock().unwrap() = Some(serving("run-2"));
    let sent = chat.send("local", prompt("third", 1)).unwrap();
    assert_eq!(until_done(&chat, &id_of(&sent)).1["terminal"], "completed");
    assert_eq!(run.asked.load(SeqCst), 2);
}

#[test]
fn without_a_serving_run_a_send_is_refused_as_the_page_explains() {
    let fake = FakeGateway::start(Script::completed(&["x"]));
    let run = run_over(http(&fake), None);
    let chat = service(run.clone(), None);
    let refused = |chat: &ChatService| {
        chat.send("local", prompt("hi", 1))
            .err()
            .map(|e| (e.error.status, e.error.code, e.retry_after))
    };
    assert_eq!(refused(&chat), Some((503, "chat_not_ready", None)));
    assert_eq!(chat.state()["available"], false);
    *run.status.lock().unwrap() = Some(
        json!({"run_id":"run-1","state":"running","step":"activate","rollover":{"state":"idle"}}),
    );
    assert_eq!(refused(&chat), Some((503, "chat_warming_up", None)));
    assert_eq!(chat.state()["warming_up"], true);
    *run.status.lock().unwrap() = Some(
        json!({"run_id":"run-1","state":"running","step":"descriptors","rollover":{"state":"switching"}}),
    );
    assert_eq!(
        refused(&chat),
        Some((
            503,
            "chat_switching_runs",
            Some(span(B::PyFinalMyceliumA13MacosChatRetryAfterSL315C60))
        ))
    );
    let state = chat.state();
    assert_eq!(
        (
            &state["switching_runs"],
            &state["model_id"],
            &state["served_by"]
        ),
        (&json!(true), &Value::Null, &json!([]))
    );
    assert_eq!(fake.seen(|s| s.posts.len()), 0);
}

#[test]
fn state_reports_the_runs_model_and_served_by_while_serving() {
    let fake = FakeGateway::start(Script::completed(&["x"]));
    let chat = service(run_over(http(&fake), Some(serving("run-1"))), None);
    let state = chat.state();
    assert_eq!(
        (
            &state["available"],
            &state["busy"],
            &state["model_id"],
            &state["served_by"][1]["label"],
            &state["limits"]["max_new_tokens"]
        ),
        (
            &json!(true),
            &json!(false),
            &json!("Qwen/Qwen3-0.6B"),
            &json!("Mac B"),
            &json!(256)
        )
    );
}

#[test]
fn an_unqualified_route_fails_the_chat_chat_route_not_qualified() {
    let mut script = Script::completed(&["x"]);
    script.qualified = false;
    let fake = FakeGateway::start(script);
    let chat = service(run_over(http(&fake), Some(serving("run-1"))), None);
    let sent = chat.send("local", prompt("hi", 1)).unwrap();
    let (_, last) = until_done(&chat, &id_of(&sent));
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("failed"), &json!("chat_route_not_qualified"))
    );
    assert_eq!(fake.seen(|s| s.posts.len()), 0);
}

#[test]
fn a_failed_terminal_keeps_the_routers_code_and_records_no_timing() {
    let root = root();
    let timing = root.0.join("chat-timing.jsonl");
    let fake = FakeGateway::start(Script::ending(
        &["a"],
        End::Failed("joined_route_member_stale"),
    ));
    let chat = service(
        run_over(http(&fake), Some(serving("run-1"))),
        Some(timing.clone()),
    );
    let sent = chat.send("local", prompt("hi", 2)).unwrap();
    let (tokens, last) = until_done(&chat, &id_of(&sent));
    assert_eq!(tokens, ["a"]);
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("failed"), &json!("joined_route_member_stale"))
    );
    assert_eq!(timing_lines(&timing), Vec::<Value>::new());
}

#[test]
fn a_silent_gateway_fails_the_chat_after_the_idle_timeout() {
    let fake = FakeGateway::start(Script::ending(&["a"], End::Stall));
    // The shipped client's 300 s socket timeout (chat.py:70) is a constructor budget; this test
    // gives it a short typed row so the stall ends within the test.
    let gateway = Arc::new(HttpRouteGateway::with_idle(
        fake.addr(),
        fake.bearer(),
        B::ModelSelection,
    ));
    let chat = service(run_over(gateway, Some(serving("run-1"))), None);
    let sent = chat.send("local", prompt("hi", 2)).unwrap();
    let (tokens, last) = until_done(&chat, &id_of(&sent));
    assert_eq!(tokens, ["a"]);
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (
            &json!("failed"),
            &json!("chat_generation_failed_timeouterror")
        )
    );
}

/// The run's teardown cancels the chat with the reason the page shows (chat.js:37-41):
/// Stop `chat_cancelled`, rollover `chat_switching_runs`, ceiling `execution_ceiling_reached`
/// (demo_chain.py:1322-1335 -> chat.py:442-445 -> :418-440 -> gateway DELETE; :367-369).
#[test]
fn the_runs_teardown_cancels_the_chat_with_the_reason_the_page_shows() {
    for reason in [
        Teardown::Stopped,
        Teardown::SwitchingRuns,
        Teardown::CeilingReached,
    ] {
        let root = root();
        let timing = root.0.join("chat-timing.jsonl");
        let fake = FakeGateway::start(Script::ending(&["a", "b"], End::Hold));
        let chat = service(
            run_over(http(&fake), Some(serving("run-1"))),
            Some(timing.clone()),
        );
        let sent = chat.send("local", prompt("hi", 8)).unwrap();
        fake.wait("both tokens sent", |s| s.tokens_sent == 2);
        let hook: &dyn ChatTeardown = &chat;
        assert_eq!(hook.cancel_all(&reason), 1, "{reason:?}");
        let (tokens, last) = until_done(&chat, &id_of(&sent));
        assert_eq!(tokens, ["a", "b"], "{reason:?}");
        assert_eq!(
            (&last["terminal"], &last["error"]),
            (&json!("cancelled"), &json!(reason.code())),
            "{reason:?}"
        );
        assert_eq!(fake.seen(|s| s.deletes), 1, "{reason:?}");
        assert_eq!(timing_lines(&timing), Vec::<Value>::new(), "{reason:?}");
        assert_eq!(hook.cancel_all(&reason), 0, "nothing left to cancel");
    }
}

/// The teardown's reason is the shipped expression over the run's context (demo_chain.py:1332-1334
/// @ f4e366e9): rollover requested wins, a failed run shows the monitor's dead-member code or
/// `owner_run_failed`, a plain Stop `chat_cancelled`.
#[test]
fn the_teardown_reason_is_the_shipped_expression() {
    assert_eq!(Teardown::of(false, false, None), Teardown::Stopped);
    assert_eq!(Teardown::of(true, false, None), Teardown::SwitchingRuns);
    assert_eq!(
        Teardown::of(true, true, Some("authority_state_write_failed")),
        Teardown::SwitchingRuns
    );
    assert_eq!(
        Teardown::of(false, true, Some("authority_state_write_failed")),
        Teardown::Failed("authority_state_write_failed".into())
    );
    assert_eq!(
        Teardown::of(false, true, None),
        Teardown::Failed("owner_run_failed".into())
    );
    assert_eq!(
        Teardown::of(false, false, Some("ignored")),
        Teardown::Stopped
    );
}

/// The teardown's reason from the run's status (demo_chain.py:1329-1331): rollover requested ->
/// `chat_switching_runs`; failed -> the monitor's code (a serving failure: the ceiling,
/// `joined_route_member_stale`, ...) else `owner_run_failed`; a plain Stop -> `chat_cancelled`.
/// A failed run's code reaches the page as the chat's error.
#[test]
fn the_teardown_reason_follows_the_runs_status_and_reaches_the_page() {
    let status = |rollover: &str, failed_step: Value, error_code: Value| {
        json!({"run_id":"run-1","state":"stopping","rollover":{"state":rollover},
            "failed_step":failed_step,"error_code":error_code})
    };
    let of = Teardown::of_run;
    assert_eq!(
        of(false, &status("idle", Value::Null, Value::Null)),
        Teardown::Stopped
    );
    assert_eq!(
        of(false, &status("switching", Value::Null, Value::Null)),
        Teardown::SwitchingRuns
    );
    assert_eq!(
        of(
            true,
            &status("idle", json!("serving"), json!("execution_ceiling_reached"))
        ),
        Teardown::CeilingReached
    );
    assert_eq!(
        of(
            true,
            &status("idle", json!("serving"), json!("joined_route_member_stale"))
        ),
        Teardown::Failed("joined_route_member_stale".into())
    );
    assert_eq!(
        of(
            true,
            &status("idle", json!("descriptors"), json!("two_macs_required"))
        ),
        Teardown::Failed("owner_run_failed".into())
    );
    let fake = FakeGateway::start(Script::ending(&["a"], End::Hold));
    let chat = service(run_over(http(&fake), Some(serving("run-1"))), None);
    let sent = chat.send("local", prompt("hi", 8)).unwrap();
    fake.wait("the token sent", |s| s.tokens_sent == 1);
    let hook: &dyn ChatTeardown = &chat;
    hook.cancel_all(&Teardown::Failed("joined_route_member_stale".into()));
    let (_, last) = until_done(&chat, &id_of(&sent));
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("cancelled"), &json!("joined_route_member_stale"))
    );
}

/// A cancel that arrives before the gateway accepted the request is sent on acceptance
/// (chat.py:349-354, 161-175).
#[test]
fn a_cancel_before_the_gateway_accepted_is_sent_on_acceptance() {
    let cancel = RequestCancel::default();
    assert!(cancel.cancel(), "a cancel before acceptance is remembered");
    let sent = Arc::new(AtomicUsize::new(0));
    let counter = sent.clone();
    cancel.arm(Box::new(move || {
        counter.fetch_add(1, SeqCst);
        Ok(true)
    }));
    assert_eq!(sent.load(SeqCst), 1, "sent on acceptance");
    assert!(cancel.cancel(), "a second cancel is a no-op success");
    assert_eq!(sent.load(SeqCst), 1, "sent once");
}

/// A clock the test moves.
#[derive(Default)]
struct ManualClock(AtomicU64);
impl ManualClock {
    fn set(&self, millis: u64) {
        self.0.store(millis, SeqCst);
    }
}
impl Clock for ManualClock {
    fn now(&self) -> Duration {
        Duration::from_millis(self.0.load(SeqCst))
    }
}
/// A gateway that plays tokens at scripted clock times.
struct Scripted {
    clock: Arc<ManualClock>,
    at: Vec<(u64, &'static str)>,
    end: u64,
}
impl RouteGateway for Scripted {
    fn stream(
        &self,
        _: &str,
        _: u64,
        _: &RequestCancel,
        on_token: &mut dyn FnMut(&str),
    ) -> Result<Streamed, GatewayError> {
        for (millis, text) in &self.at {
            self.clock.set(*millis);
            on_token(text);
        }
        self.clock.set(self.end);
        Ok(Streamed {
            terminal: Some("completed"),
            code: None,
        })
    }
}
/// `_work`'s performance record (chat.py:386-395, swarm_stats.milliseconds :27-30): TTFT from the
/// send, decode rate = (tokens-1)*1000/(last-first) in millitokens/s, request time to the end.
#[test]
fn the_timing_record_is_the_shipped_arithmetic() {
    let root = root();
    let timing = root.0.join("chat-timing.jsonl");
    let clock = Arc::new(ManualClock::default());
    clock.set(1_000);
    let gateway = Arc::new(Scripted {
        clock: clock.clone(),
        at: vec![(1_100, "a"), (1_200, "b"), (1_300, "c")],
        end: 1_500,
    });
    let chat = ChatService::new(
        run_over(gateway, Some(serving("run-7"))),
        clock,
        Some(timing.clone()),
    )
    .unwrap();
    let sent = chat.send("local", prompt("hi", 3)).unwrap();
    let (_, last) = until_done(&chat, &id_of(&sent));
    assert_eq!(last["seconds"], json!(0.5));
    assert_eq!(
        timing_lines(&timing),
        [json!({"ttft_ms":100,"decode_millitokens_s":10000,"request_ms":500,"run_id":"run-7"})]
    );
}

/// Rulings 20/25: v0.2 never writes under v0.1.4's folder.
#[test]
fn the_timing_file_is_refused_under_the_v014_app_folder() {
    let root = root();
    let fake = FakeGateway::start(Script::completed(&["x"]));
    let refused = ChatService::new(
        run_over(http(&fake), Some(serving("run-1"))),
        Arc::new(MonotonicClock::default()),
        Some(
            root.0
                .join("Mycelium A13 macOS Demo")
                .join("chat-timing.jsonl"),
        ),
    )
    .err()
    .map(|e| e.code);
    assert_eq!(refused, Some("chat_timing_path_refused"));
}

/// `_rate` (chat.py:285-298): 4 accepted sends per client per 60 s.
#[test]
fn the_fifth_send_of_a_client_in_one_window_is_rate_limited() {
    let clock = Arc::new(ManualClock::default());
    let fake = FakeGateway::start(Script::completed(&["x"]));
    let chat = ChatService::new(
        run_over(http(&fake), Some(serving("run-1"))),
        clock.clone(),
        None,
    )
    .unwrap();
    for _ in 0..4 {
        let sent = chat.send("local", prompt("hi", 1)).unwrap();
        until_done(&chat, &id_of(&sent));
    }
    let fifth = chat
        .send("local", prompt("hi", 1))
        .err()
        .map(|e| e.error.code);
    assert_eq!(fifth, Some("chat_rate_limited"));
    clock.set(u64::try_from(span(B::ChatRateWindow).as_millis()).unwrap());
    let sent = chat.send("local", prompt("hi", 1)).unwrap();
    until_done(&chat, &id_of(&sent));
}

#[test]
fn events_refuse_a_malformed_body_and_an_unknown_request() {
    let fake = FakeGateway::start(Script::completed(&["x"]));
    let chat = service(run_over(http(&fake), Some(serving("run-1"))), None);
    let code = |body: Value| {
        chat.events(&body)
            .err()
            .map(|e| (e.error.status, e.error.code))
    };
    assert_eq!(
        code(json!({"request_id":"x"})),
        Some((400, "chat_request_invalid"))
    );
    assert_eq!(
        code(json!({"request_id":"AAAAAAAAAAAAAAAAAAAAAAAA","after":-1})),
        Some((400, "chat_request_invalid"))
    );
    assert_eq!(
        code(json!({"request_id":"AAAAAAAAAAAAAAAAAAAAAAAA","after":0})),
        Some((404, "chat_request_unknown"))
    );
}
