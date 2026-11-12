// SPDX-License-Identifier: AGPL-3.0-only
//! Already-enrolled work-control rendezvous, not a pairing request.
use super::*;
use crate::{member::Session, run_exchange::owner_pin::{OwnerPin, VerifiedAnnouncement}};
use std::net::SocketAddr;
use stage_contract::member_work as contract;

/// Issued only from this member's live Session and verified announcement.
pub fn announce(session: &Session, signer: &Identity, carrier: &Identity,
    control: &iroh::EndpointAddr, announcement: &VerifiedAnnouncement, now: f64) -> Result<Value> {
    let pin = OwnerPin::from_announcement(announcement).map_err(|e| fail(e.0))?;
    let registration = crate::joined_shard::register(session, signer, carrier, now)
        .map_err(|e| fail(e.0))?;
    if session.seed_key_digest() != Some(pin.seed_key_digest()) {
        return Err(fail("owner_pin_announcement_unverified"));
    }
    envelope(signer, json!({"protocol":contract::PROTOCOL,"registration":registration,
        "control_endpoint_id":control.id.to_string(),
        "direct_addrs":control.ip_addrs().map(ToString::to_string).collect::<Vec<_>>(),
        "owner_endpoint_id":pin.endpoint().to_string(),
        "run_id":announcement.announcement()["run_id"],
        "deployment_id":announcement.announcement()["deployment_id"],
        "issued_at":now,"expires_at":now+window()?}))
}

#[derive(Clone, Debug)]
pub struct Verified {
    registration: Value,
    address: iroh::EndpointAddr,
    owner: iroh::EndpointId,
}
impl Verified {
    pub fn registration(&self) -> &Value { &self.registration }
    pub fn address(&self) -> &iroh::EndpointAddr { &self.address }
    pub fn owner(&self) -> iroh::EndpointId { self.owner }
    pub fn node_id(&self) -> &str {
        self.registration["message"]["member"]["node_id"].as_str().unwrap_or_default()
    }
}
/// Verification precedes writes, dial or thread creation. A raw descriptor is
/// not a rendezvous, and neither a seed row nor a valid signature alone enrolls it.
pub fn verify(_coordinator: &seed::Coordinator, _document: &Value,
    _owner: iroh::EndpointId, _run: &str, _deployment: &str,
    _allowed: &dyn Fn(SocketAddr) -> bool, _now: f64) -> Result<Verified> {
    Err(fail("member_work_unavailable"))
}
