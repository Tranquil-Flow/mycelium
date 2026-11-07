// SPDX-License-Identifier: AGPL-3.0-only
//! Source-bound compiler front end. No operator plan, native proof or Launch is
//! fabricated when the native assignment/challenge producer is unavailable.
use super::*;
use owner_run::chain::PortError;
use owner_run::steps::compile::{
    BuildPoll, CompileContext, CompileInputs, CompilePort, LocalDevice,
};
use owner_run::steps::observe::ObservePort;
use planner::chain_compile::{BuildRequest, GrantCaps, ModelContract};
use std::sync::{MutexGuard, Weak};

fn failed(code: &str) -> PortError {
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

fn local_device(
    root: &Path,
    seed: Option<&SeedRuntime>,
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
pub(super) struct Observation(pub Arc<Mutex<observation::Adapter>>);
impl ObservePort for Observation {
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

/// Private material retained across the actual planner/worker boundary. This is
/// compiler INPUT, not the missing compiled operator/Controller/Launch output.
struct PreparedBuild {
    source: api::serving_source::Prepared,
    inputs: CompileInputs,
    run: String,
    deployment: String,
    observations: Value,
    topology: Value,
    root: PathBuf,
}

pub(super) struct Compiler {
    core: Weak<Mutex<Core>>,
    observation: Observation,
    prepared: Option<PreparedBuild>,
    metadata: Option<CompileContext>,
    failure: Option<PortError>,
    artifact: Option<api::compiler_inputs::InputArtifact>,
    retired: bool,
    worker: Option<std::thread::JoinHandle<Result<api::compiler_inputs::InputArtifact, PortError>>>,
    cancel: Arc<std::sync::atomic::AtomicBool>,
    identity: Option<(String, String)>,
}
impl Compiler {
    pub(super) fn new(core: Weak<Mutex<Core>>, observation: Observation) -> Self {
        Self {
            core,
            observation,
            prepared: None,
            metadata: None,
            failure: None,
            artifact: None,
            retired: false,
            worker: None,
            cancel: Arc::new(std::sync::atomic::AtomicBool::new(false)),
            identity: None,
        }
    }
    fn host(&self) -> Result<Shared, PortError> {
        self.core
            .upgrade()
            .ok_or_else(|| failed("compiler_stopped"))
    }
    // demo_chain.py:794-798: no prepared-source IO or start_compile refusal
    // is permitted before member records and the compile announcement.
    fn metadata(&self) -> Result<CompileContext, PortError> {
        let core = self.host()?;
        let (root, seed, local_node) = {
            let c = lock(&core)?;
            (
                c.root.clone(),
                c.seed.clone(),
                c.contributor.as_ref().map(|m| m.node.clone()),
            )
        };
        let facts = lock(&self.observation.0)?
            .context()
            .ok_or_else(|| failed("two_distinct_hosts_required"))?
            .compiler_facts
            .clone();
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
        let core = self.host()?;
        let (root, acquisition, seed) = {
            let c = lock(&core)?;
            if c.target || !c.running {
                return Err(failed("model_preparation_consent_required"));
            }
            (c.root.clone(), c.acquisition.clone(), c.seed.clone())
        };
        let (run, deployment, members, cards, facts, observations) = {
            let observation = lock(&self.observation.0)?;
            let context = observation
                .context()
                .ok_or_else(|| failed("two_distinct_hosts_required"))?;
            let document = context
                .observations
                .as_ref()
                .ok_or_else(|| failed("two_distinct_hosts_required"))?;
            api::compiler_inputs::validate_hosts(&document.to_value(), context.facts.len())?;
            (
                context.run_id.clone(),
                context.deployment_id.clone(),
                context.members.clone(),
                context.descriptors.clone(),
                context.compiler_facts.clone(),
                document.to_value(),
            )
        };
        if self.retired && self.identity.as_ref() != Some(&(run.clone(), deployment.clone())) {
            self.prepared = None;
            self.artifact = None;
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
        if resource_check(&core).is_some() {
            return Err(failed("compiler_resource_policy_denied"));
        }
        let acquisition = acquisition.ok_or_else(|| failed("source_not_prepared"))?;
        let (source_root, prior) = acquisition
            .prepared_source()
            .map_err(|e| failed(e.code()))?;
        // The actual compiler re-hashes local source bytes; never manufacture a
        // manifest from the retained digest or an installation Source enum.
        let source =
            api::serving_source::prepare_local_source(&source_root, acquisition.model(), &|s| {
                api::local_manifest::compile_local_model_manifest(
                    &api::local_manifest::LocalModelSource {
                        root: s.root.clone(),
                        model_id: s.model_id.into(),
                        requested_revision: s.requested_revision.into(),
                        resolved_commit: s.resolved_commit.into(),
                        tokenizer_assets: Ok(Vec::new()),
                    },
                )
                .map_err(|_| api::serving_source::Failure::Other)
            })
            .map_err(|e| match e {
                api::serving_source::Failure::Serving(code) => failed(&code),
                api::serving_source::Failure::Other => failed("native_manifest_compile_failed"),
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
                observations["observations"]
                    .as_array()
                    .ok_or_else(|| failed("two_distinct_hosts_required"))?
                    .iter()
                    .map(|r| r["host_id"].as_str().unwrap_or_default().to_owned())
                    .collect(),
            ),
            local: metadata.local,
            joined_reports: metadata.joined_reports,
            now: now(),
            owner_ready: true,
            already_started: false,
            resource_denied: false,
            model: ModelContract {
                model_id: model.model_id.into(),
                resolved_commit: model.resolved_commit.into(),
                source_representation: "bfloat16-safetensors".into(),
                runtime_dtype: "float32".into(),
                runtime_quantization: "int8-weight-only".into(),
            },
            manifest_digest: source.manifest_digest.clone(),
            manifest: Value::Object(source.manifest.clone()),
            source_bytes: Some(source.source_bytes),
            context_members: members,
            current_members,
            keys: json!({"gossip":[seed.coordinator.signer().public_key_record()], "load_proof":keys}),
            epoch: 0,
        };
        let topology = api::compiler_inputs::topology(&inputs.facts)?;
        self.identity = Some((run.clone(), deployment.clone()));
        self.prepared = Some(PreparedBuild {
            source,
            inputs: inputs.clone(),
            run,
            deployment,
            observations,
            topology,
            root,
        });
        Ok(inputs)
    }
    pub(super) fn teardown(&mut self) -> Result<(), String> {
        self.retired = true;
        self.cancel.store(true, std::sync::atomic::Ordering::SeqCst);
        if let Some(worker) = self.worker.take() {
            match worker.join() {
                Ok(Ok(artifact)) => self.artifact = Some(artifact),
                Ok(Err(error)) => self.failure = Some(error),
                Err(_) => return Err("compiler_worker_panicked".into()),
            }
        }
        // Input artifacts are inert durable compiler evidence, not worker custody.
        // Never delete them (or a foreign replacement) during Stop.
        self.prepared = None;
        self.metadata = None;
        if self.identity.is_some() {
            self.failure = Some(failed("compiler_stopped"));
        }
        Ok(())
    }
}
impl CompilePort for Compiler {
    fn compile_context(&mut self) -> Result<CompileContext, PortError> {
        let context = self.metadata()?;
        self.metadata = Some(context.clone());
        Ok(context)
    }
    fn compile_inputs(&mut self) -> Result<CompileInputs, PortError> {
        self.acquire()
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
        let worker = std::thread::Builder::new()
            .name("chain-compiler-inputs".into())
            .spawn(move || {
                let check = || -> Result<(), PortError> {
                    if cancel.load(std::sync::atomic::Ordering::SeqCst) {
                        return Err(failed("compiler_stopped"));
                    }
                    let core = core.upgrade().ok_or_else(|| failed("compiler_stopped"))?;
                    let c = lock(&core)?;
                    if c.closed || !c.running {
                        return Err(failed("compiler_stopped"));
                    }
                    Ok(())
                };
                check()?;
                let artifact = api::compiler_inputs::emit(
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
                check()?;
                Ok(artifact)
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
                Ok(Ok(artifact)) => {
                    self.artifact = Some(artifact);
                    self.failure = Some(failed("native_assignment_producer_unavailable"));
                }
                Ok(Err(error)) => self.failure = Some(error),
                Err(_) => self.failure = Some(failed("compiler_worker_panicked")),
            }
        }
        BuildPoll::Done(Err(self
            .failure
            .clone()
            .unwrap_or_else(|| failed("compiler_not_started"))))
    }
}

impl Drop for Compiler {
    fn drop(&mut self) {
        if let Err(error) = self.teardown() {
            eprintln!("compiler cleanup unproven: {error}");
        }
    }
}

pub(super) struct SharedCompiler(pub Arc<Mutex<Compiler>>);
impl CompilePort for SharedCompiler {
    fn compile_context(&mut self) -> Result<CompileContext, PortError> {
        lock(&self.0)?.compile_context()
    }
    fn compile_inputs(&mut self) -> Result<CompileInputs, PortError> {
        lock(&self.0)?.compile_inputs()
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

pub(super) fn status(core: &Shared) -> Json {
    let compiler = core
        .lock()
        .ok()
        .and_then(|c| c.compiler.as_ref().and_then(Weak::upgrade));
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
pub(super) fn public_compile(
    core: &Shared,
    body: &Map<String, Value>,
) -> Result<Json, PublicError> {
    let refusal = || failure().raised_by(Raised::Builtin("ValueError"));
    let (compiler, local) = {
        let c = core.lock().map_err(|_| refusal())?;
        if c.target
            || !c.running
            || body.get("model_preparation_acknowledged") != Some(&Value::Bool(true))
        {
            return Err(refusal());
        }
        (
            c.compiler
                .as_ref()
                .and_then(Weak::upgrade)
                .ok_or_else(refusal)?,
            c.contributor.as_ref().map(|v| v.node.clone()),
        )
    };
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
