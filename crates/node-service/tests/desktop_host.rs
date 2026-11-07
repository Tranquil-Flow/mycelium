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
