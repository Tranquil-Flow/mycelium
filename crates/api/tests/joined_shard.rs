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
#[path = "support/member_remote.rs"]
mod member_remote;
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

/// EP0: a session reset between requests of one route epoch is one command
/// against the member's consented `max_commands`, like a native step: with an
/// allowance of one, the first reset consumes it (this custody has no worker,
/// so it is refused `joined_native_not_prepared`) and the second is refused
/// `joined_native_consent_exhausted` before anything else is looked at.
#[test]
fn a_session_reset_counts_against_the_consent_command_allowance() {
    let p = Pair::new();
    standing_consent::withdraw(&p.member_root, app_host::now()).unwrap();
    let policy = json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
    standing_consent::grant(&p.member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1}}), &policy, app_host::now(), Some(16_000_000_000)).unwrap();
    let binding = p.session.binding().unwrap();
    standing_consent::bind(&p.member_root, &json!({"swarm_id":binding.swarm_id,"node_id":binding.node_id,"seed_key_digest":binding.seed_key_digest}), app_host::now()).unwrap();
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
    let current = || {
        p.session
            .shard_identity(&p.signer, app_host::now())
            .map_err(|_| api::compiler_inputs::Error("test_member_invalid"))
    };
    let refused: Vec<&str> = (0..2)
        .map(|_| native.reset(&current).unwrap_err().0)
        .collect();
    assert_eq!(
        refused,
        [
            "joined_native_not_prepared",
            "joined_native_consent_exhausted"
        ]
    );
    native.discard().unwrap();
}

/// Lane EPF (REVIEW-laneEP R1): `NativeShard::live`, the check a v2 serve runs
/// on every loop pass, spends nothing, and refuses once the membership is not
/// current, the command allowance is spent or the standing consent is gone.
#[test]
fn native_liveness_checks_consent_without_spending_it() {
    let p = Pair::new();
    standing_consent::withdraw(&p.member_root, app_host::now()).unwrap();
    let policy = json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
    standing_consent::grant(&p.member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1}}), &policy, app_host::now(), Some(16_000_000_000)).unwrap();
    let binding = p.session.binding().unwrap();
    standing_consent::bind(&p.member_root, &json!({"swarm_id":binding.swarm_id,"node_id":binding.node_id,"seed_key_digest":binding.seed_key_digest}), app_host::now()).unwrap();
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
    let current = || {
        p.session
            .shard_identity(&p.signer, app_host::now())
            .map_err(|_| api::compiler_inputs::Error("test_member_invalid"))
    };
    let other = || {
        current().map(|mut m| {
            m["generation"] = json!(m["generation"].as_u64().unwrap() + 1);
            m
        })
    };
    for _ in 0..3 {
        assert_eq!(native.live(&current).map_err(|e| e.0), Ok(()));
    }
    assert_eq!(
        native.live(&other).unwrap_err().0,
        "joined_shard_member_changed"
    );
    // The one allowed command is still there: liveness spent nothing.
    assert_eq!(
        native.reset(&current).unwrap_err().0,
        "joined_native_not_prepared"
    );
    assert_eq!(
        native.live(&current).unwrap_err().0,
        "joined_native_consent_exhausted"
    );
    standing_consent::withdraw(&p.member_root, app_host::now()).unwrap();
    assert_eq!(
        native.live(&current).unwrap_err().0,
        "joined_shard_consent_required"
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

/// F3: a native worker that dies during prepare is reaped by the start itself,
/// so cleanup is proven and the custody can be retired without a restart. The
/// pinned "native binary" is the system `false` executable (a real child that
/// exits at once); no proof or model output is involved.
#[test]
fn failed_native_launch_proves_release_and_allows_discard() {
    let binary = PathBuf::from("/usr/bin/false");
    let sha = protocol::canonical::sha256_prefixed(&fs::read(&binary).unwrap())[7..].to_owned();
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "failed_native_launch_child",
            "--ignored",
            "--nocapture",
        ])
        .env("MYCELIUM_COMPILER_NATIVE_BINARY", &binary)
        .env("MYCELIUM_COMPILER_NATIVE_SHA256", sha)
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&output.stdout).into_owned()
        + &String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "{text}");
    assert!(text.contains("1 passed"), "{text}");
}
#[test]
#[ignore = "child of failed_native_launch_proves_release_and_allows_discard (pinned env)"]
fn failed_native_launch_child() {
    let p = Pair::new();
    let offer = p.offer();
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
    let path = shard.path();
    let mut native = joined_shard::native::NativeShard::new(shard).unwrap();
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
            std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
            &p.root,
        )
        .unwrap_err();
    eprintln!("failed launch refused: {}", error.0);
    assert_ne!(error.0, "native_cleanup_unproven");
    assert_eq!(native.worker_pid(), None, "no live worker remains");
    native.discard().unwrap();
    assert!(!path.exists(), "custody retired after proven release");
}

/// B16 S3 (gap row 12, TWO-MAC-TEST C1): the owner only REQUESTS consent. It
/// admits a member for a transfer only when the member's OWN standing consent
/// (member-side decision) is granted, bound to this swarm/member/seed key and
/// covers the bytes; anything else is `member_consent_required`.
#[test]
fn owner_requires_the_members_own_bound_consent_before_an_offer() {
    use api::joined_owner::consent::require;
    let p = Pair::new();
    let registration = p.registration();
    let status = |root: &std::path::Path| {
        json!({"consent":standing_consent::load(root).unwrap(),
            "policy":resource_policy::read(root).unwrap()})
    };
    let granted = require(
        &p.seed.coordinator,
        &registration,
        &status(&p.member_root),
        1_000_000,
    )
    .unwrap();
    assert_eq!(granted["state"], "granted");
    assert_eq!(granted["binding"]["node_id"], p.session.node_id());
    let refused = |reply: &Value, bytes: u64| {
        require(&p.seed.coordinator, &registration, reply, bytes)
            .unwrap_err()
            .code
    };
    assert_eq!(
        refused(&status(&p.member_root), 5_000_000_000),
        "member_consent_required",
        "caps below the transfer"
    );
    assert_eq!(
        refused(&json!({"consent":null,"policy":null}), 1),
        "member_consent_required",
        "no member-side consent"
    );
    let mut other = status(&p.member_root);
    other["consent"]["binding"]["swarm_id"] = json!("another-swarm");
    assert_eq!(
        refused(&other, 1),
        "member_consent_required",
        "bound elsewhere"
    );
    let mut stale = status(&p.member_root);
    stale["consent"]["binding"]["seed_key_digest"] = json!("sha256:00");
    assert_eq!(
        refused(&stale, 1),
        "member_consent_required",
        "other seed key"
    );
    standing_consent::withdraw(&p.member_root, app_host::now()).unwrap();
    assert_eq!(
        refused(&status(&p.member_root), 1),
        "member_consent_required",
        "withdrawn"
    );
}

/// An in-process member behind the owner's [`MemberLink`]: it answers
/// `pair_request` and `accept` with its real pair request, real seed join and
/// real signed carrier registration. A `liar` re-signs that registration with
/// its own key and claims that key as the member's enrolled key.
struct InProcessMember {
    root: PathBuf,
    signer: Identity,
    seed: api::seed::SeedOptions,
    liar: Option<Identity>,
    reply: Option<Result<Value, api::joined_owner::link::Refusal>>,
}
impl InProcessMember {
    fn answer(&mut self, command: &Value) -> Result<Value, api::joined_owner::link::Refusal> {
        let refused = |code: &str| api::joined_owner::link::Refusal(code.into());
        match command["command"].as_str() {
            Some("pair_request") => Ok(app_host::build_pair_request(&self.root, &self.root)
                .map_err(|e| refused(e.code))?
                .to_value()),
            Some("accept") => {
                let random = |n| app_host::hex(&app_host::random(n).unwrap());
                let env = peer::JoinEnv {
                    signer: &self.signer,
                    clock: &app_host::now,
                    random_hex: &random,
                    seed: &self.seed,
                };
                let mut accepted =
                    peer::accept_handoff(&self.root, true, &command["handoff"], &env)
                        .map_err(|e| refused(e.code))?;
                peer::renew(&self.root, true, accepted.session.as_mut(), &env)
                    .map_err(|e| refused(e.code))?;
                let session = accepted.session.ok_or_else(|| refused("session_missing"))?;
                let endpoint = iroh::SecretKey::generate().public().to_string();
                let carrier =
                    Identity::from_secret(&endpoint, &self.signer.secret_bytes()).unwrap();
                let genuine =
                    joined_shard::register(&session, &self.signer, &carrier, app_host::now())
                        .map_err(|e| refused(e.0))?;
                let Some(liar) = &self.liar else {
                    return Ok(genuine);
                };
                // Same node, generation and incarnation as the seed row; only
                // the key (and its endpoint) is the liar's, and the liar signs.
                let mut message = genuine["message"].clone();
                message["member"]["verification_key_digest"] =
                    json!(liar.verification_key_digest());
                message["member"]["endpoint_id"] = json!(liar.endpoint_id());
                Ok(
                    json!({"protocol":genuine["protocol"],"signature":liar.sign(&message).unwrap(),
                    "verification_key":liar.public_key_record(),"message":message}),
                )
            }
            _ => Err(refused("unexpected_command")),
        }
    }
}
impl api::joined_owner::link::MemberLink for InProcessMember {
    fn begin(&mut self, command: Value) -> Result<(), api::joined_owner::link::Refusal> {
        self.reply = Some(self.answer(&command));
        Ok(())
    }
    fn finish(
        &mut self,
        _: std::time::Duration,
    ) -> Result<Value, api::joined_owner::link::Refusal> {
        self.reply
            .take()
            .unwrap_or_else(|| Err(api::joined_owner::link::Refusal("no_command".into())))
    }
}

/// REVIEW-laneB16 F2: the owner's admission checks the member's own key claim
/// against the seed row BEFORE trusting it. The remote channel enrols the key
/// the accept reply names (`remote.rs`), and the owner then asks that key for
/// consent; a registration that names the member's node and generation but
/// another key (signed by that key) must be refused at admit, not first at the
/// later offer. The genuine registration of the same flow is admitted.
#[test]
fn owner_admission_refuses_a_member_key_claim_the_seed_row_does_not_hold() {
    let root = std::env::temp_dir().join(format!(
        "b16f-admit-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ));
    fs::create_dir(&root).unwrap();
    let root = fs::canonicalize(root).unwrap();
    let owner = root.join("owner");
    fs::create_dir(&owner).unwrap();
    let seed = app_host::start_seed::<app_host::Unmanaged>(&owner, "b16f-swarm", None).unwrap();
    let caps = json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false});
    let within = std::time::Duration::from_millis(
        budgets::BudgetId::RouterClose
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis(),
    );
    let member = |name: &str, liar: Option<Identity>| {
        let dir = root.join(name);
        fs::create_dir(&dir).unwrap();
        app_host::build_pair_request(&dir, &dir).unwrap();
        let (signer, _, _) = app_host::identity(&dir).unwrap();
        InProcessMember {
            root: dir,
            signer,
            seed: seed.options(),
            liar,
            reply: None,
        }
    };
    let mut honest = member("honest", None);
    let admitted =
        api::joined_owner::link::admit(&mut honest, &owner, &seed, seed.address, &caps, within)
            .expect("the genuine registration is admitted");
    let node = admitted["message"]["member"]["node_id"].as_str().unwrap();
    assert_eq!(
        admitted["message"]["member"]["verification_key_digest"],
        seed.coordinator.member(node).unwrap()["verification_key_digest"]
    );
    let liar = Identity::from_secret(
        &iroh::SecretKey::generate().public().to_string(),
        &[0x5a; 32],
    )
    .unwrap();
    let mut lying = member("lying", Some(liar));
    let refused =
        api::joined_owner::link::admit(&mut lying, &owner, &seed, seed.address, &caps, within);
    match refused {
        Err(refusal) => assert_eq!(refusal.0, "joined_shard_signature_invalid", "{refusal:?}"),
        Ok(registration) => panic!(
            "admitted a key the seed row does not hold: claimed {} (row {})",
            registration["message"]["member"]["verification_key_digest"],
            seed.coordinator
                .member(
                    registration["message"]["member"]["node_id"]
                        .as_str()
                        .unwrap()
                )
                .unwrap()["verification_key_digest"]
        ),
    }
    seed.close();
    fs::remove_dir_all(&root).unwrap();
}
