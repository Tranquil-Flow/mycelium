// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/a10_batch_admission.py; O:path:mycelium_router/a10_target_verification.py.
use budget_model::inventory::{Candidate, bindings, candidates, categories, checked_in, validate};
fn at(path: &str, line: usize, symbol: &str) -> Candidate {
    candidates()
        .unwrap()
        .into_iter()
        .find(|r| r.path == path && r.line == line && r.symbol == symbol)
        .unwrap()
}
#[test]
fn helper_range_operands_keep_context_units_and_strict_rejection() {
    let p = "mycelium_router/a10_batch_admission.py";
    let maximum = at(p, 178, "maximum");
    let minimum = at(p, 177, "minimum");
    assert_eq!(
        categories::reviewed_quantity(&maximum, "helper-range-operand"),
        Some(("bytes", 1_073_741_824))
    );
    assert_eq!(
        categories::reviewed_quantity(&minimum, "helper-range-operand"),
        Some(("bytes", 1))
    );
    let count = at(p, 172, "maximum");
    assert_eq!(
        categories::reviewed_quantity(&count, "helper-range-operand"),
        Some(("entries", 1_073_741_824))
    );
    for expression in ["_BOUND_CEILING + 1", "FOREIGN_CEILING"] {
        let mut bad = maximum.clone();
        bad.expression = expression.into();
        assert_eq!(
            categories::reviewed_quantity(&bad, "helper-range-operand"),
            None
        );
    }
    let mut bad = maximum.clone();
    bad.scope.push_str(".foreign");
    assert_eq!(
        categories::reviewed_quantity(&bad, "helper-range-operand"),
        None
    );
    assert_eq!(categories::reviewed_quantity(&maximum, "literal"), None);
}
#[test]
fn required_policy_fields_bind_validated_ceilings_not_invented_defaults() {
    let p = "mycelium_router/a10_batch_admission.py";
    let source = at(p, 913, "maximum_bytes");
    let target = at(p, 175, "self.maximum_batch_bytes");
    assert!(bindings::accepts("reviewed-helper-field", &source, &target));
    let foreign = at(p, 224, "self.gateway_buffer_bytes");
    assert!(!bindings::accepts(
        "reviewed-helper-field",
        &source,
        &foreign
    ));
    let mut bad = source.clone();
    bad.expression = "self._policy.maximum_members".into();
    assert!(!bindings::accepts("reviewed-helper-field", &bad, &target));
    let mut reviews = checked_in().unwrap();
    let source_index = reviews.iter().position(|r| r.key == source.key).unwrap();
    let foreign_budget = reviews
        .iter()
        .find(|r| r.key == foreign.key)
        .unwrap()
        .relation
        .clone();
    reviews[source_index].relation = foreign_budget;
    assert!(validate(&reviews).is_err());
}
