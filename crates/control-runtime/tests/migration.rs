// SPDX-License-Identifier: AGPL-3.0-only
use control_runtime::{Store, rollback_verified};
use rusqlite::Connection;
use std::{
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l6-migration-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::create_dir(&p).unwrap();
        Self(p)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
fn legacy(path: &std::path::Path) {
    Connection::open(path).unwrap().execute_batch("PRAGMA user_version=1;
    CREATE TABLE identity(node TEXT PRIMARY KEY, public_identity BLOB);
    INSERT INTO identity VALUES ('fixture-member',x'010203');
    CREATE TABLE grants(id TEXT PRIMARY KEY, caps TEXT);
    INSERT INTO grants VALUES ('fixture-grant','600s/2GB');
    CREATE TABLE managed_auth_requests(signer_key_digest TEXT NOT NULL, request_id TEXT NOT NULL, request_digest TEXT NOT NULL, sequence INTEGER NOT NULL,nonce TEXT NOT NULL,protocol TEXT NOT NULL,response_json BLOB NOT NULL,PRIMARY KEY(signer_key_digest,request_id), UNIQUE(signer_key_digest,nonce)) WITHOUT ROWID;
    CREATE TABLE managed_commands(command_id TEXT PRIMARY KEY,target_key_digest TEXT NOT NULL,claimed_request_id TEXT,state TEXT NOT NULL,run_id TEXT NOT NULL,deployment_id TEXT NOT NULL,updated_at REAL NOT NULL);
    INSERT INTO managed_auth_requests VALUES ('fixture-signer','fixture-request','fixture-digest',9,'fixture-nonce','fixture-protocol',x'7b7d');").unwrap();
}
#[test]
fn f3_migration_preserves_identity_grant_and_highwater_with_verified_rollback() {
    let t = Temp::new();
    let path = t.0.join("legacy.db");
    let backup = t.0.join("backup.db");
    legacy(&path);
    let s = Store::open_with_backup(&path, Some(&backup)).unwrap();
    assert_eq!(
        s.connection()
            .query_row("PRAGMA user_version", [], |r| r.get::<_, u32>(0))
            .unwrap(),
        2
    );
    assert_eq!(
        s.connection()
            .query_row("SELECT public_identity FROM identity", [], |r| r
                .get::<_, Vec<u8>>(0))
            .unwrap(),
        [1, 2, 3]
    );
    assert_eq!(
        s.connection()
            .query_row("SELECT caps FROM grants", [], |r| r.get::<_, String>(0))
            .unwrap(),
        "600s/2GB"
    );
    assert_eq!(
        s.connection()
            .query_row("SELECT sequence FROM control_high_water", [], |r| r
                .get::<_, u64>(0))
            .unwrap(),
        9
    );
    drop(s);
    let rollback = t.0.join("rollback.db");
    rollback_verified(&backup, &rollback).unwrap();
    let r = Connection::open(rollback).unwrap();
    assert_eq!(
        r.query_row("PRAGMA user_version", [], |r| r.get::<_, u32>(0))
            .unwrap(),
        1
    );
    assert_eq!(
        r.query_row("SELECT caps FROM grants", [], |r| r.get::<_, String>(0))
            .unwrap(),
        "600s/2GB"
    );
    assert!(
        rollback_verified(&backup, &path).is_err(),
        "never overwrite live DB during rollback"
    );
}
#[test]
fn migration_failure_rolls_back_schema_and_version_and_preserves_backup() {
    let t = Temp::new();
    let path = t.0.join("legacy.db");
    let backup = t.0.join("backup.db");
    legacy(&path);
    Connection::open(&path)
        .unwrap()
        .execute_batch("CREATE TABLE control_state (collision TEXT);")
        .unwrap();
    assert!(Store::open_with_backup(&path, Some(&backup)).is_err());
    let db = Connection::open(&path).unwrap();
    assert_eq!(
        db.query_row("PRAGMA user_version", [], |r| r.get::<_, u32>(0))
            .unwrap(),
        1
    );
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM sqlite_master WHERE name='managed_replay_history'",
            [],
            |r| r.get::<_, u32>(0)
        )
        .unwrap(),
        0
    );
    let columns: Vec<String> = db
        .prepare("PRAGMA table_info(managed_auth_requests)")
        .unwrap()
        .query_map([], |r| r.get(1))
        .unwrap()
        .collect::<std::result::Result<_, _>>()
        .unwrap();
    assert!(!columns.iter().any(|s| s == "valid_until"));
    assert!(backup.exists());
    rollback_verified(&backup, &t.0.join("recovered.db")).unwrap();
}
#[test]
fn pinned_v014_version_zero_text_rows_and_existing_bloom_survive_migration() {
    let t = Temp::new();
    let path = t.0.join("v014.db");
    let backup = t.0.join("v014-backup.db");
    let db = Connection::open(&path).unwrap();
    db.execute_batch(include_str!("fixtures/v014-mailbox.sql"))
        .unwrap();
    let keep = budgets::BudgetId::ReplayKeep.count().unwrap().get();
    db.execute("WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM seq WHERE n<?1) INSERT INTO managed_auth_requests SELECT 'fixture-signer','req-'||n,'digest-'||n,n,'nonce-'||n,'fixture','{\"statement\":{\"issued_at\":1000}}',NULL FROM seq",[keep+1]).unwrap();
    let page = vec![0xff; budgets::BudgetId::ReplayPage.bytes().unwrap().get() as usize];
    db.execute(
        "INSERT INTO managed_replay_history VALUES ('fixture-signer',0,?1)",
        [&page],
    )
    .unwrap();
    drop(db);
    let mut store = Store::open_with_backup(&path, Some(&backup)).unwrap();
    assert_eq!(
        store
            .connection()
            .query_row(
                "SELECT value FROM managed_metadata WHERE key='swarm_id'",
                [],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
        "fixture-swarm"
    );
    assert_eq!(
        store
            .connection()
            .query_row(
                "SELECT bits FROM managed_replay_history WHERE page=0",
                [],
                |r| r.get::<_, Vec<u8>>(0)
            )
            .unwrap(),
        page
    );
    assert_eq!(store.prune("fixture-signer", 2000.0).unwrap(), 1);
    rollback_verified(&backup, &t.0.join("rollback.db")).unwrap();
    let old = Connection::open(t.0.join("rollback.db")).unwrap();
    assert_eq!(
        old.query_row("PRAGMA user_version", [], |r| r.get::<_, u32>(0))
            .unwrap(),
        0
    );
}
#[test]
fn rollback_refuses_a_structurally_valid_but_changed_backup() {
    let t = Temp::new();
    let source = t.0.join("source.db");
    let backup = t.0.join("backup.db");
    let restored = t.0.join("restored.db");
    let db = Connection::open(&source).unwrap();
    db.execute_batch(
        "CREATE TABLE fixture_identity(id TEXT); INSERT INTO fixture_identity VALUES ('original');",
    )
    .unwrap();
    control_runtime::backup_verified(&db, &backup).unwrap();
    let altered = Connection::open(&backup).unwrap();
    altered
        .execute("UPDATE fixture_identity SET id='changed'", [])
        .unwrap();
    drop(altered);
    assert!(rollback_verified(&backup, &restored).is_err());
    assert!(!restored.exists());
}
#[test]
fn migration_refuses_without_backup_and_future_schema() {
    let t = Temp::new();
    let path = t.0.join("legacy.db");
    legacy(&path);
    assert!(Store::open(&path).is_err());
    Connection::open(&path)
        .unwrap()
        .execute_batch("PRAGMA user_version=99")
        .unwrap();
    assert!(Store::open_with_backup(&path, Some(&t.0.join("backup.db"))).is_err());
}
