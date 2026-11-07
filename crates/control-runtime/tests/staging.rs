// SPDX-License-Identifier: AGPL-3.0-only
// O:path:mycelium_a13_macos/managed_offers.py @ f4e366e9; F2/F3/F16.
use budgets::{BudgetId as B, Entries};
use control_runtime::{
    Store,
    mailbox::{Limits, Mailbox, StagePolicy, StagePublication},
};
use protocol::identity::Identity;
use serde_json::{Value, json};
use std::path::PathBuf;
struct Fixture {
    root: PathBuf,
    mailbox: Mailbox,
}
fn owner() -> Identity {
    Identity::from_secret("owner", &[11; 32]).unwrap()
}
fn member() -> Identity {
    Identity::from_secret("member", &[12; 32]).unwrap()
}
fn binding() -> Value {
    json!({"target_node_id":"node-member","target_endpoint_id":"member","target_key_digest":member().verification_key_digest(),"membership_generation":1,"member_incarnation":"inc-1","run_id":"run-1","deployment_id":"deployment-1"})
}
fn open(root: &std::path::Path) -> Mailbox {
    Mailbox::new(
        Store::open(&root.join("state.db")).unwrap(),
        owner(),
        "swarm",
        "node-owner",
        Limits {
            pending: Entries::new(8),
            active_work: Entries::new(1),
            active_control: Entries::new(2),
        },
    )
    .unwrap()
    .with_stage_policy(StagePolicy {
        path_characters: Entries::new(4096),
        preposition_file_bytes: budgets::Bytes::new(4_294_967_296),
    })
    .unwrap()
}
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "v02-l6-stage-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir(&root).unwrap();
        let mut mailbox = open(&root);
        mailbox
            .bind_member(&binding(), &member().public_key_record(), 3000.0)
            .unwrap();
        Self { root, mailbox }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.root).unwrap();
    }
}
fn publish(m: &mut Mailbox, id: &str, now: f64) -> Value {
    m.publish_stage(
        StagePublication {
            binding: &binding(),
            id,
            archive: b"abcde",
            ttl: B::PyFinalMyceliumA13MacosManagedOffersTtlSecondsL107C77
                .span(Entries::ZERO)
                .unwrap(),
            preposition: None,
        },
        || now,
    )
    .unwrap()
}
fn request(kind: &str, id: Option<&str>, extra: Value) -> Vec<u8> {
    let mut s = binding();
    for (k,v) in json!({"protocol":format!("mycelium.managed_stage_{kind}.v1"),"request_id":"stage-request","swarm_id":"swarm","sequence":1,"nonce":"stage-nonce","issued_at":1000,"expires_at":1030,"run_id":"stage-discovery","deployment_id":"stage-discovery","offer_id":id}).as_object().unwrap() { s[k]=v.clone(); }
    for (k, v) in extra.as_object().unwrap() {
        s[k] = v.clone();
    }
    let signature = member().sign(&s).unwrap();
    serde_json::to_vec(&json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL,"statement":s,"signature":signature,"verification_key":member().public_key_record()})).unwrap()
}
fn body(bytes: &[u8]) -> Value {
    let e: Value = serde_json::from_slice(bytes).unwrap();
    protocol::identity::verify(
        &e["statement"],
        &e["signature"],
        &owner().public_key_record(),
    )
    .unwrap();
    e["statement"].clone()
}
fn stage_ack() -> Value {
    json!({"protocol":"mycelium.controller_remote_stage_ack.v1","node_id":"node-member","staging_root":"/owned/staging","archive_digest":protocol::artifact_grant::range_digest(b"abcde"),"archive_size_bytes":5})
}
fn cleanup_ack() -> Value {
    json!({"protocol":"mycelium.controller_remote_cleanup_ack.v1","node_id":"node-member","staging_root":"/owned/staging","removed":true})
}
#[test]
fn stage_publish_read_ack_cleanup_retire_survives_reopen_without_execution_authority() {
    let mut f = Fixture::new();
    let db = rusqlite::Connection::open(f.root.join("state.db")).unwrap();
    let offer = f
        .mailbox
        .publish_stage(
            StagePublication {
                binding: &binding(),
                id: "offer-1",
                archive: b"abcde",
                ttl: B::PyFinalMyceliumA13MacosManagedOffersTtlSecondsL107C77
                    .span(Entries::ZERO)
                    .unwrap(),
                preposition: None,
            },
            || {
                db.busy_timeout(std::time::Duration::ZERO).unwrap();
                assert!(db.execute_batch("BEGIN IMMEDIATE").is_err());
                1000.0
            },
        )
        .unwrap();
    assert_eq!(offer["archive_bytes"], 5);
    assert_eq!(offer["expires_at"], 1300.0);
    let discovery = request("read", None, json!({"offset":0}));
    assert_eq!(
        body(&f.mailbox.dispatch_stage(&discovery, || 1001.0).unwrap())["offer"],
        offer
    );
    let chunk = request("read", Some("offer-1"), json!({"offset":1}));
    assert_eq!(
        body(&f.mailbox.dispatch_stage(&chunk, || 1001.0).unwrap())["data"],
        "YmNkZQ=="
    );
    assert!(f.mailbox.retire_stage(&offer, || 1001.0).is_err());
    let ack = request(
        "ack",
        Some("offer-1"),
        json!({"offset":0,"ack":stage_ack()}),
    );
    f.mailbox.dispatch_stage(&ack, || 1001.0).unwrap();
    f.mailbox.request_stage_cleanup(&offer, || 1002.0).unwrap();
    assert!(f.mailbox.dispatch_stage(&chunk, || 1002.0).is_err());
    assert!(f.mailbox.retire_stage(&offer, || 1002.0).is_err());
    let mut bad = cleanup_ack();
    bad["staging_root"] = "/foreign/staging".into();
    assert!(
        f.mailbox
            .dispatch_stage(
                &request("cleanup_ack", Some("offer-1"), json!({"ack":bad})),
                || 1002.0
            )
            .is_err()
    );
    let clean = request("cleanup_ack", Some("offer-1"), json!({"ack":cleanup_ack()}));
    f.mailbox.dispatch_stage(&clean, || 1002.0).unwrap();
    f.mailbox = open(&f.root);
    // Expiry is not cleanup; an exact retained cleanup receipt remains retireable.
    let receipt = f.mailbox.retire_stage(&offer, || 1500.0).unwrap();
    assert_eq!(
        receipt,
        json!({"offer":offer,"stage_ack":stage_ack(),"cleanup_ack":cleanup_ack()})
    );
    assert_eq!(f.mailbox.retire_stage(&offer, || 1501.0).unwrap(), receipt);
    assert_eq!(
        db.query_row("SELECT count(*) FROM managed_stage_offers", [], |r| r
            .get::<_, u64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        db.query_row("SELECT count(*) FROM managed_commands", [], |r| r
            .get::<_, u64>(0))
            .unwrap(),
        0
    );
    publish(&mut f.mailbox, "offer-2", 1501.0);
}
#[test]
fn stage_expiry_stale_membership_and_commit_failure_cannot_release_uncertain_slot() {
    let mut f = Fixture::new();
    let offer = publish(&mut f.mailbox, "offer-1", 1000.0);
    f.mailbox.request_stage_cleanup(&offer, || 1001.0).unwrap();
    assert!(
        f.mailbox
            .publish_stage(
                StagePublication {
                    binding: &binding(),
                    id: "offer-2",
                    archive: b"abcde",
                    ttl: B::PyFinalMyceliumA13MacosManagedOffersTtlSecondsL107C77
                        .span(Entries::ZERO)
                        .unwrap(),
                    preposition: None
                },
                || 1500.0
            )
            .is_err()
    );
    let req = request("cleanup_ack", Some("offer-1"), json!({"ack":cleanup_ack()}));
    f.mailbox.dispatch_stage(&req, || 1002.0).unwrap();
    let db = rusqlite::Connection::open(f.root.join("state.db")).unwrap();
    db.execute_batch("CREATE TABLE fault_parent(id INTEGER PRIMARY KEY); CREATE TABLE fault_child(id INTEGER REFERENCES fault_parent(id) DEFERRABLE INITIALLY DEFERRED); CREATE TRIGGER fail_retire AFTER INSERT ON managed_stage_history BEGIN INSERT INTO fault_child VALUES(99); END;").unwrap();
    assert!(f.mailbox.retire_stage(&offer, || 1003.0).is_err());
    assert_eq!(
        db.query_row("SELECT count(*) FROM managed_stage_offers", [], |r| r
            .get::<_, u64>(0))
            .unwrap(),
        1
    );
    assert_eq!(
        db.query_row("SELECT count(*) FROM managed_stage_history", [], |r| r
            .get::<_, u64>(0))
            .unwrap(),
        0
    );
    db.execute_batch("DROP TRIGGER fail_retire").unwrap();
    f.mailbox.revoke_member("node-member").unwrap();
    assert!(f.mailbox.dispatch_stage(&req, || 1004.0).is_err());
    assert!(f.mailbox.retire_stage(&offer, || 1004.0).is_err());
    f.mailbox
        .bind_member(&binding(), &member().public_key_record(), 3000.0)
        .unwrap();
    f.mailbox.retire_stage(&offer, || 1004.0).unwrap();
}
#[test]
fn stage_v2_pins_preposition_and_refuses_path_size_digest_and_ack_mutations() {
    let mut f = Fixture::new();
    let pre = json!({"protocol":"mycelium.controller_prepositioned_member_artifacts.v1","files":[{"content_digest":format!("sha256:{}","a".repeat(64)),"destination_path":"model/weights","size_bytes":5,"source_path":"/cache/weights"}]});
    let publication = |m: &mut Mailbox, v: &Value| {
        m.publish_stage(
            StagePublication {
                binding: &binding(),
                id: "offer-v2",
                archive: b"abcde",
                ttl: B::PyFinalMyceliumA13MacosManagedOffersTtlSecondsL107C77
                    .span(Entries::ZERO)
                    .unwrap(),
                preposition: Some(v),
            },
            || 1000.0,
        )
    };
    for (key, bad) in [
        ("source_path", json!("/cache/../weights")),
        ("destination_path", json!("/absolute")),
        ("content_digest", json!("sha256:no")),
        ("size_bytes", json!(4_294_967_297_u64)),
    ] {
        let mut v = pre.clone();
        v["files"][0][key] = bad;
        assert!(publication(&mut f.mailbox, &v).is_err(), "accepted {key}");
    }
    let offer = publication(&mut f.mailbox, &pre).unwrap();
    // Independently serialized sorted compact UTF-8 + newline: 253 bytes;
    // SHA-256 recorded with Ruby Digest, not the production canonicalizer.
    assert_eq!(
        offer["preposition_digest"],
        "sha256:738dae5ef75664f6081e88f5b9bb1c70230e62e7aba40d24103589ed2f6e9e87"
    );
    assert!(
        f.mailbox
            .dispatch_stage(
                &request(
                    "ack",
                    Some("offer-v2"),
                    json!({"offset":0,"ack":stage_ack()})
                ),
                || 1001.0
            )
            .is_err()
    );
    let mut ack = stage_ack();
    ack["preposition_digest"] = offer["preposition_digest"].clone();
    ack["preposition_size_bytes"] = 253.into();
    let signed = request("ack", Some("offer-v2"), json!({"offset":0,"ack":ack}));
    f.mailbox.dispatch_stage(&signed, || 1001.0).unwrap();
    let mut tampered: Value = serde_json::from_slice(&signed).unwrap();
    tampered["statement"]["ack"]["staging_root"] = "/foreign".into();
    assert!(
        f.mailbox
            .dispatch_stage(&serde_json::to_vec(&tampered).unwrap(), || 1001.0)
            .is_err()
    );
    let mut conflicting = ack.clone();
    conflicting["staging_root"] = "/different".into();
    assert!(
        f.mailbox
            .dispatch_stage(
                &request(
                    "ack",
                    Some("offer-v2"),
                    json!({"offset":0,"ack":conflicting})
                ),
                || 1001.0
            )
            .is_err()
    );
    f.mailbox.request_stage_cleanup(&offer, || 1002.0).unwrap();
    let mut clean = cleanup_ack();
    clean["removed"] = false.into();
    f.mailbox
        .dispatch_stage(
            &request("cleanup_ack", Some("offer-v2"), json!({"ack":clean})),
            || 1002.0,
        )
        .unwrap();
    assert_eq!(
        f.mailbox.retire_stage(&offer, || 1003.0).unwrap()["cleanup_ack"]["removed"],
        false,
        "false means target-confirmed absence, never local cleanup authority"
    );
}
#[test]
fn stage_replay_is_fresh_offset_strict_and_legacy_columns_upgrade_in_place() {
    let mut f = Fixture::new();
    let db = rusqlite::Connection::open(f.root.join("state.db")).unwrap();
    db.execute_batch("DROP TABLE managed_stage_offers; CREATE TABLE managed_stage_offers(node_id TEXT PRIMARY KEY,offer_id TEXT UNIQUE NOT NULL,expires REAL NOT NULL,metadata TEXT NOT NULL,archive BLOB NOT NULL);").unwrap();
    f.mailbox = open(&f.root);
    let discovery = request("read", None, json!({"offset":0}));
    assert!(body(&f.mailbox.dispatch_stage(&discovery, || 1000.0).unwrap())["offer"].is_null());
    let offer = publish(&mut f.mailbox, "offer-1", 1000.0);
    assert_eq!(
        body(&f.mailbox.dispatch_stage(&discovery, || 1001.0).unwrap())["offer"],
        offer
    );
    for offset in [
        json!(-1),
        json!(true),
        json!(0.5),
        json!(5),
        json!(67_108_865),
    ] {
        assert!(
            f.mailbox
                .dispatch_stage(
                    &request("read", Some("offer-1"), json!({"offset":offset})),
                    || 1001.0
                )
                .is_err()
        );
    }
    assert!(
        f.mailbox
            .dispatch_stage(&request("read", None, json!({"offset":1})), || 1001.0)
            .is_err()
    );
    assert!(f.mailbox.dispatch_stage(&discovery, || f64::NAN).is_err());
    assert!(
        f.mailbox.dispatch_stage(&discovery, || 2000.0).is_err(),
        "expired signed request cannot read"
    );
    f.mailbox.revoke_member("node-member").unwrap();
    assert!(
        f.mailbox.dispatch_stage(&discovery, || 1002.0).is_err(),
        "old signed discovery must not bypass membership"
    );
}
#[test]
fn newer_incarnation_supersedes_without_fabricating_cleanup_and_history_is_bounded() {
    let mut f = Fixture::new();
    let old = publish(&mut f.mailbox, "offer-1", 1000.0);
    let mut next = binding();
    next["membership_generation"] = 2.into();
    next["member_incarnation"] = "inc-2".into();
    f.mailbox
        .bind_member(&next, &member().public_key_record(), 3000.0)
        .unwrap();
    f.mailbox
        .publish_stage(
            StagePublication {
                binding: &next,
                id: "offer-2",
                archive: b"abcde",
                ttl: B::PyFinalMyceliumA13MacosManagedOffersTtlSecondsL107C77
                    .span(Entries::ZERO)
                    .unwrap(),
                preposition: None,
            },
            || 1001.0,
        )
        .unwrap();
    let db = rusqlite::Connection::open(f.root.join("state.db")).unwrap();
    let saved: String = db
        .query_row(
            "SELECT receipt FROM managed_stage_history WHERE offer_id='offer-1'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let saved: Value = serde_json::from_str(&saved).unwrap();
    assert_eq!(saved["offer"], old);
    assert_eq!(saved["superseded"], true);
    assert!(saved["cleanup_ack"].is_null());
    assert!(
        f.mailbox
            .dispatch_stage(&request("read", None, json!({"offset":0})), || 1001.0)
            .is_err()
    );
    let cap = B::PyStageOfferHistoryCapacity.count().unwrap().get();
    for i in 1..cap {
        db.execute(
            "INSERT INTO managed_stage_history VALUES (?1,'{}')",
            [format!("history-{i}")],
        )
        .unwrap();
    }
    next["membership_generation"] = 3.into();
    next["member_incarnation"] = "inc-3".into();
    f.mailbox
        .bind_member(&next, &member().public_key_record(), 3000.0)
        .unwrap();
    assert!(
        f.mailbox
            .publish_stage(
                StagePublication {
                    binding: &next,
                    id: "offer-3",
                    archive: b"abcde",
                    ttl: B::PyFinalMyceliumA13MacosManagedOffersTtlSecondsL107C77
                        .span(Entries::ZERO)
                        .unwrap(),
                    preposition: None
                },
                || 1002.0
            )
            .is_err()
    );
    assert_eq!(
        db.query_row("SELECT offer_id FROM managed_stage_offers", [], |r| r
            .get::<_, String>(0))
            .unwrap(),
        "offer-2"
    );
}
