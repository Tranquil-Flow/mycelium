// SPDX-License-Identifier: AGPL-3.0-only
//! `_step_targets` over existing workflow and reports; no activation effect.
use super::exchange::ReportsPort;
use super::publish::WorkPort;
use crate::chain::{StepEnv, StepError};
use crate::state::RunState;
pub fn run_targets_step(
    env: &mut dyn StepEnv,
    reports: &mut dyn ReportsPort,
    workflow: &mut dyn WorkPort,
    state: &mut RunState,
    interval: f64,
) -> Result<(), StepError> {
    use super::progress_wait::{Wait, progress_wait_with_observer};
    use super::publish::{failure_code, name_waiting, waiting_for, work_ready, workflow_snapshot};
    use crate::machine::OwnerRunError;
    use serde_json::{Value, json};
    use std::cell::RefCell;
    let now = env.now();
    state.execution_clock_started_at.get_or_insert(now);
    let workflow = RefCell::new(workflow);
    let waiting = |rows: &std::collections::BTreeMap<String, Value>, nodes: &[String]| {
        nodes
            .iter()
            .filter(|n| !work_ready(rows.get(*n).unwrap_or(&Value::Null)))
            .cloned()
            .collect()
    };
    let wait = Wait {
        budget: budgets::BudgetId::Targets,
        code: "member_work_timeout",
        step: "targets",
        interval,
        waiting: Some(&waiting),
        observed: None,
    };
    let result = progress_wait_with_observer(
        env,
        reports,
        state,
        &wait,
        &mut |env, reports| {
            let status = workflow_snapshot(*workflow.borrow_mut())?;
            super::exchange::publish_participants(env, reports, state)?;
            let rows = super::exchange::run_reports(reports, state)?;
            super::observe::raise_member_refusal(&rows, state)?;
            let done = !state.participants.is_empty()
                && state
                    .participants
                    .iter()
                    .all(|n| work_ready(rows.get(n).unwrap_or(&Value::Null)));
            super::publish::require_mapping(&status)?;
            if status["state"] == "failed" {
                let code = failure_code(&status);
                if !done && name_waiting(env, reports, state, &code) {
                    return Err(OwnerRunError::new("member_work_not_running").into());
                }
                return Err(OwnerRunError::new(&code).into());
            }
            if !done {
                let waiting = waiting_for(reports, state)?;
                if !waiting.is_empty() {
                    env.progress(&format!("waiting for {}", waiting.join("; ")));
                }
            }
            Ok(done)
        },
        &mut || {
            // The source dict comprehension reads workflow.status separately for EACH key.
            let phase = workflow_snapshot(*workflow.borrow_mut())?;
            super::publish::require_mapping(&phase)?;
            let phase = phase["state"].clone();
            let error = workflow_snapshot(*workflow.borrow_mut())?;
            super::publish::require_mapping(&error)?;
            let error = error["error_code"].clone();
            Ok(Some(json!({"state":phase,"error_code":error})))
        },
    );
    if matches!(&result,Err(StepError::Refused(e)) if e.code=="member_work_timeout") {
        name_waiting(env, reports, state, "timed out");
    }
    result
}
