// SPDX-License-Identifier: AGPL-3.0-only
//! `_step_compile` (`demo_chain.py:792-850 @ f4e366e9`): compile the route over the observed Macs. The
//! planner (`planner::chain_compile`, SE-L7-CHAIN-COMPILE) owns the capability records, the preparation
//! refusals, the split, the resource plan and the compile state; this step owns the order of the calls,
//! the wait (`chain_compile_timeout` after the `Compile` budget), the ceiling comparator and what is left
//! in the run context. The route builder and the serving workflow (L8/L9) stay behind [`CompilePort`].
use super::exchange::{ReportsPort, announce, publish_participants};
use crate::chain::{PortError, StepEnv, StepError, wait_with};
use crate::machine::OwnerRunError;
use crate::state::RunState;
use budgets::{BudgetId, Entries};
use planner::chain_compile::{
    BuildOutput, BuildRequest, ChainPolicy, GrantCaps, ModelContract, PrepareInput, Refusal, admit,
    compile_progress, failed_state, member_capability_records, prepare,
};
use serde_json::Value;
use std::collections::BTreeMap;

/// The owner's own Mac, freshly observed (`local_device`) plus its consent files.
#[derive(Clone, Debug)]
pub struct LocalDevice {
    pub memory_bytes: u64,
    /// `local_device['available_storage_bytes']` (kept by the planner only when a JSON integer).
    pub available_storage_bytes: Option<Value>,
    /// `coordinator.member(local)['generation']`.
    pub generation: Value,
    /// `standing_consent.load_standing_consent(root)` caps.
    pub grant: Option<GrantCaps>,
    /// `read_resource_policy(root)['storage_bytes']` when an integer.
    pub policy_storage_bytes: Option<u64>,
}

/// Everything `_step_compile` and `start_compile` read from the app before the compile thread starts.
#[derive(Clone, Debug)]
pub struct CompileInputs {
    /// `context.facts` (node -> native hello facts); its keys are the fleet.
    pub facts: BTreeMap<String, Value>,
    /// `[o['host_id'] ...]` of the observation; `None` without one.
    pub observation_hosts: Option<Vec<String>>,
    pub local: Option<LocalDevice>,
    /// `app.member_capability_reports()`: node -> verified signed-report content.
    pub joined_reports: Value,
    /// `time.time()`.
    pub now: f64,
    pub owner_ready: bool,
    pub already_started: bool,
    pub resource_denied: bool,
    pub model: ModelContract,
    pub manifest_digest: String,
    pub manifest: Value,
    pub source_bytes: Option<u64>,
    /// `context.members` and the coordinator's current record per member (`lease_expires_at` included).
    pub context_members: Vec<Value>,
    pub current_members: BTreeMap<String, Value>,
    /// `{'gossip': [...], 'load_proof': [...]}`.
    pub keys: Value,
    pub epoch: u64,
}

/// What the route builder thread has produced.
#[derive(Clone, Debug)]
pub enum BuildPoll {
    /// The thread has not finished (`app._chain_compile_state` still `compiling`).
    Pending,
    Done(Result<BuildOutput, PortError>),
}

/// The host's side of the compile step.
#[derive(Clone, Debug)]
pub struct CompileContext {
    pub facts: BTreeMap<String, Value>,
    pub local: Option<LocalDevice>,
    pub joined_reports: Value,
    pub now: f64,
}

/// The host's side of the compile step.
pub trait CompilePort {
    /// Metadata read by `_step_compile` BEFORE its announcement. Source IO and
    /// `start_compile` refusals occur only in `compile_inputs`, after it.
    fn compile_context(&mut self) -> Result<CompileContext, PortError>;
    /// Everything `start_compile` reads from the app.
    fn compile_inputs(&mut self) -> Result<CompileInputs, PortError>;
    /// [`Self::compile_inputs`] for a host whose source reads can observe the
    /// owner's Stop (`stopped()`): a long preflight then returns early instead
    /// of holding the run thread. Any error returned while `stopped()` holds
    /// ends the step Stopped. The default keeps the uninterruptible read.
    fn compile_inputs_until(
        &mut self,
        _stopped: &dyn Fn() -> bool,
    ) -> Result<CompileInputs, PortError> {
        self.compile_inputs()
    }
    /// `context.retire_bootstrap()`, after the preparation succeeded.
    fn retire_bootstrap(&mut self) -> Result<(), PortError>;
    /// Start the route builder (L8 `build_qwen_live_route.build` + L9 `prepare_serving_workflow`) on a worker
    /// thread; returns at once.
    fn start_build(&mut self, request: &BuildRequest);
    /// The builder thread's outcome so far (read once per poll of the step's wait).
    fn poll_build(&mut self) -> BuildPoll;
}

fn refused(r: Refusal) -> StepError {
    OwnerRunError::with(&r.code, r.message.as_deref(), None).into()
}

fn secs(id: BudgetId) -> f64 {
    id.span(Entries::ZERO)
        .map_or(f64::NAN, |s| s.millis() as f64 / 1000.0)
}

/// Runs the step. On success the run state carries `proposal`, `worker_wall_limits`,
/// `wall_cap_fallback_nodes`, `compile_state` and `layers`, and the result carries
/// `compilation_cache_hit` and `build_seconds`.
pub fn run_compile_step(
    env: &mut dyn StepEnv,
    reports: &mut dyn ReportsPort,
    compile: &mut dyn CompilePort,
    state: &mut RunState,
    interval: f64,
) -> Result<(), StepError> {
    let context = match compile.compile_context() {
        Ok(context) => context,
        Err(error) => {
            // `code_or` falls back to a generic code when the port's error carries no closed code, which
            // is exactly what happened here: the step reported `chain_compile_failed` and hid the reason.
            eprintln!("L11_COMPILE_CONTEXT_REFUSED:{error:?}");
            return Err(error.into_step_error("chain_compile_failed"));
        }
    };
    let nodes: Vec<String> = context.facts.keys().cloned().collect();
    let local = state.local_node.clone();
    let slice_mode = state.model.as_ref().is_some_and(|m| m.slice_mode);
    let local_record = match (&local, &context.local) {
        (Some(node), Some(device)) => Some(planner::chain_compile::LocalRecord {
            node_id: node,
            memory_bytes: device.memory_bytes,
            available_storage_bytes: device.available_storage_bytes.as_ref(),
            generation: &device.generation,
            grant: device.grant.clone(),
            policy_storage_bytes: device.policy_storage_bytes,
        }),
        _ => None,
    };
    let records = member_capability_records(
        &nodes,
        local_record.as_ref(),
        &context.joined_reports,
        context.now,
    );
    state.member_records = Some(records.clone());
    announce(reports, state, "compile", None)?;
    // The source preflight observes the owner's Stop; an input read that
    // returns because of it ends the step Stopped, as the build wait does.
    let inputs = compile
        .compile_inputs_until(&|| env.stopped())
        .map_err(|e| {
            if env.stopped() {
                StepError::Stopped
            } else {
                e.into_step_error("chain_compile_failed")
            }
        })?;
    let policy = ChainPolicy::pinned();
    let prepared = prepare(&PrepareInput {
        owner_ready: inputs.owner_ready,
        observation_hosts: inputs.observation_hosts.as_deref(),
        facts: &inputs.facts,
        already_started: inputs.already_started,
        resource_denied: inputs.resource_denied,
        model: &inputs.model,
        manifest_digest: &inputs.manifest_digest,
        manifest: &inputs.manifest,
        source_bytes: inputs.source_bytes,
        context_members: &inputs.context_members,
        current_members: &inputs.current_members,
        records: Some(&records),
        owner_node_id: local.as_deref(),
        slice_mode,
        keys: &inputs.keys,
        deployment_id: &state.deployment_id,
        epoch: inputs.epoch,
        now: inputs.now,
        policy: &policy,
    })
    .map_err(refused)?;
    // The commit point. Each side effect is preceded directly by a Stop check, with no work
    // between them: a Stop observed before `retire_bootstrap` prevents both it and the build, one
    // observed after it (but before `start_build`) prevents the build. A Stop that lands after a
    // check is ordered after that commit; the wait's first check then ends the step Stopped and
    // the teardown cancels what was started. Holding the Stop event's lock across these host
    // calls would not be safer: `Runner::stop` holds the run lock while it sets the event.
    if env.stopped() {
        return Err(StepError::Stopped);
    }
    compile
        .retire_bootstrap()
        .map_err(|e| e.into_step_error("chain_compile_failed"))?;
    env.progress(&compile_progress(nodes.len()));
    if env.stopped() {
        return Err(StepError::Stopped);
    }
    compile.start_build(&prepared.request);

    let compiled: Value = wait_with(
        env,
        |_| {
            reports.tick_local();
            match compile.poll_build() {
                BuildPoll::Pending => Ok(None),
                BuildPoll::Done(Ok(out)) => Ok(Some(prepared.finish(&out))),
                BuildPoll::Done(Err(error)) => {
                    // `state.get('state') == 'failed'` -> OwnerRunError(error_code). The port's own
                    // error is the evidence: reporting the constant `failed_state()` code here hid a
                    // real build refusal behind "chain compile failed" with nothing logged.
                    eprintln!("L11_CHAIN_BUILD_REFUSED:{error:?}");
                    let failed = failed_state();
                    let code = error.code_or(
                        failed["error_code"]
                            .as_str()
                            .unwrap_or("chain_compile_failed"),
                    );
                    Err(OwnerRunError::new(&code).into())
                }
            }
        },
        secs(BudgetId::Compile),
        "chain_compile_timeout",
        interval,
    )?;
    state.chain_compile_state = Some(compiled.clone());
    let admission = admit(
        &compiled,
        &records,
        &nodes,
        local.as_deref(),
        state.execution_ceiling.as_ref(),
        &crate::ceiling::proposal_within,
    );
    let admission = match admission {
        Ok(a) => a,
        Err(r) => {
            if r.code == "member_wall_limit_invalid" {
                // `ctx.state['proposal']` was stored before the wall limits were read.
                state.proposal = Some(compiled["execution_proposal"].clone());
            }
            return Err(refused(r));
        }
    };
    state.proposal = Some(admission.proposal);
    state.worker_wall_limits = admission.worker_wall_limits;
    state.wall_cap_fallback_nodes = admission.wall_cap_fallback_nodes;
    if let Some(line) = admission.progress {
        env.progress(&line);
    }
    state.compile_state = Some(admission.compile_state);
    env.set_result(
        "compilation_cache_hit",
        admission.compilation_cache_hit.into(),
    );
    env.set_result("build_seconds", admission.build_seconds);
    state.layers = admission.layers;
    publish_participants(env, reports, state)?;
    Ok(())
}
