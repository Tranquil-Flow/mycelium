// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged owner opens its network window as v0.1.4's `_start_public_relay` does
//! (app.py:676-740 @ f4e366e9) and as the headless launcher composes it (`api::relay::OwnerRelay`
//! over the host's authority, headless.rs `start_relay`): `POST /api/network/start` starts the
//! temporary relay, the status shows its `network` block, `/api/create` binds the owner's seed
//! behind it (`proxy.bind_seed`, app.py:1612-1613), a join code opened with the relay running is a
//! real window with its link (not a pending one), a second Start while it runs is refused
//! `network_requires_fresh_owner`, and Quit closes the relay with the rest of the app
//! (`DemoApp.close`, app.py:1848-1850).
//!
//! The relay is always the external provider on a loopback proxy port the test picks: no
//! `cloudflared`, no tunnel, no internet.
#![cfg(feature = "desktop-shell")]
use api::resources::{Observation, Observer};
use budgets::{BudgetId as B, Entries};
use desktop::{
    channel::{ORIGIN, Request},
    core::Core,
    lifecycle::{Cleanup, CleanupError, Poll},
};
use node_service::{
    packaged::{self, AppCleanup},
    packaged_network::NetworkConfig,
};
use serde_json::{Value, json};
use std::{
    net::{TcpListener, TcpStream},
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};

/// The owner-chosen public address of the external relay (never resolved: nothing dials it).
const PUBLIC: &str = "https://v02-packaged-owner.example.org";

fn millis(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn state_root(tag: &str) -> PathBuf {
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "packaged-network-{tag}-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    root
}
fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap()
}
struct Permissive;
impl Observer for Permissive {
    fn observe(&self) -> Observation {
        Observation {
            power: "external".into(),
            thermal: "nominal".into(),
            network: "unmetered".into(),
        }
    }
}
/// The app's network, but its relay client lookup finds nothing: a Start here that forgot
/// `provider: external` fails closed instead of running a `cloudflared` on this Mac's `PATH`.
fn test_network() -> NetworkConfig {
    let production = NetworkConfig::default().hooks;
    NetworkConfig {
        hooks: Arc::new(move || {
            let mut hooks = production();
            hooks.binary = Arc::new(|| None);
            hooks
        }),
        ..NetworkConfig::default()
    }
}
fn packaged_core(state: &Path) -> (Arc<dyn Core>, AppCleanup) {
    let (core, mut cleanup) = packaged::core_with_network(
        state,
        Arc::new(api::acquisition::HttpTransport::hugging_face(true)),
        Arc::new(Permissive),
        test_network(),
    )
    .unwrap();
    cleanup.bind_clock(Instant::now()).unwrap();
    (core, cleanup)
}
fn request(core: &Arc<dyn Core>, method: &str, path: &str, body: &Value) -> (u16, Value) {
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    core.submit(
        Request {
            window: "main".into(),
            method: method.into(),
            uri: format!("{ORIGIN}{path}"),
            headers: vec![
                ("x-mycelium-action".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: if method == "GET" {
                Vec::new()
            } else {
                serde_json::to_vec(body).unwrap()
            },
        },
        Box::new(move |reply| tx.send(reply).unwrap()),
    );
    let reply = rx.recv_timeout(millis(B::WorkerStartup)).unwrap();
    (reply.status, serde_json::from_slice(&reply.body).unwrap())
}
fn post(core: &Arc<dyn Core>, path: &str, body: Value) -> (u16, Value) {
    request(core, "POST", path, &body)
}
fn status(core: &Arc<dyn Core>) -> Value {
    let (code, value) = request(core, "GET", "/api/status", &json!({}));
    assert_eq!(code, 200, "{value}");
    value
}
fn consent() -> Value {
    json!({
        "peer_visibility": true, "power": "ac_only",
        "storage_bytes": api::model_catalog::storage_consent_ceiling(),
        "network": "unmetered_only", "background": false, "diagnostics": false,
        "thermal": "stop_on_serious",
    })
}
fn network_body(proxy: u16) -> Value {
    json!({"relay_acknowledged": true, "max_seconds": 1800, "provider": "external",
           "public_origin": PUBLIC, "proxy_port": proxy})
}
fn quit(cleanup: &mut AppCleanup) -> Result<Poll, CleanupError> {
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    let began = Instant::now();
    cleanup.begin(allowance).unwrap();
    let deadline = began + Duration::from_millis(allowance.millis());
    loop {
        match cleanup.poll() {
            Ok(Poll::Pending) => {
                assert!(Instant::now() < deadline, "the packaged core did not quit");
                std::thread::sleep(millis(B::OwnerPoll) / 5);
            }
            other => return other,
        }
    }
}
fn listening(port: u16) -> bool {
    TcpStream::connect(("127.0.0.1", port)).is_ok()
}

/// `POST /api/network/start` (external provider) before the swarm exists, as the headless pair
/// journeys do, then `/api/create`: the relay runs on the owner's chosen origin, the status shows
/// it with the seed bound once the swarm exists, a code asked for is a live window whose link
/// names that origin, a second Start while it runs is refused, and Quit closes the relay.
#[test]
fn packaged_owner_opens_the_network_window_and_quit_closes_it() {
    let state = state_root("open");
    let (core, mut cleanup) = packaged_core(&state);
    let proxy = free_port();
    let started = post(&core, "/api/network/start", network_body(proxy));
    assert_eq!(started.0, 200, "network start: {}", started.1);
    assert_eq!(started.1["state"], "relay_process_running", "{}", started.1);
    assert_eq!(started.1["public_origin"], PUBLIC, "{}", started.1);
    assert!(listening(proxy), "the relay proxy is not listening");
    let network = &status(&core)["network"];
    assert_eq!(network["state"], "relay_process_running", "{network}");
    assert_eq!(network["seed_bound"], false, "{network}");
    let created = post(&core, "/api/create", json!({"consent": consent()}));
    assert_eq!(created.0, 200, "create: {}", created.1);
    assert_eq!(created.1["membership_state"], "current", "{}", created.1);
    let network = &status(&core)["network"];
    assert_eq!(network["seed_bound"], true, "{network}");
    // The code is redeemable at once: a window on the relay origin, never `waiting_for_relay`.
    let opened = post(&core, "/api/pair/window/open", json!({"ttl_seconds": 600}));
    assert_eq!(opened.0, 200, "{}", opened.1);
    assert_eq!(opened.1["state"], "open", "{}", opened.1);
    let link = opened.1["join_link"].as_str().unwrap_or_default();
    assert!(link.starts_with("mycelium-join:"), "{}", opened.1);
    let listed = post(&core, "/api/pair/window/list", json!({}));
    assert_eq!(listed.1["windows"][0]["state"], "open", "{}", listed.1);
    // `_start_public_relay`: a relay that still runs is not started a second time.
    let again = post(&core, "/api/network/start", network_body(free_port()));
    assert_eq!(
        (again.0, again.1["error"].clone()),
        (400, json!("network_requires_fresh_owner"))
    );
    assert_eq!(quit(&mut cleanup), Ok(Poll::Verified));
    assert!(!listening(proxy), "Quit left the relay proxy listening");
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// A code asked for before the relay exists waits for it (`_open_pending_window`); the Start that
/// brings the relay up turns it into a live window (`_materialize_pending_windows`, app.py:823-834).
#[test]
fn packaged_pending_code_becomes_a_window_when_the_relay_starts() {
    let state = state_root("pending");
    let (core, mut cleanup) = packaged_core(&state);
    let created = post(&core, "/api/create", json!({"consent": consent()}));
    assert_eq!(created.0, 200, "create: {}", created.1);
    let pending = post(&core, "/api/pair/window/open", json!({"ttl_seconds": 600}));
    assert_eq!(pending.1["state"], "waiting_for_relay", "{}", pending.1);
    let started = post(&core, "/api/network/start", network_body(free_port()));
    assert_eq!(started.0, 200, "network start: {}", started.1);
    // The swarm created without a relay answers on the relay origin now (attach_public_endpoint).
    assert_eq!(started.1["seed_bound"], true, "{}", started.1);
    let listed = post(&core, "/api/pair/window/list", json!({}));
    assert_eq!(listed.0, 200, "{}", listed.1);
    assert_eq!(listed.1["windows"][0]["state"], "open", "{}", listed.1);
    let flow = &status(&core)["join_flow"];
    assert_eq!(flow["pending_windows"], json!([]), "{flow}");
    assert_eq!(quit(&mut cleanup), Ok(Poll::Verified));
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// Lane CHATS item 1 (ledger row 106 a): v0.1.4's `chat` status block (`chat.chat_status`,
/// chat.py:521-529 @ f4e366e9, wired at app.py:1280 through `_demo_status(self,'chat')`): the
/// owner's `/api/status` carries `available` and `busy` from the chat service's own `state()`, the
/// page's `local_url` (the app's origin + `/chat`) and, once the relay runs on an https origin, the
/// room's `share_url` (that origin + `/chat`). run.js shows "Open the chat on this Mac" and "Share
/// with the room" only from this block; without it the page said "The chat opens when the route is
/// serving." for a whole serving run. No run here: the service says not available.
#[test]
fn the_owner_status_carries_the_chat_block_with_the_relay_share_url() {
    let state = state_root("chat-status");
    let (core, mut cleanup) = packaged_core(&state);
    let before = status(&core);
    assert_eq!(before["installation_role"], "owner", "{before}");
    assert_eq!(
        before["chat"],
        json!({"available": false, "busy": false,
               "local_url": format!("{ORIGIN}/chat"), "share_url": null}),
        "{before}"
    );
    let started = post(&core, "/api/network/start", network_body(free_port()));
    assert_eq!(started.0, 200, "network start: {}", started.1);
    let after = status(&core);
    assert_eq!(
        after["chat"],
        json!({"available": false, "busy": false,
               "local_url": format!("{ORIGIN}/chat"), "share_url": format!("{PUBLIC}/chat")}),
        "{after}"
    );
    assert_eq!(quit(&mut cleanup), Ok(Poll::Verified));
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}
