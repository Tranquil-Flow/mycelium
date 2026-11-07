// SPDX-License-Identifier: AGPL-3.0-only
//! Data-only migration obligations. Validation never reads the historical worktree.
use serde::{Deserialize, Serialize};

pub mod a6;
pub mod contracts;
pub mod package;
pub mod static_source;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Destination {
    pub id: String,
    pub plan: String,
    pub lane: String,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(tag = "kind")]
pub enum Disposition {
    #[serde(rename = "rust")]
    Rust { destination: String },
    #[serde(rename = "external oracle only")]
    External { reason: String, replacement: String },
    #[serde(rename = "retire")]
    Retire {
        reason: String,
        replacement: String,
        reviewer: String,
    },
}
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct PathDisposition {
    pub id: String,
    pub path: String,
    pub disposition: Disposition,
    pub obligation_id: String,
    pub citation: String,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Tier {
    Unit,
    Property,
    Sim,
    Journey,
    Device,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Obligation {
    pub id: String,
    pub description: String,
    pub destination: String,
    pub tier: Tier,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Crosswalk {
    pub id: String,
    pub source: String,
    pub destination: String,
    pub obligation_id: String,
    pub citation: String,
    /// Source test names, pins, tier and migration notes remain available to consumers.
    #[serde(flatten)]
    pub evidence: std::collections::BTreeMap<String, serde_json::Value>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Escape {
    pub id: String,
    pub historical_target: String,
    pub recipe: String,
    pub observation: String,
    pub owners: String,
    pub citation: String,
    pub status: String,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Cell {
    pub id: String,
    pub scope: String,
    pub status: String,
    pub citations: Vec<String>,
    pub unknowns: Vec<String>,
    pub values: serde_json::Value,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Registry {
    pub source_sha: String,
    pub destinations: Vec<Destination>,
    pub paths: Vec<PathDisposition>,
    pub obligations: Vec<Obligation>,
    pub contracts: Vec<Crosswalk>,
    pub test_groups: Vec<Crosswalk>,
    pub escapes: Vec<Escape>,
    pub supported_cells: Vec<Cell>,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct FilePin {
    pub path: String,
    pub sha256: String,
    pub size_bytes: u64,
}
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Snapshot {
    pub source_sha: String,
    pub files: Vec<FilePin>,
    pub packaged_source_paths: Vec<String>,
    pub a6_paths: Vec<String>,
    pub test_paths: Vec<String>,
    pub contract_protocols: Vec<String>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Violation {
    pub code: &'static str,
    pub subject: String,
}

/// Check a registry against a supplied, checked-in source census.
/// Errors are accumulated in stable order so callers can report every gap.
pub fn validate(registry: &Registry, snapshot: &Snapshot) -> Result<(), Vec<Violation>> {
    use std::collections::BTreeSet;
    let mut errors = Vec::new();
    let mut ids = BTreeSet::new();
    let all_ids = registry
        .destinations
        .iter()
        .map(|r| &r.id)
        .chain(registry.paths.iter().map(|r| &r.id))
        .chain(registry.obligations.iter().map(|r| &r.id))
        .chain(registry.contracts.iter().map(|r| &r.id))
        .chain(registry.test_groups.iter().map(|r| &r.id))
        .chain(registry.escapes.iter().map(|r| &r.id))
        .chain(registry.supported_cells.iter().map(|r| &r.id));
    for id in all_ids {
        if id.trim().is_empty() {
            issue(&mut errors, "empty_id", id);
        }
        if !ids.insert(id.as_str()) {
            issue(&mut errors, "duplicate_id", id);
        }
    }
    if registry.source_sha != snapshot.source_sha {
        issue(&mut errors, "source_mismatch", &snapshot.source_sha);
    }
    if !hex(&snapshot.source_sha, 40) {
        issue(&mut errors, "invalid_source_sha", &snapshot.source_sha);
    }
    let destinations: BTreeSet<_> = registry
        .destinations
        .iter()
        .map(|r| r.id.as_str())
        .collect();
    let obligations: BTreeSet<_> = registry.obligations.iter().map(|r| r.id.as_str()).collect();
    for d in &registry.destinations {
        if !d.id.contains("::")
            || d.id.split("::").any(str::is_empty)
            || d.plan.trim().is_empty()
            || d.lane.trim().is_empty()
        {
            issue(&mut errors, "invalid_destination_declaration", &d.id);
        }
    }
    let mut files = BTreeSet::new();
    for f in &snapshot.files {
        if !files.insert(f.path.as_str()) {
            issue(&mut errors, "duplicate_snapshot_path", &f.path);
        }
        if !canonical_path(&f.path) {
            issue(&mut errors, "invalid_path", &f.path);
        }
        if !hex(&f.sha256, 64) {
            issue(&mut errors, "invalid_file_sha256", &f.path);
        }
    }
    if files.is_empty() {
        issue(&mut errors, "empty_snapshot", "files");
    }
    for group in [
        &snapshot.packaged_source_paths,
        &snapshot.a6_paths,
        &snapshot.test_paths,
    ] {
        let mut seen = BTreeSet::new();
        for path in group {
            if !seen.insert(path) {
                issue(&mut errors, "duplicate_scope_path", path);
            }
            if !files.contains(path.as_str()) {
                issue(&mut errors, "scope_path_not_pinned", path);
            }
        }
    }
    let check_destination = |name: &str, errors: &mut Vec<Violation>| {
        if !destinations.contains(name) {
            issue(errors, "unknown_destination", name);
        }
    };
    let check_obligation = |name: &str, errors: &mut Vec<Violation>| {
        if !obligations.contains(name) {
            issue(errors, "unknown_obligation", name);
        }
    };
    for obligation in &registry.obligations {
        check_destination(&obligation.destination, &mut errors);
        if obligation.description.trim().is_empty() {
            issue(&mut errors, "empty_obligation", &obligation.id);
        }
    }
    let mut paths = BTreeSet::new();
    for row in &registry.paths {
        if !paths.insert(row.path.as_str()) {
            issue(&mut errors, "duplicate_path", &row.path);
        }
        if !files.contains(row.path.as_str()) {
            issue(&mut errors, "unlisted_path", &row.path);
        }
        if !canonical_path(&row.path) {
            issue(&mut errors, "invalid_path", &row.path);
        }
        if row.citation.trim().is_empty() {
            issue(&mut errors, "missing_citation", &row.id);
        }
        check_obligation(&row.obligation_id, &mut errors);
        match &row.disposition {
            Disposition::Rust { destination } => {
                check_destination(destination, &mut errors);
                if let Some(obligation) = registry
                    .obligations
                    .iter()
                    .find(|o| o.id == row.obligation_id)
                    && obligation.destination != *destination
                {
                    issue(&mut errors, "obligation_destination_mismatch", &row.id);
                }
            }
            Disposition::External {
                reason,
                replacement,
            } => {
                if reason.trim().is_empty() {
                    issue(&mut errors, "external_reason_missing", &row.id);
                }
                check_obligation(replacement, &mut errors);
            }
            Disposition::Retire {
                reason,
                replacement,
                reviewer,
            } => {
                if reason.trim().is_empty() {
                    issue(&mut errors, "retirement_reason_missing", &row.id);
                }
                if replacement.trim().is_empty() {
                    issue(&mut errors, "retirement_replacement_missing", &row.id);
                } else {
                    check_obligation(replacement, &mut errors);
                }
                if reviewer.trim().is_empty() {
                    issue(&mut errors, "retirement_reviewer_missing", &row.id);
                }
            }
        }
    }
    for path in files.difference(&paths) {
        issue(&mut errors, "unmapped_path", path);
    }
    for (rows, expected, missing_code) in [
        (
            &registry.contracts,
            &snapshot.contract_protocols,
            "unmapped_contract",
        ),
        (&registry.test_groups, &snapshot.test_paths, "unmapped_test"),
    ] {
        let mut seen = BTreeSet::new();
        let expected_set: BTreeSet<_> = expected.iter().map(String::as_str).collect();
        if expected_set.len() != expected.len() {
            issue(&mut errors, "duplicate_expected_crosswalk", missing_code);
        }
        for row in rows {
            if !seen.insert(row.source.as_str()) {
                issue(&mut errors, "duplicate_crosswalk_source", &row.source);
            }
            if !expected_set.contains(row.source.as_str()) {
                issue(&mut errors, "unlisted_crosswalk", &row.source);
            }
            check_destination(&row.destination, &mut errors);
            check_obligation(&row.obligation_id, &mut errors);
            if row.citation.trim().is_empty() {
                issue(&mut errors, "missing_citation", &row.id);
            }
            if let Some(obligation) = registry
                .obligations
                .iter()
                .find(|o| o.id == row.obligation_id)
                && obligation.destination != row.destination
            {
                issue(&mut errors, "obligation_destination_mismatch", &row.id);
            }
        }
        for missing in expected_set.difference(&seen) {
            issue(&mut errors, missing_code, missing);
        }
    }
    let mandatory = [
        "F1", "F2", "F3", "F4", "F5", "F13", "F14", "F15", "F15b", "F16",
    ];
    let escapes: BTreeSet<_> = registry.escapes.iter().map(|e| e.id.as_str()).collect();
    for id in mandatory {
        if !escapes.contains(id) {
            issue(&mut errors, "missing_escape", id);
        }
    }
    for row in &registry.escapes {
        if !mandatory.contains(&row.id.as_str()) {
            issue(&mut errors, "unexpected_escape", &row.id);
        }
        if [
            &row.historical_target,
            &row.recipe,
            &row.observation,
            &row.owners,
            &row.citation,
        ]
        .iter()
        .any(|s| s.trim().is_empty())
        {
            issue(&mut errors, "escape_evidence_missing", &row.id);
        }
        if row.status != "inventory_only" {
            issue(&mut errors, "unsupported_escape_claim", &row.id);
        }
    }
    for cell in &registry.supported_cells {
        if cell.scope.trim().is_empty()
            || cell.status.trim().is_empty()
            || cell.citations.is_empty()
            || cell.citations.iter().any(|s| s.trim().is_empty())
            || !cell.values.is_object()
        {
            issue(&mut errors, "cell_evidence_missing", &cell.id);
        }
    }
    // Only the exact historical source has this embedded manifest authority.
    // Arbitrary caller-supplied snapshots remain structural inventories; the
    // accepted successor uses contracts::validate_binding plus byte verification.
    if snapshot.source_sha == contracts::HISTORICAL_SOURCE {
        match contracts::historical_manifest() {
            Ok(manifest) => errors.extend(contracts::validate_crosswalk(
                &registry.contracts,
                &manifest,
            )),
            Err(_) => issue(
                &mut errors,
                "contract_manifest_invalid",
                &snapshot.source_sha,
            ),
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors)
    }
}

fn issue(errors: &mut Vec<Violation>, code: &'static str, subject: &str) {
    errors.push(Violation {
        code,
        subject: subject.to_owned(),
    });
}
fn hex(value: &str, len: usize) -> bool {
    value.len() == len
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn canonical_path(value: &str) -> bool {
    !value.is_empty()
        && !value.contains('\\')
        && !value.starts_with('/')
        && value
            .split('/')
            .all(|p| !p.is_empty() && p != "." && p != "..")
}

/// Load immutable source data embedded in this crate, with no external worktree I/O.
pub fn checked_in() -> Result<(Registry, Snapshot), serde_json::Error> {
    let snapshot: Snapshot = serde_json::from_str(include_str!("../data/baseline.json"))?;
    let registry = Registry {
        source_sha: snapshot.source_sha.clone(),
        destinations: serde_json::from_str(include_str!("../data/destinations.json"))?,
        paths: serde_json::from_str(include_str!("../data/paths.json"))?,
        obligations: serde_json::from_str(include_str!("../data/obligations.json"))?,
        contracts: serde_json::from_str(include_str!("../data/contracts.json"))?,
        test_groups: serde_json::from_str(include_str!("../data/test-groups.json"))?,
        escapes: serde_json::from_str(include_str!("../data/escapes.json"))?,
        supported_cells: serde_json::from_str(include_str!("../data/supported-cells.json"))?,
    };
    Ok((registry, snapshot))
}
