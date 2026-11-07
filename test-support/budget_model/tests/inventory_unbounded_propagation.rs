// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a7_physical_measurement.py; O:path:mycelium_a7_standby_qualification.py.
//! L1b round 12, RU-033: a row whose ONLY sources are source-unbounded rows is itself source-unbounded (RU-014)
//! and gets its own `Value::Blocked` registry id, pinned per row in `python-unbounded-propagation.tsv`.
//! Two shapes: a required keyword-only parameter forwarded unchanged (or under the builtin `float`) from every
//! call site (`required-parameter`), and a `self.F` read whose stores of the class are all such rows
//! (`self-attribute`). The numeric engines (`forward-callers`, `field-origin`) keep refusing Blocked targets: a
//! derived row may never point at a Blocked id (their tests are unchanged).
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
    Bound, Candidate, callers, candidate_keys, candidates, checked_in, field_origins, propagated,
    validate,
};
use budgets::{BudgetId, table::Provenance};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};

const ORACLE: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";
const PM: &str = "mycelium_a7_physical_measurement.py";
const SQ: &str = "mycelium_a7_standby_qualification.py";
const PA: &str = "mycelium_a7_parity_measurement.py";

/// (key, rule, own registry id, [(source key, source registry id)])
type Case = (
    &'static str,
    &'static str,
    &'static str,
    &'static [(&'static str, &'static str)],
);
const CASES: &[Case] = &[
    (
        "mycelium_a7_physical_measurement.py:1442:8:assignment:self._ready_timeout",
        "required-parameter",
        "PySourceUnboundedMyceliumA7PhysicalMeasurementL1442C8",
        &[(
            "mycelium_a7_physical_measurement.py:1573:12:keyword:ready_timeout_s",
            "PySourceUnboundedMyceliumA7PhysicalMeasurementL1573C12",
        )],
    ),
    (
        "mycelium_a7_physical_measurement.py:1443:8:assignment:self._stop_timeout",
        "required-parameter",
        "PySourceUnboundedMyceliumA7PhysicalMeasurementL1443C8",
        &[(
            "mycelium_a7_physical_measurement.py:1574:12:keyword:stop_timeout_s",
            "PySourceUnboundedMyceliumA7PhysicalMeasurementL1574C12",
        )],
    ),
    (
        "mycelium_a7_standby_qualification.py:340:6:dict-field:reserved_kv_bytes",
        "required-parameter",
        "PySourceUnboundedMyceliumA7StandbyQualificationL340C6",
        &[
            (
                "mycelium_a7_parity_measurement.py:234:12:keyword:reserved_kv_bytes",
                "PySourceUnboundedMyceliumA7ParityMeasurementL234C12",
            ),
            (
                "mycelium_a7_parity_measurement.py:269:6:keyword:reserved_kv_bytes",
                "PySourceUnboundedMyceliumA7ParityMeasurementL269C6",
            ),
        ],
    ),
    (
        "mycelium_a7_physical_measurement.py:1482:29:keyword:timeout",
        "self-attribute",
        "PySourceUnboundedMyceliumA7PhysicalMeasurementL1482C29",
        &[(
            "mycelium_a7_physical_measurement.py:1443:8:assignment:self._stop_timeout",
            "PySourceUnboundedMyceliumA7PhysicalMeasurementL1443C8",
        )],
    ),
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

fn ok_with(rows: &[Bound], map: &BTreeMap<String, Candidate>, key: &str, table: &str) -> bool {
    propagated::validate_with(row(rows, key), rows, map, table)
}

/// The table with every line of `key` rewritten by `edit` (the split fields in, the new fields out).
fn edit(key: &str, edit: impl Fn(Vec<String>) -> Vec<String>) -> String {
    propagated::TABLE
        .lines()
        .map(|l| {
            if l.starts_with(&format!("{key}\t")) {
                edit(l.split('\t').map(String::from).collect()).join("\t")
            } else {
                l.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
        + "\n"
}

fn rows_with(rows: &[Bound], key: &str, change: impl Fn(&mut Bound)) -> Vec<Bound> {
    let mut out = rows.to_vec();
    change(out.iter_mut().find(|r| r.key == key).unwrap());
    out
}

#[test]
fn four_rows_are_source_unbounded_by_propagation_with_their_own_blocked_ids() {
    let (rows, map) = world();
    assert_eq!(CASES.len(), 4);
    for (key, rule, id, sources) in CASES {
        let r = row(&rows, key);
        assert_eq!(r.disposition, "source-unbounded", "{key}");
        assert!(r.budget.is_empty() && r.value.is_none(), "{key}");
        assert_eq!(r.unit, "not-applicable", "{key}");
        assert_eq!(r.relation, *id, "{key}");
        assert!(
            r.review
                .starts_with(&format!("{}{rule};", propagated::PREFIX)),
            "{key}: {}",
            r.review
        );
        let Provenance::Blocked(text) = BudgetId::from_name(id).unwrap().entry().provenance else {
            panic!("{id}")
        };
        assert!(
            text.starts_with(&format!(
                "v0.1.4 ships no ceiling at {}:{} @ ef9ae61d; field {};",
                r.path, r.line, r.symbol
            )),
            "{id}: {text}"
        );
        assert!(text.contains("V03") && text.contains("propagated"), "{id}");
        assert!(propagated::validate(r, &rows, &map), "{key}");
        // Every source is a reviewed source-unbounded row with the independently listed id.
        for (source, source_id) in *sources {
            let s = row(&rows, source);
            assert_eq!(s.disposition, "source-unbounded", "{source}");
            assert_eq!(s.relation, *source_id, "{source}");
        }
    }
    let ids: BTreeSet<_> = CASES.iter().map(|c| c.2).collect();
    assert_eq!(ids.len(), 4, "one id per row");
    validate(&rows).unwrap();
    assert_eq!(
        propagated::sources_of(propagated::TABLE),
        CASES.iter().map(|c| c.0).collect::<BTreeSet<_>>()
    );
}

#[test]
fn the_numeric_engines_still_refuse_blocked_targets() {
    // A derived row may never point at a Blocked id: the callers and field-origin engines are unchanged.
    let (rows, map) = world();
    for (key, rule, _, _) in CASES {
        let r = row(&rows, key);
        let mut derived = r.clone();
        derived.disposition = "derived".into();
        if *rule == "required-parameter" {
            derived.review = "forward-callers:required-parameter; forged".into();
            assert!(!callers::validate(&derived, &rows, &map), "{key}");
        } else {
            derived.review = "field-origin:self-attribute; forged".into();
            assert!(!field_origins::validate(&derived, &rows, &map), "{key}");
        }
        // And the propagation engine refuses a derived row.
        assert!(!propagated::validate(&derived, &rows, &map), "{key}");
    }
}

#[test]
fn each_check_of_the_propagation_rule_is_refused_when_broken() {
    let (rows, map) = world();
    for (key, rule, id, sources) in CASES {
        assert!(ok_with(&rows, &map, key, propagated::TABLE), "{key}");
        let r = row(&rows, key);
        // Row mutations.
        for (what, change) in [
            (
                "derived",
                Box::new(|b: &mut Bound| b.disposition = "derived".into())
                    as Box<dyn Fn(&mut Bound)>,
            ),
            ("value", Box::new(|b: &mut Bound| b.value = Some(1))),
            (
                "budget",
                Box::new(|b: &mut Bound| b.budget = "RouterClose".into()),
            ),
            (
                "unit",
                Box::new(|b: &mut Bound| b.unit = "milliseconds".into()),
            ),
            (
                "other id",
                Box::new(|b: &mut Bound| b.relation = "CorrectedRestart".into()),
            ),
            (
                "valued id",
                Box::new(|b: &mut Bound| b.relation = "RouterClose".into()),
            ),
            (
                "foreign blocked id",
                Box::new(|b: &mut Bound| {
                    b.relation = "PySourceUnboundedMyceliumA10VerifyControlL109C8".into();
                }),
            ),
            (
                "review prefix",
                Box::new(|b: &mut Bound| b.review = "source-unbounded:v03; forged".into()),
            ),
            (
                "other rule",
                Box::new(|b: &mut Bound| {
                    b.review = format!("{}forged;", propagated::PREFIX);
                }),
            ),
            (
                "expression",
                Box::new(|b: &mut Bound| b.expression.push(' ')),
            ),
            ("scope", Box::new(|b: &mut Bound| b.scope.push('x'))),
        ] {
            let bad = rows_with(&rows, key, change);
            assert!(
                !propagated::validate_with(row(&bad, key), &bad, &map, propagated::TABLE),
                "{key}: {what}"
            );
        }
        let _ = (r, id);
        // Source mutations: a pending source, a numeric source, a source with another id.
        for (source, _) in *sources {
            let missing: Vec<Bound> = rows.iter().filter(|b| b.key != *source).cloned().collect();
            assert!(
                !propagated::validate_with(row(&missing, key), &missing, &map, propagated::TABLE),
                "{key}: pending source {source}"
            );
            let derived = rows_with(&rows, source, |b| b.disposition = "derived".into());
            assert!(
                !propagated::validate_with(row(&derived, key), &derived, &map, propagated::TABLE),
                "{key}: numeric source {source}"
            );
            let blank = rows_with(&rows, source, |b| b.relation = String::new());
            assert!(
                !propagated::validate_with(row(&blank, key), &blank, &map, propagated::TABLE),
                "{key}: source without an id {source}"
            );
            let unit = rows_with(&rows, source, |b| b.unit = "bytes".into());
            assert!(
                !propagated::validate_with(row(&unit, key), &unit, &map, propagated::TABLE),
                "{key}: source with a unit {source}"
            );
        }
        // Table mutations.
        let table_edits: Vec<(&str, String)> = vec![
            (
                "rule",
                edit(key, |mut c| {
                    c[1] = if *rule == "self-attribute" {
                        "required-parameter".into()
                    } else {
                        "self-attribute".into()
                    };
                    c
                }),
            ),
            (
                "unknown rule",
                edit(key, |mut c| {
                    c[1] = "forged".into();
                    c
                }),
            ),
            (
                "no sources",
                edit(key, |mut c| {
                    c[6] = String::new();
                    c
                }),
            ),
            (
                "extra source",
                edit(key, |mut c| {
                    c[6] = format!(
                        "{};{}",
                        c[6], "mycelium_a7_parity_measurement.py:272:6:keyword:ttl_ms"
                    );
                    c
                }),
            ),
            (
                "wrong source",
                edit(key, |mut c| {
                    c[6] = "mycelium_a7_parity_measurement.py:237:12:keyword:ttl_ms".into();
                    c
                }),
            ),
            (
                "field",
                edit(key, |mut c| {
                    c[5].push('x');
                    c
                }),
            ),
            (
                "class file",
                edit(key, |mut c| {
                    c[3] = "elsewhere.py".into();
                    c
                }),
            ),
            (
                "class",
                edit(key, |mut c| {
                    c[4].push('x');
                    c
                }),
            ),
            (
                "reference",
                edit(key, |mut c| {
                    c[7] = "forged".into();
                    c
                }),
            ),
            (
                "hash",
                edit(key, |mut c| {
                    c[7] = c[7].replacen("exact-span-sha256=", "exact-span-sha256=00", 1);
                    c
                }),
            ),
            (
                "oracle sha",
                edit(key, |mut c| {
                    c[7] = c[7].replacen(ORACLE, "0000000000000000000000000000000000000000", 1);
                    c
                }),
            ),
            ("header", propagated::TABLE.replacen("key\t", "k\t", 1)),
            ("empty", String::new()),
            (
                "missing line",
                propagated::TABLE
                    .lines()
                    .filter(|l| !l.starts_with(&format!("{key}\t")))
                    .collect::<Vec<_>>()
                    .join("\n")
                    + "\n",
            ),
            (
                "duplicate line",
                format!(
                    "{}{}\n",
                    propagated::TABLE,
                    propagated::TABLE
                        .lines()
                        .find(|l| l.starts_with(&format!("{key}\t")))
                        .unwrap()
                ),
            ),
        ];
        for (what, text) in table_edits {
            assert!(!ok_with(&rows, &map, key, &text), "{key}: table {what}");
        }
        // Source order must be ascending (canonical) when there are two sources.
        if sources.len() == 2 {
            let swapped = edit(key, |mut c| {
                let mut parts: Vec<String> = c[6].split(';').map(String::from).collect();
                parts.reverse();
                c[6] = parts.join(";");
                c
            });
            assert!(!ok_with(&rows, &map, key, &swapped), "{key}: source order");
        }
    }
}

#[test]
fn a_second_root_or_an_unlisted_store_is_refused() {
    let (rows, map) = world();
    // required-parameter: a census row for the parameter in the callee's scope is a second root.
    for key in [CASES[0].0, CASES[2].0] {
        let c = &map[key];
        let param = if key == CASES[0].0 {
            "ready_timeout_s"
        } else {
            "reserved_kv_bytes"
        };
        let mut forged = map.clone();
        let mut root = c.clone();
        root.key = format!("{}:1:1:parameter:{param}", c.path);
        root.kind = "parameter".into();
        root.symbol = param.into();
        forged.insert(root.key.clone(), root);
        assert!(
            !propagated::validate_with(row(&rows, key), &rows, &forged, propagated::TABLE),
            "{key}: second root"
        );
    }
    // self-attribute: a census store of the attribute in the class that is not listed.
    let key = CASES[3].0;
    let mut forged = map.clone();
    let mut store = map[CASES[1].0].clone();
    store.key = format!("{PM}:1450:8:assignment:self._stop_timeout");
    store.line = 1450;
    store.scope = "ServeProcess.stop".into();
    forged.insert(store.key.clone(), store);
    assert!(
        !propagated::validate_with(row(&rows, key), &rows, &forged, propagated::TABLE),
        "{key}: unlisted store"
    );
    // A forwarded value must be the parameter or float(parameter): any other shape is refused.
    for expression in [
        "float(ready_timeout_s) * 2",
        "min(ready_timeout_s, 30)",
        "int(ready_timeout_s)",
    ] {
        let mut forged = map.clone();
        forged.get_mut(CASES[0].0).unwrap().expression = expression.into();
        let bad = rows_with(&rows, CASES[0].0, |b| b.expression = expression.into());
        assert!(
            !propagated::validate_with(row(&bad, CASES[0].0), &bad, &forged, propagated::TABLE),
            "{expression}"
        );
    }
}

#[test]
fn identity_fields_of_the_row_must_equal_the_census_row() {
    let (rows, map) = world();
    for (key, _, _, _) in CASES {
        for (what, change) in [
            (
                "path",
                Box::new(|b: &mut Bound| b.path.push('x')) as Box<dyn Fn(&mut Bound)>,
            ),
            ("line", Box::new(|b: &mut Bound| b.line += 1)),
            ("column", Box::new(|b: &mut Bound| b.column += 1)),
            ("symbol", Box::new(|b: &mut Bound| b.symbol.push('x'))),
        ] {
            let bad = rows_with(&rows, key, change);
            assert!(
                !propagated::validate_with(row(&bad, key), &bad, &map, propagated::TABLE),
                "{key}: {what}"
            );
        }
    }
}

#[test]
fn a_forged_rule_is_refused_even_when_review_and_table_agree() {
    let (rows, map) = world();
    for (key, _, _, _) in CASES {
        let bad = rows_with(&rows, key, |b| {
            b.review = format!("{}forged;", propagated::PREFIX);
        });
        let table = edit(key, |mut c| {
            c[1] = "forged".into();
            c
        });
        assert!(
            !propagated::validate_with(row(&bad, key), &bad, &map, &table),
            "{key}"
        );
    }
}

/// Registry judgements through an injected lookup: the static table cannot be forged.
#[test]
fn each_registry_check_of_the_ids_is_refused_when_broken() {
    let (rows, map) = world();
    let real = |name: &str| {
        let entry = BudgetId::from_name(name)?.entry();
        let text = match entry.provenance {
            Provenance::Blocked(text) => text.to_string(),
            _ => String::new(),
        };
        Some((entry.value == budgets::table::Value::Blocked, text))
    };
    for (key, _, own, sources) in CASES {
        let r = row(&rows, key);
        let run = |lookup: &dyn Fn(&str) -> Option<(bool, String)>| {
            propagated::validate_with_registry(r, &rows, &map, propagated::TABLE, lookup)
        };
        assert!(run(&real), "{key}: baseline");
        let tweak = |id: &'static str, change: fn(String) -> String, blocked: bool| {
            move |name: &str| {
                real(name).map(|(b, t)| {
                    if name == id {
                        (blocked, change(t))
                    } else {
                        (b, t)
                    }
                })
            }
        };
        // The row's own id: not Blocked, an unknown id, another site, no V03, not propagated, no field terminator.
        assert!(!run(&tweak(own, |t| t, false)), "{key}: own id numeric");
        assert!(
            !run(&|name: &str| if name == *own { None } else { real(name) }),
            "{key}: unknown id"
        );
        assert!(
            !run(&tweak(
                own,
                |t| t.replace(" ships no ceiling at ", " ships no ceiling at x"),
                true
            )),
            "{key}: other site"
        );
        assert!(
            !run(&tweak(own, |t| t.replace("V03", "v03"), true)),
            "{key}: V03"
        );
        assert!(
            !run(&tweak(own, |t| t.replace("propagated", "inherited"), true)),
            "{key}: propagated"
        );
        assert!(
            !run(&tweak(own, |t| t.replace("; field ", "; fields "), true)),
            "{key}: field"
        );
        let symbol = map[*key].symbol.clone();
        let longer = move |name: &str| {
            real(name).map(|(b, t)| {
                if name == *own {
                    (
                        b,
                        t.replace(&format!("field {symbol};"), &format!("field {symbol}x;")),
                    )
                } else {
                    (b, t)
                }
            })
        };
        assert!(!run(&longer), "{key}: field name must end at the semicolon");
        assert!(
            !run(&tweak(own, |t| t.replace("ef9ae61d", "00000000"), true)),
            "{key}: pin"
        );
        // A source whose id is not a Blocked registry entry is refused.
        for (_, source_id) in *sources {
            assert!(
                !run(&tweak(source_id, |t| t, false)),
                "{key}: numeric source id"
            );
            assert!(
                !run(&|name: &str| if name == *source_id { None } else { real(name) }),
                "{key}: unknown source id"
            );
        }
    }
}

#[test]
fn a_source_sharing_the_rows_id_or_a_two_row_cycle_is_refused() {
    let (rows, map) = world();
    // A source reviewed with the row's own id.
    for (key, _, own, sources) in CASES {
        for (source, _) in *sources {
            let bad = rows_with(&rows, source, |b| b.relation = (*own).into());
            assert!(
                !propagated::validate_with(row(&bad, key), &bad, &map, propagated::TABLE),
                "{key}: source {source} shares the id"
            );
        }
    }
    // The self-attribute read lists the store; the store's entry naming the read back is a cycle.
    let (read, store) = (CASES[3].0, CASES[3].3[0].0);
    let cyclic = edit(store, |mut c| {
        c[6] = format!("{read};{}", c[6]);
        c
    });
    assert!(ok_with(&rows, &map, read, propagated::TABLE));
    assert!(!ok_with(&rows, &map, read, &cyclic), "two-row cycle");
}

#[test]
fn a_second_self_root_in_the_method_is_refused() {
    let (rows, map) = world();
    let key = CASES[3].0;
    let mut forged = map.clone();
    let mut root = map[key].clone();
    root.key = format!("{PM}:1475:8:parameter:self");
    root.kind = "parameter".into();
    root.symbol = "self".into();
    forged.insert(root.key.clone(), root);
    assert!(!propagated::validate_with(
        row(&rows, key),
        &rows,
        &forged,
        propagated::TABLE
    ));
}

/// The pinned reference of `key` with `change` applied to its ` | `-separated spans.
fn respan(key: &str, change: impl Fn(Vec<String>) -> Vec<String>) -> String {
    edit(key, |mut c| {
        c[7] = change(c[7].split(" | ").map(String::from).collect()).join(" | ");
        c
    })
}

#[test]
fn span_labels_paths_and_geometry_are_each_refused_when_broken() {
    let (rows, map) = world();
    for (key, rule, _, sources) in CASES.iter() {
        let spans = if *rule == "self-attribute" {
            3
        } else {
            sources.len()
        };
        for i in 0..spans {
            // The label of every span.
            let m = respan(key, |mut s| {
                let (_, rest) = s[i].split_once(' ').unwrap();
                s[i] = format!("forged {rest}");
                s
            });
            assert!(!ok_with(&rows, &map, key, &m), "{key}: label of span {i}");
            // The path of every span (the span stays well formed).
            let m = respan(key, |mut s| {
                let (label, rest) = s[i].split_once(' ').unwrap();
                let (_, lines) = rest.split_once(':').unwrap();
                s[i] = format!("{label} elsewhere.py:{lines}");
                s
            });
            assert!(!ok_with(&rows, &map, key, &m), "{key}: path of span {i}");
        }
        // A span count that does not match the sources.
        let m = respan(key, |mut s| {
            s.push(s[s.len() - 1].clone());
            s
        });
        assert!(!ok_with(&rows, &map, key, &m), "{key}: extra span");
        let m = respan(key, |mut s| {
            s.pop();
            s
        });
        assert!(!ok_with(&rows, &map, key, &m), "{key}: missing span");
        // A duplicated source (with its span duplicated too) is refused by the ascending-distinct rule alone.
        let m = edit(key, |mut c| {
            let first = c[6].split(';').next().unwrap().to_string();
            c[6] = format!("{first};{first}");
            let mut parts: Vec<String> = c[7].split(" | ").map(String::from).collect();
            parts.push(parts[parts.len() - 1].clone());
            if *rule != "self-attribute" {
                parts = vec![parts[0].clone(), parts[0].clone()];
            }
            c[7] = parts.join(" | ");
            c
        });
        assert!(!ok_with(&rows, &map, key, &m), "{key}: duplicated source");
        // The receiver column: `-` for a parameter, `self` for an attribute.
        let m = edit(key, |mut c| {
            c[2] = if *rule == "self-attribute" {
                "-".into()
            } else {
                "self".into()
            };
            c
        });
        assert!(!ok_with(&rows, &map, key, &m), "{key}: receiver");
        // Source candidates of the wrong kind, symbol or file; a source row with a budget or a value.
        for (source, _) in *sources {
            let mut forged = map.clone();
            forged.get_mut(*source).unwrap().kind = if *rule == "self-attribute" {
                "keyword".into()
            } else {
                "assignment".into()
            };
            assert!(
                !propagated::validate_with(row(&rows, key), &rows, &forged, propagated::TABLE),
                "{key}: source kind {source}"
            );
            let mut forged = map.clone();
            forged.get_mut(*source).unwrap().symbol.push('x');
            assert!(
                !propagated::validate_with(row(&rows, key), &rows, &forged, propagated::TABLE),
                "{key}: source symbol {source}"
            );
            let mut forged = map.clone();
            forged.get_mut(*source).unwrap().path = "elsewhere.py".into();
            assert!(
                !propagated::validate_with(row(&rows, key), &rows, &forged, propagated::TABLE),
                "{key}: source path {source}"
            );
            for change in [
                (|b: &mut Bound| b.budget = "RouterClose".into()) as fn(&mut Bound),
                |b: &mut Bound| b.value = Some(1),
            ] {
                let bad = rows_with(&rows, source, change);
                assert!(
                    !propagated::validate_with(row(&bad, key), &bad, &map, propagated::TABLE),
                    "{key}: source with a budget or value {source}"
                );
            }
        }
    }
}

#[test]
fn the_self_attribute_geometry_is_refused_when_broken() {
    let (rows, map) = world();
    let key = CASES[3].0;
    // Spans: class 1431-1486, method 1475-1486, store 1443-1443.
    let moved = |index: usize, from: &str, to: &str| {
        respan(key, |mut s| {
            s[index] = s[index].replacen(from, to, 1);
            s
        })
    };
    for (what, text) in [
        (
            "method ends after the class",
            moved(1, ":1475-1486", ":1475-1490"),
        ),
        (
            "method starts before the class",
            moved(1, ":1475-1486", ":1430-1486"),
        ),
        (
            "method starts after the read",
            moved(1, ":1475-1486", ":1483-1486"),
        ),
        (
            "method ends before the read",
            moved(1, ":1475-1486", ":1475-1481"),
        ),
        (
            "store span ends before the writer",
            moved(2, ":1443-1443", ":1440-1441"),
        ),
        (
            "store span misses the writer",
            moved(2, ":1443-1443", ":1444-1444"),
        ),
        (
            "store span outside the class",
            moved(2, ":1443-1443", ":1430-1443"),
        ),
        (
            "class ends before the method",
            moved(0, ":1431-1486", ":1431-1470"),
        ),
    ] {
        assert!(!ok_with(&rows, &map, key, &text), "{what}");
    }
    // The row's expression must be exactly self.<field>, and its scope inside the class.
    let mut forged = map.clone();
    let bad = rows_with(&rows, key, |b| b.expression = "self._other".into());
    forged.get_mut(key).unwrap().expression = "self._other".into();
    assert!(!propagated::validate_with(
        row(&bad, key),
        &bad,
        &forged,
        propagated::TABLE
    ));
    let mut forged = map.clone();
    let bad = rows_with(&rows, key, |b| b.scope = "Other.stop".into());
    forged.get_mut(key).unwrap().scope = "Other.stop".into();
    assert!(!propagated::validate_with(
        row(&bad, key),
        &bad,
        &forged,
        propagated::TABLE
    ));
    // The listed store must sit in the class scope.
    let mut forged = map.clone();
    forged.get_mut(CASES[3].3[0].0).unwrap().scope = "Other.__init__".into();
    assert!(!propagated::validate_with(
        row(&rows, key),
        &rows,
        &forged,
        propagated::TABLE
    ));
}

#[test]
fn a_same_named_parameter_in_another_scope_is_not_a_second_root() {
    let (rows, map) = world();
    for (key, param) in [
        (CASES[0].0, "ready_timeout_s"),
        (CASES[2].0, "reserved_kv_bytes"),
    ] {
        let c = &map[key];
        let mut forged = map.clone();
        let mut other = c.clone();
        other.key = format!("{}:1:1:parameter:{param}", c.path);
        other.kind = "parameter".into();
        other.symbol = param.into();
        other.scope = "SomethingElse.method".into();
        forged.insert(other.key.clone(), other);
        assert!(
            propagated::validate_with(row(&rows, key), &rows, &forged, propagated::TABLE),
            "{key}: a parameter of another function is not a root of this one"
        );
        // An assignment to the parameter in the same scope IS a second root.
        let mut forged = map.clone();
        let mut rebind = c.clone();
        rebind.key = format!("{}:1:1:assignment:{param}", c.path);
        rebind.kind = "assignment".into();
        rebind.symbol = param.into();
        forged.insert(rebind.key.clone(), rebind);
        assert!(
            !propagated::validate_with(row(&rows, key), &rows, &forged, propagated::TABLE),
            "{key}: rebinding"
        );
    }
}

#[test]
fn neighbours_that_are_not_pure_propagations_stay_pending() {
    // `ready_timeout_s`'s sibling readers and the other STAY_PENDING neighbours are not touched.
    let (rows, _) = world();
    let reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.as_str()).collect();
    let pending = candidate_keys().unwrap();
    for key in [
        // callee name defined more than once
        "mycelium_a13_macos/controller_staging.py:82:65:keyword:timeout_seconds",
        // a source-unbounded call site that only partly forwards (the constructor of the other class)
        "mycelium_a7_physical_measurement.py:1573:12:keyword:ready_timeout_s",
    ] {
        assert!(pending.contains(key), "{key}");
    }
    assert!(
        !reviewed
            .contains("mycelium_a13_macos/controller_staging.py:82:65:keyword:timeout_seconds")
    );
}

// ----- oracle facts the census cannot show ------------------------------------------------------------------

fn oracle_text(path: &str) -> String {
    let show = std::process::Command::new("git")
        .arg("-C")
        .arg(oracle_blobs::oracle_dir_or_panic())
        .args(["show", &format!("{ORACLE}:{path}")])
        .output()
        .unwrap();
    assert!(show.status.success(), "{path}");
    String::from_utf8(show.stdout).unwrap()
}

fn span(text: &str, first: usize, last: usize) -> Vec<&str> {
    text.split('\n')
        .skip(first - 1)
        .take(last - first + 1)
        .collect()
}

fn sha(text: &str, first: usize, last: usize) -> String {
    Sha256::digest(span(text, first, last).join("\n").as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

/// (file, line) of every in-scope line that contains `needle`.
fn mentions(needle: &str) -> Vec<(String, usize)> {
    let mut out = Vec::new();
    for path in include_str!("../data/python-scope.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
    {
        for (i, l) in oracle_text(path).split('\n').enumerate() {
            if l.contains(needle) {
                out.push((path.to_string(), i + 1));
            }
        }
    }
    out
}

fn pinned_span(key: &str, index: usize) -> (String, usize, usize, String) {
    let line = propagated::TABLE
        .lines()
        .find(|l| l.starts_with(&format!("{key}\t")))
        .unwrap();
    let reference = line.split('\t').nth(7).unwrap();
    let s = reference.split(" | ").nth(index).unwrap();
    let (location, rest) = s.split_once(" @ ").unwrap();
    let (_, location) = location.split_once(' ').unwrap();
    let (path, lines) = location.rsplit_once(':').unwrap();
    let (a, b) = lines.split_once('-').unwrap();
    let hash = rest.split_once("exact-span-sha256=").unwrap().1.to_string();
    (
        path.to_string(),
        a.parse().unwrap(),
        b.parse().unwrap(),
        hash,
    )
}

fn call_arguments(text: &str) -> Vec<String> {
    let (mut depth, mut current, mut out) = (0usize, String::new(), Vec::new());
    for ch in text.chars() {
        match ch {
            '(' | '[' | '{' => {
                depth += 1;
                current.push(ch);
            }
            ')' | ']' | '}' if depth == 0 => break,
            ')' | ']' | '}' => {
                depth -= 1;
                current.push(ch);
            }
            ',' if depth == 0 => out.push(std::mem::take(&mut current)),
            _ => current.push(ch),
        }
    }
    if !current.trim().is_empty() {
        out.push(current);
    }
    out.into_iter().map(|a| a.trim().to_string()).collect()
}

#[test]
fn serve_process_constructor_facts_hold_in_the_oracle() {
    let text = oracle_text(PM);
    // Plain class, defined once; `__init__` takes both timeouts keyword-only and without a default.
    assert_eq!(span(&text, 1431, 1431)[0], "class ServeProcess:");
    assert!(!span(&text, 1430, 1430)[0].trim_start().starts_with('@'));
    let signature = span(&text, 1436, 1436)[0];
    assert!(
        signature.contains(
            "*, cwd: str, log_path: Path, ready_timeout_s: float, stop_timeout_s: float) -> None:"
        ),
        "{signature}"
    );
    // Each parameter occurs only as: the signature, the one store line, the pinned call keyword, the
    // `_SERVE_FIELDS` document key, and the call keyword's own key text.
    let body = span(&text, 1436, 1486);
    for (param, store) in [("ready_timeout_s", 1442usize), ("stop_timeout_s", 1443)] {
        let hits: Vec<usize> = body
            .iter()
            .enumerate()
            .filter(|(_, l)| l.contains(param))
            .map(|(i, _)| 1436 + i)
            .collect();
        assert_eq!(hits, vec![1436, store], "{param} inside ServeProcess");
        let line = span(&text, store, store)[0].trim();
        assert!(
            line.ends_with(&format!("= float({param})")),
            "{param}: {line}"
        );
    }
    // The builtin `float` is never rebound in the file.
    for (n, l) in text.split('\n').enumerate() {
        let t = l.trim_start();
        assert!(
            !(t.starts_with("def float")
                || t.starts_with("float =")
                || t.starts_with("float=")
                || t.starts_with("class float")
                || l.contains(" as float")),
            "{n}: {l}"
        );
    }
    // Class name occurrences: definition, the annotation `serve: ServeProcess | None`, the one call and the
    // `__all__` string; nothing subclasses or aliases it.
    let got = mentions("ServeProcess");
    assert_eq!(
        got.iter().map(|m| (m.0.as_str(), m.1)).collect::<Vec<_>>(),
        vec![(PM, 1431), (PM, 1563), (PM, 1569), (PM, 1601)]
    );
    // The call passes only `argv` positionally; the two timeouts are keywords (pinned span hash).
    let (path, a, b, hash) = pinned_span(CASES[0].0, 0);
    assert_eq!((path.as_str(), a, b), (PM, 1569, 1575));
    assert_eq!(sha(&text, a, b), hash);
    let call = span(&text, a, b).join("\n");
    let args = call_arguments(call.split_once("ServeProcess(").unwrap().1);
    assert_eq!(args.len(), 5, "{args:?}");
    assert!(
        !args[0].contains('=') && !args[0].starts_with('*'),
        "{args:?}"
    );
    assert!(args[1..].iter().all(|x| !x.starts_with('*')), "{args:?}");
    assert!(args.contains(&"ready_timeout_s=float(serve_config[\"ready_timeout_s\"])".to_string()));
    assert!(args.contains(&"stop_timeout_s=float(serve_config[\"stop_timeout_s\"])".to_string()));
    // The second pinned key shares the same call span.
    assert_eq!(pinned_span(CASES[1].0, 0).3, hash);
}

#[test]
fn serve_process_stop_timeout_is_stored_once_and_read_in_stop() {
    let text = oracle_text(PM);
    let (cpath, cfirst, clast, _) = pinned_span(CASES[3].0, 0);
    let (mpath, mfirst, mlast, _) = pinned_span(CASES[3].0, 1);
    let (spath, sfirst, slast, shash) = pinned_span(CASES[3].0, 2);
    assert_eq!(
        (cpath.as_str(), mpath.as_str(), spath.as_str()),
        (PM, PM, PM)
    );
    assert_eq!(
        (cfirst, clast, mfirst, mlast, sfirst, slast),
        (1431, 1486, 1475, 1486, 1443, 1443)
    );
    assert_eq!(sha(&text, sfirst, slast), shash);
    assert_eq!(span(&text, cfirst, cfirst)[0], "class ServeProcess:");
    let head = span(&text, mfirst, mfirst)[0].replace(' ', "");
    assert!(head.starts_with("defstop(self"), "{head}");
    assert!(
        !span(&text, mfirst - 1, mfirst - 1)[0]
            .trim_start()
            .starts_with('@')
    );
    // Every mention of the attribute in the class is the one store or the one read; `self` is never rebound and
    // nothing writes attributes dynamically (class body and file).
    let hits: Vec<usize> = span(&text, cfirst, clast)
        .iter()
        .enumerate()
        .filter(|(_, l)| l.contains("_stop_timeout"))
        .map(|(i, _)| cfirst + i)
        .collect();
    assert_eq!(hits, vec![1443, 1482]);
    assert_eq!(
        span(&text, 1443, 1443)[0].trim(),
        "self._stop_timeout = float(stop_timeout_s)"
    );
    assert!(span(&text, 1482, 1482)[0].contains("process.wait(timeout=self._stop_timeout)"));
    for (offset, l) in span(&text, mfirst, mlast).iter().enumerate() {
        for banned in [
            "self =",
            "global self",
            "nonlocal self",
            "del self",
            "as self",
            "for self",
        ] {
            assert!(!l.contains(banned), "{}: {l}", mfirst + offset);
        }
    }
    for banned in ["setattr(", "__dict__", "vars(", "object.__setattr__"] {
        assert!(!text.contains(banned), "{banned} occurs in {PM}");
    }
    // No other in-scope file reads or writes the attribute (other files only name `rebuild_stop_timeout` codes).
    for (path, line) in mentions("._stop_timeout") {
        assert_eq!(path, PM, "{path}:{line}");
    }
}

#[test]
fn qualify_standby_forwarding_facts_hold_in_the_oracle() {
    let text = oracle_text(SQ);
    let caller = oracle_text(PA);
    // `def qualify_standby(` once; every parameter keyword-only (`*,` first) and `reserved_kv_bytes` has no default.
    assert_eq!(span(&text, 272, 272)[0], "def qualify_standby(");
    assert_eq!(span(&text, 273, 273)[0].trim(), "*,");
    assert_eq!(span(&text, 277, 277)[0].trim(), "reserved_kv_bytes: int,");
    // The parameter's only mentions in the function: the signature, the validation, the resource comparison and
    // the record field.
    let end = text.split('\n').count();
    let hits: Vec<usize> = span(&text, 272, end)
        .iter()
        .enumerate()
        .filter(|(_, l)| l.contains("reserved_kv_bytes"))
        .map(|(i, _)| 272 + i)
        .collect();
    assert_eq!(hits, vec![277, 284, 310, 340], "{hits:?}");
    // :310 compares the parameter with the available bytes (a runtime quantity), it does not modify it.
    assert_eq!(
        span(&text, 310, 310)[0].trim(),
        "resource_ok = available_bytes is not None and reserved_kv_bytes <= available_bytes"
    );
    assert_eq!(
        span(&text, 340, 340)[0].trim(),
        "\"reserved_kv_bytes\": reserved_kv_bytes,"
    );
    assert!(
        span(&text, 284, 284)[0]
            .contains("type(reserved_kv_bytes) is not int or reserved_kv_bytes < 1")
    );
    // The callee name occurs exactly at: the import, the definition, the two calls, the module docstring.
    let got = mentions("qualify_standby");
    assert_eq!(
        got.iter().map(|m| (m.0.as_str(), m.1)).collect::<Vec<_>>(),
        vec![(PA, 22), (PA, 43), (PA, 228), (PA, 265), (SQ, 272)]
    );
    assert!(span(&caller, 22, 22)[0].contains("(``qualify_standby``)"));
    assert!(span(&caller, 43, 43)[0].trim() == "qualify_standby,");
    // Both calls pass only keywords, and the pinned spans hash to the oracle.
    for (index, (first, last)) in [(228usize, 238usize), (265, 273)].into_iter().enumerate() {
        let (path, a, b, hash) = pinned_span(CASES[2].0, index);
        assert_eq!((path.as_str(), a, b), (PA, first, last));
        assert_eq!(sha(&caller, a, b), hash);
        let call = span(&caller, a, b).join("\n");
        let args = call_arguments(call.split_once("qualify_standby(").unwrap().1);
        assert_eq!(args.len(), 7, "{args:?}");
        for arg in &args {
            let (name, _) = arg
                .split_once('=')
                .unwrap_or_else(|| panic!("positional {arg}"));
            assert!(
                !name.is_empty() && name.bytes().all(|c| c.is_ascii_alphanumeric() || c == b'_'),
                "{arg}"
            );
        }
        assert!(args.contains(
            &"reserved_kv_bytes=config[\"qualification\"][\"reserved_kv_bytes\"]".to_string()
        ));
    }
}

#[test]
fn total_pending_after_the_propagation_rule() {
    let reviewed: BTreeSet<_> = checked_in().unwrap().into_iter().map(|r| r.key).collect();
    let pending = candidate_keys().unwrap().difference(&reviewed).count();
    assert_eq!(
        pending,
        1299 + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len()
    ); // exact new manual review is outside the bounds-only view // R17 +12 tuple-floor operands; manual reviews separate. // R16 +8 census operands; manual reviews counted separately. // +570 RU-051 keyword operands. // 696 - 4 RU-033 propagated source-unbounded rows + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)
}
