// SPDX-License-Identifier: AGPL-3.0-only
//! L1b round 12 audit of the round-6 `source-unbounded` family (RU-014, owner option a). The round-6 scan asked
//! "does a census comparison name the quoted field"; round 12's alias-aware scan (see
//! `inventory_validated_document_field`) finds that EIGHT of the 24 closed rows read a value that v0.1.4 does
//! bound, by a check the scan could not see: a check on the local the value is read into, a loop variable, a
//! positional-argument range helper, or a check downstream of the read. The rows are NOT changed here (the
//! family was accepted by the owner; reverting is a parent decision, "revert = the row goes back to pending");
//! this table pins each finding with the oracle line that bounds the value so the evidence cannot rot.
//! RU-044 (round 13) then REVERTED the 8 rows: they left the family and `bounds.tsv`, their Blocked ids are
//! gone, and they are adjudicated as bounded rows (`inventory_ru044_revert`). The table stays as the evidence:
//!
//! * `validated-use-carries-bound`: the very value is range-checked a line later and the validated-use census
//!   row already carries the registered bound (the raw-read row's "no maximum" text is inaccurate);
//! * `bounded-downstream`: the value flows into a document that a later validator bounds by a registered id;
//! * `census-gap`: the bound exists in v0.1.4 but NO census row exists at that line, so the bound has no
//!   registry id yet (the discovery tables must add it before the Rust port can enforce it).
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
use budget_model::inventory::{candidates, checked_in};
use budgets::{BudgetId, table::Value};
use std::collections::BTreeSet;

const ORACLE: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";
const AUDIT: &str = include_str!("../data/python-source-unbounded-audit.tsv");
const FAMILY: &str = include_str!("../data/python-source-unbounded.tsv");

/// (bound id, independent literal of its registry value, unit)
const BOUND_VALUES: &[(&str, u64, &str)] = &[
    (
        "PyMyceliumA10BatchPolicyControlMaximumWindowMsL44",
        250,
        "ms",
    ),
    (
        "PyShiftMyceliumA13MacosAdaptiveHostMaxBytesL88C0",
        1_125_899_906_842_624,
        "bytes",
    ),
    (
        "PyMyceliumSwarmDirectoryDocsMaxGrantRequestsL43",
        10_000,
        "entries",
    ),
    (
        "PyMyceliumA7NodeWiringMaxCheckpointBytesL64",
        268_435_456,
        "bytes",
    ),
];

struct Finding<'a> {
    key: &'a str,
    kind: &'a str,
    path: &'a str,
    line: usize,
    text: &'a str,
    bound_row: &'a str,
    bound_id: &'a str,
}

fn findings() -> Vec<Finding<'static>> {
    AUDIT
        .lines()
        .skip(1)
        .map(|l| {
            let f: Vec<_> = l.split('\t').collect();
            assert_eq!(f.len(), 7, "{l}");
            Finding {
                key: f[0],
                kind: f[1],
                path: f[2],
                line: f[3].parse().unwrap(),
                text: f[4],
                bound_row: f[5],
                bound_id: f[6],
            }
        })
        .collect()
}

fn oracle_line(path: &str, line: usize) -> String {
    let show = std::process::Command::new("git")
        .arg("-C")
        .arg(oracle_blobs::oracle_dir_or_panic())
        .args(["show", &format!("{ORACLE}:{path}")])
        .output()
        .unwrap();
    assert!(show.status.success(), "{path}");
    String::from_utf8(show.stdout)
        .unwrap()
        .lines()
        .nth(line - 1)
        .unwrap()
        .trim()
        .to_string()
}

#[test]
fn eight_round6_rows_were_bounded_in_v014_and_are_reverted_by_ru044() {
    let all = checked_in().unwrap();
    let family: BTreeSet<&str> = FAMILY
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    let found = findings();
    assert_eq!(found.len(), 8);
    assert_eq!(
        found.iter().map(|f| f.key).collect::<BTreeSet<_>>().len(),
        8,
        "one finding per row"
    );
    let kinds = |k: &str| found.iter().filter(|f| f.kind == k).count();
    assert_eq!(
        (
            kinds("validated-use-carries-bound"),
            kinds("bounded-downstream"),
            kinds("census-gap")
        ),
        (2, 2, 4)
    );
    for f in &found {
        // RU-044: no longer a family row and no longer reviewed `source-unbounded`.
        assert!(!family.contains(f.key), "{}: still a family row", f.key);
        assert!(
            all.iter().all(|r| r.key != f.key),
            "{}: still in bounds.tsv",
            f.key
        );
        // The oracle line that bounds (or validates) the value.
        let got = oracle_line(f.path, f.line);
        assert_eq!(got, f.text, "{}:{}", f.path, f.line);
    }
}

#[test]
fn bound_rows_carry_registered_ids_and_census_gaps_now_have_a_manual_bound_row() {
    let all = checked_in().unwrap();
    let census = candidates().unwrap();
    for f in findings() {
        match f.kind {
            "validated-use-carries-bound" | "bounded-downstream" => {
                let bound = all
                    .iter()
                    .find(|r| r.key == f.bound_row)
                    .unwrap_or_else(|| panic!("{}: bound row missing", f.bound_row));
                let id = if bound.budget.is_empty() {
                    &bound.relation
                } else {
                    &bound.budget
                };
                assert_eq!(id, f.bound_id, "{}", f.bound_row);
                let (_, literal, unit) = BOUND_VALUES
                    .iter()
                    .find(|b| b.0 == f.bound_id)
                    .unwrap_or_else(|| panic!("{}: no independent literal", f.bound_id));
                let (got, got_unit) = match BudgetId::from_name(f.bound_id).unwrap().entry().value {
                    Value::Duration(span) => (span.millis(), "ms"),
                    Value::Storage(bytes) => (bytes.get(), "bytes"),
                    Value::Count(n) => (n.get(), "entries"),
                    other => panic!("{}: {other:?}", f.bound_id),
                };
                assert_eq!((got, got_unit), (*literal, *unit), "{}", f.bound_id);
            }
            "census-gap" => {
                assert_eq!((f.bound_row, f.bound_id), ("-", "-"), "{}", f.key);
                // RU-044 added the missing discovery row (kind `manual-bound`) with its own registry id.
                let added: Vec<_> = census
                    .iter()
                    .filter(|c| c.path == f.path && c.line == f.line)
                    .collect();
                assert_eq!(added.len(), 1, "{}:{}", f.path, f.line);
                assert_eq!(added[0].kind, "manual-bound", "{}:{}", f.path, f.line);
            }
            other => panic!("unknown finding kind {other}"),
        }
    }
}
