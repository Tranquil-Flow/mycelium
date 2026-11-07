// SPDX-License-Identifier: AGPL-3.0-only
//! Pure v0.1.4 wire contracts. No runtime, filesystem, clock, or RNG ownership.
pub mod android_onboarding;
pub mod artifact_grant;
pub mod assignment;
pub mod canonical;
pub mod capability_report;
pub mod capacity;
mod checks;
pub mod consent;
pub mod evidence;
pub mod execution_graph;
pub mod explorer;
pub mod gossip;
pub mod handoff_crypto;
pub mod http_refusal;
pub mod identity;
pub mod invite;
pub mod live_status;
pub mod loaded_proof;
pub mod managed;
pub mod managed_receipt;
pub mod member_models;
pub mod membership;
pub mod native;
pub mod observation;
pub mod observatory;
pub mod operator_projection;
pub mod origin;
pub mod package_manifest;
pub mod path_manifest;
pub mod peer_profile;
pub mod performance;
pub mod plan_binding;
pub mod planner_snapshot;
pub mod product;
pub mod progress;
pub mod projection;
pub mod promotion;
pub mod provisioning;
pub mod public;
pub mod public_evidence;
pub mod qwen_stage_manifest;
pub mod recovery;
pub mod remote;
pub mod replay_checkpoint;
pub mod replica;
pub mod request;
pub mod route_assignment;
pub mod route_contract;
pub mod router_wire;
pub mod schemas;
pub mod seed_authority;
pub mod seed_coordinator;
pub mod seed_members;
pub mod seed_transition;
pub mod strict;
pub mod token;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct ContractError(pub &'static str);

/// Absolute wire seconds are not monotonic durations. Values must come from
/// the caller's budget registry; TODO(L1): register membership-message TTL and
/// artifact-specific limits (membership lease TTL is a different policy).
impl TimeLimits {
    pub fn validate(self) -> Result<(), ContractError> {
        checks::require(
            self.max_ttl_seconds.is_finite()
                && self.max_ttl_seconds > 0.0
                && self.clock_skew_seconds.is_finite()
                && self.clock_skew_seconds >= 0.0,
            "protocol_time_limits_invalid",
        )
    }
}

#[derive(Clone, Copy, Debug)]
pub struct TimeLimits {
    pub max_ttl_seconds: f64,
    pub clock_skew_seconds: f64,
}
