// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:scripts/a13_operator/round14_rpc.py; O:path:mycelium_seed/coordinator.py.
use budget_model::inventory::{Candidate, bindings, candidates, checked_in, validate};

fn at(all: &[Candidate], path: &str, line: usize, symbol: &str) -> Candidate {
    all.iter()
        .find(|r| r.path == path && r.line == line && r.symbol == symbol)
        .unwrap()
        .clone()
}

#[test]
fn c_helper_defaults_and_return_ceilings_have_exact_distinct_witnesses() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let rpc = "scripts/a13_operator/round14_rpc.py";
    let seed = "mycelium_seed/coordinator.py";
    let mut cases = Vec::new();
    for (line, symbol, target_line, target_symbol, rule) in [
        (
            210,
            "deadline",
            168,
            "_READINESS_BUDGET_S",
            "helper-default-clock",
        ),
        (
            361,
            "deadline",
            168,
            "_READINESS_BUDGET_S",
            "helper-default-clock",
        ),
        (
            482,
            "deadline",
            168,
            "_READINESS_BUDGET_S",
            "helper-default-clock",
        ),
        (211, "retry", 169, "_READINESS_RETRY_S", "helper-default"),
        (361, "retry", 169, "_READINESS_RETRY_S", "helper-default"),
        (482, "retry", 169, "_READINESS_RETRY_S", "helper-default"),
        (
            212,
            "grace",
            115,
            "_OWNER_STARTUP_GRACE_S",
            "conditional-clock",
        ),
        (
            519,
            "grace",
            115,
            "_OWNER_STARTUP_GRACE_S",
            "conditional-clock",
        ),
    ] {
        cases.push((
            at(&all, rpc, line, symbol),
            at(&all, rpc, target_line, target_symbol),
            rule,
        ));
    }
    for (line, symbol) in [
        (498, "expires_at"),
        (639, "lease_expires_at"),
        (673, "expires_at"),
        (833, "lease_expires_at"),
        (867, "expires_at"),
        (1400, "expires_at"),
        (1404, "lease_expires_at"),
        (1412, "persisted_member['lease_expires_at']"),
        (1610, "expires_at"),
    ] {
        cases.push((
            at(&all, seed, line, symbol),
            at(
                &all,
                "mycelium_membership/contracts.py",
                30,
                "MAX_MESSAGE_TTL_SECONDS",
            ),
            "seed-return-ceiling",
        ));
    }
    for (source, target, rule) in cases {
        let rule = match rule {
            "helper-default" => "resolved-optional-value",
            "helper-default-clock" | "conditional-clock" => "reviewed-clock-offset",
            "seed-return-ceiling" => "reviewed-helper-return",
            _ => unreachable!(),
        };
        assert!(bindings::accepts(rule, &source, &target), "{}", source.key);
        let reviewed = rows.iter().find(|r| r.key == source.key).unwrap();
        assert_eq!(reviewed.disposition, "derived");
        assert!(reviewed.value.is_none());
        assert_eq!(reviewed.unit, "milliseconds");
        for field in 0..9 {
            let mutate = |r: &Candidate| {
                let mut bad = r.clone();
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
                assert_ne!(bad, *r);
                bad
            };
            assert!(
                !bindings::accepts(rule, &mutate(&source), &target),
                "{} source {field}",
                source.key
            );
            assert!(
                !bindings::accepts(rule, &source, &mutate(&target)),
                "{} target {field}",
                source.key
            );
        }
        let mut wrong = rows.clone();
        wrong
            .iter_mut()
            .find(|r| r.key == source.key)
            .unwrap()
            .relation = "RegistryPoll".into();
        assert!(
            validate(&wrong).is_err(),
            "{} wrong same-unit ID",
            source.key
        );
    }
}

#[test]
fn c_raw_expiries_and_unvalidated_intermediate_are_not_helper_returns() {
    let all = candidates().unwrap();
    let ceiling = at(
        &all,
        "mycelium_membership/contracts.py",
        30,
        "MAX_MESSAGE_TTL_SECONDS",
    );
    for (line, symbol) in [
        (280, "lease_expires_at"),
        (451, "deadline"),
        (1310, "request_expiry"),
        (1385, "previous_deadline"),
        (1243, "member.lease_expires_at"),
    ] {
        let source = at(&all, "mycelium_seed/coordinator.py", line, symbol);
        assert!(!bindings::accepts(
            "reviewed-helper-return",
            &source,
            &ceiling
        ));
    }
    let source = at(&all, "scripts/a13_operator/round14_rpc.py", 211, "retry");
    assert!(!bindings::accepts(
        "resolved-optional-value",
        &source,
        &ceiling
    ));
}
