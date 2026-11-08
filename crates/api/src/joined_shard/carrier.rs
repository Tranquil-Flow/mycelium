// SPDX-License-Identifier: AGPL-3.0-only
//! Member-signed direct-LAN carrier advertisement (`mycelium.joined_shard.carrier.v1`,
//! class A, new and versioned). The member's ENROLLED key signs which socket
//! addresses its registered carrier endpoint listens on. The owner dials a route
//! stage only at addresses from an advertisement that verifies against the
//! current seed row, the seed-signed offer's registration and the owner's LAN
//! allow-list: no trust-on-first-use, no discovery. Iroh still authenticates the
//! endpoint key on every dial, so a wrong or replaced address only fails.
use super::*;
use runtime::iroh_host::LanPolicy;
use std::net::SocketAddr;

pub const CARRIER: &str = "mycelium.joined_shard.carrier.v1";
const FIELDS: &[&str] = &[
    "protocol",
    "member",
    "carrier_endpoint_id",
    "direct_addrs",
    "issued_at",
];
/// Same bound as artifact-grant source hints.
const MAX_ADDRS: usize = 8;

/// Member side: sign this live membership's carrier endpoint and bound addresses.
pub fn advertise(
    session: &Session,
    signer: &Identity,
    carrier: &Identity,
    addrs: &[SocketAddr],
    now: f64,
) -> Result<Value> {
    let member = live(session, signer, now)?;
    if carrier.verification_key_digest() != signer.verification_key_digest()
        || addrs.is_empty()
        || addrs.len() > MAX_ADDRS
        || addrs.iter().any(|a| a.port() == 0)
    {
        return Err(fail("joined_carrier_invalid"));
    }
    signed(
        signer,
        json!({"protocol":CARRIER,"member":member,"carrier_endpoint_id":carrier.endpoint_id(),
        "direct_addrs":addrs.iter().map(|a| a.to_string()).collect::<Vec<_>>(),"issued_at":now}),
    )
}
/// A carrier the owner may dial: only `verify` constructs it.
#[derive(Clone, Debug)]
pub struct VerifiedCarrier {
    node_id: String,
    endpoint: iroh::EndpointId,
    addrs: Vec<SocketAddr>,
    generation: u64,
}
impl VerifiedCarrier {
    pub fn node_id(&self) -> &str {
        &self.node_id
    }
    pub fn endpoint(&self) -> iroh::EndpointId {
        self.endpoint
    }
    pub fn addrs(&self) -> &[SocketAddr] {
        &self.addrs
    }
    pub fn generation(&self) -> u64 {
        self.generation
    }
}
/// Owner side: the advertisement must be signed by the CURRENT seed row's key
/// for the offer's member, name the offer's registered carrier endpoint, be
/// fresh, and list only addresses the LAN allow-list admits.
pub fn verify(
    coordinator: &seed::Coordinator,
    offer: &Value,
    envelope: &Value,
    policy: &LanPolicy,
    now: f64,
) -> Result<VerifiedCarrier> {
    check_owner(coordinator, offer)?;
    let member = &offer["message"]["member"];
    let row = coordinator
        .member(text(member, "node_id")?)
        .map_err(|_| fail("joined_shard_member_changed"))?;
    let message = open(envelope, CARRIER, text(&row, "verification_key_digest")?)?;
    exact(message, FIELDS)?;
    let registered = &offer["message"]["registration"]["message"]["carrier_endpoint_id"];
    if message["member"] != *member
        || envelope["signature"]["signer_endpoint_id"] != member["endpoint_id"]
        || message["carrier_endpoint_id"] != *registered
    {
        return Err(fail("joined_carrier_identity_mismatch"));
    }
    let wall = budgets::BudgetId::MemberChosenWall
        .span(budgets::Entries::ZERO)
        .map_err(|_| fail("budget_unavailable"))?;
    let skew = grant_limits()
        .map_err(|_| fail("budget_unavailable"))?
        .clock_skew_ms as f64
        / 1000.0;
    let issued = message["issued_at"]
        .as_f64()
        .ok_or(fail("joined_carrier_invalid"))?;
    if !issued.is_finite() || issued > now + skew || issued < now - wall.millis() as f64 / 1000.0 {
        return Err(fail("joined_carrier_stale"));
    }
    let listed = message["direct_addrs"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= MAX_ADDRS)
        .ok_or(fail("joined_carrier_invalid"))?;
    let mut addrs = Vec::new();
    for entry in listed {
        let addr: SocketAddr = entry
            .as_str()
            .and_then(|t| t.parse().ok())
            .ok_or(fail("joined_carrier_invalid"))?;
        if addr.port() == 0 || !policy.allows(addr.ip()) {
            return Err(fail("joined_carrier_address_refused"));
        }
        addrs.push(addr);
    }
    Ok(VerifiedCarrier {
        node_id: text(member, "node_id")?.to_owned(),
        endpoint: text(message, "carrier_endpoint_id")?
            .parse()
            .map_err(|_| fail("joined_carrier_invalid"))?,
        addrs,
        generation: member["generation"]
            .as_u64()
            .ok_or(fail("joined_carrier_invalid"))?,
    })
}
/// Member side, on an offer already admitted by `JoinedShard::begin`: the
/// seed-signed grant's source hints for `source`, filtered by the LAN allow-list.
/// A LAN member dials its source only at these addresses.
pub fn signed_source_addrs(offer: &Value, source: &str, policy: &LanPolicy) -> Vec<SocketAddr> {
    offer["message"]["grant"]["message"]["authorized_sources"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|s| s["endpoint_id"] == source)
        .flat_map(|s| s["direct_addrs"].as_array().cloned().unwrap_or_default())
        .filter_map(|a| a.as_str().and_then(|t| t.parse::<SocketAddr>().ok()))
        .filter(|a| a.port() != 0 && policy.allows(a.ip()))
        .collect()
}
