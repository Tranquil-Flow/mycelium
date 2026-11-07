// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:mycelium_a13_macos/app.py.
//! L1b round 8, rule `r2-validated-input-binding` (same-scope guard): a request-body field read after a
//! dominating negated range check of the same field in the same function is bound to that check's reviewed
//! upper-guard row (`app.py` `30 <= body['max_seconds'] <= 1800`). The guard is an `if ... or not <chain>:
//! raise` statement that is a preceding sibling of the use's statement chain, with no rebinding or mutation
//! of the container in between (pinned AST evidence at f4e366e9, exact span sha); the census cannot show the
//! dominance, so the table pins it, and Rust re-checks everything the census can show.
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
    Bound, Candidate, candidate_keys, candidates, checked_in, input_guards, validate,
};
use std::collections::{BTreeMap, BTreeSet};

/// (use key, guard key, registry id of the guard, if line, container, field)
const CASES: &[(&str, &str, &str, &str, usize)] = &[
    (
        "mycelium_a13_macos/app.py:713:42:keyword:max_seconds",
        "mycelium_a13_macos/app.py:704:59:comparison:30",
        "PyUpperGuardMyceliumA13MacosAppL704C59",
        "body",
        703,
    ),
    (
        "mycelium_a13_macos/app.py:1462:52:keyword:max_seconds",
        "mycelium_a13_macos/app.py:1449:67:comparison:30",
        "PyUpperGuardMyceliumA13MacosAppL1449C67",
        "body",
        1446,
    ),
];

/// Same-looking rows that must stay pending (reason).
const STAY_PENDING: &[&str] = &[
    // callee-side guard: the dict is rebuilt and passed to a method that checks it (needs a callee guard)
    "mycelium_a13_macos/app.py:1597:73:dict-field:max_seconds",
    // symbolic/no negated literal range of this field in the function
    "mycelium_a13_macos/app.py:1672:70:keyword:ttl_seconds",
    "mycelium_a13_macos/app.py:1715:91:keyword:ttl_seconds",
    // policy document read, validated elsewhere (read_resource_policy), not by a same-scope guard
    "mycelium_a13_macos/app.py:1546:24:keyword:max_disk_bytes",
    "mycelium_a13_macos/app.py:994:58:keyword:storage_limit_bytes",
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

fn table(keep: impl Fn(&str) -> bool, extra: &[String]) -> String {
    let mut out: Vec<String> = input_guards::EDGES
        .lines()
        .filter(|l| l.starts_with("key\t") || keep(l))
        .map(String::from)
        .collect();
    out.extend(extra.iter().cloned());
    out.join("\n") + "\n"
}

#[test]
fn two_field_reads_derive_from_their_dominating_upper_guard() {
    let (rows, map) = world();
    for (key, guard, id, container, if_line) in CASES {
        let r = row(&rows, key);
        assert_eq!(r.disposition, "derived", "{key}");
        assert!(r.budget.is_empty() && r.value.is_none(), "{key}");
        assert_eq!(r.unit, "milliseconds", "{key}");
        assert_eq!(r.relation, *id, "{key}");
        assert!(
            r.review
                .starts_with(&format!("{}{};", input_guards::PREFIX, input_guards::RULE)),
            "{key}"
        );
        assert!(input_guards::validate(r, &rows, &map), "{key}");
        // The guard is the reviewed upper-guard row of the same field, unit and function.
        let g = row(&rows, guard);
        assert_eq!(g.budget, *id, "{guard}");
        assert_eq!(g.unit, "milliseconds");
        assert_eq!(map[*guard].scope, map[*key].scope);
        assert_eq!(map[*key].expression, format!("{container}['max_seconds']"));
        assert!(map[*guard].expression.contains("body['max_seconds']"));
        // Pinned dominance evidence.
        let line = input_guards::EDGES
            .lines()
            .find(|l| l.starts_with(&format!("{key}\t")))
            .unwrap();
        let f: Vec<_> = line.split('\t').collect();
        assert_eq!(f[1], *guard);
        assert_eq!(f[2], input_guards::RULE);
        assert_eq!(f[3], if_line.to_string());
        assert_eq!((f[4], f[5]), (*container, "max_seconds"));
        assert!(f[6].contains("exact-span-sha256="), "{key}");
        assert!(
            f[6].contains("@ f4e366e98a1f705dfaf182897044500d290dbd7f"),
            "{key}"
        );
    }
    assert!(validate(&rows).is_ok());
    let sources: BTreeSet<_> = input_guards::EDGES
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(sources, CASES.iter().map(|c| c.0).collect::<BTreeSet<_>>());
}

#[test]
fn a_missing_duplicated_or_reheaded_edge_is_refused() {
    let (rows, map) = world();
    for (key, ..) in CASES {
        let r = row(&rows, key);
        let ok = |text: &str| input_guards::validate_with(r, &rows, &map, text);
        assert!(ok(input_guards::EDGES), "{key} baseline");
        assert!(!ok(&table(|l| !l.starts_with(*key), &[])), "{key} no edge");
        let dup = input_guards::EDGES
            .lines()
            .find(|l| l.starts_with(*key))
            .unwrap()
            .to_string();
        assert!(!ok(&table(|_| true, &[dup])), "{key} duplicate edge");
        assert!(!ok(&input_guards::EDGES.replacen("key\t", "k\t", 1)));
        assert!(!ok(""));
        // A line with a missing or empty column.
        let short = input_guards::EDGES
            .lines()
            .find(|l| l.starts_with(*key))
            .unwrap()
            .rsplit_once('\t')
            .unwrap()
            .0
            .to_string();
        assert!(
            !ok(&table(|l| !l.starts_with(*key), &[short])),
            "{key} short"
        );
    }
}

#[test]
fn each_edge_column_is_checked_against_the_census_and_the_rows() {
    let (rows, map) = world();
    let (key, guard, id, ..) = CASES[0];
    let r = row(&rows, key).clone();
    let ok = |text: &str| input_guards::validate_with(&r, &rows, &map, text);
    let line = input_guards::EDGES
        .lines()
        .find(|l| l.starts_with(key))
        .unwrap()
        .to_string();
    let f: Vec<String> = line.split('\t').map(String::from).collect();
    let edit = |i: usize, v: &str| {
        let mut g = f.clone();
        g[i] = v.to_string();
        table(|l| !l.starts_with(key), &[g.join("\t")])
    };
    assert!(!ok(&edit(1, CASES[1].1)), "guard of another function");
    assert!(
        !ok(&edit(1, "mycelium_a13_macos/app.py:704:33:comparison:30")),
        "not a census guard"
    );
    assert!(!ok(&edit(2, "required-parameter")), "rule");
    assert!(!ok(&edit(3, "710")), "if line after the guard comparison");
    assert!(!ok(&edit(3, "0")), "if line zero");
    assert!(!ok(&edit(3, "x")), "if line not a number");
    assert!(!ok(&edit(4, "spec")), "container");
    assert!(!ok(&edit(5, "max_disk")), "field");
    assert!(!ok(&edit(6, "no sha here")), "evidence without a span hash");
    // Row-side mutations.
    let mut m = r.clone();
    m.relation = CASES[1].2.to_string();
    assert!(
        !input_guards::validate_with(&m, &with(&rows, m.clone()), &map, input_guards::EDGES),
        "another guard's id"
    );
    let mut m = r.clone();
    m.unit = "bytes".into();
    assert!(
        !input_guards::validate_with(&m, &with(&rows, m.clone()), &map, input_guards::EDGES),
        "unit"
    );
    let mut m = r.clone();
    m.value = Some(1_800_000);
    assert!(
        !input_guards::validate_with(&m, &with(&rows, m.clone()), &map, input_guards::EDGES),
        "value"
    );
    let mut m = r.clone();
    m.budget = id.to_string();
    m.disposition = String::new();
    assert!(
        !input_guards::validate_with(&m, &with(&rows, m.clone()), &map, input_guards::EDGES),
        "budget instead of derived"
    );
    let mut m = r.clone();
    m.review = m.review.replacen(input_guards::RULE, "other-rule", 1);
    assert!(
        !input_guards::validate_with(&m, &with(&rows, m.clone()), &map, input_guards::EDGES),
        "review rule"
    );
    let mut m = r.clone();
    m.expression = "body['other']".into();
    assert!(
        !input_guards::validate_with(&m, &with(&rows, m.clone()), &map, input_guards::EDGES),
        "expression"
    );
    // The guard row must be reviewed: with it removed the use cannot derive.
    let reduced: Vec<Bound> = rows.iter().filter(|b| b.key != guard).cloned().collect();
    assert!(
        !input_guards::validate_with(&r, &reduced, &map, input_guards::EDGES),
        "pending guard"
    );
    assert!(ok(input_guards::EDGES));
}

#[test]
fn census_mutations_of_the_use_and_the_guard_are_each_refused() {
    let (rows, map) = world();
    let (key, guard, ..) = CASES[0];
    let r = row(&rows, key);
    let check = |what: &str, f: &dyn Fn(&mut BTreeMap<String, Candidate>)| {
        let mut m = map.clone();
        f(&mut m);
        assert!(
            !input_guards::validate_with(r, &rows, &m, input_guards::EDGES),
            "{what}"
        );
    };
    check("use kind", &|m| {
        m.get_mut(key).unwrap().kind = "comparison".into()
    });
    check("use not a subscript", &|m| {
        m.get_mut(key).unwrap().expression = "body".into()
    });
    check("use subscript field", &|m| {
        m.get_mut(key).unwrap().expression = "body['other']".into()
    });
    check("use after the container call", &|m| {
        m.get_mut(key).unwrap().expression = "body.get('max_seconds')".into()
    });
    check("guard kind", &|m| {
        m.get_mut(guard).unwrap().kind = "keyword".into()
    });
    check("guard path", &|m| {
        m.get_mut(guard).unwrap().path = "mycelium_a13_macos/other.py".into()
    });
    check("guard scope", &|m| {
        m.get_mut(guard).unwrap().scope = "DemoApp._action".into()
    });
    check("guard after the use", &|m| {
        m.get_mut(guard).unwrap().line = 714
    });
    check("guard on another field", &|m| {
        let g = m.get_mut(guard).unwrap();
        g.expression = g.expression.replace("max_seconds", "other");
    });
    check("guard not negated", &|m| {
        let g = m.get_mut(guard).unwrap();
        g.source = g.source.replace("not 30<=", "30<=");
    });
    check("guard chain differs from the pinned literal range", &|m| {
        let g = m.get_mut(guard).unwrap();
        g.source = g.source.replace("<=1800", "<=1801");
    });
    check("guard is a lower-only compare", &|m| {
        let g = m.get_mut(guard).unwrap();
        g.expression = "30 <= body['max_seconds']".into();
    });
    // A census assignment to the container between the guard and the use (second root) is refused.
    let mut m = map.clone();
    let mut extra = m[key].clone();
    extra.key = "mycelium_a13_macos/app.py:710:8:assignment:body".into();
    extra.line = 710;
    extra.kind = "assignment".into();
    extra.symbol = "body".into();
    extra.expression = "{}".into();
    m.insert(extra.key.clone(), extra);
    assert!(
        !input_guards::validate_with(r, &rows, &m, input_guards::EDGES),
        "rebinding after the guard"
    );
}

#[test]
fn forged_rows_on_neighbours_are_refused_and_the_neighbours_stay_pending() {
    let (rows, map) = world();
    let reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.as_str()).collect();
    for key in STAY_PENDING {
        assert!(!reviewed.contains(key), "{key} must stay pending");
        assert!(map.contains_key(*key), "{key} census");
    }
    // Forge: copy the first case's row and edge onto a neighbour with the same field name.
    let neighbour = STAY_PENDING[0];
    let c = &map[neighbour];
    let mut forged = row(&rows, CASES[0].0).clone();
    forged.key = neighbour.to_string();
    forged.path = c.path.clone();
    forged.line = c.line;
    forged.column = c.column;
    forged.scope = c.scope.clone();
    forged.symbol = c.symbol.clone();
    forged.expression = c.expression.clone();
    let line = input_guards::EDGES
        .lines()
        .find(|l| l.starts_with(CASES[0].0))
        .unwrap()
        .replacen(CASES[0].0, neighbour, 1);
    let text = table(|_| true, &[line]);
    assert!(
        !input_guards::validate_with(&forged, &rows, &map, &text),
        "guard of another function"
    );
    assert!(
        !input_guards::validate_with(&forged, &rows, &map, input_guards::EDGES),
        "no edge"
    );
    // The remaining validated-input rows: none is a derived input-guard row.
    let kinds: BTreeSet<_> = rows
        .iter()
        .filter(|r| r.review.starts_with(input_guards::PREFIX))
        .map(|r| r.key.as_str())
        .collect();
    assert_eq!(kinds, CASES.iter().map(|c| c.0).collect::<BTreeSet<_>>());
}

#[test]
fn single_target_binding_validator_never_admits_these_rows_and_pending_is_pinned() {
    let (rows, map) = world();
    for (key, ..) in CASES {
        assert!(
            !budget_model::inventory::bindings::validate(row(&rows, key), &rows, &map),
            "{key}"
        );
    }
    let reviewed: BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    let pending = candidate_keys().unwrap().difference(&reviewed).count();
    assert_eq!(
        pending,
        1299 + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len()
    ); // exact new manual review is outside the bounds-only view // R17 +12 tuple-floor operands; manual reviews separate. // R16 +8 census operands; manual reviews counted separately. // +570 RU-051 keyword operands. // 749 - 2 input guards - 7 symbolic uppers - 4 round-10 field-origin reads - 2 round-11 self-attribute reads (inventory_field_origins) - 2 round-11 OwnerTokenPath constructor forwards - 1 EntryAdmissionPolicy constructor forward - 1 round-11 self-attribute read (inventory_forward_callers, inventory_field_origins) - 6 round-11 slice 5 lease-deadline rows - 1 slice 6 float() forward - 2 slice 6 self-attribute reads - 11 round-12 validated-document-field rows (inventory_validated_document_field) - 4 round-12 RU-033 propagated source-unbounded rows (inventory_unbounded_propagation) + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)
}

#[test]
fn evidence_columns_rows_and_boundaries_are_each_pinned() {
    let (rows, map) = world();
    let (key, guard, id, ..) = CASES[0];
    let r = row(&rows, key).clone();
    let line = input_guards::EDGES
        .lines()
        .find(|l| l.starts_with(key))
        .unwrap()
        .to_string();
    let f: Vec<String> = line.split('\t').map(String::from).collect();
    let sha_text = f[6]
        .split("exact-span-sha256=")
        .nth(1)
        .unwrap()
        .split(';')
        .next()
        .unwrap()
        .to_string();
    let ok = |text: &str| input_guards::validate_with(&r, &rows, &map, text);
    let reference = |v: String| {
        let mut g = f.clone();
        g[6] = v;
        table(|l| !l.starts_with(key), &[g.join("\t")])
    };
    // The evidence needs the span hash, the use's path and the oracle sha, each on its own.
    assert!(
        !ok(&reference(f[6].replace("exact-span-sha256=", "span="))),
        "span hash"
    );
    assert!(
        !ok(&reference(
            f[6].replace("mycelium_a13_macos/app.py:", "elsewhere:")
        )),
        "path"
    );
    assert!(
        !ok(&reference(f[6].replace("@ f4e366e9", "@ 00000000"))),
        "oracle sha"
    );
    assert!(ok(&reference(f[6].clone())), "{sha_text}");
    // Another malformed (empty-column) line anywhere in the table refuses the whole table.
    let junk = "other\t\tsame-scope-raise-guard\t1\tbody\tf\tref".to_string();
    assert!(
        !ok(&table(|_| true, &[junk])),
        "empty column on another line"
    );
    // A census store of the container: refused after the `if` up to and including the use line, allowed on
    // the `if` line itself and for non-assignment kinds.
    let with_store = |line: usize, kind: &str, symbol: &str| {
        let mut m = map.clone();
        let mut extra = m[key].clone();
        extra.key = format!("mycelium_a13_macos/app.py:{line}:8:{kind}:{symbol}");
        extra.line = line;
        extra.kind = kind.into();
        extra.symbol = symbol.into();
        extra.expression = "{}".into();
        m.insert(extra.key.clone(), extra);
        input_guards::validate_with(&r, &rows, &m, input_guards::EDGES)
    };
    assert!(
        !with_store(704, "assignment", "body"),
        "store after the if line"
    );
    assert!(
        !with_store(713, "assignment", "body"),
        "store on the use line"
    );
    assert!(
        !with_store(710, "assignment", "body['max_seconds']"),
        "item store"
    );
    assert!(
        with_store(703, "assignment", "body"),
        "store on the if line itself"
    );
    assert!(with_store(714, "assignment", "body"), "store after the use");
    assert!(with_store(710, "comparison", "body"), "non-assignment kind");
    assert!(with_store(710, "assignment", "bodyx"), "another symbol");
    // Row-side: another disposition, a budget on a derived row, a guard row that is not an upper guard or
    // has another unit, and non-digit range ends.
    let neither = |m: Bound| {
        !input_guards::validate_with(&m, &with(&rows, m.clone()), &map, input_guards::EDGES)
    };
    let mut m = r.clone();
    m.disposition = "fixture-only".into();
    assert!(neither(m), "disposition");
    let mut m = r.clone();
    m.budget = id.to_string();
    assert!(neither(m), "budget on a derived row");
    let other_ms = "PyFinalMyceliumA13MacosAdaptiveHostSampleIntervalSL49C4";
    let mut all = rows.clone();
    let i = all.iter().position(|b| b.key == guard).unwrap();
    all[i].budget = other_ms.into();
    let mut m = r.clone();
    m.relation = other_ms.into();
    assert!(
        !input_guards::validate_with(&m, &all, &map, input_guards::EDGES),
        "not an upper guard id"
    );
    let mut all = rows.clone();
    all[i].unit = "bytes".into();
    assert!(
        !input_guards::validate_with(&r, &all, &map, input_guards::EDGES),
        "guard row unit"
    );
    for (lo, hi) in [("MIN", "1800"), ("30", "MAX"), ("", "1800"), ("30", "")] {
        let mut m = map.clone();
        let g = m.get_mut(guard).unwrap();
        g.expression = format!("{lo} <= body['max_seconds'] <= {hi}");
        g.source = g
            .source
            .replace("30<=", &format!("{lo}<="))
            .replace("<=1800", &format!("<={hi}"));
        assert!(
            !input_guards::validate_with(&r, &rows, &m, input_guards::EDGES),
            "range {lo}..{hi}"
        );
    }
    // Double-quoted source text is the same chain.
    let mut m = map.clone();
    let g = m.get_mut(guard).unwrap();
    g.source = g.source.replace('\'', "\"");
    g.expression = g.expression.replace('\'', "\"");
    assert!(
        input_guards::validate_with(&r, &rows, &m, input_guards::EDGES),
        "quotes"
    );
}
