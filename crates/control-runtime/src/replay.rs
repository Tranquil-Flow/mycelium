// SPDX-License-Identifier: AGPL-3.0-only
use crate::{Error, Result, VerifiedRequest};
use budgets::{BudgetId as B, Entries};
use rusqlite::{OptionalExtension, Transaction, params};
use sha2::{Digest, Sha256};

fn seconds(id: B) -> Result<f64> {
    Ok(id.span(Entries::ZERO)?.millis() as f64 / 1000.0)
}
fn field<'a>(r: &'a VerifiedRequest, key: &str) -> Result<&'a str> {
    r.request.statement()[key]
        .as_str()
        .ok_or(Error::Refused("managed_control_state_corrupt"))
}

fn response_bytes(row: &rusqlite::Row<'_>, index: usize) -> rusqlite::Result<Vec<u8>> {
    // Python persisted TEXT; Rust also accepts opaque BLOB replies at this seam.
    match row.get_ref(index)? {
        rusqlite::types::ValueRef::Text(bytes) | rusqlite::types::ValueRef::Blob(bytes) => {
            Ok(bytes.to_vec())
        }
        other => Err(rusqlite::Error::InvalidColumnType(
            index,
            "response_json".into(),
            other.data_type(),
        )),
    }
}
pub(crate) fn execute(
    tx: &Transaction<'_>,
    r: &VerifiedRequest,
    now: f64,
    effect: impl FnOnce(&Transaction<'_>) -> Result<Vec<u8>>,
) -> Result<Vec<u8>> {
    execute_mode(tx, r, now, false, effect)
}

pub(crate) fn execute_mode(
    tx: &Transaction<'_>,
    r: &VerifiedRequest,
    now: f64,
    replace_empty: bool,
    effect: impl FnOnce(&Transaction<'_>) -> Result<Vec<u8>>,
) -> Result<Vec<u8>> {
    let s = r.request.statement();
    let expires = s["expires_at"]
        .as_f64()
        .ok_or(Error::Refused("managed_control_time_invalid"))?;
    let issued = s["issued_at"]
        .as_f64()
        .ok_or(Error::Refused("managed_control_time_invalid"))?;
    let skew = seconds(B::ControlClockSkew)?;
    // Re-check at the transaction boundary, not merely at signature verification.
    if !now.is_finite() || now > expires + skew || now < issued - skew {
        return Err(Error::Refused("managed_control_expired"));
    }
    let signer = r.request.signer_key_digest();
    let id = field(r, "request_id")?;
    let nonce = field(r, "nonce")?;
    let seq = s["sequence"]
        .as_i64()
        .ok_or(Error::Refused("managed_control_sequence_unrepresentable"))?;
    let prior: Option<(String, Vec<u8>)> = tx.query_row(
        "SELECT request_digest,response_json FROM managed_auth_requests WHERE signer_key_digest=?1 AND request_id=?2",
        params![signer,id], |r| Ok((r.get(0)?,response_bytes(r,1)?))).optional()?;
    if let Some((digest, response)) = prior {
        if digest != r.digest {
            return Err(Error::Refused("managed_control_conflicting_retry"));
        }
        if replace_empty {
            let value: serde_json::Value = serde_json::from_slice(&response)
                .map_err(|_| Error::Refused("managed_control_state_corrupt"))?;
            if value["statement"]["protocol"] == "mycelium.managed_control.poll_receipt.v1"
                && value["statement"]["delivery_kind"] == "empty"
            {
                let next = effect(tx)?;
                if next.is_empty() || next.len() > protocol::managed::MAX_FRAME_BYTES {
                    return Err(Error::Refused("managed_control_frame_too_large"));
                }
                tx.execute("UPDATE managed_auth_requests SET response_json=?1 WHERE signer_key_digest=?2 AND request_id=?3 AND request_digest=?4",params![next,signer,id,digest])?;
                return Ok(next);
            }
        }
        return Ok(response);
    }
    let nonce_present: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM managed_auth_requests WHERE signer_key_digest=?1 AND nonce=?2)",params![signer,nonce],|r|r.get(0))?;
    if nonce_present {
        return Err(Error::Refused("managed_control_nonce_replayed"));
    }
    let high: Option<i64> = tx
        .query_row(
            "SELECT sequence FROM control_high_water WHERE signer_key_digest=?1",
            [signer],
            |r| r.get(0),
        )
        .optional()?;
    if high.is_some_and(|n| seq <= n) {
        return Err(Error::Refused("managed_control_sequence_replayed"));
    }
    if historical(tx, signer, "nonce", nonce, false)? {
        return Err(Error::Refused("managed_control_nonce_replayed"));
    }
    if historical(tx, signer, "request", id, false)? {
        return Err(Error::Refused("managed_control_conflicting_retry"));
    }
    let response = effect(tx)?;
    // L5's frozen wire bound; request L1 binding for its unmerged frame BudgetId.
    if response.is_empty() || response.len() > protocol::managed::MAX_FRAME_BYTES {
        return Err(Error::Refused("managed_control_frame_too_large"));
    }
    tx.execute("INSERT INTO managed_auth_requests (signer_key_digest,request_id,request_digest,sequence,nonce,protocol,response_json,valid_until) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)",params![signer,id,r.digest,seq,nonce,field(r,"protocol")?,response,expires+skew])?;
    tx.execute("INSERT INTO control_high_water VALUES (?1,?2) ON CONFLICT(signer_key_digest) DO UPDATE SET sequence=excluded.sequence",params![signer,seq])?;
    let removed = prune(tx, signer, now)?;
    let capacity = B::ReplayCapacity.count()?.get();
    let over: bool = tx.query_row("SELECT EXISTS(SELECT 1 FROM managed_auth_requests WHERE signer_key_digest=?1 LIMIT 1 OFFSET ?2)",params![signer,capacity],|r|r.get(0))?;
    if over && removed == 0 {
        return Err(Error::Refused("managed_control_capacity_exhausted"));
    }
    prune_commands(tx, now)?;
    Ok(response)
}
pub(crate) fn prune_commands(db: &rusqlite::Connection, now: f64) -> Result<usize> {
    if !now.is_finite() {
        return Err(Error::Refused("managed_control_clock_invalid"));
    }
    let cutoff = now - seconds(B::RetiredCommandAge)?;
    Ok(db.execute("DELETE FROM managed_commands WHERE command_id IN (SELECT command_id FROM managed_commands WHERE retired_at IS NOT NULL AND state IN ('completed','failed','expired') AND retired_at<?1 AND updated_at<?1 ORDER BY retired_at,updated_at LIMIT ?2)",params![cutoff,B::RetentionBatch.count()?.get()])?)
}

// Exact SHA-256/domain-separated four-probe Bloom algorithm from
// mailbox_retention.py:21-43 @ f4e366e9. Never clear pages, even on reopen.
fn historical(
    tx: &Transaction<'_>,
    signer: &str,
    kind: &str,
    value: &str,
    remember: bool,
) -> Result<bool> {
    let bits = B::ReplayHistoryPerSigner
        .bytes()?
        .get()
        .checked_mul(u8::BITS.into())
        .ok_or(Error::Refused("history_size_overflow"))?;
    let page_bytes = B::ReplayPage.bytes()?.get();
    let page_bits = page_bytes
        .checked_mul(u8::BITS.into())
        .ok_or(Error::Refused("history_size_overflow"))?;
    let digest = Sha256::digest(format!("{kind}\0{value}").as_bytes());
    let mut present = true;
    // Four u32 probes are a wire algorithm, not a configurable resource budget.
    for chunk in digest[..16].chunks_exact(4) {
        let position =
            u64::from(u32::from_be_bytes([chunk[0], chunk[1], chunk[2], chunk[3]])) % bits;
        let page = position / page_bits;
        let bit = position % page_bits;
        let found: Option<Vec<u8>> = tx
            .query_row(
                "SELECT bits FROM managed_replay_history WHERE signer_key_digest=?1 AND page=?2",
                params![signer, page],
                |r| r.get(0),
            )
            .optional()?;
        let mut data = found.unwrap_or_else(|| vec![0; page_bytes as usize]);
        if data.len() as u64 != page_bytes {
            return Err(Error::Refused("managed_control_state_corrupt"));
        }
        let offset = (bit / u64::from(u8::BITS)) as usize;
        let mask = 1 << (bit % u64::from(u8::BITS));
        if data[offset] & mask == 0 {
            present = false;
            if remember {
                data[offset] |= mask;
                tx.execute("INSERT INTO managed_replay_history VALUES (?1,?2,?3) ON CONFLICT(signer_key_digest,page) DO UPDATE SET bits=excluded.bits",params![signer,page,data])?;
            }
        }
    }
    Ok(present)
}

pub(crate) fn prune(tx: &Transaction<'_>, signer: &str, now: f64) -> Result<usize> {
    if !now.is_finite() {
        return Err(Error::Refused("managed_control_clock_invalid"));
    }
    let cutoff: Option<i64> = tx.query_row("SELECT sequence FROM managed_auth_requests WHERE signer_key_digest=?1 ORDER BY sequence DESC LIMIT 1 OFFSET ?2",params![signer,B::ReplayKeep.count()?.get()],|r|r.get(0)).optional()?;
    let Some(cutoff) = cutoff else { return Ok(0) };
    let mut query = tx.prepare("SELECT request_id,nonce,valid_until,response_json FROM managed_auth_requests a WHERE signer_key_digest=?1 AND sequence<=?2 AND NOT EXISTS (SELECT 1 FROM managed_commands c WHERE c.target_key_digest=a.signer_key_digest AND c.claimed_request_id=a.request_id AND c.state IN ('claimed','reconciliation_required')) ORDER BY sequence LIMIT ?3")?;
    let rows = query
        .query_map(
            params![signer, cutoff, B::RetentionBatch.count()?.get()],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, Option<f64>>(2)?,
                    response_bytes(r, 3)?,
                ))
            },
        )?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    let mut removed = 0;
    for (id, nonce, until, response) in rows {
        let until = match until {
            Some(t) => t,
            None => {
                let value: serde_json::Value = serde_json::from_slice(&response)
                    .map_err(|_| Error::Refused("managed_control_state_corrupt"))?;
                value["statement"]["issued_at"]
                    .as_f64()
                    .ok_or(Error::Refused("managed_control_state_corrupt"))?
                    + seconds(B::ControlTtl)?
                    + seconds(B::ControlClockSkew)? * 2.0
            }
        };
        if !until.is_finite() {
            return Err(Error::Refused("managed_control_state_corrupt"));
        }
        if until < now {
            historical(tx, signer, "nonce", &nonce, true)?;
            historical(tx, signer, "request", &id, true)?;
            removed += tx.execute(
                "DELETE FROM managed_auth_requests WHERE signer_key_digest=?1 AND request_id=?2",
                params![signer, id],
            )?;
        }
    }
    Ok(removed)
}
