// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_interactive/swarm.py; O:path:physical_inference_qualification.py.
use budget_model::inventory::{bindings, candidates, categories, checked_in, validate};

#[test]
fn interactive_history_floors_follow_validated_inputs_not_history_ceilings() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    for (line, validated_line, maximum) in [(333, 327, 1_024), (338, 329, 4_096)] {
        let floor = all
            .iter()
            .find(|c| {
                c.path == "mycelium_interactive/swarm.py"
                    && c.line == line
                    && c.kind == "comparison-lower-reference"
            })
            .unwrap();
        let target = all
            .iter()
            .find(|c| {
                c.path == floor.path && c.line == validated_line && c.kind == "validated-field"
            })
            .expect("validated count input separately discovered");
        assert!(bindings::accepts("reviewed-helper-field", floor, target));
        let ceiling = all
            .iter()
            .find(|c| c.path == floor.path && c.line == validated_line && c.kind == "comparison")
            .unwrap();
        assert_eq!(
            categories::reviewed_quantity(ceiling, "upper-guard"),
            Some(("entries", maximum))
        );
        let bound = rows.iter().find(|b| b.key == floor.key).unwrap();
        let ceiling_bound = rows.iter().find(|b| b.key == ceiling.key).unwrap();
        assert_eq!(bound.relation, ceiling_bound.budget);
        let unrelated = all
            .iter()
            .find(|c| c.path == floor.path && c.line == line && c.kind == "comparison")
            .unwrap();
        assert!(!bindings::accepts(
            "reviewed-helper-field",
            floor,
            unrelated
        ));
        let unrelated_bound = rows.iter().find(|b| b.key == unrelated.key).unwrap();
        let mut wrong = bound.clone();
        wrong.relation = unrelated_bound.budget.clone();
        assert_ne!(wrong.relation, bound.relation);
        let mut bad_rows = rows.clone();
        let index = bad_rows.iter().position(|b| b.key == wrong.key).unwrap();
        bad_rows[index] = wrong;
        assert!(validate(&bad_rows).is_err());
        let mut mutated = floor.clone();
        mutated.source = mutated.source.replace(" <= ", " < ");
        assert_ne!(mutated.source, floor.source);
        assert!(!bindings::accepts(
            "reviewed-helper-field",
            &mutated,
            target
        ));
    }
}

#[test]
fn required_runner_timeout_uses_successful_validation_not_a_default() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let target = all
        .iter()
        .find(|c| {
            c.path == "physical_inference_qualification.py"
                && c.line == 785
                && c.kind == "validated-field"
        })
        .expect("required timeout has an explicit validation witness");
    for line in [795, 864] {
        let c = all
            .iter()
            .find(|c| c.path == target.path && c.line == line && c.kind != "validated-field")
            .unwrap();
        assert!(bindings::accepts("reviewed-helper-field", c, target));
        let b = rows.iter().find(|b| b.key == c.key).unwrap();
        assert_eq!(b.unit, "milliseconds");
        assert!(b.value.is_none());
        assert_eq!(b.disposition, "derived");
        let mut wrong = target.clone();
        wrong.scope.push_str(".unchecked");
        assert!(!bindings::accepts("reviewed-helper-field", c, &wrong));
    }
    assert_eq!(validate(&rows), Ok(()));
}
