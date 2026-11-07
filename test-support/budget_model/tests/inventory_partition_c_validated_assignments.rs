// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_seed/coordinator.py; O:path:mycelium_seed/route_assignment.py; O:path:scripts/qwen_phone_builder.py; O:path:mycelium_mobile/termux_bridge_server.py; O:path:mycelium_swarm_directory/docs.py.
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

const SC: &str = "mycelium_seed/coordinator.py";
const MC: &str = "mycelium_membership/contracts.py";
const RA: &str = "mycelium_seed/route_assignment.py";
const QP: &str = "scripts/qwen_phone_builder.py";
const TB: &str = "mycelium_mobile/termux_bridge_server.py";
const DO: &str = "mycelium_swarm_directory/docs.py";

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
    let helper = "reviewed-helper-return";
    let ceiling = "validated-required-ceiling";
    vec![
        e(
            helper,
            SC,
            447,
            "assignment",
            "deadline",
            MC,
            30,
            "assignment",
            "MAX_MESSAGE_TTL_SECONDS",
        ),
        e(
            helper,
            SC,
            449,
            "assignment",
            "deadline",
            MC,
            30,
            "assignment",
            "MAX_MESSAGE_TTL_SECONDS",
        ),
        e(
            ceiling,
            RA,
            651,
            "assignment",
            "(issued_at, expires_at)",
            RA,
            658,
            "comparison",
            "0",
        ),
        e(
            ceiling,
            QP,
            120,
            "assignment",
            "(issued, expires)",
            QP,
            125,
            "comparison",
            "0",
        ),
        e(
            ceiling,
            TB,
            69,
            "assignment",
            "timeout",
            TB,
            74,
            "comparison",
            "0.1",
        ),
        e(
            ceiling,
            DO,
            282,
            "assignment",
            "(issued, expires)",
            DO,
            42,
            "assignment",
            "MAX_GRANT_SECONDS",
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
fn c_seed_deadline_helper_and_validated_assignments_have_complete_identity_witnesses() {
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
fn c_seed_deadline_helper_does_not_bind_requested_ttl_or_previous_authority() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    // The helper's ceiling is MAX_MESSAGE_TTL_SECONDS, not the sibling clock-skew
    // declaration that lives beside it, and not a different seed deadline site.
    let s = find(&all, SC, 447, "assignment", "deadline");
    let skew = find(&all, MC, 31, "assignment", "MAX_CLOCK_SKEW_SECONDS");
    assert!(!bindings::accepts("reviewed-helper-return", s, skew));
    // Raw expiries and persisted lease reads that no ceiling guards stay pending.
    for (path, line, kind, symbol) in [
        (SC, 451usize, "assignment", "deadline"),
        (SC, 1385, "keyword", "previous_deadline"),
        (SC, 280, "keyword", "lease_expires_at"),
        (SC, 480, "keyword", "ttl_seconds"),
        (
            "mycelium_seed/state.py",
            827,
            "dict-field",
            "lease_expires_at",
        ),
        ("mycelium_seed/pairing.py", 274, "assignment", "ttl"),
    ] {
        let c = find(&all, path, line, kind, symbol);
        assert!(rows.iter().all(|r| r.key != c.key), "{} reviewed", c.key);
    }
}
