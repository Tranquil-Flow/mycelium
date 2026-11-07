// SPDX-License-Identifier: AGPL-3.0-only
//! Loopback enrollment composition of the unchanged v0.1.4 signed membership
//! and standing-consent contracts. Invitation delivery is local, not public pairing.
pub use crate::private_state::identity;
use crate::{loopback::Result, service};
use budgets::{BudgetId as B, Entries, WallExpiry};
use protocol::{identity::Identity, membership};
use serde_json::{Value, json};
#[derive(serde::Serialize, serde::Deserialize)]
pub struct LocalMembership {
    pub invitation: Invitation,
    pub consent: node::Consent,
}
impl LocalMembership {
    pub fn approve(
        root: &std::path::Path,
        invitation: Invitation,
        caps: node::Caps,
        approved: bool,
        now: WallExpiry,
    ) -> Result<Self> {
        let consent = node::Consent::record(
            approved,
            &node::disclosure_digest(),
            caps.clone(),
            node::Binding {
                swarm_id: invitation.swarm.clone(),
                node_id: invitation.member_node.clone(),
                seed_key_digest: invitation.owner_key["verification_key_digest"]
                    .as_str()
                    .ok_or("owner_key_missing")?
                    .into(),
            },
            &node::ConsentBounds {
                minimum_storage: budgets::Bytes::ZERO,
                policy_storage: B::FreshStorageConsent
                    .bytes()
                    .map_err(|e| format!("storage: {e:?}"))?,
                minimum_memory: budgets::Bytes::ZERO,
                maximum_memory: budgets::Bytes::new(caps.memory_bytes),
                device_memory_ceiling: budgets::Bytes::new(caps.memory_bytes),
                minimum_wall: budgets::Span::ZERO,
                maximum_wall: span(B::WorkerMaximumWall),
                maximum_commands: Entries::new(caps.commands),
            },
            now.unix_millis(),
        )
        .map_err(|e| format!("consent: {e:?}"))?;
        let local = Self {
            invitation,
            consent,
        };
        let store = crate::private_state::PrivateRoot::open(root)?;
        if !store.create("membership-choice.json", &serde_json::to_vec(&local)?)? {
            return Err("consent_already_recorded".into());
        }
        Ok(local)
    }
    pub fn load(root: &std::path::Path) -> Result<Self> {
        Self::load_optional(root)?.ok_or_else(|| "local_consent_required".into())
    }
    pub fn load_optional(root: &std::path::Path) -> Result<Option<Self>> {
        let store = crate::private_state::PrivateRoot::open(root)?;
        store
            .read(
                "membership-choice.json",
                api::requests::MAX_REQUEST_BODY as u64,
            )?
            .map(|bytes| serde_json::from_slice(&bytes).map_err(Into::into))
            .transpose()
    }
    pub fn verify_acceptance(
        &self,
        request: &Value,
        acceptance: &Value,
        now: WallExpiry,
    ) -> Result<()> {
        let invite = &self.invitation;
        let digest = invite.owner_key["verification_key_digest"]
            .as_str()
            .ok_or("owner_key_missing")?;
        let m = membership::verify(
            acceptance,
            membership::Expected {
                protocol: "mycelium.membership.join_acceptance.v1",
                key_digest: digest,
                swarm_id: Some(&invite.swarm),
                sender_node_id: Some(&invite.owner_node),
                sender_endpoint_id: Some(&invite.owner_endpoint),
                recipient_node_id: Some(&invite.member_node),
            },
            now.unix_millis() as f64 / 1000.,
            limits(),
        )?;
        if acceptance["verification_key"] != invite.owner_key
            || m["request_message_id"] != request["message"]["message_id"]
            || m["accepted_incarnation"] != request["message"]["incarnation"]
            || m["accepted_node_id"] != request["message"]["sender_node_id"]
        {
            return Err("join_acceptance_binding".into());
        }
        Ok(())
    }
}

fn span(id: B) -> budgets::Span {
    id.span(Entries::ZERO).expect("registered")
}
fn expiry(now: WallExpiry) -> Result<u64> {
    now.unix_millis()
        .checked_add(span(B::MembershipTtl).millis())
        .ok_or_else(|| "membership_expiry_overflow".into())
}
pub fn limits() -> protocol::TimeLimits {
    protocol::TimeLimits {
        max_ttl_seconds: span(B::PyMyceliumMembershipContractsMaxMessageTtlSecondsL30).millis()
            as f64
            / 1000.,
        clock_skew_seconds: span(B::PyMyceliumMembershipContractsMaxClockSkewSecondsL31).millis()
            as f64
            / 1000.,
    }
}
#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct Invitation {
    pub swarm: String,
    pub owner_node: String,
    pub owner_endpoint: String,
    pub owner_key: Value,
    pub member_node: String,
    pub member_key: Value,
    pub nonce: String,
}
pub fn join(
    key: &Identity,
    invite: &Invitation,
    incarnation: &str,
    address: &str,
    now: WallExpiry,
) -> Result<Value> {
    let m = json!({"protocol":"mycelium.membership.join_request.v1","message_id":service::random_id().map_err(|e| e.code)?,"swarm_id":invite.swarm,"sender_node_id":invite.member_node,"sender_endpoint_id":key.endpoint_id(),"recipient_node_id":invite.owner_node,"incarnation":incarnation,"generation":0,"issued_at":now.unix_millis() as f64/1000.,"expires_at":expiry(now)? as f64/1000.,"invite_nonce":invite.nonce,"endpoint_addr":{"id":key.endpoint_id(),"addrs":[address]},"software_version":"mycelium-a13-macos-demo/1","peer_class":"mac_mlx_iroh","runtime_capability":{"activation_protocol":"mycelium.router_wire.v1","runtime_backend":"mlx","transport":"iroh"}});
    membership::validate(&m, limits())?;
    signed(key, m)
}
pub fn signed(key: &Identity, message: Value) -> Result<Value> {
    Ok(
        json!({"protocol":membership::SIGNED_MESSAGE_PROTOCOL,"signature":key.sign(&message)?,"verification_key":key.public_key_record(),"message":message}),
    )
}
pub fn accept(
    key: &Identity,
    invite: &Invitation,
    request: &Value,
    generation: u64,
    now: WallExpiry,
) -> Result<Value> {
    // v0.1.4 joins are self-attested: `SeedCoordinator.accept_join` verifies the request against the
    // key it carries (`verify_join_request`) and binds that key on first contact; the invitation
    // never pins a member key or node id (the joiner chooses its node id; the acceptance is addressed
    // to it; executed: `round7-join-pin-oracle-outcomes.json`). Only the owner's own identity, the
    // swarm, the recipient and the invite nonce are pinned here, exactly as v0.1.4 refuses them.
    if key.public_key_record() != invite.owner_key || key.endpoint_id() != invite.owner_endpoint {
        return Err("join_identity_pin_mismatch".into());
    }
    let digest = request["verification_key"]["verification_key_digest"]
        .as_str()
        .unwrap_or_default();
    let m = membership::verify(
        request,
        membership::Expected {
            protocol: "mycelium.membership.join_request.v1",
            key_digest: digest,
            swarm_id: Some(&invite.swarm),
            sender_node_id: None,
            sender_endpoint_id: None,
            recipient_node_id: Some(&invite.owner_node),
        },
        now.unix_millis() as f64 / 1000.,
        limits(),
    )?;
    if m["invite_nonce"] != invite.nonce {
        return Err("join_invite_nonce_mismatch".into());
    }
    signed(
        key,
        json!({"protocol":"mycelium.membership.join_acceptance.v1","message_id":service::random_id().map_err(|e|e.code)?,"swarm_id":invite.swarm,"sender_node_id":invite.owner_node,"sender_endpoint_id":key.endpoint_id(),"recipient_node_id":m["sender_node_id"],"incarnation":"owner","generation":generation,"issued_at":now.unix_millis() as f64/1000.,"expires_at":expiry(now)? as f64/1000.,"request_message_id":m["message_id"],"accepted_node_id":m["sender_node_id"],"accepted_incarnation":m["incarnation"],"membership_generation":generation,"lease_expires_at":expiry(now)? as f64/1000.}),
    )
}
