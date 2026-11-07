// SPDX-License-Identifier: AGPL-3.0-only
//! Fixture provenance inventory (CL-L4-09, RU-003).
//!
//! Every expected-value fixture needs a row in its crate's `fixtures/PROVENANCE.tsv`
//! (tab separated, header below, `#` comments and blank lines allowed). A *fixture* is
//! any file below a crate directory (nearest `Cargo.toml`) whose path, relative to that
//! crate, has a directory named `fixtures`, `testdata`, `golden` or `goldens`, or starts
//! with `tests/data/`. `path` is relative to the crate directory, `/`-separated.
//!
//! | column | rule |
//! |---|---|
//! | `fixture_id` | `FX-<lane>-<name>`, unique workspace-wide |
//! | `kind` | `oracle` executed v0.1.4 output; `spec` derived from a cited spec/constants; `recorded` recorded tool output; `input` carries no expected value |
//! | `probe` | the probe script path, command or citation (nonempty) |
//! | `probe_sha256` | 64 lower-case hex; required for `oracle`, else `-` or hex |
//! | `oracle_sha` | 40 lower-case hex commit of the executed oracle; required for `oracle`, else `-` or hex |
//! | `case_count` | number of cases in the fixture (`oracle` needs >= 1) |
//! | `fixture_sha256` | 64 lower-case hex of the file bytes; a mismatch is stale |
//!
//! Findings: FP001 no row, FP002 malformed row, FP003 stale sha, FP004 row names no
//! fixture of this crate, FP005 duplicate id or path, FP006 symlink not inventoried.
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::Path,
};

pub const FILE: &str = "fixtures/PROVENANCE.tsv";
pub const HEADER: [&str; 8] = [
    "path",
    "fixture_id",
    "kind",
    "probe",
    "probe_sha256",
    "oracle_sha",
    "case_count",
    "fixture_sha256",
];
const FIXTURE_DIRS: [&str; 4] = ["fixtures", "testdata", "golden", "goldens"];
const SKIPPED_DIRS: [&str; 3] = [".git", "target", "node_modules"];

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Finding {
    pub path: String,
    pub line: usize,
    pub code: &'static str,
    pub detail: String,
}
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Report {
    pub fixtures: usize,
    /// Fixtures whose row is well formed, unique, and matches the file's bytes.
    pub covered: usize,
    /// Per crate directory: (fixtures, covered).
    pub per_crate: BTreeMap<String, (usize, usize)>,
    pub findings: Vec<Finding>,
}
impl Report {
    pub fn lacking(&self) -> usize {
        self.findings.iter().filter(|f| f.code == "FP001").count()
    }
    pub fn lacking_by_crate(&self) -> BTreeMap<String, usize> {
        let mut result = BTreeMap::new();
        for finding in self.findings.iter().filter(|f| f.code == "FP001") {
            *result.entry(finding.detail.clone()).or_insert(0) += 1;
        }
        result
    }
}

pub fn explanation(code: &str) -> &'static str {
    match code {
        "FP001" => "fixture has no provenance row (add one to the crate's fixtures/PROVENANCE.tsv)",
        "FP002" => "malformed provenance row or header",
        "FP003" => "provenance row is stale: fixture_sha256 does not match the file",
        "FP004" => "provenance row names no fixture file of this crate",
        "FP005" => "duplicate fixture_id or duplicate path in provenance rows",
        "FP006" => "symlinked fixture is not inventoried",
        _ => "unknown provenance rule",
    }
}

fn io(path: &str, error: &std::io::Error) -> String {
    format!("{path}:1: IO: {:?}", error.kind())
}

struct Walk {
    crate_dirs: BTreeSet<String>,
    files: Vec<(String, bool)>,
}
fn walk(directory: &Path, relative: &str, out: &mut Walk) -> Result<(), String> {
    let entries = fs::read_dir(directory).map_err(|e| io(relative, &e))?;
    let mut names = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|e| io(relative, &e))?;
        names.push(entry);
    }
    names.sort_by_key(|e| e.file_name());
    for entry in names {
        let name = entry
            .file_name()
            .into_string()
            .map_err(|_| format!("{relative}:1: PROVENANCE: non-UTF-8 file name"))?;
        let child = if relative.is_empty() {
            name.clone()
        } else {
            format!("{relative}/{name}")
        };
        let kind = entry.file_type().map_err(|e| io(&child, &e))?;
        if kind.is_symlink() {
            out.files.push((child, true));
        } else if kind.is_dir() {
            if !SKIPPED_DIRS.contains(&name.as_str()) {
                walk(&entry.path(), &child, out)?;
            }
        } else {
            if name == "Cargo.toml" {
                out.crate_dirs.insert(relative.to_string());
            }
            out.files.push((child, false));
        }
    }
    Ok(())
}
fn is_fixture(crate_relative: &str) -> bool {
    if crate_relative == FILE {
        return false;
    }
    let parts: Vec<&str> = crate_relative.split('/').collect();
    let directories = &parts[..parts.len() - 1];
    directories.iter().any(|d| FIXTURE_DIRS.contains(d))
        || crate_relative.starts_with("tests/data/")
}
fn owner<'a>(crate_dirs: &'a BTreeSet<String>, path: &str) -> &'a str {
    crate_dirs
        .iter()
        .filter(|dir| dir.is_empty() || path.starts_with(&format!("{dir}/")))
        .max_by_key(|dir| dir.len())
        .map_or("", String::as_str)
}
fn hex_of(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}
fn id_ok(id: &str) -> bool {
    let Some(rest) = id.strip_prefix("FX-") else {
        return false;
    };
    let Some((lane, name)) = rest.split_once('-') else {
        return false;
    };
    !lane.is_empty()
        && lane.bytes().all(|b| b.is_ascii_alphanumeric())
        && !name.is_empty()
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}
fn path_ok(path: &str) -> bool {
    !path.is_empty()
        && !path.contains('\\')
        && path
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != "..")
}

struct Row {
    line: usize,
    path: String,
    id: String,
    sha: String,
}
/// Validate one row's fields; `Err` carries the FP002 detail.
fn check_row(fields: &[&str]) -> Result<(), &'static str> {
    let [path, id, kind, probe, probe_sha, oracle, cases, sha] = fields else {
        return Err("expected 8 tab-separated columns");
    };
    if !path_ok(path) {
        return Err("path must be crate-relative with '/' separators");
    }
    if !id_ok(id) {
        return Err("fixture_id must be FX-<lane>-<name>");
    }
    if !matches!(*kind, "oracle" | "spec" | "recorded" | "input") {
        return Err("kind must be oracle, spec, recorded or input");
    }
    let oracle_kind = *kind == "oracle";
    if probe.trim().is_empty() || probe.chars().any(char::is_control) {
        return Err("probe must be nonempty text");
    }
    let optional_hex = |value: &str, length: usize, required: bool| {
        if required {
            lower_hex(value, length)
        } else {
            value == "-" || lower_hex(value, length)
        }
    };
    if !optional_hex(probe_sha, 64, oracle_kind) {
        return Err("probe_sha256 must be 64 lower-case hex (required for oracle)");
    }
    if !optional_hex(oracle, 40, oracle_kind) {
        return Err("oracle_sha must be 40 lower-case hex (required for oracle)");
    }
    match cases.parse::<u64>() {
        Ok(0) if oracle_kind => return Err("an oracle fixture needs case_count >= 1"),
        Ok(_) => {}
        Err(_) => return Err("case_count must be a number"),
    }
    if !lower_hex(sha, 64) {
        return Err("fixture_sha256 must be 64 lower-case hex");
    }
    Ok(())
}

pub fn scan(root: &Path) -> Result<Report, String> {
    let mut walked = Walk {
        crate_dirs: BTreeSet::new(),
        files: Vec::new(),
    };
    walk(root, "", &mut walked)?;
    let mut report = Report::default();
    // crate dir -> (fixture crate-relative path -> workspace-relative path)
    let mut fixtures: BTreeMap<String, BTreeMap<String, String>> = BTreeMap::new();
    let mut tables: BTreeSet<String> = BTreeSet::new();
    for (path, symlink) in &walked.files {
        let krate = owner(&walked.crate_dirs, path).to_string();
        let relative = if krate.is_empty() {
            path.as_str()
        } else {
            &path[krate.len() + 1..]
        };
        if relative == FILE {
            tables.insert(krate);
            continue;
        }
        let name = relative.rsplit('/').next().unwrap_or(relative);
        let fixture_dir_link = *symlink && FIXTURE_DIRS.contains(&name);
        if is_fixture(relative) || fixture_dir_link {
            if *symlink {
                report.findings.push(Finding {
                    path: path.clone(),
                    line: 1,
                    code: "FP006",
                    detail: krate_label(&krate),
                });
            } else {
                fixtures
                    .entry(krate)
                    .or_default()
                    .insert(relative.to_string(), path.clone());
            }
        }
    }
    let mut ids: BTreeMap<String, String> = BTreeMap::new();
    let all_crates: BTreeSet<String> = fixtures
        .keys()
        .cloned()
        .chain(tables.iter().cloned())
        .collect();
    for krate in all_crates {
        let files = fixtures.get(&krate).cloned().unwrap_or_default();
        let table_path = if krate.is_empty() {
            FILE.to_string()
        } else {
            format!("{krate}/{FILE}")
        };
        let mut mentioned: BTreeSet<String> = BTreeSet::new();
        let mut covered: BTreeSet<String> = BTreeSet::new();
        let mut seen_paths: BTreeSet<String> = BTreeSet::new();
        if tables.contains(&krate) {
            let text =
                fs::read_to_string(walk_path(root, &table_path)).map_err(|e| io(&table_path, &e));
            let finding = |line: usize, code: &'static str, detail: &str| Finding {
                path: table_path.clone(),
                line,
                code,
                detail: detail.to_string(),
            };
            match text {
                Err(error) => report.findings.push(finding(1, "FP002", &error)),
                Ok(text) => {
                    let mut header_seen = false;
                    let mut rows = Vec::new();
                    for (index, line) in text.lines().enumerate() {
                        if line.trim().is_empty() || line.starts_with('#') {
                            continue;
                        }
                        let number = index + 1;
                        let fields: Vec<&str> = line.split('\t').collect();
                        if !header_seen {
                            header_seen = true;
                            if fields != HEADER {
                                report.findings.push(finding(
                                    number,
                                    "FP002",
                                    "header must be the 8 documented columns",
                                ));
                                break;
                            }
                            continue;
                        }
                        if let Err(detail) = check_row(&fields) {
                            report.findings.push(finding(number, "FP002", detail));
                            if fields.len() == 8 && path_ok(fields[0]) {
                                mentioned.insert(fields[0].to_string());
                            }
                            continue;
                        }
                        rows.push(Row {
                            line: number,
                            path: fields[0].to_string(),
                            id: fields[1].to_string(),
                            sha: fields[7].to_string(),
                        });
                    }
                    for row in rows {
                        mentioned.insert(row.path.clone());
                        let first_row_for_path = seen_paths.insert(row.path.clone());
                        if let Some(first) = ids.get(&row.id) {
                            report.findings.push(finding(
                                row.line,
                                "FP005",
                                &format!("fixture_id {} already used at {first}", row.id),
                            ));
                            continue;
                        }
                        if !first_row_for_path {
                            report.findings.push(finding(
                                row.line,
                                "FP005",
                                "second row for the same path",
                            ));
                            continue;
                        }
                        ids.insert(row.id.clone(), format!("{table_path}:{}", row.line));
                        let Some(actual) = files.get(&row.path) else {
                            report.findings.push(finding(
                                row.line,
                                "FP004",
                                &format!("{} is not a fixture file", row.path),
                            ));
                            continue;
                        };
                        let bytes =
                            fs::read(walk_path(root, actual)).map_err(|e| io(actual, &e))?;
                        if hex_of(&bytes) != row.sha {
                            report.findings.push(finding(
                                row.line,
                                "FP003",
                                &format!("{} changed since the row was written", row.path),
                            ));
                            continue;
                        }
                        covered.insert(row.path);
                    }
                }
            }
        }
        for (relative, workspace_path) in &files {
            if !mentioned.contains(relative) {
                report.findings.push(Finding {
                    path: workspace_path.clone(),
                    line: 1,
                    code: "FP001",
                    detail: krate_label(&krate),
                });
            }
        }
        report.fixtures += files.len();
        report.covered += covered.len();
        report
            .per_crate
            .insert(krate_label(&krate), (files.len(), covered.len()));
    }
    report.findings.sort();
    Ok(report)
}
fn krate_label(krate: &str) -> String {
    if krate.is_empty() {
        ".".to_string()
    } else {
        krate.to_string()
    }
}
fn walk_path(root: &Path, relative: &str) -> std::path::PathBuf {
    root.join(relative)
}
