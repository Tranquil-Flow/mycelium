// SPDX-License-Identifier: AGPL-3.0-only
//! Static oracle inventory validation, not execution of the historical program.
use std::collections::{BTreeMap, BTreeSet};
pub mod adjudication;
pub mod bindings;
pub mod callers;
pub mod categories;
pub mod cli_literals;
pub mod epochs;
pub mod field_origins;
pub mod input_guards;
pub mod multi_target;
pub mod observation_flows;
mod partition_b;
mod prefill;
pub mod propagated;
pub mod ratio;
pub mod unbounded;
pub mod x_exclusion;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Candidate {
    pub key: String,
    pub path: String,
    pub line: usize,
    pub column: usize,
    pub scope: String,
    pub symbol: String,
    pub expression: String,
    pub kind: String,
    pub source: String,
}

/// The parsed census, built once per process; every caller sees the same rows.
pub(crate) fn candidates_ref() -> Result<&'static Vec<Candidate>, String> {
    static ROWS: std::sync::OnceLock<Result<Vec<Candidate>, String>> = std::sync::OnceLock::new();
    ROWS.get_or_init(parse_candidates)
        .as_ref()
        .map_err(Clone::clone)
}

pub fn candidates() -> Result<Vec<Candidate>, String> {
    candidates_ref().cloned()
}

fn parse_candidates() -> Result<Vec<Candidate>, String> {
    let mut lines = include_str!("../data/python-candidates.tsv").lines();
    if lines.next() != Some("key\tpath\tline\tcolumn\tscope\tsymbol\texpression\tkind\tsource") {
        return Err("candidate-header".into());
    }
    let mut seen = BTreeSet::new();
    lines
        .map(|line| {
            let fields: Vec<_> = line.split('\t').collect();
            if fields.len() != 9 || fields[0].is_empty() {
                return Err("candidate-shape".into());
            }
            if !seen.insert(fields[0]) {
                return Err(format!("duplicate-candidate: {}", fields[0]));
            }
            Ok(Candidate {
                key: fields[0].into(),
                path: fields[1].into(),
                line: fields[2].parse().map_err(|_| "candidate-line")?,
                column: fields[3].parse().map_err(|_| "candidate-column")?,
                scope: fields[4].into(),
                symbol: fields[5].into(),
                expression: fields[6].into(),
                kind: fields[7].into(),
                source: fields[8].into(),
            })
        })
        .collect()
}

/// Data lines of a static TSV whose first field equals `key`, in file order (header
/// skipped). A per-text index is built once; callers still apply their full predicate,
/// so this only narrows a scan to the lines that could possibly match.
pub(crate) fn keyed_lines(text: &'static str, key: &str) -> impl Iterator<Item = &'static str> {
    use std::sync::{Mutex, OnceLock};
    type Index = BTreeMap<&'static str, Vec<&'static str>>;
    static CACHE: OnceLock<Mutex<BTreeMap<usize, &'static Index>>> = OnceLock::new();
    let index: &'static Index = {
        let mut cache = CACHE
            .get_or_init(|| Mutex::new(BTreeMap::new()))
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        cache.entry(text.as_ptr() as usize).or_insert_with(|| {
            let mut index = Index::new();
            for line in text.lines().skip(1) {
                index
                    .entry(line.split('\t').next().unwrap_or(""))
                    .or_default()
                    .push(line);
            }
            Box::leak(Box::new(index))
        })
    };
    index.get(key).into_iter().flat_map(|v| v.iter().copied())
}

/// Final candidate; already-reviewed rows retain their original source identity.
pub const CURRENT_SOURCE_SHA: &str = "941765e882ed3eea9a5576cc53e525c4caa20300";
/// Latest census refresh; historic review identities retain their source pins.
pub const CENSUS_SOURCE_SHA: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";
pub const SOURCE_SHA: &str = "ef9ae61de0222c78d83c8133f8f709619394bf51";
pub const PREVIOUS_SOURCE_SHA: &str = "f5bfb70981569c79cb873afe8e0c5be20342db7e";
pub const REPIN_PATH: &str = "mycelium_a13_macos/managed_runtime.py";
pub const REPIN_OLD_HASH: &str = "e807a7ebe8efd267fe0c9bd98310c579d1a56ce6bbb74481ae5efbe77bdcfe02";
pub const REPIN_HASH: &str = "a4cdfd23bbc39b0ceb9e14ff423f2cf38a593f32c6a743c8bec6bfea54d9df3c";

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Bound {
    pub key: String,
    pub path: String,
    pub line: usize,
    pub column: usize,
    pub scope: String,
    pub symbol: String,
    pub expression: String,
    pub unit: String,
    /// Integral milliseconds, bytes, or entries; absent only for dispositions.
    pub value: Option<u64>,
    pub semantic: String,
    pub authority: String,
    pub budget: String,
    pub disposition: String,
    pub review: String,
    pub relation: String,
    pub source_sha: String,
}

/// The changed file keeps only explicitly preserved historical review identities.
fn review_pin_is_valid(row: &Bound) -> bool {
    // A manual adjudication cites oracle lines at the census pin and is stamped with it (RU-042).
    if row.review.starts_with(adjudication::PREFIX) {
        return row.source_sha == CENSUS_SOURCE_SHA;
    }
    if crate::inventory::keyed_lines(
        include_str!("../data/python-final-review-sites.tsv"),
        &row.key,
    )
    .any(|line| {
        let fields: Vec<_> = line.split('\t').collect();
        fields.len() == 3 && fields[0] == row.key && fields[1] == CENSUS_SOURCE_SHA
    }) {
        return row.source_sha == CENSUS_SOURCE_SHA;
    }
    if crate::inventory::keyed_lines(include_str!("../data/python-current-sites.tsv"), &row.key)
        .any(|line| {
            let fields: Vec<_> = line.split('\t').collect();
            fields.len() == 3 && fields[0] == row.key && fields[1] == CURRENT_SOURCE_SHA
        })
    {
        return row.source_sha == CURRENT_SOURCE_SHA;
    }
    if row.path != REPIN_PATH {
        return matches!(row.source_sha.as_str(), SOURCE_SHA | PREVIOUS_SOURCE_SHA);
    }
    let record = keyed_lines(
        include_str!("../data/python-repin-relocations.tsv"),
        &row.key,
    )
    .map(|line| line.split('\t').collect::<Vec<_>>())
    .find(|fields| fields.first().is_some_and(|key| *key == row.key));
    match record {
        Some(fields) => {
            fields.len() == 5
                && fields[1] == PREVIOUS_SOURCE_SHA
                && fields[3] == SOURCE_SHA
                && row.source_sha == fields[1]
                && fields[2]
                    .parse::<usize>()
                    .is_ok_and(|line| line == row.line)
        }
        None => row.source_sha == SOURCE_SHA,
    }
}

/// Validate all reviewed items. Pending discovery candidates are never dispositions.
pub fn validate(rows: &[Bound]) -> Result<(), Vec<String>> {
    use budgets::{BudgetId, table::Value};
    let mut errors = Vec::new();
    let mut seen = BTreeSet::new();
    static MAP: std::sync::OnceLock<Result<BTreeMap<String, Candidate>, String>> =
        std::sync::OnceLock::new();
    let discovered = match MAP.get_or_init(|| {
        Ok(candidates_ref()?
            .iter()
            .map(|row| (row.key.clone(), row.clone()))
            .collect())
    }) {
        Ok(map) => map,
        Err(error) => return Err(vec![error.clone()]),
    };
    // First row per key, as the linear lookups it replaces would find it.
    let mut by_key: std::collections::HashMap<&str, &Bound> = std::collections::HashMap::new();
    for row in rows {
        by_key.entry(row.key.as_str()).or_insert(row);
    }
    for row in rows {
        let mut fail = |code: &str| errors.push(format!("{code}: {}", row.key));
        if let Some(claim) = row.review.strip_prefix("category:") {
            let category = claim.split(';').next().unwrap_or("");
            match discovered.get(&row.key) {
                Some(candidate)
                    if candidate.path == row.path
                        && candidate.line == row.line
                        && candidate.column == row.column
                        && candidate.scope == row.scope
                        && candidate.symbol == row.symbol
                        && candidate.expression == row.expression
                        && categories::accepts(category, candidate)
                        && row.disposition == categories::disposition(category) => {}
                _ => fail("invalid-category-predicate"),
            }
        }
        if row.review.starts_with("numeric-category:") {
            match discovered.get(&row.key) {
                Some(candidate)
                    if candidate.path == row.path
                        && candidate.line == row.line
                        && candidate.column == row.column
                        && candidate.scope == row.scope
                        && candidate.symbol == row.symbol
                        && candidate.expression == row.expression
                        && categories::numeric_literal(candidate)
                            == row.value.map(|value| (row.unit.as_str(), value))
                        && row.value.is_some()
                        && !row.budget.is_empty()
                        && row.disposition.is_empty() => {}
                _ => fail("invalid-numeric-category"),
            }
        }
        if row.review.starts_with("deadline-category:") {
            match discovered.get(&row.key) {
                Some(candidate)
                    if row
                        .review
                        .starts_with("deadline-category:monotonic-literal;")
                        && candidate.path == row.path
                        && candidate.line == row.line
                        && candidate.column == row.column
                        && candidate.scope == row.scope
                        && candidate.symbol == row.symbol
                        && candidate.expression == row.expression
                        && categories::monotonic_allowance(candidate) == row.value
                        && row.value.is_some()
                        && row.unit == "milliseconds"
                        && !row.budget.is_empty()
                        && row.disposition.is_empty() => {}
                _ => fail("invalid-deadline-category"),
            }
        }
        if let Some(claim) = row.review.strip_prefix("quantity-category:") {
            let mode = claim.split(';').next().unwrap_or("");
            let valid = discovered.get(&row.key).is_some_and(|c| {
                c.path == row.path
                    && c.line == row.line
                    && c.column == row.column
                    && c.scope == row.scope
                    && c.symbol == row.symbol
                    && c.expression == row.expression
                    && row.disposition.is_empty()
                    && row.value.is_some()
                    && categories::reviewed_quantity(c, mode)
                        == row.value.map(|v| (row.unit.as_str(), v))
                    && BudgetId::from_name(&row.budget).is_some_and(|id| {
                        matches!(id.entry().provenance, budgets::table::Provenance::Source(s)
                            if s == format!("{}:{} @ {}", c.path, c.line,
                                if row.source_sha == CENSUS_SOURCE_SHA { "f4e366e9" } else { "ef9ae61d" }))
                    })
            });
            if !valid {
                fail("invalid-reviewed-quantity");
            }
        }
        if row.review.starts_with("clamp-category:") {
            let valid = discovered.get(&row.key).is_some_and(|c| {
                c.path == row.path
                    && c.line == row.line
                    && c.column == row.column
                    && c.scope == row.scope
                    && c.symbol == row.symbol
                    && c.expression == row.expression
                    && row
                        .review
                        .starts_with("clamp-category:literal-upper-seconds;")
                    && row.unit == "milliseconds"
                    && row.disposition.is_empty()
                    && row.value.is_some()
                    && categories::clamped_allowance(c) == row.value
                    && crate::inventory::keyed_lines(
                        include_str!("../data/python-clamp-sites.tsv"),
                        &c.key,
                    )
                    .any(|line| {
                        let f: Vec<_> = line.split('\t').collect();
                        f.len() == 11
                            && f[0] == c.key
                            && f[1] == c.path
                            && f[2].parse::<usize>() == Ok(c.line)
                            && f[3].parse::<usize>() == Ok(c.column)
                            && f[4] == c.scope
                            && f[5] == c.symbol
                            && f[6] == c.expression
                            && f[7] == c.kind
                            && f[8] == c.source
                            && f[9] == "seconds-upper-operand"
                            && !f[10].is_empty()
                    })
                    && BudgetId::from_name(&row.budget).is_some_and(|id| {
                        matches!(id.entry().provenance, budgets::table::Provenance::Source(s)
                            if s == format!("{}:{} @ ef9ae61d", c.path, c.line))
                    })
            });
            if !valid {
                fail("invalid-clamp-category");
            }
        }
        if row.review.starts_with("retention-category:") {
            let valid = discovered.get(&row.key).is_some_and(|c| {
                let ceiling = c.expression.strip_prefix(&format!("{} > ", c.symbol));
                c.kind == "comparison"
                    && c.symbol.starts_with("len(")
                    && c.symbol.ends_with(')')
                    && c.path == row.path
                    && c.line == row.line
                    && c.column == row.column
                    && c.scope == row.scope
                    && c.symbol == row.symbol
                    && c.expression == row.expression
                    && ceiling.and_then(|s| s.parse::<u64>().ok()) == row.value
                    && row.value.is_some()
                    && row.unit == "entries"
                    && row.disposition.is_empty()
                    && row
                        .review
                        .starts_with("retention-category:literal-ceiling;")
                    && BudgetId::from_name(&row.budget).is_some_and(|id| {
                        matches!(id.entry().provenance, budgets::table::Provenance::Source(s)
                            if s == format!("{}:{} @ ef9ae61d", c.path, c.line))
                    })
            });
            if !valid {
                fail("invalid-retention-category");
            }
        }
        if row.review.starts_with("partition-b-") && !partition_b::validate(row, rows, discovered) {
            fail("invalid-partition-b-family");
        }
        if row.review.starts_with("prefill-category:") && !prefill::validate(row, discovered) {
            fail("invalid-prefill-category");
        }
        if row.review.starts_with("binding-category:")
            && !bindings::validate_with_lookup(row, &|key| by_key.get(key).copied(), discovered)
        {
            fail("invalid-binding-category");
        }
        // A propagated source-unbounded row (RU-033) is validated by its own rule, never by the family table.
        let propagated_row = row.review.starts_with(propagated::PREFIX);
        // ... and a source-unbounded manual-adjudication verdict (round 15) by the adjudication engine.
        let adjudicated_row = row.review.starts_with(adjudication::PREFIX);
        if (row.review.starts_with(unbounded::PREFIX)
            || (row.disposition == unbounded::DISPOSITION && !propagated_row && !adjudicated_row))
            && !unbounded::validate(row, discovered)
        {
            fail("invalid-source-unbounded");
        }
        if propagated_row && !propagated::validate(row, rows, discovered) {
            fail("invalid-propagated-unbounded");
        }
        if row.review.starts_with(multi_target::PREFIX)
            && !multi_target::validate(row, rows, discovered)
        {
            fail("invalid-multi-target-binding");
        }
        if row.review.starts_with(x_exclusion::PREFIX) && !x_exclusion::validate(row, discovered) {
            fail("invalid-x-exclusion");
        }
        if row.review.starts_with(cli_literals::PREFIX) && !cli_literals::validate(row, discovered)
        {
            fail("invalid-cli-literal");
        }
        if row.review.starts_with(callers::PREFIX) && !callers::validate(row, rows, discovered) {
            fail("invalid-forward-callers");
        }
        if row.review.starts_with(input_guards::PREFIX)
            && !input_guards::validate(row, rows, discovered)
        {
            fail("invalid-input-guard");
        }
        if row.review.starts_with(ratio::PREFIX) && !ratio::validate(row, discovered) {
            fail("invalid-ratio-category");
        }
        if row.review.starts_with(observation_flows::PREFIX)
            && !observation_flows::validate(row, rows, discovered)
        {
            fail("invalid-observation-flow");
        }
        if row.review.starts_with(field_origins::PREFIX)
            && !field_origins::validate(row, rows, discovered)
        {
            fail("invalid-field-origin");
        }
        if (row.review.starts_with(adjudication::PREFIX)
            || row.disposition == adjudication::NOT_EXECUTED)
            && !adjudication::validate(row, discovered)
        {
            fail("invalid-manual-adjudication");
        }
        if !seen.insert(&row.key) {
            fail("duplicate-key");
        }
        if !review_pin_is_valid(row) || row.line == 0 || !row.path.ends_with(".py") {
            fail("invalid-provenance");
        }
        if [
            &row.key,
            &row.path,
            &row.scope,
            &row.symbol,
            &row.expression,
            &row.unit,
            &row.semantic,
            &row.authority,
            &row.review,
            &row.relation,
        ]
        .iter()
        .any(|s| s.trim().is_empty())
        {
            fail("missing-review-metadata");
        }
        if row.budget.is_empty() == row.disposition.is_empty() {
            fail("exactly-one-mapping-required");
        }
        if !row.disposition.is_empty() {
            if !matches!(
                row.disposition.as_str(),
                "fixture-only"
                    | "test-only"
                    | "UI-cosmetic"
                    | "not-a-bound"
                    | "derived"
                    | "source-unbounded"
                    | "not-executed"
            ) {
                fail("unreviewed-disposition");
            }
            if row.disposition == "derived" && row.review.starts_with(multi_target::PREFIX) {
                // Several exact targets: the multi-target validator above resolves each id.
                if row.value.is_some() || row.relation.split('+').count() < 2 {
                    fail("invalid-derived-unit-or-value");
                }
            } else if row.disposition == "derived"
                && (row.review.starts_with(callers::PREFIX)
                    || row.review.starts_with(field_origins::PREFIX))
            {
                // One or more exact caller targets: the validator above resolves each id and unit.
                if row.value.is_some() || row.relation.is_empty() {
                    fail("invalid-derived-unit-or-value");
                }
            } else if row.disposition == "derived" {
                // Derived rows carry a target name in relation, never a free-text waiver.
                match BudgetId::from_name(&row.relation) {
                    None => fail("dangling-derived-budget"),
                    Some(id) => {
                        let unit = match id.entry().value {
                            Value::Duration(_) | Value::Derived(_) => "milliseconds",
                            Value::Storage(_) => "bytes",
                            Value::Count(_) => "entries",
                            Value::BytesPerSecond(_) => "bytes-per-second",
                            Value::CountPerSecond(_) => "entries-per-second",
                            Value::Ratio(_) => "permille",
                            Value::Blocked => "blocked",
                        };
                        if row.value.is_some() || row.unit != unit || unit == "blocked" {
                            fail("invalid-derived-unit-or-value");
                        }
                    }
                }
            } else if row.value.is_some() || row.unit != "not-applicable" {
                fail("disposition-has-enforceable-value");
            }
        }
        if !row.budget.is_empty() {
            match BudgetId::from_name(&row.budget) {
                None => fail("dangling-budget"),
                Some(id) => {
                    let expected = match id.entry().value {
                        Value::Duration(n) => Some(("milliseconds", n.millis())),
                        Value::Storage(n) => Some(("bytes", n.get())),
                        Value::Count(n) => Some(("entries", n.get())),
                        Value::BytesPerSecond(n) => Some(("bytes-per-second", n.get())),
                        Value::CountPerSecond(n) => Some(("entries-per-second", n.get())),
                        Value::Ratio(n) => Some(("permille", n.get())),
                        Value::Derived(_) | Value::Blocked => None,
                    };
                    if expected != row.value.map(|value| (row.unit.as_str(), value))
                        || expected.is_none()
                    {
                        fail("budget-value-or-unit-mismatch");
                    }
                }
            }
        }
    }
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors)
    }
}

/// Full-scope admission is distinct from consistency of a reviewed subset.
pub fn require_complete(
    rows: &[Bound],
    candidate_keys: &BTreeSet<String>,
) -> Result<(), Vec<String>> {
    let mut errors = match validate(rows) {
        Ok(()) => Vec::new(),
        Err(errors) => errors,
    };
    let reviewed: BTreeSet<_> = rows.iter().map(|row| row.key.clone()).collect();
    errors.extend(
        candidate_keys
            .difference(&reviewed)
            .map(|key| format!("unreviewed-candidate: {key}")),
    );
    errors.extend(
        reviewed
            .difference(candidate_keys)
            .map(|key| format!("untracked-review: {key}")),
    );
    if errors.is_empty() {
        Ok(())
    } else {
        Err(errors)
    }
}

pub const HEADER: &str = "key\tpath\tline\tcolumn\tscope\tsymbol\texpression\tunit\tvalue\tsemantic\tauthority\tbudget\tdisposition\treview\trelation\tsource_sha";

/// Strict TSV with no quoting or executable source. Embedded whitespace was
/// flattened by the external one-shot AST reader; expressions are evidence only.
pub fn parse(input: &str) -> Result<Vec<Bound>, String> {
    let mut lines = input.lines();
    if lines.next() != Some(HEADER) {
        return Err("inventory-header".into());
    }
    lines
        .enumerate()
        .map(|(index, line)| {
            let fields: Vec<_> = line.split('\t').collect();
            if fields.len() != 16 {
                return Err(format!("inventory-columns: {}", index + 2));
            }
            let number = |n: usize| {
                fields[n]
                    .parse::<usize>()
                    .map_err(|error| format!("inventory-number: {}: {error}", index + 2))
            };
            let value = if fields[8].is_empty() {
                None
            } else {
                Some(
                    fields[8]
                        .parse::<u64>()
                        .map_err(|error| format!("inventory-value: {}: {error}", index + 2))?,
                )
            };
            Ok(Bound {
                key: fields[0].into(),
                path: fields[1].into(),
                line: number(2)?,
                column: number(3)?,
                scope: fields[4].into(),
                symbol: fields[5].into(),
                expression: fields[6].into(),
                unit: fields[7].into(),
                value,
                semantic: fields[9].into(),
                authority: fields[10].into(),
                budget: fields[11].into(),
                disposition: fields[12].into(),
                review: fields[13].into(),
                relation: fields[14].into(),
                source_sha: fields[15].into(),
            })
        })
        .collect()
}

pub fn checked_in() -> Result<Vec<Bound>, String> {
    parse(include_str!("../data/python-bounds.tsv"))
}

/// The checked-in reviews plus the manual-adjudication verdicts (RU-042 round 13): the full reviewed set.
pub fn all_reviewed() -> Result<Vec<Bound>, String> {
    let mut rows = checked_in()?;
    rows.extend(adjudication::rows().map_err(|errors| errors.join("; "))?);
    Ok(rows)
}

pub fn candidate_keys() -> Result<BTreeSet<String>, String> {
    Ok(candidates_ref()?
        .iter()
        .map(|row| row.key.clone())
        .collect())
}

/// Scope figure the report has always printed on its first line (the original 725 packaged Python source paths).
pub const REPORT_SCOPE: usize = 725;
/// How many closure violations the report prints before it stops sampling.
pub const REPORT_VIOLATIONS_SHOWN: usize = 5;
/// Every closure status line starts with this, so a reader or test can find it.
pub const CLOSURE_PREFIX: &str = "inventory review closure:";

/// RU-053: the inventory counts plus the closure status. The `inventory_gate` example prints this and
/// exits 0 whenever a report was produced; an open closure is a line in the report, never an error.
/// `require_complete` is unchanged and remains the rule that decides what counts as a violation.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Report {
    pub candidates: usize,
    pub reviewed: usize,
    pub mapped: usize,
    /// Every `require_complete` violation, in the order it reported them.
    pub violations: Vec<String>,
}

impl Report {
    /// Reviewed rows that carry a disposition instead of a budget mapping.
    pub fn dispositions(&self) -> usize {
        self.reviewed.saturating_sub(self.mapped)
    }

    /// The report text, one entry per line: counts, then the closure status, then a bounded sample of
    /// violations (at most [`REPORT_VIOLATIONS_SHOWN`]) when the closure is open.
    pub fn render(&self) -> Vec<String> {
        let mut lines = vec![
            format!(
                "scope={REPORT_SCOPE} candidates={} reviewed={} mapped={} dispositions={}",
                self.candidates,
                self.reviewed,
                self.mapped,
                self.dispositions()
            ),
            if self.violations.is_empty() {
                format!("{CLOSURE_PREFIX} PASS (static snapshot only)")
            } else {
                format!(
                    "{CLOSURE_PREFIX} {} violations (report only, RU-053; not whole-product complete)",
                    self.violations.len()
                )
            },
        ];
        lines.extend(
            self.violations
                .iter()
                .take(REPORT_VIOLATIONS_SHOWN)
                .map(|violation| format!("  {violation}")),
        );
        lines
    }
}

/// Pure core of [`report`]: counts and closure status for already-loaded rows and candidate keys.
pub fn build_report(rows: &[Bound], candidate_keys: &BTreeSet<String>) -> Report {
    Report {
        candidates: candidate_keys.len(),
        reviewed: rows.len(),
        mapped: rows.iter().filter(|row| !row.budget.is_empty()).count(),
        violations: require_complete(rows, candidate_keys)
            .err()
            .unwrap_or_default(),
    }
}

/// RU-053 (CL-L1-07): [`build_report`] with each validated RU-053 admission counted as the review of its
/// pending key. Only an `unreviewed-candidate` violation of an admitted key is removed; nothing else changes.
pub fn build_report_admitting(
    rows: &[Bound],
    candidate_keys: &BTreeSet<String>,
    admitted: &BTreeSet<String>,
) -> Report {
    let mut report = build_report(rows, candidate_keys);
    let before = report.violations.len();
    report.violations.retain(|violation| {
        !violation
            .strip_prefix(epochs::UNREVIEWED)
            .is_some_and(|key| admitted.contains(key))
    });
    report.reviewed += before - report.violations.len();
    report
}

/// The inventory report on the checked-in data. Errors only when an input cannot be read or parsed;
/// open closure violations are data in the [`Report`], not an `Err`. An RU-053 row counts as reviewed only
/// when the admission catalogue, including its record, validates; a refused catalogue admits nothing and is
/// itself reported as a violation with its code.
pub fn report() -> Result<Report, String> {
    let rows = all_reviewed()?;
    let keys = candidate_keys()?;
    match epochs::ru053_admitted() {
        Ok(admitted) => Ok(build_report_admitting(&rows, &keys, &admitted)),
        Err(refusal) => {
            let mut report = build_report(&rows, &keys);
            report
                .violations
                .push(format!("admission-catalogue-refused: {refusal}"));
            Ok(report)
        }
    }
}
