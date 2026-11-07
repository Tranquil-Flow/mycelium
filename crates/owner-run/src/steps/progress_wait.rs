// SPDX-License-Identifier: AGPL-3.0-only
//! `AppDemoChain._progress_wait` (`demo_chain.py:300-381 @ f4e366e9`): a predicate under a budget that
//! renews on the step's visible progress. `budget` bounds a stretch WITHOUT progress (a joined Mac on a
//! ~1 MB/s hotspot is slow, not stuck); progress is a value not seen before in this step of what it
//! observes (the report of each Mac it still waits for, and the step's own signal). A Mac that is not
//! heard from for `ReportSilence` is waited for while its seed lease is live, up to `MemberSilence`.
//! Ends with `code` (stuck; the message names the Macs it waits for) or `<step>_member_unreachable`.
use super::descriptors::secs;
use super::exchange::{ReportsPort, remote_members, run_reports};
use crate::chain::{StepEnv, StepError, wait_with};
use crate::machine::OwnerRunError;
use crate::state::RunState;
use budgets::BudgetId;
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

/// `_STEP_WHAT`.
fn what(step: &str) -> &'static str {
    match step {
        "bootstrap" => "starting native hello",
        "collect" => "observing hosts",
        "cleanup" => "cleaning up the hello workers",
        "targets" => "starting the work on every Mac",
        _ => "qualifying the route",
    }
}

fn label(node: &str, local: Option<&str>) -> String {
    let chars: Vec<char> = node.chars().collect();
    let tail: String = chars[chars.len().saturating_sub(8)..].iter().collect();
    let who = if Some(node) == local {
        "This Mac (owner)"
    } else {
        "Joined Mac"
    };
    format!("{who} …{tail}")
}

/// `'%s' % value` for a report field (`None` renders as `None`).
fn py_str(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        other => other.to_string(),
    }
}

/// `waiting(reports)`: the Macs a step still waits for, from this run's reports and its participants.
pub type Waiting<'a> = &'a dyn Fn(&BTreeMap<String, Value>, &[String]) -> Vec<String>;
/// The wait's predicate (after `self._tick_local()`): true when the step's condition holds.
pub type Predicate<'a> =
    &'a mut dyn FnMut(&mut dyn StepEnv, &mut dyn ReportsPort) -> Result<bool, StepError>;

/// The step's inputs to the wait.
pub struct Wait<'a> {
    pub budget: BudgetId,
    pub code: &'a str,
    pub step: &'a str,
    pub interval: f64,
    /// `waiting(reports)`: the Macs the step still waits for (default every participant).
    pub waiting: Option<Waiting<'a>>,
    /// `observed()`: the step's own progress signal.
    pub observed: Option<&'a dyn Fn() -> Value>,
}

/// `predicate(env, port)` is the oracle's `predicate()` after `self._tick_local()`.
pub fn progress_wait(
    env: &mut dyn StepEnv,
    port: &mut dyn ReportsPort,
    state: &RunState,
    wait: &Wait<'_>,
    predicate: Predicate<'_>,
) -> Result<(), StepError> {
    progress_wait_with_observer(env, port, state, wait, predicate, &mut || {
        Ok(wait.observed.map(|f| f()))
    })
}

/// Same wait with a fallible observation (workflow.status can raise). The existing
/// Wait and progress_wait API remain unchanged for already-wired callers.
pub fn progress_wait_with_observer(
    env: &mut dyn StepEnv,
    port: &mut dyn ReportsPort,
    state: &RunState,
    wait: &Wait<'_>,
    predicate: Predicate<'_>,
    observed: &mut dyn FnMut() -> Result<Option<Value>, StepError>,
) -> Result<(), StepError> {
    let window = secs(wait.budget);
    let silence = secs(BudgetId::ReportSilence);
    let silent_max = secs(BudgetId::MemberSilence);
    let what = what(wait.step);
    let local = state.local_node.clone();
    let mut seen: BTreeSet<String> = BTreeSet::new();
    let start = env.now();
    let (mut rose, mut flat, mut polled) = (start, 0.0_f64, start);
    wait_with(
        env,
        |env| {
            port.tick_local();
            if predicate(env, port)? {
                return Ok(Some(()));
            }
            let (now, wall) = (env.now(), env.wall());
            let reports = run_reports(port, state)?;
            let nodes: Vec<String> = match wait.waiting {
                Some(f) => f(&reports, &state.participants),
                None => state.participants.clone(),
            };
            let null = Value::Null;
            let mut values: BTreeSet<String> = nodes
                .iter()
                .map(|node| {
                    let r = reports.get(node);
                    let get = |k: &str| r.and_then(|r| r.get(k)).unwrap_or(&null);
                    format!(
                        "member|{node}|{}|{}|{}",
                        get("state"),
                        get("detail"),
                        get("progress")
                    )
                })
                .collect();
            if let Some(value) = observed()? {
                values.insert(format!("step|{value}"));
            }
            let mut silent: Vec<(String, f64)> = Vec::new();
            for node in &nodes {
                let heard = reports
                    .get(node)
                    .and_then(|r| r.get("received_at"))
                    .and_then(Value::as_number)
                    .and_then(serde_json::Number::as_f64);
                if Some(node.as_str()) != local.as_deref()
                    && let Some(heard) = heard
                    && wall - heard >= silence
                {
                    silent.push((node.clone(), wall - heard));
                }
            }
            if values.difference(&seen).next().is_some() {
                seen.extend(values);
                rose = now;
                flat = 0.0;
            } else if silent.is_empty() {
                flat += now - polled;
            }
            polled = now;
            let idle = now - rose;
            if !silent.is_empty() {
                let live = remote_members(port, local.as_deref().unwrap_or_default(), env.wall());
                let mut gone: Vec<&String> = silent
                    .iter()
                    .map(|(n, _)| n)
                    .filter(|n| !live.contains(n))
                    .collect();
                gone.sort();
                let (node, seconds) = match gone.first() {
                    Some(g) => {
                        let seconds = silent.iter().find(|(n, _)| n == *g).map_or(0.0, |s| s.1);
                        ((*g).clone(), seconds)
                    }
                    None => {
                        let mut best = &silent[0];
                        for row in &silent {
                            if row.1 > best.1 {
                                best = row;
                            }
                        }
                        best.clone()
                    }
                };
                if !gone.is_empty() || seconds >= silent_max || idle >= silent_max + window {
                    let message = format!(
                        "{} stopped answering while {what} and did not come back within {} s{}. Check its network, then press Start again.",
                        label(&node, local.as_deref()),
                        silent_max as i64,
                        if gone.is_empty() {
                            ""
                        } else {
                            " (its seed lease lapsed)"
                        }
                    );
                    return Err(OwnerRunError::with(
                        &format!("{}_member_unreachable", wait.step),
                        Some(&message),
                        None,
                    )
                    .into());
                }
                env.progress(&format!(
                    "{what}: {} has not been heard from for {} s (link drop?); waiting up to {} s",
                    label(&node, local.as_deref()),
                    seconds as i64,
                    silent_max as i64
                ));
                return Ok(None);
            }
            if flat >= window || idle >= silent_max + window {
                // `sorted(nodes, key=lambda n: n == local)`: the owner's own Mac last, otherwise stable.
                let mut ordered: Vec<&String> = nodes
                    .iter()
                    .filter(|n| Some(n.as_str()) != local.as_deref())
                    .collect();
                ordered.extend(
                    nodes
                        .iter()
                        .filter(|n| Some(n.as_str()) == local.as_deref()),
                );
                let why = ordered
                    .iter()
                    .map(|n| {
                        let r = reports.get(*n);
                        let pick = |k: &str| {
                            r.and_then(|r| r.get(k))
                                .filter(|v| super::exchange::truthy(v))
                        };
                        let shown = pick("detail")
                            .or_else(|| pick("state"))
                            .map_or_else(|| "no_report".to_owned(), py_str);
                        format!("{}: {shown}", label(n, local.as_deref()))
                    })
                    .collect::<Vec<_>>()
                    .join("; ");
                let message = format!(
                    "Nothing advanced for {} s while {what}{}.",
                    window as i64,
                    if why.is_empty() {
                        String::new()
                    } else {
                        format!("; still waiting for {why}")
                    }
                );
                return Err(OwnerRunError::with(wait.code, Some(&message), None).into());
            }
            Ok(None)
        },
        f64::INFINITY,
        wait.code,
        wait.interval,
    )
}
