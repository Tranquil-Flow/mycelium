// SPDX-License-Identifier: AGPL-3.0-only
//! Already-enrolled work-control rendezvous, not a pairing request.
pub mod inbox;
use super::*;
use crate::{
    member::Session,
    run_exchange::owner_pin::{OwnerPin, VerifiedAnnouncement},
};
use stage_contract::member_work as contract;
use std::net::SocketAddr;

/// Issued only from this member's live Session and verified announcement.
pub fn announce(
    session: &Session,
    signer: &Identity,
    carrier: &Identity,
    control: &iroh::EndpointAddr,
    announcement: &VerifiedAnnouncement,
    now: f64,
) -> Result<Value> {
    let pin = OwnerPin::from_announcement(announcement).map_err(|e| fail(e.0))?;
    let registration =
        crate::joined_shard::register(session, signer, carrier, now).map_err(|e| fail(e.0))?;
    if session.seed_key_digest() != Some(pin.seed_key_digest()) {
        return Err(fail("owner_pin_announcement_unverified"));
    }
    envelope(
        signer,
        json!({"protocol":contract::PROTOCOL,"registration":registration,
        "control_endpoint_id":control.id.to_string(),
        "direct_addrs":control.ip_addrs().map(ToString::to_string).collect::<Vec<_>>(),
        "owner_endpoint_id":pin.endpoint().to_string(),
        "run_id":announcement.announcement()["run_id"],
        "deployment_id":announcement.announcement()["deployment_id"],
        "share_digest":protocol::canonical::digest(announcement.work_limits().unwrap_or(&Value::Null)).map_err(|_| fail("member_work_binding_invalid"))?,
        "issued_at":now,"expires_at":now+window()?}),
    )
}

#[derive(Clone, Debug)]
pub struct Verified {
    registration: Value,
    address: iroh::EndpointAddr,
    owner: iroh::EndpointId,
}
impl Verified {
    pub fn registration(&self) -> &Value {
        &self.registration
    }
    pub fn address(&self) -> &iroh::EndpointAddr {
        &self.address
    }
    pub fn owner(&self) -> iroh::EndpointId {
        self.owner
    }
    pub fn node_id(&self) -> &str {
        self.registration["message"]["member"]["node_id"]
            .as_str()
            .unwrap_or_default()
    }
}
/// Verification precedes writes, dial or thread creation. A raw descriptor is
/// not a rendezvous, and neither a seed row nor a valid signature alone enrolls it.
pub fn verify(
    coordinator: &seed::Coordinator,
    document: &Value,
    owner: iroh::EndpointId,
    run: &str,
    deployment: &str,
    allowed: &dyn Fn(SocketAddr) -> bool,
    now: f64,
) -> Result<Verified> {
    let message = open(document)?;
    exact(message, contract::FIELDS)?;
    if message["protocol"] != contract::PROTOCOL {
        return Err(fail("member_work_version_unsupported"));
    }
    let registration = &message["registration"];
    crate::joined_shard::verify_registration(coordinator, registration).map_err(|e| fail(e.0))?;
    let member = &registration["message"]["member"];
    if document["verification_key"]["verification_key_digest"] != member["verification_key_digest"]
        || document["signature"]["signer_endpoint_id"] != member["endpoint_id"]
    {
        return Err(fail("member_control_reply_unenrolled"));
    }
    if message["owner_endpoint_id"] != owner.to_string()
        || message["run_id"] != run
        || message["deployment_id"] != deployment
    {
        return Err(fail("member_work_run_changed"));
    }
    let issued = message["issued_at"]
        .as_f64()
        .ok_or(fail("member_work_binding_invalid"))?;
    let expires = message["expires_at"]
        .as_f64()
        .ok_or(fail("member_work_binding_invalid"))?;
    if !now.is_finite()
        || !issued.is_finite()
        || !expires.is_finite()
        || expires <= issued
        || expires - issued > window()?
        || issued > now + skew()?
        || expires <= now
    {
        return Err(fail("member_work_binding_expired"));
    }
    let text = message["control_endpoint_id"]
        .as_str()
        .ok_or(fail("member_work_binding_invalid"))?;
    let endpoint: iroh::EndpointId = text
        .parse()
        .map_err(|_| fail("member_work_binding_invalid"))?;
    if endpoint.to_string() != text {
        return Err(fail("member_work_binding_invalid"));
    }
    let addrs = message["direct_addrs"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= 8)
        .ok_or(fail("member_work_binding_invalid"))?;
    let mut address = iroh::EndpointAddr::new(endpoint);
    for addr in addrs {
        let addr: SocketAddr = addr
            .as_str()
            .and_then(|s| s.parse().ok())
            .ok_or(fail("member_work_binding_invalid"))?;
        if addr.port() == 0 || !allowed(addr) {
            return Err(fail("member_work_address_refused"));
        }
        address = address.with_ip_addr(addr);
    }
    Ok(Verified {
        registration: registration.clone(),
        address,
        owner,
    })
}
