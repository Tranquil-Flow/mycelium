// SPDX-License-Identifier: AGPL-3.0-only
//! A packaged Mac joins a packaged owner's swarm (lane PH3a): v0.1.4's join flow through the routes
//! and actions its page uses (`/api/join/window`, `/api/join/window/poll`, app.py:1742-1813 @
//! f4e366e9) with the join-consent document the page shows (`join_flow.standing_disclosure`,
//! app.py:1214-1237), the role the installation really has (`installation_role`, app.py:595-611,
//! 1106-1116), the joined Mac's own membership, its lease renewal and its leave.
//!
//! Two packaged hosts run in this one test process over two private state roots. The owner's relay
//! is the external provider on a loopback proxy port (no `cloudflared`, no internet); the member
//! reaches it through v0.1.4's own join seam (`_test_join_base_url`) and the owner's seed through
//! its pinned local CA, both given to the member's composition, never through the environment.
#![cfg(feature = "desktop-shell")]
use api::{
    resources::{Observation, Observer},
    seed::SeedOptions,
};
use budgets::{BudgetId as B, Entries};
use desktop::{
    channel::{ORIGIN, Request},
    core::Core,
    lifecycle::{Cleanup, CleanupError, Poll},
};
use node_service::{
    packaged::{self, AppCleanup},
    packaged_network::{JoinSeams, NetworkConfig},
};
use serde_json::{Value, json};
use std::{
    net::TcpListener,
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};

const PUBLIC: &str = "https://v02-packaged-pair.example.org";
const STORAGE: u64 = 2_000_000_000;

fn millis(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn state_root(tag: &str) -> PathBuf {
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "packaged-join-{tag}-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    root
}
fn private(state: &Path) -> PathBuf {
    state.join("v02-node/private")
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
/// One packaged Mac: its window core and its Quit.
struct Mac {
    core: Arc<dyn Core>,
    cleanup: AppCleanup,
    state: PathBuf,
}
impl Mac {
    fn launch(state: &Path, config: NetworkConfig) -> Self {
        let (core, mut cleanup) = packaged::core_with_network(
            state,
            Arc::new(api::acquisition::HttpTransport::hugging_face(true)),
            Arc::new(Permissive),
            config,
        )
        .unwrap();
        cleanup.bind_clock(Instant::now()).unwrap();
        Self {
            core,
            cleanup,
            state: state.to_owned(),
        }
    }
    fn request(&self, method: &str, path: &str, body: &Value) -> (u16, Value) {
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        self.core.submit(
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
        let reply = rx.recv_timeout(millis(B::WorkerStartup) * 2).unwrap();
        (reply.status, serde_json::from_slice(&reply.body).unwrap())
    }
    fn post(&self, path: &str, body: Value) -> (u16, Value) {
        self.request("POST", path, &body)
    }
    fn status(&self) -> Value {
        let (code, value) = self.request("GET", "/api/status", &json!({}));
        assert_eq!(code, 200, "{value}");
        value
    }
    /// Ask again (one owner poll apart) until `done` holds, within the join patience.
    fn until(
        &self,
        what: &str,
        ask: impl Fn(&Self) -> Value,
        done: impl Fn(&Value) -> bool,
    ) -> Value {
        let deadline = Instant::now() + millis(B::WorkerStartup) * 4;
        loop {
            let value = ask(self);
            if done(&value) {
                return value;
            }
            assert!(Instant::now() < deadline, "{what}: {value}");
            std::thread::sleep(millis(B::OwnerPoll) / 5);
        }
    }
    fn quit(mut self) -> Result<Poll, CleanupError> {
        let allowance = desktop::lifecycle::quit_allowance().unwrap();
        let began = Instant::now();
        self.cleanup.begin(allowance).unwrap();
        let deadline = began + Duration::from_millis(allowance.millis());
        loop {
            match self.cleanup.poll() {
                Ok(Poll::Pending) => {
                    assert!(Instant::now() < deadline, "the packaged core did not quit");
                    std::thread::sleep(millis(B::OwnerPoll) / 5);
                }
                other => return other,
            }
        }
    }
}
fn consent(storage: u64) -> Value {
    json!({
        "peer_visibility": true, "power": "ac_only", "storage_bytes": storage,
        "network": "unmetered_only", "background": false, "diagnostics": false,
        "thermal": "stop_on_serious",
    })
}
/// The TCP ports this test process listens on (`lsof`), for the owner seed's own port.
fn listening() -> Vec<u16> {
    let out = std::process::Command::new("/usr/sbin/lsof")
        .args(["-nP", "-a", "-iTCP", "-sTCP:LISTEN", "-p"])
        .arg(std::process::id().to_string())
        .output()
        .unwrap();
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter_map(|line| line.rsplit(':').next()?.split(' ').next()?.parse().ok())
        .collect()
}
/// A packaged owner with its relay on a loopback proxy, its swarm and one open code; the member's
/// seams that reach it (its relay proxy for the window, its seed directly over the pinned CA).
struct Owner {
    mac: Mac,
    proxy: u16,
    link: String,
    window_id: Value,
    seams: JoinSeams,
}
fn owner(base: &Path) -> Owner {
    let state = base.join("owner");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&state)
        .unwrap();
    let before = listening();
    let mac = Mac::launch(&state, NetworkConfig::default());
    let proxy = free_port();
    let started = mac.post(
        "/api/network/start",
        json!({"relay_acknowledged": true, "max_seconds": 1800, "provider": "external",
               "public_origin": PUBLIC, "proxy_port": proxy}),
    );
    assert_eq!(started.0, 200, "network start: {}", started.1);
    let created = mac.post("/api/create", json!({"consent": consent(4_000_000_000)}));
    assert_eq!(created.0, 200, "create: {}", created.1);
    let opened = mac.post("/api/pair/window/open", json!({"ttl_seconds": 600}));
    assert_eq!(opened.1["state"], "open", "{}", opened.1);
    let seed: Vec<u16> = listening()
        .into_iter()
        .filter(|p| *p != proxy && !before.contains(p))
        .collect();
    assert_eq!(seed.len(), 1, "one owner seed listener: {seed:?}");
    let ca = std::fs::read(private(&state).join("seed-rs/tls/ca.pem")).unwrap();
    let seams = JoinSeams {
        join_base: Some(format!("http://127.0.0.1:{proxy}")),
        member_seed: SeedOptions {
            ca_pem: Some(ca),
            connect_to: Some(format!("127.0.0.1:{}", seed[0]).parse().unwrap()),
            server_name: Some("127.0.0.1".into()),
        },
    };
    Owner {
        link: opened.1["join_link"].as_str().unwrap().to_owned(),
        window_id: opened.1["window_id"].clone(),
        mac,
        proxy,
        seams,
    }
}
fn member(base: &Path, owner: &Owner) -> Mac {
    let state = base.join("member");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&state)
        .unwrap();
    Mac::launch(
        &state,
        NetworkConfig {
            seams: owner.seams.clone(),
            ..NetworkConfig::default()
        },
    )
}
fn join_body(member: &Mac, link: &str, wall: u64) -> Value {
    let digest = member.status()["join_flow"]["standing_disclosure"]["digest"].clone();
    json!({
        "consent": consent(STORAGE),
        "standing_consent": {"acknowledged": true, "disclosure_digest": digest,
            "caps": {"storage_bytes": STORAGE, "max_memory_bytes": 4_294_967_296u64,
                     "max_wall_seconds": wall, "max_commands": 5000}},
        "join": link,
    })
}
/// The owner approves the member's claimed request; the member's poll becomes `joined`.
fn approve_and_join(owner: &Owner, member: &Mac) -> Value {
    let claimed = owner.mac.until(
        "owner sees the claim",
        |o| o.post("/api/pair/window/list", json!({})).1,
        |v| v["windows"][0]["state"] == "claimed",
    );
    let digest = claimed["windows"][0]["pending"]["request_digest"].clone();
    let approved = owner.mac.post(
        "/api/pair/window/approve",
        json!({"window_id": owner.window_id, "request_digest": digest}),
    );
    assert_eq!(approved.0, 200, "approve: {}", approved.1);
    member.until(
        "member joined",
        |m| m.post("/api/join/window/poll", json!({})).1,
        |v| v["state"] == "joined",
    )
}

/// The member's window shows v0.1.4's join-consent document before anything is chosen; a grant
/// whose caps the consent does not allow is refused by name before any role change; the join
/// with consent waits for the owner, who sees the claim and approves it; the member is then a
/// target with its own current membership, and the owner counts two members.
#[test]
fn packaged_member_joins_with_consent_through_the_owners_window() {
    let base = state_root("join");
    let owner = owner(&base);
    let member = member(&base, &owner);
    let before = member.status();
    assert_eq!(before["installation_role"], "owner", "{before}");
    let disclosure = &before["join_flow"]["standing_disclosure"];
    assert_eq!(
        disclosure["digest"],
        api::preferences::standing_consent::DISCLOSURE_DIGEST,
        "{before}"
    );
    assert_eq!(
        disclosure["text"],
        api::preferences::standing_consent::DISCLOSURE_TEXT,
        "{before}"
    );
    let refused = member.post("/api/join/window", join_body(&member, &owner.link, 30));
    assert_eq!(
        (refused.0, refused.1["error"].clone()),
        (400, json!("standing_consent_caps_invalid"))
    );
    assert_eq!(member.status()["installation_role"], "owner");
    let waiting = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    assert_eq!(waiting.1["state"], "waiting_for_owner", "{}", waiting.1);
    assert_eq!(waiting.1["pinned"], true, "{}", waiting.1);
    let pending = member.status();
    assert_eq!(pending["installation_role"], "target", "{pending}");
    assert_eq!(
        pending["join_flow"]["join_window"]["state"], "waiting_for_owner",
        "{pending}"
    );
    let joined = approve_and_join(&owner, &member);
    assert_eq!(joined["membership_state"], "current", "{joined}");
    assert_eq!(joined["route_ready"], false, "{joined}");
    assert_eq!(joined["report"]["state"], "delivered", "{joined}");
    let after = member.status();
    assert_eq!(after["installation_role"], "target", "{after}");
    assert_eq!(after["authority"]["membership_state"], "current", "{after}");
    assert_eq!(
        after["resource_policy"]["storage_bytes"], STORAGE,
        "{after}"
    );
    assert_eq!(
        after["join_flow"]["standing_consent"]["state"], "granted",
        "{after}"
    );
    let seen = owner.mac.until(
        "owner sees the member",
        |o| o.status(),
        |v| v["authority"]["member_count"] == 2,
    );
    assert_eq!(seen["authority"]["membership_state"], "current", "{seen}");
    // Joined: a second join is `already_joined` (app.py:1747-1748).
    let again = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(
        (again.0, again.1["error"].clone()),
        (400, json!("already_joined"))
    );
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&base).unwrap_or_default();
}

/// v0.1.4's join refusals on the packaged page, each by name: an owner that created a swarm
/// cannot become a target (app.py:1745-1746, also `/api/join/request`), a body without its
/// consent is `consent_required` (:1743-1744), a grant without its three keys is
/// `standing_consent_required` (:1753-1754), a poll with no pending join is `join_window_missing`
/// (:1782), and a retry with another consent is `consent_retry_changed` (:1102-1103).
#[test]
fn packaged_join_refusals_are_v014s() {
    let base = state_root("refusals");
    let owner = owner(&base);
    let refused = |mac: &Mac, path: &str, body: Value| {
        let (status, answer) = mac.post(path, body);
        (
            status,
            answer["error"].as_str().unwrap_or("<answered>").to_owned(),
        )
    };
    let link = owner.link.clone();
    let body = join_body(&owner.mac, &link, 600);
    assert_eq!(
        refused(&owner.mac, "/api/join/window", body.clone()),
        (400, "owner_cannot_become_target".into())
    );
    assert_eq!(
        refused(
            &owner.mac,
            "/api/join/request",
            json!({"consent": consent(STORAGE)})
        ),
        (400, "owner_cannot_become_target".into())
    );
    let member = member(&base, &owner);
    assert_eq!(
        refused(&member, "/api/join/window", json!({"join": link})),
        (400, "consent_required".into())
    );
    let mut partial = join_body(&member, &link, 600);
    partial["standing_consent"] = json!({"acknowledged": true});
    assert_eq!(
        refused(&member, "/api/join/window", partial),
        (400, "standing_consent_required".into())
    );
    assert_eq!(
        refused(&member, "/api/join/window/poll", json!({})),
        (400, "join_window_missing".into())
    );
    // A join that reached the role change keeps its consent: another one is refused.
    let waiting = member.post("/api/join/window", join_body(&member, &link, 600));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    let mut changed = join_body(&member, &link, 600);
    changed["consent"]["storage_bytes"] = json!(STORAGE + 1_000_000_000);
    changed["standing_consent"]["caps"]["storage_bytes"] = json!(STORAGE + 1_000_000_000);
    assert_eq!(
        refused(&member, "/api/join/window", changed),
        (400, "consent_retry_changed".into())
    );
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&base).unwrap_or_default();
}
