// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_seed/pairing.py; O:path:scripts/a13_operator/wp8_05_soak.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};

#[test]
fn c_lexical_and_validated_flows_preserve_defaults_and_real_caps() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    for (path, line, symbol, target_line, target_symbol, rule) in [
        (
            "scripts/a13_operator/wp8_05_measure_stream.py",
            95,
            "timeout",
            62,
            "timeout",
            "resolved-optional-value",
        ),
        (
            "scripts/a13_operator/wp8_05_measure_stream.py",
            108,
            "timeout",
            62,
            "timeout",
            "resolved-optional-value",
        ),
        (
            "scripts/a13_operator/wp8_05_soak.py",
            234,
            "timeout",
            216,
            "timeout",
            "resolved-optional-value",
        ),
        (
            "scripts/run_a10_concurrency_probe.py",
            148,
            "timeout_seconds",
            135,
            "timeout_seconds",
            "resolved-optional-value",
        ),
        (
            "scripts/a13_operator/round14_bootstrap_compile.py",
            211,
            "time.sleep",
            163,
            "REACHABILITY_GATE_INTERVAL_SECONDS",
            "resolved-optional-value",
        ),
        (
            "mycelium_gateway/event_adapter.py",
            255,
            "self._max_qualification_age_ms",
            240,
            "max_qualification_age_ms",
            "validated-parameter-default",
        ),
        (
            "mycelium_gateway/event_adapter.py",
            258,
            "self._max_session_idle_ms",
            241,
            "max_session_idle_ms",
            "validated-parameter-default",
        ),
        (
            "mycelium_seed/pairing.py",
            126,
            "self._window_seconds",
            43,
            "MAX_WINDOW_SECONDS",
            "reviewed-helper-return",
        ),
        (
            "mycelium_seed/pairing.py",
            173,
            "self.expires_at",
            126,
            "self._window_seconds",
            "reviewed-clock-offset",
        ),
        (
            "mycelium_seed/route_assignment.py",
            771,
            "statement_expires",
            757,
            "statement_expires",
            "reviewed-clock-offset",
        ),
        (
            "mycelium_seed/route_assignment.py",
            790,
            "expires_at",
            757,
            "statement_expires",
            "reviewed-clock-offset",
        ),
        (
            "mycelium_demo/public_origin_proxy.py",
            711,
            "connection.settimeout",
            708,
            "deadline",
            "remaining-monotonic-local",
        ),
        (
            "mycelium_internet/enrollment.py",
            621,
            "time.sleep",
            146,
            "self._backoff_seconds",
            "resolved-optional-value",
        ),
    ] {
        let source = all
            .iter()
            .find(|r| r.path == path && r.line == line && r.symbol == symbol)
            .unwrap();
        let target = all
            .iter()
            .find(|r| r.path == path && r.line == target_line && r.symbol == target_symbol)
            .unwrap();
        assert!(bindings::accepts(rule, source, target), "{}", source.key);
        let b = rows.iter().find(|r| r.key == source.key).unwrap();
        assert_eq!(b.disposition, "derived");
        assert!(b.value.is_none());
        for field in 0..9 {
            for is_target in [false, true] {
                let original = if is_target { target } else { source };
                let mut changed = original.clone();
                match field {
                    0 => changed.key.push('!'),
                    1 => changed.path.push('!'),
                    2 => changed.line += 1,
                    3 => changed.column += 1,
                    4 => changed.scope.push('!'),
                    5 => changed.symbol.push('!'),
                    6 => changed.expression.push('!'),
                    7 => changed.kind.push('!'),
                    8 => changed.source.push('!'),
                    _ => unreachable!(),
                }
                assert_ne!(changed, *original);
                assert!(
                    !if is_target {
                        bindings::accepts(rule, source, &changed)
                    } else {
                        bindings::accepts(rule, &changed, target)
                    },
                    "{} {field} target={is_target}",
                    source.key
                );
            }
        }
        let mut bad = rows.clone();
        bad.iter_mut()
            .find(|r| r.key == source.key)
            .unwrap()
            .relation = "RegistryPoll".into();
        assert!(validate(&bad).is_err(), "{} wrong-ID", source.key);
    }
}

#[test]
fn c_unchecked_input_and_foreign_equal_default_are_not_resolved() {
    let all = candidates().unwrap();
    let source = all
        .iter()
        .find(|r| {
            r.path == "scripts/a13_operator/wp8_05_soak.py"
                && r.line == 196
                && r.symbol == "timeout"
        })
        .unwrap();
    let target = all
        .iter()
        .find(|r| r.path == source.path && r.line == 216 && r.symbol == "timeout")
        .unwrap();
    assert!(!bindings::accepts(
        "resolved-optional-value",
        source,
        target
    ));
    let p = "mycelium_gateway/event_adapter.py";
    let source = all.iter().find(|r| r.path == p && r.line == 255).unwrap();
    let wrong = all.iter().find(|r| r.path == p && r.line == 241).unwrap();
    assert!(!bindings::accepts(
        "validated-parameter-default",
        source,
        wrong
    ));
}
