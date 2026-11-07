// SPDX-License-Identifier: AGPL-3.0-only
use crate::SimResult;
use budgets::table::{BudgetEntry, Value};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// Full declaration: values retain formula names and typed raw representation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Declaration {
    pub value: String,
    pub unit: String,
    pub purpose: String,
    pub provenance: String,
}
pub type Snapshot = BTreeMap<String, Declaration>;
fn declaration(e: BudgetEntry) -> Declaration {
    Declaration {
        value: format!("{:?}", e.value),
        unit: match e.value {
            Value::Duration(_) | Value::Derived(_) => "milliseconds",
            Value::Storage(_) => "bytes",
            Value::Count(_) => "entries",
            Value::BytesPerSecond(_) => "bytes_per_second",
            Value::CountPerSecond(_) => "entries_per_second",
            Value::Ratio(_) => "permille",
            Value::Blocked => "blocked",
        }
        .into(),
        purpose: format!("{:?}", e.purpose),
        provenance: format!("{:?}", e.provenance),
    }
}
pub fn registry() -> Snapshot {
    snapshot(budgets::table::BUDGET_TABLE.iter().copied())
}
fn snapshot(entries: impl IntoIterator<Item = BudgetEntry>) -> Snapshot {
    entries
        .into_iter()
        .map(|e| (format!("{:?}", e.id), declaration(e)))
        .collect()
}
pub fn capture<T>(f: impl FnOnce() -> SimResult<T>) -> SimResult<(T, Snapshot)> {
    let (result, reads) = budgets::reads::capture(f);
    Ok((result?, snapshot(reads)))
}
/// Registry growth is irrelevant; every pinned declaration must still exist
/// and match exactly. Runtime read-set equality is a separate, mandatory gate.
pub fn validate(pinned: &Snapshot, current: &Snapshot) -> SimResult<()> {
    if pinned
        .iter()
        .any(|(id, entry)| current.get(id) != Some(entry))
    {
        return Err("budget_snapshot_mismatch".into());
    }
    Ok(())
}
pub fn enforce_reads(pinned: &Snapshot, reads: &Snapshot) -> SimResult<()> {
    if reads.keys().any(|id| !pinned.contains_key(id)) {
        return Err("unpinned_budget_read".into());
    }
    if reads != pinned {
        return Err("budget_read_set_mismatch".into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn count_rates_keep_their_dimension_even_at_zero() {
        let mut e = budgets::BudgetId::ControlTtl.entry();
        e.value = Value::CountPerSecond(budgets::Entries::new(0));
        let rate = declaration(e);
        assert_eq!(rate.unit, "entries_per_second");
        e.value = Value::Count(budgets::Entries::new(0));
        let count = declaration(e);
        assert_ne!(rate.unit, count.unit);
        assert_ne!(rate.value, count.value);
        e.value = Value::BytesPerSecond(budgets::Bytes::new(0));
        assert_ne!(rate.unit, declaration(e).unit);
    }
}
