// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a6_case_observations.py.
use budget_model::inventory::{candidates, checked_in};
use std::collections::BTreeMap;
#[path = "common/partition_b.rs"]
mod proof;

const P: &str = "mycelium_a6_case_observations.py";

#[test]
fn b_observation_envelope_deadline_copies_are_readouts_not_authority() {
    let all: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let rows = checked_in().unwrap();
    for line in [401, 520] {
        let r = rows
            .iter()
            .find(|r| r.path == P && r.line == line && r.review.starts_with("partition-b-readout:"))
            .unwrap_or_else(|| panic!("missing B readout row {P}:{line}"));
        assert_eq!(r.disposition, "not-a-bound");
        assert_eq!(r.unit, "not-applicable");
        assert!(r.value.is_none() && r.budget.is_empty());
        assert!(r.authority.contains("no command authority"));
        assert!(proof::validate(r, &rows, &all), "line {line}");
        // A readout must not be re-labelled as a bound or carry a registry mapping.
        let mut bad = r.clone();
        bad.disposition = String::new();
        bad.budget = "CancelAndCleanup".into();
        bad.unit = "milliseconds".into();
        bad.value = Some(1);
        assert!(!proof::validate(&bad, &rows, &all));
        bad = r.clone();
        bad.expression = "settled.deadline_unix_ms + 1".into();
        assert!(!proof::validate(&bad, &rows, &all));
        bad = r.clone();
        bad.disposition = "derived".into();
        assert!(!proof::validate(&bad, &rows, &all));
    }
    // The recorder's own join/settle waits are enforcement sites and stay pending or
    // separately reviewed; the readout rule cannot cover them.
    assert!(!rows.iter().any(|r| r.path == P
        && matches!(r.line, 710 | 714)
        && r.review.starts_with("partition-b-readout:")));
    assert_eq!(proof::validate_rows(&rows), Ok(()));
}
