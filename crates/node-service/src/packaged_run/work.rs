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
    runner::Chain,
    state::RunState,
    steps::{
        activate::ActivationPort,
        exchange::{Announcement, ClockSkew, MemberRow, ReportsPort},
        publish::{WorkPort, WorkPorts},
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
}
#[derive(Clone)]
pub(super) struct Work {
    host: PackagedRun,
    owner: iroh::SecretKey,
    resident: Arc<Mutex<Resident>>,
    stopped: Arc<AtomicBool>,
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
            })),
            stopped: Arc::new(AtomicBool::new(false)),
        }
    }
    fn local(&self) -> Result<(), StepError> {
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
                    self.host.network.work_network(),
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
            let tokens = oracle["workload"]["prompt_ids"]
                .as_array()
                .ok_or_else(|| refused("native_reference_invalid"))?
                .len()
                + stage_contract::native_proof::CHALLENGE_OUTPUT_TOKENS;
            (
                Plan {
                    run_id: state.run_id.clone(),
                    deployment_id: state.deployment_id.clone(),
                    model: json!({"model_id":model.model_id,"resolved_commit":model.resolved_commit,"manifest_digest":model.manifest_digest}),
                    document: artifact.stage_shards().map_err(|e| refused(e.0))?.clone(),
                    objects: objects::view(
                        artifact.root(),
                        artifact.stage_shards().map_err(|e| refused(e.0))?,
                    )?,
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
                },
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
        plan: Plan,
        codec: Arc<dyn Codec>,
        env: &mut dyn StepEnv,
    ) -> Result<(), StepError> {
        if lock(&self.resident).work.is_some() {
            return Err(refused("owner_work_already_composed"));
        }
        let seed = self
            .host
            .with(|c| c.authority.seed.clone())
            .ok_or_else(|| refused("owner_exchange_unavailable"))?;
        let network = self.host.network.work_network();
        let until = env.now() + span(BudgetId::Slices)?.as_secs_f64();
        let verified = loop {
            if env.stopped() || self.stopped.load(Ordering::SeqCst) {
                return Err(StepError::Stopped);
            }
            match self.local() {
                Ok(()) => {}
                Err(StepError::Refused(e)) if e.code == "member_work_renewing" => {}
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
                        &|a| network.allows(a),
                    )
                })
                .collect::<Result<Vec<_>, _>>();
            match current {
                Ok(v) => break v,
                Err(e) if e.code == "member_work_unavailable" => {}
                Err(e) => return Err(refused(e.code)),
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
                &v,
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
        let work = OwnerWork::new(
            seed.coordinator.clone(),
            members,
            plan,
            network.0,
            codec,
            Clocks {
                mono: Arc::new(move || mono + at.elapsed().as_secs_f64()),
                wall: Arc::new(move || wall + at.elapsed().as_secs_f64()),
            },
        )
        .map_err(|e| refused(&e.0))?;
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
                    &|a| self.host.network.work_network().allows(a),
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
        let mut result = Ok(());
        if let Some(mut work) = resident.work.take() {
            result = work.teardown();
            // Always retrieve custody, even if teardown reports an error. Drop closes only
            // owner-side control connections; the enrolled member and its service stay joined.
            let members = work.take_members();
            drop(members);
            let state = work.state();
            if let Some(code) = state["cleanup_error"].as_str() {
                eprintln!(
                    "owner_work_cleanup_failed:{code}:{}",
                    state["member_cleanup"]
                );
                result = Err(code.into());
            }
        }
        if let Some(mut service) = resident.local.take() {
            result = result.and(service.close());
        }
        lock(&self.host.custody).work_stop = None;
        if let Some(seed) = self.host.with(|c| c.authority.seed.clone()) {
            seed.exchange.set_owner_control(None);
        }
        result
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
    fn workflow_state(&mut self) -> Result<Value, StepError> {
        lock(&self.resident)
            .work
            .as_mut()
            .ok_or_else(|| refused("owner_work_unavailable"))?
            .workflow_state()
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
        self.work.local()
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
