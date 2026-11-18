// SPDX-License-Identifier: AGPL-3.0-only
//! The concurrent-writer merge of the Rust seed store at m4/main 7b2c8472 (rv44), copied VERBATIM from
//! `git show 7b2c8472:crates/seed/src/store.rs` lines 541-640 (`row_key`, `member_may_replace`, `merge_snapshot`),
//! plus [`commit`]: that revision's `Store::commit` (:480-537) reduced to its SQL (the same reads, merge and writes,
//! in one `BEGIN IMMEDIATE` transaction; no invite consumption, as for a heartbeat). Lane SN uses it to replay what
//! an rv44 seed process that was already open does to a database a newer build migrated under it (review SN-01).
#![allow(dead_code)]
use rusqlite::{OptionalExtension, TransactionBehavior, params};
use serde_json::Value;

/// rv44 `Store::commit` for a member message: re-read the durable row under the write reservation, merge this
/// process's changes since `baseline` over it when another process wrote since, write the signed envelopes and the
/// snapshot row, commit. Returns the row written (rv44 then keeps `current` as its next baseline).
pub fn commit(
    connection: &mut rusqlite::Connection,
    current: &Value,
    baseline: &Value,
    envelopes: &[(String, String, String)],
) -> Value {
    let tx = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .unwrap();
    let stored: Option<String> = tx
        .query_row(
            "SELECT snapshot FROM rs_seed_table WHERE id = 1",
            [],
            |row| row.get(0),
        )
        .optional()
        .unwrap();
    let durable: Option<Value> = stored.map(|t| serde_json::from_str(&t).unwrap());
    let writes = match &durable {
        Some(d) if d != baseline => merge_snapshot(d, baseline, current).unwrap(),
        _ => current.clone(),
    };
    for (kind, key, envelope) in envelopes {
        tx.execute(
            "INSERT OR REPLACE INTO rs_seed_envelopes (kind, key, envelope) VALUES (?, ?, ?)",
            params![kind, key, envelope],
        )
        .unwrap();
    }
    tx.execute(
        "INSERT OR REPLACE INTO rs_seed_table (id, snapshot) VALUES (1, ?)",
        [writes.to_string()],
    )
    .unwrap();
    tx.commit().unwrap();
    writes
}

// ---- verbatim from 7b2c8472:crates/seed/src/store.rs:541-640 ----

/// The identity of a row of one snapshot array (the key the v0.1.4 table is unique on).
fn row_key(array: &str, row: &Value) -> String {
    let field = |name: &str| row[name].to_string();
    match array {
        "members" => field("node_id"),
        "replay" => format!(
            "{}|{}|{}",
            field("node_id"),
            field("generation"),
            field("message_id")
        ),
        "renewals" => format!(
            "{}|{}|{}",
            field("node_id"),
            field("generation"),
            field("heartbeat_message_id")
        ),
        "rotations" => field("authority_generation"),
        "acks" => format!(
            "{}|{}|{}",
            field("node_id"),
            field("member_generation"),
            field("authority_generation")
        ),
        "assignments" => field("assignment_id"),
        _ => row.to_string(),
    }
}

/// `save_member`'s `ON CONFLICT ... DO UPDATE ... WHERE`: the stored member must keep its key and endpoint, and
/// the new row must be a later generation, or the same incarnation and generation at a heartbeat sequence not
/// below the stored one.
fn member_may_replace(stored: &Value, new: &Value) -> bool {
    stored["verification_key_digest"] == new["verification_key_digest"]
        && stored["endpoint_id"] == new["endpoint_id"]
        && (new["generation"].as_u64() > stored["generation"].as_u64()
            || (new["generation"] == stored["generation"]
                && new["incarnation"] == stored["incarnation"]
                && new["last_heartbeat_sequence"].as_u64()
                    >= stored["last_heartbeat_sequence"].as_u64()))
}

/// Write `current`'s changes relative to `baseline` over `durable` (what the database holds now). `Err(())`
/// when a changed member row may not replace the stored one, or a member this process adds already exists.
fn merge_snapshot(durable: &Value, baseline: &Value, current: &Value) -> Result<Value, ()> {
    let mut merged = durable.clone();
    for array in [
        "members",
        "replay",
        "renewals",
        "join_rows",
        "resume_rows",
        "assignments",
        "rotations",
        "acks",
    ] {
        let rows = |v: &Value| -> Vec<Value> { v[array].as_array().cloned().unwrap_or_default() };
        let (base_rows, now_rows) = (rows(baseline), rows(current));
        let mut out = rows(durable);
        let keyed =
            |list: &[Value]| -> Vec<String> { list.iter().map(|r| row_key(array, r)).collect() };
        let now_keys = keyed(&now_rows);
        // Rows this process removed (an expired replay id, a pruned row) leave the durable table too.
        for (row, key) in base_rows.iter().zip(keyed(&base_rows)) {
            if !now_keys.contains(&key) {
                out.retain(|r| row_key(array, r) != key && r != row);
            }
        }
        for (row, key) in now_rows.iter().zip(&now_keys) {
            let known = base_rows.iter().find(|b| row_key(array, b) == *key);
            if known == Some(row) {
                continue;
            }
            let position = out.iter().position(|r| row_key(array, r) == *key);
            if array == "members" {
                match (position, known) {
                    (Some(at), Some(_)) if member_may_replace(&out[at], row) => {}
                    (None, _) => {}
                    _ => return Err(()),
                }
            }
            match position {
                Some(at) => out[at] = row.clone(),
                None => out.push(row.clone()),
            }
        }
        merged[array] = Value::Array(out);
    }
    let generation = |v: &Value| v["authority_generation"].as_u64().unwrap_or(1);
    merged["authority_generation"] = Value::from(generation(durable).max(generation(current)));
    let mut emitted: Vec<Value> = durable["emitted"].as_array().cloned().unwrap_or_default();
    for id in current["emitted"].as_array().into_iter().flatten() {
        if !emitted.contains(id) {
            emitted.push(id.clone());
        }
    }
    emitted.sort_by_key(|v| v.as_str().map(str::to_owned));
    merged["emitted"] = Value::Array(emitted);
    Ok(merged)
}
