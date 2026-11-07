// SPDX-License-Identifier: AGPL-3.0-only
//! L1b round 6: the closure forecast. `python-closure-forecast.tsv` classifies every one of the 876
//! rows that were pending at 60ff846 into exactly one of R1 (reviewable now by an existing family
//! rule), R2 (after one named new family rule), R3 (needs another lane's seam), O (no ceiling in
//! v0.1.4: owner decision) or X (not a budget). The table is a frozen snapshot: later reviews may
//! only shrink the pending set, never add a row that is not in the snapshot. Counts are pinned
//! here per class, per partition and per risk so a silent reclassification fails.
#[path = "common/r29_retry_admission.rs"]
#[allow(dead_code, reason = "exact manually reviewed post-snapshot cohort")]
mod r29_retry_admission;
#[path = "common/r32_verifier_admission.rs"]
#[allow(
    dead_code,
    reason = "exact historical four-discovery/five-review projection"
)]
mod r32_verifier_admission;
use budget_model::inventory::{candidate_keys, candidates, checked_in};
use std::collections::{BTreeMap, BTreeSet};

const FORECAST: &str = include_str!("../data/python-closure-forecast.tsv");
const FAMILIES: &str = include_str!("../data/python-closure-forecast-families.tsv");
const FORECAST_HEADER: &str = "key	partition	class	family	risk	slot	role	producer	reason";
const FAMILY_HEADER: &str = "family	class	rows	risk	rule	closed";
/// Pending count at the snapshot (60ff846 + preview-6 merge, no review).
const SNAPSHOT_ROWS: usize = 876;

struct Row {
    key: &'static str,
    partition: &'static str,
    class: &'static str,
    family: &'static str,
    risk: &'static str,
    producer: &'static str,
    reason: &'static str,
}

fn forecast() -> Vec<Row> {
    let mut lines = FORECAST.lines();
    assert_eq!(lines.next(), Some(FORECAST_HEADER));
    lines
        .map(|line| {
            let f: Vec<_> = line.split('\t').collect();
            assert_eq!(f.len(), 9, "{line}");
            Row {
                key: f[0],
                partition: f[1],
                class: f[2],
                family: f[3],
                risk: f[4],
                producer: f[7],
                reason: f[8],
            }
        })
        .collect()
}

fn count_by<'a, K: Ord>(rows: &'a [Row], key: impl Fn(&'a Row) -> K) -> BTreeMap<K, usize> {
    let mut map = BTreeMap::new();
    for row in rows {
        *map.entry(key(row)).or_insert(0) += 1;
    }
    map
}

#[test]
fn snapshot_is_complete_unique_sorted_and_inside_the_census() {
    let rows = forecast();
    assert_eq!(rows.len(), SNAPSHOT_ROWS);
    let keys: BTreeSet<_> = rows.iter().map(|r| r.key).collect();
    assert_eq!(keys.len(), SNAPSHOT_ROWS, "duplicate forecast key");
    assert!(
        rows.windows(2).all(|w| w[0].key < w[1].key),
        "forecast must stay sorted by key"
    );
    let census = candidate_keys().unwrap();
    for row in &rows {
        assert!(
            census.contains(row.key),
            "{} not a census candidate",
            row.key
        );
        assert!(!row.reason.is_empty(), "{} lacks a reason", row.key);
    }
}

#[test]
fn pending_rows_are_always_a_subset_of_the_forecast_and_reviews_only_shrink_it() {
    let rows = forecast();
    let snapshot: BTreeSet<_> = rows.iter().map(|r| r.key).collect();
    let reviewed: BTreeSet<_> = checked_in().unwrap().into_iter().map(|r| r.key).collect();
    let pending: BTreeSet<_> = candidate_keys()
        .unwrap()
        .into_iter()
        .filter(|key| !reviewed.contains(key))
        .collect();
    // Round 13 (RU-044) discovered three bounds (round 14 six more, below) that had no census row; they were never in the snapshot.
    const POST_SNAPSHOT: [&str; 9] = [
        "mycelium_a7_node_wiring.py:182:114:manual-bound:maximum_entry_timeout_seconds",
        "mycelium_a13_macos/standing_consent.py:103:53:manual-bound:maximum_max_commands",
        "mycelium_gate_requirements/spec.py:213:56:manual-bound:maximum_max_report_age_seconds",
        // Round 14 (L1b): six more bounds v0.1.4 enforces that had no census row.
        "mycelium_a13_macos/model_resources.py:406:11:manual-bound:maximum_storage_consent_bytes",
        "mycelium_a13_macos/standing_consent.py:102:58:manual-bound:maximum_max_wall_seconds",
        "mycelium_a13_macos/standing_consent.py:53:14:manual-bound:maximum_max_memory_bytes",
        "mycelium_node/token_provisioning.py:148:59:manual-bound:maximum_process_ceiling_seconds",
        "mycelium_a13_macos/demo_chain.py:836:58:manual-bound:maximum_member_wall_cap_seconds",
        "physical_inference_node.py:6576:30:manual-bound:maximum_progress_wait_slice_seconds",
    ];
    let keyword_additions: BTreeSet<&str> = include_str!("../data/python-keyword-discovery.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .filter(|f| f[7] == "added")
        .map(|f| f[0])
        .collect();
    assert_eq!(keyword_additions.len(), 570);
    let r16: BTreeSet<_> = include_str!("../data/python-r16-census.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(r16.len(), 8);
    // R17 tuple floors are reviewed via adjudication, outside this frozen mechanical view.
    let r17: BTreeSet<_> = include_str!("../data/python-r17-tuple.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(r17.len(), 12);
    assert!(r17.is_disjoint(&r16) && r17.is_disjoint(&keyword_additions));
    let mut r29 = r29_retry_admission::keys();
    assert!(r29.is_disjoint(&r32_verifier_admission::keys()));
    r29.extend(r32_verifier_admission::keys());
    assert!(
        r29.is_disjoint(&snapshot)
            && r29.is_disjoint(&r17)
            && r29.is_disjoint(&r16)
            && r29.is_disjoint(&keyword_additions)
    );
    for key in &pending {
        assert!(
            snapshot.contains(key.as_str())
                || POST_SNAPSHOT.contains(&key.as_str())
                || keyword_additions.contains(key.as_str())
                || r16.contains(key.as_str())
                || r17.contains(key.as_str())
                || r29.contains(key.as_str()),
            "{key} is pending but missing from the closure forecast; classify it"
        );
    }
    let post = pending
        .iter()
        .filter(|k| !snapshot.contains(k.as_str()))
        .count();
    assert_eq!(
        post,
        POST_SNAPSHOT.len() + keyword_additions.len() + r16.len() + r17.len() + r29.len()
    );
    // Reviewed-since-snapshot rows are exactly the forecast rows no longer pending.
    let resolved = snapshot.iter().filter(|k| !pending.contains(**k)).count();
    assert_eq!(resolved, SNAPSHOT_ROWS - (pending.len() - post));
}

#[test]
fn class_and_partition_counts_are_pinned() {
    let rows = forecast();
    let classes = count_by(&rows, |r| r.class);
    assert_eq!(
        classes,
        BTreeMap::from([("O", 24), ("R1", 3), ("R2", 725), ("X", 124)]),
        "R3 is deliberately empty: no pending row depends on another lane's seam"
    );
    let parts = count_by(&rows, |r| r.partition);
    assert_eq!(
        parts,
        BTreeMap::from([("A", 370), ("B", 229), ("C", 246), ("D", 31)])
    );
    let table = count_by(&rows, |r| (r.partition, r.class));
    let expected = BTreeMap::from([
        (("A", "O"), 17),
        (("A", "R2"), 303),
        (("A", "X"), 50),
        (("B", "O"), 4),
        (("B", "R2"), 179),
        (("B", "X"), 46),
        (("C", "O"), 3),
        (("C", "R1"), 2),
        (("C", "R2"), 215),
        (("C", "X"), 26),
        (("D", "R1"), 1),
        (("D", "R2"), 28),
        (("D", "X"), 2),
    ]);
    assert_eq!(table, expected);
}

#[test]
fn risk_labels_keep_the_conditional_classes_visible() {
    let rows = forecast();
    let risks = count_by(&rows, |r| (r.class, r.risk));
    assert_eq!(
        risks,
        BTreeMap::from([
            (("O", "rule"), 24),
            (("R1", "rule"), 3),
            (("R2", "may-be-O"), 34),
            (("R2", "operand-dependent"), 156),
            (("R2", "root-dependent"), 458),
            (("R2", "rule"), 57),
            (("R2", "validator-row-must-be-reviewed"), 20),
            (("X", "policy-ratification"), 83),
            (("X", "rule"), 41),
        ])
    );
}

#[test]
fn families_table_matches_the_rows_one_class_per_family() {
    let rows = forecast();
    let mut lines = FAMILIES.lines();
    assert_eq!(lines.next(), Some(FAMILY_HEADER));
    let mut seen = BTreeMap::new();
    for line in lines {
        let f: Vec<_> = line.split('\t').collect();
        assert_eq!(f.len(), 6, "{line}");
        assert!(
            seen.insert(f[0], (f[1], f[2].parse::<usize>().unwrap(), f[3]))
                .is_none(),
            "duplicate family {}",
            f[0]
        );
        assert!(!f[4].is_empty());
        let prefix = match f[1] {
            "R1" => "r1-",
            "R2" => "r2-",
            "R3" => "r3-",
            "O" => "o-",
            "X" => "x-",
            other => panic!("unknown class {other}"),
        };
        assert!(f[0].starts_with(prefix), "{} vs class {}", f[0], f[1]);
    }
    // `closed` is the number of this family's snapshot rows that are reviewed now (updated with every batch).
    let reviewed: BTreeSet<_> = checked_in().unwrap().into_iter().map(|r| r.key).collect();
    for line in FAMILIES.lines().skip(1) {
        let f: Vec<_> = line.split('\t').collect();
        let closed = rows
            .iter()
            .filter(|r| r.family == f[0] && reviewed.contains(r.key))
            .count();
        assert_eq!(f[5].parse::<usize>().unwrap(), closed, "{} closed", f[0]);
    }
    let by_family = count_by(&rows, |r| r.family);
    assert_eq!(by_family.len(), seen.len());
    for (family, n) in by_family {
        let (class, declared, risk) = seen[family];
        assert_eq!(declared, n, "{family} row count");
        assert!(
            rows.iter()
                .filter(|r| r.family == family)
                .all(|r| r.class == class && r.risk == risk)
        );
    }
}

#[test]
fn lexical_invariants_hold_for_the_exclusion_and_owner_classes() {
    let rows = forecast();
    let cands: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for row in &rows {
        let c = &cands[row.key];
        match row.family {
            // Parent ruling: an X instant carrier needs a mechanically found producer row in the
            // same file that computes the instant from a relative operand; it does no clock
            // arithmetic itself.
            "x-absolute-instant-carrier"
            | "x-deadline-instant-carrier"
            | "x-absolute-instant-compare" => {
                if row.family != "x-absolute-instant-compare" {
                    for token in ["monotonic(", "time.time(", " + ", " - "] {
                        assert!(
                            !c.expression.contains(token),
                            "{} carries clock arithmetic: {}",
                            row.key,
                            c.expression
                        );
                    }
                }
                let p = cands.get(row.producer).unwrap_or_else(|| {
                    panic!(
                        "{} producer {:?} is not a census row",
                        row.key, row.producer
                    )
                });
                assert_eq!(
                    p.path, c.path,
                    "{} producer must be in the same file",
                    row.key
                );
                assert_ne!(p.key, c.key);
                assert!(
                    ["monotonic(", "time.time(", "_clock(", " + "]
                        .iter()
                        .any(|t| p.expression.contains(t)),
                    "{} producer {} does no clock + operand arithmetic: {}",
                    row.key,
                    p.key,
                    p.expression
                );
                let norm = |s: &str| {
                    s.replace("self._", "")
                        .replace("self.", "")
                        .trim_start_matches('_')
                        .to_string()
                };
                let ps = norm(&p.symbol);
                assert!(
                    norm(&c.symbol) == ps || c.expression.contains(ps.as_str()),
                    "{} producer symbol {} unrelated",
                    row.key,
                    p.symbol
                );
            }
            "r2-instant-carrier-edge" => {
                assert!(row.producer.is_empty(), "{}", row.key);
            }
            "r1-argparse-option-edge" => {
                assert!(c.expression.starts_with("args."), "{}", row.key);
            }
            // O rows are runtime reads: never a numeric literal at the site.
            f if f.starts_with("o-") => {
                assert!(
                    c.expression.contains('[') || c.expression.contains(".get("),
                    "{} is not a document/config read: {}",
                    row.key,
                    c.expression
                );
                assert!(
                    row.reason.contains("[no upper comparison of field"),
                    "{}",
                    row.key
                );
            }
            _ => {}
        }
        let needs_producer = matches!(
            row.family,
            "x-absolute-instant-carrier"
                | "x-deadline-instant-carrier"
                | "x-absolute-instant-compare"
        );
        assert_eq!(!row.producer.is_empty(), needs_producer, "{}", row.key);
    }
}
