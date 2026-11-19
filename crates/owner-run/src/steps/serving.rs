// SPDX-License-Identifier: AGPL-3.0-only
//! `_step_serving` (`demo_chain.py:987-1106 @ f4e366e9`): wait until the run's own workflow serves,
//! require that its startup challenge qualified the route, then publish `serving`.
//!
//! The host lends its workflow through [`WorkPort`]: `workflow_state` is `workflow.status()`,
//! `serving_identity` is `needs_identity(workflow)` / `current(workflow)` (:988-1004) and
//! `commit_serving` is the lifecycle-fenced publication (:1058-1097). Only the fenced branch is
//! ported: every production host owns its workflow, so the oracle's observational branch for an
//! injected legacy double (:1048-1057) has no Rust caller.
use super::exchange::{Announcement, ReportsPort, publish_participants, truthy};
use super::progress_wait::{Wait, progress_wait_with_observer};
use super::publish::{ServingIdentity, WorkPort, failure_code, require_mapping};
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

fn exception(class: &str) -> StepError {
    StepError::Exception {
        class: class.into(),
    }
}

/// `{} if workflow is None else workflow.status()` (`demo_chain.py:1007, 1017`) and the post-wait
/// `workflow.status()` (:1030), then `.get`: no `or {}` on the whole status. Only a genuinely absent
/// workflow ([`WorkPort::serving_status`] `None`) reads as `{}`; a present workflow whose status is
/// not a mapping, `null` included, has no `.get` (`AttributeError`).
fn serving_status(workflow: &mut dyn WorkPort) -> Result<Value, StepError> {
    match workflow.serving_status()? {
        None => Ok(Value::Object(Map::new())),
        Some(status @ Value::Object(_)) => Ok(status),
        Some(_) => Err(exception("AttributeError")),
    }
}

/// A Python dict key as `dict(...)` sees it (`str`, `int`, `float`, `bool`, `None`): equal keys
/// collapse as Python's do (`1 == 1.0 == True`, `0 == -0.0 == False`).
#[derive(Clone)]
enum PyKey {
    Str(String),
    Int(i128),
    Float(f64),
    None,
}
impl PyKey {
    fn of(value: &Value) -> Result<Self, StepError> {
        Ok(match value {
            Value::String(s) => Self::Str(s.clone()),
            Value::Bool(b) => Self::Int(i128::from(*b)),
            Value::Number(n) => match (n.as_i64(), n.as_u64(), n.as_f64()) {
                (Some(i), _, _) => Self::Int(i128::from(i)),
                (None, Some(u), _) => Self::Int(i128::from(u)),
                (None, None, Some(f)) => Self::Float(f),
                (None, None, None) => return Err(exception("TypeError")),
            },
            Value::Null => Self::None,
            // `unhashable type: 'list'` / `'dict'`.
            Value::Array(_) | Value::Object(_) => return Err(exception("TypeError")),
        })
    }
    fn same(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Str(a), Self::Str(b)) => a == b,
            (Self::None, Self::None) => true,
            (Self::Int(a), Self::Int(b)) => a == b,
            (Self::Float(a), Self::Float(b)) => a == b,
            (Self::Int(i), Self::Float(f)) | (Self::Float(f), Self::Int(i)) => {
                f.fract() == 0.0 && f.is_finite() && (*f as i128) == *i
            }
            _ => false,
        }
    }
    /// The key `json.dumps` writes (the announcement is `json.loads(json.dumps(...))`,
    /// run_exchange.py:175): `True` was collapsed into `1` already, `None` is `null`.
    fn json_key(&self, value: &Value) -> String {
        match self {
            Self::Str(s) => s.clone(),
            Self::None => "null".into(),
            Self::Int(_) => match value {
                Value::Bool(true) => "true".into(),
                Value::Bool(false) => "false".into(),
                other => other.to_string(),
            },
            Self::Float(f) => py_float_repr(*f),
        }
    }
}

/// Python's `repr(float)`: the shortest round-trip digits, positional for exponents -4..16,
/// otherwise `d.ddde+XX`.
fn py_float_repr(f: f64) -> String {
    let sci = format!("{f:e}");
    let Some((mantissa, exponent)) = sci.split_once('e') else {
        return sci;
    };
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let (sign, mantissa) = match mantissa.strip_prefix('-') {
        Some(rest) => ("-", rest),
        None => ("", mantissa),
    };
    let digits: String = mantissa.chars().filter(char::is_ascii_digit).collect();
    if (-4..16).contains(&exponent) {
        let point = exponent + 1;
        if point <= 0 {
            let zeros = "0".repeat(usize::try_from(-point).unwrap_or(0));
            format!("{sign}0.{zeros}{digits}")
        } else {
            let point = usize::try_from(point).unwrap_or(0);
            if point >= digits.len() {
                let zeros = "0".repeat(point - digits.len());
                format!("{sign}{digits}{zeros}.0")
            } else {
                format!("{sign}{}.{}", &digits[..point], &digits[point..])
            }
        }
    } else {
        let (first, rest) = digits.split_at(1);
        let fraction = if rest.is_empty() {
            String::new()
        } else {
            format!(".{rest}")
        };
        let exp_sign = if exponent < 0 { '-' } else { '+' };
        format!("{sign}{first}{fraction}e{exp_sign}{:02}", exponent.abs())
    }
}

/// One element of `dict(sequence)` as a key/value pair: a two-element list, a two-character
/// string, a two-key mapping's keys; any other length is a `ValueError`, a non-sequence a
/// `TypeError`.
fn py_pair(item: &Value) -> Result<(Value, Value), StepError> {
    match item {
        Value::Array(pair) if pair.len() == 2 => Ok((pair[0].clone(), pair[1].clone())),
        Value::String(pair) if pair.chars().count() == 2 => {
            let mut chars = pair.chars().map(|c| Value::String(c.to_string()));
            match (chars.next(), chars.next()) {
                (Some(k), Some(v)) => Ok((k, v)),
                _ => Err(exception("ValueError")),
            }
        }
        Value::Object(pair) if pair.len() == 2 => {
            let mut keys = pair.keys().map(|k| Value::String(k.clone()));
            match (keys.next(), keys.next()) {
                (Some(k), Some(v)) => Ok((k, v)),
                _ => Err(exception("ValueError")),
            }
        }
        Value::Array(_) | Value::String(_) | Value::Object(_) => Err(exception("ValueError")),
        Value::Null | Value::Bool(_) | Value::Number(_) => Err(exception("TypeError")),
    }
}

/// Python `dict(value)` for a JSON value, then its JSON keys (`run_exchange.py:169, 172`): a mapping
/// is copied; a list takes each element as a key/value pair ([`py_pair`]); `''` is `{}`. A number,
/// `True`/`False` or `None` is not iterable (`TypeError`). Elements are taken in order, each one
/// checked and its key hashed before the next (CPython's `dict_merge_from_seq2`), so the first
/// malformed element decides the class: a wrong length is a `ValueError`, a non-sequence element
/// or an unhashable key a `TypeError`. A mapping used as a pair yields its keys in the Value's own
/// key order.
fn py_dict(value: &Value) -> Result<Map<String, Value>, StepError> {
    let items: &[Value] = match value {
        Value::Object(map) => return Ok(map.clone()),
        Value::String(s) if s.is_empty() => &[],
        // Every character is a one-element sequence.
        Value::String(_) => return Err(exception("ValueError")),
        Value::Null | Value::Bool(_) | Value::Number(_) => return Err(exception("TypeError")),
        Value::Array(items) => items,
    };
    // Python keeps the FIRST equal key and the LAST value.
    let mut keys: Vec<(PyKey, Value, Value)> = Vec::new();
    for item in items {
        let (key, value) = py_pair(item)?;
        let py = PyKey::of(&key)?;
        match keys.iter_mut().find(|(k, _, _)| k.same(&py)) {
            Some(slot) => slot.2 = value,
            None => keys.push((py, key, value)),
        }
    }
    let mut out = Map::new();
    for (py, key, value) in keys {
        // Distinct Python keys can write the same JSON key (`1` and `'1'`): `json.loads` keeps
        // the last.
        out.insert(py.json_key(&key), value);
    }
    Ok(out)
}

/// `None if work_limits is None else dict(work_limits)` (`run_exchange.py:169`).
pub fn announcement_limits(work_limits: Option<&Value>) -> Result<Option<Value>, StepError> {
    match work_limits {
        None | Some(Value::Null) => Ok(None),
        Some(value) => Ok(Some(Value::Object(py_dict(value)?))),
    }
}

/// `{node: dict(extra) for node, extra in (per_node or {}).items()}` (`run_exchange.py:172`):
/// a falsy `per_node` is no rows; a truthy one that is not a mapping has no `.items()`
/// (`AttributeError`). `None` stays `None` (nothing announced per node).
pub fn announcement_nodes(
    work_nodes: Option<&Value>,
) -> Result<Option<Vec<(String, Value)>>, StepError> {
    match work_nodes {
        None | Some(Value::Null) => Ok(None),
        Some(value) if !truthy(value) => Ok(Some(Vec::new())),
        Some(Value::Object(nodes)) => nodes
            .iter()
            .map(|(node, extra)| Ok((node.clone(), Value::Object(py_dict(extra)?))))
            .collect::<Result<_, StepError>>()
            .map(Some),
        Some(_) => Err(exception("AttributeError")),
    }
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
        // `up()` (:1015-1022): a failed or stopped workflow first, then its identity, then
        // `api_running`.
        &mut |_, _| {
            let status = serving_status(*workflow.borrow_mut())?;
            if failed_or_stopped(&status)? {
                return Err(OwnerRunError::new(&failure_code(&status)).into());
            }
            if workflow.borrow_mut().serving_identity(run)? == ServingIdentity::Changed {
                return Err(identity_changed());
            }
            Ok(status.get("state") == Some(&json!("api_running")))
        },
        // `observed()` (:1005-1014): state and error code, plus the verified token count of the
        // run's OWN workflow when it is an exact integer in 1..=4096 (bools and floats are not).
        // Only strictly new verified indices renew the wait (:1023-1024).
        &mut || {
            let status = serving_status(*workflow.borrow_mut())?;
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
    // :1027-1029: the workflow may have changed or gone while the wait returned.
    if matches!(
        workflow.serving_identity(state)?,
        ServingIdentity::Absent | ServingIdentity::Changed
    ) {
        return Err(identity_changed());
    }
    let status = serving_status(workflow)?;
    let startup = sub_mapping(&status, "startup")?;
    // :1030-1042: `api_running` only means the API is up; the startup route challenge must have
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
    // :1043-1046: the supervisor's `http://127.0.0.1:<port>/`; chat and status carry the bare
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
    // :1058-1066: `ctx.state['model']` and the announcement are prepared before any lock, in the
    // source's order: the model, then `dict(work_limits)`, then the per-node rows.
    let model = state.model.as_ref().ok_or_else(|| exception("KeyError"))?;
    let work_limits = announcement_limits(state.work_limits.as_ref())?;
    let per_node = announcement_nodes(state.work_nodes.as_ref())?;
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
    // :1074-1097: the Stop check, the workflow re-checks, the in-memory commit and
    // `ctx.result['registry_origin']` under the owner's fence and the host's workflow lock;
    // nothing is published when any of them refuses.
    workflow.commit_serving(state, &announcement, work_limits.as_ref(), &mut |commit| {
        env.fenced(commit, "registry_origin", origin.clone())
    })?;
    if let Some(readiness) = workflow.route_readiness() {
        env.set_route_readiness(readiness);
    }
    publish_participants(env, reports, state)?;
    env.progress("route ready: open the chat");
    Ok(())
}
