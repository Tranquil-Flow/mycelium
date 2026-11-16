// SPDX-License-Identifier: AGPL-3.0-only
//! Source-bound compiler front end. No operator plan, native proof or Launch is
//! fabricated when the native assignment/challenge producer is unavailable.
//!
//! The owner's compile port (lane PH3b S3m; moved unchanged from the headless launcher,
//! `examples/headless/compiler.rs`) over a [`ChainHost`]; no step logic lives here.
use crate::app_host::{ManagedSlot, SeedRuntime, now};
use crate::joined_member::observe_device;
use crate::owner_observation::{self as observation, ChainHost, ChainView};
use crate::preferences::{resource_policy, standing_consent};
use crate::schema::{PublicError, Raised};
use crate::{jobj, wire::Json};
use owner_run::chain::PortError;
use owner_run::steps::compile::{
    BuildPoll, CompileContext, CompileInputs, CompilePort, LocalDevice,
};
use owner_run::steps::observe::ObservePort;
use planner::chain_compile::{BuildRequest, GrantCaps, ModelContract};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, MutexGuard, Weak};

/// The compile's model contract for a registered model: its pinned identity, the runtime the
/// owner's compile builds for, and the model's own serving context, which the compile charges KV
/// memory for and every stage is then prepared for.
pub fn model_contract(model: &crate::model_catalog::Model) -> ModelContract {
    ModelContract {
        model_id: model.model_id.into(),
        resolved_commit: model.resolved_commit.into(),
        source_representation: "bfloat16-safetensors".into(),
        runtime_dtype: "float32".into(),
        runtime_quantization: "int8-weight-only".into(),
        serving_context_tokens: model.serving_context_tokens(),
    }
}
fn failure() -> PublicError {
    PublicError::new(409, "action_not_completed")
}
pub fn failed(code: &str) -> PortError {
    PortError::Failed {
        code: Some(code.into()),
        text: String::new(),
    }
}
fn lock<T>(value: &Mutex<T>) -> Result<MutexGuard<'_, T>, PortError> {
    value
        .lock()
        .map_err(|_| failed("compiler_state_unavailable"))
}
/// `lock(&core)?` over a [`ChainHost`]: the host's state as one snapshot, the same refusal when
/// its lock is poisoned.
fn view<H: ChainHost, R>(
    host: &H,
    f: impl FnOnce(&mut ChainView<'_, H>) -> R,
) -> Result<R, PortError> {
    host.try_with(f)
        .ok_or_else(|| failed("compiler_state_unavailable"))
}

fn local_device<M: ManagedSlot>(
    root: &Path,
    seed: Option<&SeedRuntime<M>>,
    node: Option<&str>,
) -> Result<Option<LocalDevice>, PortError> {
    let Some(node) = node else {
        return Ok(None);
    };
    let member = seed
        .ok_or_else(|| failed("compiler_member_changed"))?
        .coordinator
        .member(node)
        .map_err(|_| failed("compiler_member_changed"))?;
    let device = observe_device(root);
    let grant =
        standing_consent::load(root).map_err(|_| failed("compiler_resource_policy_denied"))?;
    let policy =
        resource_policy::read(root).map_err(|_| failed("compiler_resource_policy_denied"))?;
    Ok(Some(LocalDevice {
        memory_bytes: device["memory_bytes"]
            .as_u64()
            .ok_or_else(|| failed("member_capability_report_required"))?,
        available_storage_bytes: device.get("available_storage_bytes").cloned(),
        generation: member["generation"].clone(),
        grant: grant.map(|g| GrantCaps {
            storage_bytes: g["caps"]["storage_bytes"].as_u64(),
            max_memory_bytes: g["caps"]["max_memory_bytes"].as_u64(),
            max_wall_seconds: g["caps"]["max_wall_seconds"].as_u64(),
        }),
        policy_storage_bytes: policy.and_then(|p| p["storage_bytes"].as_u64()),
    }))
}

/// The same actual observation/session owner is borrowed by consecutive steps.
/// No duplicated native facts or second CLIENT journal.
#[derive(Clone)]
pub struct Observation<H: ChainHost>(pub Arc<Mutex<observation::Adapter<H>>>);
impl<H: ChainHost> ObservePort for Observation<H> {
    fn prepare(&mut self, run: &str, deployment: &str, cards: &[Value]) -> Result<(), PortError> {
        lock(&self.0)?.prepare(run, deployment, cards)
    }
    fn rebuild(
        &mut self,
        run: &str,
        deployment: &str,
        cards: &[Value],
    ) -> Result<String, PortError> {
        lock(&self.0)?.rebuild(run, deployment, cards)
    }
    fn facts_nodes(&mut self) -> Vec<String> {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .facts_nodes()
    }
    fn offers(&mut self) -> Vec<(String, String)> {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .offers()
    }
    fn collect(&mut self) -> Result<(), PortError> {
        lock(&self.0)?.collect()
    }
    fn observed_hosts(&mut self) -> Vec<Option<String>> {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .observed_hosts()
    }
    fn retire_bootstrap(&mut self) -> Result<(), PortError> {
        lock(&self.0)?.retire_bootstrap()
    }
}

/// The observe step's outcome as the compile port reads it: the exact context that completed
/// native hello (`context.facts`, `context.observations`) and its bootstrap retirement. The
/// production source is the run's own [`Observation`]; a host may give the compiler another
/// observe port's context (the packaged host's test double, decision U4) through
/// [`Compiler::observing`].
pub trait ObservedSource: Send {
    /// `f` over the run's observation context (`None` before the observe step prepared one).
    fn with_context(
        &self,
        f: &mut dyn FnMut(Option<&runtime::observe::ObservationContext>),
    ) -> Result<(), PortError>;
    /// `context.retire_bootstrap()`.
    fn retire_bootstrap(&mut self) -> Result<(), PortError>;
}
impl<H: ChainHost> ObservedSource for Observation<H> {
    fn with_context(
        &self,
        f: &mut dyn FnMut(Option<&runtime::observe::ObservationContext>),
    ) -> Result<(), PortError> {
        f(lock(&self.0)?.context());
        Ok(())
    }
    fn retire_bootstrap(&mut self) -> Result<(), PortError> {
        ObservePort::retire_bootstrap(self)
    }
}

/// Private material retained across the actual planner/worker boundary. This is
/// compiler INPUT, not the missing compiled operator/Controller/Launch output.
struct PreparedBuild {
    source: crate::serving_source::Prepared,
    inputs: CompileInputs,
    run: String,
    deployment: String,
    observations: Value,
    topology: Value,
    root: PathBuf,
}

type FinishedBuild = (
    crate::compiler_inputs::InputArtifact,
    Option<crate::native_compiler::Qualified>,
);

pub struct Compiler<H: ChainHost> {
    core: H::Weak,
    observation: Box<dyn ObservedSource>,
    prepared: Option<PreparedBuild>,
    metadata: Option<CompileContext>,
    failure: Option<PortError>,
    artifact: Option<crate::compiler_inputs::InputArtifact>,
    retired: bool,
    worker: Option<std::thread::JoinHandle<Result<FinishedBuild, PortError>>>,
    pub qualified: Option<crate::native_compiler::Qualified>,
    cancel: Arc<std::sync::atomic::AtomicBool>,
    identity: Option<(String, String)>,
    native_program: crate::native_compiler::ProgramSelection,
}
impl<H: ChainHost> Compiler<H> {
    pub fn new(core: H::Weak, observation: Observation<H>) -> Self {
        Self::observing(core, Box::new(observation))
    }
    /// The compiler over another observe port's context (see [`ObservedSource`]).
    pub fn observing(core: H::Weak, observation: Box<dyn ObservedSource>) -> Self {
        Self {
            core,
            observation,
            prepared: None,
            metadata: None,
            failure: None,
            artifact: None,
            retired: false,
            worker: None,
            qualified: None,
            cancel: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            identity: None,
            native_program: crate::native_compiler::ProgramSelection::Environment,
        }
    }
    /// Packaged composition supplies the identity captured by startup admission.
    /// None disables the producer explicitly. It never enables environment lookup.
    /// Admission errors remain visible through the ordinary compile port.
    pub fn with_native_program(
        mut self,
        program: Option<crate::native_compiler::NativeProgram>,
    ) -> Self {
        let admitted = program
            .map(|program| program.admit(&|| self.cancel.load(std::sync::atomic::Ordering::SeqCst)))
            .transpose();
        self.native_program = match admitted {
            Ok(program) => crate::native_compiler::ProgramSelection::Explicit(program),
            Err(error) => {
                self.failure = Some(failed(error.0));
                crate::native_compiler::ProgramSelection::Explicit(None)
            }
        };
        self
    }
    /// A completed, non-retired compilation of exactly this run. The caller borrows the
    /// original source-bound input; no observation descriptors or synthetic Qualified values.
    pub fn completed_input(
        &self,
        run: &str,
        deployment: &str,
    ) -> Result<&crate::compiler_inputs::InputArtifact, PortError> {
        if self.retired || self.cancel.load(std::sync::atomic::Ordering::SeqCst) {
            return Err(failed("compiler_stopped"));
        }
        if self
            .identity
            .as_ref()
            .map(|(r, d)| (r.as_str(), d.as_str()))
            != Some((run, deployment))
        {
            return Err(failed("compiler_run_changed"));
        }
        if self.worker.is_some() || self.failure.is_some() || self.qualified.is_none() {
            return Err(failed("compiler_not_completed"));
        }
        let artifact = self
            .artifact
            .as_ref()
            .ok_or_else(|| failed("compiler_not_completed"))?;
        artifact.validate()?;
        Ok(artifact)
    }
    fn host(&self) -> Result<H, PortError> {
        H::upgrade(&self.core).ok_or_else(|| failed("compiler_stopped"))
    }
    // demo_chain.py:794-798: no prepared-source IO or start_compile refusal
    // is permitted before member records and the compile announcement.
    fn metadata(&self) -> Result<CompileContext, PortError> {
        let core = self.host()?;
        let (root, seed, local_node) = view(&core, |c| {
            (
                c.root.clone(),
                c.seed.clone(),
                c.run.contributor.as_ref().map(|m| m.node.clone()),
            )
        })?;
        let mut facts = None;
        self.observation.with_context(&mut |context| {
            facts = context.map(|c| c.compiler_facts.clone());
        })?;
        let facts = facts.ok_or_else(|| failed("two_distinct_hosts_required"))?;
        let local = local_device(&root, seed.as_deref(), local_node.as_deref())?;
        let joined_reports = match seed {
            Some(seed) => json!(*lock(&seed.capability_reports)?),
            None => json!({}),
        };
        Ok(CompileContext {
            facts,
            local,
            joined_reports,
            now: now(),
        })
    }
    /// Source consent/observation ladder precedes any local source reads.
    fn acquire(&mut self) -> Result<CompileInputs, PortError> {
        self.acquire_until(&|| false)
    }
    /// [`Self::acquire`] whose source manifest re-hash consults the run's Stop
    /// once per chunk (`compiler_stopped`). The acquisition's own snapshot
    /// verification inside `prepared_source` is S1-owned and still unchecked.
    fn acquire_until(&mut self, stopped: &dyn Fn() -> bool) -> Result<CompileInputs, PortError> {
        let core = self.host()?;
        let (acquisition, seed) = view(&core, |c| {
            if c.target || !c.running {
                return Err(failed("model_preparation_consent_required"));
            }
            Ok((c.acquisition.clone(), c.seed.clone()))
        })??;
        let (run, deployment, members, cards, facts, observations) = {
            let mut read = Err(failed("two_distinct_hosts_required"));
            self.observation.with_context(&mut |context| {
                read = (|| {
                    let context = context.ok_or_else(|| failed("two_distinct_hosts_required"))?;
                    let document = context
                        .observations
                        .as_ref()
                        .ok_or_else(|| failed("two_distinct_hosts_required"))?;
                    crate::compiler_inputs::validate_hosts(
                        &document.to_value(),
                        context.facts.len(),
                    )?;
                    Ok((
                        context.run_id.clone(),
                        context.deployment_id.clone(),
                        context.members.clone(),
                        context.descriptors.clone(),
                        context.compiler_facts.clone(),
                        document.clone(),
                    ))
                })();
            })?;
            read?
        };
        if self.retired && self.identity.as_ref() != Some(&(run.clone(), deployment.clone())) {
            self.prepared = None;
            self.artifact = None;
            self.qualified = None;
            self.failure = None;
            self.cancel = Arc::new(std::sync::atomic::AtomicBool::new(false));
            self.retired = false;
        }
        if self.retired {
            return Err(failed("compiler_stopped"));
        }
        if self.prepared.is_some()
            || self.artifact.is_some()
            || self.failure.is_some()
            || self.worker.is_some()
        {
            return Err(failed("compiler_already_started"));
        }
        if core.resource_check().is_some() {
            return Err(failed("compiler_resource_policy_denied"));
        }
        let acquisition = acquisition.ok_or_else(|| failed("source_not_prepared"))?;
        let (source_root, prior) = acquisition
            .prepared_source()
            .map_err(|e| failed(e.code()))?;
        // The actual compiler re-hashes local source bytes; never manufacture a
        // manifest from the retained digest or an installation Source enum.
        const STOPPED: &str = "compiler_stopped";
        if stopped() {
            return Err(failed(STOPPED));
        }
        let source =
            crate::serving_source::prepare_local_source(&source_root, acquisition.model(), &|s| {
                crate::local_manifest::compile_local_model_manifest_checked(
                    &crate::local_manifest::LocalModelSource {
                        root: s.root.clone(),
                        model_id: s.model_id.into(),
                        requested_revision: s.requested_revision.into(),
                        resolved_commit: s.resolved_commit.into(),
                        tokenizer_assets: Ok(Vec::new()),
                    },
                    &|| {
                        if stopped() {
                            Err(crate::local_manifest::CompileError(STOPPED.into()))
                        } else {
                            Ok(())
                        }
                    },
                )
                .map_err(|e| {
                    if e.0 == STOPPED {
                        crate::serving_source::Failure::Serving(STOPPED.into())
                    } else {
                        crate::serving_source::Failure::Other
                    }
                })
            })
            .map_err(|e| match e {
                crate::serving_source::Failure::Serving(code) => failed(&code),
                crate::serving_source::Failure::Other => failed("native_manifest_compile_failed"),
            })?;
        if source.manifest_digest != prior.manifest_digest {
            return Err(failed("compiler_model_changed"));
        }
        let seed = seed.ok_or_else(|| failed("compiler_member_changed"))?;
        let mut current_members = BTreeMap::new();
        let mut keys = Vec::new();
        for (member, card) in members.iter().zip(&cards) {
            let node = member["node_id"]
                .as_str()
                .ok_or_else(|| failed("compiler_member_changed"))?;
            let current = seed
                .coordinator
                .member(node)
                .map_err(|_| failed("compiler_member_changed"))?;
            if current["lease_expires_at"]
                .as_f64()
                .is_none_or(|end| now() >= end)
                || [
                    "generation",
                    "incarnation",
                    "verification_key_digest",
                    "endpoint_id",
                ]
                .iter()
                .any(|k| current[*k] != member[*k])
            {
                return Err(failed("compiler_member_changed"));
            }
            let verified = facts
                .get(node)
                .ok_or_else(|| failed("compiler_member_changed"))?;
            if verified["manifest_digest"] != source.manifest_digest {
                return Err(failed("compiler_model_changed"));
            }
            keys.push(card["verification_key"].clone());
            current_members.insert(node.to_owned(), current);
        }
        if cards.len() != members.len() {
            return Err(failed("compiler_member_changed"));
        }
        let metadata = match self.metadata.take() {
            Some(metadata) => metadata,
            None => self.metadata()?,
        };
        let model = acquisition.model();
        let inputs = CompileInputs {
            facts,
            observation_hosts: Some(
                observations
                    .observations
                    .iter()
                    .map(|r| r.host_id.clone())
                    .collect(),
            ),
            local: metadata.local,
            joined_reports: metadata.joined_reports,
            now: now(),
            owner_ready: true,
            already_started: false,
            resource_denied: false,
            model: model_contract(model),
            manifest_digest: source.manifest_digest.clone(),
            manifest: Value::Object(source.manifest.clone()),
            source_bytes: Some(source.source_bytes),
            context_members: members,
            current_members,
            keys: json!({"gossip":[seed.coordinator.signer().public_key_record()], "load_proof":keys}),
            epoch: 0,
        };
        self.retain_prepared_source(source, &inputs, &observations)?;
        Ok(inputs)
    }
    /// Retain the admitted source/observation boundary before starting byte work.
    /// This is not consent admission; acquire performs that ladder first.
    pub fn retain_prepared_source(
        &mut self,
        source: crate::serving_source::Prepared,
        inputs: &CompileInputs,
        observations: &runtime::observe::ObservationsDocument,
    ) -> Result<(), PortError> {
        if self.retired
            || self.prepared.is_some()
            || self.worker.is_some()
            || self.artifact.is_some()
            || self.failure.is_some()
        {
            return Err(failed("compiler_already_started"));
        }
        let document = observations.to_value();
        crate::compiler_inputs::validate_hosts(&document, inputs.facts.len())?;
        let nodes: std::collections::BTreeSet<_> = observations
            .observations
            .iter()
            .map(|row| row.node_id.as_str())
            .collect();
        if nodes != inputs.facts.keys().map(String::as_str).collect()
            || inputs.manifest_digest != source.manifest_digest
            || inputs.manifest != Value::Object(source.manifest.clone())
        {
            return Err(failed("compiler_model_changed"));
        }
        let core = self.host()?;
        let root = view(&core, |c| c.root.clone())?;
        let topology = crate::compiler_inputs::topology(&inputs.facts)?;
        self.identity = Some((
            observations.run_id.clone(),
            observations.deployment_id.clone(),
        ));
        self.prepared = Some(PreparedBuild {
            source,
            inputs: inputs.clone(),
            run: observations.run_id.clone(),
            deployment: observations.deployment_id.clone(),
            observations: document,
            topology,
            root,
        });
        Ok(())
    }
    /// Signal the build worker's Stop (its scans check it once per chunk)
    /// without joining it; [`Self::teardown`] does this first.
    pub fn signal_stop(&self) {
        self.cancel.store(true, std::sync::atomic::Ordering::SeqCst);
    }
    pub fn teardown(&mut self) -> Result<(), String> {
        self.retired = true;
        self.signal_stop();
        if let Some(worker) = self.worker.take() {
            match worker.join() {
                Ok(Ok((artifact, _))) => self.artifact = Some(artifact),
                Ok(Err(error)) => self.failure = Some(error),
                Err(_) => return Err("compiler_worker_panicked".into()),
            }
        }
        // Input artifacts are inert durable compiler evidence, not worker custody.
        // Never delete them (or a foreign replacement) during Stop.
        self.prepared = None;
        self.metadata = None;
        self.qualified = None;
        if self.identity.is_some() {
            self.failure = Some(failed("compiler_stopped"));
        }
        Ok(())
    }
}
impl<H: ChainHost> CompilePort for Compiler<H> {
    fn compile_context(&mut self) -> Result<CompileContext, PortError> {
        let context = self.metadata()?;
        self.metadata = Some(context.clone());
        Ok(context)
    }
    fn compile_inputs(&mut self) -> Result<CompileInputs, PortError> {
        self.acquire()
    }
    fn compile_inputs_until(
        &mut self,
        stopped: &dyn Fn() -> bool,
    ) -> Result<CompileInputs, PortError> {
        self.acquire_until(stopped)
    }
    fn retire_bootstrap(&mut self) -> Result<(), PortError> {
        self.observation.retire_bootstrap()
    }
    fn start_build(&mut self, request: &BuildRequest) {
        if self.retired
            || self.worker.is_some()
            || self.artifact.is_some()
            || self.failure.is_some()
        {
            self.failure = Some(failed("compiler_stopped"));
            return;
        }
        let Some(prepared) = self.prepared.take() else {
            self.failure = Some(failed("compiler_source_unavailable"));
            return;
        };
        let core = self.core.clone();
        let cancel = self.cancel.clone();
        let request = request.clone();
        let native_program = self.native_program.clone();
        let worker = std::thread::Builder::new()
            .name("chain-compiler-inputs".into())
            .spawn(move || {
                let check = || -> Result<BTreeMap<String, Value>, PortError> {
                    if cancel.load(std::sync::atomic::Ordering::SeqCst) {
                        return Err(failed("compiler_stopped"));
                    }
                    let core = H::upgrade(&core).ok_or_else(|| failed("compiler_stopped"))?;
                    let seed = view(&core, |c| {
                        if c.closed || !c.running || c.target {
                            return Err(failed("compiler_stopped"));
                        }
                        c.seed
                            .clone()
                            .ok_or_else(|| failed("compiler_member_changed"))
                    })??;
                    let mut current = BTreeMap::new();
                    for node in &request.node_ids {
                        let member = seed
                            .coordinator
                            .member(node)
                            .map_err(|_| failed("compiler_member_changed"))?;
                        current.insert(node.clone(), member);
                    }
                    Ok(current)
                };
                check()?;
                let mut artifact = crate::compiler_inputs::emit(
                    &prepared.root,
                    &prepared.run,
                    &prepared.deployment,
                    prepared.inputs.epoch,
                    &request,
                    &prepared.source,
                    &prepared.topology,
                    &prepared.observations,
                    &prepared.inputs.keys,
                    &prepared.inputs.context_members,
                )?;
                artifact.bind_cancellation(cancel.clone())?;
                if request.stage_sharded {
                    // The producer already independently recompiles and reopens
                    // its recipes before returning. Do not immediately repeat
                    // the model-sized scan with no intervening asynchronous work.
                    artifact.produce_stage_shards(&check()?, now())?;
                    artifact.produce_assignments(&check()?, now())?;
                } else {
                    artifact.produce_assignments(&check()?, now())?;
                    artifact.validate_assignments(&check()?, now())?;
                }
                artifact.validate_members(&check()?, now())?;
                let seed = view(
                    &H::upgrade(&core).ok_or_else(|| failed("compiler_stopped"))?,
                    |c| c.seed.clone(),
                )?
                .ok_or_else(|| failed("compiler_member_changed"))?;
                let current = || {
                    check().map_err(|e| {
                        crate::native_compiler::Error(
                            if e.code_or("compiler_member_changed") == "compiler_stopped" {
                                "compiler_stopped"
                            } else {
                                "compiler_member_changed"
                            },
                        )
                    })
                };
                let qualified = crate::native_compiler::build_selected(
                    &artifact,
                    seed.coordinator.signer(),
                    &current,
                    cancel.clone(),
                    &native_program,
                )
                .map_err(|e| failed(e.0))?;
                artifact.validate_members(&check()?, now())?;
                Ok((artifact, qualified))
            });
        match worker {
            Ok(worker) => self.worker = Some(worker),
            Err(_) => self.failure = Some(failed("compiler_worker_start_failed")),
        }
    }
    fn poll_build(&mut self) -> BuildPoll {
        if self.worker.as_ref().is_some_and(|w| !w.is_finished()) {
            return BuildPoll::Pending;
        }
        if let Some(worker) = self.worker.take() {
            match worker.join() {
                Ok(Ok((artifact, qualified))) => {
                    self.artifact = Some(artifact);
                    self.qualified = qualified;
                    if self.qualified.is_none() {
                        self.failure = Some(failed("native_challenge_producer_unavailable"));
                    }
                }
                Ok(Err(error)) => self.failure = Some(error),
                Err(_) => self.failure = Some(failed("compiler_worker_panicked")),
            }
        }
        if self.failure.is_none()
            && let Some(qualified) = &self.qualified
        {
            return BuildPoll::Done(Ok(qualified.output.clone()));
        }
        BuildPoll::Done(Err(self
            .failure
            .clone()
            .unwrap_or_else(|| failed("compiler_not_started"))))
    }
}

impl<H: ChainHost> Drop for Compiler<H> {
    fn drop(&mut self) {
        if let Err(error) = self.teardown() {
            eprintln!("compiler cleanup unproven: {error}");
        }
    }
}

pub struct SharedCompiler<H: ChainHost>(pub Arc<Mutex<Compiler<H>>>);
impl<H: ChainHost> CompilePort for SharedCompiler<H> {
    fn compile_context(&mut self) -> Result<CompileContext, PortError> {
        lock(&self.0)?.compile_context()
    }
    fn compile_inputs(&mut self) -> Result<CompileInputs, PortError> {
        lock(&self.0)?.compile_inputs()
    }
    fn compile_inputs_until(
        &mut self,
        stopped: &dyn Fn() -> bool,
    ) -> Result<CompileInputs, PortError> {
        lock(&self.0)?.compile_inputs_until(stopped)
    }
    fn retire_bootstrap(&mut self) -> Result<(), PortError> {
        lock(&self.0)?.retire_bootstrap()
    }
    fn start_build(&mut self, request: &BuildRequest) {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .start_build(request);
    }
    fn poll_build(&mut self) -> BuildPoll {
        match lock(&self.0) {
            Ok(mut compiler) => compiler.poll_build(),
            Err(error) => BuildPoll::Done(Err(error)),
        }
    }
}

pub fn status<H: ChainHost>(core: &H) -> Json {
    let compiler = core
        .try_with(|c| c.compiler.as_ref().and_then(Weak::upgrade))
        .flatten();
    let compilation = compiler.and_then(|compiler| {
        // Status never waits on source IO or on the owner's action mutex.
        let Ok(mut compiler) = compiler.try_lock() else {
            return None;
        };
        if compiler.worker.is_some() {
            match compiler.poll_build() {
                BuildPoll::Pending => {
                    return Some(json!({"state":"compiling", "route_ready":false}));
                }
                BuildPoll::Done(_) => (),
            }
        }
        compiler
            .failure
            .as_ref()
            .map(|_| planner::chain_compile::failed_state())
    });
    jobj! {"compilation" => compilation.map_or(Json::Null, Json::from), "serving" => Json::Null, "route_ready" => false}
}

/// Public route preserves source preflight and private builtin identity. The
/// complete compiler owner remains the actual OwnerChain, not a detached runner.
pub fn public_compile<H: ChainHost>(
    core: &H,
    body: &Map<String, Value>,
) -> Result<Json, PublicError> {
    let refusal = || failure().raised_by(Raised::Builtin("ValueError"));
    let (compiler, local) = core
        .try_with(|c| {
            if c.target
                || !c.running
                || body.get("model_preparation_acknowledged") != Some(&Value::Bool(true))
            {
                return Err(refusal());
            }
            Ok((
                c.compiler
                    .as_ref()
                    .and_then(Weak::upgrade)
                    .ok_or_else(refusal)?,
                c.run.contributor.as_ref().map(|v| v.node.clone()),
            ))
        })
        .ok_or_else(refusal)??;
    let mut compiler = lock(&compiler).map_err(|_| refusal())?;
    let input = compiler.acquire().map_err(|_| refusal())?;
    let prepared = compiler.prepared.as_ref().ok_or_else(refusal)?;
    let nodes: Vec<_> = input.facts.keys().cloned().collect();
    let local_record = local
        .as_ref()
        .zip(input.local.as_ref())
        .map(|(node, device)| planner::chain_compile::LocalRecord {
            node_id: node,
            memory_bytes: device.memory_bytes,
            available_storage_bytes: device.available_storage_bytes.as_ref(),
            generation: &device.generation,
            grant: device.grant.clone(),
            policy_storage_bytes: device.policy_storage_bytes,
        });
    let records = planner::chain_compile::member_capability_records(
        &nodes,
        local_record.as_ref(),
        &input.joined_reports,
        input.now,
    );
    let policy = planner::chain_compile::ChainPolicy::pinned();
    let plan = planner::chain_compile::prepare(&planner::chain_compile::PrepareInput {
        owner_ready: input.owner_ready,
        observation_hosts: input.observation_hosts.as_deref(),
        facts: &input.facts,
        already_started: input.already_started,
        resource_denied: input.resource_denied,
        model: &input.model,
        manifest_digest: &input.manifest_digest,
        manifest: &input.manifest,
        source_bytes: input.source_bytes,
        context_members: &input.context_members,
        current_members: &input.current_members,
        records: Some(&records),
        owner_node_id: local.as_deref(),
        slice_staging: false,
        slice_mode: false,
        keys: &input.keys,
        deployment_id: &prepared.deployment,
        epoch: input.epoch,
        now: input.now,
        policy: &policy,
    })
    .map_err(|_| refusal())?;
    compiler.retire_bootstrap().map_err(|_| refusal())?;
    compiler.start_build(&plan.request);
    Ok(jobj! {"state" => "compiling", "route_ready" => false})
}
