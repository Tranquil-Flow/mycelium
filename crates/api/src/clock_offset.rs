// SPDX-License-Identifier: AGPL-3.0-only
//! Source-faithful peer clock diagnostics (`clock_offset.py @ f4e366e9`).
//! Only authenticated observations belong here; these estimates never grant execution authority.
use budgets::{BudgetId, Entries};
use serde_json::{Value, json};
use std::{collections::VecDeque, sync::Mutex};

#[derive(Default)]
struct Row {
    offsets: VecDeque<f64>,
    rtt: Option<f64>,
    seen: Option<f64>,
}
pub struct PeerClockOffsets {
    clock: Box<dyn Fn() -> f64 + Send + Sync>,
    samples: usize,
    stale_after: f64,
    tolerance: f64,
    max_rtt: f64,
    rows: Mutex<Vec<(String, Row)>>,
}
fn seconds(id: BudgetId) -> Result<f64, budgets::Refusal> {
    Ok(id.span(Entries::ZERO)?.millis() as f64 / 1000.0)
}
fn number(value: &Value) -> Option<f64> {
    value.as_f64().filter(|n| n.is_finite())
}
fn median(row: &Row) -> Option<f64> {
    let mut values: Vec<_> = row.offsets.iter().copied().collect();
    if values.is_empty() {
        return None;
    }
    values.sort_by(f64::total_cmp);
    let mid = values.len() / 2;
    Some(if values.len().is_multiple_of(2) {
        (values[mid - 1] + values[mid]) / 2.0
    } else {
        values[mid]
    })
}
fn rounded(value: f64) -> f64 {
    // Rust's decimal formatting, like Python round(x, 3), rounds the binary input to nearest/even.
    format!("{value:.3}")
        .parse()
        .expect("formatted finite decimal")
}
impl PeerClockOffsets {
    pub fn new(clock: Box<dyn Fn() -> f64 + Send + Sync>) -> Result<Self, budgets::Refusal> {
        Ok(Self {
            clock,
            samples: BudgetId::PyFinalMyceliumA13MacosClockOffsetL21C0Assignment
                .count()?
                .get() as usize,
            stale_after: seconds(BudgetId::PyMyceliumA13MacosClockOffsetStaleAfterSecondsL23)?,
            tolerance: seconds(BudgetId::ControlClockSkew)?,
            max_rtt: seconds(BudgetId::PyUpperGuardMyceliumA13MacosClockOffsetL49C45)?,
            rows: Mutex::new(Vec::new()),
        })
    }
    fn fresh(&self, row: &Row) -> bool {
        row.seen
            .is_some_and(|seen| (self.clock)() - seen <= self.stale_after)
    }
    /// Value inputs preserve source rejection of booleans/non-numbers. Missing/null receipt time reads clock.
    pub fn observe(
        &self,
        node: &str,
        signed_at: &Value,
        received_at: Option<&Value>,
        rtt: Option<&Value>,
    ) {
        let received = match received_at.filter(|v| !v.is_null()) {
            Some(value) => number(value),
            None => Some((self.clock)()),
        };
        let (Some(signed), Some(received)) =
            (number(signed_at), received.filter(|n| n.is_finite()))
        else {
            return;
        };
        let mut rows = self
            .rows
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let index = rows
            .iter()
            .position(|(key, _)| key == node)
            .unwrap_or_else(|| {
                rows.push((node.to_owned(), Row::default()));
                rows.len() - 1
            });
        let row = &mut rows[index].1;
        if let Some(rtt) = rtt
            .and_then(number)
            .filter(|r| (0.0..=self.max_rtt).contains(r))
        {
            row.rtt = Some(rtt);
        }
        if row.seen.is_some() && !self.fresh(row) {
            row.offsets.clear();
        }
        if row.offsets.len() == self.samples {
            row.offsets.pop_front();
        }
        row.offsets
            .push_back(signed - received + row.rtt.unwrap_or(0.0) / 2.0);
        row.seen = Some((self.clock)());
    }
    pub fn offset(&self, node: &str) -> Option<f64> {
        let rows = self
            .rows
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let row = &rows.iter().find(|(key, _)| key == node)?.1;
        self.fresh(row).then(|| median(row)).flatten()
    }
    pub fn snapshot(&self) -> Value {
        Value::Object(self.snapshot_rows().into_iter().collect())
    }
    fn snapshot_rows(&self) -> Vec<(String, Value)> {
        let rows = self
            .rows
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let now = (self.clock)();
        rows.iter().filter_map(|(node, row)| {
            let value = median(row)?;
            let fresh = self.fresh(row);
            Some((node.clone(), json!({"offset_seconds":rounded(value),"rtt_seconds":row.rtt,
                "samples":row.offsets.len(),"age_seconds":rounded(now-row.seen.expect("sample has timestamp")),
                "fresh":fresh,"tolerance_seconds":self.tolerance,"exceeds_tolerance":fresh && value.abs()>self.tolerance})))
        }).collect()
    }
    pub fn tolerance(&self) -> f64 {
        self.tolerance
    }
    /// Source insertion order is significant when two peers tie for the worst skew.
    pub fn exceeded(&self, nodes: Option<&[String]>) -> Vec<(String, f64)> {
        self.snapshot_rows()
            .iter()
            .filter(|(node, row)| {
                row["exceeds_tolerance"] == true && nodes.is_none_or(|nodes| nodes.contains(node))
            })
            .map(|(node, row)| {
                (
                    node.clone(),
                    row["offset_seconds"].as_f64().expect("offset number"),
                )
            })
            .collect()
    }
    pub fn view(&self, node: &str) -> Value {
        self.snapshot().get(node).cloned().unwrap_or_else(|| {
            json!({"offset_seconds":null,
            "exceeds_tolerance":false,"tolerance_seconds":self.tolerance,"samples":0})
        })
    }
}
