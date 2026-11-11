// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "desktop-shell")]
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
    sync::Arc,
    time::{Duration, Instant},
};
struct MustNotRun;
impl Driver for MustNotRun {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        panic!("refused desktop request started native work")
    }
}
/// A fresh private state root for the packaged action host (these chat paths never write it).
fn private() -> std::path::PathBuf {
    use std::os::unix::fs::DirBuilderExt;
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "desktop-host-private-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    root
}
fn host() -> DesktopServices {
    DesktopServices::new(
        NodeService::new(
            Arc::new(MustNotRun),
            "private-local-token".into(),
            node_service::native::development_qualification().unwrap(),
        ),
        NonZeroUsize::new(2).unwrap(),
        &private(),
    )
}
fn call(host: &DesktopServices, path: &str, body: Value) -> desktop::core::Reply {
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
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
        Box::new(move |reply| tx.send(reply).unwrap()),
    );
    rx.recv_timeout(Duration::from_millis(
        B::WorkerStartup.span(Entries::ZERO).unwrap().millis(),
    ))
    .unwrap()
}
#[test]
fn native_service_callback_validates_chat_without_fixture_inference() {
    let host = host();
    let state = call(&host, "/chat/api/state", json!({}));
    assert_eq!(state.status, 200);
    assert_eq!(
        serde_json::from_slice::<Value>(&state.body).unwrap()["available"],
        true
    );
    assert_eq!(
        call(
            &host,
            "/chat/api/send",
            json!({"prompt":true,"max_new_tokens":1})
        )
        .status,
        400
    );
    assert_eq!(
        call(
            &host,
            "/chat/api/events",
            json!({"request_id":"missing","after":0})
        )
        .status,
        404
    );
    host.begin_shutdown(
        Instant::now()
            + Duration::from_millis(B::StageCleanup.span(Entries::ZERO).unwrap().millis()),
    )
    .unwrap();
    while host.poll_shutdown().unwrap() == Poll::Pending {
        std::thread::yield_now();
    }
}
#[test]
fn native_shutdown_fences_chat_before_callback_or_backend_effects() {
    let host = host();
    host.begin_shutdown(
        Instant::now()
            + Duration::from_millis(B::StageCleanup.span(Entries::ZERO).unwrap().millis()),
    )
    .unwrap();
    let response = call(
        &host,
        "/chat/api/send",
        json!({"prompt":"must not run","max_new_tokens":1}),
    );
    assert_eq!(response.status, 503);
    assert_eq!(
        serde_json::from_slice::<Value>(&response.body).unwrap()["error"],
        "application_closed"
    );
    assert_eq!(host.poll_shutdown().unwrap(), Poll::Verified);
}

/// Round 6: page-fetch receipts are an optional observation. Turning them off must leave
/// the scripted chat flow identical, so the flake can be bisected on receipts alone.
#[cfg(debug_assertions)]
#[test]
fn probe_script_page_receipts_are_switchable_without_touching_the_flow() {
    let on = node_service::desktop_host::probe_script("\"p\"", "\"3\"", true);
    let off = node_service::desktop_host::probe_script("\"p\"", "\"3\"", false);
    assert!(on.contains("const pageReceipts = true;"));
    assert!(off.contains("const pageReceipts = false;"));
    assert_eq!(
        on.replace("const pageReceipts = true;", "const pageReceipts = false;"),
        off
    );
    assert!(!off.contains("__"), "every placeholder is substituted");
}

const TEMPLATE_FIXTURE: &str = include_str!("fixtures/qwen2-chat-template-oracle.json");
struct Capture(Arc<std::sync::Mutex<Vec<String>>>);
impl Driver for Capture {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        self.0
            .lock()
            .unwrap()
            .push(job.document["prompt"].as_str().unwrap().to_owned());
        Ok(())
    }
}
/// FX-L11-chat-template (executed `Qwen2PromptCodec` of v0.1.4 @ f4e366e9, probe sha256 in the
/// fixture). v0.1.4's chat page sends the user's plain prompt and the gateway codec wraps it in
/// the Qwen2 system/user/assistant envelope (`mycelium_live/codec.py:153-157`); the native service
/// takes a finished ChatML string, so the chat edge must do that wrapping. Without it the
/// packaged chat answered "\n\n" (round 9): the page-trimmed ChatML ended without its assistant
/// newline, so the model's first token was whitespace.
#[test]
fn chat_send_hands_the_native_service_the_v014_qwen2_chatml_of_the_user_prompt() {
    let fixture: Value = serde_json::from_str(TEMPLATE_FIXTURE).unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        let seen = Arc::new(std::sync::Mutex::new(Vec::new()));
        let host = DesktopServices::new(
            NodeService::new(
                Arc::new(Capture(seen.clone())),
                "private-local-token".into(),
                node_service::native::development_qualification().unwrap(),
            ),
            NonZeroUsize::new(2).unwrap(),
            &private(),
        );
        let user = case["user"].as_str().unwrap();
        let reply = call(
            &host,
            "/chat/api/send",
            json!({"prompt":user,"max_new_tokens":1}),
        );
        assert_eq!(reply.status, 200, "{user:?}: {:?}", reply.body);
        let deadline = Instant::now()
            + Duration::from_millis(B::WorkerStartup.span(Entries::ZERO).unwrap().millis());
        while seen.lock().unwrap().is_empty() {
            assert!(Instant::now() < deadline, "driver never saw {user:?}");
            std::thread::sleep(Duration::from_millis(
                B::RegistryPoll.span(Entries::ZERO).unwrap().millis(),
            ));
        }
        assert_eq!(
            seen.lock().unwrap()[0],
            case["templated"].as_str().unwrap(),
            "{user:?}"
        );
        host.begin_shutdown(
            Instant::now()
                + Duration::from_millis(B::StageCleanup.span(Entries::ZERO).unwrap().millis()),
        )
        .unwrap();
        // The capture driver returns without cleanup evidence ("a successful driver return is not
        // cleanup evidence", tests/service.rs): the host must NOT call that verified.
        let outcome = loop {
            match host.poll_shutdown() {
                Ok(Poll::Pending) => std::thread::yield_now(),
                other => break other,
            }
        };
        assert_eq!(outcome, Err(desktop::lifecycle::CleanupError::Poll));
    }
}
#[test]
fn the_probe_question_is_the_recorded_oracle_question() {
    let fixture: Value = serde_json::from_str(TEMPLATE_FIXTURE).unwrap();
    assert_eq!(
        node_service::chat_template::ORACLE_USER_PROMPT,
        fixture["cases"][0]["user"]
    );
}
/// The envelope text tokenises to the ids v0.1.4's codec produced (needs the pinned model's
/// tokenizer; explicit gate: `MYCELIUM_MODEL_SNAPSHOT=<snapshot dir> ... -- --ignored`).
#[test]
#[ignore = "needs the pinned Qwen2.5-0.5B-Instruct tokenizer.json (MYCELIUM_MODEL_SNAPSHOT)"]
fn the_chat_envelope_tokenises_to_the_v014_codec_ids() {
    let snapshot = std::path::PathBuf::from(std::env::var("MYCELIUM_MODEL_SNAPSHOT").unwrap());
    let codec = tokenizers::Tokenizer::from_file(snapshot.join("tokenizer.json")).unwrap();
    let fixture: Value = serde_json::from_str(TEMPLATE_FIXTURE).unwrap();
    for case in fixture["cases"].as_array().unwrap() {
        let user = case["user"].as_str().unwrap();
        let ids: Vec<u32> = serde_json::from_value(case["ids"].clone()).unwrap();
        let prompt = node_service::chat_template::qwen2_chat_prompt(user);
        assert_eq!(
            codec.encode(prompt, false).unwrap().get_ids(),
            ids,
            "{user:?}"
        );
    }
}

/// Round 10 (SE-SLICE1-L11-QUIT-UNPROVEN): the host the shell polls. `FX-SLICE1-quit-after-failure`
/// q2 (v0.1.4 executed): Quit after a FAILED run whose cleanup is unproven finishes with exit 0;
/// the shell maps `Poll::Verified` to 0 and any `Err` to 3 (`launcher_cleanup_failed`), so the
/// host must verify here, not fail.
struct DiesWithoutProof;
impl Driver for DiesWithoutProof {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        Err(PublicError::new(503, "worker_native_exited"))
    }
}
#[test]
fn quit_after_a_failed_run_without_a_cleanup_proof_is_verified_like_v014_exit_zero() {
    let fx: Value =
        serde_json::from_str(include_str!("fixtures/quit-after-failure-oracle.json")).unwrap();
    assert_eq!(
        fx["observations"]["q2_failed_run_cleanup_unproven"]["exit_code"],
        0
    );
    let host = DesktopServices::new(
        NodeService::new(
            Arc::new(DiesWithoutProof),
            "private-local-token".into(),
            node_service::native::development_qualification().unwrap(),
        ),
        NonZeroUsize::new(2).unwrap(),
        &private(),
    );
    let sent = call(
        &host,
        "/chat/api/send",
        json!({"prompt":"hello","max_new_tokens":1}),
    );
    assert_eq!(sent.status, 200);
    host.begin_shutdown(
        Instant::now()
            + Duration::from_millis(B::StageCleanup.span(Entries::ZERO).unwrap().millis()),
    )
    .unwrap();
    let deadline = Instant::now()
        + Duration::from_millis(B::StageCleanup.span(Entries::ZERO).unwrap().millis());
    let verdict = loop {
        match host.poll_shutdown() {
            Ok(Poll::Pending) if Instant::now() < deadline => std::thread::yield_now(),
            other => break other,
        }
    };
    assert_eq!(verdict, Ok(Poll::Verified));
}

// ---- REVIEW-laneR1-own F5: a send waiting for its start never blocks the other callbacks ----

/// The development record behind a probe the test can hold: `Serving::submit` reads it first, so
/// a held read keeps a send inside its admission (as a slow worker start does, up to
/// `WorkerStartup`).
#[derive(Clone)]
struct HeldRecord {
    record: Arc<node_service::router_port::DevelopmentRecord>,
    probe: Arc<(std::sync::Mutex<(bool, bool)>, std::sync::Condvar)>,
}
impl HeldRecord {
    fn hold(&self) {
        self.probe.0.lock().unwrap().0 = true;
    }
    fn wait_inside(&self) {
        let deadline = Instant::now() + millis(B::WorkerStartup);
        let mut probe = self.probe.0.lock().unwrap();
        while !probe.1 {
            let left = deadline.saturating_duration_since(Instant::now());
            assert!(!left.is_zero(), "the send never read its qualification");
            probe = self.probe.1.wait_timeout(probe, left).unwrap().0;
        }
    }
    fn release(&self) {
        self.probe.0.lock().unwrap().0 = false;
        self.probe.1.notify_all();
    }
}
impl api::gateway::QualificationSource for HeldRecord {
    fn current(
        &self,
        request_id: Option<&str>,
    ) -> Result<Option<api::gateway::Qualification>, api::gateway::Unavailable> {
        let mut probe = self.probe.0.lock().unwrap();
        if probe.0 {
            probe.1 = true;
            self.probe.1.notify_all();
            while probe.0 {
                probe = self.probe.1.wait(probe).unwrap();
            }
            probe.1 = false;
        }
        drop(probe);
        self.record.current(request_id)
    }
}
/// Releases the held record however the test ends, so no callback thread outlives it blocked.
struct Release(HeldRecord);
impl Drop for Release {
    fn drop(&mut self) {
        self.0.release();
    }
}
fn millis(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
/// `method path` submitted from its own thread (as the window's protocol thread submits): the
/// reply if one arrives within the model-selection budget, `None` if the submission or its
/// answer is still blocked then.
fn answered_within(
    host: &DesktopServices,
    method: &str,
    path: &str,
    body: Value,
) -> Option<desktop::core::Reply> {
    let (tx, rx) = std::sync::mpsc::channel();
    let (host, method, path) = (host.clone(), method.to_owned(), path.to_owned());
    let submitter = std::thread::spawn(move || {
        let reply = tx.clone();
        host.submit(
            ServiceRequest {
                method,
                target: path.clone(),
                headers: vec![
                    (
                        if path.starts_with("/chat/") {
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
            Box::new(move |answer| reply.send(Some(answer)).unwrap_or_default()),
        );
    });
    let reply = rx.recv_timeout(millis(B::ModelSelection)).ok().flatten();
    // Answered: the submission returned (a blocked one is left to the failing test's end).
    if reply.is_some() {
        submitter.join().unwrap();
    }
    reply
}
/// A run the node service can always settle: it fails at once (v0.1.4: a failed run is left
/// alone at Quit, `FX-SLICE1-quit-after-failure`).
struct FailsAtOnce;
impl Driver for FailsAtOnce {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        Err(PublicError::new(503, "worker_native_exited"))
    }
}
/// The page's status, chat state, a second send and Stop, and the window's Quit, while one send
/// waits for its start (`Serving::submit` -> `await_start`, up to `WorkerStartup` 30 s): every one
/// answers at once (`desktop::services`: callbacks never block, Stop/status always servable),
/// the second send is still refused `chat_busy` (one admitted send at a time), and Quit's begin
/// returns at once and verifies once the send ended.
#[test]
fn stop_and_status_answer_while_a_send_waits_for_its_start() {
    let node = NodeService::new(
        Arc::new(FailsAtOnce),
        "private-local-token".into(),
        node_service::native::development_qualification().unwrap(),
    );
    let held = HeldRecord {
        record: Arc::new(node_service::router_port::DevelopmentRecord::of(&node).unwrap()),
        probe: Arc::default(),
    };
    let host = DesktopServices::with_qualification(
        node,
        NonZeroUsize::new(4).unwrap(),
        &private(),
        Arc::new(held.clone()),
    );
    let release = Release(held.clone());
    // What the page's status answers with nothing in flight (here: nothing published yet).
    let idle = answered_within(&host, "GET", "/api/status", json!({})).expect("idle status");
    held.hold();
    let (sent_tx, sent) = std::sync::mpsc::channel();
    host.submit(
        ServiceRequest {
            method: "POST".into(),
            target: "/chat/api/send".into(),
            headers: vec![
                ("x-mycelium-chat".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: serde_json::to_vec(&json!({"prompt":"hello","max_new_tokens":1})).unwrap(),
        },
        Box::new(move |reply| sent_tx.send(reply).unwrap_or_default()),
    );
    held.wait_inside();
    let status = answered_within(&host, "GET", "/api/status", json!({}))
        .expect("/api/status blocked behind a send waiting for its start");
    assert_eq!((status.status, status.body), (idle.status, idle.body));
    let state = answered_within(&host, "POST", "/chat/api/state", json!({}))
        .expect("/chat/api/state blocked behind a send waiting for its start");
    assert_eq!(state.status, 200);
    assert_eq!(
        serde_json::from_slice::<Value>(&state.body).unwrap()["busy"],
        true
    );
    let second = answered_within(
        &host,
        "POST",
        "/chat/api/send",
        json!({"prompt":"again","max_new_tokens":1}),
    )
    .expect("a second send blocked behind the first");
    assert_eq!(
        (
            second.status,
            serde_json::from_slice::<Value>(&second.body).unwrap()["error"].clone()
        ),
        (429, json!("chat_busy"))
    );
    let stop = answered_within(&host, "POST", "/api/serving/stop", json!({}))
        .expect("Stop blocked behind a send waiting for its start");
    assert_ne!(
        stop.status,
        503,
        "{:?}",
        String::from_utf8_lossy(&stop.body)
    );
    // Quit's begin is the window's own call: it must return at once too.
    let (began_tx, began) = std::sync::mpsc::channel();
    let quitting = host.clone();
    let quit = std::thread::spawn(move || {
        began_tx
            .send(quitting.begin_shutdown(Instant::now() + millis(B::StageCleanup)))
            .unwrap_or_default();
    });
    assert_eq!(
        began
            .recv_timeout(millis(B::ModelSelection))
            .expect("Quit's begin blocked behind a send waiting for its start"),
        Ok(())
    );
    quit.join().unwrap();
    drop(release);
    sent.recv_timeout(millis(B::WorkerStartup))
        .expect("the held send never answered");
    let deadline = Instant::now() + millis(B::StageCleanup);
    let verdict = loop {
        match host.poll_shutdown() {
            Ok(Poll::Pending) if Instant::now() < deadline => std::thread::yield_now(),
            other => break other,
        }
    };
    assert_eq!(verdict, Ok(Poll::Verified));
}

// ---- REVIEW-lanePH1 finding 3: a Stop answered while a send is still being admitted ----

/// A run that ends only when it is cancelled (`request_cancelled`, as the native driver's token
/// and receipt steps answer), when the test releases it (`driver_released`), or after the
/// stage-cleanup budget (`driver_never_cancelled`). It counts its runs and whether it was cancelled.
#[derive(Clone, Default)]
struct UntilCancelled {
    runs: Arc<std::sync::atomic::AtomicUsize>,
    cancelled: Arc<std::sync::atomic::AtomicBool>,
    released: Arc<std::sync::atomic::AtomicBool>,
}
impl Driver for UntilCancelled {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        use std::sync::atomic::Ordering::SeqCst;
        self.runs.fetch_add(1, SeqCst);
        let deadline = Instant::now() + millis(B::StageCleanup);
        while !job.cancelled() && !self.released.load(SeqCst) && Instant::now() < deadline {
            std::thread::yield_now();
        }
        if job.cancelled() {
            self.cancelled.store(true, SeqCst);
            return Err(PublicError::new(503, "request_cancelled"));
        }
        Err(PublicError::new(
            503,
            if self.released.load(SeqCst) {
                "driver_released"
            } else {
                "driver_never_cancelled"
            },
        ))
    }
}
/// When the page sends Stop relative to its chat send.
#[derive(Clone, Copy, Debug, PartialEq)]
enum StopAt {
    /// While the send waits inside its admission (its qualification read held).
    Admission,
    /// Once the send answered with its request id (the request is registered).
    Registered,
    /// Before the send, with nothing in flight.
    Idle,
    /// Never.
    Never,
}
/// What the page saw of one chat send with `route` (a Stop) sent at `at`: the Stop's answer, the
/// request's `(terminal, error)` from `/chat/api/events`, how many runs the driver started and
/// whether it was cancelled. A run that is not cancelled is released once the send answered.
fn chat_with_stop(route: &str, at: StopAt) -> (Option<(u16, Value)>, (Value, Value), usize, bool) {
    use std::sync::atomic::Ordering::SeqCst;
    let driver = UntilCancelled::default();
    let node = NodeService::new(
        Arc::new(driver.clone()),
        "private-local-token".into(),
        node_service::native::development_qualification().unwrap(),
    );
    let held = HeldRecord {
        record: Arc::new(node_service::router_port::DevelopmentRecord::of(&node).unwrap()),
        probe: Arc::default(),
    };
    let private = private();
    let host = DesktopServices::with_qualification(
        node,
        NonZeroUsize::new(4).unwrap(),
        &private,
        Arc::new(held.clone()),
    );
    let release = Release(held.clone());
    let stop = || {
        let reply = answered_within(&host, "POST", route, json!({}))
            .unwrap_or_else(|| panic!("{route} got no answer"));
        (
            reply.status,
            serde_json::from_slice::<Value>(&reply.body).unwrap_or(Value::Null),
        )
    };
    let mut stopped = None;
    if at == StopAt::Idle {
        stopped = Some(stop());
    }
    if at == StopAt::Admission {
        held.hold();
    }
    let (sent_tx, sent) = std::sync::mpsc::channel();
    host.submit(
        ServiceRequest {
            method: "POST".into(),
            target: "/chat/api/send".into(),
            headers: vec![
                ("x-mycelium-chat".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: serde_json::to_vec(&json!({"prompt":"hello","max_new_tokens":1})).unwrap(),
        },
        Box::new(move |reply| sent_tx.send(reply).unwrap_or_default()),
    );
    if at == StopAt::Admission {
        held.wait_inside();
        stopped = Some(stop());
        drop(release);
    }
    let sent = sent
        .recv_timeout(millis(B::WorkerStartup))
        .expect("the send never answered");
    assert_eq!(sent.status, 200, "{}", String::from_utf8_lossy(&sent.body));
    let id = serde_json::from_slice::<Value>(&sent.body).unwrap()["request_id"].clone();
    match at {
        StopAt::Registered => stopped = Some(stop()),
        StopAt::Idle | StopAt::Never => driver.released.store(true, SeqCst),
        StopAt::Admission => {}
    }
    let deadline = Instant::now() + millis(B::StageCleanup) + millis(B::WorkerStartup);
    let mut after = 0;
    let outcome = loop {
        let events = call(
            &host,
            "/chat/api/events",
            json!({"request_id": id, "after": after}),
        );
        assert_eq!(
            events.status,
            200,
            "{}",
            String::from_utf8_lossy(&events.body)
        );
        let events: Value = serde_json::from_slice(&events.body).unwrap();
        if events["done"] == true {
            break (events["terminal"].clone(), events["error"].clone());
        }
        after = events["next"].as_u64().unwrap();
        assert!(Instant::now() < deadline, "the request never ended");
    };
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
    (
        stopped,
        outcome,
        driver.runs.load(SeqCst),
        driver.cancelled.load(SeqCst),
    )
}
/// v0.1.4 registers the chat record before `send` returns and its Stop's `cancel_all` cancels
/// every record not done, stickily: a record whose backend client does not exist yet is
/// cancelled the moment it does (chat.py:319-333, 349-354, 418-452; demo_chain.py:1324-1336 @
/// f4e366e9). So a Stop the page sends while its send is still being admitted
/// (`Serving::submit`, before the request is live) cancels that request, exactly as the same
/// Stop does once the send answered, for `/api/serving/stop` and `/api/demo/stop` alike.
fn a_stop_during_the_send_admission_cancels_that_request(route: &str) {
    let (stop, registered, runs, cancelled) = chat_with_stop(route, StopAt::Registered);
    assert_eq!(stop, Some((200, json!({"stopped": true}))), "{route}");
    assert_eq!(
        (runs, cancelled),
        (1, true),
        "{route}: Stop after registration"
    );
    let (stop, admission, runs, cancelled) = chat_with_stop(route, StopAt::Admission);
    assert_eq!(stop, Some((200, json!({"stopped": true}))), "{route}");
    assert!(
        runs == 0 || cancelled,
        "{route}: a Stop answered while the send was admitted was lost: its run went on \
         uncancelled ({admission:?})"
    );
    assert_eq!(
        admission, registered,
        "{route}: the request did not end as a Stop after registration ends it"
    );
}
#[test]
fn a_serving_stop_during_the_send_admission_cancels_that_request() {
    a_stop_during_the_send_admission_cancels_that_request("/api/serving/stop");
}
#[test]
fn a_demo_stop_during_the_send_admission_cancels_that_request() {
    a_stop_during_the_send_admission_cancels_that_request("/api/demo/stop");
}
/// The admission's Stop check never reaches a later request: a Stop with nothing in flight
/// leaves the next send to run, as v0.1.4's `cancel_all` cancels only the records it finds.
#[test]
fn a_stop_with_no_send_in_flight_does_not_cancel_the_next_send() {
    for route in ["/api/serving/stop", "/api/demo/stop"] {
        let (_, unstopped, runs, cancelled) = chat_with_stop(route, StopAt::Never);
        assert_eq!((runs, cancelled), (1, false), "{route}");
        let (stop, after_stop, runs, cancelled) = chat_with_stop(route, StopAt::Idle);
        assert_eq!(stop, Some((200, json!({"stopped": true}))), "{route}");
        assert_eq!(
            (runs, cancelled),
            (1, false),
            "{route}: an earlier Stop cancelled a later send"
        );
        assert_eq!(after_stop, unstopped, "{route}");
    }
}

// ---- laneB16f §5 / laneB18 FU1: admitted requests are capped, Stop and status stay servable ----

/// The packaged window's callbacks (`packaged::assemble`: the `desktop::services` core over the
/// packaged services) with every admitted slot held: one send waiting for its start and three
/// chat event polls waiting behind it. v0.1.4 counts only admitted requests against its cap and
/// always serves Stop and status (laneB18 call for any packaged loopback gateway); the packaged
/// window has no socket listener (its requests arrive through the `mycelium://` scheme handler),
/// so this is the whole of that rule here: Stop, status and the chat state answer, and the next
/// admitted request is refused with the existing capacity code.
#[test]
fn stop_and_status_answer_while_every_admitted_slot_is_held() {
    use desktop::{channel::Request, lifecycle::Cleanup};
    let node = NodeService::new(
        Arc::new(FailsAtOnce),
        "private-local-token".into(),
        node_service::native::development_qualification().unwrap(),
    );
    let held = HeldRecord {
        record: Arc::new(node_service::router_port::DevelopmentRecord::of(&node).unwrap()),
        probe: Arc::default(),
    };
    let private = private();
    let services = DesktopServices::with_qualification(
        node,
        node_service::packaged::CALLBACK_CAPACITY,
        &private,
        Arc::new(held.clone()),
    );
    let source = node_service::app_host::ModelSource::new(
        private.clone(),
        Arc::new(api::acquisition::HttpTransport::hugging_face(true)),
    );
    let (core, mut cleanup) = node_service::packaged::assemble(services, source);
    cleanup.bind_clock(Instant::now()).unwrap();
    let submit = |method: &str, path: &str, body: Value| {
        let (tx, rx) = std::sync::mpsc::channel();
        core.submit(
            Request {
                window: "main".into(),
                method: method.into(),
                uri: format!("{}{path}", desktop::channel::ORIGIN),
                headers: vec![
                    (
                        if path.starts_with("/chat/") {
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
        rx
    };
    let answer = |rx: std::sync::mpsc::Receiver<desktop::core::Reply>, what: &str| {
        let reply = rx
            .recv_timeout(millis(B::ModelSelection))
            .unwrap_or_else(|_| panic!("{what} got no answer"));
        let body: Value = serde_json::from_slice(&reply.body).unwrap_or(Value::Null);
        (reply.status, body)
    };
    let idle = answer(submit("GET", "/api/status", json!({})), "idle status");
    let release = Release(held.clone());
    held.hold();
    let sent = submit(
        "POST",
        "/chat/api/send",
        json!({"prompt":"hello","max_new_tokens":1}),
    );
    held.wait_inside();
    let polls: Vec<_> = (0..3)
        .map(|_| {
            submit(
                "POST",
                "/chat/api/events",
                json!({"request_id":"pending","after":0}),
            )
        })
        .collect();
    let stop = answer(submit("POST", "/api/serving/stop", json!({})), "Stop");
    assert!(
        !(stop.0 == 503
            && matches!(
                stop.1["error"].as_str(),
                Some("desktop_callback_capacity" | "native_service_capacity")
            )),
        "Stop refused for capacity: {stop:?}"
    );
    // Stop published the node service's status, so the page's status now answers it.
    let status = answer(submit("GET", "/api/status", json!({})), "status");
    assert_eq!((idle.0, status.0), (503, 200), "{status:?}");
    assert_eq!(status.1["owner_run"]["publisher_generation"], 1);
    let state = answer(submit("POST", "/chat/api/state", json!({})), "chat state");
    assert_eq!((state.0, &state.1["busy"]), (200, &json!(true)));
    // Past the admitted slots: refused at once with the existing capacity code.
    let fifth = answer(
        submit(
            "POST",
            "/chat/api/events",
            json!({"request_id":"pending","after":0}),
        ),
        "the fifth admitted request",
    );
    assert_eq!(
        (fifth.0, fifth.1["error"].clone()),
        (503, json!("desktop_callback_capacity"))
    );
    drop(release);
    answer(sent, "the held send");
    for poll in polls {
        answer(poll, "a chat events poll");
    }
    cleanup
        .begin(desktop::lifecycle::quit_allowance().unwrap())
        .unwrap();
    let deadline = Instant::now() + millis(B::StageCleanup);
    let verdict = loop {
        match cleanup.poll() {
            Ok(Poll::Pending) if Instant::now() < deadline => std::thread::yield_now(),
            other => break other,
        }
    };
    assert_eq!(verdict, Ok(Poll::Verified));
}

// ---- REVIEW-lanePH1 finding 4: the chat state's model gate never starves status and Stop ----

/// The packaged model gate held by the test: every `/chat/*` request waits inside it, as one
/// waits for a native compose under the gate's lock, until released.
#[derive(Clone, Default)]
struct HeldGate(Arc<(std::sync::Mutex<(bool, usize)>, std::sync::Condvar)>);
impl HeldGate {
    fn wait_inside(&self, count: usize) {
        let deadline = Instant::now() + millis(B::WorkerStartup);
        let mut gate = self.0.0.lock().unwrap();
        while gate.1 < count {
            let left = deadline.saturating_duration_since(Instant::now());
            assert!(
                !left.is_zero(),
                "only {} requests reached the model gate",
                gate.1
            );
            gate = self.0.1.wait_timeout(gate, left).unwrap().0;
        }
    }
    fn release(&self) {
        self.0.0.lock().unwrap().0 = true;
        self.0.1.notify_all();
    }
}
impl node_service::desktop_host::ModelGate for HeldGate {
    fn ready(&self) -> Result<(), PublicError> {
        let mut gate = self.0.0.lock().unwrap();
        gate.1 += 1;
        self.0.1.notify_all();
        while !gate.0 {
            gate = self.0.1.wait(gate).unwrap();
        }
        gate.1 -= 1;
        Ok(())
    }
}
/// Releases the held gate however the test ends.
struct OpenGate(HeldGate);
impl Drop for OpenGate {
    fn drop(&mut self) {
        self.0.release();
    }
}
/// v0.1.4's page server (a `ThreadingHTTPServer`, app.py:1940-2025 @ f4e366e9) has no admitted cap:
/// the page's status and its Stop answer whatever the chat state's handlers are doing. On the
/// packaged window the chat state first waits for the model gate (a native compose under the
/// gate's lock); with the page's four callbacks waiting there, the status and Stop still answer,
/// neither with a capacity refusal.
#[test]
fn status_and_stop_answer_while_the_chat_state_waits_for_the_model() {
    use desktop::{channel::Request, lifecycle::Cleanup};
    let node = NodeService::new(
        Arc::new(FailsAtOnce),
        "private-local-token".into(),
        node_service::native::development_qualification().unwrap(),
    );
    let private = private();
    let gate = HeldGate::default();
    let open = OpenGate(gate.clone());
    let services = DesktopServices::new(node, node_service::packaged::CALLBACK_CAPACITY, &private)
        .gated(Arc::new(gate.clone()));
    let source = node_service::app_host::ModelSource::new(
        private.clone(),
        Arc::new(api::acquisition::HttpTransport::hugging_face(true)),
    );
    let (core, mut cleanup) = node_service::packaged::assemble(services, source);
    cleanup.bind_clock(Instant::now()).unwrap();
    let submit = |method: &str, path: &str| {
        let (tx, rx) = std::sync::mpsc::channel();
        core.submit(
            Request {
                window: "main".into(),
                method: method.into(),
                uri: format!("{}{path}", desktop::channel::ORIGIN),
                headers: vec![
                    (
                        if path.starts_with("/chat/") {
                            "x-mycelium-chat"
                        } else {
                            "x-mycelium-action"
                        }
                        .into(),
                        "1".into(),
                    ),
                    ("content-type".into(), "application/json".into()),
                ],
                body: b"{}".to_vec(),
            },
            Box::new(move |reply| tx.send(reply).unwrap_or_default()),
        );
        rx
    };
    let answer = |rx: std::sync::mpsc::Receiver<desktop::core::Reply>, what: &str| {
        let reply = rx
            .recv_timeout(millis(B::ModelSelection))
            .unwrap_or_else(|_| panic!("{what} got no answer"));
        let body: Value = serde_json::from_slice(&reply.body).unwrap_or(Value::Null);
        (reply.status, body)
    };
    let states: Vec<_> = (0..node_service::packaged::CALLBACK_CAPACITY.get())
        .map(|_| submit("POST", "/chat/api/state"))
        .collect();
    gate.wait_inside(states.len());
    for (method, path) in [("GET", "/api/status"), ("POST", "/api/serving/stop")] {
        let reply = answer(submit(method, path), path);
        assert!(
            !(reply.0 == 503
                && matches!(
                    reply.1["error"].as_str(),
                    Some("desktop_callback_capacity" | "native_service_capacity")
                )),
            "{path} refused for capacity while the chat state waits for the model: {reply:?}"
        );
    }
    drop(open);
    for state in states {
        let state = answer(state, "a chat state");
        assert_eq!(state.0, 200, "{state:?}");
    }
    cleanup
        .begin(desktop::lifecycle::quit_allowance().unwrap())
        .unwrap();
    let deadline = Instant::now() + millis(B::StageCleanup);
    let verdict = loop {
        match cleanup.poll() {
            Ok(Poll::Pending) if Instant::now() < deadline => std::thread::yield_now(),
            other => break other,
        }
    };
    assert_eq!(verdict, Ok(Poll::Verified));
}
