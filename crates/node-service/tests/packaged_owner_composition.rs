// SPDX-License-Identifier: AGPL-3.0-only
//! S4c-b1: owner composition, without models or application launches.
#![cfg(feature = "desktop-shell")]
use api::{
    chat_service::{RouteGateway, RunRoute},
    schema::PublicError,
};
use desktop::{install::Roots, lifecycle::Poll, services::Services};
use node_service::{
    desktop_host::DesktopServices,
    packaged,
    service::{Driver, Job, NodeService},
};
use serde_json::Value;
use std::{
    path::PathBuf,
    sync::Arc,
    time::{Duration, Instant},
};

struct NoNative;
impl Driver for NoNative {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        panic!("model-free owner composition must not execute native inference")
    }
}
/// Named constructor-only double: no serving run or gateway exists in this root-admission test.
struct IdleRunDouble;
impl RunRoute for IdleRunDouble {
    fn status(&self) -> Option<Value> {
        None
    }
    fn gateway(&self, _: &str) -> Option<Arc<dyn RouteGateway>> {
        None
    }
    fn model_id(&self) -> Option<String> {
        None
    }
}
struct Scratch(PathBuf);
impl Scratch {
    fn new() -> Self {
        use std::os::unix::fs::DirBuilderExt;
        // Deliberately retain macOS's /var spelling. Admission must canonicalize the ancestor,
        // not demand lexical equality with /private/var.
        let path = std::env::temp_dir().join(format!(
            "s4cb1-{}",
            node_service::service::random_id().unwrap()
        ));
        std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .unwrap();
        Self(path)
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        if let Err(error) = std::fs::remove_dir_all(&self.0) {
            eprintln!("s4cb1_scratch_cleanup_failed: {error}");
        }
    }
}
fn services(state: &std::path::Path) -> DesktopServices {
    let dirs = packaged::prepare_dirs(state).unwrap();
    DesktopServices::new(
        NodeService::new(
            Arc::new(NoNative),
            "model-free-owner".into(),
            node_service::native::development_qualification().unwrap(),
        ),
        packaged::CALLBACK_CAPACITY,
        &dirs.private,
    )
    .with_chat_run(
        Arc::new(IdleRunDouble),
        Some(state.join("v02-node/evidence/chat-timing.jsonl")),
    )
    .expect("W1: the packaged owner's timing file must be admitted under its own state root")
}
fn shutdown(host: &DesktopServices) {
    let allowance = budgets::BudgetId::StageCleanup
        .span(budgets::Entries::ZERO)
        .unwrap();
    let end = Instant::now() + Duration::from_millis(allowance.millis());
    host.begin_shutdown(end).unwrap();
    loop {
        match host.poll_shutdown().unwrap() {
            Poll::Verified => break,
            Poll::Pending => {
                assert!(Instant::now() < end, "model-free services did not close");
                std::thread::yield_now();
            }
        }
    }
}
/// One page request through the composed window core, as chat.js sends it (`X-Mycelium-Chat`).
fn chat(core: &Arc<dyn desktop::core::Core>, path: &str, body: Value) -> (u16, Value) {
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    core.submit(
        desktop::channel::Request {
            window: "main".into(),
            method: "POST".into(),
            uri: format!("{}{path}", desktop::channel::ORIGIN),
            headers: vec![
                ("x-mycelium-chat".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: serde_json::to_vec(&body).unwrap(),
        },
        Box::new(move |reply| tx.send(reply).unwrap_or_default()),
    );
    let patience = budgets::BudgetId::WorkerStartup
        .span(budgets::Entries::ZERO)
        .unwrap();
    let reply = rx
        .recv_timeout(Duration::from_millis(patience.millis()))
        .expect("the window answers");
    (
        reply.status,
        serde_json::from_slice(&reply.body).unwrap_or(Value::Null),
    )
}
/// The packaged executable's real composition over `state` (`packaged::core_with_network`, no
/// model, no relay client, offline hub).
fn packaged_core(
    state: &std::path::Path,
) -> Result<(Arc<dyn desktop::core::Core>, packaged::AppCleanup), String> {
    let production = node_service::packaged_network::NetworkConfig::default().hooks;
    packaged::core_with_network(
        state,
        Arc::new(api::acquisition::HttpTransport::hugging_face(true)),
        Arc::new(AlwaysPermitted),
        node_service::packaged_network::NetworkConfig {
            hooks: Arc::new(move || {
                let mut hooks = production();
                hooks.binary = Arc::new(|| None);
                hooks
            }),
            ..node_service::packaged_network::NetworkConfig::default()
        },
    )
}
struct AlwaysPermitted;
impl api::resources::Observer for AlwaysPermitted {
    fn observe(&self) -> api::resources::Observation {
        api::resources::Observation {
            power: "external".into(),
            thermal: "nominal".into(),
            network: "unmetered".into(),
        }
    }
}
fn quit(mut cleanup: packaged::AppCleanup) {
    use desktop::lifecycle::Cleanup;
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    cleanup.bind_clock(Instant::now()).unwrap();
    cleanup.begin(allowance).unwrap();
    let deadline = Instant::now() + Duration::from_millis(allowance.millis());
    loop {
        match cleanup.poll() {
            Ok(Poll::Pending) => {
                assert!(Instant::now() < deadline, "the packaged core did not quit");
                std::thread::yield_now();
            }
            other => {
                assert_eq!(other, Ok(Poll::Verified));
                return;
            }
        }
    }
}

/// Proves: ledger row 17 (owner ruling 14 Nov: one chat path, as v0.1.4). The packaged
/// executable's own composition (`packaged::compose`, what `bin/mycelium.rs` runs) always composes
/// the chat over the owner run's route: with no run started and no model on this Mac, every
/// `/chat/*` route is answered by the shipped `ChatService` (chat.js `call` -> `chat.handle`
/// chat.py:553-584 -> `ChatService.state` :476-504 / `send` :300-318 with no serving run ->
/// `chat_not_ready` :317-318 / `events` :460-468 -> `chat_request_unknown`), never by the one-Mac
/// native model (whose gate answered `model_not_installed` here before the wiring). The release
/// preflight registers this test as the compiled proof that the packaged chat has a run route.
#[test]
fn the_packaged_composition_always_has_a_chat_run() {
    let scratch = Scratch::new();
    let state = scratch.0.join("state");
    let (core, cleanup) = packaged_core(&state).expect("the packaged app composes");
    let state_reply = chat(&core, "/chat/api/state", serde_json::json!({}));
    let send = chat(
        &core,
        "/chat/api/send",
        serde_json::json!({"prompt":"hi","max_new_tokens":2}),
    );
    let events = chat(
        &core,
        "/chat/api/events",
        serde_json::json!({"request_id":"A".repeat(24),"after":0}),
    );
    quit(cleanup);
    assert_eq!(state_reply.0, 200, "{}", state_reply.1);
    let limits = &state_reply.1["limits"];
    assert_eq!(
        state_reply.1,
        serde_json::json!({"available":false,"busy":false,"switching_runs":false,
            "warming_up":false,"rejoining":false,"model_id":null,"served_by":[],"limits":limits}),
        "the chat state is the run route's, not the native model's"
    );
    assert!(limits["max_new_tokens"].is_u64(), "{limits}");
    assert_eq!(send, (503, serde_json::json!({"error":"chat_not_ready"})));
    assert_eq!(
        events,
        (404, serde_json::json!({"error":"chat_request_unknown"}))
    );
}

/// Proves: ledger row 17 (the timing file is admitted through `TimingFile::under` when the app is
/// composed): a state root inside v0.1.4's own folder (rulings 20/25: v0.2 never writes there)
/// fails the packaged composition with `chat_timing_path_refused` instead of composing a chat
/// whose timing file the service would then refuse on every completed chat.
#[test]
fn a_refused_timing_file_fails_the_packaged_composition() {
    let scratch = Scratch::new();
    let state = scratch.0.join("Mycelium A13 macOS Demo").join("state");
    let composed = packaged_core(&state);
    let refusal = match composed {
        Ok((_core, cleanup)) => {
            quit(cleanup);
            None
        }
        Err(error) => Some(error),
    };
    assert_eq!(
        refusal.as_deref(),
        Some("compose:chat_timing_path_refused"),
        "the packaged app composed a chat whose timing file lies in v0.1.4's folder"
    );
}

/// Proves: ledger row 17 (owner ruling 14 Nov: the packaged app no longer offers one-Mac native
/// chat; v0.1.4 offers no chat without a second Mac). A SLICE-1 model folder left on disk where
/// the retired one-Mac chat looked for it (`model-source-<rev12>/models--Qwen--Qwen2.5-0.5B-
/// Instruct/snapshots/<rev>/`) is not composed into a native driver at window open: the
/// composition succeeds, writes no native member / owner-leaf identity, and the chat is the
/// run route's.
#[test]
fn the_packaged_app_never_composes_the_one_mac_native_model() {
    let scratch = Scratch::new();
    let state = scratch.0.join("state");
    let revision = backend_mlx::admission::REVISION;
    let snapshot = state
        .join(format!("model-source-{}", &revision[..12]))
        .join("models--Qwen--Qwen2.5-0.5B-Instruct/snapshots")
        .join(revision);
    std::fs::create_dir_all(&snapshot).unwrap();
    for file in ["model.safetensors", "tokenizer.json"] {
        std::fs::write(snapshot.join(file), b"not a model").unwrap();
    }
    let composed = packaged_core(&state);
    let private = state.join("v02-node/private");
    let native_identities: Vec<_> = ["member", "owner-leaf"]
        .into_iter()
        .filter(|name| private.join(name).exists())
        .collect();
    let (core, cleanup) = composed.expect("the packaged app composes");
    let state_reply = chat(&core, "/chat/api/state", serde_json::json!({}));
    quit(cleanup);
    assert_eq!(
        native_identities,
        Vec::<&str>::new(),
        "the packaged app composed the one-Mac native driver"
    );
    assert_eq!(state_reply.0, 200, "{}", state_reply.1);
    assert_eq!(state_reply.1["available"], false, "{}", state_reply.1);
    assert_eq!(state_reply.1["model_id"], Value::Null, "{}", state_reply.1);
}

#[test]
fn timing_admission_accepts_the_real_default_folder_shape_and_macos_temp_alias() {
    let scratch = Scratch::new();
    let roots = Roots::new(&scratch.0, &scratch.0.join("Applications")).unwrap();
    for state in [roots.state(), scratch.0.join("temporary-owner")] {
        let host = services(&state);
        assert!(host.run_chats().is_some());
        shutdown(&host);
    }
}
