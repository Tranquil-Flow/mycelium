// SPDX-License-Identifier: AGPL-3.0-only
//! Candidate-bound collected-ID/obligation audit. Evidence is supplied, not authenticated here.
use crate::{Result, release::Identity};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Test {
    pub job: String,
    pub obligations: BTreeSet<String>,
    pub incidents: BTreeSet<String>,
    pub passed: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Removal {
    pub reviewer: String,
    pub evidence: String,
    pub replacements: BTreeSet<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Inventory {
    pub source: String,
    pub source_files: BTreeMap<String, String>,
    pub required_obligations: BTreeSet<String>,
    pub previous_tests: BTreeMap<String, BTreeSet<String>>,
    pub tests: BTreeMap<String, Test>,
    pub selected_jobs: BTreeSet<String>,
    pub removals: BTreeMap<String, Removal>,
}
fn require(ok: bool, code: &'static str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(crate::Fault::new(code, crate::FaultKind::Admission))
    }
}
fn identifier(value: &str) -> bool {
    !value.trim().is_empty() && !value.chars().any(char::is_control)
}
pub fn validate(inventory: &Inventory, id: &Identity) -> Result<()> {
    require(
        inventory.source == id.source && crate::package::hex(&inventory.source, 40),
        "audit_source_mismatch",
    )?;
    let encoded = serde_json::to_vec(inventory)
        .map_err(|_| crate::Fault::new("audit_encoding_failed", crate::FaultKind::Input))?;
    require(
        crate::package::digest(&encoded) == id.selection,
        "audit_selection_mismatch",
    )?;
    require(
        !inventory.source_files.is_empty(),
        "audit_source_inventory_missing",
    )?;
    for (path, hash) in &inventory.source_files {
        require(
            identifier(path)
                && !path.contains('\\')
                && path
                    .split('/')
                    .all(|p| !p.is_empty() && p != "." && p != "..")
                && crate::package::hex(hash, 64),
            "audit_source_entry_invalid",
        )?;
        let lower = path.to_ascii_lowercase();
        require(
            !lower.ends_with(".py")
                && !lower.ends_with(".pyc")
                && !lower.ends_with(".pyo")
                && !lower.ends_with(".whl")
                && !lower.split('/').any(|p| {
                    matches!(p, "pyo3" | "venv" | ".venv" | "__pycache__")
                        || p.starts_with("python")
                }),
            "audit_python_inventory_forbidden",
        )?;
    }
    require(
        !inventory.tests.is_empty()
            && !inventory.selected_jobs.is_empty()
            && !inventory.required_obligations.is_empty(),
        "audit_selection_empty",
    )?;
    let mut obligations = BTreeSet::new();
    let mut incidents = BTreeSet::new();
    let mut jobs = BTreeSet::new();
    for (name, test) in &inventory.tests {
        require(
            identifier(name)
                && identifier(&test.job)
                && test
                    .obligations
                    .iter()
                    .chain(&test.incidents)
                    .all(|s| identifier(s)),
            "audit_test_invalid",
        )?;
        require(
            inventory.selected_jobs.contains(&test.job),
            "audit_test_job_unselected",
        )?;
        require(test.passed, "audit_selected_test_not_passed")?;
        obligations.extend(test.obligations.iter().cloned());
        incidents.extend(test.incidents.iter().cloned());
        jobs.insert(test.job.clone());
    }
    require(
        jobs == inventory.selected_jobs,
        "audit_selected_job_missing",
    )?;
    require(
        inventory.required_obligations.iter().all(|s| identifier(s))
            && inventory.required_obligations.is_subset(&obligations),
        "audit_obligation_missing",
    )?;
    require(
        crate::release::MANDATORY
            .iter()
            .all(|s| incidents.contains(*s)),
        "audit_incident_missing",
    )?;
    let removed: BTreeSet<_> = inventory
        .previous_tests
        .keys()
        .filter(|name| !inventory.tests.contains_key(*name))
        .cloned()
        .collect();
    require(
        inventory.removals.keys().cloned().collect::<BTreeSet<_>>() == removed,
        "audit_test_removal_unapproved",
    )?;
    for (name, old_obligations) in &inventory.previous_tests {
        require(
            identifier(name) && old_obligations.iter().all(|s| identifier(s)),
            "audit_previous_test_invalid",
        )?;
        let mut covered = BTreeSet::new();
        if let Some(test) = inventory.tests.get(name) {
            covered.extend(test.obligations.iter().cloned());
        } else {
            let approval = inventory.removals.get(name).ok_or(crate::Fault::new(
                "audit_test_removal_unapproved",
                crate::FaultKind::Admission,
            ))?;
            require(
                identifier(&approval.reviewer)
                    && identifier(&approval.evidence)
                    && !approval.replacements.is_empty(),
                "audit_removal_evidence_missing",
            )?;
            for replacement in &approval.replacements {
                let test = inventory.tests.get(replacement).ok_or(crate::Fault::new(
                    "audit_replacement_missing",
                    crate::FaultKind::Admission,
                ))?;
                covered.extend(test.obligations.iter().cloned());
            }
        }
        require(
            old_obligations.is_subset(&covered),
            "audit_removed_obligation_missing",
        )?;
    }
    Ok(())
}
