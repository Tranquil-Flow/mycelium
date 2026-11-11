// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's work phase over the joined route (PH3b S4a, gap G4a).
//!
//! Per participant: the member's own consent (`consent::require`, inside
//! `source::transfer`) -> stage shard transfer over the artifact carrier ->
//! native prepare (`link::prepare`) -> seed-signed route (`route::issue`) ->
//! member `serve` -> [`Epoch::open`]; then settle and close. Lifted from the
//! operator-driven owner example (`examples/joined_owner.rs`), which now calls
//! these functions.
//!
//! The carrier network is ONE caller-chosen value ([`Network`]) handed through
//! unchanged to the shard source and the epoch carriers; nothing here binds a
//! LAN or loopback endpoint, reads an interface or branches on the choice.
use super::{
    epoch::{self, Epoch, StageCarrier},
    link::{MemberLink, Refusal},
    source::{self, ShardSource, Transferred},
};
use crate::{
    app_host,
    joined_shard::{native, route},
    m16_route::Codec,
};
use budgets::BudgetId as B;
use runtime::iroh_host::LanPolicy;
use serde_json::{Value, json};
use std::{net::IpAddr, sync::Arc, time::Duration};

/// The caller's carrier network choice, passed through unchanged (LAN on one
/// interface within a policy, or `None` = LocalOnly loopback).
pub type Network = Option<(IpAddr, LanPolicy)>;

fn refusal(code: impl Into<String>) -> Refusal {
    Refusal(code.into())
}

/// One stage's transfer from the compiled document's rows for `node_id`
/// (`node_files[node][0]`, `node_slice_plans[node]`), member consent first.
#[allow(clippy::too_many_arguments)]
pub fn transfer_stage(
    source: &ShardSource,
    coordinator: &seed::Coordinator,
    link: &mut dyn MemberLink,
    registration: &Value,
    node_id: &str,
    document: &Value,
    run: &str,
    within: Duration,
) -> Result<Transferred, Refusal> {
    source::transfer(
        source,
        coordinator,
        link,
        registration,
        run,
        &document["node_files"][node_id][0],
        &document["node_slice_plans"][node_id],
        within,
    )
}

/// One route stage the epoch delivers its route to.
pub struct EpochStage<'a> {
    pub node_id: &'a str,
    pub link: &'a mut dyn MemberLink,
    pub offer: &'a Value,
}

/// Issue the seed-signed route over the admitted stages, ask each member for
/// its carrier address, deliver the route (`serve`, the member serves until the
/// epoch ends) and open the owner's epoch on the caller's network.
#[allow(clippy::too_many_arguments)]
pub fn open_epoch(
    coordinator: &Arc<seed::Coordinator>,
    model: &Value,
    deployment_id: &str,
    epoch: u64,
    admitted: &[native::Admitted],
    stages: &mut [EpochStage<'_>],
    network: &Network,
    codec: Arc<dyn Codec>,
    within: Duration,
) -> Result<Epoch, Refusal> {
    let owner = iroh::SecretKey::generate();
    let envelope = route::issue(
        coordinator,
        model,
        deployment_id,
        epoch,
        &owner.public().to_string(),
        admitted,
        app_host::now(),
    )?;
    let mut addresses = Vec::new();
    for s in stages.iter_mut() {
        addresses.push(s.link.call(json!({"command":"carrier_address"}), within)?);
    }
    let carriers = epoch::carriers_for(
        network,
        coordinator,
        &envelope,
        &stages
            .iter()
            .zip(&addresses)
            .map(|(s, address)| StageCarrier {
                node_id: s.node_id,
                offer: s.offer,
                address,
            })
            .collect::<Vec<_>>(),
    )?;
    // Deliver the signed route; each member serves until the epoch ends.
    for s in stages.iter_mut() {
        s.link.begin(json!({"command":"serve","route":envelope}))?;
    }
    Ok(Epoch::open(
        coordinator.clone(),
        envelope,
        owner,
        carriers,
        codec,
    )?)
}

/// Wait (bounded) until the epoch's request settled (teardown finished).
pub fn wait_settled(epoch: &Epoch) -> Result<(), Refusal> {
    let poll = super::span(B::OwnerPoll)?;
    let end = std::time::Instant::now() + super::span(B::WorkerStartup)?;
    let mut seen = 0;
    while !epoch
        .handle()
        .state
        .lock()
        .map_err(|_| refusal("carrier_state_poisoned"))?
        .teardown_finished
    {
        if std::time::Instant::now() >= end {
            return Err(refusal("owner_epoch_not_settled"));
        }
        seen = epoch.handle().wait(seen, poll);
    }
    Ok(())
}

/// One stage whose serve report the owner collects.
pub struct SettleStage<'a> {
    pub node_id: &'a str,
    pub link: &'a mut dyn MemberLink,
}

/// After the epoch settled: collect each member's signed serve report, prove
/// its worker gone (member-side exact-pid probe). Returns the reports and the
/// stages' acknowledgement times; the caller closes the epoch.
pub fn settle_members(
    epoch: &Epoch,
    stages: &mut [SettleStage<'_>],
    within: Duration,
) -> Result<(Vec<Value>, Vec<Option<f64>>), Refusal> {
    let pids = epoch.worker_pids();
    let mut reports = Vec::new();
    for (m, pid) in stages.iter_mut().zip(&pids) {
        let report = m.link.finish(within)?;
        let close = std::time::Instant::now() + super::span(B::RouterClose)?;
        loop {
            let probe = m
                .link
                .call(json!({"command":"status","probe_pids":[pid]}), within)?;
            if probe["probe_alive"][pid.to_string()] == false {
                break;
            }
            if std::time::Instant::now() >= close {
                return Err(refusal("member_worker_still_alive"));
            }
        }
        reports.push(
            json!({"node_id":m.node_id,"ack_delivered":report["ack_delivered"],
            "teardown":report["teardown"],"failure":report["failure"],"worker_gone":true}),
        );
    }
    let acks: Vec<Option<f64>> = epoch
        .handle()
        .state
        .lock()
        .map_err(|_| refusal("carrier_state_poisoned"))?
        .stages
        .iter()
        .map(|s| s.acknowledged_ms)
        .collect();
    Ok((reports, acks))
}
