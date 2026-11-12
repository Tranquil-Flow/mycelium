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
        Self::with_cap(CAP)
    }
    /// A real pairing whose member consented to `cap` bytes of memory.
    fn with_cap(cap: u64) -> Self {
        Self::with_caps(cap, 4_000_000_000)
    }
    /// A real pairing whose member consented to `cap` bytes of memory and
    /// `storage` bytes of storage.
    fn with_caps(cap: u64, storage: u64) -> Self {
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
        standing_consent::grant(&member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":storage,"max_memory_bytes":cap,"max_wall_seconds":600,"max_commands":1000}}), &policy, app_host::now(), Some(16_000_000_000)).unwrap();
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
        self.offer_for("mg-transfer")
    }
    /// As `offer`, for the owner transfer (run) `transfer_id`.
    fn offer_for(&self, transfer_id: &str) -> Value {
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
            transfer_id,
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

// ---- Lane MGF: the number the worker is ACTUALLY prepared with and guarded at ----
// A pinned "native binary" (a shell script) runs this test binary as a fake
// worker, `fake_native_worker`, speaking the private worker link on fd 3 (the
// script moves the socket off stdout so the harness's own output cannot reach
// it). The fake records the prepare document it received and then holds real,
// touched memory above every cap, so the host-probed guard of the production
// start path (`NativeShard::prepare_until` -> `Worker::start_scoped_until`)
// trips and reports the limit it compared against. No model, no Metal.

/// The smallest cap a member can consent to (protocol consent `max_memory_bytes`).
const SMALL_CAP: u64 = 256 << 20;
/// Touched by the fake worker: above `SMALL_CAP` and every share below it.
const HOG_BYTES: usize = 384 << 20;
/// A prepare argument far above any cap; it must change nothing.
const TOKENS: usize = 1 << 20;
const NONCE: &str = "00112233445566778899aabbccddeeff";

fn bound() -> std::time::Duration {
    // Explicit and generous: v0.1.4's worker start allowance (worker.py:36).
    std::time::Duration::from_millis(
        budgets::BudgetId::WorkerStartup
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis(),
    )
}

/// Run `test` (an ignored child test of this file) in a fresh process whose
/// pinned native binary is the fake worker.
fn run_child(test: &str) {
    let dir = std::env::temp_dir().join(format!(
        "mgf-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ));
    fs::create_dir(&dir).unwrap();
    let dir = fs::canonicalize(dir).unwrap();
    let record = dir.join("record");
    fs::create_dir(&record).unwrap();
    let script = dir.join("worker.sh");
    let exe = std::env::current_exe().unwrap();
    fs::write(
        &script,
        format!(
            "#!/bin/sh\nexec 3>&1 1>/dev/null\nexec '{}' --exact fake_native_worker --ignored --nocapture\n",
            exe.display()
        ),
    )
    .unwrap();
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(&script, fs::Permissions::from_mode(0o700)).unwrap();
    let sha = protocol::canonical::sha256_prefixed(&fs::read(&script).unwrap())[7..].to_owned();
    let output = std::process::Command::new(&exe)
        .args(["--exact", test, "--ignored", "--nocapture"])
        .env("MYCELIUM_COMPILER_NATIVE_BINARY", &script)
        .env("MYCELIUM_COMPILER_NATIVE_SHA256", sha)
        .env("MGF_FAKE_DIR", &record)
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&output.stdout).into_owned()
        + &String::from_utf8_lossy(&output.stderr);
    fs::remove_dir_all(&dir).unwrap();
    assert!(output.status.success(), "{text}");
    assert!(text.contains("1 passed"), "{text}");
}

fn read_frame(link: &mut fs::File) -> Option<Value> {
    use std::io::Read;
    let mut len = [0; 4];
    link.read_exact(&mut len).ok()?;
    let mut head = vec![0; u32::from_be_bytes(len) as usize];
    link.read_exact(&mut head).ok()?;
    link.read_exact(&mut len).ok()?;
    let mut payload = vec![0; u32::from_be_bytes(len) as usize];
    link.read_exact(&mut payload).ok()?;
    serde_json::from_slice(&head).ok()
}
fn write_frame(link: &mut fs::File, head: &Value) {
    use std::io::Write;
    let head = serde_json::to_vec(head).unwrap();
    let mut frame = (head.len() as u32).to_be_bytes().to_vec();
    frame.extend(&head);
    frame.extend(0_u32.to_be_bytes());
    link.write_all(&frame).unwrap();
}
fn hog() {
    let mut bytes = vec![0_u8; HOG_BYTES];
    for page in bytes.chunks_mut(4096) {
        page[0] = 1;
    }
    std::mem::forget(std::hint::black_box(bytes));
}

/// Where the `grow_disk` fake writes: beside the shard, in the custody's
/// staging folder (`chain-runs/<run>/stage-shards`).
fn grown_path(prepare: &Value) -> PathBuf {
    std::path::Path::new(prepare["checkpoint"].as_str().unwrap())
        .parent()
        .unwrap()
        .join("grown.bin")
}

/// The fake worker. Mode (file `mode` in the record dir): `hog_after_reply`
/// accepts the prepare, then holds memory during the resident-proof exchange;
/// `hog_during_prepare` holds memory and never answers the prepare;
/// `refuse_proof` accepts the prepare and answers the proof with `ok:false`;
/// `grow_disk` accepts the prepare, then grows the custody folder;
/// `exit_during_prepare` exits (status 3) without answering the prepare.
#[test]
#[ignore = "fake native worker, run only by the pinned script of run_child"]
fn fake_native_worker() {
    let dir = PathBuf::from(std::env::var_os("MGF_FAKE_DIR").unwrap());
    let mode = fs::read_to_string(dir.join("mode")).unwrap();
    let mut link = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open("/dev/fd/3")
        .unwrap();
    let prepare = read_frame(&mut link).unwrap();
    fs::write(
        dir.join("prepare.json"),
        serde_json::to_vec(&prepare).unwrap(),
    )
    .unwrap();
    if mode == "exit_during_prepare" {
        // Dies by itself while its prepare (the load) is in flight.
        std::process::exit(3);
    }
    if mode == "grow_disk" {
        // Accepts the prepare, then grows the custody folder (a sparse file:
        // its length counts, as v0.1.4's st_size) and never answers the proof.
        write_frame(&mut link, &json!({"ok":true}));
        let _proof = read_frame(&mut link);
        let file = fs::File::create(grown_path(&prepare)).unwrap();
        file.set_len(GROWN_BYTES).unwrap();
        while read_frame(&mut link).is_some() {}
        return;
    }
    if mode == "hog_during_prepare" {
        // Never answers the prepare: the breach lands while it is in flight.
        hog();
        while read_frame(&mut link).is_some() {}
        return;
    }
    write_frame(&mut link, &json!({"ok":true}));
    let _proof = read_frame(&mut link);
    if matches!(
        mode.as_str(),
        "refuse_proof" | "refuse_close" | "accept_close"
    ) {
        write_frame(&mut link, &json!({"ok":false}));
        if mode == "refuse_proof" {
            while read_frame(&mut link).is_some() {}
            return;
        }
        // The proof refusal left the worker owned; answer its close request.
        let Some(close) = read_frame(&mut link) else {
            return;
        };
        assert_eq!(close, json!({"command":"close"}), "{mode} got {close}");
        if mode == "refuse_close" {
            write_frame(&mut link, &json!({"ok":false}));
            while read_frame(&mut link).is_some() {}
            return;
        }
        write_frame(
            &mut link,
            &json!({"ok":true,"stats":{"active_bytes":0,"reserved_memory_bytes":0,
                "context_closed":true}}),
        );
        // Exits 0 after a graceful close; the owner observes the real exit.
        return;
    }
    hog();
    // Hold until the guard or the owner kills this worker, or the link closes.
    while read_frame(&mut link).is_some() {}
}

fn fake_dir() -> PathBuf {
    PathBuf::from(std::env::var_os("MGF_FAKE_DIR").unwrap())
}
fn set_mode(mode: &str) {
    let prepared = fake_dir().join("prepare.json");
    if prepared.exists() {
        fs::remove_file(prepared).unwrap();
    }
    fs::write(fake_dir().join("mode"), mode).unwrap();
}
/// The `memory` of the prepare document the fake worker actually received.
fn prepared_memory() -> u64 {
    let prepare: Value =
        serde_json::from_slice(&fs::read(fake_dir().join("prepare.json")).unwrap()).unwrap();
    prepare["memory"].as_u64().unwrap()
}
impl Pair {
    /// Received, verified custody of the fixture shard for `transfer_id`.
    fn custody(&self, transfer_id: &str) -> joined_shard::native::NativeShard {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../planner/tests/fixtures/source-slices.json"
        ))
        .unwrap();
        let case = &fixture["cases"][0];
        let name = case["node_files"]["node-0"][0]["path"]
            .as_str()
            .unwrap()
            .strip_prefix("deployment/")
            .unwrap();
        let encoded = case["shard_files"][name].as_str().unwrap();
        let bytes: Vec<u8> = (0..encoded.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&encoded[i..i + 2], 16).unwrap())
            .collect();
        let mut shard = joined_shard::JoinedShard::begin(
            &self.member_root,
            &self.session,
            &self.signer,
            &self.carrier,
            &self.offer_for(transfer_id),
            app_host::now(),
        )
        .unwrap();
        shard
            .write_chunk(
                0,
                &bytes,
                &self.session,
                &self.signer,
                &self.carrier,
                app_host::now(),
            )
            .unwrap();
        shard
            .verify(&self.session, &self.signer, &self.carrier, &app_host::now)
            .unwrap();
        joined_shard::native::NativeShard::new(shard).unwrap()
    }
    /// The production prepare path with a large prepare argument (`TOKENS`).
    fn prepare(
        &self,
        native: &mut joined_shard::native::NativeShard,
    ) -> Result<Value, &'static str> {
        let current = || {
            self.session
                .shard_identity(&self.signer, app_host::now())
                .map_err(|_| api::compiler_inputs::Error("test_member_invalid"))
        };
        native
            .prepare_until(
                [0, 14],
                TOKENS,
                NONCE,
                &self.signer,
                &current,
                std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false)),
                &self.root.join("source"),
                std::time::Instant::now() + bound(),
            )
            .map_err(|e| e.0)
    }
}
/// The guard's verdict numbers once it stopped the worker.
fn guarded_limit(native: &joined_shard::native::NativeShard) -> Option<u64> {
    let failure = native.worker_failure()?;
    assert_eq!(failure["code"], "worker_memory_limit_exceeded");
    let detail = &failure["limit_detail"];
    let (observed, limit) = (
        detail["observed_group_rss_bytes"].as_u64()?,
        detail["max_rss_bytes"].as_u64()?,
    );
    assert!(observed > limit);
    assert_eq!(detail["overshoot_bytes"], observed - limit);
    assert_eq!(detail.as_object().map(serde_json::Map::len), Some(3));
    Some(limit)
}

/// R1 (REVIEW-laneMG): the memory the worker is prepared with (the prepare
/// document it receives) and the limit its guard compares against are the
/// member's own consent cap, tightened only by `limit_memory_to`; a larger
/// prepare argument (`TOKENS`) or a larger owner share changes nothing.
/// v0.1.4 `standing_limits` (standing_consent.py:230-261) -> `WorkerLimits`
/// -> `ManagedWorker._check` (worker.py:845-860).
#[test]
fn the_worker_is_prepared_and_guarded_at_the_members_own_cap() {
    run_child("prepared_and_guarded_child");
}
#[test]
#[ignore = "child of the_worker_is_prepared_and_guarded_at_the_members_own_cap"]
fn prepared_and_guarded_child() {
    // The consent cap alone; an owner share above it is refused and loosens nothing.
    let p = Pair::with_cap(SMALL_CAP);
    let mut native = p.custody("mgf-cap");
    assert_eq!(
        native.limit_memory_to(SMALL_CAP * 4).map_err(|e| e.0),
        Err("member_memory_cap_below_share")
    );
    set_mode("hog_after_reply");
    assert_eq!(p.prepare(&mut native), Err("worker_memory_limit_exceeded"));
    assert_eq!(prepared_memory(), SMALL_CAP, "the prepared memory");
    assert_eq!(guarded_limit(&native), Some(SMALL_CAP), "the guard's limit");
    native.abort().unwrap();
    native.discard().unwrap();
    // A tighter share tightens both; a later larger one does not raise them.
    let p = Pair::with_cap(SMALL_CAP);
    let mut native = p.custody("mgf-share");
    let share = SMALL_CAP / 2;
    assert_eq!(native.limit_memory_to(share).map_err(|e| e.0), Ok(share));
    assert_eq!(
        native.limit_memory_to(SMALL_CAP).map_err(|e| e.0),
        Ok(share)
    );
    set_mode("hog_after_reply");
    assert_eq!(p.prepare(&mut native), Err("worker_memory_limit_exceeded"));
    assert_eq!(prepared_memory(), share, "the prepared memory");
    assert_eq!(guarded_limit(&native), Some(share), "the guard's limit");
    native.abort().unwrap();
    native.discard().unwrap();
}

/// F2 (REVIEW-laneMG): after the guard killed a worker, the same run cannot
/// prepare that member again; a new run (a new offer, new custody) can.
/// v0.1.4: the dead worker's runtime is `blocked`/`runtime_cold_restart_required`
/// once load was admitted (managed_runtime.py:279-345, worker.py:1549-1552,1368),
/// the runtime stays attached (`runtime_already_attached`, target_app.py:192)
/// and the handled offer is never re-accepted (standing_consent.py:420-421,465-466)
/// until `stop_control` (target_app.py:280-284) and a NEW offer.
#[test]
fn after_a_memory_kill_the_same_run_cannot_prepare_again_until_a_new_offer() {
    run_child("same_run_blocked_child");
}
#[test]
#[ignore = "child of after_a_memory_kill_the_same_run_cannot_prepare_again_until_a_new_offer"]
fn same_run_blocked_child() {
    let p = Pair::with_cap(SMALL_CAP);
    let mut native = p.custody("mgf-run-1");
    set_mode("hog_after_reply");
    assert_eq!(p.prepare(&mut native), Err("worker_memory_limit_exceeded"));
    native.abort().unwrap();
    let killed = native.worker_failure();
    assert_eq!(guarded_limit(&native), Some(SMALL_CAP));
    // The same run: refused before any worker is launched, the verdict kept.
    set_mode("refuse_proof");
    assert_eq!(
        p.prepare(&mut native),
        Err("runtime_cold_restart_required"),
        "a fresh prepare after a memory-limit kill"
    );
    assert!(
        !fake_dir().join("prepare.json").exists(),
        "no worker was launched"
    );
    assert_eq!(native.worker_pid(), None);
    assert_eq!(native.worker_failure(), killed);
    native.discard().unwrap();
    // A new run (new offer, new custody) on the same member prepares again.
    let mut next = p.custody("mgf-run-2");
    set_mode("refuse_proof");
    assert_eq!(p.prepare(&mut next), Err("native_proof_binding_mismatch"));
    assert_eq!(
        prepared_memory(),
        SMALL_CAP,
        "the new run's worker was prepared"
    );
    assert_eq!(next.worker_failure(), None);
    next.abort().unwrap();
    next.discard().unwrap();
}

/// F4 (REVIEW-laneMG): a kill that lands DURING prepare (weights load, the
/// likeliest moment) keeps the guard's three `limit_detail` numbers, as a kill
/// after prepare does. v0.1.4 records code + detail for any breach
/// (worker.py:845-860, `_watch` 1236-1270).
#[test]
fn a_kill_during_prepare_keeps_the_guard_numbers() {
    run_child("kill_during_prepare_child");
}
#[test]
#[ignore = "child of a_kill_during_prepare_keeps_the_guard_numbers"]
fn kill_during_prepare_child() {
    let p = Pair::with_cap(SMALL_CAP);
    let mut native = p.custody("mgf-during");
    set_mode("hog_during_prepare");
    assert_eq!(p.prepare(&mut native), Err("worker_memory_limit_exceeded"));
    assert_eq!(prepared_memory(), SMALL_CAP);
    assert_eq!(
        native.worker_pid(),
        None,
        "the failed start reaped its child"
    );
    assert_eq!(
        guarded_limit(&native),
        Some(SMALL_CAP),
        "the numbers of a kill during prepare"
    );
    native.discard().unwrap();
}

// ---- Lane MG2 item 1: the storage the member consented to ----

/// The smallest storage a member can consent to (protocol consent `storage_bytes`).
const STORAGE_CAP: u64 = 100_000_000;
/// The `grow_disk` fake's sparse file: above `STORAGE_CAP` and every share below it.
const GROWN_BYTES: u64 = 150_000_000;

/// The guard's storage verdict numbers once it stopped the worker.
fn guarded_storage(native: &joined_shard::native::NativeShard) -> Option<u64> {
    let failure = native.worker_failure()?;
    assert_eq!(failure["code"], "worker_disk_limit_exceeded");
    let detail = &failure["limit_detail"];
    let (observed, limit) = (
        detail["observed_disk_bytes"].as_u64()?,
        detail["max_disk_bytes"].as_u64()?,
    );
    assert!(observed > limit);
    assert_eq!(detail["overshoot_bytes"], observed - limit);
    assert_eq!(detail.as_object().map(serde_json::Map::len), Some(3));
    Some(limit)
}
/// Remove what the fake grew, so the custody can be discarded.
fn remove_grown() {
    let prepare: Value =
        serde_json::from_slice(&fs::read(fake_dir().join("prepare.json")).unwrap()).unwrap();
    fs::remove_file(grown_path(&prepare)).unwrap();
}

/// MG2 item 1: the production start path (`NativeShard::prepare_until` ->
/// `Worker::start_scoped_until`) walks the custody's staging folder and stops a
/// worker that grows it past the member's OWN consent `storage_bytes`, or past
/// a tighter owner share; a share above the cap is refused and loosens nothing.
/// v0.1.4: `standing_limits` (standing_consent.py:241,252-260) -> `WorkerLimits`
/// -> `ManagedWorker._check` (worker.py:820-844) on `staging_root`
/// (managed_runtime.py:229).
#[test]
fn the_custody_folder_is_guarded_at_the_members_own_storage_cap() {
    run_child("storage_guarded_child");
}
#[test]
#[ignore = "child of the_custody_folder_is_guarded_at_the_members_own_storage_cap"]
fn storage_guarded_child() {
    let p = Pair::with_caps(CAP, STORAGE_CAP);
    let mut native = p.custody("mg2-disk");
    assert_eq!(native.storage_limit().map_err(|e| e.0), Ok(STORAGE_CAP));
    set_mode("grow_disk");
    assert_eq!(p.prepare(&mut native), Err("worker_disk_limit_exceeded"));
    assert_eq!(
        guarded_storage(&native),
        Some(STORAGE_CAP),
        "the guard's storage limit"
    );
    remove_grown();
    native.abort().unwrap();
    native.discard().unwrap();
    // A share above the cap is refused and loosens nothing; a tighter share
    // tightens the guard; a later larger one does not raise it.
    let mut native = p.custody("mg2-disk-share");
    assert_eq!(
        native.limit_storage_to(STORAGE_CAP + 1).map_err(|e| e.0),
        Err("member_storage_cap_below_share")
    );
    assert_eq!(
        native.limit_storage_to(0).map_err(|e| e.0),
        Err("member_resources_invalid")
    );
    assert_eq!(native.storage_limit().map_err(|e| e.0), Ok(STORAGE_CAP));
    let share = STORAGE_CAP / 2;
    assert_eq!(native.limit_storage_to(share).map_err(|e| e.0), Ok(share));
    assert_eq!(
        native.limit_storage_to(STORAGE_CAP).map_err(|e| e.0),
        Ok(share)
    );
    assert_eq!(native.storage_limit().map_err(|e| e.0), Ok(share));
    set_mode("grow_disk");
    assert_eq!(p.prepare(&mut native), Err("worker_disk_limit_exceeded"));
    assert_eq!(
        guarded_storage(&native),
        Some(share),
        "the guard's storage limit"
    );
    remove_grown();
    native.abort().unwrap();
    native.discard().unwrap();
}

// ---- Lane MG2 item 2: any worker death once the load began blocks the run ----

const NATIVE_EXITED: &str = "worker_native_exited";

/// Signal this process's OWN worker child (the pid its spawn returned, via
/// `worker_pid`) and wait, without reaping it, until it has exited. The
/// guard thread may reap it first; that also means it is gone.
fn kill_own_worker(pid: u32) {
    use rustix::process::{Pid, Signal, WaitId, WaitIdOptions, kill_process, waitid};
    let pid = Pid::from_raw(i32::try_from(pid).unwrap()).unwrap();
    kill_process(pid, Signal::KILL).unwrap();
    match waitid(
        WaitId::Pid(pid),
        WaitIdOptions::EXITED | WaitIdOptions::NOWAIT,
    ) {
        Ok(status) => assert!(status.is_some(), "the worker has exited"),
        Err(e) => assert_eq!(e, rustix::io::Errno::CHILD, "already reaped by its guard"),
    }
}

/// MG2 item 2 (REVIEW-laneMGF F2 follow-up): a worker that dies for any reason
/// after its prepare (the load) leaves this run blocked on this member until a
/// new offer with new custody; the owner's own deliberate abort of a live
/// worker (an interrupted epoch) does not. v0.1.4 (DemoApp app.py:505 ->
/// `auto_accept` 1175 -> `_start_offer` target_app.py:189-238 -> `_serve`
/// managed_runtime.py:447-455 -> `_replace_worker_if_dead` 279-345): a dead
/// worker whose `requires_cold_restart()` (configure dispatched or load
/// admitted, worker.py:1171,1368,1549-1552) puts the runtime in `blocked`/
/// `runtime_cold_restart_required`, never a replacement; `_watch` records
/// `worker_native_exited` (worker.py:1462-1464).
#[test]
fn a_worker_killed_after_prepare_blocks_the_same_run_until_a_new_offer() {
    run_child("killed_after_prepare_child");
}
#[test]
#[ignore = "child of a_worker_killed_after_prepare_blocks_the_same_run_until_a_new_offer"]
fn killed_after_prepare_child() {
    let p = Pair::with_cap(SMALL_CAP);
    let mut native = p.custody("mg2-death");
    set_mode("refuse_proof");
    assert_eq!(p.prepare(&mut native), Err("native_proof_binding_mismatch"));
    assert_eq!(prepared_memory(), SMALL_CAP, "the prepare completed");
    kill_own_worker(native.worker_pid().expect("the prepared worker is owned"));
    assert_eq!(
        native.abort().unwrap()["killed"],
        false,
        "it died before abort"
    );
    set_mode("refuse_proof");
    assert_eq!(
        p.prepare(&mut native),
        Err("runtime_cold_restart_required"),
        "a fresh prepare after the prepared worker died"
    );
    assert!(
        !fake_dir().join("prepare.json").exists(),
        "no worker was launched"
    );
    assert_eq!(
        native.worker_failure(),
        Some(json!({"code":NATIVE_EXITED,"limit_detail":null}))
    );
    native.discard().unwrap();
    // A new run (new offer, new custody) prepares; the owner's own abort of
    // its live worker (an interrupted epoch) does not block it.
    let mut next = p.custody("mg2-death-next");
    set_mode("refuse_proof");
    assert_eq!(p.prepare(&mut next), Err("native_proof_binding_mismatch"));
    assert_eq!(next.abort().unwrap()["killed"], true);
    assert_eq!(next.worker_failure(), None);
    set_mode("refuse_proof");
    assert_eq!(p.prepare(&mut next), Err("native_proof_binding_mismatch"));
    assert_eq!(
        prepared_memory(),
        SMALL_CAP,
        "prepared again after a deliberate abort"
    );
    next.abort().unwrap();
    next.discard().unwrap();
}

/// MG2 item 2: v0.2's prepare IS the load (v0.1.4 `configure`), dispatched
/// as soon as the worker is spawned, so a worker that dies while it is in
/// flight blocks the run as well (v0.1.4 sets `_configure_dispatched` before
/// sending, worker.py:1171: "even if its reply was lost").
#[test]
fn a_worker_that_dies_during_prepare_blocks_the_same_run() {
    run_child("dies_during_prepare_child");
}
#[test]
#[ignore = "child of a_worker_that_dies_during_prepare_blocks_the_same_run"]
fn dies_during_prepare_child() {
    let p = Pair::with_cap(SMALL_CAP);
    let mut native = p.custody("mg2-dies");
    set_mode("exit_during_prepare");
    assert_eq!(p.prepare(&mut native), Err("native_worker_exchange_failed"));
    assert_eq!(
        native.worker_pid(),
        None,
        "the failed start reaped its child"
    );
    set_mode("refuse_proof");
    assert_eq!(
        p.prepare(&mut native),
        Err("runtime_cold_restart_required"),
        "a fresh prepare after the worker died during prepare"
    );
    assert!(
        !fake_dir().join("prepare.json").exists(),
        "no worker was launched"
    );
    assert_eq!(
        native.worker_failure(),
        Some(json!({"code":NATIVE_EXITED,"limit_detail":null}))
    );
    native.discard().unwrap();
}

// ---- NATIVE-F1: the close path through NativeShard, with a real child ----

/// NATIVE-F1: a graceful close that the peer refuses fails explicitly and keeps
/// the exact fake child owned; the established abort escalation (the `stop(false)`
/// fallthrough) kills and reaps that same child, and only then may the custody
/// be discarded. The fake is NOT MLX computation.
#[test]
fn a_failed_native_close_escalates_to_the_exact_owned_child() {
    run_child("failed_close_child");
}
#[test]
#[ignore = "child of a_failed_native_close_escalates_to_the_exact_owned_child"]
fn failed_close_child() {
    let p = Pair::with_cap(SMALL_CAP);
    let mut native = p.custody("f1-close");
    set_mode("refuse_close");
    // The proof refusal leaves the prepared worker owned; no model is loaded.
    assert_eq!(p.prepare(&mut native), Err("native_proof_binding_mismatch"));
    let pid = native.worker_pid().expect("the prepared worker is owned");
    assert_eq!(
        native.close().map_err(|e| e.0),
        Err("native_cleanup_unproven"),
        "a refused close is explicit cleanup-unproven"
    );
    assert_eq!(
        native.worker_pid(),
        Some(pid),
        "the exact child stays owned for abort"
    );
    assert_eq!(
        native.discard().map_err(|e| e.0),
        Err("joined_native_cleanup_required"),
        "no replacement/discard while release is unproven"
    );
    let evidence = native.abort().unwrap();
    assert_eq!(evidence["worker_pid"], pid);
    assert_eq!(evidence["killed"], true);
    assert_eq!(native.worker_pid(), None);
    assert_eq!(native.worker_failure(), None);
    native.discard().unwrap();
}

/// NATIVE-F1: a graceful close under the consent end reaps the exact fake child
/// that ACKs and exits 0, and proves cleanup so the custody can be retired.
#[test]
fn a_graceful_native_close_reaps_the_exact_fake_child() {
    run_child("graceful_close_child");
}
#[test]
#[ignore = "child of a_graceful_native_close_reaps_the_exact_fake_child"]
fn graceful_close_child() {
    let p = Pair::with_cap(SMALL_CAP);
    let mut native = p.custody("f1-close-ok");
    set_mode("accept_close");
    assert_eq!(p.prepare(&mut native), Err("native_proof_binding_mismatch"));
    assert!(native.worker_pid().is_some());
    let stats = native.close().unwrap();
    assert_eq!(stats["context_closed"], true);
    assert_eq!(native.worker_pid(), None);
    assert_eq!(native.worker_failure(), None);
    native.discard().unwrap();
}
