// SPDX-License-Identifier: AGPL-3.0-only
//! Real pairing/seed authority for inert shard custody; no native proof.
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
#[path = "support/joined_native_process.rs"]
mod native_process;
#[path = "support/joined_shard_process.rs"]
mod process;
#[path = "support/joined_route_process.rs"]
mod route_process;
#[path = "support/sse_client.rs"]
mod sse_client;
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Pair {
    root: PathBuf,
    member_root: PathBuf,
    seed: app_host::SeedRuntime<app_host::Unmanaged>,
    signer: Identity,
    carrier: Identity,
    session: Session,
}
impl Pair {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "b7b-pair-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir(&root).unwrap();
        let root = fs::canonicalize(root).unwrap();
        let owner = root.join("owner");
        let member_root = root.join("member");
        fs::create_dir(&owner).unwrap();
        fs::create_dir(&member_root).unwrap();
        let seed = app_host::start_seed::<app_host::Unmanaged>(&owner, "b7b-swarm", None).unwrap();
        let request = app_host::build_pair_request(&member_root, &member_root).unwrap();
        let (signer, _, _) = app_host::identity(&member_root).unwrap();
        let (seed_signer, _, _) =
            app_host::identity_in(&owner, app_host::SEED_IDENTITY_FILE).unwrap();
        let coordinator = handoff::Coordinator {
            signer: seed_signer,
            swarm_id: "b7b-swarm".into(),
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
        standing_consent::grant(&member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1000}}), &policy, app_host::now(), Some(16_000_000_000)).unwrap();
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
    fn registration(&self) -> Value {
        joined_shard::register(&self.session, &self.signer, &self.carrier, app_host::now()).unwrap()
    }
    fn material(&self) -> (Value, Value, Vec<u8>) {
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
        let name = transfer["path"]
            .as_str()
            .unwrap()
            .strip_prefix("deployment/")
            .unwrap();
        let encoded = case["shard_files"][name].as_str().unwrap();
        let bytes = (0..encoded.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&encoded[i..i + 2], 16).unwrap())
            .collect();
        (recipe, transfer, bytes)
    }
    fn offer(&self) -> Value {
        let (recipe, transfer, _) = self.material();
        let endpoint = iroh::SecretKey::generate().public().to_string();
        let source =
            Identity::from_secret(&endpoint, &self.seed.coordinator.signer().secret_bytes())
                .unwrap();
        joined_shard::issue(
            &self.seed.coordinator,
            &self.registration(),
            "b7b-transfer",
            &transfer,
            &recipe,
            &source,
        )
        .unwrap()
    }
    fn resume(&mut self) {
        let options = self.seed.options();
        let random = |n| app_host::hex(&app_host::random(n).unwrap());
        let env = peer::JoinEnv {
            signer: &self.signer,
            clock: &app_host::now,
            random_hex: &random,
            seed: &options,
        };
        self.session = peer::start(&self.member_root, &self.signer, true, &env)
            .unwrap()
            .session
            .unwrap();
        peer::renew(&self.member_root, true, Some(&mut self.session), &env).unwrap();
    }
}
impl Drop for Pair {
    fn drop(&mut self) {
        self.seed.close();
        fs::remove_dir_all(&self.root).unwrap();
    }
}
#[test]
fn paired_identity_and_actual_generation_authorize_only_the_pinned_transfer() {
    let p = Pair::new();
    let offer = p.offer();
    let row = p.seed.coordinator.member(p.session.node_id()).unwrap();
    assert_eq!(offer["message"]["member"]["generation"], row["generation"]);
    assert_eq!(offer["message"]["member"]["node_id"], p.session.node_id());
    joined_shard::check_owner(&p.seed.coordinator, &offer).unwrap();
    let mut shard = joined_shard::JoinedShard::begin(
        &p.member_root,
        &p.session,
        &p.signer,
        &p.carrier,
        &offer,
        app_host::now(),
    )
    .unwrap();
    let (_, _, bytes) = p.material();
    shard
        .write_chunk(
            0,
            &bytes,
            &p.session,
            &p.signer,
            &p.carrier,
            app_host::now(),
        )
        .unwrap();
    shard
        .verify(&p.session, &p.signer, &p.carrier, &app_host::now)
        .unwrap();
    assert_eq!(fs::read(shard.path()).unwrap(), bytes);
    shard.discard().unwrap();
}
#[test]
fn actual_resume_fences_an_old_offer_before_any_new_bytes() {
    let mut p = Pair::new();
    let offer = p.offer();
    let old = p.session.generation().unwrap();
    let mut shard = joined_shard::JoinedShard::begin(
        &p.member_root,
        &p.session,
        &p.signer,
        &p.carrier,
        &offer,
        app_host::now(),
    )
    .unwrap();
    p.resume();
    assert!(p.session.generation().unwrap() > old);
    assert_eq!(
        joined_shard::check_owner(&p.seed.coordinator, &offer)
            .unwrap_err()
            .0,
        "joined_shard_member_changed"
    );
    assert_eq!(
        shard
            .write_chunk(0, &[1], &p.session, &p.signer, &p.carrier, app_host::now())
            .unwrap_err()
            .0,
        "joined_shard_member_changed"
    );
    assert_eq!(fs::metadata(shard.path()).unwrap().len(), 0);
    shard.discard().unwrap();
}
#[test]
fn native_custody_refuses_changed_consent_caps_before_launch() {
    let p = Pair::new();
    let offer = p.offer();
    let shard = joined_shard::JoinedShard::begin(
        &p.member_root,
        &p.session,
        &p.signer,
        &p.carrier,
        &offer,
        app_host::now(),
    )
    .unwrap();
    let mut native = joined_shard::native::NativeShard::new(shard).unwrap();
    standing_consent::withdraw(&p.member_root, app_host::now()).unwrap();
    let policy = json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
    standing_consent::grant(&p.member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":2_000_000_000u64,"max_wall_seconds":600,"max_commands":1000}}), &policy, app_host::now(), Some(16_000_000_000)).unwrap();
    let binding = p.session.binding().unwrap();
    standing_consent::bind(&p.member_root, &json!({"swarm_id":binding.swarm_id,"node_id":binding.node_id,"seed_key_digest":binding.seed_key_digest}), app_host::now()).unwrap();
    // Invalid range is a second, independent effect fence: this RED can never launch.
    assert_eq!(
        native
            .prepare(
                [0, 0],
                1,
                "00112233445566778899aabbccddeeff",
                &p.signer,
                &|| p
                    .session
                    .shard_identity(&p.signer, app_host::now())
                    .map_err(|_| api::compiler_inputs::Error("test_member_invalid")),
                std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
                &p.root
            )
            .unwrap_err()
            .0,
        "joined_native_consent_changed"
    );
    native.discard().unwrap();
}

#[test]
fn cancelled_native_prepare_refuses_before_scanning_partial_custody() {
    let p = Pair::new();
    let offer = p.offer();
    let shard = joined_shard::JoinedShard::begin(
        &p.member_root,
        &p.session,
        &p.signer,
        &p.carrier,
        &offer,
        app_host::now(),
    )
    .unwrap();
    let mut native = joined_shard::native::NativeShard::new(shard).unwrap();
    // No model or executable: partial custody is an independent effect fence.
    let error = native
        .prepare(
            [0, 14],
            64,
            "00112233445566778899aabbccddeeff",
            &p.signer,
            &|| {
                p.session
                    .shard_identity(&p.signer, app_host::now())
                    .map_err(|_| api::compiler_inputs::Error("test_member_invalid"))
            },
            std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true)),
            &p.root,
        )
        .unwrap_err();
    native.discard().unwrap();
    assert_eq!(error.0, "compiler_stopped");
}

#[test]
fn expired_native_prepare_refuses_before_scanning_partial_custody() {
    let p = Pair::new();
    let offer = p.offer();
    let shard = joined_shard::JoinedShard::begin(
        &p.member_root,
        &p.session,
        &p.signer,
        &p.carrier,
        &offer,
        app_host::now(),
    )
    .unwrap();
    let mut native = joined_shard::native::NativeShard::new(shard).unwrap();
    let error = native
        .prepare_until(
            [0, 14],
            64,
            "00112233445566778899aabbccddeeff",
            &p.signer,
            &|| {
                p.session
                    .shard_identity(&p.signer, app_host::now())
                    .map_err(|_| api::compiler_inputs::Error("test_member_invalid"))
            },
            std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
            &p.root,
            std::time::Instant::now(),
        )
        .unwrap_err();
    native.discard().unwrap();
    assert_eq!(error.0, "native_worker_deadline");
}

#[test]
fn wrong_signer_endpoint_recipe_and_withdrawn_consent_refuse() {
    let p = Pair::new();
    let mut offer = p.offer();
    let stranger = Identity::from_secret(p.signer.endpoint_id(), &[77; 32]).unwrap();
    assert_eq!(
        joined_shard::register(&p.session, &stranger, &p.carrier, app_host::now())
            .unwrap_err()
            .0,
        "joined_shard_identity_mismatch"
    );
    offer["message"]["recipe"]["node_id"] = json!("other-node");
    assert!(
        joined_shard::JoinedShard::begin(
            &p.member_root,
            &p.session,
            &p.signer,
            &p.carrier,
            &offer,
            app_host::now()
        )
        .is_err()
    );
    let offer = p.offer();
    standing_consent::withdraw(&p.member_root, app_host::now()).unwrap();
    assert_eq!(
        joined_shard::JoinedShard::begin(
            &p.member_root,
            &p.session,
            &p.signer,
            &p.carrier,
            &offer,
            app_host::now()
        )
        .unwrap_err()
        .0,
        "joined_shard_consent_required"
    );
    assert!(!p.member_root.join("chain-runs/b7b-transfer").exists());
}
