// SPDX-License-Identifier: AGPL-3.0-only
// Obligation: O:path:mycelium_a13_macos/demo_chain.py.
use budget_model::inventory::{candidates, categories, checked_in, validate};
#[test]
fn demo_profile_keeps_typed_defaults_without_a_container_exemption() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let parent = all
        .iter()
        .find(|c| {
            c.path == "mycelium_a13_macos/demo_chain.py"
                && c.line == 259
                && c.symbol == "self.budgets"
        })
        .unwrap();
    // Preserve the inherited refusal: a policy record is not a generic nonscalar exemption.
    assert!(!categories::accepts("reviewed-nonscalar-slot", parent));
    let expected = [
        ("model", 5000),
        ("descriptors", 180000),
        ("download_stall", 120000),
        ("report_silence", 30000),
        ("member_silence", 300000),
        ("verify_stall", 600000),
        ("verify_rate", 25000000),
        ("descriptor_margin", 60000),
        ("bootstrap", 180000),
        ("collect", 180000),
        ("cleanup", 120000),
        ("compile", 3600000),
        ("slices", 7200000),
        ("publish", 600000),
        ("targets", 280000),
        ("serving", 900000),
        ("member_restart_grace", 15000),
        ("rollover_margin", 120000),
        ("previous_close", 120000),
    ];
    for (symbol, value) in expected {
        let c = all
            .iter()
            .find(|c| {
                c.path == parent.path
                    && c.scope == parent.scope
                    && c.kind == "dict-field"
                    && c.symbol == symbol
            })
            .unwrap();
        assert!(!categories::accepts("reviewed-nonscalar-slot", c));
        let r = rows.iter().find(|r| r.key == c.key).unwrap();
        assert_eq!(r.value, Some(value));
        assert!(!r.budget.is_empty());
        assert_eq!(
            r.unit,
            if symbol == "verify_rate" {
                "bytes-per-second"
            } else {
                "milliseconds"
            }
        );
    }
    let mut bad = parent.clone();
    bad.expression = bad.expression.replace("**(budgets or {})", "**{} ");
    assert_ne!(bad.expression, parent.expression);
    assert!(!categories::accepts("reviewed-nonscalar-slot", &bad));
    assert_eq!(validate(&rows), Ok(()));
}
