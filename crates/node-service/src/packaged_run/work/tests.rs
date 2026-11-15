// SPDX-License-Identifier: AGPL-3.0-only
//! Two packaged host roles on loopback, and the same pair on this Mac's real LAN interface (lane
//! LAN). Real enrollment, seed TLS, member control, carrier, inert shard receipts and cleanup.
//! Constructor-only source plan and refusing native admission.
use super::*;
mod chat_route;
mod flake;
mod regressions;
mod serving_q;
mod serving_r2;
mod serving_r3;
mod serving_r3b;
use crate::{
    packaged_network::{JoinSeams, MemberSeams, NetworkConfig, PackagedMember},
    packaged_work::{NetworkChoice, WorkNetwork},
};
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
/// A run step environment on a simulated clock: a wait advances it instead of sleeping.
struct SimEnv {
    now: f64,
}
impl StepEnv for SimEnv {
    fn progress(&mut self, _: &str) {}
    fn stopped(&self) -> bool {
        false
    }
    fn now(&mut self) -> f64 {
        self.now
    }
    fn wall(&mut self) -> f64 {
        app_host::now()
    }
    fn set_result(&mut self, _: &str, _: Value) {}
    fn record_exception(&mut self, _: &str, _: &str) {
        panic!("unexpected exception")
    }
    fn pause(&mut self, seconds: f64) -> bool {
        self.now += seconds;
        false
    }
}

struct Fixture {
    root: PathBuf,
    host: PackagedRun,
    network: Arc<Network>,
    member: Arc<PackagedMember>,
    /// The joined Mac's authority (lane FLK: its renewal flight is reproduced on it; `network`
    /// above is the joined Mac's network).
    member_authority: Arc<Mutex<Authority>>,
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
    /// Both hosts on loopback, the owner's own contributor exactly as the product's contributor
    /// step leaves it. Every fixture does so now: a resource policy and a standing grant written
    /// by hand into its root hid two defects of that step (lane TEN rows 6), and a hand-written
    /// grant with other caps than the product's is refused `standing_consent_changed`.
    fn product_contributor() -> Self {
        Self::new()
    }
    /// Both hosts on loopback (`NetworkMode::LocalOnly`), whatever this Mac's interfaces are.
    fn new() -> Self {
        Self::on(WorkNetwork::loopback())
    }
    /// Both hosts' work transports over `work` (the seed stays on its loopback TLS address).
    fn on(work: WorkNetwork) -> Self {
        Self::on_each(work.clone(), work)
    }
    /// The owner's work transport over `owner`, the joined Mac's over `member`.
    fn on_each(owner_work: WorkNetwork, member_work: WorkNetwork) -> Self {
        let work = member_work;
        let root = std::env::temp_dir().join(format!(
            "s4cb2-owner-{}",
            crate::service::random_id().unwrap()
        ));
        fs::create_dir(&root).unwrap();
        let root = root.canonicalize().unwrap();
        let owner = root.join("owner");
        let authority = Arc::new(Mutex::new(Authority::load(owner.clone()).unwrap()));
        let network = Network::new(
            &owner,
            NetworkConfig {
                work_network: owner_work,
                ..NetworkConfig::default()
            },
        );
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
                work_network: work,
                ..Default::default()
            },
        );
        let member_authority = Arc::new(Mutex::new(authority));
        let member = PackagedMember::new(
            member_authority.clone(),
            network.clone(),
            MemberSeams {
                closed: Box::new(|| false),
                resource: Box::new(|_| None),
                acquisition: Box::new(|| None),
                start_whole_model: Box::new(
                    |_| Err("member_model_download_not_ported".to_string()),
                ),
            },
        );
        Self {
            root,
            host,
            network,
            member,
            member_authority,
            agent: MemberAgent::default(),
            remote,
        }
    }
    fn plan(&self, state: &RunState) -> (Plan, Arc<runtime::artifact_journal::PinnedObjects>) {
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
        let objects = objects::view(&self.root, &json!({"node_files":files})).unwrap();
        (
            Plan {
                run_id: state.run_id.clone(),
                deployment_id: state.deployment_id.clone(),
                model: json!({"model_id":"Qwen/Qwen3-0.6B","resolved_commit":"c1899de289a04d12100db370d81485cdf75e47ca","manifest_digest":"sha256:00"}),
                document: json!({"node_files":files,"node_slice_plans":plans}),
                objects: objects.path,
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
                challenge: None,
            },
            objects.pinned,
        )
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
    handoff_and_push_before_publish(Fixture::new(), &|address| {
        assert!(address.ip().is_loopback(), "{address}")
    });
}
/// Lane TEN (the hermetic ten-row gate, real model): row 6 ended `compiler_storage_consent_required`.
/// The owner's own contributor receives its stage shard into its own root
/// (`JoinedShard::begin` -> `MemberShard::begin` -> `reserve_output`, compiler_inputs.rs:532-560), which
/// bounds the reservation by that root's resource policy. The fixture above writes one there by hand;
/// the product's contributor step (`owner_ports::start_local_contributor`) writes only the standing
/// grant, so a real run refused the owner's own stage before any byte arrived.
#[test]
fn the_owners_own_contributor_receives_its_shard_in_the_state_the_contributor_step_leaves() {
    handoff_and_push_before_publish(Fixture::product_contributor(), &|address| {
        assert!(address.ip().is_loopback(), "{address}")
    });
}
/// Lane TEN (ten-row gate, real model, runs `gate3-noenv` and `gate4-noenv`): row 6 ended
/// `member_wall_cap_below_share`. The compile gives the owner's own worker the proposal's wall
/// (`planner::chain_compile::admit`: no caps for the local node -> `proposed`, 1800 s,
/// `PROPOSED_WALL_SECONDS`; v0.1.4's owner worker holds no grant at all), and the contributor's own
/// standing check refuses any wall above its grant (`local_agent.rs:420-424`,
/// `standing_acceptor::standing_limits`). The contributor step granted 600 s
/// (`MemberChosenWall`, a v0.1.4 TEST fixture's member choice). The shares below are the real
/// run's (gate3: layers 19-28 of Qwen3-0.6B).
#[test]
fn the_owners_own_contributor_grant_admits_the_wall_the_compile_gives_the_local_node() {
    let f = Fixture::product_contributor();
    let dir = owner_ports::contributor_dir(&f.root.join("owner"));
    let grant = standing_consent::load(&dir)
        .unwrap()
        .expect("the contributor step's grant");
    let share = json!({"max_wall_seconds": 1800, "max_rss_bytes": 2_625_486_324u64,
        "max_disk_bytes": 1_074_471_271u64});
    assert_eq!(
        standing_consent::standing_limits(&grant, Some(&share))
            .map(|l| l["max_wall_seconds"].clone()),
        Ok(json!(1800)),
        "{grant:?}"
    );
}
/// This Mac's private LAN address, as the `lan-real` gate step exports it (`ipconfig getifaddr
/// en0`). Absent or not private: the test FAILS, it never skips.
fn lan_ip() -> std::net::IpAddr {
    let value = std::env::var("MYCELIUM_LAN_IP").expect("MYCELIUM_LAN_IP = this host's LAN IP");
    let address: std::net::IpAddr = value.parse().expect("MYCELIUM_LAN_IP is an IP address");
    assert!(
        runtime::iroh_host::private_lan(address),
        "{address} is not a private LAN address"
    );
    address
}
/// The loopback pair above, with both hosts' work transports on this Mac's real LAN interface
/// under the packaged policy (`LanPolicy::private_only`: exact pins, no relay, no lookup). Every
/// work address the owner admits (from member-signed registrations whose key the seed currently
/// enrolls) is on that interface.
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn two_packaged_hosts_on_the_real_lan_interface_handoff_and_push_before_publish() {
    let ip = lan_ip();
    let work = WorkNetwork::Fixed(NetworkChoice(Some((
        ip,
        runtime::iroh_host::LanPolicy::private_only(),
    ))));
    handoff_and_push_before_publish(Fixture::on(work), &|address| {
        assert_eq!(address.ip(), ip, "{address}")
    });
}
/// The production default on this Mac: [`NetworkConfig::default`]'s work transport, resolved
/// through the real host constructor as a run starts, is the address of this Mac's default-route
/// interface (whatever its name: `route -n get default`, then `ipconfig getifaddr`), not loopback.
/// Proves: F8 (c, d) (runs in the `lan-real` gate step; FAILS without MYCELIUM_LAN_IP; no en0).
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn the_app_default_work_transport_is_this_macs_lan_address() {
    lan_ip();
    let ip = default_route_address().expect("this Mac has a private default route");
    let root = std::env::temp_dir().join(format!(
        "lan-default-{}",
        crate::service::random_id().unwrap()
    ));
    fs::create_dir(&root).unwrap();
    let network = Network::new(&root, NetworkConfig::default());
    let choice = network.resolve_work_network("test");
    fs::remove_dir_all(&root).unwrap();
    assert_eq!(
        choice,
        NetworkChoice(Some((ip, runtime::iroh_host::LanPolicy::private_only())))
    );
    assert!(!choice.allows(std::net::SocketAddr::from(([127, 0, 0, 1], 9))));
}
/// The IPv4 address of the interface `route -n get default` names, from `ipconfig`: this Mac's
/// answer from tools that share no code with `api::lan_interface`.
fn default_route_address() -> Option<std::net::IpAddr> {
    let route = std::process::Command::new("/sbin/route")
        .args(["-n", "get", "default"])
        .output()
        .unwrap();
    let text = String::from_utf8(route.stdout).unwrap();
    let interface = text
        .lines()
        .find_map(|l| l.trim().strip_prefix("interface: "))?
        .to_owned();
    let address = std::process::Command::new("/usr/sbin/ipconfig")
        .args(["getifaddr", &interface])
        .output()
        .unwrap();
    String::from_utf8(address.stdout)
        .unwrap()
        .trim()
        .parse()
        .ok()
}
/// A run's state with this fixture's local contributor and joined Mac, the owner announced.
fn run_state(f: &mut Fixture, ports: &PackagedChainPorts) -> RunState {
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
    state
}
/// Proves: F6 and LAN-A2 c (an owner whose own reading of this Mac found no single private network
/// refuses a run with a joined Mac by name, with the reason and the side, before it starts its
/// local worker or dials anyone).
#[test]
fn an_owner_without_a_single_private_network_refuses_a_joined_mac_by_name() {
    use api::lan_interface::{AMBIGUOUS_PRIVATE_INTERFACES, Selection};
    let owner = WorkNetwork::Read(
        Arc::new(|| Selection::LocalOnly(AMBIGUOUS_PRIVATE_INTERFACES)),
        runtime::iroh_host::LanPolicy::private_only(),
    );
    let mut f = Fixture::on_each(owner, WorkNetwork::loopback());
    let ports = PackagedChainPorts::new(&f.host, None, None);
    let state = run_state(&mut f, &ports);
    let mut env = Env { at: Instant::now() };
    let refused = ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap_err();
    let StepError::Refused(error) = refused else {
        panic!("{refused:?}")
    };
    assert_eq!(error.code, "owner_work_network_unavailable");
    let message = error.message.unwrap_or_default();
    assert!(
        message.contains(AMBIGUOUS_PRIVATE_INTERFACES) && message.contains("This Mac"),
        "{message}"
    );
    let resident = lock(&ports.work.resident);
    assert!(resident.local.is_none(), "no local worker was started");
    assert!(resident.bindings.is_empty() && resident.work.is_none());
}
/// Proves: F6 (owner on this Mac's real LAN, joined Mac on its loopback: refused by name, naming
/// the joined Mac's side and both addresses, before any dial; the real-LAN gate step runs it).
#[test]
#[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
fn on_the_real_lan_an_owner_refuses_a_joined_mac_on_its_loopback_by_name() {
    let ip = lan_ip();
    let owner = WorkNetwork::Fixed(NetworkChoice(Some((
        ip,
        runtime::iroh_host::LanPolicy::private_only(),
    ))));
    let mut f = Fixture::on_each(owner, WorkNetwork::loopback());
    let ports = PackagedChainPorts::new(&f.host, None, None);
    let state = run_state(&mut f, &ports);
    let mut env = Env { at: Instant::now() };
    let begun = Instant::now();
    let refused = ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap_err();
    let StepError::Refused(error) = refused else {
        panic!("{refused:?}")
    };
    assert_eq!(error.code, "member_work_network_mismatch");
    let message = error.message.unwrap_or_default();
    assert!(
        message.contains("joined Mac") && message.contains("127.0.0.1"),
        "{message}"
    );
    assert!(message.contains(&ip.to_string()), "{message}");
    eprintln!("refused in {:?}: {message}", begun.elapsed());
    assert!(lock(&ports.work.resident).work.is_none(), "nothing dialed");
}
/// Proves: F2 (one transport per owner run: the owner reads this Mac's interfaces once, when the run
/// composes, and its local worker, member dials, shard source, route and shares check all keep that
/// answer, even though every later reading would answer differently. Mutation M3 re-reads it.)
#[test]
fn one_owner_run_reads_this_macs_interfaces_once() {
    use api::lan_interface::Selection;
    let loopback = std::net::IpAddr::from([127, 0, 0, 1]);
    let policy = runtime::iroh_host::LanPolicy::with_explicit(vec![loopback]).unwrap();
    let reads = Arc::new(AtomicUsize::new(0));
    let counted = reads.clone();
    let owner = WorkNetwork::Read(
        Arc::new(move || match counted.fetch_add(1, Ordering::SeqCst) {
            0 => Selection::Lan(loopback),
            _ => Selection::LocalOnly("interfaces_changed_mid_run"),
        }),
        policy.clone(),
    );
    let member = WorkNetwork::Fixed(NetworkChoice(Some((loopback, policy))));
    handoff_and_push_before_publish(Fixture::on_each(owner, member), &|address| {
        assert_eq!(address.ip(), loopback, "{address}")
    });
    assert_eq!(
        reads.load(Ordering::SeqCst),
        1,
        "the owner run read the interfaces again after it composed"
    );
}
fn handoff_and_push_before_publish(mut f: Fixture, on: &dyn Fn(std::net::SocketAddr)) {
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
    {
        let resident = lock(&ports.work.resident);
        assert_eq!(resident.bindings.len(), 2);
        for binding in &resident.bindings {
            assert!(binding.address().ip_addrs().count() > 0);
            binding.address().ip_addrs().copied().for_each(on);
        }
    }
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
        forced_slice: false,
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
/// Proves: B-RUN-10 (the packaged app's ten-step table runs step 10 through its own work ports,
/// never `owner_run_step_not_ported`; with no workflow it does what v0.1.4 does,
/// `state = {} if workflow is None else workflow.status()` (demo_chain.py:1016-1022): an unbound
/// run keeps waiting and ends `route_qualification_timeout`; a run whose compile bound an epoch
/// refuses `serving_workflow_identity_changed` (`needs_identity(workflow) and not
/// current(workflow)`, :1020-1021). REVIEW-laneSRV ASRV-04 / SRV-3: round 1 expected
/// `owner_work_unavailable`, a code the source never produces here.) Model-free; no route opened.
#[test]
fn the_packaged_chain_runs_the_serving_step_instead_of_refusing_it_as_unported() {
    let f = Fixture::new();
    let (last, _) = owner_run::machine::STEPS[owner_run::machine::STEPS.len() - 1];
    assert_eq!(last, "serving");
    // Through the chain the Runner drives: a fresh run binds nothing.
    let mut chain = Composed::new(&f.host, None, None);
    chain.new_run("run-ten", "dep-ten", owner_run::Reason::Start, None, None);
    let budget = span(BudgetId::Serving).unwrap().as_secs_f64();
    let mut env = SimEnv { now: 0.0 };
    match chain.run_step(last, &mut env) {
        Err(StepError::Refused(e)) => {
            assert_ne!(e.code, owner_run::steps::NOT_PORTED_CODE, "{e:?}");
            assert_eq!(e.code, "route_qualification_timeout", "{e:?}");
        }
        other => panic!("an unbound run with no workflow waits: {other:?}"),
    }
    assert!(env.now >= budget, "the wait ran its budget: {}", env.now);
    // A run whose compile bound an epoch.
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let mut bound = RunState::new("run-bound", "dep-bound", owner_run::Reason::Start);
    bound.compile_state = Some(json!({"epoch": 7}));
    let publication = ports.publication();
    let mut env = SimEnv { now: 0.0 };
    let refused = owner_run::steps::serving::run_serving_step(
        &mut env,
        publication.reports,
        publication.workflow,
        &mut bound,
        span(BudgetId::OwnerPoll).unwrap().as_secs_f64(),
    );
    assert!(
        matches!(&refused, Err(StepError::Refused(e)) if e.code == "serving_workflow_identity_changed"),
        "{refused:?}"
    );
    assert_eq!(ports.work.workflow_state(), Ok(Value::Null), "absent: null");
}
/// Proves: B-RUN-10 (the packaged chain asks its resident owner workflow the serving run's
/// `rollover_due`, `ceiling_reached` and `monitor`, demo_chain.py:1132-1186, instead of the trait
/// defaults that never roll over, never reach a ceiling and never fail a serving run).
#[test]
fn the_packaged_chain_forwards_rollover_ceiling_and_monitor_to_its_owner_workflow() {
    use owner_run::{
        machine::{CeilingProbe, EXECUTION_CEILING_CODE, MonitorProbe},
        owner_chain::ChainPorts as _,
    };
    let mut f = Fixture::new();
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    seed.exchange
        .set_owner_control(Some(OwnerControl::new(ports.work.owner.public())));
    seed.exchange
        .announce(Announce {
            phase: "compile".into(),
            run_id: Some("run-probe".into()),
            deployment_id: Some("dep-probe".into()),
            ..Default::default()
        })
        .unwrap();
    f.agent.tick(&f.member);
    let local = f
        .host
        .with(|c| c.run.contributor.as_ref().unwrap().node.clone());
    let mut state = RunState::new("run-probe", "dep-probe", owner_run::Reason::Start);
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
    // Long past the worker wall on the Runner's clock: rollover due and the ceiling reached.
    let mut serving = state.clone();
    serving.work_limits = Some(json!({ "max_wall_seconds": wall }));
    serving.execution_clock_started_at = Some(-(wall as f64) * 10.0);
    let mut env = Env { at: Instant::now() };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    let ceiling = Some(EXECUTION_CEILING_CODE.to_owned());
    assert_eq!(ports.rollover_due(&serving), Some(true));
    assert_eq!(
        ports.ceiling_reached(&serving),
        CeilingProbe::Code(ceiling.clone())
    );
    assert_eq!(ports.monitor(&serving), MonitorProbe::Code(ceiling));
    assert_eq!(
        ports.rollover_due(&state),
        Some(false),
        "no publish clock yet"
    );
    assert_eq!(ports.ceiling_reached(&state), CeilingProbe::Code(None));
    assert_eq!(ports.monitor(&state), MonitorProbe::Code(None));
    ports.close_work().unwrap();
}
/// Proves: R2-4 (the named network refusals refuse only a run with a joined Mac: an owner-only
/// run, whose one participant is this Mac's own local worker, composes and starts that worker
/// under a reading that found no single private network).
#[test]
fn an_owner_only_run_without_a_single_private_network_still_starts_its_local_worker() {
    use api::lan_interface::{AMBIGUOUS_PRIVATE_INTERFACES, Selection};
    let owner = WorkNetwork::Read(
        Arc::new(|| Selection::LocalOnly(AMBIGUOUS_PRIVATE_INTERFACES)),
        runtime::iroh_host::LanPolicy::private_only(),
    );
    let mut f = Fixture::on_each(owner, WorkNetwork::loopback());
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let mut state = run_state(&mut f, &ports);
    let local = state.local_node.clone().unwrap();
    state.participants = vec![local.clone()];
    state.worker_wall_limits.retain(|node, _| *node == local);
    let mut env = Env { at: Instant::now() };
    let composed = ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env);
    {
        let resident = lock(&ports.work.resident);
        assert!(
            composed.is_ok(),
            "an owner-only run was refused: {composed:?}"
        );
        assert!(resident.local.is_some(), "its local worker started");
        assert_eq!(resident.bindings.len(), 1);
        assert!(resident.work.is_some());
        assert_eq!(resident.network, Some(NetworkChoice::default()));
    }
    ports.close_work().unwrap();
}
/// Proves: R2-8 (the compose step maps a refused joined-Mac address to its named refusal in a
/// test every run executes, not only on the real LAN: the owner on a LAN policy that does not
/// admit the joined Mac's loopback address refuses `member_work_network_mismatch`, naming both
/// addresses, before any dial).
#[test]
fn an_owner_on_the_lan_refuses_a_joined_mac_on_its_loopback_by_name() {
    let loopback = std::net::IpAddr::from([127, 0, 0, 1]);
    let other = std::net::IpAddr::from([127, 0, 0, 2]);
    // The owner binds 127.0.0.1 as its LAN address; its peers may only be at 127.0.0.2.
    let policy = runtime::iroh_host::LanPolicy::with_explicit(vec![loopback, other])
        .unwrap()
        .with_peer_hosts(vec![other])
        .unwrap();
    let owner = WorkNetwork::Fixed(NetworkChoice(Some((loopback, policy))));
    let mut f = Fixture::on_each(owner, WorkNetwork::loopback());
    let ports = PackagedChainPorts::new(&f.host, None, None);
    let mut state = run_state(&mut f, &ports);
    // The joined Mac's address is checked first (its own local worker's would be refused too).
    state.participants.reverse();
    let mut env = Env { at: Instant::now() };
    let refused = ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap_err();
    let StepError::Refused(error) = refused else {
        panic!("{refused:?}")
    };
    assert_eq!(error.code, "member_work_network_mismatch");
    let message = error.message.unwrap_or_default();
    assert!(
        message.contains("joined Mac") && message.contains("loopback address (127.0.0.1)"),
        "{message}"
    );
    assert!(message.contains("works on 127.0.0.1"), "{message}");
    assert!(lock(&ports.work.resident).work.is_none(), "nothing dialed");
}
/// Proves: R2-7 (a run's network choice ends with its teardown: after one run closed, the same
/// process reads this Mac's interfaces afresh, whatever asks first, and a second run uses its own
/// reading, not the first run's).
#[test]
fn a_second_owner_run_in_one_process_reads_the_interfaces_again() {
    use api::lan_interface::{AMBIGUOUS_PRIVATE_INTERFACES, Selection};
    let loopback = std::net::IpAddr::from([127, 0, 0, 1]);
    let policy = runtime::iroh_host::LanPolicy::with_explicit(vec![loopback]).unwrap();
    let reads = Arc::new(AtomicUsize::new(0));
    let counted = reads.clone();
    let owner = WorkNetwork::Read(
        Arc::new(move || match counted.fetch_add(1, Ordering::SeqCst) {
            0 => Selection::Lan(loopback),
            _ => Selection::LocalOnly(AMBIGUOUS_PRIVATE_INTERFACES),
        }),
        policy.clone(),
    );
    let member = WorkNetwork::Fixed(NetworkChoice(Some((loopback, policy.clone()))));
    let mut f = Fixture::on_each(owner, member);
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let state = run_state(&mut f, &ports);
    let mut env = Env { at: Instant::now() };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    assert_eq!(
        lock(&ports.work.resident).network,
        Some(NetworkChoice(Some((loopback, policy))))
    );
    ports.close_work().unwrap();
    assert_eq!(reads.load(Ordering::SeqCst), 1);
    // Whatever asks for the transport between runs gets a fresh reading.
    assert_eq!(
        ports.work.run_network(),
        NetworkChoice::default(),
        "the closed run's choice was reused"
    );
    assert_eq!(reads.load(Ordering::SeqCst), 2);
    // The second run reads once more and goes by that reading (no single network here now).
    fs::remove_dir_all(f.root.join("stage-shards")).unwrap();
    let refused = ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap_err();
    let StepError::Refused(error) = refused else {
        panic!("{refused:?}")
    };
    assert_eq!(error.code, "owner_work_network_unavailable");
    assert_eq!(reads.load(Ordering::SeqCst), 3);
}
