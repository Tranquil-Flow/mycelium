// SPDX-License-Identifier: AGPL-3.0-only
//! `_step_serving` (`demo_chain.py:987-1106 @ f4e366e9`): wait until the run's own workflow serves,
//! require that its startup challenge qualified the route, then publish `serving`.
//!
//! The host lends its workflow through [`WorkPort`]: `workflow_state` is `workflow.status()`,
//! `serving_identity` is `needs_identity(workflow)` / `current(workflow)` (:988-1004) and
//! `commit_serving` is the lifecycle-fenced publication (:1056-1097). Only the fenced branch is
//! ported: every production host owns its workflow, so the oracle's observational branch for an
//! injected legacy double (:1049-1055) has no Rust caller.
use super::exchange::{Announcement, ReportsPort, publish_participants, truthy};
use super::progress_wait::{Wait, progress_wait_with_observer};
use super::publish::{ServingIdentity, WorkPort, failure_code, require_mapping, workflow_snapshot};
use crate::chain::{StepEnv, StepError};
use crate::machine::OwnerRunError;
use crate::state::RunState;
use serde_json::{Map, Value, json};
use std::cell::RefCell;

/// `needs_identity(workflow)` (`demo_chain.py:988-992`) for a host whose workflow is absent: the
/// run's compile bound an epoch (`ctx.state['compile_state']['epoch'] is not None`, or the app's
/// compile state carries an `epoch` key). A present host workflow is always run-bound.
pub fn needs_identity(state: &RunState) -> bool {
    let epoch = state
        .compile_state
        .as_ref()
        .and_then(|c| c.get("epoch"))
        .is_some_and(|e| !e.is_null());
    let compiled = state
        .chain_compile_state
        .as_ref()
        .is_some_and(|c| c.get("epoch").is_some());
    epoch || compiled
}

/// `current(workflow)`'s epoch half (`demo_chain.py:995-1000`), from the run's own state: the run's
/// compile epoch and the compile state's epoch are both exact integers, equal, and the compile
/// state is `compiled`. Hosts add their own half (same run and deployment, not stopped).
pub fn compiled_epoch(state: &RunState) -> Option<&Value> {
    let exact = |v: &&Value| v.is_i64() || v.is_u64();
    let epoch = state.compile_state.as_ref()?.get("epoch").filter(exact)?;
    let compiled = state.chain_compile_state.as_ref()?;
    (compiled.get("epoch") == Some(epoch) && compiled.get("state") == Some(&json!("compiled")))
        .then_some(epoch)
}

/// Python `x != 0` for a JSON value: `0`, `0.0` and `False` equal zero; anything else does not.
fn nonzero(v: Option<&Value>) -> bool {
    match v {
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        _ => true,
    }
}

/// `state.get('state') in {'failed', 'stopped'}`: a list or mapping is unhashable (`TypeError`).
fn failed_or_stopped(status: &Value) -> Result<bool, StepError> {
    match status.get("state") {
        Some(Value::Array(_) | Value::Object(_)) => Err(StepError::Exception {
            class: "TypeError".into(),
        }),
        Some(Value::String(s)) => Ok(s == "failed" || s == "stopped"),
        _ => Ok(false),
    }
}

/// `(mapping.get(key) or {})` as the step reads nested documents: a falsy value is `{}`, a truthy
/// non-mapping has no `.get` (`AttributeError`).
fn sub_mapping(status: &Value, key: &str) -> Result<Value, StepError> {
    let value = status.get(key).cloned().unwrap_or(Value::Null);
    if !truthy(&value) {
        return Ok(Value::Object(Map::new()));
    }
    require_mapping(&value)?;
    Ok(value)
}

fn identity_changed() -> StepError {
    OwnerRunError::new("serving_workflow_identity_changed").into()
}

pub fn run_serving_step(
    env: &mut dyn StepEnv,
    reports: &mut dyn ReportsPort,
    workflow: &mut dyn WorkPort,
    state: &mut RunState,
    interval: f64,
) -> Result<(), StepError> {
    // A host that does not lend the serving collaborators refuses before the wait starts, with no
    // collaborator call and no clock read (its legacy `owner_run_step_not_ported` refusal). The
    // read itself is side-effect free, like the oracle's attribute reads.
    workflow.serving_identity(state)?;
    let workflow = RefCell::new(workflow);
    let run: &RunState = state;
    let wait = Wait {
        budget: budgets::BudgetId::Serving,
        code: "route_qualification_timeout",
        step: "serving",
        interval,
        waiting: None,
        observed: None,
    };
    progress_wait_with_observer(
        env,
        reports,
        run,
        &wait,
        // `up()` (:1011-1018): a failed or stopped workflow first, then its identity, then
        // `api_running`.
        &mut |_, _| {
            let status = workflow_snapshot(*workflow.borrow_mut())?;
            require_mapping(&status)?;
            if failed_or_stopped(&status)? {
                return Err(OwnerRunError::new(&failure_code(&status)).into());
            }
            if workflow.borrow_mut().serving_identity(run)? == ServingIdentity::Changed {
                return Err(identity_changed());
            }
            Ok(status.get("state") == Some(&json!("api_running")))
        },
        // `observed()` (:1002-1010): state and error code, plus the verified token count of the
        // run's OWN workflow when it is an exact integer in 1..=4096 (bools and floats are not).
        // Only strictly new verified indices renew the wait (:1019-1020).
        &mut || {
            let status = workflow_snapshot(*workflow.borrow_mut())?;
            require_mapping(&status)?;
            let mut view = json!({
                "state": status.get("state").cloned().unwrap_or(Value::Null),
                "error_code": status.get("error_code").cloned().unwrap_or(Value::Null),
            });
            if workflow.borrow_mut().serving_identity(run)? == ServingIdentity::Current {
                let progress = sub_mapping(&status, "challenge_progress")?;
                if let Some(count) = progress
                    .get("verified_token_count")
                    .and_then(Value::as_u64)
                    .filter(|c| (1..=4096).contains(c))
                {
                    view["verified_token_count"] = json!(count);
                }
            }
            Ok(Some(view))
        },
    )?;
    let workflow = workflow.into_inner();
    // :1021-1023: the workflow may have changed or gone while the wait returned.
    if matches!(
        workflow.serving_identity(state)?,
        ServingIdentity::Absent | ServingIdentity::Changed
    ) {
        return Err(identity_changed());
    }
    let status = workflow_snapshot(workflow)?;
    require_mapping(&status)?;
    let startup = sub_mapping(&status, "startup")?;
    // :1024-1043: `api_running` only means the API is up; the startup route challenge must have
    // qualified the route (two-Mac run 14 served with no qualified route).
    if startup.get("startup_qualification_complete") != Some(&Value::Bool(true))
        || nonzero(startup.get("unavailable_deployment_count"))
    {
        let unavailable = startup
            .get("unavailable_deployment_count")
            .filter(|v| v.is_i64() || v.is_u64())
            .map_or_else(|| "unknown".to_owned(), Value::to_string);
        env.progress(&format!(
            "the startup route challenge did not qualify the route ({unavailable} deployment(s) unavailable); the owner log names the reason (mycelium.live_serve_diagnostic.v1)"
        ));
        return Err(OwnerRunError::new("route_qualification_failed").into());
    }
    // :1044-1047: the supervisor's `http://127.0.0.1:<port>/`; chat and status carry the bare
    // origin. `startup.get('url') or ctx.state.get('registry_origin')`, stripped only if a string.
    let origin = startup
        .get("url")
        .filter(|u| truthy(u))
        .cloned()
        .or_else(|| state.registry_origin.clone())
        .unwrap_or(Value::Null);
    let origin = match origin {
        Value::String(s) => Value::String(s.trim_end_matches('/').to_owned()),
        other => other,
    };
    // :1058-1063: `ctx.state['model']` and the announcement are prepared before any lock.
    let model = state.model.as_ref().ok_or_else(|| StepError::Exception {
        class: "KeyError".into(),
    })?;
    let per_node = match &state.work_nodes {
        None | Some(Value::Null) => None,
        Some(Value::Object(nodes)) => Some(nodes.clone().into_iter().collect()),
        Some(_) => {
            return Err(StepError::Exception {
                class: "AttributeError".into(),
            });
        }
    };
    let announcement = Announcement {
        phase: "serving".into(),
        run_id: state.run_id.clone(),
        deployment_id: state.deployment_id.clone(),
        model_id: model.model_id.clone(),
        resolved_commit: model.resolved_commit.clone(),
        slice_mode: model.slice_mode,
        manifest_digest: model.manifest_digest.clone(),
        per_node,
    };
    // :1064-1097: the Stop check, the workflow re-checks and the in-memory commit under the owner's
    // Stop fence and the host's workflow lock; nothing is published when any of them refuses.
    workflow.commit_serving(
        state,
        &announcement,
        state.work_limits.as_ref(),
        &mut |commit| env.fenced(commit),
    )?;
    // Written after the fence: a result write takes the Runner's run lock, which it orders before
    // its stop event (the oracle writes `ctx.result['registry_origin']` inside the lock, :1097).
    env.set_result("registry_origin", origin);
    publish_participants(env, reports, state)?;
    env.progress("route ready: open the chat");
    Ok(())
}
