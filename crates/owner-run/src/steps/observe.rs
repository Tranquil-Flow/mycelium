// SPDX-License-Identifier: AGPL-3.0-only
//! `_step_observe` (`demo_chain.py:727-790 @ f4e366e9`): every Mac says hello natively (no model load) and
//! the owner collects the observations. The step logic is ported; the managed observation context behind
//! [`ObservePort`] (bootstrap offers, `collect`, `retire_bootstrap`) belongs to L6/L9 (seam
//! SE-SLICE1-OBSERVE, boot id / host id, B-RUN-25) and is NOT wired by this crate: a test double stands
//! behind the trait only to prove the step's own waits, never the observation itself.
use super::exchange::{ReportsPort, announce, member_row, publish_participants, truthy};
use super::progress_wait::{Wait, progress_wait};
use crate::chain::{PortError, StepEnv, StepError};
use crate::machine::{OwnerRunError, Reason};
use crate::state::RunState;
use budgets::BudgetId;
use serde_json::{Value, json};
use std::cell::RefCell;
use std::collections::BTreeSet;

/// `_OBSERVATION_FINAL`: no retry can change one physical Mac or one boot.
const FINAL: [&str; 2] = [
    "managed_observation_hosts_not_distinct",
    "managed_observation_boots_not_distinct",
];
/// `_RESOURCE_CODES` and the wall-cap codes older member agents report as `detail`.
const RESOURCE_CODES: [&str; 2] = [
    "member_memory_cap_below_share",
    "member_storage_cap_below_share",
];
const WALL_CODES: [&str; 2] = ["member_wall_cap_below_share", "member_wall_limit_invalid"];

/// The observation context of the current run (`app._observation_context`) and the chain ops around it.
pub trait ObservePort {
    /// `ops.prepare_observation(app, run_id=, deployment_id=, descriptors=)`.
    fn prepare(
        &mut self,
        run_id: &str,
        deployment_id: &str,
        descriptors: &[Value],
    ) -> Result<(), PortError>;
    /// `ops.rebuild(...)`: the rebuild state (`awaiting_bootstrap_workers` when the old hello workers are gone).
    fn rebuild(
        &mut self,
        run_id: &str,
        deployment_id: &str,
        descriptors: &[Value],
    ) -> Result<String, PortError>;
    /// `sorted(context.facts)`.
    fn facts_nodes(&mut self) -> Vec<String>;
    /// `{node: offer['offer_id'] for node, offer in context.offers.items()}`, in the context's order.
    fn offers(&mut self) -> Vec<(String, String)>;
    /// `context.collect()`.
    fn collect(&mut self) -> Result<(), PortError>;
    /// `[o.get('host_id') for o in context.observations['observations']]`.
    fn observed_hosts(&mut self) -> Vec<Option<String>>;
    /// `context.retire_bootstrap()`.
    fn retire_bootstrap(&mut self) -> Result<(), PortError>;
}

/// `_raise_member_refusal(ctx, reports)`: inspect every participant before checking readiness, so an
/// earlier missing/unready report cannot hide a later refusal.
pub(crate) fn raise_member_refusal(
    reports: &std::collections::BTreeMap<String, Value>,
    state: &RunState,
) -> Result<(), StepError> {
    let null = Value::Null;
    for node in &state.participants {
        let report = reports.get(node).unwrap_or(&null);
        let get = |k: &str| report.get(k).unwrap_or(&null);
        let detail = get("detail");
        let code = detail.as_str();
        let is_error = get("state") == "error";
        let resource = code.is_some_and(|c| RESOURCE_CODES.contains(&c) || WALL_CODES.contains(&c));
        if !is_error && !resource {
            continue;
        }
        let code = if truthy(detail) {
            code.unwrap_or("member_reported_error")
        } else {
            "member_reported_error"
        };
        if RESOURCE_CODES.contains(&code)
            && let Some(row) = member_row(state.compile_state.as_ref(), node)
        {
            let message = super::slices::resource_refusal(code, &row, state)?;
            return Err(OwnerRunError::with(code, Some(&message), None).into());
        }
        return Err(OwnerRunError::new(code).into());
    }
    Ok(())
}

/// `_all_in(ctx, states)`.
fn all_in(
    port: &mut dyn ReportsPort,
    state: &RunState,
    states: &[&str],
) -> Result<bool, StepError> {
    let reports = super::exchange::run_reports(port, state)?;
    raise_member_refusal(&reports, state)?;
    let nodes = &state.participants;
    Ok(!nodes.is_empty()
        && nodes.iter().all(|n| {
            reports
                .get(n)
                .and_then(|r| r.get("state"))
                .and_then(Value::as_str)
                .is_some_and(|s| states.contains(&s))
        }))
}

/// Runs the step. On success `state.participants` is the sorted facts nodes.
pub fn run_observe_step(
    env: &mut dyn StepEnv,
    reports: &mut dyn ReportsPort,
    observe: &mut dyn ObservePort,
    state: &mut RunState,
    interval: f64,
) -> Result<(), StepError> {
    let cards = state.descriptors.clone();
    let (run_id, deployment_id) = (state.run_id.clone(), state.deployment_id.clone());
    if matches!(state.reason, Reason::Rebuild | Reason::Rollover) {
        let wait = Wait {
            budget: BudgetId::InitialCleanup,
            code: "rebuild_member_cleanup_timeout",
            step: "cleanup",
            interval,
            waiting: None,
            observed: None,
        };
        progress_wait(
            env,
            reports,
            state,
            &wait,
            &mut |_, _| match observe.rebuild(&run_id, &deployment_id, &cards) {
                Ok(s) => Ok(s == "awaiting_bootstrap_workers"),
                Err(e) => Err(e.into_step_error("observe_prepare_failed")),
            },
        )?;
    } else {
        observe
            .prepare(&run_id, &deployment_id, &cards)
            .map_err(|e| e.into_step_error("observe_prepare_failed"))?;
    }
    state.participants = observe.facts_nodes();
    let offers = observe.offers();
    announce(
        reports,
        state,
        "observe",
        Some(
            offers
                .into_iter()
                .map(|(n, offer)| (n, json!({"bootstrap_offer_id": offer})))
                .collect(),
        ),
    )?;
    env.progress("starting native hello (no model load) on every Mac");
    // Progress: a member's report advancing towards bootstrap_running.
    {
        let wait = Wait {
            budget: BudgetId::Bootstrap,
            code: "member_bootstrap_timeout",
            step: "bootstrap",
            interval,
            waiting: Some(&|reports, participants| {
                participants
                    .iter()
                    .filter(|n| {
                        reports
                            .get(*n)
                            .and_then(|r| r.get("state"))
                            .and_then(Value::as_str)
                            != Some("bootstrap_running")
                    })
                    .cloned()
                    .collect()
            }),
            observed: None,
        };
        let snapshot = state.clone();
        progress_wait(env, reports, &snapshot, &wait, &mut |env, port| {
            publish_participants(env, port, &snapshot)?;
            all_in(port, &snapshot, &["bootstrap_running"])
        })?;
    }

    let last: RefCell<Option<String>> = RefCell::new(None);
    env.progress("observing hosts");
    // One hello attempt covers every Mac and has no partial result: progress is a new retry code (a
    // further stage reached) or a member's report.
    {
        let observed = || last.borrow().as_ref().map_or(Value::Null, |c| json!(c));
        let wait = Wait {
            budget: BudgetId::Collect,
            code: "member_observation_timeout",
            step: "collect",
            interval,
            waiting: None,
            observed: Some(&observed),
        };
        let snapshot = state.clone();
        progress_wait(env, reports, &snapshot, &wait, &mut |env, _| {
            match observe.collect() {
                Ok(()) => Ok(true),
                Err(e) => {
                    let code = e.code_or("observation_collect_failed");
                    if code == "observations_already_collected" {
                        return Ok(true);
                    }
                    // Same host / same boot is a fact about the members, not a member still starting.
                    if FINAL.contains(&code.as_str()) {
                        return Err(OwnerRunError::new(&code).into());
                    }
                    if last.borrow().as_deref() != Some(code.as_str()) {
                        env.progress(&format!("observing hosts (retrying after {code})"));
                        *last.borrow_mut() = Some(code);
                    }
                    Ok(false)
                }
            }
        })?;
    }
    let hosts = observe.observed_hosts();
    let distinct: BTreeSet<&Option<String>> = hosts.iter().collect();
    env.progress(&format!(
        "{} Macs observed, {} distinct hosts",
        hosts.len(),
        distinct.len()
    ));

    let retire_code: RefCell<Option<String>> = RefCell::new(None);
    {
        let observed = || {
            retire_code
                .borrow()
                .as_ref()
                .map_or(Value::Null, |c| json!(c))
        };
        let wait = Wait {
            budget: BudgetId::InitialCleanup,
            code: "member_cleanup_timeout",
            step: "cleanup",
            interval,
            waiting: None,
            observed: Some(&observed),
        };
        let snapshot = state.clone();
        progress_wait(
            env,
            reports,
            &snapshot,
            &wait,
            &mut |_, _| match observe.retire_bootstrap() {
                Ok(()) => Ok(true),
                Err(e) => {
                    *retire_code.borrow_mut() = Some(e.code_or("bootstrap_retire_failed"));
                    Ok(false)
                }
            },
        )?;
    }
    Ok(())
}
