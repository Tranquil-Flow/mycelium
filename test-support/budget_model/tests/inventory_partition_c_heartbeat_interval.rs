// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_seed/coordinator.py.
use budget_model::inventory::{Candidate, bindings, candidates, checked_in, validate};

struct Edge {
    rule: &'static str,
    path: &'static str,
    line: usize,
    kind: &'static str,
    symbol: &'static str,
    target_path: &'static str,
    target_line: usize,
    target_kind: &'static str,
    target_symbol: &'static str,
}

const CO: &str = "mycelium_seed/coordinator.py";

fn edges() -> Vec<Edge> {
    let e =
        |rule, path, line, kind, symbol, target_path, target_line, target_kind, target_symbol| {
            Edge {
                rule,
                path,
                line,
                kind,
                symbol,
                target_path,
                target_line,
                target_kind,
                target_symbol,
            }
        };
    let clock = "reviewed-clock-offset";
    let alias = "resolved-optional-value";
    let ka = "self._keepalive_interval_seconds";
    vec![
        e(
            clock,
            CO,
            1419,
            "assignment",
            "next_heartbeat_due_at",
            CO,
            255,
            "assignment",
            ka,
        ),
        e(
            clock,
            CO,
            1248,
            "assignment",
            "member.next_heartbeat_due_at",
            CO,
            255,
            "assignment",
            ka,
        ),
        e(
            alias,
            CO,
            1422,
            "assignment",
            "persisted_member['next_heartbeat_due_at']",
            CO,
            1419,
            "assignment",
            "next_heartbeat_due_at",
        ),
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
fn c_heartbeat_interval_multiples_have_complete_identity_witnesses() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    for edge in edges() {
        let s = find(&all, edge.path, edge.line, edge.kind, edge.symbol);
        let t = find(
            &all,
            edge.target_path,
            edge.target_line,
            edge.target_kind,
            edge.target_symbol,
        );
        assert!(bindings::accepts(edge.rule, s, t), "{}", s.key);
        let row = rows.iter().find(|r| r.key == s.key).unwrap();
        assert_eq!(row.disposition, "derived", "{}", s.key);
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
            "validated-required-ceiling",
            "argparse-option",
        ] {
            if other != edge.rule {
                assert!(!bindings::accepts(other, s, t), "{} as {other}", s.key);
            }
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
fn c_heartbeat_multiples_do_not_cover_absolute_deadlines_or_lease_ceilings() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    // Absolute stored deadlines and the unvalidated max() intermediate stay pending.
    for (line, kind, symbol) in [
        (451usize, "assignment", "deadline"),
        (1243, "assignment", "member.lease_expires_at"),
        (1385, "keyword", "previous_deadline"),
    ] {
        let c = find(&all, CO, line, kind, symbol);
        assert!(rows.iter().all(|r| r.key != c.key), "{} reviewed", c.key);
    }
    // The multiple is not a validated lease lifetime: the keepalive interval edge is the
    // only accepted target, not the lease constant.
    let s = find(&all, CO, 1419, "assignment", "next_heartbeat_due_at");
    for (line, kind, symbol) in [(240usize, "assignment", "self._lease_seconds")] {
        if let Some(t) = all
            .iter()
            .find(|c| c.path == CO && c.line == line && c.kind == kind && c.symbol == symbol)
        {
            assert!(!bindings::accepts("reviewed-clock-offset", s, t));
        }
    }
}
