// SPDX-License-Identifier: AGPL-3.0-only
//! Two packaged host roles on loopback. Real enrollment, seed TLS, member control, carrier,
//! inert shard receipts and cleanup. Constructor-only source plan and refusing native admission.
use super::*;
mod regressions;
use crate::packaged_network::{JoinSeams, MemberSeams, NetworkConfig, PackagedMember};
use api::{
    handoff,
    joined_owner::{
        link::{Refusal, Stage},
        work::{Admission, RouteOrder},
    },
    member_agent::MemberAgent,
    peer,
    preferences::standing_consent,
};
use std::{fs, path::Path, sync::atomic::AtomicUsize};
struct NoNative;
impl crate::service::Driver for NoNative {
    fn run(&self, _: &mut crate::service::Job) -> Result<(), PublicError> {
        panic!("native/model execution forbidden")
    }
}
struct NoCodec;
impl Codec for NoCodec {
    fn encode(&self, _: &str) -> Result<Vec<u32>, PublicError> {
        panic!("tokenizer must not run")
    }
    fn decode(&self, _: u32) -> Result<String, PublicError> {
        panic!("tokenizer must not run")
    }
}
struct RefusingCompute(Arc<AtomicUsize>);
impl Admission for RefusingCompute {
    fn prepare(
        &mut self,
        stages: &mut [Stage<'_>],
        _: usize,
        _: &Path,
        _: Duration,
    ) -> Result<(), Refusal> {
        assert_eq!(stages.len(), 2);
        self.0.fetch_add(1, Ordering::SeqCst);
        Err(Refusal("test_native_compute_not_run".into()))
    }
    fn issue(&mut self, _: &RouteOrder<'_>) -> Result<Value, Refusal> {
        panic!("no native proof exists")
    }
}
struct Env {
    at: Instant,
}
impl StepEnv for Env {
    fn progress(&mut self, _: &str) {}
    fn stopped(&self) -> bool {
        false
    }
    fn now(&mut self) -> f64 {
        100.0 + self.at.elapsed().as_secs_f64()
    }
    fn wall(&mut self) -> f64 {
        app_host::now()
    }
    fn set_result(&mut self, _: &str, _: Value) {}
    fn record_exception(&mut self, _: &str, _: &str) {
        panic!("unexpected exception")
    }
    fn pause(&mut self, seconds: f64) -> bool {
        std::thread::sleep(Duration::from_secs_f64(seconds));
        false
    }
}
struct Fixture {
    root: PathBuf,
    host: PackagedRun,
    member: Arc<PackagedMember>,
    agent: MemberAgent<PackagedMember>,
    remote: String,
}
fn consent(root: &Path, session: &api::member::Session, policy: &Value) {
    resource_policy::write(root, policy).unwrap();
    let wall = BudgetId::MemberChosenWall
        .span(Entries::ZERO)
        .unwrap()
        .millis()
        / 1000;
    standing_consent::grant(root,&json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,
        "caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":wall,"max_commands":1000}}),policy,app_host::now(),Some(16_000_000_000)).unwrap();
    let b = session.binding().unwrap();
    standing_consent::bind(
        root,
        &json!({"swarm_id":b.swarm_id,"node_id":b.node_id,"seed_key_digest":b.seed_key_digest}),
        app_host::now(),
    )
    .unwrap();
}
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "s4cb2-owner-{}",
            crate::service::random_id().unwrap()
        ));
        fs::create_dir(&root).unwrap();
        let root = root.canonicalize().unwrap();
        let owner = root.join("owner");
        let authority = Arc::new(Mutex::new(Authority::load(owner.clone()).unwrap()));
        let network = Network::new(&owner, NetworkConfig::default());
        let policy = json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
        resource_policy::write(&owner, &policy).unwrap();
        app_host::create_owner_json(&network.cell(&authority), policy.as_object().unwrap())
            .unwrap();
        let host = PackagedRun::new(
            authority,
            network,
            NodeService::new(
                Arc::new(NoNative),
                "s4cb2-owner".into(),
                crate::native::development_qualification().unwrap(),
            ),
            ModelHooks {
                acquisition: Box::new(|| None),
                resource_check: Box::new(|| None),
                reverify: Box::new(|| panic!("no model")),
            },
            None,
        );
        owner_ports::start_local_contributor(&host).unwrap();
        owner_ports::renew_contributor(&host);
        host.with(|c| {
            consent(
                &owner_ports::contributor_dir(&owner),
                c.run
                    .contributor
                    .as_ref()
                    .unwrap()
                    .session
                    .as_ref()
                    .unwrap(),
                &policy,
            )
        });
        let seed = host.with(|c| c.authority.seed.clone().unwrap());
        let member_root = root.join("member");
        let mut authority = Authority::load(member_root.clone()).unwrap();
        authority.target = true;
        authority.running = true;
        let request = app_host::build_pair_request(&member_root, &member_root).unwrap();
        let (signer, _, _) = app_host::identity(&member_root).unwrap();
        let (seed_signer, _, _) =
            app_host::identity_in(&owner, app_host::SEED_IDENTITY_FILE).unwrap();
        let issuer = handoff::Coordinator {
            signer: seed_signer,
            swarm_id: seed.coordinator.swarm_id().into(),
            seed_url: seed.seed_url.clone(),
        };
        let ttl = BudgetId::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37
            .span(Entries::ZERO)
            .unwrap()
            .millis()
            / 1000;
        let offered=handoff::approve_pairing(&owner,true,Some(&issuer),&request.to_value(),&json!(ttl),&json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false}),app_host::now(),&handoff::Entropy::os).unwrap();
        let options = seed.options();
        let random = |n| app_host::hex(&app_host::random(n).unwrap());
        let env = peer::JoinEnv {
            signer: &signer,
            clock: &app_host::now,
            random_hex: &random,
            seed: &options,
        };
        let mut joined =
            peer::accept_handoff(&member_root, true, &offered.to_value(), &env).unwrap();
        peer::renew(&member_root, true, joined.session.as_mut(), &env).unwrap();
        let session = joined.session.unwrap();
        consent(&member_root, &session, &policy);
        let remote = session.node_id().to_owned();
        authority.session = Some(session);
        let network = Network::new(
            &member_root,
            NetworkConfig {
                seams: JoinSeams {
                    member_seed: options,
                    ..Default::default()
                },
                ..Default::default()
            },
        );
        let member = PackagedMember::new(
            Arc::new(Mutex::new(authority)),
            network,
            MemberSeams {
                closed: Box::new(|| false),
                resource: Box::new(|_| None),
                acquisition: Box::new(|| None),
            },
        );
        Self {
            root,
            host,
            member,
            agent: MemberAgent::default(),
            remote,
        }
    }
    fn plan(&self, state: &RunState) -> Plan {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../planner/tests/fixtures/source-slices.json"
        ))
        .unwrap();
        let case = &fixture["cases"][0];
        let objects = self.root.join("stage-shards");
        fs::create_dir(&objects).unwrap();
        let (mut files, mut plans) = (Map::new(), Map::new());
        for (i, node) in state.participants.iter().enumerate() {
            let fixture_node = format!("node-{i}");
            let mut recipe = case["node_plans"][&fixture_node].clone();
            recipe["node_id"] = json!(node);
            recipe.as_object_mut().unwrap().remove("node_plan_digest");
            recipe["node_plan_digest"] = json!(protocol::canonical::digest(&recipe).unwrap());
            let file = case["node_files"][&fixture_node][0].clone();
            let name = file["path"]
                .as_str()
                .unwrap()
                .strip_prefix("deployment/")
                .unwrap();
            let encoded = case["shard_files"][name].as_str().unwrap();
            let bytes: Vec<u8> = (0..encoded.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&encoded[i..i + 2], 16).unwrap())
                .collect();
            fs::write(objects.join(name), bytes).unwrap();
            files.insert(node.clone(), json!([file]));
            plans.insert(node.clone(), recipe);
        }
        Plan {
            run_id: state.run_id.clone(),
            deployment_id: state.deployment_id.clone(),
            model: json!({"model_id":"Qwen/Qwen3-0.6B","resolved_commit":"c1899de289a04d12100db370d81485cdf75e47ca","manifest_digest":"sha256:00"}),
            document: json!({"node_files":files,"node_slice_plans":plans}),
            objects: objects::view(&self.root, &json!({"node_files":files})).unwrap(),
            journal: self.root.join("journal"),
            ranges: state
                .participants
                .iter()
                .enumerate()
                .map(|(i, n)| (n.clone(), [14 * i as u64, 14 * (i as u64 + 1)]))
                .collect(),
            tokens: 32,
            forbidden_source: self.root.join("snapshot"),
            worker_walls: state.worker_wall_limits.clone(),
            owner_root: self.root.join("owner"),
        }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        self.agent.close(&self.member);
        self.host.stop_work();
        if let Some(seed) = self.host.with(|c| c.authority.seed.clone()) {
            seed.close();
        }
        if let Err(e) = fs::remove_dir_all(&self.root) {
            eprintln!("s4cb2_fixture_cleanup_failed:{e}");
        }
    }
}
#[test]
fn local_work_session_tracks_renewal_and_contributor_removal() {
    let f = Fixture::new();
    let work = Work::new(f.host.clone());
    f.host.with(|_| ());
    let cell = lock(&work.resident).local_session.clone();
    assert!(
        lock(&cell).is_some(),
        "local work must observe enrolled Session, not a lease-frozen clone"
    );
    let session = f
        .host
        .with(|c| c.run.contributor.as_mut().unwrap().session.take());
    assert!(
        lock(&cell).is_some(),
        "renewal flight retains the lease-bounded snapshot"
    );
    f.host
        .with(|c| c.run.contributor.as_mut().unwrap().session = session);
    f.host.with(|c| c.run.contributor = None);
    assert!(
        lock(&cell).is_none(),
        "contributor removal must invalidate resident work"
    );
}
#[test]
fn stop_reaches_real_push_while_member_receive_is_held() {
    let mut f = Fixture::new();
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    seed.exchange
        .set_owner_control(Some(OwnerControl::new(ports.work.owner.public())));
    seed.exchange
        .announce(Announce {
            phase: "compile".into(),
            run_id: Some("run-stop".into()),
            deployment_id: Some("dep-stop".into()),
            ..Default::default()
        })
        .unwrap();
    f.agent.tick(&f.member);
    let local = f
        .host
        .with(|c| c.run.contributor.as_ref().unwrap().node.clone());
    let mut state = RunState::new("run-stop", "dep-stop", owner_run::Reason::Start);
    state.participants = vec![local.clone(), f.remote.clone()];
    state.local_node = Some(local);
    let wall = BudgetId::MemberChosenWall
        .span(Entries::ZERO)
        .unwrap()
        .millis()
        / 1000;
    state.worker_wall_limits = state
        .participants
        .iter()
        .cloned()
        .map(|n| (n, wall))
        .collect();
    let mut env = Env { at: Instant::now() };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    let within = span(BudgetId::RouterClose).unwrap();
    let (entered, waiting) = std::sync::mpsc::sync_channel(1);
    let (release, held) = std::sync::mpsc::sync_channel(1);
    lock(&ports.work.resident)
        .local
        .as_ref()
        .unwrap()
        .on_receive(Box::new(move |_| {
            entered.send(()).unwrap();
            held.recv_timeout(span(BudgetId::MemberChosenWall).unwrap())
                .unwrap();
        }));
    let (done, finished) = std::sync::mpsc::sync_channel(1);
    let mut work = ports.work.clone();
    std::thread::scope(|scope| {
        let thread = scope.spawn(move || {
            done.send(work.node_slice_plans()).unwrap();
        });
        waiting.recv_timeout(within).unwrap();
        f.host.stop_work();
        let result = finished.recv_timeout(within);
        release.send(()).unwrap();
        thread.join().unwrap();
        assert!(
            matches!(result, Ok(Err(StepError::Stopped))),
            "Stop must reach the port before the held receive is released: {result:?}"
        );
    });
    ports.close_work().unwrap();
    assert!(lock(&ports.work.resident).work.is_none());
}
#[test]
fn two_packaged_hosts_handoff_local_and_remote_links_and_push_before_publish() {
    let mut f = Fixture::new();
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    seed.exchange
        .set_owner_control(Some(OwnerControl::new(ports.work.owner.public())));
    seed.exchange
        .announce(Announce {
            phase: "compile".into(),
            run_id: Some("run-s4cb2".into()),
            deployment_id: Some("dep-s4cb2".into()),
            ..Default::default()
        })
        .unwrap();
    f.agent.tick(&f.member);
    let local = f
        .host
        .with(|c| c.run.contributor.as_ref().unwrap().node.clone());
    let mut state = RunState::new("run-s4cb2", "dep-s4cb2", owner_run::Reason::Start);
    state.participants = vec![local.clone(), f.remote.clone()];
    state.local_node = Some(local);
    let wall = BudgetId::MemberChosenWall
        .span(Entries::ZERO)
        .unwrap()
        .millis()
        / 1000;
    state.worker_wall_limits = state
        .participants
        .iter()
        .cloned()
        .map(|n| (n, wall))
        .collect();
    let mut env = Env { at: Instant::now() };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    let called = Arc::new(AtomicUsize::new(0));
    {
        let mut resident = lock(&ports.work.resident);
        let work = resident.work.take().unwrap();
        resident.work = Some(work.with_admission(Box::new(RefusingCompute(called.clone()))));
    }
    let proposal = json!({"max_combined_rss_bytes":2_147_483_648u64,"max_additional_disk_bytes":4_000_000_000u64,"max_wall_seconds":wall});
    state.proposal = Some(proposal.clone());
    use owner_run::steps::model::{ModelStepOutput, PreparedModel, PreparedSource};
    state.model = Some(ModelStepOutput {
        model_id: "Qwen/Qwen3-0.6B".into(),
        resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
        manifest_digest: "sha256:00".into(),
        slice_mode: true,
        choice: None,
        prepared: PreparedSource {
            model: PreparedModel {
                model_id: "Qwen/Qwen3-0.6B".into(),
                resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
                architecture: "qwen3".into(),
                label: "constructor-only inert source fixture".into(),
                choice: None,
                shape_registered: true,
                owner_storage_need: 0,
            },
            manifest_digest: "sha256:00".into(),
            source_bytes: json!(0),
        },
    });
    let slices = ports.slices();
    owner_run::steps::slices::run_slices_step(
        &mut env,
        slices.reports,
        slices.slices,
        &mut state,
        span(BudgetId::OwnerPoll).unwrap().as_secs_f64(),
    )
    .unwrap();
    assert!(
        state.execution_clock_started_at.is_none(),
        "slices do not start the serving clock"
    );
    assert_eq!(
        ports.work.shares_ready().unwrap_err(),
        "member_work_share_changed"
    );
    f.agent.tick(&f.member);
    ports.work.shares_ready().unwrap();
    let flow = ports.work.workflow_state().unwrap();
    assert_eq!(flow["shards_transferred"], true);
    assert!(flow["route_expires_at"].is_null());
    assert_eq!(called.load(Ordering::SeqCst), 0);
    assert_eq!(flow["transferred"].as_array().unwrap().len(), 2);
    let publication = ports.publication();
    let outcome = owner_run::steps::publish::run_publish_step(
        &mut env,
        publication.reports,
        publication.workflow,
        &mut state,
        span(BudgetId::OwnerPoll).unwrap().as_secs_f64(),
    );
    assert!(
        matches!(outcome, Err(StepError::Refused(ref e)) if e.code == "test_native_compute_not_run"),
        "{outcome:?}"
    );
    assert!(
        state.execution_clock_started_at.is_some_and(|v| v >= 100.0),
        "publish uses StepEnv origin"
    );
    assert!(
        ports
            .publication()
            .workflow
            .start_serving(&proposal)
            .is_err(),
        "publish is once-only"
    );
    let until = Instant::now() + span(BudgetId::RouterClose).unwrap();
    while ports.work.workflow_state().unwrap()["state"] != "failed" {
        assert!(Instant::now() < until);
        std::thread::sleep(span(BudgetId::OwnerPoll).unwrap());
    }
    assert_eq!(
        ports.work.workflow_state().unwrap()["error_code"],
        "test_native_compute_not_run"
    );
    assert_eq!(called.load(Ordering::SeqCst), 1);
    assert!(
        ports.work.activate(&proposal).is_err(),
        "no fabricated activation proof"
    );
    ports.close_work().unwrap();
    assert!(lock(&ports.work.resident).work.is_none());
    assert!(lock(&ports.work.resident).local.is_none());
    assert_eq!(
        seed.coordinator.member(&f.remote).unwrap()["lifecycle_state"],
        "RUNNING"
    );
}
