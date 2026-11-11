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
    fn bind(&self) {
        work::bind_consent(&self.member_root, self).unwrap();
    }
    /// The owner's signed read answer for this member, announcing `work_limits` for it, verified
    /// by the member's own exchange client against the seed key it pinned at join.
    fn announced(&self, work_limits: Option<Value>) -> VerifiedAnnouncement {
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
                run_id: Some("run-s4b".into()),
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
        request: json!({"command":"receive","operation_id":1,"offer":{}}),
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
