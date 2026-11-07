// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_gateway/event_adapter.py; O:path:mycelium_product_spine/app.py; O:path:mycelium_seed/route_assignment.py.
use budget_model::inventory::{candidates, categories, checked_in, validate};

#[test]
fn c_guarded_deques_and_immediate_relative_expiries_keep_exact_units() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    for (path, line, mode, unit, value) in [
        (
            "mycelium_gateway/event_adapter.py",
            266,
            "validated-deque-ceiling",
            "entries",
            256,
        ),
        (
            "mycelium_gateway/event_adapter.py",
            307,
            "validated-deque-ceiling",
            "entries",
            256,
        ),
        (
            "mycelium_gateway/event_adapter.py",
            850,
            "validated-deque-ceiling",
            "entries",
            256,
        ),
        (
            "mycelium_product_spine/app.py",
            111,
            "validated-deque-ceiling",
            "entries",
            4096,
        ),
        (
            "mycelium_product_spine/app.py",
            281,
            "validated-deque-ceiling",
            "entries",
            4096,
        ),
        (
            "scripts/run_a10_physical_gate.py",
            619,
            "validated-deque-ceiling",
            "entries",
            4096,
        ),
        (
            "mycelium_seed/coordinator.py",
            1090,
            "immediate-relative-expiry",
            "milliseconds",
            0,
        ),
        (
            "mycelium_seed/coordinator.py",
            1097,
            "immediate-relative-expiry",
            "milliseconds",
            0,
        ),
        (
            "mycelium_product_spine/projector.py",
            56,
            "immediate-relative-expiry",
            "milliseconds",
            0,
        ),
        (
            "mycelium_product_spine/state.py",
            128,
            "immediate-relative-expiry",
            "milliseconds",
            0,
        ),
        (
            "mycelium_seed/operator.py",
            496,
            "immediate-relative-expiry",
            "milliseconds",
            0,
        ),
        (
            "mycelium_seed/route_assignment.py",
            263,
            "comparison-constant-product",
            "bytes",
            8388608,
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
        let b = rows.iter().find(|r| r.key == c.key).unwrap();
        assert_eq!(b.value, Some(value));
        assert_eq!(b.unit, unit);
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
        let other = rows
            .iter()
            .find(|r| !r.budget.is_empty() && r.unit == unit && r.budget != b.budget)
            .unwrap();
        let mut wrong = rows.clone();
        wrong
            .iter_mut()
            .find(|r| r.key == c.key)
            .unwrap()
            .budget
            .clone_from(&other.budget);
        assert!(validate(&wrong).is_err(), "{} wrong same-unit ID", c.key);
    }
}

#[test]
fn c_uncapped_replay_and_raw_lease_do_not_borrow_finite_policies() {
    let all = candidates().unwrap();
    for (p, l) in [
        ("mycelium_gateway/observatory.py", 427),
        ("mycelium_seed/coordinator.py", 280),
    ] {
        let r = all.iter().find(|r| r.path == p && r.line == l).unwrap();
        assert_eq!(
            categories::reviewed_quantity(r, "validated-deque-ceiling"),
            None
        );
        assert_eq!(
            categories::reviewed_quantity(r, "immediate-relative-expiry"),
            None
        );
    }
}
