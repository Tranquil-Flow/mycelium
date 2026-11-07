// SPDX-License-Identifier: AGPL-3.0-only
//! Report predicate matches for human review; never modifies the inventory.
use budget_model::inventory::{candidates, categories::accepts, checked_in};
use std::collections::BTreeSet;
fn main() -> Result<(), String> {
    let reviewed: BTreeSet<_> = checked_in()?.into_iter().map(|row| row.key).collect();
    let numeric = std::env::args().any(|arg| arg == "--numeric");
    for row in candidates()? {
        if reviewed.contains(&row.key) {
            continue;
        }
        if numeric {
            if let Some((unit, value)) = budget_model::inventory::categories::numeric_literal(&row)
            {
                println!("{}\t{}\t{}", row.key, unit, value);
            }
            continue;
        }
        for category in [
            "diagnostic-field",
            "reviewed-state-field",
            "fixture-data",
            "timestamp-context",
            "latency-observation",
            "planner-cost",
            "window-identity",
            "windows-path-grammar",
            "http-status-code",
            "packaged-unit-test",
            "string-join",
            "path-join",
            "boolean-state",
            "install-release-data",
            "clock-provider",
            "clock-observation",
            "absent-measurement",
            "protocol-label",
            "schema-unit-label",
        ] {
            if accepts(category, &row) {
                println!("{}\t{}", row.key, category);
                break;
            }
        }
    }
    Ok(())
}
