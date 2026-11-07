// SPDX-License-Identifier: AGPL-3.0-only
//! Offline readiness only: inspect files and PATH, never execute a probe or job.
use crate::policy::{Date, expiry, nonempty, relative_path};
use serde::Deserialize;
use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::{OsStr, OsString},
    fs,
    path::{Path, PathBuf},
};

#[derive(Debug, PartialEq, Eq)]
pub struct GroupStatus {
    pub group: String,
    pub mandatory: bool,
    pub reason: Option<String>,
}
impl GroupStatus {
    pub fn render(&self) -> String {
        match &self.reason {
            Some(reason) => format!("{}: NOT_RUN(BLOCKED: {reason})", self.group),
            None => format!("{}: RUN", self.group),
        }
    }
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    version: u32,
    tools: Vec<Tool>,
    groups: Vec<Group>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Tool {
    name: String,
    detect: Detection,
}
#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum Detection {
    PathExecutable { program: String },
    File { path: String },
    EnvFile { variable: String },
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Group {
    id: String,
    kind: GroupKind,
    mandatory: bool,
    needs: Vec<String>,
    after: Vec<String>,
    command: Vec<String>,
}
#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
enum GroupKind {
    Unit,
    Doctest,
    Custom,
    Native,
    Journey,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Quarantines {
    version: u32,
    entries: Vec<Quarantine>,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Quarantine {
    group: String,
    owner: String,
    reason: String,
    expiry: String,
    replacement_coverage: Vec<String>,
}

pub struct Preflight {
    manifest: Manifest,
    quarantines: Quarantines,
}
impl Preflight {
    pub fn parse(manifest: &str, quarantine: &str, today: Date) -> Result<Self, String> {
        let manifest: Manifest =
            serde_json::from_str(manifest).map_err(|e| format!("PREREQUISITES: {e}"))?;
        let quarantines: Quarantines =
            serde_json::from_str(quarantine).map_err(|e| format!("QUARANTINE: {e}"))?;
        if manifest.version != 1 || quarantines.version != 1 {
            return Err("PREFLIGHT: unsupported manifest version".into());
        }
        if manifest.groups.is_empty() {
            return Err("PREREQUISITES: no test groups".into());
        }
        let mut tools = BTreeSet::new();
        for tool in &manifest.tools {
            nonempty(&tool.name, "tool name")?;
            if !tools.insert(tool.name.as_str()) {
                return Err("PREREQUISITES: duplicate tool".into());
            }
            match &tool.detect {
                Detection::PathExecutable { program } => {
                    nonempty(program, "program")?;
                    if program.contains(['/', '\\']) || program == "." || program == ".." {
                        return Err("PREREQUISITES: PATH program must be a bare name".into());
                    }
                }
                Detection::File { path } => relative_path(path)?,
                Detection::EnvFile { variable } => {
                    nonempty(variable, "environment variable")?;
                    if !variable
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b == b'_')
                    {
                        return Err("PREREQUISITES: invalid environment variable".into());
                    }
                }
            }
        }
        let mut groups = BTreeSet::new();
        for group in &manifest.groups {
            nonempty(&group.id, "group id")?;
            if !groups.insert(group.id.as_str()) {
                return Err("PREREQUISITES: duplicate group".into());
            }
            if group.command.is_empty() {
                return Err("PREREQUISITES: group has no declared command".into());
            }
            for arg in &group.command {
                nonempty(arg, "command argument")?;
            }
            // Parsing the enum makes doctest/custom jobs first-class, not inferred
            // from nextest discovery. Commands are descriptive and never launched.
            match group.kind {
                GroupKind::Unit
                | GroupKind::Doctest
                | GroupKind::Custom
                | GroupKind::Native
                | GroupKind::Journey => {}
            }
            for name in &group.needs {
                if !tools.contains(name.as_str()) {
                    return Err(format!("PREREQUISITES: unknown tool {name}"));
                }
            }
        }
        for group in &manifest.groups {
            for dependency in &group.after {
                if !groups.contains(dependency.as_str()) {
                    return Err(format!(
                        "PREREQUISITES: unknown dependency group {dependency}"
                    ));
                }
            }
        }
        let mut quarantined = BTreeSet::new();
        for entry in &quarantines.entries {
            if !groups.contains(entry.group.as_str()) || !quarantined.insert(entry.group.as_str()) {
                return Err("QUARANTINE: unknown or duplicate group".into());
            }
            nonempty(&entry.owner, "owner")?;
            nonempty(&entry.reason, "reason")?;
            expiry(&entry.expiry, today)?;
            if entry.replacement_coverage.is_empty() {
                return Err("QUARANTINE: replacement coverage is required".into());
            }
            for replacement in &entry.replacement_coverage {
                if replacement == &entry.group || !groups.contains(replacement.as_str()) {
                    return Err("QUARANTINE: invalid replacement coverage group".into());
                }
            }
        }
        for entry in &quarantines.entries {
            if entry
                .replacement_coverage
                .iter()
                .any(|g| quarantined.contains(g.as_str()))
            {
                return Err("QUARANTINE: replacement coverage is also quarantined".into());
            }
        }
        let preflight = Self {
            manifest,
            quarantines,
        };
        preflight.selection(&[])?; // Reject cycles even in unselected groups.
        Ok(preflight)
    }

    fn group(&self, id: &str) -> Result<&Group, String> {
        self.manifest
            .groups
            .iter()
            .find(|group| group.id == id)
            .ok_or_else(|| format!("PREFLIGHT: unknown group {id}"))
    }
    fn expand(
        &self,
        id: &str,
        visiting: &mut BTreeSet<String>,
        selected: &mut BTreeSet<String>,
    ) -> Result<(), String> {
        if selected.contains(id) {
            return Ok(());
        }
        if !visiting.insert(id.into()) {
            return Err(format!("PREREQUISITES: dependency cycle at {id}"));
        }
        for dependency in &self.group(id)?.after {
            self.expand(dependency, visiting, selected)?;
        }
        visiting.remove(id);
        selected.insert(id.into());
        Ok(())
    }
    fn selection(&self, ids: &[String]) -> Result<BTreeSet<String>, String> {
        let mut selected = BTreeSet::new();
        let mut visiting = BTreeSet::new();
        if ids.is_empty() {
            for group in &self.manifest.groups {
                self.expand(&group.id, &mut visiting, &mut selected)?;
            }
        } else {
            for id in ids {
                self.expand(id, &mut visiting, &mut selected)?;
            }
        }
        Ok(selected)
    }
    fn status(
        &self,
        id: &str,
        tools: &BTreeMap<String, Option<String>>,
        cache: &mut BTreeMap<String, GroupStatus>,
    ) -> Result<(), String> {
        if cache.contains_key(id) {
            return Ok(());
        }
        let group = self.group(id)?;
        let mut reasons = Vec::new();
        for name in &group.needs {
            if let Some(reason) = tools
                .get(name)
                .ok_or_else(|| format!("PREFLIGHT: tool missing from detector {name}"))?
            {
                reasons.push(format!("{name}: {reason}"));
            }
        }
        for dependency in &group.after {
            self.status(dependency, tools, cache)?;
            if cache
                .get(dependency)
                .is_some_and(|status| status.reason.is_some())
            {
                reasons.push(format!("dependency {dependency} blocked"));
            }
        }
        if let Some(entry) = self
            .quarantines
            .entries
            .iter()
            .find(|entry| entry.group == id)
        {
            reasons.push(format!(
                "quarantined; owner={}; expiry={}; reason={}; replacement={}",
                entry.owner,
                entry.expiry,
                entry.reason,
                entry.replacement_coverage.join(",")
            ));
        }
        cache.insert(
            id.into(),
            GroupStatus {
                group: id.into(),
                mandatory: group.mandatory,
                reason: if reasons.is_empty() {
                    None
                } else {
                    Some(reasons.join("; "))
                },
            },
        );
        Ok(())
    }
    pub fn evaluate(
        &self,
        root: &Path,
        path: &OsStr,
        environment: &BTreeMap<String, OsString>,
        selected: &[String],
    ) -> Result<Vec<GroupStatus>, String> {
        let selection = self.selection(selected)?;
        let needed = self
            .manifest
            .groups
            .iter()
            .filter(|g| selection.contains(&g.id))
            .flat_map(|g| &g.needs)
            .collect::<BTreeSet<_>>();
        let mut tools = BTreeMap::new();
        for tool in &self.manifest.tools {
            if needed.contains(&tool.name) {
                tools.insert(
                    tool.name.clone(),
                    detect(&tool.detect, root, path, environment),
                );
            }
        }
        let mut statuses = BTreeMap::new();
        for group in &self.manifest.groups {
            if selection.contains(&group.id) {
                self.status(&group.id, &tools, &mut statuses)?;
            }
        }
        let mut ordered = Vec::new();
        for group in &self.manifest.groups {
            if let Some(status) = statuses.remove(&group.id) {
                ordered.push(status);
            }
        }
        Ok(ordered)
    }
}

fn file_status(path: &Path, executable: bool) -> Option<String> {
    match fs::metadata(path) {
        Ok(metadata) if metadata.is_file() => {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                if executable && metadata.permissions().mode() & 0o111 == 0 {
                    return Some("not executable".into());
                }
            }
            #[cfg(not(unix))]
            if executable && path.extension().is_none() {
                return Some("not an executable file".into());
            }
            None
        }
        Ok(_) => Some("not a regular file".into()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Some("missing file".into()),
        Err(error) => Some(format!("file inspection failed ({:?})", error.kind())),
    }
}
fn detect(
    detection: &Detection,
    root: &Path,
    path: &OsStr,
    environment: &BTreeMap<String, OsString>,
) -> Option<String> {
    match detection {
        Detection::File { path } => file_status(&root.join(path), false),
        Detection::EnvFile { variable } => {
            match environment.get(variable).filter(|value| !value.is_empty()) {
                Some(value) => file_status(&PathBuf::from(value), false),
                None => Some(format!("environment {variable} is unset")),
            }
        }
        Detection::PathExecutable { program } => {
            let found = std::env::split_paths(path)
                .filter(|dir| dir.is_absolute())
                .any(|dir| file_status(&dir.join(program), true).is_none());
            if found {
                None
            } else {
                Some(format!(
                    "executable {program} absent from absolute PATH directories"
                ))
            }
        }
    }
}
