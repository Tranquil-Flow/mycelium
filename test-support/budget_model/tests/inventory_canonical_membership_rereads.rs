// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_membership/contracts.py; O:path:mycelium_platform_neutral/acceleration_profile.py; O:path:mycelium_platform_neutral/peer_profile.py.
//! Coordinator adjudication of C's retracted proposal: the per-protocol expiry re-reads in
//! `_validate_assignment_offer` (:660) and `_validate_drain_ack` (:724). `validate_membership_message`
//! (:758-773) is their sole dispatch and runs `_validate_common` first, whose :445-451 check bounds
//! `expires_at - issued_at` by `MAX_MESSAGE_TTL_SECONDS`; no other module references either validator.
use budget_model::inventory::{Candidate, bindings, candidates, checked_in, validate};

const CT: &str = "mycelium_membership/contracts.py";
const RULE: &str = "validated-required-ceiling";

fn find<'a>(
    all: &'a [Candidate],
    path: &str,
    line: usize,
    kind: &str,
    symbol: &str,
) -> &'a Candidate {
    let hits: Vec<_> = all
        .iter()
        .filter(|r| r.path == path && r.line == line && r.kind == kind && r.symbol == symbol)
        .collect();
    assert_eq!(hits.len(), 1, "{path}:{line} {kind} {symbol}");
    hits[0]
}

#[test]
fn per_protocol_expiry_rereads_inherit_the_common_ttl_ceiling_exactly() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let t = find(&all, CT, 445, "assignment", "expires_at");
    let target_row = rows.iter().find(|r| r.key == t.key).unwrap();
    for (line, scope, symbol) in [
        (660usize, "_validate_assignment_offer", "expires_at"),
        (724, "_validate_drain_ack", "expires"),
    ] {
        let s = find(&all, CT, line, "assignment", symbol);
        assert_eq!(s.scope, scope);
        assert_eq!(s.expression, "_number(message['expires_at'])");
        assert!(bindings::accepts(RULE, s, t), "{}", s.key);
        let row = rows
            .iter()
            .find(|r| r.key == s.key)
            .expect("reviewed, not pending");
        assert_eq!(row.disposition, "derived", "{}", s.key);
        assert_eq!(
            row.relation, target_row.relation,
            "{} common ceiling",
            s.key
        );
        assert_eq!(
            row.relation,
            "PyMyceliumMembershipContractsMaxMessageTtlSecondsL30"
        );
        assert!(row.review.contains("exact-span-sha256="), "{}", s.key);
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
                assert_ne!(bad, if is_target { t.clone() } else { s.clone() });
                assert!(
                    !if is_target {
                        bindings::accepts(RULE, s, &bad)
                    } else {
                        bindings::accepts(RULE, &bad, t)
                    },
                    "{} field {field} target={is_target}",
                    s.key
                );
            }
        }
        for other in [
            "upper-cap-reference",
            "reviewed-clock-offset",
            "reviewed-local-comparison",
            "constructor-field",
            "resolved-optional-value",
            "argparse-option",
        ] {
            assert!(!bindings::accepts(other, s, t), "{} as {other}", s.key);
        }
        // Same-unit wrong ID: another valid millisecond policy must not satisfy provenance.
        let mut bad = rows.clone();
        let r = bad.iter_mut().find(|r| r.key == s.key).unwrap();
        r.relation = if r.relation == "RegistryPoll" {
            "RegistryPoll2"
        } else {
            "RegistryPoll"
        }
        .into();
        assert!(validate(&bad).is_err(), "{} unrelated same-unit ID", s.key);
        // The lease reads in other validators carry their own lease check, not this target.
        let lease = find(&all, CT, 517, "assignment", "lease");
        assert!(!bindings::accepts(RULE, s, lease));
    }
}

#[test]
fn rereads_do_not_cover_platform_neutral_expiries_without_the_ceiling() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let t = find(&all, CT, 445, "assignment", "expires_at");
    // The from_mapping validators only require expires_at > issued_at: no borrowed ceiling.
    for (path, line) in [
        ("mycelium_platform_neutral/acceleration_profile.py", 156),
        ("mycelium_platform_neutral/peer_profile.py", 336),
    ] {
        let c = all
            .iter()
            .find(|r| r.path == path && r.line == line && r.kind == "assignment")
            .unwrap();
        assert!(rows.iter().all(|r| r.key != c.key), "{} reviewed", c.key);
        assert!(!bindings::accepts(RULE, c, t), "{}", c.key);
    }
}
