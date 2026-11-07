// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_router/terminal_kv_retention.py; O:path:mycelium_a7_node_wiring.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};
#[test]
fn terminal_expiry_uses_the_retained_deadline_not_a_duration_or_disabled_zero() {
    let all = candidates().unwrap();
    let source = all
        .iter()
        .find(|r| {
            r.path == "mycelium_router/terminal_kv_retention.py"
                && r.line == 106
                && r.kind == "comparison"
        })
        .unwrap();
    let writer = all
        .iter()
        .find(|r| r.path == source.path && r.line == 79)
        .unwrap();
    assert!(bindings::accepts("retained-kv-expiry", source, writer));
    for expression in [
        "float(now) > deadline",
        "float(now) <= deadline",
        "float(now) >= self.seconds",
    ] {
        let mut bad = source.clone();
        bad.expression = expression.into();
        assert!(!bindings::accepts("retained-kv-expiry", &bad, writer));
    }
    for line in [36, 44] {
        let wrong = all
            .iter()
            .find(|r| r.path == source.path && r.line == line)
            .unwrap();
        assert!(!bindings::accepts("retained-kv-expiry", source, wrong));
    }
    let mut wrong_writer = writer.clone();
    wrong_writer.expression = "float(now) + self.seconds".into();
    assert!(!bindings::accepts(
        "retained-kv-expiry",
        source,
        &wrong_writer
    ));
    let rows = checked_in().unwrap();
    let reviewed = rows.iter().find(|r| r.key == source.key).unwrap();
    assert_eq!(reviewed.disposition, "derived");
    assert!(reviewed.value.is_none());
    assert_eq!(validate(&rows), Ok(()));
}
#[test]
fn all_terminal_policy_witnesses_are_exact_on_both_sides() {
    let all = candidates().unwrap();
    let mut count = 0;
    for line in include_str!("../data/python-terminal-policy-edges.tsv")
        .lines()
        .skip(1)
    {
        let f: Vec<_> = line.split('\t').collect();
        assert_eq!(f.len(), 20);
        let source = all.iter().find(|r| r.key == f[0]).unwrap();
        let target = all.iter().find(|r| r.key == f[9]).unwrap();
        assert!(bindings::accepts(f[18], source, target));
        count += 1;
        for field in 0..9 {
            let mut bad = target.clone();
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
            assert!(!bindings::accepts(f[18], source, &bad));
            let mut bad = source.clone();
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
            assert!(!bindings::accepts(f[18], &bad, target));
        }
    }
    assert!(count > 0);
}
