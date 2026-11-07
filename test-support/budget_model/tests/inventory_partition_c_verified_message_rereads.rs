// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_membership/contracts.py; O:path:mycelium_seed/coordinator.py.
use budget_model::inventory::{Candidate, bindings, candidates, checked_in, validate};

struct Edge {
    rule: &'static str,
    path: &'static str,
    line: usize,
    kind: &'static str,
    symbol: &'static str,
}

const CT: &str = "mycelium_membership/contracts.py";
const CO: &str = "mycelium_seed/coordinator.py";

fn edges() -> Vec<Edge> {
    let e = |path, line, kind, symbol| Edge {
        rule: "validated-required-ceiling",
        path,
        line,
        kind,
        symbol,
    };
    vec![
        e(CO, 1185, "keyword", "expires_at"),
        e(CO, 1433, "keyword", "heartbeat_expires_at"),
    ]
}

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
fn c_verified_message_rereads_have_complete_identity_witnesses() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let t = find(&all, CT, 445, "assignment", "expires_at");
    for edge in edges() {
        let s = find(&all, edge.path, edge.line, edge.kind, edge.symbol);
        assert!(bindings::accepts(edge.rule, s, t), "{}", s.key);
        let row = rows.iter().find(|r| r.key == s.key).unwrap();
        assert_eq!(row.disposition, "derived", "{}", s.key);
        assert_eq!(
            row.relation,
            rows.iter().find(|r| r.key == t.key).unwrap().relation,
            "{} carries the common ceiling relation",
            s.key
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
                assert!(
                    !if is_target {
                        bindings::accepts(edge.rule, s, &bad)
                    } else {
                        bindings::accepts(edge.rule, &bad, t)
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
        let mut bad = rows.clone();
        let row = bad.iter_mut().find(|r| r.key == s.key).unwrap();
        row.relation = if row.relation == "RegistryPoll" {
            "RegistryPoll2".into()
        } else {
            "RegistryPoll".into()
        };
        assert!(validate(&bad).is_err(), "{} unrelated same-unit ID", s.key);
    }
}

#[test]
fn c_verified_message_rereads_do_not_cover_unverified_or_unrelated_expiries() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    let t = find(&all, CT, 445, "assignment", "expires_at");
    // Invite expiries (no lifetime ceiling), the untrusted pre-verification claim, the
    // persisted-state loads and the max() against a retained lease stay unreviewed.
    for (path, line, kind, symbol) in [
        (CO, 690usize, "keyword", "invite_expires_at"),
        (CO, 1310, "assignment", "request_expiry"),
        (CO, 280, "keyword", "lease_expires_at"),
        (CO, 1243, "assignment", "member.lease_expires_at"),
    ] {
        let c = find(&all, path, line, kind, symbol);
        assert!(rows.iter().all(|r| r.key != c.key), "{} reviewed", c.key);
        assert!(
            !bindings::accepts("validated-required-ceiling", c, t),
            "{}",
            c.key
        );
    }
    // Each exact row only accepts its own validator: another message validator's lease read
    // or a sibling expiry target is not interchangeable.
    let lease = find(&all, CT, 517, "assignment", "lease");
    let s = find(&all, CO, 1433, "keyword", "heartbeat_expires_at");
    assert!(!bindings::accepts("validated-required-ceiling", s, lease));
    // The per-protocol re-reads (660, 724) were pinned unreviewed here; the coordinator
    // adjudicated them (sole dispatch runs `_validate_common` first) into exact reviewed
    // records, pinned by inventory_canonical_membership_rereads.
    for line in [660usize, 724] {
        let c = all
            .iter()
            .find(|r| r.path == CT && r.line == line && r.kind == "assignment")
            .unwrap();
        let row = rows
            .iter()
            .find(|r| r.key == c.key)
            .expect("exact reviewed record");
        assert_eq!(row.disposition, "derived", "{}", c.key);
        assert!(
            bindings::accepts("validated-required-ceiling", c, t),
            "{}",
            c.key
        );
    }
}
