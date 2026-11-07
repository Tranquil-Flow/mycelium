// SPDX-License-Identifier: AGPL-3.0-only
//! Durable publication of L5-validated activation observations. A stored public
//! document is not a route qualification or permission to launch a worker.
use crate::{Error, Result, Store};
use rusqlite::{OptionalExtension, params};
use serde_json::Value;
impl Store {
    pub fn publish_activation(&mut self, value: &Value, expected: Option<u64>) -> Result<u64> {
        protocol::public::activation(value).map_err(|e| Error::Refused(e.0))?;
        let next = expected
            .unwrap_or(0)
            .checked_add(1)
            .ok_or(Error::Refused("revision_overflow"))?;
        let bytes = protocol::canonical::to_bytes(value)
            .map_err(|_| Error::Refused("activation_json_invalid"))?;
        if bytes.len() > protocol::managed::MAX_FRAME_BYTES {
            return Err(Error::Refused("activation_too_large"));
        }
        let tx = self
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let prior: Option<(u64, Vec<u8>)> = tx
            .query_row(
                "SELECT revision,document FROM control_state WHERE name='activation'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        if prior.as_ref().map(|(r, _)| *r) != expected {
            return Err(Error::Refused("control_state_stale_writer"));
        }
        if let Some((_, old)) = prior {
            let old: Value = serde_json::from_slice(&old)
                .map_err(|_| Error::Refused("activation_state_corrupt"))?;
            // Generations are unbounded wire integers; compare canonical decimal
            // lengths/digits, not a lossy float or a host-sized numeric cast.
            let old = old["generation"].to_string();
            let new = value["generation"].to_string();
            if (new.len(), new.as_str()) < (old.len(), old.as_str()) {
                return Err(Error::Refused("activation_generation_stale"));
            }
        }
        tx.execute("INSERT INTO control_state VALUES ('activation',?1,?2) ON CONFLICT(name) DO UPDATE SET revision=excluded.revision,document=excluded.document",params![next,bytes])?;
        tx.commit()?;
        Ok(next)
    }
}
