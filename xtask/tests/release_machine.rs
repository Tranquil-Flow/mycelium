// SPDX-License-Identifier: AGPL-3.0-only
//! The release state machine over fixture evidence ledgers (CL-L12-01, CL-L12-07).
//!
//! Fixtures: `fixtures/release/` (candidate bytes, manifest, profile, claims, five ledgers, logs).
//! Digests in the fixtures are re-derived here with `/usr/bin/shasum`, not with the code under test.
use serde_json::{Value, json};
use std::{
    fs,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::{AtomicUsize, Ordering},
};
use xtask::release::{
    self, CellResult, Inputs, Manifest, Outcome, RunOptions, State, Status, bundle_digest,
};

static NEXT: AtomicUsize = AtomicUsize::new(0);
const SOURCE: &str = "5ae5c0de5ae5c0de5ae5c0de5ae5c0de5ae5c0de";

fn fixtures() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("fixtures/release")
}
fn shasum(path: &Path) -> String {
    let out = Command::new("/usr/bin/shasum")
        .args(["-a", "256"])
        .arg(path)
        .output()
        .unwrap();
    assert!(out.status.success(), "{out:?}");
    String::from_utf8(out.stdout)
        .unwrap()
        .split_whitespace()
        .next()
        .unwrap()
        .to_string()
}
fn shasum_stdin(bytes: &[u8]) -> String {
    let mut child = Command::new("/usr/bin/shasum")
        .args(["-a", "256"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(bytes).unwrap();
    let out = child.wait_with_output().unwrap();
    String::from_utf8(out.stdout)
        .unwrap()
        .split_whitespace()
        .next()
        .unwrap()
        .to_string()
}
fn copy_tree(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let target = to.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_tree(&entry.path(), &target);
        } else {
            fs::copy(entry.path(), target).unwrap();
        }
    }
}

struct Sandbox {
    root: PathBuf,
}
impl Sandbox {
    fn new(ledger: &str) -> Self {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../target/xtask-release-tests")
            .join(format!(
                "{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
        fs::create_dir_all(root.join("registry")).unwrap();
        copy_tree(&fixtures().join("candidate"), &root.join("candidate"));
        copy_tree(&fixtures().join("logs"), &root.join("registry/logs"));
        for name in ["manifest.json", "profile.json"] {
            fs::copy(fixtures().join(name), root.join(name)).unwrap();
        }
        fs::copy(
            fixtures().join("claims.tsv"),
            root.join("registry/CLAIMS.tsv"),
        )
        .unwrap();
        fs::copy(
            fixtures().join(format!("ledgers/{ledger}.jsonl")),
            root.join("registry/evidence.jsonl"),
        )
        .unwrap();
        Self { root }
    }
    fn inputs(&self) -> Inputs {
        Inputs {
            manifest: self.root.join("manifest.json"),
            profile: self.root.join("profile.json"),
            registry: self.root.join("registry"),
            attempts: self.root.join("attempts"),
        }
    }
    fn ledger_path(&self) -> PathBuf {
        self.root.join("registry/evidence.jsonl")
    }
    fn records(&self) -> Vec<Value> {
        fs::read_to_string(self.ledger_path())
            .unwrap()
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect()
    }
    fn write_records(&self, records: &[Value]) {
        let text: String = records.iter().map(|r| format!("{r}\n")).collect();
        fs::write(self.ledger_path(), text).unwrap();
    }
    fn edit_record(&self, id: &str, edit: impl FnOnce(&mut Value)) {
        let mut records = self.records();
        let record = records.iter_mut().find(|r| r["id"] == id).unwrap();
        edit(record);
        self.write_records(&records);
    }
    fn append(&self, record: Value) {
        let mut records = self.records();
        records.push(record);
        self.write_records(&records);
    }
    fn edit_json(&self, name: &str, edit: impl FnOnce(&mut Value)) {
        let path = self.root.join(name);
        let mut value: Value = serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        edit(&mut value);
        fs::write(path, value.to_string()).unwrap();
    }
    fn evaluate(&self) -> Vec<CellResult> {
        let loaded = release::load(&self.inputs()).unwrap();
        release::evaluate_all(&loaded.env())
    }
    fn run(&self, attempt: &str) -> Result<release::Report, String> {
        release::run(
            &self.inputs(),
            &RunOptions {
                attempt: attempt.into(),
                create: true,
                stop_after: None,
            },
        )
    }
    fn resume(&self, attempt: &str) -> Result<release::Report, String> {
        release::run(
            &self.inputs(),
            &RunOptions {
                attempt: attempt.into(),
                create: false,
                stop_after: None,
            },
        )
    }
    fn state(&self, attempt: &str) -> Value {
        serde_json::from_str(
            &fs::read_to_string(self.root.join("attempts").join(attempt).join("state.json"))
                .unwrap(),
        )
        .unwrap()
    }
    fn attempt_dir(&self, attempt: &str) -> PathBuf {
        self.root.join("attempts").join(attempt)
    }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}

fn cell<'a>(results: &'a [CellResult], id: &str) -> &'a CellResult {
    results.iter().find(|r| r.cell == id).unwrap()
}
fn red(result: &CellResult) -> (&'static str, &[String], &str) {
    match &result.outcome {
        Outcome::Red { code, evs, detail } => (code, evs, detail),
        other => panic!("{} is not red: {other:?}", result.cell),
    }
}
fn code_of(results: &[CellResult], id: &str) -> &'static str {
    red(cell(results, id)).0
}
fn greens(results: &[CellResult]) -> Vec<String> {
    results
        .iter()
        .filter(|r| matches!(r.outcome, Outcome::Green { .. }))
        .map(|r| r.cell.clone())
        .collect()
}
fn status_of(report: &release::Report, state: State) -> Status {
    report.states.iter().find(|(s, _)| *s == state).unwrap().1
}
fn tests_record(sandbox: &Sandbox, id: &str, at: f64, verdict: &str) -> Value {
    let mut record = sandbox
        .records()
        .into_iter()
        .find(|r| r["id"] == "EV-20261028-0004")
        .unwrap();
    record["id"] = json!(id);
    record["at"] = json!(at);
    record["verdict"] = json!(verdict);
    record
}

// ================================================================== the candidate bytes

#[test]
fn bundle_digest_is_the_sha256_of_the_sorted_per_file_listing() {
    let bundle = fixtures().join("candidate/bundle");
    let files = [
        "Contents/Frameworks/libfixture.dylib",
        "Contents/Info.plist",
        "Contents/MacOS/mycelium",
    ];
    let listing: String = files
        .iter()
        .map(|f| {
            let path = bundle.join(f);
            format!(
                "{}  {}  {f}\n",
                shasum(&path),
                fs::metadata(&path).unwrap().len()
            )
        })
        .collect();
    let expected = shasum_stdin(listing.as_bytes());
    assert_eq!(bundle_digest(&bundle).unwrap(), expected);
    let manifest = Manifest::load(&fixtures().join("manifest.json")).unwrap();
    assert_eq!(manifest.candidate.bundle_sha256, expected);
    assert_eq!(
        manifest.candidate.exe_sha256,
        shasum(&bundle.join("Contents/MacOS/mycelium"))
    );
    assert_eq!(manifest.sha256, shasum(&fixtures().join("manifest.json")));
}

#[test]
fn bundle_digest_changes_for_a_byte_a_rename_an_extra_file_and_a_link_and_refuses_links_out_and_specials()
 {
    // Re-pinned (lane REL): an in-bundle relative link was refused before; the real bundle has
    // v0.1.4's signable Metal link (c85059e), so a link inside the bundle is now part of the
    // digest (never followed) and only links out of the bundle and other specials are refused.
    let sandbox = Sandbox::new("accept");
    let bundle = sandbox.root.join("candidate/bundle");
    let original = bundle_digest(&bundle).unwrap();
    let plist = bundle.join("Contents/Info.plist");
    let bytes = fs::read(&plist).unwrap();
    let mut flipped = bytes.clone();
    flipped[0] ^= 1;
    fs::write(&plist, &flipped).unwrap();
    assert_ne!(bundle_digest(&bundle).unwrap(), original, "byte flip");
    fs::write(&plist, &bytes).unwrap();
    fs::rename(&plist, bundle.join("Contents/Info2.plist")).unwrap();
    assert_ne!(bundle_digest(&bundle).unwrap(), original, "rename");
    fs::rename(bundle.join("Contents/Info2.plist"), &plist).unwrap();
    assert_eq!(bundle_digest(&bundle).unwrap(), original);
    fs::write(bundle.join("extra"), b"x").unwrap();
    assert_ne!(bundle_digest(&bundle).unwrap(), original, "extra file");
    fs::remove_file(bundle.join("extra")).unwrap();
    let link = bundle.join("Contents/link");
    std::os::unix::fs::symlink("Info.plist", &link).unwrap();
    let linked = bundle_digest(&bundle).unwrap();
    assert_ne!(linked, original, "a link is part of the bytes");
    fs::remove_file(&link).unwrap();
    std::os::unix::fs::symlink("../../outside", &link).unwrap();
    let error = bundle_digest(&bundle).unwrap_err();
    assert!(error.contains("bundle_link_outside"), "{error}");
    fs::remove_file(&link).unwrap();
    let made = std::process::Command::new("/usr/bin/mkfifo")
        .arg(&link)
        .status()
        .unwrap();
    assert!(made.success());
    let error = bundle_digest(&bundle).unwrap_err();
    assert!(error.contains("bundle_special_file"), "{error}");
}

#[test]
fn manifest_refuses_malformed_candidates() {
    let text = fs::read_to_string(fixtures().join("manifest.json")).unwrap();
    let base: Value = serde_json::from_str(&text).unwrap();
    let parse = |edit: &dyn Fn(&mut Value)| {
        let mut value = base.clone();
        edit(&mut value);
        Manifest::parse(&value.to_string(), &fixtures()).unwrap_err()
    };
    type Edit = Box<dyn Fn(&mut Value)>;
    let cases: [(&str, Edit); 8] = [
        ("manifest_invalid", Box::new(|v| v["schema"] = json!(2))),
        ("manifest_invalid", Box::new(|v| v["extra"] = json!(1))),
        (
            "candidate_digest_invalid",
            Box::new(|v| v["candidate"]["exe_sha256"] = json!("abc")),
        ),
        (
            "candidate_digest_invalid",
            Box::new(|v| v["candidate"]["bundle_sha256"] = json!("A".repeat(64))),
        ),
        (
            "candidate_source_invalid",
            Box::new(|v| v["candidate"]["source_commit"] = json!("5ae5c0de")),
        ),
        (
            "manifest_invalid",
            Box::new(|v| v["frozen_at"] = json!(-1.0)),
        ),
        (
            "manifest_invalid",
            Box::new(|v| {
                v["candidate"].as_object_mut().unwrap().remove("exe_sha256");
            }),
        ),
        (
            "manifest_path_invalid",
            Box::new(|v| v["exe_path"] = json!("")),
        ),
    ];
    for (code, edit) in cases {
        let error = parse(&*edit);
        assert!(error.contains(code), "{code}: {error}");
    }
    assert!(Manifest::parse(&text, &fixtures()).is_ok());
}

#[test]
fn verify_bytes_checks_the_exe_and_the_bundle_against_the_frozen_digests() {
    let sandbox = Sandbox::new("accept");
    let manifest = Manifest::load(&sandbox.root.join("manifest.json")).unwrap();
    manifest.verify_bytes().unwrap();
    let exe = sandbox
        .root
        .join("candidate/bundle/Contents/MacOS/mycelium");
    let original = fs::read(&exe).unwrap();
    let mut flipped = original.clone();
    flipped[3] ^= 1;
    fs::write(&exe, &flipped).unwrap();
    let error = manifest.verify_bytes().unwrap_err();
    assert!(error.contains("package_exe_mismatch"), "{error}");
    fs::write(&exe, &original).unwrap();
    fs::write(sandbox.root.join("candidate/bundle/stray"), b"x").unwrap();
    let error = manifest.verify_bytes().unwrap_err();
    assert!(error.contains("package_bundle_mismatch"), "{error}");
    fs::remove_file(sandbox.root.join("candidate/bundle/stray")).unwrap();
    fs::remove_file(&exe).unwrap();
    let error = manifest.verify_bytes().unwrap_err();
    assert!(error.contains("package_exe_unreadable"), "{error}");
}

// ================================================================== records

#[test]
fn a_ledger_with_a_malformed_line_or_a_repeated_id_is_refused() {
    let good = fs::read_to_string(fixtures().join("ledgers/accept.jsonl")).unwrap();
    assert_eq!(release::Ledger::parse(&good).unwrap().records.len(), 7);
    let error = release::Ledger::parse(&format!("{good}{{not json\n")).unwrap_err();
    assert!(error.contains("ledger_line_invalid"), "{error}");
    assert!(error.contains(":8"), "names the line: {error}");
    let first = good.lines().next().unwrap();
    let error = release::Ledger::parse(&format!("{good}{first}\n")).unwrap_err();
    assert!(error.contains("ledger_duplicate_id"), "{error}");
    let error = release::Ledger::parse("[1]\n").unwrap_err();
    assert!(error.contains("ledger_line_invalid"), "{error}");
    let error = release::Ledger::parse("{\"kind\":\"gate\"}\n").unwrap_err();
    assert!(error.contains("ledger_line_invalid"), "{error}");
    assert_eq!(
        release::Ledger::parse(&good).unwrap().sha256,
        shasum(&fixtures().join("ledgers/accept.jsonl"))
    );
}

#[test]
fn claims_parse_status_and_criticality_and_refuse_an_unknown_status() {
    let text = fs::read_to_string(fixtures().join("claims.tsv")).unwrap();
    let claims = release::Claims::parse(&text).unwrap();
    assert_eq!(claims.rows.len(), 8);
    let claim = claims.get("CL-X-03").unwrap();
    assert_eq!((claim.status.as_str(), claim.critical), ("open", true));
    assert!(!claims.get("CL-X-07").unwrap().critical);
    let error = release::Claims::parse(&text.replace("\topen\t", "\tdone\t")).unwrap_err();
    assert!(error.contains("claims_status_invalid"), "{error}");
    let error = release::Claims::parse("id\tlane\n").unwrap_err();
    assert!(error.contains("claims_header_invalid"), "{error}");
    let doubled = format!("{text}{}\n", text.lines().nth(1).unwrap());
    let error = release::Claims::parse(&doubled).unwrap_err();
    assert!(error.contains("claims_duplicate_id"), "{error}");
}

fn claims() -> release::Claims {
    release::Claims::parse(&fs::read_to_string(fixtures().join("claims.tsv")).unwrap()).unwrap()
}
fn profile_error(edit: impl FnOnce(&mut Value)) -> String {
    let mut value: Value =
        serde_json::from_str(&fs::read_to_string(fixtures().join("profile.json")).unwrap())
            .unwrap();
    edit(&mut value);
    release::Profile::parse(&value.to_string(), &claims()).unwrap_err()
}

#[test]
fn the_profile_cannot_leave_a_mandatory_or_critical_claim_without_a_cell() {
    let text = fs::read_to_string(fixtures().join("profile.json")).unwrap();
    let profile = release::Profile::parse(&text, &claims()).unwrap();
    assert_eq!(
        (profile.cells.len(), profile.mandatory_claims.len()),
        (7, 6)
    );
    let error = profile_error(|v| {
        v["cells"]
            .as_array_mut()
            .unwrap()
            .retain(|c| c["claim"] != "CL-X-04");
    });
    assert!(error.contains("mandatory_claim_without_cell"), "{error}");
    assert!(error.contains("CL-X-04"), "{error}");
    let error = profile_error(|v| {
        v["mandatory_claims"]
            .as_array_mut()
            .unwrap()
            .retain(|c| c != "CL-X-06");
    });
    assert!(error.contains("critical_claim_not_mandatory"), "{error}");
    assert!(error.contains("CL-X-06"), "{error}");
    let error = profile_error(|v| v["require_critical_lanes"] = json!(["X", "Y"]));
    assert!(error.contains("critical_claim_not_mandatory"), "{error}");
    assert!(error.contains("CL-Y-01"), "{error}");
}

#[test]
fn the_profile_refuses_unknown_claims_duplicates_and_a_test_cell_that_allows_zero_tests() {
    let error = profile_error(|v| v["cells"][0]["claim"] = json!("CL-Z-99"));
    assert!(error.contains("profile_claim_unknown"), "{error}");
    let error = profile_error(|v| v["cells"][1]["id"] = json!("fmt"));
    assert!(error.contains("profile_duplicate_cell"), "{error}");
    let error = profile_error(|v| v["mandatory_claims"][1] = json!("CL-X-01"));
    assert!(error.contains("profile_duplicate_claim"), "{error}");
    let error = profile_error(|v| v["cells"][3]["verify"]["min_passed"] = json!(0));
    assert!(error.contains("profile_tests_cell_allows_zero"), "{error}");
    let error = profile_error(|v| {
        v["cells"][3]["verify"]
            .as_object_mut()
            .unwrap()
            .remove("min_passed");
    });
    assert!(error.contains("profile_tests_cell_allows_zero"), "{error}");
    let error = profile_error(|v| v["cells"][3]["state"] = json!("publish"));
    assert!(error.contains("profile_invalid"), "{error}");
    let error = profile_error(|v| v["cells"][3]["state"] = json!("set-aside"));
    assert!(error.contains("profile_cell_state_invalid"), "{error}");
    let error = profile_error(|v| v["cells"][3]["extra"] = json!(1));
    assert!(error.contains("profile_invalid"), "{error}");
    let error = profile_error(|v| v["cells"][0]["verify"]["argv_has"] = json!([]));
    assert!(error.contains("profile_verify_unselective"), "{error}");
}

// ================================================================== cell verdicts

#[test]
fn the_accept_ledger_makes_every_cell_green_and_cites_its_evidence_id() {
    let results = Sandbox::new("accept").evaluate();
    let expected = [
        ("fmt", "EV-20261028-0001"),
        ("lint", "EV-20261028-0002"),
        ("build", "EV-20261028-0003"),
        ("tests", "EV-20261028-0004"),
        ("stranger-install", "EV-20261028-0005"),
        ("inventory", "EV-20261028-0006"),
        ("relocation", "EV-20261028-0007"),
    ];
    assert_eq!(results.len(), expected.len());
    for (id, ev) in expected {
        match &cell(&results, id).outcome {
            Outcome::Green { ev: got, .. } => assert_eq!(got, ev, "{id}"),
            other => panic!("{id}: {other:?}"),
        }
    }
    match &cell(&results, "tests").outcome {
        Outcome::Green {
            passed, log_sha256, ..
        } => {
            assert_eq!(*passed, 5);
            assert_eq!(
                log_sha256.as_deref(),
                Some(shasum(&fixtures().join("logs/tests-ok.log")).as_str())
            );
        }
        other => panic!("{other:?}"),
    }
}

#[test]
fn a_missing_cell_is_red_and_only_that_cell() {
    let results = Sandbox::new("missing-cell").evaluate();
    let (code, evs, detail) = red(cell(&results, "stranger-install"));
    assert_eq!(code, "cell_missing");
    assert!(evs.is_empty());
    assert!(
        detail.contains("device-harness stranger"),
        "the hint names the job: {detail}"
    );
    assert_eq!(greens(&results).len(), 6);
}

#[test]
fn evidence_bound_to_other_exe_bytes_is_not_evidence_for_this_candidate() {
    let results = Sandbox::new("wrong-digest").evaluate();
    let (code, evs, detail) = red(cell(&results, "tests"));
    assert_eq!(code, "cell_wrong_digest");
    assert_eq!(evs, ["EV-20261028-0004"]);
    assert!(
        detail.contains("exe_sha256"),
        "names the field that differs: {detail}"
    );
    assert!(!detail.contains("bundle_sha256"), "{detail}");
    assert_eq!(greens(&results).len(), 6);
}

#[test]
fn evidence_bound_to_another_bundle_is_wrong_digest_and_names_only_the_bundle() {
    let sandbox = Sandbox::new("accept");
    sandbox.edit_record("EV-20261028-0004", |r| {
        r["candidate"]["bundle_sha256"] = json!("9".repeat(64));
    });
    let results = sandbox.evaluate();
    let (code, _, detail) = red(cell(&results, "tests"));
    assert_eq!(code, "cell_wrong_digest");
    assert!(detail.contains("bundle_sha256"), "{detail}");
    assert!(!detail.contains("exe_sha256"), "{detail}");
    let commit = Sandbox::new("accept");
    commit.edit_record("EV-20261028-0004", |r| {
        r["candidate"]["source_commit"] = json!("9".repeat(40));
    });
    assert_eq!(code_of(&commit.evaluate(), "tests"), "cell_wrong_digest");
}

#[test]
fn a_bytes_cell_with_a_record_that_carries_no_binding_is_unbound() {
    let sandbox = Sandbox::new("accept");
    sandbox.edit_record("EV-20261028-0004", |r| {
        r.as_object_mut().unwrap().remove("candidate");
    });
    assert_eq!(code_of(&sandbox.evaluate(), "tests"), "cell_unbound");
}

#[test]
fn a_record_older_than_the_candidate_freeze_is_stale() {
    let results = Sandbox::new("stale").evaluate();
    let (code, evs, _) = red(cell(&results, "tests"));
    assert_eq!(code, "cell_stale");
    assert_eq!(evs, ["EV-20261028-0004"]);
}

#[test]
fn a_zero_test_record_is_never_green() {
    let results = Sandbox::new("zero-test").evaluate();
    assert_eq!(code_of(&results, "tests"), "cell_zero_tests");
}

#[test]
fn a_record_that_lies_pass_over_zero_tests_is_still_zero_tests() {
    let sandbox = Sandbox::new("accept");
    sandbox.edit_record("EV-20261028-0004", |r| {
        r["log"] = json!("logs/tests-zero.log");
        r["targets"] = json!([{"failed": 0, "ignored": 0, "ok": true, "passed": 0, "target": "tests/a.rs (a)"}]);
        r["total"] = json!({"failed": 0, "ignored": 0, "passed": 0});
    });
    assert_eq!(code_of(&sandbox.evaluate(), "tests"), "cell_zero_tests");
}

#[test]
fn fewer_passed_tests_than_the_cell_requires_is_red() {
    let sandbox = Sandbox::new("accept");
    sandbox.edit_json("profile.json", |p| {
        p["cells"][3]["verify"]["min_passed"] = json!(6)
    });
    assert_eq!(code_of(&sandbox.evaluate(), "tests"), "cell_too_few_tests");
}

#[test]
fn a_required_target_that_did_not_run_is_red() {
    let sandbox = Sandbox::new("accept");
    sandbox.edit_json("profile.json", |p| {
        p["cells"][3]["verify"]["required_targets"] = json!(["unittests", "tests/nope.rs"]);
    });
    let results = sandbox.evaluate();
    let (code, _, detail) = red(cell(&results, "tests"));
    assert_eq!(code, "cell_target_missing");
    assert!(detail.contains("tests/nope.rs"), "{detail}");
}

#[test]
fn a_target_that_ran_zero_tests_does_not_satisfy_a_required_target() {
    let sandbox = Sandbox::new("accept");
    sandbox.edit_json("profile.json", |p| {
        p["cells"][3]["verify"]["min_passed"] = json!(2)
    });
    sandbox.edit_record("EV-20261028-0004", |r| {
        r["targets"][1]["passed"] = json!(0);
        r["total"]["passed"] = json!(2);
    });
    // The record alone (before the log is read) must already refuse the required target.
    let loaded = release::load(&sandbox.inputs()).unwrap();
    let mut env = loaded.env();
    let missing_log = sandbox.root.join("no-such-registry");
    env.log_base = &missing_log;
    let result = release::evaluate_cell(&env, &loaded.profile.cells[3]);
    assert_eq!(red(&result).0, "cell_target_missing");
}

#[test]
fn the_latest_matching_record_decides_a_late_failure_beats_an_earlier_pass() {
    let sandbox = Sandbox::new("accept");
    let mut failing = tests_record(&sandbox, "EV-20261028-0008", 1793200500.0, "FAIL");
    failing["exit"] = json!(101);
    failing["failed_tests"] = json!(["t1"]);
    sandbox.append(failing);
    let results = sandbox.evaluate();
    let (code, evs, _) = red(cell(&results, "tests"));
    assert_eq!(code, "cell_not_green");
    assert_eq!(evs, ["EV-20261028-0008"]);
}

#[test]
fn a_green_rerun_after_an_earlier_failure_is_green_and_cites_the_rerun() {
    let sandbox = Sandbox::new("accept");
    let rerun = tests_record(&sandbox, "EV-20261028-0008", 1793200500.0, "PASS");
    sandbox.edit_record("EV-20261028-0004", |r| {
        r["verdict"] = json!("FAIL");
        r["exit"] = json!(101);
        r["failed_tests"] = json!(["t1"]);
    });
    sandbox.append(rerun);
    match &cell(&sandbox.evaluate(), "tests").outcome {
        Outcome::Green { ev, .. } => assert_eq!(ev, "EV-20261028-0008"),
        other => panic!("{other:?}"),
    }
}

#[test]
fn internally_inconsistent_records_are_red() {
    let totals = Sandbox::new("accept");
    totals.edit_record("EV-20261028-0004", |r| r["total"]["passed"] = json!(6));
    assert_eq!(code_of(&totals.evaluate(), "tests"), "record_inconsistent");
    let pass_with_failures = Sandbox::new("accept");
    pass_with_failures.edit_record("EV-20261028-0004", |r| {
        r["failed_tests"] = json!(["t1"]);
    });
    assert_eq!(
        code_of(&pass_with_failures.evaluate(), "tests"),
        "record_inconsistent"
    );
    let pass_with_exit = Sandbox::new("accept");
    pass_with_exit.edit_record("EV-20261028-0001", |r| r["exit"] = json!(1));
    assert_eq!(
        code_of(&pass_with_exit.evaluate(), "fmt"),
        "record_inconsistent"
    );
    let pass_with_red_target = Sandbox::new("accept");
    pass_with_red_target.edit_record("EV-20261028-0004", |r| {
        r["targets"][0]["ok"] = json!(false);
    });
    assert_eq!(
        code_of(&pass_with_red_target.evaluate(), "tests"),
        "record_inconsistent"
    );
}

#[test]
fn the_log_is_re_read_a_missing_log_or_one_that_disagrees_is_red() {
    let missing = Sandbox::new("accept");
    fs::remove_file(missing.root.join("registry/logs/tests-ok.log")).unwrap();
    assert_eq!(code_of(&missing.evaluate(), "tests"), "log_missing");
    let disagree = Sandbox::new("accept");
    disagree.edit_record("EV-20261028-0004", |r| {
        r["targets"][1]["passed"] = json!(4);
        r["total"]["passed"] = json!(6);
    });
    let results = disagree.evaluate();
    let (code, _, detail) = red(cell(&results, "tests"));
    assert_eq!(code, "log_disagrees");
    assert!(detail.contains("log has 5 passed"), "{detail}");
    let wrong_verdict = Sandbox::new("accept");
    fs::write(
        wrong_verdict.root.join("registry/logs/check.log"),
        "     Running tests/a.rs (/t/deps/a-0123456789abcdef)\n\nrunning 1 test\ntest x ... FAILED\n\ntest result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n",
    )
    .unwrap();
    assert_eq!(code_of(&wrong_verdict.evaluate(), "fmt"), "log_disagrees");
}

#[test]
fn a_source_cell_needs_the_candidate_source_commit() {
    let sandbox = Sandbox::new("accept");
    sandbox.edit_record("EV-20261028-0001", |r| r["commit"] = json!("0".repeat(40)));
    let results = sandbox.evaluate();
    let (code, evs, detail) = red(cell(&results, "fmt"));
    assert_eq!(code, "cell_wrong_commit");
    assert_eq!(evs, ["EV-20261028-0001"]);
    assert!(detail.contains(SOURCE), "{detail}");
}

#[test]
fn a_cell_matches_only_its_kind_name_and_forbidden_arguments() {
    let sandbox = Sandbox::new("accept");
    sandbox.edit_record("EV-20261028-0005", |r| r["kind"] = json!("gate"));
    assert_eq!(
        code_of(&sandbox.evaluate(), "stranger-install"),
        "cell_missing"
    );
    let named = Sandbox::new("accept");
    named.edit_record("EV-20261028-0005", |r| r["name"] = json!("something-else"));
    assert_eq!(
        code_of(&named.evaluate(), "stranger-install"),
        "cell_missing"
    );
    let forbidden = Sandbox::new("accept");
    forbidden.edit_json("profile.json", |p| {
        p["cells"][0]["verify"]["argv_forbids"] = json!(["--check"]);
    });
    assert_eq!(code_of(&forbidden.evaluate(), "fmt"), "cell_missing");
}

#[test]
fn a_receipt_that_is_not_pass_is_not_green() {
    let sandbox = Sandbox::new("accept");
    sandbox.edit_record("EV-20261028-0005", |r| r["verdict"] = json!("FAIL"));
    assert_eq!(
        code_of(&sandbox.evaluate(), "stranger-install"),
        "cell_not_green"
    );
}

// ================================================================== the machine

const ALL_DONE: [State; 8] = [
    State::Preflight,
    State::Package,
    State::Cert,
    State::Device,
    State::Inventory,
    State::SetAside,
    State::Proof,
    State::Promotion,
];

#[test]
fn the_accept_ledger_reaches_accepted_with_a_receipt_citing_every_evidence_id() {
    let sandbox = Sandbox::new("accept");
    let report = sandbox.run("A1").unwrap();
    assert_eq!(report.outcome, "accepted");
    assert!(report.blocks.is_empty(), "{:?}", report.blocks);
    for state in ALL_DONE {
        assert_eq!(status_of(&report, state), Status::Done, "{state:?}");
    }
    let receipt_path = report.receipt.unwrap();
    assert_eq!(
        receipt_path,
        sandbox.attempt_dir("A1").join("promotion-receipt.json")
    );
    let receipt: Value = serde_json::from_str(&fs::read_to_string(&receipt_path).unwrap()).unwrap();
    assert_eq!(receipt["decision"], "ACCEPTED");
    assert_eq!(
        receipt["publication"],
        "NOT_PERFORMED: owner token required"
    );
    assert_eq!(receipt["candidate"]["source_commit"], SOURCE);
    assert_eq!(
        receipt["candidate"]["exe_sha256"],
        shasum(
            &sandbox
                .root
                .join("candidate/bundle/Contents/MacOS/mycelium")
        )
    );
    assert_eq!(
        receipt["manifest_sha256"],
        shasum(&sandbox.root.join("manifest.json"))
    );
    assert_eq!(receipt["ledger_sha256"], shasum(&sandbox.ledger_path()));
    assert_eq!(
        receipt["claims_sha256"],
        shasum(&sandbox.root.join("registry/CLAIMS.tsv"))
    );
    let mut cited: Vec<String> = receipt["claims"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|c| c["cells"].as_array().unwrap().iter())
        .map(|c| c["ev"].as_str().unwrap().to_string())
        .collect();
    cited.sort();
    let expected: Vec<String> = (1..=7).map(|n| format!("EV-20261028-{n:04}")).collect();
    assert_eq!(cited, expected);
    let claims = receipt["claims"].as_array().unwrap();
    assert_eq!(claims.len(), 6);
    let tests = claims.iter().find(|c| c["claim"] == "CL-X-03").unwrap();
    assert_eq!(
        tests["status_in_table"], "open",
        "table status is quoted, not trusted"
    );
    assert_eq!(
        tests["cells"][0]["log_sha256"],
        shasum(&fixtures().join("logs/tests-ok.log"))
    );
}

#[test]
fn a_missing_cell_blocks_that_state_and_everything_after_it_and_writes_no_receipt() {
    let sandbox = Sandbox::new("missing-cell");
    let report = sandbox.run("A1").unwrap();
    assert_eq!(report.outcome, "blocked");
    assert_eq!(status_of(&report, State::Preflight), Status::Done);
    assert_eq!(status_of(&report, State::Package), Status::Done);
    assert_eq!(status_of(&report, State::Cert), Status::Done);
    assert_eq!(status_of(&report, State::Device), Status::Blocked);
    for state in [
        State::Inventory,
        State::SetAside,
        State::Proof,
        State::Promotion,
    ] {
        assert_eq!(status_of(&report, state), Status::Pending, "{state:?}");
    }
    assert_eq!(report.blocks.len(), 1);
    let block = &report.blocks[0];
    assert_eq!(
        (
            block.state,
            block.code.as_str(),
            block.cell.as_deref(),
            block.claim.as_deref()
        ),
        (
            State::Device,
            "cell_missing",
            Some("stranger-install"),
            Some("CL-X-04")
        )
    );
    assert!(report.receipt.is_none());
    assert!(
        !sandbox
            .attempt_dir("A1")
            .join("promotion-receipt.json")
            .exists()
    );
    assert!(!sandbox.attempt_dir("A1").join("set-aside.json").exists());
}

#[test]
fn a_blocked_branch_does_not_stop_its_sibling_branch_from_being_evaluated() {
    let sandbox = Sandbox::new("accept");
    let kept: Vec<Value> = sandbox
        .records()
        .into_iter()
        .filter(|r| r["id"] != "EV-20261028-0004")
        .collect();
    sandbox.write_records(&kept);
    let report = sandbox.run("A1").unwrap();
    assert_eq!(status_of(&report, State::Cert), Status::Blocked);
    assert_eq!(status_of(&report, State::Device), Status::Done);
    assert_eq!(status_of(&report, State::Inventory), Status::Pending);
}

#[test]
fn every_failing_ledger_variant_blocks_in_cert_and_never_accepts() {
    for ledger in ["wrong-digest", "stale", "zero-test"] {
        let sandbox = Sandbox::new(ledger);
        let report = sandbox.run("A1").unwrap();
        assert_eq!(report.outcome, "blocked", "{ledger}");
        assert_eq!(status_of(&report, State::Cert), Status::Blocked, "{ledger}");
        assert_eq!(
            status_of(&report, State::Promotion),
            Status::Pending,
            "{ledger}"
        );
        assert!(report.receipt.is_none(), "{ledger}");
    }
}

#[test]
fn table_prose_never_substitutes_for_a_record_and_a_blocked_claim_blocks_promotion() {
    // The claim says `met`, the record is missing: still blocked.
    let sandbox = Sandbox::new("missing-cell");
    let claims = sandbox.root.join("registry/CLAIMS.tsv");
    let text = fs::read_to_string(&claims)
        .unwrap()
        .replace("\tpartial\t", "\tmet\t");
    fs::write(&claims, text).unwrap();
    assert_eq!(sandbox.run("A1").unwrap().outcome, "blocked");
    // Every record is green but the claim table says blocked: promotion refuses, naming blocked_by.
    let blocked = Sandbox::new("accept");
    let claims = blocked.root.join("registry/CLAIMS.tsv");
    let text = fs::read_to_string(&claims)
        .unwrap()
        .replace("tests gate\topen\t-\t-", "tests gate\tblocked\t-\tOQ-001");
    fs::write(&claims, text).unwrap();
    let report = blocked.run("A1").unwrap();
    assert_eq!(report.outcome, "blocked");
    assert_eq!(status_of(&report, State::Proof), Status::Done);
    assert_eq!(status_of(&report, State::Promotion), Status::Blocked);
    let block = report
        .blocks
        .iter()
        .find(|b| b.code == "claim_blocked")
        .unwrap();
    assert_eq!(block.claim.as_deref(), Some("CL-X-03"));
    assert!(block.detail.contains("OQ-001"), "{}", block.detail);
    assert!(report.receipt.is_none());
}

#[test]
fn stop_after_leaves_a_state_file_that_a_resume_continues_without_redoing_history() {
    let sandbox = Sandbox::new("accept");
    let first = release::run(
        &sandbox.inputs(),
        &RunOptions {
            attempt: "A1".into(),
            create: true,
            stop_after: Some(State::Package),
        },
    )
    .unwrap();
    assert_eq!(first.outcome, "in-progress");
    assert_eq!(status_of(&first, State::Package), Status::Done);
    assert_eq!(status_of(&first, State::Cert), Status::Pending);
    let before = sandbox.state("A1");
    let preflight_done_at = before["states"]["preflight"]["first_done_revision"].clone();
    assert!(preflight_done_at.is_number(), "{before}");
    let second = sandbox.resume("A1").unwrap();
    assert_eq!(second.outcome, "accepted");
    let after = sandbox.state("A1");
    assert_eq!(
        after["states"]["preflight"]["first_done_revision"],
        preflight_done_at
    );
    assert!(after["revision"].as_u64().unwrap() > before["revision"].as_u64().unwrap());
    assert_eq!(after["outcome"], "accepted");
}

#[test]
fn a_resume_after_the_missing_record_arrives_completes_the_same_attempt() {
    let sandbox = Sandbox::new("missing-cell");
    assert_eq!(sandbox.run("A1").unwrap().outcome, "blocked");
    let blocked_state = sandbox.state("A1");
    assert_eq!(blocked_state["outcome"], "blocked");
    let accept: Vec<Value> = fs::read_to_string(fixtures().join("ledgers/accept.jsonl"))
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect();
    sandbox.write_records(&accept);
    let report = sandbox.resume("A1").unwrap();
    assert_eq!(report.outcome, "accepted");
    let events = fs::read_to_string(sandbox.attempt_dir("A1").join("events.jsonl")).unwrap();
    assert!(
        events.lines().count() >= 8,
        "append-only transition log: {events}"
    );
    let first_line: Value = serde_json::from_str(events.lines().next().unwrap()).unwrap();
    assert_eq!(first_line["attempt"], "A1");
}

#[test]
fn an_attempt_id_is_created_once_and_a_resume_needs_an_existing_attempt() {
    let sandbox = Sandbox::new("accept");
    assert!(
        sandbox
            .resume("A1")
            .unwrap_err()
            .contains("attempt_missing")
    );
    sandbox.run("A1").unwrap();
    assert!(sandbox.run("A1").unwrap_err().contains("attempt_exists"));
    for bad in ["", ".", "..", "a/b", "../x", ".hidden", "a b"] {
        let error = sandbox.run(bad).unwrap_err();
        assert!(error.contains("attempt_id_invalid"), "{bad:?}: {error}");
    }
}

#[test]
fn a_resume_refuses_when_the_candidate_manifest_or_profile_changed() {
    let manifest = Sandbox::new("accept");
    manifest.run("A1").unwrap();
    manifest.edit_json("manifest.json", |m| m["frozen_at"] = json!(1793200001.0));
    assert!(
        manifest
            .resume("A1")
            .unwrap_err()
            .contains("attempt_identity_changed")
    );
    let profile = Sandbox::new("accept");
    profile.run("A1").unwrap();
    profile.edit_json("profile.json", |p| p["name"] = json!("other"));
    assert!(
        profile
            .resume("A1")
            .unwrap_err()
            .contains("attempt_identity_changed")
    );
}

#[test]
fn a_corrupt_state_file_is_refused_never_silently_restarted_and_a_torn_temp_is_ignored() {
    let sandbox = Sandbox::new("accept");
    release::run(
        &sandbox.inputs(),
        &RunOptions {
            attempt: "A1".into(),
            create: true,
            stop_after: Some(State::Preflight),
        },
    )
    .unwrap();
    let dir = sandbox.attempt_dir("A1");
    fs::write(dir.join("state.json.tmp"), "{\"torn\": ").unwrap();
    assert_eq!(sandbox.resume("A1").unwrap().outcome, "accepted");
    let state = fs::read(dir.join("state.json")).unwrap();
    fs::write(dir.join("state.json"), &state[..state.len() / 2]).unwrap();
    assert!(sandbox.resume("A1").unwrap_err().contains("state_corrupt"));
    fs::write(dir.join("state.json"), "{\"schema\":1}").unwrap();
    assert!(sandbox.resume("A1").unwrap_err().contains("state_corrupt"));
}

#[test]
fn two_runs_on_one_attempt_cannot_overlap() {
    let sandbox = Sandbox::new("accept");
    sandbox.run("A1").unwrap();
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(sandbox.attempt_dir("A1").join("lock"))
        .unwrap();
    lock.lock().unwrap();
    let error = sandbox.resume("A1").unwrap_err();
    assert!(error.contains("attempt_locked"), "{error}");
    lock.unlock().unwrap();
    assert_eq!(sandbox.resume("A1").unwrap().outcome, "accepted");
}

#[test]
fn changed_bytes_after_acceptance_block_the_package_state_and_set_the_receipt_aside() {
    let sandbox = Sandbox::new("accept");
    assert_eq!(sandbox.run("A1").unwrap().outcome, "accepted");
    let exe = sandbox
        .root
        .join("candidate/bundle/Contents/MacOS/mycelium");
    let mut bytes = fs::read(&exe).unwrap();
    bytes[0] ^= 1;
    fs::write(&exe, bytes).unwrap();
    let report = sandbox.resume("A1").unwrap();
    assert_eq!(report.outcome, "blocked");
    assert_eq!(status_of(&report, State::Package), Status::Blocked);
    assert_eq!(status_of(&report, State::Promotion), Status::Pending);
    assert!(
        report
            .blocks
            .iter()
            .any(|b| b.code == "package_exe_mismatch")
    );
    let dir = sandbox.attempt_dir("A1");
    assert!(!dir.join("promotion-receipt.json").exists());
    let kept: Vec<String> = fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
        .filter(|n| n.starts_with("promotion-receipt.json.invalidated-"))
        .collect();
    assert_eq!(
        kept.len(),
        1,
        "the old receipt is renamed, never deleted: {kept:?}"
    );
    assert_eq!(sandbox.state("A1")["outcome"], "blocked");
}

#[test]
fn a_failing_record_appended_after_acceptance_invalidates_the_acceptance() {
    let sandbox = Sandbox::new("accept");
    assert_eq!(sandbox.run("A1").unwrap().outcome, "accepted");
    let mut failing = tests_record(&sandbox, "EV-20261028-0008", 1793200500.0, "FAIL");
    failing["exit"] = json!(101);
    failing["failed_tests"] = json!(["t1"]);
    sandbox.append(failing);
    let report = sandbox.resume("A1").unwrap();
    assert_eq!(report.outcome, "blocked");
    assert_eq!(status_of(&report, State::Cert), Status::Blocked);
    assert!(
        !sandbox
            .attempt_dir("A1")
            .join("promotion-receipt.json")
            .exists()
    );
}

#[test]
fn an_unchanged_ledger_makes_a_re_resume_of_an_accepted_attempt_a_no_op_on_the_receipt() {
    let sandbox = Sandbox::new("accept");
    sandbox.run("A1").unwrap();
    let path = sandbox.attempt_dir("A1").join("promotion-receipt.json");
    let before = fs::read(&path).unwrap();
    assert_eq!(sandbox.resume("A1").unwrap().outcome, "accepted");
    assert_eq!(fs::read(&path).unwrap(), before);
}

#[test]
fn a_receipt_that_changes_with_the_ledger_keeps_the_old_one_beside_it() {
    let sandbox = Sandbox::new("accept");
    sandbox.run("A1").unwrap();
    let path = sandbox.attempt_dir("A1").join("promotion-receipt.json");
    let old = fs::read(&path).unwrap();
    // An unrelated record changes the ledger hash the receipt cites; acceptance is unchanged.
    let mut unrelated = sandbox.records()[0].clone();
    unrelated["id"] = json!("EV-20261028-0099");
    unrelated["name"] = json!("unrelated");
    sandbox.append(unrelated);
    assert_eq!(sandbox.resume("A1").unwrap().outcome, "accepted");
    let new = fs::read(&path).unwrap();
    assert_ne!(new, old);
    let kept = fs::read(
        sandbox
            .attempt_dir("A1")
            .join("promotion-receipt.json.superseded-1"),
    )
    .unwrap();
    assert_eq!(kept, old, "the superseded receipt is kept byte for byte");
}

#[test]
fn the_set_aside_seal_is_written_once_and_a_conflicting_seal_blocks() {
    let sandbox = Sandbox::new("accept");
    sandbox.run("A1").unwrap();
    let seal_path = sandbox.attempt_dir("A1").join("set-aside.json");
    let seal: Value = serde_json::from_str(&fs::read_to_string(&seal_path).unwrap()).unwrap();
    assert_eq!(
        seal["manifest_sha256"],
        shasum(&sandbox.root.join("manifest.json"))
    );
    assert_eq!(
        seal["candidate"]["bundle_sha256"],
        bundle_digest(&sandbox.root.join("candidate/bundle")).unwrap()
    );
    fs::write(&seal_path, "{\"manifest_sha256\":\"0\"}").unwrap();
    let report = sandbox.resume("A1").unwrap();
    assert_eq!(status_of(&report, State::SetAside), Status::Blocked);
    assert!(report.blocks.iter().any(|b| b.code == "set_aside_conflict"));
    assert_eq!(report.outcome, "blocked");
}

#[test]
fn a_missing_candidate_file_blocks_the_package_state_with_a_specific_code() {
    let sandbox = Sandbox::new("accept");
    fs::remove_file(
        sandbox
            .root
            .join("candidate/bundle/Contents/MacOS/mycelium"),
    )
    .unwrap();
    let report = sandbox.run("A1").unwrap();
    assert_eq!(status_of(&report, State::Package), Status::Blocked);
    assert!(
        report
            .blocks
            .iter()
            .any(|b| b.code == "package_exe_unreadable"),
        "{:?}",
        report.blocks
    );
    assert_eq!(status_of(&report, State::Cert), Status::Pending);
}

// ================================================================== publication stub

fn token(sandbox: &Sandbox, body: &str) -> PathBuf {
    let path = sandbox.root.join("owner-token.txt");
    fs::write(&path, body).unwrap();
    path
}
fn good_token_body(sandbox: &Sandbox) -> String {
    let manifest = Manifest::load(&sandbox.root.join("manifest.json")).unwrap();
    format!(
        "OWNER-PUBLISH-TOKEN v1\naction=publish\nexe_sha256={}\nbundle_sha256={}\n",
        manifest.candidate.exe_sha256, manifest.candidate.bundle_sha256
    )
}
fn tree(dir: &Path) -> Vec<(String, Vec<u8>)> {
    let mut out = Vec::new();
    for entry in fs::read_dir(dir).unwrap() {
        let entry = entry.unwrap();
        let name = entry.file_name().to_string_lossy().to_string();
        if entry.file_type().unwrap().is_dir() {
            out.extend(tree(&entry.path()));
        } else {
            out.push((name, fs::read(entry.path()).unwrap()));
        }
    }
    out.sort();
    out
}

#[test]
fn publication_refuses_a_candidate_that_is_not_accepted_even_with_a_valid_token() {
    let sandbox = Sandbox::new("missing-cell");
    sandbox.run("A1").unwrap();
    let valid = token(&sandbox, &good_token_body(&sandbox));
    let error = release::publish(&sandbox.inputs(), "A1", Some(&valid)).unwrap_err();
    assert!(error.contains("publish_not_accepted"), "{error}");
    let error = release::publish(&sandbox.inputs(), "ghost", Some(&valid)).unwrap_err();
    assert!(error.contains("attempt_missing"), "{error}");
}

#[test]
fn publication_needs_an_owner_token_that_binds_this_exact_candidate_and_still_never_publishes() {
    let sandbox = Sandbox::new("accept");
    sandbox.run("A1").unwrap();
    let before = tree(&sandbox.attempt_dir("A1"));
    let refuse = |token: Option<&Path>, code: &str| {
        let error = release::publish(&sandbox.inputs(), "A1", token).unwrap_err();
        assert!(error.contains(code), "{code}: {error}");
    };
    refuse(None, "owner_token_missing");
    refuse(
        Some(&sandbox.root.join("absent.txt")),
        "owner_token_unreadable",
    );
    let link = sandbox.root.join("link.txt");
    let real = token(&sandbox, &good_token_body(&sandbox));
    std::os::unix::fs::symlink(&real, &link).unwrap();
    refuse(Some(&link), "owner_token_not_regular");
    refuse(Some(&sandbox.root), "owner_token_not_regular");
    let empty = token(&sandbox, "");
    refuse(Some(&empty), "owner_token_invalid");
    let wrong_action = token(
        &sandbox,
        &good_token_body(&sandbox).replace("publish", "promote"),
    );
    refuse(Some(&wrong_action), "owner_token_invalid");
    let exe = Manifest::load(&sandbox.root.join("manifest.json"))
        .unwrap()
        .candidate
        .exe_sha256;
    let other = token(
        &sandbox,
        &good_token_body(&sandbox).replace(&exe, &"1".repeat(64)),
    );
    refuse(Some(&other), "owner_token_mismatch");
    let huge = token(&sandbox, &"x".repeat(1_000_000));
    refuse(Some(&huge), "owner_token_invalid");
    // A fully valid token is still a refusal: publication is not implemented here.
    let valid = token(&sandbox, &good_token_body(&sandbox));
    refuse(Some(&valid), "publication_not_implemented");
    assert_eq!(
        tree(&sandbox.attempt_dir("A1")),
        before,
        "a refusal writes nothing"
    );
}

#[test]
fn the_release_module_contains_no_network_process_or_repository_publishing_code() {
    let text = include_str!("../src/release.rs");
    for banned in [
        "std::net",
        "TcpStream",
        "UdpSocket",
        "std::process",
        "Command::new",
        "reqwest",
        "ureq",
        "hyper",
        "git push",
        "curl",
        "gh release",
    ] {
        assert!(
            !text.contains(banned),
            "release.rs must not contain {banned:?}"
        );
    }
}
