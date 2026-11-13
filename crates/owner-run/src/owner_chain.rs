// SPDX-License-Identifier: AGPL-3.0-only
//! `AppDemoChain` for the owner run: the one [`Chain`] both hosts use. A host implements [`ChainPorts`]
//! (the collaborators behind each step); every step that is ported runs here over its port, and a step
//! whose port the host does not wire ends the run with a closed, tagged refusal naming its seam
//! (`owner_run_step_not_ported`, a code v0.1.4 never produces), never a success. No step logic lives in a
//! host.
use crate::chain::{StepEnv, StepError};
use crate::machine::{CeilingProbe, MonitorProbe, OwnerRunError, Reason};
use crate::runner::Chain;
use crate::state::RunState;
use crate::steps::{
    self, NOT_PORTED_CODE,
    compile::{CompilePort, run_compile_step},
    contributor::{ContributorPort, run_contributor_step},
    descriptors::run_descriptors_step,
    exchange::ReportsPort,
    model::{ModelPort, StepInput, run_model_step},
    observe::{ObservePort, run_observe_step},
};
use budgets::{BudgetId, Entries};
use serde_json::Value;

/// The collaborators of one host, borrowed field by field (a step may need several at once). A port a host
/// does not have is `None`; its step then ends as "no collaborator" (see [`unwired`]).
pub struct Ports<'a> {
    pub model: &'a mut dyn ModelPort,
    pub contributor: Option<&'a mut dyn ContributorPort>,
    pub reports: Option<&'a mut dyn ReportsPort>,
    pub observe: Option<&'a mut dyn ObservePort>,
    pub compile: Option<&'a mut dyn CompilePort>,
}

/// What a host must give the chain.
pub trait ChainPorts: Send {
    fn ports(&mut self) -> Ports<'_>;
    /// Borrow the workflow source and exchange together. Additive so existing
    /// hosts retain their Ports literals and closed refusal until wired.
    fn slices_ports(&mut self) -> Option<steps::slices::SlicesPorts<'_>> {
        None
    }
    /// Borrow the existing host-owned workflow and exchange. Installed callbacks
    /// outlive this borrow, own their captures without an owner/Runner cycle, and
    /// retain the host's lock order. Only the owning Runner closes and joins.
    fn work_ports(&mut self) -> Option<steps::publish::WorkPorts<'_>> {
        None
    }
    /// Temporarily borrow the host-owned activation scheduling operation.
    /// The host enforces consent/proposal identity and retains cleanup ownership.
    fn activation_port(&mut self) -> Option<&mut dyn steps::activate::ActivationPort> {
        None
    }
    /// `chain.teardown(ctx, failed=)`: close what the run started (nothing is started before the steps
    /// that need it are wired). The default has nothing to close.
    fn teardown(&mut self, _failed: bool, _env: &mut dyn StepEnv) -> Result<(), String> {
        Ok(())
    }
    /// `chain.rollover_due(ctx)`, `ceiling_reached(ctx)`, `monitor(ctx)`: only a serving run asks.
    fn rollover_due(&mut self, _state: &RunState) -> Option<bool> {
        None
    }
    fn ceiling_reached(&mut self, _state: &RunState) -> CeilingProbe {
        CeilingProbe::Unavailable
    }
    fn monitor(&mut self, _state: &RunState) -> MonitorProbe {
        MonitorProbe::Code(None)
    }
}

/// The closed refusal of a step the host has no collaborator for: names the tag and the seam.
pub fn unwired(step: &str, tag: &str, seam: &str) -> StepError {
    let message = format!(
        "The {step} step is not wired in this Rust host yet ({tag}, waiting for seam {seam}); the previous release runs it."
    );
    OwnerRunError::with(NOT_PORTED_CODE, Some(&message), None).into()
}

fn interval() -> f64 {
    BudgetId::OwnerPoll
        .span(Entries::ZERO)
        .map_or(f64::NAN, |s| s.millis() as f64 / 1000.0)
}

/// The chain of one host.
pub struct OwnerChain<P: ChainPorts> {
    ports: P,
    state: RunState,
    /// Where a copy of `ctx.state` is published after every step (diagnostics and tests; the chain lives on the
    /// run thread, so nothing else can read it).
    sink: Option<std::sync::Arc<std::sync::Mutex<RunState>>>,
}

impl<P: ChainPorts> OwnerChain<P> {
    pub fn new(ports: P) -> Self {
        Self {
            ports,
            state: RunState::new("", "", Reason::Start),
            sink: None,
        }
    }
    /// Publish `ctx.state` into `sink` after every step.
    pub fn with_state_sink(mut self, sink: std::sync::Arc<std::sync::Mutex<RunState>>) -> Self {
        self.sink = Some(sink);
        self
    }
    fn publish(&self) {
        if let Some(sink) = &self.sink {
            *sink
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner) = self.state.clone();
        }
    }
    fn step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        let state = &mut self.state;
        if step == "slices" {
            let ports = self
                .ports
                .slices_ports()
                .ok_or_else(|| steps::not_ported("slices"))?;
            return steps::slices::run_slices_step(
                env,
                ports.reports,
                ports.slices,
                state,
                interval(),
            );
        }
        if matches!(step, "publish" | "targets") {
            let ports = self
                .ports
                .work_ports()
                .ok_or_else(|| steps::not_ported(step))?;
            return if step == "publish" {
                steps::publish::run_publish_step(
                    env,
                    ports.reports,
                    ports.workflow,
                    state,
                    interval(),
                )
            } else {
                steps::targets::run_targets_step(
                    env,
                    ports.reports,
                    ports.workflow,
                    state,
                    interval(),
                )
            };
        }
        if step == "activate" {
            let port = self
                .ports
                .activation_port()
                .ok_or_else(|| steps::not_ported("activate"))?;
            return steps::activate::run_activate_step(env, port, state);
        }
        let ports = self.ports.ports();
        match step {
            "model" => {
                let input = StepInput {
                    rollover: state.reason == Reason::Rollover,
                    rollover_binding: state.rollover_binding.clone(),
                    slice_staging: ports.model.slice_staging(),
                };
                state.model = Some(run_model_step(env, ports.model, &input)?);
                Ok(())
            }
            "contributor" => {
                let port = ports.contributor.ok_or_else(|| {
                    unwired(
                        "contributor",
                        "NP-SLICE1-contributor",
                        "SE-L10-LOCAL-CONTRIBUTOR",
                    )
                })?;
                // Log-only: the published code for this step is the closed `owner_run_step_failed`,
                // so the step's own refusal/exception must be visible in the app log.
                state.local_node = Some(match run_contributor_step(env, port) {
                    Ok(local) => local,
                    Err(error) => {
                        eprintln!("owner_run_step2_failed:{error:?}");
                        return Err(error);
                    }
                });
                Ok(())
            }
            "descriptors" => {
                let reports = ports.reports.ok_or_else(|| {
                    unwired(
                        "descriptors",
                        "NP-SLICE1-descriptors",
                        "SE-L10-REPORTS-PORT",
                    )
                })?;
                run_descriptors_step(env, reports, state, interval())
            }
            "observe" => {
                let reports = ports.reports.ok_or_else(|| {
                    unwired("observe", "NP-SLICE1-observe", "SE-L10-REPORTS-PORT")
                })?;
                let observe = ports
                    .observe
                    .ok_or_else(|| unwired("observe", "NP-SLICE1-observe", "SE-SLICE1-OBSERVE"))?;
                run_observe_step(env, reports, observe, state, interval())
            }
            "compile" => {
                let reports = ports.reports.ok_or_else(|| {
                    unwired("compile", "NP-SLICE1-compile", "SE-L10-REPORTS-PORT")
                })?;
                let compile = ports.compile.ok_or_else(|| {
                    unwired("compile", "NP-SLICE1-compile", "SE-L7-CHAIN-COMPILE")
                })?;
                run_compile_step(env, reports, compile, state, interval())
            }
            other => Err(steps::not_ported(other)),
        }
    }

    /// `ctx.state` as the steps left it.
    pub fn state(&self) -> &RunState {
        &self.state
    }
}

impl<P: ChainPorts> Chain for OwnerChain<P> {
    fn new_run(
        &mut self,
        run_id: &str,
        deployment_id: &str,
        reason: Reason,
        ceiling: Option<&Value>,
        rollover_binding: Option<&Value>,
    ) {
        self.state = RunState::new(run_id, deployment_id, reason);
        self.state.execution_ceiling = ceiling.cloned();
        self.state.rollover_binding = rollover_binding.cloned();
        self.publish();
    }

    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        let result = self.step(step, env);
        self.publish();
        result
    }

    fn teardown(&mut self, failed: bool, env: &mut dyn StepEnv) -> Result<(), String> {
        self.ports.teardown(failed, env)
    }
    fn rollover_binding(&self) -> Option<Value> {
        self.state.rollover_binding.clone()
    }
    fn rollover_due(&mut self) -> Option<bool> {
        self.ports.rollover_due(&self.state)
    }
    fn ceiling_reached(&mut self) -> CeilingProbe {
        self.ports.ceiling_reached(&self.state)
    }
    fn monitor(&mut self) -> MonitorProbe {
        self.ports.monitor(&self.state)
    }
}
