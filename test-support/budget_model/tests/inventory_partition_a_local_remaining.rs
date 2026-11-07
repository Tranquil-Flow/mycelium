// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a6_direct_socket.py; O:path:mycelium_a6_http_fault_control.py.
use budget_model::inventory::{Candidate, bindings, candidates, categories, checked_in, validate};
use std::collections::BTreeMap;

const RULE: &str = "remaining-monotonic-local";
const SITES: [(&str, &str); 2] = [
    (
        "mycelium_a6_direct_socket.py:77:12:positional-call:sock.settimeout",
        "mycelium_a6_direct_socket.py:71:8:assignment:deadline",
    ),
    (
        "mycelium_a6_http_fault_control.py:150:64:keyword:timeout",
        "mycelium_a6_http_fault_control.py:144:12:assignment:deadline",
    ),
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
fn a_local_remaining_slices_follow_only_their_exact_reviewed_deadline() {
    let all = all();
    let rows = checked_in().unwrap();
    for (key, target) in SITES {
        let (c, t) = (&all[key], &all[target]);
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
        assert_eq!(bound.unit, "milliseconds");
        assert!(bound.value.is_none() && bound.budget.is_empty());
        let target_bound = rows.iter().find(|r| r.key == target).unwrap();
        let expected = if target_bound.budget.is_empty() {
            &target_bound.relation
        } else {
            &target_bound.budget
        };
        assert_eq!(&bound.relation, expected, "slice inherits its deadline");
        // Same-unit wrong ID: another valid millisecond budget must be refused.
        let mut wrong = rows.clone();
        let b = wrong.iter_mut().find(|r| r.key == key).unwrap();
        let replacement = if b.relation == "RegistryPoll" {
            "PyReturnedMyceliumGatewayEventAdapterL247C48"
        } else {
            "RegistryPoll"
        };
        b.relation = replacement.into();
        assert!(validate(&wrong).is_err(), "wrong policy {key}");
    }
    assert_eq!(validate(&rows), Ok(()));
}

#[test]
fn a_local_remaining_family_does_not_reach_swapped_or_unrelated_waits() {
    let all = all();
    // Swapping the two reviewed pairs must fail: each witness is path-exact.
    let [(s0, t0), (s1, t1)] = SITES;
    assert!(!bindings::accepts(RULE, &all[s0], &all[t1]));
    assert!(!bindings::accepts(RULE, &all[s1], &all[t0]));
    // Other `remaining`-named wait arguments have no reviewed witness: a deadline
    // parameter, a field read or an unreviewed wakeup loop are not this family.
    for (key, deadline) in [
        (
            "mycelium_node/process.py:422:35:keyword:timeout",
            "mycelium_node/process.py:2954:24:keyword:timeout",
        ),
        (
            "mycelium_a13_macos/managed_control.py:1371:16:positional-call:self._delivery_wakeup.wait",
            "mycelium_a13_macos/managed_control.py:1364:8:assignment:deadline",
        ),
        (
            "mycelium_a13_macos/worker.py:793:62:keyword:timeout",
            "mycelium_a13_macos/worker.py:592:12:assignment:options['timeout_seconds']",
        ),
    ] {
        assert!(
            !bindings::accepts(RULE, &all[key], &all[deadline]),
            "unreviewed wait {key}"
        );
        assert!(!categories::accepts("reviewed-structural-slot", &all[key]));
        assert!(!categories::accepts("reviewed-observation-slot", &all[key]));
    }
    // Same shape but another file's reviewed deadline cannot stand in.
    assert!(!bindings::accepts(
        RULE,
        &all[s0],
        &all["mycelium_demo/public_origin_proxy.py:708:12:assignment:deadline"]
    ));
}
