// SPDX-License-Identifier: AGPL-3.0-only
//! `_step_model` (`demo_chain.py:442-507 @ f4e366e9`): this Mac's pinned model is prepared. The first
//! step of every run and the one an owner meets first (`owner_model_not_prepared`).
//!
//! Collaborator calls happen in the oracle's order and number (the executed trace records them): the
//! acquisition state is read three times before a prepared model is accepted, once more inside the
//! wait, and so on. A port that logs its calls therefore reproduces the oracle's log exactly.
use crate::chain::{StepEnv, StepError, wait_until};
use crate::machine::OwnerRunError;
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value};

/// `MODEL_NOT_PREPARED_MESSAGE` (`demo_chain.py:132-133`).
pub const NOT_PREPARED_MESSAGE: &str = "Download the selected model on this Mac first: under Inference, tick the authorization box, press “Download / resume source” and wait for “prepared”, then press Start again.";
/// `ACQUISITION_ACTIVE_STATES` (`demo_chain.py:130-131`): a download is really running.
pub const ACQUISITION_ACTIVE_STATES: [&str; 6] = [
    "starting",
    "resolving_metadata",
    "checking_capacity",
    "downloading",
    "verifying",
    "compiling",
];
const SLICE_ARCHITECTURES: [&str; 1] = ["qwen3"];
/// `SLICE_FETCH_MIN_SOURCE_BYTES` (`serving.py:56`): not a registered budget id yet (seam SE-SLICE1-L1B-SLICE-MIN).
const SLICE_MIN_SOURCE_BYTES: u64 = 4_000_000_000;

/// The pinned model a prepared source carries (`ModelContract` plus its registered shape).
#[derive(Clone, Debug, PartialEq)]
pub struct PreparedModel {
    pub model_id: String,
    pub resolved_commit: String,
    pub architecture: String,
    /// `model_shape(model).label`, used in the owner-storage refusal.
    pub label: String,
    /// `choice_for_model(model).name`.
    pub choice: Option<String>,
    /// `model_shape(model) is not None`.
    pub shape_registered: bool,
    /// `owner_storage_requirement(model)`.
    pub owner_storage_need: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PreparedSource {
    pub model: PreparedModel,
    pub manifest_digest: String,
    /// `source.source_bytes`: any JSON value (the oracle accepts only a real `int`).
    pub source_bytes: Value,
}

/// What `reverify_model_snapshot()` did; a failure is swallowed (`except Exception: pass`).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Reverify {
    Done,
    Failed,
}

/// The host's side of the step.
pub trait ModelPort {
    /// Whether this host stages by slice regardless of the source-size window.
    fn slice_staging(&self) -> bool {
        false
    }

    /// `app.installation_role == 'owner' and app.running`.
    fn owner_and_running(&self) -> bool;
    /// `app.authority.status().get('swarm_created')`.
    fn swarm_created(&self) -> bool;
    /// `app._chain_serving_thread is not None`.
    fn has_closing_thread(&self) -> bool;
    /// `app._chain_serving_thread.is_alive()`.
    fn closing_thread_alive(&mut self) -> bool;
    /// `app.acquisition.status().get('state')`.
    fn acquisition_state(&mut self) -> String;
    /// `callable(getattr(app, 'reverify_model_snapshot', None))`.
    fn reverify_available(&self) -> bool;
    /// `app.reverify_model_snapshot()`.
    fn reverify(&mut self) -> Reverify;
    /// `app.acquisition.prepared_source()`; the oracle's call raises `AcquisitionError` when the accepted
    /// snapshot no longer verifies, which fails the step with only that class.
    fn prepared_source(&mut self) -> Result<PreparedSource, StepError>;
    /// `read_resource_policy(root).get('storage_bytes')` (`None` when there is no policy).
    fn storage_consent_cap(&mut self) -> Result<Option<Value>, StepError>;
    /// `_check_rollover_members(binding)` (`demo_chain.py:1117-1123`).
    fn check_rollover_members(&mut self, binding: &Value) -> Result<(), StepError>;
}

/// What the step reads from its run context.
#[derive(Clone, Debug, Default)]
pub struct StepInput {
    /// `ctx.reason == 'rollover'`.
    pub rollover: bool,
    /// `ctx.state.get('rollover_binding')`.
    pub rollover_binding: Option<Value>,
    /// The host stages by slice whatever the source size (the packaged two-Mac flow). The ported
    /// window (`slice_fetch_permitted`) is for 8B-14B sources; a host that has only the slice
    /// stage flavour must slice smaller ones too, or it has no stage at all.
    pub slice_staging: bool,
}

/// What the step leaves in `ctx.state` (and `app._chain_slice_fetch`).
#[derive(Clone, Debug, PartialEq)]
pub struct ModelStepOutput {
    pub prepared: PreparedSource,
    pub slice_mode: bool,
    pub manifest_digest: String,
    pub choice: Option<String>,
    pub model_id: String,
    pub resolved_commit: String,
}

fn secs(id: BudgetId) -> f64 {
    id.span(Entries::ZERO)
        .map_or(f64::NAN, |s| s.millis() as f64 / 1000.0)
}

fn not_prepared() -> StepError {
    let mut fix = Map::new();
    fix.insert("action".into(), "prepare_model".into());
    fix.insert("target".into(), "acquisition".into());
    OwnerRunError::with(
        "owner_model_not_prepared",
        Some(NOT_PREPARED_MESSAGE),
        Some(fix),
    )
    .into()
}

/// `serving.slice_fetch_permitted(source_bytes, architecture=, model=)`.
/// Whether this run stages by slice: the host's own requirement, or the ported source-size window.
/// A host with only the slice stage flavour must slice a small source too (see `StepInput`).
pub fn staging_slices(staging: bool, source_bytes: &Value, model: &PreparedModel) -> bool {
    staging || slice_fetch_permitted(source_bytes, model)
}

pub fn slice_fetch_permitted(source_bytes: &Value, model: &PreparedModel) -> bool {
    if !model.shape_registered {
        return false;
    }
    let max = BudgetId::PyMyceliumA13MacosServingSliceFetchMaxSourceBytesL57
        .bytes()
        .map_or(0, |b| b.get());
    // `type(x) is int`: a JSON bool is not a number here, a float is not an integer
    let Some(bytes) = source_bytes.as_u64() else {
        return false;
    };
    SLICE_ARCHITECTURES.contains(&model.architecture.as_str())
        && SLICE_MIN_SOURCE_BYTES < bytes
        && bytes <= max
}

/// `_gb(value)`: `f"{value / 1e9:.1f} GB"`.
fn gb(value: u64) -> String {
    format!("{:.1} GB", value as f64 / 1e9)
}

pub fn run_model_step(
    env: &mut dyn StepEnv,
    port: &mut dyn ModelPort,
    input: &StepInput,
) -> Result<ModelStepOutput, StepError> {
    if !port.owner_and_running() {
        return Err(OwnerRunError::new("owner_role_required").into());
    }
    if !port.swarm_created() {
        return Err(OwnerRunError::new("owner_swarm_required").into());
    }
    let interval = secs(BudgetId::OwnerPoll);
    if port.has_closing_thread() && port.closing_thread_alive() {
        env.progress("waiting for the previous route to finish closing");
        wait_until(
            env,
            || Ok((!port.closing_thread_alive()).then_some(())),
            secs(BudgetId::PreviousClose),
            "previous_run_still_closing",
            interval,
        )?;
    }
    env.progress("checking the pinned model on this Mac");
    let prepared = |port: &mut dyn ModelPort| port.acquisition_state() == "prepared";
    let downloading = |port: &mut dyn ModelPort| {
        ACQUISITION_ACTIVE_STATES.contains(&port.acquisition_state().as_str())
    };
    // Refuse at once without a prepared model; wait (bounded) only while a download is really running.
    if !prepared(port) && !downloading(port) {
        // After a relaunch the acquisition reads 'closed' although the verified snapshot is on disk:
        // re-hash it (nothing is fetched) rather than make the owner repeat the download.
        // `if callable(reverify)`: the progress line only when there is something to call.
        if port.reverify_available() {
            env.progress("checking the model files already on this Mac");
            match port.reverify() {
                // `except Exception: pass`: a failed re-hash only leaves the model unprepared
                Reverify::Done | Reverify::Failed => {}
            }
        }
    }
    if !prepared(port) && !downloading(port) {
        return Err(not_prepared());
    }
    wait_until(
        env,
        || Ok((prepared(port) || !downloading(port)).then_some(())),
        secs(BudgetId::ModelSelection),
        "owner_model_not_prepared",
        interval.min(secs(BudgetId::PyUpperMyceliumA13MacosDemoChainL475C56)),
    )?;
    if !prepared(port) {
        return Err(not_prepared());
    }
    let source = port.prepared_source()?;
    let model = source.model.clone();
    if input.rollover {
        let binding = input.rollover_binding.as_ref();
        let want = serde_json::json!([
            model.model_id,
            model.resolved_commit,
            source.manifest_digest
        ]);
        match binding {
            Some(b) if b.get("model") == Some(&want) => port.check_rollover_members(b)?,
            _ => return Err(OwnerRunError::new("rollover_model_changed").into()),
        }
    }
    let slice_mode = staging_slices(input.slice_staging, &source.source_bytes, &model);
    check_owner_storage(port, &model)?;
    let head = if slice_mode {
        " · each Mac fetches only its slice"
    } else {
        " · whole snapshot per Mac"
    };
    env.progress(&format!("{}{head}", model.model_id));
    Ok(ModelStepOutput {
        slice_mode,
        manifest_digest: source.manifest_digest.clone(),
        choice: model.choice.clone(),
        model_id: model.model_id.clone(),
        resolved_commit: model.resolved_commit.clone(),
        prepared: source,
    })
}

/// `_check_owner_storage(model)`: the owner's storage consent must hold the selected model here.
fn check_owner_storage(port: &mut dyn ModelPort, model: &PreparedModel) -> Result<(), StepError> {
    if !model.shape_registered {
        return Ok(());
    }
    let cap = port.storage_consent_cap()?;
    // `type(cap) is int and cap < need`
    let cap = match &cap {
        Some(Value::Number(n)) => n
            .as_i64()
            .map(i128::from)
            .or_else(|| n.as_u64().map(i128::from)),
        _ => None,
    };
    let need = model.owner_storage_need;
    if let Some(cap) = cap
        && cap < i128::from(need)
    {
        let message = format!(
            "This Mac's storage limit is {}, but {} needs {} here (its pinned download plus headroom). \
             Choose a larger storage limit or a smaller model.",
            gb_signed(cap),
            model.label,
            gb(need)
        );
        return Err(
            OwnerRunError::with("owner_storage_consent_below_model", Some(&message), None).into(),
        );
    }
    Ok(())
}

fn gb_signed(value: i128) -> String {
    format!("{:.1} GB", value as f64 / 1e9)
}

#[cfg(test)]
mod staging_slices_tests {
    //! The packaged two-Mac flow stages by slice whatever the source size; the ported window is
    //! 4 GB..32 GB for registered qwen3 shapes (`serving.py:54`: "Qwen3 8B..14B slice").
    use super::*;

    fn shape() -> PreparedModel {
        PreparedModel {
            model_id: "Qwen/Qwen3-0.6B".into(),
            resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
            architecture: "qwen3".into(),
            label: "Qwen3-0.6B".into(),
            choice: None,
            shape_registered: true,
            owner_storage_need: 4_000_000_000,
        }
    }

    #[test]
    fn a_host_that_stages_by_slice_slices_a_source_below_the_window() {
        let source = serde_json::json!(1_519_209_243u64);
        assert!(!slice_fetch_permitted(&source, &shape()));
        assert!(staging_slices(true, &source, &shape()));
    }

    #[test]
    fn the_ported_window_still_applies_to_a_host_without_the_requirement() {
        let small = serde_json::json!(1_519_209_243u64);
        let large = serde_json::json!(8_000_000_000u64);
        assert!(!staging_slices(false, &small, &shape()));
        assert!(staging_slices(false, &large, &shape()));
    }
}
