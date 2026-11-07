// SPDX-License-Identifier: AGPL-3.0-only
//! `xtask behaviours check`: validation of the behaviour catalogue (`registry/BEHAVIOURS.tsv`,
//! RU-042) and the fault catalogue (`registry/FAULTS.tsv`). The TSVs stay the source of truth.
//!
//! What is checked (every finding is reported, none is fatal on its own):
//!
//! * ids: `B-<AREA>-nn` / `F-<AREA>-nn` with the area equal to the `area` column and one of the
//!   known areas; tb4 additions `NEW-P<n>-nn`; no duplicates;
//! * the 12 / 5 required columns exist and the required cells are not empty;
//! * `v014_source` is `<file>:<lines> ... @ f4e366e9`, `kind` is `+`-joined proof kinds, `oracle`
//!   names its basis, `key_cases` parses (items split on `;` outside parentheses);
//! * references both ways: every fault named by a behaviour's `must_catch` exists and lists that
//!   behaviour in its `must_break`, and the reverse; every area has a positive-control fault;
//! * `rust_tests` are `rs:<path>::<fn>` ids that exist in the workspace (syn discovery),
//!   `evidence` ids exist in the ledger and are PASS gate records;
//! * `status`: open, partial or proven. `proven` is checked against what the resolver computes
//!   from evidence (an over-claim is a finding); the resolver never trusts the column.
use crate::{
    retire::{Finding, Ledger, is_evidence_id},
    tsv::{Table, id_list},
};
use std::collections::{BTreeMap, BTreeSet};

pub const AREAS: [&str; 17] = [
    "INST", "ONB", "SWARM", "CONSENT", "MODEL", "PLAN", "RUN", "CHAT", "ROLL", "NET", "AUTH",
    "FAIL", "HYG", "OBS", "NUM", "LIM", "REL",
];
pub const ORACLE_SHA8: &str = "f4e366e9";
pub const BEHAVIOUR_COLUMNS: [&str; 12] = [
    "id",
    "area",
    "statement",
    "why",
    "v014_source",
    "oracle",
    "kind",
    "key_cases",
    "must_catch",
    "rust_tests",
    "evidence",
    "status",
];
pub const FAULT_COLUMNS: [&str; 5] = ["id", "area", "fault", "must_break", "origin"];

#[derive(Debug, Clone, Default)]
pub struct Behaviour {
    pub id: String,
    pub area: String,
    pub statement: String,
    pub why: String,
    pub v014_source: String,
    pub oracle: String,
    pub kind: String,
    pub key_cases: String,
    pub must_catch: Vec<String>,
    pub rust_tests: Vec<String>,
    pub evidence: Vec<String>,
    pub status: String,
    /// The file the row came from (for messages).
    pub file: String,
}
#[derive(Debug, Clone, Default)]
pub struct Fault {
    pub id: String,
    pub area: String,
    pub fault: String,
    pub must_break: Vec<String>,
    pub origin: String,
    pub file: String,
}

fn finding(code: &'static str, subject: &str, detail: impl Into<String>) -> Finding {
    Finding {
        code,
        subject: subject.to_string(),
        detail: detail.into(),
    }
}
fn ragged(table: &Table, file: &str, out: &mut Vec<Finding>) {
    for (line, got) in &table.ragged {
        out.push(finding(
            "row_columns_invalid",
            &format!("{file}:{line}"),
            format!("{got} cells, header has {}", table.columns.len()),
        ));
    }
}

/// Reads behaviour rows (`BEHAVIOURS.tsv` or a tb4 `*-new-behaviours.tsv`, same 12 columns).
pub fn load_behaviours(text: &str, file: &str) -> Result<(Vec<Behaviour>, Vec<Finding>), String> {
    let table = Table::parse(text, &BEHAVIOUR_COLUMNS, true).map_err(|e| format!("{file}: {e}"))?;
    let mut findings = Vec::new();
    ragged(&table, file, &mut findings);
    let rows = (0..table.rows.len())
        .map(|i| Behaviour {
            id: table.get(i, "id").into(),
            area: table.get(i, "area").into(),
            statement: table.get(i, "statement").into(),
            why: table.get(i, "why").into(),
            v014_source: table.get(i, "v014_source").into(),
            oracle: table.get(i, "oracle").into(),
            kind: table.get(i, "kind").into(),
            key_cases: table.get(i, "key_cases").into(),
            must_catch: id_list(table.get(i, "must_catch")),
            rust_tests: id_list(table.get(i, "rust_tests")),
            evidence: id_list(table.get(i, "evidence")),
            status: table.get(i, "status").into(),
            file: file.into(),
        })
        .collect();
    Ok((rows, findings))
}
pub fn load_faults(text: &str, file: &str) -> Result<(Vec<Fault>, Vec<Finding>), String> {
    let table = Table::parse(text, &FAULT_COLUMNS, true).map_err(|e| format!("{file}: {e}"))?;
    let mut findings = Vec::new();
    ragged(&table, file, &mut findings);
    let rows = (0..table.rows.len())
        .map(|i| Fault {
            id: table.get(i, "id").into(),
            area: table.get(i, "area").into(),
            fault: table.get(i, "fault").into(),
            must_break: id_list(table.get(i, "must_break")),
            origin: table.get(i, "origin").into(),
            file: file.into(),
        })
        .collect();
    Ok((rows, findings))
}

/// What the catalogue check may look at besides the two files themselves.
#[derive(Default)]
pub struct Context<'a> {
    /// Rust test ids that exist (`retire::discover_rust_tests`); `None` skips existence checks.
    pub rust_tests: Option<&'a BTreeSet<String>>,
    /// The evidence ledger; `None` skips evidence checks.
    pub ledger: Option<&'a Ledger>,
}

/// Splits `key_cases` on `;` outside (round or square) brackets. Unbalanced parentheses, an empty item or no item
/// at all is an error (a trailing `;` is tolerated; `[a,b)` intervals balance).
pub fn parse_key_cases(text: &str) -> Result<Vec<String>, String> {
    let mut items = Vec::new();
    let mut depth = 0usize;
    let mut current = String::new();
    for c in text.chars() {
        match c {
            // `[start,end)` half-open intervals are legitimate, so either closer closes either opener.
            '(' | '[' => {
                depth += 1;
                current.push(c);
            }
            ')' | ']' => {
                depth = depth
                    .checked_sub(1)
                    .ok_or("a closing bracket without an opening one".to_string())?;
                current.push(c);
            }
            ';' if depth == 0 => {
                items.push(std::mem::take(&mut current));
            }
            _ => current.push(c),
        }
    }
    if depth != 0 {
        return Err("an opening bracket is never closed".into());
    }
    items.push(current);
    // A trailing `;` leaves one empty last item, which is fine.
    if items.last().is_some_and(|last| last.trim().is_empty()) {
        items.pop();
    }
    if items.is_empty() {
        return Err("no key case".into());
    }
    if items.iter().any(|item| item.trim().is_empty()) {
        return Err("an empty key case between two `;`".into());
    }
    Ok(items.into_iter().map(|i| i.trim().to_string()).collect())
}

fn number_tail(text: &str) -> bool {
    !text.is_empty() && text.bytes().all(|b| b.is_ascii_digit())
}
/// `B-<AREA>-<nn>` / `F-<AREA>-<nn>` -> the area; `None` when the shape is wrong.
fn id_area<'a>(id: &'a str, prefix: &str) -> Option<&'a str> {
    let rest = id.strip_prefix(prefix)?.strip_prefix('-')?;
    let (area, number) = rest.rsplit_once('-')?;
    (!area.is_empty() && area.bytes().all(|b| b.is_ascii_uppercase()) && number_tail(number))
        .then_some(area)
}
/// `NEW-P<n>-<nn>`: a behaviour a TB-4 pass proposes before it joins BEHAVIOURS.tsv.
fn is_new_behaviour_id(id: &str) -> bool {
    id.strip_prefix("NEW-P")
        .and_then(|rest| rest.split_once('-'))
        .is_some_and(|(pass, number)| number_tail(pass) && number_tail(number))
}
/// A `v014_source` cell ends in ` @ f4e366e9` and cites at least one `<file.ext>:<line>`.
fn source_ok(text: &str) -> bool {
    let Some(body) = text.strip_suffix(&format!(" @ {ORACLE_SHA8}")) else {
        return false;
    };
    let bytes = body.as_bytes();
    (0..bytes.len()).any(|i| {
        if bytes[i] != b':' || !bytes.get(i + 1).is_some_and(u8::is_ascii_digit) {
            return false;
        }
        let word: &str = body[..i]
            .rsplit(|c: char| !(c.is_ascii_alphanumeric() || "_./-".contains(c)))
            .next()
            .unwrap_or("");
        word.contains('.') && !word.starts_with('.') && !word.ends_with('.')
    })
}
fn kind_ok(text: &str) -> bool {
    text.split('+')
        .all(|part| part.len() == 1 && "VUMSJND".contains(part))
}
/// `author-reference golden` (RU-050): the existing author-derived Qwen fixtures; valid for the
/// numerical area only (V02-PLAN section 1.4).
const AUTHOR_REFERENCE_GOLDEN: &str = "author-reference golden";
fn oracle_ok(text: &str, area: &str) -> bool {
    ["executed-v0.1.4", "independent-model", "spec"]
        .iter()
        .any(|basis| text.starts_with(basis))
        || (area == "NUM" && text.starts_with(AUTHOR_REFERENCE_GOLDEN))
}
fn empty(code_subject: &str, column: &str, out: &mut Vec<Finding>) {
    out.push(finding(
        "column_empty",
        code_subject,
        format!("{column} is empty"),
    ));
}

/// Validates the catalogue (see the module documentation). `proven` lists the behaviours the
/// resolver computed as proven (to catch an over-claimed `status`); `None` skips that check.
pub fn validate(
    behaviours: &[Behaviour],
    faults: &[Fault],
    context: &Context,
    proven: Option<&BTreeSet<String>>,
) -> Vec<Finding> {
    let mut out = Vec::new();
    let mut seen: BTreeSet<&str> = BTreeSet::new();
    for b in behaviours {
        let id = b.id.as_str();
        if !seen.insert(id) {
            out.push(finding("id_duplicate", id, format!("twice ({})", b.file)));
        }
        match id_area(id, "B") {
            Some(area) => {
                if area != b.area {
                    out.push(finding(
                        "area_prefix_mismatch",
                        id,
                        format!("id says {area}, area column says {}", b.area),
                    ));
                }
            }
            None if is_new_behaviour_id(id) => {}
            None => out.push(finding("id_invalid", id, "expected B-<AREA>-<nn>")),
        }
        if !AREAS.contains(&b.area.as_str()) {
            out.push(finding("area_unknown", id, format!("area {:?}", b.area)));
        }
        for (column, value) in [
            ("statement", &b.statement),
            ("why", &b.why),
            ("v014_source", &b.v014_source),
            ("oracle", &b.oracle),
            ("kind", &b.kind),
            ("key_cases", &b.key_cases),
            ("status", &b.status),
        ] {
            if value.trim().is_empty() {
                empty(id, column, &mut out);
            }
        }
        if b.must_catch.is_empty() {
            empty(id, "must_catch", &mut out);
        }
        if !b.v014_source.is_empty() && !source_ok(&b.v014_source) {
            out.push(finding(
                "v014_source_invalid",
                id,
                format!(
                    "expected `<file>:<lines> ... @ {ORACLE_SHA8}`, got {:?}",
                    b.v014_source
                ),
            ));
        }
        if !b.kind.is_empty() && !kind_ok(&b.kind) {
            out.push(finding(
                "kind_invalid",
                id,
                format!("{:?}: proof kinds V U M S J N D joined by +", b.kind),
            ));
        }
        if !b.oracle.is_empty() && !oracle_ok(&b.oracle, &b.area) {
            out.push(finding(
                "oracle_invalid",
                id,
                format!(
                    "{:?}: executed-v0.1.4, independent-model, spec or (NUM only) author-reference golden",
                    b.oracle
                ),
            ));
        }
        if !b.key_cases.trim().is_empty()
            && let Err(why) = parse_key_cases(&b.key_cases)
        {
            out.push(finding("key_cases_invalid", id, why));
        }
        match b.status.as_str() {
            "" | "open" => {}
            "partial" | "proven" => {
                if b.rust_tests.is_empty() {
                    empty(id, "rust_tests", &mut out);
                }
                if b.evidence.is_empty() {
                    empty(id, "evidence", &mut out);
                }
            }
            other => out.push(finding(
                "status_invalid",
                id,
                format!("{other:?}: open, partial or proven"),
            )),
        }
        for test in &b.rust_tests {
            if !test.starts_with("rs:") || !test.contains(".rs::") {
                out.push(finding(
                    "rust_test_malformed",
                    id,
                    format!("{test}: expected rs:<path>.rs::<fn>"),
                ));
            } else if context
                .rust_tests
                .is_some_and(|known| !known.contains(test))
            {
                out.push(finding(
                    "rust_test_unknown",
                    id,
                    format!("no such Rust test: {test}"),
                ));
            }
        }
        for ev in &b.evidence {
            if !is_evidence_id(ev) {
                out.push(finding(
                    "evidence_malformed",
                    id,
                    format!("{ev:?} is not an EV-yyyymmdd-nnnn id"),
                ));
            } else if let Some(ledger) = context.ledger {
                if !ledger.ids.contains(ev) {
                    out.push(finding(
                        "evidence_unknown",
                        id,
                        format!("{ev} is not in the ledger"),
                    ));
                } else if !ledger.pass_gates.contains(ev) {
                    out.push(finding(
                        "evidence_not_pass_gate",
                        id,
                        format!("{ev} is not a PASS gate record"),
                    ));
                }
            }
        }
        if b.status == "proven" && proven.is_some_and(|set| !set.contains(id)) {
            out.push(finding(
                "status_overclaim",
                id,
                "status proven, but its tests, evidence or faults do not prove it",
            ));
        }
    }

    let fault_by_id: BTreeMap<&str, &Fault> = faults.iter().map(|f| (f.id.as_str(), f)).collect();
    let behaviour_by_id = by_id(behaviours);
    let mut seen_faults: BTreeSet<&str> = BTreeSet::new();
    for f in faults {
        let id = f.id.as_str();
        if !seen_faults.insert(id) {
            out.push(finding("id_duplicate", id, format!("twice ({})", f.file)));
        }
        match id_area(id, "F") {
            Some(area) => {
                if area != f.area {
                    out.push(finding(
                        "area_prefix_mismatch",
                        id,
                        format!("id says {area}, area column says {}", f.area),
                    ));
                }
            }
            None => out.push(finding("id_invalid", id, "expected F-<AREA>-<nn>")),
        }
        if !AREAS.contains(&f.area.as_str()) {
            out.push(finding("area_unknown", id, format!("area {:?}", f.area)));
        }
        for (column, value) in [("fault", &f.fault), ("origin", &f.origin)] {
            if value.trim().is_empty() {
                empty(id, column, &mut out);
            }
        }
        if f.must_break.is_empty() {
            empty(id, "must_break", &mut out);
        }
    }
    // References, both ways. One finding per subject lists every offending id.
    for b in behaviours {
        let unknown: Vec<&String> = b
            .must_catch
            .iter()
            .filter(|f| !fault_by_id.contains_key(f.as_str()))
            .collect();
        if !unknown.is_empty() {
            out.push(finding(
                "must_catch_unknown_fault",
                &b.id,
                format!("{unknown:?}"),
            ));
        }
        let back: Vec<&String> = b
            .must_catch
            .iter()
            .filter(|f| {
                fault_by_id
                    .get(f.as_str())
                    .is_some_and(|fault| !fault.must_break.contains(&b.id))
            })
            .collect();
        if !back.is_empty() {
            out.push(finding(
                "fault_back_reference_missing",
                &b.id,
                format!("{back:?} do not list this behaviour in must_break"),
            ));
        }
    }
    for f in faults {
        let unknown: Vec<&String> = f
            .must_break
            .iter()
            .filter(|b| !behaviour_by_id.contains_key(b.as_str()))
            .collect();
        if !unknown.is_empty() {
            out.push(finding(
                "must_break_unknown_behaviour",
                &f.id,
                format!("{unknown:?}"),
            ));
        }
        let back: Vec<&String> = f
            .must_break
            .iter()
            .filter(|b| {
                behaviour_by_id
                    .get(b.as_str())
                    .is_some_and(|behaviour| !behaviour.must_catch.contains(&f.id))
            })
            .collect();
        if !back.is_empty() {
            out.push(finding(
                "behaviour_back_reference_missing",
                &f.id,
                format!("{back:?} do not list this fault in must_catch"),
            ));
        }
    }
    let areas: BTreeSet<&str> = behaviours.iter().map(|b| b.area.as_str()).collect();
    for area in areas {
        if !faults
            .iter()
            .any(|f| f.area == area && f.origin == "positive-control")
        {
            out.push(finding(
                "positive_control_missing",
                area,
                "the area has no positive-control fault (origin positive-control)",
            ));
        }
    }
    out
}

pub fn evidence_id_ok(text: &str) -> bool {
    is_evidence_id(text)
}
pub fn by_id(behaviours: &[Behaviour]) -> BTreeMap<&str, &Behaviour> {
    behaviours.iter().map(|b| (b.id.as_str(), b)).collect()
}
