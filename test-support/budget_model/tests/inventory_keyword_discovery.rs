// SPDX-License-Identifier: AGPL-3.0-only
//! RU-051: validator keyword operands are separate census candidates, even when the
//! receiving variable has no quantity hint or another operand on the same line exists.
#[path = "common/r25_count_admission.rs"]
#[allow(dead_code, reason = "shared exact additive cohort helper")]
mod r25_count_admission;
#[path = "common/r27_delivery_wake_admission.rs"]
#[allow(dead_code, reason = "shared exact additive cohort helper")]
mod r27_delivery_wake_admission;
#[path = "common/r29_retry_admission.rs"]
#[allow(dead_code, reason = "exact additive discovery cohort")]
mod r29_retry_admission;
#[path = "common/r30_workflow_admission.rs"]
#[allow(dead_code, reason = "exact additive source discovery cohort")]
mod r30_workflow_admission;
#[path = "common/r32_verifier_admission.rs"]
#[allow(
    dead_code,
    reason = "exact historical four-discovery/five-review projection"
)]
mod r32_verifier_admission;
#[path = "common/r36_stats_admission.rs"]
#[allow(dead_code, reason = "exact three-key historical projection")]
mod r36_stats_admission;
#[path = "common/r50_recovery_forward_admission.rs"]
#[allow(dead_code, reason = "exact two-key historical projection")]
mod r50_recovery_forward_admission;
use budget_model::inventory::{adjudication, candidates};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared oracle helper")]
mod oracle_blobs;
#[path = "common/r19_ratio_admission.rs"]
mod r19_ratio_admission;

const LEDGER: &str = include_str!("../data/python-keyword-discovery.tsv");
const NONCALL: &str = include_str!("../data/python-keyword-noncall-sites.tsv");
const KEYWORDS: [&str; 10] = [
    "maximum",
    "minimum",
    "high",
    "low",
    "upper",
    "lower",
    "max_value",
    "min_value",
    "max",
    "min",
];
type Location = (String, usize, usize, String);

// Independent lexical scan, deliberately broader than AST call discovery. Names in
// comments and strings are not code; defaults/assignments remain explicit NONCALL rows.
fn keyword_tokens(path: &str, source: &str) -> BTreeSet<Location> {
    let b = source.as_bytes();
    let mut i = 0;
    let mut line = 1;
    let mut start = 0;
    let mut found = BTreeSet::new();
    while i < b.len() {
        if b[i] == b'\n' {
            line += 1;
            i += 1;
            start = i;
            continue;
        }
        if b[i] == b'#' {
            while i < b.len() && b[i] != b'\n' {
                i += 1;
            }
            continue;
        }
        if matches!(b[i], b'\'' | b'"') {
            let q = b[i];
            let triple = b.get(i..i + 3) == Some(&[q, q, q]);
            i += if triple { 3 } else { 1 };
            while i < b.len() {
                if b[i] == b'\\' {
                    i += 1;
                    if b.get(i) == Some(&b'\n') {
                        line += 1;
                        start = i + 1;
                    }
                    i = (i + 1).min(b.len());
                } else if b[i] == q && (!triple || b.get(i..i + 3) == Some(&[q, q, q])) {
                    i += if triple { 3 } else { 1 };
                    break;
                } else {
                    if b[i] == b'\n' {
                        line += 1;
                        start = i + 1;
                    }
                    i += 1;
                }
            }
            continue;
        }
        if b[i].is_ascii_alphabetic() || b[i] == b'_' {
            let at = i;
            while i < b.len() && (b[i].is_ascii_alphanumeric() || b[i] == b'_') {
                i += 1;
            }
            let name = &source[at..i];
            let mut next = i;
            while next < b.len() && b[next].is_ascii_whitespace() {
                next += 1;
            }
            if KEYWORDS.contains(&name)
                && b.get(next) == Some(&b'=')
                && b.get(next + 1) != Some(&b'=')
            {
                found.insert((path.to_owned(), line, at - start, name.to_owned()));
            }
            continue;
        }
        i += 1;
    }
    found
}

fn ledger_locations() -> BTreeSet<Location> {
    LEDGER
        .lines()
        .skip(1)
        .map(|l| {
            let f: Vec<_> = l.split('\t').collect();
            assert_eq!(f.len(), 8);
            (
                f[1].to_owned(),
                f[2].parse().unwrap(),
                f[3].parse().unwrap(),
                f[4].to_owned(),
            )
        })
        .collect()
}

#[test]
fn keyword_scanner_distinguishes_operands_from_comments_strings_and_comparisons() {
    let text = "check(x, maximum=71, minimum = 0,\n high=cap, low=1)\n# maximum=999\ns = 'minimum=99'\nt = \"\"\"high=88\nlow=77\"\"\"\nmaximum == 12\n";
    let got = keyword_tokens("x.py", text);
    assert_eq!(
        got,
        BTreeSet::from([
            ("x.py".into(), 1, 9, "maximum".into()),
            ("x.py".into(), 1, 21, "minimum".into()),
            ("x.py".into(), 2, 1, "high".into()),
            ("x.py".into(), 2, 11, "low".into()),
        ])
    );
}

#[test]
fn every_keyword_token_in_every_scoped_oracle_file_has_an_explicit_discovery_record() {
    assert_eq!(
        oracle_blobs::sha256_hex(LEDGER.as_bytes()),
        "b81ea381701f43135567ba16fbd9fc313d88397d9f24656257f6a1470409cda7"
    );
    assert_eq!(
        oracle_blobs::sha256_hex(NONCALL.as_bytes()),
        "4e2733475af62eab547206bc2e29276e46a292c82afe8f9d123d81e736ce9138"
    );
    let parsed = adjudication::parse(&adjudication::TABLES).unwrap();
    let paths = parsed.scope.keys().cloned().collect::<Vec<_>>();
    assert_eq!(paths.len(), 725);
    let blobs = oracle_blobs::blobs(&paths);
    let found: BTreeSet<_> = blobs
        .iter()
        .flat_map(|(p, s)| keyword_tokens(p, s))
        .collect();
    let calls = ledger_locations();
    assert_eq!(calls.len(), 674);
    let excluded: BTreeSet<_> = NONCALL
        .lines()
        .skip(1)
        .map(|l| {
            let f: Vec<_> = l.split('\t').collect();
            assert_eq!(f[4], "non-call-argument");
            (
                f[0].to_owned(),
                f[1].parse().unwrap(),
                f[2].parse().unwrap(),
                f[3].to_owned(),
            )
        })
        .collect();
    assert_eq!(excluded.len(), 38);
    assert!(calls.is_disjoint(&excluded));
    assert_eq!(found, calls.union(&excluded).cloned().collect());
}

#[test]
fn discovery_is_an_exact_append_and_does_not_claim_new_reviews() {
    let census = include_str!("../data/python-candidates.tsv");
    let prefix: String = census.split_inclusive('\n').take(7663).collect();
    assert_eq!(
        oracle_blobs::sha256_hex(prefix.as_bytes()),
        "d4a034d3cb445c679a9fdf3d64025ec38c7d5015b9eb6a4bee2c72ed84406cbb"
    );
    let mut r25: BTreeSet<_> = r25_count_admission::keys()
        .union(
            &r27_delivery_wake_admission::keys()
                .union(
                    &r29_retry_admission::keys()
                        .union(&r30_workflow_admission::keys())
                        .copied()
                        .collect(),
                )
                .copied()
                .collect(),
        )
        .copied()
        .collect();
    r25.extend(r32_verifier_admission::keys());
    let mut rows = candidates().unwrap();
    rows.retain(|r| !r25.contains(r.key.as_str()));
    assert_eq!(rows.len(), 8252); // R16 eight operands + R17 twelve tuple floors.
    let by_key: BTreeMap<_, _> = rows.iter().map(|r| (r.key.as_str(), r)).collect();
    let mut added = BTreeSet::new();
    let mut kinds = BTreeMap::new();
    let mut partitions = BTreeMap::new();
    let parsed = adjudication::parse(&adjudication::TABLES).unwrap();
    for l in LEDGER.lines().skip(1) {
        let f: Vec<_> = l.split('\t').collect();
        let row = by_key[f[0]];
        assert_eq!(row.path, f[1]);
        assert_eq!(row.expression, f[5]);
        assert!(!f[6].is_empty());
        if f[7] == "added" {
            assert!(added.insert(f[0]));
            assert_eq!(row.kind, "validator-keyword");
            assert_eq!(row.symbol, f[4]);
            *kinds.entry(f[4]).or_insert(0usize) += 1;
            *partitions
                .entry(parsed.partitions[f[0]].0)
                .or_insert(0usize) += 1;
        } else {
            assert_eq!(f[7], "existing");
        }
    }
    assert_eq!(added.len(), 570);
    assert_eq!(
        kinds,
        BTreeMap::from([
            ("maximum", 175),
            ("minimum", 354),
            ("high", 26),
            ("low", 15)
        ])
    );
    assert_eq!(partitions, BTreeMap::from([('A', 184), ('B', 386)]));
    assert_eq!(
        rows.iter()
            .skip(7662)
            .take(570)
            .map(|r| r.key.as_str())
            .collect::<BTreeSet<_>>(),
        added
    );
    let reviewed = budget_model::inventory::all_reviewed().unwrap();
    let accepted: BTreeSet<_> = include_str!("../data/python-r16-reviews.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .filter(|f| f[3] == "L1b-r16-keyword")
        .map(|f| f[0])
        .collect();
    assert_eq!(accepted.len(), 20);
    let scope: BTreeSet<_> = include_str!("../data/python-r16-scope-reviewed.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(scope.len(), 7);
    assert!(accepted.is_disjoint(&scope));
    // R17: only independently executed/source-pinned A operands can join the allowlist.
    let numeric: BTreeSet<_> = include_str!("../data/python-r17-numeric.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(numeric.len(), 33);
    assert!(numeric.is_disjoint(&accepted) && numeric.is_disjoint(&scope));
    assert!(
        numeric
            .iter()
            .all(|k| added.contains(k) && parsed.partitions[*k].0 == 'A')
    );
    let integers: BTreeSet<_> = include_str!("../data/python-r17-integers.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(integers.len(), 54);
    assert!(
        integers.is_disjoint(&accepted)
            && integers.is_disjoint(&scope)
            && integers.is_disjoint(&numeric)
    );
    assert!(
        integers
            .iter()
            .all(|k| added.contains(k) && parsed.partitions[*k].0 == 'A')
    );
    let finite: BTreeSet<_> = include_str!("../data/python-r17-finite.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(finite.len(), 48);
    assert!(
        finite.is_disjoint(&accepted)
            && finite.is_disjoint(&scope)
            && finite.is_disjoint(&numeric)
            && finite.is_disjoint(&integers)
    );
    assert!(
        finite
            .iter()
            .all(|k| added.contains(k) && parsed.partitions[*k].0 == 'A')
    );
    let misc: BTreeSet<_> = include_str!("../data/python-r17-misc.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(misc.len(), 11);
    assert!(
        misc.is_disjoint(&accepted)
            && misc.is_disjoint(&scope)
            && misc.is_disjoint(&numeric)
            && misc.is_disjoint(&integers)
            && misc.is_disjoint(&finite)
    );
    assert!(
        misc.iter()
            .all(|k| added.contains(k) && parsed.partitions[*k].0 == 'A')
    );
    let forward: BTreeSet<_> = include_str!("../data/python-r17-forward-domain.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(forward.len(), 6);
    assert!(
        forward.is_disjoint(&accepted)
            && forward.is_disjoint(&scope)
            && forward.is_disjoint(&numeric)
            && forward.is_disjoint(&integers)
            && forward.is_disjoint(&finite)
            && forward.is_disjoint(&misc)
    );
    assert!(
        forward
            .iter()
            .all(|k| added.contains(k) && parsed.partitions[*k].0 == 'A')
    );
    let acquisition: BTreeSet<_> = include_str!("../data/python-r17-acquisition.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    let rate: BTreeSet<_> = include_str!("../data/python-r17-rate.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(acquisition.len(), 3);
    assert_eq!(rate.len(), 1);
    let last: BTreeMap<_, _> = acquisition
        .iter()
        .map(|k| (*k, "reviewer L1b-r17-acquisition:"))
        .chain(rate.iter().map(|k| (*k, "reviewer L1b-r17-rate:")))
        .chain([(
            "mycelium_live/a4_contracts.py:108:42:validator-keyword:minimum",
            "reviewer L1b-r17-tuple-carrier:",
        )])
        .collect();
    assert_eq!(last.len(), 5);
    assert!(
        last.keys()
            .all(|k| added.contains(k) && parsed.partitions[*k].0 == 'A')
    );
    let mut expected: BTreeSet<_> = accepted
        .union(&scope)
        .copied()
        .chain(numeric.iter().copied())
        .chain(integers.iter().copied())
        .chain(finite.iter().copied())
        .chain(misc.iter().copied())
        .chain(forward.iter().copied())
        .collect();
    assert!(last.keys().all(|k| !expected.contains(k)));
    expected.extend(last.keys().copied());
    assert_eq!(
        expected
            .iter()
            .filter(|k| parsed.partitions[**k].0 == 'A')
            .count(),
        184
    );
    let a_manifest = include_str!("../data/python-r17-admissions.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(a_manifest.as_bytes()),
        "200e6016b4d4580fb5e84fcd20c62a73de929285094206372311af5e737c2171"
    );
    assert_eq!(a_manifest.lines().skip(1).count(), 169);
    let a_keys: BTreeSet<_> = a_manifest
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .filter(|k| k.contains(":validator-keyword:"))
        .collect();
    assert_eq!(a_keys.len(), 157);
    let inherited: BTreeSet<_> = accepted.union(&scope).copied().collect();
    assert_eq!(
        expected
            .difference(&inherited)
            .copied()
            .collect::<BTreeSet<_>>(),
        a_keys
    );
    let b_manifest = include_str!("../data/python-kb-r1-proposals.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(b_manifest.as_bytes()),
        "088e9e8caaa91606d1790ecf9102cd5c355ab853f7b2353fa1a3a9cedcf182d2"
    );
    let kb: BTreeMap<_, _> = b_manifest
        .lines()
        .skip(1)
        .map(|l| {
            let f: Vec<_> = l.split('\t').collect();
            (f[0], format!("reviewer {}:", f[1]))
        })
        .collect();
    assert_eq!(kb.len(), 359);
    assert!(
        kb.keys()
            .all(|k| added.contains(k) && parsed.partitions[*k].0 == 'B' && !expected.contains(k))
    );
    expected.extend(kb.keys().copied());
    assert_eq!(expected.len(), 543); // Preserved R16/R17 cohort.
    let ratio = r19_ratio_admission::admitted(&parsed);
    assert!(ratio.is_disjoint(&expected));
    assert!(ratio.is_subset(&added));
    expected.extend(ratio.iter().copied());
    assert_eq!(expected.len(), 545);
    let r36 = r36_stats_admission::admitted(&parsed);
    assert!(r36.is_disjoint(&expected) && r36.is_subset(&added));
    expected.extend(r36.iter().copied());
    let r50 = r50_recovery_forward_admission::admitted(&parsed);
    assert!(r50.is_disjoint(&expected) && r50.is_subset(&added));
    expected.extend(r50.iter().copied());
    let r52 = r50_recovery_forward_admission::lane_a::admitted(&parsed);
    assert!(r52.is_disjoint(&expected) && r52.is_subset(&added));
    expected.extend(r52.iter().copied());
    let actual: BTreeSet<_> = reviewed
        .iter()
        .filter(|r| added.contains(r.key.as_str()))
        .map(|r| {
            let reviewer = if scope.contains(r.key.as_str()) {
                "reviewer L1b-r16-scope:"
            } else if numeric.contains(r.key.as_str()) {
                "reviewer L1b-r17-numeric:"
            } else if integers.contains(r.key.as_str()) {
                "reviewer L1b-r17-integers:"
            } else if finite.contains(r.key.as_str()) {
                "reviewer L1b-r17-finite:"
            } else if misc.contains(r.key.as_str()) {
                "reviewer L1b-r17-misc:"
            } else if forward.contains(r.key.as_str()) {
                "reviewer L1b-r17-forward-domain:"
            } else if let Some(reviewer) = last.get(r.key.as_str()).copied() {
                reviewer
            } else if r52.contains(r.key.as_str()) {
                "reviewer L1b-r52-lane-a:"
            } else if r50.contains(r.key.as_str()) {
                "reviewer L1b-R50-recon-proposal:"
            } else if r36.contains(r.key.as_str()) {
                "reviewer L1b-r36-stats-forwarded:"
            } else if ratio.contains(r.key.as_str()) {
                "reviewer L1b-KB-r2-ratio:"
            } else if let Some(reviewer) = kb.get(r.key.as_str()) {
                reviewer.as_str()
            } else {
                "reviewer L1b-r16-keyword:"
            };
            assert!(r.review.contains(reviewer));
            r.key.as_str()
        })
        .collect();
    assert_eq!(
        actual, expected,
        "only exact source-proven R16/R17 and two-key R19 cohorts may leave pending"
    );
    let gaps: BTreeSet<_> = include_str!("../data/python-r16-census.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(
        rows.iter()
            .skip(8232)
            .take(8)
            .map(|r| r.key.as_str())
            .collect::<BTreeSet<_>>(),
        gaps
    );
    assert_eq!(gaps.len(), 8);
    let tuple: BTreeSet<_> = include_str!("../data/python-r17-tuple.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(tuple.len(), 12);
    assert_eq!(
        rows.iter()
            .skip(8240)
            .map(|r| r.key.as_str())
            .collect::<BTreeSet<_>>(),
        tuple
    );
    assert!(
        tuple
            .iter()
            .all(|k| by_key[*k].kind == "manual-bound" && parsed.partitions[*k].0 == 'A')
    );
}

#[test]
fn known_missing_validator_keyword_bound_is_a_census_candidate() {
    let rows = candidates().unwrap();
    assert!(
        rows.iter().any(|r| r.path == "mycelium_a6_recovery.py"
            && r.line == 135
            && r.symbol == "maximum"
            && r.expression == "71"),
        "RU-051: missing maximum=71 validator operand at mycelium_a6_recovery.py:135"
    );
}
