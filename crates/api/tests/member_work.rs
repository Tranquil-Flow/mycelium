// SPDX-License-Identifier: AGPL-3.0-only
//! The member work service (`api::joined_member::work`, slice S4b item 3): work is refused without
//! standing consent bound to the member's current membership, and when the share the owner
//! announced for this member (signed `work_limits`) does not fit the member's caps — by name,
//! before any custody or dial (v0.1.4 `standing_limits`, standing_consent.py:230-261, reached from
//! `DemoApp.start` -> `_standing_loop` app.py:642,1201 -> `auto_accept` :1175-1189 ->
//! `StandingAcceptor.tick`). Model-free: real seed, pairing and signed exchange; no source dialled.
use api::{
    app_host, handoff,
    joined_member::work::{self, Carrier, Member, Operation, Origin, Work},
    member::Session,
    peer,
    preferences::{resource_policy, standing_consent},
    run_exchange::{
        Announce, RunExchange,
        owner_pin::{OwnerControl, VerifiedAnnouncement},
    },
    run_exchange_client::{ExchangeClient, Membership},
    run_exchange_routes::{DISCOVERY, PATH_READ, READ, SignedRoutes},
    seed::{SeedClient, SeedOptions},
};
use budgets::{BudgetId as B, Entries};
use protocol::identity::Identity;
use runtime::iroh_host::LoopbackHost;
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
use transport::Clock;
static NEXT: AtomicU64 = AtomicU64::new(0);
/// A paired member of a real seed (as `joined_shard.rs`'s pair), with a policy but no consent.
struct Joined {
    root: PathBuf,
    member_root: PathBuf,
    seed: app_host::SeedRuntime<app_host::Unmanaged>,
    signer: Identity,
    session: Arc<Mutex<Option<Session>>>,
    policy: Value,
}
impl Member for Joined {
    fn session(&self) -> &Arc<Mutex<Option<Session>>> {
        &self.session
    }
    fn current(&self, signer: &Identity) -> Result<Value, api::compiler_inputs::Error> {
        self.session
            .lock()
            .unwrap()
            .as_ref()
            .unwrap()
            .shard_identity(signer, app_host::now())
            .map_err(|_| api::compiler_inputs::Error("joined_shard_lease_expired"))
    }
}
impl Joined {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "s4b-work-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir(&root).unwrap();
        let root = fs::canonicalize(root).unwrap();
        let owner = root.join("owner");
        let member_root = root.join("member");
        fs::create_dir(&owner).unwrap();
        fs::create_dir(&member_root).unwrap();
        let seed = app_host::start_seed::<app_host::Unmanaged>(&owner, "s4b-swarm", None).unwrap();
        let request = app_host::build_pair_request(&member_root, &member_root).unwrap();
        let (signer, _, _) = app_host::identity(&member_root).unwrap();
        let (seed_signer, _, _) =
            app_host::identity_in(&owner, app_host::SEED_IDENTITY_FILE).unwrap();
        let coordinator = handoff::Coordinator {
            signer: seed_signer,
            swarm_id: "s4b-swarm".into(),
            seed_url: seed.seed_url.clone(),
        };
        let ttl = B::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37
            .span(Entries::ZERO)
            .unwrap()
            .millis()
            / 1000;
        let handoff = handoff::approve_pairing(&owner, true, Some(&coordinator), &request.to_value(), &json!(ttl),
            &json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false}), app_host::now(), &handoff::Entropy::os).unwrap();
        let options = seed.options();
        let random = |n| app_host::hex(&app_host::random(n).unwrap());
        let env = peer::JoinEnv {
            signer: &signer,
            clock: &app_host::now,
            random_hex: &random,
            seed: &options,
        };
        let mut accepted =
            peer::accept_handoff(&member_root, true, &handoff.to_value(), &env).unwrap();
        peer::renew(&member_root, true, accepted.session.as_mut(), &env).unwrap();
        let policy = json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
        resource_policy::write(&member_root, &policy).unwrap();
        Self {
            root,
            member_root,
            seed,
            signer,
            session: Arc::new(Mutex::new(accepted.session)),
            policy,
        }
    }
    /// The member's own standing grant (caps: 4 GB storage and memory, 600 s, 1000 commands).
    fn grant(&self) {
        standing_consent::grant(&self.member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1000}}), &self.policy, app_host::now(), Some(16_000_000_000)).unwrap();
    }
    /// [`Joined::grant`] with the operator's own consent wall `seconds`.
    fn grant_wall(&self, seconds: u64) {
        standing_consent::grant(&self.member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":seconds,"max_commands":1000}}), &self.policy, app_host::now(), Some(16_000_000_000)).unwrap();
    }
    fn bind(&self) {
        work::bind_consent(&self.member_root, self).unwrap();
    }
    /// The owner's signed read answer for this member, announcing `work_limits` for it, verified
    /// by the member's own exchange client against the seed key it pinned at join.
    fn announced(&self, work_limits: Option<Value>) -> VerifiedAnnouncement {
        self.announced_for("run-s4b", work_limits)
    }
    /// [`Joined::announced`] for the owner run `run_id`.
    fn announced_for(&self, run_id: &str, work_limits: Option<Value>) -> VerifiedAnnouncement {
        let now = app_host::now();
        let coordinator = &self.seed.coordinator;
        let session = self.session.lock().unwrap().clone().unwrap();
        let owner_key = coordinator.signer().public_key_record();
        let client = ExchangeClient::new(
            SeedClient::new(
                "https://127.0.0.1:9",
                coordinator.swarm_id(),
                owner_key["verification_key_digest"].as_str().unwrap(),
                std::slice::from_ref(&owner_key),
                &SeedOptions {
                    ca_pem: Some(Vec::new()),
                    ..SeedOptions::default()
                },
            )
            .unwrap(),
            Arc::new(
                Identity::from_secret(self.signer.endpoint_id(), &self.signer.secret_bytes())
                    .unwrap(),
            ),
            Membership {
                swarm_id: coordinator.swarm_id().into(),
                owner_node_id: coordinator.seed_node_id().into(),
                owner_key,
                node_id: session.node_id().into(),
                generation: 1,
                incarnation: "s4b-incarnation".into(),
            },
            Box::new(move || now),
            Box::new(|| Some("nonce-s4b".into())),
        );
        let exchange = RunExchange::new(Box::new(move || now));
        let mut node = serde_json::Map::new();
        if let Some(limits) = work_limits {
            node.insert("work_limits".into(), limits);
        }
        exchange
            .announce(Announce {
                phase: "work".into(),
                run_id: Some(run_id.into()),
                deployment_id: Some("dep-s4b".into()),
                per_node: json!({session.node_id(): node})
                    .as_object()
                    .unwrap()
                    .clone(),
                ..Default::default()
            })
            .unwrap();
        exchange.set_owner_control(Some(OwnerControl::new(
            iroh::SecretKey::from_bytes(&[61; 32]).public(),
        )));
        let ttl = B::ControlTtl.span(Entries::ZERO).unwrap().millis() as f64 / 1000.0;
        let mut statement = Value::Object(client.binding(DISCOVERY, DISCOVERY));
        for (k, v) in json!({"protocol": READ, "swarm_id": coordinator.swarm_id(),
            "request_id": "request-s4b", "sequence": 1, "nonce": "nonce-s4b", "issued_at": now,
            "expires_at": now + ttl})
        .as_object()
        .unwrap()
        {
            statement[k] = v.clone();
        }
        let m = client.member();
        let row = json!({"node_id":m.node_id,"endpoint_id":client.signer().endpoint_id(),
            "verification_key_digest":client.signer().verification_key_digest(),
            "generation":m.generation,"incarnation":m.incarnation,"peer_class":"mac_mlx_iroh",
            "lifecycle_state":"RUNNING","lease_expires_at":now + 300.0});
        let observe = |_: &str, _: f64, _: Option<f64>| {};
        let routes = SignedRoutes {
            exchange: &exchange,
            signer: coordinator.signer(),
            swarm_id: coordinator.swarm_id(),
            owner_node_id: coordinator.seed_node_id(),
            observe_clock: &observe,
            response_ttl: budgets::Span::from_millis(60_000),
        };
        let response = routes
            .dispatch_guarded(PATH_READ, &client.sign(&statement).unwrap(), &row, now)
            .unwrap();
        VerifiedAnnouncement::from_response(&client, &response, "request-s4b").unwrap()
    }
}
impl Drop for Joined {
    fn drop(&mut self) {
        self.seed.close();
        fs::remove_dir_all(&self.root).unwrap();
    }
}
/// One `receive` work request on a loopback carrier. The request names no source: a request the
/// work admission lets through ends at `source_id_missing`, before any custody or dial.
fn receive(work: &mut Work, member: &Joined) -> Result<Value, String> {
    // The owner run's stage transfer (`<run_id>-transfer-<stage>`) the S4b announcements name.
    receive_offer(
        work,
        member,
        &json!({"message":{"run":"run-s4b-transfer-0"}}),
    )
}
/// [`receive`] of `offer`.
fn receive_offer(work: &mut Work, member: &Joined, offer: &Value) -> Result<Value, String> {
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let clock = api::managed_carrier::HostClock::default();
    let deadline = clock
        .monotonic()
        .checked_add(B::RouterClose.span(Entries::ZERO).unwrap())
        .unwrap();
    let host = LoopbackHost::bind(&rt, &clock, deadline).unwrap();
    let endpoint = host.address().unwrap().id;
    let carrier =
        Identity::from_secret(&endpoint.to_string(), &member.signer.secret_bytes()).unwrap();
    let wall = B::MemberChosenWall.span(Entries::ZERO).unwrap().millis();
    let operation = Operation {
        request: json!({"command":"receive","operation_id":1,"offer":offer}),
        cancelled: Arc::new(AtomicBool::new(false)),
        deadline: Instant::now() + Duration::from_millis(wall),
        origin: Origin::Remote,
        id: Some(1),
    };
    let out = work.receive(
        Some(member),
        &Carrier {
            host: &host,
            runtime: &rt,
            clock: &clock,
            endpoint,
            lan: None,
        },
        &member.signer,
        &carrier,
        &operation,
    );
    assert!(
        work.shard.is_none() && work.native.is_none(),
        "no custody was taken"
    );
    out
}
fn share(rss: u64, disk: u64, wall: u64) -> Value {
    json!({"max_rss_bytes": rss, "max_disk_bytes": disk, "max_wall_seconds": wall, "max_commands": 100})
}

/// Announced work needs standing consent granted AND bound to this membership, checked first.
/// (Without an announced share the operator path is unchanged: its consent refusal stays inside
/// `JoinedShard::begin`, pinned by joined_shard.rs `wrong_signer_endpoint_recipe_and_withdrawn_consent_refuse`.)
#[test]
fn announced_work_without_bound_standing_consent_is_refused_before_custody() {
    let member = Joined::new();
    let mut work = Work::new(member.member_root.clone());
    assert_eq!(
        receive(&mut work, &member),
        Err("source_id_missing".into()),
        "no share announced"
    );
    work.announced(&member.announced(Some(share(1_000_000_000, 1_000_000_000, 300))));
    assert_eq!(
        receive(&mut work, &member),
        Err("joined_shard_consent_required".into()),
        "no consent"
    );
    member.grant();
    assert_eq!(
        receive(&mut work, &member),
        Err("joined_shard_consent_required".into()),
        "unbound"
    );
    member.bind();
    assert_eq!(
        receive(&mut work, &member),
        Err("source_id_missing".into()),
        "bound, within caps"
    );
    // A share verified against ANOTHER swarm's seed key is not this membership's.
    let other = Joined::new();
    work.announced(&other.announced(Some(share(1_000_000_000, 1_000_000_000, 300))));
    assert_eq!(
        receive(&mut work, &member),
        Err("owner_pin_announcement_unverified".into()),
        "another seed"
    );
}

/// The announced share must fit the member's bound caps; each excess is refused by name before
/// custody (standing_consent.py:246-258).
#[test]
fn a_share_above_the_members_caps_is_refused_by_name_before_custody() {
    let member = Joined::new();
    member.grant();
    member.bind();
    let mut work = Work::new(member.member_root.clone());
    for (limits, code) in [
        (
            share(8_000_000_000, 1_000_000_000, 300),
            "member_memory_cap_below_share",
        ),
        (
            share(1_000_000_000, 8_000_000_000, 300),
            "member_storage_cap_below_share",
        ),
        (
            share(1_000_000_000, 1_000_000_000, 1200),
            "member_wall_cap_below_share",
        ),
        (
            share(1_000_000_000, 1_000_000_000, 5),
            "member_wall_limit_invalid",
        ),
        // `member_resources_invalid` (a zero or missing rss/disk) is unreachable here: a
        // conforming owner refuses to announce it (`demo_announcement_invalid`, run_exchange.rs
        // check_node_extra); `standing_limits` still refuses it on the member.
    ] {
        work.announced(&member.announced(Some(limits.clone())));
        assert_eq!(work.share(), Some(&limits));
        assert_eq!(receive(&mut work, &member), Err(code.into()), "{limits}");
    }
}

// ---- Lane EP2 / REVIEW-lanePH3B-S4AF F1: the share is checked for the offer's OWN run ----
//
// Shipped app: DemoApp (app.py:505) -> `demo_app.py:156-172` DemoTargetAgent.tick ->
// `_step` (demo_target.py:430): whole-snapshot mode downloads the model first (step 0, :449-454,
// before the `work` announcement), and the share is checked against the CURRENT announcement
// `a` (its run_id, :447) at step 4 (`_share_refusal`, :351-367, :516-519) BEFORE the work is
// accepted and its worker started (step 6, :531-538). v0.2 (S4aF) pushes the shard in the slices
// step, before the run's `work` announcement: so the member checks the run's share at
// `prepare_native`, against the announcement of the offer's own run (fail closed), and at custody
// only when this run's share is already known.

/// A seed-signed shard offer for this member, transfer `run` (`<owner run>-transfer-<stage>`), and
/// the member's custody of it (no bytes fetched: nothing reaches a native worker).
fn custody(member: &Joined, run: &str) -> (Value, api::joined_shard::JoinedShard) {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../planner/tests/fixtures/source-slices.json"
    ))
    .unwrap();
    let case = &fixture["cases"][0];
    let session = member.session.lock().unwrap().clone().unwrap();
    let carrier_key = iroh::SecretKey::generate();
    let carrier = Identity::from_secret(
        &carrier_key.public().to_string(),
        &member.signer.secret_bytes(),
    )
    .unwrap();
    let registration =
        api::joined_shard::register(&session, &member.signer, &carrier, app_host::now()).unwrap();
    let mut recipe = case["node_plans"]["node-0"].clone();
    recipe["node_id"] = json!(session.node_id());
    recipe.as_object_mut().unwrap().remove("node_plan_digest");
    recipe["node_plan_digest"] = json!(protocol::canonical::digest(&recipe).unwrap());
    let source_key = iroh::SecretKey::generate();
    let source = Identity::from_secret(&source_key.public().to_string(), &[9; 32]).unwrap();
    let offer = api::joined_shard::issue(
        &member.seed.coordinator,
        &registration,
        run,
        &case["node_files"]["node-0"][0],
        &recipe,
        &source,
    )
    .unwrap();
    let shard = api::joined_shard::JoinedShard::begin(
        &member.member_root,
        &session,
        &member.signer,
        &carrier,
        &offer,
        app_host::now(),
    )
    .unwrap();
    (offer, shard)
}
/// The owner's `prepare_native` for stage [0, 14) with a fresh challenge.
fn prepare(work: &mut Work, member: &Joined) -> Result<Value, String> {
    let challenges = api::joined_shard::native::Challenges::new();
    let nonce = challenges.issue("node", [0, 14]).unwrap();
    let wall = B::MemberChosenWall.span(Entries::ZERO).unwrap().millis();
    let operation = Operation {
        request: json!({"command":"prepare_native","operation_id":2,"range":[0,14],"tokens":32,
            "nonce":nonce,"forbidden_source":member.root.join("snapshot")}),
        cancelled: Arc::new(AtomicBool::new(false)),
        deadline: Instant::now() + Duration::from_millis(wall),
        origin: Origin::Remote,
        id: Some(2),
    };
    work.prepare_native(Some(member), &member.signer, &operation)
}
const OVER_MEMORY: u64 = 8_000_000_000;

/// F1: with the S4aF order the member takes the shard BEFORE the run's `work` announcement (no
/// share known: custody admitted on the hard limits), then the owner asks for the native prepare.
/// A share announced for THIS run above the member's own bound caps refuses the prepare by name,
/// before any native worker, and the custody stays owned (discardable).
#[test]
fn a_share_above_the_caps_announced_for_this_run_after_the_push_refuses_the_prepare() {
    let member = Joined::new();
    member.grant();
    member.bind();
    let mut work = Work::new(member.member_root.clone());
    let (offer, shard) = custody(&member, "run-ep2-transfer-0");
    assert_eq!(
        receive_offer(&mut work, &member, &offer),
        Err("source_id_missing".into()),
        "the push precedes the `work` announcement: custody on the hard limits"
    );
    work.shard = Some(shard);
    work.announced(&member.announced_for("run-ep2", Some(share(OVER_MEMORY, 1_000_000_000, 300))));
    assert_eq!(
        prepare(&mut work, &member),
        Err("member_memory_cap_below_share".into())
    );
    assert!(
        work.native
            .as_ref()
            .is_some_and(|n| n.worker_pid().is_none())
            || work.shard.is_some(),
        "custody kept, no worker"
    );
    assert!(work.discard().is_ok());
}

/// F1, stale announcement: the member still holds the PREVIOUS run's announcement (a smaller
/// share that fits) when this run's prepare arrives: refused (fail closed,
/// `member_share_unannounced`); once this run's larger share is read, it is refused by name. At
/// custody a previous run's share is never applied (here: larger than the caps, this run's offer
/// is still admitted on the hard limits).
#[test]
fn a_previous_runs_announcement_never_decides_this_runs_share() {
    let member = Joined::new();
    member.grant();
    member.bind();
    let mut work = Work::new(member.member_root.clone());
    let (offer, shard) = custody(&member, "run-ep2-transfer-1");
    work.announced(&member.announced_for("run-ep1", Some(share(OVER_MEMORY, 1_000_000_000, 300))));
    assert_eq!(
        receive_offer(&mut work, &member, &offer),
        Err("source_id_missing".into()),
        "another run's share is not this offer's"
    );
    work.shard = Some(shard);
    work.announced(
        &member.announced_for("run-ep1", Some(share(1_000_000_000, 1_000_000_000, 300))),
    );
    assert_eq!(
        prepare(&mut work, &member),
        Err("member_share_unannounced".into()),
        "the previous run's smaller share does not admit this run"
    );
    work.announced(&member.announced_for("run-ep2", Some(share(OVER_MEMORY, 1_000_000_000, 300))));
    assert_eq!(
        prepare(&mut work, &member),
        Err("member_memory_cap_below_share".into())
    );
    assert!(work.discard().is_ok());
}

/// The operator-driven member (`Work::operator`, the example's CLI-pinned owner) has no
/// announcement: its prepare is not refused for a missing share (its consent caps still bound
/// the worker at prepare).
#[test]
fn an_operator_driven_member_prepares_without_an_announcement() {
    let member = Joined::new();
    member.grant();
    member.bind();
    let mut work = Work::operator(member.member_root.clone());
    let (_, shard) = custody(&member, "run-ep2-transfer-0");
    work.shard = Some(shard);
    let prepared = prepare(&mut work, &member);
    assert!(
        prepared.as_ref().is_err_and(
            |code| code != "member_share_unannounced" && !code.ends_with("_below_share")
        ),
        "reaches the native prepare (no worker binary here): {prepared:?}"
    );
    if let Some(native) = work.native.as_mut() {
        assert!(native.worker_pid().is_none());
    }
    assert!(work.discard().is_ok());
}

// ---- Lane EP2 item 2: the member serves ONE epoch for the run, until the route's signed expiry ----
//
// v0.1.4: the member's native worker lives for its consented wall (worker.py:342) and serves every
// request of the run's route (`run_registry_server`, supervisor.py:4196, :4427-4428); the member
// does not end serving after one operation's wall. v0.2: the serve operation lasts until the
// route's signed `expires_at` (publish + the smallest consented wall), bounded by the member's own
// consent wall from its first prepare and its command allowance (NativeShard, per command).

/// `serve` of a seed-signed v2 route naming this member, with an owner that dials the member's
/// carrier and stays silent; the dispatcher's operation wall (`op_wall`) is shorter than the
/// route's lifetime (`lifetime`). Returns the serve outcome and when it ended (wall clock), and the
/// route's expiry.
fn serve_route(
    member: &Joined,
    prepared_ago: Option<Duration>,
    op_wall: Duration,
    lifetime: Duration,
) -> (Result<Value, String>, f64, f64) {
    let mut work = Work::new(member.member_root.clone());
    let (offer, shard) = custody(member, "run-ep2-transfer-0");
    work.native = Some(api::joined_shard::native::NativeShard::new(shard).unwrap());
    work.prepared_at = prepared_ago.map(|ago| Instant::now().checked_sub(ago).unwrap());
    let document = json!({"controlled_proof":"serve-deadline"});
    let digest = |v: &Value| protocol::canonical::digest(v).unwrap();
    work.proof_digest = Some(digest(&document));
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let clock = api::managed_carrier::HostClock::default();
    let until = |id: B| {
        clock
            .monotonic()
            .checked_add(id.span(Entries::ZERO).unwrap())
            .unwrap()
    };
    let mut host = LoopbackHost::bind(&rt, &clock, until(B::RouterClose)).unwrap();
    let address = host.address().unwrap();
    let owner = iroh::SecretKey::generate();
    let row = member.current(&member.signer).unwrap();
    let stage = |i: usize, start: u64, end: u64| {
        json!({"stage_index":i,"placement_id":format!("{}:{start}-{end}",row["node_id"].as_str().unwrap()),
            "member":row,"carrier_endpoint_id":address.id.to_string(),"run":"run-ep2-transfer-0",
            "offer_digest":digest(&offer),"proof_digest":digest(&document),"range":[start,end],
            "worker_pid":4242,"tokens":32,"memory":4_000_000_000u64})
    };
    let now = app_host::now();
    let expires_at = now + lifetime.as_secs_f64();
    let message = json!({"protocol":api::joined_shard::route::ROUTE_V2,
        "swarm_id":member.seed.coordinator.swarm_id(),"deployment_id":"12345678-1234-5678-9234-abcdefabcdef",
        "deployment_epoch":1,"topology_version":1,"model_id":"Qwen/Qwen3-0.6B",
        "resolved_commit":"c1899de289a04d12100db370d81485cdf75e47ca",
        "manifest_digest":digest(&json!("manifest")),"owner_endpoint_id":owner.public().to_string(),
        "stages":[stage(0,0,14),stage(1,14,28)],"issued_at":now,"expires_at":expires_at,
        "route_ready":false,"scope":api::joined_shard::route::SCOPE});
    let signer = member.seed.coordinator.signer();
    let route = json!({"protocol":"mycelium.joined_shard.signed.v1","signature":signer.sign(&message).unwrap(),
        "verification_key":signer.public_key_record(),"message":message});
    // The owner: dials the member's carrier twice (data + control) and stays silent.
    let (done, hold) = std::sync::mpsc::channel::<()>();
    let (endpoint, dial) = (address.id, address.clone());
    let dialer = std::thread::spawn(move || {
        let address = dial;
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(1)
            .enable_all()
            .build()
            .unwrap();
        let clock = api::managed_carrier::HostClock::default();
        let until = |id: B| {
            clock
                .monotonic()
                .checked_add(id.span(Entries::ZERO).unwrap())
                .unwrap()
        };
        let mut host =
            LoopbackHost::bind_with_secret(&rt, &clock, until(B::RouterClose), owner).unwrap();
        let connections: Vec<_> = (0..2)
            .map(|_| {
                host.connect_channel(
                    &address,
                    transport::Channel::Router,
                    &clock,
                    until(B::RouterClose),
                )
                .unwrap()
            })
            .collect();
        let held = hold.recv_timeout(
            B::WorkerStartup
                .span(Entries::ZERO)
                .map(|s| Duration::from_millis(s.millis()))
                .unwrap(),
        );
        drop(connections);
        host.close(&clock, until(B::RouterClose)).unwrap();
        held.is_ok()
    });
    let operation = Operation {
        request: json!({"command":"serve","operation_id":3,"route":route}),
        cancelled: Arc::new(AtomicBool::new(false)),
        deadline: Instant::now() + op_wall,
        origin: Origin::Remote,
        id: Some(3),
    };
    let served = work.serve(
        Some(member),
        &Carrier {
            host: &host,
            runtime: &rt,
            clock: &clock,
            endpoint,
            lan: None,
        },
        &member.signer,
        &operation,
    );
    let ended = app_host::now();
    done.send(()).unwrap();
    assert!(
        dialer.join().unwrap(),
        "the owner held its connections until the serve ended"
    );
    host.close(&clock, until(B::RouterClose)).unwrap();
    assert!(work.discard().is_ok());
    (served, ended, expires_at)
}

/// Item 2: the serve operation's deadline is the route's signed expiry, not the dispatcher's
/// one-operation wall: with an operation wall shorter than the route's lifetime the member keeps
/// serving the epoch until the route expires (it does not end `operator_cancelled` at the
/// operation wall).
#[test]
fn a_member_serves_its_epoch_until_the_routes_signed_expiry_not_one_operation_wall() {
    // Order 1: the route's signed expiry ends first (consent wall 600 s from the serve).
    let member = Joined::new();
    member.grant();
    member.bind();
    let poll = Duration::from_millis(B::OwnerPoll.span(Entries::ZERO).unwrap().millis());
    let lifetime = Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis());
    let (served, ended, expires_at) = serve_route(&member, None, poll * 2, lifetime);
    let report = served.unwrap();
    assert!(
        matches!(
            report["teardown"].as_str(),
            Some("route_expired" | "owner_closed")
        ),
        "the epoch ends at the route's expiry: {report}"
    );
    assert!(
        ended >= expires_at - poll.as_secs_f64(),
        "served until {ended}, the route expires at {expires_at}"
    );
}

/// Item 2, the other order (REVIEW-laneEP R1: the member never outlives its OWN consent): when the
/// member's consent wall ends before the route's signed expiry, the serve ends at the consent wall
/// (counted from the first prepare, set here 59 s before a 60 s wall), not at the expiry the
/// owner signed. (The serve reports `operator_cancelled` for that end: `joined_shard/serve.rs` has no
/// consent-wall code; lane EPF owns that file.)
#[test]
fn a_member_whose_consent_wall_ends_first_stops_serving_at_its_consent_wall_not_the_routes_expiry() {
    let member = Joined::new();
    member.grant_wall(60);
    member.bind();
    let lifetime = Duration::from_millis(B::CancelAndCleanup.span(Entries::ZERO).unwrap().millis());
    let started = app_host::now();
    // The first prepare ran 59 s ago: one second of the 60 s consent wall is left.
    let (served, ended, expires_at) =
        serve_route(&member, Some(Duration::from_secs(59)), lifetime, lifetime);
    let report = served.unwrap();
    assert_ne!(report["teardown"], "route_expired", "{report}");
    assert!(
        ended < expires_at - 2.0,
        "the consent wall (1 s left) ended the serve: ended {ended}, route expires {expires_at}"
    );
    assert!(ended >= started + 0.9, "served until the consent wall: {started}..{ended}");
}
