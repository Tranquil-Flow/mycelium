// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:scripts/run_a10_physical_gate.py; O:path:scripts/run_a5_benchmark_gate.py.
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

const A10: &str = "scripts/run_a10_physical_gate.py";
const A5: &str = "scripts/run_a5_benchmark_gate.py";

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
    let alias = "resolved-optional-value";
    let clock = "reviewed-clock-offset";
    vec![
        e(
            alias,
            A10,
            1668,
            "assignment",
            "self._timeout_s",
            A10,
            1870,
            "keyword",
            "timeout_s",
        ),
        e(
            "constructor-field",
            A10,
            1674,
            "dict-field",
            "timeout_s",
            A10,
            1668,
            "assignment",
            "self._timeout_s",
        ),
        e(
            clock,
            A5,
            395,
            "assignment",
            "deadline",
            A5,
            889,
            "keyword",
            "timeout",
        ),
        e(
            clock, A5, 514, "keyword", "timeout", A5, 800, "keyword", "deadline",
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
fn c_gate_script_forwards_and_caller_sets_have_complete_identity_witnesses() {
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
                // constructor-field is a structural rule over scope, kind and the
                // source expression/target symbol equality; it does not bind identity
                // fields, so only those mutations are meaningful for it.
                if edge.rule == "constructor-field"
                    && !(matches!(field, 4 | 5 | 7) && is_target || field == 6 && !is_target)
                {
                    continue;
                }
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
fn c_gate_flows_do_not_cover_margins_floors_or_divergent_callers() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    // +30 s socket margin and the arrival-rate scaled collection budget (uncapped above) stay pending; the
    // two differently-budgeted _invoke callers' shared sink (:1401) may only be a forward-callers `+` row.
    for (path, line, kind, symbol) in [
        (A10, 1676usize, "keyword", "timeout"),
        (A10, 1401, "keyword", "timeout"),
        (A5, 856, "keyword", "duration_after_measure"),
    ] {
        let c = find(&all, path, line, kind, symbol);
        // Round 8 (parent decision a): the shared-sink row alone may be closed, and only by the
        // forward-callers `+` relation of both callers' own values.
        assert!(
            rows.iter().filter(|r| r.key == c.key).all(
                |r| line == 1401 && r.review.starts_with("forward-callers:required-parameter;")
            ),
            "{} reviewed",
            c.key
        );
    }
}
