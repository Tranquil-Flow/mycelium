// SPDX-License-Identifier: AGPL-3.0-only
//! Three discovered source resource Counts; no closure of the previous pending queue.
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
#[path = "common/r25_count_admission.rs"]
#[allow(dead_code, reason = "shared exact additive cohort helper")]
mod r25_count_admission;
#[path = "common/r26_seed_inputs_admission.rs"]
mod r26_seed_inputs_admission;
#[path = "common/r27_delivery_wake_admission.rs"]
#[allow(dead_code, reason = "shared exact additive cohort helper")]
mod r27_delivery_wake_admission;
#[path = "common/r28_seed_http_admission.rs"]
mod r28_seed_http_admission;
#[path = "common/r29_retry_admission.rs"]
#[allow(dead_code, reason = "exact additive discovery cohort")]
mod r29_retry_admission;
#[path = "common/r30_workflow_admission.rs"]
#[allow(dead_code, reason = "exact additive source discovery cohort")]
mod r30_workflow_admission;
#[path = "common/r31_seed_admission.rs"]
mod r31_seed_admission;
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
use std::collections::{BTreeMap, BTreeSet};
const CENSUS: &str = include_str!("../data/python-candidates.tsv");
const BOUNDS: &str = include_str!("../data/python-bounds.tsv");
const ARG: &str = include_str!("../data/python-r25-source-argument.json");
#[test]
fn exact_three_discoveries_reviews_and_full_union_preserve_prior_pending() {
    let own = r25_count_admission::keys();
    let r27 = r27_delivery_wake_admission::keys();
    let later = r29_retry_admission::keys();
    let r30 = r30_workflow_admission::keys();
    for (text, lines, digest) in [
        (
            CENSUS,
            8253,
            "5403af5c35957148405820d4b7e7135736ae7f9a660c91477e9cd2d3e0630121",
        ),
        (
            BOUNDS,
            6954,
            "58be344d0a4ff19a8d63cd5255b8713626f47d7de386e9cca49e1cfe3ccc3070",
        ),
    ] {
        let prefix: String = text.split_inclusive('\n').take(lines).collect();
        assert_eq!(oracle_blobs::sha256_hex(prefix.as_bytes()), digest);
        let appended: BTreeSet<_> = text
            .lines()
            .skip(lines)
            .filter(|l| {
                !r27.contains(l.split('\t').next().unwrap())
                    && !later.contains(l.split('\t').next().unwrap())
                    && !r30.contains(l.split('\t').next().unwrap())
                    && !r32_verifier_admission::keys().contains(l.split('\t').next().unwrap())
            })
            .map(|l| l.split('\t').next().unwrap())
            .collect();
        assert_eq!(appended, own);
        assert_eq!(
            text.lines().count()
                - r27.len()
                - r30.len()
                - if text == CENSUS {
                    later.len() + r32_verifier_admission::keys().len()
                } else {
                    0
                },
            lines + 3
        );
    }
    let mut all = budget_model::inventory::candidate_keys().unwrap();
    let mut reviewed: BTreeSet<_> = budget_model::inventory::all_reviewed()
        .unwrap()
        .into_iter()
        .map(|r| r.key)
        .collect();
    r32_verifier_admission::remove_from(&mut all, &mut reviewed);
    r36_stats_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::lane_a::remove_from_reviewed(&mut reviewed);
    let pending: BTreeSet<_> = all.difference(&reviewed).map(String::as_str).collect();
    let prior: BTreeSet<_> = include_str!("../data/python-r24-prior-pending.tsv")
        .lines()
        .skip(1)
        .collect();
    let r24: BTreeSet<_> = include_str!("../data/python-r24-member-lease-admission.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').next().unwrap())
        .collect();
    assert_eq!(prior.len(), 75);
    assert_eq!(r24.len(), 3);
    let p = budget_model::inventory::adjudication::parse(
        &budget_model::inventory::adjudication::TABLES,
    )
    .unwrap();
    let mut r26 = r26_seed_inputs_admission::admitted(&p);
    let r28 = r28_seed_http_admission::admitted(&p);
    assert!(r26.is_disjoint(&r28));
    r26.extend(r28);
    let r31 = r31_seed_admission::admitted(&p);
    assert!(r26.is_disjoint(&r31));
    r26.extend(r31);
    assert!(r24.is_disjoint(&r26));
    let admitted = r24.union(&r26).copied().collect();
    assert_eq!(pending, prior.difference(&admitted).copied().collect());
    assert_eq!(
        (all.len(), reviewed.len(), pending.len()),
        (
            8255 + r27.len() + later.len() + r30.len(),
            8183 + r26.len() + r27.len() + later.len() + r30.len(),
            72 - r26.len()
        )
    );
    assert!(own.is_disjoint(&prior));
    let mut reconstructed = String::new();
    for (i, line) in include_str!("../data/python-scope.tsv").lines().enumerate() {
        if i == 0 {
            reconstructed.push_str(line);
            reconstructed.push('\n');
            continue;
        }
        let mut f: Vec<_> = line.split('\t').map(str::to_string).collect();
        let delta = match f[0].as_str() {
            "mycelium_a13_macos/managed_control.py" => 2,
            "mycelium_a13_macos/managed_offers.py" => 1,
            "mycelium_a13_macos/managed_runtime.py" => r27.len() + later.len(),
            "mycelium_a13_macos/workflow.py" => r30.len(),
            r32_verifier_admission::PATH => r32_verifier_admission::keys().len(),
            _ => 0,
        };
        f[3] = (f[3].parse::<usize>().unwrap() - delta).to_string();
        reconstructed.push_str(&f.join("\t"));
        reconstructed.push('\n');
    }
    assert_eq!(
        oracle_blobs::sha256_hex(reconstructed.as_bytes()),
        "d532777d2211682c1ebb4ccea71b82697e19edbc91b0497e738b434d49c37319"
    );
}
#[test]
fn full_source_arguments_constants_and_consumers_are_independently_pinned() {
    assert_eq!(
        oracle_blobs::sha256_hex(ARG.as_bytes()),
        "fd3e44d1a86cbfc358691247089edcdefb200030ad6a46f07dfcf04003c518aa"
    );
    let pins = include_str!("../data/python-r25-source-lines.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(pins.as_bytes()),
        "1a26917fb87e3f14e37b56cf16d998bd26c3061562458005454aaf9c3e5ecf71"
    );
    let rows: Vec<Vec<_>> = pins
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect())
        .collect();
    assert_eq!(rows.len(), 23);
    oracle_blobs::oracle_dir_or_panic();
    let paths: Vec<_> = rows
        .iter()
        .map(|f| f[0].to_string())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let blobs = oracle_blobs::blobs(&paths);
    assert_eq!(blobs.len(), 2);
    for f in &rows {
        assert_eq!(f.len(), 4);
        let text = &blobs[f[0]];
        assert_eq!(oracle_blobs::sha256_hex(text.as_bytes()), f[2]);
        assert_eq!(
            oracle_blobs::line_of(text, f[1].parse().unwrap()).unwrap(),
            f[3]
        );
    }
    let control = &blobs["mycelium_a13_macos/managed_control.py"];
    for (name, expected) in [
        ("_MAX_ACTIVE_WORK", vec![104, 1480, 1554, 1574]),
        ("_MAX_ACTIVE_CONTROL", vec![105, 1480, 1554, 1575]),
    ] {
        let actual: Vec<_> = control
            .lines()
            .enumerate()
            .filter(|(_, l)| l.contains(name))
            .map(|(i, _)| i + 1)
            .collect();
        assert_eq!(actual, expected);
    }
    let offers = &blobs["mycelium_a13_macos/managed_offers.py"];
    assert!(
        oracle_blobs::line_of(offers, 61)
            .unwrap()
            .contains("0<len(text)<=4096")
    );
    assert!(
        oracle_blobs::line_of(offers, 219)
            .unwrap()
            .contains("def read_offer")
    );
    // Separate trace, not aliases or reviews of these distinct root policies.
    assert!(
        oracle_blobs::line_of(offers, 246)
            .unwrap()
            .contains("len(root)<=4096")
    );
    assert!(
        oracle_blobs::line_of(offers, 294)
            .unwrap()
            .contains("len(root)<=4096")
    );
    let candidates: BTreeMap<_, _> = budget_model::inventory::candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for key in r25_count_admission::keys() {
        let c = &candidates[key];
        assert_eq!(
            c.source,
            oracle_blobs::line_of(&blobs[&c.path], c.line)
                .unwrap()
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
        );
    }
}
#[test]
fn paired_original_validator_capture_distinguishes_characters_from_bytes() {
    let raw = include_str!("../data/fx-l1b-r25-path-characters.json");
    assert_eq!(
        oracle_blobs::sha256_hex(raw.as_bytes()),
        "1c9b0f23241218ca99565a606988221faa6aefecb2796565268e4dfefa68921b"
    );
    let text = include_str!("../data/fx-l1b-r25-path-characters.tsv");
    assert_eq!(
        oracle_blobs::sha256_hex(text.as_bytes()),
        "c826fda42b8c6bbe12a2d90884b17c28d36a93d0e08179f5a954c7343a1dd86d"
    );
    let rows: Vec<Vec<_>> = text
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect())
        .collect();
    assert_eq!(rows.len(), 6);
    for f in rows {
        assert_eq!(f.len(), 7);
        let n = f[1].parse::<usize>().unwrap();
        assert!(f[2].parse::<usize>().unwrap() > 4096);
        if n <= 4096 {
            assert_eq!(f[3], "accepted");
            assert_eq!(f[4].parse::<usize>().unwrap(), n);
        } else {
            assert_eq!(n, 4097);
            assert_eq!(f[3], "refused");
            assert_eq!(f[6], "offer_preposition_invalid");
        }
    }
}
