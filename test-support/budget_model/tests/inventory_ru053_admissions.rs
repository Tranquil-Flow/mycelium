// SPDX-License-Identifier: AGPL-3.0-only
//! RU-053 admissions (CL-L1-07): the live closure equals the catalogue's projection, i.e. the pinned
//! pre-RU-053 base minus exactly the RU-053 rows whose admission records validate. Every later RU-053 batch
//! adds its own `ru053_admitted_*` tests here; none of them edits a historical test.
use budget_model::inventory::{
    epochs::{self, UNREVIEWED},
    report,
};
use std::collections::BTreeSet;

/// The pre-RU-053 closure: 55 rows to port verbatim plus the 3 guarded stats rows (RU-053).
const PRE_RU053_VIOLATIONS: usize = 58;

fn keys(violations: &[String]) -> BTreeSet<&str> {
    violations
        .iter()
        .map(|v| v.strip_prefix(UNREVIEWED).unwrap_or(v.as_str()))
        .collect()
}

#[test]
fn ru053_admitted_closure_matches_catalogue() {
    let live = report().expect("the checked-in inventory report");
    let admitted = epochs::ru053_admitted().expect("validated RU-053 admissions");
    let projected = epochs::projected_closure().expect("the catalogue's projected closure");
    assert_eq!(keys(&live.violations), keys(&projected));
    assert_eq!(live.violations, projected);
    assert_eq!(live.violations.len(), PRE_RU053_VIOLATIONS - admitted.len());
    for key in &admitted {
        assert!(!keys(&live.violations).contains(key.as_str()), "{key}");
    }
}
