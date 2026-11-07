// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask retire check` schema 2: groups resolved against behaviours, faults and the evidence ledger,
//! on fixture TSVs. The base world resolves completely; every test breaks one thing and expects the
//! precise group, behaviour or fault to fall. The negative tests double as the kill set for the
//! always-resolve and ignore-faults mutants of the resolver (see `L12-evidence/r13-mutation.py`).
mod common;
use common::*;
use xtask::groups::{FaultState, GroupResult, Resolution};

fn group<'a>(resolution: &'a Resolution, id: &str) -> &'a GroupResult {
    resolution
        .groups
        .iter()
        .find(|g| g.id == id)
        .unwrap_or_else(|| panic!("no group {id}"))
}
fn unresolved(resolution: &Resolution) -> Vec<String> {
    resolution
        .groups
        .iter()
        .filter(|g| g.via.is_none())
        .map(|g| g.id.clone())
        .collect()
}
fn reasons(resolution: &Resolution, id: &str) -> Vec<&'static str> {
    group(resolution, id).reasons.clone()
}

// ------------------------------------------------------------------------------ the base world

#[test]
fn the_base_world_resolves_every_group_every_behaviour_and_every_fault() {
    let world = World::new("g-base");
    let (resolution, findings) = world.resolve();
    assert_eq!(codes(&findings), Vec::<&str>::new(), "{findings:?}");
    assert_eq!(resolution.groups.len(), 7);
    assert_eq!(unresolved(&resolution), Vec::<String>::new());
    assert!(resolution.behaviours.values().all(Vec::is_empty));
    assert_eq!(resolution.behaviours.len(), 3);
    assert_eq!(
        resolution.faults["F-REL-01"],
        FaultState::Adjudicated("equivalent".into())
    );
    assert!(matches!(
        resolution.faults["F-INST-01"],
        FaultState::Caught(_)
    ));
    let via = |id: &str| group(&resolution, id).via.clone().unwrap();
    assert_eq!(via("T:test_alpha.py"), "evidence_for");
    assert_eq!(via("T:test_beta.py"), "not_shipped");
    assert_eq!(via("T:test_gamma.py"), "not_executed");
    assert_eq!(via("T:test_delta.py"), "superseded_tooling");
    assert_eq!(via("T:test_eps.py"), "structure_only");
    assert_eq!(via("T:test_zeta.py"), "python_only");
    assert_eq!(via("T:conftest.py"), "python_only");
    // The areas a group touches come from the behaviours it names.
    assert_eq!(
        group(&resolution, "T:test_alpha.py")
            .areas
            .iter()
            .cloned()
            .collect::<Vec<_>>(),
        vec!["CONSENT", "INST"]
    );
}

// ------------------------------------------------------------------------------ behaviours

fn assert_alpha_falls(world: &World, behaviour: &str, reason: &str) {
    let (resolution, _) = world.resolve();
    assert!(
        resolution.behaviours[behaviour].contains(&reason),
        "{behaviour}: {:?}",
        resolution.behaviours[behaviour]
    );
    assert_eq!(unresolved(&resolution), vec!["T:test_alpha.py"], "{reason}");
    assert!(reasons(&resolution, "T:test_alpha.py").contains(&"behaviour_unproven"));
}

#[test]
fn a_behaviour_without_rust_tests_or_evidence_is_not_proven() {
    let mut world = World::new("g-b-notests");
    world.behaviours.set("B-CONSENT-01", "rust_tests", "");
    assert_alpha_falls(&world, "B-CONSENT-01", "no_rust_tests");
    let mut world = World::new("g-b-noev");
    world.behaviours.set("B-CONSENT-01", "evidence", "");
    assert_alpha_falls(&world, "B-CONSENT-01", "no_evidence");
}

#[test]
fn a_rust_test_that_does_not_exist_or_no_cited_log_shows_ok_is_not_proof() {
    let mut world = World::new("g-b-unknown");
    world.behaviours.set(
        "B-CONSENT-01",
        "rust_tests",
        "rs:crates/x/tests/alpha.rs::gone",
    );
    assert_alpha_falls(&world, "B-CONSENT-01", "rust_test_unknown");

    // The log of the cited run lacks the test line.
    let mut world = World::new("g-b-notok");
    world.logs[0].1 = RUN_LOG.replace("test test_c ... ok\n", "");
    assert_alpha_falls(&world, "B-CONSENT-01", "rust_test_not_proved");

    // A FAILED line is not an ok line.
    let mut world = World::new("g-b-failed");
    world.logs[0].1 = RUN_LOG.replace("test test_c ... ok", "test test_c ... FAILED");
    assert_alpha_falls(&world, "B-CONSENT-01", "rust_test_not_proved");

    // The test passed in a run that is not cited by this behaviour.
    let mut world = World::new("g-b-othercite");
    world.logs.push((
        "other.log".into(),
        RUN_LOG.replace("test test_c ... ok\n", ""),
    ));
    world.behaviours.set("B-CONSENT-01", "evidence", EV_ADJ);
    assert_alpha_falls(&world, "B-CONSENT-01", "rust_test_not_proved");

    // The cited record is a FAIL gate or not a gate: no proof, even though its log shows the test ok.
    for ev in [EV_FAIL, EV_NOTGATE] {
        let mut world = World::new("g-b-badev");
        world.behaviours.set("B-CONSENT-01", "evidence", ev);
        assert_alpha_falls(&world, "B-CONSENT-01", "rust_test_not_proved");
    }
}

#[test]
fn a_fault_the_behaviour_names_but_the_catalogue_lacks_leaves_it_unproven() {
    let mut world = World::new("g-b-ghost-fault");
    world.behaviours.set(
        "B-CONSENT-01",
        "must_catch",
        "F-CONSENT-01,F-CONSENT-02,F-CONSENT-99",
    );
    let (resolution, _) = world.resolve();
    assert!(resolution.behaviours["B-CONSENT-01"].contains(&"fault_unknown"));
    assert_eq!(unresolved(&resolution), vec!["T:test_alpha.py"]);
}

#[test]
fn a_fault_that_must_break_a_behaviour_counts_even_if_the_behaviour_does_not_list_it() {
    // The fault lists B-CONSENT-01, B-CONSENT-01 does not list the fault, and nothing caught it.
    let mut world = World::new("g-b-onesided");
    world.faults.push(&[
        "F-CONSENT-03",
        "CONSENT",
        "a bug only the fault list knows about",
        "B-CONSENT-01",
        "plausible",
    ]);
    let (resolution, findings) = world.resolve();
    assert!(has(
        &findings,
        "behaviour_back_reference_missing",
        "F-CONSENT-03"
    ));
    assert!(resolution.behaviours["B-CONSENT-01"].contains(&"fault_not_caught"));
    assert_eq!(unresolved(&resolution), vec!["T:test_alpha.py"]);
}

#[test]
fn every_listed_test_needs_proof_not_just_one() {
    let mut world = World::new("g-b-every");
    world.logs[0].1 = RUN_LOG.replace("test test_b ... ok\n", "");
    let (resolution, _) = world.resolve();
    assert!(resolution.behaviours["B-INST-01"].contains(&"rust_test_not_proved"));
    assert!(resolution.behaviours["B-CONSENT-01"].is_empty());
}

#[test]
fn a_behaviour_with_a_catalogue_finding_is_not_proven() {
    let mut world = World::new("g-b-row");
    world
        .behaviours
        .set("B-CONSENT-01", "key_cases", "unbalanced (");
    assert_alpha_falls(&world, "B-CONSENT-01", "row_invalid");
}

// ------------------------------------------------------------------------------ faults

#[test]
fn an_uncaught_fault_leaves_its_behaviour_unproven() {
    let mut world = World::new("g-f-none");
    world.catches.drop_row("F-CONSENT-01");
    let (resolution, _) = world.resolve();
    assert_eq!(
        resolution.faults["F-CONSENT-01"],
        FaultState::Open("no_catch_row")
    );
    assert!(resolution.behaviours["B-CONSENT-01"].contains(&"fault_not_caught"));
    assert_eq!(unresolved(&resolution), vec!["T:test_alpha.py"]);
}

#[test]
fn a_catch_needs_a_pass_gate_log_line_naming_the_fault_and_the_test() {
    // The log does not print FAULT-CAUGHT for this fault.
    let mut world = World::new("g-f-noline");
    world.logs[1].1 = MUTATION_LOG.replace("FAULT-CAUGHT F-CONSENT-01 by test_c\n", "");
    let (resolution, _) = world.resolve();
    assert_eq!(
        resolution.faults["F-CONSENT-01"],
        FaultState::Open("catch_not_proved")
    );
    assert!(resolution.behaviours["B-CONSENT-01"].contains(&"fault_not_caught"));

    // The line names another test than the row.
    let mut world = World::new("g-f-othertest");
    world.catches.set("F-CONSENT-01", "caught_by", T1);
    let (resolution, _) = world.resolve();
    assert_eq!(
        resolution.faults["F-CONSENT-01"],
        FaultState::Open("catch_not_proved")
    );

    // A FAIL record cannot prove a catch.
    let mut world = World::new("g-f-failrec");
    world.catches.set("F-CONSENT-01", "evidence", EV_FAIL);
    world.logs[0].1 = format!("{RUN_LOG}FAULT-CAUGHT F-CONSENT-01 by test_c\n");
    let (resolution, _) = world.resolve();
    assert_eq!(
        resolution.faults["F-CONSENT-01"],
        FaultState::Open("catch_not_proved")
    );
}

#[test]
fn the_catching_test_must_belong_to_a_behaviour_the_fault_must_break() {
    // test_a catches F-CONSENT-01, but test_a belongs to B-INST-01, not B-CONSENT-01.
    let mut world = World::new("g-f-unrelated");
    world.catches.set("F-CONSENT-01", "caught_by", T1);
    world.logs[1].1 = MUTATION_LOG.replace("F-CONSENT-01 by test_c", "F-CONSENT-01 by test_a");
    let (resolution, _) = world.resolve();
    assert_eq!(
        resolution.faults["F-CONSENT-01"],
        FaultState::Open("catch_by_unrelated_test")
    );
    assert!(resolution.behaviours["B-CONSENT-01"].contains(&"fault_not_caught"));
}

#[test]
fn a_catching_test_that_does_not_exist_is_not_a_catch() {
    let mut world = World::new("g-f-ghost");
    world.catches.set(
        "F-CONSENT-01",
        "caught_by",
        "rs:crates/x/tests/alpha.rs::ghost",
    );
    let (resolution, _) = world.resolve();
    assert_eq!(
        resolution.faults["F-CONSENT-01"],
        FaultState::Open("catch_test_unknown")
    );
}

#[test]
fn an_adjudication_needs_a_known_reason_an_existing_record_and_an_approver() {
    for (column, value, state) in [
        ("reason", "", "adjudication_invalid"),
        ("reason", "because I said so", "adjudication_invalid"),
        ("reason", "equivalent:", "adjudication_invalid"),
        ("approver", "", "adjudication_invalid"),
        ("approver", "nobody", "adjudication_invalid"),
        ("evidence", "", "adjudication_invalid"),
        ("evidence", "EV-20261029-9999", "adjudication_invalid"),
    ] {
        let mut world = World::new("g-f-adj");
        world.catches.set("F-REL-01", column, value);
        let (resolution, _) = world.resolve();
        assert_eq!(
            resolution.faults["F-REL-01"],
            FaultState::Open(state),
            "{column}={value:?}"
        );
        assert!(resolution.behaviours["B-REL-01"].contains(&"fault_not_caught"));
    }
    for category in ["equivalent", "unviable", "timeout", "not-run"] {
        let mut world = World::new("g-f-adj-ok");
        world
            .catches
            .set("F-REL-01", "reason", &format!("{category}: documented"));
        let (resolution, _) = world.resolve();
        assert_eq!(
            resolution.faults["F-REL-01"],
            FaultState::Adjudicated(category.into())
        );
    }
}

#[test]
fn a_positive_control_can_only_be_caught_never_adjudicated() {
    let mut world = World::new("g-f-pc");
    world.catches.set("F-REL-02", "status", "adjudicated");
    world.catches.set("F-REL-02", "caught_by", "");
    world.catches.set("F-REL-02", "evidence", EV_ADJ);
    world
        .catches
        .set("F-REL-02", "reason", "equivalent: skip it");
    world
        .catches
        .set("F-REL-02", "approver", "integrator-provisional");
    let (resolution, _) = world.resolve();
    assert_eq!(
        resolution.faults["F-REL-02"],
        FaultState::Open("positive_control_adjudicated")
    );
    assert!(resolution.behaviours["B-REL-01"].contains(&"fault_not_caught"));
}

#[test]
fn catch_rows_for_unknown_faults_unknown_status_or_twice_are_findings() {
    let mut world = World::new("g-f-rows");
    world
        .catches
        .push(&["F-GHOST-01", "caught", EV_MUT, T1, "", ""]);
    let (_, findings) = world.resolve();
    assert!(has(&findings, "catch_fault_unknown", "F-GHOST-01"));
    let mut world = World::new("g-f-rows2");
    world.catches.set("F-INST-01", "status", "maybe");
    let (resolution, findings) = world.resolve();
    assert!(has(&findings, "catch_status_invalid", "F-INST-01"));
    assert!(matches!(
        resolution.faults["F-INST-01"],
        FaultState::Open(_)
    ));
    let mut world = World::new("g-f-rows3");
    let row = world.catches.rows[0].clone();
    world.catches.rows.push(row);
    let (resolution, findings) = world.resolve();
    assert!(has(&findings, "catch_duplicate", "F-INST-01"));
    // An ambiguous (duplicated) row proves nothing.
    assert!(matches!(
        resolution.faults["F-INST-01"],
        FaultState::Open(_)
    ));
}

// ------------------------------------------------------------------------------ evidence_for groups

#[test]
fn an_evidence_for_group_needs_named_known_proven_behaviours() {
    let mut world = World::new("g-e-none");
    world.disp.set("T:test_alpha.py", "b_ids", "");
    let (resolution, _) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_alpha.py"),
        vec!["no_behaviours"]
    );

    let mut world = World::new("g-e-unknown");
    world
        .disp
        .set("T:test_alpha.py", "b_ids", "B-INST-01;B-INST-77");
    let (resolution, _) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_alpha.py"),
        vec!["behaviour_unknown"]
    );

    // One of two named behaviours is not proven: the group is not resolved.
    let mut world = World::new("g-e-half");
    world.behaviours.set("B-INST-01", "rust_tests", "");
    let (resolution, _) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_alpha.py"),
        vec!["behaviour_unproven"]
    );
    assert_eq!(unresolved(&resolution), vec!["T:test_alpha.py"]);
}

#[test]
fn a_group_may_name_its_behaviours_with_commas_too() {
    let mut world = World::new("g-e-commas");
    world
        .disp
        .set("T:test_alpha.py", "b_ids", "B-INST-01,B-CONSENT-01");
    let (resolution, _) = world.resolve();
    assert_eq!(unresolved(&resolution), Vec::<String>::new());
}

// ------------------------------------------------------------------------------ retirement groups

#[test]
fn a_retirement_group_needs_a_reason() {
    for (key, column) in [
        ("T:test_zeta.py", "reason_or_reachability"),
        ("T:test_eps.py", "reason_or_reachability"),
    ] {
        let mut world = World::new("g-r-reason");
        world.disp.set(key, column, "  ");
        let (resolution, _) = world.resolve();
        assert_eq!(reasons(&resolution, key), vec!["reason_missing"], "{key}");
    }
    // Mechanical import evidence is a reason like any other.
    let mut world = World::new("g-r-reason3");
    world
        .disp
        .set("T:test_beta.py", "reason_or_reachability", "");
    let (resolution, _) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_beta.py"),
        vec!["reason_missing"]
    );
}

#[test]
fn a_retirement_needs_its_rollback_location_in_the_inventory() {
    // A group whose file is not in the archived inventory has no rollback location.
    let mut world = World::new("g-r-rollback");
    world
        .disp
        .set("T:conftest.py", "group_id", "T:conftest_gone.py");
    world
        .disp
        .set("T:conftest_gone.py", "path", "conftest_gone.py");
    let (resolution, findings) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:conftest_gone.py"),
        vec!["rollback_unknown"]
    );
    assert!(has(
        &findings,
        "group_not_in_inventory",
        "T:conftest_gone.py"
    ));
}

#[test]
fn a_retirement_needs_an_allowed_approver_and_a_real_date() {
    let mut world = World::new("g-r-approver");
    world.policy = POLICY.replace(
        "\"default_approver\":\"integrator-provisional\"",
        "\"default_approver\":\"nobody\"",
    );
    let (resolution, _) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_beta.py"),
        vec!["approver_invalid"]
    );
    // The evidence_for group needs no retirement approver.
    assert!(group(&resolution, "T:test_alpha.py").via.is_some());

    let mut world = World::new("g-r-date");
    world.policy = POLICY.replace("2026-10-29", "2026-13-40");
    let (resolution, _) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_beta.py"),
        vec!["approved_invalid"]
    );

    // An owner override resolves a group the default approver is not allowed to.
    let mut world = World::new("g-r-override");
    world.policy = POLICY
        .replace("\"default_approver\":\"integrator-provisional\"", "\"default_approver\":\"nobody\"")
        .replace(
            "\"approved\":\"2026-10-29\"",
            "\"approved\":\"2026-10-29\",\"overrides\":{\"T:test_beta.py\":{\"approver\":\"owner\",\"approved\":\"2026-10-30\"}}",
        );
    let (resolution, _) = world.resolve();
    assert!(!unresolved(&resolution).contains(&"T:test_beta.py".to_string()));
    assert!(unresolved(&resolution).contains(&"T:test_zeta.py".to_string()));
}

#[test]
fn not_executed_needs_a_reachability_argument_that_cites_code() {
    for argument in [
        "",
        "not executed",
        "NOT EXECUTED because nothing calls it and I am sure about that, honestly.",
    ] {
        let mut world = World::new("g-r-reach");
        world
            .disp
            .set("T:test_gamma.py", "reason_or_reachability", argument);
        let (resolution, _) = world.resolve();
        assert_eq!(
            reasons(&resolution, "T:test_gamma.py"),
            vec![if argument.is_empty() {
                "reason_missing"
            } else {
                "reachability_missing"
            }],
            "{argument:?}"
        );
    }
    // A short argument in module-name style, as the real files write them, is accepted.
    let mut world = World::new("g-r-reach-short");
    world.disp.set(
        "T:test_gamma.py",
        "reason_or_reachability",
        "mycelium_layer_planner.__main__ planner CLI: no app, worker, seed or relay path execs it.",
    );
    let (resolution, _) = world.resolve();
    assert!(group(&resolution, "T:test_gamma.py").via.is_some());
    // Non-ASCII text around the citation (the real files contain section signs) is read, not sliced.
    let mut world = World::new("g-r-reach-utf8");
    world.disp.set(
        "T:test_gamma.py",
        "reason_or_reachability",
        "§ NOT EXECUTED: phone path §§ importers: scripts/run.py:12 and nothing from the app",
    );
    let (resolution, _) = world.resolve();
    assert!(group(&resolution, "T:test_gamma.py").via.is_some());
    // The same text is a fine reason for the other categories.
    let mut world = World::new("g-r-reach2");
    world.disp.set(
        "T:test_zeta.py",
        "reason_or_reachability",
        "NOT EXECUTED because nothing calls it and I am sure about that, honestly.",
    );
    let (resolution, _) = world.resolve();
    assert!(group(&resolution, "T:test_zeta.py").via.is_some());
}

#[test]
fn superseded_tooling_names_an_existing_release_behaviour() {
    let mut world = World::new("g-r-sup1");
    world.disp.set("T:test_delta.py", "b_ids", "");
    let (resolution, _) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_delta.py"),
        vec!["no_behaviours"]
    );
    let mut world = World::new("g-r-sup2");
    world.disp.set("T:test_delta.py", "b_ids", "B-REL-99");
    let (resolution, _) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_delta.py"),
        vec!["behaviour_unknown"]
    );
    let mut world = World::new("g-r-sup3");
    world.disp.set("T:test_delta.py", "b_ids", "B-INST-01");
    let (resolution, _) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_delta.py"),
        vec!["not_a_release_behaviour"]
    );
    // It needs one B-REL behaviour; other behaviours may be named beside it.
    let mut world = World::new("g-r-sup3b");
    world
        .disp
        .set("T:test_delta.py", "b_ids", "B-INST-01,B-REL-01");
    let (resolution, _) = world.resolve();
    assert!(group(&resolution, "T:test_delta.py").via.is_some());
    // The replacing behaviour need not be proven yet for the *retirement* (plan section 7).
    let mut world = World::new("g-r-sup4");
    world.behaviours.set("B-REL-01", "rust_tests", "");
    let (resolution, _) = world.resolve();
    assert!(group(&resolution, "T:test_delta.py").via.is_some());
}

// ------------------------------------------------------------------------------ dispositions

#[test]
fn a_group_still_at_needs_review_or_with_an_invalid_category_is_unresolved() {
    let mut world = World::new("g-d-review");
    world.disp.drop_row("T:test_zeta.py");
    let (resolution, _) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_zeta.py"),
        vec!["undispositioned"]
    );
    assert_eq!(unresolved(&resolution), vec!["T:test_zeta.py"]);

    let mut world = World::new("g-d-invalid");
    world.disp.set("T:test_zeta.py", "disposition", "retired");
    let (resolution, findings) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_zeta.py"),
        vec!["disposition_invalid"]
    );
    assert!(has(&findings, "disposition_invalid", "T:test_zeta.py"));

    // evidence_for with no behaviours has nothing to stand on.
    let mut world = World::new("g-d-evidence-nob");
    world
        .disp
        .set("T:test_zeta.py", "disposition", "evidence_for");
    let (resolution, _) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_zeta.py"),
        vec!["no_behaviours"]
    );

    // A row left at needs_review in the final file is a finding, not just an open group.
    let mut world = World::new("g-d-review-row");
    world
        .disp
        .set("T:test_zeta.py", "disposition", "needs_review");
    let (resolution, findings) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_zeta.py"),
        vec!["undispositioned"]
    );
    assert!(has(&findings, "disposition_invalid", "T:test_zeta.py"));

    // A group with no row in the final file stays in the report, open, with a finding.
    let mut world = World::new("g-d-norow");
    world.disp.drop_row("T:test_zeta.py");
    let (resolution, findings) = world.resolve();
    assert_eq!(resolution.groups.len(), 7);
    assert_eq!(
        reasons(&resolution, "T:test_zeta.py"),
        vec!["undispositioned"]
    );
    assert!(has(
        &findings,
        "inventory_file_without_group",
        "test_zeta.py"
    ));
}

#[test]
fn the_final_file_is_the_only_source_and_its_conflicts_and_strays_are_findings() {
    // One row per group: the disposition in it is the disposition.
    let mut world = World::new("g-d-final");
    world
        .disp
        .set("T:test_zeta.py", "disposition", "not_shipped");
    let (resolution, findings) = world.resolve();
    assert_eq!(codes(&findings), Vec::<&str>::new(), "{findings:?}");
    assert_eq!(
        group(&resolution, "T:test_zeta.py").via.as_deref(),
        Some("not_shipped")
    );

    // A group listed twice conflicts and resolves nothing, whichever row comes first.
    let mut world = World::new("g-d-dup");
    let mut row = world.disp.rows[5].clone();
    row[3] = "not_shipped".into();
    world.disp.rows.push(row);
    let (resolution, findings) = world.resolve();
    assert!(has(&findings, "group_duplicate", "T:test_zeta.py"));
    assert_eq!(
        reasons(&resolution, "T:test_zeta.py"),
        vec!["disposition_conflict"]
    );
    assert_eq!(resolution.groups.len(), 7);

    // A row for a file the Python inventory does not have.
    let mut world = World::new("g-d-stray");
    world.disp.push(&[
        "T:nope.py",
        "nope.py",
        "1",
        "python_only",
        "",
        "x",
        "TB-3",
        "high",
    ]);
    let (_, findings) = world.resolve();
    assert!(has(&findings, "group_not_in_inventory", "T:nope.py"));

    // A group id that is not `T:<path>`.
    let mut world = World::new("g-d-path");
    world.disp.set("T:test_zeta.py", "path", "test_eps.py");
    let (_, findings) = world.resolve();
    assert!(has(&findings, "group_path_mismatch", "T:test_zeta.py"));
}

#[test]
fn n_tests_and_the_inventory_must_agree_with_the_groups() {
    let mut world = World::new("g-d-n");
    world.disp.set("T:test_beta.py", "n_tests", "3");
    let (_, findings) = world.resolve();
    assert!(has(&findings, "n_tests_mismatch", "T:test_beta.py"));
    let mut world = World::new("g-d-n2");
    world.disp.set("T:test_beta.py", "n_tests", "many");
    let (_, findings) = world.resolve();
    assert!(has(&findings, "n_tests_invalid", "T:test_beta.py"));
    // An inventory file with no group at all.
    let mut world = World::new("g-d-missing");
    world.disp.drop_row("T:test_beta.py");
    let (_, findings) = world.resolve();
    assert!(has(
        &findings,
        "inventory_file_without_group",
        "test_beta.py"
    ));
}

// ------------------------------------------------------------------------------ schema-1 rows

#[test]
fn schema_1_rows_resolve_a_group_only_when_every_id_of_it_is_mapped() {
    let all_beta = ["py:test_beta.py::test_four", "py:test_beta.py::test_five"];
    // Even a group nobody dispositioned resolves when schema-1 rows map every id.
    let mut world = World::new("g-s1");
    world.disp.drop_row("T:test_zeta.py");
    world.schema1 = vec!["py:test_zeta.py::test_z".into()];
    let (resolution, _) = world.resolve();
    assert_eq!(
        group(&resolution, "T:test_zeta.py").via.as_deref(),
        Some("schema1")
    );

    // One id of two mapped: the group is not resolved by schema 1.
    let mut world = World::new("g-s1-half");
    world
        .disp
        .set("T:test_beta.py", "disposition", "needs_review");
    world.schema1 = vec![all_beta[0].into()];
    let (resolution, _) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:test_beta.py"),
        vec!["undispositioned"]
    );

    let mut world = World::new("g-s1-all");
    world
        .disp
        .set("T:test_beta.py", "disposition", "needs_review");
    world.schema1 = all_beta.iter().map(|s| (*s).to_string()).collect();
    let (resolution, _) = world.resolve();
    assert_eq!(
        group(&resolution, "T:test_beta.py").via.as_deref(),
        Some("schema1")
    );

    // A group with no tests cannot be resolved by schema-1 rows (nothing is mapped).
    let mut world = World::new("g-s1-empty");
    world
        .disp
        .set("T:conftest.py", "disposition", "needs_review");
    let (resolution, _) = world.resolve();
    assert_eq!(
        reasons(&resolution, "T:conftest.py"),
        vec!["undispositioned"]
    );
}

// ------------------------------------------------------------------ the documented line format

#[test]
fn the_fault_caught_line_format_in_the_readme_is_what_the_resolver_reads() {
    let closed = |log: &str| {
        let mut world = World::new("g-f-format");
        world.logs[1].1 = log.to_string();
        let (resolution, _) = world.resolve();
        matches!(resolution.faults["F-CONSENT-01"], FaultState::Caught(_))
    };
    let base = MUTATION_LOG;
    assert!(closed(base));
    // Blanks around the line and a trailing note are allowed.
    assert!(closed(&base.replace(
        "FAULT-CAUGHT F-CONSENT-01 by test_c",
        "   FAULT-CAUGHT F-CONSENT-01 by test_c   (mutant M7, intended assertion)"
    )));
    // A longer module path that ends with the test name is the same test.
    assert!(closed(&base.replace(
        "FAULT-CAUGHT F-CONSENT-01 by test_c",
        "FAULT-CAUGHT F-CONSENT-01 by outer::test_c"
    )));
    // Near misses are not catches.
    for wrong in [
        "FAULT-CAUGHT F-CONSENT-01 test_c",
        "FAULT-CAUGHT F-CONSENT-01 by",
        "FAULT-CAUGHT F-CONSENT-01 bytest_c",
        "fault-caught F-CONSENT-01 by test_c",
        "XFAULT-CAUGHT F-CONSENT-01 by test_c",
        "FAULT-CAUGHT F-CONSENT-01 by test_cc",
        "FAULT-CAUGHT F-CONSENT-02 by test_c",
        "FAULT-CAUGHT: F-CONSENT-01 by test_c",
    ] {
        let log = base.replace("FAULT-CAUGHT F-CONSENT-01 by test_c", wrong);
        assert!(!closed(&log), "{wrong}");
    }
}
