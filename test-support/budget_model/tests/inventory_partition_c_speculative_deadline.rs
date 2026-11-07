// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_speculative_decode_execution.py; O:path:mycelium_speculative_decode.py.
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

const EX: &str = "mycelium_speculative_decode_execution.py";
const SD: &str = "mycelium_speculative_decode.py";

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
    let alias = "resolved-optional-value";
    let ceiling = "validated-required-ceiling";
    let tuple = "(admitted_ms, clamp_reason, _deadline_evidence)";
    vec![
        e(
            helper,
            EX,
            1299,
            "assignment",
            tuple,
            SD,
            76,
            "assignment",
            "DEADLINE_MAXIMUM_MS",
        ),
        e(
            alias,
            EX,
            1359,
            "keyword",
            "deadline_ms",
            EX,
            1299,
            "assignment",
            tuple,
        ),
        e(
            alias,
            EX,
            1449,
            "keyword",
            "deadline_at",
            EX,
            1299,
            "assignment",
            tuple,
        ),
        e(
            alias,
            EX,
            1462,
            "dict-field",
            "deadline_at",
            EX,
            1299,
            "assignment",
            tuple,
        ),
        e(
            ceiling,
            EX,
            2063,
            "keyword",
            "deadline_ms",
            EX,
            1299,
            "assignment",
            tuple,
        ),
        e(
            ceiling,
            EX,
            2187,
            "keyword",
            "deadline_ms",
            EX,
            2063,
            "keyword",
            "deadline_ms",
        ),
        e(
            ceiling,
            EX,
            2317,
            "keyword",
            "deadline_ms",
            EX,
            2063,
            "keyword",
            "deadline_ms",
        ),
        e(
            ceiling,
            EX,
            2328,
            "keyword",
            "deadline_ms",
            EX,
            2063,
            "keyword",
            "deadline_ms",
        ),
        e(
            ceiling,
            EX,
            2445,
            "keyword",
            "deadline_ms",
            EX,
            2063,
            "keyword",
            "deadline_ms",
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
fn c_speculative_deadline_clamp_flows_have_complete_identity_witnesses() {
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
fn c_speculative_deadline_flows_do_not_cover_the_remaining_deadline_input_or_other_clamps() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    // The minimum is a floor, not the ceiling, and the alias cannot skip the clamp site.
    let s = find(
        &all,
        EX,
        1299,
        "assignment",
        "(admitted_ms, clamp_reason, _deadline_evidence)",
    );
    let floor = find(&all, SD, 75, "assignment", "DEADLINE_MINIMUM_MS");
    assert!(!bindings::accepts("reviewed-helper-return", s, floor));
    let alias = find(&all, EX, 1359, "keyword", "deadline_ms");
    let mx = find(&all, SD, 76, "assignment", "DEADLINE_MAXIMUM_MS");
    assert!(!bindings::accepts("resolved-optional-value", alias, mx));
    // The remaining request deadline is a document-provided input and the p95 observation
    // and transfer values are not budgets this clamp covers.
    for (line, kind, symbol) in [
        (1096usize, "keyword", "remaining_request_deadline_ms"),
        (1150, "dict-field", "transfer_ms"),
        (1361, "assignment", "observed_ms"),
    ] {
        let c = find(&all, EX, line, kind, symbol);
        // Round 9 (RU-023) narrowing: the `transfer_ms` record field is reviewed only as a measured
        // observation, never as a budget this clamp covers.
        let observation = symbol == "transfer_ms";
        assert!(
            rows.iter().all(|r| r.key != c.key
                || (observation && r.review.starts_with("observation-flow:record-field; "))),
            "{} reviewed",
            c.key
        );
    }
}
