// SPDX-License-Identifier: AGPL-3.0-only
//! Dependency-closure retention planner. No filesystem/deletion capability exists.
use crate::Result;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Node {
    pub digest: String,
    /// Include resolved symlink/hardlink identities as dependencies.
    pub dependencies: BTreeSet<String>,
    pub protected: bool,
    pub active_lease: bool,
    pub restore_verified: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Plan {
    pub keep: BTreeSet<String>,
    pub archive_candidates: BTreeSet<String>,
    pub deletion_authorized: bool,
}
pub fn plan(
    nodes: &BTreeMap<String, Node>,
    offline_roots: &BTreeSet<String>,
    candidates: &BTreeSet<String>,
) -> Result<Plan> {
    let error = |code| crate::Fault::new(code, crate::FaultKind::Admission);
    if nodes.is_empty()
        || offline_roots.is_empty()
        || offline_roots
            .iter()
            .chain(candidates)
            .any(|k| !nodes.contains_key(k))
    {
        return Err(error("retention_inventory_missing"));
    }
    for (name, node) in nodes {
        if name.trim().is_empty()
            || name.chars().any(char::is_control)
            || !crate::package::hex(&node.digest, 64)
            || node.dependencies.iter().any(|k| !nodes.contains_key(k))
        {
            return Err(error("retention_inventory_invalid"));
        }
    }
    let mut keep = BTreeSet::new();
    let mut pending: Vec<_> = nodes
        .iter()
        .filter(|(name, node)| {
            !candidates.contains(*name)
                || node.protected
                || node.active_lease
                || offline_roots.contains(*name)
        })
        .map(|(name, _)| name.clone())
        .collect();
    while let Some(name) = pending.pop() {
        if keep.insert(name.clone()) {
            let node = nodes
                .get(&name)
                .ok_or(error("retention_dependency_missing"))?;
            pending.extend(node.dependencies.iter().cloned());
        }
    }
    if !keep.is_disjoint(candidates) {
        return Err(error("retention_protected_dependency"));
    }
    for name in candidates {
        if !nodes
            .get(name)
            .ok_or(error("retention_inventory_missing"))?
            .restore_verified
        {
            return Err(error("retention_restore_unproven"));
        }
    }
    Ok(Plan {
        keep,
        archive_candidates: candidates.clone(),
        deletion_authorized: false,
    })
}
