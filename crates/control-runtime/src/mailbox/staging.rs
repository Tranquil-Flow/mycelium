// SPDX-License-Identifier: AGPL-3.0-only
//! Durable owner-local staging publication and signed target observations.
//! Source: managed_offers.py:86-341 @ f4e366e9. Staging ACKs are NOT
//! environmental cleanup proof and never release native reservations.
use super::{BINDING, Context, Mailbox, binding, bytes, contract, require, segment, text};
use crate::{Error, Result};
use budgets::{BudgetId as B, Bytes, Entries, Span};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Value, json};
mod validation;
use validation::*;

/// L1 must bind the two remaining source grammar bounds at composition. No
/// equal-valued unrelated budget or implicit default is substituted here.
#[derive(Clone, Copy)]
pub struct StagePolicy {
    pub path_characters: Entries,
    pub preposition_file_bytes: Bytes,
}
pub struct StagePublication<'a> {
    pub binding: &'a Value,
    /// Host-generated unique identity, not an arbitrary arriving path.
    pub id: &'a str,
    pub archive: &'a [u8],
    pub ttl: Span,
    pub preposition: Option<&'a Value>,
}
pub(super) fn initialize(db: &Connection) -> Result<()> {
    db.execute_batch("CREATE TABLE IF NOT EXISTS managed_stage_offers(node_id TEXT PRIMARY KEY,offer_id TEXT UNIQUE NOT NULL,expires REAL NOT NULL,metadata TEXT NOT NULL,archive BLOB NOT NULL,ack TEXT,cleanup_requested INTEGER NOT NULL DEFAULT 0,cleanup_ack TEXT);
        CREATE TABLE IF NOT EXISTS managed_stage_history(offer_id TEXT PRIMARY KEY,receipt TEXT NOT NULL);")?;
    let mut q = db.prepare("PRAGMA table_info(managed_stage_offers)")?;
    let columns = q
        .query_map([], |r| r.get::<_, String>(1))?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    for (name, declaration) in [
        ("ack", "TEXT"),
        ("cleanup_requested", "INTEGER NOT NULL DEFAULT 0"),
        ("cleanup_ack", "TEXT"),
    ] {
        if !columns.iter().any(|s| s == name) {
            db.execute_batch(&format!(
                "ALTER TABLE managed_stage_offers ADD COLUMN {name} {declaration}"
            ))?;
        }
    }
    Ok(())
}
fn encoded(v: &Value) -> Result<String> {
    String::from_utf8(bytes(v)?).map_err(|_| Error::Refused("offer_json_invalid"))
}
fn decoded(s: &str) -> Result<Value> {
    serde_json::from_str(s).map_err(|_| Error::Refused("offer_storage_invalid"))
}
fn member(db: &Connection, b: &Value, now: f64) -> Result<Value> {
    require(now.is_finite(), "managed_control_clock_invalid")?;
    let saved: Option<Vec<u8>> = db
        .query_row(
            "SELECT document FROM control_mailbox_members WHERE node=?1",
            [text(b, "target_node_id")?],
            |r| r.get(0),
        )
        .optional()?;
    let m: Value =
        serde_json::from_slice(&saved.ok_or(Error::Refused("managed_control_member_stale"))?)
            .map_err(|_| Error::Refused("managed_control_state_corrupt"))?;
    require(
        m["expires"].as_f64().is_some_and(|end| now < end),
        "managed_control_member_stale",
    )?;
    for k in &BINDING[..5] {
        require(b[*k] == m["binding"][*k], "managed_control_member_stale")?;
    }
    Ok(m)
}
struct OfferRow {
    offer: Value,
    ack: Option<Value>,
    cleanup_requested: bool,
    cleanup_ack: Option<Value>,
}
fn current(
    db: &Connection,
    b: &Value,
    now: f64,
    cleanup: bool,
    policy: StagePolicy,
) -> Result<Option<OfferRow>> {
    type Row = (String, Option<String>, bool, Option<String>, f64);
    let row: Option<Row> = db.query_row("SELECT metadata,ack,cleanup_requested,cleanup_ack,expires FROM managed_stage_offers WHERE node_id=?1",[text(b,"target_node_id")?],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?;
    let Some((meta, ack, requested, clean, end)) = row else {
        return Ok(None);
    };
    let offer = decoded(&meta)?;
    validate_offer(&offer, policy)?;
    if (!cleanup && end <= now) || BINDING[..5].iter().any(|k| offer[*k] != b[*k]) {
        return Ok(None);
    }
    Ok(Some(OfferRow {
        offer,
        ack: ack.as_deref().map(decoded).transpose()?,
        cleanup_requested: requested,
        cleanup_ack: clean.as_deref().map(decoded).transpose()?,
    }))
}
fn exact_current(
    db: &Connection,
    offer: &Value,
    now: f64,
    policy: StagePolicy,
) -> Result<OfferRow> {
    validate_offer(offer, policy)?;
    let row = current(db, offer, now, true, policy)?.ok_or(Error::Refused("offer_unavailable"))?;
    require(row.offer == *offer, "offer_unavailable")?;
    Ok(row)
}
fn history(db: &Connection, id: &str, receipt: &Value) -> Result<()> {
    let count: u64 = db.query_row("SELECT count(*) FROM managed_stage_history", [], |r| {
        r.get(0)
    })?;
    require(
        count < B::PyStageOfferHistoryCapacity.count()?.get(),
        "offer_history_capacity_exhausted",
    )?;
    db.execute(
        "INSERT INTO managed_stage_history VALUES (?1,?2)",
        params![id, encoded(receipt)?],
    )?;
    Ok(())
}
impl Mailbox {
    pub fn with_stage_policy(mut self, policy: StagePolicy) -> Result<Self> {
        require(
            policy.path_characters.get() > 0 && policy.preposition_file_bytes.get() > 0,
            "offer_policy_invalid",
        )?;
        self.stage_policy = Some(policy);
        Ok(self)
    }
    fn stage_policy(&self) -> Result<StagePolicy> {
        self.stage_policy
            .ok_or(Error::Refused("offer_policy_required"))
    }
    /// Owner-local only. One trusted clock sample after acquiring the write lock.
    pub fn publish_stage(
        &mut self,
        p: StagePublication<'_>,
        clock: impl FnOnce() -> f64,
    ) -> Result<Value> {
        let policy = self.stage_policy()?;
        require(
            !p.archive.is_empty()
                && p.archive.len() as u64
                    <= B::PyMyceliumA13MacosManagedOffersMaxArchiveL32
                        .bytes()?
                        .get(),
            "offer_archive_invalid",
        )?;
        require(
            p.ttl > Span::ZERO
                && p.ttl
                    <= B::PyUpperGuardMyceliumA13MacosManagedOffersL110C16.span(Entries::ZERO)?,
            "offer_ttl_invalid",
        )?;
        let tx = self
            .store
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let now = clock();
        member(&tx, p.binding, now)?;
        let mut offer = binding(p.binding);
        offer["offer_id"] = p.id.into();
        offer["archive_digest"] = protocol::artifact_grant::range_digest(p.archive).into();
        offer["archive_bytes"] = json!(p.archive.len());
        offer["expires_at"] = json!(now + p.ttl.millis() as f64 / 1000.0);
        if let Some(v) = p.preposition {
            let pre = preposition(v, policy)?;
            offer["protocol"] = "mycelium.managed_stage_offer.v2".into();
            offer["preposition"] = v.clone();
            offer["preposition_digest"] = protocol::artifact_grant::range_digest(&pre).into();
        }
        validate_offer(&offer, policy)?;
        tx.execute("DELETE FROM managed_stage_offers WHERE expires<=?1 AND (cleanup_requested=0 OR cleanup_ack IS NOT NULL)",[now])?;
        let prior: Option<(String, Option<String>)> = tx
            .query_row(
                "SELECT metadata,ack FROM managed_stage_offers WHERE node_id=?1",
                [text(p.binding, "target_node_id")?],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        if let Some((old, ack)) = prior {
            let old = decoded(&old)?;
            validate_offer(&old, policy)?;
            if newer_generation(
                &offer["membership_generation"],
                &old["membership_generation"],
            ) && offer["member_incarnation"] != old["member_incarnation"]
            {
                let receipt = json!({"offer":old,"stage_ack":ack.as_deref().map(decoded).transpose()?,"cleanup_ack":null,"superseded":true,"superseded_by":{"membership_generation":offer["membership_generation"],"member_incarnation":offer["member_incarnation"]}});
                history(&tx, text(&old, "offer_id")?, &receipt)?;
                tx.execute(
                    "DELETE FROM managed_stage_offers WHERE node_id=?1",
                    [text(p.binding, "target_node_id")?],
                )?;
            } else {
                return Err(Error::Refused("offer_already_pending"));
            }
        }
        let (count, total): (u64, u64) = tx.query_row(
            "SELECT count(*),coalesce(sum(length(archive)),0) FROM managed_stage_offers",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        let total_bound = B::PyMyceliumA13MacosManagedOffersMaxTotalL33.bytes()?.get();
        require(
            count
                < B::PyMyceliumA13MacosManagedOffersMaxOffersL34
                    .count()?
                    .get()
                && total
                    .checked_add(p.archive.len() as u64)
                    .is_some_and(|v| v <= total_bound),
            "offer_capacity_exhausted",
        )?;
        tx.execute("INSERT INTO managed_stage_offers(node_id,offer_id,expires,metadata,archive) VALUES(?1,?2,?3,?4,?5)",params![text(p.binding,"target_node_id")?,p.id,offer["expires_at"].as_f64(),encoded(&offer)?,p.archive])?;
        tx.commit()?;
        Ok(offer)
    }
    pub fn request_stage_cleanup(
        &mut self,
        offer: &Value,
        clock: impl FnOnce() -> f64,
    ) -> Result<()> {
        let policy = self.stage_policy()?;
        let tx = self
            .store
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let now = clock();
        member(&tx, offer, now)?;
        exact_current(&tx, offer, now, policy)?;
        tx.execute(
            "UPDATE managed_stage_offers SET cleanup_requested=1 WHERE offer_id=?1",
            [text(offer, "offer_id")?],
        )?;
        tx.commit()?;
        Ok(())
    }
    /// Retire the artifact slot after an authenticated target cleanup ACK. This
    /// preserves the original `removed: false` receipt semantics: it does NOT
    /// turn a remote assertion into local process/device cleanup authority.
    pub fn retire_stage(&mut self, offer: &Value, clock: impl FnOnce() -> f64) -> Result<Value> {
        let policy = self.stage_policy()?;
        validate_offer(offer, policy)?;
        let tx = self
            .store
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let now = clock();
        member(&tx, offer, now)?;
        let old: Option<String> = tx
            .query_row(
                "SELECT receipt FROM managed_stage_history WHERE offer_id=?1",
                [text(offer, "offer_id")?],
                |r| r.get(0),
            )
            .optional()?;
        if let Some(old) = old {
            let old = decoded(&old)?;
            require(old["offer"] == *offer, "offer_retirement_conflict")?;
            tx.commit()?;
            return Ok(old);
        }
        let row = exact_current(&tx, offer, now, policy)?;
        require(
            row.cleanup_requested && row.cleanup_ack.is_some(),
            "offer_cleanup_unconfirmed",
        )?;
        let clean = row
            .cleanup_ack
            .ok_or(Error::Refused("offer_cleanup_unconfirmed"))?;
        validate_cleanup(offer, &clean, policy)?;
        if let Some(ack) = &row.ack {
            validate_ack(offer, ack, policy)?;
        }
        let receipt = json!({"offer":offer,"stage_ack":row.ack,"cleanup_ack":clean});
        history(&tx, text(offer, "offer_id")?, &receipt)?;
        tx.execute(
            "DELETE FROM managed_stage_offers WHERE offer_id=?1",
            [text(offer, "offer_id")?],
        )?;
        tx.commit()?;
        Ok(receipt)
    }
    /// Replay-safe staging observations use fresh membership and signatures on
    /// EVERY call, not command sequence/high-water semantics. No native effects.
    pub fn dispatch_stage(&mut self, raw: &[u8], clock: impl FnOnce() -> f64) -> Result<Vec<u8>> {
        let policy = self.stage_policy()?;
        let env = protocol::strict::object(raw, protocol::managed::MAX_FRAME_BYTES)
            .map_err(|_| Error::Refused("managed_control_json_invalid"))?;
        let s = &env["statement"];
        let kind = text(s, "protocol")?;
        let cleanup = matches!(kind, CLEANUP_READ | CLEANUP_ACK);
        let acknowledge = matches!(kind, ACK | CLEANUP_ACK);
        require(
            matches!(kind, READ | ACK | CLEANUP_READ | CLEANUP_ACK),
            "managed_control_protocol_invalid",
        )?;
        let tx = self
            .store
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let now = clock();
        let m = member(&tx, s, now)?;
        protocol::managed::verify(
            &env,
            kind,
            &m["key"],
            text(&m["binding"], "target_endpoint_id")?,
            now,
            control::managed_time_limits()?,
        )
        .map_err(contract)?;
        require(
            s["swarm_id"] == self.swarm,
            "managed_control_authority_binding_invalid",
        )?;
        validate_request(s, cleanup, acknowledge)?;
        let mut row = current(&tx, s, now, cleanup, policy)?;
        if cleanup {
            if let Some(r) = &row
                && (!r.cleanup_requested || (s["offer_id"].is_null() && r.cleanup_ack.is_some()))
            {
                row = None;
            }
        } else if let Some(r) = &row {
            require(!r.cleanup_requested, "offer_cleanup_requested")?;
        }
        if !s["offer_id"].is_null() {
            require(
                row.as_ref()
                    .is_some_and(|r| r.offer["offer_id"] == s["offer_id"]),
                "offer_unavailable",
            )?;
        }
        let mut data = Value::Null;
        if acknowledge {
            let r = row.as_mut().ok_or(Error::Refused("offer_unavailable"))?;
            require(!s["offer_id"].is_null(), "offer_unavailable")?;
            let supplied = &s["ack"];
            let previous = if cleanup {
                validate_cleanup(&r.offer, supplied, policy)?;
                if let Some(ack) = &r.ack {
                    require(
                        ack["staging_root"] == supplied["staging_root"],
                        "offer_cleanup_ack_invalid",
                    )?;
                }
                &mut r.cleanup_ack
            } else {
                validate_ack(&r.offer, supplied, policy)?;
                &mut r.ack
            };
            require(
                previous.as_ref().is_none_or(|p| p == supplied),
                if cleanup {
                    "offer_cleanup_ack_conflict"
                } else {
                    "offer_ack_conflict"
                },
            )?;
            *previous = Some(supplied.clone());
            let column = if cleanup { "cleanup_ack" } else { "ack" };
            tx.execute(
                &format!("UPDATE managed_stage_offers SET {column}=?1 WHERE offer_id=?2"),
                params![encoded(supplied)?, text(&r.offer, "offer_id")?],
            )?;
        } else if !cleanup && !s["offer_id"].is_null() {
            let r = row.as_ref().ok_or(Error::Refused("offer_unavailable"))?;
            let offset = s["offset"]
                .as_u64()
                .ok_or(Error::Refused("offer_offset_invalid"))?;
            let size = r.offer["archive_bytes"]
                .as_u64()
                .ok_or(Error::Refused("offer_storage_invalid"))?;
            require(offset < size, "offer_offset_invalid")?;
            let chunk = B::PyMyceliumA13MacosManagedOffersChunkL31.bytes()?.get();
            let data_bytes: Vec<u8> = tx.query_row(
                "SELECT substr(archive,?1,?2) FROM managed_stage_offers WHERE offer_id=?3",
                params![offset + 1, chunk, text(&r.offer, "offer_id")?],
                |r| r.get(0),
            )?;
            require(
                data_bytes.len() as u64 == chunk.min(size - offset),
                "offer_storage_invalid",
            )?;
            data = base64(&data_bytes).into();
        }
        let extra = if cleanup {
            json!({"protocol":CLEANUP_RESPONSE,"offer":row.as_ref().map(|r|&r.offer),"ack":row.as_ref().and_then(|r|r.cleanup_ack.as_ref())})
        } else {
            json!({"protocol":RESPONSE,"offer":row.as_ref().map(|r|&r.offer),"offset":s["offset"],"data":data})
        };
        let c = Context {
            owner: &self.owner,
            swarm: &self.swarm,
            owner_node: &self.owner_node,
            now,
            limits: self.limits,
        };
        let response = c.response("stage", s, extra)?;
        tx.commit()?;
        Ok(response)
    }
}
