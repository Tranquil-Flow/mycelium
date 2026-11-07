// SPDX-License-Identifier: AGPL-3.0-only
//! Manual adjudication (RU-042, L1b round 13): the mechanical levers are exhausted, so the remaining pending
//! rows are decided by a reviewer, one verdict per row, in one TSV per partition.
//!
//! * `python-adjudication-partitions.tsv` (`key partition basis`) assigns every row to exactly one partition
//!   (`A` or `B`) by top-level module; it is hash-pinned by the tests and never edited by a partition lane.
//! * `python-adjudication-a.tsv` / `-b.tsv` (`key verdict target citations rationale reviewer`) hold the
//!   verdicts. A lane writes ONLY its own file, so two lanes merge without touching each other's data; the
//!   wiring of both files already exists here.
//! * Pins: `python-oracle-line-pins-a.tsv` / `-b.tsv`, one per lane (see `Tables`).
//! * Verdicts: `bounded` (target = a numeric registry id), `derived` (target = a numeric registry id),
//!   `not-a-bound`, `fixture-only`, `test-only`, `UI-cosmetic` (target `-`), `not-executed` (target
//!   `launch-graph`, RU-046) and `source-unbounded` (round 15, RU-033: target = the row's own `Value::Blocked`
//!   registry id, backed by a per-partition proof table, see below). The older `source-unbounded` family tables
//!   (`unbounded.rs`, `propagated.rs`) stay separate and keep their own review prefixes.
//! * `source-unbounded` proof (`python-unbounded-proofs-a.tsv` / `-b.tsv`, `key kind path line needle verdict`):
//!   `hop` rows are the flow from the origin (`origin`, `carrier`) to the row's own `read`; `scan` rows declare a
//!   textual needle and the number of oracle lines that hold it (path `*` = every scoped file); one
//!   `occurrence` row classifies each such line with a non-ceiling verdict (`ceiling` is not one). The engine
//!   demands the counts agree, every proof line is cited and every citation is in the proof; the tests re-derive
//!   each scan from the oracle blobs, so an occurrence missing from the table fails.
//! * A citation is `path:line @ f4e366e9 sha256=<hex of that exact oracle line>`; citations are joined by
//!   ` | `. Every citation must be present in the lane's pin file with the same digest, its path must
//!   be a scoped file and its line within that file; the pin tables are re-derived from the oracle by the tests
//!   (a stale or wrong citation fails). The row's own source line must be cited, and the registry provenance
//!   of a `bounded`/`derived` target must name one of the cited lines.
//! * `not-executed` additionally requires the row's file to be `unreached` in `python-launch-graph.tsv`, which
//!   the tests recompute from the oracle sources (static launch graph, RU-046).
//!
//! The reviewed row is synthesised from the verdict, so the verdict file is the only data a lane writes;
//! `validate` rebuilds the row and demands byte equality.
use super::{Bound, CENSUS_SOURCE_SHA, Candidate};
use budgets::{
    BudgetId,
    table::{Provenance, Value},
};
use std::collections::{BTreeMap, BTreeSet};

pub const PREFIX: &str = "manual-adjudication:";
pub const HEADER: &str = "key\tverdict\ttarget\tcitations\trationale\treviewer";
pub const PARTITION_HEADER: &str = "key\tpartition\tbasis";
pub const PINS_HEADER: &str = "path\tline\tsha256";
pub const LAUNCH_HEADER: &str = "path\tstatus\tvia_path\tvia_line\trule";
pub const VERDICTS: [&str; 8] = [
    "bounded",
    "derived",
    "not-a-bound",
    "fixture-only",
    "test-only",
    "UI-cosmetic",
    "not-executed",
    "source-unbounded",
];
pub const SOURCE_UNBOUNDED: &str = "source-unbounded";
pub const PROOFS_HEADER: &str = "key\tkind\tpath\tline\tneedle\tverdict";
pub const PROOF_KINDS: [&str; 3] = ["hop", "scan", "occurrence"];
/// Flow roles of a `hop` proof row: where the value enters, how it is carried, where this row reads it.
pub const HOP_ROLES: [&str; 3] = ["origin", "carrier", "read"];
/// Verdicts of an `occurrence` (none is a ceiling; the same closed set as the validated-document-field family).
pub const OCCURRENCE_VERDICTS: [&str; 7] = super::unbounded::VERDICTS;
/// Prefix of a Blocked registry id's provenance for a site decided by this verdict kind.
pub const BLOCKED_PREFIX: &str = "v0.1.4 ships no ceiling at ";
pub const NOT_EXECUTED: &str = "not-executed";
pub const LAUNCH_TARGET: &str = "launch-graph";
pub const ORACLE_TAG: &str = "f4e366e9";
/// Pins a registry `Source` provenance may carry (each registered id keeps the pin it was reviewed at).
pub const REGISTRY_TAGS: [&str; 3] = ["f4e366e9", "ef9ae61d", "f5bfb709"];
pub const MIN_RATIONALE: usize = 24;
/// `frozen`: in the app PYZ; `entry`: the PyInstaller entry script (carchive `__main__`); `reached`: referenced by
/// an executed file (witnessed); `unreached`: referenced by none (RU-046).
pub const LAUNCH_STATUSES: [&str; 4] = ["frozen", "entry", "reached", "unreached"];

/// The data tables, injectable so that tests can forge any of them.
#[derive(Clone, Copy)]
pub struct Tables<'a> {
    pub partitions: &'a str,
    /// Oracle line pins of partition A's citations and of partition B's: one file per lane, so lanes never
    /// touch the same file. The same line may be pinned in both with the same digest.
    pub pins_a: &'a str,
    pub pins_b: &'a str,
    /// `source-unbounded` proof tables, one per lane (header `PROOFS_HEADER`).
    pub proofs_a: &'a str,
    pub proofs_b: &'a str,
    pub scope: &'a str,
    pub launch: &'a str,
    pub a: &'a str,
    pub b: &'a str,
    /// Guard-separated forwarded threshold verdicts, validated by the same engine.
    pub forwarded_thresholds: &'a str,
}

pub const TABLES: Tables<'static> = Tables {
    partitions: include_str!("../../data/python-adjudication-partitions.tsv"),
    pins_a: include_str!("../../data/python-oracle-line-pins-a.tsv"),
    pins_b: include_str!("../../data/python-oracle-line-pins-b.tsv"),
    proofs_a: include_str!("../../data/python-unbounded-proofs-a.tsv"),
    proofs_b: include_str!("../../data/python-unbounded-proofs-b.tsv"),
    scope: include_str!("../../data/python-scope.tsv"),
    launch: include_str!("../../data/python-launch-graph.tsv"),
    a: include_str!("../../data/python-adjudication-a.tsv"),
    b: include_str!("../../data/python-adjudication-b.tsv"),
    forwarded_thresholds: include_str!("../../data/python-adjudication-forwarded-thresholds.tsv"),
};

/// What the registry says about an id: canonical unit, value (`None` for Derived/Blocked) and the text of a
/// `Source` provenance (`None` for any other kind).
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RegistryView {
    pub unit: &'static str,
    pub value: Option<u64>,
    pub source: Option<String>,
    /// The text of a `Provenance::Blocked` entry (`Value::Blocked` ids only).
    pub blocked: Option<String>,
}
pub type Lookup<'a> = &'a dyn Fn(&str) -> Option<RegistryView>;

pub fn registry(name: &str) -> Option<RegistryView> {
    let entry = BudgetId::from_name(name)?.entry();
    let (unit, value) = match entry.value {
        Value::Duration(n) => ("milliseconds", Some(n.millis())),
        Value::Storage(n) => ("bytes", Some(n.get())),
        Value::Count(n) => ("entries", Some(n.get())),
        Value::BytesPerSecond(n) => ("bytes-per-second", Some(n.get())),
        Value::CountPerSecond(n) => ("entries-per-second", Some(n.get())),
        Value::Ratio(n) => ("permille", Some(n.get())),
        Value::Derived(_) | Value::Blocked => ("not-applicable", None),
    };
    let source = match entry.provenance {
        Provenance::Source(text) => Some(text.to_string()),
        _ => None,
    };
    let blocked = match (entry.value, entry.provenance) {
        (Value::Blocked, Provenance::Blocked(text)) => Some(text.to_string()),
        _ => None,
    };
    Some(RegistryView {
        unit,
        value,
        source,
        blocked,
    })
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    pub key: String,
    pub verdict: String,
    pub target: String,
    pub citations: String,
    pub rationale: String,
    pub reviewer: String,
    /// `A` or `B`: the file the verdict was written in.
    pub partition: char,
}

/// One row of a `source-unbounded` proof table.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Proof {
    pub kind: String,
    pub path: String,
    pub line: usize,
    pub needle: String,
    pub verdict: String,
}

pub struct Parsed {
    /// key -> proof rows in file order
    pub proofs: BTreeMap<String, Vec<Proof>>,
    /// key -> (partition, basis)
    pub partitions: BTreeMap<String, (char, String)>,
    pub pins: BTreeMap<(String, usize), String>,
    /// path -> line count
    pub scope: BTreeMap<String, usize>,
    /// path -> (status, via_path, via_line, rule)
    pub launch: BTreeMap<String, (String, String, usize, String)>,
    pub entries: Vec<Entry>,
    /// Separate view: never ordinary entries, but part of the exact reviewed union.
    pub forwarded_thresholds: Vec<Entry>,
}

fn is_hex64(text: &str) -> bool {
    text.len() == 64 && text.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

fn data_lines<'a>(
    text: &'a str,
    header: &str,
    columns: usize,
) -> Result<Vec<Vec<&'a str>>, String> {
    let mut lines = text.lines();
    if lines.next() != Some(header) {
        return Err(format!("header: {header}"));
    }
    lines
        .map(|line| {
            let f: Vec<_> = line.split('\t').collect();
            if f.len() == columns && f.iter().all(|s| s.trim() == *s) {
                Ok(f)
            } else {
                Err(format!("shape: {line}"))
            }
        })
        .collect()
}

pub fn parse(tables: &Tables) -> Result<Parsed, String> {
    let mut partitions = BTreeMap::new();
    for f in data_lines(tables.partitions, PARTITION_HEADER, 3)? {
        let side = match f[1] {
            "A" => 'A',
            "B" => 'B',
            _ => return Err(format!("partition: {}", f[0])),
        };
        let basis_ok = f[2].strip_prefix("module:").is_some_and(|m| !m.is_empty())
            || matches!(f[2], "ru-044" | "ru-044-discovery");
        if f[0].is_empty() || !basis_ok {
            return Err(format!("partition-basis: {}", f[0]));
        }
        if partitions
            .insert(f[0].to_string(), (side, f[2].to_string()))
            .is_some()
        {
            return Err(format!("duplicate-partition-key: {}", f[0]));
        }
    }
    let mut pins: BTreeMap<(String, usize), String> = BTreeMap::new();
    for text in [tables.pins_a, tables.pins_b] {
        let mut own = BTreeSet::new();
        for f in data_lines(text, PINS_HEADER, 3)? {
            let line: usize = f[1].parse().map_err(|_| format!("pin-line: {}", f[0]))?;
            if line == 0 || !is_hex64(f[2]) {
                return Err(format!("pin: {}:{}", f[0], f[1]));
            }
            if !own.insert((f[0].to_string(), line)) {
                return Err(format!("duplicate-pin: {}:{}", f[0], f[1]));
            }
            // the other lane may pin the same line, but never with a different digest
            if pins
                .insert((f[0].to_string(), line), f[2].to_string())
                .is_some_and(|previous| previous != f[2])
            {
                return Err(format!("conflicting-pin: {}:{}", f[0], f[1]));
            }
        }
    }
    let mut scope = BTreeMap::new();
    let mut lines = tables.scope.lines();
    if lines.next() != Some("path\tsha256\tlines\tcandidates") {
        return Err("scope-header".into());
    }
    for line in lines {
        let f: Vec<_> = line.split('\t').collect();
        if f.len() != 4 {
            return Err(format!("scope-shape: {line}"));
        }
        scope.insert(
            f[0].to_string(),
            f[2].parse().map_err(|_| format!("scope-lines: {}", f[0]))?,
        );
    }
    let mut launch = BTreeMap::new();
    for f in data_lines(tables.launch, LAUNCH_HEADER, 5)? {
        let via_line = if f[3] == "-" {
            0
        } else {
            f[3].parse().map_err(|_| format!("launch-line: {}", f[0]))?
        };
        if !LAUNCH_STATUSES.contains(&f[1]) || !scope.contains_key(f[0]) {
            return Err(format!("launch-status: {}", f[0]));
        }
        let witnessed = f[1] == "reached";
        if witnessed != (f[2] != "-") || witnessed != (via_line > 0) || (f[4] == "-") == witnessed {
            return Err(format!("launch-witness: {}", f[0]));
        }
        if launch
            .insert(
                f[0].to_string(),
                (
                    f[1].to_string(),
                    f[2].to_string(),
                    via_line,
                    f[4].to_string(),
                ),
            )
            .is_some()
        {
            return Err(format!("duplicate-launch: {}", f[0]));
        }
    }
    let mut proofs: BTreeMap<String, Vec<Proof>> = BTreeMap::new();
    for (side, text) in [('A', tables.proofs_a), ('B', tables.proofs_b)] {
        let mut seen = BTreeSet::new();
        for f in data_lines(text, PROOFS_HEADER, 6)? {
            if partitions.get(f[0]).is_none_or(|(owner, _)| *owner != side) {
                return Err(format!("proof-partition: {}", f[0]));
            }
            let line: usize = f[3].parse().map_err(|_| format!("proof-line: {}", f[0]))?;
            let known = scope.contains_key(f[2]) || (f[1] == "scan" && f[2] == "*");
            let well_formed = match f[1] {
                "hop" => f[4] == "-" && HOP_ROLES.contains(&f[5]),
                "scan" => !f[4].is_empty() && f[4] != "-" && f[5] == "-",
                "occurrence" => {
                    !f[4].is_empty() && f[4] != "-" && OCCURRENCE_VERDICTS.contains(&f[5])
                }
                _ => return Err(format!("proof-kind: {}", f[0])),
            };
            if line == 0 || !known || !well_formed {
                return Err(format!("proof-shape: {}", f[0]));
            }
            if f[1] != "scan" && scope.get(f[2]).is_some_and(|count| line > *count) {
                return Err(format!("proof-shape: {}", f[0]));
            }
            if !seen.insert((f[0], f[1], f[2], line, f[4])) {
                return Err(format!("duplicate-proof: {}", f[0]));
            }
            proofs.entry(f[0].to_string()).or_default().push(Proof {
                kind: f[1].into(),
                path: f[2].into(),
                line,
                needle: f[4].into(),
                verdict: f[5].into(),
            });
        }
    }
    let mut entries = Vec::new();
    let mut forwarded_thresholds = Vec::new();
    for (side, text, separate) in [
        ('A', tables.a, false),
        ('B', tables.b, false),
        ('B', tables.forwarded_thresholds, true),
    ] {
        for f in data_lines(text, HEADER, 6)? {
            // This is not a general bypass of the historical entry-membership guards.
            if separate
                && (f[1] != SOURCE_UNBOUNDED
                    || !matches!(
                        f[0],
                        "mycelium_swarm_directory/stats_docs.py:21:30:validator-keyword:low"
                            | "mycelium_swarm_directory/stats_docs.py:21:40:validator-keyword:high"
                            | "mycelium_swarm_directory/stats_docs.py:31:26:validator-keyword:high"
                    ))
            {
                return Err(format!("forwarded-threshold-scope: {}", f[0]));
            }
            let view = if separate {
                &mut forwarded_thresholds
            } else {
                &mut entries
            };
            view.push(Entry {
                key: f[0].into(),
                verdict: f[1].into(),
                target: f[2].into(),
                citations: f[3].into(),
                rationale: f[4].into(),
                reviewer: f[5].into(),
                partition: side,
            });
        }
    }
    Ok(Parsed {
        proofs,
        partitions,
        pins,
        scope,
        launch,
        entries,
        forwarded_thresholds,
    })
}

/// One citation: `(path, line)`, after checking tag, digest, scope and the pin table.
fn cited_lines(parsed: &Parsed, citations: &str) -> Option<Vec<(String, usize)>> {
    let mut cited = Vec::new();
    for part in citations.split(" | ") {
        let (site, rest) = part.split_once(" @ ")?;
        let (tag, digest) = rest.split_once(" sha256=")?;
        let (path, line) = site.rsplit_once(':')?;
        let line: usize = line.parse().ok()?;
        let pinned = parsed.pins.get(&(path.to_string(), line))?;
        // (`pinned` is a validated 64-digit digest and a validated line >= 1, so equality and the pin lookup
        // already imply a well-formed digest and a non-zero line)
        if tag != ORACLE_TAG
            || pinned != digest
            || parsed.scope.get(path).is_none_or(|count| line > *count)
        {
            return None;
        }
        cited.push((path.to_string(), line));
    }
    let distinct: BTreeSet<_> = cited.iter().collect();
    // (an empty citation list cannot reach here: `"".split(" | ")` yields one part that has no ` @ `)
    (distinct.len() == cited.len()).then_some(cited)
}

/// The negative proof of a `source-unbounded` verdict: its code on refusal.
fn unbounded_proof(
    parsed: &Parsed,
    key: &str,
    own: &(String, usize),
    cited: &[(String, usize)],
) -> Result<(), &'static str> {
    let rows = parsed.proofs.get(key).ok_or("unbounded-proof-missing")?;
    let hops = |role: &'static str| {
        rows.iter()
            .filter(move |p| p.kind == "hop" && p.verdict == role)
    };
    if hops("origin").next().is_none() {
        return Err("unbounded-proof-origin");
    }
    let mut reads = hops("read");
    if !matches!((reads.next(), reads.next()), (Some(p), None) if (&p.path, p.line) == (&own.0, own.1))
    {
        return Err("unbounded-proof-read");
    }
    let scans: Vec<_> = rows.iter().filter(|p| p.kind == "scan").collect();
    if scans.is_empty() {
        return Err("unbounded-proof-scan-missing");
    }
    let occurrences: Vec<_> = rows.iter().filter(|p| p.kind == "occurrence").collect();
    for scan in &scans {
        let found = occurrences
            .iter()
            .filter(|o| o.needle == scan.needle && (scan.path == "*" || o.path == scan.path))
            .count();
        if found != scan.line {
            return Err("unbounded-proof-scan-count");
        }
    }
    if occurrences.iter().any(|o| {
        !scans
            .iter()
            .any(|s| s.needle == o.needle && (s.path == "*" || s.path == o.path))
    }) {
        return Err("unbounded-proof-orphan-occurrence");
    }
    let proven: BTreeSet<(String, usize)> = rows
        .iter()
        .filter(|p| p.kind != "scan")
        .map(|p| (p.path.clone(), p.line))
        .chain(std::iter::once(own.clone()))
        .collect();
    let cited: BTreeSet<(String, usize)> = cited.iter().cloned().collect();
    if proven.iter().any(|line| !cited.contains(line)) {
        return Err("unbounded-proof-uncited");
    }
    if cited.iter().any(|line| !proven.contains(line)) {
        return Err("unbounded-proof-dead-citation");
    }
    Ok(())
}

/// Build the reviewed row for one verdict, or the code of the first refusal.
pub fn row_of(
    parsed: &Parsed,
    entry: &Entry,
    candidates: &BTreeMap<String, Candidate>,
    lookup: Lookup,
) -> Result<Bound, String> {
    let fail = |code: &str| Err(format!("{code}: {}", entry.key));
    let Some(c) = candidates.get(&entry.key) else {
        return fail("unknown-candidate");
    };
    match parsed.partitions.get(&entry.key) {
        Some((side, _)) if *side == entry.partition => {}
        _ => return fail("wrong-partition"),
    }
    if !VERDICTS.contains(&entry.verdict.as_str()) {
        return fail("unknown-verdict");
    }
    if entry.reviewer.is_empty() || entry.rationale.chars().count() < MIN_RATIONALE {
        return fail("missing-rationale-or-reviewer");
    }
    let Some(cited) = cited_lines(parsed, &entry.citations) else {
        return fail("invalid-citation");
    };
    if !cited.contains(&(c.path.clone(), c.line)) {
        return fail("own-line-not-cited");
    }
    let (mut unit, mut value, mut budget, mut disposition) = (
        "not-applicable".to_string(),
        None,
        String::new(),
        String::new(),
    );
    let mut relation = format!("{PREFIX}{}", entry.verdict);
    match entry.verdict.as_str() {
        "bounded" | "derived" => {
            let Some(view) = lookup(&entry.target) else {
                return fail("dangling-target");
            };
            let named = view.source.as_deref().is_some_and(|source| {
                cited.iter().any(|(path, line)| {
                    REGISTRY_TAGS
                        .iter()
                        .any(|tag| source == format!("{path}:{line} @ {tag}"))
                })
            });
            if view.value.is_none() || !named {
                return fail("target-not-bound-by-citation");
            }
            unit = view.unit.to_string();
            if entry.verdict == "bounded" {
                value = view.value;
                budget.clone_from(&entry.target);
            } else {
                disposition = "derived".into();
                relation.clone_from(&entry.target);
            }
        }
        SOURCE_UNBOUNDED => {
            let Some(view) = lookup(&entry.target) else {
                return fail("dangling-target");
            };
            let here = format!("{BLOCKED_PREFIX}{}:{} @ {ORACLE_TAG};", c.path, c.line);
            // (`blocked` is only ever set for a `Value::Blocked` entry, so a value or a source line cannot coexist)
            let blocked_here = view
                .blocked
                .as_deref()
                .is_some_and(|text| text.starts_with(&here) && text.contains("V03"));
            if !blocked_here {
                return fail("unbounded-target-not-blocked-here");
            }
            if let Err(code) =
                unbounded_proof(parsed, &entry.key, &(c.path.clone(), c.line), &cited)
            {
                return fail(code);
            }
            disposition = SOURCE_UNBOUNDED.into();
            relation.clone_from(&entry.target);
        }
        NOT_EXECUTED => {
            let reachable = parsed
                .launch
                .get(&c.path)
                .is_none_or(|(status, ..)| status != "unreached");
            if entry.target != LAUNCH_TARGET || reachable {
                return fail("not-executed-without-proof");
            }
            disposition = NOT_EXECUTED.into();
        }
        _ => {
            if entry.target != "-" {
                return fail("target-on-disposition");
            }
            disposition = entry.verdict.clone();
        }
    }
    Ok(Bound {
        key: c.key.clone(),
        path: c.path.clone(),
        line: c.line,
        column: c.column,
        scope: c.scope.clone(),
        symbol: c.symbol.clone(),
        expression: c.expression.clone(),
        unit,
        value,
        semantic: format!("manual adjudication verdict {}", entry.verdict),
        authority: "manual adjudication (RU-042 round 13); original authority unchanged".into(),
        budget,
        disposition,
        review: format!(
            "{PREFIX}{};{}; {}; reviewer {}: {}",
            entry.partition, entry.verdict, entry.citations, entry.reviewer, entry.rationale
        ),
        relation,
        source_sha: CENSUS_SOURCE_SHA.into(),
    })
}

/// Every verdict as a reviewed row (A, B, then separate thresholds), or all refusals.
pub fn rows_with(
    tables: &Tables,
    candidates: &BTreeMap<String, Candidate>,
    lookup: Lookup,
) -> Result<Vec<Bound>, Vec<String>> {
    let parsed = parse(tables).map_err(|e| vec![e])?;
    let mut errors = Vec::new();
    let mut seen = BTreeSet::new();
    let mut targets = BTreeSet::new();
    let mut rows = Vec::new();
    for entry in parsed.entries.iter().chain(&parsed.forwarded_thresholds) {
        if !seen.insert(entry.key.clone()) {
            errors.push(format!("duplicate-verdict: {}", entry.key));
            continue;
        }
        match row_of(&parsed, entry, candidates, lookup) {
            Ok(row)
                if row.disposition == SOURCE_UNBOUNDED && !targets.insert(row.relation.clone()) =>
            {
                errors.push(format!("duplicate-unbounded-target: {}", entry.key));
            }
            Ok(row) => rows.push(row),
            Err(error) => errors.push(error),
        }
    }
    if errors.is_empty() {
        Ok(rows)
    } else {
        Err(errors)
    }
}

pub fn validate(row: &Bound, candidates: &BTreeMap<String, Candidate>) -> bool {
    validate_with(row, candidates, &TABLES, &registry)
}

/// A row carrying the prefix is valid only when it equals the row synthesised from its own verdict.
pub fn validate_with(
    row: &Bound,
    candidates: &BTreeMap<String, Candidate>,
    tables: &Tables,
    lookup: Lookup,
) -> bool {
    if !row.review.starts_with(PREFIX) {
        return false;
    }
    let Ok(parsed) = parse(tables) else {
        return false;
    };
    let mut verdicts = parsed
        .entries
        .iter()
        .chain(&parsed.forwarded_thresholds)
        .filter(|e| e.key == row.key);
    let (Some(entry), None) = (verdicts.next(), verdicts.next()) else {
        return false;
    };
    if entry.verdict == SOURCE_UNBOUNDED
        && parsed
            .entries
            .iter()
            .chain(&parsed.forwarded_thresholds)
            .any(|e| {
                e.key != entry.key && e.verdict == SOURCE_UNBOUNDED && e.target == entry.target
            })
    {
        return false;
    }
    row_of(&parsed, entry, candidates, lookup).is_ok_and(|expected| expected == *row)
}

/// The verdict files as reviewed rows, checked against the real census and registry.
pub fn rows() -> Result<Vec<Bound>, Vec<String>> {
    let census = super::candidates_ref().map_err(|e| vec![e.clone()])?;
    let map: BTreeMap<String, Candidate> =
        census.iter().map(|c| (c.key.clone(), c.clone())).collect();
    rows_with(&TABLES, &map, &registry)
}
