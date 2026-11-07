// SPDX-License-Identifier: AGPL-3.0-only
// Obligations: O:path:scripts/a13_operator/packaged_app_daemon.py.
//! L1b round 8, rule `r2-argparse-receiver-alias`: the `argparse-option` edge admits the one-letter namespace
//! receiver `a` only for a table row whose pinned reason states the alias (`namespace-alias:`); every other row
//! still needs the receiver `args` or `arguments`. `scripts/a13_operator/packaged_app_daemon.py:30`
//! `child.wait(timeout=a.max_seconds)` forwards the `--max-seconds` default (1800 s, `type=int`) that is also
//! range-asserted at :5 (`30 <= a.max_seconds <= 1800`); the namespace is only ever loaded (AST: `a` has no
//! attribute stores, one parser, one `parse_args()`).
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
    Bound, Candidate, bindings, candidate_keys, candidates, checked_in, validate,
};
use std::collections::BTreeMap;

const KEY: &str = "scripts/a13_operator/packaged_app_daemon.py:30:15:keyword:timeout";
const TARGET: &str = "scripts/a13_operator/packaged_app_daemon.py:5:152:cli-default:--max-seconds";
const ID: &str = "PyCliScriptsA13OperatorPackagedAppDaemonL5C152";
const RULE: &str = "argparse-option";

fn world() -> (Vec<Bound>, BTreeMap<String, Candidate>) {
    let map = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    (checked_in().unwrap(), map)
}

#[test]
fn the_alias_receiver_row_derives_from_the_reviewed_option_default() {
    let (rows, map) = world();
    let r = rows.iter().find(|r| r.key == KEY).expect("row");
    assert_eq!(r.disposition, "derived");
    assert!(r.budget.is_empty() && r.value.is_none());
    assert_eq!(r.unit, "milliseconds");
    assert_eq!(r.relation, ID);
    assert!(r.review.starts_with("binding-category:argparse-option;"));
    // The option default is the reviewed 1800 s row.
    let t = rows.iter().find(|b| b.key == TARGET).unwrap();
    assert_eq!(t.budget, ID);
    assert_eq!(map[KEY].expression, "a.max_seconds");
    assert!(bindings::accepts(RULE, &map[KEY], &map[TARGET]));
    assert!(bindings::validate(r, &rows, &map));
    assert!(validate(&rows).is_ok());
    // The pinned table row names the alias and the declaration line (the one-line parser at :5).
    let line = include_str!("../data/python-argparse-bindings.tsv")
        .lines()
        .find(|l| l.starts_with(KEY))
        .unwrap();
    let f: Vec<_> = line.split('\t').collect();
    assert_eq!(f.len(), 9);
    assert_eq!(
        (f[3], f[4], f[6], f[7]),
        ("a.max_seconds", TARGET, "--max-seconds", "5")
    );
    assert!(f[8].starts_with("namespace-alias:"), "{}", f[8]);
}

#[test]
fn only_a_pinned_alias_row_may_use_a_one_letter_receiver_and_other_receivers_are_refused() {
    let (_, map) = world();
    // Receiver spellings other than the pinned alias are refused (the table row fixes the expression too).
    for expression in [
        "b.max_seconds",
        "x.max_seconds",
        "args.max_seconds",
        "a.other",
        "a.max_seconds2",
        "a",
    ] {
        let mut bad = map[KEY].clone();
        bad.expression = expression.into();
        assert!(!bindings::accepts(RULE, &bad, &map[TARGET]), "{expression}");
    }
    // The row's pinned reason is what admits the alias: a table without the prefix refuses it.
    let table = include_str!("../data/python-argparse-bindings.tsv");
    assert!(bindings::argparse_option_with(
        table,
        &map[KEY],
        &map[TARGET]
    ));
    let stripped = table.replace("\tnamespace-alias:", "\tnote:");
    assert!(!bindings::argparse_option_with(
        &stripped,
        &map[KEY],
        &map[TARGET]
    ));
    // The alias allowance does not extend to any other one-letter or short receiver in the same table.
    for receiver in ["ab", "b", "x", "A", "alias"] {
        let widened = table.replace("\ta.max_seconds\t", &format!("\t{receiver}.max_seconds\t"));
        let mut bad = map[KEY].clone();
        bad.expression = format!("{receiver}.max_seconds");
        assert!(
            !bindings::argparse_option_with(&widened, &bad, &map[TARGET]),
            "{receiver}"
        );
    }
    // A reason that merely mentions the alias (not as its prefix) does not admit it.
    for reason in [
        "note: namespace-alias:",
        "alias",
        " namespace-alias:",
        "Namespace-alias:",
    ] {
        let mentioned = table.replace(
            "\tnamespace-alias:",
            &format!("\t{reason} x namespace-alias-not:"),
        );
        assert!(
            !bindings::argparse_option_with(&mentioned, &map[KEY], &map[TARGET]),
            "{reason}"
        );
    }
    // Existing `args`/`arguments` rows are unaffected by the prefix requirement.
    let other = map
        .values()
        .find(|c| c.path == "mycelium_node/__main__.py" && c.line == 630)
        .unwrap();
    let target = map
        .values()
        .find(|c| {
            c.path == other.path && c.kind == "cli-default" && c.symbol == "--heartbeat-interval"
        })
        .unwrap();
    assert!(bindings::argparse_option_with(table, other, target));
    // Target and key mutations.
    let mut t = map[TARGET].clone();
    t.symbol = "--root".into();
    assert!(!bindings::accepts(RULE, &map[KEY], &t));
    let mut t = map[TARGET].clone();
    t.kind = "assignment".into();
    assert!(!bindings::accepts(RULE, &map[KEY], &t));
}

#[test]
fn the_pending_count_is_pinned() {
    let rows = checked_in().unwrap();
    let reviewed: std::collections::BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    let pending = candidate_keys().unwrap().difference(&reviewed).count();
    assert_eq!(
        pending,
        1299 + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len()
    ); // exact new manual review is outside the bounds-only view // R17 +12 tuple-floor operands; manual reviews separate. // R16 +8 census operands; manual reviews counted separately. // +570 RU-051 keyword operands. // 740 - 1 argparse receiver alias - 2 RU-023 observations - 11 RU-024 ratio literals (round 9) - 4 round-10 field-origin reads - 2 round-11 self-attribute reads (inventory_field_origins) - 2 round-11 OwnerTokenPath constructor forwards - 1 EntryAdmissionPolicy constructor forward - 1 round-11 self-attribute read (inventory_forward_callers, inventory_field_origins) - 6 round-11 slice 5 lease-deadline rows - 1 slice 6 float() forward - 2 slice 6 self-attribute reads - 11 round-12 validated-document-field rows (inventory_validated_document_field) - 4 round-12 RU-033 propagated source-unbounded rows (inventory_unbounded_propagation) + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)
}
