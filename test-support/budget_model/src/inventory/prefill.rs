// SPDX-License-Identifier: AGPL-3.0-only
//! Pinned prefill policy review, including consumption (not mere telemetry).
use super::{Bound, Candidate};
use std::collections::BTreeMap;

pub fn validate(row: &Bound, candidates: &BTreeMap<String, Candidate>) -> bool {
    let (rule, budget) = match row.key.as_str() {
        "mycelium_router/transports/iroh.py:175:4:assignment:stall_rtt_multiplier" => {
            ("factor", "PyPrefillStallRttMultiplier")
        }
        "mycelium_router/transports/iroh.py:176:4:assignment:stall_transfer_multiplier" => {
            ("factor", "PyPrefillStallTransferMultiplier")
        }
        "mycelium_router/transports/iroh.py:182:4:assignment:unmeasured_rtt_seconds" => {
            ("fallback", "PyPrefillUnmeasuredRttFallback")
        }
        "mycelium_router/transports/iroh.py:259:15:comparison:self.stall_rtt_multiplier" => {
            ("factor-zero-check", "PyPrefillStallRttMultiplier")
        }
        "mycelium_router/transports/iroh.py:1667:8:assignment:stalled_windows" => {
            ("consumed-resends", "PyPrefillMaximumResends")
        }
        "mycelium_router/transports/iroh.py:1718:16:assignment:stalled_windows" => {
            ("consumed-resends", "PyPrefillMaximumResends")
        }
        "mycelium_router/transports/iroh.py:1774:16:keyword:stalled_windows" => {
            ("consumed-resends", "PyPrefillMaximumResends")
        }
        "mycelium_router/transports/iroh.py:2074:31:parameter:stalled_windows" => {
            ("consumed-resends", "PyPrefillMaximumResends")
        }
        "mycelium_router/transports/iroh.py:2105:8:assignment:resends" => {
            ("consumed-resends", "PyPrefillMaximumResends")
        }
        "mycelium_router/transports/iroh.py:2125:15:comparison:resends" => {
            ("consumed-resends", "PyPrefillMaximumResends")
        }
        "mycelium_router/transports/iroh.py:1931:32:comparison:windows" => {
            ("consumed-resends", "PyPrefillMaximumResends")
        }
        _ => return false,
    };
    let Some(candidate) = candidates.get(&row.key) else {
        return false;
    };
    let identity = candidate.path == row.path
        && candidate.line == row.line
        && candidate.column == row.column
        && candidate.scope == row.scope
        && candidate.symbol == row.symbol
        && candidate.expression == row.expression
        && row.source_sha == super::SOURCE_SHA
        && row.review.split(';').next() == Some(format!("prefill-category:{rule}").as_str());
    identity
        && match rule {
            "factor" => {
                row.budget == budget
                    && row.unit == "entries"
                    && row.disposition.is_empty()
                    && row.value.is_some()
            }
            "fallback" => {
                row.budget == budget
                    && row.unit == "milliseconds"
                    && row.disposition.is_empty()
                    && row.value.is_some()
            }
            "consumed-resends" | "factor-zero-check" => {
                row.relation == budget
                    && row.budget.is_empty()
                    && row.disposition == "derived"
                    && row.unit == "entries"
                    && row.value.is_none()
            }
            _ => false,
        }
}
