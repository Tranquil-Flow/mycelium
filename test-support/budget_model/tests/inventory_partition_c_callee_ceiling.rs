// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:scripts/manage_seed_membership.py; O:path:mycelium_seed/operator.py; O:path:mycelium_a7_physical_measurement.py.
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

const MS: &str = "scripts/manage_seed_membership.py";
const OP: &str = "mycelium_seed/operator.py";
const A7: &str = "mycelium_a7_physical_measurement.py";

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
    vec![
        e(
            "validated-required-ceiling",
            MS,
            81,
            "keyword",
            "overlap_seconds",
            OP,
            43,
            "assignment",
            "_MAX_ROTATION_OVERLAP_SECONDS",
        ),
        e(
            "resolved-optional-value",
            A7,
            677,
            "keyword",
            "timeout",
            A7,
            588,
            "assignment",
            "terminal_timeout_s",
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
fn c_callee_ceiling_and_settings_callers_have_complete_identity_witnesses() {
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
fn c_callee_and_settings_flows_do_not_cover_config_overrides_or_other_fields() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    // Operator-config values and the serve ready/stop timeouts have no ceiling in the module;
    // the other settings fields are separately inventoried, not waived by this edge.
    for (path, line, kind, symbol) in [
        (A7, 1550usize, "assignment", "timeouts"),
        (A7, 1573, "keyword", "ready_timeout_s"),
        (A7, 1482, "keyword", "timeout"),
        (OP, 204, "assignment", "overlap_expires"),
    ] {
        let c = find(&all, path, line, kind, symbol);
        // The ready timeout is reviewed (owner ruling, source-unbounded V03) but never by an edge. Round 12
        // (RU-033): the stop wait `timeout=self._stop_timeout` (:1482) is reviewed ONLY as a propagated
        // source-unbounded row (its sole store forwards the o2 call-site `stop_timeout_s`), never by an edge.
        assert!(
            rows.iter().all(|r| r.key != c.key
                || (line == 1573 && r.disposition == "source-unbounded")
                || (line == 1482
                    && r.disposition == "source-unbounded"
                    && r.review
                        .starts_with("source-unbounded-propagated:self-attribute;"))),
            "{} reviewed by an edge",
            c.key
        );
    }
    let s = find(&all, A7, 677, "keyword", "timeout");
    let other = find(&all, A7, 587, "assignment", "result_timeout_s");
    assert!(!bindings::accepts("resolved-optional-value", s, other));
}
