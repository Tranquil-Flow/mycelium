// SPDX-License-Identifier: AGPL-3.0-only
//! Source-unbounded propagation (RU-033): a row whose ONLY sources are source-unbounded rows is itself
//! source-unbounded (RU-014) and carries its own `Value::Blocked` registry id. The numeric engines
//! (`forward-callers`, `field-origin`) keep refusing Blocked targets (a derived row may not point at a Blocked
//! id); this rule is the separate place where "every source accepts any value" is stated, per row.
//!
//! Rules (`python-unbounded-propagation.tsv`, one line per row):
//! * `required-parameter`: the census row forwards a required keyword-only parameter of its function unchanged
//!   (or under the builtin `float`); the pinned `sources` are the call-site keyword rows, in ascending key order,
//!   each with its call-statement span (`call` label, exact sha256). That the callee is defined once, is never
//!   rebound and is called only at the pinned sites is pinned and re-derived from the oracle by the tests.
//! * `self-attribute`: the census row reads `self.F` in a method of the pinned class; the sources are the
//!   class's stores of `self.F` (the census lists every one, so none may be missing), pinned with `class`,
//!   `method` and `store` spans. Same file-level limitation as `field_origins` (no dynamic attribute write).
//!
//! What this module re-checks: the row identity, its own Blocked id (provenance names this site and
//! "propagated"), every source being a reviewed `source-unbounded` row with its own id and no unit, the
//! ascending distinct source list, the parameter shape, second roots, census store completeness and the span
//! geometry. A source that is derived, pending or numeric refuses the row.
use super::field_origins::{identifier, parse_span};
use super::{Bound, Candidate};
use budgets::{
    BudgetId,
    table::{Provenance, Value},
};
use std::collections::{BTreeMap, BTreeSet};

pub const HEADER: &str = "key\trule\treceiver\tclass_file\tclass\tfield\tsources\treference";
pub const PREFIX: &str = "source-unbounded-propagated:";
pub const TABLE: &str = include_str!("../../data/python-unbounded-propagation.tsv");
pub const RULES: [&str; 2] = ["required-parameter", "self-attribute"];
const PIN: &str = "ef9ae61d";

struct Entry<'a> {
    rule: &'a str,
    receiver: &'a str,
    class_file: &'a str,
    class: &'a str,
    field: &'a str,
    sources: &'a str,
    reference: &'a str,
}

/// The single table entry of `key`; `None` for a malformed table or a missing/duplicated entry.
fn entry_of<'a>(table: &'a str, key: &str) -> Option<Entry<'a>> {
    let mut lines = table.lines();
    if lines.next() != Some(HEADER) {
        return None;
    }
    let mut found = None;
    for line in lines {
        let f: Vec<_> = line.split('\t').collect();
        if f.len() != 8 || f.iter().any(|s| s.trim().is_empty()) {
            return None;
        }
        if f[0] == key {
            if found.is_some() {
                return None;
            }
            found = Some(Entry {
                rule: f[1],
                receiver: f[2],
                class_file: f[3],
                class: f[4],
                field: f[5],
                sources: f[6],
                reference: f[7],
            });
        }
    }
    found
}

pub fn sources_of(text: &str) -> BTreeSet<&str> {
    text.lines()
        .skip(1)
        .filter_map(|l| l.split('\t').next())
        .collect()
}

pub fn validate(row: &Bound, rows: &[Bound], candidates: &BTreeMap<String, Candidate>) -> bool {
    validate_with(row, rows, candidates, TABLE)
}

/// The row's own registry id is a Blocked entry whose provenance names this very site and says "propagated".
fn own_id(row: &Bound, c: &Candidate, lookup: RegistryLookup) -> bool {
    lookup(&row.relation).is_some_and(|(blocked, text)| {
        blocked
            && text.starts_with(&format!(
                "v0.1.4 ships no ceiling at {}:{} @ {PIN}; field {};",
                c.path, c.line, c.symbol
            ))
            && text.contains("V03")
            && text.contains("propagated")
    })
}

/// A reviewed source-unbounded row with an id of its own that is a Blocked registry entry.
fn unbounded_source<'a>(rows: &'a [Bound], key: &str, lookup: RegistryLookup) -> Option<&'a Bound> {
    let b = rows.iter().find(|b| b.key == key)?;
    (b.disposition == "source-unbounded"
        && b.budget.is_empty()
        && b.value.is_none()
        && b.unit == "not-applicable"
        && lookup(&b.relation).is_some_and(|(blocked, _)| blocked))
    .then_some(b)
}

/// What the registry says about an id: `(is_blocked, provenance text of a Blocked entry)`.
pub type RegistryLookup<'a> = &'a dyn Fn(&str) -> Option<(bool, String)>;

fn registry(name: &str) -> Option<(bool, String)> {
    let entry = BudgetId::from_name(name)?.entry();
    let text = match entry.provenance {
        Provenance::Blocked(text) => text.to_string(),
        _ => String::new(),
    };
    Some((entry.value == Value::Blocked, text))
}

pub fn validate_with(
    row: &Bound,
    rows: &[Bound],
    candidates: &BTreeMap<String, Candidate>,
    table: &str,
) -> bool {
    validate_with_registry(row, rows, candidates, table, &registry)
}

/// `validate_with` over an injectable registry (the static table cannot be forged, so tests pass their own).
pub fn validate_with_registry(
    row: &Bound,
    rows: &[Bound],
    candidates: &BTreeMap<String, Candidate>,
    table: &str,
    lookup: RegistryLookup,
) -> bool {
    let Some(claim) = row.review.strip_prefix(PREFIX) else {
        return false;
    };
    let rule = claim.split(';').next().unwrap_or("");
    let Some(entry) = entry_of(table, &row.key) else {
        return false;
    };
    let Some(c) = candidates.get(&row.key) else {
        return false;
    };
    if !RULES.contains(&rule)
        || entry.rule != rule
        || c.path != row.path
        || c.line != row.line
        || c.column != row.column
        || c.scope != row.scope
        || c.symbol != row.symbol
        || c.expression != row.expression
        || row.disposition != "source-unbounded"
        || !row.budget.is_empty()
        || row.value.is_some()
        || row.unit != "not-applicable"
        || !own_id(row, c, lookup)
        || entry.class_file != c.path
    {
        return false;
    }
    // Sources: ascending and distinct, each a reviewed source-unbounded row with an id other than this row's.
    let source_keys: Vec<&str> = entry.sources.split(';').collect();
    if source_keys.windows(2).any(|w| w[0] >= w[1]) {
        return false;
    }
    let mut source_cands = Vec::with_capacity(source_keys.len());
    for key in &source_keys {
        let (Some(s), Some(bound)) = (candidates.get(*key), unbounded_source(rows, key, lookup))
        else {
            return false;
        };
        // (a row listing itself has the same id, so the id comparison covers that case too)
        if bound.relation == row.relation {
            return false;
        }
        // No two-row cycle: a propagated source must not name this row as one of its own sources.
        if bound.review.starts_with(PREFIX)
            && entry_of(table, key).is_some_and(|e| e.sources.split(';').any(|k| k == row.key))
        {
            return false;
        }
        source_cands.push(s);
    }
    let spans: Option<Vec<_>> = entry.reference.split(" | ").map(parse_span).collect();
    let Some(spans) = spans else {
        return false;
    };
    match rule {
        "required-parameter" => required_parameter(&entry, c, &source_cands, &spans, candidates),
        _ => self_attribute(&entry, row, c, &source_cands, &spans, candidates),
    }
}

fn required_parameter(
    entry: &Entry,
    c: &Candidate,
    sources: &[&Candidate],
    spans: &[super::field_origins::Span],
    candidates: &BTreeMap<String, Candidate>,
) -> bool {
    let param = entry.field;
    // The forwarded value is the parameter itself or `float(parameter)` (the builtin; the tests show it is never
    // rebound in the file), in the function whose qualified scope is pinned as the callee.
    let value = c
        .expression
        .strip_prefix("float(")
        .and_then(|inner| inner.strip_suffix(')'))
        .unwrap_or(c.expression.as_str());
    if entry.receiver != "-"
        || entry.class != c.scope
        || !identifier(param)
        || value != param
        || !matches!(
            c.kind.as_str(),
            "keyword" | "dict-field" | "assignment" | "positional-call" | "positional-timeout"
        )
    {
        return false;
    }
    // A census row for the parameter (a default) or an assignment to the name in the scope is a second root.
    if candidates.values().any(|o| {
        o.path == c.path
            && o.scope == c.scope
            && o.symbol == param
            && matches!(o.kind.as_str(), "parameter" | "assignment")
    }) {
        return false;
    }
    // One call-statement span per source, in the same order, holding the source's keyword.
    spans.len() == sources.len()
        && spans.iter().zip(sources).all(|(span, s)| {
            span.label == "call"
                && span.path == s.path
                && span.first <= s.line
                && s.line <= span.last
                && s.kind == "keyword"
                && s.symbol == param
        })
}

fn self_attribute(
    entry: &Entry,
    row: &Bound,
    c: &Candidate,
    sources: &[&Candidate],
    spans: &[super::field_origins::Span],
    candidates: &BTreeMap<String, Candidate>,
) -> bool {
    let (class, field) = (entry.class, entry.field);
    let read = format!("self.{field}");
    let class_prefix = format!("{class}.");
    if entry.receiver != "self"
        || !identifier(class)
        || !identifier(field)
        || c.expression != read
        || !c.scope.strip_prefix(&class_prefix).is_some_and(identifier)
    {
        return false;
    }
    // A census assignment or parameter of `self` in the scope is a second root.
    if candidates.values().any(|o| {
        o.path == c.path
            && o.scope == c.scope
            && o.symbol == "self"
            && matches!(o.kind.as_str(), "parameter" | "assignment")
    }) {
        return false;
    }
    // Sources: stores `self.F = ...` of the class; the census lists every store, none may be missing.
    if !sources.iter().all(|s| {
        s.kind == "assignment"
            && s.path == c.path
            && s.symbol == read
            && s.scope.strip_prefix(&class_prefix).is_some_and(identifier)
    }) || candidates.values().any(|o| {
        o.path == c.path
            && o.kind == "assignment"
            && o.symbol == read
            && o.scope.starts_with(&class_prefix)
            && !sources.iter().any(|s| s.key == o.key)
    }) {
        return false;
    }
    // Spans: class, method, then one store per source (all in the row's file, geometry as field_origins).
    let n = sources.len();
    if spans.len() != 2 + n {
        return false;
    }
    let (class_span, method) = (&spans[0], &spans[1]);
    let stores = &spans[2..];
    let inside = |inner: &super::field_origins::Span, outer: &super::field_origins::Span| {
        outer.first <= inner.first && inner.last <= outer.last
    };
    class_span.label == "class"
        && method.label == "method"
        && stores.iter().all(|s| s.label == "store")
        && class_span.path == row.path
        && method.path == row.path
        && inside(method, class_span)
        && method.first <= row.line
        && row.line <= method.last
        && stores.iter().zip(sources).all(|(s, w)| {
            s.path == row.path && inside(s, class_span) && s.first <= w.line && w.line <= s.last
        })
}
