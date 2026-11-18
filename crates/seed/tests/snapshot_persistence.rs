// SPDX-License-Identifier: AGPL-3.0-only
//! Lane SN: the owner seed's durable state must not cost O(history) per member message.
//!
//! v0.1.4 keeps every heartbeat renewal and every emitted message id as one ROW (`mycelium_seed/state.py:112-144`
//! `seed_emitted_messages`, `seed_heartbeat_renewals`; one INSERT per heartbeat in `commit_heartbeat_renewal`,
//! state.py:1358), never pruned. The Rust store kept them inside one JSON snapshot row that every member message
//! rewrote under the coordinator lock (15.6 MB on the owner Mac of 18 Nov). These tests pin:
//! - durability and semantics across reopen, including the one-time migration of an existing single-blob
//!   snapshot (retry answers, stored renewal bytes, the emitted-id set, a failed commit leaves no effect, a
//!   failed migration leaves the old blob untouched);
//! - a bound: with ~16k renewals already stored, one heartbeat rewrites only a few pages and the stored
//!   snapshot row stays small.
//!
//! The legacy single-blob shape is `SeedTable::snapshot()` (the whole table as one document in
//! `rs_seed_table.snapshot`), which is what every Rust seed up to m4/main 7b2c8472 wrote.
use protocol::{identity::Identity, membership};
use seed::{Coordinator, Options};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

const HEARTBEAT: &str = "mycelium.membership.heartbeat.v1";
const JOIN_AT: f64 = 1000.0; // the join fixture's issued_at (an instant, not a timeout)
const KEEPALIVE: f64 = 30.0; // v0.1.4 keepalive interval (an instant step between heartbeats, not a timeout)
/// Renewals synthesized into the bound test's state: the owner Mac held 15,926 on 18 Nov.
const SYNTHETIC_RENEWALS: usize = 16_000;
/// At most this many 4 KiB pages may change for one heartbeat (256 KiB). The single-blob store rewrites the whole
/// ~15 MB snapshot (thousands of pages) per message.
const MAX_CHANGED_PAGES: usize = 64;
/// The stored snapshot row may hold the small part of the table only (members, replay window, retry rows).
const MAX_SNAPSHOT_ROW_BYTES: usize = 64 * 1024;
/// The busy timeout of the test coordinators (v0.1.4 waits 30 s): a held write lock answers quickly.
const BUSY_MS: u64 = 300;

fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
}

struct Ids {
    next: u64,
    forced: Option<String>,
}

struct World {
    dir: PathBuf,
    clock: Arc<AtomicU64>,
    ids: Arc<Mutex<Ids>>,
    member: Identity,
    seed: Option<Coordinator>,
}

impl World {
    fn new(name: &str) -> Self {
        let dir = fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!("lane-sn-{name}-{}", std::process::id()));
        if dir.exists() {
            fs::remove_dir_all(&dir).unwrap();
        }
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        let mut world = Self {
            dir,
            clock: Arc::new(AtomicU64::new(JOIN_AT.to_bits())),
            ids: Arc::new(Mutex::new(Ids {
                next: 0,
                forced: None,
            })),
            member: Identity::from_secret("endpoint-node-a", &[7; 32]).unwrap(),
            seed: None,
        };
        world.open().unwrap();
        world
    }

    fn db(&self) -> PathBuf {
        self.dir.join("seed.sqlite")
    }

    fn open(&mut self) -> Result<(), seed::SeedError> {
        self.seed = None;
        let clock = self.clock.clone();
        let ids = self.ids.clone();
        let coordinator = Coordinator::open_with_busy_timeout(
            &self.db(),
            Identity::from_secret("seed-endpoint", &[9; 32]).unwrap(),
            Options::new("swarm-fixture", "seed-node", "seed-boot").unwrap(),
            Box::new(move || f64::from_bits(clock.load(Ordering::SeqCst))),
            Box::new(move || {
                let mut ids = ids.lock().unwrap();
                if let Some(id) = ids.forced.take() {
                    return id;
                }
                ids.next += 1;
                format!("seed-message-{}", ids.next)
            }),
            ms(BUSY_MS),
        )?;
        coordinator
            .bind_seed_url("https://seed.example.test")
            .unwrap();
        self.seed = Some(coordinator);
        Ok(())
    }

    fn seed(&self) -> &Coordinator {
        self.seed.as_ref().unwrap()
    }

    fn now(&self) -> f64 {
        f64::from_bits(self.clock.load(Ordering::SeqCst))
    }

    fn advance(&self, seconds: f64) {
        self.clock
            .store((self.now() + seconds).to_bits(), Ordering::SeqCst);
    }

    fn join(&self) {
        let bundle = self.seed().mint_invite("invite-fixture", 300).unwrap();
        let fixture: Value = serde_json::from_str(include_str!(
            "../../protocol/fixtures/contracts/membership-signed-message-v1.json"
        ))
        .unwrap();
        let envelope = membership::sign(
            &self.member,
            &fixture["message"],
            seed::coordinator::limits().unwrap(),
        )
        .unwrap();
        self.seed()
            .accept_join_value(&bundle["token"], &envelope)
            .unwrap();
    }

    /// One signed heartbeat of node-a at the current clock (`MemberSession.heartbeat`'s message).
    fn heartbeat(&self, id: &str, sequence: u64, active_requests: u64) -> Value {
        let member = self.seed().member("node-a").unwrap();
        let now = self.now();
        let message = json!({
            "protocol": HEARTBEAT,
            "message_id": id,
            "swarm_id": "swarm-fixture",
            "sender_node_id": "node-a",
            "sender_endpoint_id": "endpoint-node-a",
            "recipient_node_id": "seed-node",
            "incarnation": member["incarnation"],
            "generation": member["generation"],
            "issued_at": now,
            "expires_at": now + KEEPALIVE,
            "heartbeat_sequence": sequence,
            "lifecycle_state": member["lifecycle_state"],
            "route_ready": false,
            "active_requests": active_requests,
            "liveness_source": "scheduled_heartbeat",
            "activity_receipt_digest": Value::Null,
            "activity_peer_node_id": Value::Null,
        });
        membership::sign(&self.member, &message, seed::coordinator::limits().unwrap()).unwrap()
    }

    fn receive(&self, envelope: &Value) -> Result<Value, seed::SeedError> {
        self.seed().receive_member_message(envelope, HEARTBEAT)
    }
}

impl Drop for World {
    fn drop(&mut self) {
        self.seed = None;
        fs::remove_dir_all(&self.dir).unwrap_or(());
    }
}

fn connection(db: &Path) -> rusqlite::Connection {
    rusqlite::Connection::open(db).unwrap()
}

fn table_exists(c: &rusqlite::Connection, name: &str) -> bool {
    c.query_row(
        "SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
        [name],
        |r| r.get::<_, i64>(0),
    )
    .unwrap()
        == 1
}

fn stored_snapshot_text(db: &Path) -> String {
    connection(db)
        .query_row("SELECT snapshot FROM rs_seed_table WHERE id = 1", [], |r| {
            r.get(0)
        })
        .unwrap()
}

/// The whole durable table as ONE `SeedTable::snapshot()` document, whichever way the store keeps it: the legacy
/// single blob as is, or the small snapshot row plus the renewal rows (in insertion order) and the emitted ids
/// (sorted, as the in-memory `BTreeSet`).
fn full_document(db: &Path) -> Value {
    let c = connection(db);
    let mut document: Value = serde_json::from_str(&stored_snapshot_text(db)).unwrap();
    if document.get("renewals").is_some() {
        return document;
    }
    let renewals: Vec<Value> = c
        .prepare("SELECT row FROM rs_seed_renewals ORDER BY ordinal")
        .unwrap()
        .query_map([], |r| r.get::<_, String>(0))
        .unwrap()
        .map(|t| serde_json::from_str(&t.unwrap()).unwrap())
        .collect();
    let emitted: Vec<Value> = c
        .prepare("SELECT message_id FROM rs_seed_emitted ORDER BY message_id")
        .unwrap()
        .query_map([], |r| r.get::<_, String>(0))
        .unwrap()
        .map(|t| Value::String(t.unwrap()))
        .collect();
    let map = document.as_object_mut().unwrap();
    map.remove("journals");
    map.insert("renewals".into(), Value::Array(renewals));
    map.insert("emitted".into(), Value::Array(emitted));
    document
}

/// Rewrite the database the way every Rust seed up to 7b2c8472 kept it: one snapshot row holding the whole table,
/// no row tables. A no-op on a database that is already in that shape.
fn to_legacy(db: &Path) {
    let document = full_document(db);
    let c = connection(db);
    c.execute(
        "UPDATE rs_seed_table SET snapshot = ? WHERE id = 1",
        [document.to_string()],
    )
    .unwrap();
    for table in ["rs_seed_renewals", "rs_seed_emitted"] {
        c.execute_batch(&format!("DROP TABLE IF EXISTS {table}"))
            .unwrap();
    }
}

fn pages(db: &Path) -> (usize, Vec<u8>) {
    let bytes = fs::read(db).unwrap();
    let size = u16::from_be_bytes([bytes[16], bytes[17]]) as usize;
    (if size == 1 { 65536 } else { size }, bytes)
}

fn changed_pages(before: &(usize, Vec<u8>), after: &(usize, Vec<u8>)) -> usize {
    let size = before.0;
    let longest = before.1.len().max(after.1.len());
    (0..longest.div_ceil(size))
        .filter(|page| {
            let range = |b: &[u8]| {
                let start = (page * size).min(b.len());
                let end = ((page + 1) * size).min(b.len());
                b[start..end].to_vec()
            };
            range(&before.1) != range(&after.1)
        })
        .count()
}

/// (a) Durability and semantics are unchanged across reopen, through the one-time migration of a single-blob
/// snapshot: stored renewal bytes, exact retry answers, retry mismatch, the emitted-id set, ACK only after the
/// renewal is durable, and a failed commit leaves nothing behind in memory or on disk.
#[test]
fn renewals_and_emitted_ids_survive_reopen_and_the_single_blob_migration() {
    let mut w = World::new("durable");
    w.join();
    let mut sent = Vec::new();
    let mut renewals = Vec::new();
    for sequence in 1..=3u64 {
        w.advance(KEEPALIVE);
        let id = format!("heartbeat-{sequence}");
        let envelope = w.heartbeat(&id, sequence, 0);
        w.receive(&envelope).unwrap();
        // Persist-before-ACK: once the seed answered, a separate connection reads the committed renewal.
        let durable = full_document(&w.db());
        assert!(
            durable["renewals"]
                .as_array()
                .unwrap()
                .iter()
                .any(|r| r["heartbeat_message_id"] == id.as_str()),
            "renewal of {id} durable before the answer"
        );
        renewals.push(w.seed().lease_renewal("node-a", &id).unwrap());
        sent.push((id, envelope));
    }
    let before = full_document(&w.db());
    assert_eq!(before["renewals"].as_array().unwrap().len(), 3);
    assert!(before["emitted"].as_array().unwrap().len() >= 4);

    // The database as an older seed left it: one snapshot row holding the whole table.
    w.seed = None;
    to_legacy(&w.db());
    assert!(
        serde_json::from_str::<Value>(&stored_snapshot_text(&w.db())).unwrap()["renewals"]
            .is_array()
    );
    w.open().unwrap();
    assert_eq!(
        full_document(&w.db()),
        before,
        "the migration keeps every row, in order"
    );
    let stored: Value = serde_json::from_str(&stored_snapshot_text(&w.db())).unwrap();
    assert!(
        stored.get("renewals").is_none() && stored.get("emitted").is_none(),
        "after the migration the snapshot row no longer carries the append-only collections: {}",
        stored
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect::<Vec<_>>()
            .join(",")
    );

    for _ in 0..2 {
        // Stored renewals are answered byte for byte, retries answer the committed row, a changed retry is refused.
        for ((id, envelope), renewal) in sent.iter().zip(&renewals) {
            assert_eq!(&w.seed().lease_renewal("node-a", id).unwrap(), renewal);
            assert_eq!(
                w.receive(envelope).unwrap()["message_id"],
                id.as_str(),
                "a retry of {id} is answered"
            );
        }
        let changed = w.heartbeat(&sent[1].0, 2, 1);
        assert_eq!(
            w.receive(&changed).unwrap_err().code,
            "seed_heartbeat_retry_mismatch"
        );
        // The emitted-id set is durable: the id source offering an id the seed already used is refused.
        let used = renewals[0]["message"]["message_id"]
            .as_str()
            .unwrap()
            .to_owned();
        w.ids.lock().unwrap().forced = Some(used);
        w.advance(KEEPALIVE);
        let next = w.heartbeat(&format!("heartbeat-reused-{}", w.now()), 10, 0);
        assert_eq!(w.receive(&next).unwrap_err().code, "seed_message_id_reused");
        w.seed = None;
        w.open().unwrap();
    }

    // A commit the database refuses leaves no effect: no renewal, no emitted id, and the same heartbeat is then
    // committed as new once the database is writable again.
    // A heartbeat committed since the reopen must survive the refused commit that follows it.
    w.advance(KEEPALIVE);
    w.receive(&w.heartbeat("heartbeat-before-blocked", 19, 0))
        .unwrap();
    let kept = w
        .seed()
        .lease_renewal("node-a", "heartbeat-before-blocked")
        .unwrap();
    w.advance(KEEPALIVE);
    let blocked = w.heartbeat("heartbeat-blocked", 20, 0);
    let draw_before = w.ids.lock().unwrap().next;
    let mut holder = connection(&w.db());
    let held = holder
        .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .unwrap();
    let refused = w.receive(&blocked);
    held.rollback().unwrap();
    drop(holder);
    assert_eq!(refused.unwrap_err().code, "seed_state_unavailable");
    assert_eq!(
        w.seed()
            .lease_renewal("node-a", "heartbeat-blocked")
            .unwrap_err()
            .code,
        "seed_lease_renewal_unknown",
        "memory never leads disk"
    );
    assert_eq!(
        w.seed()
            .lease_renewal("node-a", "heartbeat-before-blocked")
            .unwrap(),
        kept,
        "a refused commit forgets only its own rows"
    );
    let refused_id = format!("seed-message-{}", draw_before + 1);
    assert!(
        !full_document(&w.db())["emitted"]
            .as_array()
            .unwrap()
            .contains(&json!(refused_id)),
        "the refused commit's renewal id is not durable"
    );
    w.ids.lock().unwrap().forced = Some(refused_id.clone());
    w.receive(&blocked).unwrap();
    let renewal = w
        .seed()
        .lease_renewal("node-a", "heartbeat-blocked")
        .unwrap();
    assert_eq!(
        renewal["message"]["message_id"], refused_id,
        "the id of the refused commit was never recorded as emitted"
    );
    w.seed = None;
    w.open().unwrap();
    assert_eq!(
        w.seed()
            .lease_renewal("node-a", "heartbeat-blocked")
            .unwrap(),
        renewal
    );
    assert_eq!(
        w.seed()
            .lease_renewal("node-a", "heartbeat-before-blocked")
            .unwrap(),
        kept
    );
    let after = full_document(&w.db());
    assert_eq!(after["renewals"].as_array().unwrap().len(), 5);
    assert_eq!(
        after["renewals"].as_array().unwrap()[..3],
        before["renewals"].as_array().unwrap()[..],
        "earlier rows unchanged and in order"
    );
}

/// (a, crash safety) The migration of a single-blob snapshot is one transaction: when its last write fails, the
/// database still holds the old blob and no partial row, and the next open migrates it completely.
#[test]
fn a_failed_migration_leaves_the_single_blob_untouched() {
    let mut w = World::new("migration-abort");
    w.join();
    for sequence in 1..=2u64 {
        w.advance(KEEPALIVE);
        w.receive(&w.heartbeat(&format!("heartbeat-{sequence}"), sequence, 0))
            .unwrap();
    }
    w.seed = None;
    to_legacy(&w.db());
    let legacy = stored_snapshot_text(&w.db());
    let c = connection(&w.db());
    c.execute_batch(
        "CREATE TRIGGER lane_sn_abort_insert BEFORE INSERT ON rs_seed_table
             BEGIN SELECT RAISE(ABORT, 'lane-sn injected'); END;
         CREATE TRIGGER lane_sn_abort_update BEFORE UPDATE ON rs_seed_table
             BEGIN SELECT RAISE(ABORT, 'lane-sn injected'); END;",
    )
    .unwrap();
    let refused = w.open();
    assert!(
        refused.is_err(),
        "the migration's final snapshot write was refused, so open refuses"
    );
    assert_eq!(stored_snapshot_text(&w.db()), legacy, "old blob untouched");
    for table in ["rs_seed_renewals", "rs_seed_emitted"] {
        if table_exists(&c, table) {
            let rows: i64 = c
                .query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))
                .unwrap();
            assert_eq!(rows, 0, "{table} holds no partial migration");
        }
    }
    c.execute_batch("DROP TRIGGER lane_sn_abort_insert; DROP TRIGGER lane_sn_abort_update;")
        .unwrap();
    let legacy_document: Value = serde_json::from_str(&legacy).unwrap();
    w.open().unwrap();
    assert_eq!(full_document(&w.db()), legacy_document);
    assert_eq!(
        w.seed().lease_renewal("node-a", "heartbeat-2").unwrap()["message"]["heartbeat_message_id"],
        "heartbeat-2"
    );
}

/// (b) The bound: with ~16k renewals already stored (the owner Mac's 18 Nov state, synthesized), one heartbeat
/// rewrites a few pages, not the history; the snapshot row stays small; the rows stay durable and queryable.
#[test]
fn a_heartbeat_over_sixteen_thousand_stored_renewals_writes_only_its_own_rows() {
    let mut w = World::new("bound");
    w.join();
    w.advance(KEEPALIVE);
    w.receive(&w.heartbeat("heartbeat-1", 1, 0)).unwrap();
    w.seed = None;
    // Synthesize the history as the single blob an older seed kept (then migrated by the open below).
    to_legacy(&w.db());
    let mut document = full_document(&w.db());
    let template = document["renewals"][0].clone();
    let mut history = Vec::new();
    let mut emitted = Vec::new();
    for i in 0..SYNTHETIC_RENEWALS {
        let mut row = template.clone();
        let heartbeat = format!("synthetic-heartbeat-{i:05}");
        let renewal = format!("synthetic-renewal-{i:05}");
        row["heartbeat_message_id"] = json!(heartbeat);
        row["renewal"]["heartbeat_message_id"] = json!(heartbeat);
        row["renewal"]["message_id"] = json!(renewal);
        history.push(row);
        emitted.push(json!(renewal));
    }
    history.push(template);
    document["renewals"] = Value::Array(history);
    let all = document["emitted"].as_array_mut().unwrap();
    all.extend(emitted);
    all.sort_by_key(|v| v.as_str().unwrap().to_owned());
    connection(&w.db())
        .execute(
            "UPDATE rs_seed_table SET snapshot = ? WHERE id = 1",
            [document.to_string()],
        )
        .unwrap();
    let opened = Instant::now();
    w.open().unwrap();
    eprintln!(
        "lane-sn bound: open (incl. any migration) of {} renewals took {} ms",
        SYNTHETIC_RENEWALS + 1,
        opened.elapsed().as_millis()
    );

    let mut worst = 0;
    for sequence in 2..=4u64 {
        w.advance(KEEPALIVE);
        let envelope = w.heartbeat(&format!("heartbeat-{sequence}"), sequence, 0);
        let before = pages(&w.db());
        let started = Instant::now();
        w.receive(&envelope).unwrap();
        let took = started.elapsed();
        let changed = changed_pages(&before, &pages(&w.db()));
        eprintln!(
            "lane-sn bound: heartbeat {sequence} over {} stored renewals: {} ms under the lock, {changed} pages changed ({} KiB)",
            SYNTHETIC_RENEWALS + sequence as usize - 1,
            took.as_millis(),
            changed * before.0 / 1024
        );
        worst = worst.max(changed);
    }
    let snapshot_row = stored_snapshot_text(&w.db()).len();
    eprintln!("lane-sn bound: snapshot row {snapshot_row} bytes");
    let durable = full_document(&w.db());
    assert_eq!(
        durable["renewals"].as_array().unwrap().len(),
        SYNTHETIC_RENEWALS + 4,
        "every renewal stays durable"
    );
    assert_eq!(
        w.seed().lease_renewal("node-a", "heartbeat-4").unwrap()["message"]["heartbeat_message_id"],
        "heartbeat-4"
    );
    assert!(
        snapshot_row <= MAX_SNAPSHOT_ROW_BYTES,
        "the snapshot row holds {snapshot_row} bytes with {SYNTHETIC_RENEWALS} stored renewals (bound {MAX_SNAPSHOT_ROW_BYTES})"
    );
    assert!(
        worst <= MAX_CHANGED_PAGES,
        "one heartbeat changed {worst} pages of the database (bound {MAX_CHANGED_PAGES}): the history is rewritten per message"
    );
}
