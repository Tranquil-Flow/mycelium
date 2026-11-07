// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask gate` parser and verdicts against real logs taken from registry/logs/*
//! (FX-L12-gate-*, see fixtures/PROVENANCE.tsv rows). Expected counts come from
//! `probes/gate-counts.awk` (independent of the parser) and agree with `v02.py parse`.
use std::{fs, path::Path};
use xtask::gate::{Kind, Parsed, parse_output, verdict};

fn log(name: &str) -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("fixtures/gate-logs")
        .join(name);
    fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}
fn totals(p: &Parsed) -> (u64, u64, u64) {
    (
        p.targets.iter().map(|t| t.passed).sum(),
        p.targets.iter().map(|t| t.failed).sum(),
        p.targets.iter().map(|t| t.ignored).sum(),
    )
}

/// (file, exit, kind, verdict, passed, failed, ignored, targets)
type Case = (&'static str, i32, Kind, &'static str, u64, u64, u64, usize);
const CASES: &[Case] = &[
    (
        "pass-multi-target.log",
        0,
        Kind::Test,
        "PASS",
        109,
        0,
        0,
        54,
    ),
    (
        "pass-with-ignored.log",
        0,
        Kind::Test,
        "PASS",
        218,
        0,
        1,
        57,
    ),
    (
        "pass-doctests-ignored.log",
        0,
        Kind::Test,
        "PASS",
        136,
        0,
        11,
        31,
    ),
    ("fail-mixed.log", 101, Kind::Test, "FAIL", 48, 1, 0, 23),
    ("fail-all-red.log", 101, Kind::Test, "FAIL", 0, 3, 0, 1),
    (
        "zero-tests-excerpt.log",
        0,
        Kind::Test,
        "ZERO-TESTS",
        0,
        0,
        0,
        1,
    ),
    ("check-clippy.log", 0, Kind::Check, "PASS", 0, 0, 0, 0),
    (
        "check-clippy.log",
        0,
        Kind::Test,
        "NO-TEST-OUTPUT",
        0,
        0,
        0,
        0,
    ),
    ("check-clippy.log", 101, Kind::Check, "FAIL", 0, 0, 0, 0),
    (
        "compile-error.log",
        101,
        Kind::Test,
        "COMPILE-ERROR",
        0,
        0,
        0,
        0,
    ),
    (
        "compile-error.log",
        0,
        Kind::Test,
        "NO-TEST-OUTPUT",
        0,
        0,
        0,
        0,
    ),
    (
        "pass-multi-target.log",
        101,
        Kind::Test,
        "EXIT-NONZERO-NO-TEST-FAILURE",
        109,
        0,
        0,
        54,
    ),
];

#[test]
fn recorded_logs_give_the_independent_counts_and_verdicts() {
    for (name, exit, kind, expect, passed, failed, ignored, targets) in CASES {
        let parsed = parse_output(&log(name));
        assert_eq!(
            (totals(&parsed), parsed.targets.len()),
            ((*passed, *failed, *ignored), *targets),
            "{name}"
        );
        assert_eq!(
            verdict(&parsed, Some(*exit), *kind).as_str(),
            *expect,
            "{name} exit {exit} {kind:?}"
        );
    }
}

#[test]
fn ran_nothing_and_zero_tests_are_never_pass() {
    for name in [
        "zero-tests-excerpt.log",
        "check-clippy.log",
        "compile-error.log",
    ] {
        let parsed = parse_output(&log(name));
        for exit in [Some(0), Some(101), None] {
            assert_ne!(
                verdict(&parsed, exit, Kind::Test).as_str(),
                "PASS",
                "{name} {exit:?}"
            );
        }
    }
    // Empty and whitespace-only output, with a clean exit: still not a pass.
    for text in ["", "\n\n  \n"] {
        assert_ne!(
            verdict(&parse_output(text), Some(0), Kind::Test).as_str(),
            "PASS"
        );
    }
}

#[test]
fn a_process_killed_by_a_signal_is_never_pass_even_with_green_output() {
    let parsed = parse_output(&log("pass-multi-target.log"));
    assert_eq!(verdict(&parsed, None, Kind::Test).as_str(), "KILLED");
    assert_eq!(verdict(&parsed, None, Kind::Check).as_str(), "KILLED");
}

#[test]
fn failed_test_names_and_unlisted_targets_are_extracted() {
    let mixed = parse_output(&log("fail-mixed.log"));
    assert_eq!(
        mixed.failed_tests,
        ["a_proven_death_verifies_the_shutdown_like_a_quit_that_exits_zero"]
    );
    let red = parse_output(&log("fail-all-red.log"));
    assert_eq!(
        red.failed_tests,
        [
            "a_consistent_mutation_of_edge_target_expression_relation_or_unit_is_refused",
            "neighbours_stay_pending_and_the_pending_count_is_pinned",
            "seven_symbolic_upper_comparisons_derive_from_their_module_constant",
        ],
        "sorted, de-duplicated"
    );
    assert!(red.targets.iter().all(|t| !t.ok));
    let compile = parse_output(&log("compile-error.log"));
    assert_eq!(
        compile.compile_errors, 2,
        "`grep -c '^error'` over the recorded log"
    );
}

#[test]
fn targets_are_labelled_with_file_and_binary_and_doc_tests_are_marked() {
    let parsed = parse_output(&log("pass-doctests-ignored.log"));
    let labels: Vec<&str> = parsed.targets.iter().map(|t| t.target.as_str()).collect();
    assert_eq!(labels[0], "unittests src/lib.rs (desktop)");
    assert_eq!(labels[1], "unittests src/bin/package.rs (desktop_package)");
    assert_eq!(labels[2], "tests/assets.rs (assets)");
    assert_eq!(*labels.last().unwrap(), "doc:desktop");
    // Two crates can both have `unittests src/lib.rs`: labels stay distinct.
    let distinct: std::collections::BTreeSet<_> = labels.iter().collect();
    assert_eq!(distinct.len(), labels.len());
}

#[test]
fn zero_test_targets_are_reported_but_do_not_hide_other_passes() {
    let parsed = parse_output(&log("pass-with-ignored.log"));
    assert!(
        parsed
            .zero_test_targets
            .contains(&"unittests src/lib.rs (api)".to_string())
    );
    assert!(
        parsed
            .zero_test_targets
            .iter()
            .all(|t| !t.starts_with("doc:"))
    );
    assert_eq!(
        verdict(&parsed, Some(0), Kind::Test).as_str(),
        "PASS",
        "218 tests ran elsewhere"
    );
}

#[test]
fn a_running_line_without_a_result_is_surfaced_not_silently_dropped() {
    let text = "     Running tests/a.rs (/t/deps/a-0123456789abcdef)\n\nrunning 1 test\ntest x ... ok\n\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n\n     Running tests/b.rs (/t/deps/b-fedcba9876543210)\n\nrunning 1 test\n";
    let parsed = parse_output(text);
    assert_eq!(parsed.targets.len(), 1);
    assert_eq!(parsed.unreported_targets, ["tests/b.rs (b)"]);
}

#[test]
fn cargo_json_messages_count_compile_errors_and_failed_builds() {
    let text = concat!(
        "{\"reason\":\"compiler-message\",\"message\":{\"level\":\"warning\",\"message\":\"unused\"}}\n",
        "{\"reason\":\"compiler-message\",\"message\":{\"level\":\"error\",\"message\":\"cannot find\"}}\n",
        "{\"reason\":\"build-finished\",\"success\":false}\n"
    );
    let parsed = parse_output(text);
    assert_eq!(parsed.compile_errors, 1);
    assert!(parsed.build_failed);
    assert_eq!(
        verdict(&parsed, Some(101), Kind::Test).as_str(),
        "COMPILE-ERROR"
    );
    assert_eq!(verdict(&parsed, Some(101), Kind::Check).as_str(), "FAIL");
    let ok = parse_output("{\"reason\":\"build-finished\",\"success\":true}\n");
    assert!(!ok.build_failed);
    assert_eq!(ok.compile_errors, 0);
}

#[test]
fn a_failed_build_in_json_cannot_pass_even_if_old_targets_printed_results() {
    let mut text = log("pass-with-ignored.log");
    text.push_str("{\"reason\":\"build-finished\",\"success\":false}\n");
    let parsed = parse_output(&text);
    assert!(parsed.build_failed);
    assert_ne!(verdict(&parsed, Some(0), Kind::Test).as_str(), "PASS");
}

#[test]
fn cargo_run_announcements_are_not_test_targets() {
    let text = "   Compiling xtask v0.2.0-dev (/w/xtask)\n    Finished `dev` profile [unoptimized] target(s) in 1.78s\n     Running `/t/debug/xtask lint`\nlint: 3 source files, 0 violations\n";
    let parsed = parse_output(text);
    assert_eq!(parsed.targets.len(), 0);
    assert_eq!(parsed.unreported_targets, Vec::<String>::new());
    assert_eq!(verdict(&parsed, Some(0), Kind::Check).as_str(), "PASS");
}
