// SPDX-License-Identifier: AGPL-3.0-only
//! Real signed SQLite mailbox -> API pump -> admitted model-free Rust child.
//! No model, public remote peer, or multi-physical-host claim.
use api::{
    bootstrap_process::TrustedBootstrap,
    bootstrap_stage::BootstrapStage,
    managed_carrier::{HostClock, TargetFence},
    target_pump::{TargetHost, TargetPump},
};
use budgets::{BudgetId as B, Entries, MonoInstant};
use control_runtime::{
    Store,
    mailbox::{Limits, Mailbox, StagePolicy, StagePublication},
};
use protocol::identity::Identity;
use serde_json::{Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
};
use transport::Clock;
#[path = "support/target_replay.rs"]
mod replay_tests;
static NONCE: AtomicU64 = AtomicU64::new(0);
fn nonce() -> String {
    format!("nonce-{}", NONCE.fetch_add(1, Ordering::Relaxed))
}
fn owner() -> Identity {
    Identity::from_secret("owner-endpoint", &[31; 32]).unwrap()
}
fn member() -> Identity {
    Identity::from_secret("target-endpoint", &[32; 32]).unwrap()
}
fn scope() -> Value {
    json!({"target_node_id":"target","target_endpoint_id":member().endpoint_id(),"target_key_digest":member().verification_key_digest(),"membership_generation":1,"member_incarnation":"incarnation","run_id":"run","deployment_id":"deployment"})
}
fn signed(s: Value, who: &Identity) -> Value {
    json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL,"signature":who.sign(&s).unwrap(),"verification_key":who.public_key_record(),"statement":s})
}
fn request(kind: &str, seq: u64, extra: Value, who: &Identity, clock: &HostClock) -> Value {
    let now = clock.wall_seconds();
    let mut s = scope();
    let ttl = B::ControlTtl.span(Entries::ZERO).unwrap().millis() as f64 / 1000.0;
    for(k,v)in json!({"protocol":kind,"swarm_id":"swarm","sequence":seq,"nonce":nonce(),"request_id":nonce(),"issued_at":now,"expires_at":now+ttl}).as_object().unwrap(){s[k]=v.clone();}
    for (k, v) in extra.as_object().unwrap() {
        s[k] = v.clone();
    }
    signed(s, who)
}
type ExchangeFault = (&'static str, bool, &'static str);
#[derive(Clone)]
struct Host {
    mailbox: Arc<Mutex<Mailbox>>,
    clock: HostClock,
    signer: Arc<Identity>,
    current: Arc<AtomicBool>,
    revoke_after_reply: Arc<AtomicBool>,
    checks: Arc<AtomicU64>,
    fail_check_at: u64,
    faults: Arc<Mutex<std::collections::VecDeque<ExchangeFault>>>,
    requests: Arc<Mutex<Vec<Vec<u8>>>>,
    wall_advance: Arc<AtomicU64>,
    cancel_after_reply: Arc<AtomicBool>,
    carrier: Option<Arc<api::managed_carrier::LocalMailboxCarrier>>,
}
impl Clock for Host {
    fn monotonic(&self) -> MonoInstant {
        self.clock.monotonic()
    }
    fn wall_seconds(&self) -> f64 {
        self.clock.wall_seconds() + self.wall_advance.load(Ordering::Acquire) as f64
    }
}
impl TargetHost for Host {
    fn check(&self, b: &Value) -> Result<(), &'static str> {
        let check = self.checks.fetch_add(1, Ordering::AcqRel) + 1;
        if check != self.fail_check_at && *b == scope() && self.current.load(Ordering::Acquire) {
            Ok(())
        } else {
            Err("managed_control_member_stale")
        }
    }
    fn signer(&self) -> &Identity {
        &self.signer
    }
    fn owner_key(&self) -> Value {
        owner().public_key_record()
    }
    fn owner_node(&self) -> String {
        "owner".into()
    }
    fn swarm(&self) -> String {
        "swarm".into()
    }
    fn nonce(&self) -> Result<String, &'static str> {
        Ok(nonce())
    }
    fn exchange(
        &self,
        e: &Value,
        end: MonoInstant,
        f: &TargetFence,
    ) -> Result<Value, &'static str> {
        f.check().map_err(|e| e.0)?;
        if let Some(carrier) = &self.carrier {
            let bytes = carrier
                .call_target(self, &protocol::canonical::to_bytes(e).unwrap(), end, f)
                .map_err(|e| e.0)?;
            return serde_json::from_slice(&bytes).map_err(|_| "managed_control_response_invalid");
        }
        let encoded = protocol::canonical::to_bytes(e).unwrap();
        self.requests.lock().unwrap().push(encoded.clone());
        let operation = if e["statement"]["protocol"] == "mycelium.managed_control.target_poll.v1" {
            "poll"
        } else {
            "ack"
        };
        let fault = {
            let mut faults = self.faults.lock().unwrap();
            if faults.front().is_some_and(|(op, _, _)| *op == operation) {
                faults.pop_front()
            } else {
                None
            }
        };
        if let Some((_, false, code)) = fault {
            return Err(code);
        }
        let bytes = self
            .mailbox
            .lock()
            .unwrap()
            .dispatch(&encoded, || self.wall_seconds(), || Ok(nonce()))
            .map_err(|e| match e {
                control_runtime::Error::Refused(code) => code,
                _ => "test_mailbox_refused",
            })?;
        if self.cancel_after_reply.load(Ordering::Acquire) {
            f.cancel().unwrap();
        }
        if let Some((_, true, code)) = fault {
            if code == "corrupt_response_signature" {
                let mut response: Value = serde_json::from_slice(&bytes).unwrap();
                response["statement"]["request_id"] = "tampered".into();
                return Ok(response);
            }
            match code {
                "revoke_after_commit" => self.current.store(false, Ordering::Release),
                "cancel_after_commit" => f.cancel().unwrap(),
                _ => return Err(code),
            }
        }
        if self.revoke_after_reply.load(Ordering::Acquire) {
            self.current.store(false, Ordering::Release);
        }
        serde_json::from_slice(&bytes).map_err(|_| "test_invalid_response")
    }
    fn stage_ack(&self, offer: &Value, root: &Path, removed: bool) -> Result<(), &'static str> {
        let ack = if removed {
            json!({"protocol":"mycelium.controller_remote_cleanup_ack.v1","node_id":"target","staging_root":root,"removed":true})
        } else {
            json!({"protocol":"mycelium.controller_remote_stage_ack.v1","node_id":"target","staging_root":root,"archive_digest":offer["archive_digest"],"archive_size_bytes":offer["archive_bytes"]})
        };
        let mut extra = json!({"run_id":"stage-discovery","deployment_id":"stage-discovery","offer_id":offer["offer_id"],"ack":ack});
        if !removed {
            extra["offset"] = 0.into();
        }
        let kind = if removed {
            "mycelium.managed_stage_cleanup_ack.v1"
        } else {
            "mycelium.managed_stage_ack.v1"
        };
        let e = request(kind, 1, extra, &self.signer, &self.clock);
        let bytes = self
            .mailbox
            .lock()
            .unwrap()
            .dispatch_stage(&protocol::canonical::to_bytes(&e).unwrap(), || {
                self.wall_seconds()
            })
            .map_err(|_| "test_stage_refused")?;
        let reply: Value = serde_json::from_slice(&bytes).unwrap();
        protocol::identity::verify(
            &reply["statement"],
            &reply["signature"],
            &owner().public_key_record(),
        )
        .unwrap();
        assert_eq!(
            reply["statement"]["request_id"],
            e["statement"]["request_id"]
        );
        assert_eq!(reply["statement"]["offer"], *offer);
        Ok(())
    }
}
#[test]
fn stage_rejects_changed_archive_offer_and_each_scope_field_before_creation() {
    let f = Fixture::new();
    let path = f.root.join("refused-stage");
    let mut archive = f.archive.clone();
    archive[0] ^= 1;
    assert!(BootstrapStage::create(&path, &scope(), &f.offer, &archive).is_err());
    let mut offer = f.offer.clone();
    offer["archive_digest"] = "changed".into();
    assert!(BootstrapStage::create(&path, &scope(), &offer, &f.archive).is_err());
    for key in scope().as_object().unwrap().keys() {
        let mut changed = scope();
        changed[key] = "changed".into();
        assert!(
            BootstrapStage::create(&path, &changed, &f.offer, &f.archive).is_err(),
            "scope field {key}"
        );
    }
    assert!(!path.exists());
}
#[test]
fn staged_payload_tampering_is_refused_and_owned_files_still_cleaned() {
    let f = Fixture::new();
    let path = f.root.join("stage");
    let mut stage = BootstrapStage::create(&path, &scope(), &f.offer, &f.archive).unwrap();
    fs::write(path.join("bootstrap.json"), b"tampered").unwrap();
    assert_eq!(stage.verify(), Err("bootstrap_stage_changed"));
    stage.remove().unwrap();
    assert!(!path.exists());
}
#[test]
fn stage_cleanup_cannot_delete_replacement_inode() {
    let f = Fixture::new();
    let path = f.root.join("stage");
    let mut stage = BootstrapStage::create(&path, &scope(), &f.offer, &f.archive).unwrap();
    let file = path.join("bootstrap.json");
    let held = f.root.join("retained-owned-file");
    fs::rename(&file, &held).unwrap();
    fs::write(&file, b"unowned replacement").unwrap();
    assert_eq!(stage.remove(), Err("bootstrap_stage_identity_changed"));
    assert_eq!(fs::read(&file).unwrap(), b"unowned replacement");
    fs::remove_file(&file).unwrap();
    fs::rename(&held, &file).unwrap();
    stage.remove().unwrap();
    assert!(!path.exists());
}
// Exercise real unlink + parent-open failure, not an injected cleanup result.
// The separate parent leaves the pump journal and SQLite mailbox accessible.
struct StageParent(PathBuf);
impl StageParent {
    fn new(root: &Path) -> Self {
        use std::os::unix::fs::DirBuilderExt;
        let path = root.join("stage-parent");
        fs::DirBuilder::new().mode(0o700).create(&path).unwrap();
        Self(path)
    }
    fn path(&self) -> PathBuf {
        self.0.join("stage")
    }
    fn mode(&self, mode: u32) {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&self.0, fs::Permissions::from_mode(mode)).unwrap();
    }
    fn deny_durability_open(&self) {
        self.mode(0o300);
        assert_eq!(
            fs::File::open(&self.0).unwrap_err().kind(),
            std::io::ErrorKind::PermissionDenied,
            "fixture must actually deny the production durability open"
        );
    }
}
impl Drop for StageParent {
    fn drop(&mut self) {
        use std::os::unix::fs::PermissionsExt;
        if let Err(error) = fs::set_permissions(&self.0, fs::Permissions::from_mode(0o700)) {
            eprintln!("test stage parent restore: {error}");
        }
    }
}
fn assert_absent(path: &Path) {
    assert_eq!(
        fs::symlink_metadata(path).unwrap_err().kind(),
        std::io::ErrorKind::NotFound
    );
}
#[test]
fn stage_durability_healthy_cleanup_is_idempotent() {
    let f = Fixture::new();
    let parent = StageParent::new(&f.root);
    let path = parent.path();
    let mut stage = BootstrapStage::create(&path, &scope(), &f.offer, &f.archive).unwrap();
    stage.verify().unwrap();
    stage.remove().unwrap();
    assert_absent(&path);
    stage.remove().unwrap();
    drop(stage);
    assert_absent(&path);
}
#[test]
fn stage_durability_retry_after_real_parent_open_refusal() {
    let f = Fixture::new();
    let parent = StageParent::new(&f.root);
    let path = parent.path();
    let mut stage = BootstrapStage::create(&path, &scope(), &f.offer, &f.archive).unwrap();
    parent.deny_durability_open();
    assert_eq!(stage.remove(), Err("bootstrap_stage_cleanup_unproven"));
    assert_absent(&path);
    assert_eq!(stage.remove(), Err("bootstrap_stage_cleanup_unproven"));
    parent.mode(0o700);
    assert_eq!(stage.remove(), Ok(()), "same-owner durability retry");
    stage.remove().unwrap();
    assert_absent(&path);
}
#[test]
fn stage_durability_retry_preserves_foreign_replacements() {
    use std::os::unix::fs::symlink;
    for replacement in ["directory", "file", "symlink"] {
        let f = Fixture::new();
        let parent = StageParent::new(&f.root);
        let path = parent.path();
        let mut stage = BootstrapStage::create(&path, &scope(), &f.offer, &f.archive).unwrap();
        parent.deny_durability_open();
        assert_eq!(stage.remove(), Err("bootstrap_stage_cleanup_unproven"));
        assert_absent(&path);
        parent.mode(0o700);
        let marker = match replacement {
            "directory" => {
                fs::create_dir(&path).unwrap();
                let marker = path.join("foreign-marker");
                fs::write(&marker, b"foreign").unwrap();
                marker
            }
            "file" => {
                fs::write(&path, b"foreign").unwrap();
                path.clone()
            }
            "symlink" => {
                let marker = f.root.join("foreign-marker");
                fs::write(&marker, b"foreign").unwrap();
                symlink(&marker, &path).unwrap();
                marker
            }
            _ => unreachable!(),
        };
        assert_eq!(stage.remove(), Err("bootstrap_stage_identity_changed"));
        assert_eq!(fs::read(&marker).unwrap(), b"foreign");
        drop(stage);
        assert_eq!(fs::read(&marker).unwrap(), b"foreign");
        assert!(fs::symlink_metadata(&path).is_ok());
    }
}
fn assert_cleanup_ack_absent(f: &Fixture) {
    let db = rusqlite::Connection::open(f.root.join("mailbox.sqlite")).unwrap();
    let (requested, ack): (bool, Option<String>) = db
        .query_row(
            "SELECT cleanup_requested,cleanup_ack FROM managed_stage_offers",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert!(requested);
    assert!(
        ack.is_none(),
        "removal progress must not become signed cleanup ACK"
    );
    assert!(
        f.host
            .mailbox
            .lock()
            .unwrap()
            .retire_stage(&f.offer, || f.host.wall_seconds())
            .is_err()
    );
    let state: Value =
        serde_json::from_slice(&fs::read(f.root.join("state.json")).unwrap()).unwrap();
    assert_eq!(state["phase"], "cleanup_unproven");
}
#[test]
#[ignore = "requires pinned real model-free helper"]
fn pump_stage_durability_retry_gates_signed_cleanup_ack() {
    let f = Fixture::new();
    let parent = StageParent::new(&f.root);
    let path = parent.path();
    let mut pump = f.try_pump_at(&path).unwrap();
    f.cleanup();
    parent.deny_durability_open();
    assert_eq!(pump.close(), Err("bootstrap_stage_cleanup_unproven"));
    assert_absent(&path);
    assert_cleanup_ack_absent(&f);
    parent.mode(0o700);
    pump.close().unwrap();
    pump.close().unwrap();
    let receipt = f
        .host
        .mailbox
        .lock()
        .unwrap()
        .retire_stage(&f.offer, || f.host.wall_seconds())
        .unwrap();
    assert_eq!(receipt["cleanup_ack"]["removed"], true);
    let state: Value =
        serde_json::from_slice(&fs::read(f.root.join("state.json")).unwrap()).unwrap();
    assert_eq!(state["phase"], "closed");
    assert_absent(&path);
}
#[test]
#[ignore = "requires pinned real model-free helper"]
fn pump_stage_durability_foreign_path_cannot_be_acknowledged() {
    let f = Fixture::new();
    let parent = StageParent::new(&f.root);
    let path = parent.path();
    let mut pump = f.try_pump_at(&path).unwrap();
    f.cleanup();
    parent.deny_durability_open();
    assert_eq!(pump.close(), Err("bootstrap_stage_cleanup_unproven"));
    assert_absent(&path);
    parent.mode(0o700);
    fs::create_dir(&path).unwrap();
    let marker = path.join("foreign-marker");
    fs::write(&marker, b"foreign").unwrap();
    assert_eq!(pump.close(), Err("bootstrap_stage_identity_changed"));
    assert_cleanup_ack_absent(&f);
    assert_eq!(fs::read(&marker).unwrap(), b"foreign");
    // Only the fixture removes its own replacement; the retained pump may then retry.
    fs::remove_file(&marker).unwrap();
    fs::remove_dir(&path).unwrap();
    pump.close().unwrap();
    let receipt = f
        .host
        .mailbox
        .lock()
        .unwrap()
        .retire_stage(&f.offer, || f.host.wall_seconds())
        .unwrap();
    assert_eq!(receipt["cleanup_ack"]["removed"], true);
}
struct Fixture {
    root: PathBuf,
    host: Host,
    offer: Value,
    archive: Vec<u8>,
}
impl Fixture {
    fn new() -> Self {
        use std::os::unix::fs::DirBuilderExt;
        let root = fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!("target-pump-{}-{}", std::process::id(), nonce()));
        fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
        let clock = HostClock::default();
        let mut m = Mailbox::new(
            Store::open(&root.join("mailbox.sqlite")).unwrap(),
            owner(),
            "swarm",
            "owner",
            Limits {
                pending: B::PyManagedControlMaxActiveControl.count().unwrap(),
                active_work: B::PyManagedControlMaxActiveWork.count().unwrap(),
                active_control: B::PyManagedControlMaxActiveControl.count().unwrap(),
            },
        )
        .unwrap()
        .with_stage_policy(StagePolicy {
            path_characters: B::PyStagePrepositionPathCharacters.count().unwrap(),
            preposition_file_bytes: B::PyUpperGuardMyceliumA13MacosManagedOffersL58C52
                .bytes()
                .unwrap(),
        })
        .unwrap();
        let ttl = B::ControlTtl.span(Entries::ZERO).unwrap();
        m.bind_member(
            &scope(),
            &member().public_key_record(),
            clock.wall_seconds() + ttl.millis() as f64 / 1000.0,
        )
        .unwrap();
        let archive = runtime::observe::bootstrap_archive("run", "deployment");
        let offer = m
            .publish_stage(
                StagePublication {
                    binding: &scope(),
                    id: "offer",
                    archive: &archive,
                    ttl,
                    preposition: None,
                },
                || clock.wall_seconds(),
            )
            .unwrap();
        let host = Host {
            mailbox: Arc::new(Mutex::new(m)),
            clock,
            signer: Arc::new(member()),
            current: Arc::new(AtomicBool::new(true)),
            revoke_after_reply: Arc::new(AtomicBool::new(false)),
            checks: Arc::new(AtomicU64::new(0)),
            fail_check_at: 0,
            faults: Arc::default(),
            requests: Arc::default(),
            wall_advance: Arc::default(),
            cancel_after_reply: Arc::default(),
            carrier: None,
        };
        Self {
            root,
            host,
            offer,
            archive,
        }
    }
    fn pump(&self) -> TargetPump<Host> {
        self.try_pump().unwrap()
    }
    fn try_pump(&self) -> Result<TargetPump<Host>, api::bootstrap_process::CleanupFailure> {
        self.try_pump_at(&self.root.join("stage"))
    }
    fn try_pump_at(
        &self,
        path: &Path,
    ) -> Result<TargetPump<Host>, api::bootstrap_process::CleanupFailure> {
        let v: Value = serde_json::from_slice(
            &fs::read(
                std::env::var("MYCELIUM_BOOTSTRAP_TEST_CONFIG").expect("explicit helper admission"),
            )
            .unwrap(),
        )
        .unwrap();
        let p = TrustedBootstrap::open(
            Path::new(v["path"].as_str().unwrap()),
            v["sha256"].as_str().unwrap(),
            v["source_sha"].as_str().unwrap(),
        )
        .unwrap();
        let stage = BootstrapStage::create(path, &scope(), &self.offer, &self.archive).unwrap();
        TargetPump::start(
            &self.root,
            self.host.clone(),
            scope(),
            self.offer.clone(),
            stage,
            &p,
            TargetFence::default(),
        )
    }
    fn enqueue(&self, seq: u64, op: &str) {
        let e = request(
            "mycelium.managed_control.owner_command.v1",
            seq,
            json!({"owner_node_id":"owner","native_command":{"protocol":protocol::native::CONTROL_PROTOCOL,"command_id":op,"run_id":"run","deployment_id":"deployment","command":op,"payload":{}}}),
            &owner(),
            &self.host.clock,
        );
        self.host
            .mailbox
            .lock()
            .unwrap()
            .dispatch(
                &protocol::canonical::to_bytes(&e).unwrap(),
                || self.host.wall_seconds(),
                || Ok(nonce()),
            )
            .unwrap();
    }
    fn cleanup(&self) {
        self.host
            .mailbox
            .lock()
            .unwrap()
            .request_stage_cleanup(&self.offer, || self.host.wall_seconds())
            .unwrap();
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Err(e) = fs::remove_dir_all(&self.root) {
            eprintln!("test root cleanup: {e}");
        }
    }
}
#[test]
#[ignore = "requires pinned real model-free helper"]
fn post_exec_currentness_failure_retains_pump_until_signed_cleanup_retry() {
    let mut f = Fixture::new();
    f.host.fail_check_at = 3;
    let mut failure = match f.try_pump() {
        Err(failure) => failure,
        Ok(_) => panic!("post-exec currentness failure accepted"),
    };
    assert_eq!(failure.code(), "managed_control_member_stale");
    assert!(failure.owner_retained());
    let state: Value =
        serde_json::from_slice(&fs::read(f.root.join("state.json")).unwrap()).unwrap();
    assert_eq!(state["phase"], "cleanup_unproven");
    assert!(state["process"]["pid"].is_number());
    assert!(failure.retry().is_err());
    assert!(failure.owner_retained());
    f.cleanup();
    failure.retry().unwrap();
    assert!(!failure.owner_retained());
    let state: Value =
        serde_json::from_slice(&fs::read(f.root.join("state.json")).unwrap()).unwrap();
    assert_eq!(state["phase"], "closed");
    let receipt = f
        .host
        .mailbox
        .lock()
        .unwrap()
        .retire_stage(&f.offer, || f.host.wall_seconds())
        .unwrap();
    assert_eq!(receipt["cleanup_ack"]["removed"], true);
}
#[test]
#[ignore = "requires pinned real model-free helper"]
fn signed_target_poll_hello_stop_and_stage_cleanup_are_real() {
    let f = Fixture::new();
    let mut pump = f.pump();
    f.enqueue(1, "hello");
    assert!(!pump.step().unwrap());
    let db = rusqlite::Connection::open(f.root.join("mailbox.sqlite")).unwrap();
    let native: Vec<u8> = db
        .query_row(
            "SELECT native FROM control_dispatch WHERE command_id='hello'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let native: Value = serde_json::from_slice(&native).unwrap();
    assert_eq!(native["result"]["state"], "NEW");
    assert_eq!(native["route_ready"], false);
    f.enqueue(2, "stop");
    assert!(pump.step().unwrap());
    f.cleanup();
    pump.close().unwrap();
    let receipt = f
        .host
        .mailbox
        .lock()
        .unwrap()
        .retire_stage(&f.offer, || f.host.wall_seconds())
        .unwrap();
    assert_eq!(receipt["cleanup_ack"]["removed"], true);
    assert!(!f.root.join("stage").exists());
    let state: Value =
        serde_json::from_slice(&fs::read(f.root.join("state.json")).unwrap()).unwrap();
    assert_eq!(state["phase"], "closed");
    assert_eq!(state["native_stop_receipt"]["result"]["state"], "STOPPED");
}
#[test]
#[ignore = "requires pinned real model-free helper"]
fn authenticated_poll_revocation_cannot_dispatch_late_native_work() {
    let f = Fixture::new();
    let mut pump = f.pump();
    f.enqueue(1, "hello");
    f.host.revoke_after_reply.store(true, Ordering::Release);
    assert_eq!(pump.step(), Err("managed_control_member_stale"));
    let state: Value =
        serde_json::from_slice(&fs::read(f.root.join("state.json")).unwrap()).unwrap();
    assert!(state["pending"].is_null());
    assert!(!state["outbound"].is_null());
    f.cleanup();
    pump.close().unwrap();
    let state: Value =
        serde_json::from_slice(&fs::read(f.root.join("state.json")).unwrap()).unwrap();
    assert!(state["native_stop_receipt"].is_null());
    assert_eq!(state["phase"], "closed");
}
