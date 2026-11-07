// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_product_spine/state.py; O:path:mycelium_product_spine/app.py; O:path:scripts/pilot_readiness_gate.py; O:path:scripts/qualify_a5_replica_track.py; O:path:scripts/run_a5_benchmark_supervisor.py; O:path:scripts/a13_operator/ssh_window_evidence.py.
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

const ST: &str = "mycelium_product_spine/state.py";
const AP: &str = "mycelium_product_spine/app.py";
const PG: &str = "scripts/pilot_readiness_gate.py";
const QA: &str = "scripts/qualify_a5_replica_track.py";
const SV: &str = "scripts/run_a5_benchmark_supervisor.py";
const SW: &str = "scripts/a13_operator/ssh_window_evidence.py";

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
    let ceiling = "validated-required-ceiling";
    let alias = "resolved-optional-value";
    let clock = "reviewed-clock-offset";
    vec![
        e(
            ceiling,
            ST,
            364,
            "assignment",
            "retained",
            AP,
            111,
            "keyword",
            "maxlen",
        ),
        e(
            alias,
            ST,
            376,
            "assignment",
            "retained",
            ST,
            364,
            "assignment",
            "retained",
        ),
        e(
            "argparse-option",
            PG,
            1771,
            "keyword",
            "timeout",
            PG,
            1747,
            "cli-default",
            "--timeout",
        ),
        e(
            clock,
            QA,
            298,
            "assignment",
            "deadline",
            QA,
            493,
            "keyword",
            "timeout_seconds",
        ),
        e(
            alias,
            QA,
            318,
            "dict-field",
            "timeout_seconds",
            QA,
            493,
            "keyword",
            "timeout_seconds",
        ),
        e(
            alias,
            SV,
            567,
            "assignment",
            "deadline",
            SV,
            561,
            "assignment",
            "termination_deadline[0]",
        ),
        e(
            alias,
            SW,
            457,
            "keyword",
            "timeout",
            SW,
            580,
            "parameter",
            "timeout",
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
fn c_retention_cast_caller_and_cell_flows_have_complete_identity_witnesses() {
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
fn c_closures_do_not_cover_neighbouring_unreviewed_sites_or_other_callers() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    // The second caller keyword is a distinct witness target: the closure is pinned to
    // the exact reviewed pair, and a sibling timeout (guard upper bound) is not accepted.
    let s = find(&all, QA, 298, "assignment", "deadline");
    let guard = find(&all, QA, 615, "comparison", "1.0");
    assert!(!bindings::accepts("reviewed-clock-offset", s, guard));
    // The drop-oldest slice literal, the unvalidated lifetime deadline and the
    // unguarded previous-deadline max stay pending.
    for (path, line, kind, symbol) in [
        (ST, 376usize, "slice", "retained"),
        (SV, 512, "assignment", "lifetime_deadline"),
        (SV, 175, "dict-field", "maximum_lifetime_seconds"),
        (
            "mycelium_seed/coordinator.py",
            451,
            "assignment",
            "deadline",
        ),
    ] {
        let c = find(&all, path, line, kind, symbol);
        assert!(rows.iter().all(|r| r.key != c.key), "{} reviewed", c.key);
    }
}
