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

/// TEST DOUBLE (decision U4, PH3B-COMMON) of the run's OBSERVE PORT, never product code: the real
/// observe adapter over the packaged owner's REAL observation backend (descriptor verification
/// against the owner's seed, real bootstrap offers on the owner's managed mailbox, both Macs' real
/// hello helpers, the real hello and stop over the owner's control journal, cleanup and
/// retirement), except that each hello answer's host and boot identity is replaced by a distinct
/// one per Mac. It is not physical-host evidence.
mod observe_test_double {
    use api::{
        observation_host::{ObservationBackend, VerifiedObservationHost},
        owner_observation::Backend,
    };
    use node_service::packaged_run::{ObserveDouble, PackagedRun};
    use owner_run::{chain::PortError, steps::observe::ObservePort};
    use runtime::{
        observe::{
            CloseFailed, HelloSession, HelloSessions, ObservationContext, ObservationError, Offer,
        },
        observe_owner::ObserveAdapter,
    };
    use serde_json::{Value, json};

    /// The real hello sessions (the owner's managed control journal, the real hello and stop to
    /// each Mac's own helper) with each answer's host and boot identity replaced by a distinct
    /// one per Mac: the one machine-identity fact a single Mac cannot give.
    struct DistinctHosts {
        real: Box<dyn HelloSessions>,
        nodes: Vec<String>,
    }
    struct DistinctHost<'a> {
        real: Box<dyn HelloSession + 'a>,
        index: usize,
    }
    impl HelloSession for DistinctHost<'_> {
        fn send_hello(&mut self, command: &str) -> Result<Value, Option<String>> {
            let mut response = self.real.send_hello(command)?;
            // The validator's identity shape (`host-`/`boot-` + 32 hex digits), one per Mac.
            let index = 0x00d0_b1e0_u128 + self.index as u128;
            response["result"]["host_id"] = json!(format!("host-{index:032x}"));
            response["result"]["boot_id"] = json!(format!("boot-{index:032x}"));
            Ok(response)
        }
        fn close(&mut self) -> Result<(), CloseFailed> {
            self.real.close()
        }
        fn cleanup_error(&self) -> bool {
            self.real.cleanup_error()
        }
        fn stop_response_ok(&self) -> Option<bool> {
            self.real.stop_response_ok()
        }
    }
    impl HelloSessions for DistinctHosts {
        fn run_id(&self) -> &str {
            self.real.run_id()
        }
        fn deployment_id(&self) -> &str {
            self.real.deployment_id()
        }
        fn bound_nodes(&self) -> std::collections::BTreeSet<String> {
            self.real.bound_nodes()
        }
        fn binding_current(&mut self, node: &str) -> bool {
            self.real.binding_current(node)
        }
        fn open(
            &mut self,
            node: &str,
            timeout: f64,
        ) -> Result<Box<dyn HelloSession + '_>, Option<String>> {
            let index = self
                .nodes
                .iter()
                .position(|n| n == node)
                .ok_or(Some("managed_observation_member_not_current".to_owned()))?;
            Ok(Box::new(DistinctHost {
                real: self.real.open(node, timeout)?,
                index,
            }))
        }
    }
    /// The real backend; only the host identity in its hello answers is replaced.
    struct Hellos(Backend<PackagedRun>);
    impl ObservationBackend for Hellos {
        fn is_owner(&self) -> bool {
            self.0.is_owner()
        }
        fn running(&self) -> bool {
            self.0.running()
        }
        fn prepared_manifest_digest(&mut self) -> Result<String, ObservationError> {
            self.0.prepared_manifest_digest()
        }
        fn swarm_id(&self) -> String {
            self.0.swarm_id()
        }
        fn member(&self, node: &str) -> Option<Value> {
            self.0.member(node)
        }
        fn wall_clock(&self) -> f64 {
            self.0.wall_clock()
        }
        fn session_factory(
            &mut self,
            run: &str,
            deployment: &str,
            members: &[Value],
        ) -> Result<Box<dyn HelloSessions>, ObservationError> {
            Ok(Box::new(DistinctHosts {
                real: self.0.session_factory(run, deployment, members)?,
                nodes: members
                    .iter()
                    .filter_map(|m| m["node_id"].as_str().map(str::to_owned))
                    .collect(),
            }))
        }
        fn publish_offer(
            &mut self,
            member: &Value,
            run: &str,
            deployment: &str,
            archive: &[u8],
        ) -> Result<Offer, ObservationError> {
            self.0.publish_offer(member, run, deployment, archive)
        }
        fn retire_offer(&mut self, offer: &Offer) -> Result<Value, ObservationError> {
            self.0.retire_offer(offer)
        }
        fn request_stage_cleanup(&mut self, offer: &Offer) -> Result<(), ObservationError> {
            self.0.request_stage_cleanup(offer)
        }
        fn rebuild_observation(
            &mut self,
            context: &mut Option<ObservationContext>,
            run: &str,
            deployment: &str,
            descriptors: &[Value],
        ) -> Result<String, PortError> {
            self.0
                .rebuild_observation(context, run, deployment, descriptors)
        }
    }
    fn nonce() -> String {
        api::app_host::hex(&api::app_host::random(16).unwrap_or_default())
    }
    type Adapter = ObserveAdapter<VerifiedObservationHost<Hellos>, fn() -> f64, fn() -> String>;
    /// The observe port the test gives the packaged run (`RunConfig::observe_test_double`).
    pub struct HostIdentityTestDouble(Adapter);
    impl HostIdentityTestDouble {
        pub fn over(backend: Backend<PackagedRun>) -> Box<dyn ObserveDouble> {
            Box::new(Self(ObserveAdapter::new(
                VerifiedObservationHost {
                    backend: Hellos(backend),
                },
                api::app_host::now as fn() -> f64,
                nonce as fn() -> String,
            )))
        }
    }
    impl ObservePort for HostIdentityTestDouble {
        fn prepare(
            &mut self,
            run: &str,
            deployment: &str,
            cards: &[Value],
        ) -> Result<(), PortError> {
            self.0.prepare(run, deployment, cards)
        }
        fn rebuild(
            &mut self,
            run: &str,
            deployment: &str,
            cards: &[Value],
        ) -> Result<String, PortError> {
            self.0.rebuild(run, deployment, cards)
        }
        fn facts_nodes(&mut self) -> Vec<String> {
            self.0.facts_nodes()
        }
        fn offers(&mut self) -> Vec<(String, String)> {
            self.0.offers()
        }
        fn collect(&mut self) -> Result<(), PortError> {
            self.0.collect()
        }
        fn observed_hosts(&mut self) -> Vec<Option<String>> {
            self.0.observed_hosts()
        }
        fn retire_bootstrap(&mut self) -> Result<(), PortError> {
            self.0.retire_bootstrap()
        }
    }
    impl ObserveDouble for HostIdentityTestDouble {
        fn context(&self) -> Option<&ObservationContext> {
            self.0.context()
        }
        fn teardown(&mut self) -> Result<(), String> {
            self.0.host_mut().backend.0.teardown()
        }
    }
}

/// With the OBSERVE PORT replaced by a test double (decision U4), the same two packaged Macs run
/// the chain past observe: both Macs' real hello helpers answer, the owner's cleanup request reaches
/// both (the owner's own contributor closes its worker on request, `_cleanup_if_requested`), every
/// offer is retired and observe is done; the real compile port then reads the double's context.
/// The joined Mac's capability report is current (S4c-a item 2: bound to its membership
/// generation), so compile passes its report check and starts the build; the model-free fixture
/// model (the default Qwen2.5-0.5B run model) has no native challenge producer (only Qwen3-0.6B
/// with the pinned native worker and the real checkpoint, ruling 28: `native_compiler::build`
/// answers none, `native_challenge_producer_unavailable` inside the compiler), and the step fails
/// with the workflow's failed state, `chain_compile_failed` (chain_compile `failed_state`). The
/// work phase (slices, publish, targets, activate: PH3b S4c-b) is not reached.
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
    // generation and the member sent no periodic report).
    assert_eq!(run["error_code"], "chain_compile_failed", "{run}");
    for step in run["steps"].as_array().unwrap().iter().skip(5) {
        assert_eq!(
            step["state"], "pending",
            "the work phase is not reached: {run}"
        );
    }
    assert_eq!(run["route_ready"], false, "{run}");
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
    // native challenge producer (Metal, Qwen3-0.6B only), as the test above.
    assert_eq!(run["error_code"], "chain_compile_failed", "{run}");
    let report = member.status()["join_flow"]["last_report"].clone();
    assert_eq!(report["purpose"], "periodic", "{report}");
    assert_eq!(report["state"], "delivered", "{report}");
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    drop(repository);
    std::fs::remove_dir_all(&base).unwrap_or_default();
}
