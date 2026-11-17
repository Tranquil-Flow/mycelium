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
    packaged_work::WorkNetwork,
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
/// A Mac's state root. The repository serves the tiny Qwen2 snapshot of `a9-route` (`SOURCE`):
/// both Macs take that run model explicitly, as a saved picker choice, since v0.2's starter is
/// Qwen3-0.6B (B13, `api/contracts/divergences.json` `run_model_default`); a member opens its own
/// run model's acquisition. These tests are about the observation, not the default.
fn private_dir(base: &Path, name: &str) -> PathBuf {
    let state = base.join(name);
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&state)
        .unwrap();
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(private(&state))
        .unwrap();
    api::preferences::run_model::save_choice(&private(&state), "a9-route").unwrap();
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
    pair_on(base, repository, run, WorkNetwork::loopback())
}
/// [`pair`] with the owner's work transport over `owner_work` (the joined Mac's stays on its
/// loopback).
fn pair_on(
    base: &Path,
    repository: &Repository,
    run: RunConfig,
    owner_work: WorkNetwork,
) -> (Owner, Mac) {
    let sidecar = sidecar_file(base);
    let state = private_dir(base, "owner");
    let before = listening();
    // Both Macs of every pair here keep the work transport on loopback, whatever this host's
    // interfaces are (lane LAN: the app's own default reads the Mac's private LAN address).
    let mac = launch(
        &state,
        repository,
        NetworkConfig {
            work_network: owner_work,
            ..NetworkConfig::default()
        },
        run,
    );
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
            work_network: WorkNetwork::loopback(),
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
    let mut last = Value::Null;
    loop {
        let value = ask();
        if done(&value) {
            return value;
        }
        if value != last {
            // The progress so far, in the test's log (`--no-capture`).
            println!("{what}: {value}");
            last = value.clone();
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
    assert_eq!(
        run["participants"].as_array().map(Vec::len),
        Some(2),
        "{run}"
    );
    assert_eq!(run["steps"][3]["name"], "observe", "{run}");
    assert_eq!(run["failed_step"], "observe", "{run}");
    assert_eq!(
        run["error_code"], "managed_observation_hosts_not_distinct",
        "{run}"
    );
    assert_eq!(run["route_ready"], false, "{run}");
    // Both Macs ran their own admitted hello helper, and both closed it on the owner's cleanup.
    let owner_native =
        private(&owner.mac.state).join("local-contributor/native-bootstrap/state.json");
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

#[path = "support/host_identity_double.rs"]
mod observe_test_double;

/// With the OBSERVE PORT replaced by a test double (decision U4), the same two packaged Macs run
/// the chain past observe: both Macs' real hello helpers answer, the owner's cleanup request reaches
/// both (the owner's own contributor closes its worker on request, `_cleanup_if_requested`), every
/// offer is retired and observe is done; the real compile port then reads the double's context.
/// The joined Mac's capability report is current (S4c-a item 2: bound to its membership
/// generation), so compile passes its report check and starts the build; the model-free fixture
/// model (the default Qwen2.5-0.5B run model) has no native challenge producer (only Qwen3-0.6B
/// with the pinned native worker and the real checkpoint, ruling 28: `native_compiler::build`
/// answers none, `native_challenge_producer_unavailable` inside the compiler), and the step fails
/// with that refusal by name. v0.1.4 reports the constant `chain_compile_failed` for any failed
/// build (chain_compile.py:302, demo_chain.py:806-808 @ f4e366e9, the reason only in
/// `compile-error.private.log`); v0.2 deliberately keeps the build's own closed code
/// (`8a4bab8e`, `owner-run/src/steps/compile.rs` `BuildPoll::Done(Err)`; roadmap addendum
/// 13 Nov: the constant had hidden this very refusal for several cycles). This test predated
/// that change (lane BND). The work phase (slices, publish, targets, activate: PH3b S4c-b) is
/// not reached.
#[test]
fn with_an_observe_test_double_the_run_passes_observe_and_compile_refuses_by_name() {
    let base = state_root("observe-double");
    let repository = Repository::start();
    let sidecar = sidecar_file(&base);
    let (owner, member) = pair(
        &base,
        &repository,
        RunConfig {
            sidecar: Some(sidecar),
            observe_test_double: Some(Arc::new(observe_test_double::HostIdentityTestDouble::over)),
            ..RunConfig::default()
        },
    );
    let (code, started) = owner.mac.post("/api/demo/start", json!({}));
    assert_eq!(code, 200, "{started}");
    let run = until_within(
        "the run ended",
        observe_patience() + millis(B::Compile),
        || owner.mac.status()["owner_run"].clone(),
        |r| matches!(r["state"].as_str(), Some("failed" | "stopped" | "serving")),
    );
    println!("S3c observe test double: {run}");
    assert_eq!(run["steps"][2]["detail"], "2 of 2 Macs reported", "{run}");
    assert_eq!(run["steps"][3]["name"], "observe", "{run}");
    assert_eq!(run["steps"][3]["state"], "done", "{run}");
    assert_eq!(
        run["steps"][3]["detail"], "2 Macs observed, 2 distinct hosts",
        "{run}"
    );
    assert_eq!(run["steps"][4]["name"], "compile", "{run}");
    assert_eq!(run["steps"][4]["state"], "failed", "{run}");
    assert_eq!(run["failed_step"], "compile", "{run}");
    // Changed in S4c-a item 2 (was `member_capability_report_stale`: the join report carried no
    // generation and the member sent no periodic report), then by `8a4bab8e` (was the constant
    // `chain_compile_failed`; the build's own refusal is kept, see above).
    assert_eq!(
        run["error_code"], "native_challenge_producer_unavailable",
        "{run}"
    );
    for step in run["steps"].as_array().unwrap().iter().skip(5) {
        assert_eq!(
            step["state"], "pending",
            "the work phase is not reached: {run}"
        );
    }
    assert_eq!(run["route_ready"], false, "{run}");
    // S4c-a item 4: observe retired both bootstrap offers after their cleanup was acknowledged;
    // the run's teardown does not clean them again (a retired offer is final, its receipt kept:
    // `ManagedStageOffers.retire_offer`, managed_offers.py:158-170), so no teardown failure is
    // recorded and the cleanup is not left unproven.
    assert_eq!(run["cleanup_unproven"], Value::Null, "{run}");
    let teardown_failures: Vec<&Value> = run["cleanup_errors"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["code"] == "owner_teardown_failed")
        .collect();
    assert!(teardown_failures.is_empty(), "{run}");
    // Both hello helpers are closed (observe finished only once both acknowledged their cleanup).
    for path in [
        private(&owner.mac.state).join("local-contributor/native-bootstrap/state.json"),
        private(&member.state).join("native-bootstrap/state.json"),
    ] {
        assert_eq!(native_state(&path)["phase"], "closed", "{}", path.display());
    }
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    drop(repository);
    std::fs::remove_dir_all(&base).unwrap_or_default();
}

/// Lane TEN (ten-row gate, real model, run `gate7-wd2`): the second Start in one process ended row 4
/// `bootstrap_context_already_exists` after 6 s. The chain's ports live as long as the host
/// (`work::Composed`), and their teardown never ended the observe port's context, which v0.1.4's
/// teardown does last, whatever failed before (`app._observation_context = None`,
/// demo_chain.py:1436 @ f4e366e9), so the next run's `observe/prepare` refused (app.py:1405-1406).
/// Here the first run ends at compile (no native challenge producer for this model); the second
/// must pass observe again.
#[test]
fn a_second_start_in_one_process_passes_observe_again() {
    let base = state_root("observe-twice");
    let repository = Repository::start();
    let sidecar = sidecar_file(&base);
    let (owner, member) = pair(
        &base,
        &repository,
        RunConfig {
            sidecar: Some(sidecar),
            observe_test_double: Some(Arc::new(observe_test_double::HostIdentityTestDouble::over)),
            ..RunConfig::default()
        },
    );
    let mut ended = Vec::new();
    for _ in 0..2 {
        let (code, started) = owner.mac.post("/api/demo/start", json!({}));
        assert_eq!(code, 200, "{started}");
        let previous = ended.last().map(|r: &Value| r["deployment_id"].clone());
        let run = until_within(
            "the run ended",
            observe_patience() + millis(B::Compile),
            || owner.mac.status()["owner_run"].clone(),
            |r| {
                Some(r["deployment_id"].clone()) != previous
                    && matches!(r["state"].as_str(), Some("failed" | "stopped" | "serving"))
            },
        );
        ended.push(run);
    }
    for run in &ended {
        assert_eq!(run["steps"][3]["name"], "observe", "{run}");
        assert_eq!(run["steps"][3]["state"], "done", "{run}");
    }
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    drop(repository);
    std::fs::remove_dir_all(&base).unwrap_or_default();
}

/// Lane CHATPAGE patch 2, CP-03 (merge-chat2 ten-row gate `20261116T222718Z-gate`, run2 row 3
/// `two_macs_required`, "1 of 2 Macs reported" after 180 s): the gate's two-run shape, model-free.
/// Run 1 passes observe (the owner's managed mailbox now exists) and ends; its teardown announces
/// `stopped` (lane CHATPAGE item 3, demo_chain.py:1381-1390 @ f4e366e9), which the owner's next
/// action may refresh to `idle`; the joined Mac's card reads that end; the owner's Stop; then the
/// next Start. The joined Mac must report into run 2 as it
/// did into run 1: v0.1.4's agent reads the next run's announcement after `stopped` and reports
/// its descriptor (demo_target.py:437-470), and a standing acceptor with no offer to read never
/// keeps it from doing so (the owner's `_member_guard`, managed_control.py:909-941, checks the
/// membership only).
#[test]
fn after_a_runs_stopped_announcement_the_joined_mac_reports_into_the_next_run() {
    let base = state_root("observe-after-stopped");
    let repository = Repository::start();
    let sidecar = sidecar_file(&base);
    let (owner, member) = pair(
        &base,
        &repository,
        RunConfig {
            sidecar: Some(sidecar),
            observe_test_double: Some(Arc::new(observe_test_double::HostIdentityTestDouble::over)),
            ..RunConfig::default()
        },
    );
    let (code, started) = owner.mac.post("/api/demo/start", json!({}));
    assert_eq!(code, 200, "{started}");
    let first = until_within(
        "run 1 ended",
        observe_patience() + millis(B::Compile),
        || owner.mac.status()["owner_run"].clone(),
        |r| matches!(r["state"].as_str(), Some("failed" | "stopped" | "serving")),
    );
    assert_eq!(
        first["steps"][3]["state"], "done",
        "run 1 passed observe: {first}"
    );
    // The gate's Stop (the run already ended here: an idempotent Stop).
    let (code, stopped) = owner.mac.post("/api/demo/stop", json!({}));
    println!("CP-03 run 1 Stop: {code} {stopped}");
    // The joined Mac read the run's end: `stopped`, or `idle` once the owner's next action
    // refreshed the exchange over it (`owner_ports::ensure_exchange`, over idle/stopped only).
    let card = until_within(
        "the joined Mac's card reads run 1's end",
        millis(B::PyMyceliumA13MacosAppDemoappStandingLoopSelfClosedWaitL1203) * 10,
        || member.status()["demo_target"].clone(),
        |c| c["phase"] == "stopped" || c["phase"] == "idle",
    );
    println!("CP-03 member card after run 1: {card}");
    let (code, started) = owner.mac.post("/api/demo/start", json!({}));
    assert_eq!(code, 200, "{started}");
    let second = until_within(
        "run 2 ended",
        observe_patience() + millis(B::Collect) + millis(B::Compile),
        || owner.mac.status()["owner_run"].clone(),
        |r| {
            r["deployment_id"] != first["deployment_id"]
                && matches!(r["state"].as_str(), Some("failed" | "stopped" | "serving"))
        },
    );
    println!("CP-03 run 2: {second}");
    assert_eq!(
        second["steps"][2]["detail"],
        "2 of 2 Macs reported",
        "the joined Mac reported into run 2: {second}; its card: {}",
        member.status()["demo_target"]
    );
    assert_eq!(second["steps"][3]["state"], "done", "{second}");
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    drop(repository);
    std::fs::remove_dir_all(&base).unwrap_or_default();
}

/// S4c-a item 2 (REVIEW-lanePH3B-S2 F4): the joined Mac's capability report stays current as the
/// shipped app keeps it. v0.1.4 @ f4e366e9: every report after the membership exists carries its
/// generation (`signed_capability_link_report`, peer.py:1118-1139, called by `_measure_and_sign`
/// app.py:1128-1136 from `_auto_report` :1138-1163), and the standing loop reports `periodic` on its
/// first tick after a launch and every 300 s (`_next_periodic_report`, :532, :1162; `_standing_tick`
/// :1195-1199 -> standing_consent.py:362-363), recorded as `join_flow.last_report` (:1236).
/// Here the member is relaunched after its join (no re-join): its first standing pass reports, and
/// the owner's run (observe test double, as above) gets past compile's report check
/// (`member_capability_report_stale`, chain_compile `_capability_rows`).
#[test]
fn after_a_member_relaunch_the_run_passes_the_capability_report_check_without_a_rejoin() {
    let base = state_root("observe-relaunch");
    let repository = Repository::start();
    let sidecar = sidecar_file(&base);
    let (owner, member) = pair(
        &base,
        &repository,
        RunConfig {
            sidecar: Some(sidecar.clone()),
            observe_test_double: Some(Arc::new(observe_test_double::HostIdentityTestDouble::over)),
            ..RunConfig::default()
        },
    );
    let state = member.state.clone();
    assert_eq!(member.quit(), Ok(Poll::Verified));
    let member = launch(
        &state,
        &repository,
        NetworkConfig {
            seams: owner.seams.clone(),
            sidecar: Some(sidecar),
            work_network: WorkNetwork::loopback(),
            ..NetworkConfig::default()
        },
        RunConfig::default(),
    );
    let (code, started) = owner.mac.post("/api/demo/start", json!({}));
    assert_eq!(code, 200, "{started}");
    let run = until_within(
        "the run ended",
        observe_patience() + millis(B::Compile),
        || owner.mac.status()["owner_run"].clone(),
        |r| matches!(r["state"].as_str(), Some("failed" | "stopped" | "serving")),
    );
    println!("S4c-a relaunched member: {run}");
    assert_eq!(run["steps"][2]["detail"], "2 of 2 Macs reported", "{run}");
    assert_eq!(run["steps"][3]["state"], "done", "{run}");
    assert_ne!(
        run["error_code"], "member_capability_report_stale",
        "the relaunched member's report is current: {run}"
    );
    assert_ne!(
        run["error_code"], "member_capability_report_required",
        "{run}"
    );
    // S4c-a item 3: past the report check the model-free run ends where the build needs the
    // native challenge producer (Metal, Qwen3-0.6B only), by that name, as the test above.
    assert_eq!(
        run["error_code"], "native_challenge_producer_unavailable",
        "{run}"
    );
    let report = member.status()["join_flow"]["last_report"].clone();
    assert_eq!(report["purpose"], "periodic", "{report}");
    assert_eq!(report["state"], "delivered", "{report}");
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    drop(repository);
    std::fs::remove_dir_all(&base).unwrap_or_default();
}

/// The owner mailbox's rows that would show a claimed, dispatched, acknowledged or replay-recorded
/// request (`control-runtime` mailbox tables), dumped for a before/after comparison.
fn mailbox_effects(owner: &Mac) -> Vec<String> {
    let mut found = Vec::new();
    let mut stack = vec![private(&owner.state)];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(&dir).unwrap().flatten() {
            let path = entry.path();
            if path.is_dir() {
                stack.push(path);
            } else if path.file_name().is_some_and(|n| n == "mailbox.sqlite") {
                found.push(path);
            }
        }
    }
    assert_eq!(found.len(), 1, "one owner mailbox: {found:?}");
    let db = rusqlite::Connection::open_with_flags(
        &found[0],
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    let mut rows = Vec::new();
    for table in [
        "managed_commands",
        "managed_auth_requests",
        "managed_replay_history",
        "control_high_water",
        "control_dispatch",
        "control_dispatch_receipts",
        "managed_stage_offers",
        "managed_stage_history",
    ] {
        let Ok(mut query) = db.prepare(&format!("SELECT * FROM {table}")) else {
            continue;
        };
        let columns = query.column_count();
        let mut result = query.query([]).unwrap();
        while let Some(row) = result.next().unwrap() {
            let cells: Vec<String> = (0..columns)
                .map(|i| format!("{:?}", row.get_ref(i).unwrap()))
                .collect();
            rows.push(format!("{table}: {}", cells.join(" | ")));
        }
    }
    rows
}
/// POST one target poll for `node`'s binding, signed by `signer`, to the owner seed's
/// `/managed-control/poll` (the route a joined Mac's pump uses); the seed's answer or its code.
fn post_poll(
    membership: &serde_json::Map<String, Value>,
    seams: &JoinSeams,
    node: &str,
    signer: &protocol::identity::Identity,
) -> Result<Value, String> {
    let text = |k: &str| membership[k].as_str().unwrap().to_owned();
    let records: Vec<Value> = membership["seed_key_records"].as_array().unwrap().clone();
    let client = api::seed::SeedClient::new(
        &text("seed_url"),
        &text("swarm_id"),
        &text("seed_key_digest"),
        &records,
        &seams.member_seed,
    )
    .unwrap();
    let now = api::app_host::now();
    let nonce = || api::app_host::hex(&api::app_host::random(16).unwrap());
    let statement = json!({
        "protocol": "mycelium.managed_control.target_poll.v1",
        "swarm_id": text("swarm_id"),
        "target_node_id": node,
        "target_endpoint_id": signer.endpoint_id(),
        "target_key_digest": signer.verification_key_digest(),
        "membership_generation": membership["membership_generation"],
        "member_incarnation": membership["incarnation"],
        "run_id": "stage-discovery", "deployment_id": "stage-discovery",
        "request_id": nonce(), "sequence": 1, "nonce": nonce(),
        "issued_at": now, "expires_at": now + 30.0,
    });
    let envelope = json!({
        "protocol": protocol::managed::SIGNED_ENVELOPE_PROTOCOL,
        "signature": signer.sign(&statement).unwrap(),
        "verification_key": signer.public_key_record(),
        "statement": statement,
    });
    client
        .post("/managed-control/poll", &envelope)
        .map_err(|e| e.code)
}

/// REVIEW-lanePH3B-S3C F1 (security boundary): once the owner's run has bound its mailbox on the
/// packaged seed's network routes, a target poll that does not come from a CURRENT member is
/// refused before the mailbox, and nothing is claimed, dispatched, acknowledged or recorded.
/// v0.1.4 @ f4e366e9: the seed's managed routes (`ManagedControlMailbox.handle_http`,
/// managed_control.py:1895-1910, mounted by `run_registry_server` -> tls.py) authenticate every
/// envelope against the current membership (`_current_member`: lifecycle NEW / CONFIGURED /
/// RUNNING and an unexpired lease, else `managed_control_member_stale`).
/// * a stranger (an identity that never joined) naming its own node: `managed_control_member_stale`;
/// * the joined Mac's binding signed by a key that is not its own: refused;
/// * the joined Mac once it stopped renewing (Quit; a Mac that LEFT does the same,
///   member_leave.py:17-18) and its lease (300 s) ran out, with its own key and a foreign one:
///   `managed_control_member_stale`. This test waits that lease out (about five minutes).
#[test]
fn after_observe_the_owners_mailbox_refuses_a_stranger_a_foreign_key_and_an_expired_member() {
    let base = state_root("observe-mailbox-gate");
    let repository = Repository::start();
    let sidecar = sidecar_file(&base);
    let (owner, member) = pair(
        &base,
        &repository,
        RunConfig {
            sidecar: Some(sidecar),
            observe_test_double: Some(Arc::new(observe_test_double::HostIdentityTestDouble::over)),
            ..RunConfig::default()
        },
    );
    let (code, started) = owner.mac.post("/api/demo/start", json!({}));
    assert_eq!(code, 200, "{started}");
    let run = until_within(
        "the run ended",
        observe_patience() + millis(B::Compile),
        || owner.mac.status()["owner_run"].clone(),
        |r| matches!(r["state"].as_str(), Some("failed" | "stopped" | "serving")),
    );
    assert_eq!(
        run["steps"][3]["state"], "done",
        "observe bound the mailbox: {run}"
    );
    let root = private(&member.state);
    let membership = api::peer::load_membership_state(&root).unwrap().unwrap();
    let node = membership["node_id"].as_str().unwrap().to_owned();
    let (own, _, _) = api::app_host::identity(&root).unwrap();
    let stranger = protocol::identity::Identity::from_secret(
        "stranger-endpoint",
        &api::app_host::random(32).unwrap().try_into().unwrap(),
    )
    .unwrap();
    let before = mailbox_effects(&owner.mac);

    let refused = post_poll(&membership, &owner.seams, "stranger-node", &stranger);
    assert_eq!(refused, Err("managed_control_member_stale".to_owned()));
    let refused = post_poll(&membership, &owner.seams, &node, &stranger);
    assert!(
        refused.is_err(),
        "a foreign key for the member's binding: {refused:?}"
    );
    assert_eq!(mailbox_effects(&owner.mac), before);

    // The joined Mac quits: it stops renewing, so its seed row stays RUNNING until its lease runs
    // out (the shipped "left" too: member_leave.py:17-18; the packaged host has no `/api/revoke`,
    // it answers `slice_action_unsupported`, and its seed no clock seam). Until then its own
    // poll, a replay of a sequence its pump already used, is refused by the mailbox
    // (`managed_control_sequence_replayed`); from then on by the member gate, before the mailbox.
    assert_eq!(member.quit(), Ok(Poll::Verified));
    let deadline = std::time::Instant::now()
        + millis(B::PyFinalMyceliumSeedCoordinatorLeaseSecondsL180C31)
        + millis(B::OwnerPoll) * 10;
    loop {
        let answer = post_poll(&membership, &owner.seams, &node, &own);
        if answer == Err("managed_control_member_stale".to_owned()) {
            break;
        }
        assert!(answer.is_err(), "a quit member's replayed poll: {answer:?}");
        assert!(
            std::time::Instant::now() < deadline,
            "the member's lease never ran out: {answer:?}"
        );
        std::thread::sleep(millis(B::OwnerPoll));
    }
    // A foreign key for the expired member: the same.
    let refused = post_poll(&membership, &owner.seams, &node, &stranger);
    assert_eq!(refused, Err("managed_control_member_stale".to_owned()));
    assert_eq!(mailbox_effects(&owner.mac), before);

    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    drop(repository);
    std::fs::remove_dir_all(&base).unwrap_or_default();
}

/// Lane XB patch 3 (REVIEW XB2-03): a joined Mac that refuses the run's SIGNED reach by name says
/// so in its report during collect, long before compose. The owner chooses "Macs on different
/// networks" in its window; the joined Mac's work transport is fixed to its own loopback, so it
/// cannot bind the signed relay mode and reports `work_network_unavailable` at once (no relay is
/// contacted by either Mac: the owner's relay check runs only in compose). The owner's real run
/// (Start: model, contributor, descriptors) fails at descriptors by the joined Mac's name, with
/// its plain message, promptly, instead of waiting out `Descriptors` and failing on the raw
/// member code.
#[test]
fn a_joined_macs_reach_refusal_fails_the_owners_collect_promptly_by_name() {
    let base = state_root("reach-refusal");
    let repository = Repository::start();
    let loopback = std::net::IpAddr::from([127, 0, 0, 1]);
    // The owner's work transport may cross networks (a LAN-shaped choice on loopback).
    let owner_work = WorkNetwork::Fixed(node_service::packaged_work::NetworkChoice(Some((
        loopback,
        runtime::iroh_host::LanPolicy::with_explicit(vec![loopback]).unwrap(),
    ))));
    let run = RunConfig {
        sidecar: Some(sidecar_file(&base)),
        ..RunConfig::default()
    };
    let (owner, member) = pair_on(&base, &repository, run, owner_work);
    let (code, chosen) = owner
        .mac
        .post("/api/network/reach", json!({"reach": "relay"}));
    assert_eq!(code, 200, "{chosen}");
    let began = Instant::now();
    let (code, started) = owner.mac.post("/api/demo/start", json!({}));
    assert_eq!(code, 200, "{started}");
    let run = until_within(
        "the run ended",
        millis(B::Descriptors) * 2,
        || owner.mac.status()["owner_run"].clone(),
        |r| matches!(r["state"].as_str(), Some("failed" | "stopped" | "serving")),
    );
    let waited = began.elapsed();
    println!("XB2-03 reach refusal: {run} after {waited:?}");
    assert_eq!(run["failed_step"], "descriptors", "{run}");
    assert_eq!(run["error_code"], "joined_mac_network_refused", "{run}");
    let message = run["error_message"].as_str().unwrap_or_default().to_owned();
    assert!(
        message.contains("work_network_unavailable") && message.contains("joined Mac"),
        "{run}"
    );
    assert!(
        waited < millis(B::Descriptors) / 4,
        "prompt: {waited:?} of the {:?} collect budget",
        millis(B::Descriptors)
    );
    assert_eq!(
        member.status()["network_reach"]["error"],
        "work_network_unavailable"
    );
    drop((owner, member));
    drop(repository);
    std::fs::remove_dir_all(&base).unwrap_or_default();
}
