// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's operator channel to one member, and the native-prepare step.
//!
//! A [`MemberLink`] carries member operator commands (the member process's
//! private stdin when this host owns it, or the signed remote control channel
//! `mycelium.member_control.v1`); replies are the member's own results or its
//! typed refusal code. [`prepare`] issues one single-use challenge per stage,
//! starts every stage's native prepare before waiting on any (both load in
//! parallel) and admits each member-signed resident proof against the seed.
use crate::{
    app_host, handoff,
    joined_shard::native::{Admitted, Challenges},
};
use serde_json::{Value, json};
use std::{net::SocketAddr, path::Path, time::Duration};

/// A member's refusal (its own typed code) or a link failure code.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Refusal(pub String);
impl From<crate::compiler_inputs::Error> for Refusal {
    fn from(e: crate::compiler_inputs::Error) -> Self {
        Self(e.0.to_owned())
    }
}
impl From<crate::schema::PublicError> for Refusal {
    fn from(e: crate::schema::PublicError) -> Self {
        Self(e.code.to_owned())
    }
}
pub trait MemberLink {
    /// Admit one operator command (the link assigns operation ids it needs).
    fn begin(&mut self, command: Value) -> Result<(), Refusal>;
    /// The next finished result, within `within`.
    fn finish(&mut self, within: Duration) -> Result<Value, Refusal>;
    fn call(&mut self, command: Value, within: Duration) -> Result<Value, Refusal> {
        self.begin(command)?;
        self.finish(within)
    }
}
/// One route stage: its member link, the offer it received, its assigned range.
pub struct Stage<'a> {
    pub member: &'a mut dyn MemberLink,
    pub offer: &'a Value,
    pub range: [u64; 2],
}
/// Owner-issued single-use challenge per stage; each verified proof consumes
/// its nonce. `forbidden_source` is the owner's whole snapshot, which a stage
/// worker must never read.
pub fn prepare(
    challenges: &Challenges,
    coordinator: &seed::Coordinator,
    stages: &mut [Stage<'_>],
    tokens: usize,
    forbidden_source: &Path,
    within: Duration,
) -> Result<Vec<Admitted>, Refusal> {
    for stage in stages.iter_mut() {
        let node = stage.offer["message"]["member"]["node_id"]
            .as_str()
            .ok_or_else(|| Refusal("joined_shard_invalid".into()))?;
        let nonce = challenges.issue(node, stage.range)?;
        stage
            .member
            .begin(json!({"command":"prepare_native","range":stage.range,
            "tokens":tokens,"nonce":nonce,"forbidden_source":forbidden_source}))?;
    }
    let mut admitted = Vec::with_capacity(stages.len());
    for stage in stages.iter_mut() {
        let signed = stage.member.finish(within)?;
        admitted.push(challenges.admit(coordinator, stage.offer, &signed)?);
    }
    Ok(admitted)
}
/// Admit a member into the owner's swarm: its signed pair request, the
/// owner's approval (seed-issued handoff, owner-offered `caps`, membership
/// ttl), the member's acceptance against this seed (installation CA pinned,
/// `seed_address` reachable from the member) and its signed carrier
/// registration, which must name the member's CURRENT seed row.
pub fn admit(
    member: &mut dyn MemberLink,
    owner_root: &Path,
    seed: &app_host::SeedRuntime<app_host::Unmanaged>,
    seed_address: SocketAddr,
    caps: &Value,
    within: Duration,
) -> Result<Value, Refusal> {
    let request = member.call(json!({"command":"pair_request"}), within)?;
    let (signer, _, _) = app_host::identity_in(owner_root, app_host::SEED_IDENTITY_FILE)?;
    let issuer = handoff::Coordinator {
        signer,
        swarm_id: seed.coordinator.swarm_id().into(),
        seed_url: seed.seed_url.clone(),
    };
    let ttl = budgets::BudgetId::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37
        .span(budgets::Entries::ZERO)
        .map_err(|_| Refusal("budget_unavailable".into()))?
        .millis()
        / 1000;
    let approved = handoff::approve_pairing(
        owner_root,
        true,
        Some(&issuer),
        &request,
        &json!(ttl),
        caps,
        app_host::now(),
        &handoff::Entropy::os,
    )?;
    let ca = seed
        .options()
        .ca_pem
        .and_then(|pem| String::from_utf8(pem).ok())
        .ok_or_else(|| Refusal("seed_ca_unavailable".into()))?;
    let registration = member.call(
        json!({"command":"accept","handoff":approved.to_value(),"ca_pem":ca,"seed_address":seed_address}),
        within,
    )?;
    let node = registration["message"]["member"]["node_id"]
        .as_str()
        .ok_or_else(|| Refusal("joined_shard_invalid".into()))?;
    let row = seed
        .coordinator
        .member(node)
        .map_err(|_| Refusal("joined_shard_member_changed".into()))?;
    if row["generation"] != registration["message"]["member"]["generation"] {
        return Err(Refusal("joined_shard_member_changed".into()));
    }
    Ok(registration)
}
