// SPDX-License-Identifier: AGPL-3.0-only
//! Resumable release decisions; no process, device, deletion or publication adapter.
use crate::{Fault, FaultKind, Result};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
pub const MANDATORY: [&str; 10] = [
    "F1", "F2", "F3", "F4", "F5", "F13", "F14", "F15", "F15b", "F16",
];
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Identity {
    pub source: String,
    pub lock: String,
    pub executable: String,
    pub archive: String,
    pub selection: String,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub enum Stage {
    Preflight,
    Package,
    Cert,
    Device,
    Soak,
    Proof,
    Publish,
    Download,
    Promote,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct FailureRecord {
    pub code: String,
    pub kind: FaultKind,
}
#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct State {
    pub identity: Identity,
    pub revision: u64,
    pub issued: BTreeSet<Stage>,
    pub completed: BTreeSet<Stage>,
    #[serde(default)]
    pub failures: BTreeMap<Stage, FailureRecord>,
}
#[derive(Clone, Debug)]
pub struct Receipt {
    pub identity: Identity,
    pub stage: Stage,
    pub passed: bool,
    pub physical: bool,
    pub incidents: Vec<String>,
    pub removed_tests: Vec<String>,
    pub approved_removals: Vec<String>,
}
#[derive(Debug)]
pub struct Progress {
    pub release_qualified: bool,
    pub boundary: &'static str,
}
pub trait ReleaseIo {
    fn load(&mut self) -> Result<Option<State>>;
    fn save(&mut self, expected: Option<u64>, next: &State) -> Result<()>;
    fn read_receipt(&mut self, stage: Stage) -> Result<Option<Receipt>>;
    fn execute(&mut self, stage: Stage, id: &Identity) -> Result<()>;
    fn read_audit(&mut self) -> Result<Option<crate::audit::Inventory>> {
        Ok(None)
    }
    /// Approval must bind this exact candidate and action; default is refusal.
    fn authorized(&mut self, _stage: Stage, _id: &Identity) -> Result<bool> {
        Ok(false)
    }
}
fn refusal(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Admission)
}
fn prerequisites(stage: Stage) -> &'static [Stage] {
    match stage {
        Stage::Preflight => &[],
        Stage::Package => &[Stage::Preflight],
        Stage::Cert | Stage::Device => &[Stage::Preflight, Stage::Package],
        Stage::Soak => &[Stage::Preflight, Stage::Package, Stage::Cert, Stage::Device],
        Stage::Proof => &[
            Stage::Preflight,
            Stage::Package,
            Stage::Cert,
            Stage::Device,
            Stage::Soak,
        ],
        Stage::Publish => &[
            Stage::Preflight,
            Stage::Package,
            Stage::Cert,
            Stage::Device,
            Stage::Soak,
            Stage::Proof,
        ],
        Stage::Download => &[
            Stage::Preflight,
            Stage::Package,
            Stage::Cert,
            Stage::Device,
            Stage::Soak,
            Stage::Proof,
            Stage::Publish,
        ],
        Stage::Promote => &[
            Stage::Preflight,
            Stage::Package,
            Stage::Cert,
            Stage::Device,
            Stage::Soak,
            Stage::Proof,
            Stage::Publish,
            Stage::Download,
        ],
    }
}
fn valid(r: &Receipt, id: &Identity, stage: Stage) -> Result<()> {
    if r.identity != *id || r.stage != stage || !r.passed {
        return Err(refusal("release_receipt_invalid"));
    }
    Ok(())
}
fn persist<I: ReleaseIo>(io: &mut I, expected: Option<u64>, state: &State) -> Result<()> {
    io.save(expected, state)?;
    let loaded = io.load()?.ok_or(refusal("release_state_readback_failed"))?;
    if loaded != *state {
        return Err(refusal("release_state_readback_failed"));
    }
    Ok(())
}
fn validate_state(state: &State) -> Result<()> {
    if !state.completed.is_subset(&state.issued)
        || state.issued.iter().any(|s| {
            prerequisites(*s)
                .iter()
                .any(|p| !state.completed.contains(p))
        })
        || state
            .failures
            .keys()
            .any(|s| !state.issued.contains(s) || state.completed.contains(s))
    {
        return Err(refusal("release_state_invalid"));
    }
    Ok(())
}
fn audit<I: ReleaseIo>(io: &mut I, id: &Identity) -> Result<()> {
    let inventory = io.read_audit()?.ok_or(refusal("release_audit_missing"))?;
    crate::audit::validate(&inventory, id)
}
pub fn advance<I: ReleaseIo>(io: &mut I, id: &Identity, stage: Stage) -> Result<Progress> {
    if !crate::package::hex(&id.source, 40)
        || [&id.lock, &id.executable, &id.archive, &id.selection]
            .iter()
            .any(|s| !crate::package::hex(s, 64))
    {
        return Err(refusal("release_identity_invalid"));
    }
    let old = io.load()?;
    let expected = old.as_ref().map(|s| s.revision);
    let mut state = old.unwrap_or(State {
        identity: id.clone(),
        revision: 0,
        issued: BTreeSet::new(),
        completed: BTreeSet::new(),
        failures: BTreeMap::new(),
    });
    if state.identity != *id {
        return Err(refusal("release_identity_changed"));
    }
    validate_state(&state)?;
    for required in prerequisites(stage) {
        if !state.completed.contains(required) {
            return Err(refusal("release_prerequisite_missing"));
        }
        let r = io
            .read_receipt(*required)?
            .ok_or(refusal("release_prerequisite_missing"))?;
        valid(&r, id, *required)?;
        if matches!(
            stage,
            Stage::Proof | Stage::Publish | Stage::Download | Stage::Promote
        ) && *required == Stage::Cert
        {
            if MANDATORY
                .iter()
                .any(|s| !r.incidents.iter().any(|id| id == s))
            {
                return Err(refusal("release_mandatory_incident_missing"));
            }
            if r.removed_tests
                .iter()
                .any(|t| !r.approved_removals.contains(t))
            {
                return Err(refusal("release_test_removal_unapproved"));
            }
        }
    }
    if matches!(
        stage,
        Stage::Proof | Stage::Publish | Stage::Download | Stage::Promote
    ) {
        audit(io, id)?;
    }
    if matches!(stage, Stage::Publish | Stage::Promote) && !io.authorized(stage, id)? {
        return Err(refusal("release_owner_approval_required"));
    }
    if state.failures.contains_key(&stage) {
        return Err(refusal("release_effect_unproven"));
    }
    if state.completed.contains(&stage) {
        let r = io
            .read_receipt(stage)?
            .ok_or(refusal("release_receipt_missing"))?;
        valid(&r, id, stage)?;
        return Ok(Progress {
            release_qualified: false,
            boundary: "fake_only",
        });
    }
    if !state.issued.contains(&stage) {
        state.issued.insert(stage);
        state.revision = state
            .revision
            .checked_add(1)
            .ok_or(refusal("release_revision_overflow"))?;
        persist(io, expected, &state)?;
        // Intent is durable BEFORE the effect. An ambiguous crash can only resume via a matching readback.
        if let Err(first) = io.execute(stage, id) {
            let previous = state.revision;
            state.failures.insert(
                stage,
                FailureRecord {
                    code: first.code.into(),
                    kind: first.kind,
                },
            );
            state.revision = state
                .revision
                .checked_add(1)
                .ok_or(refusal("release_revision_overflow"))?;
            persist(io, Some(previous), &state)?;
            return Err(first);
        }
    }
    let r = io
        .read_receipt(stage)?
        .ok_or(refusal("release_effect_unproven"))?;
    valid(&r, id, stage)?;
    let previous = state.revision;
    state.completed.insert(stage);
    state.revision = state
        .revision
        .checked_add(1)
        .ok_or(refusal("release_revision_overflow"))?;
    persist(io, Some(previous), &state)?;
    // This lane cannot authenticate physical receipts or qualify a release.
    Ok(Progress {
        release_qualified: false,
        boundary: "fake_only",
    })
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PromotionReceipt {
    pub schema_version: u32,
    pub identity: Identity,
    pub stages: Vec<Stage>,
    pub release_qualified: bool,
    pub boundary: String,
}
pub fn promotion_receipt<I: ReleaseIo>(io: &mut I, id: &Identity) -> Result<PromotionReceipt> {
    let state = io.load()?.ok_or(refusal("promotion_receipt_missing"))?;
    if state.identity != *id || !state.completed.contains(&Stage::Promote) {
        return Err(refusal("promotion_receipt_missing"));
    }
    validate_state(&state)?;
    audit(io, id)?;
    if !crate::package::hex(&id.source, 40)
        || [&id.lock, &id.executable, &id.archive, &id.selection]
            .iter()
            .any(|s| !crate::package::hex(s, 64))
    {
        return Err(refusal("release_identity_invalid"));
    }
    let stages = prerequisites(Stage::Promote)
        .iter()
        .copied()
        .chain([Stage::Promote])
        .collect::<Vec<_>>();
    for stage in &stages {
        if !state.completed.contains(stage) || !state.issued.contains(stage) {
            return Err(refusal("promotion_receipt_missing"));
        }
        let r = io
            .read_receipt(*stage)?
            .ok_or(refusal("release_receipt_missing"))?;
        valid(&r, id, *stage)?;
        if *stage == Stage::Cert {
            if MANDATORY
                .iter()
                .any(|s| !r.incidents.iter().any(|id| id == s))
            {
                return Err(refusal("release_mandatory_incident_missing"));
            }
            if r.removed_tests
                .iter()
                .any(|t| !r.approved_removals.contains(t))
            {
                return Err(refusal("release_test_removal_unapproved"));
            }
        }
    }
    let after = io.load()?.ok_or(refusal("release_state_readback_failed"))?;
    if after != state {
        return Err(refusal("release_state_readback_failed"));
    }
    Ok(PromotionReceipt {
        schema_version: 1,
        identity: id.clone(),
        stages,
        release_qualified: false,
        boundary: "fake_only".into(),
    })
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Slot {
    Root,
    Parent,
    Branch,
    Log,
}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CandidateSlots {
    pub source: String,
    pub generation: u64,
    pub root: Option<String>,
    pub parent: Option<String>,
    pub branch: Option<String>,
    pub log: Option<String>,
    pub unfired: bool,
}
pub trait AsideIo {
    fn inspect(&mut self) -> Result<CandidateSlots>;
    fn set_aside_exact(&mut self, expected: &CandidateSlots, slot: Slot) -> Result<()>;
}
#[derive(Debug)]
pub struct AsideReceipt {
    pub source: String,
    pub same_sha_retry_ready: bool,
}
pub fn set_aside<I: AsideIo>(io: &mut I, expected: &CandidateSlots) -> Result<AsideReceipt> {
    if !crate::package::hex(&expected.source, 40) || !expected.unfired {
        return Err(refusal("candidate_not_proven_unfired"));
    }
    let mut current = io.inspect()?;
    if current != *expected {
        return Err(refusal("candidate_changed_or_fired"));
    }
    for slot in [Slot::Root, Slot::Parent, Slot::Branch, Slot::Log] {
        if io.inspect()? != current || !current.unfired {
            return Err(refusal("candidate_changed_or_fired"));
        }
        io.set_aside_exact(&current, slot)?;
        match slot {
            Slot::Root => current.root = None,
            Slot::Parent => current.parent = None,
            Slot::Branch => current.branch = None,
            Slot::Log => current.log = None,
        }
        if io.inspect()? != current {
            return Err(refusal("candidate_set_aside_unproven"));
        }
    }
    Ok(AsideReceipt {
        source: current.source,
        same_sha_retry_ready: true,
    })
}
