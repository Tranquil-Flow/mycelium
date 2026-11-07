// SPDX-License-Identifier: AGPL-3.0-only
//! Source-unbounded family (owner ruling 2026-10-28, option a).
//!
//! A runtime-supplied request/config/policy field for which v0.1.4 ships no ceiling is registered as an
//! explicit `Value::Blocked` id whose provenance reads `v0.1.4 ships no ceiling at path:line ...` and is
//! tagged V03 (a ceiling is designed in v0.3, never here). The port accepts exactly what v0.1.4 accepts:
//! this family adds no ceiling and no refusal. Membership is an exact pinned table
//! (`python-source-unbounded.tsv`); a field with any v0.1.4 range check in the census is refused.
use super::{Bound, Candidate, candidates_ref};
use budgets::{
    BudgetId,
    table::{Provenance, Value},
};
use std::collections::BTreeMap;

pub const PREFIX: &str = "source-unbounded:v03;";
pub const DISPOSITION: &str = "source-unbounded";
const PIN: &str = "ef9ae61d";
const TABLE: &str = include_str!("../../data/python-source-unbounded.tsv");
const OCCURRENCES: &str = include_str!("../../data/python-field-occurrences.tsv");

fn quoted(text: &str, field: &str) -> bool {
    text.contains(&format!("'{field}'")) || text.contains(&format!("\"{field}\""))
}

/// True when some census comparison names the quoted field with an ordering operator against anything but a
/// literal zero floor (zero floors and `in {..}` membership tests are not ceilings).
pub fn field_has_census_range_check(field: &str) -> bool {
    candidates_ref().is_ok_and(|rows| {
        rows.iter().any(|c| {
            let e = c.expression.trim();
            c.kind == "comparison"
                && quoted(&c.source, field)
                && (e.contains('<') || e.contains('>'))
                && !["<= 0", "< 0", "<= 0.0", "< 0.0"]
                    .iter()
                    .any(|zero| e.ends_with(zero))
        })
    })
}

pub const OCCURRENCE_HEADER: &str = "field\tpath\tline\tverdict\twitness\talias";
/// Pinned verdicts of occurrences of a validated document field in the oracle. None is a ceiling: `read` and
/// `declare`/`write` are uses, `floor` a lower-bound check, `type-guard` a lower-bound check whose helper has an int64 default maximum (a type guard, judged not a policy ceiling), `relative` a comparison of the field (or the local
/// it is read into) with an unrelated quantity, `other-document` a same-named key of a different document.
pub const VERDICTS: [&str; 7] = [
    "read",
    "declare",
    "write",
    "floor",
    "type-guard",
    "relative",
    "other-document",
];

/// Family `o4` (validated document field): the key's every pinned occurrence has a non-ceiling verdict (an
/// unknown verdict, e.g. `ceiling`, refuses), and the row's own read is pinned as `read` or `floor` at its line
/// or the next two (which also proves the field is pinned at all). The pins are re-derived from the oracle by the tests
/// (`inventory_validated_document_field`); a text-only check here cannot prove the negative alone.
fn negative_proof(path: &str, line: usize, field: &str, occurrences: &str) -> bool {
    let mut rows = occurrences.lines();
    if rows.next() != Some(OCCURRENCE_HEADER) {
        return false;
    }
    let mut own = false;
    for l in rows {
        let f: Vec<_> = l.split('\t').collect();
        if f.len() != 6 {
            return false;
        }
        if f[0] != field {
            continue;
        }
        if !VERDICTS.contains(&f[3]) {
            return false;
        }
        own |= f[1] == path
            && matches!(f[3], "read" | "floor" | "type-guard")
            && f[2]
                .parse::<usize>()
                .is_ok_and(|n| (line..=line + 2).contains(&n));
    }
    own
}

pub fn validate(row: &Bound, candidates: &BTreeMap<String, Candidate>) -> bool {
    validate_with(row, candidates, TABLE, OCCURRENCES)
}

/// `validate` over an explicit family table and occurrence table (tests pass modified text).
pub fn validate_with(
    row: &Bound,
    candidates: &BTreeMap<String, Candidate>,
    table: &str,
    occurrences: &str,
) -> bool {
    let mut lines = table
        .lines()
        .skip(1)
        .filter(|l| l.split('\t').next() == Some(row.key.as_str()));
    let (Some(line), None) = (lines.next(), lines.next()) else {
        return false;
    };
    let f: Vec<_> = line.split('\t').collect();
    if f.len() != 4 || f[0] != row.key || !matches!(f[1], "o1" | "o2" | "o3" | "o4") {
        return false;
    }
    let (field, id) = (f[2], f[3]);
    let Some(c) = candidates.get(&row.key) else {
        return false;
    };
    let blocked_here = BudgetId::from_name(id).is_some_and(|b| {
        let entry = b.entry();
        entry.value == Value::Blocked
            && matches!(entry.provenance, Provenance::Blocked(text)
                if text.starts_with(&format!(
                        "v0.1.4 ships no ceiling at {}:{} @ {PIN}; field {field};", c.path, c.line))
                    && text.contains("V03"))
    });
    blocked_here
        && c.path == row.path
        && c.line == row.line
        && c.column == row.column
        && c.scope == row.scope
        && c.symbol == row.symbol
        && c.expression == row.expression
        && quoted(&c.expression, field)
        && !field_has_census_range_check(field)
        && (f[1] != "o4" || negative_proof(&c.path, c.line, field, occurrences))
        && row.review.starts_with(PREFIX)
        && row.disposition == DISPOSITION
        && row.relation == id
        && row.budget.is_empty()
        && row.value.is_none()
        && row.unit == "not-applicable"
}
