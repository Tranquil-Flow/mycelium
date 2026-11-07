// SPDX-License-Identifier: AGPL-3.0-only
//! CL-L4-09: every expected-value fixture needs a provenance row. Level is WARN today
//! (inventory for lanes to backfill) and `--strict-provenance` turns it into failures.
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Output},
    sync::atomic::{AtomicUsize, Ordering},
};
use xtask::provenance::{self, Finding};

static NEXT: AtomicUsize = AtomicUsize::new(0);
const HEADER: &str =
    "path\tfixture_id\tkind\tprobe\tprobe_sha256\toracle_sha\tcase_count\tfixture_sha256";
const ORACLE: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";

struct Sandbox(PathBuf);
impl Sandbox {
    fn new() -> Self {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../target/xtask-provenance-tests")
            .join(format!(
                "{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
        fs::create_dir_all(path.join("xtask")).unwrap();
        fs::write(path.join("Cargo.toml"), "[workspace]\n").unwrap();
        fs::write(
            path.join("xtask/allowlist.json"),
            r#"{"version":1,"entries":[]}"#,
        )
        .unwrap();
        fs::write(
            path.join("xtask/quarantine.json"),
            r#"{"version":1,"entries":[]}"#,
        )
        .unwrap();
        Self(path)
    }
    fn krate(&self, dir: &str) {
        self.write(&format!("{dir}/Cargo.toml"), "[package]\nname = \"c\"\n");
    }
    fn write(&self, rel: &str, text: &str) {
        let path = self.0.join(rel);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }
    /// sha256 by the OS tool, independent of the code under test.
    fn sha(&self, rel: &str) -> String {
        let out = Command::new("shasum")
            .args(["-a", "256"])
            .arg(self.0.join(rel))
            .output()
            .unwrap();
        String::from_utf8(out.stdout)
            .unwrap()
            .split_whitespace()
            .next()
            .unwrap()
            .to_string()
    }
    fn rows(&self, krate: &str, rows: &[String]) {
        let mut text = format!("{HEADER}\n");
        for row in rows {
            text.push_str(row);
            text.push('\n');
        }
        self.write(&format!("{krate}/fixtures/PROVENANCE.tsv"), &text);
    }
    fn oracle_row(&self, krate: &str, rel: &str, id: &str) -> String {
        let sha = self.sha(&format!("{krate}/{rel}"));
        format!(
            "{rel}\t{id}\toracle\tprobes/p.py\t{}\t{ORACLE}\t3\t{sha}",
            "a".repeat(64)
        )
    }
    fn scan(&self) -> provenance::Report {
        provenance::scan(&self.0).unwrap()
    }
    fn lint(&self, extra: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_xtask"))
            .arg("lint")
            .arg("--root")
            .arg(&self.0)
            .args(extra)
            .output()
            .unwrap()
    }
}
impl Drop for Sandbox {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn codes(findings: &[Finding]) -> Vec<(&str, &'static str)> {
    findings.iter().map(|f| (f.path.as_str(), f.code)).collect()
}

#[test]
fn a_fixture_with_a_valid_oracle_row_is_covered_and_clean() {
    let s = Sandbox::new();
    s.krate("crates/a");
    s.write("crates/a/fixtures/x.json", "{\"v\":1}\n");
    s.rows(
        "crates/a",
        &[s.oracle_row("crates/a", "fixtures/x.json", "FX-L3-x")],
    );
    let report = s.scan();
    assert_eq!(report.findings, vec![]);
    assert_eq!((report.fixtures, report.covered), (1, 1));
    assert_eq!(report.per_crate["crates/a"], (1, 1));
}

#[test]
fn every_fixture_without_a_row_is_listed_and_counted_per_crate() {
    let s = Sandbox::new();
    s.krate("crates/a");
    s.write("crates/a/fixtures/x.json", "{}");
    s.write("crates/a/tests/fixtures/y.json", "{}");
    s.write("crates/a/tests/data/z.tsv", "a\tb\n");
    s.write("crates/a/testdata/t.bin", "bin");
    s.write("crates/a/golden/g.txt", "g");
    // Not fixtures: source, a plain data dir, docs, build output, VCS metadata.
    s.write("crates/a/src/lib.rs", "");
    s.write("crates/a/data/k.json", "{}");
    s.write("crates/a/README.md", "x");
    s.write("crates/a/target/fixtures/built.json", "{}");
    s.write("crates/a/.git/fixtures/h.json", "{}");
    let report = s.scan();
    assert_eq!(
        codes(&report.findings),
        vec![
            ("crates/a/fixtures/x.json", "FP001"),
            ("crates/a/golden/g.txt", "FP001"),
            ("crates/a/testdata/t.bin", "FP001"),
            ("crates/a/tests/data/z.tsv", "FP001"),
            ("crates/a/tests/fixtures/y.json", "FP001"),
        ]
    );
    assert_eq!((report.fixtures, report.covered), (5, 0));
    assert_eq!(report.per_crate["crates/a"], (5, 0));
}

#[test]
fn rows_belong_to_the_nearest_crate_only() {
    let s = Sandbox::new();
    s.krate("crates/a");
    s.krate("crates/b");
    s.write("crates/a/fixtures/x.json", "{\"a\":1}");
    s.write("crates/b/fixtures/x.json", "{\"b\":1}");
    s.rows(
        "crates/b",
        &[s.oracle_row("crates/b", "fixtures/x.json", "FX-L3-bx")],
    );
    let report = s.scan();
    assert_eq!(
        codes(&report.findings),
        vec![("crates/a/fixtures/x.json", "FP001")]
    );
    assert_eq!(report.per_crate["crates/a"], (1, 0));
    assert_eq!(report.per_crate["crates/b"], (1, 1));
}

#[test]
fn a_changed_fixture_makes_its_row_stale() {
    let s = Sandbox::new();
    s.krate("crates/a");
    s.write("crates/a/fixtures/x.json", "{\"v\":1}");
    s.rows(
        "crates/a",
        &[s.oracle_row("crates/a", "fixtures/x.json", "FX-L3-x")],
    );
    s.write("crates/a/fixtures/x.json", "{\"v\":2}");
    let report = s.scan();
    assert_eq!(
        codes(&report.findings),
        vec![("crates/a/fixtures/PROVENANCE.tsv", "FP003")]
    );
    assert_eq!(report.findings[0].line, 2);
    assert_eq!(
        report.covered, 0,
        "a stale row does not count as provenance"
    );
}

#[test]
fn a_row_for_a_missing_file_or_a_non_fixture_path_is_an_orphan() {
    let s = Sandbox::new();
    s.krate("crates/a");
    s.write("crates/a/fixtures/x.json", "{}");
    s.write("crates/a/src/lib.rs", "");
    let sha = s.sha("crates/a/fixtures/x.json");
    let ghost = format!("fixtures/ghost.json\tFX-L3-ghost\tinput\t-\t-\t-\t1\t{sha}");
    let outside = format!(
        "src/lib.rs\tFX-L3-lib\tinput\t-\t-\t-\t1\t{}",
        s.sha("crates/a/src/lib.rs")
    );
    s.rows(
        "crates/a",
        &[
            ghost,
            outside,
            s.oracle_row("crates/a", "fixtures/x.json", "FX-L3-x"),
        ],
    );
    let report = s.scan();
    assert_eq!(
        codes(&report.findings),
        vec![
            ("crates/a/fixtures/PROVENANCE.tsv", "FP004"),
            ("crates/a/fixtures/PROVENANCE.tsv", "FP004"),
        ]
    );
    assert_eq!(
        report.findings.iter().map(|f| f.line).collect::<Vec<_>>(),
        vec![2, 3]
    );
}

#[test]
fn duplicate_ids_and_duplicate_paths_are_refused() {
    let s = Sandbox::new();
    s.krate("crates/a");
    s.krate("crates/b");
    s.write("crates/a/fixtures/x.json", "{\"1\":1}");
    s.write("crates/a/fixtures/y.json", "{\"2\":2}");
    s.write("crates/b/fixtures/z.json", "{\"3\":3}");
    s.rows(
        "crates/a",
        &[
            s.oracle_row("crates/a", "fixtures/x.json", "FX-L3-same"),
            s.oracle_row("crates/a", "fixtures/y.json", "FX-L3-same"),
            s.oracle_row("crates/a", "fixtures/y.json", "FX-L3-y2"),
        ],
    );
    s.rows(
        "crates/b",
        &[s.oracle_row("crates/b", "fixtures/z.json", "FX-L3-same")],
    );
    let report = s.scan();
    let dup: Vec<_> = report
        .findings
        .iter()
        .filter(|f| f.code == "FP005")
        .collect();
    assert_eq!(
        dup.iter()
            .map(|f| (f.path.as_str(), f.line))
            .collect::<Vec<_>>(),
        vec![
            ("crates/a/fixtures/PROVENANCE.tsv", 3),
            ("crates/a/fixtures/PROVENANCE.tsv", 4),
            ("crates/b/fixtures/PROVENANCE.tsv", 2),
        ],
        "ids are unique workspace-wide; a path has one row"
    );
}

#[test]
fn malformed_rows_are_fp002_at_their_line() {
    let s = Sandbox::new();
    s.krate("crates/a");
    s.write("crates/a/fixtures/x.json", "{}");
    let sha = s.sha("crates/a/fixtures/x.json");
    let h64 = "b".repeat(64);
    let cases: Vec<(&str, String)> = vec![
        (
            "id without FX- prefix",
            format!("fixtures/x.json\tL3-x\tinput\t-\t-\t-\t1\t{sha}"),
        ),
        (
            "id with a space",
            format!("fixtures/x.json\tFX-L3-a b\tinput\t-\t-\t-\t1\t{sha}"),
        ),
        (
            "unknown kind",
            format!("fixtures/x.json\tFX-L3-x\tgolden\t-\t-\t-\t1\t{sha}"),
        ),
        (
            "too few columns",
            "fixtures/x.json\tFX-L3-x\tinput".to_string(),
        ),
        (
            "too many columns",
            format!("fixtures/x.json\tFX-L3-x\tinput\t-\t-\t-\t1\t{sha}\textra"),
        ),
        (
            "oracle without oracle sha",
            format!("fixtures/x.json\tFX-L3-x\toracle\tp\t{h64}\t-\t1\t{sha}"),
        ),
        (
            "oracle with short oracle sha",
            format!("fixtures/x.json\tFX-L3-x\toracle\tp\t{h64}\tabc123\t1\t{sha}"),
        ),
        (
            "oracle without probe sha",
            format!("fixtures/x.json\tFX-L3-x\toracle\tp\t-\t{ORACLE}\t1\t{sha}"),
        ),
        (
            "oracle with empty probe",
            format!("fixtures/x.json\tFX-L3-x\toracle\t\t{h64}\t{ORACLE}\t1\t{sha}"),
        ),
        (
            "oracle with zero cases",
            format!("fixtures/x.json\tFX-L3-x\toracle\tp\t{h64}\t{ORACLE}\t0\t{sha}"),
        ),
        (
            "non-numeric case count",
            format!("fixtures/x.json\tFX-L3-x\tinput\t-\t-\t-\tmany\t{sha}"),
        ),
        (
            "uppercase fixture sha",
            format!(
                "fixtures/x.json\tFX-L3-x\tinput\t-\t-\t-\t1\t{}",
                sha.to_uppercase()
            ),
        ),
        (
            "short fixture sha",
            "fixtures/x.json\tFX-L3-x\tinput\t-\t-\t-\t1\tabc".to_string(),
        ),
        (
            "absolute path",
            format!("/etc/x.json\tFX-L3-x\tinput\t-\t-\t-\t1\t{sha}"),
        ),
        (
            "parent path",
            format!("../x.json\tFX-L3-x\tinput\t-\t-\t-\t1\t{sha}"),
        ),
        (
            "probe sha malformed",
            format!("fixtures/x.json\tFX-L3-x\tspec\tcite\tzz\t-\t1\t{sha}"),
        ),
    ];
    for (what, row) in cases {
        s.rows("crates/a", &[row]);
        let report = s.scan();
        let bad: Vec<_> = report
            .findings
            .iter()
            .filter(|f| f.code == "FP002")
            .collect();
        assert_eq!(bad.len(), 1, "{what}: {:?}", report.findings);
        assert_eq!(bad[0].line, 2, "{what}");
        assert_eq!(
            report.covered, 0,
            "{what}: an invalid row is not provenance"
        );
    }
}

#[test]
fn a_wrong_header_is_refused_and_comments_and_blank_lines_are_fine() {
    let s = Sandbox::new();
    s.krate("crates/a");
    s.write("crates/a/fixtures/x.json", "{}");
    let sha = s.sha("crates/a/fixtures/x.json");
    s.write(
        "crates/a/fixtures/PROVENANCE.tsv",
        &format!("# provenance\n\n{HEADER}\n\n# one row\nfixtures/x.json\tFX-L3-x\tinput\t-\t-\t-\t1\t{sha}\n"),
    );
    assert_eq!(s.scan().findings, vec![]);
    s.write(
        "crates/a/fixtures/PROVENANCE.tsv",
        &format!("path\tid\tkind\nfixtures/x.json\tFX-L3-x\tinput\t-\t-\t-\t1\t{sha}\n"),
    );
    let report = s.scan();
    assert!(
        report
            .findings
            .iter()
            .any(|f| f.code == "FP002" && f.line == 1)
    );
    assert_eq!(report.covered, 0);
}

#[test]
fn spec_recorded_and_input_rows_do_not_need_oracle_fields() {
    let s = Sandbox::new();
    s.krate("crates/a");
    for name in ["a", "b", "c"] {
        s.write(&format!("crates/a/fixtures/{name}.txt"), name);
    }
    let sha = |n: &str| s.sha(&format!("crates/a/fixtures/{n}.txt"));
    s.rows(
        "crates/a",
        &[
            format!("fixtures/a.txt\tFX-L4-a\tspec\tREADME rule table row SE001\t-\t-\t4\t{}", sha("a")),
            format!("fixtures/b.txt\tFX-L4-b\trecorded\tcargo test log of EV-20261028-0236\t-\t-\t37\t{}", sha("b")),
            format!("fixtures/c.txt\tFX-L4-c\tinput\t-\t-\t-\t0\t{}", sha("c")),
        ],
    );
    let report = s.scan();
    assert_eq!(report.findings, vec![]);
    assert_eq!(report.covered, 3);
}

#[test]
fn symlinked_fixtures_are_reported_never_followed_or_silently_skipped() {
    let s = Sandbox::new();
    s.krate("crates/a");
    s.write("secret.json", "{}");
    fs::create_dir_all(s.0.join("crates/a/fixtures")).unwrap();
    std::os::unix::fs::symlink(
        s.0.join("secret.json"),
        s.0.join("crates/a/fixtures/link.json"),
    )
    .unwrap();
    let report = s.scan();
    assert_eq!(
        codes(&report.findings),
        vec![("crates/a/fixtures/link.json", "FP006")]
    );
}

#[test]
fn the_xtask_crate_dogfoods_its_own_fixtures() {
    let report = provenance::scan(Path::new(env!("CARGO_MANIFEST_DIR"))).unwrap();
    assert_eq!(
        report.findings,
        vec![],
        "xtask fixtures all carry valid rows"
    );
    assert_eq!(
        report.fixtures, 61,
        "18 lint + 11 crypto + 5 guard fixtures + 8 gate logs + 16 release + 3 retire fixtures"
    );
    assert_eq!(report.covered, report.fixtures);
}

#[test]
fn lint_cli_prints_the_inventory_as_warnings_and_exits_zero_until_strict() {
    let s = Sandbox::new();
    s.krate("crates/a");
    s.write("crates/a/fixtures/x.json", "{}");
    s.write("crates/a/fixtures/y.json", "{}");
    s.write("crates/a/src/lib.rs", "pub fn f() {}\n");
    let warn = s.lint(&[]);
    assert_eq!(warn.status.code(), Some(0), "{warn:?}");
    let out = String::from_utf8(warn.stdout).unwrap();
    assert!(
        out.contains("crates/a/fixtures/x.json:1: WARN FP001:"),
        "{out}"
    );
    assert!(
        out.contains("crates/a/fixtures/y.json:1: WARN FP001:"),
        "{out}"
    );
    assert!(
        out.contains("provenance: 2 fixture files, 0 with rows, 2 lacking (WARN)"),
        "{out}"
    );
    assert!(out.contains("crates/a=2"), "per-crate counts: {out}");
    assert!(out.contains("lint: 1 source files, 0 violations"), "{out}");
    let strict = s.lint(&["--strict-provenance"]);
    assert_eq!(strict.status.code(), Some(1), "{strict:?}");
    assert!(
        String::from_utf8(strict.stdout)
            .unwrap()
            .contains("crates/a/fixtures/x.json:1: FP001:")
    );
    s.rows(
        "crates/a",
        &[
            s.oracle_row("crates/a", "fixtures/x.json", "FX-L3-x"),
            s.oracle_row("crates/a", "fixtures/y.json", "FX-L3-y"),
        ],
    );
    assert_eq!(s.lint(&["--strict-provenance"]).status.code(), Some(0));
}
