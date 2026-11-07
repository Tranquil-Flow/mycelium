// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:physical_inference_node.py; O:path:mycelium_a13_macos/managed_provisioning.py; O:path:mycelium_node/__main__.py.
use budget_model::inventory::{candidates, categories, checked_in, validate};
#[test]
fn serialized_policy_callback_handles_and_flags_do_not_waive_scalar_consumption() {
    let cs = candidates().unwrap();
    let rows = checked_in().unwrap();
    for (path, line, category) in [
        (
            "physical_inference_node.py",
            2676,
            "reviewed-nonscalar-slot",
        ),
        (
            "mycelium_a13_macos/managed_provisioning.py",
            684,
            "reviewed-policy-readout",
        ),
        ("mycelium_node/__main__.py", 848, "reviewed-policy-readout"),
        (
            "mycelium_service_package.py",
            662,
            "reviewed-structural-slot",
        ),
        (
            "mycelium_candidate_promotion.py",
            129,
            "reviewed-observation-slot",
        ),
    ] {
        let c = cs
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        assert!(categories::accepts(category, c), "{}", c.key);
        let row = rows.iter().find(|r| r.key == c.key).unwrap();
        assert_eq!(row.disposition, "not-a-bound");
        assert!(row.budget.is_empty() && row.value.is_none());
        for i in 0..9 {
            let mut bad = c.clone();
            match i {
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
        ("physical_inference_node.py", 2743),
        ("physical_inference_node.py", 6758),
        ("mycelium_a10_batch_policy_control.py", 120),
        ("mycelium_a13_macos/demo_chain.py", 259),
    ] {
        let c = cs
            .iter()
            .find(|c| c.path == path && c.line == line)
            .unwrap();
        for category in [
            "reviewed-nonscalar-slot",
            "reviewed-policy-readout",
            "reviewed-observation-slot",
        ] {
            assert!(!categories::accepts(category, c), "{}", c.key);
        }
    }
    assert_eq!(validate(&rows), Ok(()));
}
