// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_capacity_profiles/catalog.py; O:path:runtime_contracts.py.
use budget_model::inventory::{candidates, categories, checked_in, validate};
#[test]
fn partition_a_readouts_and_schema_names_do_not_exempt_policy_consumers() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    for (path, line, category) in [
        (
            "mycelium_capacity_profiles/catalog.py",
            213,
            "reviewed-policy-readout",
        ),
        (
            "mycelium_layer_planner/network_cost.py",
            69,
            "reviewed-policy-readout",
        ),
        ("runtime_contracts.py", 387, "reviewed-structural-slot"),
        (
            "mycelium_a13_macos/managed_control.py",
            1903,
            "reviewed-nonscalar-slot",
        ),
        ("mycelium_m16_runtime.py", 318, "reviewed-observation-slot"),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        assert!(categories::accepts(category, c), "{path}:{line}");
        assert!(
            rows.iter()
                .any(|b| b.key == c.key && b.value.is_none() && b.budget.is_empty())
        );
        for field in 0..9 {
            let mut bad = c.clone();
            match field {
                0 => bad.key.push('!'),
                1 => bad.path.push('!'),
                2 => bad.line += 1,
                3 => bad.column += 1,
                4 => bad.scope.push('!'),
                5 => bad.symbol.push('!'),
                6 => bad.expression.push('!'),
                7 => bad.kind.push('!'),
                8 => bad.source.push('!'),
                _ => unreachable!(),
            }
            assert!(!categories::accepts(category, &bad));
        }
    }
    for (path, line) in [
        ("mycelium_capacity_profiles/catalog.py", 271),
        ("mycelium_a13_macos/worker.py", 540),
        ("mycelium_interactive/swarm.py", 757),
    ] {
        let c = all
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        for category in [
            "reviewed-policy-readout",
            "reviewed-nonscalar-slot",
            "reviewed-observation-slot",
            "reviewed-structural-slot",
        ] {
            assert!(
                !categories::accepts(category, c),
                "{path}:{line} {category}"
            );
        }
    }
    assert_eq!(validate(&rows), Ok(()));
}
