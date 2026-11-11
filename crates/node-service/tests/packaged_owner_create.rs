// SPDX-License-Identifier: AGPL-3.0-only
//! S1 create on the packaged executable: `POST /api/create` (app.py:1589-1622 @ f4e366e9) goes
//! through `PersistentBackend<PackagedHost, NodeService>` into the owner authority of
//! `api::app_host`, the same code the headless L3 launcher runs: the consent is persisted, the
//! swarm record and its seed are created, and this Mac joins its own swarm as member 1 over the
//! seed's pinned loopback TLS. The packaged process has no relay and no public origin.
//!
//! The answers are compared with what the real v0.1.4 packaged process answered at the same
//! public boundaries (`test-support/journeys/fixtures/python-v014-boundaries.json`).
#![cfg(feature = "desktop-shell")]
use api::schema::PublicError;
use budgets::{BudgetId as B, Entries};
use desktop::services::{ServiceRequest, Services};
use node_service::{
    desktop_host::DesktopServices,
    service::{Driver, Job, NodeService},
};
use serde_json::{Value, json};
use std::{
    io::Write,
    num::NonZeroUsize,
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};

/// v0.1.4's real packaged process over loopback HTTP: what it answered at each public boundary.
const PACKAGED_BOUNDARIES: &str =
    include_str!("../../../test-support/journeys/fixtures/python-v014-boundaries.json");
/// The owner's swarm record in the private state root (`api::app_host::SWARM_FILE`).
const SWARM_FILE: &str = "owner-swarm.json";
/// The owner's own membership directory (`authority/member`) and its retained pairing request.
const MEMBER_DIR: &str = "authority/member";
const PENDING_FILE: &str = "pairing.pending.json";

fn millis(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
struct MustNotRun;
impl Driver for MustNotRun {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        panic!("a desktop action started native work")
    }
}
fn private_root() -> PathBuf {
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "packaged-owner-create-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    root
}
/// The packaged composition (`desktop_host::compose` minus the native driver) over `root`.
fn compose(root: &Path) -> DesktopServices {
    let node = NodeService::new(
        Arc::new(MustNotRun),
        "private-local-token".into(),
        node_service::native::development_qualification().unwrap(),
    );
    DesktopServices::new(node, NonZeroUsize::new(4).unwrap(), root)
}
fn post(host: &DesktopServices, path: &str, body: Value) -> (u16, Value) {
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    host.submit(
        ServiceRequest {
            method: "POST".into(),
            target: path.into(),
            headers: vec![
                ("x-mycelium-action".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: serde_json::to_vec(&body).unwrap(),
        },
        Box::new(move |reply| tx.send(reply).unwrap()),
    );
    let reply = rx.recv_timeout(millis(B::WorkerStartup)).unwrap();
    (reply.status, serde_json::from_slice(&reply.body).unwrap())
}
fn refusal(reply: &(u16, Value)) -> (u16, &str) {
    (
        reply.0,
        reply.1["error"].as_str().unwrap_or("<not a refusal>"),
    )
}
/// The consent form's body as the v0.1.4 window sends it (`consent_valid`).
fn consent() -> Value {
    json!({
        "peer_visibility": true, "power": "ac_only",
        "storage_bytes": api::model_catalog::storage_consent_ceiling(),
        "network": "unmetered_only", "background": false, "diagnostics": false,
        "thermal": "stop_on_serious",
    })
}
fn create(host: &DesktopServices) -> (u16, Value) {
    post(host, "/api/create", json!({"consent": consent()}))
}
/// The v0.1.4 packaged process's answer at boundary `id`: same status, same fields.
fn assert_observed(id: &str, reply: &(u16, Value)) {
    let capture: Value = serde_json::from_str(PACKAGED_BOUNDARIES).unwrap();
    assert_eq!(
        capture["capture_kind"],
        "real_packaged_process_loopback_http"
    );
    let observed = capture["observations"]
        .as_array()
        .unwrap()
        .iter()
        .find(|o| o["id"] == id)
        .unwrap();
    assert_eq!(json!(reply.0), observed["status"], "{id}: {}", reply.1);
    for (pointer, value) in observed["fields"].as_object().unwrap() {
        assert_eq!(
            reply.1.pointer(pointer),
            Some(value),
            "{id} {pointer}: {}",
            reply.1
        );
    }
}
fn mode(path: &Path) -> u32 {
    std::fs::metadata(path).unwrap().mode() & 0o7777
}
fn private_json(path: &Path) -> Value {
    assert_eq!(mode(path), 0o600, "{}", path.display());
    serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
}
fn last_failure(root: &Path) -> (Value, Value) {
    let record = private_json(&root.join("last-action-error.private.json"));
    (record["path"].clone(), record["public_code"].clone())
}

#[test]
fn packaged_owner_create_persists_swarm_and_owner_membership() {
    let root = private_root();
    let host = compose(&root);
    assert!(!root.join("target-identity.json").exists());
    let created = create(&host);
    // `result = self.authority.accept_pairing(handoff)`: this Mac is the swarm's current member.
    assert_observed("boundary_owner_created", &created);
    assert_eq!(created.1["protocol"], "mycelium.demo_membership_result.v1");
    assert!(
        created.1["membership_generation"]
            .as_u64()
            .is_some_and(|g| g >= 1)
    );
    // The consent was persisted exactly once, as sent, before the swarm (app.py:1600-1603).
    let policy = private_json(&root.join("resource-policy.json"));
    assert_eq!(policy, consent());
    // The swarm record lives in the private root and names this Mac's own identity.
    let record = private_json(&root.join(SWARM_FILE));
    let swarm_id = record["swarm_id"].as_str().unwrap();
    assert!(
        swarm_id.starts_with("sw-") && swarm_id.len() == 23,
        "{record}"
    );
    assert!(
        record["node_id"]
            .as_str()
            .unwrap()
            .starts_with("remote-mac-"),
        "{record}"
    );
    private_json(&root.join("target-identity.json"));
    // The owner's membership is retained beside its seed, and its seed state exists.
    assert!(
        root.join(MEMBER_DIR)
            .join("membership.state.json")
            .is_file()
    );
    assert!(root.join("seed-rs/seed.sqlite").is_file());
}

#[test]
fn packaged_owner_create_current_member_returns_authority_status() {
    let root = private_root();
    let host = compose(&root);
    assert_eq!(create(&host).0, 200);
    let record = std::fs::read(root.join(SWARM_FILE)).unwrap();
    // `if current.get('membership_state')=='current': return current` (app.py:1604-1608).
    let again = create(&host);
    assert_eq!(again.0, 200, "{}", again.1);
    for (key, value) in [
        ("protocol", json!("mycelium.demo_authority_status.v1")),
        ("started", json!(true)),
        ("swarm_created", json!(true)),
        ("local_tls", json!("pinned_loopback")),
        ("membership_state", json!("current")),
        ("member_count", json!(1)),
        ("route_ready", json!(false)),
    ] {
        assert_eq!(again.1[key], value, "{key}: {}", again.1);
    }
    // `authority.status()`, not `DemoApp.status()`: no member list.
    assert!(again.1.get("members").is_none(), "{}", again.1);
    // No second swarm: the record is byte for byte the first one.
    assert_eq!(std::fs::read(root.join(SWARM_FILE)).unwrap(), record);
    // A changed consent is refused before the authority is asked (app.py:1601).
    let mut changed = consent();
    changed["power"] = json!("battery_allowed");
    assert_eq!(
        refusal(&post(&host, "/api/create", json!({"consent": changed}))),
        (400, "consent_retry_changed")
    );
    assert_eq!(std::fs::read(root.join(SWARM_FILE)).unwrap(), record);
}

/// What a test plants in the private root before the host binds, so one owner-join (or seed start)
/// step of `/api/create` fails on its own state.
enum Fault {
    /// A 0600 regular file that is not a private record.
    Garbage,
    /// A 0700 directory where the step writes a file.
    Directory,
}
/// Plant `fault` at `relative` (parents 0700) and answer its path.
fn plant(root: &Path, relative: &str, fault: Fault) -> PathBuf {
    let path = root.join(relative);
    std::fs::DirBuilder::new()
        .mode(0o700)
        .recursive(true)
        .create(path.parent().unwrap())
        .unwrap();
    match fault {
        Fault::Garbage => std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&path)
            .unwrap()
            .write_all(b"not a private record")
            .unwrap(),
        Fault::Directory => std::fs::DirBuilder::new()
            .mode(0o700)
            .create(&path)
            .unwrap(),
    }
    path
}
/// Remove what [`plant`] put at `path`.
fn unplant(path: &Path) {
    if path.is_dir() {
        std::fs::remove_dir(path).unwrap();
    } else {
        std::fs::remove_file(path).unwrap();
    }
}
/// The swarm the seed state is bound to (`seed_metadata.swarm_id` of `seed-rs/seed.sqlite`, the
/// identity `bind_identity` refuses to change: `seed_state_identity_mismatch`).
fn seed_swarm_id(root: &Path) -> Value {
    let database = root.join("seed-rs/seed.sqlite");
    assert!(database.is_file(), "the seed state was removed");
    let seed =
        rusqlite::Connection::open_with_flags(database, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .unwrap();
    let id: String = seed
        .query_row(
            "SELECT value FROM seed_metadata WHERE key = 'swarm_id'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    json!(id)
}
/// The swarm record, after asserting that it is still there.
fn kept_record(root: &Path) -> Value {
    assert!(
        root.join(SWARM_FILE).is_file(),
        "the failed create removed its swarm record"
    );
    private_json(&root.join(SWARM_FILE))
}
/// `_membership_result` of a (re-)enrolled owner as the oracle's retry answered it: this Mac is
/// member 1 of its swarm, generation 1, RUNNING.
fn assert_reenrolled(reply: &(u16, Value)) {
    assert_observed("boundary_owner_created", reply);
    assert_eq!(
        (
            &reply.1["protocol"],
            &reply.1["membership_generation"],
            &reply.1["lifecycle_state"]
        ),
        (
            &json!("mycelium.demo_membership_result.v1"),
            &json!(1),
            &json!("RUNNING")
        ),
        "{}",
        reply.1
    );
}
/// `authority.status()` of the current member a later create answers (app.py:1608).
fn assert_current_status(reply: &(u16, Value), generation: u64) {
    assert_eq!(reply.0, 200, "{}", reply.1);
    for (key, value) in [
        ("protocol", json!("mycelium.demo_authority_status.v1")),
        ("started", json!(true)),
        ("swarm_created", json!(true)),
        ("local_tls", json!("pinned_loopback")),
        ("membership_state", json!("current")),
        ("member_count", json!(1)),
        ("membership_generation", json!(generation)),
        ("lease_state", json!("fresh")),
        ("route_ready", json!(false)),
    ] {
        assert_eq!(reply.1[key], value, "{key}: {}", reply.1);
    }
}
/// The owner's retained pairing request, where the owner-join fault is planted.
fn pending_fault() -> String {
    format!("{MEMBER_DIR}/{PENDING_FILE}")
}

/// The owner's own join through its new seed fails at its pairing request (a retained pending
/// record that is not a private record). v0.1.4 lets the exception out of `/api/create` and
/// closes only a relay this call started (app.py:1589-1625 @ f4e366e9): `create_swarm` had
/// committed the metadata and its runtime keeps running (authority.py:405-455). Executed oracle
/// (`workers/s1-owner-create-failed-join-retry/oracle-failed-join.json`, case `pair_request`):
/// 409 `authority_state_invalid`; the metadata and the seed state bound to it stay, no membership
/// is retained, the failure is recorded; the authority is `unenrolled` with its seed up, so a
/// pairing code is a pending window; a retry while the fault persists and a relaunch over the
/// same root see the same swarm and the same refusal.
#[test]
fn packaged_owner_create_failed_join_keeps_swarm_record() {
    let root = private_root();
    plant(&root, &pending_fault(), Fault::Garbage);
    let host = compose(&root);
    assert_eq!(refusal(&create(&host)), (409, "authority_state_invalid"));
    let record = kept_record(&root);
    assert_eq!(
        seed_swarm_id(&root),
        record["swarm_id"],
        "the seed state is bound to the kept swarm"
    );
    assert!(!root.join(MEMBER_DIR).join("membership.state.json").exists());
    assert_eq!(private_json(&root.join("resource-policy.json")), consent());
    assert_eq!(
        last_failure(&root),
        (json!("/api/create"), json!("authority_state_invalid"))
    );
    let open = json!({"ttl_seconds": 30});
    assert_observed(
        "join_window_waits_for_public_relay",
        &post(&host, "/api/pair/window/open", open.clone()),
    );
    assert_eq!(refusal(&create(&host)), (409, "authority_state_invalid"));
    assert_eq!(private_json(&root.join(SWARM_FILE)), record);
    drop(host);
    let relaunched = compose(&root);
    assert_observed(
        "join_window_waits_for_public_relay",
        &post(&relaunched, "/api/pair/window/open", open),
    );
    assert_eq!(
        refusal(&create(&relaunched)),
        (409, "authority_state_invalid")
    );
    assert_eq!(private_json(&root.join(SWARM_FILE)), record);
    assert_eq!(seed_swarm_id(&root), record["swarm_id"]);
    assert_eq!(
        last_failure(&root),
        (json!("/api/create"), json!("authority_state_invalid"))
    );
}

/// The retry after a failed owner join (app.py:1600-1622): a changed consent is still refused
/// before the authority (`consent_retry_changed`); with the fault removed the `unenrolled` swarm
/// re-enrols through its own seed (`pair_request` -> `approve_pairing` -> `accept_pairing` again):
/// this Mac is member 1 of the SAME swarm, generation 1, and a later create answers
/// `authority.status()` of that current member. The failure stays the last recorded one. A
/// stopped owner's membership is `retained`, a state that cannot re-enrol (app.py:1607). Executed
/// oracle: cases `pair_request` and `pair_request_same_process`.
#[test]
fn packaged_owner_create_retry_after_failed_join_reenrols() {
    let root = private_root();
    let fault = plant(&root, &pending_fault(), Fault::Garbage);
    let host = compose(&root);
    assert_eq!(refusal(&create(&host)), (409, "authority_state_invalid"));
    let record = kept_record(&root);
    let mut changed = consent();
    changed["power"] = json!("battery_allowed");
    assert_eq!(
        refusal(&post(&host, "/api/create", json!({"consent": changed}))),
        (400, "consent_retry_changed")
    );
    unplant(&fault);
    let retried = create(&host);
    assert_reenrolled(&retried);
    assert_eq!(
        private_json(&root.join(SWARM_FILE)),
        record,
        "the retry made another swarm"
    );
    assert_eq!(seed_swarm_id(&root), record["swarm_id"]);
    let membership: Value = serde_json::from_slice(
        &std::fs::read(root.join(MEMBER_DIR).join("membership.state.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(membership["swarm_id"], record["swarm_id"]);
    assert_eq!(owner_lease(&root).0, 1);
    assert_current_status(&create(&host), 1);
    assert_eq!(
        last_failure(&root),
        (json!("/api/create"), json!("authority_state_invalid"))
    );
    assert_eq!(post(&host, "/api/stop", json!({})).0, 200);
    assert_eq!(refusal(&create(&host)), (400, "membership_cannot_reenroll"));
    assert_eq!(private_json(&root.join(SWARM_FILE)), record);
}

/// Near misses around the retry: a failed join, then `/api/stop`, leaves an `unenrolled` swarm
/// whose retry cannot reach a stopped authority (oracle case `stopped_unenrolled`: 409
/// `authority_not_started`); a relaunch between the failure and the retry brings the same seed
/// back and the retry re-enrols there (oracle case `pair_request`, `relaunch_*`).
#[test]
fn packaged_owner_create_relaunch_after_failed_join_reenrols() {
    let root = private_root();
    let fault = plant(&root, &pending_fault(), Fault::Garbage);
    let host = compose(&root);
    assert_eq!(refusal(&create(&host)), (409, "authority_state_invalid"));
    let record = kept_record(&root);
    assert_eq!(post(&host, "/api/stop", json!({})).0, 200);
    unplant(&fault);
    assert_eq!(refusal(&create(&host)), (409, "authority_not_started"));
    drop(host);
    let relaunched = compose(&root);
    assert_reenrolled(&create(&relaunched));
    assert_eq!(private_json(&root.join(SWARM_FILE)), record);
    assert_eq!(seed_swarm_id(&root), record["swarm_id"]);
    assert_current_status(&create(&relaunched), 1);
}

/// The other failure points of the owner's join keep the swarm the same way, and the retry with
/// the fault removed re-enrols in the same process (executed oracle cases `approve`, `accept`,
/// `*_same_process`): an approval store that is not a directory (409
/// `authority_state_path_invalid`), a handoff path that is a directory (409
/// `authority_state_write_failed`).
#[test]
fn packaged_owner_create_failed_approve_or_accept_keeps_swarm_record() {
    for (relative, fault, code) in [
        (
            "authority/member/seed/approvals",
            Fault::Garbage,
            "authority_state_path_invalid",
        ),
        (
            "authority/member/pairing.handoff.json",
            Fault::Directory,
            "authority_state_write_failed",
        ),
    ] {
        let root = private_root();
        let planted = plant(&root, relative, fault);
        let host = compose(&root);
        assert_eq!(refusal(&create(&host)), (409, code), "{relative}");
        let record = kept_record(&root);
        assert_eq!(seed_swarm_id(&root), record["swarm_id"], "{relative}");
        assert_eq!(refusal(&create(&host)), (409, code), "{relative}");
        unplant(&planted);
        assert_reenrolled(&create(&host));
        assert_eq!(private_json(&root.join(SWARM_FILE)), record, "{relative}");
        assert_current_status(&create(&host), 1);
    }
}

/// The seed itself fails to start after `create_swarm` committed the swarm (here its kept-url file
/// is not a private record). v0.1.4 keeps the metadata and only closes the runtime
/// (authority.py:448-454); executed oracle case `runtime_start`: no pairing code without a
/// coordinator (`pairing_window_swarm_required`), every retry in that process refuses
/// `authority_swarm_missing` (the swarm exists, its seed does not run), and a relaunch with the
/// fault gone starts the seed and the retry re-enrols the same swarm.
#[test]
fn packaged_owner_create_failed_seed_start_keeps_swarm_record() {
    let root = private_root();
    let fault = plant(&root, "seed-rs/seed-url.json", Fault::Garbage);
    let host = compose(&root);
    let refused = create(&host);
    assert_eq!(refused.0, 409, "{}", refused.1);
    let record = kept_record(&root);
    assert_eq!(seed_swarm_id(&root), record["swarm_id"]);
    assert_eq!(
        refusal(&post(
            &host,
            "/api/pair/window/open",
            json!({"ttl_seconds": 30})
        )),
        (400, "pairing_window_swarm_required")
    );
    assert_eq!(refusal(&create(&host)), (409, "authority_swarm_missing"));
    unplant(&fault);
    assert_eq!(refusal(&create(&host)), (409, "authority_swarm_missing"));
    assert_eq!(private_json(&root.join(SWARM_FILE)), record);
    drop(host);
    let relaunched = compose(&root);
    assert_reenrolled(&create(&relaunched));
    assert_eq!(private_json(&root.join(SWARM_FILE)), record);
    assert_eq!(seed_swarm_id(&root), record["swarm_id"]);
}

/// One step's answer as the oracle trace records it (`short`): a refusal's code, else the
/// answer's `state` or `protocol` (empty for a body that has neither).
fn answer(reply: &(u16, Value)) -> (u16, String) {
    let field = if reply.0 == 200 {
        reply.1.get("state").or_else(|| reply.1.get("protocol"))
    } else {
        reply.1.get("error")
    };
    (
        reply.0,
        field.and_then(Value::as_str).unwrap_or_default().to_owned(),
    )
}

/// A failure while `create_swarm` still prepares the swarm (here the seed's TLS directory is a
/// regular file). v0.1.4 makes the identities, the local TLS material and the seed database bound
/// to the new swarm id BEFORE it commits the metadata (authority.py:412-449 @ f4e366e9), so the
/// failure leaves no swarm. Executed oracle (`workers/rv-s1-owner-create-failed-join-retry/
/// evidence/rv-logs-s1-failed-join-retry/oracle-extra.json`, case `tls_precommit`): 409
/// `local_tls_path_invalid` with no metadata, no seed state and no membership, the consent kept and
/// the failure recorded; with the fault removed the retry refuses `authority_swarm_exists` (the
/// prepared state is there, authority.py:410-411) and still commits nothing; a relaunch refuses
/// that partial state (`authority.start()`, authority.py:390-391: `authority_partial_state`). The
/// oracle's relaunch raises out of `DemoApp.__init__`; the packaged host binds and keeps that code,
/// which its authority seams answer (`PackagedHost::authority`).
#[test]
fn packaged_owner_create_failed_preparation_commits_no_swarm() {
    let root = private_root();
    let fault = plant(&root, "seed-rs/tls", Fault::Garbage);
    // The answer, then whether a swarm record, a seed database and a membership exist.
    let step = |reply: &(u16, Value)| {
        (
            answer(reply),
            root.join(SWARM_FILE).exists(),
            root.join("seed-rs/seed.sqlite").exists(),
            root.join(MEMBER_DIR).join("membership.state.json").exists(),
        )
    };
    let host = compose(&root);
    let failed = step(&create(&host));
    let failed_record = last_failure(&root);
    unplant(&fault);
    let retried = step(&create(&host));
    let retried_record = last_failure(&root);
    drop(host);
    let relaunched = compose(&root);
    let relaunch = step(&create(&relaunched));
    let nothing = |code: &str| ((409, code.to_owned()), false, false, false);
    assert_eq!(
        [failed, retried, relaunch],
        [
            nothing("local_tls_path_invalid"),
            nothing("authority_swarm_exists"),
            nothing("authority_partial_state")
        ]
    );
    assert_eq!(
        failed_record,
        (json!("/api/create"), json!("local_tls_path_invalid"))
    );
    assert_eq!(
        retried_record,
        (json!("/api/create"), json!("authority_swarm_exists"))
    );
    assert_eq!(private_json(&root.join("resource-policy.json")), consent());
}

/// A stopped owner whose retained membership cannot be read. `/api/create` asks
/// `authority.status()` (app.py:1604), whose branch without a coordinator lets
/// `load_membership_state`'s `DurableMembershipError` out (authority.py:669-674), answered 409 with
/// its code and recorded as the last failure (app.py:2010-2032). Executed oracle
/// (`oracle-extra.json`, cases `invalid_json` and `unreadable_parent`).
fn membership_read_error_after_stop(fault: impl FnOnce(&Path, &Path), code: &str) {
    let root = private_root();
    let host = compose(&root);
    assert_eq!(create(&host).0, 200);
    assert_eq!(post(&host, "/api/stop", json!({})).0, 200);
    let dir = root.join(MEMBER_DIR);
    let path = dir.join("membership.state.json");
    let original = std::fs::read(&path).unwrap();
    fault(&dir, &path);
    let retried = create(&host);
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    std::fs::write(&path, original).unwrap();
    assert_eq!(refusal(&retried), (409, code), "{}", retried.1);
    let recorded = root.join("last-action-error.private.json");
    assert!(recorded.is_file(), "the refusal recorded no failure");
    let failure = private_json(&recorded);
    assert_eq!(
        [
            &failure["path"],
            &failure["public_code"],
            &failure["error_code"],
            &failure["exception_type"]
        ],
        [
            &json!("/api/create"),
            &json!(code),
            &json!(code),
            &json!("DurableMembershipError")
        ]
    );
}
#[test]
fn packaged_owner_create_invalid_membership_after_stop_refuses() {
    membership_read_error_after_stop(
        |_, path| std::fs::write(path, b"not json").unwrap(),
        "node_membership_state_invalid",
    );
}
#[test]
fn packaged_owner_create_unreadable_membership_after_stop_refuses() {
    membership_read_error_after_stop(
        |dir, _| std::fs::set_permissions(dir, std::fs::Permissions::from_mode(0o000)).unwrap(),
        "node_membership_state_unavailable",
    );
}

/// The owner's join fails at its acceptance (its handoff path is a directory), the host relaunches
/// and the retry runs with the fault removed (executed oracle case `accept`,
/// `workers/s1-owner-create-failed-join-retry/oracle-failed-join.json`, rerun by the review and by
/// attempt 2). Up to the retry Rust answers as v0.1.4 does: the relaunch brings the same swarm back
/// `unenrolled` (a pairing code is a pending window) and the retry while the fault persists refuses
/// as before. Declared difference from there on: v0.1.4 answers 409 `seed_http_seed_pin_mismatch`
/// to the retry, to a later create and after a second relaunch (409 `authority_not_started` once
/// stopped), because its loopback seed binds a new port at every start (no network
/// configuration, authority.py:526-527; executed `oracle-seed-url.json`: the url moves) while the
/// approval stored before the relaunch (authority.py:903-915) pins the old seed url (seed
/// http.py:957-960). This seed keeps answering for its first url (`run_seed`, `seed-url.json`), so
/// the stored approval still matches and the retry re-enrols the same swarm. The difference rests
/// on the admitted seam SE-L10-SEED-PORT (the oracle's loopback seed port moves at every start;
/// this seed keeps its first url) and on nothing else: the a2 review's executed oracle trace
/// (`workers/rv-s1-owner-create-failed-join-retry-a2/evidence/.../oracle-probes.json`, cases
/// `accept_native_port` and `accept_fixed_port`) answers exactly the list below once only the
/// seed port is held fixed. Not aligned here: changing the kept url is that seam's decision.
#[test]
fn packaged_owner_create_accept_failure_relaunch_retry() {
    let root = private_root();
    let fault = plant(
        &root,
        "authority/member/pairing.handoff.json",
        Fault::Directory,
    );
    let host = compose(&root);
    let mut answers = vec![answer(&create(&host)), answer(&create(&host))];
    let record = kept_record(&root);
    drop(host);
    let relaunched = compose(&root);
    let open = json!({"ttl_seconds": 30});
    answers.push(answer(&post(&relaunched, "/api/pair/window/open", open)));
    answers.push(answer(&create(&relaunched)));
    unplant(&fault);
    answers.push(answer(&create(&relaunched)));
    let retried_record = last_failure(&root);
    let membership = root.join(MEMBER_DIR).join("membership.state.json").exists();
    answers.push(answer(&create(&relaunched)));
    answers.push(answer(&post(&relaunched, "/api/stop", json!({}))));
    answers.push(answer(&create(&relaunched)));
    drop(relaunched);
    let again = compose(&root);
    answers.push(answer(&create(&again)));
    let write_failed = (409, "authority_state_write_failed".to_owned());
    let reenrolled = (200, "mycelium.demo_membership_result.v1".to_owned());
    let current = (200, "mycelium.demo_authority_status.v1".to_owned());
    assert_eq!(
        answers,
        [
            write_failed.clone(),
            write_failed.clone(),
            (200, "waiting_for_relay".to_owned()),
            write_failed,
            reenrolled,
            current.clone(),
            (200, String::new()),
            (400, "membership_cannot_reenroll".to_owned()),
            current
        ]
    );
    assert_eq!(
        retried_record,
        (json!("/api/create"), json!("authority_state_write_failed"))
    );
    assert!(membership, "the retry retained no membership");
    assert_eq!(private_json(&root.join(SWARM_FILE)), record);
    assert_eq!(seed_swarm_id(&root), record["swarm_id"]);
}

/// The packaged process has no relay: a pairing code asked for is a pending window that waits
/// for one (app.py:1703-1710, `_open_pending_window`), exactly as the real v0.1.4 packaged
/// process answered, and the owner's window store lists no redeemable window.
#[test]
fn packaged_owner_create_pair_window_waits_for_relay() {
    let root = private_root();
    let host = compose(&root);
    let open = json!({"ttl_seconds": 30});
    assert_eq!(
        refusal(&post(&host, "/api/pair/window/open", open.clone())),
        (400, "pairing_window_swarm_required")
    );
    assert_eq!(create(&host).0, 200);
    let opened = post(&host, "/api/pair/window/open", open);
    assert_observed("join_window_waits_for_public_relay", &opened);
    let pending = opened.1["pending_id"].as_str().unwrap().to_owned();
    assert!(pending.starts_with("pending-"), "{}", opened.1);
    let none = (200, json!({"windows": []}));
    assert_eq!(post(&host, "/api/pair/window/list", json!({})), none);
    let revoked = post(
        &host,
        "/api/pair/window/revoke",
        json!({"window_id": pending}),
    );
    assert_observed("pending_window_revoked", &revoked);
    assert_eq!(revoked.1["window_id"], json!(pending));
    assert_eq!(post(&host, "/api/pair/window/list", json!({})), none);
}

/// A relaunch over the same private root brings the owner's seed back at bind
/// (`DemoApp.start` -> `authority.start()`): the same swarm, still current, no new record.
#[test]
fn packaged_owner_create_relaunch_resumes_the_same_swarm() {
    let root = private_root();
    let host = compose(&root);
    let created = create(&host);
    assert_eq!(created.0, 200, "{}", created.1);
    let record = std::fs::read(root.join(SWARM_FILE)).unwrap();
    assert_eq!(
        post(&host, "/api/stop", json!({})),
        (
            200,
            json!({"stopped": true, "identity_retained": true, "evidence_retained": true})
        )
    );
    drop(host);
    let relaunched = compose(&root);
    let again = create(&relaunched);
    assert_eq!(again.0, 200, "{}", again.1);
    assert_eq!(
        (
            &again.1["protocol"],
            &again.1["membership_state"],
            &again.1["started"],
            &again.1["local_tls"],
        ),
        (
            &json!("mycelium.demo_authority_status.v1"),
            &json!("current"),
            &json!(true),
            &json!("pinned_loopback"),
        ),
        "{}",
        again.1
    );
    assert_eq!(std::fs::read(root.join(SWARM_FILE)).unwrap(), record);
}

/// Bodies the window must refuse before the authority, and the seams around create that are
/// not this step's: their answers are pinned so a later step changes them on purpose.
#[test]
fn packaged_owner_create_near_misses_and_unported_seams() {
    let root = private_root();
    let host = compose(&root);
    for body in [
        json!({"consent": "yes"}),
        json!({"consent": [consent()]}),
        json!({"consent": consent(), "x": 1}),
        json!({}),
    ] {
        assert_eq!(
            refusal(&post(&host, "/api/create", body.clone())),
            (400, "consent_required"),
            "{body}"
        );
    }
    let entries = |root: &Path| {
        let mut names: Vec<String> = std::fs::read_dir(root)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    };
    assert_eq!(
        entries(&root),
        Vec::<String>::new(),
        "a refusal wrote state"
    );
    // The owner is not a joining target: its pairing request seam has no swarm to answer for.
    assert_eq!(
        refusal(&post(&host, "/api/pair/request", json!({}))),
        (409, "authority_swarm_missing")
    );
    // `/api/start` runs the authority (an owner already runs) and answers the host's status
    // document, which the packaged host does not have yet (`Host::status_document` default).
    assert_eq!(post(&host, "/api/start", json!({})), (200, Value::Null));
    assert_eq!(create(&host).0, 200);
    // The owner cannot become a target (app.py:1099-1100); joining is a later S1 step.
    assert_eq!(
        refusal(&post(
            &host,
            "/api/join/request",
            json!({"consent": consent()})
        )),
        (400, "owner_cannot_become_target")
    );
    // A handoff is only accepted by a target.
    assert_eq!(
        refusal(&post(&host, "/api/pair/accept", json!({"handoff": {}}))),
        (409, "action_not_completed")
    );
    // Seams outside this step keep the trait default.
    for path in ["/api/model/cancel", "/api/local-contributor/start"] {
        assert_eq!(
            refusal(&post(&host, path, json!({}))),
            (409, "action_not_completed"),
            "{path}"
        );
    }
}

/// The renewal tests run the packaged loop on this budget instead of its own 20 s
/// (`RenewLoopSelfClosedWait`, app.py:917), so they see several passes in a few seconds.
const RENEWAL: B = B::OwnerPoll;
/// The packaged composition with the owner's lease renewal every `RENEWAL`.
fn compose_renewing(root: &Path) -> DesktopServices {
    let node = NodeService::new(
        Arc::new(MustNotRun),
        "private-local-token".into(),
        node_service::native::development_qualification().unwrap(),
    );
    DesktopServices::with_renewal_period(node, NonZeroUsize::new(4).unwrap(), root, RENEWAL)
}
/// `(generation, lease_expires_at)` of this Mac's own member row in its seed's committed table
/// (`seed-rs/seed.sqlite`, the snapshot the seed persists with each renewal).
fn owner_lease(root: &Path) -> (u64, f64) {
    let node = private_json(&root.join(SWARM_FILE))["node_id"].clone();
    let seed = rusqlite::Connection::open_with_flags(
        root.join("seed-rs/seed.sqlite"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    let snapshot: String = seed
        .query_row(
            "SELECT snapshot FROM rs_seed_table WHERE id = 1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let snapshot: Value = serde_json::from_str(&snapshot).unwrap();
    let member = snapshot["members"]
        .as_array()
        .unwrap()
        .iter()
        .find(|member| member["node_id"] == node)
        .unwrap();
    (
        member["generation"].as_u64().unwrap(),
        member["lease_expires_at"].as_f64().unwrap(),
    )
}
/// The owner's lease once a renewal has moved it past `after` (a pass comes every `RENEWAL`).
fn renewed_past(root: &Path, after: (u64, f64)) -> (u64, f64) {
    let deadline = Instant::now() + millis(B::WorkerStartup);
    loop {
        let lease = owner_lease(root);
        if lease.1 > after.1 {
            return lease;
        }
        assert!(
            Instant::now() < deadline,
            "the owner's lease was not renewed: {after:?} -> {lease:?}"
        );
        std::thread::sleep(millis(RENEWAL));
    }
}
/// Three loop periods pass and the owner's lease is still what it was.
fn not_renewed(root: &Path, what: &str) {
    let before = owner_lease(root);
    std::thread::sleep(millis(RENEWAL) * 3);
    assert_eq!(owner_lease(root), before, "{what}");
}

/// `DemoApp._renew_loop` (app.py:911-927 @ f4e366e9) on the packaged process: the owner that
/// `/api/create` made keeps its lease, renewed through its own seed by `PackagedHost`'s own loop,
/// pass after pass. As the oracle's renewal does, a renewal keeps the generation and moves
/// `lease_expires_at` later. After `/api/stop` (`authority.stop()`, `self.running = False`)
/// nothing renews it.
#[test]
fn packaged_owner_create_owner_lease_renews() {
    let root = private_root();
    let host = compose_renewing(&root);
    let created = create(&host);
    assert_eq!(created.0, 200, "{}", created.1);
    let first = owner_lease(&root);
    let renewed = renewed_past(&root, first);
    assert_eq!(renewed.0, first.0, "a renewal keeps the generation");
    let again = renewed_past(&root, renewed);
    assert_eq!(again.0, first.0, "a renewal keeps the generation");
    assert_eq!(post(&host, "/api/stop", json!({})).0, 200);
    not_renewed(&root, "a stopped owner's lease was renewed");
    drop(host);
    std::fs::remove_dir_all(&root).unwrap_or_default();
}

/// A pass whose renewal fails is logged and tried again at the next pass with the same
/// membership (app.py:921-927: `renew_error = 'renewal_unavailable'`, the member keeps its
/// session). Here the seed renews the lease but clearing the retained disposition fails (it is a
/// directory), so every renewal fails after its heartbeat and the lease still moves at every
/// pass. Dropping the host ends and joins its loop: nothing keeps the owner's seed open and the
/// lease is not renewed any more.
#[test]
fn packaged_owner_renewal_retries_a_failed_renew_and_ends_with_the_host() {
    let root = private_root();
    let host = compose_renewing(&root);
    assert_eq!(create(&host).0, 200);
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(root.join(MEMBER_DIR).join("membership.disposition.json"))
        .unwrap();
    let first = owner_lease(&root);
    let failed = renewed_past(&root, first);
    renewed_past(&root, failed);
    let seed = private_json(&root.join("seed-rs/seed-url.json"))["seed_url"]
        .as_str()
        .unwrap()
        .trim_start_matches("https://")
        .to_owned();
    assert!(
        std::net::TcpStream::connect(&seed).is_ok(),
        "the owner's seed answers at {seed}"
    );
    drop(host);
    let deadline = Instant::now() + millis(B::WorkerStartup);
    while std::net::TcpStream::connect(&seed).is_ok() {
        assert!(
            Instant::now() < deadline,
            "the dropped host's seed still answers at {seed}"
        );
        std::thread::sleep(millis(RENEWAL));
    }
    not_renewed(&root, "the dropped host's lease was renewed");
    std::fs::remove_dir_all(&root).unwrap_or_default();
}

/// The loop runs from the host's start: before a swarm exists a pass renews nothing and writes
/// nothing, and the swarm created later is renewed by the same loop. Quit's shutdown fence
/// (`self.closed`: the node service's, set by `begin_shutdown`) ends it.
#[test]
fn packaged_owner_renewal_waits_for_a_swarm_and_ends_at_quit() {
    let root = private_root();
    let host = compose_renewing(&root);
    std::thread::sleep(millis(RENEWAL) * 3);
    assert_eq!(
        std::fs::read_dir(&root).unwrap().count(),
        0,
        "a pass without a swarm wrote state"
    );
    assert_eq!(create(&host).0, 200);
    let first = owner_lease(&root);
    renewed_past(&root, first);
    host.begin_shutdown(Instant::now()).unwrap();
    // A pass already on the wire when Quit began may still land; none starts after it.
    std::thread::sleep(millis(RENEWAL));
    not_renewed(&root, "a lease was renewed after Quit");
    drop(host);
    std::fs::remove_dir_all(&root).unwrap_or_default();
}
