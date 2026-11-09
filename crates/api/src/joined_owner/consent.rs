// SPDX-License-Identifier: AGPL-3.0-only
//! Member consent on the owner's product path (gap row 12, TWO-MAC-TEST C1).
//!
//! Consent stays member-side: the member's own operator/UI decides and the
//! member binds that standing consent to the swarm it joined. The owner only
//! REQUESTS it (`consent_status` over the member channel, member-signed on the
//! remote channel) and admits the member to a transfer only when that consent
//! is granted, bound to exactly this swarm, member and seed key, and its
//! storage cap covers the bytes. The member re-checks the same consent itself
//! before accepting any byte (`joined_shard_consent_required`).
use super::fail;
use crate::schema::PublicError;
use serde_json::{Value, json};

/// The member's `consent_status` reply -> its granted, bound consent record.
/// Every refusal is `member_consent_required` (the owner never writes it).
pub fn require(
    coordinator: &seed::Coordinator,
    registration: &Value,
    status: &Value,
    bytes: u64,
) -> Result<Value, PublicError> {
    let refused = || fail("member_consent_required");
    let member = &registration["message"]["member"];
    let node = member["node_id"].as_str().ok_or_else(refused)?;
    // The registration must name a CURRENT seed member of this swarm.
    let row = coordinator.member(node).map_err(|_| refused())?;
    if member["swarm_id"] != coordinator.swarm_id()
        || member["seed_key_digest"] != coordinator.signer().verification_key_digest()
        || row["generation"] != member["generation"]
    {
        return Err(refused());
    }
    let consent = &status["consent"];
    let binding = json!({"swarm_id":member["swarm_id"],"node_id":member["node_id"],
        "seed_key_digest":member["seed_key_digest"]});
    if consent["state"] != "granted"
        || consent["binding"] != binding
        || consent["caps"]["storage_bytes"]
            .as_u64()
            .is_none_or(|cap| bytes == 0 || cap < bytes)
    {
        return Err(refused());
    }
    Ok(consent.clone())
}
