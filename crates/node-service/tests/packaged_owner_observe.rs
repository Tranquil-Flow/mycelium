// SPDX-License-Identifier: AGPL-3.0-only
//! PH3b S3c: the owner's run in the PACKAGED host continues past descriptors to observe, with a
//! joined packaged Mac in the same test process (`support/packaged_macs.rs`) running its own
//! standing loop and agent (PH3b S2). v0.1.4 @ f4e366e9, reached from `DemoApp`:
//!
//! * `#demo-start` (run.js:192) -> `DemoApp.action` (app.py:1306) -> `_action` (:1362-1365) ->
//!   `demo_app.action` (demo_app.py:109-133) -> `OwnerRun` -> `AppDemoChain._step_descriptors`
//!   (demo_chain.py:562-725: both Macs report) -> `_step_observe` (demo_chain.py:727-791):
//!   `prepare_observation` publishes a hello-only bootstrap offer per Mac on the owner's managed
//!   mailbox; the owner's own contributor stages its hello locally and the joined Mac's agent
//!   fetches its offer over the owner seed's managed-control routes (demo_target.py:494-514) and
//!   starts its admitted hello helper; `collect` asks both for hello
//!   (`managed_observations.collect_hello_observations`).
//! * On ONE Mac both answers carry the same host identity, so the observation refuses by name:
//!   `managed_observation_hosts_not_distinct` (managed_observations.py), the step fails and the run
//!   ends `failed` at observe (ruling 14: no host-identity seam in the product).
//!
//! Model-free: a loopback repository serves both Macs a tiny snapshot of the pinned run model; the
//! descriptors name an owned executable FILE as the sidecar (never started); the hello helper is
//! the admitted model-free program (`MYCELIUM_BOOTSTRAP_TEST_CONFIG`). No `cloudflared`: the
//! owner's relay is the external provider on a loopback proxy port.
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
#[path = "support/packaged_repository.rs"]
mod repository;
use budgets::BudgetId as B;
use desktop::lifecycle::{Cleanup, Poll};
use macs::{
    Mac, Owner, PUBLIC, Permissive, approve_and_join, consent, free_port, join_body, listening,
    millis, private, state_root,
};
use node_service::{
    packaged::{self, RunConfig},
    packaged_network::{JoinSeams, NetworkConfig},
};
use repository::{Repository, SOURCE, sidecar_file};
use serde_json::{Value, json};
use std::{
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};

/// One packaged Mac over the loopback repository with its network and run seams.
fn launch(state: &Path, repository: &Repository, network: NetworkConfig, run: RunConfig) -> Mac {
    let (core, mut cleanup) = packaged::core_with_run(
        state,
        repository.transport(),
        Arc::new(Permissive),
        network,
        run,
    )
    .unwrap();
    cleanup.bind_clock(Instant::now()).unwrap();
    Mac {
        core,
        cleanup,
        state: state.to_owned(),
    }
}
fn private_dir(base: &Path, name: &str) -> PathBuf {
    let state = base.join(name);
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&state)
        .unwrap();
    state
}
/// `/api/model/download` of the tiny snapshot until this Mac's acquisition prepared it.
fn prepare_model(mac: &Mac) {
    let grant = json!({"consent": {"download_acknowledged": true,
        "max_download_bytes": 10_000_000u64, "storage_limit_bytes": 10_000_000u64}});
    let started = mac.post("/api/model/download", grant);
    assert_eq!(started.0, 200, "download: {}", started.1);
    let status = mac.until(
        "the model prepared",
        |m| {
            std::fs::read(m.state.join(SOURCE).join("status.json"))
                .ok()
                .and_then(|b| serde_json::from_slice(&b).ok())
                .unwrap_or(Value::Null)
        },
        |s| {
            matches!(
                s["state"].as_str(),
                Some("prepared" | "failed" | "cancelled")
            )
        },
    );
    assert_eq!(status["state"], "prepared", "{status}");
}
/// The admitted model-free hello helper (`bootstrap-program.json` in this Mac's private root).
fn admit_hello_program(mac: &Mac) {
    let config = std::env::var("MYCELIUM_BOOTSTRAP_TEST_CONFIG")
        .expect("MYCELIUM_BOOTSTRAP_TEST_CONFIG names the admitted hello helper");
    let document: Value = serde_json::from_slice(&std::fs::read(config).unwrap()).unwrap();
    api::preferences::private_state::store(
        &private(&mac.state).join("bootstrap-program.json"),
        &document,
        api::preferences::private_state::private_json_limit().unwrap(),
    )
    .unwrap();
}
/// A packaged owner (relay on a loopback proxy, swarm, prepared model, open code) and a packaged
/// member joined to it with its own prepared model; both run the admitted hello helper.
fn pair(base: &Path, repository: &Repository, run: RunConfig) -> (Owner, Mac) {
    let sidecar = sidecar_file(base);
    let state = private_dir(base, "owner");
    let before = listening();
    let mac = launch(&state, repository, NetworkConfig::default(), run);
    let proxy = free_port();
    let started = mac.post(
        "/api/network/start",
        json!({"relay_acknowledged": true, "max_seconds": 1800, "provider": "external",
               "public_origin": PUBLIC, "proxy_port": proxy}),
    );
    assert_eq!(started.0, 200, "network start: {}", started.1);
    let created = mac.post(
        "/api/create",
        json!({"consent": consent(api::model_catalog::storage_consent_ceiling())}),
    );
    assert_eq!(created.0, 200, "create: {}", created.1);
    prepare_model(&mac);
    admit_hello_program(&mac);
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
        member_seed: api::seed::SeedOptions {
            ca_pem: Some(ca),
            connect_to: Some(format!("127.0.0.1:{}", seed[0]).parse().unwrap()),
            server_name: Some("127.0.0.1".into()),
        },
    };
    let owner = Owner {
        link: opened.1["join_link"].as_str().unwrap().to_owned(),
        window_id: opened.1["window_id"].clone(),
        mac,
        proxy,
        seams,
    };
    let member = launch(
        &private_dir(base, "member"),
        repository,
        NetworkConfig {
            seams: owner.seams.clone(),
            sidecar: Some(sidecar),
            ..NetworkConfig::default()
        },
        RunConfig::default(),
    );
    let waiting = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    approve_and_join(&owner, &member);
    prepare_model(&member);
    admit_hello_program(&member);
    (owner, member)
}
/// Ask again (one owner poll apart) until `done` holds, within `patience`.
fn until_within(
    what: &str,
    patience: Duration,
    ask: impl Fn() -> Value,
    done: impl Fn(&Value) -> bool,
) -> Value {
    let deadline = Instant::now() + patience;
    loop {
        let value = ask();
        if done(&value) {
            return value;
        }
        assert!(Instant::now() < deadline, "{what}: {value}");
        std::thread::sleep(millis(B::OwnerPoll) / 5);
    }
}
/// The observe step's own registered budgets: initial cleanup, bootstrap, hello collection.
fn observe_patience() -> Duration {
    millis(B::InitialCleanup) * 2 + millis(B::Bootstrap) + millis(B::Collect)
}
fn native_state(path: &Path) -> Value {
    std::fs::read(path)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or(Value::Null)
}

/// Both Macs answer hello with their own helper; on one Mac the observation refuses by name.
#[test]
fn a_joined_packaged_mac_answers_hello_and_one_mac_ends_observe_hosts_not_distinct() {
    let base = state_root("observe");
    let repository = Repository::start();
    let sidecar = sidecar_file(&base);
    let (owner, member) = pair(
        &base,
        &repository,
        RunConfig {
            sidecar: Some(sidecar),
            ..RunConfig::default()
        },
    );
    let (code, started) = owner.mac.post("/api/demo/start", json!({}));
    assert_eq!(code, 200, "{started}");
    let run = until_within(
        "the run ended",
        observe_patience(),
        || owner.mac.status()["owner_run"].clone(),
        |r| matches!(r["state"].as_str(), Some("failed" | "stopped" | "serving")),
    );
    assert_eq!(run["steps"][2]["name"], "descriptors", "{run}");
    assert_eq!(run["steps"][2]["state"], "done", "{run}");
    assert_eq!(run["steps"][2]["detail"], "2 of 2 Macs reported", "{run}");
    assert_eq!(run["participants"].as_array().map(Vec::len), Some(2), "{run}");
    assert_eq!(run["steps"][3]["name"], "observe", "{run}");
    assert_eq!(run["failed_step"], "observe", "{run}");
    assert_eq!(
        run["error_code"], "managed_observation_hosts_not_distinct",
        "{run}"
    );
    assert_eq!(run["route_ready"], false, "{run}");
    // Both Macs ran their own admitted hello helper, and both closed it on the owner's cleanup.
    let owner_native = private(&owner.mac.state).join("local-contributor/native-bootstrap/state.json");
    let member_native = private(&member.state).join("native-bootstrap/state.json");
    for (who, path) in [("owner", &owner_native), ("member", &member_native)] {
        let closed = until_within(
            &format!("the {who}'s hello helper closed"),
            observe_patience(),
            || native_state(path),
            |s| s["phase"] == "closed" || s["phase"] == "cleanup_unproven",
        );
        assert_eq!(closed["phase"], "closed", "{who}: {closed}");
    }
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    drop(repository);
    std::fs::remove_dir_all(&base).unwrap_or_default();
}
