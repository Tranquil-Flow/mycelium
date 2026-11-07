// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask retire check` (schema 2) and `xtask behaviours check` through the real binary, on a
//! registry directory written from the fixture world.
mod common;
use common::*;

fn retire(world: &World, extra: &[&str]) -> std::process::Output {
    run_xtask("retire", &world.materialise(), extra)
}
fn behaviours(world: &World, extra: &[&str]) -> std::process::Output {
    run_xtask("behaviours", &world.materialise(), extra)
}

#[test]
fn schema_2_exits_0_only_when_every_group_behaviour_and_fault_is_resolved() {
    let world = World::new("c-ok");
    let output = retire(&world, &[]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(0), "{text}\n{output:?}");
    assert!(text.contains("7 total, 7 resolved"), "{text}");
    assert!(text.contains("3 total, 3 proven, 0 unproven"), "{text}");
    assert!(text.contains("RESOLVED"), "{text}");
    assert!(text.contains("INST"), "{text}");
}

#[test]
fn an_undispositioned_group_exits_1_and_is_listed_with_its_reason() {
    let mut world = World::new("c-open");
    world.disp.drop_row("T:test_zeta.py");
    let output = retire(&world, &[]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(1), "{text}");
    assert!(
        text.contains("unresolved T:test_zeta.py [needs_review] undispositioned"),
        "{text}"
    );
    assert!(text.contains("NOT RESOLVED"), "{text}");
}

#[test]
fn an_unproven_behaviour_or_an_open_fault_fails_even_when_every_group_resolves() {
    // The retirement groups need no behaviour; B-CONSENT-01 is named by alpha only, so make the
    // fault of a behaviour nothing names open instead: B-REL-01 is named by the delta retirement.
    let mut world = World::new("c-fault");
    world.catches.drop_row("F-REL-02");
    let output = retire(&world, &[]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(1), "{text}");
    assert!(
        text.contains("1 behaviours and 1 faults unresolved"),
        "{text}"
    );
    assert!(text.contains("no_catch_row=1"), "{text}");
}

#[test]
fn each_kind_of_leftover_alone_is_enough_to_exit_1() {
    // An unproven behaviour with every fault closed and every group resolved. (open, no evidence:
    // no catalogue finding, just not proven.)
    let mut world = World::new("c-only-unproven");
    world.behaviours.set("B-REL-01", "status", "open");
    world.behaviours.set("B-REL-01", "evidence", "");
    let output = retire(&world, &[]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(1), "{text}");
    assert!(
        text.contains("0 groups, 1 behaviours and 0 faults unresolved, 0 findings"),
        "{text}"
    );
    // Only a finding: everything resolves, but a catch row names a fault nobody has.
    let mut world = World::new("c-only-finding");
    world
        .catches
        .push(&["F-GHOST-01", "caught", EV_MUT, T1, "", ""]);
    let output = retire(&world, &[]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(1), "{text}");
    assert!(
        text.contains("0 groups, 0 behaviours and 0 faults unresolved, 1 findings"),
        "{text}"
    );
}

#[test]
fn without_a_ledger_schema_2_refuses_unless_structure_only() {
    let world = World::new("c-noledger");
    let mut args = world.materialise();
    let at = args.iter().position(|a| a == "--ledger").unwrap();
    args.drain(at..at + 2);
    args.push("--registry-dir".into());
    args.push(world.dir.display().to_string());
    let output = run_xtask("retire", &args, &[]);
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stderr).contains("needs --ledger"));
    let output = run_xtask("retire", &args, &["--structure-only"]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(0), "{text}");
    assert!(text.contains("STRUCTURE-ONLY"), "{text}");
    assert!(text.contains("not an R6 pass"), "{text}");
    assert!(!text.contains("RESOLVED"), "{text}");
}

#[test]
fn structure_only_still_fails_on_a_catalogue_defect() {
    let mut world = World::new("c-structure");
    world.behaviours.set("B-INST-01", "kind", "X");
    let output = retire(&world, &["--structure-only"]);
    assert_eq!(output.status.code(), Some(1), "{}", stdout(&output));
}

#[test]
fn a_malformed_registry_file_exits_2() {
    let mut world = World::new("c-bad");
    world.behaviours.cols[2] = "claim";
    let output = retire(&world, &[]);
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    assert!(String::from_utf8_lossy(&output.stderr).contains("BEHAVIOURS.tsv"));
    let world = World::new("c-bad2");
    let mut args = world.materialise();
    args.extend(["--bogus".to_string()]);
    let output = run_xtask("retire", &args, &[]);
    assert_eq!(output.status.code(), Some(2));
}

#[test]
fn schema_1_rows_count_as_evidence_and_resolve_their_group_in_the_schema_2_report() {
    let mut world = World::new("c-s1");
    // evidence_for with no behaviours: the group stands on nothing by itself.
    world
        .disp
        .set("T:test_zeta.py", "disposition", "evidence_for");
    let args = world.materialise();
    let output = run_xtask("retire", &args, &[]);
    assert_eq!(output.status.code(), Some(1), "{}", stdout(&output));
    assert!(stdout(&output).contains("0 of 9 old test ids resolved"));
    // A schema-1 row maps its only test; the cited PASS log shows the Rust test ok.
    let sha = {
        let text = std::fs::read_to_string(world.dir.join("registry.json")).unwrap();
        let v: serde_json::Value = serde_json::from_str(&text).unwrap();
        v["baseline"]["sha256"].as_str().unwrap().to_string()
    };
    std::fs::write(
        world.dir.join("obligations.json"),
        r#"[{"id":"O:path:test_zeta.py"}]"#,
    )
    .unwrap();
    std::fs::write(
        world.dir.join("registry.json"),
        serde_json::json!({"schema": 1, "baseline": {"sha256": sha, "source_sha": "f5bfb709"},
            "approvers": ["integrator", "owner"],
            "rows": [{"old": "py:test_zeta.py::test_z", "disposition": "mapped", "new": [T1],
                      "obligation": "O:path:test_zeta.py", "approver": "integrator",
                      "approved": "2026-10-28", "evidence": [EV_RUN]}]})
        .to_string(),
    )
    .unwrap();
    let output = run_xtask("retire", &args, &[]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(0), "{text}");
    assert!(
        text.contains("1 rows, 1 of 9 old test ids resolved"),
        "{text}"
    );
    assert!(text.contains("schema1=1"), "{text}");
}

#[test]
fn tb4_new_behaviours_are_loaded_and_counted() {
    let world = World::new("c-new");
    let args = world.materialise();
    std::fs::write(world.dir.join("tb4/P1-new-behaviours.tsv"), {
        let mut tsv = Tsv::new(&BCOLS, &[]);
        tsv.push(&[
            "NEW-P1-01",
            "HYG",
            "the socket lives under a short private parent",
            "why",
            SRC,
            "executed-v0.1.4",
            "J",
            "path under 100 bytes",
            "F-HYG-01",
            "",
            "",
            "open",
        ]);
        tsv.render()
    })
    .unwrap();
    let output = run_xtask("retire", &args, &[]);
    let text = stdout(&output);
    assert!(
        text.contains("4 behaviours (3 in BEHAVIOURS.tsv + 1 tb4 additions)"),
        "{text}"
    );
    // Its fault does not exist, and an open behaviour is not proven.
    assert_eq!(output.status.code(), Some(1), "{text}");
    assert!(text.contains("must_catch_unknown_fault"), "{text}");
}

#[test]
fn behaviours_check_exits_0_on_the_clean_world_and_1_on_findings() {
    let world = World::new("c-b-ok");
    let output = behaviours(&world, &[]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(0), "{text}\n{output:?}");
    assert!(text.contains("behaviours check: OK"), "{text}");
    assert!(text.contains("proven by evidence: 3 of 3"), "{text}");

    let mut world = World::new("c-b-dup");
    let row = world.behaviours.rows[0].clone();
    world.behaviours.rows.push(row);
    let output = behaviours(&world, &[]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(1), "{text}");
    assert!(text.contains("FINDINGS id_duplicate: 1"), "{text}");
}

#[test]
fn behaviours_check_catches_a_proven_status_the_evidence_does_not_back() {
    let mut world = World::new("c-b-over");
    world.logs[0].1 = RUN_LOG.replace("test test_c ... ok\n", "");
    let output = behaviours(&world, &[]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(1), "{text}");
    assert!(text.contains("status_overclaim"), "{text}");
}

#[test]
fn behaviours_check_needs_a_ledger_unless_structure_only_and_says_it_is_no_pass() {
    let world = World::new("c-b-struct");
    let mut args = world.materialise();
    let at = args.iter().position(|a| a == "--ledger").unwrap();
    args.drain(at..at + 2);
    args.push("--registry-dir".into());
    args.push(world.dir.display().to_string());
    let output = run_xtask("behaviours", &args, &[]);
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    let output = run_xtask("behaviours", &args, &["--structure-only"]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(0), "{text}");
    assert!(text.contains("STRUCTURE-ONLY"), "{text}");
}

#[test]
fn the_release_profile_r6_cell_refuses_structure_only_and_a_schema_override() {
    let profile: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(
            std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("release-profile.json"),
        )
        .unwrap(),
    )
    .unwrap();
    let cell = profile["cells"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["id"] == "r6-retirement")
        .unwrap();
    let forbids: Vec<&str> = cell["verify"]["argv_forbids"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|v| v.as_str())
        .collect();
    assert_eq!(forbids, vec!["--structure-only", "--schema"]);
}

#[test]
fn a_new_behaviour_that_joined_the_catalogue_is_read_once_and_groups_naming_it_are_translated() {
    let mut world = World::new("c-map");
    // The group names the proposal; the proposal has since become B-INST-01's twin in the catalogue.
    world
        .disp
        .set("T:test_alpha.py", "b_ids", "NEW-P1-01;B-CONSENT-01");
    let args = world.materialise();
    let new_row = |id: &str| {
        let mut tsv = Tsv::new(&BCOLS, &[]);
        tsv.push(&[
            id,
            "INST",
            "a proposal",
            "why",
            SRC,
            "executed-v0.1.4",
            "J",
            "case",
            "F-INST-01",
            "",
            "",
            "open",
        ]);
        tsv.render()
    };
    std::fs::write(
        world.dir.join("tb4/P1-new-behaviours.tsv"),
        new_row("NEW-P1-01"),
    )
    .unwrap();
    // Auxiliary files in tb4/ (not disposition files) are ignored, whatever their header.
    std::fs::write(
        world.dir.join("tb4/KEYCASE-HARVEST.tsv"),
        "behaviour\tcase\n",
    )
    .unwrap();
    // Not yet mapped: the proposal is a separate, unproven behaviour, so the group stays open.
    let output = run_xtask("retire", &args, &[]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(1), "{text}");
    assert!(
        text.contains("4 behaviours (3 in BEHAVIOURS.tsv + 1 tb4 additions)"),
        "{text}"
    );
    // Mapped to a catalogue id: counted once, translated, the group resolves again.
    std::fs::write(
        world.dir.join("tb4/NEW-ID-MAP.tsv"),
        "new_id\tbehaviour_id\tarea\tsource_file\nNEW-P1-01\tB-INST-01\tINST\ttb4/P1-new-behaviours.tsv\n",
    )
    .unwrap();
    let output = run_xtask("retire", &args, &[]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(0), "{text}");
    assert!(
        text.contains("3 behaviours (3 in BEHAVIOURS.tsv + 0 tb4 additions)"),
        "{text}"
    );
}

// ------------------------------------------------------------------ the disposition source (r14)

#[test]
fn the_consolidated_final_file_is_the_default_source_and_p_files_are_provenance_only() {
    let world = World::new("c-final");
    let args = world.materialise();
    // Decoys that would change the answer if anything read them: a TB-3 style file with a bad
    // header and a P1.tsv that dispositions every group `needs_review`.
    std::fs::write(
        world.dir.join("TEST-GROUP-DISPOSITIONS.tsv"),
        "not\ta\tdisposition\tfile\n",
    )
    .unwrap();
    let mut p1 = Tsv::new(&DISPCOLS, &[]);
    for row in &world.disp.rows {
        let mut row = row.clone();
        row[3] = "needs_review".into();
        let cells: Vec<&str> = row.iter().map(String::as_str).collect();
        p1.push(&cells);
    }
    std::fs::write(world.dir.join("tb4/P1.tsv"), p1.render()).unwrap();
    std::fs::write(world.dir.join("tb4/P2.tsv"), "garbage\n").unwrap();
    let output = run_xtask("retire", &args, &[]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(0), "{text}\n{output:?}");
    assert!(text.contains("7 total, 7 resolved"), "{text}");
}

#[test]
fn dispositions_flag_names_another_final_file_and_a_missing_or_old_style_file_exits_2() {
    let world = World::new("c-final-flag");
    let args = world.materialise();
    // An alternative file with one group flipped to an invalid category.
    let mut other = world
        .disp
        .render()
        .replace("python_only\t\ttests a pytest", "retired\t\ttests a pytest");
    assert_ne!(other, world.disp.render(), "the fixture row must exist");
    let alt = world.dir.join("alt.final.tsv");
    std::fs::write(&alt, &other).unwrap();
    let alt_arg = alt.display().to_string();
    let output = run_xtask("retire", &args, &["--dispositions", &alt_arg]);
    let text = stdout(&output);
    assert_eq!(output.status.code(), Some(1), "{text}");
    assert!(text.contains("disposition_invalid"), "{text}");
    // Missing file: a load error, never "no groups".
    let gone = world.dir.join("gone.tsv").display().to_string();
    let output = run_xtask("retire", &args, &["--dispositions", &gone]);
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    // The TB-3 header is not a final file.
    other = "group_id\tpath\tn_tests\tproposed_disposition\tevidence\n".into();
    std::fs::write(&alt, &other).unwrap();
    let output = run_xtask("retire", &args, &["--dispositions", &alt_arg]);
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    assert!(
        String::from_utf8_lossy(&output.stderr).contains("TEST-GROUP-DISPOSITIONS"),
        "{output:?}"
    );
}
