// SPDX-License-Identifier: AGPL-3.0-only
//! R6 test-retirement check: old test IDs against Rust test IDs and obligations, never counts.
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    fs,
    path::PathBuf,
    process::{Command, Output},
};
use xtask::{
    lint::Source,
    retire::{self, Baseline, Context, Proofs, Report},
};

fn fixture(name: &str) -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures/retire");
    fs::read_to_string(path.join(name)).unwrap()
}
fn fixture_baseline() -> Baseline {
    retire::load_baseline(
        fixture("baseline.json").as_bytes(),
        "f5bfb70981569c79cb873afe8e0c5be20342db7e",
    )
    .unwrap()
}
fn source(path: &str, text: &str) -> Source {
    Source {
        path: path.to_string(),
        text: text.to_string(),
    }
}
const ALPHA: &str = "#[test]\nfn replaces_one_and_two() {}\nmod nested {\n    #[test]\n    fn inner() {}\n}\n#[tokio::test]\nasync fn async_case() {}\nfn helper() {}\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn in_cfg() {}\n}\n";
fn rust_tests() -> BTreeSet<String> {
    retire::discover_rust_tests(&[source("crates/x/tests/alpha.rs", ALPHA)]).unwrap()
}
fn obligations() -> BTreeSet<String> {
    let rows: Vec<Value> = serde_json::from_str(&fixture("obligations.json")).unwrap();
    rows.iter()
        .map(|r| r["id"].as_str().unwrap().to_string())
        .collect()
}
fn check_with(registry: &Value, evidence: Option<&BTreeSet<String>>) -> Report {
    let baseline = fixture_baseline();
    let obligations = obligations();
    let rust = rust_tests();
    retire::check(
        &registry.to_string(),
        &Context {
            baseline: &baseline,
            obligations: &obligations,
            rust_tests: &rust,
            evidence_ids: evidence,
            test_proofs: None,
        },
    )
    .unwrap()
}
fn ok_registry() -> Value {
    serde_json::from_str(&fixture("registry-ok.json")).unwrap()
}
fn codes(registry: &Value) -> Vec<&'static str> {
    check_with(registry, None).codes()
}

#[test]
fn baseline_ids_are_file_and_test_name_not_a_count() {
    let baseline = fixture_baseline();
    let expected: BTreeSet<String> = [
        "py:test_alpha.py::test_one",
        "py:test_alpha.py::test_two",
        "py:test_alpha.py::test_three",
        "py:test_beta.py::test_four",
        "py:test_beta.py::test_five",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    assert_eq!(baseline.ids, expected);
    assert_eq!((baseline.files, baseline.support_files), (2, 1));
    assert_eq!(
        baseline.source_sha,
        "f5bfb70981569c79cb873afe8e0c5be20342db7e"
    );
}

#[test]
fn the_baseline_pin_is_the_sha256_of_the_file_bytes() {
    let out = Command::new("/usr/bin/shasum")
        .args(["-a", "256"])
        .arg(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures/retire/baseline.json"))
        .output()
        .unwrap();
    let expected = String::from_utf8(out.stdout)
        .unwrap()
        .split_whitespace()
        .next()
        .unwrap()
        .to_string();
    assert_eq!(fixture_baseline().sha256, expected);
}

#[test]
fn a_repeated_test_name_inside_one_file_is_a_baseline_defect() {
    let text = json!([{"id": "T:a", "source": "a.py", "role": "test_file", "source_tests": [
        {"name": "test_x", "line": 1}, {"name": "test_x", "line": 9}]}]);
    let error = retire::load_baseline(text.to_string().as_bytes(), "f").unwrap_err();
    assert!(error.contains("baseline_duplicate_id"), "{error}");
}

#[test]
fn a_baseline_with_an_unknown_role_or_bad_json_is_refused() {
    let bad_role = json!([{"id": "T:a", "source": "a.py", "role": "mystery", "source_tests": []}]);
    let error = retire::load_baseline(bad_role.to_string().as_bytes(), "f").unwrap_err();
    assert!(error.contains("baseline_invalid"), "{error}");
    let error = retire::load_baseline(b"[", "f").unwrap_err();
    assert!(error.contains("baseline_invalid"), "{error}");
}

#[test]
fn rust_discovery_finds_every_test_attribute_shape_and_no_helper() {
    let ids = rust_tests();
    let expected: BTreeSet<String> = [
        "rs:crates/x/tests/alpha.rs::replaces_one_and_two",
        "rs:crates/x/tests/alpha.rs::nested::inner",
        "rs:crates/x/tests/alpha.rs::async_case",
        "rs:crates/x/tests/alpha.rs::tests::in_cfg",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    assert_eq!(ids, expected);
}

#[test]
fn rust_discovery_refuses_a_file_it_cannot_parse() {
    let error = retire::discover_rust_tests(&[source("crates/x/src/lib.rs", "fn (")]).unwrap_err();
    assert!(error.contains("rust_parse_failed"), "{error}");
    assert!(error.contains("crates/x/src/lib.rs"), "{error}");
}

#[test]
fn an_approved_registry_resolves_every_old_id_though_the_counts_differ() {
    // 5 old tests map onto 2 distinct Rust tests (consolidation), one is retired.
    let report = check_with(&ok_registry(), None);
    assert!(report.findings.is_empty(), "{:?}", report.findings);
    assert_eq!((report.total_old, report.resolved, report.rows), (5, 5, 4));
    assert!(report.ok());
    assert!(report.unresolved.is_empty());
}

#[test]
fn an_empty_registry_leaves_every_old_id_unresolved_even_if_the_counts_match() {
    // 5 old IDs and (below) 5 Rust tests: equal counts, still nothing resolved.
    let mut registry = ok_registry();
    registry["rows"] = json!([]);
    let baseline = fixture_baseline();
    let rust: BTreeSet<String> = (0..5)
        .map(|n| format!("rs:crates/x/tests/n.rs::t{n}"))
        .collect();
    assert_eq!(rust.len(), baseline.ids.len());
    let obligations = obligations();
    let report = retire::check(
        &registry.to_string(),
        &Context {
            baseline: &baseline,
            obligations: &obligations,
            rust_tests: &rust,
            evidence_ids: None,
            test_proofs: None,
        },
    )
    .unwrap();
    assert!(report.structure_ok());
    assert!(!report.ok());
    assert_eq!(report.resolved, 0);
    assert_eq!(report.unresolved, baseline.ids);
    assert_eq!(report.unresolved_by_file["test_alpha.py"], 3);
    assert_eq!(report.unresolved_by_file["test_beta.py"], 2);
}

#[test]
fn dropping_one_row_leaves_exactly_that_id_unresolved() {
    let mut registry = ok_registry();
    registry["rows"].as_array_mut().unwrap().remove(1);
    let report = check_with(&registry, None);
    assert_eq!(
        report.unresolved.iter().collect::<Vec<_>>(),
        ["py:test_alpha.py::test_two"]
    );
    assert!(!report.ok());
}

fn mutate(edit: impl FnOnce(&mut Value)) -> Value {
    let mut registry = ok_registry();
    edit(&mut registry);
    registry
}
fn row(registry: &mut Value, index: usize) -> &mut Value {
    &mut registry["rows"][index]
}

#[test]
fn the_pin_and_the_source_sha_must_equal_the_baseline() {
    let pin = mutate(|r| r["baseline"]["sha256"] = json!("0".repeat(64)));
    assert_eq!(codes(&pin), ["baseline_pin_mismatch"]);
    assert_eq!(
        check_with(&pin, None).resolved,
        0,
        "a wrong pin resolves nothing"
    );
    let source = mutate(|r| r["baseline"]["source_sha"] = json!("1".repeat(40)));
    assert_eq!(codes(&source), ["baseline_source_mismatch"]);
}

#[test]
fn rows_naming_unknown_or_repeated_old_tests_are_findings() {
    let unknown = mutate(|r| row(r, 0)["old"] = json!("py:test_alpha.py::test_zzz"));
    let report = check_with(&unknown, None);
    assert!(report.codes().contains(&"old_id_unknown"));
    // The row that no longer names test_one leaves test_one unresolved.
    assert!(report.unresolved.contains("py:test_alpha.py::test_one"));
    let file = mutate(|r| row(r, 3)["old_file"] = json!("test_gamma.py"));
    assert!(codes(&file).contains(&"old_file_unknown"));
    let repeated = mutate(|r| row(r, 1)["old"] = json!("py:test_alpha.py::test_one"));
    assert!(codes(&repeated).contains(&"old_id_duplicate"));
    let overlap = mutate(|r| row(r, 3)["old_file"] = json!("test_alpha.py"));
    assert!(codes(&overlap).contains(&"old_id_duplicate"));
}

#[test]
fn a_row_names_exactly_one_of_old_and_old_file() {
    let both = mutate(|r| row(r, 0)["old_file"] = json!("test_alpha.py"));
    assert!(codes(&both).contains(&"row_shape_invalid"));
    let neither = mutate(|r| {
        row(r, 0).as_object_mut().unwrap().remove("old");
    });
    assert!(codes(&neither).contains(&"row_shape_invalid"));
}

#[test]
fn dispositions_and_replacements_are_checked() {
    let bad = mutate(|r| row(r, 0)["disposition"] = json!("deleted"));
    assert!(codes(&bad).contains(&"disposition_invalid"));
    let no_new = mutate(|r| row(r, 0)["new"] = json!([]));
    assert!(codes(&no_new).contains(&"new_missing"));
    let ghost = mutate(|r| row(r, 0)["new"] = json!(["rs:crates/x/tests/alpha.rs::vanished"]));
    assert!(codes(&ghost).contains(&"new_test_unknown"));
}

#[test]
fn every_row_needs_a_known_obligation() {
    let missing = mutate(|r| {
        row(r, 0).as_object_mut().unwrap().remove("obligation");
    });
    assert!(codes(&missing).contains(&"obligation_missing"));
    let unknown = mutate(|r| row(r, 0)["obligation"] = json!("O:path:nothing"));
    assert!(codes(&unknown).contains(&"obligation_unknown"));
}

#[test]
fn every_row_needs_a_listed_approver_and_a_real_date() {
    let none = mutate(|r| {
        row(r, 0).as_object_mut().unwrap().remove("approver");
    });
    assert!(codes(&none).contains(&"approver_missing"));
    let stranger = mutate(|r| row(r, 0)["approver"] = json!("the-agent"));
    assert!(codes(&stranger).contains(&"approver_unknown"));
    // An unapproved mapping resolves nothing.
    let report = check_with(&stranger, None);
    assert!(report.unresolved.contains("py:test_alpha.py::test_one"));
    assert_eq!(report.resolved, 4);
    let date = mutate(|r| row(r, 0)["approved"] = json!("2026-02-30"));
    assert!(codes(&date).contains(&"approved_invalid"));
    let undated = mutate(|r| {
        row(r, 0).as_object_mut().unwrap().remove("approved");
    });
    assert!(codes(&undated).contains(&"approved_invalid"));
}

#[test]
fn evidence_is_required_and_ledger_checked_when_a_ledger_is_given() {
    let empty = mutate(|r| row(r, 0)["evidence"] = json!([]));
    assert!(codes(&empty).contains(&"evidence_missing"));
    let prose = mutate(|r| row(r, 0)["evidence"] = json!(["see the report"]));
    assert!(codes(&prose).contains(&"evidence_malformed"));
    let ledger: BTreeSet<String> = ["EV-20261028-0001".to_string()].into();
    let report = check_with(&ok_registry(), Some(&ledger));
    assert!(
        report.codes().contains(&"evidence_unknown"),
        "{:?}",
        report.codes()
    );
    let ledger: BTreeSet<String> = ["EV-20261028-0004".to_string()].into();
    assert!(check_with(&ok_registry(), Some(&ledger)).ok());
}

#[test]
fn a_retirement_needs_a_reason_a_rollback_and_an_honest_mutation_claim() {
    let no_reason = mutate(|r| {
        row(r, 2).as_object_mut().unwrap().remove("reason");
    });
    assert!(codes(&no_reason).contains(&"reason_missing"));
    let no_rollback = mutate(|r| {
        row(r, 2).as_object_mut().unwrap().remove("rollback");
    });
    assert!(codes(&no_rollback).contains(&"rollback_missing"));
    let claimed = mutate(|r| row(r, 2)["mutation_verified"] = json!(true));
    assert!(codes(&claimed).contains(&"mutation_unproven"));
    let proven = mutate(|r| {
        row(r, 2)["mutation_verified"] = json!(true);
        row(r, 2)["kill_matrix"] = json!("EV-20261028-0004");
    });
    assert!(check_with(&proven, None).ok());
}

#[test]
fn an_external_oracle_row_needs_a_reason_but_no_replacement() {
    let external = mutate(|r| {
        *row(r, 2) = json!({"old": "py:test_alpha.py::test_three", "disposition": "external_oracle",
            "new": [], "obligation": "O:path:test_alpha.py", "approver": "owner",
            "approved": "2026-10-28", "evidence": ["EV-20261028-0004"], "reason": "numerical report generator kept outside the product"});
    });
    assert!(check_with(&external, None).ok());
    let reasonless = mutate(|r| {
        *row(r, 2) = json!({"old": "py:test_alpha.py::test_three", "disposition": "external_oracle",
            "new": [], "obligation": "O:path:test_alpha.py", "approver": "owner",
            "approved": "2026-10-28", "evidence": ["EV-20261028-0004"]});
    });
    assert!(codes(&reasonless).contains(&"reason_missing"));
}

#[test]
fn every_finding_is_reported_not_just_the_first() {
    let broken = mutate(|r| {
        row(r, 0)["approver"] = json!("nobody");
        row(r, 1)["obligation"] = json!("O:path:nothing");
        row(r, 2)["rollback"] = json!("");
    });
    let found = codes(&broken);
    for code in ["approver_unknown", "obligation_unknown", "rollback_missing"] {
        assert!(found.contains(&code), "{code} in {found:?}");
    }
}

#[test]
fn a_registry_with_an_unknown_field_or_schema_is_malformed_not_silently_read() {
    let baseline = fixture_baseline();
    let obligations = obligations();
    let rust = rust_tests();
    let context = Context {
        baseline: &baseline,
        obligations: &obligations,
        rust_tests: &rust,
        evidence_ids: None,
        test_proofs: None,
    };
    let typo = mutate(|r| row(r, 0)["aproved"] = json!("2026-10-28"));
    let error = retire::check(&typo.to_string(), &context).unwrap_err();
    assert!(error.contains("registry_invalid"), "{error}");
    let schema = mutate(|r| r["schema"] = json!(2));
    let error = retire::check(&schema.to_string(), &context).unwrap_err();
    assert!(error.contains("registry_invalid"), "{error}");
}

// ----------------------------------------------------------------- the checked-in data

fn workspace() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .to_path_buf()
}

#[test]
fn the_checked_in_registry_is_pinned_to_the_real_python_inventory_and_well_formed() {
    let root = workspace();
    let data = root.join("test-support/obligations/data");
    let bytes = fs::read(data.join("test-groups.json")).unwrap();
    let baseline =
        retire::load_baseline(&bytes, "f5bfb70981569c79cb873afe8e0c5be20342db7e").unwrap();
    // Independent expected total: sum of the discovered definitions straight from the JSON.
    let rows: Vec<Value> = serde_json::from_slice(&bytes).unwrap();
    let defined: usize = rows
        .iter()
        .map(|r| r["source_tests"].as_array().unwrap().len())
        .sum();
    assert_eq!(baseline.ids.len(), defined);
    let obligations: BTreeSet<String> =
        serde_json::from_slice::<Vec<Value>>(&fs::read(data.join("obligations.json")).unwrap())
            .unwrap()
            .iter()
            .map(|r| r["id"].as_str().unwrap().to_string())
            .collect();
    let registry = fs::read_to_string(root.join("xtask/retirement/registry.json")).unwrap();
    let rust = BTreeSet::new();
    let report = retire::check(
        &registry,
        &Context {
            baseline: &baseline,
            obligations: &obligations,
            rust_tests: &rust,
            evidence_ids: None,
            test_proofs: None,
        },
    )
    .unwrap();
    assert!(
        !report.codes().contains(&"baseline_pin_mismatch"),
        "the registry pin must equal the sha256 of test-groups.json: {:?}",
        report.findings
    );
    // Rows that name Rust tests cannot be checked with an empty Rust set; only structure is asserted
    // here. Status is reported, never assumed: resolved + unresolved is the whole inventory.
    assert_eq!(
        report.resolved + report.unresolved.len(),
        baseline.ids.len()
    );
}

// ----------------------------------------------------------------- the command

fn xtask(args: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_xtask"))
        .arg("retire")
        .args(args)
        .output()
        .unwrap()
}
struct Temp(PathBuf);
impl Temp {
    fn new(tag: &str) -> Self {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../target/xtask-retire-tests")
            .join(format!("{}-{tag}", std::process::id()));
        fs::create_dir_all(&path).unwrap();
        Self(path)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn write_workspace(temp: &Temp, registry: &Value) -> Vec<String> {
    fs::write(temp.0.join("Cargo.toml"), "[workspace]\n").unwrap();
    fs::create_dir_all(temp.0.join("crates/x/tests")).unwrap();
    fs::write(temp.0.join("crates/x/tests/alpha.rs"), ALPHA).unwrap();
    fs::write(temp.0.join("baseline.json"), fixture("baseline.json")).unwrap();
    fs::write(temp.0.join("obligations.json"), fixture("obligations.json")).unwrap();
    fs::write(temp.0.join("registry.json"), registry.to_string()).unwrap();
    let path = |name: &str| temp.0.join(name).display().to_string();
    vec![
        "check".into(),
        "--schema".into(),
        "1".into(),
        "--baseline".into(),
        path("baseline.json"),
        "--baseline-source-sha".into(),
        "f5bfb70981569c79cb873afe8e0c5be20342db7e".into(),
        "--registry".into(),
        path("registry.json"),
        "--obligations".into(),
        path("obligations.json"),
        "--root".into(),
        temp.0.display().to_string(),
    ]
}
fn run_check(temp: &Temp, registry: &Value, extra: &[&str]) -> Output {
    let mut args = write_workspace(temp, registry);
    args.extend(extra.iter().map(|s| s.to_string()));
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    xtask(&refs)
}

#[test]
fn the_command_exits_0_only_when_every_old_id_is_resolved() {
    let temp = Temp::new("ok");
    let output = run_check(&temp, &ok_registry(), &[]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(
        text.contains("5 old test IDs: 5 resolved, 0 unresolved"),
        "{text}"
    );
}

#[test]
fn the_command_exits_1_and_lists_files_when_ids_are_unresolved() {
    let temp = Temp::new("open");
    let mut registry = ok_registry();
    registry["rows"].as_array_mut().unwrap().remove(1);
    let output = run_check(&temp, &registry, &[]);
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(text.contains("py:test_alpha.py::test_two"), "{text}");
    assert!(
        text.contains("unresolved by file: test_alpha.py=1"),
        "{text}"
    );
}

#[test]
fn structure_only_reports_the_gap_but_exits_0_and_says_it_is_not_a_release_pass() {
    let temp = Temp::new("structure");
    let mut registry = ok_registry();
    registry["rows"] = json!([]);
    let output = run_check(&temp, &registry, &["--structure-only"]);
    assert_eq!(output.status.code(), Some(0), "{output:?}");
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(text.contains("0 resolved, 5 unresolved"), "{text}");
    assert!(text.contains("STRUCTURE-ONLY"), "{text}");
    // A structural finding still fails in this mode.
    let mut bad = ok_registry();
    bad["rows"][0]["approver"] = json!("nobody");
    let output = run_check(&temp, &bad, &["--structure-only"]);
    assert_eq!(output.status.code(), Some(1), "{output:?}");
}

#[test]
fn an_unreadable_or_malformed_input_exits_2() {
    let temp = Temp::new("bad");
    let output = run_check(&temp, &ok_registry(), &["--bogus"]);
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    fs::write(temp.0.join("registry.json"), "{").unwrap();
    let mut args = write_workspace(&temp, &ok_registry());
    fs::write(temp.0.join("registry.json"), "{").unwrap();
    args.push("--structure-only".into());
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    let output = xtask(&refs);
    assert_eq!(output.status.code(), Some(2), "{output:?}");
}

// ------------------------------------------------------------ evidence must show the tests ran

const LOG_BOTH: &str = "     Running tests/alpha.rs (target/debug/deps/alpha-1)\n\nrunning 3 tests\ntest nested::inner ... ok\ntest replaces_one_and_two ... ok\ntest async_case ... ok\n\ntest result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n";

fn gate_record(id: &str, verdict: &str, log: &str) -> Value {
    json!({"id": id, "kind": "gate", "lane": "T1", "name": "g", "verdict": verdict, "log": log,
           "exit": i32::from(verdict != "PASS"), "total": {"passed": 3, "failed": 0, "ignored": 0}})
}
fn proofs_of(temp: &Temp, records: &[Value], logs: &[(&str, &str)]) -> Proofs {
    for (name, body) in logs {
        fs::write(temp.0.join(name), body).unwrap();
    }
    let text: Vec<String> = records.iter().map(Value::to_string).collect();
    Proofs::from_ledger(&text.join("\n"), &temp.0).unwrap()
}
fn check_proved(registry: &Value, proofs: &Proofs) -> Report {
    let baseline = fixture_baseline();
    let obligations = obligations();
    let rust = rust_tests();
    let ids: BTreeSet<String> = ["EV-20261028-0004".to_string()].into();
    retire::check(
        &registry.to_string(),
        &Context {
            baseline: &baseline,
            obligations: &obligations,
            rust_tests: &rust,
            evidence_ids: Some(&ids),
            test_proofs: Some(proofs),
        },
    )
    .unwrap()
}

#[test]
fn a_mapped_row_is_resolved_only_when_a_cited_pass_shows_each_new_test_ok() {
    let temp = Temp::new("proof-ok");
    let proofs = proofs_of(
        &temp,
        &[gate_record("EV-20261028-0004", "PASS", "alpha.log")],
        &[("alpha.log", LOG_BOTH)],
    );
    let report = check_proved(&ok_registry(), &proofs);
    assert!(report.ok(), "{:?}", report.findings);
    assert_eq!(report.resolved, 5);
}

#[test]
fn a_test_the_log_does_not_show_leaves_its_row_unproved_and_unresolved() {
    let temp = Temp::new("proof-missing");
    let log = LOG_BOTH.replace("test nested::inner ... ok\n", "");
    let proofs = proofs_of(
        &temp,
        &[gate_record("EV-20261028-0004", "PASS", "alpha.log")],
        &[("alpha.log", &log)],
    );
    let report = check_proved(&ok_registry(), &proofs);
    assert_eq!(
        report.codes(),
        vec!["new_test_not_proved"],
        "{:?}",
        report.findings
    );
    assert!(
        report.findings[0].detail.contains("nested::inner"),
        "{:?}",
        report.findings
    );
    assert!(
        report.unresolved.contains("py:test_beta.py::test_four"),
        "the row with a finding resolves nothing: {:?}",
        report.unresolved
    );
}

#[test]
fn failed_ignored_and_other_target_lines_are_not_proof() {
    let temp = Temp::new("proof-lines");
    for log in [
        LOG_BOTH.replace("nested::inner ... ok", "nested::inner ... FAILED"),
        LOG_BOTH.replace("nested::inner ... ok", "nested::inner ... ignored"),
        LOG_BOTH.replace("Running tests/alpha.rs", "Running tests/other.rs"),
        LOG_BOTH.replace("test nested::inner", "test nested::inner_two"),
    ] {
        let proofs = proofs_of(
            &temp,
            &[gate_record("EV-20261028-0004", "PASS", "alpha.log")],
            &[("alpha.log", &log)],
        );
        let report = check_proved(&ok_registry(), &proofs);
        assert!(
            report.codes().contains(&"new_test_not_proved"),
            "{log}: {:?}",
            report.findings
        );
    }
}

#[test]
fn only_a_pass_gate_record_with_a_readable_log_can_prove_a_test() {
    let temp = Temp::new("proof-records");
    let fail = gate_record("EV-20261028-0004", "FAIL", "alpha.log");
    let mut receipt = gate_record("EV-20261028-0004", "PASS", "alpha.log");
    receipt["kind"] = json!("device");
    let no_log = gate_record("EV-20261028-0004", "PASS", "absent.log");
    for record in [fail, receipt, no_log] {
        let proofs = proofs_of(&temp, &[record], &[("alpha.log", LOG_BOTH)]);
        let report = check_proved(&ok_registry(), &proofs);
        assert!(
            report.codes().contains(&"new_test_not_proved"),
            "{:?}",
            report.findings
        );
    }
}

#[test]
fn a_unit_test_in_a_source_file_is_proved_by_its_module_path_in_any_target() {
    let temp = Temp::new("proof-unit");
    let sources = [source(
        "crates/x/src/table.rs",
        "#[cfg(test)]\nmod tests {\n    #[test]\n    fn rows() {}\n}\n",
    )];
    let rust = retire::discover_rust_tests(&sources).unwrap();
    assert!(rust.contains("rs:crates/x/src/table.rs::tests::rows"));
    let log = "     Running unittests src/lib.rs (t)\ntest table::tests::rows ... ok\n";
    let proofs = proofs_of(
        &temp,
        &[gate_record("EV-20261028-0004", "PASS", "u.log")],
        &[("u.log", log)],
    );
    assert!(proofs.shows("EV-20261028-0004", "rs:crates/x/src/table.rs::tests::rows"));
    assert!(!proofs.shows("EV-20261028-0004", "rs:crates/x/src/table.rs::tests::other"));
    assert!(!proofs.shows("EV-20261028-0099", "rs:crates/x/src/table.rs::tests::rows"));
}

#[test]
fn the_command_with_a_ledger_demands_the_cited_logs_show_the_tests() {
    let temp = Temp::new("proof-cli");
    let log_dir = temp.0.join("reg");
    fs::create_dir_all(&log_dir).unwrap();
    let ledger = log_dir.join("evidence.jsonl");
    fs::write(
        log_dir.join("alpha.log"),
        LOG_BOTH.replace("test replaces_one_and_two ... ok\n", ""),
    )
    .unwrap();
    fs::write(
        &ledger,
        format!("{}\n", gate_record("EV-20261028-0004", "PASS", "alpha.log")),
    )
    .unwrap();
    let output = run_check(
        &temp,
        &ok_registry(),
        &["--ledger", ledger.to_str().unwrap()],
    );
    assert_eq!(output.status.code(), Some(1), "{output:?}");
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(text.contains("new_test_not_proved"), "{text}");
    assert!(text.contains("replaces_one_and_two"), "{text}");
    fs::write(log_dir.join("alpha.log"), LOG_BOTH).unwrap();
    let output = run_check(
        &temp,
        &ok_registry(),
        &["--ledger", ledger.to_str().unwrap()],
    );
    assert_eq!(output.status.code(), Some(0), "{output:?}");
}
