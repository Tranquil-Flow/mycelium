// SPDX-License-Identifier: AGPL-3.0-only
//! Durable database-only effects. A claimed intent is never automatically retried:
//! a crash after external IO requires reconciliation by the exact execution owner.
//! This is an outbox, not an exactly-once native IO claim or execution authority.
use super::{Mailbox, bytes, require, segment};
use crate::{Error, Result};
use budgets::Entries;
use rusqlite::{Connection, OptionalExtension, Transaction, params};
use serde_json::Value;

pub(super) fn initialize(db: &Connection) -> Result<()> {
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS control_effects (
      id TEXT PRIMARY KEY, document BLOB NOT NULL, created_at REAL NOT NULL,
      state TEXT NOT NULL CHECK(state IN ('pending','claimed','completed')),
      attempt TEXT, claimed_at REAL, receipt BLOB);",
    )?;
    Ok(())
}
fn bounded(value: &Value) -> Result<Vec<u8>> {
    let data = bytes(value)?;
    require(
        data.len() <= protocol::managed::MAX_FRAME_BYTES,
        "control_effect_too_large",
    )?;
    Ok(data)
}
/// Only constructed inside authenticated dispatch, after lock/clock/auth/replay.
/// Methods mutate this transaction only; callbacks must not perform external IO
/// or update in-memory authority until dispatch has returned a committed success.
pub struct DispatchEffects<'a, 'db> {
    pub(super) tx: &'a Transaction<'db>,
    pub(super) statement: &'a Value,
    pub(super) response: &'a Value,
    pub(super) now: f64,
}
impl DispatchEffects<'_, '_> {
    /// Owner-local teardown publication only. An ACK/delivery is not teardown;
    /// the trusted callback must have independently established run retirement.
    pub fn finish_run(&self, run: &str, deployment: &str) -> Result<usize> {
        retire(self.tx, run, deployment, self.now)
    }
    /// Fail closed if a core and mailbox were opened on different SQLite files.
    pub fn belongs_to(&self, store: &crate::Store) -> bool {
        self.tx
            .path()
            .zip(store.connection.path())
            .is_some_and(|(a, b)| !a.is_empty() && a == b)
    }
    pub fn now(&self) -> f64 {
        self.now
    }
    pub fn statement(&self) -> &Value {
        self.statement
    }
    /// Validated dispatch outcome; delivery/reconciliation is not execution authority.
    pub fn response(&self) -> &Value {
        self.response
    }
    pub fn load(&self, name: &str) -> Result<Option<(u64, Vec<u8>)>> {
        Ok(self
            .tx
            .query_row(
                "SELECT revision,document FROM control_state WHERE name=?1",
                [name],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?)
    }
    /// Caller-owned namespace/CAS. Not an activation/consent validation bypass;
    /// use only after the owning core has validated its prospective state.
    pub fn publish(&self, name: &str, expected: Option<u64>, document: &Value) -> Result<u64> {
        require(segment(name), "control_effect_identifier_invalid")?;
        let next = expected
            .unwrap_or(0)
            .checked_add(1)
            .ok_or(Error::Refused("revision_overflow"))?;
        let data = bounded(document)?;
        let changed = match expected {
            None => self.tx.execute(
                "INSERT INTO control_state VALUES (?1,?2,?3) ON CONFLICT(name) DO NOTHING",
                params![name, next, data],
            )?,
            Some(old) => self.tx.execute(
                "UPDATE control_state SET revision=?1,document=?2 WHERE name=?3 AND revision=?4",
                params![next, data, name, old],
            )?,
        };
        require(changed == 1, "control_state_stale_writer")?;
        Ok(next)
    }
    /// Explicit registry-derived total capacity includes completed and uncertain
    /// records. Never evict unknown effects to admit a new command.
    /// Stable internal intent identity scoped to this authenticated statement.
    pub fn enqueue_scoped(
        &self,
        namespace: &str,
        index: usize,
        document: &Value,
        capacity: Entries,
    ) -> Result<String> {
        require(segment(namespace), "control_effect_identifier_invalid")?;
        let digest = protocol::canonical::digest(
            &serde_json::json!({"namespace":namespace,"index":index,"statement":self.statement}),
        )
        .map_err(|_| Error::Refused("control_effect_json_invalid"))?;
        let id = format!(
            "effect-{}",
            digest
                .strip_prefix("sha256:")
                .ok_or(Error::Refused("control_effect_digest_invalid"))?
        );
        self.enqueue(&id, document, capacity)?;
        Ok(id)
    }
    pub fn enqueue(&self, id: &str, document: &Value, capacity: Entries) -> Result<()> {
        require(segment(id), "control_effect_identifier_invalid")?;
        let data = bounded(document)?;
        let count: u64 = self
            .tx
            .query_row("SELECT count(*) FROM control_effects", [], |r| r.get(0))?;
        require(count < capacity.get(), "control_effect_capacity_exhausted")?;
        self.tx.execute(
            "INSERT INTO control_effects(id,document,created_at,state) VALUES (?1,?2,?3,'pending')",
            params![id, data, self.now],
        )?;
        Ok(())
    }
}
#[derive(Debug)]
pub struct DurableEffect {
    pub id: String,
    pub document: Value,
    pub created_at: f64,
    pub state: String,
    pub attempt: Option<String>,
    pub receipt: Option<Value>,
}
type Row = (
    String,
    Vec<u8>,
    f64,
    String,
    Option<String>,
    Option<Vec<u8>>,
);
fn decode(row: Row) -> Result<DurableEffect> {
    let value = |bytes: &[u8]| {
        serde_json::from_slice(bytes).map_err(|_| Error::Refused("control_effect_corrupt"))
    };
    Ok(DurableEffect {
        id: row.0,
        document: value(&row.1)?,
        created_at: row.2,
        state: row.3,
        attempt: row.4,
        receipt: row.5.map(|r| value(&r)).transpose()?,
    })
}
fn row(r: &rusqlite::Row<'_>) -> rusqlite::Result<Row> {
    Ok((
        r.get(0)?,
        r.get(1)?,
        r.get(2)?,
        r.get(3)?,
        r.get(4)?,
        r.get(5)?,
    ))
}
fn get(db: &Connection, id: &str) -> Result<Option<DurableEffect>> {
    db.query_row(
        "SELECT id,document,created_at,state,attempt,receipt FROM control_effects WHERE id=?1",
        [id],
        row,
    )
    .optional()?
    .map(decode)
    .transpose()
}
fn retire(db: &Connection, run: &str, deployment: &str, now: f64) -> Result<usize> {
    require(
        segment(run) && segment(deployment),
        "managed_control_identifier_invalid",
    )?;
    require(now.is_finite(), "managed_control_clock_invalid")?;
    Ok(db.execute("UPDATE managed_commands SET retired_at=coalesce(retired_at,?1) WHERE run_id=?2 AND deployment_id=?3",params![now,run,deployment])?)
}
impl Mailbox {
    /// Source: managed_control.py:1093-1105 @ f4e366e9. The actual run owner
    /// calls this only AFTER teardown; uncertain rows remain non-pruneable.
    pub fn finish_run(
        &mut self,
        run: &str,
        deployment: &str,
        clock: impl FnOnce() -> f64,
    ) -> Result<usize> {
        let tx = self
            .store
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let count = retire(&tx, run, deployment, clock())?;
        tx.commit()?;
        Ok(count)
    }
    /// Fresh readback; a durable claim is an uncertainty record on restart, NOT
    /// permission to resend external effects. Reconcile with the native owner.
    pub fn effect(&self, id: &str) -> Result<Option<DurableEffect>> {
        get(&self.store.connection, id)
    }
    pub fn pending_effects(&self, limit: Entries) -> Result<Vec<DurableEffect>> {
        let limit = i64::try_from(limit.get())
            .map_err(|_| Error::Refused("control_effect_limit_invalid"))?;
        let mut q = self.store.connection.prepare("SELECT id,document,created_at,state,attempt,receipt FROM control_effects WHERE state='pending' ORDER BY created_at,id LIMIT ?1")?;
        q.query_map([limit], row)?.map(|r| decode(r?)).collect()
    }
    /// Host executor must independently recheck authority/deadline before IO.
    /// Attempt is a host-generated capability, never a peer-supplied retry token.
    pub fn claim_effect(
        &mut self,
        id: &str,
        attempt: &str,
        clock: impl FnOnce() -> f64,
    ) -> Result<DurableEffect> {
        require(segment(attempt), "control_effect_attempt_invalid")?;
        let tx = self
            .store
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let now = clock();
        require(now.is_finite(), "managed_control_clock_invalid")?;
        let changed = tx.execute("UPDATE control_effects SET state='claimed',attempt=?1,claimed_at=?2 WHERE id=?3 AND state='pending'", params![attempt,now,id])?;
        require(changed == 1, "control_effect_claim_stale")?;
        let effect = get(&tx, id)?.ok_or(Error::Refused("control_effect_missing"))?;
        tx.commit()?;
        Ok(effect)
    }
    /// Exact id/attempt/result idempotence. Completion never means resource cleanup.
    pub fn complete_effect(&mut self, id: &str, attempt: &str, receipt: &Value) -> Result<()> {
        let encoded = bounded(receipt)?;
        let tx = self
            .store
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let effect = get(&tx, id)?.ok_or(Error::Refused("control_effect_missing"))?;
        require(
            effect.attempt.as_deref() == Some(attempt),
            "control_effect_claim_stale",
        )?;
        if effect.state == "completed" {
            require(
                effect.receipt.as_ref() == Some(receipt),
                "control_effect_conflicting_retry",
            )?;
        } else {
            require(effect.state == "claimed", "control_effect_claim_stale")?;
            tx.execute(
                "UPDATE control_effects SET state='completed',receipt=?1 WHERE id=?2",
                params![encoded, id],
            )?;
        }
        tx.commit()?;
        Ok(())
    }
}
