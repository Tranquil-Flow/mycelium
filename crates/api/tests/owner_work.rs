// SPDX-License-Identifier: AGPL-3.0-only
//! PH3b S4a: the owner's work port over `api::joined_owner` (gap G4a).
//!
//! v0.1.4 call chain (f4e366e9): `DemoApp` (app.py:505) -> `_action` start (app.py:1362) ->
//! `demo_app.action` (demo_app.py:109-133) -> `OwnerRun` steps publish/targets/activate
//! (owner_run.py:30-40) -> `demo_chain.py:890-985` -> `chain_compile.start_serving`
//! (chain_compile.py:308-341) -> `workflow.run` (workflow.py:277-345); serving loop
//! (owner_run.py:252-257) -> `rollover_due` / `ceiling_reached` (demo_chain.py:1125-1164);
//! `status()` monitor (owner_run.py:171-185) -> demo_chain.py:1166-1186, lease-expired member
//! (demo_chain.py:532-546, control_session.py:315-326).
//!
//! Model-free: a real TLS seed with real pairing and standing consent, the real artifact
//! carrier over iroh loopback, and a TEST MEMBER behind the owner's `MemberLink` that runs the
//! member's real receive (`JoinedShard` + `ChunkSession`). The test member has no native worker:
//! it refuses `prepare_native` by name and never fabricates a resident proof. Clocks are injected.
use api::{
    app_host, handoff,
    joined_owner::{
        link::{MemberLink, Refusal},
        work::{self, Clocks, OwnerWork, Plan, WorkMember},
    },
    joined_shard::{self, JoinedShard},
    m16_route::Codec,
    member::Session,
    peer,
    preferences::{resource_policy, standing_consent},
    schema::PublicError,
};
use budgets::{BudgetId as B, Entries};
use owner_run::{
    Clock, Identity as RunIdentity, OwnerRun, Reason, StepOutcome,
    machine::{CeilingProbe, MonitorProbe},
    state::RunState,
    steps::publish::WorkPort,
};
use protocol::{artifact_grant::ChunkRequest, identity::Identity};
use runtime::{
    iroh_artifact::ChunkSession, iroh_host::LoopbackHost, sidecar_artifact::serve_limits,
};
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};
use transport::{Channel, Clock as _};

static NEXT: AtomicU64 = AtomicU64::new(0);

fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn seconds(id: B) -> f64 {
    id.span(Entries::ZERO).unwrap().millis() as f64 / 1000.0
}

/// What a test member saw and received.
#[derive(Default)]
struct Seen {
    commands: Vec<String>,
    received: Option<Vec<u8>>,
}
/// TEST MEMBER (test double, not product code): a joined member behind the owner's link that
/// answers `consent_status` with its own consent and policy and runs the member's real shard
/// receive; it has no native worker, so `prepare_native` is refused by name.
struct TestMember {
    root: PathBuf,
    signer: Identity,
    secret: iroh::SecretKey,
    session: Session,
    seen: Arc<Mutex<Seen>>,
    reply: Option<Result<Value, Refusal>>,
}
const NO_NATIVE: &str = "test_member_native_unavailable";
impl TestMember {
    fn carrier(&self) -> Identity {
        Identity::from_secret(
            &self.secret.public().to_string(),
            &self.signer.secret_bytes(),
        )
        .unwrap()
    }
    fn answer(&mut self, command: &Value) -> Result<Value, Refusal> {
        let refused = |code: &str| Refusal(code.into());
        match command["command"].as_str() {
            Some("consent_status") => Ok(json!({
                "consent": standing_consent::load(&self.root).map_err(|e| refused(e.0))?,
                "policy": resource_policy::read(&self.root).map_err(|e| refused(e.code))?})),
            Some("receive") => self.receive(command).map_err(|e| refused(&e)),
            Some("prepare_native") => Err(refused(NO_NATIVE)),
            _ => Err(refused("test_member_command_unexpected")),
        }
    }
    /// The member's real receive: custody first, then every chunk over the carrier, then verify.
    fn receive(&mut self, command: &Value) -> Result<Value, String> {
        let offer = &command["offer"];
        let carrier = self.carrier();
        let mut shard = JoinedShard::begin(
            &self.root,
            &self.session,
            &self.signer,
            &carrier,
            offer,
            app_host::now(),
        )
        .map_err(|e| e.0.to_owned())?;
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .map_err(|e| e.to_string())?;
        let clock = api::managed_carrier::HostClock::default();
        let deadline = || {
            clock
                .monotonic()
                .checked_add(B::WorkerStartup.span(Entries::ZERO).unwrap())
                .unwrap()
        };
        let mut host = LoopbackHost::bind_with_secret(&rt, &clock, deadline(), self.secret.clone())
            .map_err(|e| format!("{e:?}"))?;
        let source: iroh::EndpointId = command["source_id"].as_str().unwrap().parse().unwrap();
        let socket: std::net::SocketAddr =
            serde_json::from_value(command["source_socket"].clone()).map_err(|e| e.to_string())?;
        let connection = host
            .connect_channel(
                &iroh::EndpointAddr::new(source).with_ip_addr(socket),
                Channel::ArtifactChunk,
                &clock,
                deadline(),
            )
            .map_err(|e| format!("{e:?}"))?;
        let mut link = ChunkSession::new(
            connection,
            &rt,
            &clock,
            self.secret.public(),
            serve_limits().unwrap().wire,
        )
        .map_err(|e| e.0)?;
        let size = offer["message"]["transfer"]["size_bytes"].as_u64().unwrap();
        let mut offset = 0;
        while offset < size {
            let length = protocol::artifact_grant::MAX_CHUNK_BYTES.min(size - offset);
            let request = ChunkRequest {
                request_id: format!("chunk-{offset}"),
                source_endpoint_id: source.to_string(),
                object_digest: offer["message"]["transfer"]["content_digest"]
                    .as_str()
                    .unwrap()
                    .into(),
                offset_bytes: offset,
                length_bytes: length,
            };
            shard
                .fetch_chunk_checked(
                    &mut link,
                    &request,
                    &self.session,
                    &self.signer,
                    &carrier,
                    &app_host::now,
                    deadline(),
                    &|| Ok(()),
                )
                .map_err(|e| e.0.to_owned())?;
            offset += length;
        }
        link.finish();
        shard
            .verify(&self.session, &self.signer, &carrier, &app_host::now)
            .map_err(|e| e.0.to_owned())?;
        self.seen.lock().unwrap().received = Some(fs::read(shard.path()).unwrap());
        host.close(&clock, deadline())
            .map_err(|e| format!("{e:?}"))?;
        drop(host);
        Ok(json!({"received_bytes":size,"route_ready":false}))
    }
}
impl MemberLink for TestMember {
    fn begin(&mut self, command: Value) -> Result<(), Refusal> {
        self.seen
            .lock()
            .unwrap()
            .commands
            .push(command["command"].as_str().unwrap_or_default().to_owned());
        self.reply = Some(self.answer(&command));
        Ok(())
    }
    fn finish(&mut self, _: Duration) -> Result<Value, Refusal> {
        self.reply
            .take()
            .unwrap_or_else(|| Err(Refusal("test_member_no_command".into())))
    }
}

/// Model-free codec: the epoch is never opened by these tests.
struct NoCodec;
impl Codec for NoCodec {
    fn encode(&self, _: &str) -> Result<Vec<u32>, PublicError> {
        Err(PublicError::new(409, "test_codec_unused"))
    }
    fn decode(&self, _: u32) -> Result<String, PublicError> {
        Err(PublicError::new(409, "test_codec_unused"))
    }
}

struct Joined {
    node_id: String,
    member: TestMember,
    registration: Value,
    expected: Vec<u8>,
    seen: Arc<Mutex<Seen>>,
}
/// A real owner seed and `n` members paired, joined, renewed, consented and bound.
struct Swarm {
    root: PathBuf,
    owner: PathBuf,
    seed: app_host::SeedRuntime<app_host::Unmanaged>,
    members: Vec<Joined>,
    document: Value,
    objects: PathBuf,
}
impl Drop for Swarm {
    fn drop(&mut self) {
        self.seed.close();
        if let Err(e) = fs::remove_dir_all(&self.root) {
            eprintln!("swarm root cleanup: {e}");
        }
    }
}
fn swarm(n: usize) -> Swarm {
    let root = std::env::temp_dir().join(format!(
        "s4a-work-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ));
    fs::create_dir(&root).unwrap();
    let root = fs::canonicalize(root).unwrap();
    let owner = root.join("owner");
    fs::create_dir(&owner).unwrap();
    let seed = app_host::start_seed::<app_host::Unmanaged>(&owner, "s4a-swarm", None).unwrap();
    let (seed_signer, _, _) = app_host::identity_in(&owner, app_host::SEED_IDENTITY_FILE).unwrap();
    let fixture: Value = serde_json::from_str(include_str!(
        "../../planner/tests/fixtures/source-slices.json"
    ))
    .unwrap();
    let case = &fixture["cases"][0];
    let objects = root.join("objects");
    fs::create_dir(&objects).unwrap();
    let policy = json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
    resource_policy::write(&owner, &policy).unwrap();
    let mut members = Vec::new();
    let (mut files, mut plans) = (serde_json::Map::new(), serde_json::Map::new());
    for i in 0..n {
        let member_root = root.join(format!("member{i}"));
        fs::create_dir(&member_root).unwrap();
        let request = app_host::build_pair_request(&member_root, &member_root).unwrap();
        let (signer, _, _) = app_host::identity(&member_root).unwrap();
        let issuer = handoff::Coordinator {
            signer: Identity::from_secret(seed_signer.endpoint_id(), &seed_signer.secret_bytes())
                .unwrap(),
            swarm_id: "s4a-swarm".into(),
            seed_url: seed.seed_url.clone(),
        };
        let ttl = seconds(B::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37) as u64;
        let approved = handoff::approve_pairing(&owner, true, Some(&issuer), &request.to_value(), &json!(ttl),
            &json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false}),
            app_host::now(), &handoff::Entropy::os).unwrap();
        let options = seed.options();
        let random = |n| app_host::hex(&app_host::random(n).unwrap());
        let env = peer::JoinEnv {
            signer: &signer,
            clock: &app_host::now,
            random_hex: &random,
            seed: &options,
        };
        let mut accepted =
            peer::accept_handoff(&member_root, true, &approved.to_value(), &env).unwrap();
        peer::renew(&member_root, true, accepted.session.as_mut(), &env).unwrap();
        let session = accepted.session.unwrap();
        resource_policy::write(&member_root, &policy).unwrap();
        standing_consent::grant(&member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1000}}), &policy, app_host::now(), Some(16_000_000_000)).unwrap();
        let binding = session.binding().unwrap();
        standing_consent::bind(&member_root, &json!({"swarm_id":binding.swarm_id,"node_id":binding.node_id,"seed_key_digest":binding.seed_key_digest}), app_host::now()).unwrap();
        let seen = Arc::new(Mutex::new(Seen::default()));
        let member = TestMember {
            root: member_root,
            signer,
            secret: iroh::SecretKey::generate(),
            session: session.clone(),
            seen: seen.clone(),
            reply: None,
        };
        let registration =
            joined_shard::register(&session, &member.signer, &member.carrier(), app_host::now())
                .unwrap();
        // The compiler's rows for fixture node i, bound to this member.
        let node_id = session.node_id().to_owned();
        let fixture_node = format!("node-{i}");
        let mut recipe = case["node_plans"][&fixture_node].clone();
        recipe["node_id"] = json!(node_id);
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
        let hex = file["content_digest"]
            .as_str()
            .unwrap()
            .strip_prefix("sha256:")
            .unwrap();
        fs::write(objects.join(hex), &bytes).unwrap();
        files.insert(node_id.clone(), json!([file]));
        plans.insert(node_id.clone(), recipe);
        members.push(Joined {
            node_id,
            member,
            registration,
            expected: bytes,
            seen,
        });
    }
    Swarm {
        root,
        owner,
        seed,
        members,
        document: json!({"node_files":files,"node_slice_plans":plans}),
        objects,
    }
}
impl Swarm {
    fn plan(&self, wall: u64) -> Plan {
        let ids: Vec<&String> = self.members.iter().map(|m| &m.node_id).collect();
        Plan {
            run_id: "s4a-run".into(),
            deployment_id: "12345678-1234-5678-9234-abcdefabcdef".into(),
            model: json!({"model_id":"Qwen/Qwen3-0.6B","resolved_commit":"c1899de289a04d12100db370d81485cdf75e47ca","manifest_digest":"sha256:00"}),
            document: self.document.clone(),
            objects: self.objects.clone(),
            journal: self.root.join("journal"),
            ranges: ids
                .iter()
                .enumerate()
                .map(|(i, id)| ((*id).clone(), [14 * i as u64, 14 * (i as u64 + 1)]))
                .collect(),
            tokens: 32,
            forbidden_source: self.root.join("snapshot"),
            worker_walls: ids.iter().map(|id| ((*id).clone(), wall)).collect(),
            owner_root: self.owner.clone(),
        }
    }
    /// Hand the members to an owner work port (in stage order).
    fn work(&mut self, clocks: Clocks) -> (OwnerWork, Vec<(Arc<Mutex<Seen>>, Vec<u8>)>) {
        let plan = self.plan(600);
        let mut taken = Vec::new();
        let mut members = Vec::new();
        for j in self.members.drain(..) {
            taken.push((j.seen.clone(), j.expected.clone()));
            members.push(WorkMember {
                node_id: j.node_id,
                link: Box::new(j.member),
                registration: j.registration,
            });
        }
        let work = OwnerWork::new(
            self.seed.coordinator.clone(),
            members,
            plan,
            None,
            Arc::new(NoCodec),
            clocks,
        )
        .unwrap();
        (work, taken)
    }
}
fn host_clocks() -> Clocks {
    Clocks {
        mono: Arc::new(|| 0.0),
        wall: Arc::new(app_host::now),
    }
}
fn settled(state: &Value) -> bool {
    matches!(
        state["state"].as_str(),
        Some("awaiting_activation" | "failed" | "stopped")
    )
}

/// G4a: start_serving runs consent -> transfer for every participant (real carrier, the member
/// verifies the exact compiled bytes), then native prepare on every stage; the test member has
/// no native worker, so the workflow fails with ITS code (v0.1.4 workflow.py:337-343: a closed
/// collaborator code becomes the workflow's error_code).
#[test]
fn the_work_port_transfers_every_stage_then_asks_each_member_for_its_native_prepare() {
    let mut s = swarm(2);
    let (mut work, members) = s.work(host_clocks());
    let started = work.start_serving(&json!({"max_wall_seconds":600}));
    assert_eq!(
        started.map(|v| v["registry_origin"].clone()),
        Ok(Value::Null)
    );
    let state = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(state["state"], "failed", "{state}");
    assert_eq!(state["error_code"], NO_NATIVE, "{state}");
    for (seen, expected) in &members {
        let seen = seen.lock().unwrap();
        assert_eq!(
            seen.commands,
            ["consent_status", "receive", "prepare_native"],
            "consent, transfer, then native prepare"
        );
        assert_eq!(seen.received.as_deref(), Some(expected.as_slice()));
    }
    assert_eq!(
        state["transferred"]
            .as_array()
            .map(|rows| rows.iter().map(|r| r["received_bytes"].clone()).collect()),
        Some(
            members
                .iter()
                .map(|(_, e)| json!(e.len()))
                .collect::<Vec<_>>()
        ),
        "{state}"
    );
    work.teardown().unwrap();
}

/// The owner only REQUESTS consent: a member whose standing consent was withdrawn is refused
/// `member_consent_required` before any offer reaches it; the run fails with that code.
#[test]
fn a_member_without_its_own_bound_consent_is_refused_before_any_offer() {
    let mut s = swarm(2);
    standing_consent::withdraw(&s.members[1].member.root, app_host::now()).unwrap();
    let (mut work, members) = s.work(host_clocks());
    let started = work.start_serving(&json!({"max_wall_seconds":600}));
    assert!(started.is_ok(), "{started:?}");
    let state = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(state["state"], "failed", "{state}");
    assert_eq!(state["error_code"], "member_consent_required", "{state}");
    let first = members[0].0.lock().unwrap();
    assert_eq!(first.commands, ["consent_status", "receive"]);
    assert_eq!(first.received.as_deref(), Some(members[0].1.as_slice()));
    let second = members[1].0.lock().unwrap();
    assert_eq!(second.commands, ["consent_status"], "never offered");
    assert!(second.received.is_none());
    drop((first, second));
    work.teardown().unwrap();
}

fn walled(walls: &[(&str, u64)], started: Option<f64>) -> RunState {
    let mut state = RunState::new("s4a-run", "s4a-deployment", Reason::Start);
    let min = walls.iter().map(|(_, w)| *w).min().unwrap();
    state.work_limits = Some(json!({"max_wall_seconds":min}));
    state.work_nodes = Some(Value::Object(
        walls
            .iter()
            .map(|(n, w)| {
                (
                    (*n).to_owned(),
                    json!({"work_limits":{"max_wall_seconds":w}}),
                )
            })
            .collect(),
    ));
    state.execution_clock_started_at = started;
    state
}

/// J12: the run's clock starts at publish (`execution_clock_started_at`) and the members' REAL
/// worker wall bounds it: a 600 s wall rolls over at 480 s (lead = min(max(30, 120), 300)) and
/// hits the ceiling at 570 s (`execution_ceiling_reached`); v0.1.4 test_consented_wall_limits.py:76
/// pins 600/480/570. The smallest member wall wins; no clock or no wall is never due.
#[test]
fn rollover_is_due_at_480_s_and_the_ceiling_at_570_s_of_a_600_s_worker_wall() {
    let t0 = 1000.0;
    let state = walled(&[("node-a", 600), ("node-b", 900)], Some(t0));
    assert_eq!(work::rollover_due(&state, t0 + 479.999), Some(false));
    assert_eq!(work::rollover_due(&state, t0 + 480.0), Some(true));
    assert_eq!(
        work::ceiling_reached(&state, t0 + 569.999),
        CeilingProbe::Code(None)
    );
    assert_eq!(
        work::ceiling_reached(&state, t0 + 570.0),
        CeilingProbe::Code(Some("execution_ceiling_reached".into()))
    );
    let short = walled(&[("node-a", 120), ("node-b", 900)], Some(t0));
    assert_eq!(
        work::rollover_due(&short, t0 + 59.999),
        Some(false),
        "lead = wall/2"
    );
    assert_eq!(work::rollover_due(&short, t0 + 60.0), Some(true));
    assert_eq!(
        work::ceiling_reached(&short, t0 + 90.0),
        CeilingProbe::Code(Some("execution_ceiling_reached".into()))
    );
    let unstarted = walled(&[("node-a", 600)], None);
    assert_eq!(work::rollover_due(&unstarted, t0 + 1e6), Some(false));
    assert_eq!(
        work::ceiling_reached(&unstarted, t0 + 1e6),
        CeilingProbe::Code(None)
    );
}

/// A serving owner run (sans-IO machine, every step done).
fn serving_run() -> OwnerRun {
    let mut run = OwnerRun::new(Box::new(|| RunIdentity {
        run_id: "s4a-run".into(),
        deployment_id: "s4a-deployment".into(),
    }));
    let clock = Clock {
        mono: 0.0,
        wall: 0.0,
    };
    run.start("start", None, clock).unwrap();
    for _ in owner_run::machine::STEPS {
        run.step_finished(StepOutcome::Ok);
    }
    assert!(run.serving());
    run
}

/// The port's own probes on the injected run clock drive the machine: at 480 s the serving run
/// switches (rollover), and with the rollover not yet due the ceiling ends it
/// `execution_ceiling_reached`.
#[test]
fn the_ports_probes_switch_the_run_at_480_s_and_end_it_at_the_570_s_ceiling() {
    let mut s = swarm(2);
    let now = Arc::new(Mutex::new(0.0f64));
    let read = now.clone();
    let (mut work, _) = s.work(Clocks {
        mono: Arc::new(move || *read.lock().unwrap()),
        wall: Arc::new(app_host::now),
    });
    let state = walled(&[("node-a", 600), ("node-b", 600)], Some(50.0));
    *now.lock().unwrap() = 50.0 + 480.0;
    let mut run = serving_run();
    run.serving_poll(work.rollover_due(&state), work.ceiling_reached(&state));
    assert_eq!(run.snapshot().rollover.state, "switching");
    *now.lock().unwrap() = 50.0 + 570.0;
    let mut run = serving_run();
    run.serving_poll(Some(false), work.ceiling_reached(&state));
    assert_eq!(
        run.snapshot().error_code.as_deref(),
        Some("execution_ceiling_reached")
    );
    work.teardown().unwrap();
}

/// J13: a participant whose seed lease ran out (lease + the members' clock skew, the member's own
/// fence point) is gone for the owner: the monitor answers `joined_route_member_stale`
/// (v0.1.4 `managed_control_member_stale`) and the serving run fails with it as
/// `owner_run.error_code`. A renewed member is not stale. Seed clock injected.
#[test]
fn a_member_whose_seed_lease_ran_out_fails_the_serving_run_joined_route_member_stale() {
    let mut s = swarm(2);
    let coordinator = s.seed.coordinator.clone();
    let seed_now = Arc::new(Mutex::new(None::<f64>));
    let at = seed_now.clone();
    let view: api::m16_route::SeedMembers = Arc::new(move |node: &str| {
        let row = coordinator.member(node).ok()?;
        let lease = row["lease_expires_at"].as_f64()?;
        Some((row, at.lock().unwrap().unwrap_or(lease - 1.0)))
    });
    let stale_node = s.members[1].node_id.clone();
    let lease = s.seed.coordinator.member(&stale_node).unwrap()["lease_expires_at"]
        .as_f64()
        .unwrap();
    let (work, _) = s.work(host_clocks());
    let mut work = work.with_seed_view(view);
    let state = walled(&[("node-a", 600)], None);
    assert_eq!(work.monitor(&state), MonitorProbe::Code(None), "renewed");
    *seed_now.lock().unwrap() =
        Some(lease + seconds(B::PyMyceliumMembershipContractsMaxClockSkewSecondsL31));
    let probe = work.monitor(&state);
    assert_eq!(
        probe,
        MonitorProbe::Code(Some("joined_route_member_stale".into()))
    );
    let mut run = serving_run();
    run.monitor_result(probe);
    let status = run.snapshot();
    assert_eq!(
        status.error_code.as_deref(),
        Some("joined_route_member_stale")
    );
    assert_eq!(status.failed_step, Some("serving"));
    work.teardown().unwrap();
}
