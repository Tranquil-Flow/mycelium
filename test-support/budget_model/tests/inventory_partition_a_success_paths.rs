// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/control_session.py; O:path:physical_inference_qualification.py; O:path:mycelium_a13_macos/clock_offset.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};
#[test]
fn successful_constructor_and_staging_return_ceilings_stay_separate_from_defaults() {
    let cs = candidates().unwrap();
    let rows = checked_in().unwrap();
    for (path, line, ceiling) in [
        ("mycelium_a13_macos/control_session.py", 574, 240000),
        (
            "physical_inference_qualification.py",
            2824,
            6 * 60 * 60 * 1000,
        ),
    ] {
        let c = cs
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        let r = rows
            .iter()
            .find(|r| r.key == c.key)
            .expect("required successful path reviewed");
        let v = cs
            .iter()
            .find(|c| {
                c.path == path
                    && c.kind == "validated-field"
                    && if line == 574 {
                        c.scope == "ManagedNodeSession.__init__"
                    } else {
                        c.scope == "_stage_timeout_seconds"
                    }
            })
            .unwrap();
        assert!(bindings::accepts("reviewed-helper-field", c, v));
        assert!(r.value.is_none());
        assert_eq!(r.unit, "milliseconds");
        assert_eq!(
            budgets::BudgetId::from_name(&r.relation)
                .unwrap()
                .span(budgets::Entries::new(0))
                .unwrap()
                .millis(),
            ceiling
        );
        let mut bad = rows.clone();
        let i = bad.iter().position(|b| b.key == c.key).unwrap();
        bad[i].relation = "PyFinalMyceliumNodeProcessL2963C65CeilingOperand".into();
        assert!(validate(&bad).is_err());
    }
    let c = cs
        .iter()
        .find(|c| c.path == "mycelium_a13_macos/clock_offset.py" && c.line == 52)
        .unwrap();
    let r = rows
        .iter()
        .find(|r| r.key == c.key)
        .expect("default count chain must be tracked");
    assert_eq!(r.unit, "entries");
    assert!(r.value.is_none());
    assert_eq!(
        budgets::BudgetId::from_name(&r.relation)
            .unwrap()
            .count()
            .unwrap()
            .get(),
        8
    );
    assert_eq!(validate(&rows), Ok(()));
}
