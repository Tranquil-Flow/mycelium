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
    link::{MemberLink, Refusal, Stage},
    source::{self, ShardSource, Transferred},
};
use crate::{
    app_host,
    joined_shard::{native, route},
    m16_route::{Codec, RouteHandle, SeedMembers},
};
use budgets::{BudgetId as B, Entries, Span};
use owner_run::{
    OwnerRunError,
    chain::{PortError, StepError},
    machine::{CeilingProbe, EXECUTION_CEILING_CODE, MonitorProbe},
    state::RunState,
    steps::{activate::ActivationPort, publish::WorkPort, slices::SlicesPort},
};
use runtime::iroh_host::LanPolicy;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    net::IpAddr,
    path::{Path, PathBuf},
    sync::{
        Arc, Condvar, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};

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

/// Issue the seed-signed route over the admitted stages (expiring at
/// `expires_at`, or the rehearsal default when `None`), ask each member for its
/// carrier address, deliver the route (`serve`, the member serves until the
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
    expires_at: Option<f64>,
) -> Result<Epoch, Refusal> {
    let owner = iroh::SecretKey::generate();
    let now = app_host::now();
    let owner_id = owner.public().to_string();
    // `None`: the rehearsal default (`route::issue`, MemberChosenWall from now).
    let envelope = match expires_at {
        Some(at) => route::issue_until(
            coordinator,
            model,
            deployment_id,
            epoch,
            &owner_id,
            admitted,
            now,
            at,
        ),
        None => route::issue(
            coordinator,
            model,
            deployment_id,
            epoch,
            &owner_id,
            admitted,
            now,
        ),
    }?;
    deliver(coordinator, envelope, owner, stages, network, codec, within)
}

/// Ask each member for its carrier address, deliver the signed route
/// (`serve`: the member serves until the epoch ends) and open the owner's
/// epoch on the caller's network with the route's `owner` key.
fn deliver(
    coordinator: &Arc<seed::Coordinator>,
    envelope: Value,
    owner: iroh::SecretKey,
    stages: &mut [EpochStage<'_>],
    network: &Network,
    codec: Arc<dyn Codec>,
    within: Duration,
) -> Result<Epoch, Refusal> {
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

/// What the owner asks the run's [`Admission`] to sign: a route of `protocol`
/// over the stages it admitted, for the epoch's `owner_endpoint_id`.
pub struct RouteOrder<'a> {
    pub protocol: &'a str,
    pub model: &'a Value,
    pub deployment_id: &'a str,
    pub epoch: u64,
    pub owner_endpoint_id: &'a str,
    pub now: f64,
    pub expires_at: f64,
}
/// The run's native stage admission: prepare every stage (one single-use
/// challenge each, all loading in parallel) and issue the seed-signed route over
/// the stages it admitted. Production: [`Proven`]. The composer that holds the
/// seed coordinator chooses it; a member still serves only a route naming its
/// OWN resident proof (`route::verify_member_epoch`).
pub trait Admission: Send {
    fn prepare(
        &mut self,
        stages: &mut [Stage<'_>],
        tokens: usize,
        forbidden_source: &Path,
        within: Duration,
    ) -> Result<(), Refusal>;
    fn issue(&mut self, order: &RouteOrder<'_>) -> Result<Value, Refusal>;
}
/// Admission by verified resident proofs: `link::prepare` (single-use
/// challenges, each proof checked against the seed and the native reference)
/// and `route::issue_*` over exactly those admitted stages.
pub struct Proven {
    coordinator: Arc<seed::Coordinator>,
    challenges: native::Challenges,
    admitted: Vec<native::Admitted>,
}
impl Proven {
    pub fn new(coordinator: Arc<seed::Coordinator>) -> Self {
        Self {
            coordinator,
            challenges: native::Challenges::new(),
            admitted: Vec::new(),
        }
    }
}
impl Admission for Proven {
    fn prepare(
        &mut self,
        stages: &mut [Stage<'_>],
        tokens: usize,
        forbidden_source: &Path,
        within: Duration,
    ) -> Result<(), Refusal> {
        self.admitted = super::link::prepare(
            &self.challenges,
            &self.coordinator,
            stages,
            tokens,
            forbidden_source,
            within,
        )?;
        Ok(())
    }
    fn issue(&mut self, o: &RouteOrder<'_>) -> Result<Value, Refusal> {
        let issue = if o.protocol == route::ROUTE_V2 {
            route::issue_epoch_until
        } else {
            route::issue_until
        };
        Ok(issue(
            &self.coordinator,
            o.model,
            o.deployment_id,
            o.epoch,
            o.owner_endpoint_id,
            &self.admitted,
            o.now,
            o.expires_at,
        )?)
    }
}

/// Wait (bounded) until the epoch's request settled (teardown finished).
pub fn wait_settled(epoch: &Epoch) -> Result<(), Refusal> {
    let poll = super::span(B::OwnerPoll)?;
    let end = Instant::now() + super::span(B::WorkerStartup)?;
    let mut seen = 0;
    while !epoch
        .handle()
        .state
        .lock()
        .map_err(|_| refusal("carrier_state_poisoned"))?
        .teardown_finished
    {
        if Instant::now() >= end {
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
        let close = Instant::now() + super::span(B::RouterClose)?;
        loop {
            let probe = m
                .link
                .call(json!({"command":"status","probe_pids":[pid]}), within)?;
            if probe["probe_alive"][pid.to_string()] == false {
                break;
            }
            if Instant::now() >= close {
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
/// Asks a member to cancel the operation it is running (member_control
/// `cancel_operation`); callable from any thread while the link is in use.
pub type Cancel = Arc<dyn Fn() + Send + Sync>;
/// One admitted participant: its member link, seed-verified registration and
/// the link's canceller.
pub struct WorkMember {
    pub node_id: String,
    pub link: Box<dyn MemberLink + Send>,
    pub registration: Value,
    pub cancel: Option<Cancel>,
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

// Workflow states as v0.1.4 `ManagedServingWorkflow` (workflow.py:277-345).
const PREPARED: &str = "prepared";
const PUBLISHING: &str = "publishing";
const STAGING: &str = "staging";
const AWAITING: &str = "awaiting_activation";
const STARTING: &str = "starting";
const RUNNING: &str = "api_running";
const FAILED: &str = "failed";
const STOPPED: &str = "stopped";
/// Lease-expired participant (v0.1.4 `managed_control_member_stale`).
const MEMBER_STALE: &str = "joined_route_member_stale";
/// A member stage not proven released (v0.1.4 demo_chain.py:1392-1396).
const CLEANUP_UNPROVEN: &str = "owner_target_cleanup_unproven";
/// A link's `finish` that ran out of time: the reply may still arrive.
const REPLY_TIMEOUT: &str = "member_reply_timeout";

struct Flow {
    state: &'static str,
    error: Option<String>,
    version: u64,
    transferred: Vec<Value>,
    epoch: u64,
    route: Option<RouteHandle>,
    gateway: Option<Value>,
    serve_reports: Vec<Value>,
    /// Every route of this run expires here: publish wall + worker wall.
    expires_at: Option<f64>,
    /// The closing epoch's teardown refusal, or `owner_target_cleanup_unproven`
    /// when a member's stage was not proven released (cleanup not proven).
    cleanup_error: Option<String>,
    /// Per member: its stage release on the run's exit (`discard`).
    member_cleanup: Vec<Value>,
    /// Every stage shard is on its member (verified there).
    transferred_all: bool,
}
struct Shared {
    flow: Mutex<Flow>,
    changed: Condvar,
}
impl Shared {
    fn lock(&self) -> std::sync::MutexGuard<'_, Flow> {
        self.flow.lock().unwrap_or_else(|p| p.into_inner())
    }
    fn update(&self, f: impl FnOnce(&mut Flow)) {
        let mut flow = self.lock();
        f(&mut flow);
        flow.version += 1;
        drop(flow);
        self.changed.notify_all();
    }
    fn set(&self, state: &'static str) {
        self.update(|f| f.state = state);
    }
    fn snapshot(flow: &Flow) -> Value {
        json!({"state":flow.state,"error_code":flow.error,"route_ready":false,
            "transferred":flow.transferred,"epoch":flow.epoch,"gateway":flow.gateway,
            "serve_reports":flow.serve_reports,"route_expires_at":flow.expires_at,
            "cleanup_error":flow.cleanup_error,"member_cleanup":flow.member_cleanup,
            "shards_transferred":flow.transferred_all})
    }
}
/// A closed code survives as the workflow's error code, anything else is
/// `managed_workflow_failed` (workflow.py:337-343).
fn closed(r: &Refusal) -> String {
    if owner_run::machine::closed_code(&r.0) {
        r.0.clone()
    } else {
        "managed_workflow_failed".into()
    }
}
enum Command {
    /// Publish: prepare the transferred stages; routes expire at `expires_at`.
    Prepare { expires_at: f64 },
    /// Open the next epoch (activation, and again after each spent request).
    Open,
}
struct Start {
    members: Vec<WorkMember>,
    plan: Plan,
    network: Network,
    codec: Arc<dyn Codec>,
}
/// The owner's work port over the joined route for one run.
pub struct OwnerWork {
    coordinator: Arc<seed::Coordinator>,
    clocks: Clocks,
    seed: SeedMembers,
    skew: f64,
    /// Participants in stage order with the generation their registration signed.
    stages: Vec<(String, u64)>,
    owner_root: PathBuf,
    /// `Plan::worker_walls` (route expiry at publish).
    walls: BTreeMap<String, u64>,
    shared: Arc<Shared>,
    start: Option<Start>,
    proposal: Option<Value>,
    commands: Option<mpsc::Sender<Command>>,
    stop: Arc<AtomicBool>,
    /// Each participant's canceller (Stop interrupts the member operation in flight).
    cancels: Vec<Cancel>,
    /// Set once the worker reached its exit tail (it cancels on its own from there).
    exiting: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<Vec<WorkMember>>>,
    /// Member links handed back by teardown.
    returned: Vec<WorkMember>,
    /// The run's stage admission (taken by the worker at start).
    admission: Option<Box<dyn Admission>>,
}
impl OwnerWork {
    /// The port for `members` (in stage order) and `plan`; nothing starts until
    /// `start_serving`. `network` is handed through unchanged.
    pub fn new(
        coordinator: Arc<seed::Coordinator>,
        members: Vec<WorkMember>,
        plan: Plan,
        network: Network,
        codec: Arc<dyn Codec>,
        clocks: Clocks,
    ) -> Result<Self, Refusal> {
        let stages = members
            .iter()
            .map(|m| {
                m.registration["message"]["member"]["generation"]
                    .as_u64()
                    .map(|g| (m.node_id.clone(), g))
                    .ok_or_else(|| refusal("joined_shard_invalid"))
            })
            .collect::<Result<Vec<_>, _>>()?;
        let skew = super::span(B::PyMyceliumMembershipContractsMaxClockSkewSecondsL31)?;
        let cancels = members.iter().filter_map(|m| m.cancel.clone()).collect();
        let admission: Box<dyn Admission> = Box::new(Proven::new(coordinator.clone()));
        Ok(Self {
            seed: epoch::seed_members(&coordinator),
            coordinator,
            clocks,
            skew: skew.as_secs_f64(),
            stages,
            owner_root: plan.owner_root.clone(),
            walls: plan.worker_walls.clone(),
            shared: Arc::new(Shared {
                flow: Mutex::new(Flow {
                    state: PREPARED,
                    error: None,
                    version: 0,
                    transferred: Vec::new(),
                    epoch: 0,
                    route: None,
                    gateway: None,
                    serve_reports: Vec::new(),
                    expires_at: None,
                    cleanup_error: None,
                    member_cleanup: Vec::new(),
                    transferred_all: false,
                }),
                changed: Condvar::new(),
            }),
            start: Some(Start {
                members,
                plan,
                network,
                codec,
            }),
            proposal: None,
            commands: None,
            stop: Arc::new(AtomicBool::new(false)),
            cancels,
            exiting: Arc::new(AtomicBool::new(false)),
            thread: None,
            returned: Vec::new(),
            admission: Some(admission),
        })
    }
    /// Admit the run's stages through `admission` instead of [`Proven`].
    pub fn with_admission(mut self, admission: Box<dyn Admission>) -> Self {
        self.admission = Some(admission);
        self
    }
    /// Read member leases from `seed` (an injected view) instead of the
    /// coordinator's own rows at its clock.
    pub fn with_seed_view(mut self, seed: SeedMembers) -> Self {
        self.seed = seed;
        self
    }
    /// The workflow state document (as `workflow_state`).
    pub fn state(&self) -> Value {
        Shared::snapshot(&self.shared.lock())
    }
    /// Wait (bounded by `within`) until `done` holds for the workflow state;
    /// returns the last state read.
    pub fn wait_state(&self, done: impl Fn(&Value) -> bool, within: Duration) -> Value {
        let end = Instant::now() + within;
        let mut flow = self.shared.lock();
        loop {
            let state = Shared::snapshot(&flow);
            let left = end.saturating_duration_since(Instant::now());
            if done(&state) || left.is_zero() {
                return state;
            }
            flow = self
                .shared
                .changed
                .wait_timeout(flow, left)
                .unwrap_or_else(|p| p.into_inner())
                .0;
        }
    }
    /// The open epoch's loopback gateway `{addr, bearer, qualification}`.
    pub fn gateway(&self) -> Option<Value> {
        self.shared.lock().gateway.clone()
    }
    /// The open epoch is single-use (U5, per-request epochs): after its
    /// request settled, retire it and open the next one over freshly prepared
    /// stages.
    pub fn next_epoch(&self) -> Result<(), Refusal> {
        if self.shared.lock().state != RUNNING {
            return Err(refusal("route_not_serving"));
        }
        self.send(Command::Open)
    }
    fn send(&self, command: Command) -> Result<(), Refusal> {
        self.commands
            .as_ref()
            .ok_or_else(|| refusal("managed_workflow_stopped"))?
            .send(command)
            .map_err(|_| refusal("managed_workflow_stopped"))
    }
    /// `rollover_due(ctx)` on the run's clock.
    pub fn rollover_due(&self, state: &RunState) -> Option<bool> {
        rollover_due(state, (self.clocks.mono)())
    }
    /// `ceiling_reached(ctx)` on the run's clock.
    pub fn ceiling_reached(&self, state: &RunState) -> CeilingProbe {
        ceiling_reached(state, (self.clocks.mono)())
    }
    /// `monitor(ctx)` (demo_chain.py:1166-1186): the ceiling first, then the
    /// workflow's own failure, then a participant whose seed lease ran out
    /// (the open epoch's death evidence, read by its pump each OwnerPoll; with
    /// no epoch open, the same seed-row rule as `observe_member_leases`).
    pub fn monitor(&self, state: &RunState) -> MonitorProbe {
        if let CeilingProbe::Code(Some(code)) = self.ceiling_reached(state) {
            return MonitorProbe::Code(Some(code));
        }
        let (current, error, route) = {
            let flow = self.shared.lock();
            (flow.state, flow.error.clone(), flow.route.clone())
        };
        if current == FAILED || current == STOPPED {
            return MonitorProbe::Code(Some(
                error.unwrap_or_else(|| "managed_workflow_stopped".into()),
            ));
        }
        let lease_evidence = route.is_some_and(|r| {
            r.state.lock().is_ok_and(|o| {
                o.stages.iter().any(|s| {
                    s.death_evidence
                        .as_ref()
                        .is_some_and(|e| e["source"] == "seed_member_lease")
                })
            })
        });
        if lease_evidence
            || self
                .stages
                .iter()
                .any(|(node, generation)| self.lease_ran_out(node, *generation))
        {
            return MonitorProbe::Code(Some(MEMBER_STALE.into()));
        }
        MonitorProbe::Code(None)
    }
    /// The member's CURRENT seed row is the one the run admitted and the seed
    /// clock is past its lease plus the members' clock skew (the member's own
    /// fence point): `m16_route::observe_member_leases`' rule.
    fn lease_ran_out(&self, node: &str, generation: u64) -> bool {
        let Some((row, now)) = (self.seed)(node) else {
            return false;
        };
        row["node_id"] == node
            && row["generation"].as_u64() == Some(generation)
            && now.is_finite()
            && row["lease_expires_at"]
                .as_f64()
                .filter(|l| l.is_finite())
                .is_some_and(|lease| now >= lease + self.skew)
    }
    /// `chain.teardown`: stop the work, interrupting the member operation in
    /// flight (F4: `cancel_operation`, re-sent every OwnerPoll because a cancel
    /// reaches only an admitted operation, until the worker reached its exit
    /// tail), close the open epoch and the shard source, release the members'
    /// stage (R1, bounded by the stop allowance) and join the worker. Idempotent.
    pub fn teardown(&mut self) -> Result<(), String> {
        self.stop.store(true, Ordering::SeqCst);
        self.commands.take();
        if let Some(start) = self.start.take() {
            self.returned.extend(start.members);
        }
        if let Some(t) = self.thread.take() {
            let poll = super::span(B::OwnerPoll);
            if let Err(e) = &poll {
                eprintln!("owner work stop: {} (one cancel round only)", e.code);
            }
            loop {
                if !self.exiting.load(Ordering::SeqCst) {
                    for cancel in &self.cancels {
                        cancel();
                    }
                }
                let Ok(poll) = poll else { break };
                if t.is_finished() || self.exiting.load(Ordering::SeqCst) {
                    break;
                }
                self.wait_state(|s| s["state"] == FAILED || s["state"] == STOPPED, poll);
            }
            let members = t.join().map_err(|_| "ThreadPanicked".to_owned())?;
            self.returned.extend(members);
        }
        Ok(())
    }
    /// The Stop the host signals while a step blocks in this port (the slices
    /// step's push, F5): stops the work and cancels the member operation in
    /// flight; the step then ends `Stopped` and `teardown` follows. Callable
    /// from any thread (the owner run's Stop, owner_run.py:275-283).
    pub fn stopper(&self) -> impl Fn() + Send + Sync + 'static {
        let (stop, cancels, shared) =
            (self.stop.clone(), self.cancels.clone(), self.shared.clone());
        move || {
            stop.store(true, Ordering::SeqCst);
            for cancel in &cancels {
                cancel();
            }
            shared.changed.notify_all();
        }
    }
    /// Start the owned worker: consent and shard push per participant, then
    /// it waits for publish (`Command::Prepare`).
    fn spawn(&mut self) -> Result<(), Refusal> {
        let Some(start) = self.start.take() else {
            return Err(refusal("managed_workflow_not_startable"));
        };
        let Some(wall) = start.plan.worker_walls.values().copied().min() else {
            self.start = Some(start);
            return Err(refusal("member_wall_limit_invalid"));
        };
        let Some(admission) = self.admission.take() else {
            self.start = Some(start);
            return Err(refusal("managed_workflow_not_startable"));
        };
        let within = super::span(B::MemberChosenWall)?;
        let stop_allowance = stop_allowance(wall, start.members.len())
            .ok_or_else(|| refusal("budget_unavailable"))?;
        let (commands, inbox) = mpsc::channel();
        let worker = Worker {
            shared: self.shared.clone(),
            coordinator: self.coordinator.clone(),
            members: start.members.into_iter().map(Member::from).collect(),
            plan: start.plan,
            network: start.network,
            codec: start.codec,
            admission,
            stop: self.stop.clone(),
            exiting: self.exiting.clone(),
            inbox,
            within,
            serve_within: Duration::from_secs(wall),
            stop_allowance,
        };
        let thread = std::thread::Builder::new()
            .name("joined-owner-work".into())
            .spawn(move || worker.run())
            .map_err(|_| refusal("managed_workflow_failed"))?;
        self.thread = Some(thread);
        self.commands = Some(commands);
        Ok(())
    }
    /// Wait until every shard is pushed, the work ended, a Stop was signalled
    /// or `within` ran out; re-sends the cancels while stopping.
    fn wait_transferred(&self, within: Duration) -> Result<(), StepError> {
        let poll = super::span(B::OwnerPoll).map_err(|e| step_refusal(e.code))?;
        let end = Instant::now() + within;
        loop {
            if self.stop.load(Ordering::SeqCst) {
                return Err(StepError::Stopped);
            }
            let state = self.wait_state(
                |s| {
                    s["shards_transferred"] == true || s["state"] == FAILED || s["state"] == STOPPED
                },
                poll.min(end.saturating_duration_since(Instant::now())),
            );
            if state["shards_transferred"] == true {
                return Ok(());
            }
            if self.stop.load(Ordering::SeqCst) || state["state"] == STOPPED {
                return Err(StepError::Stopped);
            }
            if state["state"] == FAILED {
                return Err(step_refusal(
                    state["error_code"]
                        .as_str()
                        .unwrap_or("managed_workflow_stopped"),
                ));
            }
            if Instant::now() >= end {
                return Err(step_refusal("member_slice_timeout"));
            }
        }
    }
    /// The member links, handed back after [`OwnerWork::teardown`] (the run
    /// never closes them: members stay joined).
    pub fn take_members(&mut self) -> Vec<WorkMember> {
        std::mem::take(&mut self.returned)
    }
}
impl Drop for OwnerWork {
    fn drop(&mut self) {
        if let Err(e) = self.teardown() {
            eprintln!("owner work teardown: {e}");
        }
    }
}
fn refused(code: &str) -> PortError {
    PortError::Refused(OwnerRunError::new(code))
}
fn step_refusal(code: &str) -> StepError {
    StepError::Refused(OwnerRunError::new(code))
}
impl WorkPort for OwnerWork {
    /// The joined route has no local cleanup tick: members own their cleanup.
    fn install_cleanup_progress(&mut self) -> Result<(), StepError> {
        Ok(())
    }
    /// `ChainOps.start_serving`: start the owned worker (consent and transfer
    /// per participant, then native prepare) and return at once; the route's
    /// gateway exists only after activation, so `registry_origin` is null.
    fn start_serving(&mut self, proposal: &Value) -> Result<Value, PortError> {
        if self.proposal.is_some() {
            return Err(refused("managed_workflow_not_startable"));
        }
        // Not pushed in the slices step (a caller without that step): push now.
        if self.thread.is_none() {
            self.spawn().map_err(|e| refused(&e.0))?;
        }
        let Some(wall) = self.walls.values().copied().min() else {
            return Err(refused("member_wall_limit_invalid"));
        };
        // Counted from the publish step (demo_chain.py:896): no route outlives
        // the members' worker wall.
        let expires_at = (self.clocks.wall)() + wall as f64;
        self.proposal = Some(proposal.clone());
        self.shared.update(|f| {
            if f.state == PREPARED {
                f.state = PUBLISHING;
            }
            f.expires_at = Some(expires_at);
        });
        self.send(Command::Prepare { expires_at })
            .map_err(|e| refused(&e.0))?;
        Ok(json!({"registry_origin":null}))
    }
    fn workflow_state(&mut self) -> Result<Value, StepError> {
        Ok(self.state())
    }
}
impl ActivationPort for OwnerWork {
    /// `activate_serving(proposal)`: only the started proposal, only once the
    /// stages are prepared; schedules the route (issue -> serve -> epoch).
    fn activate(&mut self, proposal: &Value) -> Result<(), PortError> {
        if self.proposal.as_ref() != Some(proposal) || self.shared.lock().state != AWAITING {
            return Err(refused("activation_failed"));
        }
        self.send(Command::Open).map_err(|e| refused(&e.0))
    }
}
impl SlicesPort for OwnerWork {
    /// No slice plan is announced on the joined route: every member receives
    /// its stage shard pushed by the owner (U1). The push happens HERE, in the
    /// slices step, before publish starts the run's execution clock (F5; as
    /// v0.1.4 members download before publish, demo_chain.py:562-725 vs :896),
    /// bounded by the slices budget (`member_slice_timeout`, :888) and ended by
    /// [`OwnerWork::stopper`]. A refused push fails the step with its code.
    fn node_slice_plans(&mut self) -> Result<Option<BTreeMap<String, Value>>, StepError> {
        if self.thread.is_none() {
            self.spawn().map_err(|e| step_refusal(&e.0))?;
        }
        let budget = super::span(B::Slices).map_err(|e| step_refusal(e.code))?;
        self.wait_transferred(budget)?;
        Ok(None)
    }
    fn storage_policy(&mut self) -> Result<Option<Value>, StepError> {
        let policy = crate::preferences::resource_policy::read(&self.owner_root).map_err(|_| {
            StepError::Exception {
                class: "ResourcePolicyError".into(),
            }
        })?;
        Ok(policy.and_then(|p| p.get("storage_bytes").cloned()))
    }
}

/// `_stop_deadline` (demo_chain.py:1240-1253): how long the owner waits for
/// its members' cleanup, from the smallest worker wall and the member count.
fn stop_allowance(wall: u64, members: usize) -> Option<Duration> {
    let span = B::EffectiveStopAllowance
        .wall_span(
            Span::from_millis(wall.checked_mul(1000)?),
            Entries::new(u64::try_from(members).ok()?),
        )
        .ok()?;
    Some(Duration::from_millis(span.millis()))
}
/// A member link that counts operations begun and not yet answered, and
/// whether a `receive` was begun (from then on the member may hold the stage:
/// it takes custody before any carrier work).
struct Counted {
    inner: Box<dyn MemberLink + Send>,
    outstanding: usize,
    custody: bool,
}
impl MemberLink for Counted {
    fn begin(&mut self, command: Value) -> Result<(), Refusal> {
        let receive = command["command"] == "receive";
        self.inner.begin(command)?;
        self.outstanding += 1;
        self.custody |= receive;
        Ok(())
    }
    fn finish(&mut self, within: Duration) -> Result<Value, Refusal> {
        let reply = self.inner.finish(within);
        if !matches!(&reply, Err(Refusal(code)) if code == REPLY_TIMEOUT) {
            self.outstanding = self.outstanding.saturating_sub(1);
        }
        reply
    }
}
/// A participant inside the worker.
struct Member {
    node_id: String,
    link: Counted,
    registration: Value,
    cancel: Option<Cancel>,
}
impl From<WorkMember> for Member {
    fn from(m: WorkMember) -> Self {
        Self {
            node_id: m.node_id,
            link: Counted {
                inner: m.link,
                outstanding: 0,
                custody: false,
            },
            registration: m.registration,
            cancel: m.cancel,
        }
    }
}
impl From<Member> for WorkMember {
    fn from(m: Member) -> Self {
        Self {
            node_id: m.node_id,
            link: m.link.inner,
            registration: m.registration,
            cancel: m.cancel,
        }
    }
}

/// The owned worker of one started run.
struct Worker {
    shared: Arc<Shared>,
    coordinator: Arc<seed::Coordinator>,
    members: Vec<Member>,
    plan: Plan,
    network: Network,
    codec: Arc<dyn Codec>,
    admission: Box<dyn Admission>,
    stop: Arc<AtomicBool>,
    exiting: Arc<AtomicBool>,
    inbox: mpsc::Receiver<Command>,
    /// Member operation wait (transfer, prepare, control calls).
    within: Duration,
    /// The members' worker wall: how long a member may serve one route.
    serve_within: Duration,
    /// How long the exit waits for the members' stage release.
    stop_allowance: Duration,
}
impl Worker {
    fn run(mut self) -> Vec<WorkMember> {
        let mut open = None;
        let worked = self.work(&mut open);
        // A Stop that arrived while working ends the workflow `stopped`
        // (whatever the interrupted member operation answered).
        let stopping = self.stop.load(Ordering::SeqCst);
        self.exiting.store(true, Ordering::SeqCst);
        // F4: on the exit an open epoch's members answer within the stop allowance.
        let released = match open.take() {
            Some(epoch) => self.retire(epoch, self.stop_allowance),
            None => Ok(()),
        };
        // R1: every exit releases what the members hold.
        let discarded = self.release_members();
        // A teardown failure is kept on its own even when the work failed first.
        let cleanup = released.as_ref().err().map(closed).or(discarded);
        let failure = match stopping {
            true => None,
            false => worked.err().or(released.err()),
        };
        self.shared.update(|f| {
            f.route = None;
            f.gateway = None;
            f.cleanup_error = cleanup;
            match failure {
                Some(r) => {
                    f.state = FAILED;
                    f.error = Some(closed(&r));
                }
                None => f.state = STOPPED,
            }
        });
        self.members.into_iter().map(WorkMember::from).collect()
    }
    /// R1 (v0.1.4 teardown, demo_chain.py:1283-1320, 1380): on every exit,
    /// cancel what members still run (`cancel_operation`) and drain those
    /// replies, then ask every member that began a `receive` to `discard`
    /// (closes its native worker and discards the stage bytes; the member's
    /// own `discard`), all under ONE deadline: the run's stop allowance. A
    /// refusal, a missing or unconfirmed answer leaves the cleanup unproven
    /// (`owner_target_cleanup_unproven`), reported per member, never swallowed.
    fn release_members(&mut self) -> Option<String> {
        let end = Instant::now() + self.stop_allowance;
        let left = || end.saturating_duration_since(Instant::now());
        for m in &self.members {
            if let (true, Some(cancel)) = (m.link.outstanding > 0, &m.cancel) {
                cancel();
            }
        }
        let mut rows = Vec::new();
        let mut asked = Vec::new();
        for (i, m) in self.members.iter_mut().enumerate() {
            while m.link.outstanding > 0 {
                if let Err(Refusal(code)) = m.link.finish(left())
                    && code == REPLY_TIMEOUT
                {
                    break;
                }
            }
            if !m.link.custody {
                continue;
            }
            let begun = match m.link.outstanding {
                0 => m.link.begin(json!({"command":"discard"})),
                _ => Err(refusal(REPLY_TIMEOUT)),
            };
            match begun {
                Ok(()) => asked.push(i),
                Err(Refusal(code)) => {
                    rows.push(json!({"node_id":m.node_id,"discarded":false,"code":code}))
                }
            }
        }
        for i in asked {
            let m = &mut self.members[i];
            rows.push(match m.link.finish(left()) {
                Ok(r) if r["inert_bytes_discarded"] == true || r["nothing_owned"] == true => {
                    m.link.custody = false;
                    json!({"node_id":m.node_id,"discarded":true})
                }
                Ok(_) => {
                    json!({"node_id":m.node_id,"discarded":false,"code":"member_discard_unconfirmed"})
                }
                Err(Refusal(code)) => json!({"node_id":m.node_id,"discarded":false,"code":code}),
            });
        }
        let unproven = rows.iter().any(|r| r["discarded"] != true);
        self.shared.update(|f| f.member_cleanup = rows);
        unproven.then(|| CLEANUP_UNPROVEN.to_owned())
    }
    fn stopped(&self) -> Result<(), Refusal> {
        if self.stop.load(Ordering::SeqCst) {
            return Err(refusal("managed_workflow_stopped"));
        }
        Ok(())
    }
    fn work(&mut self, open: &mut Option<Epoch>) -> Result<(), Refusal> {
        let source = ShardSource::start(
            self.coordinator.clone(),
            self.plan.objects.clone(),
            self.plan.journal.clone(),
            self.network.clone(),
        )?;
        let offers = self.transfer_all(&source);
        let released = source.close();
        let offers = offers?;
        released?;
        self.shared.update(|f| f.transferred_all = true);
        // F5: the native prepare starts at publish (the run's clock).
        let expires_at = match self.inbox.recv() {
            Ok(Command::Prepare { expires_at }) => expires_at,
            Ok(Command::Open) => return Err(refusal("activation_failed")),
            Err(_) => return Ok(()),
        };
        self.stopped()?;
        self.shared.set(STAGING);
        self.prepare(&offers)?;
        let mut prepared = true;
        self.shared.set(AWAITING);
        let mut number = 0;
        while let Ok(command) = self.inbox.recv() {
            if !matches!(command, Command::Open) {
                return Err(refusal("managed_workflow_not_startable"));
            }
            self.stopped()?;
            if let Some(spent) = open.take() {
                self.retire(spent, self.serve_within)?;
            }
            if !prepared {
                self.shared.set(STAGING);
                self.prepare(&offers)?;
            }
            self.stopped()?;
            self.shared.set(STARTING);
            number += 1;
            let owner = iroh::SecretKey::generate();
            let envelope = self.admission.issue(&RouteOrder {
                protocol: route::ROUTE,
                model: &self.plan.model,
                deployment_id: &self.plan.deployment_id,
                epoch: number,
                owner_endpoint_id: &owner.public().to_string(),
                now: app_host::now(),
                expires_at,
            })?;
            // Single-use: the next epoch re-prepares (U5).
            prepared = false;
            let mut stages: Vec<EpochStage<'_>> = self
                .members
                .iter_mut()
                .zip(&offers)
                .map(|(m, offer)| EpochStage {
                    node_id: &m.node_id,
                    link: &mut m.link,
                    offer,
                })
                .collect();
            let epoch = deliver(
                &self.coordinator,
                envelope,
                owner,
                &mut stages,
                &self.network,
                self.codec.clone(),
                self.within,
            )?;
            let gateway = json!({"addr":epoch.addr(),"bearer":epoch.bearer(),
                "qualification":epoch.qualification().ok(),
                "route_expires_at":epoch.host().message()["expires_at"]});
            let handle = epoch.handle().clone();
            *open = Some(epoch);
            self.shared.update(|f| {
                f.route = Some(handle);
                f.gateway = Some(gateway);
                f.epoch = number;
                f.state = RUNNING;
            });
        }
        Ok(())
    }
    fn transfer_all(&mut self, source: &ShardSource) -> Result<Vec<Value>, Refusal> {
        let mut offers = Vec::new();
        for (i, m) in self.members.iter_mut().enumerate() {
            if self.stop.load(Ordering::SeqCst) {
                return Err(refusal("managed_workflow_stopped"));
            }
            let done = transfer_stage(
                source,
                &self.coordinator,
                &mut m.link,
                &m.registration,
                &m.node_id,
                &self.plan.document,
                &format!("{}-transfer-{i}", self.plan.run_id),
                self.within,
            )?;
            let row = json!({"node_id":m.node_id,"received_bytes":done.received["received_bytes"]});
            self.shared.update(|f| f.transferred.push(row));
            offers.push(done.offer);
        }
        Ok(offers)
    }
    /// One single-use challenge per stage; both stages load in parallel.
    fn prepare(&mut self, offers: &[Value]) -> Result<(), Refusal> {
        let mut stages = Vec::new();
        for (m, offer) in self.members.iter_mut().zip(offers) {
            stages.push(Stage {
                range: *self
                    .plan
                    .ranges
                    .get(&m.node_id)
                    .ok_or_else(|| refusal("joined_route_range_invalid"))?,
                member: &mut m.link,
                offer,
            });
        }
        self.admission.prepare(
            &mut stages,
            self.plan.tokens,
            &self.plan.forbidden_source,
            self.within,
        )
    }
    /// Retire an epoch: a settled request gets its members' signed serve
    /// reports and worker-gone proofs; an idle one is closed first, after
    /// which each member ends its serve (its compute released) and reports.
    fn retire(&mut self, epoch: Epoch, within: Duration) -> Result<(), Refusal> {
        let settled = epoch
            .handle()
            .state
            .lock()
            .map_err(|_| refusal("carrier_state_poisoned"))?
            .teardown_finished;
        let mut reports = Vec::new();
        if settled {
            let mut stages: Vec<SettleStage<'_>> = self
                .members
                .iter_mut()
                .map(|m| SettleStage {
                    node_id: &m.node_id,
                    link: &mut m.link,
                })
                .collect();
            reports = settle_members(&epoch, &mut stages, within)?.0;
        }
        let refusals = epoch.close();
        if !settled {
            for m in self.members.iter_mut() {
                let report = m.link.finish(within)?;
                reports.push(json!({"node_id":m.node_id,"teardown":report["teardown"],
                    "failure":report["failure"]}));
            }
        }
        self.shared.update(|f| {
            f.route = None;
            f.gateway = None;
            f.serve_reports = reports;
            f.serve_reports
                .extend(refusals.iter().map(|code| json!({"pump_refusal":code})));
        });
        Ok(())
    }
}

/// `_smallest_worker_wall(ctx)` (demo_chain.py:1125-1130): the smallest
/// positive integer `max_wall_seconds` of the run-wide and per-node work limits.
pub fn smallest_worker_wall(state: &RunState) -> Option<u64> {
    let nodes = state
        .work_nodes
        .as_ref()
        .and_then(Value::as_object)
        .into_iter()
        .flat_map(|n| n.values())
        .map(|r| &r["work_limits"]);
    state
        .work_limits
        .iter()
        .chain(nodes)
        .filter_map(|l| l["max_wall_seconds"].as_u64())
        .filter(|w| *w > 0)
        .min()
}
/// `rollover_due(ctx)` (demo_chain.py:1132-1141): due at the run's publish
/// clock + the smallest worker wall - min(max(ceiling lead, margin), wall/2).
/// A zero margin, no execution clock or no wall is never due.
pub fn rollover_due(state: &RunState, now: f64) -> Option<bool> {
    let margin = B::RolloverMargin.span(Entries::ZERO).ok()?;
    let (Some(started), Some(wall)) = (
        state.execution_clock_started_at,
        smallest_worker_wall(state),
    ) else {
        return Some(false);
    };
    if margin.millis() == 0 {
        return Some(false);
    }
    let lead = B::EffectiveRolloverLead
        .wall_span(Span::from_millis(wall.checked_mul(1000)?), Entries::ZERO)
        .ok()?;
    Some(now >= started + wall as f64 - lead.millis() as f64 / 1000.0)
}
/// `ceiling_reached(ctx)` (demo_chain.py:1143-1164): `execution_ceiling_reached`
/// once the run's clock reaches the smallest worker wall - the ceiling lead.
pub fn ceiling_reached(state: &RunState, now: f64) -> CeilingProbe {
    let (Some(started), Some(wall)) = (
        state.execution_clock_started_at,
        smallest_worker_wall(state),
    ) else {
        return CeilingProbe::Code(None);
    };
    let Ok(lead) = B::ExecutionCeilingLead.span(Entries::ZERO) else {
        return CeilingProbe::Raised {
            class: "BudgetUnavailable".into(),
        };
    };
    if now < started + wall as f64 - lead.millis() as f64 / 1000.0 {
        return CeilingProbe::Code(None);
    }
    CeilingProbe::Code(Some(EXECUTION_CEILING_CODE.into()))
}
