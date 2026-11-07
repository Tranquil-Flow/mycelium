// SPDX-License-Identifier: AGPL-3.0-only
//! Optional one-click execution ceiling, bound to one run before Start.
//! Oracle `execution_admission.py:8-47 @ f4e366e9`: an exact, bounded request body; bool, float and
//! string are not ints; `proposal_within_ceiling` is the synchronous admission check the compile
//! step makes against the compiled proposal before any work is published.
use budgets::BudgetId;
use serde_json::{Map, Value};

/// The closed code of a refused ceiling (`execution_admission.py:24-32`, `owner_run.py:222`).
pub const INVALID: &str = "execution_ceiling_invalid";

const DOWNLOAD: &str = "max_execution_download_bytes";
const RSS: &str = "max_combined_rss_bytes";
const DISK: &str = "max_additional_disk_bytes";
const WALL: &str = "max_wall_seconds";
const RUNS: &str = "run_count";

/// `REHEARSAL_MAX` in the oracle's key order (`execution_admission.py:11-17`).
pub fn rehearsal_max() -> Result<[(&'static str, u64); 5], &'static str> {
    let bytes = |id: BudgetId| {
        id.bytes()
            .map(|b| b.get())
            .map_err(|_| "budget_unavailable")
    };
    let wall = BudgetId::PyFinalMyceliumA13MacosExecutionAdmissionMaxWallSecondsL15C4
        .span(budgets::Entries::ZERO)
        .map_err(|_| "budget_unavailable")?
        .millis()
        / 1000;
    Ok([
        (
            DOWNLOAD,
            bytes(BudgetId::PyDefaultMyceliumA13MacosExecutionAdmissionL12C4)?,
        ),
        (
            RSS,
            bytes(BudgetId::PyFinalMyceliumA13MacosExecutionAdmissionMaxCombinedRssBytesL13C4)?,
        ),
        (
            DISK,
            bytes(BudgetId::PyFinalMyceliumA13MacosExecutionAdmissionMaxAdditionalDiskBytesL14C4)?,
        ),
        (WALL, wall),
        // `'run_count': 1` (execution_admission.py:16): one rehearsal run, not a registered budget.
        (RUNS, 1),
    ])
}

/// A validated ceiling: the five integers, in oracle order.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Ceiling(Map<String, Value>);

impl Ceiling {
    pub fn to_value(&self) -> Value {
        Value::Object(self.0.clone())
    }
}

/// An int in the oracle's sense: `type(x) is int` (no bool, no float; JSON gives neither a float
/// that is whole nor a bool as a number). A number beyond u64 is above every bound.
fn int(v: &Value) -> Option<u64> {
    match v {
        Value::Number(n) => n.as_u64(),
        _ => None,
    }
}

/// `checked_ceiling(value)`.
pub fn checked(value: &Value) -> Result<Ceiling, &'static str> {
    let bounds = rehearsal_max().map_err(|_| INVALID)?;
    let Value::Object(map) = value else {
        return Err(INVALID);
    };
    if map.len() != bounds.len() || bounds.iter().any(|(k, _)| !map.contains_key(*k)) {
        return Err(INVALID);
    }
    for (key, upper) in bounds {
        // a negative number or a non-int is `None`; both are refused
        let observed = int(&map[key]).ok_or(INVALID)?;
        if observed > upper {
            return Err(INVALID);
        }
        if key != DOWNLOAD && key != RUNS && observed == 0 {
            return Err(INVALID);
        }
    }
    if int(&map[RUNS]) != Some(1) {
        return Err(INVALID);
    }
    let mut ordered = Map::new();
    for (key, _) in bounds {
        ordered.insert(key.to_owned(), map[key].clone());
    }
    Ok(Ceiling(ordered))
}

/// `proposal_within_ceiling(proposal, ceiling)`.
pub fn proposal_within(proposal: &Value, ceiling: &Value) -> bool {
    let Ok(ceiling) = checked(ceiling) else {
        return false;
    };
    let Value::Object(proposal) = proposal else {
        return false;
    };
    ceiling.0.iter().all(|(key, bound)| {
        let (Some(bound), Some(p)) = (int(bound), proposal.get(key).and_then(int)) else {
            return false;
        };
        p <= bound && (key == DOWNLOAD || p > 0) && (key != DOWNLOAD && key != RUNS || p == bound)
    })
}
