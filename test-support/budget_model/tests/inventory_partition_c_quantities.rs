// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:scripts/a13_operator/round14_rpc.py; O:path:mycelium_product_spine/state.py.
use budget_model::inventory::{candidates, categories, checked_in, validate};

#[test]
fn c_selected_defaults_keep_exact_units_without_claiming_override_ceilings() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let cases = [
        (
            "scripts/a13_operator/round14_rpc.py:80:4:assignment:deadline",
            "environment-clock-default-seconds",
            "milliseconds",
            180000,
        ),
        (
            "scripts/a13_operator/round14_rpc.py:663:8:assignment:deadline",
            "environment-clock-default-seconds",
            "milliseconds",
            180000,
        ),
        (
            "scripts/a13_operator/round14_rpc.py:81:4:assignment:sleep_s",
            "environment-default-seconds",
            "milliseconds",
            2000,
        ),
        (
            "scripts/a13_operator/round14_rpc.py:664:8:assignment:sleep_s",
            "environment-default-seconds",
            "milliseconds",
            2000,
        ),
        (
            "scripts/a13_operator/round15_status.py:45:4:assignment:deadline",
            "argv-default-seconds",
            "milliseconds",
            420000,
        ),
        (
            "scripts/a13_operator/wp8_03_laptop_driver.py:128:8:assignment:deadline",
            "clock-mapping-default-seconds",
            "milliseconds",
            120000,
        ),
        (
            "scripts/a13_operator/wp8_05_soak.py:197:36:keyword:cancel_after_s",
            "optional-seconds",
            "milliseconds",
            2000,
        ),
        (
            "mycelium_demo/gateway_control_runtime.py:923:20:keyword:retry_after",
            "http-retry-after-seconds",
            "milliseconds",
            1000,
        ),
        (
            "mycelium_product_spine/state.py:374:15:comparison:len(retained)",
            "upper-guard",
            "entries",
            1,
        ),
    ];
    for (key, mode, unit, value) in cases {
        let c = all.iter().find(|c| c.key == key).unwrap();
        assert_eq!(
            categories::reviewed_quantity(c, mode),
            Some((unit, value)),
            "{key}"
        );
        let b = rows.iter().find(|b| b.key == key).unwrap();
        assert_eq!(b.value, Some(value));
        assert_eq!(b.unit, unit);
        let mut changed = c.clone();
        changed.expression = "float(os.environ.get('UNREVIEWED', '180'))".into();
        assert_eq!(categories::reviewed_quantity(&changed, mode), None);
        changed = c.clone();
        changed.scope = "unreviewed_scope".into();
        assert_eq!(categories::reviewed_quantity(&changed, mode), None);
        let id = budgets::BudgetId::from_name(&b.budget).unwrap();
        let other = budgets::table::BUDGET_TABLE
            .iter()
            .find(|e| e.value == id.entry().value && e.provenance != id.entry().provenance)
            .unwrap();
        let mut mutated = rows.clone();
        mutated.iter_mut().find(|r| r.key == key).unwrap().budget = other.id.name().into();
        assert!(
            validate(&mutated).is_err(),
            "{key}: wrong same-unit same-value ID"
        );
    }
    for c in all.iter().filter(|c| {
        c.path == "scripts/a13_operator/round14_rpc.py" && [210, 212, 519].contains(&c.line)
    }) {
        assert_eq!(
            categories::reviewed_quantity(c, "environment-clock-default-seconds"),
            None
        );
    }
}
