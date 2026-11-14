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
        }
    }
    /// The run's transport, resolved at `compose`; a work announcement before any compose (none
    /// in the chain's order) resolves its own.
    fn run_network(&self) -> crate::packaged_work::NetworkChoice {
        let held = lock(&self.resident).network.clone();
        held.unwrap_or_else(|| self.host.network.resolve_work_network("owner_local_work"))
    }
    fn local(&self, network: &crate::packaged_work::NetworkChoice) -> Result<(), StepError> {
        let (root, seed, session, signer, node) = self.host.with(|c| {
            let member = c
                .run
                .contributor
                .as_ref()
                .ok_or_else(|| refused("member_work_unavailable"))?;
            Ok::<_, StepError>((
                owner_ports::contributor_dir(&c.authority.root),
                c.authority
                    .seed
                    .clone()
                    .ok_or_else(|| refused("owner_exchange_unavailable"))?,
                member
                    .session
                    .clone()
                    .ok_or_else(|| refused("member_work_renewing"))?,
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
            let artifact = compiler
                .completed_input(&state.run_id, &state.deployment_id)
                .map_err(|e| refused(&e.code_or("compiler_not_completed")))?;
            let request = artifact.request();
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
            let tokens = challenge_tokens(&oracle)?;
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
        // One transport per run, read from this Mac's interfaces as the run starts its work.
        let resolved = self.host.network.resolve_run_network("owner_run");
        // No single private network here while a joined Mac is in the run: refused by name now,
        // before the local worker starts or anything is dialed (lane LAN round 2, F6).
        if let Some(reason) = resolved.local_only
            && state
                .participants
                .iter()
                .any(|node| Some(node) != state.local_node.as_ref())
        {
            let (code, message) = crate::packaged_work::owner_network_refusal(reason);
            return Err(owner_run::OwnerRunError::with(code, Some(&message), None).into());
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
                Ok(()) => {}
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
        lock(&self.host.custody).work_stop = Some(Arc::new(move || {
            stopped.store(true, Ordering::SeqCst);
            stopper();
        }));
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
        Self {
            inner: SharedHostChain::new(ports),
            work,
            compiler,
            program,
        }
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
        if let Some(seed) = self.work.host.with(|c| c.authority.seed.clone()) {
            seed.exchange
                .set_owner_control(Some(OwnerControl::new(self.work.owner.public())));
        }
        self.inner
            .new_run(run, deployment, reason, ceiling, binding);
    }
    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        if step == "slices" {
            let state = self
                .inner
                .state()
                .ok_or_else(|| refused("compiler_run_changed"))?;
            self.work
                .prepare(state, env, &self.compiler, self.program.as_ref())?;
        }
        self.inner.run_step(step, env)
    }
    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        self.inner.teardown(failed, env)
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
