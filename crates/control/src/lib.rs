// SPDX-License-Identifier: AGPL-3.0-only
//! Pure authority/replay seams. No mailbox, persistence, clock sampling or worker.
//! Authentication is NOT permission to execute `native_command`; L6 must enforce
//! operation payload, consent, lifecycle and atomic durable replay policy.
use protocol::{ContractError, TimeLimits, managed};
use serde_json::Value;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MemberBinding {
    pub target_node_id: String,
    pub target_endpoint_id: String,
    pub target_key_digest: String,
    /// Canonical decimal Python integer (wire generations need not fit u64).
    pub membership_generation: String,
    pub member_incarnation: String,
    pub run_id: String,
    pub deployment_id: String,
}

pub struct ManagedContext<'a> {
    pub protocol: &'a str,
    pub signer_key: &'a Value,
    pub signer_endpoint: &'a str,
    pub swarm_id: &'a str,
    pub owner_node_id: &'a str,
    pub binding: &'a MemberBinding,
}

/// Only constructible after authenticated common/binding checks. No execution API.
#[derive(Clone, Debug)]
pub struct AuthenticatedRequest {
    statement: Value,
    signer_key_digest: String,
}
impl AuthenticatedRequest {
    pub fn statement(&self) -> &Value {
        &self.statement
    }
    pub fn signer_key_digest(&self) -> &str {
        &self.signer_key_digest
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ReplayError {
    Duplicate,
    Conflict,
    Sequence,
    HistoryUnavailable,
    Storage(&'static str),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ReplayOutcome {
    FirstSeen,
    CachedResponse(Vec<u8>),
}

/// Atomically compare/record all of request-id, nonce and sequence in the signed
/// identity scope. Preserve v0.1.4 exact-cache/idempotency and Bloom/sequence
/// history after prune/reopen. A cache miss MUST NOT imply absent history.
/// L6 owns the durable implementation; this trait supplies no permissive default.
pub trait ReplayStore {
    fn claim(&mut self, request: &AuthenticatedRequest) -> Result<ReplayOutcome, ReplayError>;
}

pub fn managed_time_limits() -> Result<TimeLimits, budgets::Refusal> {
    use budgets::{BudgetId, Entries};
    Ok(TimeLimits {
        max_ttl_seconds: BudgetId::ControlTtl.span(Entries::new(0))?.millis() as f64 / 1000.0,
        clock_skew_seconds: BudgetId::ControlClockSkew.span(Entries::new(0))?.millis() as f64
            / 1000.0,
    })
}

pub fn authenticate_request(
    env: &Value,
    context: &ManagedContext<'_>,
    now: f64,
    limits: TimeLimits,
) -> Result<AuthenticatedRequest, ContractError> {
    let statement = managed::verify(
        env,
        context.protocol,
        context.signer_key,
        context.signer_endpoint,
        now,
        limits,
    )?;
    managed::validate_request_common(&statement)?;
    if statement["swarm_id"] != context.swarm_id
        || (statement["protocol"] == "mycelium.managed_control.owner_command.v1"
            && statement["owner_node_id"] != context.owner_node_id)
    {
        return Err(ContractError("managed_control_authority_binding_invalid"));
    }
    let b = context.binding;
    for (field, expected) in [
        ("target_node_id", &b.target_node_id),
        ("target_endpoint_id", &b.target_endpoint_id),
        ("target_key_digest", &b.target_key_digest),
        ("member_incarnation", &b.member_incarnation),
    ] {
        if statement[field].as_str() != Some(expected.as_str()) {
            return Err(ContractError("managed_control_member_stale"));
        }
    }
    if statement["membership_generation"]
        .as_number()
        .map(serde_json::Number::as_str)
        != Some(b.membership_generation.as_str())
    {
        return Err(ContractError("managed_control_member_stale"));
    }
    if statement["run_id"] != b.run_id || statement["deployment_id"] != b.deployment_id {
        return Err(ContractError("managed_control_run_binding_mismatch"));
    }
    Ok(AuthenticatedRequest {
        statement,
        signer_key_digest: context.signer_key["verification_key_digest"]
            .as_str()
            .ok_or(ContractError("managed_control_key_pin_mismatch"))?
            .to_owned(),
    })
}
