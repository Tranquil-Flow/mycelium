// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask behaviours check`: validation of BEHAVIOURS.tsv / FAULTS.tsv on fixture TSVs. Each test
//! starts from a clean world (no findings) and breaks one thing.
mod common;
use common::*;
use xtask::behaviours::{self, parse_key_cases};

#[test]
fn the_clean_world_has_no_findings() {
    let world = World::new("b-clean");
    assert_eq!(codes(&world.validate()), Vec::<&str>::new());
}

#[test]
fn duplicate_ids_bad_ids_unknown_areas_and_area_prefix_mismatches_are_findings() {
    let mut world = World::new("b-ids");
    let row = world.behaviours.rows[0].clone();
    world.behaviours.rows.push(row);
    assert!(has(&world.validate(), "id_duplicate", "B-INST-01"));

    let mut world = World::new("b-ids2");
    world.behaviours.set("B-INST-01", "id", "B-inst-1");
    world.faults.set("F-INST-01", "must_break", "B-inst-1");
    assert!(has(&world.validate(), "id_invalid", "B-inst-1"));

    let mut world = World::new("b-ids3");
    world.behaviours.set("B-INST-01", "area", "ONB");
    assert!(has(&world.validate(), "area_prefix_mismatch", "B-INST-01"));

    let mut world = World::new("b-ids4");
    world.behaviours.set("B-INST-01", "area", "BOGUS");
    assert!(has(&world.validate(), "area_unknown", "B-INST-01"));

    let mut world = World::new("b-ids5");
    world.faults.set("F-INST-01", "area", "CHAT");
    assert!(has(&world.validate(), "area_prefix_mismatch", "F-INST-01"));

    let mut world = World::new("b-ids5b");
    world.faults.set("F-INST-01", "id", "FINST1");
    world
        .behaviours
        .set("B-INST-01", "must_catch", "FINST1,F-INST-02");
    assert!(has(&world.validate(), "id_invalid", "FINST1"));

    let mut world = World::new("b-ids6");
    let row = world.faults.rows[0].clone();
    world.faults.rows.push(row);
    assert!(has(&world.validate(), "id_duplicate", "F-INST-01"));

    // A tb4 addition id is accepted.
    let mut world = World::new("b-ids7");
    world.behaviours.set("B-REL-01", "id", "NEW-P1-01");
    world.faults.set("F-REL-01", "must_break", "NEW-P1-01");
    world.faults.set("F-REL-02", "must_break", "NEW-P1-01");
    assert_eq!(codes(&world.validate()), Vec::<&str>::new());
}

#[test]
fn every_required_cell_must_be_non_empty() {
    for column in [
        "statement",
        "why",
        "v014_source",
        "oracle",
        "kind",
        "key_cases",
        "must_catch",
        "status",
    ] {
        let mut world = World::new(&format!("b-empty-{column}"));
        world.behaviours.set("B-CONSENT-01", column, "");
        let findings = world.validate();
        assert!(
            has(&findings, "column_empty", "B-CONSENT-01"),
            "{column}: {findings:?}"
        );
    }
    for column in ["fault", "must_break", "origin"] {
        let mut world = World::new(&format!("f-empty-{column}"));
        world.faults.set("F-CONSENT-01", column, "");
        assert!(
            has(&world.validate(), "column_empty", "F-CONSENT-01"),
            "{column}"
        );
    }
}

#[test]
fn v014_source_kind_oracle_and_status_formats_are_checked() {
    for (column, value, code) in [
        (
            "v014_source",
            "installer_core.py:179-193",
            "v014_source_invalid",
        ),
        (
            "v014_source",
            "no citation here @ f4e366e9",
            "v014_source_invalid",
        ),
        (
            "v014_source",
            "installer_core.py:179-193 @ deadbeef",
            "v014_source_invalid",
        ),
        ("kind", "X", "kind_invalid"),
        ("kind", "V++J", "kind_invalid"),
        ("kind", "V+", "kind_invalid"),
        ("oracle", "the production function", "oracle_invalid"),
        ("status", "done", "status_invalid"),
    ] {
        let mut world = World::new("b-format");
        world.behaviours.set("B-INST-01", column, value);
        assert!(
            has(&world.validate(), code, "B-INST-01"),
            "{column}={value}"
        );
    }
    // Accepted shapes seen in the real catalogue.
    for value in [
        "a.py:1-5 ; b.py:3,9-12 @ f4e366e9",
        "n/a-v02 (v0.2-only constraint): V02-PLAN.md:17; v0.1.4 comparator: a.py:76-92 @ f4e366e9",
    ] {
        let mut world = World::new("b-format-ok");
        world.behaviours.set("B-INST-01", "v014_source", value);
        assert_eq!(codes(&world.validate()), Vec::<&str>::new(), "{value}");
    }
    for value in ["J+D", "M+S+D", "N"] {
        let mut world = World::new("b-kind-ok");
        world.behaviours.set("B-INST-01", "kind", value);
        assert_eq!(codes(&world.validate()), Vec::<&str>::new(), "{value}");
    }
}

#[test]
fn key_cases_parse_on_semicolons_outside_parentheses() {
    assert_eq!(
        parse_key_cases("a (x; y); b ; c").unwrap(),
        vec!["a (x; y)", "b", "c"]
    );
    assert_eq!(parse_key_cases("a; b;").unwrap(), vec!["a", "b"]);
    // Half-open intervals as in the real catalogue (B-PLAN-10).
    assert_eq!(
        parse_key_cases("layers map node -> [start,end); next").unwrap(),
        vec!["layers map node -> [start,end)", "next"]
    );
    assert_eq!(
        parse_key_cases("range [0,1); x (y; z)").unwrap(),
        vec!["range [0,1)", "x (y; z)"]
    );
    assert!(parse_key_cases("a [b").is_err());
    assert!(parse_key_cases("").is_err());
    assert!(parse_key_cases("a (b").is_err());
    assert!(parse_key_cases("a) b").is_err());
    assert!(parse_key_cases("a;; b").is_err());
    let mut world = World::new("b-keycases");
    world
        .behaviours
        .set("B-INST-01", "key_cases", "unbalanced (case");
    assert!(has(&world.validate(), "key_cases_invalid", "B-INST-01"));
}

#[test]
fn references_between_behaviours_and_faults_hold_both_ways() {
    // A behaviour names an unknown fault.
    let mut world = World::new("b-ref1");
    world
        .behaviours
        .set("B-INST-01", "must_catch", "F-INST-01,F-INST-02,F-INST-99");
    assert!(has(
        &world.validate(),
        "must_catch_unknown_fault",
        "B-INST-01"
    ));
    // A behaviour names a fault that does not list it.
    let mut world = World::new("b-ref2");
    world.faults.set("F-INST-01", "must_break", "B-CONSENT-01");
    let findings = world.validate();
    assert!(
        has(&findings, "fault_back_reference_missing", "B-INST-01"),
        "{findings:?}"
    );
    assert!(
        has(&findings, "behaviour_back_reference_missing", "F-INST-01"),
        "{findings:?}"
    );
    // A fault names an unknown behaviour.
    let mut world = World::new("b-ref3");
    world
        .faults
        .set("F-INST-01", "must_break", "B-INST-01,B-INST-77");
    assert!(has(
        &world.validate(),
        "must_break_unknown_behaviour",
        "F-INST-01"
    ));
    // A behaviour with no fault at all cannot be shown strong.
    let mut world = World::new("b-ref4");
    world.behaviours.set("B-REL-01", "must_catch", "");
    world.faults.set("F-REL-01", "must_break", "B-CONSENT-01");
    world.faults.set("F-REL-02", "must_break", "B-CONSENT-01");
    world.behaviours.set(
        "B-CONSENT-01",
        "must_catch",
        "F-CONSENT-01,F-CONSENT-02,F-REL-01,F-REL-02",
    );
    assert!(has(&world.validate(), "column_empty", "B-REL-01"));
}

#[test]
fn every_area_with_behaviours_needs_a_positive_control_fault() {
    let mut world = World::new("b-pc");
    world.faults.set("F-CONSENT-02", "origin", "plausible");
    assert!(has(
        &world.validate(),
        "positive_control_missing",
        "CONSENT"
    ));
}

#[test]
fn rust_tests_must_be_rs_ids_that_exist() {
    let mut world = World::new("b-rs1");
    world.behaviours.set(
        "B-CONSENT-01",
        "rust_tests",
        "crates/x/tests/alpha.rs::test_c",
    );
    assert!(has(
        &world.validate(),
        "rust_test_malformed",
        "B-CONSENT-01"
    ));
    let mut world = World::new("b-rs2");
    world.behaviours.set(
        "B-CONSENT-01",
        "rust_tests",
        "rs:crates/x/tests/alpha.rs::missing_fn",
    );
    assert!(has(&world.validate(), "rust_test_unknown", "B-CONSENT-01"));
    // A renamed test: the file exists but not the fn.
    let mut world = World::new("b-rs3");
    world.rust = "#[test]\nfn test_a() {}\n".into();
    let findings = world.validate();
    assert!(
        has(&findings, "rust_test_unknown", "B-INST-01"),
        "{findings:?}"
    );
}

#[test]
fn evidence_must_exist_and_be_a_pass_gate_record() {
    let mut world = World::new("b-ev1");
    world.behaviours.set("B-CONSENT-01", "evidence", "garbage");
    assert!(has(&world.validate(), "evidence_malformed", "B-CONSENT-01"));
    let mut world = World::new("b-ev2");
    world
        .behaviours
        .set("B-CONSENT-01", "evidence", "EV-20261029-9999");
    assert!(has(&world.validate(), "evidence_unknown", "B-CONSENT-01"));
    let mut world = World::new("b-ev3");
    world.behaviours.set("B-CONSENT-01", "evidence", EV_FAIL);
    assert!(has(
        &world.validate(),
        "evidence_not_pass_gate",
        "B-CONSENT-01"
    ));
    let mut world = World::new("b-ev4");
    world.behaviours.set("B-CONSENT-01", "evidence", EV_NOTGATE);
    assert!(has(
        &world.validate(),
        "evidence_not_pass_gate",
        "B-CONSENT-01"
    ));
}

#[test]
fn partial_and_proven_rows_need_tests_and_evidence_while_open_rows_may_have_none() {
    let mut world = World::new("b-open");
    world.behaviours.set("B-CONSENT-01", "status", "open");
    world.behaviours.set("B-CONSENT-01", "rust_tests", "");
    world.behaviours.set("B-CONSENT-01", "evidence", "");
    assert_eq!(codes(&world.validate()), Vec::<&str>::new());
    for status in ["partial", "proven"] {
        let mut world = World::new("b-partial");
        world.behaviours.set("B-CONSENT-01", "status", status);
        world.behaviours.set("B-CONSENT-01", "rust_tests", "");
        assert!(
            has(&world.validate(), "column_empty", "B-CONSENT-01"),
            "{status}"
        );
        let mut world = World::new("b-partial2");
        world.behaviours.set("B-CONSENT-01", "status", status);
        world.behaviours.set("B-CONSENT-01", "evidence", "");
        assert!(
            has(&world.validate(), "column_empty", "B-CONSENT-01"),
            "{status}"
        );
    }
}

#[test]
fn a_proven_status_the_evidence_does_not_back_is_an_over_claim() {
    let world = World::new("b-over");
    let (b, f, _) = world.catalogue();
    let ledger = world.ledger_parsed();
    let rust = world.rust_tests();
    let context = behaviours::Context {
        rust_tests: Some(&rust),
        ledger: Some(&ledger),
    };
    let proven: std::collections::BTreeSet<String> =
        ["B-INST-01", "B-CONSENT-01"].map(String::from).into();
    let findings = behaviours::validate(&b, &f, &context, Some(&proven));
    assert_eq!(codes(&findings), vec!["status_overclaim"], "{findings:?}");
    assert!(has(&findings, "status_overclaim", "B-REL-01"));
    let all: std::collections::BTreeSet<String> = ["B-INST-01", "B-CONSENT-01", "B-REL-01"]
        .map(String::from)
        .into();
    assert!(behaviours::validate(&b, &f, &context, Some(&all)).is_empty());
}

#[test]
fn a_ragged_row_is_reported_and_a_wrong_header_is_refused() {
    let world = World::new("b-ragged");
    let mut text = world.behaviours.render();
    text.push_str("B-X-01\tINST\ttoo few cells\n");
    let (rows, findings) = behaviours::load_behaviours(&text, "B.tsv").unwrap();
    assert_eq!(rows.len(), 3);
    assert_eq!(codes(&findings), vec!["row_columns_invalid"]);
    let swapped = world.behaviours.render().replacen("statement", "claim", 1);
    assert!(behaviours::load_behaviours(&swapped, "B.tsv").is_err());
    let extra = world.faults.render().replacen("origin", "origin\tnote", 1);
    assert!(behaviours::load_faults(&extra, "F.tsv").is_err());
    assert!(behaviours::load_behaviours("", "B.tsv").is_err());
}

#[test]
fn id_lists_split_on_semicolons_commas_and_spaces() {
    let mut world = World::new("b-lists");
    world
        .behaviours
        .set("B-INST-01", "must_catch", "F-INST-01, F-INST-02;F-X");
    let (rows, _) = behaviours::load_behaviours(&world.behaviours.render(), "B.tsv").unwrap();
    assert_eq!(rows[0].must_catch, vec!["F-INST-01", "F-INST-02", "F-X"]);
}

#[test]
fn the_author_reference_golden_oracle_is_valid_for_num_rows_only() {
    // RU-050: existing author-derived Qwen fixtures are a valid oracle for numerical behaviours.
    for value in [
        "author-reference golden",
        "author-reference golden (Qwen2.5 scalar/block vectors)",
    ] {
        let mut world = World::new("b-author-ref-num");
        world.behaviours.set("B-INST-01", "id", "B-NUM-01");
        world.behaviours.set("B-NUM-01", "area", "NUM");
        world.behaviours.set("B-NUM-01", "oracle", value);
        let findings = world.validate();
        assert!(
            !has(&findings, "oracle_invalid", "B-NUM-01"),
            "{value}: {findings:?}"
        );
        assert!(
            !has(&findings, "area_prefix_mismatch", "B-NUM-01"),
            "{value}: {findings:?}"
        );
    }
    // Any other area still needs one of the three original bases.
    let mut world = World::new("b-author-ref-inst");
    world
        .behaviours
        .set("B-INST-01", "oracle", "author-reference golden");
    assert!(has(&world.validate(), "oracle_invalid", "B-INST-01"));
    // Near misses and the three original bases are unchanged.
    for value in [
        "author-reference",
        "author reference golden",
        "golden",
        "executed author-reference goldens (oracle)",
        "independent author-reference golden",
    ] {
        let mut world = World::new("b-author-ref-near");
        world.behaviours.set("B-INST-01", "id", "B-NUM-01");
        world.behaviours.set("B-NUM-01", "area", "NUM");
        world.behaviours.set("B-NUM-01", "oracle", value);
        assert!(
            has(&world.validate(), "oracle_invalid", "B-NUM-01"),
            "{value}"
        );
    }
    for value in ["executed-v0.1.4", "independent-model", "spec"] {
        let mut world = World::new("b-author-ref-orig");
        world.behaviours.set("B-INST-01", "oracle", value);
        assert!(!has(&world.validate(), "oracle_invalid", "B-INST-01"));
    }
}
