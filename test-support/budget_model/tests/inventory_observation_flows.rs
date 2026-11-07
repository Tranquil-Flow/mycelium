// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_speculative_decode_execution.py.
//! L1b round 9, RU-023: a value that is only MEASURED and then recorded/reported is an observation, not a limit,
//! when a pinned flow reaches only observation sinks (no comparison, wait/timeout argument or clamp). The 26
//! forecast rows of the families `x-elapsed-or-measured-observation` and `x-measured-uncertainty` were each
//! traced in the oracle AST at f4e366e9; exactly two reach only observation sinks (the speculative-decode record
//! field `transfer_ms` and the keyword that feeds it). The other 24 stay pending, each with a pinned verdict
//! naming the first forbidden sink. Rust re-checks everything the census can show.
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
#[path = "common/r32_verifier_admission.rs"]
#[allow(
    dead_code,
    reason = "exact four-discovery addition to the bounds-only gap"
)]
mod r32_verifier_admission;

#[path = "common/r29_retry_admission.rs"]
#[allow(
    dead_code,
    reason = "exact additive manual review outside bounds-only count"
)]
mod r29_retry_admission;
use budget_model::inventory::{
    Bound, Candidate, candidate_keys, candidates, checked_in, observation_flows, validate,
    x_exclusion,
};
use std::collections::{BTreeMap, BTreeSet};

const P: &str = "mycelium_speculative_decode_execution.py";
const RECORD: &str = "mycelium_speculative_decode_execution.py:1150:8:dict-field:transfer_ms";
const FORWARD: &str = "mycelium_speculative_decode_execution.py:1702:8:keyword:transfer_ms";
/// Same family, same-looking carrier, but the flow reaches a comparison through the breaker window.
const LOCAL: &str = "mycelium_speculative_decode_execution.py:1415:4:assignment:transfer_ms";
const OBSERVED: &str = "mycelium_speculative_decode_execution.py:1361:4:assignment:observed_ms";

/// verdict -> row count of the pinned table (independent literal, from the AST trace).
const VERDICTS: &[(&str, usize)] = &[
    ("is-a-comparison", 2),
    ("not-a-measurement", 4),
    ("observation", 2),
    ("reaches-clamp", 3),
    ("reaches-comparison", 7),
    ("reaches-deadline-arithmetic", 7),
    ("reaches-wait", 1),
];

fn world() -> (Vec<Bound>, BTreeMap<String, Candidate>) {
    let map = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    (checked_in().unwrap(), map)
}

fn row<'a>(rows: &'a [Bound], key: &str) -> &'a Bound {
    rows.iter()
        .find(|r| r.key == key)
        .unwrap_or_else(|| panic!("missing row {key}"))
}

fn with(rows: &[Bound], m: Bound) -> Vec<Bound> {
    let mut all = rows.to_vec();
    let i = all.iter().position(|r| r.key == m.key).unwrap();
    all[i] = m;
    all
}

fn table_lines() -> Vec<&'static str> {
    observation_flows::TABLE.lines().collect()
}

/// The table with the line of `key` replaced by `f(line)`.
fn edit(key: &str, f: impl Fn(Vec<String>) -> Vec<String>) -> String {
    let mut out = Vec::new();
    for line in observation_flows::TABLE.lines() {
        if line.split('\t').next() == Some(key) {
            let cols = f(line.split('\t').map(String::from).collect());
            out.push(cols.join("\t"));
        } else {
            out.push(line.to_string());
        }
    }
    out.join("\n") + "\n"
}

fn ok_with(rows: &[Bound], map: &BTreeMap<String, Candidate>, key: &str, table: &str) -> bool {
    observation_flows::validate_with(row(rows, key), rows, map, table)
}

#[test]
fn two_transfer_ms_rows_are_measured_observations_with_a_pinned_flow() {
    let (rows, map) = world();
    let reviewed: Vec<&Bound> = rows
        .iter()
        .filter(|r| r.review.starts_with(observation_flows::PREFIX))
        .collect();
    assert_eq!(
        reviewed
            .iter()
            .map(|r| r.key.as_str())
            .collect::<BTreeSet<_>>(),
        BTreeSet::from([RECORD, FORWARD])
    );
    for (key, rule) in [
        (RECORD, "record-field"),
        (FORWARD, "forwarded-record-field"),
    ] {
        let r = row(&rows, key);
        assert_eq!(r.path, P);
        assert_eq!(r.symbol, "transfer_ms");
        assert_eq!(r.disposition, "not-a-bound");
        assert!(r.budget.is_empty() && r.value.is_none(), "{key}");
        assert_eq!(r.unit, "not-applicable");
        assert_eq!(
            r.relation,
            format!("measured-observation-not-a-limit; flow={rule}")
        );
        assert!(r.review.starts_with(&format!("observation-flow:{rule}; ")));
        assert!(observation_flows::validate(r, &rows, &map), "{key}");
    }
    assert_eq!(row(&rows, RECORD).scope, "_runtime_request");
    assert_eq!(row(&rows, FORWARD).scope, "run_speculative_cycle");
    assert!(validate(&rows).is_ok());
}

#[test]
fn the_table_covers_exactly_the_26_forecast_rows_with_pinned_verdicts() {
    let lines = table_lines();
    assert_eq!(lines[0], observation_flows::HEADER);
    let body: Vec<Vec<&str>> = lines[1..].iter().map(|l| l.split('\t').collect()).collect();
    assert!(body.iter().all(|f| f.len() == 7));
    let keys: BTreeSet<&str> = body.iter().map(|f| f[0]).collect();
    assert_eq!(keys.len(), 26, "unique keys");
    // Independently: the forecast X rows of the two families.
    let forecast: BTreeSet<&str> = x_exclusion::FORECAST
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .filter(|f| {
            f[2] == "X"
                && matches!(
                    f[3],
                    "x-elapsed-or-measured-observation" | "x-measured-uncertainty"
                )
        })
        .map(|f| f[0])
        .collect();
    assert_eq!(forecast, keys);
    let mut got: BTreeMap<&str, usize> = BTreeMap::new();
    for f in &body {
        *got.entry(f[1]).or_default() += 1;
    }
    assert_eq!(got, VERDICTS.iter().copied().collect::<BTreeMap<_, _>>());
    // Every non-observation row names at least one forbidden sink as `path:line:kind` and a reason.
    for f in &body {
        if f[1] == "observation" {
            assert!(!f[2].is_empty() && !f[3].is_empty(), "{}", f[0]);
            continue;
        }
        assert!(f[2].is_empty() && f[4].is_empty(), "{}", f[0]);
        let sink = f[5].split(';').next().unwrap();
        let mut parts = sink.rsplitn(3, ':');
        let (kind, line, path) = (parts.next(), parts.next(), parts.next());
        assert!(
            kind.is_some_and(|k| !k.is_empty())
                && line.is_some_and(|l| l.parse::<usize>().is_ok_and(|n| n > 0))
                && path.is_some_and(|p| p.ends_with(".py")),
            "{}: {sink}",
            f[0]
        );
        assert!(f[6].contains("f4e366e9") || f[6].len() > 20, "{}", f[0]);
    }
}

#[test]
fn the_24_other_rows_stay_pending_and_a_forged_row_on_each_is_refused() {
    let (rows, map) = world();
    let reviewed: BTreeSet<&str> = rows.iter().map(|r| r.key.as_str()).collect();
    let mut checked = 0;
    for f in table_lines()
        .iter()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
    {
        if f[1] == "observation" {
            continue;
        }
        let key = f[0];
        assert!(!reviewed.contains(key), "{key} must stay pending");
        let c = &map[key];
        for rule in observation_flows::RULES {
            let forged = Bound {
                key: key.into(),
                path: c.path.clone(),
                line: c.line,
                column: c.column,
                scope: c.scope.clone(),
                symbol: c.symbol.clone(),
                expression: c.expression.clone(),
                unit: "not-applicable".into(),
                value: None,
                semantic: "forged".into(),
                authority: "forged".into(),
                budget: String::new(),
                disposition: "not-a-bound".into(),
                review: format!(
                    "observation-flow:{rule}; {} @ f4e366e98a1f705dfaf182897044500d290dbd7f exact-span-sha256=x",
                    c.path
                ),
                relation: format!("measured-observation-not-a-limit; flow={rule}"),
                source_sha: "ef9ae61de0222c78d83c8133f8f709619394bf51".into(),
            };
            let mut all = rows.clone();
            all.push(forged.clone());
            assert!(
                !observation_flows::validate_with(&forged, &all, &map, observation_flows::TABLE),
                "{key} {rule}"
            );
        }
        checked += 1;
    }
    assert_eq!(checked, 24);
    assert!(validate(&rows).is_ok());
    // The local that also feeds the breaker window and the comparison-bound neighbours.
    for key in [LOCAL, OBSERVED] {
        assert!(!reviewed.contains(key));
    }
}

#[test]
fn census_visible_forbidden_sinks_refuse_even_a_consistently_forged_table() {
    let (rows, map) = world();
    // Rows whose scope contains a census comparison/keyword/call row that reads the carrier: the validator must
    // refuse a rule claim even when the table is edited to say `observation` with the matching rule and sinks.
    for key in [
        LOCAL,
        OBSERVED,
        "physical_inference_node.py:315:4:assignment:budget_ms",
        "mycelium_live/router_port.py:335:12:assignment:remaining_ms",
    ] {
        let c = &map[key];
        let rule = "record-field";
        let edited = edit(key, |mut cols| {
            cols[1] = "observation".into();
            cols[2] = rule.into();
            cols[5] = format!("return-dict:{}:{}", c.path, c.line);
            cols[6] = format!(
                "{}:1-2 @ f4e366e98a1f705dfaf182897044500d290dbd7f; exact-span-sha256=x",
                c.path
            );
            cols
        });
        // The census kind is `assignment`, never `dict-field`, so the shape check also refuses it; mutate the kind
        // by moving the claim onto a dict-field twin in the same scope is covered below.
        let forged = Bound {
            key: key.into(),
            path: c.path.clone(),
            line: c.line,
            column: c.column,
            scope: c.scope.clone(),
            symbol: c.symbol.clone(),
            expression: c.expression.clone(),
            unit: "not-applicable".into(),
            value: None,
            semantic: "forged".into(),
            authority: "forged".into(),
            budget: String::new(),
            disposition: "not-a-bound".into(),
            review: format!(
                "observation-flow:{rule}; {} @ f4e366e98a1f705dfaf182897044500d290dbd7f",
                c.path
            ),
            relation: format!("measured-observation-not-a-limit; flow={rule}"),
            source_sha: "ef9ae61de0222c78d83c8133f8f709619394bf51".into(),
        };
        let mut all = rows.clone();
        all.push(forged.clone());
        assert!(
            !observation_flows::validate_with(&forged, &all, &map, &edited),
            "{key}"
        );
    }
}

#[test]
fn each_check_of_the_two_accepted_rows_is_refused_when_broken() {
    let (rows, map) = world();
    let t = observation_flows::TABLE;
    assert!(ok_with(&rows, &map, RECORD, t) && ok_with(&rows, &map, FORWARD, t));

    // Table shape: header, missing row, duplicated row, short row.
    let no_header = t.replacen("key\tverdict", "k\tverdict", 1);
    assert!(!ok_with(&rows, &map, RECORD, &no_header));
    let missing: String = t
        .lines()
        .filter(|l| !l.starts_with(RECORD))
        .map(|l| format!("{l}\n"))
        .collect();
    assert!(!ok_with(&rows, &map, RECORD, &missing));
    let line = t.lines().find(|l| l.starts_with(RECORD)).unwrap();
    assert!(!ok_with(&rows, &map, RECORD, &format!("{t}{line}\n")));
    assert!(!ok_with(
        &rows,
        &map,
        RECORD,
        &format!("{t}{RECORD}\tobservation\n")
    ));

    // Verdict, rule, carrier, sinks, reference.
    for (idx, value) in [
        (1, "reaches-comparison"),
        (1, ""),
        (2, "forwarded-record-field"),
        (2, ""),
        (3, ""),
        (3, "other_ms"),
    ] {
        let t2 = edit(RECORD, |mut c| {
            c[idx] = value.into();
            c
        });
        assert!(!ok_with(&rows, &map, RECORD, &t2), "col {idx} = {value:?}");
    }
    let sinks = |new: &str| {
        edit(RECORD, |mut c| {
            c[5] = new.into();
            c
        })
    };
    assert!(!ok_with(&rows, &map, RECORD, &sinks("")));
    assert!(!ok_with(
        &rows,
        &map,
        RECORD,
        &sinks(&format!("return-dict:{P}:1151"))
    ));
    assert!(!ok_with(
        &rows,
        &map,
        RECORD,
        &sinks("return-dict:other.py:1150")
    ));
    assert!(!ok_with(
        &rows,
        &map,
        RECORD,
        &sinks(&format!("return-dict:{P}:1150;compare:{P}:1160"))
    ));
    assert!(!ok_with(
        &rows,
        &map,
        RECORD,
        &sinks(&format!(
            "return-dict:{P}:1150;wait:mycelium_speculative_decode.py:799"
        ))
    ));
    assert!(!ok_with(
        &rows,
        &map,
        RECORD,
        &sinks(&format!("return-dict:{P}:x"))
    ));
    for (needle, repl) in [
        ("exact-span-sha256=", "span="),
        ("f4e366e98a1f705dfaf182897044500d290dbd7f", "f4e366e9"),
        (P, "other.py"),
    ] {
        let t2 = edit(RECORD, |mut c| {
            c[6] = c[6].replacen(needle, repl, 1);
            c
        });
        assert!(!ok_with(&rows, &map, RECORD, &t2), "{needle}");
    }

    // The bounds row: every mapped column.
    let base = row(&rows, RECORD).clone();
    type Mutation = Box<dyn Fn(&mut Bound)>;
    let muts: Vec<Mutation> = vec![
        Box::new(|b| b.disposition = "reviewed".into()),
        Box::new(|b| b.budget = "PyUpperGuardX".into()),
        Box::new(|b| b.value = Some(1)),
        Box::new(|b| b.unit = "milliseconds".into()),
        Box::new(|b| {
            b.relation = "measured-observation-not-a-limit; flow=forwarded-record-field".into()
        }),
        Box::new(|b| b.relation = "absolute-instant-not-a-magnitude".into()),
        Box::new(|b| b.review = "observation-flow:unknown-rule; x".into()),
        Box::new(|b| b.review = "observation-flow:forwarded-record-field; x".into()),
        Box::new(|b| b.review = "observation-flow:".into()),
        Box::new(|b| b.line += 1),
        Box::new(|b| b.column += 1),
        Box::new(|b| b.scope = "other".into()),
        Box::new(|b| b.symbol = "other_ms".into()),
        Box::new(|b| b.expression = "transfer_ms + 1".into()),
        Box::new(|b| b.path = "other.py".into()),
    ];
    for (i, m) in muts.iter().enumerate() {
        let mut b = base.clone();
        m(&mut b);
        let all = with(&rows, b);
        assert!(
            !observation_flows::validate_with(row(&all, RECORD), &all, &map, t),
            "mutation {i}"
        );
    }

    // Census mutations (consistent: candidate and row change together).
    let mutate_census = |f: &dyn Fn(&mut Candidate, &mut Bound)| {
        let mut m = map.clone();
        let mut b = base.clone();
        f(m.get_mut(RECORD).unwrap(), &mut b);
        let all = with(&rows, b);
        observation_flows::validate_with(row(&all, RECORD), &all, &m, t)
    };
    assert!(!mutate_census(&|c, _| c.kind = "keyword".into()));
    assert!(!mutate_census(&|c, b| {
        c.expression = "float(transfer_ms)".into();
        b.expression = c.expression.clone();
    }));
    assert!(!mutate_census(&|c, b| {
        c.symbol = "other_ms".into();
        b.symbol = c.symbol.clone();
    }));
    // A census row in the same scope that compares the carrier is a forbidden sink.
    for (kind, expr) in [
        ("comparison", "transfer_ms > 5"),
        ("positional-call", "transfer_ms"),
        ("keyword", "transfer_ms"),
        ("positional-timeout", "min(1, transfer_ms)"),
    ] {
        let mut m = map.clone();
        let src = m[RECORD].clone();
        m.insert(
            "extra".into(),
            Candidate {
                key: "extra".into(),
                line: src.line + 100,
                kind: kind.into(),
                expression: expr.into(),
                symbol: "timeout".into(),
                ..src
            },
        );
        assert!(
            !observation_flows::validate_with(row(&rows, RECORD), &rows, &m, t),
            "{kind}"
        );
    }
    // A longer identifier containing the carrier is not a use of it.
    let mut m = map.clone();
    let src = m[RECORD].clone();
    m.insert(
        "extra".into(),
        Candidate {
            key: "extra".into(),
            line: src.line + 100,
            kind: "comparison".into(),
            expression: "transfer_ms_total > 5 and not_transfer_ms".into(),
            ..src
        },
    );
    assert!(observation_flows::validate_with(
        row(&rows, RECORD),
        &rows,
        &m,
        t
    ));
}

#[test]
fn the_forwarded_keyword_needs_a_closed_record_field_target() {
    let (rows, map) = world();
    let t = observation_flows::TABLE;
    let base = row(&rows, FORWARD).clone();
    for (idx, value) in [
        (1, "reaches-comparison"),
        (2, "record-field"),
        (3, ""),
        (3, "_other"),
        (4, ""),
        (4, LOCAL),
        (4, OBSERVED),
    ] {
        let t2 = edit(FORWARD, |mut c| {
            c[idx] = value.into();
            c
        });
        assert!(!ok_with(&rows, &map, FORWARD, &t2), "col {idx} = {value:?}");
    }
    let sinks = |new: &str| {
        edit(FORWARD, |mut c| {
            c[5] = new.into();
            c
        })
    };
    assert!(!ok_with(&rows, &map, FORWARD, &sinks("")));
    assert!(!ok_with(
        &rows,
        &map,
        FORWARD,
        &sinks(&format!("keyword-to-record-field:{P}:1703"))
    ));
    assert!(!ok_with(
        &rows,
        &map,
        FORWARD,
        &sinks(&format!("return-dict:{P}:1702"))
    ));
    assert!(!ok_with(
        &rows,
        &map,
        FORWARD,
        &sinks(&format!(
            "keyword-to-record-field:{P}:1702;return-dict:{P}:1150"
        ))
    ));
    // The target row must itself be a reviewed record-field observation.
    let without_target: Vec<Bound> = rows.iter().filter(|r| r.key != RECORD).cloned().collect();
    assert!(!observation_flows::validate_with(
        &base,
        &without_target,
        &map,
        t
    ));
    let mut other = row(&rows, RECORD).clone();
    other.review = other.review.replace(
        "observation-flow:record-field",
        "observation-flow:forwarded-record-field",
    );
    let all = with(&rows, other);
    assert!(!observation_flows::validate_with(&base, &all, &map, t));
    // Target census must be the same file, the same parameter name, a dict-field, in the callee scope.
    let mutate_target = |f: &dyn Fn(&mut Candidate)| {
        let mut m = map.clone();
        f(m.get_mut(RECORD).unwrap());
        observation_flows::validate_with(&base, &rows, &m, t)
    };
    assert!(!mutate_target(&|c| c.path = "other.py".into()));
    assert!(!mutate_target(&|c| c.symbol = "other_ms".into()));
    assert!(!mutate_target(&|c| c.kind = "keyword".into()));
    assert!(!mutate_target(
        &|c| c.scope = "Other.run_speculative_cycle".into()
    ));
    assert!(!mutate_target(&|c| c.scope = "other".into()));
    // The keyword row: kind and carrier.
    let mutate_use = |f: &dyn Fn(&mut Candidate, &mut Bound)| {
        let mut m = map.clone();
        let mut b = base.clone();
        f(m.get_mut(FORWARD).unwrap(), &mut b);
        let all = with(&rows, b);
        observation_flows::validate_with(row(&all, FORWARD), &all, &m, t)
    };
    assert!(!mutate_use(&|c, _| c.kind = "dict-field".into()));
    assert!(!mutate_use(&|c, b| {
        c.expression = "transfer_ms * 2".into();
        b.expression = c.expression.clone();
    }));
    assert!(!mutate_use(&|c, b| {
        c.symbol = "elapsed_ms".into();
        b.symbol = c.symbol.clone();
    }));
    // The same-scope breaker rows do not matter for the keyword: its flow is the callee parameter.
    assert!(map.values().any(|c| {
        c.path == P
            && c.scope == base.scope
            && c.kind == "keyword"
            && c.expression == "observed_ms + transfer_ms"
    }));
    assert!(observation_flows::validate_with(&base, &rows, &map, t));
}

/// Consistent mutation: the census, the bounds rows and the table change together, so only the check under
/// test can refuse.
fn scenario(
    key: &str,
    f: impl Fn(&mut BTreeMap<String, Candidate>, &mut Vec<Bound>, &mut String),
) -> bool {
    let (mut rows, mut map) = world();
    let mut table = observation_flows::TABLE.to_string();
    f(&mut map, &mut rows, &mut table);
    observation_flows::validate_with(row(&rows, key), &rows, &map, &table)
}

fn set_row(rows: &mut [Bound], key: &str, f: impl Fn(&mut Bound)) {
    f(rows.iter_mut().find(|r| r.key == key).unwrap());
}

#[test]
fn consistent_mutations_of_each_remaining_check_are_refused() {
    assert!(scenario(RECORD, |_, _, _| {}) && scenario(FORWARD, |_, _, _| {}));
    // A review without the prefix is not an observation-flow claim.
    assert!(!scenario(RECORD, |_, rows, _| set_row(rows, RECORD, |b| {
        b.review = b.review.replacen("observation-flow:", "other-flow:", 1);
    })));
    // An unknown rule is refused even when row, relation and table agree on it.
    assert!(!scenario(FORWARD, |_, rows, table| {
        set_row(rows, FORWARD, |b| {
            b.review = b.review.replacen("forwarded-record-field", "bogus", 1);
            b.relation = "measured-observation-not-a-limit; flow=bogus".into();
        });
        *table = edit(FORWARD, |mut c| {
            c[2] = "bogus".into();
            c
        });
    }));
    // A carrier that is not a plain identifier.
    assert!(!scenario(RECORD, |map, rows, table| {
        map.get_mut(RECORD).unwrap().symbol = "transfer ms".into();
        map.get_mut(RECORD).unwrap().expression = "transfer ms".into();
        set_row(rows, RECORD, |b| {
            b.symbol = "transfer ms".into();
            b.expression = "transfer ms".into();
        });
        *table = edit(RECORD, |mut c| {
            c[3] = "transfer ms".into();
            c
        });
    }));
    // Table rows with a wrong column count, or a non-empty target on a record field.
    assert!(!scenario(RECORD, |_, _, table| {
        *table = edit(RECORD, |mut c| {
            c.push("extra".into());
            c
        });
    }));
    assert!(!scenario(RECORD, |_, _, table| {
        *table = edit(RECORD, |mut c| {
            c[4] = FORWARD.into();
            c
        });
    }));
    assert!(!scenario(RECORD, |_, _, table| {
        *table = edit(RECORD, |mut c| {
            c[4] = String::new();
            c[5] = format!("{};", c[5]);
            c
        });
    }));
    // Sink well-formedness: kind, `.py` path, positive line, a single own return-dict.
    let own = format!("return-dict:{P}:1150");
    for extra in [
        "is-none-check:notes.txt:5".to_string(),
        "floor-validator:mycelium_speculative_decode.py:0".to_string(),
        format!("return-dict:{P}:1151"),
        "wait:mycelium_speculative_decode.py:799".to_string(),
    ] {
        assert!(
            !scenario(RECORD, |_, _, table| {
                *table = edit(RECORD, |mut c| {
                    c[5] = format!("{own};{extra}");
                    c
                });
            }),
            "{extra}"
        );
    }
    assert!(scenario(RECORD, |_, _, table| {
        *table = edit(RECORD, |mut c| {
            c[5] = format!("{own};is-none-check:mycelium_speculative_decode.py:794");
            c
        });
    }));
    // A reader of the same scope name in another file does not count as a use.
    assert!(scenario(RECORD, |map, _, _| {
        let src = map[RECORD].clone();
        map.insert(
            "elsewhere".into(),
            Candidate {
                key: "elsewhere".into(),
                path: "other.py".into(),
                kind: "comparison".into(),
                expression: "transfer_ms > 5".into(),
                ..src
            },
        );
    }));
}

#[test]
fn the_forwarded_target_is_revalidated_and_matched_to_the_keyword() {
    // The target row stays a record-field row but breaks one of its own checks: the forwarded row follows it.
    assert!(!scenario(FORWARD, |_, rows, _| set_row(
        rows,
        RECORD,
        |b| b.relation = "x".into()
    )));
    // A self-referencing target would recurse forever without the record-field gate.
    assert!(!scenario(FORWARD, |_, _, table| {
        *table = edit(FORWARD, |mut c| {
            c[4] = FORWARD.into();
            c
        });
    }));
    // Consistent target mutations (census, row, table): a different file, a different parameter, the caller's own
    // scope.
    assert!(!scenario(FORWARD, |map, rows, table| {
        map.get_mut(RECORD).unwrap().path = "other.py".into();
        set_row(rows, RECORD, |b| b.path = "other.py".into());
        *table = edit(RECORD, |mut c| {
            c[5] = c[5].replace(P, "other.py");
            c[6] = c[6].replacen(P, "other.py", 1);
            c
        });
    }));
    assert!(!scenario(FORWARD, |map, rows, table| {
        map.get_mut(RECORD).unwrap().symbol = "elapsed_ms".into();
        map.get_mut(RECORD).unwrap().expression = "elapsed_ms".into();
        set_row(rows, RECORD, |b| {
            b.symbol = "elapsed_ms".into();
            b.expression = "elapsed_ms".into();
        });
        *table = edit(RECORD, |mut c| {
            c[3] = "elapsed_ms".into();
            c
        });
    }));
    assert!(!scenario(FORWARD, |map, rows, table| {
        map.get_mut(RECORD).unwrap().scope = "run_speculative_cycle".into();
        set_row(rows, RECORD, |b| b.scope = "run_speculative_cycle".into());
        *table = edit(FORWARD, |mut c| {
            c[3] = "run_speculative_cycle".into();
            c
        });
    }));
    // The callee name must be an identifier.
    assert!(!scenario(FORWARD, |map, rows, table| {
        map.get_mut(RECORD).unwrap().scope = "bad name".into();
        set_row(rows, RECORD, |b| b.scope = "bad name".into());
        *table = edit(FORWARD, |mut c| {
            c[3] = "bad name".into();
            c
        });
    }));
}

#[test]
fn a_malformed_neighbour_row_invalidates_the_whole_table() {
    let (rows, map) = world();
    // Every column that `entry_of` requires to be non-empty, on a row that is not the one being validated.
    for (idx, label) in [(0, "key"), (1, "verdict"), (3, "carrier"), (5, "sinks")] {
        let mut cols: Vec<String> = vec!["k".into(); 7];
        cols[idx] = String::new();
        let bad = format!("{}{}\n", observation_flows::TABLE, cols.join("\t"));
        assert!(!ok_with(&rows, &map, RECORD, &bad), "{label}");
        assert!(!ok_with(&rows, &map, FORWARD, &bad), "{label}");
    }
    // A well-formed extra row for another key does not disturb the accepted ones.
    let extra = "k\tk\t\tk\t\tk\tk\n";
    let good = format!("{}{extra}", observation_flows::TABLE);
    assert!(ok_with(&rows, &map, RECORD, &good) && ok_with(&rows, &map, FORWARD, &good));
}

#[test]
fn the_pinned_spans_hash_to_the_oracle_text() {
    // Host check: re-derive every `exact-span-sha256` from the read-only oracle checkout.
    let oracle = oracle_blobs::oracle_dir_or_panic();
    let mut checked = 0;
    for f in table_lines()
        .iter()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
    {
        if f[1] != "observation" {
            continue;
        }
        let reference = f[6];
        // `<path>:<a>-<b> @ <sha>; exact-span-sha256=<hex>` (+ reader spans `<path>:<a>-<b> sha256=<hex>`).
        let mut spans: Vec<(String, usize, usize, String)> = Vec::new();
        let mut rest = reference;
        let mut first = true;
        while let Some(at) = rest.find(".py:") {
            let start = rest[..at]
                .rfind(|c: char| !(c.is_ascii_alphanumeric() || c == '_' || c == '/'))
                .map_or(0, |i| i + 1);
            let path = &rest[start..at + 3];
            let after = &rest[at + 4..];
            let dash = after.find('-').unwrap();
            let a: usize = after[..dash].parse().unwrap();
            let tail = &after[dash + 1..];
            let end = tail.find(|c: char| !c.is_ascii_digit()).unwrap();
            let b: usize = tail[..end].parse().unwrap();
            let marker = if first {
                "exact-span-sha256="
            } else {
                "sha256="
            };
            let hex_at = tail.find(marker).unwrap() + marker.len();
            let hex: String = tail[hex_at..].chars().take(64).collect();
            spans.push((path.into(), a, b, hex));
            rest = &tail[hex_at + 64..];
            first = false;
        }
        assert!(!spans.is_empty(), "{}", f[0]);
        for (path, a, b, hex) in spans {
            let show = std::process::Command::new("git")
                .arg("-C")
                .arg(&oracle)
                .args([
                    "show",
                    &format!("f4e366e98a1f705dfaf182897044500d290dbd7f:{path}"),
                ])
                .output()
                .unwrap();
            assert!(show.status.success(), "{path}");
            let text = String::from_utf8(show.stdout).unwrap();
            let span: Vec<&str> = text.split('\n').skip(a - 1).take(b - a + 1).collect();
            let mut child = std::process::Command::new("shasum")
                .args(["-a", "256"])
                .stdin(std::process::Stdio::piped())
                .stdout(std::process::Stdio::piped())
                .spawn()
                .unwrap();
            use std::io::Write;
            child
                .stdin
                .take()
                .unwrap()
                .write_all(span.join("\n").as_bytes())
                .unwrap();
            let out = child.wait_with_output().unwrap();
            let got = String::from_utf8(out.stdout).unwrap();
            assert_eq!(
                got.split_whitespace().next().unwrap(),
                hex,
                "{} {path}:{a}-{b}",
                f[0]
            );
            checked += 1;
        }
    }
    assert_eq!(checked, 4, "1 record span + 2 reader spans + 1 call span");
}

#[test]
fn the_pending_count_is_pinned() {
    let rows = checked_in().unwrap();
    let reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    let pending = candidate_keys().unwrap().difference(&reviewed).count();
    assert_eq!(
        pending,
        1299 + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len()
    ); // exact new manual review is outside the bounds-only view // R17 +12 tuple-floor operands; manual reviews separate. // R16 +8 census operands; manual reviews counted separately. // +570 RU-051 keyword operands. // 739 - 2 RU-023 measured observations - 11 RU-024 ratio literals - 4 round-10 field-origin reads - 2 round-11 self-attribute reads (inventory_field_origins) - 2 round-11 OwnerTokenPath constructor forwards - 1 EntryAdmissionPolicy constructor forward - 1 round-11 self-attribute read (inventory_forward_callers, inventory_field_origins) - 6 round-11 slice 5 lease-deadline rows - 1 slice 6 float() forward - 2 slice 6 self-attribute reads - 11 round-12 validated-document-field rows (inventory_validated_document_field) - 4 round-12 RU-033 propagated source-unbounded rows (inventory_unbounded_propagation) + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)
}
