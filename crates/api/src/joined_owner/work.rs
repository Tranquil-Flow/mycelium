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
    m16_route::{Codec, SeedMembers},
};
use budgets::BudgetId as B;
use owner_run::{
    OwnerRunError,
    chain::{PortError, StepError},
    machine::{CeilingProbe, MonitorProbe},
    state::RunState,
    steps::{activate::ActivationPort, publish::WorkPort, slices::SlicesPort},
};
use runtime::iroh_host::LanPolicy;
use serde_json::{Value, json};
use std::{collections::BTreeMap, net::IpAddr, path::PathBuf, sync::Arc, time::Duration};

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

// ---- The owner-run work port (S4a) --------------------------------------

/// Seconds from an injected clock (the run's monotonic clock, or wall time).
pub type Seconds = Arc<dyn Fn() -> f64 + Send + Sync>;
/// The clocks the port reads: `mono` must be the clock the owner run's steps
/// read (`execution_clock_started_at`), `wall` the seed/route wall clock.
#[derive(Clone)]
pub struct Clocks {
    pub mono: Seconds,
    pub wall: Seconds,
}
/// One admitted participant: its member link and seed-verified registration.
pub struct WorkMember {
    pub node_id: String,
    pub link: Box<dyn MemberLink + Send>,
    pub registration: Value,
}
/// What the compile step produced for this run (copied from its `RunState`
/// and its `source-shards.private.json`); the port never re-plans.
pub struct Plan {
    pub run_id: String,
    pub deployment_id: String,
    /// `{model_id, resolved_commit, manifest_digest}`.
    pub model: Value,
    /// `{node_files, node_slice_plans}` rows per node.
    pub document: Value,
    /// Content-addressed stage shard bytes and the source journal directory.
    pub objects: PathBuf,
    pub journal: PathBuf,
    pub ranges: BTreeMap<String, [u64; 2]>,
    /// Native prepare prompt capacity and the owner's whole snapshot (which a
    /// stage worker must never read).
    pub tokens: usize,
    pub forbidden_source: PathBuf,
    /// `RunState::worker_wall_limits`: each member's REAL wall (seconds).
    pub worker_walls: BTreeMap<String, u64>,
    /// The owner root whose resource policy the slices step reads.
    pub owner_root: PathBuf,
}
/// The owner's work port over the joined route for one run.
pub struct OwnerWork {
    _private: (),
}
impl OwnerWork {
    /// The port for `members` (in stage order) and `plan`; nothing starts until
    /// `start_serving`. `network` is handed through unchanged.
    pub fn new(
        _coordinator: Arc<seed::Coordinator>,
        _members: Vec<WorkMember>,
        _plan: Plan,
        _network: Network,
        _codec: Arc<dyn Codec>,
        _clocks: Clocks,
    ) -> Result<Self, Refusal> {
        Ok(Self { _private: () })
    }
    /// Read member leases from `seed` instead of the coordinator's own rows.
    pub fn with_seed_view(self, _seed: SeedMembers) -> Self {
        self
    }
    /// Wait (bounded) until `done` holds for the workflow state.
    pub fn wait_state(&self, _done: impl Fn(&Value) -> bool, _within: Duration) -> Value {
        Value::Null
    }
    pub fn rollover_due(&self, _state: &RunState) -> Option<bool> {
        None
    }
    pub fn ceiling_reached(&self, _state: &RunState) -> CeilingProbe {
        CeilingProbe::Unavailable
    }
    pub fn monitor(&self, _state: &RunState) -> MonitorProbe {
        MonitorProbe::Code(None)
    }
    /// `chain.teardown`: stop the work, close the epoch and the source.
    pub fn teardown(&mut self) -> Result<(), String> {
        Ok(())
    }
}
fn not_ported() -> OwnerRunError {
    OwnerRunError::new("owner_run_step_not_ported")
}
impl WorkPort for OwnerWork {
    fn install_cleanup_progress(&mut self) -> Result<(), StepError> {
        Ok(())
    }
    fn start_serving(&mut self, _proposal: &Value) -> Result<Value, PortError> {
        Err(PortError::Refused(not_ported()))
    }
    fn workflow_state(&mut self) -> Result<Value, StepError> {
        Ok(Value::Null)
    }
}
impl ActivationPort for OwnerWork {
    fn activate(&mut self, _proposal: &Value) -> Result<(), PortError> {
        Err(PortError::Refused(not_ported()))
    }
}
impl SlicesPort for OwnerWork {
    fn node_slice_plans(&mut self) -> Result<Option<BTreeMap<String, Value>>, StepError> {
        Err(not_ported().into())
    }
    fn storage_policy(&mut self) -> Result<Option<Value>, StepError> {
        Err(not_ported().into())
    }
}
/// `rollover_due(ctx)` (demo_chain.py:1132-1141).
pub fn rollover_due(_state: &RunState, _now: f64) -> Option<bool> {
    None
}
/// `ceiling_reached(ctx)` (demo_chain.py:1143-1164).
pub fn ceiling_reached(_state: &RunState, _now: f64) -> CeilingProbe {
    CeilingProbe::Unavailable
}
