// SPDX-License-Identifier: AGPL-3.0-only
//! Lane MG item 3: the memory the member's worker guard enforces comes from the
//! member's OWN bound consent (or a tighter owner share), never a larger number.
//! v0.1.4: `standing_limits` (mycelium_a13_macos/standing_consent.py:230-261 @ f4e366e9).
use api::{
    app_host, handoff, joined_shard,
    member::Session,
    peer,
    preferences::{resource_policy, standing_consent},
};
use protocol::identity::Identity;
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
const CAP: u64 = 2_000_000_000;
struct Pair {
    root: PathBuf,
    member_root: PathBuf,
    seed: app_host::SeedRuntime<app_host::Unmanaged>,
    signer: Identity,
    carrier: Identity,
    session: Session,
}
impl Pair {
    /// A real pairing whose member consented to `CAP` bytes of memory.
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "mg-cap-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir(&root).unwrap();
        let root = fs::canonicalize(root).unwrap();
        let owner = root.join("owner");
        let member_root = root.join("member");
        fs::create_dir(&owner).unwrap();
        fs::create_dir(&member_root).unwrap();
        let seed = app_host::start_seed::<app_host::Unmanaged>(&owner, "mg-swarm", None).unwrap();
        let request = app_host::build_pair_request(&member_root, &member_root).unwrap();
        let (signer, _, _) = app_host::identity(&member_root).unwrap();
        let (seed_signer, _, _) =
            app_host::identity_in(&owner, app_host::SEED_IDENTITY_FILE).unwrap();
        let coordinator = handoff::Coordinator {
            signer: seed_signer,
            swarm_id: "mg-swarm".into(),
            seed_url: seed.seed_url.clone(),
        };
        let ttl = budgets::BudgetId::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37
            .span(budgets::Entries::ZERO)
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
        let session = accepted.session.unwrap();
        let policy = json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
        resource_policy::write(&member_root, &policy).unwrap();
        standing_consent::grant(&member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":CAP,"max_wall_seconds":600,"max_commands":1000}}), &policy, app_host::now(), Some(16_000_000_000)).unwrap();
        let binding = session.binding().unwrap();
        standing_consent::bind(&member_root, &json!({"swarm_id":binding.swarm_id,"node_id":binding.node_id,"seed_key_digest":binding.seed_key_digest}), app_host::now()).unwrap();
        let endpoint = iroh::SecretKey::generate().public().to_string();
        let carrier = Identity::from_secret(&endpoint, &signer.secret_bytes()).unwrap();
        Self {
            root,
            member_root,
            seed,
            signer,
            carrier,
            session,
        }
    }
    /// The owner's signed offer for this member (the planner's first fixture slice).
    fn offer(&self) -> Value {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../planner/tests/fixtures/source-slices.json"
        ))
        .unwrap();
        let case = &fixture["cases"][0];
        let mut recipe = case["node_plans"]["node-0"].clone();
        recipe["node_id"] = json!(self.session.node_id());
        recipe.as_object_mut().unwrap().remove("node_plan_digest");
        recipe["node_plan_digest"] = json!(protocol::canonical::digest(&recipe).unwrap());
        let transfer = case["node_files"]["node-0"][0].clone();
        let registration =
            joined_shard::register(&self.session, &self.signer, &self.carrier, app_host::now())
                .unwrap();
        let endpoint = iroh::SecretKey::generate().public().to_string();
        let source =
            Identity::from_secret(&endpoint, &self.seed.coordinator.signer().secret_bytes())
                .unwrap();
        joined_shard::issue(
            &self.seed.coordinator,
            &registration,
            "mg-transfer",
            &transfer,
            &recipe,
            &source,
        )
        .unwrap()
    }
}
impl Drop for Pair {
    fn drop(&mut self) {
        self.seed.close();
        fs::remove_dir_all(&self.root).unwrap();
    }
}

/// The guard's limit is the member's own consent cap. An owner share above it
/// is refused by name and loosens nothing; a share below it tightens the guard
/// (the shipped `standing_limits` rule), and a later larger share cannot raise
/// it again, even one that is still under the cap.
#[test]
fn the_guard_limit_is_the_consent_cap_and_an_owner_share_only_tightens_it() {
    let p = Pair::new();
    let shard = joined_shard::JoinedShard::begin(
        &p.member_root,
        &p.session,
        &p.signer,
        &p.carrier,
        &p.offer(),
        app_host::now(),
    )
    .unwrap();
    let mut native = joined_shard::native::NativeShard::new(shard).unwrap();
    assert_eq!(native.memory_limit().map_err(|e| e.0), Ok(CAP));
    assert_eq!(
        native.limit_memory_to(CAP + 1).map_err(|e| e.0),
        Err("member_memory_cap_below_share")
    );
    assert_eq!(native.memory_limit().map_err(|e| e.0), Ok(CAP));
    assert_eq!(
        native.limit_memory_to(0).map_err(|e| e.0),
        Err("member_resources_invalid")
    );
    assert_eq!(native.memory_limit().map_err(|e| e.0), Ok(CAP));
    let share = CAP / 4 * 3;
    assert_eq!(native.limit_memory_to(share).map_err(|e| e.0), Ok(share));
    assert_eq!(
        native.limit_memory_to(CAP - 1).map_err(|e| e.0),
        Ok(share),
        "a later share must not raise the guard"
    );
    assert_eq!(native.memory_limit().map_err(|e| e.0), Ok(share));
    assert_eq!(native.worker_failure(), None);
    native.discard().unwrap();
}
