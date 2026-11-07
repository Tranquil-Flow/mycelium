// SPDX-License-Identifier: AGPL-3.0-only
//! `AppDemoChain` for the owner run (SE-L10-OWNER-RUN-HOST / SE-L10-MODEL-FACTS): the chain a host hands to
//! [`crate::owner_driver::DemoRuns`]. A step with a ported implementation in `owner-run` runs over the
//! host's [`ChainPorts`]; a step whose port the host does not wire ends the run with a closed, tagged
//! refusal that names its seam (`owner_run_step_not_ported`, a code v0.1.4 never produces), never a
//! success. No step logic lives here.
use crate::{
    acquisition::AcqError,
    model_catalog::{self, Model},
    owner_driver::Chain,
};
use owner_run::{
    chain::{StepEnv, StepError},
    machine::{CeilingProbe, MonitorProbe, OwnerRunError, Reason},
    state::RunState,
    steps::{
        self,
        contributor::ContributorPort,
        exchange::ReportsPort,
        model::{ModelPort, PreparedModel, PreparedSource, Reverify},
    },
};
use serde_json::Value;

/// What the host must answer for the model step (`demo_chain.py:442-507` reads these from `app`).
pub trait ChainPorts: Send {
    /// `app.installation_role == 'owner' and app.running`.
    fn owner_and_running(&self) -> bool;
    /// `app.authority.status().get('swarm_created')`.
    fn swarm_created(&self) -> bool;
    /// `app.acquisition.status().get('state')`.
    fn acquisition_state(&self) -> String;
    /// `callable(getattr(app, 'reverify_model_snapshot', None))`.
    fn reverify_available(&self) -> bool {
        true
    }
    /// `app.reverify_model_snapshot()` (a failure only leaves the model unprepared).
    fn reverify(&self) -> Reverify;
    /// `app.acquisition.prepared_source()` with the registered model's facts; a failure keeps only its class.
    fn prepared_source(&self) -> Result<PreparedSource, StepError>;
    /// `read_resource_policy(root).get('storage_bytes')`.
    fn storage_consent_cap(&self) -> Result<Option<Value>, StepError>;
    /// `_check_rollover_members(binding)`.
    fn check_rollover_members(&self, binding: &Value) -> Result<(), StepError>;
    /// The local contributor step's collaborators (`app._start_local_contributor`, the run exchange, the
    /// in-process agent); `None` leaves the step unwired (a tagged refusal).
    fn contributor(&mut self) -> Option<&mut dyn ContributorPort> {
        None
    }
    /// The run exchange and the seed coordinator as the descriptors step reads them; `None` leaves the
    /// step unwired.
    fn reports(&mut self) -> Option<&mut dyn ReportsPort> {
        None
    }
    /// The model step passed: the picker choice the announcements carry (`demo_chain.py:491`).
    fn note_model(&mut self, _choice: Option<&str>) {}
}

/// The exception class of a failed acquisition call (`AcquisitionError`).
pub fn acquisition_exception(_: &AcqError) -> StepError {
    StepError::Exception {
        class: "AcquisitionError".to_owned(),
    }
}

/// `PreparedSource` of a registered model: label, architecture, picker choice and owner storage from the
/// catalog (SE-L10-MODEL-FACTS), digest and bytes from the accepted snapshot.
pub fn prepared_source_from(
    model: &Model,
    manifest_digest: String,
    source_bytes: Value,
) -> PreparedSource {
    PreparedSource {
        model: PreparedModel {
            model_id: model.model_id.to_owned(),
            resolved_commit: model.resolved_commit.to_owned(),
            architecture: model.architecture().to_owned(),
            label: model.label().to_owned(),
            choice: model_catalog::choice_for_model(model).map(str::to_owned),
            shape_registered: true,
            owner_storage_need: model_catalog::owner_storage_requirement(model),
        },
        manifest_digest,
        source_bytes,
    }
}

/// `ModelPort` over the host's [`ChainPorts`].
pub struct ModelAdapter<P: ChainPorts> {
    ports: P,
}
impl<P: ChainPorts> ModelAdapter<P> {
    pub fn new(ports: P) -> Self {
        Self { ports }
    }
}
impl<P: ChainPorts> ModelPort for ModelAdapter<P> {
    fn owner_and_running(&self) -> bool {
        self.ports.owner_and_running()
    }
    fn swarm_created(&self) -> bool {
        self.ports.swarm_created()
    }
    // The previous run's closing thread is the driver's own: a new run starts only after it ended.
    fn has_closing_thread(&self) -> bool {
        false
    }
    fn closing_thread_alive(&mut self) -> bool {
        false
    }
    fn acquisition_state(&mut self) -> String {
        self.ports.acquisition_state()
    }
    fn reverify_available(&self) -> bool {
        self.ports.reverify_available()
    }
    fn reverify(&mut self) -> Reverify {
        self.ports.reverify()
    }
    fn prepared_source(&mut self) -> Result<PreparedSource, StepError> {
        self.ports.prepared_source()
    }
    fn storage_consent_cap(&mut self) -> Result<Option<Value>, StepError> {
        self.ports.storage_consent_cap()
    }
    fn check_rollover_members(&mut self, binding: &Value) -> Result<(), StepError> {
        self.ports.check_rollover_members(binding)
    }
}

/// The closed refusal of a step the host has no collaborator for: names the tag and the seam.
pub fn unwired(step: &str, tag: &str, seam: &str) -> StepError {
    let message = format!(
        "The {step} step is not wired in this Rust host yet ({tag}, waiting for seam {seam}); the previous release runs it."
    );
    OwnerRunError::with(steps::NOT_PORTED_CODE, Some(&message), None).into()
}

#[path = "owner_chain_legacy.rs"]
mod legacy;

/// Original constructor/host trait retained for existing API consumers. Its
/// collaborators are adapted into the shared chain, not another step machine.
pub struct HostChain<P: ChainPorts> {
    inner: SharedHostChain<legacy::LegacyPorts<P>>,
    host: std::sync::Arc<std::sync::Mutex<P>>,
}

/// Direct host composition over admitted shared ports. WorkPorts and SlicesPorts
/// borrow the host's actual independent fields; absent producers stay None.
/// The wrapper only preserves API refusal wording and the pre-new_run None view.
pub struct SharedHostChain<P: owner_run::owner_chain::ChainPorts> {
    inner: owner_run::owner_chain::OwnerChain<P>,
    started: bool,
}
impl<P: owner_run::owner_chain::ChainPorts> SharedHostChain<P> {
    pub fn new(ports: P) -> Self {
        Self {
            inner: owner_run::owner_chain::OwnerChain::new(ports),
            started: false,
        }
    }
    pub fn state(&self) -> Option<&RunState> {
        self.started.then(|| self.inner.state())
    }
}

fn api_refusal(error: StepError) -> StepError {
    // Translate only the shared adapter's exact missing-capability refusals.
    // Collaborator errors, partial states, and global unported stages are untouched.
    for (step, old_seam, api_seam) in [
        (
            "contributor",
            "SE-L10-LOCAL-CONTRIBUTOR",
            "SE-L10-LOCAL-CONTRIBUTOR",
        ),
        ("descriptors", "SE-L10-REPORTS-PORT", "SE-L10-REPORTS-PORT"),
        ("observe", "SE-L10-REPORTS-PORT", "SE-L9-OBSERVE-PORT"),
        ("observe", "SE-SLICE1-OBSERVE", "SE-L9-OBSERVE-PORT"),
        ("compile", "SE-L10-REPORTS-PORT", "SE-L7-CHAIN-COMPILE"),
        ("compile", "SE-L7-CHAIN-COMPILE", "SE-L7-CHAIN-COMPILE"),
    ] {
        if error == owner_run::owner_chain::unwired(step, &format!("NP-SLICE1-{step}"), old_seam) {
            return unwired(step, &format!("NP-L10-{step}"), api_seam);
        }
    }
    error
}
impl<P: owner_run::owner_chain::ChainPorts> Chain for SharedHostChain<P> {
    fn new_run(
        &mut self,
        run: &str,
        deployment: &str,
        reason: Reason,
        ceiling: Option<&Value>,
        binding: Option<&Value>,
    ) {
        self.inner
            .new_run(run, deployment, reason, ceiling, binding);
        self.started = true;
    }
    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        self.inner.run_step(step, env).map_err(api_refusal)
    }
    fn rollover_binding(&self) -> Option<Value> {
        self.inner.rollover_binding()
    }
    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        self.inner.teardown(failed, env)
    }
    fn rollover_due(&mut self) -> Option<bool> {
        self.inner.rollover_due()
    }
    fn ceiling_reached(&mut self) -> CeilingProbe {
        self.inner.ceiling_reached()
    }
    fn monitor(&mut self) -> MonitorProbe {
        self.inner.monitor()
    }
}
