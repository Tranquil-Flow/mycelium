// SPDX-License-Identifier: AGPL-3.0-only
//! L5 authenticated owner acceptance plus explicit local consent into L6 SQLite.
use crate::loopback::Result;
use serde_json::Value;
pub struct Admission {
    pub owner: node::Binding,
    pub policy: node::Caps,
    pub member: control::MemberBinding,
    pub owner_key: Value,
    pub acceptance: Value,
    pub consent: Option<node::Consent>,
}
pub fn open(path: &std::path::Path, setup: Admission, now: node::Now) -> Result<node::Core> {
    use budgets::{BudgetId as B, Entries, WallExpiry};
    let span = |id: B| id.span(Entries::ZERO).map_err(|e| format!("budget: {e:?}"));
    if setup.owner_key["verification_key_digest"] != setup.owner.seed_key_digest {
        return Err("owner_pin_mismatch".into());
    }
    let statement = protocol::membership::verify(
        &setup.acceptance,
        protocol::membership::Expected {
            protocol: "mycelium.membership.join_acceptance.v1",
            key_digest: &setup.owner.seed_key_digest,
            swarm_id: Some(&setup.owner.swarm_id),
            sender_node_id: Some(&setup.owner.node_id),
            sender_endpoint_id: None,
            recipient_node_id: Some(&setup.member.target_node_id),
        },
        now.wall.unix_millis() as f64 / 1000.,
        protocol::TimeLimits {
            max_ttl_seconds: span(B::PyMyceliumMembershipContractsMaxMessageTtlSecondsL30)?.millis()
                as f64
                / 1000.,
            clock_skew_seconds: span(B::PyMyceliumMembershipContractsMaxClockSkewSecondsL31)?
                .millis() as f64
                / 1000.,
        },
    )?;
    if setup.acceptance["verification_key"] != setup.owner_key
        || statement["accepted_node_id"] != setup.member.target_node_id
        || statement["accepted_incarnation"] != setup.member.member_incarnation
        || statement["membership_generation"]
            .as_u64()
            .map(|g| g.to_string())
            .as_ref()
            != Some(&setup.member.membership_generation)
    {
        return Err("owner_acceptance_binding".into());
    }
    // Consume the member's separately recorded local choice, never owner policy
    // or an owner-supplied boolean. Refuse BEFORE opening SQLite.
    let consent = setup.consent.ok_or("standing_consent_required")?;
    if consent.withdrawn_at.is_some()
        || consent.disclosure_digest != node::disclosure_digest()
        || !consent.caps.positive()
        || consent.binding
            != (node::Binding {
                node_id: setup.member.target_node_id.clone(),
                ..setup.owner.clone()
            })
    {
        return Err("standing_consent_binding_invalid".into());
    }
    let millis = (statement["lease_expires_at"].as_f64().ok_or("lease_time")? * 1000.).floor();
    if !millis.is_finite() || millis < 0. || millis > ((1u64 << 53) - 1) as f64 {
        return Err("lease_time".into());
    }
    let expiry = WallExpiry::from_unix_millis(millis as u64);
    expiry
        .remaining(now.wall)
        .map_err(|e| format!("lease: {e:?}"))?;
    let (mut core, _) = node::Core::open_with_policy(
        path,
        setup.owner,
        setup.policy,
        now,
        span(B::PyBoundaryPhysicalSqliteCapacityL72C15)?,
    )
    .map_err(|e| format!("core open: {e:?}"))?;
    core.join(&setup.member)
        .map_err(|e| format!("core join: {e:?}"))?;
    core.consent(consent)
        .map_err(|e| format!("core consent: {e:?}"))?;
    core.report(
        &setup.member.target_node_id,
        &setup.member.membership_generation,
        core.state().epoch,
        expiry,
        now,
    )
    .map_err(|e| format!("core lease: {e:?}"))?;
    Ok(core)
}
