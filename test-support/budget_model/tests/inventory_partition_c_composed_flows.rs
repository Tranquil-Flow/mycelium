// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_seed/phone_preparation.py; O:path:mycelium_a6_recovery.py; O:path:mycelium_swarm_directory/watchdog.py.
use budget_model::inventory::{bindings, candidates, checked_in, validate};

#[test]
fn c_composed_source_flows_have_complete_identity_witnesses() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    for (path, line, tp, tl, rule) in [
        (
            "mycelium_a6_product_fault_seams.py",
            289,
            "mycelium_a6_product_fault_seams.py",
            207,
            "reviewed-clock-offset",
        ),
        (
            "mycelium_a6_recovery.py",
            1670,
            "mycelium_a6_recovery.py",
            82,
            "reviewed-local-comparison",
        ),
        (
            "mycelium_a7_physical_measurement.py",
            406,
            "mycelium_a7_physical_measurement.py",
            348,
            "constructor-field",
        ),
        (
            "mycelium_internet/physical.py",
            2623,
            "mycelium_internet/physical.py",
            2612,
            "validated-required-ceiling",
        ),
        (
            "mycelium_seed/coordinator.py",
            915,
            "mycelium_seed/coordinator.py",
            256,
            "reviewed-local-comparison",
        ),
        (
            "mycelium_seed/coordinator.py",
            456,
            "mycelium_membership/contracts.py",
            30,
            "reviewed-helper-return",
        ),
        (
            "mycelium_seed/pairing.py",
            178,
            "mycelium_seed/pairing.py",
            173,
            "reviewed-clock-offset",
        ),
        (
            "mycelium_seed/phone_preparation.py",
            184,
            "mycelium_seed/phone_preparation.py",
            176,
            "upper-cap-reference",
        ),
        (
            "mycelium_seed/phone_preparation.py",
            202,
            "mycelium_seed/phone_preparation.py",
            176,
            "upper-cap-reference",
        ),
        (
            "scripts/qualify_a5_replica_track.py",
            309,
            "mycelium_deadlines.py",
            188,
            "reviewed-clock-offset",
        ),
        (
            "scripts/pilot_readiness_gate.py",
            285,
            "scripts/pilot_readiness_gate.py",
            241,
            "resolved-optional-value",
        ),
        (
            "mycelium_swarm_directory/watchdog.py",
            44,
            "mycelium_swarm_directory/watchdog.py",
            42,
            "resolved-optional-value",
        ),
        (
            "mycelium_swarm_directory/watchdog.py",
            51,
            "mycelium_swarm_directory/watchdog.py",
            42,
            "constructor-field",
        ),
    ] {
        let s = all
            .iter()
            .find(|r| r.path == path && r.line == line)
            .unwrap();
        let t = all
            .iter()
            .find(|r| r.path == tp && r.line == tl && (tl != 42 || r.symbol == "timeout"))
            .unwrap();
        assert!(bindings::accepts(rule, s, t), "{}", s.key);
        let row = rows.iter().find(|r| r.key == s.key).unwrap();
        assert_eq!(row.disposition, "derived");
        for field in 0..9 {
            for is_target in [false, true] {
                let mut bad = if is_target { t.clone() } else { s.clone() };
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
                assert!(
                    !if is_target {
                        bindings::accepts(rule, s, &bad)
                    } else {
                        bindings::accepts(rule, &bad, t)
                    },
                    "{} field {field} target={is_target}",
                    s.key
                );
            }
        }
        let mut bad = rows.clone();
        bad.iter_mut().find(|r| r.key == s.key).unwrap().relation = "RegistryPoll".into();
        assert!(validate(&bad).is_err(), "{} unrelated same-unit ID", s.key);
    }
}
