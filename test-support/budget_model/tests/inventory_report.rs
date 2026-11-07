// SPDX-License-Identifier: AGPL-3.0-only
//! RU-053: `inventory_gate` is a report. It prints the counts and a closure status line and exits 0
//! whenever the report was produced; open closure violations are reported, never turned into an error.
//! These tests drive the same library function the example prints, on the current checked-in data, so
//! they do not spawn `cargo run` and read no host paths. Counts are cross-checked against independent
//! recomputation rather than pinned, so porting more rows later does not break them.
use budget_model::inventory::{
    CLOSURE_PREFIX, REPORT_SCOPE, REPORT_VIOLATIONS_SHOWN, all_reviewed, build_report,
    candidate_keys, report,
};
use std::collections::BTreeSet;

#[test]
fn report_on_current_data_has_counts_and_one_closure_line_without_failing() {
    let report = report().expect("the checked-in inventory must be readable and well formed");
    let rows = all_reviewed().expect("reviewed rows");
    let keys = candidate_keys().expect("candidate keys");
    let mapped = rows.iter().filter(|row| !row.budget.is_empty()).count();
    assert_eq!(report.candidates, keys.len());
    assert_eq!(report.reviewed, rows.len());
    assert_eq!(report.mapped, mapped);
    assert_eq!(report.dispositions(), rows.len() - mapped);
    assert!(
        report.candidates > 0 && report.reviewed > 0,
        "an empty report would hide a data-loading fault"
    );
    // The report must carry exactly the candidate/review mismatches that closure is about, whichever
    // way it currently falls. Recomputed from the raw sets: `require_complete` itself costs about a
    // minute at opt-level 0, so the report runs it once and this test does not run it again.
    let reviewed: BTreeSet<_> = rows.iter().map(|row| row.key.clone()).collect();
    let count_with = |prefix: &str| {
        report
            .violations
            .iter()
            .filter(|violation| violation.starts_with(prefix))
            .count()
    };
    assert_eq!(
        count_with("unreviewed-candidate: "),
        keys.difference(&reviewed).count()
    );
    assert_eq!(
        count_with("untracked-review: "),
        reviewed.difference(&keys).count()
    );

    let lines = report.render();
    assert_eq!(
        lines[0],
        format!(
            "scope={REPORT_SCOPE} candidates={} reviewed={} mapped={} dispositions={}",
            keys.len(),
            rows.len(),
            mapped,
            rows.len() - mapped
        )
    );
    let closure: Vec<_> = lines
        .iter()
        .filter(|line| line.starts_with(CLOSURE_PREFIX))
        .collect();
    assert_eq!(closure.len(), 1, "exactly one closure status line");
    let shown = lines.len() - 2;
    if report.violations.is_empty() {
        assert!(closure[0].contains("PASS"), "{closure:?}");
        assert_eq!(shown, 0);
    } else {
        assert!(
            closure[0].contains(&format!("{} violations", report.violations.len())),
            "{closure:?}"
        );
        assert_eq!(shown, report.violations.len().min(REPORT_VIOLATIONS_SHOWN));
    }
}

#[test]
fn open_violations_are_reported_with_a_count_and_a_bounded_sample_not_returned_as_an_error() {
    let keys: BTreeSet<String> = (0..8).map(|n| format!("candidate-{n}")).collect();
    let report = build_report(&[], &keys);
    assert_eq!(report.candidates, 8);
    assert_eq!(report.reviewed, 0);
    assert_eq!(report.violations.len(), 8);
    assert!(
        report
            .violations
            .iter()
            .all(|violation| violation.starts_with("unreviewed-candidate: "))
    );
    let lines = report.render();
    assert_eq!(
        lines[0],
        format!("scope={REPORT_SCOPE} candidates=8 reviewed=0 mapped=0 dispositions=0")
    );
    assert!(lines[1].starts_with(CLOSURE_PREFIX) && lines[1].contains("8 violations"));
    assert!(!lines[1].contains("PASS"));
    assert_eq!(lines.len(), 2 + REPORT_VIOLATIONS_SHOWN);
    assert!(
        lines[2..]
            .iter()
            .all(|line| line.contains("unreviewed-candidate: candidate-"))
    );
}

#[test]
fn a_closed_inventory_reports_pass() {
    let report = build_report(&[], &BTreeSet::new());
    assert!(report.violations.is_empty());
    let lines = report.render();
    assert_eq!(lines.len(), 2);
    assert!(lines[1].starts_with(CLOSURE_PREFIX) && lines[1].contains("PASS"));
}
