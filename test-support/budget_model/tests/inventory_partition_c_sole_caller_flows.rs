// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a7_physical_measurement.py; O:path:mycelium_internet/physical.py; O:path:scripts/a10_batch_policy.py; O:path:scripts/inventory_member_models.py; O:path:mycelium_platform_neutral/mac_mlx_int8.py.
use budget_model::inventory::{Candidate, bindings, candidates, checked_in, validate};

struct Edge {
    role: &'static str,
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

const A7: &str = "mycelium_a7_physical_measurement.py";
const IP: &str = "mycelium_internet/physical.py";
const AB: &str = "scripts/a10_batch_policy.py";
const MM: &str = "scripts/inventory_member_models.py";
const MX: &str = "mycelium_platform_neutral/mac_mlx_int8.py";
const A9: &str = "scripts/run_a9_gate.py";

fn edges() -> Vec<Edge> {
    let e = |role,
             rule,
             path,
             line,
             kind,
             symbol,
             target_path,
             target_line,
             target_kind,
             target_symbol| Edge {
        role,
        rule,
        path,
        line,
        kind,
        symbol,
        target_path,
        target_line,
        target_kind,
        target_symbol,
    };
    let clock = "reviewed-clock-offset";
    let forward = "resolved-optional-value";
    vec![
        e(
            "sole-caller-clock",
            clock,
            A7,
            305,
            "assignment",
            "deadline",
            A7,
            1021,
            "keyword",
            "timeout_s",
        ),
        e(
            "sole-caller-clock",
            clock,
            A7,
            687,
            "assignment",
            "deadline",
            A7,
            978,
            "keyword",
            "timeout_s",
        ),
        e(
            "sole-caller-clock",
            clock,
            A7,
            722,
            "assignment",
            "deadline",
            A7,
            1224,
            "keyword",
            "timeout_s",
        ),
        e(
            "sole-caller-forward",
            forward,
            IP,
            1525,
            "keyword",
            "poll_interval_seconds",
            IP,
            2623,
            "keyword",
            "poll_interval_seconds",
        ),
        e(
            "sole-caller-forward",
            forward,
            AB,
            54,
            "dict-field",
            "timeout_s",
            AB,
            134,
            "keyword",
            "timeout_s",
        ),
        e(
            "sole-caller-forward",
            forward,
            AB,
            56,
            "assignment",
            "document_out['window_ms']",
            AB,
            136,
            "keyword",
            "window_ms",
        ),
        e(
            "cli-guarded-argument",
            "validated-required-ceiling",
            AB,
            136,
            "keyword",
            "window_ms",
            AB,
            119,
            "comparison",
            "0",
        ),
        e(
            "cli-guarded-clock",
            clock,
            MM,
            70,
            "keyword",
            "valid_until_unix_ms",
            MM,
            51,
            "comparison",
            "1",
        ),
        e(
            "sole-caller-forward",
            forward,
            MX,
            151,
            "keyword",
            "paired_windows",
            A9,
            806,
            "keyword",
            "paired_windows",
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
fn c_sole_caller_and_cli_guard_flows_have_complete_identity_witnesses() {
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
        assert!(
            bindings::accepts(edge.rule, s, t),
            "{} {}",
            edge.role,
            s.key
        );
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
        // The witness is rule-specific: no other reviewed rule may reuse it.
        for other in [
            "upper-cap-reference",
            "reviewed-helper-return",
            "reviewed-local-comparison",
            "constructor-field",
            "validated-parameter-default",
        ] {
            if other != edge.rule {
                assert!(!bindings::accepts(other, s, t), "{} as {other}", s.key);
            }
        }
        // Existence and unit checks alone do not prove provenance.
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
fn c_sole_caller_flows_reject_other_arguments_and_unrelated_callers() {
    let all = candidates().unwrap();
    // The a7 deadline in wait_for_tokens is fed by the first-token field, not by the
    // settle-timeout call site, and a sibling deadline does not inherit another caller.
    let s = find(&all, A7, 305, "assignment", "deadline");
    for line in [978usize, 1224] {
        let t = find(&all, A7, line, "keyword", "timeout_s");
        assert!(!bindings::accepts("reviewed-clock-offset", s, t), "{line}");
    }
    // A reviewed argument of a different callee is not the sole caller of switch_policy.
    let s = find(&all, AB, 54, "dict-field", "timeout_s");
    let t = find(&all, AB, 136, "keyword", "window_ms");
    assert!(!bindings::accepts("resolved-optional-value", s, t));
    // The urlopen deadline `timeout_s + 10.0` and the unvalidated await seconds stay open.
    let rows = checked_in().unwrap();
    for (path, line, kind, symbol) in [
        (AB, 65usize, "keyword", "timeout"),
        (IP, 2599, "assignment", "await_seconds"),
    ] {
        let c = find(&all, path, line, kind, symbol);
        assert!(rows.iter().all(|r| r.key != c.key), "{} reviewed", c.key);
    }
}
