// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_live/route.py.
use budget_model::inventory::{candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

#[test]
fn b_fault_admission_offset_and_retained_diagnostic_characters_are_not_epochs_or_bytes() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for (line, unit, value, wrong) in [
        (1373, "milliseconds", 60000, "CancelAndCleanup"),
        (4014, "entries", 128, "RenewalDivisor"),
    ] {
        let r = rows
            .iter()
            .find(|r| r.path == "mycelium_live/route.py" && r.line == line)
            .expect("independent exact offset/count policy");
        assert_eq!(r.unit, unit);
        assert_eq!(r.value, Some(value));
        assert!(proof::validate(r, &rows, &all));
        let mut bad = r.clone();
        bad.budget = wrong.into();
        assert_ne!(bad.budget, r.budget);
        assert!(!proof::validate(&bad, &rows, &all));
        bad = r.clone();
        bad.unit = "bytes".into();
        assert!(!proof::validate(&bad, &rows, &all));
        bad = r.clone();
        bad.expression.push_str(" + 1");
        assert!(!proof::validate(&bad, &rows, &all));
    }
    // Static unit-control fixture; not a ported cleanup consumer or latency measurement.
    let text = "é".repeat(129);
    let retained: String = text.chars().take(128).collect();
    assert_eq!(retained.chars().count(), 128);
    assert!(retained.len() > 128, "UTF-8 bytes are a different quantity");
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}
