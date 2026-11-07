// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask retire check` schema 2 (RU-042, TEST-BEHAVIOURS-PLAN section 7): the unit is the test
//! group, not the Python test id.
//!
//! * Group dispositions: the consolidated `TEST-GROUP-DISPOSITIONS.final.tsv` (TB-4c, one row per
//!   group, duplicates resolved) is the only source. The TB-3 file and `tb4/P<n>.tsv` are
//!   provenance and are not read.
//! * An `evidence_for` group resolves when every B-id it names exists and is **proven**.
//! * A behaviour is proven when it lists Rust tests and evidence, every test exists and is shown
//!   `ok` by a cited PASS gate log, and every fault in its `must_catch` is caught (a PASS gate log
//!   prints `FAULT-CAUGHT <fault> by <test>` for a test of a behaviour the fault must break) or
//!   adjudicated (equivalent, unviable, timeout, not-run: reason, evidence, approver).
//! * A retirement group (`not_shipped`, `python_only`, `structure_only`, `superseded_tooling`,
//!   `not_executed`) resolves when it has a reason, the archived rollback location (its file at the
//!   pinned oracle commit) and an allowed approver; `not_executed` also needs a reachability
//!   argument that cites code. `superseded_tooling` names an existing `B-REL-*` behaviour.
//! * Schema-1 per-id rows stay valid evidence: a group whose every id is resolved by them resolves.
use crate::{
    behaviours::{Behaviour, Fault},
    policy::Date,
    retire::{Baseline, Finding, Ledger},
    tsv::{Table, id_list},
};
use serde::Deserialize;
use std::collections::{BTreeMap, BTreeSet};

pub const RETIREMENT_CATEGORIES: [&str; 5] = [
    "not_shipped",
    "python_only",
    "structure_only",
    "superseded_tooling",
    "not_executed",
];
pub const ADJUDICATIONS: [&str; 4] = ["equivalent", "unviable", "timeout", "not-run"];
/// The consolidated final dispositions file (one row per group; extra columns are allowed).
pub const FINAL_COLUMNS: [&str; 8] = [
    "group_id",
    "path",
    "n_tests",
    "disposition",
    "b_ids",
    "reason_or_reachability",
    "source_step",
    "confidence",
];
/// File name of the consolidated dispositions inside the registry directory.
pub const FINAL_FILE: &str = "TEST-GROUP-DISPOSITIONS.final.tsv";
pub const CATCH_COLUMNS: [&str; 6] = [
    "fault",
    "status",
    "evidence",
    "caught_by",
    "reason",
    "approver",
];

fn finding(code: &'static str, subject: &str, detail: impl Into<String>) -> Finding {
    Finding {
        code,
        subject: subject.to_string(),
        detail: detail.into(),
    }
}

// ------------------------------------------------------------------------------------ policy

/// `xtask/retirement/groups.json`: what a retirement group is approved and archived under.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub schema: u64,
    /// The archived oracle tree a retired group's file can be recovered from.
    pub rollback_sha: String,
    pub rollback_note: String,
    pub approvers: Vec<String>,
    pub default_approver: String,
    pub approved: String,
    /// Per-group owner (or other) approvals replacing the default approver.
    #[serde(default)]
    pub overrides: BTreeMap<String, Override>,
}
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Override {
    pub approver: String,
    pub approved: String,
}
impl Policy {
    pub fn parse(json: &str) -> Result<Self, String> {
        let policy: Self = serde_json::from_str(json)
            .map_err(|e| crate::retire::fail("groups_policy_invalid", e))?;
        if policy.schema != 2 {
            return Err(crate::retire::fail(
                "groups_policy_invalid",
                "schema must be 2",
            ));
        }
        Ok(policy)
    }
}

// ------------------------------------------------------------------------------------ groups

#[derive(Debug, Clone, Default)]
pub struct Group {
    pub id: String,
    pub path: String,
    pub n_tests: usize,
    /// `evidence_for`, a retirement category, `needs_review` (no row), or `disposition_conflict`.
    pub disposition: String,
    pub b_ids: Vec<String>,
    /// Reason or reachability argument (TB-3 import evidence where TB-4 added nothing).
    pub reason: String,
    pub source: String,
}

/// The dispositions a final row may hold.
fn disposition_known(disposition: &str) -> bool {
    disposition == "evidence_for" || RETIREMENT_CATEGORIES.contains(&disposition)
}

/// Reads the consolidated final dispositions: one row per group. A group listed twice is a
/// conflict and resolves nothing; a disposition that is not `evidence_for` or a retirement
/// category (including `needs_review`) is a finding; a file of the Python inventory without a
/// row stays in the report as an open group. Also checks every group against the inventory
/// (`n_tests`, file present, none missing).
pub fn load_final_dispositions(
    text: &str,
    baseline: &Baseline,
) -> Result<(Vec<Group>, Vec<Finding>), String> {
    let mut findings = Vec::new();
    let table =
        Table::parse(text, &FINAL_COLUMNS, false).map_err(|e| format!("{FINAL_FILE}: {e}"))?;
    for (line, got) in &table.ragged {
        findings.push(finding(
            "row_columns_invalid",
            &format!("{FINAL_FILE}:{line}"),
            format!("{got} cells, header has {}", table.columns.len()),
        ));
    }
    let mut groups: Vec<Group> = Vec::new();
    let mut index: BTreeMap<String, usize> = BTreeMap::new();
    for i in 0..table.rows.len() {
        let id = table.get(i, "group_id").to_string();
        let n_tests = table.get(i, "n_tests").parse::<usize>().ok();
        if n_tests.is_none() {
            findings.push(finding(
                "n_tests_invalid",
                &id,
                format!("n_tests {:?}", table.get(i, "n_tests")),
            ));
        }
        if let Some(&slot) = index.get(&id) {
            findings.push(finding(
                "group_duplicate",
                &id,
                format!("twice in {FINAL_FILE} (line {})", table.lines[i]),
            ));
            groups[slot].disposition = "disposition_conflict".into();
            continue;
        }
        let disposition = table.get(i, "disposition");
        if !disposition_known(disposition) {
            findings.push(finding(
                "disposition_invalid",
                &id,
                format!("{disposition:?} is not evidence_for or a retirement category"),
            ));
        }
        index.insert(id.clone(), groups.len());
        groups.push(Group {
            id,
            path: table.get(i, "path").into(),
            n_tests: n_tests.unwrap_or(0),
            disposition: disposition.into(),
            b_ids: id_list(table.get(i, "b_ids")),
            reason: table.get(i, "reason_or_reachability").into(),
            source: table.get(i, "source_step").into(),
        });
    }
    // An inventory file with no row is still a group of the report: open, undispositioned.
    for (source, count) in &baseline.sources {
        if !groups.iter().any(|g| &g.path == source) {
            findings.push(finding(
                "inventory_file_without_group",
                source,
                "a file of the Python inventory has no group row",
            ));
            groups.push(Group {
                id: format!("T:{source}"),
                path: source.clone(),
                n_tests: *count,
                disposition: "needs_review".into(),
                b_ids: Vec::new(),
                reason: String::new(),
                source: String::new(),
            });
        }
    }
    for group in &groups {
        if group.id.strip_prefix("T:") != Some(group.path.as_str()) {
            findings.push(finding(
                "group_path_mismatch",
                &group.id,
                format!("path {:?} is not the id without `T:`", group.path),
            ));
        }
        match baseline.sources.get(&group.path) {
            None => findings.push(finding(
                "group_not_in_inventory",
                &group.id,
                format!("{} is not a file of the Python inventory", group.path),
            )),
            Some(count) => {
                if *count != group.n_tests {
                    findings.push(finding(
                        "n_tests_mismatch",
                        &group.id,
                        format!(
                            "the group says {}, the inventory has {count}",
                            group.n_tests
                        ),
                    ));
                }
            }
        }
    }
    Ok((groups, findings))
}

// ------------------------------------------------------------------------------------ catches

#[derive(Debug, Clone, Default)]
pub struct Catch {
    pub fault: String,
    pub status: String,
    pub evidence: Vec<String>,
    pub caught_by: String,
    pub reason: String,
    pub approver: String,
    pub line: usize,
}
/// `xtask/retirement/fault-catches.tsv`: how each fault was caught or adjudicated.
pub fn load_catches(text: &str) -> Result<(Vec<Catch>, Vec<Finding>), String> {
    let table =
        Table::parse(text, &CATCH_COLUMNS, true).map_err(|e| format!("fault-catches: {e}"))?;
    let mut findings = Vec::new();
    for (line, got) in &table.ragged {
        findings.push(finding(
            "row_columns_invalid",
            &format!("fault-catches.tsv:{line}"),
            format!("{got} cells, header has {}", table.columns.len()),
        ));
    }
    let rows = (0..table.rows.len())
        .map(|i| Catch {
            fault: table.get(i, "fault").into(),
            status: table.get(i, "status").into(),
            evidence: id_list(table.get(i, "evidence")),
            caught_by: table.get(i, "caught_by").into(),
            reason: table.get(i, "reason").into(),
            approver: table.get(i, "approver").into(),
            line: table.lines[i],
        })
        .collect();
    Ok((rows, findings))
}

// ------------------------------------------------------------------------------------ resolver

pub struct Inputs<'a> {
    pub behaviours: &'a [Behaviour],
    pub faults: &'a [Fault],
    pub groups: &'a [Group],
    pub catches: &'a [Catch],
    /// Findings of `behaviours::validate` (a behaviour named as a subject is not proven).
    pub catalogue_findings: &'a [Finding],
    pub rust_tests: &'a BTreeSet<String>,
    pub ledger: &'a Ledger,
    pub policy: &'a Policy,
    pub baseline: &'a Baseline,
    /// Old Python test ids resolved by schema-1 rows.
    pub schema1_resolved: &'a BTreeSet<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FaultState {
    Caught(String),
    Adjudicated(String),
    /// Not caught: the reason code.
    Open(&'static str),
}
#[derive(Debug, Clone)]
pub struct GroupResult {
    pub id: String,
    pub disposition: String,
    pub n_tests: usize,
    /// How it resolved (`schema1`, `evidence_for`, or the retirement category); `None` = unresolved.
    pub via: Option<String>,
    /// Why it is unresolved (empty when resolved).
    pub reasons: Vec<&'static str>,
    pub areas: BTreeSet<String>,
}
#[derive(Debug, Clone, Default)]
pub struct Resolution {
    pub faults: BTreeMap<String, FaultState>,
    /// Empty reasons = proven.
    pub behaviours: BTreeMap<String, Vec<&'static str>>,
    pub groups: Vec<GroupResult>,
    /// Findings about the catches and the dispositions themselves.
    pub findings: Vec<Finding>,
}
impl Resolution {
    pub fn proven(&self) -> BTreeSet<String> {
        self.behaviours
            .iter()
            .filter(|(_, reasons)| reasons.is_empty())
            .map(|(id, _)| id.clone())
            .collect()
    }
    pub fn unresolved_groups(&self) -> usize {
        self.groups.iter().filter(|g| g.via.is_none()).count()
    }
}

fn is_test_id(id: &str) -> Option<&str> {
    id.strip_prefix("rs:")?
        .split_once(".rs::")
        .map(|(_, name)| name)
}

/// Where every fault stands: caught, adjudicated or open (with the reason code), plus findings
/// about the catch rows themselves.
pub fn fault_states(inputs: &Inputs) -> (BTreeMap<String, FaultState>, Vec<Finding>) {
    let mut findings = Vec::new();
    let known: BTreeMap<&str, &Fault> = inputs.faults.iter().map(|f| (f.id.as_str(), f)).collect();
    let mut by_fault: BTreeMap<&str, Vec<&Catch>> = BTreeMap::new();
    for catch in inputs.catches {
        if !known.contains_key(catch.fault.as_str()) {
            findings.push(finding(
                "catch_fault_unknown",
                &catch.fault,
                format!(
                    "fault-catches.tsv:{}: no such fault in FAULTS.tsv",
                    catch.line
                ),
            ));
            continue;
        }
        by_fault
            .entry(catch.fault.as_str())
            .or_default()
            .push(catch);
    }
    let tests_of = |fault: &Fault| -> BTreeSet<&str> {
        inputs
            .behaviours
            .iter()
            .filter(|b| fault.must_break.contains(&b.id))
            .flat_map(|b| b.rust_tests.iter().map(String::as_str))
            .collect()
    };
    let mut states = BTreeMap::new();
    for fault in inputs.faults {
        let id = fault.id.as_str();
        let state = match by_fault.get(id).map(Vec::as_slice) {
            None | Some([]) => FaultState::Open("no_catch_row"),
            Some([_, _, ..]) => {
                findings.push(finding(
                    "catch_duplicate",
                    id,
                    "more than one row in fault-catches.tsv; none is trusted",
                ));
                FaultState::Open("catch_duplicate")
            }
            Some([catch]) => match catch.status.as_str() {
                "caught" => caught_state(inputs, fault, catch, &tests_of(fault)),
                "adjudicated" => adjudicated_state(inputs, fault, catch),
                other => {
                    findings.push(finding(
                        "catch_status_invalid",
                        id,
                        format!("{other:?}: caught or adjudicated"),
                    ));
                    FaultState::Open("catch_status_invalid")
                }
            },
        };
        states.insert(fault.id.clone(), state);
    }
    (states, findings)
}
fn caught_state(
    inputs: &Inputs,
    fault: &Fault,
    catch: &Catch,
    related: &BTreeSet<&str>,
) -> FaultState {
    let Some(name) = is_test_id(&catch.caught_by) else {
        return FaultState::Open("catch_test_unknown");
    };
    if !inputs.rust_tests.contains(&catch.caught_by) {
        return FaultState::Open("catch_test_unknown");
    }
    let Some(evidence) = catch
        .evidence
        .iter()
        .find(|ev| inputs.ledger.proofs.shows_caught(ev, &fault.id, name))
    else {
        return FaultState::Open("catch_not_proved");
    };
    if !related.contains(catch.caught_by.as_str()) {
        return FaultState::Open("catch_by_unrelated_test");
    }
    FaultState::Caught(evidence.clone())
}
fn adjudicated_state(inputs: &Inputs, fault: &Fault, catch: &Catch) -> FaultState {
    if fault.origin == "positive-control" {
        return FaultState::Open("positive_control_adjudicated");
    }
    let category = catch
        .reason
        .split_once(':')
        .filter(|(category, text)| {
            ADJUDICATIONS.contains(&category.trim()) && !text.trim().is_empty()
        })
        .map(|(category, _)| category.trim().to_string());
    let evidence_ok = !catch.evidence.is_empty()
        && catch
            .evidence
            .iter()
            .all(|ev| inputs.ledger.ids.contains(ev));
    let approver_ok = inputs.policy.approvers.contains(&catch.approver);
    match category {
        Some(category) if evidence_ok && approver_ok => FaultState::Adjudicated(category),
        _ => FaultState::Open("adjudication_invalid"),
    }
}

/// Reasons (empty = proven) for every behaviour. The faults that count are those the behaviour
/// lists in `must_catch` plus those that list it in `must_break`.
pub fn behaviour_states(
    inputs: &Inputs,
    faults: &BTreeMap<String, FaultState>,
) -> BTreeMap<String, Vec<&'static str>> {
    let mut out = BTreeMap::new();
    for b in inputs.behaviours {
        let mut reasons: Vec<&'static str> = Vec::new();
        let mut add = |reason: &'static str| {
            if !reasons.contains(&reason) {
                reasons.push(reason);
            }
        };
        if inputs.catalogue_findings.iter().any(|f| f.subject == b.id) {
            add("row_invalid");
        }
        if b.rust_tests.is_empty() {
            add("no_rust_tests");
        }
        if b.evidence.is_empty() {
            add("no_evidence");
        }
        for test in &b.rust_tests {
            if !inputs.rust_tests.contains(test) {
                add("rust_test_unknown");
            } else if !b
                .evidence
                .iter()
                .any(|ev| inputs.ledger.proofs.shows(ev, test))
            {
                add("rust_test_not_proved");
            }
        }
        let mut faults_of: BTreeSet<&str> = b.must_catch.iter().map(String::as_str).collect();
        faults_of.extend(
            inputs
                .faults
                .iter()
                .filter(|f| f.must_break.contains(&b.id))
                .map(|f| f.id.as_str()),
        );
        for fault in faults_of {
            match faults.get(fault) {
                None => add("fault_unknown"),
                Some(FaultState::Open(_)) => add("fault_not_caught"),
                Some(FaultState::Caught(_) | FaultState::Adjudicated(_)) => {}
            }
        }
        out.insert(b.id.clone(), reasons);
    }
    out
}

/// A reachability argument is long enough to say something, names code (a module, file or
/// identifier such as `foo_bar`, `a.b` or `x.py`) and states a verdict on whether the app reaches it.
/// The owner reviews the arguments themselves (a sample per category); this only refuses a
/// placeholder.
fn reachability_ok(text: &str) -> bool {
    // A verdict is a negation: the app does not reach, import, launch or execute it.
    const VERDICTS: [&str; 9] = [
        "not ", " no ", "never", "only", "without", "nothing", "none", "unreach", "neither",
    ];
    let lower = format!(" {} ", text.to_lowercase());
    let names_code = text
        .split(|c: char| !(c.is_ascii_alphanumeric() || c == '_' || c == '.'))
        .map(|token| token.trim_matches('.'))
        .any(|token| {
            token.contains('_')
                || token
                    .split_once('.')
                    .is_some_and(|(a, b)| !a.is_empty() && !b.is_empty())
        });
    text.trim().len() >= 40 && names_code && VERDICTS.iter().any(|v| lower.contains(v))
}
fn rollback_ok(sha: &str) -> bool {
    sha.len() == 40 && sha.bytes().all(|b| b.is_ascii_hexdigit())
}

fn group_reasons(
    inputs: &Inputs,
    group: &Group,
    behaviours: &BTreeMap<String, Vec<&'static str>>,
    area_of: &BTreeMap<&str, &str>,
    ids_of_file: &BTreeMap<&str, Vec<&String>>,
) -> (Option<String>, Vec<&'static str>) {
    // Schema-1 per-id rows: every id of the group mapped. `ids_of_file` only has files with at
    // least one test, so a group without tests is never resolved this way.
    if let Some(ids) = ids_of_file.get(group.path.as_str())
        && ids.iter().all(|id| inputs.schema1_resolved.contains(*id))
    {
        return (Some("schema1".into()), Vec::new());
    }
    let mut reasons: Vec<&'static str> = Vec::new();
    let mut add = |reason: &'static str| {
        if !reasons.contains(&reason) {
            reasons.push(reason);
        }
    };
    let disposition = group.disposition.as_str();
    match disposition {
        "" | "needs_review" => add("undispositioned"),
        "disposition_conflict" => add("disposition_conflict"),
        "evidence_for" => {
            if group.b_ids.is_empty() {
                add("no_behaviours");
            }
            for id in &group.b_ids {
                match behaviours.get(id) {
                    None => add("behaviour_unknown"),
                    Some(r) if !r.is_empty() => add("behaviour_unproven"),
                    Some(_) => {}
                }
            }
        }
        category if RETIREMENT_CATEGORIES.contains(&category) => {
            if group.reason.trim().is_empty() {
                add("reason_missing");
            } else if category == "not_executed" && !reachability_ok(&group.reason) {
                add("reachability_missing");
            }
            if !inputs.baseline.sources.contains_key(&group.path)
                || !rollback_ok(&inputs.policy.rollback_sha)
            {
                add("rollback_unknown");
            }
            let (approver, approved) = inputs.policy.overrides.get(&group.id).map_or(
                (
                    inputs.policy.default_approver.as_str(),
                    inputs.policy.approved.as_str(),
                ),
                |o| (o.approver.as_str(), o.approved.as_str()),
            );
            if !inputs.policy.approvers.iter().any(|a| a == approver) {
                add("approver_invalid");
            }
            if Date::parse(approved).is_err() {
                add("approved_invalid");
            }
            if category == "superseded_tooling" {
                if group.b_ids.is_empty() {
                    add("no_behaviours");
                }
                for id in &group.b_ids {
                    if !behaviours.contains_key(id) {
                        add("behaviour_unknown");
                    }
                }
                // Plan section 5: it names the B-REL-* behaviour that replaces it (others may join it).
                if !group.b_ids.is_empty() && !group.b_ids.iter().any(|id| id.starts_with("B-REL-"))
                {
                    add("not_a_release_behaviour");
                }
            }
        }
        _ => add("disposition_invalid"),
    }
    let _ = area_of;
    if reasons.is_empty() {
        (Some(group.disposition.clone()), reasons)
    } else {
        (None, reasons)
    }
}

pub fn resolve(inputs: &Inputs) -> Resolution {
    let (faults, findings) = fault_states(inputs);
    let behaviours = behaviour_states(inputs, &faults);
    let area_of: BTreeMap<&str, &str> = inputs
        .behaviours
        .iter()
        .map(|b| (b.id.as_str(), b.area.as_str()))
        .collect();
    let mut ids_of_file: BTreeMap<&str, Vec<&String>> = BTreeMap::new();
    for id in &inputs.baseline.ids {
        ids_of_file
            .entry(crate::retire::file_of(id))
            .or_default()
            .push(id);
    }
    let groups = inputs
        .groups
        .iter()
        .map(|g| {
            let (via, reasons) = group_reasons(inputs, g, &behaviours, &area_of, &ids_of_file);
            GroupResult {
                id: g.id.clone(),
                disposition: g.disposition.clone(),
                n_tests: g.n_tests,
                via,
                reasons,
                areas: g
                    .b_ids
                    .iter()
                    .filter_map(|id| area_of.get(id.as_str()).map(|a| (*a).to_string()))
                    .collect(),
            }
        })
        .collect();
    Resolution {
        faults,
        behaviours,
        groups,
        findings,
    }
}

// ------------------------------------------------------------------------------------ command line

use crate::{
    behaviours::{self, load_behaviours, load_faults},
    retire::{Options, Schema1, load_schema1, parse_options, read, root_of},
};
use std::{
    fs,
    path::{Path, PathBuf},
};

/// Everything schema 2 reads besides the Python inventory, the Rust sources and the ledger.
pub(crate) struct Loaded {
    pub behaviours: Vec<Behaviour>,
    pub faults: Vec<Fault>,
    pub groups: Vec<Group>,
    pub catches: Vec<Catch>,
    pub policy: Policy,
    /// Behaviours proposed by TB-4 passes (`tb4/*-new-behaviours.tsv`), already in `behaviours`.
    pub tb4_additions: usize,
    pub findings: Vec<Finding>,
}
fn text(path: &Path) -> Result<String, String> {
    Ok(String::from_utf8_lossy(&read(path)?).to_string())
}
/// Reads the registry TSVs. `baseline` is only used to check the groups against the inventory.
pub(crate) fn load_registry(
    options: &Options,
    root: &Path,
    baseline: Option<&Baseline>,
) -> Result<Loaded, String> {
    let dir = options
        .values
        .get("--registry-dir")
        .map(PathBuf::from)
        .or_else(|| {
            options
                .values
                .get("--ledger")
                .and_then(|l| Path::new(l).parent().map(Path::to_path_buf))
        })
        .ok_or(
            "USAGE: give --registry-dir or --ledger (the registry directory holds BEHAVIOURS.tsv)",
        )?;
    let path_of =
        |key: &str, default: PathBuf| options.values.get(key).map_or(default, PathBuf::from);
    let tb4_dir = path_of("--tb4-dir", dir.join("tb4"));
    let mut findings = Vec::new();
    let behaviours_path = path_of("--behaviours", dir.join("BEHAVIOURS.tsv"));
    let (mut behaviours, found) = load_behaviours(&text(&behaviours_path)?, "BEHAVIOURS.tsv")?;
    findings.extend(found);
    let faults_path = path_of("--faults", dir.join("FAULTS.tsv"));
    let (faults, found) = load_faults(&text(&faults_path)?, "FAULTS.tsv")?;
    findings.extend(found);
    // `tb4/NEW-ID-MAP.tsv` (new_id -> behaviour_id): a proposed behaviour that has joined
    // BEHAVIOURS.tsv under its final id is read from there; groups naming the NEW id are
    // translated. A mapping whose final id is not in BEHAVIOURS.tsv yet is not applied.
    let known: BTreeSet<String> = behaviours.iter().map(|b| b.id.clone()).collect();
    let mut integrated: BTreeMap<String, String> = BTreeMap::new();
    if let Ok(map_text) = text(&tb4_dir.join("NEW-ID-MAP.tsv")) {
        let table = Table::parse(&map_text, &["new_id", "behaviour_id"], false)
            .map_err(|e| format!("tb4/NEW-ID-MAP.tsv: {e}"))?;
        for i in 0..table.rows.len() {
            if known.contains(table.get(i, "behaviour_id")) {
                integrated.insert(
                    table.get(i, "new_id").to_string(),
                    table.get(i, "behaviour_id").to_string(),
                );
            }
        }
    }
    let mut tb4_additions = 0;
    if let Ok(entries) = fs::read_dir(&tb4_dir) {
        let mut names: Vec<String> = entries
            .filter_map(|e| e.ok()?.file_name().into_string().ok())
            .filter(|n| n.ends_with("-new-behaviours.tsv"))
            .collect();
        names.sort();
        for name in names {
            let (rows, found) =
                load_behaviours(&text(&tb4_dir.join(&name))?, &format!("tb4/{name}"))?;
            for row in rows {
                if !integrated.contains_key(&row.id) {
                    tb4_additions += 1;
                    behaviours.push(row);
                }
            }
            findings.extend(found);
        }
    }
    let (groups, found) = match baseline {
        Some(baseline) => {
            let final_dispositions = text(&path_of("--dispositions", dir.join(FINAL_FILE)))?;
            load_final_dispositions(&final_dispositions, baseline)?
        }
        None => (Vec::new(), Vec::new()),
    };
    findings.extend(found);
    let mut groups = groups;
    for group in &mut groups {
        for id in &mut group.b_ids {
            if let Some(final_id) = integrated.get(id) {
                *id = final_id.clone();
            }
        }
    }
    let catches_path = path_of("--catches", root.join("xtask/retirement/fault-catches.tsv"));
    let (catches, found) = load_catches(&text(&catches_path)?)?;
    findings.extend(found);
    let policy = Policy::parse(&text(&path_of(
        "--policy",
        root.join("xtask/retirement/groups.json"),
    ))?)?;
    Ok(Loaded {
        behaviours,
        faults,
        groups,
        catches,
        policy,
        tb4_additions,
        findings,
    })
}

/// Runs validation and resolution; returns the resolution and every finding (loading, catalogue,
/// catches, dispositions, status over-claims).
pub(crate) fn run(
    loaded: &Loaded,
    rust_tests: &BTreeSet<String>,
    ledger: &Ledger,
    baseline: &Baseline,
    schema1: &BTreeSet<String>,
) -> (Resolution, Vec<Finding>) {
    let context = behaviours::Context {
        rust_tests: Some(rust_tests),
        ledger: Some(ledger),
    };
    let catalogue = behaviours::validate(&loaded.behaviours, &loaded.faults, &context, None);
    let resolution = resolve(&Inputs {
        behaviours: &loaded.behaviours,
        faults: &loaded.faults,
        groups: &loaded.groups,
        catches: &loaded.catches,
        catalogue_findings: &catalogue,
        rust_tests,
        ledger,
        policy: &loaded.policy,
        baseline,
        schema1_resolved: schema1,
    });
    let mut findings = loaded.findings.clone();
    findings.extend(catalogue);
    findings.extend(resolution.findings.clone());
    let proven = resolution.proven();
    findings.extend(
        behaviours::validate(&loaded.behaviours, &loaded.faults, &context, Some(&proven))
            .into_iter()
            .filter(|f| f.code == "status_overclaim"),
    );
    (resolution, findings)
}

fn count_by<'a>(items: impl Iterator<Item = &'a str>) -> String {
    let mut counts: BTreeMap<&str, usize> = BTreeMap::new();
    for item in items {
        *counts.entry(item).or_default() += 1;
    }
    if counts.is_empty() {
        return "none".into();
    }
    counts
        .iter()
        .map(|(k, n)| format!("{k}={n}"))
        .collect::<Vec<_>>()
        .join(", ")
}
const EXAMPLES: usize = 3;
pub(crate) fn print_findings(findings: &[Finding], full: bool) {
    let mut by_code: BTreeMap<&str, Vec<&Finding>> = BTreeMap::new();
    for finding in findings {
        by_code.entry(finding.code).or_default().push(finding);
    }
    for (code, list) in &by_code {
        println!("FINDINGS {code}: {}", list.len());
        for f in list.iter().take(if full { usize::MAX } else { EXAMPLES }) {
            println!("  FINDING {} {}: {}", f.code, f.subject, f.detail);
        }
    }
}

/// `xtask retire check` schema 2 (`--schema 2`, the default).
pub(crate) fn cli(options: &Options) -> Result<u8, String> {
    let root = root_of(options);
    let Schema1 {
        baseline,
        report,
        rust_tests,
        ledger,
    } = load_schema1(options, &root)?;
    if ledger.is_none() && !options.structure_only {
        return Err(
            "USAGE: schema 2 needs --ledger <registry>/evidence.jsonl (or --structure-only)\n\
                    the evidence ledger is what proves tests and fault catches"
                .into(),
        );
    }
    let loaded = load_registry(options, &root, Some(&baseline))?;
    let schema1_resolved: BTreeSet<String> = baseline
        .ids
        .difference(&report.unresolved)
        .cloned()
        .collect();
    println!(
        "retire (schema 2): {} groups; {} behaviours ({} in BEHAVIOURS.tsv + {} tb4 additions); {} faults; {} catch rows; inventory sha256 {} @ source {}",
        loaded.groups.len(),
        loaded.behaviours.len(),
        loaded.behaviours.len() - loaded.tb4_additions,
        loaded.tb4_additions,
        loaded.faults.len(),
        loaded.catches.len(),
        &baseline.sha256[..12],
        baseline.source_sha.chars().take(8).collect::<String>()
    );
    println!(
        "schema-1 rows (evidence): {} rows, {} of {} old test ids resolved; {} schema-1 findings",
        report.rows,
        report.resolved,
        report.total_old,
        report.findings.len()
    );
    for finding in &report.findings {
        println!(
            "FINDING {} {}: {}",
            finding.code, finding.subject, finding.detail
        );
    }
    let Some(ledger) = ledger else {
        // Structure only: the registry loads and the catalogue is well formed; nothing is judged.
        let context = behaviours::Context {
            rust_tests: Some(&rust_tests),
            ledger: None,
        };
        let mut findings = loaded.findings.clone();
        findings.extend(behaviours::validate(
            &loaded.behaviours,
            &loaded.faults,
            &context,
            None,
        ));
        print_findings(&findings, options.full);
        println!(
            "STRUCTURE-ONLY: files loaded and the catalogue checked without the ledger; groups, behaviours and faults were not resolved. This is not an R6 pass."
        );
        return Ok(u8::from(!findings.is_empty() || !report.structure_ok()));
    };
    let (resolution, findings) = run(&loaded, &rust_tests, &ledger, &baseline, &schema1_resolved);
    print_resolution(&loaded, &resolution, options.full);
    print_findings(&findings, options.full);
    let unproven = resolution
        .behaviours
        .values()
        .filter(|r| !r.is_empty())
        .count();
    let open_faults = resolution
        .faults
        .values()
        .filter(|s| matches!(s, FaultState::Open(_)))
        .count();
    let unresolved = resolution.unresolved_groups();
    let ok = findings.is_empty()
        && report.structure_ok()
        && unresolved == 0
        && unproven == 0
        && open_faults == 0;
    println!(
        "retire (schema 2): {}: {unresolved} groups, {unproven} behaviours and {open_faults} faults unresolved, {} findings",
        if ok { "RESOLVED" } else { "NOT RESOLVED" },
        findings.len() + report.findings.len()
    );
    Ok(u8::from(!ok))
}

fn print_resolution(loaded: &Loaded, resolution: &Resolution, full: bool) {
    let groups = &resolution.groups;
    let resolved = groups.iter().filter(|g| g.via.is_some()).count();
    let tests = |want: bool| -> usize {
        groups
            .iter()
            .filter(|g| g.via.is_some() == want)
            .map(|g| g.n_tests)
            .sum()
    };
    println!(
        "groups: {} total, {resolved} resolved ({} old tests), {} unresolved ({} old tests)",
        groups.len(),
        tests(true),
        groups.len() - resolved,
        tests(false)
    );
    println!(
        "  resolved by: {}",
        count_by(groups.iter().filter_map(|g| g.via.as_deref()))
    );
    println!(
        "  unresolved by reason: {}",
        count_by(
            groups
                .iter()
                .filter(|g| g.via.is_none())
                .flat_map(|g| g.reasons.iter().copied())
        )
    );
    println!(
        "  unresolved by disposition: {}",
        count_by(
            groups
                .iter()
                .filter(|g| g.via.is_none())
                .map(|g| g.disposition.as_str())
        )
    );
    let proven = resolution
        .behaviours
        .values()
        .filter(|r| r.is_empty())
        .count();
    println!(
        "behaviours: {} total, {proven} proven, {} unproven; unproven by reason: {}",
        resolution.behaviours.len(),
        resolution.behaviours.len() - proven,
        count_by(
            resolution
                .behaviours
                .values()
                .flat_map(|r| r.iter().copied())
        )
    );
    let state = |f: &FaultState| match f {
        FaultState::Caught(_) => "caught",
        FaultState::Adjudicated(_) => "adjudicated",
        FaultState::Open(_) => "open",
    };
    println!(
        "faults: {} total: {}; open by reason: {}",
        resolution.faults.len(),
        count_by(resolution.faults.values().map(state)),
        count_by(resolution.faults.values().filter_map(|f| match f {
            FaultState::Open(reason) => Some(*reason),
            _ => None,
        }))
    );
    println!(
        "by area (behaviours proven/total, faults closed/total, groups resolved/total naming the area):"
    );
    let mut areas: BTreeMap<&str, [usize; 6]> = BTreeMap::new();
    for b in &loaded.behaviours {
        let row = areas.entry(b.area.as_str()).or_default();
        row[1] += 1;
        if resolution.behaviours.get(&b.id).is_some_and(Vec::is_empty) {
            row[0] += 1;
        }
    }
    for f in &loaded.faults {
        let row = areas.entry(f.area.as_str()).or_default();
        row[3] += 1;
        if !matches!(
            resolution.faults.get(&f.id),
            Some(FaultState::Open(_)) | None
        ) {
            row[2] += 1;
        }
    }
    for g in groups {
        for area in &g.areas {
            let row = areas.entry(area.as_str()).or_default();
            row[5] += 1;
            if g.via.is_some() {
                row[4] += 1;
            }
        }
    }
    for (area, r) in &areas {
        println!(
            "  {area:<8} behaviours {}/{}  faults {}/{}  groups {}/{}",
            r[0], r[1], r[2], r[3], r[4], r[5]
        );
    }
    let limit = if full { usize::MAX } else { 10 };
    let unproven: Vec<String> = resolution
        .behaviours
        .iter()
        .filter(|(_, r)| !r.is_empty())
        .map(|(id, r)| format!("{id}[{}]", r.join(",")))
        .collect();
    if !unproven.is_empty() {
        println!(
            "unproven behaviours: {}{}",
            unproven
                .iter()
                .take(limit)
                .cloned()
                .collect::<Vec<_>>()
                .join(" "),
            if unproven.len() > limit {
                format!(
                    " ... and {} more (--full lists all)",
                    unproven.len() - limit
                )
            } else {
                String::new()
            }
        );
    }
    let open: Vec<&GroupResult> = groups.iter().filter(|g| g.via.is_none()).collect();
    for g in open.iter().take(limit) {
        println!(
            "  unresolved {} [{}] {}",
            g.id,
            g.disposition,
            g.reasons.join(",")
        );
    }
    if open.len() > limit {
        println!(
            "  ... and {} more unresolved groups (--full lists all)",
            open.len() - limit
        );
    }
}

/// `xtask behaviours check`: the catalogue only (no groups, no Python inventory).
pub(crate) fn behaviours_cli(options: &Options) -> Result<u8, String> {
    let root = root_of(options);
    let loaded = load_registry(options, &root, None)?;
    let rust_tests = crate::retire::discover_rust_tests(&crate::workspace::sources(&root)?)?;
    let ledger = match options.values.get("--ledger") {
        Some(path) => {
            let path = Path::new(path);
            Some(Ledger::parse(
                &text(path)?,
                path.parent().unwrap_or(Path::new(".")),
            )?)
        }
        None if options.structure_only => None,
        None => {
            return Err(
                "USAGE: behaviours check needs --ledger <registry>/evidence.jsonl (or --structure-only)"
                    .into(),
            );
        }
    };
    println!(
        "behaviours: {} ({} in BEHAVIOURS.tsv + {} tb4 additions); faults: {}; Rust tests discovered: {}",
        loaded.behaviours.len(),
        loaded.behaviours.len() - loaded.tb4_additions,
        loaded.tb4_additions,
        loaded.faults.len(),
        rust_tests.len()
    );
    let mut findings = loaded.findings.clone();
    match &ledger {
        Some(ledger) => {
            let baseline = Baseline::default();
            let none = BTreeSet::new();
            let (resolution, all) = run(&loaded, &rust_tests, ledger, &baseline, &none);
            findings = all;
            let proven = resolution.proven().len();
            println!(
                "proven by evidence: {proven} of {} behaviours; faults closed: {} of {}",
                resolution.behaviours.len(),
                resolution
                    .faults
                    .values()
                    .filter(|f| !matches!(f, FaultState::Open(_)))
                    .count(),
                resolution.faults.len()
            );
        }
        None => findings.extend(behaviours::validate(
            &loaded.behaviours,
            &loaded.faults,
            &behaviours::Context {
                rust_tests: Some(&rust_tests),
                ledger: None,
            },
            None,
        )),
    }
    print_findings(&findings, options.full);
    println!(
        "behaviours check: {}{}",
        if findings.is_empty() {
            "OK"
        } else {
            "FINDINGS"
        },
        if ledger.is_none() {
            " (STRUCTURE-ONLY: evidence and proof not checked; this is not a pass)"
        } else {
            ""
        }
    );
    Ok(u8::from(!findings.is_empty()))
}

/// `xtask behaviours ...` entry point.
pub fn behaviours_main(args: &[String]) -> Result<u8, String> {
    if args.first().map(String::as_str) != Some("check") {
        return Err("USAGE: xtask behaviours check [--ledger FILE] [--registry-dir DIR] [--behaviours FILE] [--faults FILE] [--tb4-dir DIR] [--catches FILE] [--policy FILE] [--root DIR] [--structure-only] [--full]".into());
    }
    behaviours_cli(&parse_options(&args[1..], true)?)
}
