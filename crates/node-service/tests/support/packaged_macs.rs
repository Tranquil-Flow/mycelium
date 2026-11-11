// SPDX-License-Identifier: AGPL-3.0-only
//! Two packaged Macs in one test process (lane PH3a): each is the packaged executable's real
//! composition (`packaged::core_with_network`) over its own private state root, driven through the
//! window's callbacks as the page drives them. The owner's relay is the external provider on a
//! loopback proxy port (no `cloudflared`, no internet); a member reaches it through v0.1.4's own
//! join seam (`_test_join_base_url`, app.py:506-513 @ f4e366e9) and the owner's seed through its
//! pinned local CA, both given to the member's composition, never through the environment.
#![allow(dead_code)] // each test binary uses its own part of these helpers
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

pub const PUBLIC: &str = "https://v02-packaged-pair.example.org";
pub const STORAGE: u64 = 2_000_000_000;

pub fn millis(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
pub fn state_root(tag: &str) -> PathBuf {
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
pub fn private(state: &Path) -> PathBuf {
    state.join("v02-node/private")
}
pub fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap()
}
pub struct Permissive;
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
pub struct Mac {
    pub core: Arc<dyn Core>,
    pub cleanup: AppCleanup,
    pub state: PathBuf,
}
impl Mac {
    pub fn launch(state: &Path, config: NetworkConfig) -> Self {
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
    pub fn request(&self, method: &str, path: &str, body: &Value) -> (u16, Value) {
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
    pub fn post(&self, path: &str, body: Value) -> (u16, Value) {
        self.request("POST", path, &body)
    }
    pub fn status(&self) -> Value {
        let (code, value) = self.request("GET", "/api/status", &json!({}));
        assert_eq!(code, 200, "{value}");
        value
    }
    /// Ask again (one owner poll apart) until `done` holds, within the join patience.
    pub fn until(
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
    pub fn quit(mut self) -> Result<Poll, CleanupError> {
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
pub fn consent(storage: u64) -> Value {
    json!({
        "peer_visibility": true, "power": "ac_only", "storage_bytes": storage,
        "network": "unmetered_only", "background": false, "diagnostics": false,
        "thermal": "stop_on_serious",
    })
}
/// The TCP ports this test process listens on (`lsof`), for the owner seed's own port.
pub fn listening() -> Vec<u16> {
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
pub struct Owner {
    pub mac: Mac,
    pub proxy: u16,
    pub link: String,
    pub window_id: Value,
    pub seams: JoinSeams,
}
pub fn owner(base: &Path) -> Owner {
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
pub fn member(base: &Path, owner: &Owner) -> Mac {
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
/// A member whose own lease renewal runs every `period` (the composition's renewal-period seam)
/// instead of v0.1.4's 20 s (`RenewLoopSelfClosedWait`, app.py:917).
pub fn member_renewing(base: &Path, owner: &Owner, period: B) -> Mac {
    let state = base.join("member");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&state)
        .unwrap();
    Mac::launch(
        &state,
        NetworkConfig {
            seams: owner.seams.clone(),
            renewal: period,
            ..NetworkConfig::default()
        },
    )
}
/// `(node_id, lease_expires_at)` of every member row in the owner's seed's committed table
/// (`seed-rs/seed.sqlite`, the snapshot the seed persists with each renewal).
pub fn leases(owner: &Mac) -> Vec<(String, f64)> {
    let seed = rusqlite::Connection::open_with_flags(
        private(&owner.state).join("seed-rs/seed.sqlite"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    let snapshot: String = seed
        .query_row(
            "SELECT snapshot FROM rs_seed_table WHERE id = 1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let snapshot: Value = serde_json::from_str(&snapshot).unwrap();
    snapshot["members"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| {
            (
                m["node_id"].as_str().unwrap().to_owned(),
                m["lease_expires_at"].as_f64().unwrap(),
            )
        })
        .collect()
}
/// The lease of the one member row that is not the owner's own (`owner-swarm.json`'s node).
pub fn member_lease(owner: &Mac) -> Option<f64> {
    let own: Value = serde_json::from_slice(
        &std::fs::read(private(&owner.state).join("owner-swarm.json")).unwrap(),
    )
    .unwrap();
    leases(owner)
        .into_iter()
        .find(|(node, _)| own["node_id"] != node.as_str())
        .map(|(_, at)| at)
}
pub fn join_body(member: &Mac, link: &str, wall: u64) -> Value {
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
pub fn approve_and_join(owner: &Owner, member: &Mac) -> Value {
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
