// SPDX-License-Identifier: AGPL-3.0-only
//! Fixture world for the TB-5 checks: small TSV registries built row by row so each test edits one
//! cell. The base world is fully resolved; every test breaks exactly one thing.
#![allow(dead_code)]
use serde_json::json;
use std::{collections::BTreeSet, fs, path::PathBuf};
use xtask::{
    behaviours::{self, Behaviour, Fault},
    groups::{self, Catch, Group, Inputs, Policy, Resolution},
    lint::Source,
    retire::{self, Baseline, Finding, Ledger},
};

pub struct Tsv {
    pub cols: Vec<&'static str>,
    pub rows: Vec<Vec<String>>,
}
impl Tsv {
    pub fn new(cols: &[&'static str], rows: &[&[&str]]) -> Self {
        Self {
            cols: cols.to_vec(),
            rows: rows
                .iter()
                .map(|r| {
                    assert_eq!(r.len(), cols.len(), "fixture row width");
                    r.iter().map(|c| (*c).to_string()).collect()
                })
                .collect(),
        }
    }
    fn col(&self, name: &str) -> usize {
        self.cols.iter().position(|c| *c == name).unwrap()
    }
    /// The row whose first cell is `key`.
    pub fn row(&mut self, key: &str) -> usize {
        self.rows.iter().position(|r| r[0] == key).unwrap()
    }
    pub fn set(&mut self, key: &str, column: &str, value: &str) {
        let (r, c) = (self.row(key), self.col(column));
        self.rows[r][c] = value.to_string();
    }
    pub fn get(&mut self, key: &str, column: &str) -> String {
        let (r, c) = (self.row(key), self.col(column));
        self.rows[r][c].clone()
    }
    pub fn drop_row(&mut self, key: &str) {
        let r = self.row(key);
        self.rows.remove(r);
    }
    pub fn push(&mut self, row: &[&str]) {
        assert_eq!(row.len(), self.cols.len());
        self.rows
            .push(row.iter().map(|c| (*c).to_string()).collect());
    }
    pub fn render(&self) -> String {
        let mut text = self.cols.join("\t") + "\n";
        for row in &self.rows {
            text += &(row.join("\t") + "\n");
        }
        text
    }
}

pub const ALPHA_RS: &str = "#[test]\nfn test_a() {}\n#[test]\nfn test_b() {}\n#[test]\nfn test_c() {}\nmod nested {\n    #[test]\n    fn inner() {}\n}\n";
pub const RUN_LOG: &str = "     Running tests/alpha.rs (target/debug/deps/alpha-1)\n\nrunning 4 tests\ntest test_a ... ok\ntest test_b ... ok\ntest test_c ... ok\ntest nested::inner ... ok\n\ntest result: ok. 4 passed; 0 failed; 0 ignored\n";
pub const MUTATION_LOG: &str = "FAULT-CAUGHT F-INST-01 by test_a\nFAULT-CAUGHT F-INST-02 by test_b\nFAULT-CAUGHT F-CONSENT-01 by test_c\nFAULT-CAUGHT F-CONSENT-02 by test_c\nFAULT-CAUGHT F-REL-02 by nested::inner\nmutation: 5 caught\n";
pub const T1: &str = "rs:crates/x/tests/alpha.rs::test_a";
pub const T2: &str = "rs:crates/x/tests/alpha.rs::test_b";
pub const T3: &str = "rs:crates/x/tests/alpha.rs::test_c";
pub const T4: &str = "rs:crates/x/tests/alpha.rs::nested::inner";
pub const EV_RUN: &str = "EV-20261029-0001";
pub const EV_MUT: &str = "EV-20261029-0002";
pub const EV_ADJ: &str = "EV-20261029-0003";
pub const EV_FAIL: &str = "EV-20261029-0004";
pub const EV_NOTGATE: &str = "EV-20261029-0005";
pub const SRC: &str = "mycelium_a13_packaging/installer_core.py:179-193 @ f4e366e9";
pub const REACH: &str = "NOT EXECUTED. Entry points checked: app.py:10 main() dispatches installer only; routes.py:44 never imports it; no worker launches it.";

pub const POLICY: &str = r#"{"schema":2,"rollback_sha":"f4e366e98a1f705dfaf182897044500d290dbd7f","rollback_note":"v0.1.4 oracle tree, read-only archive","approvers":["integrator-provisional","owner"],"default_approver":"integrator-provisional","approved":"2026-10-29"}"#;
pub const BASELINE: &str = r#"[
 {"id":"T:test_alpha.py","source":"test_alpha.py","role":"test_file","source_tests":[{"name":"test_one"},{"name":"test_two"},{"name":"test_three"}]},
 {"id":"T:test_beta.py","source":"test_beta.py","role":"test_file","source_tests":[{"name":"test_four"},{"name":"test_five"}]},
 {"id":"T:test_gamma.py","source":"test_gamma.py","role":"test_file","source_tests":[{"name":"test_g"}]},
 {"id":"T:test_delta.py","source":"test_delta.py","role":"test_file","source_tests":[{"name":"test_d"}]},
 {"id":"T:test_eps.py","source":"test_eps.py","role":"test_file","source_tests":[{"name":"test_e"}]},
 {"id":"T:test_zeta.py","source":"test_zeta.py","role":"test_file","source_tests":[{"name":"test_z"}]},
 {"id":"T:conftest.py","source":"conftest.py","role":"support_or_fixture","source_tests":[]}
]"#;

pub struct World {
    pub behaviours: Tsv,
    pub faults: Tsv,
    pub disp: Tsv,
    pub catches: Tsv,
    pub policy: String,
    pub baseline: String,
    pub ledger: Vec<serde_json::Value>,
    pub logs: Vec<(String, String)>,
    pub rust: String,
    pub schema1: Vec<String>,
    pub dir: PathBuf,
}
pub const BCOLS: [&str; 12] = behaviours::BEHAVIOUR_COLUMNS;
pub const FCOLS: [&str; 5] = behaviours::FAULT_COLUMNS;
pub const DISPCOLS: [&str; 8] = [
    "group_id",
    "path",
    "n_tests",
    "disposition",
    "b_ids",
    "reason_or_reachability",
    "source_step",
    "confidence",
];

impl World {
    pub fn new(tag: &str) -> Self {
        let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../target/xtask-tb5-tests")
            .join(format!("{}-{tag}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let b = |id: &str, area: &str, catch: &str, tests: &str| -> Vec<String> {
            vec![
                id.into(),
                area.into(),
                format!("statement of {id}"),
                "why".into(),
                SRC.into(),
                "executed-v0.1.4".into(),
                "V+J".into(),
                "bad digest (package_digest_mismatch); downgrade refused (floor)".into(),
                catch.into(),
                tests.into(),
                EV_RUN.into(),
                "proven".into(),
            ]
        };
        let behaviours = Tsv {
            cols: BCOLS.to_vec(),
            rows: vec![
                b(
                    "B-INST-01",
                    "INST",
                    "F-INST-01,F-INST-02",
                    &format!("{T1};{T2}"),
                ),
                b("B-CONSENT-01", "CONSENT", "F-CONSENT-01,F-CONSENT-02", T3),
                b("B-REL-01", "REL", "F-REL-01,F-REL-02", T4),
            ],
        };
        let faults = Tsv::new(
            &FCOLS,
            &[
                &[
                    "F-INST-01",
                    "INST",
                    "copy before verify",
                    "B-INST-01",
                    "plausible",
                ],
                &[
                    "F-INST-02",
                    "INST",
                    "POSITIVE CONTROL: always refuse",
                    "B-INST-01",
                    "positive-control",
                ],
                &[
                    "F-CONSENT-01",
                    "CONSENT",
                    "ignore the cap",
                    "B-CONSENT-01",
                    "plausible",
                ],
                &[
                    "F-CONSENT-02",
                    "CONSENT",
                    "POSITIVE CONTROL",
                    "B-CONSENT-01",
                    "positive-control",
                ],
                &["F-REL-01", "REL", "skip the pin", "B-REL-01", "F5"],
                &[
                    "F-REL-02",
                    "REL",
                    "POSITIVE CONTROL",
                    "B-REL-01",
                    "positive-control",
                ],
            ],
        );
        let disp = Tsv::new(
            &DISPCOLS,
            &[
                &[
                    "T:test_alpha.py",
                    "test_alpha.py",
                    "3",
                    "evidence_for",
                    "B-INST-01;B-CONSENT-01",
                    "digest mismatch",
                    "TB-4-P1",
                    "high",
                ],
                &[
                    "T:test_beta.py",
                    "test_beta.py",
                    "2",
                    "not_shipped",
                    "",
                    "imports no shipped-closure module; imports repo module mycelium_beta@L7",
                    "TB-3",
                    "high",
                ],
                &[
                    "T:test_gamma.py",
                    "test_gamma.py",
                    "1",
                    "not_executed",
                    "",
                    REACH,
                    "TB-4-P1",
                    "high",
                ],
                &[
                    "T:test_delta.py",
                    "test_delta.py",
                    "1",
                    "superseded_tooling",
                    "B-REL-01",
                    "old release script replaced by xtask",
                    "TB-4-P2",
                    "high",
                ],
                &[
                    "T:test_eps.py",
                    "test_eps.py",
                    "1",
                    "structure_only",
                    "",
                    "source-text shape check replaced by xtask lint SE001",
                    "TB-4-P2",
                    "high",
                ],
                &[
                    "T:test_zeta.py",
                    "test_zeta.py",
                    "1",
                    "python_only",
                    "",
                    "tests a pytest helper, not product code",
                    "TB-4-P2",
                    "high",
                ],
                &[
                    "T:conftest.py",
                    "conftest.py",
                    "0",
                    "python_only",
                    "",
                    "pytest fixture module",
                    "TB-3",
                    "high",
                ],
            ],
        );
        let catches = Tsv::new(
            &groups::CATCH_COLUMNS,
            &[
                &["F-INST-01", "caught", EV_MUT, T1, "", ""],
                &["F-INST-02", "caught", EV_MUT, T2, "", ""],
                &["F-CONSENT-01", "caught", EV_MUT, T3, "", ""],
                &["F-CONSENT-02", "caught", EV_MUT, T3, "", ""],
                &[
                    "F-REL-01",
                    "adjudicated",
                    EV_ADJ,
                    "",
                    "equivalent: the pin check is dead code behind the floor check",
                    "integrator-provisional",
                ],
                &["F-REL-02", "caught", EV_MUT, T4, "", ""],
            ],
        );
        let ledger_row = |id: &str, kind: &str, verdict: &str, log: &str| json!({"id": id, "kind": kind, "verdict": verdict, "log": dir.join(log).display().to_string()});
        Self {
            behaviours,
            faults,
            disp,
            catches,
            policy: POLICY.into(),
            baseline: BASELINE.into(),
            ledger: vec![
                ledger_row(EV_RUN, "gate", "PASS", "run.log"),
                ledger_row(EV_MUT, "gate", "PASS", "mut.log"),
                ledger_row(EV_ADJ, "gate", "PASS", "adj.log"),
                ledger_row(EV_FAIL, "gate", "FAIL", "run.log"),
                ledger_row(EV_NOTGATE, "note", "PASS", "run.log"),
            ],
            logs: vec![
                ("run.log".into(), RUN_LOG.into()),
                ("mut.log".into(), MUTATION_LOG.into()),
                ("adj.log".into(), "adjudication note\n".into()),
            ],
            rust: ALPHA_RS.into(),
            schema1: Vec::new(),
            dir,
        }
    }
    pub fn ledger_text(&self) -> String {
        self.ledger
            .iter()
            .map(|v| v.to_string() + "\n")
            .collect::<String>()
    }
    pub fn write_logs(&self) {
        for (name, text) in &self.logs {
            fs::write(self.dir.join(name), text).unwrap();
        }
    }
    pub fn ledger_parsed(&self) -> Ledger {
        self.write_logs();
        Ledger::parse(&self.ledger_text(), &self.dir).unwrap()
    }
    pub fn rust_tests(&self) -> BTreeSet<String> {
        retire::discover_rust_tests(&[Source {
            path: "crates/x/tests/alpha.rs".into(),
            text: self.rust.clone(),
        }])
        .unwrap()
    }
    pub fn baseline_parsed(&self) -> Baseline {
        retire::load_baseline(self.baseline.as_bytes(), "f5bfb709").unwrap()
    }
    pub fn catalogue(&self) -> (Vec<Behaviour>, Vec<Fault>, Vec<Finding>) {
        let (b, mut findings) =
            behaviours::load_behaviours(&self.behaviours.render(), "B.tsv").unwrap();
        let (f, more) = behaviours::load_faults(&self.faults.render(), "F.tsv").unwrap();
        findings.extend(more);
        (b, f, findings)
    }
    /// Catalogue validation findings (with the ledger and the Rust tests).
    pub fn validate(&self) -> Vec<Finding> {
        let (b, f, mut findings) = self.catalogue();
        let ledger = self.ledger_parsed();
        let rust = self.rust_tests();
        findings.extend(behaviours::validate(
            &b,
            &f,
            &behaviours::Context {
                rust_tests: Some(&rust),
                ledger: Some(&ledger),
            },
            None,
        ));
        findings
    }
    /// Runs the whole schema-2 resolution on this world.
    pub fn resolve(&self) -> (Resolution, Vec<Finding>) {
        let (b, f, load) = self.catalogue();
        let ledger = self.ledger_parsed();
        let rust = self.rust_tests();
        let baseline = self.baseline_parsed();
        let catalogue_findings = behaviours::validate(
            &b,
            &f,
            &behaviours::Context {
                rust_tests: Some(&rust),
                ledger: Some(&ledger),
            },
            None,
        );
        let (groups, group_findings): (Vec<Group>, _) =
            groups::load_final_dispositions(&self.disp.render(), &baseline).unwrap();
        let (catches, catch_findings): (Vec<Catch>, _) =
            groups::load_catches(&self.catches.render()).unwrap();
        let policy = Policy::parse(&self.policy).unwrap();
        let schema1: BTreeSet<String> = self.schema1.iter().cloned().collect();
        let resolution = groups::resolve(&Inputs {
            behaviours: &b,
            faults: &f,
            groups: &groups,
            catches: &catches,
            catalogue_findings: &catalogue_findings,
            rust_tests: &rust,
            ledger: &ledger,
            policy: &policy,
            baseline: &baseline,
            schema1_resolved: &schema1,
        });
        let mut findings = load;
        findings.extend(catalogue_findings);
        findings.extend(group_findings);
        findings.extend(catch_findings);
        findings.extend(resolution.findings.clone());
        (resolution, findings)
    }
}
impl Drop for World {
    fn drop(&mut self) {
        if let Err(error) = fs::remove_dir_all(&self.dir) {
            eprintln!("could not remove {}: {error}", self.dir.display());
        }
    }
}
pub fn codes(findings: &[Finding]) -> Vec<&'static str> {
    findings.iter().map(|f| f.code).collect()
}
pub fn has(findings: &[Finding], code: &str, subject: &str) -> bool {
    findings
        .iter()
        .any(|f| f.code == code && f.subject == subject)
}

use std::process::{Command, Output};

impl World {
    /// Writes the registry directory (BEHAVIOURS.tsv, FAULTS.tsv, dispositions, tb4/, ledger, logs),
    /// a throw-away workspace root and the inventory files; returns the xtask arguments after the
    /// subcommand word (`--root`, `--ledger`, inventory and policy paths).
    pub fn materialise(&self) -> Vec<String> {
        use sha2::{Digest, Sha256};
        self.write_logs();
        let root = self.dir.join("root");
        fs::create_dir_all(root.join("crates/x/tests")).unwrap();
        fs::create_dir_all(self.dir.join("tb4")).unwrap();
        fs::write(root.join("Cargo.toml"), "[workspace]\n").unwrap();
        fs::write(root.join("crates/x/tests/alpha.rs"), &self.rust).unwrap();
        let write = |name: &str, text: String| fs::write(self.dir.join(name), text).unwrap();
        write("BEHAVIOURS.tsv", self.behaviours.render());
        write("FAULTS.tsv", self.faults.render());
        write("TEST-GROUP-DISPOSITIONS.final.tsv", self.disp.render());
        write("evidence.jsonl", self.ledger_text());
        write("fault-catches.tsv", self.catches.render());
        write("groups.json", self.policy.clone());
        write("baseline.json", self.baseline.clone());
        write("obligations.json", "[]".into());
        let sha: String = Sha256::digest(self.baseline.as_bytes())
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        write(
            "registry.json",
            json!({"schema": 1, "baseline": {"sha256": sha, "source_sha": "f5bfb709"},
                   "approvers": ["integrator", "owner"], "rows": []})
            .to_string(),
        );
        let path = |name: &str| self.dir.join(name).display().to_string();
        let mut args = vec![
            "--root".to_string(),
            root.display().to_string(),
            "--ledger".into(),
            path("evidence.jsonl"),
            "--baseline".into(),
            path("baseline.json"),
            "--baseline-source-sha".into(),
            "f5bfb709".into(),
            "--obligations".into(),
            path("obligations.json"),
            "--registry".into(),
            path("registry.json"),
            "--catches".into(),
            path("fault-catches.tsv"),
            "--policy".into(),
            path("groups.json"),
        ];
        args.shrink_to_fit();
        args
    }
}
/// Runs `xtask <word> check <args...>`.
pub fn run_xtask(word: &str, args: &[String], extra: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_xtask"))
        .arg(word)
        .arg("check")
        .args(args)
        .args(extra)
        .output()
        .unwrap()
}
pub fn stdout(output: &Output) -> String {
    String::from_utf8_lossy(&output.stdout).to_string()
}
