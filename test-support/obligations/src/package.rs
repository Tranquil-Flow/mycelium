// SPDX-License-Identifier: AGPL-3.0-only
//! Exact released-package membership, separate from the provisional source census.
//! No filesystem, archive execution, wildcard expansion or historical runtime I/O.
use crate::{FilePin, Registry, Violation, canonical_path, hex, issue};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Deserialize, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum EntryKind {
    File,
    Directory,
    Symlink,
    ArchiveMember,
    Module,
    Package,
    Namespace,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PackageEntry {
    pub path: String,
    pub kind: EntryKind,
    pub size_bytes: u64,
    pub sha256: String,
    pub scope: String,
    pub link_target: Option<String>,
    pub resolved_target: Option<String>,
    pub container: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PackageCensus {
    pub schema: String,
    pub spdx_license_identifier: String,
    pub source_sha: String,
    pub archive: FilePin,
    /// Accepted-source objects acquired with read-only git show at source_sha.
    /// These authenticate citations/crosswalks, not Python bytecode equivalence.
    pub source_objects: Vec<FilePin>,
    pub entries: Vec<PackageEntry>,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum PackageDisposition {
    Rust,
    NativeReused,
    WebRetained,
    Retire,
    DataRetained,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FamilyRule {
    /// A shape constraint, NEVER an instruction to admit unenumerated children.
    pub prefix: String,
    pub kinds: Vec<EntryKind>,
    pub suffixes: Vec<String>,
}

impl FamilyRule {
    pub fn matches(&self, entry: &PackageEntry) -> bool {
        entry.path.starts_with(&self.prefix)
            && self.kinds.contains(&entry.kind)
            && (self.suffixes.is_empty() || self.suffixes.iter().any(|s| entry.path.ends_with(s)))
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PackageMember {
    pub path: String,
    pub size_bytes: u64,
    pub sha256: String,
    pub link_target: Option<String>,
    pub source_path: Option<String>,
    pub source_sha256: Option<String>,
    pub obligation_id: Option<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PackageFamily {
    pub id: String,
    pub disposition: PackageDisposition,
    pub destination: String,
    pub component: String,
    pub reason: String,
    pub reviewer: String,
    pub citation: String,
    pub evidence: Vec<FilePin>,
    pub rule: FamilyRule,
    pub members: Vec<PackageMember>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct PackageRegistry {
    pub schema: String,
    pub spdx_license_identifier: String,
    pub source_sha: String,
    pub archive_sha256: String,
    pub review_scope: String,
    pub families: Vec<PackageFamily>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct PackageCoverage {
    pub zip_entries: usize,
    pub directories: usize,
    pub dispositioned_zip_entries: usize,
    pub embedded_entries: usize,
    pub families: usize,
    /// Success has zero gaps; errors carry each exact unexplained path instead.
    pub unexplained_paths: usize,
}

/// Validate *every* census member, not a claimed total or a prefix-derived sample.
/// Returning success means inventory coverage only, not replacement/release parity.
pub fn validate_package(
    registry: &PackageRegistry,
    census: &PackageCensus,
    source: &Registry,
) -> Result<PackageCoverage, Vec<Violation>> {
    let mut errors = Vec::new();
    if census.schema != "mycelium.package-census.v1"
        || registry.schema != "mycelium.package-dispositions.v1"
        || census.spdx_license_identifier != "AGPL-3.0-only"
        || registry.spdx_license_identifier != "AGPL-3.0-only"
    {
        issue(&mut errors, "package_schema", "package");
    }
    if !hex(&census.source_sha, 40)
        || registry.source_sha != census.source_sha
        || !hex(&census.archive.sha256, 64)
        || registry.archive_sha256 != census.archive.sha256
        || census.archive.size_bytes == 0
        || census.archive.path.trim().is_empty()
    {
        issue(&mut errors, "package_identity", "package");
    }
    if registry.review_scope.trim().is_empty() {
        issue(&mut errors, "package_review_scope", "package");
    }
    let mut entries = BTreeMap::new();
    for entry in &census.entries {
        if entries.insert(entry.path.as_str(), entry).is_some() {
            issue(&mut errors, "duplicate_package_path", &entry.path);
        }
        let path = if entry.kind == EntryKind::Directory {
            entry.path.strip_suffix('/').unwrap_or("")
        } else {
            &entry.path
        };
        if !canonical_path(path) || path.contains(['*', '?', '\0']) {
            issue(&mut errors, "invalid_package_path", &entry.path);
        }
        if !hex(&entry.sha256, 64) {
            issue(&mut errors, "invalid_package_pin", &entry.path);
        }
        if entry.kind == EntryKind::Directory
            && (entry.size_bytes != 0
                || entry.sha256
                    != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        {
            issue(&mut errors, "nonempty_package_directory", &entry.path);
        }
        if entry.scope == "zip" {
            if entry.container.is_some()
                || entry.path.contains("!/")
                || !matches!(
                    entry.kind,
                    EntryKind::File | EntryKind::Directory | EntryKind::Symlink
                )
            {
                issue(&mut errors, "invalid_package_scope", &entry.path);
            }
        } else if !matches!(entry.scope.as_str(), "carchive" | "pyz" | "base-library")
            || entry.container.is_none()
        {
            issue(&mut errors, "invalid_package_scope", &entry.path);
        }
        if entry.kind != EntryKind::Symlink
            && (entry.link_target.is_some() || entry.resolved_target.is_some())
        {
            issue(&mut errors, "unexpected_link_metadata", &entry.path);
        }
    }
    if entries.is_empty() || !census.entries.iter().any(|e| e.scope == "zip") {
        issue(&mut errors, "empty_package_census", "package");
    }
    for entry in &census.entries {
        if let Some(container) = &entry.container
            && !entries.get(container.as_str()).is_some_and(|parent| {
                matches!(parent.kind, EntryKind::File | EntryKind::ArchiveMember)
                    && entry.path.starts_with(&format!("{container}!/"))
                    && container.len() < entry.path.len()
            })
        {
            issue(&mut errors, "invalid_package_container", &entry.path);
        }
        if entry.kind == EntryKind::Symlink {
            let lexical = entry
                .link_target
                .as_deref()
                .and_then(|target| normalize_link(&entry.path, target));
            if lexical.as_ref() != entry.resolved_target.as_ref()
                || lexical
                    .as_deref()
                    .and_then(|p| resolve_link(p, &entries))
                    .is_none()
            {
                issue(&mut errors, "invalid_package_link", &entry.path);
            }
        }
    }
    let mut source_objects = BTreeMap::new();
    for pin in &census.source_objects {
        if !canonical_path(&pin.path)
            || pin.path.contains(['*', '?', '\0', '!'])
            || !hex(&pin.sha256, 64)
            || source_objects.insert(pin.path.as_str(), pin).is_some()
        {
            issue(&mut errors, "invalid_package_source_object", &pin.path);
        }
    }
    let destinations: BTreeSet<_> = source.destinations.iter().map(|d| d.id.as_str()).collect();
    let source_paths: BTreeMap<_, _> = source.paths.iter().map(|p| (p.path.as_str(), p)).collect();
    let obligations: BTreeMap<_, _> = source
        .obligations
        .iter()
        .map(|o| (o.id.as_str(), o))
        .collect();
    let mut ids = BTreeSet::new();
    let mut covered = BTreeSet::new();
    for family in &registry.families {
        if !ids.insert(family.id.as_str()) {
            issue(&mut errors, "duplicate_package_family", &family.id);
        }
        if [
            &family.id,
            &family.component,
            &family.reason,
            &family.reviewer,
            &family.citation,
        ]
        .iter()
        .any(|s| s.trim().is_empty())
            || family.evidence.is_empty()
            || family
                .evidence
                .iter()
                .any(|p| p.path.trim().is_empty() || !hex(&p.sha256, 64))
        {
            issue(&mut errors, "package_review_missing", &family.id);
        }
        for pin in &family.evidence {
            let package_pin = entries.get(pin.path.as_str()).is_some_and(|entry| {
                entry.sha256 == pin.sha256 && entry.size_bytes == pin.size_bytes
            });
            let source_pin = source_objects.get(pin.path.as_str()).is_some_and(|entry| {
                entry.sha256 == pin.sha256 && entry.size_bytes == pin.size_bytes
            });
            if !package_pin && !source_pin {
                issue(&mut errors, "package_evidence_identity", &pin.path);
            }
        }
        if !destinations.contains(family.destination.as_str()) {
            issue(&mut errors, "unknown_package_destination", &family.id);
        }
        if family.members.is_empty()
            || family.rule.prefix.is_empty()
            || family.rule.prefix.contains(['*', '?', '\0'])
            || !canonical_path(family.rule.prefix.trim_end_matches('/'))
            || family.rule.kinds.is_empty()
            || family.rule.kinds.contains(&EntryKind::Directory)
            || family
                .rule
                .suffixes
                .iter()
                .any(|s| s.is_empty() || s.contains(['*', '?']))
        {
            issue(&mut errors, "invalid_package_family_rule", &family.id);
        }
        for member in &family.members {
            if !covered.insert(member.path.as_str()) {
                issue(&mut errors, "duplicate_package_disposition", &member.path);
            }
            let Some(entry) = entries.get(member.path.as_str()) else {
                issue(&mut errors, "unlisted_package_disposition", &member.path);
                continue;
            };
            if entry.kind == EntryKind::Directory || !family.rule.matches(entry) {
                issue(&mut errors, "package_family_mismatch", &member.path);
            }
            if member.sha256 != entry.sha256
                || member.size_bytes != entry.size_bytes
                || member.link_target != entry.link_target
            {
                issue(&mut errors, "package_pin_mismatch", &member.path);
            }
            // A source crosswalk is per member: no family destination may overwrite it.
            match (
                &member.source_path,
                &member.source_sha256,
                &member.obligation_id,
            ) {
                (Some(path), Some(digest), Some(obligation)) => {
                    if !hex(digest, 64)
                        || source_objects
                            .get(path.as_str())
                            .is_none_or(|pin| pin.sha256 != *digest)
                        || source_paths
                            .get(path.as_str())
                            .is_none_or(|r| r.obligation_id != *obligation)
                        || obligations
                            .get(obligation.as_str())
                            .is_none_or(|o| o.destination != family.destination)
                        || (entry.scope == "zip" && *digest != entry.sha256)
                    {
                        issue(&mut errors, "package_source_crosswalk", &member.path);
                    }
                }
                (None, None, None) => {}
                _ => issue(&mut errors, "package_source_crosswalk", &member.path),
            }
            if family.disposition == PackageDisposition::WebRetained
                && (member.source_path.is_none()
                    || entry.scope != "zip"
                    || entry.kind != EntryKind::File)
            {
                issue(&mut errors, "package_web_identity", &member.path);
            }
            if family.disposition == PackageDisposition::NativeReused
                && (entry.kind != EntryKind::File || entry.scope != "zip")
            {
                issue(&mut errors, "package_native_identity", &member.path);
            }
        }
    }
    for entry in &census.entries {
        if entry.kind != EntryKind::Directory && !covered.contains(entry.path.as_str()) {
            issue(&mut errors, "unexplained_package_path", &entry.path);
        }
    }
    if !errors.is_empty() {
        return Err(errors);
    }
    let zip_entries = census.entries.iter().filter(|e| e.scope == "zip").count();
    let directories = census
        .entries
        .iter()
        .filter(|e| e.scope == "zip" && e.kind == EntryKind::Directory)
        .count();
    Ok(PackageCoverage {
        zip_entries,
        directories,
        dispositioned_zip_entries: zip_entries - directories,
        embedded_entries: census.entries.len() - zip_entries,
        families: registry.families.len(),
        unexplained_paths: 0,
    })
}

fn normalize_link(path: &str, target: &str) -> Option<String> {
    if target.is_empty() || target.starts_with('/') || target.contains(['\\', '\0', '!']) {
        return None;
    }
    let (parent, _) = path.rsplit_once('/')?;
    let mut parts: Vec<_> = parent.split('/').collect();
    for part in target.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                parts.pop()?;
            }
            value => parts.push(value),
        }
    }
    let normalized = parts.join("/");
    // Links may not escape the outer package root.
    (parts.first() == path.split('/').next().as_ref()).then_some(normalized)
}

fn resolve_link(path: &str, entries: &BTreeMap<&str, &PackageEntry>) -> Option<String> {
    let mut path = path.to_owned();
    let mut visited = BTreeSet::new();
    loop {
        if !visited.insert(path.clone()) {
            return None;
        }
        let parts: Vec<_> = path.split('/').collect();
        let mut redirected = None;
        for end in 1..=parts.len() {
            let prefix = parts[..end].join("/");
            if let Some(entry) = entries.get(prefix.as_str())
                && entry.kind == EntryKind::Symlink
            {
                let target = normalize_link(&prefix, entry.link_target.as_deref()?)?;
                redirected = Some(if end == parts.len() {
                    target
                } else {
                    format!("{target}/{}", parts[end..].join("/"))
                });
                break;
            }
        }
        match redirected {
            Some(next) if visited.len() <= entries.len() => path = next,
            Some(_) => return None,
            None => {
                return (entries.contains_key(path.as_str())
                    || entries.contains_key(format!("{path}/").as_str()))
                .then_some(path);
            }
        }
    }
}

pub fn checked_in_package() -> Result<(PackageRegistry, PackageCensus), serde_json::Error> {
    Ok((
        serde_json::from_str(include_str!("../data/package-dispositions.json"))?,
        serde_json::from_str(include_str!("../data/package-census.json"))?,
    ))
}
