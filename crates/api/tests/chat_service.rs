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
    RequestCancel, RouteGateway, RunChats, RunRoute, Streamed, Teardown, TimingFile,
};
use api::prompt_codec::ChatPrompt;
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
/// The timing file `path`, admitted under its own folder as the state root.
fn timing_file(path: &std::path::Path) -> TimingFile {
    TimingFile::under(path.parent().unwrap(), path).unwrap()
}
fn service(run: Arc<FakeRun>, timing: Option<PathBuf>) -> ChatService {
    ChatService::new(
        run,
        Arc::new(MonotonicClock::default()),
        timing.as_deref().map(timing_file),
    )
    .unwrap()
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
            // The run model's chat envelope (lane CHATFIX; this line pinned the raw text before).
            &json!(
                api::prompt_codec::PromptCodec::Qwen3
                    .chat("hi there")
                    .as_str()
            ),
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

/// Lane CAP item 3: a prompt the page accepts (at most 2,000 characters, at most 256 new tokens)
/// can still exceed the route's serving context: byte-level BPE makes 2,000 characters up to
/// 8,000 tokens. The route refuses it before any work (`400 joined_route_context_exceeds_worker`);
/// the page then names the limit (`chat_context_too_long`, and the context in its `limits`)
/// instead of showing the route's internal code.
#[test]
fn a_request_above_the_serving_context_is_refused_naming_the_limit() {
    let mut script = Script::completed(&["x"]);
    script.refuse_post = Some((400, "joined_route_context_exceeds_worker"));
    let fake = FakeGateway::start(script);
    let chat = service(run_over(http(&fake), Some(serving("run-1"))), None);
    assert_eq!(
        chat.state()["limits"]["context_tokens"],
        json!(api::model_catalog::serving_context_for("Qwen/Qwen3-0.6B").unwrap()),
        "the page states the model contract's serving context"
    );
    let sent = chat.send("local", prompt("hi", 256)).unwrap();
    let (tokens, last) = until_done(&chat, &id_of(&sent));
    assert!(tokens.is_empty());
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("failed"), &json!("chat_context_too_long"))
    );
    assert_eq!(fake.seen(|s| s.posts.len()), 1);
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
/// The page's code is pinned LITERALLY per reason (REVIEW-lanePH3B-S5 R1), from the shipped
/// expression `cancel_all('chat_switching_runs' if ctx.state.get('rollover_requested') else
/// (ctx.state.get('monitor_error_code') or 'owner_run_failed') if failed else 'chat_cancelled')`
/// (demo_chain.py:1332-1335 @ f4e366e9) and the ceiling's own `cancel_all(EXECUTION_CEILING_CODE)`
/// (demo_chain.py:1157-1164, `EXECUTION_CEILING_CODE = 'execution_ceiling_reached'` owner_run.py:44).
#[test]
fn the_runs_teardown_cancels_the_chat_with_the_reason_the_page_shows() {
    for (reason, page) in [
        // Plain Stop / Quit: `... if failed else 'chat_cancelled'` (demo_chain.py:1335).
        (Teardown::of(false, false, None), "chat_cancelled"),
        // Rollover: `'chat_switching_runs' if ctx.state.get('rollover_requested')` (:1333).
        (Teardown::of(true, false, None), "chat_switching_runs"),
        // Failed run, the monitor's dead-member code (:1334, set only at :1179-1181).
        (
            Teardown::of(false, true, Some("joined_route_member_stale")),
            "joined_route_member_stale",
        ),
        // Failed run without a monitor code: `or 'owner_run_failed'` (:1334).
        (Teardown::of(false, true, None), "owner_run_failed"),
        // The ceiling cancels its chats itself (demo_chain.py:1157-1164, owner_run.py:44).
        (Teardown::CeilingReached, "execution_ceiling_reached"),
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
            (&json!("cancelled"), &json!(page)),
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

/// A failed run's teardown reason (the monitor's dead-member code) reaches the page as the
/// chat's error (demo_chain.py:1332-1334 -> chat.py:367-369).
#[test]
fn a_failed_runs_teardown_code_reaches_the_page() {
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
        _: &ChatPrompt,
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
        Some(timing_file(&timing)),
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
    let refused = TimingFile::under(
        &root.0,
        &root
            .0
            .join("Mycelium A13 macOS Demo")
            .join("chat-timing.jsonl"),
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

/// Records every teardown reason the run's handle passes on.
#[derive(Default)]
struct Recorder(Mutex<Vec<Teardown>>);
impl ChatTeardown for Recorder {
    fn cancel_all(&self, reason: &Teardown) -> usize {
        self.0.lock().unwrap().push(reason.clone());
        1
    }
}
/// The run's handle keeps the `ctx.state` facts the shipped teardown reads and forgets them with a
/// fresh run (owner_run.py:392-399 copies only `execution_ceiling` and `rollover_binding`):
/// `rollover_requested` (owner_run.py:256) wins over a failure, the dead-member code
/// (demo_chain.py:1181) names a failed run, the ceiling cancels once per run
/// (`execution_ceiling_cancelled`, demo_chain.py:1159-1163) and is finished before the teardown's own
/// `cancel_all` (demo_chain.py:1329-1335).
#[test]
fn the_runs_handle_keeps_its_runs_facts_and_forgets_them_with_a_fresh_run() {
    let recorder = Arc::new(Recorder::default());
    let chats = RunChats::new(recorder.clone());
    chats.rollover_requested();
    assert_eq!(chats.teardown(true), 1);
    chats.new_run();
    assert_eq!(chats.teardown(false), 1);
    chats.dead_member("joined_route_member_stale");
    assert_eq!(chats.teardown(true), 1);
    chats.new_run();
    assert_eq!(chats.teardown(true), 1);
    assert!(chats.ceiling_reached());
    assert!(!chats.ceiling_reached(), "the ceiling cancels once per run");
    assert_eq!(chats.teardown(true), 1);
    chats.new_run();
    assert!(
        chats.ceiling_reached(),
        "a fresh run may cancel at its own ceiling"
    );
    chats.new_run();
    assert_eq!(
        recorder.0.lock().unwrap().as_slice(),
        [
            Teardown::SwitchingRuns,
            Teardown::Stopped,
            Teardown::Failed("joined_route_member_stale".into()),
            Teardown::Failed("owner_run_failed".into()),
            Teardown::CeilingReached,
            Teardown::Failed("owner_run_failed".into()),
            Teardown::CeilingReached,
        ]
    );
}

/// REVIEW-lanePH3B-S5 F4 (rulings 20/25): the timing file is admitted by its CANONICAL path (the
/// deepest existing ancestor resolved, symlinks followed) and compared case-insensitively, as the
/// default (case-insensitive) macOS volume does: nothing under v0.1.4's folders
/// (`Mycelium A13 macOS Demo`, __main__.py:67, installer.py:121; `Mycelium/programs`,
/// installer.py:117, launch_location.py:30), and nothing outside the state root the host was built
/// with. The reviewer's three cases: a lower-case variant, a symlinked parent, `Mycelium/programs`.
#[test]
fn the_timing_file_is_refused_by_its_canonical_path_and_outside_the_state_root() {
    let root = root();
    let state = root.0.join("state");
    std::fs::create_dir(&state).unwrap();
    let refused = |path: PathBuf| {
        TimingFile::under(&state, &path)
            .err()
            .map(|e| (e.status, e.code))
    };
    let refusal = Some((500, "chat_timing_path_refused"));
    // A lower-case variant of v0.1.4's state folder (the same folder on the default volume).
    let lower = state.join("lc").join("mycelium a13 macos demo");
    std::fs::create_dir_all(&lower).unwrap();
    assert_eq!(
        refused(lower.join("chat-timing.jsonl")),
        refusal,
        "lower-case"
    );
    // A symlinked parent whose target is v0.1.4's folder: lexically harmless.
    let demo = state.join("v014").join("Mycelium A13 macOS Demo");
    std::fs::create_dir_all(&demo).unwrap();
    std::os::unix::fs::symlink(&demo, state.join("evidence")).unwrap();
    assert_eq!(
        refused(state.join("evidence").join("chat-timing.jsonl")),
        refusal,
        "symlinked parent"
    );
    // v0.1.4's program folder, existing or not yet.
    assert_eq!(
        refused(state.join("Mycelium").join("programs").join("t.jsonl")),
        refusal,
        "Mycelium/programs"
    );
    // Outside the state root, also through `..` below a missing folder.
    assert_eq!(refused(root.0.join("elsewhere.jsonl")), refusal, "outside");
    assert_eq!(
        refused(state.join("missing").join("..").join("..").join("x.jsonl")),
        refusal,
        "dot-dot"
    );
    // A final component that is a symlink (an append would follow it).
    let target = state.join("target.jsonl");
    std::fs::write(&target, b"").unwrap();
    std::os::unix::fs::symlink(&target, state.join("link.jsonl")).unwrap();
    assert_eq!(refused(state.join("link.jsonl")), refusal, "symlink file");
    // Accepted: a plain file under the state root, also spelled through a symlinked ANCESTOR of the
    // root (`/var` -> `/private/var` on macOS: the default temp dir, not canonicalized).
    let real = state.join("real");
    std::fs::create_dir(&real).unwrap();
    assert_eq!(refused(real.join("chat-timing.jsonl")), None, "plain");
    let spelled = std::env::temp_dir()
        .join(root.0.file_name().unwrap())
        .join("state");
    assert_eq!(
        TimingFile::under(&spelled, &spelled.join("real").join("chat-timing.jsonl"))
            .err()
            .map(|e| e.code),
        None,
        "symlinked ancestor of the root"
    );
}

/// A timing file swapped for a symlink after it was admitted is not followed (O_NOFOLLOW): the
/// completed chat's line is not written through it (REVIEW-lanePH3B-S5 F4, `append`).
#[test]
fn a_timing_file_swapped_for_a_symlink_is_not_followed() {
    let root = root();
    let timing = root.0.join("chat-timing.jsonl");
    let file = TimingFile::under(&root.0, &timing).unwrap();
    let target = root.0.join("target.jsonl");
    std::fs::write(&target, b"").unwrap();
    std::os::unix::fs::symlink(&target, &timing).unwrap();
    let clock = Arc::new(ManualClock::default());
    let gateway = Arc::new(Scripted {
        clock: clock.clone(),
        at: vec![(100, "a")],
        end: 200,
    });
    let chat =
        ChatService::new(run_over(gateway, Some(serving("run-1"))), clock, Some(file)).unwrap();
    let sent = chat.send("local", prompt("hi", 1)).unwrap();
    let (_, last) = until_done(&chat, &id_of(&sent));
    assert_eq!(last["terminal"], "completed");
    assert_eq!(
        std::fs::read(&target).unwrap(),
        b"",
        "the symlink was followed"
    );
}

/// A gateway that panics inside `stream` (a defect in a later `RouteGateway`).
struct Panics;
impl RouteGateway for Panics {
    fn stream(
        &self,
        _: &ChatPrompt,
        _: u64,
        _: &RequestCancel,
        _: &mut dyn FnMut(&str),
    ) -> Result<Streamed, GatewayError> {
        panic!("test gateway panics inside stream")
    }
}
/// REVIEW-lanePH3B-S5 F6: a panic inside the gateway call ends that chat (`failed`,
/// `chat_generation_failed_panic`, as `failure_code` names an unexpected exception,
/// chat.py:202-207, 376-381) and the `finally` of `_work` (chat.py:383-398) still clears `busy`:
/// the next send is admitted, not `chat_busy`.
#[test]
fn a_gateway_that_panics_does_not_leave_the_service_busy() {
    let chat = service(run_over(Arc::new(Panics), Some(serving("run-1"))), None);
    let sent = chat.send("local", prompt("one", 1)).unwrap();
    let (_, last) = until_done(&chat, &id_of(&sent));
    assert_eq!(
        (&last["terminal"], &last["error"]),
        (&json!("failed"), &json!("chat_generation_failed_panic"))
    );
    let next = chat
        .send("local", prompt("two", 1))
        .err()
        .map(|e| e.error.code);
    assert_eq!(next, None, "the service stayed busy after a panic");
}

// ------------------------------------------------- lane CHATFIX: the model's chat envelope -----

/// FX-CHATFIX-prompt-codec: v0.1.4's `Qwen3PromptCodec` / `Qwen2PromptCodec` EXECUTED on the
/// pinned Qwen3-0.6B snapshot (`mycelium_live/codec.py:17-36,120-180,240-253 @ f4e366e9`).
const CODEC_FIXTURE: &str = include_str!("fixtures/prompt-codec-oracle.json");
/// A gateway that records the exact prompt text each chat hands it, then completes.
#[derive(Default)]
struct Recording(Mutex<Vec<String>>);
impl RouteGateway for Recording {
    fn stream(
        &self,
        prompt: &ChatPrompt,
        _: u64,
        _: &RequestCancel,
        on_token: &mut dyn FnMut(&str),
    ) -> Result<Streamed, GatewayError> {
        self.0.lock().unwrap().push(prompt.as_str().to_owned());
        on_token("ok");
        Ok(Streamed {
            terminal: Some("completed"),
            code: None,
        })
    }
}
/// The run serving `run-1` with `model` as its run model (`app.run_model_contract().model_id`).
struct ModelRun {
    model: Option<&'static str>,
    gateway: Arc<dyn RouteGateway>,
}
fn model_service(model: Option<&'static str>, gateway: Arc<Recording>) -> ChatService {
    ChatService::new(
        Arc::new(ModelRun { model, gateway }),
        Arc::new(MonotonicClock::default()),
        None,
    )
    .unwrap()
}
impl RunRoute for ModelRun {
    fn status(&self) -> Option<Value> {
        Some(serving("run-1"))
    }
    fn gateway(&self, _: &str) -> Option<Arc<dyn RouteGateway>> {
        Some(self.gateway.clone())
    }
    fn model_id(&self) -> Option<String> {
        self.model.map(str::to_owned)
    }
}

/// Lane CHATFIX item 1 (ledger row 112 a): v0.1.4's gateway encodes every chat prompt with the
/// deployment's codec (`RouterSessionBackend._run`, backend.py:196-209: `codec.encode(prompt)` =
/// `TEMPLATE.format(system=_SYSTEM, prompt=prompt)` then the tokenizer, codec.py:153-157); the
/// codec is chosen by architecture (`prompt_codec_from_deployment`, codec.py:270-287): `qwen3` ->
/// ChatML + system + the closed empty think block, `qwen2` -> ChatML + system. rv40's route chat
/// sent the user's RAW text, so the model continued it. Every registered model's chat must reach
/// the gateway as exactly the executed codec's text, byte for byte, for every probed prompt.
#[test]
fn every_chat_reaches_the_gateway_in_the_run_models_v014_codec_envelope() {
    let fixture: Value = serde_json::from_str(CODEC_FIXTURE).unwrap();
    for (model, codec) in [
        ("Qwen/Qwen3-0.6B", "qwen3"),
        ("Qwen/Qwen3-8B", "qwen3"),
        ("Qwen/Qwen3-14B", "qwen3"),
        ("Qwen/Qwen2.5-0.5B-Instruct", "qwen2"),
        ("Qwen/Qwen2.5-7B-Instruct", "qwen2"),
    ] {
        let gateway = Arc::new(Recording::default());
        let chat = model_service(Some(model), gateway.clone());
        let mut expected = Vec::new();
        for (i, case) in fixture[codec]["cases"]
            .as_array()
            .unwrap()
            .iter()
            .enumerate()
        {
            let user = case["user"].as_str().unwrap();
            if user.trim().is_empty() || !case["policy_response"].is_null() {
                // The body check refuses a blank prompt before the service; the policy's
                // fixed answers never reach the gateway (item 1b, its own test).
                continue;
            }
            let sent = chat
                .send(&format!("client-{i}"), prompt(user, 4))
                .unwrap_or_else(|e| panic!("{model} {user:?}: {:?}", e.error));
            let (tokens, last) = until_done(&chat, &id_of(&sent));
            assert_eq!(last["terminal"], "completed", "{model} {user:?}: {last}");
            assert_eq!(tokens, ["ok"], "{model} {user:?}");
            expected.push(case["templated"].as_str().unwrap().to_owned());
        }
        assert_eq!(*gateway.0.lock().unwrap(), expected, "{model}");
    }
}

/// Lane CHATFIX item 1: a run model with no v0.1.4 codec (`prompt_codec_from_deployment` raises
/// `unsupported_prompt_codec:<model_type>`, codec.py:287) is refused by name; its prompt never
/// reaches the gateway raw. So is a run whose model is not known at all.
#[test]
fn a_model_without_a_codec_is_refused_by_name_and_never_sent_raw() {
    for model in [Some("meta-llama/Llama-3.1-8B-Instruct"), None] {
        let gateway = Arc::new(Recording::default());
        let chat = model_service(model, gateway.clone());
        let refused = chat
            .send("local", prompt("What is the capital of France?", 4))
            .err()
            .map(|e| (e.error.status, e.error.code));
        assert_eq!(
            refused,
            Some((503, "unsupported_prompt_codec")),
            "{model:?}"
        );
        assert!(gateway.0.lock().unwrap().is_empty(), "{model:?}");
        assert_eq!(
            chat.busy(),
            false,
            "{model:?}: a refused send leaves the service free"
        );
    }
}

/// Lane CHATFIX: the seam's codecs against v0.1.4's EXECUTED codecs (FX-CHATFIX-prompt-codec):
/// the text of every probed prompt byte for byte (braces, Unicode, ChatML-looking text and
/// whitespace stay verbatim), the protocol, the stop ids, and the selection by architecture
/// (`prompt_codec_from_deployment` executed on each `model_type`).
#[test]
fn the_seams_codecs_are_v014s_executed_codecs() {
    use api::prompt_codec::{PromptCodec, QWEN_STOP_TOKEN_IDS, UNSUPPORTED};
    let fixture: Value = serde_json::from_str(CODEC_FIXTURE).unwrap();
    assert_eq!(
        fixture["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    assert_eq!(fixture["oracle_tree_dirty"], false);
    for (codec, name) in [(PromptCodec::Qwen3, "qwen3"), (PromptCodec::Qwen2, "qwen2")] {
        let oracle = &fixture[name];
        assert_eq!(codec.protocol(), oracle["protocol"], "{name}");
        assert_eq!(
            json!(codec.stop_token_ids()),
            oracle["stop_token_ids"],
            "{name}"
        );
        let cases = oracle["cases"].as_array().unwrap();
        assert_eq!(cases.len(), 12, "{name}");
        for case in cases {
            let user = case["user"].as_str().unwrap();
            assert_eq!(
                codec.chat(user).as_str(),
                case["templated"],
                "{name} {user:?}"
            );
        }
    }
    assert_eq!(QWEN_STOP_TOKEN_IDS, [151_643, 151_645]);
    let selection = &fixture["selection_by_model_type"];
    for (model_type, codec) in [
        ("qwen2", Some(PromptCodec::Qwen2)),
        ("qwen3", Some(PromptCodec::Qwen3)),
        ("llama", None),
        ("mistral", None),
    ] {
        let ours = PromptCodec::for_architecture(model_type).map_err(|e| e.code);
        match codec {
            Some(codec) => {
                assert_eq!(ours, Ok(codec), "{model_type}");
                let class = if codec == PromptCodec::Qwen3 {
                    "Qwen3PromptCodec"
                } else {
                    "Qwen2PromptCodec"
                };
                assert_eq!(selection[model_type], class, "{model_type}");
            }
            None => {
                assert_eq!(ours, Err(UNSUPPORTED), "{model_type}");
                assert!(
                    selection[model_type]
                        .as_str()
                        .unwrap()
                        .starts_with("refused:unsupported_prompt_codec:"),
                    "{model_type}"
                );
            }
        }
    }
    // v0.1.4 also selects Qwen3.5 (`qwen3_5`, `qwen3_5_moe`); no registered v0.2 model has it, its
    // tokenizer and stop ids differ, so the seam refuses it by name rather than guessing.
    assert_eq!(selection["qwen3_5"], "Qwen35PromptCodec");
    assert_eq!(
        PromptCodec::for_architecture("qwen3_5").map_err(|e| e.code),
        Err(UNSUPPORTED)
    );
    // The snapshot the fixture was executed on selects Qwen3, as the catalog's contract does.
    assert_eq!(fixture["snapshot"]["model_type"], "qwen3");
    assert_eq!(
        PromptCodec::for_model("Qwen/Qwen3-0.6B").map_err(|e| e.code),
        Ok(PromptCodec::Qwen3)
    );
}
