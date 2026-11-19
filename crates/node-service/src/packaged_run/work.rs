// SPDX-License-Identifier: AGPL-3.0-only
//! Run-bound glue only. Shared owner-run steps retain planning, share arithmetic and clocks.
use super::*;
mod objects;
#[cfg(test)]
mod tests;
use api::{
    app_host,
    joined_owner::{
        remote::RemoteMember,
        work::{Clocks, OwnerWork, Plan, WorkMember},
    },
    m16_route::Codec,
    member_control::joined,
    run_exchange::{
        Announce,
        owner_pin::{OwnerControl, VerifiedAnnouncement},
    },
    run_exchange_client::{ExchangeClient, Membership},
    seed::SeedClient,
};
use owner_run::{
    chain::StepEnv,
    machine::{CeilingProbe, MonitorProbe},
    runner::Chain,
    state::RunState,
    steps::{
        activate::ActivationPort,
        exchange::{Announcement, ClockSkew, MemberRow, ReportsPort},
        publish::{Fence, ServingIdentity, WorkPort, WorkPorts},
        slices::{SlicesPort, SlicesPorts},
    },
};
use std::{
    collections::BTreeMap,
    sync::atomic::{AtomicBool, Ordering},
};
fn refused(code: &str) -> StepError {
    owner_run::OwnerRunError::new(code).into()
}

/// The document `prepare` hands the workflow. A stage-sharded compile retains its shard document;
/// a whole-snapshot run has none, and `_step_slices` (`demo_chain.py:838-839`) passes exactly then
/// ("whole snapshots: no slice to fetch") because every member binds the copy it already holds.
fn work_document(sharded: bool, shards: Option<&Value>) -> Result<Value, StepError> {
    match (sharded, shards) {
        (true, Some(document)) => Ok(document.clone()),
        (true, None) => Err(refused("compiler_outputs_changed")),
        (false, _) => Ok(serde_json::json!({
            "protocol": "mycelium.source_whole_snapshot.v1",
            "node_files": {},
            "node_slice_plans": {},
        })),
    }
}

/// Tokens one native stage prepares for: the oracle's prompt plus the challenge's own output.
///
/// The packed oracle keeps every recorded value under `recorded` (`native_compiler.rs:493` reads
/// `reference["recorded"]` for exactly this reason), so a read at the document's top level finds
/// nothing and answers `native_reference_invalid` even though the constant is valid JSON.
fn challenge_tokens(reference: &Value) -> Result<usize, StepError> {
    let prompt = reference["recorded"]["workload"]["prompt_ids"]
        .as_array()
        .ok_or_else(|| refused("native_reference_invalid"))?;
    Ok(prompt.len() + stage_contract::native_proof::CHALLENGE_OUTPUT_TOKENS)
}
/// Tokens every stage is prepared for (lane CAP, owner ruling 15 Nov): the serving context the
/// compile planned each stage's memory for, carried by its builder request; never the startup
/// check's own length, which left no room for any product chat. The startup check stays one
/// request inside that capacity: a context that cannot hold it is refused here, by name, before
/// any stage is prepared.
fn stage_tokens(
    request: &planner::chain_compile::BuildRequest,
    reference: &Value,
) -> Result<usize, StepError> {
    let capacity = usize::try_from(request.serving_context_tokens)
        .map_err(|_| refused("model_serving_context_invalid"))?;
    if challenge_tokens(reference)? > capacity {
        return Err(refused("startup_challenge_exceeds_context"));
    }
    Ok(capacity)
}
/// Lane CAP: every stage's memory consent must cover the worker's own reservation for the serving
/// context (`native_selection::stage_reservation`, the figure the stage's prepare holds against
/// `max_memory_bytes`). Known as soon as the compile assigned the ranges, so a consent that cannot
/// cover it is refused here, at the slices step, by name and before any shard is pushed, never at
/// the stage's prepare or the first chat. `consent(node)` is the memory the stage's own prepare
/// will hold the worker to; a node without one is left to that prepare's check.
fn check_stage_memory(
    request: &planner::chain_compile::BuildRequest,
    consent: &dyn Fn(&str) -> Option<u64>,
    tokens: usize,
) -> Result<(), StepError> {
    use crate::native_selection::{Profile, stage_reservation};
    let total = request
        .managed_layer_ranges
        .iter()
        .map(|r| r[1])
        .max()
        .and_then(|end| u32::try_from(end).ok())
        .ok_or_else(|| refused("compiler_run_changed"))?;
    let context = u32::try_from(tokens).map_err(|_| refused("model_serving_context_invalid"))?;
    for (node, range) in request.node_ids.iter().zip(&request.managed_layer_ranges) {
        let Some(consent) = consent(node) else {
            continue;
        };
        let (start, end) = (
            u32::try_from(range[0]).map_err(|_| refused("compiler_run_changed"))?,
            u32::try_from(range[1]).map_err(|_| refused("compiler_run_changed"))?,
        );
        let needed = stage_reservation(
            &request.model_id,
            Profile::PackedInt8,
            start..end,
            total,
            context,
        )
        .map_err(|e| {
            eprintln!("stage_reservation_refused:{e}");
            refused("native_stage_unsupported")
        })?;
        if needed > consent {
            let message = format!(
                "Mac {node} holds its stage worker to {consent} bytes of memory (its consent and the \
                 share the compile announced), but its stage (layers {start}-{}) needs {needed} \
                 bytes for the model's {context}-token serving context",
                end - 1
            );
            return Err(owner_run::OwnerRunError::with(
                "stage_memory_consent_below_context",
                Some(&message),
                None,
            )
            .into());
        }
    }
    Ok(())
}
/// The memory a stage's prepare will hold its worker to (`NativeShard::memory_limit`), as the
/// slices step can know it: the owner's own stage, its contributor's standing grant (the
/// compile's records carry none for the owner: v0.1.4's owner holds no grant); a joined Mac's,
/// its signed report's caps (`joined_member::planning_caps` = its standing consent).
///
/// Lane A, B7 / CAP-1: the member then holds its worker to the SHARE it admitted (its consent
/// tightened by the compile's announced `max_rss_bytes` for that node), so the check reads the
/// same effective limit: the smaller of the consent and the share, whichever is known.
fn stage_memory_limit(state: &RunState, contributor: &Path, node: &str) -> Option<u64> {
    let consent = if state.local_node.as_deref() == Some(node) {
        api::preferences::standing_consent::load(contributor)
            .ok()
            .flatten()
            .and_then(|g| g["caps"]["max_memory_bytes"].as_u64())
    } else {
        state
            .member_records
            .as_ref()
            .and_then(|r| r[node]["caps"]["max_memory_bytes"].as_u64())
    };
    let share = state
        .compile_state
        .as_ref()
        .and_then(|c| c["resource_plan"]["members"].as_array())
        .and_then(|rows| rows.iter().find(|r| r["node_id"].as_str() == Some(node)))
        .and_then(|r| r["max_rss_bytes"].as_u64());
    match (consent, share) {
        (Some(consent), Some(share)) => Some(consent.min(share)),
        (consent, share) => consent.or(share),
    }
}
/// The compile's completed run as the slices step's preparation reads it: in a product build
/// always the compile's own completed input.
enum Completed<'a> {
    Artifact(&'a api::compiler_inputs::InputArtifact),
    /// TEST ONLY: [`COMPLETED_REQUEST`].
    #[cfg(test)]
    Request(planner::chain_compile::BuildRequest),
}
impl Completed<'_> {
    fn request(&self) -> &planner::chain_compile::BuildRequest {
        match self {
            Self::Artifact(artifact) => artifact.request(),
            #[cfg(test)]
            Self::Request(request) => request,
        }
    }
    fn artifact(&self) -> Result<&api::compiler_inputs::InputArtifact, StepError> {
        match self {
            Self::Artifact(artifact) => Ok(artifact),
            #[cfg(test)]
            Self::Request(_) => Err(refused("test_completed_request_only")),
        }
    }
}
#[cfg(test)]
thread_local! {
    /// TEST ONLY (lane A patch 2, A-04): the completed compile's request for this thread's next
    /// [`Work::prepare`], in place of a compile only the real model can complete (its native
    /// challenge). It replaces the compile's completion and nothing else: every check `prepare`
    /// makes on the request (run identity, the serving context, the stage memory) is the product's,
    /// and a request that passes them is refused `test_completed_request_only` right after the
    /// memory check, before any shard document, push or prepare.
    pub(super) static COMPLETED_REQUEST: std::cell::RefCell<Option<planner::chain_compile::BuildRequest>> =
        const { std::cell::RefCell::new(None) };
}
/// The slices step's memory check over the run's own consent sources (lane A, CAP-4: the
/// production wiring a test can reach, not a closure each test writes for itself).
fn check_run_stage_memory(
    request: &planner::chain_compile::BuildRequest,
    state: &RunState,
    contributor: &Path,
    tokens: usize,
) -> Result<(), StepError> {
    check_stage_memory(
        request,
        &|node| stage_memory_limit(state, contributor, node),
        tokens,
    )
}
/// Retry the owners a failed close retained: first the member Work (its repeated teardown
/// retries the exact unproven member links), and only once every member stage is proven,
/// the local Service whose control loop those links reach. Unresolved owners stay retained.
pub(super) fn settle_debt(host: &PackagedRun) -> Result<(), String> {
    let debt = lock(&host.custody).work_debt.take();
    let Some(mut debt) = debt else {
        return Ok(());
    };
    let mut failure = None;
    let unprovable = debt.unprovable.clone();
    if let Some(mut work) = debt.work.take() {
        if let Err(code) = work.teardown() {
            failure = Some(code);
        } else if let Some(code) = work.state()["cleanup_error"].as_str() {
            eprintln!(
                "owner_work_cleanup_failed:{code}:{}",
                work.state()["member_cleanup"]
            );
            failure = Some(code.to_owned());
        } else if let Some(code) = unprovable {
            // The worker died without reporting per-member cleanup: the retained links are
            // kept, but no later close turns that absence of evidence into success.
            eprintln!("owner_work_cleanup_failed:{code}:worker_unproven");
            failure = Some(code);
        } else {
            drop(work.take_members());
        }
        if failure.is_some() {
            debt.work = Some(work);
        }
    }
    if failure.is_none()
        && let Some(mut service) = debt.local.take()
        && let Err(code) = service.close()
    {
        eprintln!("member_work_custody_cleanup_failed:{code}");
        debt.local = Some(service);
        failure = Some(code);
    }
    if let Some(code) = failure {
        lock(&host.custody).work_debt = Some(debt);
        return Err(code);
    }
    Ok(())
}
/// Keep the exact unresolved owners for the next Stop/Quit/close; never replace an owner that
/// still owes cleanup (a second failure with debt present is a bug and is logged, not dropped).
fn retain_debt(
    host: &PackagedRun,
    work: Option<api::joined_owner::work::OwnerWork>,
    local: Option<crate::packaged_work::Service>,
) {
    let mut custody = lock(&host.custody);
    let debt = custody.work_debt.get_or_insert_with(WorkDebt::default);
    if debt.work.is_none() {
        debt.work = work;
    } else if work.is_some() {
        eprintln!("work_custody_debt_not_replaced");
    }
    if debt.local.is_none() {
        debt.local = local;
    } else if local.is_some() {
        eprintln!("work_custody_debt_not_replaced");
    }
}
fn span(id: BudgetId) -> Result<Duration, StepError> {
    id.span(Entries::ZERO)
        .map(|s| Duration::from_millis(s.millis()))
        .map_err(|_| refused("budget_unavailable"))
}
/// A lazy tokenizer: production never substitutes made-up tokens. It rechecks the captured
/// installed identity before starting the existing helper, and that helper owns its child.
struct PinnedCodec {
    program: api::native_compiler::NativeProgram,
    tokenizer: PathBuf,
    helper: Mutex<Option<api::joined_owner::codec::NativeCodec>>,
}
impl PinnedCodec {
    fn with<T>(
        &self,
        f: impl FnOnce(&api::joined_owner::codec::NativeCodec) -> Result<T, PublicError>,
    ) -> Result<T, PublicError> {
        let mut helper = lock(&self.helper);
        if helper.is_none() {
            let program = self
                .program
                .clone()
                .admit(&|| false)
                .map_err(|e| PublicError::new(409, e.0))?;
            *helper = Some(api::joined_owner::codec::NativeCodec::start(
                &program.binary,
                &self.tokenizer,
            )?);
        }
        f(helper
            .as_ref()
            .ok_or_else(|| PublicError::new(503, "tokenizer_unavailable"))?)
    }
}
impl Codec for PinnedCodec {
    fn encode(&self, text: &str) -> Result<Vec<u32>, PublicError> {
        self.with(|h| h.encode(text))
    }
    fn decode(&self, token: u32) -> Result<String, PublicError> {
        self.with(|h| h.decode(token))
    }
}
/// A single owner of all links, including its enrolled local contributor. Teardown always
/// takes returned members, even on cleanup failure; links then close locally, membership stays.
struct Resident {
    work: Option<OwnerWork>,
    local: Option<crate::packaged_work::Service>,
    local_session: crate::packaged_work::SessionCell,
    bindings: Vec<joined::Verified>,
    run: Option<(String, String)>,
    /// The work transport this run resolved when it started (`compose`): its local worker, its
    /// member dials, its shard source and route all use this one choice for the whole run.
    network: Option<crate::packaged_work::NetworkChoice>,
}
/// The Runner's monotonic clock as the run's work sees it (aligned when the work is composed).
type RunClock = Arc<dyn Fn() -> f64 + Send + Sync>;
#[derive(Clone)]
pub(super) struct Work {
    host: PackagedRun,
    owner: iroh::SecretKey,
    resident: Arc<Mutex<Resident>>,
    stopped: Arc<AtomicBool>,
    /// The run's clock, kept after the workflow is gone: `rollover_due` and `ceiling_reached`
    /// read `ctx.state` and the clock alone (demo_chain.py:1132-1164).
    clock: Arc<Mutex<Option<RunClock>>>,
    /// Sends the one datagram that asks macOS's Local Network permission (lane PHY item 2).
    probe: Arc<dyn api::local_network::Probe>,
    /// What the probe reached in the current run (never a target list: see [`LocalNetwork`]).
    local_network: Arc<Mutex<LocalNetwork>>,
}
/// Lane XB patch 2 (D3): the named refusal of a run whose joined Mac reported (state `error`,
/// for THIS run) that it cannot bind the run's signed network reach.
fn member_reach_refusal(reports: &Value, state: &RunState) -> Option<StepError> {
    member_reach_refusal_in(
        reports,
        &state.run_id,
        &state.deployment_id,
        &state.participants,
    )
}
/// Lane XB patch 3 (REVIEW XB2-03), scoped in patch 4 (REVIEW XB3-02): [`member_reach_refusal`]
/// over the run's authenticated reports (the exchange keeps only verified ones). Only the reports
/// of `participants` (the run's CURRENT member set: the collect step's candidates, then the Macs it
/// selected) count, and only for exactly this `run_id` AND `deployment` (a report without a
/// deployment, or of another run or deployment, never counts), as the shared owner steps read
/// them (`run_reports`, `raise_member_refusal`). Any other Mac that reports into the exchange (a
/// Mac that came online after collect began, one the collect did not select) has no say over
/// this run.
fn member_reach_refusal_in(
    reports: &Value,
    run_id: &str,
    deployment: &str,
    participants: &[String],
) -> Option<StepError> {
    use crate::packaged_work::reach::MEMBER_REFUSALS;
    let (node, code) = participants.iter().find_map(|node| {
        let report = &reports[node.as_str()];
        let code = report["detail"].as_str()?;
        (report["state"] == "error"
            && report["run_id"].as_str() == Some(run_id)
            && report["deployment_id"].as_str() == Some(deployment)
            && MEMBER_REFUSALS.contains(&code))
        .then(|| (node.clone(), code.to_owned()))
    })?;
    let (owner_code, message) = crate::packaged_work::reach::member_refusal(&node, &code);
    Some(owner_run::OwnerRunError::with(owner_code, Some(&message), None).into())
}
/// Lane XB patch 3 (REVIEW XB2-03): the run steps before compose (collect, observe, compile)
/// wait on the joined Macs' reports for minutes (`Descriptors`). A joined Mac that refused the
/// run's signed reach by name says so in its report at once (`descriptor: null`), so these steps
/// run under this watch: every wait checks the run's reports for such a refusal and ends the
/// step (as a Stop would, at its next check); the step then fails by the joined Mac's name
/// instead of waiting out its budget. Anything else passes to the run's own environment.
/// Patch 4 (XB3-02): the watch reads only the run's current participants: the run state's list
/// when the step starts, then every list the step itself publishes (`participants`: the collect's
/// candidates on its first pass, the selected Macs when it ends; observe's facts nodes).
/// Patch 4 (REVIEW XB4-01): during collect the participants are only CANDIDATES. As the shared
/// collect does for any candidate without a usable card (`demo_chain.py:638-643,688-713`), a
/// candidate's reach refusal is a reason that drops that candidate; the run goes on with the
/// Macs that reported and fails (`two_macs_required`, or the candidate's own code) only below two.
/// The watch therefore ends collect early only when the refusals leave fewer than this many
/// candidates; after collect (observe, compile) every participant is selected and any refusal
/// ends the step.
const COLLECT_REQUIRED_MACS: usize = 2;
struct ReachWatch<'e> {
    env: &'e mut dyn StepEnv,
    reports: &'e dyn Fn() -> Value,
    run: (String, String),
    participants: Vec<String>,
    /// The step is collect (`descriptors`): `participants` are candidates, not a selection.
    collect: bool,
    refused: std::cell::RefCell<Option<StepError>>,
}
impl ReachWatch<'_> {
    /// A participant's named refusal of this run, scoped as [`member_reach_refusal_in`] reads.
    fn named(&self) -> Option<StepError> {
        member_reach_refusal_in(
            &(self.reports)(),
            &self.run.0,
            &self.run.1,
            &self.participants,
        )
    }
    fn refused(&self) -> bool {
        if self.refused.borrow().is_some() {
            return true;
        }
        let found = if self.collect {
            let reports = (self.reports)();
            let refusing = self
                .participants
                .iter()
                .filter(|node| {
                    member_reach_refusal_in(
                        &reports,
                        &self.run.0,
                        &self.run.1,
                        std::slice::from_ref(*node),
                    )
                    .is_some()
                })
                .count();
            (self.participants.len() - refusing < COLLECT_REQUIRED_MACS)
                .then(|| self.named())
                .flatten()
        } else {
            self.named()
        };
        let refused = found.is_some();
        *self.refused.borrow_mut() = found;
        refused
    }
}
/// Runs one step before compose under a [`ReachWatch`] for the run `run` whose participants are
/// `participants` when the step starts; a refusal the watch saw replaces the step's outcome.
fn watched_step(
    env: &mut dyn StepEnv,
    reports: &dyn Fn() -> Value,
    run: (String, String),
    participants: Vec<String>,
    collect: bool,
    step: impl FnOnce(&mut dyn StepEnv) -> Result<(), StepError>,
) -> Result<(), StepError> {
    let mut watch = ReachWatch {
        env,
        reports,
        run,
        participants,
        collect,
        refused: std::cell::RefCell::new(None),
    };
    let outcome = step(&mut watch);
    // Patch 4 (REVIEW XB3-04): a shared step can return a participant's reach refusal itself as
    // its raw code (observe's `raise_member_refusal` reads the reports after the watch's last
    // check). That code is reconciled with the same scoped report before it is returned, so the
    // owner still sees the joined Mac's named refusal. Stop and every other outcome pass as
    // they are.
    let raw_reach_refusal = matches!(&outcome, Err(StepError::Refused(raw))
        if crate::packaged_work::reach::MEMBER_REFUSALS.contains(&raw.code.as_str()));
    if raw_reach_refusal && watch.refused.borrow().is_none() {
        // The step already failed on that code: the plain scoped rule names it (in collect too).
        let named = watch.named();
        *watch.refused.borrow_mut() = named;
    }
    match watch.refused.into_inner() {
        Some(refused) => Err(refused),
        None => outcome,
    }
}
impl StepEnv for ReachWatch<'_> {
    fn progress(&mut self, detail: &str) {
        self.env.progress(detail);
    }
    fn stopped(&self) -> bool {
        self.env.stopped() || self.refused()
    }
    fn now(&mut self) -> f64 {
        self.env.now()
    }
    fn wall(&mut self) -> f64 {
        self.env.wall()
    }
    fn set_result(&mut self, key: &str, value: Value) {
        // The step's own participant list (`publish_participants`: one row per node) is the
        // run's current member set from now on.
        if key == "participants"
            && let Some(rows) = value.as_array()
        {
            self.participants = rows
                .iter()
                .filter_map(|row| row["node_id"].as_str().map(str::to_owned))
                .collect();
        }
        self.env.set_result(key, value);
    }
    fn record_exception(&mut self, code: &str, class: &str) {
        self.env.record_exception(code, class);
    }
    fn pause(&mut self, seconds: f64) -> bool {
        self.refused() || self.env.pause(seconds) || self.refused()
    }
    fn fenced(
        &mut self,
        commit: &mut dyn FnMut() -> Result<(), StepError>,
        key: &str,
        value: Value,
    ) -> Result<(), StepError> {
        if self.refused() {
            return Err(StepError::Stopped);
        }
        self.env.fenced(commit, key, value)
    }
}
/// Lane PHY item 2, patch 2 (PHY-02): what one run's probes reached. Targets are never cached:
/// every pass reads them afresh from this run's current, verified member records
/// (`Inbox::get` -> `joined::verify`: current enrollment, lease, generation, run and share
/// binding). This only spares a second datagram to an address already reached for the same member
/// generation in the same run, and it is dropped when the run changes.
#[derive(Default)]
struct LocalNetwork {
    /// The run the `reached` set and the `network` belong to.
    run: String,
    /// (node, generation, address) a datagram reached in this run.
    reached: std::collections::BTreeSet<(String, String, std::net::SocketAddr)>,
    /// PHY-03: this run's work transport as its first probe pass with a joined Mac resolved it
    /// (`Network::resolve_run_network`, the rule step 6 uses), shared by its later probe passes.
    /// Patch 3 (PHY2-01): compose never binds this; it reads this Mac's interfaces itself and only
    /// compares its own reading with this one (a change drops `reached` and asks again).
    network: Option<crate::packaged_work::Resolved>,
}
impl LocalNetwork {
    /// Bind this record to `run`, forgetting another run's reached addresses and network.
    fn bind(&mut self, run: &str) {
        if self.run != run {
            self.run = run.to_owned();
            self.reached.clear();
            self.network = None;
        }
    }
    /// Forget the run's preflight record altogether (a new run, Stop and teardown).
    fn reset(&mut self) {
        *self = Self::default();
    }
}
impl Work {
    pub(super) fn new(host: PackagedRun) -> Self {
        let local_session = Arc::new(Mutex::new(None));
        lock(&host.custody).work_session = Some(local_session.clone());
        Self {
            host,
            owner: iroh::SecretKey::generate(),
            resident: Arc::new(Mutex::new(Resident {
                work: None,
                local: None,
                local_session,
                bindings: Vec::new(),
                run: None,
                network: None,
            })),
            stopped: Arc::new(AtomicBool::new(false)),
            clock: Arc::default(),
            probe: Arc::new(api::local_network::Udp),
            local_network: Arc::default(),
        }
    }
    /// Lane PHY item 2 (rows 79, 80): one datagram to every joined Mac's verified work address
    /// in THIS run, so macOS raises its Local Network alert before the compile is spent, and a
    /// pending or denied permission is refused by name when `refuse` (Start's first step and the
    /// compile's start). Patch 2 (PHY-02): the targets are this run's current verified member
    /// records only, read afresh on every pass; a run with no verified joined member sends nothing
    /// and is never refused here.
    fn local_network(&self, state: &RunState, refuse: bool) -> Result<(), StepError> {
        self.probe_pass(state, refuse, || self.run_resolution(&state.run_id))
    }
    /// One probe pass of this run over the network `resolve` answers (asked only once the run has
    /// a joined Mac and a seed): steps 1-5 pass the run's preflight resolution, compose its own
    /// fresh one when that differs (PHY2-01).
    fn probe_pass(
        &self,
        state: &RunState,
        refuse: bool,
        resolve: impl FnOnce() -> crate::packaged_work::Resolved,
    ) -> Result<(), StepError> {
        use api::local_network::Answer;
        let joined: Vec<&String> = state
            .participants
            .iter()
            .filter(|node| Some(*node) != state.local_node.as_ref())
            .collect();
        if joined.is_empty() {
            return Ok(());
        }
        let Some(seed) = self.host.with(|c| c.authority.seed.clone()) else {
            return Ok(());
        };
        // PHY-03: the run's network first; with no admitted LAN path (`local_only:*`, or a
        // loopback-only run) nothing is sent: compose, reading the interfaces itself, refuses a
        // run with a joined Mac on it by name, as at the base.
        let resolved = resolve();
        // Lane XB2: across networks the joined Mac is not on this LAN; the relay is the path.
        let Some((from, _)) = resolved
            .choice
            .0
            .clone()
            .filter(|_| resolved.reach != "relay")
        else {
            eprintln!("local_network_probe_skipped:{}", resolved.transport);
            return Ok(());
        };
        // (node, generation, address) of every current verified joined member's work address
        // that this run's transport admits (family, loopback rule, exact hosts).
        let mut targets = Vec::new();
        for node in joined {
            // The address step 6 will dial, verified as it will be (signed by the joined Mac's
            // currently enrolled key, for this run, admitted by the run's transport); not
            // published yet or not admitted: nothing to ask with.
            let Ok(verified) = seed.work_members.get(
                &seed.coordinator,
                node,
                self.owner.public(),
                &state.run_id,
                &state.deployment_id,
                &|address| resolved.choice.allows(address),
            ) else {
                continue;
            };
            let generation = verified.registration()["message"]["member"]["generation"].to_string();
            for to in verified.address().ip_addrs() {
                targets.push((node.clone(), generation.clone(), *to));
            }
        }
        let mut seen = lock(&self.local_network);
        seen.bind(&state.run_id);
        let mut blocked = None;
        for target in targets {
            if seen.reached.contains(&target) {
                continue;
            }
            let to = target.2;
            let answer = self.probe.send(from, to);
            eprintln!("local_network_probe:{to}:{answer:?}");
            match answer {
                Answer::Sent => {
                    seen.reached.insert(target);
                }
                Answer::Blocked => {
                    blocked.get_or_insert(to);
                }
                Answer::NotLocal | Answer::Failed(_) => {}
            }
        }
        match blocked {
            Some(to) if refuse => Err(owner_run::OwnerRunError::with(
                api::local_network::PERMISSION_REQUIRED,
                Some(&api::local_network::owner_message(to.ip())),
                None,
            )
            .into()),
            _ => Ok(()),
        }
    }
    /// Lane PHY item 2 (c): a step-6 dial that got no answer, named by one datagram to each joined
    /// Mac it dialed: this Mac's permission ([`api::local_network::PERMISSION_REQUIRED`]) or the
    /// joined Mac's silence ([`api::local_network::JOINED_MAC_SILENT`]); `None` for any other cause.
    fn named_dial_failure(&self, cause: &str) -> Option<StepError> {
        use api::local_network::{Answer, dial_unanswered};
        if !dial_unanswered(cause) {
            return None;
        }
        // The joined Macs it dialed (this run's verified bindings, admitted by its transport at
        // compose); the owner's own contributor is this Mac. Sent from the run's bound address;
        // a run with no LAN address sends nothing and keeps its own failure.
        let local = self
            .host
            .with(|c| c.run.contributor.as_ref().map(|n| n.node.clone()));
        let resident = lock(&self.resident);
        let network = resident.network.clone()?;
        let (from, policy) = network.0.clone()?;
        // Lane XB patch 2 (XB-12): across networks the joined Mac's advertised private address
        // names nothing on this network: no permission probe, no same-network advice.
        if policy.relay().is_some() {
            return Some(
                owner_run::OwnerRunError::with(
                    crate::packaged_work::reach::SILENT_VIA_RELAY,
                    Some(&crate::packaged_work::reach::silent_via_relay_message(
                        cause,
                    )),
                    None,
                )
                .into(),
            );
        }
        let (Some((run, deployment)), bindings) = (resident.run.clone(), resident.bindings.clone())
        else {
            return None;
        };
        drop(resident);
        let seed = self.host.with(|c| c.authority.seed.clone())?;
        // PHY-02 residual (lane PHY patch 3): only a binding that is STILL this run's current
        // verified member is probed: a fresh `Inbox::get` (current enrollment, lease, generation,
        // run, deployment, owner and share binding, expiry) under the run's resident network,
        // answering the same registration and address compose bound. A member revoked, expired
        // or re-addressed since compose gets no datagram and cannot decide the returned error.
        let dialed: Vec<std::net::SocketAddr> = bindings
            .iter()
            .filter(|v| Some(v.node_id()) != local.as_deref())
            .filter(|v| {
                seed.work_members
                    .get(
                        &seed.coordinator,
                        v.node_id(),
                        self.owner.public(),
                        &run,
                        &deployment,
                        &|a| network.allows(a),
                    )
                    .is_ok_and(|current| {
                        current.registration() == v.registration()
                            && current.address() == v.address()
                    })
            })
            .flat_map(|v| v.address().ip_addrs().copied().collect::<Vec<_>>())
            .filter(|address| network.allows(*address))
            .collect();
        let mut silent = None;
        for to in dialed {
            let answer = self.probe.send(from, to);
            eprintln!("local_network_probe:{to}:{answer:?} after {cause}");
            match answer {
                Answer::Blocked => {
                    return Some(
                        owner_run::OwnerRunError::with(
                            api::local_network::PERMISSION_REQUIRED,
                            Some(&api::local_network::owner_message(to.ip())),
                            None,
                        )
                        .into(),
                    );
                }
                Answer::Sent => {
                    silent.get_or_insert(to);
                }
                Answer::NotLocal | Answer::Failed(_) => {}
            }
        }
        silent.map(|to| {
            owner_run::OwnerRunError::with(
                api::local_network::JOINED_MAC_SILENT,
                Some(&api::local_network::joined_mac_message(to.ip(), cause)),
                None,
            )
            .into()
        })
    }
    /// The run's failed workflow's open cause, named when it was a dial without an answer.
    fn named_step_failure(&self) -> Option<StepError> {
        let cause = lock(&self.resident).work.as_ref()?.failure_cause()?;
        self.named_dial_failure(&cause)
    }
    /// PHY-03: the run's work transport as its first probe pass resolved it (held for that run's
    /// later probe passes), resolving it now if no pass has. Compose never binds it (PHY2-01).
    fn run_resolution(&self, run: &str) -> crate::packaged_work::Resolved {
        let mut seen = lock(&self.local_network);
        seen.bind(run);
        match &seen.network {
            Some(resolved) => resolved.clone(),
            None => {
                let resolved = self.host.network.resolve_run_network("owner_run");
                seen.network = Some(resolved.clone());
                resolved
            }
        }
    }
    /// PHY2-01 (lane PHY patch 3): the resolution this run's probe passes used, taken by compose
    /// to compare with its own fresh reading; `None` when no pass of this run resolved one.
    fn take_preflight(&self, run: &str) -> Option<crate::packaged_work::Resolved> {
        let mut seen = lock(&self.local_network);
        if seen.run != run {
            return None;
        }
        seen.network.take()
    }
    /// Forget the run's preflight record (`Composed::new_run`, teardown).
    fn reset_preflight(&self) {
        lock(&self.local_network).reset();
    }
    /// The run's transport, resolved at `compose`; a work announcement before any compose (none
    /// in the chain's order) resolves its own.
    fn run_network(&self) -> crate::packaged_work::NetworkChoice {
        let held = lock(&self.resident).network.clone();
        held.unwrap_or_else(|| self.host.network.resolve_work_network("owner_local_work"))
    }
    /// The packaged host this run's work belongs to (the run ports' chat hooks read its custody).
    pub(super) fn host(&self) -> &PackagedRun {
        &self.host
    }
    fn local(&self, network: &crate::packaged_work::NetworkChoice) -> Result<(), StepError> {
        // The renewal loop holds the contributor's Session for one seed heartbeat
        // (`owner_ports::renew_contributor`). For exactly that flight `PackagedRun::with` keeps
        // the preceding lease-bounded snapshot in the run's work-session cell, which the local
        // worker's own admission already reads; a registration reads it too, so a renewal in
        // flight never fails the step that announces work (lane FLK, row 6). The Session, its
        // flight and the snapshot are read in ONE acquisition (lane FLK patch 2): a flight that
        // no longer stands in (unwound, past its deadline) answers `member_work_renewing` as a
        // missing Session did before the fallback; a renewal the seed refused answers its code.
        let cell = lock(&self.resident).local_session.clone();
        let (root, seed, session, signer, node) = self.host.with(|c| {
            let member = c
                .run
                .contributor
                .as_ref()
                .ok_or_else(|| refused("member_work_unavailable"))?;
            let session = match &member.session {
                Some(session) => session.clone(),
                None if member
                    .flight
                    .as_ref()
                    .is_some_and(api::renewal::Flight::stands_in) =>
                {
                    lock(&cell)
                        .clone()
                        .ok_or_else(|| refused("member_work_renewing"))?
                }
                None => {
                    return Err(refused(member.refused.unwrap_or("member_work_renewing")));
                }
            };
            Ok::<_, StepError>((
                owner_ports::contributor_dir(&c.authority.root),
                c.authority
                    .seed
                    .clone()
                    .ok_or_else(|| refused("owner_exchange_unavailable"))?,
                session,
                member.signer.clone(),
                member.node.clone(),
            ))
        })?;
        let row = session
            .shard_identity(&signer, app_host::now())
            .map_err(|_| refused("joined_shard_member_changed"))?;
        let key = seed.coordinator.signer().public_key_record();
        let client = SeedClient::new(
            &seed.seed_url,
            seed.coordinator.swarm_id(),
            key["verification_key_digest"]
                .as_str()
                .ok_or_else(|| refused("member_registration_key_missing"))?,
            std::slice::from_ref(&key),
            &seed.options(),
        )
        .map_err(|_| refused("member_seed_unavailable"))?;
        let mut client = ExchangeClient::new(
            client,
            signer.clone(),
            Membership {
                swarm_id: seed.coordinator.swarm_id().into(),
                owner_node_id: seed.coordinator.seed_node_id().into(),
                owner_key: key,
                node_id: node,
                generation: session.generation().ok_or_else(|| refused("not_joined"))?,
                incarnation: row["incarnation"]
                    .as_str()
                    .ok_or_else(|| refused("not_joined"))?
                    .into(),
            },
            Box::new(app_host::now),
            Box::new(|| match app_host::random(16) {
                Ok(bytes) => Some(app_host::hex(&bytes)),
                Err(e) => {
                    eprintln!("member_work_nonce_failed:{e}");
                    None
                }
            }),
        );
        let announcement = VerifiedAnnouncement::read(&mut client)
            .map_err(|_| refused("owner_pin_announcement_unverified"))?;
        let mut resident = lock(&self.resident);
        if let Some(service) = &mut resident.local {
            service.update(announcement).map_err(|e| refused(&e))?;
        } else {
            resident.local = Some(
                crate::packaged_work::Service::on_network(
                    root,
                    resident.local_session.clone(),
                    signer,
                    announcement,
                    network.clone(),
                    self.host.network.native_program(),
                )
                .map_err(|e| refused(&e))?,
            );
        }
        let document = resident
            .local
            .as_ref()
            .ok_or_else(|| refused("member_work_unavailable"))?
            .registration()
            .map_err(|e| refused(&e))?;
        drop(resident);
        joined::inbox::publish(&client, &document).map_err(|e| refused(e.code))
    }
    fn prepare(
        &self,
        state: &RunState,
        env: &mut dyn StepEnv,
        compiler: &SharedCompiler<PackagedRun>,
        program: Option<&api::native_compiler::NativeProgram>,
    ) -> Result<(), StepError> {
        let (plan, tokenizer) = {
            let compiler = lock(&compiler.0);
            #[cfg(test)]
            let completed = match COMPLETED_REQUEST.with(|c| c.borrow_mut().take()) {
                Some(request) => Completed::Request(request),
                None => Completed::Artifact(
                    compiler
                        .completed_input(&state.run_id, &state.deployment_id)
                        .map_err(|e| refused(&e.code_or("compiler_not_completed")))?,
                ),
            };
            #[cfg(not(test))]
            let completed = Completed::Artifact(
                compiler
                    .completed_input(&state.run_id, &state.deployment_id)
                    .map_err(|e| refused(&e.code_or("compiler_not_completed")))?,
            );
            let request = completed.request();
            if request.node_ids != state.participants
                || request.deployment_id != state.deployment_id
            {
                return Err(refused("compiler_run_changed"));
            }
            let model = state
                .model
                .as_ref()
                .ok_or_else(|| refused("compiler_not_completed"))?;
            let oracle: Value =
                serde_json::from_str(stage_contract::native_proof::QWEN3_PACKED_ORACLE)
                    .map_err(|_| refused("native_reference_invalid"))?;
            let tokens = stage_tokens(request, &oracle)?;
            let contributor = self
                .host
                .with(|c| owner_ports::contributor_dir(&c.authority.root));
            check_run_stage_memory(request, state, &contributor, tokens)?;
            let artifact = completed.artifact()?;
            // `_step_slices` (`demo_chain.py:838-839`): a whole-snapshot run has no slice to
            // fetch. v0.1.4 binds the snapshot each member already holds
            // (`bind_hf_prepositions`) and passes the step; only a stage-sharded compile has a
            // shard document to read. Reading it unconditionally refused every packaged run at
            // this step with `compiler_outputs_changed`.
            let document = work_document(request.stage_sharded, artifact.stage_shards().ok())?;
            let objects = if request.stage_sharded {
                objects::view(artifact.root(), &document)?
            } else {
                objects::view_whole(artifact.root())?
            };
            (
                (
                    Plan {
                        run_id: state.run_id.clone(),
                        deployment_id: state.deployment_id.clone(),
                        model: json!({"model_id":model.model_id,"resolved_commit":model.resolved_commit,"manifest_digest":model.manifest_digest}),
                        document,
                        objects: objects.path,
                        journal: artifact.root().join("work-source"),
                        ranges: request
                            .node_ids
                            .iter()
                            .cloned()
                            .zip(
                                request
                                    .managed_layer_ranges
                                    .iter()
                                    .map(|r| [r[0] as u64, r[1] as u64]),
                            )
                            .collect(),
                        tokens,
                        forbidden_source: artifact.source().source_root.clone(),
                        worker_walls: state.worker_wall_limits.clone(),
                        owner_root: self.host.with(|c| c.authority.root.clone()),
                        // The compile's passed native challenge, equal to the recorded reference
                        // (`route.startup_challenge`, route.py:2371-2378 @ f4e366e9).
                        challenge: Some(
                            api::joined_owner::work::compiled_challenge(artifact.root())
                                .map_err(|e| refused(&e.0))?,
                        ),
                    },
                    objects.pinned,
                ),
                artifact.source().source_root.join("tokenizer.json"),
            )
        };
        let codec = Arc::new(PinnedCodec {
            program: program
                .cloned()
                .ok_or_else(|| refused("native_challenge_producer_unavailable"))?,
            tokenizer,
            helper: Mutex::new(None),
        });
        self.compose(state, plan, codec, env)
    }
    /// The sole constructor seam used by model-free witnesses supplies source shards/codec,
    /// never a fake Qualified artifact. Production reaches this only through completed_input.
    fn compose(
        &self,
        state: &RunState,
        (plan, objects): (Plan, Arc<runtime::artifact_journal::PinnedObjects>),
        codec: Arc<dyn Codec>,
        env: &mut dyn StepEnv,
    ) -> Result<(), StepError> {
        // A previous close's owners are settled (or still owed) before this run may compose:
        // no replacement while member custody is unresolved.
        settle_debt(&self.host).map_err(|code| refused(&code))?;
        if lock(&self.resident).work.is_some() {
            return Err(refused("owner_work_already_composed"));
        }
        let seed = self
            .host
            .with(|c| c.authority.seed.clone())
            .ok_or_else(|| refused("owner_exchange_unavailable"))?;
        // One transport per run, read from this Mac's interfaces as the run starts its work, here
        // and always here (lane PHY patch 3, PHY2-01): the probe passes before the compile may have
        // read an older state of them.
        let resolved = self.host.network.resolve_run_network("owner_run");
        // Lane XB3: a run across networks whose relay is unreachable fails closed, by name,
        // before the local worker starts or anything is dialed (never a fallback relay or n0).
        if crate::packaged_work::reach::require_relay(&resolved).is_err() {
            let (code, message) = crate::packaged_work::reach::relay_refusal();
            return Err(owner_run::OwnerRunError::with(code, Some(&message), None).into());
        }
        let probed = self.take_preflight(&state.run_id);
        // No single private network here while a joined Mac is in the run: refused by name now,
        // before the local worker starts or anything is dialed (lane LAN round 2, F6).
        if let Some(reason) = resolved.local_only
            && state
                .participants
                .iter()
                .any(|node| Some(node) != state.local_node.as_ref())
        {
            // Lane XB patch 3 (REVIEW XB2-07): a run across networks gets relay-mode advice.
            let across = matches!(
                self.host.network.reach.run_choice(),
                Some(Ok(api::run_exchange::work_network::RunReach::Relay(_)))
            );
            let (code, message) = if across {
                crate::packaged_work::reach::owner_network_refusal_across(reason)
            } else {
                crate::packaged_work::owner_network_refusal(reason)
            };
            return Err(owner_run::OwnerRunError::with(code, Some(&message), None).into());
        }
        // PHY2-01: a network other than the one the probe passes asked on (local-only <-> LAN, or
        // another local address): what they reached proves nothing for this one. Ask again, once,
        // from this network, this run's current verified members only, before any work endpoint
        // or dial; a blocked answer refuses by name as before.
        if let Some(probed) = probed
            && probed.choice != resolved.choice
        {
            eprintln!(
                "local_network_resolution_changed:{}:{}",
                probed.transport, resolved.transport
            );
            lock(&self.local_network).reached.clear();
            self.probe_pass(state, true, || resolved.clone())?;
        }
        let network = resolved.choice;
        lock(&self.resident).network = Some(network.clone());
        // The first advertised address the run's transport refuses: its refusal is named below.
        let refused_at = std::cell::Cell::new(None);
        let admits = |address: std::net::SocketAddr| {
            let admitted = network.allows(address);
            if !admitted {
                refused_at.set(Some(address));
            }
            admitted
        };
        let until = env.now() + span(BudgetId::Slices)?.as_secs_f64();
        let verified = loop {
            if env.stopped() || self.stopped.load(Ordering::SeqCst) {
                return Err(StepError::Stopped);
            }
            match self.local(&network) {
                // Lane XB patch 3 (REVIEW XB2-04): status names the run's transport ACTIVE only
                // once this Mac's own work endpoints are bound (across networks: with their relay).
                Ok(()) => self.host.network.reach.activate(&network),
                Err(StepError::Refused(e)) if e.code == "member_work_renewing" => {
                    env.progress("member_work_renewing");
                }
                Err(e) => return Err(e),
            }
            let current = state
                .participants
                .iter()
                .map(|node| {
                    seed.work_members.get(
                        &seed.coordinator,
                        node,
                        self.owner.public(),
                        &state.run_id,
                        &state.deployment_id,
                        &admits,
                    )
                })
                .collect::<Result<Vec<_>, _>>();
            match current {
                Ok(v) => break v,
                Err(e) if e.code == "member_work_unavailable" => {
                    // Lane XB patch 2 (D3): a joined Mac that refused this run's signed reach
                    // by name (its report) fails the run by that name, never by the timeout.
                    if let Some(refused) = member_reach_refusal(&seed.exchange.reports(), state) {
                        return Err(refused);
                    }
                    env.progress("member_work_unavailable");
                }
                Err(e) => match refused_at.get() {
                    Some(address) if e.code == "member_work_address_refused" => {
                        let (code, message) = network.address_refusal(address);
                        return Err(
                            owner_run::OwnerRunError::with(code, Some(&message), None).into()
                        );
                    }
                    _ => return Err(refused(e.code)),
                },
            }
            if env.now() >= until {
                return Err(refused("member_work_unavailable"));
            }
            if env.pause(span(BudgetId::OwnerPoll)?.as_secs_f64()) {
                return Err(StepError::Stopped);
            }
        };
        let mut members = Vec::new();
        for (node, v) in state.participants.iter().zip(&verified) {
            let link = RemoteMember::already_joined(
                self.owner.clone(),
                &seed.coordinator,
                v,
                network.0.clone(),
            )
            .map_err(|e| refused(&e.0))?;
            members.push(WorkMember {
                node_id: node.clone(),
                registration: v.registration().clone(),
                cancel: Arc::new(link.canceller()),
                link: Box::new(link),
            });
        }
        // Align to the Runner's origin, not this constructor's Instant zero.
        let at = Instant::now();
        let mono = env.now();
        let wall = env.wall();
        let run_clock: RunClock = Arc::new(move || mono + at.elapsed().as_secs_f64());
        *lock(&self.clock) = Some(run_clock.clone());
        let work = OwnerWork::new(
            seed.coordinator.clone(),
            members,
            plan,
            network.0,
            codec,
            Clocks {
                mono: run_clock,
                wall: Arc::new(move || wall + at.elapsed().as_secs_f64()),
            },
        )
        .map_err(|e| refused(&e.0))?
        .with_objects(objects);
        let stopper = Arc::new(work.stopper());
        let stopped = self.stopped.clone();
        let read = Arc::new(work.gateway_reader());
        let mut custody = lock(&self.host.custody);
        custody.work_stop = Some(Arc::new(move || {
            stopped.store(true, Ordering::SeqCst);
            stopper();
        }));
        // The packaged chat's run route reads this run's open epoch here (`chat::RunChatRoute`).
        custody.route = Some(super::chat::RouteSource {
            run_id: state.run_id.clone(),
            read,
        });
        drop(custody);
        let mut resident = lock(&self.resident);
        resident.bindings = verified;
        resident.run = Some((state.run_id.clone(), state.deployment_id.clone()));
        resident.work = Some(work);
        Ok(())
    }
    fn shares_ready(&self) -> Result<(), String> {
        let seed = self
            .host
            .with(|c| c.authority.seed.clone())
            .ok_or("owner_exchange_unavailable")?;
        let resident = lock(&self.resident);
        let (run, deployment) = resident.run.as_ref().ok_or("owner_work_unavailable")?;
        let network = resident.network.clone().ok_or("owner_work_unavailable")?;
        for binding in &resident.bindings {
            let announcement = seed
                .exchange
                .announcement(Some(binding.node_id()))
                .to_value();
            if announcement["phase"] != "work" || announcement["work_limits"].is_null() {
                return Err("member_share_unannounced".into());
            }
            let current = seed
                .work_members
                .get(
                    &seed.coordinator,
                    binding.node_id(),
                    self.owner.public(),
                    run,
                    deployment,
                    &|a| network.allows(a),
                )
                .map_err(|e| e.code.to_owned())?;
            if current.registration() != binding.registration()
                || current.address() != binding.address()
            {
                return Err("member_work_binding_changed".into());
            }
        }
        Ok(())
    }
    fn await_shares(&self) -> Result<(), PortError> {
        let budget = span(BudgetId::Publish)
            .map_err(|_| api::owner_compiler::failed("budget_unavailable"))?;
        let poll = span(BudgetId::OwnerPoll)
            .map_err(|_| api::owner_compiler::failed("budget_unavailable"))?;
        let until = Instant::now() + budget;
        loop {
            if self.stopped.load(Ordering::SeqCst) {
                return Err(api::owner_compiler::failed("owner_work_stopped"));
            }
            match self.shares_ready() {
                Ok(()) => return Ok(()),
                Err(code)
                    if code == "member_work_share_changed" || code == "member_work_unavailable" =>
                {
                    if Instant::now() >= until {
                        return Err(api::owner_compiler::failed("member_share_timeout"));
                    }
                    std::thread::sleep(poll.min(until.saturating_duration_since(Instant::now())));
                }
                Err(code) => return Err(api::owner_compiler::failed(&code)),
            }
        }
    }
    fn teardown(&self) -> Result<(), String> {
        // The chat's route ends with the run's work (the epoch closes below).
        lock(&self.host.custody).route = None;
        // So does what its probe passes resolved and reached (PHY2-01).
        self.reset_preflight();
        let mut resident = lock(&self.resident);
        // The run's transport ends with it (R2-7): whatever asks next reads this Mac afresh.
        resident.network = None;
        // Retry a previous close's exact owners first: a new close never proceeds while
        // custody it did not receive is still owed.
        settle_debt(&self.host)?;
        if let Some(mut work) = resident.work.take() {
            if let Err(code) = work.teardown() {
                retain_debt(&self.host, Some(work), resident.local.take());
                if let Some(debt) = lock(&self.host.custody).work_debt.as_mut() {
                    debt.unprovable = Some(code.clone());
                }
                return Err(code);
            }
            if let Some(code) = work.state()["cleanup_error"].as_str() {
                eprintln!(
                    "owner_work_cleanup_failed:{code}:{}",
                    work.state()["member_cleanup"]
                );
                // Keep the member Work (with its returned links) and the local Service whose
                // control loop those links reach; the next close retries only its open rows.
                retain_debt(&self.host, Some(work), resident.local.take());
                return Err(code.into());
            }
            // Every member stage is proven released: drop the returned owner-side links only
            // now (dropping a link never proves cleanup, so it is never the exit's substitute).
            drop(work.take_members());
        }
        if let Some(mut service) = resident.local.take()
            && let Err(code) = service.close()
        {
            eprintln!("member_work_custody_cleanup_failed:{code}");
            retain_debt(&self.host, None, Some(service));
            return Err(code);
        }
        lock(&self.host.custody).work_stop = None;
        if let Some(seed) = self.host.with(|c| c.authority.seed.clone()) {
            seed.exchange.set_owner_control(None);
        }
        Ok(())
    }
    /// The Runner's clock as the run's work aligned it; none before any work was composed.
    fn run_clock(&self) -> Option<f64> {
        lock(&self.clock).as_ref().map(|mono| mono())
    }
    /// `chain.rollover_due(ctx)`, `ceiling_reached(ctx)` and `monitor(ctx)` of a serving run
    /// (`demo_chain.py:1132-1186`) over its resident workflow, whose clock is the Runner's. With no
    /// workflow resident, `rollover_due` and `ceiling_reached` still read the run's state and clock
    /// (:1132-1164) and `monitor` answers `managed_workflow_missing` after the ceiling and after
    /// `tick_local` (`self._tick_local()`, :1166-1176). The resident workflow's own monitor
    /// (`OwnerWork::monitor`) does not tick (a pre-existing difference, outside lane SRV).
    /// Before any work was composed there is no run clock and no `execution_clock_started_at`
    /// (publish starts it after the slices step composed the work): nothing is due.
    pub(super) fn rollover_due(&self, state: &RunState) -> Option<bool> {
        if let Some(work) = lock(&self.resident).work.as_ref() {
            return work.rollover_due(state);
        }
        match self.run_clock() {
            Some(now) => api::joined_owner::work::rollover_due(state, now),
            None => Some(false),
        }
    }
    pub(super) fn ceiling_reached(&self, state: &RunState) -> CeilingProbe {
        if let Some(work) = lock(&self.resident).work.as_ref() {
            return work.ceiling_reached(state);
        }
        match self.run_clock() {
            Some(now) => api::joined_owner::work::ceiling_reached(state, now),
            None => CeilingProbe::Code(None),
        }
    }
    pub(super) fn monitor(&self, state: &RunState, tick_local: &mut dyn FnMut()) -> MonitorProbe {
        if let Some(work) = lock(&self.resident).work.as_ref() {
            return work.monitor(state);
        }
        match self.ceiling_reached(state) {
            CeilingProbe::Code(Some(code)) => MonitorProbe::Code(Some(code)),
            CeilingProbe::Raised { class } => MonitorProbe::Raised { class },
            CeilingProbe::Code(None) | CeilingProbe::Unavailable => {
                // Never under the resident lock: the local tick takes it itself.
                tick_local();
                MonitorProbe::Code(Some("managed_workflow_missing".into()))
            }
        }
    }
    /// True while this run, or the host after its own close failed, still owns cleanup debt.
    #[cfg(test)]
    fn retained(&self) -> bool {
        let resident = lock(&self.resident);
        resident.work.is_some()
            || resident.local.is_some()
            || lock(&self.host.custody).work_debt.is_some()
    }
    /// True while the exact member Work (with its returned links) is still owned. A local
    /// service error alone must not mask a dropped member owner.
    #[cfg(test)]
    fn member_work_retained(&self) -> bool {
        let resident = lock(&self.resident);
        resident.work.is_some()
            || lock(&self.host.custody)
                .work_debt
                .as_ref()
                .is_some_and(|debt| debt.work.is_some())
    }
}
impl SlicesPort for Work {
    fn node_slice_plans(&mut self) -> Result<Option<BTreeMap<String, Value>>, StepError> {
        lock(&self.resident)
            .work
            .as_mut()
            .ok_or_else(|| refused("owner_work_unavailable"))?
            .node_slice_plans()
    }
    fn storage_policy(&mut self) -> Result<Option<Value>, StepError> {
        lock(&self.resident)
            .work
            .as_mut()
            .ok_or_else(|| refused("owner_work_unavailable"))?
            .storage_policy()
    }
}
impl WorkPort for Work {
    fn route_readiness(&self) -> Option<owner_run::chain::RouteReadiness> {
        // Capture this workflow, never resolve a successor through the resident slot later.
        Some(
            lock(&self.resident)
                .work
                .as_ref()
                .and_then(WorkPort::route_readiness)
                .unwrap_or_else(|| Arc::new(|| false)),
        )
    }
    fn install_cleanup_progress(&mut self) -> Result<(), StepError> {
        lock(&self.resident)
            .work
            .as_mut()
            .ok_or_else(|| refused("owner_work_unavailable"))?
            .install_cleanup_progress()
    }
    fn start_serving(&mut self, proposal: &Value) -> Result<Value, PortError> {
        self.await_shares()?;
        lock(&self.resident)
            .work
            .as_mut()
            .ok_or_else(|| api::owner_compiler::failed("owner_work_unavailable"))?
            .start_serving(proposal)
    }
    /// `workflow.status()`, or null when no workflow is resident (`_workflow_state()` is None,
    /// demo_chain.py:434-436; the serving wait reads `{}`, :1016-1017).
    fn workflow_state(&mut self) -> Result<Value, StepError> {
        match lock(&self.resident).work.as_mut() {
            Some(work) => work.workflow_state(),
            None => Ok(Value::Null),
        }
    }
    /// The serving step's read: absent only when no workflow is resident; a resident workflow's
    /// status is passed on as it is (a present `null` raises in the step, demo_chain.py:1017).
    fn serving_status(&mut self) -> Result<Option<Value>, StepError> {
        match lock(&self.resident).work.as_mut() {
            Some(work) => work.workflow_state().map(Some),
            None => Ok(None),
        }
    }
    /// `needs_identity` / `current` over the run's resident workflow; with none resident, the
    /// run's own compile decides whether one was required (`demo_chain.py:988-992`).
    fn serving_identity(&mut self, state: &RunState) -> Result<ServingIdentity, StepError> {
        Ok(match lock(&self.resident).work.as_ref() {
            Some(work) => work.serving_identity_of(state),
            None if owner_run::steps::serving::needs_identity(state) => ServingIdentity::Changed,
            None => ServingIdentity::Absent,
        })
    }
    /// `self._exchange()` and `exchange.prepare_announcement(...)` before any lock
    /// (`demo_chain.py:1059-1066`: the exchange's clock and the validation run under no lock),
    /// then the resident lock (the app's guard over installing and retiring the run's workflow,
    /// :1074), then the owner run's fence and the workflow lock inside
    /// `OwnerWork::commit_prepared_on`.
    fn commit_serving(
        &mut self,
        state: &RunState,
        announcement: &Announcement,
        work_limits: Option<&Value>,
        fence: Fence<'_>,
    ) -> Result<(), StepError> {
        let exchange = owner_ports::ensure_exchange(&self.host)
            .ok_or_else(|| refused("owner_exchange_unavailable"))?;
        let prepared =
            api::joined_owner::work::prepare_serving(&exchange, announcement, work_limits)?;
        let resident = lock(&self.resident);
        match resident.work.as_ref() {
            Some(work) => work.commit_prepared_on(state, &exchange, &prepared, fence),
            None => fence(&mut || Err(refused("serving_workflow_identity_changed"))),
        }
    }
}
impl ActivationPort for Work {
    fn activate(&mut self, proposal: &Value) -> Result<(), PortError> {
        lock(&self.resident)
            .work
            .as_mut()
            .ok_or_else(|| api::owner_compiler::failed("owner_work_unavailable"))?
            .activate(proposal)
    }
}
/// Existing reports for every other phase. Work announcement preserves the steps' exact limits.
pub(super) struct Reports {
    pub inner: OwnerPorts<PackagedRun>,
    pub work: Work,
}
impl ReportsPort for Reports {
    fn forget_reports(&mut self) {
        self.inner.forget_reports();
    }
    fn announce(&mut self, a: &Announcement) -> Result<(), StepError> {
        self.inner.announce(a)
    }
    fn announce_work(&mut self, a: &Announcement, limits: &Value) -> Result<(), StepError> {
        let exchange = owner_ports::ensure_exchange(&self.work.host)
            .ok_or_else(|| refused("owner_exchange_unavailable"))?;
        exchange.announce(Announce { phase:a.phase.clone(),run_id:Some(a.run_id.clone()),deployment_id:Some(a.deployment_id.clone()),
            model:Some(json!({"choice":api::model_catalog::find(&a.model_id,&a.resolved_commit).and_then(api::model_catalog::choice_for_model),"model_id":a.model_id,"resolved_commit":a.resolved_commit})),
            slice_mode:a.slice_mode,manifest_digest:Some(a.manifest_digest.clone()),work_limits:Some(limits.clone()),
            per_node:a.per_node.iter().flatten().cloned().collect() }).map_err(|_| refused("demo_announcement_invalid"))?;
        let network = self.work.run_network();
        self.work.local(&network)
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        self.inner.reports()
    }
    fn members(&mut self) -> Vec<MemberRow> {
        self.inner.members()
    }
    fn own_node_id(&mut self) -> Option<String> {
        self.inner.own_node_id()
    }
    fn member_identity(&mut self, node: &str) -> Value {
        self.inner.member_identity(node)
    }
    fn clock_skew(&mut self, nodes: &[String]) -> Result<Option<ClockSkew>, StepError> {
        self.inner.clock_skew(nodes)
    }
    fn tick_local(&mut self) {
        let report = lock(&self.work.resident)
            .local
            .as_ref()
            .map_or(Ok(None), crate::packaged_work::Service::report);
        match report {
            Ok(Some(report)) => {
                let sent = self.work.host.with(|c| {
                    let seed = c
                        .authority
                        .seed
                        .as_ref()
                        .ok_or("owner_exchange_unavailable")?;
                    let node = c
                        .run
                        .contributor
                        .as_ref()
                        .ok_or("member_work_unavailable")?;
                    seed.exchange
                        .submit_local(&node.node, &report)
                        .map_err(|_| "member_work_report_failed")
                });
                if let Err(code) = sent {
                    eprintln!("member_work_report_failed:{code}");
                }
            }
            Ok(None) => self.inner.tick_local(),
            Err(code) => {
                self.work.host.stop_work();
                eprintln!("member_work_report_failed:{code}");
            }
        }
    }
}
/// Intercepts only composition, never a step implementation or plan. The shared chain continues
/// to own slices -> publish -> targets -> activation and its state transitions.
pub(super) struct Composed {
    inner: SharedHostChain<PackagedChainPorts>,
    work: Work,
    compiler: SharedCompiler<PackagedRun>,
    program: Option<api::native_compiler::NativeProgram>,
    /// PHY-06: a model-free witness replaces `Work::prepare` and the shared chain's slices step,
    /// never the rename between them and the returned refusal.
    #[cfg(test)]
    slices_seam: Option<SlicesSeam>,
}
/// PHY-06 (lane PHY patch 2): the two halves of the slices step a model-free test supplies.
#[cfg(test)]
type SlicesHalf = Box<dyn FnMut(&Work, &mut dyn StepEnv) -> Result<(), StepError> + Send>;
#[cfg(test)]
pub(super) struct SlicesSeam {
    /// Stands for `Work::prepare` (the compile's artifact into the run's composed work).
    pub prepare: SlicesHalf,
    /// Stands for the shared chain's slices step (`run_slices_step` over the work's ports).
    pub step: SlicesHalf,
}
impl Composed {
    pub(super) fn new(
        host: &PackagedRun,
        double: Option<&ObserveTestDouble>,
        program: Option<api::native_compiler::NativeProgram>,
    ) -> Self {
        let ports = PackagedChainPorts::new(host, double, program.clone());
        let work = ports.work.clone();
        let compiler = SharedCompiler(ports.compiler.0.clone());
        // Lane A, B7 / CAP-1: the compile plans each member's memory share with the stage
        // worker's own reservation, the figure the member's worker guard then enforces.
        lock(&compiler.0).set_stage_reservation(Some(crate::native_selection::planned_reservation));
        Self {
            inner: SharedHostChain::new(ports),
            work,
            compiler,
            program,
            #[cfg(test)]
            slices_seam: None,
        }
    }
    /// The slices step's preparation: the compile's artifact into the run's composed work.
    fn prepare_slices(&mut self, env: &mut dyn StepEnv) -> Result<(), StepError> {
        #[cfg(test)]
        if let Some(seam) = self.slices_seam.as_mut() {
            return (seam.prepare)(&self.work, env);
        }
        let state = self
            .inner
            .state()
            .ok_or_else(|| refused("compiler_run_changed"))?;
        self.work
            .prepare(state, env, &self.compiler, self.program.as_ref())
    }
    /// The shared chain's slices step over the composed work.
    fn shared_slices(&mut self, env: &mut dyn StepEnv) -> Result<(), StepError> {
        #[cfg(test)]
        if let Some(seam) = self.slices_seam.as_mut() {
            return (seam.step)(&self.work, env);
        }
        self.inner.run_step("slices", env)
    }
}
impl Chain for Composed {
    fn new_run(
        &mut self,
        run: &str,
        deployment: &str,
        reason: owner_run::Reason,
        ceiling: Option<&Value>,
        binding: Option<&Value>,
    ) {
        // Lane XB patch 2 (D1): the owner's network reach for THIS run, read once now and
        // signed in every announcement of the run; a joined Mac binds in exactly that mode. An
        // invalid choice publishes nothing and refuses the run's first step by name.
        let reach = self.work.host.network.begin_owner_run();
        if let Some(seed) = self.work.host.with(|c| c.authority.seed.clone()) {
            seed.exchange
                .set_owner_control(Some(OwnerControl::new(self.work.owner.public())));
            seed.exchange.set_work_network(
                reach.unwrap_or(api::run_exchange::work_network::RunReach::SameNetwork),
            );
        }
        // A fresh `RunContext` for the page's chats too (lane CHAT, owner_run.py:392-399).
        super::chat::new_run(&self.work.host);
        // Before this run reserves anything: the charges of this build's runs that ended without
        // retiring (a lost process) are released; an older build's run is only named (lane PHY
        // item 1, PHY-01).
        super::retire_dead_runs(&self.work.host);
        // Nothing an earlier run's probe passes resolved or reached (PHY2-01).
        self.work.reset_preflight();
        self.inner
            .new_run(run, deployment, reason, ceiling, binding);
    }
    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        // Lane XB patch 2 (D1, D3): a reach this Mac cannot bind (an invalid preference file, a
        // loopback-fixed transport across networks) refuses the run's steps by name.
        if let Err(code) = self.work.host.network.owner_run_network() {
            return Err(owner_run::OwnerRunError::with(
                code,
                Some(&crate::packaged_work::reach::owner_refusal_message(code)),
                None,
            )
            .into());
        }
        // Lane PHY item 2: macOS's Local Network question is asked before the compile is spent,
        // and a pending or denied answer refuses Start's first step and the compile by name.
        // Steps 1-5 (`machine::STEPS`): the joined Macs' work addresses arrive during step 3.
        if let ("model" | "contributor" | "descriptors" | "observe" | "compile", Some(state)) =
            (step, self.inner.state())
        {
            self.work
                .local_network(state, matches!(step, "model" | "compile"))?;
        }
        // Lane XB patch 3 (REVIEW XB2-03): a joined Mac's named reach refusal ends the steps
        // before compose at once, by its name.
        let watched = match (step, self.inner.state()) {
            ("contributor" | "descriptors" | "observe" | "compile", Some(state)) => Some((
                (state.run_id.clone(), state.deployment_id.clone()),
                state.participants.clone(),
            )),
            _ => None,
        };
        if let (Some((run, participants)), Some(seed)) =
            (watched, self.work.host.with(|c| c.authority.seed.clone()))
        {
            let reports = move || seed.exchange.reports();
            let inner = &mut self.inner;
            return watched_step(
                env,
                &reports,
                run,
                participants,
                step == "descriptors",
                |watch| inner.run_step(step, watch),
            );
        }
        if step == "slices" {
            // A preparation refusal is returned as it is (no workflow has run).
            self.prepare_slices(env)?;
            // Lane PHY item 2 (c): a refused step whose workflow failed on a dial without an
            // answer is named, not `managed_workflow_failed`; Stop, an exception and every other
            // refusal pass unchanged (only a refusal is probed).
            return self.shared_slices(env).map_err(|failed| match failed {
                StepError::Refused(_) => self.work.named_step_failure().unwrap_or(failed),
                other => other,
            });
        }
        self.inner.run_step(step, env)
    }
    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        let outcome = self.inner.teardown(failed, env);
        // Lane XB patch 2: the run's reach ends with it (status shows no active endpoints).
        self.work.host.network.end_owner_run();
        if let Some(seed) = self.work.host.with(|c| c.authority.seed.clone()) {
            seed.exchange
                .set_work_network(api::run_exchange::work_network::RunReach::SameNetwork);
        }
        outcome
    }
    fn rollover_binding(&self) -> Option<Value> {
        self.inner.rollover_binding()
    }
    fn rollover_due(&mut self) -> Option<bool> {
        self.inner.rollover_due()
    }
    fn ceiling_reached(&mut self) -> owner_run::machine::CeilingProbe {
        self.inner.ceiling_reached()
    }
    fn monitor(&mut self) -> owner_run::machine::MonitorProbe {
        self.inner.monitor()
    }
}
impl PackagedRun {
    /// Independent of OwnerWork's mutex: reachable while a slices push blocks.
    pub(super) fn stop_work(&self) {
        let stop = lock(&self.custody).work_stop.clone();
        if let Some(stop) = stop {
            stop();
        }
    }
}
impl PackagedChainPorts {
    pub(super) fn slices(&mut self) -> SlicesPorts<'_> {
        SlicesPorts {
            reports: &mut self.reports,
            slices: &mut self.work,
        }
    }
    pub(super) fn publication(&mut self) -> WorkPorts<'_> {
        WorkPorts {
            reports: &mut self.reports,
            workflow: &mut self.work,
        }
    }
    pub(super) fn close_work(&mut self) -> Result<(), String> {
        self.work.teardown()
    }
}

#[cfg(test)]
mod challenge_tokens_tests {
    //! A joined Mac's prepare step needs the token count of the recorded workload. The packed
    //! oracle keeps every recorded value under `recorded`; reading the document's top level found
    //! nothing and answered `native_reference_invalid` on a two-Mac run.
    #[test]
    fn the_packed_oracle_yields_the_recorded_prompt_plus_the_challenge_output() {
        let reference: serde_json::Value =
            serde_json::from_str(stage_contract::native_proof::QWEN3_PACKED_ORACLE)
                .expect("the shipped oracle is JSON");
        let prompt = reference["recorded"]["workload"]["prompt_ids"]
            .as_array()
            .expect("the oracle records its workload")
            .len();
        assert_eq!(
            super::challenge_tokens(&reference).expect("tokens"),
            prompt + stage_contract::native_proof::CHALLENGE_OUTPUT_TOKENS
        );
        assert!(reference.get("workload").is_none(), "no top-level workload");
    }
}

#[cfg(test)]
mod stage_tokens_tests {
    //! Lane CAP: a stage is prepared for the serving context the compile planned (the model
    //! contract's, `model_catalog::Model::serving_context_tokens`), never for the startup check's own length (35 + 4),
    //! which left no room for any product chat (`joined_route_context_exceeds_worker`, D6). The
    //! startup check stays one request inside that capacity.
    fn request(context: u64) -> planner::chain_compile::BuildRequest {
        planner::chain_compile::BuildRequest {
            model_id: "Qwen/Qwen3-0.6B".into(),
            resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
            deployment_id: "d".into(),
            route_label: "a13",
            stage_sharded: false,
            compilation_cache_key: "k".into(),
            node_ids: vec!["a".into(), "b".into()],
            managed_layer_ranges: vec![[0, 19], [19, 28]],
            layer_split: serde_json::json!({}),
            serving_context_tokens: context,
        }
    }
    fn oracle() -> serde_json::Value {
        serde_json::from_str(stage_contract::native_proof::QWEN3_PACKED_ORACLE).unwrap()
    }
    #[test]
    fn every_stage_is_prepared_for_the_compiles_serving_context() {
        let model = api::model_catalog::find(
            "Qwen/Qwen3-0.6B",
            "c1899de289a04d12100db370d81485cdf75e47ca",
        )
        .expect("the demo model is registered");
        let contract = api::owner_compiler::model_contract(model);
        assert_eq!(
            contract.serving_context_tokens,
            model.serving_context_tokens()
        );
        let tokens = super::stage_tokens(&request(contract.serving_context_tokens), &oracle())
            .expect("the startup check fits the context");
        assert_eq!(
            tokens as u64,
            model.serving_context_tokens(),
            "the contract's context, not the startup check's length"
        );
        assert_ne!(tokens, super::challenge_tokens(&oracle()).unwrap());
        // Whatever the compile planned is what the stage gets (no second figure).
        assert_eq!(
            super::stage_tokens(&request(1000), &oracle()).unwrap(),
            1000
        );
    }
    #[test]
    fn a_context_that_cannot_hold_the_startup_check_is_refused_by_name() {
        let challenge = super::challenge_tokens(&oracle()).unwrap();
        assert_eq!(challenge, 39, "the recorded 35 prompt ids plus 4");
        assert_eq!(
            super::stage_tokens(&request(challenge as u64), &oracle()).unwrap(),
            challenge
        );
        let refused = super::stage_tokens(&request(challenge as u64 - 1), &oracle())
            .expect_err("the startup check does not fit");
        assert!(
            format!("{refused:?}").contains("startup_challenge_exceeds_context"),
            "{refused:?}"
        );
    }
}

#[cfg(test)]
mod stage_memory_tests {
    //! Lane CAP item 2: at the oracle's 4,096-token context (the contract's until the owner's
    //! 16 Nov ruling), the stage worker reserves more memory than the owner's own 4 GB self-grant
    //! (`owner_ports.rs`): measured from the worker's admission, 6,792,778,240 B for layers [0,19]
    //! and 5,992,888,832 B for [19,28]. The run refuses that by name at the slices step instead of
    //! failing at the stage's prepare. These tests drive the check at explicit token counts; the
    //! contract's own figure is `work::tests::both_demo_stages_fit_the_owners_self_grant_…`.
    use super::*;
    const OWNER: u64 = 4_000_000_000;
    const JOINED: u64 = 8_589_934_592;
    fn request(ranges: [[usize; 2]; 2]) -> planner::chain_compile::BuildRequest {
        planner::chain_compile::BuildRequest {
            model_id: "Qwen/Qwen3-0.6B".into(),
            resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
            deployment_id: "d".into(),
            route_label: "a13",
            stage_sharded: false,
            compilation_cache_key: "k".into(),
            node_ids: vec!["owner".into(), "joined".into()],
            managed_layer_ranges: ranges.to_vec(),
            layer_split: json!({}),
            serving_context_tokens: 4096,
        }
    }
    fn records(owner: Option<u64>, joined: Option<u64>) -> Value {
        let caps = |m: Option<u64>| m.map_or(json!({}), |m| json!({"max_memory_bytes": m}));
        json!({"owner": {"caps": caps(owner)}, "joined": {"caps": caps(joined)}})
    }
    fn check(
        request: &planner::chain_compile::BuildRequest,
        records: &Value,
        tokens: usize,
    ) -> Result<(), StepError> {
        check_stage_memory(
            request,
            &|node| records[node]["caps"]["max_memory_bytes"].as_u64(),
            tokens,
        )
    }
    fn code(result: Result<(), StepError>) -> String {
        match result {
            Ok(()) => "ok".into(),
            Err(StepError::Refused(e)) => {
                assert!(
                    e.message.as_deref().is_some_and(|m| m.contains("owner")
                        && m.contains("4000000000")
                        && m.contains("4096")),
                    "the refusal names the Mac, its consent and the context: {e:?}"
                );
                e.code
            }
            Err(other) => format!("{other:?}"),
        }
    }
    #[test]
    fn the_worker_reservation_is_the_one_the_stage_prepare_holds() {
        use crate::native_selection::{Profile, stage_reservation};
        let at = |range: std::ops::Range<u32>, tokens| {
            stage_reservation("Qwen/Qwen3-0.6B", Profile::PackedInt8, range, 28, tokens).unwrap()
        };
        assert_eq!(at(0..19, 39), 982_349_824);
        assert_eq!(at(19..28, 39), 811_458_560);
        assert_eq!(at(0..19, 4096), 6_792_778_240);
        assert_eq!(at(19..28, 4096), 5_992_888_832);
    }
    #[test]
    fn a_stage_whose_consent_cannot_cover_the_context_is_refused_by_name_at_slices() {
        let owner_first = request([[0, 19], [19, 28]]);
        assert_eq!(
            code(check(
                &owner_first,
                &records(Some(OWNER), Some(JOINED)),
                4096
            )),
            "stage_memory_consent_below_context"
        );
        let mut swapped = request([[19, 28], [0, 19]]);
        swapped.node_ids = vec!["joined".into(), "owner".into()];
        assert_eq!(
            code(check(&swapped, &records(Some(OWNER), Some(JOINED)), 4096)),
            "stage_memory_consent_below_context",
            "the owner's 9 layers at 4,096 (5.99 GB) do not fit 4 GB either"
        );
        // The startup check's own 39 tokens fit both, and a joined Mac's 8 GiB covers either range.
        assert_eq!(
            code(check(&owner_first, &records(Some(OWNER), Some(JOINED)), 39)),
            "ok"
        );
        assert_eq!(
            code(check(
                &owner_first,
                &records(Some(JOINED), Some(JOINED)),
                4096
            )),
            "ok"
        );
        // No cap in the record: the stage's own prepare is the check.
        assert_eq!(
            code(check(&owner_first, &records(None, Some(JOINED)), 4096)),
            "ok"
        );
    }
}

#[cfg(test)]
mod work_document_tests {
    //! A packaged run is never stage-sharded (`slice_mode: false`), yet `prepare` used to read the
    //! shard document unconditionally and refuse `compiler_outputs_changed` at the slices step.
    //! v0.1.4 passes that step for whole snapshots (`demo_chain.py:838-839`).
    #[test]
    fn a_whole_snapshot_run_composes_without_a_shard_document() {
        let document = super::work_document(false, None).expect("whole document");
        assert_eq!(document["protocol"], "mycelium.source_whole_snapshot.v1");
        assert!(document["node_files"].as_object().unwrap().is_empty());
        assert!(document["node_slice_plans"].as_object().unwrap().is_empty());

        let shards = serde_json::json!({"node_files":{"n":[{"path":"deployment/s.safetensors"}]}});
        assert_eq!(super::work_document(true, Some(&shards)).unwrap(), shards);
        match super::work_document(true, None) {
            Err(super::StepError::Refused(e)) => assert_eq!(e.code, "compiler_outputs_changed"),
            other => panic!("a sharded run without its document must refuse: {other:?}"),
        }
    }
}
