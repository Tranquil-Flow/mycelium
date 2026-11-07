// SPDX-License-Identifier: AGPL-3.0-only
//! RU-023: a value that is only MEASURED and then recorded is an observation, not a limit, when a pinned
//! flow (`python-observation-flows.tsv`, traced in the oracle AST at f4e366e9) reaches only observation
//! sinks: no comparison, no wait/timeout argument, no clamp. The table lists all 26 forecast rows of the two
//! measured families with a verdict; only `observation` rows may be reviewed under this module. The census
//! cannot show callee-side flows, so the table pins them (exact-span sha) and this module re-checks every
//! census-visible fact: the row identity, the carrier shape, the absence of a comparison/call/keyword census
//! row that reads the carrier in the same scope, and the closed target of a forwarded keyword.
use super::{Bound, Candidate};
use std::collections::BTreeMap;

pub const HEADER: &str = "key\tverdict\trule\tcarrier\ttarget_key\tsinks\treference";
pub const PREFIX: &str = "observation-flow:";
pub const TABLE: &str = include_str!("../../data/python-observation-flows.tsv");
pub const RULES: [&str; 2] = ["record-field", "forwarded-record-field"];
const ORACLE: &str = "@ f4e366e98a1f705dfaf182897044500d290dbd7f";
/// Sinks that record or validate a value without limiting anything with it.
const RECORD_SINKS: [&str; 3] = ["return-dict", "is-none-check", "floor-validator"];
const FORWARD_SINK: &str = "keyword-to-record-field";

struct Entry<'a> {
    rule: &'a str,
    carrier: &'a str,
    target_key: &'a str,
    sinks: &'a str,
    reference: &'a str,
}

/// The single `observation` entry of `key`; `None` for a malformed table, a missing or duplicated entry, or a
/// verdict other than `observation`.
fn entry_of<'a>(table: &'a str, key: &str) -> Option<Entry<'a>> {
    let mut lines = table.lines();
    if lines.next() != Some(HEADER) {
        return None;
    }
    let mut found = None;
    for line in lines {
        let f: Vec<_> = line.split('\t').collect();
        if f.len() != 7 || f[0].is_empty() || f[1].is_empty() || f[3].is_empty() || f[5].is_empty()
        {
            return None;
        }
        if f[0] == key {
            if found.is_some() || f[1] != "observation" {
                return None;
            }
            found = Some(Entry {
                rule: f[2],
                carrier: f[3],
                target_key: f[4],
                sinks: f[5],
                reference: f[6],
            });
        }
    }
    found
}

fn identifier(s: &str) -> bool {
    !s.is_empty()
        && !s.starts_with(|c: char| c.is_ascii_digit())
        && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

/// Whether `expr` contains `word` as a whole identifier.
fn mentions(expr: &str, word: &str) -> bool {
    let bytes = expr.as_bytes();
    let is_word = |b: u8| b.is_ascii_alphanumeric() || b == b'_';
    expr.match_indices(word).any(|(i, _)| {
        (i == 0 || !is_word(bytes[i - 1])) && bytes.get(i + word.len()).is_none_or(|b| !is_word(*b))
    })
}

pub fn validate(row: &Bound, rows: &[Bound], candidates: &BTreeMap<String, Candidate>) -> bool {
    validate_with(row, rows, candidates, TABLE)
}

pub fn validate_with(
    row: &Bound,
    rows: &[Bound],
    candidates: &BTreeMap<String, Candidate>,
    table: &str,
) -> bool {
    let Some(claim) = row.review.strip_prefix(PREFIX) else {
        return false;
    };
    let rule = claim.split(';').next().unwrap_or("");
    if !RULES.contains(&rule) {
        return false;
    }
    let Some(entry) = entry_of(table, &row.key) else {
        return false;
    };
    let Some(c) = candidates.get(&row.key) else {
        return false;
    };
    // The reviewed row is the census row, as a not-a-bound observation with no budget.
    if entry.rule != rule
        || c.path != row.path
        || c.line != row.line
        || c.column != row.column
        || c.scope != row.scope
        || c.symbol != row.symbol
        || c.expression != row.expression
        || row.disposition != "not-a-bound"
        || !row.budget.is_empty()
        || row.value.is_some()
        || row.unit != "not-applicable"
        || row.relation != format!("measured-observation-not-a-limit; flow={rule}")
        || !entry.reference.starts_with(&format!("{}:", c.path))
        || !entry.reference.contains("exact-span-sha256=")
        || !entry.reference.contains(ORACLE)
        || c.expression != c.symbol
        || !identifier(&c.symbol)
    {
        return false;
    }
    let here = format!("{}:{}", c.path, c.line);
    let sinks: Vec<&str> = entry.sinks.split(';').collect();
    if rule == "record-field" {
        // The carrier is a record field: the sole use of the parameter is the returned dict; readers of the key
        // only test for None and apply the non-negative finite validator (pinned as sink kinds).
        let well_formed = sinks.iter().all(|s| {
            s.split_once(':').is_some_and(|(kind, at)| {
                RECORD_SINKS.contains(&kind)
                    && at.rsplit_once(':').is_some_and(|(p, n)| {
                        p.ends_with(".py") && n.parse::<usize>().is_ok_and(|n| n > 0)
                    })
            })
        });
        let own = format!("return-dict:{here}");
        if !well_formed
            || sinks.iter().filter(|s| **s == own).count() != 1
            || sinks
                .iter()
                .filter(|s| s.starts_with("return-dict:"))
                .count()
                != 1
            || c.kind != "dict-field"
            || entry.carrier != c.symbol
            || !entry.target_key.is_empty()
        {
            return false;
        }
        // Mechanical census sink check: no comparison, call-argument or keyword row of this scope reads the carrier.
        return !candidates.values().any(|o| {
            o.path == c.path
                && o.scope == c.scope
                && matches!(
                    o.kind.as_str(),
                    "comparison" | "keyword" | "positional-call" | "positional-timeout"
                )
                && mentions(&o.expression, &c.symbol)
        });
    }
    // forwarded-record-field: a keyword argument whose callee parameter is a closed record-field observation.
    let own = format!("{FORWARD_SINK}:{here}");
    if sinks != [own.as_str()] || c.kind != "keyword" || !identifier(entry.carrier) {
        return false;
    }
    let (Some(target_row), Some(t)) = (
        rows.iter().find(|r| r.key == entry.target_key),
        candidates.get(entry.target_key),
    ) else {
        return false;
    };
    target_row
        .review
        .starts_with("observation-flow:record-field; ")
        && validate_with(target_row, rows, candidates, table)
        && t.path == c.path
        && t.symbol == c.symbol
        && t.scope == entry.carrier
}
