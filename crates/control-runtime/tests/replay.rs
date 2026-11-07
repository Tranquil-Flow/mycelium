// SPDX-License-Identifier: AGPL-3.0-only
use control::{ManagedContext, MemberBinding};
use control_runtime::{Error, Store, VerifiedRequest};
use protocol::identity::Identity;
use serde_json::{Value, json};
use std::sync::atomic::{AtomicU64, Ordering};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Temp {
    path: std::path::PathBuf,
}
impl Temp {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "v02-l6-db-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::create_dir(&path).unwrap();
        Self { path }
    }
    fn db(&self) -> std::path::PathBuf {
        self.path.join("state.sqlite")
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.path).unwrap();
    }
}
fn request(seq: u64, id: &str, nonce: &str, at: f64) -> VerifiedRequest {
    let fixture: Value =
        serde_json::from_str(include_str!("../../protocol/fixtures/managed-vectors.json")).unwrap();
    let mut env = fixture[0]["envelope"].clone();
    let s = &mut env["statement"];
    s["sequence"] = seq.into();
    s["request_id"] = id.into();
    s["nonce"] = nonce.into();
    s["issued_at"] = json!(at);
    s["expires_at"] = json!(at + 1.0);
    env["signature"] = Identity::from_secret("endpoint-owner", &[0x11; 32])
        .unwrap()
        .sign(&env["statement"])
        .unwrap();
    let binding = MemberBinding {
        target_node_id: "node-member".into(),
        target_endpoint_id: "endpoint-member".into(),
        target_key_digest: format!("sha256:{}", "a".repeat(64)),
        membership_generation: "2".into(),
        member_incarnation: "incarnation-2".into(),
        run_id: "run-1".into(),
        deployment_id: "deployment-1".into(),
    };
    let c = ManagedContext {
        protocol: "mycelium.managed_control.owner_command.v1",
        signer_key: &fixture[0]["context"]["expected_key_record"],
        signer_endpoint: "endpoint-owner",
        swarm_id: "swarm-fixture",
        owner_node_id: "node-owner",
        binding: &binding,
    };
    VerifiedRequest::authenticate(&env, &c, at).unwrap()
}
fn signer() -> String {
    Identity::from_secret("endpoint-owner", &[0x11; 32])
        .unwrap()
        .public_key_record()["verification_key_digest"]
        .as_str()
        .unwrap()
        .into()
}
#[test]
fn production_clock_is_sampled_under_write_lock_and_expired_request_has_no_effect() {
    let t = Temp::new();
    let mut s = Store::open(&t.db()).unwrap();
    let r = request(1, "req-1", "nonce-1", 1000.0);
    let result = s.execute_with_clock(
        &r,
        || {
            let other = rusqlite::Connection::open(t.db()).unwrap();
            other.busy_timeout(std::time::Duration::ZERO).unwrap();
            assert!(
                other.execute_batch("BEGIN IMMEDIATE").is_err(),
                "trusted time sampled before write-lock admission"
            );
            2000.0
        },
        |_| panic!("expired after admission"),
    );
    assert!(matches!(
        result,
        Err(Error::Refused("managed_control_expired"))
    ));
}
#[test]
fn terminal_only_retention_never_deletes_live_or_unretired_commands() {
    let t = Temp::new();
    let mut s = Store::open(&t.db()).unwrap();
    let batch = budgets::BudgetId::RetentionBatch.count().unwrap().get();
    let age = budgets::BudgetId::RetiredCommandAge
        .span(budgets::Entries::ZERO)
        .unwrap()
        .millis() as f64
        / 1000.0;
    s.connection().execute("WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<?1) INSERT INTO managed_commands SELECT 'retired-'||n,'key',NULL,'completed','run','deployment',0,0 FROM seq",[batch+1]).unwrap();
    for state in ["pending", "claimed", "reconciliation_required", "completed"] {
        s.connection()
            .execute(
                "INSERT INTO managed_commands VALUES (?1,'key',NULL,?1,'run','deployment',0,?2)",
                rusqlite::params![
                    state,
                    if state == "completed" {
                        None
                    } else {
                        Some(0.0)
                    }
                ],
            )
            .unwrap();
    }
    assert_eq!(s.prune_retired_commands(age).unwrap(), 0);
    assert_eq!(s.prune_retired_commands(age + age).unwrap() as u64, batch);
    assert_eq!(s.prune_retired_commands(age + age).unwrap(), 1);
    assert_eq!(s.prune_retired_commands(age + age).unwrap(), 0);
    assert_eq!(
        s.connection()
            .query_row("SELECT count(*) FROM managed_commands", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        4
    );
}
#[test]
fn f2_all_live_capacity_refuses_atomically_without_evicting_valid_retry() {
    let t = Temp::new();
    let mut s = Store::open(&t.db()).unwrap();
    let cap = budgets::BudgetId::ReplayCapacity.count().unwrap().get();
    s.connection().execute("WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<?1) INSERT INTO managed_auth_requests SELECT ?2,'req-'||n,'digest-'||n,n,'nonce-'||n,'fixture',x'6f6b',2000.0 FROM seq",rusqlite::params![cap,signer()]).unwrap();
    s.connection()
        .execute(
            "INSERT INTO control_high_water VALUES (?1,?2)",
            rusqlite::params![signer(), cap],
        )
        .unwrap();
    s.connection()
        .execute_batch("CREATE TABLE effect(v TEXT)")
        .unwrap();
    let result = s.execute(
        &request(cap + 1, "fresh-id", "fresh-nonce", 1000.0),
        1000.0,
        |tx| {
            tx.execute("INSERT INTO effect VALUES ('must-rollback')", [])?;
            Ok(b"ok".to_vec())
        },
    );
    assert!(matches!(
        result,
        Err(Error::Refused("managed_control_capacity_exhausted"))
    ));
    assert_eq!(
        s.connection()
            .query_row("SELECT count(*) FROM managed_auth_requests", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        cap
    );
    assert_eq!(
        s.connection()
            .query_row("SELECT count(*) FROM effect", [], |r| r.get::<_, u64>(0))
            .unwrap(),
        0
    );
}
#[test]
fn f2_live_claim_survives_prune_and_failed_history_write_rolls_back_deletion() {
    let t = Temp::new();
    let mut s = Store::open(&t.db()).unwrap();
    let keep = budgets::BudgetId::ReplayKeep.count().unwrap().get();
    for i in 1..=keep + 2 {
        s.execute(
            &request(i, &format!("req-{i}"), &format!("nonce-{i}"), 1000.0),
            1000.0,
            |_| Ok(b"ok".to_vec()),
        )
        .unwrap();
    }
    s.connection().execute("INSERT INTO managed_commands VALUES ('claim',?1,'req-1','claimed','run','deployment',0,NULL)",[signer()]).unwrap();
    s.connection().execute_batch("CREATE TRIGGER no_history BEFORE INSERT ON managed_replay_history BEGIN SELECT RAISE(ABORT,'injected_history_failure'); END;").unwrap();
    assert!(s.prune(&signer(), 2000.0).is_err());
    assert_eq!(
        s.connection()
            .query_row("SELECT count(*) FROM managed_auth_requests", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        keep + 2
    );
    s.connection()
        .execute_batch("DROP TRIGGER no_history")
        .unwrap();
    assert_eq!(s.prune(&signer(), 2000.0).unwrap(), 1);
    assert_eq!(
        s.connection()
            .query_row(
                "SELECT count(*) FROM managed_auth_requests WHERE request_id='req-1'",
                [],
                |r| r.get::<_, u64>(0)
            )
            .unwrap(),
        1
    );
}
#[test]
fn signed_request_expiring_between_verification_and_transaction_has_no_effect() {
    let t = Temp::new();
    let mut s = Store::open(&t.db()).unwrap();
    let r = request(1, "req-1", "nonce-1", 1000.0);
    assert!(matches!(
        s.execute(&r, 2000.0, |_| panic!("expired effect")),
        Err(Error::Refused("managed_control_expired"))
    ));
}
#[test]
fn oversized_response_rolls_back_effect_and_allows_valid_retry() {
    let t = Temp::new();
    let mut s = Store::open(&t.db()).unwrap();
    let r = request(1, "req-1", "nonce-1", 1000.0);
    assert!(
        s.execute(&r, 1000.0, |_| Ok(vec![
            0;
            protocol::managed::MAX_FRAME_BYTES
                + 1
        ]))
        .is_err()
    );
    assert_eq!(
        s.execute(&r, 1000.0, |_| Ok(b"ok".to_vec())).unwrap(),
        b"ok"
    );
}
#[test]
fn f2_exact_retry_does_not_repeat_effect_and_conflicting_retry_refuses() {
    let tmp = Temp::new();
    let mut s = Store::open(&tmp.db()).unwrap();
    let req = request(1, "request-first", "nonce-first", 1000.0);
    assert_eq!(
        s.execute(&req, 1000.0, |_| Ok(b"first".to_vec())).unwrap(),
        b"first"
    );
    assert_eq!(
        s.execute(&req, 1000.0, |_| panic!("duplicate effect"))
            .unwrap(),
        b"first"
    );
    assert!(matches!(
        s.execute(
            &request(2, "request-first", "nonce-second", 1000.0),
            1000.0,
            |_| panic!("conflict effect")
        ),
        Err(Error::Refused("managed_control_conflicting_retry"))
    ));
}
#[test]
fn f2_prune_reopen_rejects_old_nonce_and_request_at_new_sequence() {
    let tmp = Temp::new();
    let mut s = Store::open(&tmp.db()).unwrap();
    let keep = budgets::BudgetId::ReplayKeep.count().unwrap().get();
    for i in 1..=keep + 1 {
        s.execute(
            &request(i, &format!("req-{i}"), &format!("nonce-{i}"), 1000.0),
            1000.0,
            |_| Ok(b"ok".to_vec()),
        )
        .unwrap();
    }
    assert_eq!(s.prune(&signer(), 2000.0).unwrap(), 1);
    drop(s);
    let mut s = Store::open(&tmp.db()).unwrap();
    assert!(matches!(
        s.execute(
            &request(keep + 2, "fresh", "nonce-1", 2000.0),
            2000.0,
            |_| panic!("nonce replay effect")
        ),
        Err(Error::Refused("managed_control_nonce_replayed"))
    ));
    assert!(matches!(
        s.execute(
            &request(keep + 2, "req-1", "fresh", 2000.0),
            2000.0,
            |_| panic!("id replay effect")
        ),
        Err(Error::Refused("managed_control_conflicting_retry"))
    ));
    assert!(matches!(
        s.execute(
            &request(1, "fresh-id", "fresh-nonce", 2000.0),
            2000.0,
            |_| panic!("sequence replay effect")
        ),
        Err(Error::Refused("managed_control_sequence_replayed"))
    ));
    assert_eq!(
        s.execute(
            &request(keep + 2, "fresh-id", "fresh-nonce", 2000.0),
            2000.0,
            |_| Ok(b"progress".to_vec())
        )
        .unwrap(),
        b"progress"
    );
}
#[test]
fn sqlite_effect_failure_rolls_back_replay_and_partial_mutation() {
    let tmp = Temp::new();
    let mut s = Store::open(&tmp.db()).unwrap();
    s.connection()
        .execute_batch("CREATE TABLE effects (value TEXT);")
        .unwrap();
    let r = request(1, "req-first", "nonce-first", 1000.0);
    assert!(
        s.execute(&r, 1000.0, |tx| {
            tx.execute("INSERT INTO effects VALUES ('partial')", [])?;
            Err(Error::Refused("injected_effect_failure"))
        })
        .is_err()
    );
    assert_eq!(
        s.connection()
            .query_row("SELECT count(*) FROM effects", [], |r| r.get::<_, u64>(0))
            .unwrap(),
        0
    );
    assert_eq!(
        s.execute(&r, 1000.0, |_| Ok(b"retry".to_vec())).unwrap(),
        b"retry"
    );
}
