// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_node/process.py.
use budget_model::inventory::{Candidate, bindings, candidates, categories, checked_in, validate};
use std::collections::BTreeMap;

const RULE: &str = "comparison-reference-constructor-field";
const TARGET: &str = "mycelium_node/process.py:2001:8:assignment:self.max_frame_bytes";
const SITES: [&str; 2] = [
    "mycelium_node/process.py:2647:19:comparison:len(line)",
    "mycelium_node/process.py:2667:23:comparison:len(raw)",
];

fn mutate(c: &Candidate, field: usize) -> Candidate {
    let mut m = c.clone();
    match field {
        0 => m.key.push('!'),
        1 => m.path.push('!'),
        2 => m.line += 1,
        3 => m.column += 1,
        4 => m.scope.push('!'),
        5 => m.symbol.push('!'),
        6 => m.expression.push('!'),
        7 => m.kind.push('!'),
        8 => m.source.push('!'),
        _ => unreachable!(),
    }
    assert_ne!(m, *c);
    m
}

fn all() -> BTreeMap<String, Candidate> {
    candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect()
}

#[test]
fn a_frame_sentinel_comparisons_reference_the_exact_constructor_ceiling() {
    let all = all();
    let rows = checked_in().unwrap();
    let t = &all[TARGET];
    let target_bound = rows.iter().find(|r| r.key == TARGET).unwrap();
    for key in SITES {
        let c = &all[key];
        assert!(bindings::accepts(RULE, c, t), "missing reviewed edge {key}");
        for field in 0..9 {
            assert!(
                !bindings::accepts(RULE, &mutate(c, field), t),
                "source {key} {field}"
            );
            assert!(
                !bindings::accepts(RULE, c, &mutate(t, field)),
                "target {key} {field}"
            );
        }
        let bound = rows.iter().find(|r| r.key == key).expect("accepted review");
        assert_eq!(bound.disposition, "derived");
        assert_eq!(bound.unit, "bytes");
        assert!(bound.value.is_none() && bound.budget.is_empty());
        assert_eq!(bound.relation, target_bound.relation);
        // Same-unit wrong ID: another valid byte budget must be refused.
        let mut wrong = rows.clone();
        let b = wrong.iter_mut().find(|r| r.key == key).unwrap();
        let replacement = if b.relation == "PyThresholdPhysicalInferenceNodeL883C0" {
            "PyThresholdMyceliumRouterTransportsIrohL68C0"
        } else {
            "PyThresholdPhysicalInferenceNodeL883C0"
        };
        b.relation = replacement.into();
        assert!(validate(&wrong).is_err(), "wrong policy {key}");
    }
    assert_eq!(validate(&rows), Ok(()));
}

#[test]
fn a_frame_sentinel_family_does_not_reach_other_comparisons_or_fields() {
    let all = all();
    let t = &all[TARGET];
    // Other plus-one comparisons and unrelated bounded predicates have no witness.
    for key in [
        "mycelium_swarm_artifacts.py:1110:11:comparison:serving_reserve",
        "mycelium_a13_macos/app.py:1563:27:comparison:0",
        "mycelium_a13_macos/capability.py:166:38:comparison:1024",
    ] {
        assert!(!bindings::accepts(RULE, &all[key], t), "{key}");
        assert!(!categories::accepts("reviewed-structural-slot", &all[key]));
    }
    // The sentinel witness is attached to its own field: it cannot reference a
    // different self field of the same file.
    let other = all
        .values()
        .find(|c| {
            c.path == "mycelium_node/process.py"
                && c.kind == "assignment"
                && c.symbol.starts_with("self.")
                && c.key != TARGET
                && c.scope.ends_with(".__init__")
        })
        .expect("another constructor field");
    for key in SITES {
        assert!(!bindings::accepts(RULE, &all[key], other), "{key}");
    }
}
