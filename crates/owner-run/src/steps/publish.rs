// SPDX-License-Identifier: AGPL-3.0-only
//! Publish and targets borrow the existing prepared workflow; never manufacture it.
use super::exchange::ReportsPort;
use crate::chain::{PortError, StepEnv, StepError};
use crate::state::RunState;
use serde_json::Value;

/// Host-owned workflow lifecycle. Implementations use the existing workflow and
/// local-agent callback; they must not create a planner, signer or client journal.
pub trait WorkPort {
    /// Install the existing local tick as cleanup_progress, if a workflow exists.
    /// Callback lifetime remains host-owned, including after this borrow ends.
    fn install_cleanup_progress(&mut self) -> Result<(), StepError>;
    /// Existing ChainOps.start_serving(app, proposal), returning its actual document.
    fn start_serving(&mut self, proposal: &Value) -> Result<Value, PortError>;
    /// Existing workflow.status(); null only when the workflow is genuinely absent.
    fn workflow_state(&mut self) -> Result<Value, StepError>;
}
/// Additive borrow bundle; existing Ports literals remain valid.
pub struct WorkPorts<'a> {
    pub reports: &'a mut dyn ReportsPort,
    pub workflow: &'a mut dyn WorkPort,
}

/// `_step_publish` (`demo_chain.py:890-918 @ f4e366e9`).
pub fn run_publish_step(
    env: &mut dyn StepEnv,
    reports: &mut dyn ReportsPort,
    workflow: &mut dyn WorkPort,
    state: &mut RunState,
    interval: f64,
) -> Result<(), StepError> {
    workflow.install_cleanup_progress()?;
    // Python evaluates the default expression even when setdefault keeps an old value.
    let now = env.now();
    state.execution_clock_started_at.get_or_insert(now);
    let proposal = state
        .proposal
        .as_ref()
        .ok_or_else(|| crate::machine::OwnerRunError::new("serving_start_failed"))?;
    let started = workflow.start_serving(proposal).map_err(|e| {
        // This callsite catches *all* exceptions: even OwnerRunError loses message/fix.
        StepError::from(crate::machine::OwnerRunError::new(
            &e.code_or("serving_start_failed"),
        ))
    })?;
    if !started.is_object() {
        return Err(StepError::Exception {
            class: "AttributeError".into(),
        });
    }
    state.registry_origin = Some(
        started
            .get("registry_origin")
            .cloned()
            .unwrap_or(Value::Null),
    );
    env.progress("work offers published");
    let result = crate::chain::wait_with(
        env,
        |env| {
            reports.tick_local();
            let status = workflow_snapshot(workflow)?;
            super::exchange::publish_participants(env, reports, state)?;
            super::observe::raise_member_refusal(
                &super::exchange::run_reports(reports, state)?,
                state,
            )?;
            require_mapping(&status)?;
            if status["state"] == "failed" {
                let code = failure_code(&status);
                name_waiting(env, reports, state, &code);
                return Err(crate::machine::OwnerRunError::new(&code).into());
            }
            Ok((status["state"] == "awaiting_activation").then_some(()))
        },
        super::descriptors::secs(budgets::BudgetId::Publish),
        "member_staging_timeout",
        interval,
    );
    if matches!(&result, Err(StepError::Refused(e)) if e.code == "member_staging_timeout") {
        name_waiting(env, reports, state, "timed out");
    }
    result
}

pub(crate) fn workflow_snapshot(workflow: &mut dyn WorkPort) -> Result<Value, StepError> {
    let value = workflow.workflow_state()?;
    if !super::exchange::truthy(&value) {
        return Ok(serde_json::json!({}));
    }
    Ok(value)
}
pub(crate) fn require_mapping(value: &Value) -> Result<(), StepError> {
    if !value.is_object() {
        return Err(StepError::Exception {
            class: "AttributeError".into(),
        });
    }
    Ok(())
}
pub(crate) fn failure_code(status: &Value) -> String {
    status
        .get("error_code")
        .filter(|v| super::exchange::truthy(v))
        .map_or_else(|| "managed_workflow_failed".into(), py_str)
}
pub(crate) fn work_ready(report: &Value) -> bool {
    matches!(
        report.get("state").and_then(Value::as_str),
        Some("work_running" | "serving")
    )
}
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        other => other.to_string(),
    }
}
pub(crate) fn waiting_for(
    reports: &mut dyn ReportsPort,
    state: &RunState,
) -> Result<Vec<String>, StepError> {
    let current = super::exchange::run_reports(reports, state)?;
    Ok(state
        .participants
        .iter()
        .filter_map(|node| {
            let report = current.get(node).unwrap_or(&Value::Null);
            if work_ready(report) {
                return None;
            }
            let label = if state.local_node.as_ref() == Some(node) {
                "This Mac (owner)"
            } else {
                "Joined Mac"
            };
            let why = report
                .get("detail")
                .filter(|v| super::exchange::truthy(v))
                .or_else(|| report.get("state").filter(|v| super::exchange::truthy(v)))
                .map_or_else(|| "no_report".into(), py_str);
            let tail = node
                .chars()
                .rev()
                .take(8)
                .collect::<Vec<_>>()
                .into_iter()
                .rev()
                .collect::<String>();
            Some(format!("{label} …{tail}: {why}"))
        })
        .collect())
}
/// Best-effort diagnostic only: the oracle catches waiting-report errors and keeps
/// the original workflow/timeout refusal (`demo_chain.py:922-930`).
pub(crate) fn name_waiting(
    env: &mut dyn StepEnv,
    reports: &mut dyn ReportsPort,
    state: &RunState,
    prefix: &str,
) -> bool {
    match waiting_for(reports, state) {
        Ok(waiting) if !waiting.is_empty() => {
            env.progress(&format!(
                "{prefix}; still waiting for {}",
                waiting.join("; ")
            ));
            true
        }
        Ok(_) | Err(_) => false,
    }
}
