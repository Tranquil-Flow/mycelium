// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_mobile/a12_activation.py; O:path:mycelium_membership/contracts.py; O:path:mycelium_seed/invite_batch.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};

#[test]
fn c_successful_validation_is_not_raw_input_or_an_equal_default() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    for (p, l, tp, tl, rule) in [
        (
            "mycelium_seed/invite_batch.py",
            199,
            "mycelium_seed/invite_batch.py",
            28,
            "validated-required-ceiling",
        ),
        (
            "mycelium_seed/invite_batch.py",
            214,
            "mycelium_seed/invite_batch.py",
            28,
            "reviewed-clock-offset",
        ),
        (
            "mycelium_seed/operator.py",
            468,
            "mycelium_seed/operator.py",
            43,
            "reviewed-clock-offset",
        ),
        (
            "mycelium_seed/operator.py",
            497,
            "mycelium_seed/operator.py",
            43,
            "reviewed-clock-offset",
        ),
        (
            "mycelium_membership/contracts.py",
            873,
            "mycelium_membership/contracts.py",
            30,
            "reviewed-helper-return",
        ),
        (
            "mycelium_mobile/a12_activation.py",
            1254,
            "mycelium_mobile/a12_activation.py",
            141,
            "reviewed-helper-return",
        ),
        (
            "mycelium_mobile/a12_activation.py",
            1346,
            "mycelium_mobile/a12_activation.py",
            141,
            "reviewed-helper-return",
        ),
        (
            "mycelium_mobile/a12_activation.py",
            1407,
            "mycelium_mobile/a12_activation.py",
            141,
            "reviewed-helper-return",
        ),
        (
            "mycelium_mobile/a12_activation.py",
            1493,
            "mycelium_mobile/a12_activation.py",
            141,
            "reviewed-helper-return",
        ),
        (
            "mycelium_mobile/a12_activation.py",
            1645,
            "mycelium_mobile/a12_activation.py",
            141,
            "reviewed-clock-offset",
        ),
        (
            "mycelium_mobile/termux_bridge_server.py",
            173,
            "mycelium_mobile/termux_bridge_server.py",
            74,
            "reviewed-helper-return",
        ),
        (
            "mycelium_mobile/termux_bridge_server.py",
            201,
            "mycelium_mobile/termux_bridge_server.py",
            74,
            "reviewed-helper-return",
        ),
    ] {
        let s = all.iter().find(|r| r.path == p && r.line == l).unwrap();
        let t = all
            .iter()
            .find(|r| r.path == tp && r.line == tl && r.kind != "comparison-lower")
            .unwrap();
        assert!(bindings::accepts(rule, s, t), "{}", s.key);
        for field in 0..9 {
            for target in [false, true] {
                let original = if target { t } else { s };
                let mut b = original.clone();
                match field {
                    0 => b.key.push('!'),
                    1 => b.path.push('!'),
                    2 => b.line += 1,
                    3 => b.column += 1,
                    4 => b.scope.push('!'),
                    5 => b.symbol.push('!'),
                    6 => b.expression.push('!'),
                    7 => b.kind.push('!'),
                    8 => b.source.push('!'),
                    _ => unreachable!(),
                }
                assert_ne!(b, *original);
                assert!(
                    !if target {
                        bindings::accepts(rule, s, &b)
                    } else {
                        bindings::accepts(rule, &b, t)
                    },
                    "{} field {field} target={target}",
                    s.key
                );
            }
        }
        let mut wrong = rows.clone();
        wrong.iter_mut().find(|r| r.key == s.key).unwrap().relation = "RegistryPoll".into();
        assert!(validate(&wrong).is_err(), "{} same-unit wrong-ID", s.key);
    }
}

#[test]
fn c_prevalidation_fields_and_decoder_raw_inputs_stay_unresolved() {
    let all = candidates().unwrap();
    for (p, l, tp, tl) in [
        (
            "mycelium_membership/contracts.py",
            445,
            "mycelium_membership/contracts.py",
            30,
        ),
        (
            "mycelium_mobile/termux_bridge_server.py",
            69,
            "mycelium_mobile/termux_bridge_server.py",
            74,
        ),
        (
            "mycelium_mobile/a12_activation.py",
            851,
            "mycelium_mobile/a12_activation.py",
            141,
        ),
    ] {
        let s = all.iter().find(|r| r.path == p && r.line == l).unwrap();
        let t = all
            .iter()
            .find(|r| r.path == tp && r.line == tl && r.kind != "comparison-lower")
            .unwrap();
        assert!(!bindings::accepts("reviewed-helper-return", s, t));
        assert!(!bindings::accepts("reviewed-clock-offset", s, t));
    }
}
