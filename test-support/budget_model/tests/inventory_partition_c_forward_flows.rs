// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:scripts/a13_operator/wp8_05_soak.py; O:path:scripts/run_a4_physical_idle_staleness_gate.py; O:path:scripts/run_a10_concurrency_probe.py; O:path:mycelium_qualification/safetensors_sharding.py; O:path:scripts/run_a9_withdrawal_browser.py; O:path:mycelium_a7_physical_measurement.py; O:path:mycelium_member_models.py; O:path:mycelium_internet/bootstrap.py; O:path:scripts/experimental_candidate_isolation.py; O:path:mycelium_a6_recovery.py; O:path:scripts/run_a12_mobile_physical_gate.py; O:path:mycelium_a6_product_fault_seams.py; O:path:scripts/qwen_phone_builder.py.
use budget_model::inventory::{Candidate, bindings, candidates, checked_in, validate};

type Site = (&'static str, usize, &'static str, &'static str);

struct Edge {
    rule: &'static str,
    source: Site,
    target: Site,
}

const SOAK: &str = "scripts/a13_operator/wp8_05_soak.py";
const A4: &str = "scripts/run_a4_physical_idle_staleness_gate.py";
const A10: &str = "scripts/run_a10_concurrency_probe.py";
const SHARD: &str = "mycelium_qualification/safetensors_sharding.py";
const QWEN: &str = "scripts/build_qwen_live_route.py";
const A9: &str = "scripts/run_a9_withdrawal_browser.py";
const A7: &str = "mycelium_a7_physical_measurement.py";
const MM: &str = "mycelium_member_models.py";
const IM: &str = "scripts/inventory_member_models.py";
const BS: &str = "mycelium_internet/bootstrap.py";
const EI: &str = "scripts/experimental_candidate_isolation.py";
const AR: &str = "mycelium_a6_recovery.py";
const A12: &str = "scripts/run_a12_mobile_physical_gate.py";
const QP: &str = "scripts/qwen_phone_builder.py";
const FS: &str = "mycelium_a6_product_fault_seams.py";

fn edges() -> Vec<Edge> {
    vec![
        Edge {
            rule: "resolved-optional-value",
            source: (SOAK, 196, "keyword", "timeout"),
            target: (SOAK, 234, "keyword", "timeout"),
        },
        Edge {
            rule: "reviewed-clock-offset",
            source: (A4, 133, "assignment", "deadline"),
            target: (A4, 392, "cli-default", "--observation-window"),
        },
        Edge {
            rule: "resolved-optional-value",
            source: (A10, 85, "keyword", "timeout"),
            target: (A10, 148, "keyword", "timeout_seconds"),
        },
        Edge {
            rule: "resolved-optional-value",
            source: (A10, 113, "keyword", "timeout"),
            target: (A10, 170, "keyword", "timeout_seconds"),
        },
        Edge {
            rule: "resolved-optional-value",
            source: (SHARD, 387, "keyword", "max_file_bytes"),
            target: (QWEN, 253, "keyword", "max_file_bytes"),
        },
        Edge {
            rule: "reviewed-clock-offset",
            source: (A9, 218, "assignment", "deadline"),
            target: (A9, 238, "keyword", "timeout_s"),
        },
        Edge {
            rule: "resolved-optional-value",
            source: (MM, 238, "dict-field", "valid_until_unix_ms"),
            target: (IM, 70, "keyword", "valid_until_unix_ms"),
        },
        Edge {
            rule: "resolved-optional-value",
            source: (BS, 229, "assignment", "self._max_attempts"),
            target: (BS, 329, "keyword", "max_attempts"),
        },
        Edge {
            rule: "validated-required-ceiling",
            source: (EI, 116, "assignment", "(issued, expires)"),
            target: (EI, 120, "comparison", "expires - issued"),
        },
        Edge {
            rule: "reviewed-clock-offset",
            source: (AR, 1674, "assignment", "self._breaker_open_until_unix_ms"),
            target: (AR, 81, "assignment", "SUCCESSOR_BREAKER_SECONDS"),
        },
        Edge {
            rule: "reviewed-clock-offset",
            source: (A12, 181, "assignment", "deadline"),
            target: (A12, 279, "keyword", "timeout"),
        },
        Edge {
            rule: "resolved-optional-value",
            source: (FS, 783, "positional-call", "join"),
            target: (FS, 207, "assignment", "self._wait_seconds"),
        },
        Edge {
            rule: "resolved-optional-value",
            source: (FS, 561, "keyword", "timeout"),
            target: (FS, 783, "positional-call", "join"),
        },
        Edge {
            rule: "reviewed-clock-offset",
            source: (QP, 378, "positional-call", "selector.select"),
            target: (QP, 397, "assignment", "deadline"),
        },
        Edge {
            rule: "typed-config-field",
            source: (A7, 637, "positional-call", "time.sleep"),
            target: (A7, 590, "assignment", "poll_s"),
        },
    ]
}

fn find(all: &[Candidate], site: Site) -> &Candidate {
    let (path, line, kind, symbol) = site;
    let hits: Vec<_> = all
        .iter()
        .filter(|r| r.path == path && r.line == line && r.kind == kind && r.symbol == symbol)
        .collect();
    assert_eq!(hits.len(), 1, "{path}:{line} {kind} {symbol}");
    hits[0]
}

#[test]
fn c_forward_flows_have_complete_identity_witnesses() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    for edge in edges() {
        let s = find(&all, edge.source);
        let t = find(&all, edge.target);
        assert!(bindings::accepts(edge.rule, s, t), "{}", s.key);
        let row = rows.iter().find(|r| r.key == s.key).unwrap();
        assert_eq!(row.disposition, "derived", "{}", s.key);
        assert!(row.review.contains("exact-span-sha256="), "{}", s.key);
        assert!(
            row.review
                .contains(&format!("binding-category:{}", edge.rule)),
            "{}",
            s.key
        );

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
            "typed-config-field",
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
fn c_forward_flows_do_not_cover_other_callers_targets_or_neighbours() {
    let all = candidates().unwrap();
    let rows = checked_in().unwrap();
    // The sharder edge names one caller row; the other two callers carry the same constant but
    // are not substitutable witnesses for it.
    let s = find(&all, (SHARD, 387, "keyword", "max_file_bytes"));
    for line in [1639usize, 1666] {
        let other = find(&all, (QWEN, line, "keyword", "max_file_bytes"));
        assert!(
            !bindings::accepts("resolved-optional-value", s, other),
            "{line}"
        );
    }
    // Only the exact poll_s/5 sleep qualifies; the whole-interval sleeps keep their own rule and a
    // different divisor or receiver is not accepted by the sub-multiple row.
    let sub = find(&all, (A7, 637, "positional-call", "time.sleep"));
    let mut other = sub.clone();
    other.expression = "settings.poll_s / 4".into();
    let poll = find(&all, (A7, 590, "assignment", "poll_s"));
    assert!(!bindings::accepts("typed-config-field", &other, poll));
    // The breaker edge names the 30 s allowance only; the 60 s failure window is a different ID,
    // and the breaker_open comparison plus the isolation freshness chain stay pending.
    let breaker = find(
        &all,
        (AR, 1674, "assignment", "self._breaker_open_until_unix_ms"),
    );
    let window = find(
        &all,
        (AR, 82, "assignment", "SUCCESSOR_BREAKER_WINDOW_SECONDS"),
    );
    assert!(!bindings::accepts("reviewed-clock-offset", breaker, window));
    for (path, line, kind) in [(AR, 1677usize, "comparison"), (EI, 119, "comparison")] {
        let c = all
            .iter()
            .find(|r| r.path == path && r.line == line && r.kind == kind)
            .unwrap();
        // Round 7 (RU-013): the breaker-open expiry compare at AR:1677 is an absolute-instant compare with a
        // pinned same-file producer (`inventory_x_exclusion`); it is still never a clock-offset edge.
        let allowed = if path == AR {
            Some("x-exclusion:")
        } else {
            None
        };
        assert!(
            rows.iter()
                .filter(|r| r.key == c.key)
                .all(|r| allowed.is_some_and(|p| r.review.starts_with(p))),
            "{} reviewed",
            c.key
        );
    }
    // The seven self.wait callers are named by one row; a sibling caller row is not the witness.
    let wait = find(&all, (A12, 181, "assignment", "deadline"));
    let sibling = find(&all, (A12, 404, "keyword", "timeout"));
    assert!(!bindings::accepts("reviewed-clock-offset", wait, sibling));
    // The other fault-target deadline plumbing (absolute command deadline, wall-clock publish) is not
    // covered by the wait_seconds property edge.
    for (line, kind) in [(329usize, "assignment"), (336, "keyword")] {
        let c = all
            .iter()
            .find(|r| r.path == FS && r.line == line && r.kind == kind)
            .unwrap();
        assert!(pending_or_x_excluded(&rows, &c.key), "{} reviewed", c.key);
    }
    // The a10 post/get edges are not interchangeable.
    let post = find(&all, (A10, 85, "keyword", "timeout"));
    let get_target = find(&all, (A10, 170, "keyword", "timeout_seconds"));
    assert!(!bindings::accepts(
        "resolved-optional-value",
        post,
        get_target
    ));
    // Differently-budgeted multi-target callers, the pilot gate runner, the string-valued CLI
    // lifetime and an unvalidated persisted-deadline plumbing stay pending.
    for (path, line, kind, symbol) in [
        (
            "scripts/run_a4_product_gate.py",
            165usize,
            "assignment",
            "deadline",
        ),
        (
            "scripts/run_a5_product_gate.py",
            167,
            "assignment",
            "deadline",
        ),
        (
            "scripts/run_a10_physical_gate.py",
            1401,
            "keyword",
            "timeout",
        ),
        ("scripts/pilot_readiness_gate.py", 185, "keyword", "timeout"),
        (
            "scripts/run_a5_benchmark_supervisor.py",
            512,
            "assignment",
            "lifetime_deadline",
        ),
        (
            "mycelium_qualification/replica_track_backend.py",
            314,
            "keyword",
            "deadline_monotonic_s",
        ),
    ] {
        let c = all
            .iter()
            .find(|r| r.path == path && r.line == line && r.kind == kind && r.symbol == symbol)
            .unwrap();
        // Round 8 (parent decision a): the divergent-callers sink may carry exactly one review kind, the
        // forward-callers `+` relation of both callers' own values; nothing else passes.
        let allowed = if path == "scripts/run_a10_physical_gate.py" && line == 1401 {
            "forward-callers:required-parameter;"
        } else {
            "x-exclusion:"
        };
        assert!(
            rows.iter()
                .filter(|r| r.key == c.key)
                .all(|r| r.review.starts_with(allowed)),
            "{} reviewed",
            c.key
        );
    }
}

/// Round 7 (RU-013): a row these tests pin as "not covered by the clock-offset/forward edge" may later be
/// `not-a-bound` as an absolute instant with a pinned producer (`inventory_x_exclusion`); any other review is still
/// refused here.
fn pending_or_x_excluded(rows: &[budget_model::inventory::Bound], key: &str) -> bool {
    rows.iter()
        .filter(|r| r.key == key)
        .all(|r| r.review.starts_with("x-exclusion:"))
}
