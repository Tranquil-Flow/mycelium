// SPDX-License-Identifier: AGPL-3.0-only
//! Offline closure inspection and private-fixture cleanup, never a live cache collector.
//! Cleanup requires exclusive ownership of a disposable tree and a manifest-bound marker.
//! Filesystem inspection is not a defence against a concurrent hostile filesystem writer.
use crate::{
    policy::{Date, nonempty, relative_path},
    preflight::Preflight,
};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::OsStr,
    fs, io,
    path::{Path, PathBuf},
};

pub const KINDS: [&str; 6] = ["active", "native", "npm", "model", "continuity", "rollback"];
pub const FIXTURE_MARKER: &str = ".retention-fixture";
const CONTROLS: [&str; 3] = [
    FIXTURE_MARKER,
    "xtask/prerequisites.json",
    "xtask/quarantine.json",
];
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Finding {
    pub code: &'static str,
    pub subject: String,
    pub detail: String,
}
fn finding(code: &'static str, subject: impl Into<String>, detail: impl Into<String>) -> Finding {
    Finding {
        code,
        subject: subject.into(),
        detail: detail.into(),
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    schema: u32,
    preserve: Vec<String>,
    closures: Vec<Closure>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Closure {
    id: String,
    kind: String,
    members: Vec<Member>,
    #[serde(default)]
    requires: Vec<String>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Member {
    path: String,
    sha256: Option<String>,
}
pub struct Retention {
    sha256: String,
    closures: BTreeMap<String, Closure>,
    protected: BTreeSet<String>,
}
fn literal_path(path: &str) -> Result<(), String> {
    relative_path(path)?;
    // Path::components normalizes interior dots, repeated separators and trailing slashes.
    // Refuse those spellings rather than letting them bypass duplicate/ancestry checks.
    if path
        .split('/')
        .any(|p| p.is_empty() || p == "." || p == "..")
        || path.contains(['[', ']'])
    {
        return Err("retention path must use canonical literal components".into());
    }
    Ok(())
}
impl Retention {
    pub fn parse(text: &str) -> Result<Self, String> {
        let manifest: Manifest =
            serde_json::from_str(text).map_err(|e| format!("RETENTION: {e}"))?;
        if manifest.schema != 1 || manifest.preserve.is_empty() {
            return Err("RETENTION: schema must be 1 and preserve must not be empty".into());
        }
        let mut closures = BTreeMap::new();
        for closure in manifest.closures {
            nonempty(&closure.id, "closure id")?;
            if !KINDS.contains(&closure.kind.as_str()) {
                return Err(format!("RETENTION: unknown kind {}", closure.kind));
            }
            if closure.members.is_empty() {
                return Err(format!("RETENTION: no members in {}", closure.id));
            }
            let mut paths = BTreeSet::new();
            for member in &closure.members {
                literal_path(&member.path)?;
                if !paths.insert(&member.path) {
                    return Err(format!("RETENTION: duplicate member {}", member.path));
                }
                if let Some(sha) = &member.sha256
                    && (sha.len() != 64
                        || !sha
                            .bytes()
                            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
                {
                    return Err(format!("RETENTION: invalid sha256 for {}", member.path));
                }
            }
            if closures.insert(closure.id.clone(), closure).is_some() {
                return Err("RETENTION: duplicate id".into());
            }
        }
        for closure in closures.values() {
            for dependency in &closure.requires {
                if !closures.contains_key(dependency) {
                    return Err(format!("RETENTION: unknown requires {dependency}"));
                }
            }
        }
        let mut pending = manifest.preserve;
        let mut protected = BTreeSet::new();
        while let Some(id) = pending.pop() {
            let closure = closures
                .get(&id)
                .ok_or_else(|| format!("RETENTION: unknown preserve {id}"))?;
            if protected.insert(id) {
                pending.extend(closure.requires.iter().cloned());
            }
        }
        for kind in KINDS {
            if !protected.iter().any(|id| closures[id].kind == kind) {
                return Err(format!("RETENTION: unprotected kind {kind}"));
            }
        }
        Ok(Self {
            sha256: format!("{:x}", Sha256::digest(text.as_bytes())),
            closures,
            protected,
        })
    }
    pub fn manifest_sha256(&self) -> &str {
        &self.sha256
    }
    pub fn protected_kinds(&self) -> Vec<String> {
        self.protected
            .iter()
            .map(|id| self.closures[id].kind.clone())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }
    pub fn protected_ids(&self) -> Vec<String> {
        self.protected.iter().cloned().collect()
    }
    fn members(&self) -> impl Iterator<Item = (&str, &Member)> {
        self.protected.iter().flat_map(|id| {
            self.closures[id]
                .members
                .iter()
                .map(move |member| (id.as_str(), member))
        })
    }
}
fn error(e: impl std::fmt::Display) -> String {
    e.to_string()
}
fn root_check(root: &Path) -> Result<(), String> {
    if !root.is_absolute() || root.parent().is_none() {
        return Err("RETENTION: root must be an absolute non-filesystem-root directory".into());
    }
    // Include ancestors: a symlink at an intermediate component is not a safe fixture root.
    for path in root.ancestors() {
        let metadata = fs::symlink_metadata(path).map_err(error)?;
        if !metadata.is_dir() {
            return Err(format!(
                "RETENTION: root ancestor is not a real directory: {}",
                path.display()
            ));
        }
    }
    Ok(())
}
fn member_path(root: &Path, relative: &str) -> Result<PathBuf, String> {
    literal_path(relative)?;
    let mut path = root.to_path_buf();
    for part in Path::new(relative) {
        path.push(part);
        let metadata = fs::symlink_metadata(&path).map_err(error)?;
        if metadata.file_type().is_symlink() || (!metadata.is_file() && !metadata.is_dir()) {
            return Err(format!(
                "not a regular file or directory: {}",
                path.display()
            ));
        }
    }
    Ok(path)
}
// Returns logical bytes and file count; never follows links, even nested ones.
fn tree_size(path: &Path) -> Result<(u64, u64), String> {
    let metadata = fs::symlink_metadata(path).map_err(error)?;
    if metadata.is_file() {
        return Ok((metadata.len(), 1));
    }
    if !metadata.is_dir() {
        return Err(format!("not a regular tree: {}", path.display()));
    }
    let mut entries = fs::read_dir(path)
        .map_err(error)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(error)?;
    entries.sort_by_key(|entry| entry.file_name());
    let (mut bytes, mut files) = (0u64, 0u64);
    for entry in entries {
        let (b, f) = tree_size(&entry.path())?;
        bytes = bytes
            .checked_add(b)
            .ok_or("retention byte count overflow")?;
        files = files
            .checked_add(f)
            .ok_or("retention file count overflow")?;
    }
    Ok((bytes, files))
}
fn file_sha(path: &Path) -> Result<String, String> {
    let mut reader = fs::File::open(path).map_err(error)?;
    let mut hash = Sha256::new();
    // io::copy's bounded internal buffer avoids reading models into memory.
    io::copy(&mut reader, &mut hash).map_err(error)?;
    Ok(format!("{:x}", hash.finalize()))
}
fn contains(parent: &str, child: &str) -> bool {
    // Conservatively protect case aliases on the default macOS case-insensitive filesystem.
    Path::new(&child.to_lowercase()).starts_with(parent.to_lowercase())
}
#[derive(Debug, Default)]
pub struct Plan {
    pub deletable: Vec<(String, u64)>,
    pub findings: Vec<Finding>,
}
pub fn plan(r: &Retention, root: &Path, candidates: &[String]) -> Plan {
    let mut out = Plan::default();
    if let Err(e) = root_check(root) {
        out.findings
            .push(finding("root_invalid", root.display().to_string(), e));
        return out;
    }
    let mut seen = BTreeSet::<String>::new();
    for candidate in candidates {
        if let Err(e) = literal_path(candidate) {
            out.findings
                .push(finding("candidate_path_invalid", candidate, e));
            continue;
        }
        if !seen.insert(candidate.to_lowercase()) {
            out.findings.push(finding(
                "candidate_duplicate",
                candidate,
                "candidate repeated",
            ));
            continue;
        }
        if seen.iter().any(|other| {
            other != &candidate.to_lowercase()
                && (contains(other, candidate) || contains(candidate, other))
        }) {
            out.findings.push(finding(
                "candidate_overlap",
                candidate,
                "overlapping candidates",
            ));
            continue;
        }
        let before = out.findings.len();
        for (id, member) in r.members() {
            if contains(candidate, &member.path) {
                out.findings.push(finding(
                    "candidate_contains_member",
                    candidate,
                    format!("closure {id}: {}", member.path),
                ));
            } else if contains(&member.path, candidate) {
                out.findings.push(finding(
                    "candidate_inside_member",
                    candidate,
                    format!("closure {id}: {}", member.path),
                ));
            }
        }
        if CONTROLS
            .iter()
            .any(|p| contains(candidate, p) || contains(p, candidate))
        {
            out.findings.push(finding(
                "candidate_control_file",
                candidate,
                "fixture marker/preflight input protected",
            ));
        }
        if out.findings.len() != before {
            continue;
        }
        match member_path(root, candidate).and_then(|p| tree_size(&p)) {
            Ok((bytes, _)) => out.deletable.push((candidate.clone(), bytes)),
            Err(e) => out
                .findings
                .push(finding("candidate_unreadable", candidate, e)),
        }
    }
    out
}
pub fn verify(r: &Retention, root: &Path) -> Vec<Finding> {
    let mut out = Vec::new();
    if let Err(e) = root_check(root) {
        return vec![finding("root_invalid", root.display().to_string(), e)];
    }
    for (id, member) in r.members() {
        let subject = format!("{id}:{}", member.path);
        let checked = member_path(root, &member.path).and_then(|path| {
            let (_, files) = tree_size(&path)?;
            if files == 0 {
                return Err("protected tree contains no files".into());
            }
            Ok(path)
        });
        match checked {
            Err(e) => out.push(finding("closure_member_missing", subject, e)),
            Ok(path) => {
                if let Some(expected) = &member.sha256 {
                    if !path.is_file() {
                        out.push(finding(
                            "closure_member_not_a_file",
                            subject,
                            "sha256 pin requires a regular file",
                        ));
                    } else {
                        match file_sha(&path) {
                            Ok(actual) if actual == *expected => {}
                            Ok(_) => out.push(finding(
                                "closure_member_changed",
                                subject,
                                "sha256 differs",
                            )),
                            Err(e) => out.push(finding("closure_member_missing", subject, e)),
                        }
                    }
                }
            }
        }
    }
    out
}
#[derive(Debug, Default)]
pub struct CleanupReport {
    pub removed: Vec<(String, u64)>,
    pub findings: Vec<Finding>,
    pub preflight_before: Vec<String>,
    pub preflight_after: Vec<String>,
}
fn read_text(path: &Path) -> Result<String, String> {
    fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))
}
fn preflight(root: &Path) -> Result<Vec<String>, String> {
    let prerequisites = member_path(root, CONTROLS[1])?;
    let quarantine = member_path(root, CONTROLS[2])?;
    let pf = Preflight::parse(
        &read_text(&prerequisites)?,
        &read_text(&quarantine)?,
        Date::today()?,
    )?;
    // Hermetic readiness: no ambient PATH/env can hide a removed fixture dependency.
    Ok(pf
        .evaluate(root, OsStr::new(""), &BTreeMap::new(), &[])?
        .iter()
        .map(|s| s.render())
        .collect())
}
pub fn cleanup(
    r: &Retention,
    root: &Path,
    candidates: &[String],
    compare_preflight: bool,
) -> Result<CleanupReport, String> {
    root_check(root)?;
    let marker = member_path(root, FIXTURE_MARKER).map_err(|e| format!("fixture marker: {e}"))?;
    if read_text(&marker)?.trim() != r.manifest_sha256() {
        return Err("fixture marker sha256 differs from manifest".into());
    }
    let mut report = CleanupReport {
        findings: verify(r, root),
        ..CleanupReport::default()
    };
    let planned = plan(r, root, candidates);
    report.findings.extend(planned.findings);
    if !report.findings.is_empty() {
        return Ok(report);
    }
    if compare_preflight {
        report.preflight_before = preflight(root)?;
        if report
            .preflight_before
            .iter()
            .any(|s| !s.ends_with(": RUN"))
        {
            report.findings.push(finding(
                "preflight_blocked",
                "before",
                "offline preflight must pass before cleanup",
            ));
            return Ok(report);
        }
    }
    for (candidate, bytes) in planned.deletable {
        let path = member_path(root, &candidate)?;
        if path.is_dir() {
            fs::remove_dir_all(&path).map_err(error)?;
        } else {
            fs::remove_file(&path).map_err(error)?;
        }
        report.removed.push((candidate, bytes));
    }
    report.findings.extend(verify(r, root));
    if compare_preflight {
        report.preflight_after = preflight(root)?;
        if report.preflight_after != report.preflight_before {
            report.findings.push(finding(
                "preflight_changed",
                "after",
                "offline preflight differs after cleanup",
            ));
        }
    }
    Ok(report)
}
const USAGE: &str = "USAGE: xtask retention plan|verify|cleanup --manifest FILE --root DIR [--candidates FILE] [--preflight]\ncleanup is ONLY for an exclusively owned disposable fixture with a manifest-bound .retention-fixture marker";
pub fn cli(args: &[String]) -> Result<u8, String> {
    let mode = args.first().ok_or(USAGE)?;
    if !["plan", "verify", "cleanup"].contains(&mode.as_str()) {
        return Err(USAGE.into());
    }
    let mut values = BTreeMap::new();
    let mut compare = false;
    let mut rest = args[1..].iter();
    while let Some(key) = rest.next() {
        if key == "--preflight" && !compare {
            compare = true;
        } else if ["--manifest", "--root", "--candidates"].contains(&key.as_str())
            && !values.contains_key(key)
        {
            values.insert(key.clone(), rest.next().ok_or(USAGE)?.clone());
        } else {
            return Err(USAGE.into());
        }
    }
    if compare && mode != "cleanup" {
        return Err(USAGE.into());
    }
    let manifest = Retention::parse(&read_text(Path::new(
        values.get("--manifest").ok_or(USAGE)?,
    ))?)?;
    let root = Path::new(values.get("--root").ok_or(USAGE)?);
    let candidates = match values.get("--candidates") {
        Some(path) => read_text(Path::new(path))?
            .lines()
            .filter(|line| !line.trim().is_empty() && !line.trim_start().starts_with('#'))
            .map(str::to_string)
            .collect::<Vec<_>>(),
        None if mode == "verify" => Vec::new(),
        None => return Err(USAGE.into()),
    };
    let findings = match mode.as_str() {
        "plan" => {
            let p = plan(&manifest, root, &candidates);
            for (path, bytes) in &p.deletable {
                println!("DELETE-CANDIDATE {path} ({bytes} B)");
            }
            println!(
                "{} may go, {} refused (dry run, nothing touched)",
                p.deletable.len(),
                p.findings.len()
            );
            p.findings
        }
        "verify" => {
            let findings = verify(&manifest, root);
            println!(
                "{} protected closures, {} findings",
                manifest.protected.len(),
                findings.len()
            );
            findings
        }
        _ => {
            let report = cleanup(&manifest, root, &candidates, compare)?;
            for (path, bytes) in &report.removed {
                println!("REMOVED {path} ({bytes} B)");
            }
            if !report.preflight_after.is_empty() {
                println!("preflight compared before/after");
            }
            for line in &report.preflight_before {
                println!("BEFORE {line}");
            }
            for line in &report.preflight_after {
                println!("AFTER {line}");
            }
            report.findings
        }
    };
    for f in &findings {
        println!("FINDING {} {}: {}", f.code, f.subject, f.detail);
    }
    Ok(u8::from(!findings.is_empty()))
}
