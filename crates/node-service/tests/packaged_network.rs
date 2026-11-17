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

/// One request to the relay's loopback proxy, as the public front forwards it: `(status, headers,
/// body)`. `Connection: close`, so the body ends with the stream.
fn relay_request(
    proxy: u16,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: &[u8],
) -> (u16, String, Vec<u8>) {
    use std::io::{Read, Write};
    let mut stream = TcpStream::connect(("127.0.0.1", proxy)).unwrap();
    stream
        .set_read_timeout(Some(millis(B::WorkerStartup)))
        .unwrap();
    let mut head = format!(
        "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Length: {}\r\n",
        body.len()
    );
    for (name, value) in headers {
        head.push_str(&format!("{name}: {value}\r\n"));
    }
    head.push_str("\r\n");
    stream.write_all(head.as_bytes()).unwrap();
    stream.write_all(body).unwrap();
    let mut raw = Vec::new();
    stream.read_to_end(&mut raw).unwrap();
    let split = raw.windows(4).position(|w| w == b"\r\n\r\n").unwrap();
    let head = String::from_utf8_lossy(&raw[..split]).into_owned();
    let status = head.split(' ').nth(1).unwrap().parse().unwrap();
    (status, head.to_ascii_lowercase(), raw[split + 4..].to_vec())
}
fn relay_chat(proxy: u16, path: &str, body: &Value) -> (u16, Value) {
    let (status, _, raw) = relay_request(
        proxy,
        "POST",
        path,
        &[
            ("Content-Type", "application/json"),
            ("X-Mycelium-Chat", "1"),
        ],
        &serde_json::to_vec(body).unwrap(),
    );
    (status, serde_json::from_slice(&raw).unwrap_or(Value::Null))
}

/// Lane CHATS item 2 (ledger row 106 a): v0.1.4 binds the owner's chat behind its relay
/// (`_bind_public_routes`, app.py:1070-1077 @ f4e366e9: `proxy.bind_chat(... chat.handle(self, ...,
/// via_relay=True))`), so the room's `share_url` reaches the run's chat service. On rv39 the relay
/// answered `/chat/api/state` with `relay_chat_not_bound` (503) while the run served. With the
/// relay up and no run, the relay's `/chat/*` answer from the run's chat service itself: its state
/// document (not available), its own `chat_not_ready` for a send, its header check, and the chat
/// page under the chat CSP.
#[test]
fn the_relay_answers_the_chat_from_the_runs_chat_service() {
    let state = state_root("relay-chat");
    let (core, mut cleanup) = packaged_core(&state);
    let proxy = free_port();
    let started = post(&core, "/api/network/start", network_body(proxy));
    assert_eq!(started.0, 200, "network start: {}", started.1);
    let (code, chat_state) = relay_chat(proxy, "/chat/api/state", &json!({}));
    assert_eq!(code, 200, "{chat_state}");
    assert_eq!(
        (&chat_state["available"], &chat_state["busy"]),
        (&json!(false), &json!(false)),
        "{chat_state}"
    );
    assert!(chat_state["limits"].is_object(), "{chat_state}");
    let sent = relay_chat(proxy, "/chat/api/send", &json!({"prompt": "hi"}));
    assert_eq!(sent, (503, json!({"error": "chat_not_ready"})));
    let (code, _, raw) = relay_request(
        proxy,
        "POST",
        "/chat/api/state",
        &[("Content-Type", "application/json")],
        b"{}",
    );
    assert_eq!(
        (code, serde_json::from_slice::<Value>(&raw).unwrap()),
        (403, json!({"error": "chat_header_required"}))
    );
    let (code, head, page) = relay_request(proxy, "GET", "/chat", &[], b"");
    assert_eq!(code, 200, "{head}");
    assert!(head.contains("content-type: text/html"), "{head}");
    assert!(head.contains("content-security-policy:"), "{head}");
    assert!(!page.is_empty());
    assert_eq!(quit(&mut cleanup), Ok(Poll::Verified));
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// Lane CHATPAGE item 1: the window's chat page offers the owner's Stop, but the relay's public
/// chat page must not, and whatever a page shows or a client crafts, an owner action never
/// arrives through the relay: the relay knows no `/api/*` route (`relay_route_unknown`, before
/// any handler), and the chat boundary behind `/chat` answers only its own three routes and its
/// three page assets (`not_found`, owner check first). The owner's status afterwards shows the
/// relay still running: none of them acted.
#[test]
fn the_relay_refuses_every_owner_action_and_serves_no_owner_control() {
    let state = state_root("relay-owner-actions");
    let (core, mut cleanup) = packaged_core(&state);
    let proxy = free_port();
    let started = post(&core, "/api/network/start", network_body(proxy));
    assert_eq!(started.0, 200, "network start: {}", started.1);
    let created = post(&core, "/api/create", json!({"consent": consent()}));
    assert_eq!(created.0, 200, "create: {}", created.1);
    let action = [
        ("Content-Type", "application/json"),
        ("X-Mycelium-Action", "1"),
        ("X-Mycelium-Chat", "1"),
        ("Origin", ORIGIN),
    ];
    for path in [
        "/api/demo/stop",
        "/api/demo/start",
        "/api/stop",
        "/api/serving/stop",
        "/api/quit",
        "/api/network/stop",
        "/api/status",
        "/chat/../api/demo/stop",
    ] {
        let (code, _, raw) = relay_request(proxy, "POST", path, &action, b"{}");
        assert_eq!(
            (code, serde_json::from_slice::<Value>(&raw).unwrap()),
            (404, json!({"error": {"code": "relay_route_unknown"}})),
            "{path} reached the owner through the relay"
        );
    }
    for path in ["/chat/api/stop", "/chat/api/demo/stop", "/chat/api/quit"] {
        let (code, _, raw) = relay_request(proxy, "POST", path, &action, b"{}");
        assert_eq!(
            (code, serde_json::from_slice::<Value>(&raw).unwrap()),
            (404, json!({"error": "not_found"})),
            "{path} reached an owner action through the relay chat"
        );
    }
    let (code, _, _) = relay_request(proxy, "GET", "/chat/owner.js", &[], b"");
    assert_eq!(code, 404, "the relay serves the window's owner script");
    let (code, _, page) = relay_request(proxy, "GET", "/chat", &[], b"");
    assert_eq!(code, 200);
    let (code, _, script) = relay_request(proxy, "GET", "/chat/chat.js", &[], b"");
    assert_eq!(code, 200);
    let page = String::from_utf8(page).unwrap();
    let script = String::from_utf8(script).unwrap();
    for owner in ["Back to Mycelium", "run-stop", "owner.js", "Stop the run"] {
        assert!(!page.contains(owner), "the relay's chat page shows {owner}");
    }
    for owner in ["/api/demo", "X-Mycelium-Action", "run-stop"] {
        assert!(
            !script.contains(owner),
            "the relay's chat script reaches {owner}"
        );
    }
    let network = &status(&core)["network"];
    assert_eq!(network["state"], "relay_process_running", "{network}");
    assert_eq!(quit(&mut cleanup), Ok(Poll::Verified));
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// Lane CHATPAGE item 2 (REVIEW-laneCHATS-astra CHATS-01): the relay's `/chat` served the api
/// crate's pinned v0.1.4 `chat.js`, which has no message for `chat_context_too_long`, so a room
/// member read the raw code where the window's page explains the context limit. One source of
/// truth: the relay serves the window's own chat script, byte for byte (`desktop::page::CHAT_JS`),
/// under the chat CSP, and the relay's page loads it from there.
#[test]
fn the_relay_chat_page_shows_the_windows_refusal_messages() {
    let state = state_root("relay-chat-script");
    let (core, mut cleanup) = packaged_core(&state);
    let proxy = free_port();
    let started = post(&core, "/api/network/start", network_body(proxy));
    assert_eq!(started.0, 200, "network start: {}", started.1);
    let (code, head, script) = relay_request(proxy, "GET", "/chat/chat.js", &[], b"");
    assert_eq!(code, 200, "{head}");
    assert!(head.contains("content-type: text/javascript"), "{head}");
    assert!(head.contains("content-security-policy:"), "{head}");
    assert!(
        String::from_utf8_lossy(&script).contains("chat_context_too_long:'"),
        "the relay's chat script has no message for chat_context_too_long: a room member reads the raw code"
    );
    assert!(
        script == desktop::page::CHAT_JS,
        "the relay's chat script is not the window's (one source of truth)"
    );
    let (code, _, page) = relay_request(proxy, "GET", "/chat", &[], b"");
    assert_eq!(code, 200);
    assert!(
        String::from_utf8_lossy(&page).contains(r#"src="/chat/chat.js""#),
        "the relay's chat page does not load the chat script from /chat/chat.js"
    );
    assert_eq!(quit(&mut cleanup), Ok(Poll::Verified));
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}
