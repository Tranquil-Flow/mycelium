// SPDX-License-Identifier: AGPL-3.0-only
//! Manual adjudication engine (RU-042, L1b round 13). Forged tables prove every refusal; the real tables are
//! checked against the pinned oracle (a stale or wrong citation fails here).
#[path = "common/oracle_blobs.rs"]
#[allow(
    dead_code,
    reason = "shared test helper: each test binary uses a subset"
)]
mod oracle_blobs;

use budget_model::inventory::{
    Bound, Candidate, adjudication as adj, adjudication::RegistryView, candidates, checked_in,
    validate,
};
use std::collections::{BTreeMap, BTreeSet};

const SCOPE: &str = include_str!("../data/python-scope.tsv");
const D1: &str = "1111111111111111111111111111111111111111111111111111111111111111";
const D2: &str = "2222222222222222222222222222222222222222222222222222222222222222";
const RATIONALE: &str = "Reviewed against the cited oracle lines; not a limit.";

fn census() -> BTreeMap<String, Candidate> {
    candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect()
}

/// A pending row in a scoped file; the world below is built around it.
fn site() -> Candidate {
    let bounds: BTreeSet<_> = checked_in().unwrap().into_iter().map(|b| b.key).collect();
    candidates()
        .unwrap()
        .into_iter()
        .find(|c| !bounds.contains(&c.key) && c.path == "mycelium_a15_release_decision.py")
        .expect("a pending a15 row")
}

fn registry(name: &str) -> Option<RegistryView> {
    match name {
        "Fx250ms" => Some(RegistryView {
            unit: "milliseconds",
            value: Some(250),
            source: Some("layer_assignment.py:44 @ f4e366e9".into()),
            blocked: None,
        }),
        "FxOldPin" => Some(RegistryView {
            unit: "bytes",
            value: Some(7),
            source: Some("layer_assignment.py:44 @ ef9ae61d".into()),
            blocked: None,
        }),
        "FxF5bf" => Some(RegistryView {
            unit: "entries",
            value: Some(3),
            source: Some("layer_assignment.py:44 @ f5bfb709".into()),
            blocked: None,
        }),
        "FxForeignPin" => Some(RegistryView {
            unit: "entries",
            value: Some(3),
            source: Some("layer_assignment.py:44 @ 0123abcd".into()),
            blocked: None,
        }),
        // a Derived-valued entry has a source line but no canonical value: not a bound
        "FxNoValue" => Some(RegistryView {
            unit: "not-applicable",
            value: None,
            source: Some("layer_assignment.py:44 @ f4e366e9".into()),
            blocked: None,
        }),
        "FxBlocked" => Some(RegistryView {
            unit: "not-applicable",
            value: None,
            source: None,
            blocked: None,
        }),
        "FxMeasured" => Some(RegistryView {
            unit: "milliseconds",
            value: Some(9),
            source: None,
            blocked: None,
        }),
        "FxUnrelated" => Some(RegistryView {
            unit: "milliseconds",
            value: Some(250),
            source: Some("layer_assignment.py:99 @ f4e366e9".into()),
            blocked: None,
        }),
        // Blocked ids of the round-15 `source-unbounded` verdict kind; the text names the site of `site()`
        "FxUnbounded" => Some(blocked_view(&unbounded_text(0, "f4e366e9", "V03"))),
        "FxUnboundedElsewhere" => Some(blocked_view(&unbounded_text(1, "f4e366e9", "V03"))),
        "FxUnboundedOldPin" => Some(blocked_view(&unbounded_text(0, "ef9ae61d", "V03"))),
        "FxUnboundedNoV03" => Some(blocked_view(&unbounded_text(0, "f4e366e9", "v3"))),
        _ => None,
    }
}

fn unbounded_text(line_offset: usize, tag: &str, designed: &str) -> String {
    let c = site();
    format!(
        "{}{}:{} @ {tag}; field x; {designed}: ceiling to be designed in v0.3 (RU-014)",
        adj::BLOCKED_PREFIX,
        c.path,
        c.line + line_offset
    )
}

fn blocked_view(text: &str) -> RegistryView {
    RegistryView {
        unit: "not-applicable",
        value: None,
        source: None,
        blocked: Some(text.to_string()),
    }
}

const NO_PROOFS: &str = "key\tkind\tpath\tline\tneedle\tverdict\n";

struct World {
    key: String,
    path: String,
    line: usize,
    partitions: String,
    pins: String,
    /// rows of the partition-A `source-unbounded` proof table
    proofs: String,
    launch: String,
    a: String,
    b: String,
}

impl World {
    fn new(verdict: &str, target: &str) -> World {
        let c = site();
        let own = format!("{}:{} @ f4e366e9 sha256={D1}", c.path, c.line);
        let other = format!("layer_assignment.py:44 @ f4e366e9 sha256={D2}");
        let cites = if matches!(verdict, "bounded" | "derived") {
            format!("{own} | {other}")
        } else {
            own
        };
        World {
            partitions: format!("{}\tA\tmodule:{}\n", c.key, "mycelium_a15_release_decision"),
            pins: format!(
                "{}\t{}\t{D1}\nlayer_assignment.py\t44\t{D2}\nlayer_assignment.py\t99\t{D2}\n",
                c.path, c.line
            ),
            launch: format!("{}\tunreached\t-\t-\t-\n", c.path),
            proofs: String::new(),
            a: format!(
                "{}\t{verdict}\t{target}\t{cites}\t{RATIONALE}\tL1b-r13\n",
                c.key
            ),
            b: String::new(),
            key: c.key,
            path: c.path,
            line: c.line,
        }
    }
    fn with_tables<R>(&self, f: impl FnOnce(&adj::Tables) -> R) -> R {
        let (partitions, pins, launch, a, b) = (
            format!("{}\n{}", adj::PARTITION_HEADER, self.partitions),
            format!("{}\n{}", adj::PINS_HEADER, self.pins),
            format!("{}\n{}", adj::LAUNCH_HEADER, self.launch),
            format!("{}\n{}", adj::HEADER, self.a),
            format!("{}\n{}", adj::HEADER, self.b),
        );
        let pins_b = format!("{}\n", adj::PINS_HEADER);
        let proofs = format!("{}\n{}", adj::PROOFS_HEADER, self.proofs);
        f(&adj::Tables {
            partitions: &partitions,
            pins_a: &pins,
            pins_b: &pins_b,
            proofs_a: &proofs,
            proofs_b: NO_PROOFS,
            scope: SCOPE,
            launch: &launch,
            a: &a,
            b: &b,
            forwarded_thresholds: adj::HEADER,
        })
    }
    fn rows(&self) -> Result<Vec<Bound>, Vec<String>> {
        self.with_tables(|t| adj::rows_with(t, &census(), &registry))
    }
    /// The table-level refusal (a malformed table is refused by `parse` itself, before any verdict is read).
    fn parse_error(&self) -> String {
        self.with_tables(|t| adj::parse(t).err().expect("parse must refuse"))
    }
    fn refused(&self, code: &str) {
        let errors = self.rows().expect_err("must refuse");
        assert!(
            errors.iter().any(|e| e.starts_with(code)),
            "{code}: {errors:?}"
        );
    }
    fn with(mut self, f: impl FnOnce(&mut World)) -> World {
        f(&mut self);
        self
    }
}

#[test]
fn a_valid_disposition_verdict_becomes_a_reviewed_row() {
    let w = World::new("not-a-bound", "-");
    let rows = w.rows().unwrap();
    assert_eq!(rows.len(), 1);
    let r = &rows[0];
    assert_eq!(
        (r.key.as_str(), r.disposition.as_str()),
        (w.key.as_str(), "not-a-bound")
    );
    assert_eq!(
        (r.unit.as_str(), r.value, r.budget.as_str()),
        ("not-applicable", None, "")
    );
    assert!(r.review.starts_with("manual-adjudication:A;not-a-bound; "));
    assert!(
        r.review
            .contains(&format!("{}:{} @ f4e366e9 sha256={D1}", w.path, w.line))
    );
    assert_eq!(r.source_sha, "f4e366e98a1f705dfaf182897044500d290dbd7f");
}

#[test]
fn every_disposition_verdict_is_accepted_and_the_set_is_exact() {
    // Round 15 (RU-033) adds exactly one kind, `source-unbounded`; the other seven are unchanged.
    assert_eq!(
        adj::VERDICTS,
        [
            "bounded",
            "derived",
            "not-a-bound",
            "fixture-only",
            "test-only",
            "UI-cosmetic",
            "not-executed",
            "source-unbounded"
        ]
    );
    for verdict in ["not-a-bound", "fixture-only", "test-only", "UI-cosmetic"] {
        let rows = World::new(verdict, "-").rows().unwrap();
        assert_eq!(rows[0].disposition, verdict);
    }
    // a bare `source-unbounded` verdict (no proof rows) is no longer an unknown verdict, but it is refused
    World::new("source-unbounded", "FxUnbounded").refused("unbounded-proof-missing");
    World::new("Source-Unbounded", "FxUnbounded").refused("unknown-verdict");
    World::new("Not-A-Bound", "-").refused("unknown-verdict");
}

#[test]
fn bounded_takes_value_and_unit_from_the_registry_and_needs_a_cited_provenance_line() {
    let rows = World::new("bounded", "Fx250ms").rows().unwrap();
    let r = &rows[0];
    assert_eq!(
        (r.budget.as_str(), r.unit.as_str(), r.value),
        ("Fx250ms", "milliseconds", Some(250))
    );
    assert_eq!(r.disposition, "");
    // the older census pins of a registry entry are accepted too, an unknown pin is not
    let old = World::new("bounded", "FxOldPin").rows().unwrap();
    assert_eq!((old[0].unit.as_str(), old[0].value), ("bytes", Some(7)));
    let older = World::new("bounded", "FxF5bf").rows().unwrap();
    assert_eq!(
        (older[0].unit.as_str(), older[0].value),
        ("entries", Some(3))
    );
    World::new("bounded", "FxForeignPin").refused("target-not-bound-by-citation");
    World::new("bounded", "FxUnrelated").refused("target-not-bound-by-citation");
    World::new("bounded", "FxBlocked").refused("target-not-bound-by-citation");
    World::new("bounded", "FxNoValue").refused("target-not-bound-by-citation");
    World::new("derived", "FxNoValue").refused("target-not-bound-by-citation");
    World::new("bounded", "FxMeasured").refused("target-not-bound-by-citation");
    World::new("bounded", "NoSuchId").refused("dangling-target");
    World::new("bounded", "-").refused("dangling-target");
}

#[test]
fn derived_carries_the_target_in_relation_and_no_value() {
    let rows = World::new("derived", "Fx250ms").rows().unwrap();
    let r = &rows[0];
    assert_eq!(
        (r.disposition.as_str(), r.relation.as_str(), r.value),
        ("derived", "Fx250ms", None)
    );
    assert_eq!((r.unit.as_str(), r.budget.as_str()), ("milliseconds", ""));
    World::new("derived", "FxUnrelated").refused("target-not-bound-by-citation");
}

#[test]
fn a_disposition_verdict_with_a_target_is_refused() {
    World::new("not-a-bound", "Fx250ms").refused("target-on-disposition");
    World::new("test-only", "launch-graph").refused("target-on-disposition");
}

#[test]
fn citations_must_be_pinned_exact_and_cover_the_rows_own_line() {
    let c = site();
    let own = |tag: &str, digest: &str| format!("{}:{} @ {tag} sha256={digest}", c.path, c.line);
    let set = |w: &mut World, cites: String| {
        w.a = format!("{}\tnot-a-bound\t-\t{cites}\t{RATIONALE}\tL1b-r13\n", w.key);
    };
    World::new("not-a-bound", "-")
        .with(|w| set(w, own("ef9ae61d", D1)))
        .refused("invalid-citation");
    World::new("not-a-bound", "-")
        .with(|w| set(w, own("f4e366e9", D2)))
        .refused("invalid-citation");
    World::new("not-a-bound", "-")
        .with(|w| set(w, own("f4e366e9", "zz")))
        .refused("invalid-citation");
    World::new("not-a-bound", "-")
        .with(|w| set(w, String::new()))
        .refused("invalid-citation");
    World::new("not-a-bound", "-")
        .with(|w| {
            set(
                w,
                format!("{} | {}", own("f4e366e9", D1), own("f4e366e9", D1)),
            )
        })
        .refused("invalid-citation");
    // a pinned line of another file does not cover this row
    World::new("not-a-bound", "-")
        .with(|w| set(w, format!("layer_assignment.py:44 @ f4e366e9 sha256={D2}")))
        .refused("own-line-not-cited");
    // a line that is not in the pin table
    World::new("not-a-bound", "-")
        .with(|w| w.pins = format!("{}\t{}\t{D1}\n", c.path, c.line + 1))
        .refused("invalid-citation");
    // no `path:line` shape
    World::new("not-a-bound", "-")
        .with(|w| set(w, format!("{} @ f4e366e9 sha256={D1}", c.path)))
        .refused("invalid-citation");
}

#[test]
fn a_pinned_line_outside_the_scoped_file_or_path_is_refused() {
    let c = site();
    let lines: usize = SCOPE
        .lines()
        .skip(1)
        .find_map(|l| {
            let f: Vec<_> = l.split('\t').collect();
            (f[0] == c.path).then(|| f[2].parse().unwrap())
        })
        .unwrap();
    let w = World::new("not-a-bound", "-").with(|w| {
        let cite = format!("{}:{} @ f4e366e9 sha256={D1}", w.path, lines + 1);
        w.pins
            .push_str(&format!("{}\t{}\t{D1}\n", w.path, lines + 1));
        w.a = format!(
            "{}\tnot-a-bound\t-\t{} | {}:{} @ f4e366e9 sha256={D1}\t{RATIONALE}\tL1b-r13\n",
            w.key, cite, w.path, w.line
        );
    });
    w.refused("invalid-citation");
    let unscoped = World::new("not-a-bound", "-").with(|w| {
        w.pins.push_str(&format!("not/in/scope.py\t3\t{D1}\n"));
        w.a = format!(
            "{}\tnot-a-bound\t-\tnot/in/scope.py:3 @ f4e366e9 sha256={D1} | {}:{} @ f4e366e9 sha256={D1}\t{RATIONALE}\tL1b-r13\n",
            w.key, w.path, w.line
        );
    });
    unscoped.refused("invalid-citation");
}

#[test]
fn rationale_reviewer_partition_and_identity_are_checked() {
    World::new("not-a-bound", "-")
        .with(|w| w.a = w.a.replace(RATIONALE, "too short"))
        .refused("missing-rationale-or-reviewer");
    World::new("not-a-bound", "-")
        .with(|w| w.a = w.a.replace("\tL1b-r13\n", "\t\n"))
        .rows()
        .expect_err("empty reviewer");
    // key in the wrong partition's file
    World::new("not-a-bound", "-")
        .with(|w| std::mem::swap(&mut w.a, &mut w.b))
        .refused("wrong-partition");
    // key missing from the partition table
    World::new("not-a-bound", "-")
        .with(|w| w.partitions.clear())
        .refused("wrong-partition");
    // unknown candidate
    World::new("not-a-bound", "-")
        .with(|w| w.a = w.a.replace(&w.key.clone(), "nope.py:1:0:assignment:x"))
        .refused("unknown-candidate");
}

#[test]
fn duplicates_across_files_are_refused() {
    let w = World::new("not-a-bound", "-").with(|w| w.b = w.a.clone());
    let errors = w.rows().expect_err("duplicate");
    assert!(
        errors.iter().any(|e| e.starts_with("duplicate-verdict")),
        "{errors:?}"
    );
}

#[test]
fn malformed_tables_are_refused_by_parse() {
    let base = || World::new("not-a-bound", "-");
    assert!(base().with_tables(adj::parse).is_ok());
    let c = site();
    let cases: Vec<(&str, World)> = vec![
        (
            "verdict row with 5 fields",
            base().with(|w| w.a.push_str("only\tfive\tfields\tin\tthis\n")),
        ),
        (
            "verdict row with 7 fields",
            base().with(|w| w.a = w.a.replace("L1b-r13\n", "L1b-r13\textra\n")),
        ),
        (
            "padded field",
            base().with(|w| w.a = w.a.replace("L1b-r13", " L1b-r13")),
        ),
        (
            "partition side C",
            base().with(|w| w.partitions = w.partitions.replace("\tA\t", "\tC\t")),
        ),
        (
            "partition basis x:",
            base().with(|w| w.partitions = w.partitions.replace("module:", "x:")),
        ),
        (
            "partition basis module: empty",
            base().with(|w| {
                w.partitions = w
                    .partitions
                    .replace("module:mycelium_a15_release_decision", "module:")
            }),
        ),
        (
            "partition empty key",
            base().with(|w| w.partitions.push_str("\tA\tmodule:x\n")),
        ),
        (
            "partition duplicate key",
            base().with(|w| w.partitions.push_str(&w.partitions.clone())),
        ),
        (
            "partition 2 fields",
            base().with(|w| w.partitions = format!("{}\tA\n", w.key)),
        ),
        (
            "pin not hex (abc)",
            base().with(|w| w.pins = w.pins.replace(D1, "abc")),
        ),
        (
            "pin 65 hex digits",
            base().with(|w| w.pins.push_str(&format!("layer_assignment.py\t7\t{D1}1\n"))),
        ),
        (
            "pin with a non-hex letter",
            base().with(|w| {
                w.pins
                    .push_str(&format!("layer_assignment.py\t7\t{}g\n", &D1[1..]))
            }),
        ),
        (
            "pin uppercase hex",
            base().with(|w| {
                w.pins
                    .push_str(&format!("layer_assignment.py\t7\t{}A\n", &D1[1..]))
            }),
        ),
        (
            "pin at line 0",
            base().with(|w| w.pins.push_str(&format!("layer_assignment.py\t0\t{D1}\n"))),
        ),
        (
            "pin duplicated in one file",
            base().with(|w| w.pins.push_str(&w.pins.clone())),
        ),
        (
            "launch status maybe",
            base().with(|w| w.launch = w.launch.replace("unreached", "maybe")),
        ),
        (
            "launch path outside the scope",
            base().with(|w| w.launch.push_str("not/in/scope.py\tunreached\t-\t-\t-\n")),
        ),
        (
            "launch path duplicated",
            base().with(|w| w.launch.push_str(&w.launch.clone())),
        ),
        (
            "launch reached without via",
            base().with(|w| w.launch = format!("{}\treached\t-\t3\tdotted\n", c.path)),
        ),
        (
            "launch reached without line",
            base().with(|w| {
                w.launch = format!("{}\treached\tlayer_assignment.py\t-\tdotted\n", c.path)
            }),
        ),
        (
            "launch reached with line 0",
            base().with(|w| {
                w.launch = format!("{}\treached\tlayer_assignment.py\t0\tdotted\n", c.path)
            }),
        ),
        (
            "launch reached without rule",
            base().with(|w| w.launch = format!("{}\treached\tlayer_assignment.py\t3\t-\n", c.path)),
        ),
        (
            "launch unreached with via",
            base().with(|w| {
                w.launch = format!("{}\tunreached\tlayer_assignment.py\t3\tdotted\n", c.path)
            }),
        ),
        (
            "launch frozen with a rule",
            base().with(|w| w.launch = format!("{}\tfrozen\t-\t-\tdotted\n", c.path)),
        ),
        (
            "launch entry with a via",
            base().with(|w| {
                w.launch = format!("{}\tentry\tlayer_assignment.py\t3\tdotted\n", c.path)
            }),
        ),
    ];
    for (what, world) in cases {
        let error = world.parse_error();
        assert!(!error.is_empty(), "{what}");
    }
    // each refusal carries its own code, so one check cannot stand in for another
    let code = |w: World| w.parse_error().split(':').next().unwrap().to_string();
    assert_eq!(
        code(base().with(|w| w.pins.push_str(&format!("layer_assignment.py\t0\t{D1}\n")))),
        "pin"
    );
    assert_eq!(
        code(base().with(|w| w.pins.push_str(&format!("layer_assignment.py\t7\t{D1}1\n")))),
        "pin"
    );
    assert_eq!(
        code(base().with(|w| w.pins.push_str(&w.pins.clone()))),
        "duplicate-pin"
    );
    assert_eq!(
        code(base().with(|w| w.partitions.push_str(&w.partitions.clone()))),
        "duplicate-partition-key"
    );
    assert_eq!(
        code(base().with(|w| w.partitions.push_str("\tA\tmodule:x\n"))),
        "partition-basis"
    );
    assert_eq!(
        code(base().with(|w| w.launch.push_str("not/in/scope.py\tunreached\t-\t-\t-\n"))),
        "launch-status"
    );
    assert_eq!(
        code(base().with(|w| w.launch.push_str(&w.launch.clone()))),
        "duplicate-launch"
    );
    assert_eq!(
        code(base().with(|w| w.launch = format!("{}\treached\t-\t3\tdotted\n", c.path))),
        "launch-witness"
    );
}

#[test]
fn not_executed_requires_an_unreached_file_and_the_launch_graph_target() {
    let rows = World::new("not-executed", "launch-graph").rows().unwrap();
    assert_eq!(rows[0].disposition, "not-executed");
    assert_eq!(
        (rows[0].unit.as_str(), rows[0].value),
        ("not-applicable", None)
    );
    World::new("not-executed", "-").refused("not-executed-without-proof");
    World::new("not-executed", "Fx250ms").refused("not-executed-without-proof");
    for status in ["frozen", "reached"] {
        let w = World::new("not-executed", "launch-graph").with(|w| {
            w.launch = if status == "frozen" {
                format!("{}\tfrozen\t-\t-\t-\n", w.path)
            } else {
                format!("{}\treached\tlayer_assignment.py\t3\tdotted\n", w.path)
            }
        });
        w.refused("not-executed-without-proof");
    }
    // no launch-graph entry at all is not a proof
    World::new("not-executed", "launch-graph")
        .with(|w| w.launch.clear())
        .refused("not-executed-without-proof");
}

#[test]
fn a_row_validates_only_against_its_own_verdict() {
    let w = World::new("not-a-bound", "-");
    let rows = w.rows().unwrap();
    let row = rows[0].clone();
    let (partitions, pins, launch, a, b) = (
        format!("{}\n{}", adj::PARTITION_HEADER, w.partitions),
        format!("{}\n{}", adj::PINS_HEADER, w.pins),
        format!("{}\n{}", adj::LAUNCH_HEADER, w.launch),
        format!("{}\n{}", adj::HEADER, w.a),
        format!("{}\n", adj::HEADER),
    );
    let pins_b = format!("{}\n", adj::PINS_HEADER);
    let tables = adj::Tables {
        partitions: &partitions,
        pins_a: &pins,
        pins_b: &pins_b,
        proofs_a: NO_PROOFS,
        proofs_b: NO_PROOFS,
        scope: SCOPE,
        launch: &launch,
        a: &a,
        b: &b,
        forwarded_thresholds: adj::HEADER,
    };
    let ok = |r: &Bound| adj::validate_with(r, &census(), &tables, &registry);
    assert!(ok(&row));
    let mut mutants: Vec<Bound> = Vec::new();
    for edit in 0..10 {
        let mut m = row.clone();
        match edit {
            0 => m.disposition = "test-only".into(),
            1 => m.value = Some(1),
            2 => m.unit = "milliseconds".into(),
            3 => m.budget = "Fx250ms".into(),
            4 => m.review.push('x'),
            5 => m.relation = "other".into(),
            6 => m.source_sha = "ef9ae61de0222c78d83c8133f8f709619394bf51".into(),
            7 => m.expression.push('x'),
            8 => m.semantic = "x".into(),
            _ => m.authority = "x".into(),
        }
        mutants.push(m);
    }
    for m in &mutants {
        assert!(!ok(m), "{m:?}");
    }
    // a prefix-less row, an unknown key and a second verdict for the key never validate
    let mut plain = row.clone();
    plain.review = "category:x;".into();
    assert!(!ok(&plain));
    let mut other = row.clone();
    other.key = "x.py:1:0:assignment:y".into();
    assert!(!ok(&other));
    let dup = format!("{a}{}", a.lines().nth(1).unwrap().to_string() + "\n");
    let tables2 = adj::Tables { a: &dup, ..tables };
    assert!(!adj::validate_with(&row, &census(), &tables2, &registry));
}

// ---- the `source-unbounded` verdict kind (round 15, RU-033) ----

const NEEDLE: &str = "max_bytes_per_second";

impl World {
    /// A valid `source-unbounded` world: origin at line 1, the read at the row's own line, two occurrences of
    /// one needle; line 2 is pinned but not part of the proof.
    fn unbounded() -> World {
        let mut w = World::new("source-unbounded", "FxUnbounded");
        w.pins
            .push_str(&format!("{}\t1\t{D2}\n{}\t2\t{D2}\n", w.path, w.path));
        w.a = format!(
            "{}\tsource-unbounded\tFxUnbounded\t{} | {}\t{RATIONALE}\tL1b-r15\n",
            w.key,
            format_args!("{}:{} @ f4e366e9 sha256={D1}", w.path, w.line),
            format_args!("{}:1 @ f4e366e9 sha256={D2}", w.path),
        );
        w.set_proofs(&[
            ("hop", &w.path.clone(), "1", "-", "origin"),
            ("hop", &w.path.clone(), &w.line.to_string(), "-", "read"),
            ("scan", "*", "2", NEEDLE, "-"),
            ("occurrence", &w.path.clone(), "1", NEEDLE, "read"),
            (
                "occurrence",
                &w.path.clone(),
                &w.line.to_string(),
                NEEDLE,
                "read",
            ),
        ]);
        w
    }
    fn set_proofs(&mut self, rows: &[(&str, &str, &str, &str, &str)]) {
        self.proofs = rows
            .iter()
            .map(|(kind, path, line, needle, verdict)| {
                format!(
                    "{}\t{kind}\t{path}\t{line}\t{needle}\t{verdict}\n",
                    self.key
                )
            })
            .collect();
    }
    /// Replace one proof line (a tab-joined row without its key) by another; the row must exist.
    fn swap_proof(&mut self, from: &str, to: &str) {
        let from = format!("{}\t{from}\n", self.key);
        assert!(self.proofs.contains(&from), "{from}");
        let to = if to.is_empty() {
            String::new()
        } else {
            format!("{}\t{to}\n", self.key)
        };
        self.proofs = self.proofs.replace(&from, &to);
    }
    fn proof_row(&self, kind: &str, line: usize, verdict: &str) -> String {
        format!("{kind}\t{}\t{line}\t{NEEDLE}\t{verdict}", self.path)
    }
    fn parse_proof_error(&self) -> String {
        self.parse_error()
    }
}

#[test]
fn a_proved_source_unbounded_verdict_becomes_a_reviewed_row_with_its_own_blocked_id() {
    let w = World::unbounded();
    let rows = w.rows().unwrap();
    let r = &rows[0];
    assert_eq!(
        (
            r.disposition.as_str(),
            r.relation.as_str(),
            r.unit.as_str(),
            r.value,
            r.budget.as_str()
        ),
        (
            "source-unbounded",
            "FxUnbounded",
            "not-applicable",
            None,
            ""
        )
    );
    assert!(
        r.review
            .starts_with("manual-adjudication:A;source-unbounded; ")
    );
    // the row validates against its own verdict and its proof, and only then
    let (partitions, pins, launch, a) = (
        format!("{}\n{}", adj::PARTITION_HEADER, w.partitions),
        format!("{}\n{}", adj::PINS_HEADER, w.pins),
        format!("{}\n{}", adj::LAUNCH_HEADER, w.launch),
        format!("{}\n{}", adj::HEADER, w.a),
    );
    let (proofs, b, pins_b) = (
        format!("{}\n{}", adj::PROOFS_HEADER, w.proofs),
        format!("{}\n", adj::HEADER),
        format!("{}\n", adj::PINS_HEADER),
    );
    let t = adj::Tables {
        partitions: &partitions,
        pins_a: &pins,
        pins_b: &pins_b,
        proofs_a: &proofs,
        proofs_b: NO_PROOFS,
        scope: SCOPE,
        launch: &launch,
        a: &a,
        b: &b,
        forwarded_thresholds: adj::HEADER,
    };
    let ok = |row: &Bound, t: &adj::Tables| adj::validate_with(row, &census(), t, &registry);
    assert!(ok(r, &t));
    for edit in 0..5 {
        let mut m = r.clone();
        match edit {
            0 => m.disposition = "not-a-bound".into(),
            1 => m.relation = "FxUnboundedElsewhere".into(),
            2 => m.unit = "milliseconds".into(),
            3 => m.value = Some(1),
            _ => m.budget = "FxUnbounded".into(),
        }
        assert!(!ok(&m, &t), "{m:?}");
    }
    // without its proof table the same row no longer validates
    let t_empty = adj::Tables {
        proofs_a: NO_PROOFS,
        ..t
    };
    assert!(!ok(r, &t_empty));
}

#[test]
fn the_target_must_be_a_blocked_id_that_names_this_site_and_v03() {
    let with_target =
        |target: &str| World::unbounded().with(|w| w.a = w.a.replace("FxUnbounded", target));
    with_target("FxUnboundedElsewhere").refused("unbounded-target-not-blocked-here");
    with_target("FxUnboundedOldPin").refused("unbounded-target-not-blocked-here");
    with_target("FxUnboundedNoV03").refused("unbounded-target-not-blocked-here");
    // numeric ids, a measured id and a Blocked id without provenance are not source-unbounded targets
    for target in ["Fx250ms", "FxBlocked", "FxMeasured", "FxNoValue"] {
        with_target(target).refused("unbounded-target-not-blocked-here");
    }
    with_target("NoSuchId").refused("dangling-target");
    with_target("-").refused("dangling-target");
    // the `target` of the other kinds is untouched: a Blocked id is not a bound or a derivation
    World::new("bounded", "FxUnbounded").refused("target-not-bound-by-citation");
    World::new("derived", "FxUnbounded").refused("target-not-bound-by-citation");
    World::new("not-a-bound", "FxUnbounded").refused("target-on-disposition");
}

#[test]
fn two_rows_may_not_share_one_blocked_id() {
    // two census keys on one line may both name an id that names that line; only one verdict may own it
    let first = site();
    let mut second = first.clone();
    second.key = format!("{}~second", first.key);
    let mut map = census();
    map.insert(second.key.clone(), second.clone());
    let mut w = World::unbounded();
    w.partitions.push_str(&format!(
        "{}\tA\tmodule:mycelium_a15_release_decision\n",
        second.key
    ));
    let extra_verdict = w.a.replace(&w.key, &second.key);
    let extra_proofs = w.proofs.replace(&w.key, &second.key);
    w.a.push_str(&extra_verdict);
    w.proofs.push_str(&extra_proofs);
    let errors = w
        .with_tables(|t| adj::rows_with(t, &map, &registry))
        .expect_err("one Blocked id for two rows");
    assert!(
        errors
            .iter()
            .any(|e| e == &format!("duplicate-unbounded-target: {}", second.key)),
        "{errors:?}"
    );
    // each alone is fine
    let mut alone = World::unbounded();
    alone.partitions.push_str(&format!(
        "{}\tA\tmodule:mycelium_a15_release_decision\n",
        second.key
    ));
    assert!(
        alone
            .with_tables(|t| adj::rows_with(t, &map, &registry))
            .is_ok()
    );
}

#[test]
fn the_proof_must_exist_and_have_an_origin_and_exactly_one_read_at_the_rows_own_line() {
    World::unbounded()
        .with(|w| w.proofs.clear())
        .refused("unbounded-proof-missing");
    let (own, path) = {
        let w = World::unbounded();
        (w.line, w.path.clone())
    };
    World::unbounded()
        .with(|w| w.swap_proof(&format!("hop\t{path}\t1\t-\torigin"), ""))
        .refused("unbounded-proof-origin");
    // a carrier is not an origin
    World::unbounded()
        .with(|w| {
            w.swap_proof(
                &format!("hop\t{path}\t1\t-\torigin"),
                &format!("hop\t{path}\t1\t-\tcarrier"),
            )
        })
        .refused("unbounded-proof-origin");
    // the read is at another line, absent, or duplicated
    World::unbounded()
        .with(|w| {
            w.swap_proof(
                &format!("hop\t{path}\t{own}\t-\tread"),
                &format!("hop\t{path}\t2\t-\tread"),
            )
        })
        .refused("unbounded-proof-read");
    World::unbounded()
        .with(|w| w.swap_proof(&format!("hop\t{path}\t{own}\t-\tread"), ""))
        .refused("unbounded-proof-read");
    World::unbounded()
        .with(|w| {
            w.proofs
                .push_str(&format!("{}\thop\t{path}\t2\t-\tread\n", w.key))
        })
        .refused("unbounded-proof-read");
    // the read of a different file at the same line number is not the row's own read
    World::unbounded()
        .with(|w| {
            w.swap_proof(
                &format!("hop\t{path}\t{own}\t-\tread"),
                &format!("hop\tphysical_inference_node.py\t{own}\t-\tread"),
            )
        })
        .refused("unbounded-proof-read");
}

#[test]
fn a_scan_declares_the_needle_and_the_exact_number_of_classified_occurrences() {
    let path = World::unbounded().path;
    let line = World::unbounded().line;
    let scan = |count: usize| format!("scan\t*\t{count}\t{NEEDLE}\t-");
    World::unbounded()
        .with(|w| w.swap_proof(&scan(2), ""))
        .refused("unbounded-proof-scan-missing");
    for wrong in [1, 3] {
        World::unbounded()
            .with(|w| w.swap_proof(&scan(2), &scan(wrong)))
            .refused("unbounded-proof-scan-count");
    }
    // an occurrence of a needle nobody scanned for
    World::unbounded()
        .with(|w| {
            w.proofs.push_str(&format!(
                "{}\toccurrence\t{path}\t2\tother_needle\tread\n",
                w.key
            ))
        })
        .refused("unbounded-proof-orphan-occurrence");
    // a file-scoped scan counts only its own file: the occurrences of another file are orphans
    World::unbounded()
        .with(|w| {
            w.swap_proof(&scan(2), &format!("scan\t{path}\t2\t{NEEDLE}\t-"));
            w.swap_proof(
                &w.proof_row("occurrence", 1, "read"),
                &format!("occurrence\tlayer_assignment.py\t1\t{NEEDLE}\tread"),
            );
        })
        .refused("unbounded-proof-scan-count");
    World::unbounded()
        .with(|w| {
            w.swap_proof(&scan(2), &format!("scan\t{path}\t1\t{NEEDLE}\t-"));
            w.swap_proof(
                &w.proof_row("occurrence", 1, "read"),
                &format!("occurrence\tlayer_assignment.py\t1\t{NEEDLE}\tread"),
            );
        })
        .refused("unbounded-proof-orphan-occurrence");
    // a file-scoped scan of the right file is accepted
    World::unbounded()
        .with(|w| w.swap_proof(&scan(2), &format!("scan\t{path}\t2\t{NEEDLE}\t-")))
        .rows()
        .unwrap();
    // two scans with different needles are counted independently
    World::unbounded()
        .with(|w| {
            w.proofs.push_str(&format!(
                "{k}\tscan\t*\t1\tsecond\t-\n{k}\toccurrence\t{path}\t{line}\tsecond\tfloor\n",
                k = w.key
            ))
        })
        .rows()
        .unwrap();
}

#[test]
fn every_proof_line_must_be_cited_and_every_citation_must_be_in_the_proof() {
    let path = World::unbounded().path;
    // line 2 is pinned and classified, but the verdict does not cite it
    World::unbounded()
        .with(|w| {
            w.swap_proof(
                "scan\t*\t2\tmax_bytes_per_second\t-",
                "scan\t*\t3\tmax_bytes_per_second\t-",
            );
            w.proofs.push_str(&format!(
                "{}\toccurrence\t{path}\t2\t{NEEDLE}\tfloor\n",
                w.key
            ));
        })
        .refused("unbounded-proof-uncited");
    // a hop line that the verdict does not cite
    World::unbounded()
        .with(|w| {
            w.proofs
                .push_str(&format!("{}\thop\t{path}\t2\t-\tcarrier\n", w.key))
        })
        .refused("unbounded-proof-uncited");
    // a pinned citation that no proof row uses is a dead citation
    World::unbounded()
        .with(|w| {
            w.a = w.a.replace(
                &format!("{}:1 @ f4e366e9 sha256={D2}", w.path),
                &format!(
                    "{p}:1 @ f4e366e9 sha256={D2} | {p}:2 @ f4e366e9 sha256={D2}",
                    p = w.path
                ),
            )
        })
        .refused("unbounded-proof-dead-citation");
}

#[test]
fn the_proof_table_is_validated_by_parse() {
    let path = World::unbounded().path;
    let line = World::unbounded().line;
    let code = |w: World, expected: &str| {
        let error = w.parse_proof_error();
        assert!(error.starts_with(expected), "{expected}: {error}");
    };
    let u = World::unbounded;
    // an occurrence can never be a ceiling, and the set of verdicts is exactly the family's
    assert_eq!(
        adj::OCCURRENCE_VERDICTS,
        [
            "read",
            "declare",
            "write",
            "floor",
            "type-guard",
            "relative",
            "other-document"
        ]
    );
    assert_eq!(adj::HOP_ROLES, ["origin", "carrier", "read"]);
    assert_eq!(adj::PROOF_KINDS, ["hop", "scan", "occurrence"]);
    code(
        u().with(|w| {
            w.swap_proof(
                &w.proof_row("occurrence", 1, "read"),
                &w.proof_row("occurrence", 1, "ceiling"),
            )
        }),
        "proof-shape",
    );
    code(
        u().with(|w| {
            w.swap_proof(
                &format!("hop\t{path}\t1\t-\torigin"),
                &format!("hop\t{path}\t1\t-\tconsumer"),
            )
        }),
        "proof-shape",
    );
    code(
        u().with(|w| {
            w.swap_proof(
                &format!("hop\t{path}\t1\t-\torigin"),
                &format!("hop\t{path}\t1\t{NEEDLE}\torigin"),
            )
        }),
        "proof-shape",
    );
    code(
        u().with(|w| {
            w.swap_proof(
                &format!("hop\t{path}\t1\t-\torigin"),
                &format!("hop\t{path}\t0\t-\torigin"),
            )
        }),
        "proof-shape",
    );
    code(
        u().with(|w| {
            w.swap_proof(
                &format!("hop\t{path}\t1\t-\torigin"),
                "hop\tnot/in/scope.py\t1\t-\torigin",
            )
        }),
        "proof-shape",
    );
    code(
        u().with(|w| {
            w.swap_proof(
                &format!("hop\t{path}\t1\t-\torigin"),
                "hop\t*\t1\t-\torigin",
            )
        }),
        "proof-shape",
    );
    code(
        u().with(|w| {
            w.swap_proof(
                &format!("hop\t{path}\t1\t-\torigin"),
                &format!("hop\t{path}\t99999999\t-\torigin"),
            )
        }),
        "proof-shape",
    );
    code(
        u().with(|w| {
            w.swap_proof(
                &format!("scan\t*\t2\t{NEEDLE}\t-"),
                &format!("scan\t*\t2\t{NEEDLE}\tread"),
            )
        }),
        "proof-shape",
    );
    code(
        u().with(|w| w.swap_proof(&format!("scan\t*\t2\t{NEEDLE}\t-"), "scan\t*\t2\t-\t-")),
        "proof-shape",
    );
    code(
        u().with(|w| {
            w.swap_proof(
                &format!("scan\t*\t2\t{NEEDLE}\t-"),
                &format!("scan\t*\t0\t{NEEDLE}\t-"),
            )
        }),
        "proof-shape",
    );
    code(
        u().with(|w| {
            w.swap_proof(
                &format!("scan\t*\t2\t{NEEDLE}\t-"),
                &format!("scan\tnot/in/scope.py\t2\t{NEEDLE}\t-"),
            )
        }),
        "proof-shape",
    );
    code(
        u().with(|w| {
            w.swap_proof(
                &w.proof_row("occurrence", 1, "read"),
                &format!("occurrence\t{path}\t1\t-\tread"),
            )
        }),
        "proof-shape",
    );
    code(
        u().with(|w| {
            w.swap_proof(
                &w.proof_row("occurrence", 1, "read"),
                &format!("occurrence\t*\t1\t{NEEDLE}\tread"),
            )
        }),
        "proof-shape",
    );
    code(
        u().with(|w| {
            w.swap_proof(
                &w.proof_row("occurrence", 1, "read"),
                &format!("bogus\t{path}\t1\t{NEEDLE}\tread"),
            )
        }),
        "proof-kind",
    );
    code(
        u().with(|w| {
            w.swap_proof(
                &w.proof_row("occurrence", 1, "read"),
                &format!("occurrence\t{path}\tx\t{NEEDLE}\tread"),
            )
        }),
        "proof-line",
    );
    code(
        u().with(|w| {
            w.proofs.push_str(&format!(
                "{}\toccurrence\t{path}\t{line}\t{NEEDLE}\tread\n",
                w.key
            ))
        }),
        "duplicate-proof",
    );
    // a proof row of a key that belongs to the other partition, or of an unknown key
    code(
        u().with(|w| {
            w.proofs.push_str(&format!(
                "x.py:1:0:assignment:y\thop\t{path}\t2\t-\tcarrier\n"
            ))
        }),
        "proof-partition",
    );
    code(
        u().with(|w| w.partitions = w.partitions.replace("\tA\t", "\tB\t")),
        "proof-partition",
    );
    // header and shape
    code(u().with(|w| w.proofs = format!("{}x", w.proofs)), "shape");
    // the table of the other lane holds only that lane's keys
    let w = u();
    let (partitions, pins, launch, a, b) = (
        format!("{}\n{}", adj::PARTITION_HEADER, w.partitions),
        format!("{}\n{}", adj::PINS_HEADER, w.pins),
        format!("{}\n{}", adj::LAUNCH_HEADER, w.launch),
        format!("{}\n{}", adj::HEADER, w.a),
        format!("{}\n", adj::HEADER),
    );
    let (proofs, pins_b) = (
        format!("{}\n{}", adj::PROOFS_HEADER, w.proofs),
        format!("{}\n", adj::PINS_HEADER),
    );
    let t = adj::Tables {
        partitions: &partitions,
        pins_a: &pins,
        pins_b: &pins_b,
        proofs_a: NO_PROOFS,
        proofs_b: &proofs,
        scope: SCOPE,
        launch: &launch,
        a: &a,
        b: &b,
        forwarded_thresholds: adj::HEADER,
    };
    let error = adj::parse(&t).err().expect("A's key in B's proof table");
    assert!(error.starts_with("proof-partition"), "{error}");
    let bad_header = adj::Tables {
        proofs_a: "key\tkind\n",
        ..t
    };
    assert!(adj::parse(&bad_header).is_err());
}

// ---- the real tables ----

#[test]
fn real_tables_parse_and_every_verdict_row_validates() {
    let rows = adj::rows().expect("the checked-in verdicts are valid");
    let all: Vec<Bound> = checked_in()
        .unwrap()
        .into_iter()
        .chain(rows.clone())
        .collect();
    validate(&all).expect("generic validation including the adjudication branch");
    let keys: BTreeSet<_> = rows.iter().map(|r| r.key.as_str()).collect();
    assert_eq!(keys.len(), rows.len(), "one verdict per row");
    // a synthesised row is the census site itself: nothing is re-labelled
    let census = census();
    for r in &rows {
        let c = &census[&r.key];
        assert_eq!(
            (
                &r.path,
                r.line,
                r.column,
                &r.scope,
                &r.symbol,
                &r.expression
            ),
            (
                &c.path,
                c.line,
                c.column,
                &c.scope,
                &c.symbol,
                &c.expression
            ),
            "{}",
            r.key
        );
        assert_eq!(
            r.source_sha, "f4e366e98a1f705dfaf182897044500d290dbd7f",
            "{}",
            r.key
        );
    }
}

#[test]
fn real_pin_tables_equal_the_oracle_lines_and_exactly_the_cited_lines() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let cited = |side: char| -> BTreeSet<(String, usize)> {
        parsed
            .entries
            .iter()
            .chain(&parsed.forwarded_thresholds)
            .filter(|e| e.partition == side)
            .flat_map(|e| {
                e.citations
                    .split(" | ")
                    .map(|c| {
                        let (site, _) = c.split_once(" @ ").unwrap();
                        let (p, l) = site.rsplit_once(':').unwrap();
                        (p.to_string(), l.parse().unwrap())
                    })
                    .collect::<Vec<_>>()
            })
            .collect()
    };
    let pinned = |text: &str| -> Vec<(String, usize, String)> {
        text.lines()
            .skip(1)
            .map(|l| {
                let f: Vec<_> = l.split('\t').collect();
                (f[0].to_string(), f[1].parse().unwrap(), f[2].to_string())
            })
            .collect()
    };
    for (side, text) in [('A', adj::TABLES.pins_a), ('B', adj::TABLES.pins_b)] {
        let rows = pinned(text);
        let set: BTreeSet<(String, usize)> = rows.iter().map(|r| (r.0.clone(), r.1)).collect();
        assert_eq!(
            set,
            cited(side),
            "pin file {side} is exactly the lines its verdicts cite"
        );
        assert!(
            rows.windows(2)
                .all(|w| (&w[0].0, w[0].1) < (&w[1].0, w[1].1)),
            "pin file {side} sorted"
        );
    }
    oracle_blobs::oracle_dir_or_panic();
    let mut rows = pinned(adj::TABLES.pins_a);
    rows.extend(pinned(adj::TABLES.pins_b));
    let paths: Vec<String> = rows
        .iter()
        .map(|r| r.0.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let blobs = oracle_blobs::blobs(&paths);
    for (path, line, digest) in &rows {
        let text = oracle_blobs::line_of(&blobs[path], *line)
            .unwrap_or_else(|| panic!("{path}:{line} is past the end of the oracle file"));
        assert_eq!(
            &oracle_blobs::sha256_hex(text.as_bytes()),
            digest,
            "{path}:{line}"
        );
    }
}

#[test]
fn the_two_pin_files_may_share_a_line_but_never_disagree_on_it() {
    let w = World::new("not-a-bound", "-");
    let pins_b_same = format!("{}\n{}", adj::PINS_HEADER, w.pins);
    let pins_b_other = format!("{}\n{}\t{}\t{}\n", adj::PINS_HEADER, w.path, w.line, D2);
    let (partitions, pins, launch, a, b) = (
        format!("{}\n{}", adj::PARTITION_HEADER, w.partitions),
        format!("{}\n{}", adj::PINS_HEADER, w.pins),
        format!("{}\n{}", adj::LAUNCH_HEADER, w.launch),
        format!("{}\n{}", adj::HEADER, w.a),
        format!("{}\n", adj::HEADER),
    );
    let mk = |pins_b: &str| {
        let t = adj::Tables {
            partitions: &partitions,
            pins_a: &pins,
            pins_b,
            proofs_a: NO_PROOFS,
            proofs_b: NO_PROOFS,
            scope: SCOPE,
            launch: &launch,
            a: &a,
            b: &b,
            forwarded_thresholds: adj::HEADER,
        };
        adj::rows_with(&t, &census(), &registry)
    };
    assert!(mk(&pins_b_same).is_ok());
    let t = adj::Tables {
        partitions: &partitions,
        pins_a: &pins,
        pins_b: &pins_b_other,
        proofs_a: NO_PROOFS,
        proofs_b: NO_PROOFS,
        scope: SCOPE,
        launch: &launch,
        a: &a,
        b: &b,
        forwarded_thresholds: adj::HEADER,
    };
    let error = adj::parse(&t).err().expect("conflicting pins");
    assert!(error.starts_with("conflicting-pin"), "{error}");
    assert!(mk(&pins_b_other).is_err());
}

#[test]
fn partition_table_is_pinned_and_covers_every_row_without_a_bounds_row() {
    // Written once by round 13; a partition lane never edits it.
    const SHA: &str = include_str!("../data/python-adjudication-partitions.sha256");
    assert_eq!(
        oracle_blobs::sha256_hex(adj::TABLES.partitions.as_bytes()),
        SHA.trim(),
        "python-adjudication-partitions.tsv changed"
    );
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let reviewed: BTreeSet<_> = checked_in().unwrap().into_iter().map(|b| b.key).collect();
    let pending: BTreeSet<String> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| c.key)
        .filter(|k| !reviewed.contains(k))
        .collect();
    let assigned: BTreeSet<String> = parsed.partitions.keys().cloned().collect();
    assert_eq!(
        assigned, pending,
        "partition table == candidates without a bounds.tsv row"
    );
    // module basis equals the key's top-level module
    for (key, (_, basis)) in &parsed.partitions {
        if let Some(module) = basis.strip_prefix("module:") {
            let top = key.split(':').next().unwrap().split('/').next().unwrap();
            assert_eq!(
                top.trim_end_matches(".py"),
                module.trim_end_matches(".py"),
                "{key}"
            );
        }
    }
}

#[test]
fn a_partition_is_a_union_of_top_level_modules_except_ru044() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let mut side_of: BTreeMap<&str, BTreeSet<char>> = BTreeMap::new();
    for (side, basis) in parsed.partitions.values() {
        if let Some(m) = basis.strip_prefix("module:") {
            side_of.entry(m).or_default().insert(*side);
        }
    }
    for (module, sides) in side_of {
        assert_eq!(sides.len(), 1, "{module} is split across partitions");
    }
}

#[test]
fn verdict_files_hold_only_their_own_partitions_rows() {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    for e in &parsed.entries {
        assert_eq!(parsed.partitions[&e.key].0, e.partition, "{}", e.key);
    }
}
