// SPDX-License-Identifier: AGPL-3.0-only
//! L5/L6 discovery seam. Authentication is delegated, never reimplemented here.
use crate::placement::Member;
use crate::{Error, Result};
use budgets::{
    Authority, AuthoritySource, MonoInstant, RunDeadlines, Span, WallExpiry, WorkerWindow,
};
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MemberIdentity {
    pub node_id: String,
    pub generation: u64,
    pub incarnation: String,
    pub endpoint_id: String,
    pub verification_key_digest: String,
}
#[derive(Clone, Debug)]
pub struct VerifiedReport {
    pub identity: MemberIdentity,
    pub expires_at: WallExpiry,
    pub member: Member,
}
/// Implement in the L5 composition crate using its verified capability envelope.
/// Must authenticate signature, recipient/swarm, membership and reported caps.
/// A report constrains planning; it is NOT signed execution consent.
pub trait CapabilityVerifier {
    fn verify(
        &self,
        envelope: &[u8],
        expected: &MemberIdentity,
        now: WallExpiry,
    ) -> Result<VerifiedReport>;
}
/// Membership hardware reports do not carry consent. `caps` must come from
/// current L6 consent/local policy under the owning transaction, never request JSON.
/// The frame bound is injected by the owning transport; there is no local default.
pub struct MembershipVerifier<'a> {
    pub swarm_id: &'a str,
    pub recipient_node_id: &'a str,
    pub caps: crate::placement::Caps,
    pub backend: &'a str,
    pub precision: &'a str,
    pub maximum_bytes: usize,
}
impl CapabilityVerifier for MembershipVerifier<'_> {
    fn verify(
        &self,
        envelope: &[u8],
        expected: &MemberIdentity,
        now: WallExpiry,
    ) -> Result<VerifiedReport> {
        use budgets::{BudgetId as B, Entries};
        let span = |id: B| {
            id.span(Entries::ZERO)
                .map_err(|_| Error::new("capability_budget_invalid"))
        };
        let limits = protocol::TimeLimits {
            max_ttl_seconds: span(B::PyMyceliumMembershipContractsMaxMessageTtlSecondsL30)?.millis()
                as f64
                / 1000.0,
            clock_skew_seconds: span(B::PyMyceliumMembershipContractsMaxClockSkewSecondsL31)?
                .millis() as f64
                / 1000.0,
        };
        let env =
            protocol::strict::object(envelope, self.maximum_bytes).map_err(|e| Error::new(e.0))?;
        // All integers below 2^53 are exactly representable in the L5 float-time API.
        if now.unix_millis() > (1u64 << 53) - 1 {
            return Err(Error::new("membership_time_invalid"));
        }
        let m = protocol::membership::verify(
            &env,
            protocol::membership::Expected {
                protocol: "mycelium.membership.capability_report.v1",
                key_digest: &expected.verification_key_digest,
                swarm_id: Some(self.swarm_id),
                sender_node_id: Some(&expected.node_id),
                sender_endpoint_id: Some(&expected.endpoint_id),
                recipient_node_id: Some(self.recipient_node_id),
            },
            now.unix_millis() as f64 / 1000.0,
            limits,
        )
        .map_err(|e| Error::new(e.0))?;
        if m["generation"].as_u64() != Some(expected.generation)
            || m["incarnation"] != expected.incarnation
        {
            return Err(Error::new("member_capability_binding"));
        }
        for (field, required) in [("backends", self.backend), ("precisions", self.precision)] {
            if !m[field]
                .as_array()
                .is_some_and(|v| v.iter().any(|x| x == required))
            {
                return Err(Error::new("member_backend_ineligible"));
            }
        }
        let expires = m["expires_at"]
            .as_f64()
            .ok_or_else(|| Error::new("membership_time_invalid"))?;
        // Floor rather than round a wire expiry: precision conversion cannot add authority.
        let millis = crate::ratio::Ratio::float(expires)?
            .mul(crate::ratio::Ratio::int(1000))?
            .floor()?;
        let expires_at = WallExpiry::from_unix_millis(millis);
        expires_at
            .remaining(now)
            .map_err(|_| Error::new("member_capability_report_stale"))?;
        let integer = |name: &str| {
            m[name]
                .as_u64()
                .ok_or_else(|| Error::new("member_capability_invalid"))
        };
        Ok(VerifiedReport {
            identity: expected.clone(),
            expires_at,
            member: Member {
                node_id: expected.node_id.clone(),
                memory_bytes: integer("memory_bytes")?,
                available_storage_bytes: Some(integer("available_storage_bytes")?),
                decode_tokens_per_second: None,
                caps: Some(self.caps.clone()),
            },
        })
    }
}
#[derive(Clone, Debug)]
pub struct AdmittedMember {
    member: Member,
    identity: MemberIdentity,
    wall: Span,
    source: AuthoritySource,
    authorized_until: MonoInstant,
}
impl AdmittedMember {
    /// Intersect a report with the current durable L6 policy; never enlarge it.
    pub(crate) fn constrained(&self, caps: &node::Caps) -> Self {
        let mut out = self.clone();
        let report = self.member.caps.clone().unwrap_or_default();
        out.member.caps = Some(crate::placement::Caps {
            storage_bytes: Some(
                report
                    .storage_bytes
                    .unwrap_or(caps.storage_bytes)
                    .min(caps.storage_bytes),
            ),
            max_memory_bytes: Some(
                report
                    .max_memory_bytes
                    .unwrap_or(caps.memory_bytes)
                    .min(caps.memory_bytes),
            ),
            max_wall_seconds: Some(
                report
                    .max_wall_seconds
                    .unwrap_or(caps.wall_ms / 1000)
                    .min(caps.wall_ms / 1000),
            ),
        });
        out.wall = self.wall.min(Span::from_millis(caps.wall_ms));
        out
    }
    pub fn member(&self) -> &Member {
        &self.member
    }
    pub fn identity(&self) -> &MemberIdentity {
        &self.identity
    }
    pub fn wall(&self) -> Span {
        self.wall
    }
    pub fn source(&self) -> AuthoritySource {
        self.source
    }
    pub fn remaining(&self, now: MonoInstant) -> Result<Span> {
        self.authorized_until
            .remaining(now)
            .map_err(|_| Error::node("member_capability_report_stale", &self.identity.node_id))
    }
    pub fn current(&self, now: MonoInstant) -> Result<()> {
        self.authorized_until
            .remaining(now)
            .map_err(|_| Error::node("member_capability_report_stale", &self.identity.node_id))?;
        Ok(())
    }
}
pub fn local_owner(
    identity: MemberIdentity,
    member: Member,
    proposal: Span,
    authorized_until: MonoInstant,
    now: MonoInstant,
) -> Result<AdmittedMember> {
    if identity.node_id != member.node_id {
        return Err(Error::new("member_capability_binding"));
    }
    authorized_until
        .remaining(now)
        .map_err(|_| Error::new("owner_authority_expired"))?;
    let wall = budgets::effective_wall(Authority::OwnerLocal { proposal })
        .map_err(|_| Error::new("worker_wall_invalid"))?;
    Ok(AdmittedMember {
        member,
        identity,
        wall: wall.limit(),
        source: wall.source(),
        authorized_until,
    })
}
pub fn joined_member(
    verifier: &impl CapabilityVerifier,
    envelope: &[u8],
    expected: &MemberIdentity,
    proposal: Span,
    wall_now: WallExpiry,
    now: MonoInstant,
) -> Result<AdmittedMember> {
    let report = verifier.verify(envelope, expected, wall_now)?;
    if &report.identity != expected || report.member.node_id != expected.node_id {
        return Err(Error::node("member_capability_binding", &expected.node_id));
    }
    let remaining = report
        .expires_at
        .remaining(wall_now)
        .map_err(|_| Error::node("member_capability_report_stale", &expected.node_id))?;
    let authorized_until = now.checked_add(remaining).map_err(|_| Error::overflow())?;
    let reported_cap = report
        .member
        .caps
        .as_ref()
        .and_then(|c| c.max_wall_seconds)
        .map(|seconds| {
            seconds
                .checked_mul(1000)
                .map(Span::from_millis)
                .ok_or_else(Error::overflow)
        })
        .transpose()?;
    let wall = budgets::effective_wall(Authority::Member {
        proposal,
        reported_cap,
    })
    .map_err(|_| Error::node("worker_wall_invalid", &expected.node_id))?;
    Ok(AdmittedMember {
        member: report.member,
        identity: report.identity,
        wall: wall.limit(),
        source: wall.source(),
        authorized_until,
    })
}
pub fn worker_deadlines(
    members: &[AdmittedMember],
    origins: &[MonoInstant],
    now: MonoInstant,
) -> Result<RunDeadlines> {
    if members.len() != origins.len() {
        return Err(Error::new("worker_origin_binding"));
    }
    let mut seen = std::collections::BTreeSet::new();
    let mut windows = Vec::new();
    for (m, origin) in members.iter().zip(origins) {
        m.current(now)?;
        if !seen.insert(&m.identity.node_id) {
            return Err(Error::new("member_duplicate"));
        }
        windows.push(WorkerWindow {
            origin: *origin,
            wall: m.wall,
            authorized_until: m.authorized_until,
        });
    }
    budgets::deadlines(&windows, now).map_err(|_| Error::new("worker_deadline_invalid"))
}
