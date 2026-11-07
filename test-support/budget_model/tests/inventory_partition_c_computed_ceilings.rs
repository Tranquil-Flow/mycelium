// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:scripts/a13_operator/round14_rpc.py; O:path:mycelium_seed/coordinator.py; O:path:mycelium_seed/phone_preparation.py.
use budget_model::inventory::{candidates, categories, checked_in, validate};

#[test]
fn c_static_computed_ceilings_are_distinct_from_defaults_and_epochs() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    for (path, line, mode, unit, value) in [
        (
            "scripts/a13_operator/round14_bootstrap_compile.py",
            118,
            "loop-sleep-ceiling",
            "milliseconds",
            15000,
        ),
        (
            "scripts/a13_operator/round14_bootstrap_compile.py",
            144,
            "loop-sleep-ceiling",
            "milliseconds",
            10000,
        ),
        (
            "scripts/a13_operator/round14_rpc.py",
            642,
            "loop-sleep-ceiling",
            "milliseconds",
            15000,
        ),
        (
            "mycelium_seed/coordinator.py",
            1239,
            "conditional-count-ceiling",
            "entries",
            2,
        ),
        (
            "mycelium_seed/coordinator.py",
            1414,
            "conditional-count-ceiling",
            "entries",
            2,
        ),
        (
            "mycelium_seed/pairing.py",
            128,
            "positional-validation-upper",
            "bytes",
            1048576,
        ),
        (
            "mycelium_seed/phone_preparation.py",
            136,
            "minimum-clock-offset",
            "milliseconds",
            600000,
        ),
        (
            "mycelium_seed/phone_preparation.py",
            176,
            "minimum-clock-offset",
            "milliseconds",
            600000,
        ),
    ] {
        let c = all
            .iter()
            .find(|r| r.path == path && r.line == line)
            .unwrap();
        assert_eq!(
            categories::reviewed_quantity(c, mode),
            Some((unit, value)),
            "{}",
            c.key
        );
        let row = rows.iter().find(|r| r.key == c.key).unwrap();
        assert_eq!(row.unit, unit);
        assert_eq!(row.value, Some(value));
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
            assert_ne!(bad, *c);
            assert_eq!(
                categories::reviewed_quantity(&bad, mode),
                None,
                "{} field {field}",
                c.key
            );
        }
        let wrong = rows
            .iter()
            .find(|r| !r.budget.is_empty() && r.unit == unit && r.budget != row.budget)
            .unwrap();
        let mut bad = rows.clone();
        bad.iter_mut()
            .find(|r| r.key == c.key)
            .unwrap()
            .budget
            .clone_from(&wrong.budget);
        assert!(validate(&bad).is_err(), "{} same-unit wrong ID", c.key);
    }
}

#[test]
fn c_raw_renewal_and_unreviewed_delay_are_not_finite_ceiling_proofs() {
    let all = candidates().unwrap();
    for (path, line) in [
        ("mycelium_seed/coordinator.py", 451),
        ("scripts/run_a10_physical_gate.py", 838),
    ] {
        let c = all
            .iter()
            .find(|r| r.path == path && r.line == line)
            .unwrap();
        for mode in [
            "conditional-count-ceiling",
            "loop-sleep-ceiling",
            "minimum-clock-offset",
            "positional-validation-upper",
        ] {
            assert_eq!(categories::reviewed_quantity(c, mode), None);
        }
    }
}
