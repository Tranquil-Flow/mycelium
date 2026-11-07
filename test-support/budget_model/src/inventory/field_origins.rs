// SPDX-License-Identifier: AGPL-3.0-only
//! Class/field resolution, slices 1 and 2 (`CLASS-FIELD-DESIGN.md` sections 3 and 6).
//!
//! Slice 1, rule `local-constructor`: `v.field` where `v` is a local bound
//! exactly once, by `v = Cls(...)`, to a frozen dataclass whose field has no default and whose constructor
//! calls are all keyword-only. The read is then one of the values the constructor keywords carry, so it
//! derives from the ordered `+` of every writer row (the same claim `forward-callers` makes for a forwarded
//! parameter). The census cannot show the class, the binding or the completeness of the constructor calls,
//! so `python-field-origins.tsv` pins them with exact-span hashes and this module re-checks every
//! census-visible fact: the row identity and `receiver.field` shape, no census assignment of the receiver
//! in the scope, the writer rows (ascending, keyword rows of the field, reviewed, one unit), the relation,
//! and the geometry of the pinned spans (field inside class, binding inside the read's function and before
//! the read, one constructor span per writer, no second constructor inside the function).
//!
//! Slice 2, rule `self-attribute`: `self.field` read in a method `C.m`; the writers are the stores
//! `self.field = ...` of `C`, which the census lists completely (every census assignment row of that symbol
//! in the class must be a listed writer). The class, the method, `self` never being rebound, the absence of
//! subclasses, outside attribute writes and dynamic attribute writes are pinned (spans) and re-derived from
//! the oracle by the tests; the pinned spans are class, method and one `store` per writer.
//!
//! Slice 5, rule `self-clamp` and inactive initialisers: a row `self.X = min(self.F, other)` (the census
//! expression is exactly that call, two top-level arguments, the second free of string literals) is bounded by
//! its first operand, so it derives from the stores of `self.F` like a `self-attribute` read of `self.F`
//! (`min` is the builtin: the tests show the file never rebinds it). A store `self.F = None` reviewed as
//! `inactive-policy-state` (no budget, no value) confers no value: it must still be a listed store with its own
//! span, but contributes no id, and at least one live store must remain.
//!
//! Limitation (stated, not hidden): the "no dynamic attribute write" fact is FILE-LEVEL. The tests show that no
//! `setattr(`, `__dict__` or `vars(` occurs in any in-scope file naming the class (and none inside the class
//! body); they do not type-check that no instance of the class reaches a dynamic writer through another name or
//! an untyped container. That residual is what a type-level proof would add.
use super::{Bound, Candidate};
use std::collections::BTreeMap;

pub const HEADER: &str = "key\trule\treceiver\tclass_file\tclass\tfield\twriters\treference";
pub const PREFIX: &str = "field-origin:";
pub const TABLE: &str = include_str!("../../data/python-field-origins.tsv");
pub const RULES: [&str; 3] = ["local-constructor", "self-attribute", "self-clamp"];
pub const ORACLE: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";

struct Entry<'a> {
    rule: &'a str,
    receiver: &'a str,
    class_file: &'a str,
    class: &'a str,
    field: &'a str,
    writers: &'a str,
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
                writers: f[6],
                reference: f[7],
            });
        }
    }
    found
}

/// `self.F` when `expr` is exactly `min(self.F, other)`: two top-level arguments, the second non-empty, balanced
/// and free of string literals (the census text is normalised, so the separator is exactly `, `).
fn clamp_operand(expr: &str) -> Option<&str> {
    let inner = expr.strip_prefix("min(")?.strip_suffix(')')?;
    let (first, rest) = inner.split_once(", ")?;
    if rest.is_empty() || rest.contains(['\'', '"']) {
        return None;
    }
    let mut depth = 0usize;
    for ch in rest.chars() {
        match ch {
            '(' | '[' | '{' => depth += 1,
            ')' | ']' | '}' => depth = depth.checked_sub(1)?,
            ',' if depth == 0 => return None,
            _ => {}
        }
    }
    (depth == 0).then_some(first)
}

/// A reviewed inactive initialiser: no budget, no value, the `inactive-policy-state` category.
fn inactive_state(bound: &Bound) -> bool {
    bound.disposition == "not-a-bound"
        && bound.budget.is_empty()
        && bound.value.is_none()
        && bound
            .review
            .starts_with("category:reviewed-inactive-state; ")
}

pub(crate) fn identifier(s: &str) -> bool {
    !s.is_empty()
        && !s.starts_with(|c: char| c.is_ascii_digit())
        && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
}

pub(crate) struct Span<'a> {
    pub(crate) label: &'a str,
    pub(crate) path: &'a str,
    pub(crate) first: usize,
    pub(crate) last: usize,
}

/// `<label> <path>:<a>-<b> @ <oracle sha>; exact-span-sha256=<64 hex>`, nothing else.
pub(crate) fn parse_span(text: &str) -> Option<Span<'_>> {
    let (label, rest) = text.split_once(' ')?;
    let (location, rest) = rest.split_once(" @ ")?;
    let (oracle, hash) = rest.split_once("; exact-span-sha256=")?;
    let (path, lines) = location.rsplit_once(':')?;
    let (first, last) = lines.split_once('-')?;
    let (first, last): (usize, usize) = (first.parse().ok()?, last.parse().ok()?);
    let hash_ok = hash.len() == 64
        && hash
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b));
    (oracle == ORACLE && hash_ok && !path.contains(' ') && first >= 1 && first <= last).then_some(
        Span {
            label,
            path,
            first,
            last,
        },
    )
}

/// The ids a writer row stands for (its own id, or the ids of its derived relation) when every id is a
/// numeric registry entry of `unit`.
fn writer_ids<'a>(bound: &'a Bound, unit: &str) -> Option<&'a str> {
    use budgets::{BudgetId, table::Value};
    let id_text = if bound.budget.is_empty() && bound.disposition == "derived" {
        bound.relation.as_str()
    } else if bound.disposition.is_empty() {
        bound.budget.as_str()
    } else {
        return None;
    };
    let unit_ok = !id_text.is_empty()
        && id_text.split('+').all(|part| {
            BudgetId::from_name(part).is_some_and(|id| {
                let entry_unit = match id.entry().value {
                    Value::Duration(_) | Value::Derived(_) => "milliseconds",
                    Value::Storage(_) => "bytes",
                    Value::Count(_) => "entries",
                    Value::BytesPerSecond(_) => "bytes-per-second",
                    Value::CountPerSecond(_) => "entries-per-second",
                    Value::Ratio(_) => "permille",
                    Value::Blocked => "blocked",
                };
                entry_unit == unit
            })
        });
    unit_ok.then_some(id_text)
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
    // The reviewed row is the census row, a derived edge with no budget and no value of its own.
    if entry.rule != rule
        || c.path != row.path
        || c.line != row.line
        || c.column != row.column
        || c.scope != row.scope
        || c.symbol != row.symbol
        || c.expression != row.expression
        || row.disposition != "derived"
        || !row.budget.is_empty()
        || row.value.is_some()
        || !identifier(entry.class)
    {
        return false;
    }
    // The read is exactly `receiver.field` with a plain local receiver; a clamp reads it as the first operand of
    // `min(self.F, other)`.
    let self_rule = rule != "local-constructor";
    let read = if rule == "self-clamp" {
        let Some(operand) = clamp_operand(&c.expression) else {
            return false;
        };
        operand
    } else {
        c.expression.as_str()
    };
    let Some((receiver, field)) = read.split_once('.') else {
        return false;
    };
    if !identifier(receiver)
        || !identifier(field)
        || (receiver == "self") != self_rule
        || receiver == "cls"
        || receiver != entry.receiver
        || field != entry.field
    {
        return false;
    }
    // Slice 2: the read sits in a method `C.m` of the pinned class, which lives in the row's own file.
    let class_prefix = format!("{}.", entry.class);
    if self_rule
        && (entry.class_file != c.path
            || !c.scope.strip_prefix(&class_prefix).is_some_and(identifier))
    {
        return false;
    }
    // A census assignment or parameter of the receiver in the same scope is a second root.
    if candidates.values().any(|o| {
        o.path == c.path
            && o.scope == c.scope
            && o.symbol == receiver
            && matches!(o.kind.as_str(), "parameter" | "assignment")
    }) {
        return false;
    }
    // Writers: ascending census keyword rows of the field, each reviewed in the row's unit (a repeated key
    // is refused by the one-to-one cover below).
    let writer_keys: Vec<&str> = entry.writers.split(';').collect();
    if writer_keys.windows(2).any(|w| w[0] > w[1]) {
        return false;
    }
    let mut ids = Vec::with_capacity(writer_keys.len());
    let mut writer_cands = Vec::with_capacity(writer_keys.len());
    for key in &writer_keys {
        let Some(w) = candidates.get(*key) else {
            return false;
        };
        let Some(bound) = rows.iter().find(|b| b.key == *key) else {
            return false;
        };
        // An exact `None` initialiser reviewed as inactive state confers no value (self rules only).
        let inactive = self_rule && w.expression == "None" && inactive_state(bound);
        let id_text = if inactive {
            None
        } else {
            let Some(id_text) = writer_ids(bound, &row.unit) else {
                return false;
            };
            Some(id_text)
        };
        let shape_ok = if self_rule {
            w.kind == "assignment"
                && w.path == c.path
                && w.symbol == read
                && w.scope.strip_prefix(&class_prefix).is_some_and(identifier)
        } else {
            w.kind == "keyword" && w.symbol == field
        };
        if !shape_ok || (!inactive && bound.unit != row.unit) {
            return false;
        }
        ids.extend(id_text);
        writer_cands.push(w);
    }
    if ids.is_empty() || row.relation != ids.join("+") {
        return false;
    }
    // The census lists every store of the attribute: none in the class may be missing from the table.
    if self_rule
        && candidates.values().any(|o| {
            o.path == c.path
                && o.kind == "assignment"
                && o.symbol == read
                && o.scope.starts_with(&class_prefix)
                && !writer_keys.contains(&o.key.as_str())
        })
    {
        return false;
    }
    let n = writer_keys.len();
    if self_rule {
        return self_spans(&entry, row, &writer_cands, n);
    }
    // Pinned spans: class, field, function, bind, then one ctor per remaining writer.
    let spans: Option<Vec<Span>> = entry.reference.split(" | ").map(parse_span).collect();
    let Some(spans) = spans else {
        return false;
    };
    if spans.len() < 4 + (n - 1) {
        return false;
    }
    let labels_ok = ["class", "field", "function", "bind"]
        .iter()
        .zip(&spans)
        .all(|(want, span)| span.label == *want)
        && spans[4..].iter().all(|span| span.label == "ctor");
    let (class, field_span, function, bind) = (&spans[0], &spans[1], &spans[2], &spans[3]);
    if !labels_ok
        || class.path != entry.class_file
        || field_span.path != class.path
        || field_span.first < class.first
        || field_span.last > class.last
        || function.path != row.path
        || row.line > function.last
        || bind.path != row.path
        || bind.first < function.first
        || bind.last >= row.line
    {
        return false;
    }
    // No second constructor call inside the function that binds the receiver.
    if spans[4..]
        .iter()
        .any(|s| s.path == function.path && s.first <= function.last && function.first <= s.last)
    {
        return false;
    }
    // One constructor span per writer: every span holds a writer and every writer sits in exactly one span
    // (so extra spans, which would have to double-cover a writer, are refused); the binding span's writer
    // lives in the read's own scope.
    let construction: Vec<&Span> = std::iter::once(bind).chain(&spans[4..]).collect();
    let covers = |span: &Span, w: &Candidate| {
        w.path == span.path && span.first <= w.line && w.line <= span.last
    };
    let bijection = construction
        .iter()
        .all(|span| writer_cands.iter().any(|w| covers(span, w)))
        && writer_cands
            .iter()
            .all(|w| construction.iter().filter(|span| covers(span, w)).count() == 1);
    bijection
        && writer_cands
            .iter()
            .any(|w| covers(bind, w) && w.scope == c.scope)
}

/// Slice 2 spans: `class`, `method`, then one `store` per writer (all in the row's file).
fn self_spans(entry: &Entry, row: &Bound, writers: &[&Candidate], n: usize) -> bool {
    let spans: Option<Vec<Span>> = entry.reference.split(" | ").map(parse_span).collect();
    let Some(spans) = spans else {
        return false;
    };
    if spans.len() < 2 + n {
        return false;
    }
    let (class, method) = (&spans[0], &spans[1]);
    let stores = &spans[2..];
    let inside =
        |inner: &Span, outer: &Span| outer.first <= inner.first && inner.last <= outer.last;
    if class.label != "class"
        || method.label != "method"
        || stores.iter().any(|s| s.label != "store")
        || class.path != row.path
        || method.path != row.path
        || !inside(method, class)
        || method.first > row.line
        || row.line > method.last
        || stores
            .iter()
            .any(|s| s.path != row.path || !inside(s, class))
    {
        return false;
    }
    // Every store span holds a writer and every writer sits in exactly one store span.
    let covers = |span: &Span, w: &Candidate| span.first <= w.line && w.line <= span.last;
    stores.iter().all(|s| writers.iter().any(|w| covers(s, w)))
        && writers
            .iter()
            .all(|w| stores.iter().filter(|s| covers(s, w)).count() == 1)
}
