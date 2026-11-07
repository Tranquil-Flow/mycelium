// SPDX-License-Identifier: AGPL-3.0-only
//! CL-L1-07: the one ordered admission catalogue (`budget_model::inventory::epochs`).
//!
//! Every expected value here is copied from today's historical assertions, the cohort helpers in
//! `tests/common/*_admission.rs` or the sealed manifests themselves; none is recomputed by the code under test.
//! Each refusal control mutates an injected copy of the catalogue (or of the RU-053 records), asserts its
//! named code, and then proves the checked-in bytes are unchanged and still validate.
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "immutable oracle reader")]
mod oracle_blobs;

use budget_model::inventory::adjudication as adj;
use budget_model::inventory::epochs::{
    self, CATALOGUE, Inputs, RU053_HEADER, RU053_PINS, Refusal, Tree, UNREVIEWED, Workspace,
};
use std::collections::{BTreeMap, BTreeSet};

/// Admission order: every cohort a `tests/common/*_admission.rs` helper or `inventory_r52_lane_a.rs`
/// projects today, then the RU-053 epoch.
const HISTORY: [&str; 16] = [
    "r19", "r21", "r23", "r24", "r25", "r26", "r27", "r28", "r29", "r30", "r31", "r32", "r36",
    "r50", "r52", "ru053",
];

/// `(epoch, manifest, sealed sha256, data rows' key count, sealed record-digest column)` copied from the helpers'
/// own pins (`r19_ratio_admission.rs:12` ... `r50_recovery_forward_admission.rs:101-104`, R52 `lane_a::pins`).
const MANIFESTS: [(&str, &str, &str, usize, usize); 15] = [
    (
        "r19",
        "python-r19-ratio-admissions.tsv",
        "56354d113eaecd1f10bbe2c250354cca797fb4f73b729141a7a346515f98766a",
        2,
        4,
    ),
    (
        "r21",
        "python-r21-ratio-admissions.tsv",
        "918250854e4c2d807a8fe090208cde815905e17f05c63dcc611cdf0f479d3ee4",
        2,
        4,
    ),
    (
        "r23",
        "python-r23-seed-invite-admission.tsv",
        "db4feda148813f51e7913435e48cf12911de83ad52b89ea2d4adfba83015c9ff",
        1,
        4,
    ),
    (
        "r24",
        "python-r24-member-lease-admission.tsv",
        "1e9c97d8f04c005c8ff4f9d83a5d16d1423085dd770a1cb8917505e87ec94414",
        3,
        4,
    ),
    (
        "r25",
        "python-r25-counts.tsv",
        "2264339462758573d6d592c7d71e8f26016368e107c5df243abc435e1bddb900",
        3,
        3,
    ),
    (
        "r26",
        "python-r26-seed-inputs-admission.tsv",
        "f4fa62f141482988f6c6ee920123d3a502db0015990d0b8dc50ea5fc64efe893",
        2,
        4,
    ),
    (
        "r27",
        "python-r27-delivery-wake.tsv",
        "81027e8e5f5ac0b5abd29b8c122b3ff28ba3f0d5f24c2c4664e02b60d1e0ca55",
        1,
        3,
    ),
    (
        "r28",
        "python-r28-seed-http-admission.tsv",
        "81bd77b6f4a2c5f3ffbde578bff75bd5321b378dda6df84de09d30b684270e70",
        1,
        4,
    ),
    (
        "r29",
        "python-r29-retry-multiplier.tsv",
        "5e2a0d17e0e57f3f3777a0c05b930da1f24cce7fa809219e750912b69a08eb2b",
        1,
        3,
    ),
    (
        "r30",
        "python-r30-workflow-tokens.tsv",
        "58f3d10316b0fd33bd99fcbcb526441b6d0348a9e3303329d94ef0bfec593114",
        1,
        3,
    ),
    (
        "r31",
        "python-r31-seed-admission.tsv",
        "d4bdffae9b3628faccc9e74cc290cd4c2845f6db7f3f02906d56dac16d6b3b24",
        3,
        4,
    ),
    (
        "r32",
        "python-r32-verifier-cohort.tsv",
        "a0df304554a5c1001c59da50255e79f5c611da340a2fe45c87fd17826155c6af",
        5,
        3,
    ),
    (
        "r36",
        "python-r36-stats-cohort.tsv",
        "09524ce27dcc28e97c56fc79c34d8e38d8a22430d3acceb05dba57e9f5284d94",
        3,
        3,
    ),
    (
        "r50",
        "python-r50-recovery-forward-cohort.tsv",
        "bf23108957366bf8aac2d84f3e3a693d5f97e5528d8e00069fbacfb8d0de1ff0",
        2,
        3,
    ),
    (
        "r52",
        "python-r52-lane-a-pins.tsv",
        "4b39747c02c22b4b52558bfcbb3eac4d001f407ae166a0ac7d37c97159040cf8",
        2,
        3,
    ),
];

/// The sealed manifest bytes, read independently of the library.
fn manifest_text(name: &str) -> &'static str {
    match name {
        "python-r19-ratio-admissions.tsv" => {
            include_str!("../data/python-r19-ratio-admissions.tsv")
        }
        "python-r21-ratio-admissions.tsv" => {
            include_str!("../data/python-r21-ratio-admissions.tsv")
        }
        "python-r23-seed-invite-admission.tsv" => {
            include_str!("../data/python-r23-seed-invite-admission.tsv")
        }
        "python-r24-member-lease-admission.tsv" => {
            include_str!("../data/python-r24-member-lease-admission.tsv")
        }
        "python-r25-counts.tsv" => include_str!("../data/python-r25-counts.tsv"),
        "python-r26-seed-inputs-admission.tsv" => {
            include_str!("../data/python-r26-seed-inputs-admission.tsv")
        }
        "python-r27-delivery-wake.tsv" => include_str!("../data/python-r27-delivery-wake.tsv"),
        "python-r28-seed-http-admission.tsv" => {
            include_str!("../data/python-r28-seed-http-admission.tsv")
        }
        "python-r29-retry-multiplier.tsv" => {
            include_str!("../data/python-r29-retry-multiplier.tsv")
        }
        "python-r30-workflow-tokens.tsv" => include_str!("../data/python-r30-workflow-tokens.tsv"),
        "python-r31-seed-admission.tsv" => include_str!("../data/python-r31-seed-admission.tsv"),
        "python-r32-verifier-cohort.tsv" => include_str!("../data/python-r32-verifier-cohort.tsv"),
        "python-r36-stats-cohort.tsv" => include_str!("../data/python-r36-stats-cohort.tsv"),
        "python-r50-recovery-forward-cohort.tsv" => {
            include_str!("../data/python-r50-recovery-forward-cohort.tsv")
        }
        "python-r52-lane-a-pins.tsv" => include_str!("../data/python-r52-lane-a-pins.tsv"),
        other => panic!("unknown manifest {other}"),
    }
}

/// R50 `KEYS` (`r50_recovery_forward_admission.rs:11-14`) and R52 `lane_a::KEYS` (`:195-198`), in order.
const R50_KEYS: [&str; 2] = [
    "mycelium_a6_recovery.py:150:31:validator-keyword:maximum",
    "mycelium_a6_recovery.py:158:34:validator-keyword:minimum",
];
const R52_KEYS: [&str; 2] = [
    "mycelium_replica_contracts.py:146:42:validator-keyword:minimum",
    "mycelium_qualification/qualifier.py:777:42:validator-keyword:minimum",
];
/// R36 `KEYS` (`r36_stats_admission.rs:10-14`).
const R36_KEYS: [&str; 3] = [
    "mycelium_swarm_directory/stats_docs.py:21:30:validator-keyword:low",
    "mycelium_swarm_directory/stats_docs.py:21:40:validator-keyword:high",
    "mycelium_swarm_directory/stats_docs.py:31:26:validator-keyword:high",
];
/// R32 `OLD` (`r32_verifier_admission.rs:13`).
const R32_OLD: &str = "mycelium_release_bundle/verifier.py:529:4:assignment:maximum_entries";
/// The discovery cohorts' census keys (R25/R27/R29/R30 `keys()`, R32 `keys()` = reviews minus `OLD`).
const DISCOVERIES: [(&str, &[&str]); 5] = [
    (
        "r25",
        &[
            "mycelium_a13_macos/managed_control.py:104:0:assignment:_MAX_ACTIVE_WORK",
            "mycelium_a13_macos/managed_control.py:105:0:assignment:_MAX_ACTIVE_CONTROL",
            "mycelium_a13_macos/managed_offers.py:61:59:manual-bound:maximum_preposition_path_characters",
        ],
    ),
    (
        "r27",
        &["mycelium_a13_macos/managed_runtime.py:461:16:assignment:delay"],
    ),
    (
        "r29",
        &["mycelium_a13_macos/managed_runtime.py:463:93:manual-bound:outbound_retry_multiplier"],
    ),
    (
        "r30",
        &[
            "mycelium_a13_macos/workflow.py:206:66:manual-bound:verified_challenge_token_limit.maximum",
        ],
    ),
    (
        "r32",
        &[
            "mycelium_release_bundle/verifier.py:24:0:assignment:MAX_FILE_COUNT",
            "mycelium_release_bundle/verifier.py:26:0:assignment:MAX_BUNDLE_PATH_COMPONENTS",
            "mycelium_release_bundle/verifier.py:615:11:comparison:len(pure.parts)",
            "mycelium_release_bundle/verifier.py:676:55:comparison:len(entries)",
        ],
    ),
];
/// Registry ids the helpers pin for their own cohort (`IDS` of R32/R36/R50; R52 is a not-a-bound cohort).
const IDS: [(&str, &[&str]); 4] = [
    (
        "r32",
        &[
            "PyReleaseBundleVerifierMaxFileCountL24C0",
            "PyReleaseBundleVerifierMaxPathComponentsL26C0",
            "PyReleaseBundleVerifierInventoryShippedCeilingL529C4",
        ],
    ),
    (
        "r36",
        &[
            "PySourceUnboundedR36StatsLowL21C30",
            "PySourceUnboundedR36StatsHighL21C40",
            "PySourceUnboundedR36StatsMaxNL31C26",
        ],
    ),
    (
        "r50",
        &[
            "PySourceUnboundedR50RecoveryMaximumL150C31",
            "PySourceUnboundedR50RecoveryMinimumL158C34",
        ],
    ),
    ("r52", &[]),
];
/// Citation pin unions: before R52 (`r50_recovery_forward_admission.rs:139-141`) and live (`:211-213`).
const PRE_R52_PIN_UNION: &str = "f7c5468baaa8777bfddb495c652b7173c0f7a3b8dda689c9da01fcdd04ffad7c";
const LIVE_PIN_UNION: &str = "4c28fcc08f9531db31cf62d22ef384b6b008f03a7b62058f6defb0302697d35a";

fn sha(text: &str) -> String {
    oracle_blobs::sha256_hex(text.as_bytes())
}

fn code<T>(result: Result<T, Refusal>) -> Option<&'static str> {
    result.err().map(|refusal| refusal.code)
}

fn production_inputs(catalogue: &str) -> Inputs<'_> {
    Inputs {
        catalogue,
        ru053: epochs::RU053_ADMISSIONS,
        ru053_pins: RU053_PINS,
        tree: &Workspace,
    }
}

fn pin_union(pins: &epochs::PinMap) -> String {
    sha(&pins
        .iter()
        .map(|((path, line), digest)| format!("{path}\t{line}\t{digest}\n"))
        .collect::<String>())
}

/// The one exact review line of `key` in the checked-in review tables, found without the library.
fn review_line(key: &str) -> Option<&'static str> {
    let found: Vec<_> = [
        include_str!("../data/python-bounds.tsv"),
        include_str!("../data/python-adjudication-a.tsv"),
        include_str!("../data/python-adjudication-b.tsv"),
        include_str!("../data/python-adjudication-forwarded-thresholds.tsv"),
    ]
    .iter()
    .flat_map(|text| text.lines().skip(1))
    .filter(|line| line.split('\t').next() == Some(key))
    .collect();
    (found.len() == 1).then(|| found[0])
}

fn candidate_line(key: &str) -> Option<&'static str> {
    include_str!("../data/python-candidates.tsv")
        .lines()
        .skip(1)
        .find(|line| line.split('\t').next() == Some(key))
}

/// Copy `text` with every line for which `drop` holds removed, keeping the final newline.
fn without(text: &str, mut drop: impl FnMut(&[&str]) -> bool) -> String {
    text.lines()
        .filter(|line| !drop(&line.split('\t').collect::<Vec<_>>()))
        .map(|line| format!("{line}\n"))
        .collect()
}

/// Run a refusal on an injected copy, then prove the checked-in catalogue is byte-identical and validates.
fn refused_then_restored(injected: &str, expected: &'static str) {
    let before = sha(CATALOGUE);
    assert_ne!(
        injected, CATALOGUE,
        "the control must actually mutate its copy"
    );
    assert_eq!(
        code(epochs::validate_with(&production_inputs(injected))),
        Some(expected)
    );
    assert_eq!(
        sha(CATALOGUE),
        before,
        "checked-in catalogue bytes unchanged"
    );
    assert_eq!(
        code(epochs::validate_with(&production_inputs(CATALOGUE))),
        None
    );
}

#[test]
fn admission_catalogue_order_matches_round_history() {
    let ids: Vec<&str> = epochs::EPOCHS.iter().map(|e| e.id).collect();
    assert_eq!(ids, HISTORY);
    // Every helper cohort is one epoch, and every historical epoch's manifest is its helper's include.
    let common = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/common");
    let mut helpers = BTreeMap::new();
    for entry in std::fs::read_dir(&common).unwrap() {
        let name = entry.unwrap().file_name().into_string().unwrap();
        if let Some(stem) = name.strip_suffix("_admission.rs") {
            let round = stem.split('_').next().unwrap().to_string();
            helpers.insert(round, std::fs::read_to_string(common.join(&name)).unwrap());
        }
    }
    let lane_a = helpers["r50"]
        .split("pub mod lane_a")
        .nth(1)
        .unwrap()
        .to_string();
    helpers.insert("r52".into(), lane_a);
    let historical: BTreeSet<&str> = HISTORY[..15].iter().copied().collect();
    assert_eq!(
        helpers.keys().map(String::as_str).collect::<BTreeSet<_>>(),
        historical
    );
    for spec in epochs::EPOCHS.iter().filter(|e| e.id != epochs::RU053) {
        assert!(
            helpers[spec.id].contains(&format!("data/{}\")", spec.manifest_name)),
            "{} helper includes {}",
            spec.id,
            spec.manifest_name
        );
    }
    let rounds: Vec<u32> = HISTORY[..15]
        .iter()
        .map(|id| id[1..].parse().unwrap())
        .collect();
    assert!(rounds.windows(2).all(|w| w[0] < w[1]));
    // The checked-in catalogue lists the same epochs in the same order.
    let catalogue = epochs::catalogue();
    assert_eq!(catalogue.as_ref().map(|c| c.ids()), Ok(HISTORY.to_vec()));
}

#[test]
fn admission_catalogue_records_match_pinned_manifests() {
    let catalogue = epochs::catalogue().expect("checked-in catalogue validates");
    for (id, name, pinned, count, column) in MANIFESTS {
        let text = manifest_text(name);
        assert_eq!(sha(text), pinned, "{name}");
        let epoch = catalogue.epoch(id);
        assert_eq!(epoch.map(|e| e.manifest.as_str()), Some(name), "{id}");
        assert_eq!(
            epoch.map(|e| e.manifest_sha256.as_str()),
            Some(pinned),
            "{id}"
        );
        // Ordered keys and the sealed record digest per key, straight from the manifest.
        let sealed: Vec<(String, String)> = if id == "r52" {
            text.lines()
                .skip(1)
                .map(|l| l.split('\t').collect::<Vec<_>>())
                .filter(|f| f[0] == "record")
                .map(|f| (f[1].to_string(), f[column].to_string()))
                .collect()
        } else {
            text.lines()
                .skip(1)
                .map(|l| l.split('\t').collect::<Vec<_>>())
                .map(|f| (f[0].to_string(), f[column].to_string()))
                .collect()
        };
        assert_eq!(sealed.len(), count, "{id}");
        let keys: Vec<String> = sealed.iter().map(|(k, _)| k.clone()).collect();
        assert_eq!(epoch.map(|e| e.keys.clone()), Some(keys), "{id}");
        for (key, digest) in &sealed {
            assert_eq!(
                epoch.and_then(|e| e.records.get(key)),
                Some(digest),
                "{id} {key}"
            );
            assert_eq!(
                review_line(key).map(sha).as_ref(),
                Some(digest),
                "{id} live {key}"
            );
        }
    }
    let epoch = |id: &str| catalogue.epoch(id).cloned().unwrap_or_default();
    assert_eq!(epoch("r50").keys, R50_KEYS);
    assert_eq!(epoch("r52").keys, R52_KEYS);
    assert_eq!(epoch("r36").keys, R36_KEYS);
    assert!(epoch("r32").keys.contains(&R32_OLD.to_string()));
    // Discovery cohorts add exactly their census keys, each bound to its census line.
    for (id, census) in DISCOVERIES {
        let expected: BTreeMap<String, String> = census
            .iter()
            .map(|k| (k.to_string(), sha(candidate_line(k).unwrap())))
            .collect();
        assert_eq!(epoch(id).census, expected, "{id}");
    }
    let discovery: BTreeSet<&str> = DISCOVERIES.iter().map(|(id, _)| *id).collect();
    for id in HISTORY.iter().filter(|id| !discovery.contains(*id)) {
        assert!(epoch(id).census.is_empty(), "{id}");
    }
    for (id, ids) in IDS {
        let expected: BTreeSet<String> = ids.iter().map(|s| s.to_string()).collect();
        assert_eq!(epoch(id).ids, expected, "{id}");
    }
    // R52's exact added pin set is its manifest's `added-pin` rows; the RU-053 epoch is empty here.
    let r52_added: epochs::PinMap = manifest_text("python-r52-lane-a-pins.tsv")
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .filter(|f| f[0] == "added-pin")
        .map(|f| ((f[1].to_string(), f[2].parse().unwrap()), f[3].to_string()))
        .collect();
    assert_eq!(r52_added.len(), 9475);
    assert_eq!(epoch("r52").pins, r52_added);
    assert_eq!(epoch("r50").pins.len(), 218);
    let ru053 = epoch(epochs::RU053);
    assert!(ru053.keys.is_empty() && ru053.pins.is_empty() && ru053.ids.is_empty());
    // Every RU-053 pin is the oracle line's digest in the ledger encoding (no strip()).
    let rows: Vec<Vec<&str>> = RU053_PINS
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect())
        .collect();
    if !rows.is_empty() {
        let paths: Vec<String> = rows.iter().map(|f| f[0].to_string()).collect();
        let blobs = oracle_blobs::blobs(&paths);
        for f in &rows {
            let line = oracle_blobs::line_of(&blobs[f[0]], f[1].parse().unwrap()).unwrap();
            assert_eq!(sha(line), f[2], "{}:{}", f[0], f[1]);
        }
    }
    // The projected citation pin unions reproduce both historical union digests.
    let r50 = epochs::as_of("r50").expect("as of r50");
    assert_eq!(pin_union(&r50.pins), PRE_R52_PIN_UNION);
    let r52 = epochs::as_of("r52").expect("as of r52");
    assert_eq!(pin_union(&r52.pins), LIVE_PIN_UNION);
}

#[test]
fn admission_catalogue_as_of_reproduces_pinned_triples() {
    // Expected triples copied from today's historical asserts.
    for (epoch, triple, site) in [
        (
            "r30",
            (8258, 8189, 69),
            "inventory_r30_workflow_tokens.rs:164",
        ),
        ("r32", (8262, 8197, 65), "inventory_r32_verifier.rs:273"),
        (
            "r36",
            (8262, 8200, 62),
            "inventory_r36_stats_forwarded.rs:333",
        ),
        (
            "r50",
            (8262, 8202, 60),
            "inventory_r50_recovery_forward.rs:431",
        ),
        ("r52", (8262, 8204, 58), "inventory_r52_lane_a.rs:37-39"),
    ] {
        assert_eq!(
            epochs::as_of(epoch).map(|v| v.triple()),
            Ok(triple),
            "{site}"
        );
    }
    let keys = |text: &'static str| -> BTreeSet<String> {
        text.lines().skip(1).map(str::to_string).collect()
    };
    let minus = |set: &BTreeSet<String>, gone: &[&str]| -> BTreeSet<String> {
        set.iter()
            .filter(|k| !gone.contains(&k.as_str()))
            .cloned()
            .collect()
    };
    let pending = |epoch: &str| {
        epochs::as_of(epoch)
            .map(|v| v.pending())
            .unwrap_or_default()
    };
    // inventory_r30_workflow_tokens.rs:155-163: the R28 prior pending minus the R28 key.
    let r28 = keys(include_str!("../data/python-r28-prior-pending.tsv"));
    assert_eq!(
        pending("r30"),
        minus(&r28, &["mycelium_seed/http.py:968:8:assignment:expires"])
    );
    // inventory_r32_verifier.rs:259-272: the R32 prior 66 minus OLD.
    let r32 = keys(include_str!("../data/python-r32-prior-pending.tsv"));
    assert_eq!(r32.len(), 66);
    assert_eq!(pending("r32"), minus(&r32, &[R32_OLD]));
    // inventory_r36_stats_forwarded.rs:325-332 and inventory_r50_recovery_forward.rs:420-430.
    let r36 = keys(include_str!("../data/python-r36-prior-pending.tsv"));
    assert_eq!(r36.len(), 65);
    let prior62 = minus(&r36, &R36_KEYS);
    assert_eq!(pending("r36"), prior62);
    let prior60 = minus(&prior62, &R50_KEYS);
    assert_eq!(pending("r50"), prior60);
    // inventory_r52_lane_a.rs:40-55: the 60 minus exactly the two lane-a dispositions.
    assert_eq!(pending("r52"), minus(&prior60, &R52_KEYS));
    // added_after names exactly the later cohorts' additions.
    let after50 = epochs::added_after("r50").unwrap_or_default();
    assert_eq!(
        after50.reviewed,
        R52_KEYS
            .iter()
            .map(|k| k.to_string())
            .collect::<BTreeSet<String>>()
    );
    assert_eq!(after50.pins.len(), 9475);
    assert!(after50.census.is_empty() && after50.ids.is_empty());
    let after30 = epochs::added_after("r30").unwrap_or_default();
    assert_eq!(
        after30.census,
        DISCOVERIES[4]
            .1
            .iter()
            .map(|k| k.to_string())
            .collect::<BTreeSet<String>>()
    );
    assert_eq!(after30.reviewed.len(), 8204 - 8189);
}

#[test]
fn admission_catalogue_refuses_missing_pin() {
    // Drop one R50 pin row: the cohort's citations still add that line, so its pin set is incomplete.
    let mut dropped = false;
    let injected = without(CATALOGUE, |f| {
        let hit = !dropped && f[0] == "r50" && f[1] == "pin";
        dropped |= hit;
        hit
    });
    refused_then_restored(&injected, "missing-pin");
}

#[test]
fn admission_catalogue_refuses_extra_pin() {
    // Declare an R19 pin a second time under R50: an earlier cohort already added it, so R50 does not.
    let stray = CATALOGUE
        .lines()
        .find(|l| l.starts_with("r19\tpin\t"))
        .unwrap()
        .replacen("r19", "r50", 1);
    let last = CATALOGUE
        .lines()
        .rfind(|l| l.starts_with("r50\tpin\t"))
        .unwrap();
    let injected = CATALOGUE.replacen(&format!("{last}\n"), &format!("{last}\n{stray}\n"), 1);
    refused_then_restored(&injected, "extra-pin");
}

#[test]
fn admission_catalogue_refuses_dropped_key() {
    let injected = without(CATALOGUE, |f| {
        f[0] == "r50" && f[1] == "key" && f[2] == R50_KEYS[1]
    });
    refused_then_restored(&injected, "dropped-key");
}

#[test]
fn admission_catalogue_refuses_flipped_record_hash() {
    let row = CATALOGUE
        .lines()
        .find(|l| l.starts_with(&format!("r50\tkey\t{}\t", R50_KEYS[0])))
        .unwrap();
    let (head, digest) = row.rsplit_once('\t').unwrap();
    let flipped = if digest.starts_with('0') { '1' } else { '0' };
    let injected = CATALOGUE.replacen(row, &format!("{head}\t{flipped}{}", &digest[1..]), 1);
    refused_then_restored(&injected, "record-hash");
}

/// A synthetic RU-053 record used only as a validator control. It is NOT an admission: the artifact-chunk
/// batch owns the real record, its behaviour test and its parity tests.
#[derive(Clone)]
struct Record {
    key: &'static str,
    site: (&'static str, usize),
    carrier: String,
    token: String,
    /// `(rust path:fn, v0.1.4 path, line)`, root first, carrier last.
    hops: Vec<(String, &'static str, usize)>,
    behaviour: String,
    parity: String,
    /// The reviewed edge rows that join each hop pair (hop i to hop i+1): their `path:line` sites joined by `,`
    /// (one per reviewed step), pairs joined by ` > `. Emitted only where `RU053_HEADER` names the
    /// `consumer_calls` column.
    calls: Vec<String>,
}

impl Record {
    fn control() -> Self {
        Record {
            key: "mycelium_mobile/artifact_grant.py:416:15:comparison:0",
            site: ("mycelium_mobile/artifact_grant.py", 416),
            carrier: "crates/protocol/src/artifact_grant.rs:806".into(),
            token: "!data.is_empty()".into(),
            hops: vec![
                (
                    "crates/runtime/src/iroh_artifact.rs:ChunkSession::fetch".into(),
                    "native/mobile_core/src/artifact_net.rs",
                    583,
                ),
                (
                    "crates/protocol/src/artifact_grant.rs:verify_chunk_receipt".into(),
                    "mycelium_mobile/artifact_grant.py",
                    416,
                ),
            ],
            behaviour: "protocol::artifact_refusals::every_executed_oracle_grant_request_and_receipt_outcome_is_reproduced".into(),
            parity: "-".into(),
            // Reviewed edge rows: `ChunkSession::fetch` calls `self.fetch_once(..)` at :60, whose body calls
            // `protocol::artifact_grant::verify_chunk_receipt(..)` at :126.
            calls: vec![format!("{CONSUMER_FILE}:60,{CONSUMER_FILE}:126")],
        }
    }

    fn cited(&self) -> BTreeSet<(String, usize)> {
        std::iter::once((self.site.0.to_string(), self.site.1))
            .chain(self.hops.iter().map(|h| (h.1.to_string(), h.2)))
            .collect()
    }

    fn line(&self, digests: &BTreeMap<(String, usize), String>) -> String {
        let consumer = if self.hops.is_empty() {
            "-".to_string()
        } else {
            self.hops
                .iter()
                .map(|(rust, path, line)| format!("{rust}={path}:{line}"))
                .collect::<Vec<_>>()
                .join(" > ")
        };
        let calls = if self.calls.is_empty() {
            "-".to_string()
        } else {
            self.calls.join(" > ")
        };
        RU053_HEADER
            .split('\t')
            .map(|column| match column {
                "key" => self.key.to_string(),
                "site" => format!("{}:{}", self.site.0, self.site.1),
                "site_sha256" => digests[&(self.site.0.to_string(), self.site.1)].clone(),
                "carrier" => self.carrier.clone(),
                "carrier_token" => self.token.clone(),
                "consumer" => consumer.clone(),
                "consumer_calls" => calls.clone(),
                "behaviour_test" => self.behaviour.clone(),
                "parity_test" => self.parity.clone(),
                other => panic!("unknown RU-053 column {other}"),
            })
            .collect::<Vec<_>>()
            .join("\t")
    }
}

/// Injected `(catalogue, ru053 records, ru053 pins)` for `records`: the RU-053 epoch rows are rebuilt from
/// the injected bytes; every other catalogue row, including the pinned pre-RU-053 base, is kept verbatim.
fn inject(records: &[Record]) -> (String, String, String) {
    let parsed = adj::parse(&adj::TABLES).unwrap();
    let cited: BTreeSet<(String, usize)> = records.iter().flat_map(Record::cited).collect();
    let paths: Vec<String> = cited
        .iter()
        .map(|(p, _)| p.clone())
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect();
    let blobs = if paths.is_empty() {
        BTreeMap::new()
    } else {
        oracle_blobs::blobs(&paths)
    };
    let digests: BTreeMap<(String, usize), String> = cited
        .iter()
        .map(|(p, n)| {
            let line = oracle_blobs::line_of(&blobs[p], *n).unwrap();
            ((p.clone(), *n), sha(line))
        })
        .collect();
    let pins: String = std::iter::once("path\tline\tsha256\n".to_string())
        .chain(
            digests
                .iter()
                .filter(|(k, _)| !parsed.pins.contains_key(*k))
                .map(|((p, n), d)| format!("{p}\t{n}\t{d}\n")),
        )
        .collect();
    let lines: Vec<String> = records.iter().map(|r| r.line(&digests)).collect();
    let ru053: String = std::iter::once(format!("{RU053_HEADER}\n"))
        .chain(lines.iter().map(|l| format!("{l}\n")))
        .collect();
    let mut catalogue = without(CATALOGUE, |f| f[0] == epochs::RU053 && f[1] != "base");
    let base = CATALOGUE
        .lines()
        .find(|l| l.starts_with("ru053\tbase\t"))
        .unwrap();
    catalogue = catalogue.replacen(&format!("{base}\n"), "", 1);
    catalogue.push_str(&format!(
        "ru053\tmanifest\tru053-admissions.tsv\t{}\t{}\n",
        records.len(),
        sha(&ru053)
    ));
    for (record, line) in records.iter().zip(&lines) {
        catalogue.push_str(&format!("ru053\tkey\t{}\t0\t{}\n", record.key, sha(line)));
    }
    catalogue.push_str(&format!(
        "ru053\tpins\tru053-admission-pins.tsv\t{}\t{}\n",
        pins.lines().count() - 1,
        sha(&pins)
    ));
    catalogue.push_str(&format!("{base}\n"));
    (catalogue, ru053, pins)
}

fn ru053_code(records: &[Record]) -> Option<&'static str> {
    let (catalogue, ru053, pins) = inject(records);
    code(epochs::validate_with(&Inputs {
        catalogue: &catalogue,
        ru053: &ru053,
        ru053_pins: &pins,
        tree: &Workspace,
    }))
}

/// The unmodified control validates, admits exactly its key and drops exactly that violation, while the
/// historical view before RU-053 keeps its pinned triple.
fn control_is_admitted() {
    let (catalogue, ru053, pins) = inject(&[Record::control()]);
    let inputs = Inputs {
        catalogue: &catalogue,
        ru053: &ru053,
        ru053_pins: &pins,
        tree: &Workspace,
    };
    assert_eq!(code(epochs::validate_with(&inputs)), None);
    let key = Record::control().key.to_string();
    assert_eq!(
        epochs::ru053_admitted_with(&inputs),
        Ok(BTreeSet::from([key.clone()]))
    );
    let violations = epochs::evaluate_with(&inputs).unwrap_or_default();
    assert_eq!(violations.len(), 57);
    assert!(!violations.contains(&format!("{UNREVIEWED}{key}")));
    assert_eq!(
        epochs::as_of_with(&inputs, "r52").map(|v| v.triple()),
        Ok((8262, 8204, 58))
    );
}

#[test]
fn admission_catalogue_ru053_refuses_missing_consumer_mapping() {
    let mut record = Record::control();
    record.hops.clear();
    assert_eq!(
        ru053_code(&[record]),
        Some("ru053-missing-consumer-mapping")
    );
    // The carrier alone (one hop, no product root) is not a consumer mapping either.
    let mut record = Record::control();
    record.hops.remove(0);
    assert_eq!(
        ru053_code(&[record]),
        Some("ru053-missing-consumer-mapping")
    );
    // Every hop must resolve to an existing fn in the tree.
    let mut record = Record::control();
    record.hops[0].0 = "crates/runtime/src/iroh_artifact.rs:no_such_fetch".into();
    assert_eq!(ru053_code(&[record]), Some("ru053-unresolved-consumer-hop"));
    control_is_admitted();
}

#[test]
fn admission_catalogue_ru053_refuses_unresolved_carrier() {
    // A token that is not on the cited line.
    let mut record = Record::control();
    record.carrier = "crates/protocol/src/artifact_grant.rs:479".into();
    assert_eq!(ru053_code(&[record]), Some("ru053-unresolved-carrier"));
    // A carrier file that does not exist.
    let mut record = Record::control();
    record.carrier = "crates/protocol/src/no_such_carrier.rs:480".into();
    assert_eq!(ru053_code(&[record]), Some("ru053-unresolved-carrier"));
    // Free text instead of path:line (8282391 accepted any non-empty rust site).
    let mut record = Record::control();
    record.carrier = "not yet ported: lands in crates/protocol".into();
    assert_eq!(ru053_code(&[record]), Some("ru053-unresolved-carrier"));
    control_is_admitted();
}

#[test]
fn admission_catalogue_ru053_refuses_missing_behaviour_test() {
    let mut record = Record::control();
    record.behaviour = "-".into();
    assert_eq!(ru053_code(&[record]), Some("ru053-missing-behaviour-test"));
    // A named test that does not exist is missing too.
    let mut record = Record::control();
    record.behaviour = "protocol::artifact_refusals::no_such_behaviour_test".into();
    assert_eq!(ru053_code(&[record]), Some("ru053-missing-behaviour-test"));
    control_is_admitted();
}

#[test]
fn admission_catalogue_ru053_empty_keeps_closure_58() {
    // An injected EMPTY RU-053 catalogue against the pinned pre-RU-053 base: all 58 violations remain.
    let (catalogue, ru053, pins) = inject(&[]);
    assert_eq!(ru053, format!("{RU053_HEADER}\n"));
    let inputs = Inputs {
        catalogue: &catalogue,
        ru053: &ru053,
        ru053_pins: &pins,
        tree: &Workspace,
    };
    let violations = epochs::evaluate_with(&inputs).unwrap_or_default();
    assert_eq!(violations.len(), 58);
    assert!(violations.iter().all(|v| v.starts_with(UNREVIEWED)));
    // The three guarded stats rows (RU-053) stay among them (inventory_r36_stats_forwarded.rs:334-340).
    for key in [
        "mycelium_swarm_directory/stats_docs.py:45:41:validator-keyword:high",
        "mycelium_swarm_directory/stats_docs.py:76:35:validator-keyword:high",
        "mycelium_swarm_directory/stats_docs.py:80:41:validator-keyword:high",
    ] {
        assert!(violations.contains(&format!("{UNREVIEWED}{key}")), "{key}");
    }
}

/// The workspace with one file's text replaced: a reviewer probe on the validator's text input only (no
/// file on disk changes and nothing but budget-model is built).
struct Patched {
    path: &'static str,
    text: String,
}

impl Tree for Patched {
    fn read(&self, path: &str) -> Option<String> {
        if path == self.path {
            Some(self.text.clone())
        } else {
            Workspace.read(path)
        }
    }
}

/// `records` injected against `tree` is refused with exactly `expected` by validation, admission and
/// evaluation alike, so nothing is admitted: the closure stays the pinned 58, the refused keys among them.
fn refused_keeps_58(records: &[Record], tree: &dyn Tree, expected: &'static str) {
    let (catalogue, ru053, pins) = inject(records);
    let inputs = Inputs {
        catalogue: &catalogue,
        ru053: &ru053,
        ru053_pins: &pins,
        tree,
    };
    assert_eq!(code(epochs::validate_with(&inputs)), Some(expected));
    assert_eq!(code(epochs::ru053_admitted_with(&inputs)), Some(expected));
    assert_eq!(code(epochs::evaluate_with(&inputs)), Some(expected));
    let (catalogue, ru053, pins) = inject(&[]);
    let empty = Inputs {
        catalogue: &catalogue,
        ru053: &ru053,
        ru053_pins: &pins,
        tree,
    };
    let violations = epochs::evaluate_with(&empty).unwrap_or_default();
    assert_eq!(violations.len(), 58);
    for record in records {
        assert!(
            violations.contains(&format!("{UNREVIEWED}{}", record.key)),
            "{}",
            record.key
        );
    }
}

/// `record` injected against `tree` validates, admits exactly its key and drops exactly that violation
/// (closure 57), while the historical view before RU-053 keeps its pinned triple. Mapping-only: an edited
/// `tree` is admitted only once its reviewed rows are re-pinned to the edited bytes (a re-review, [`repinned`]);
/// on the unedited workspace the re-pinned rows are exactly the checked-in ones.
fn admitted_in(record: &Record, tree: &dyn Tree) {
    let (catalogue, ru053, pins) = inject(std::slice::from_ref(record));
    let rereviewed = repinned(tree);
    let inputs = Inputs {
        catalogue: &catalogue,
        ru053: &ru053,
        ru053_pins: &pins,
        tree: &rereviewed,
    };
    assert_eq!(code(epochs::validate_with(&inputs)), None);
    assert_eq!(
        epochs::ru053_admitted_with(&inputs),
        Ok(BTreeSet::from([record.key.to_string()]))
    );
    let violations = epochs::evaluate_with(&inputs).unwrap_or_default();
    assert_eq!(violations.len(), 57);
    assert!(!violations.contains(&format!("{UNREVIEWED}{}", record.key)));
    assert_eq!(
        epochs::as_of_with(&inputs, "r52").map(|v| v.triple()),
        Ok((8262, 8204, 58))
    );
}

#[test]
fn admission_catalogue_ru053_refuses_carrier_only_mapping() {
    // Reviewer probe (rv-logs-cat/duplicate-carrier-only.diff): the carrier hop twice, no product root.
    let mut record = Record::control();
    record.hops[0] = record.hops[1].clone();
    refused_keeps_58(&[record], &Workspace, "ru053-duplicate-consumer-hop");
    // An unconnected hop list: ArtifactWireLimits::wait exists but never reaches verify_chunk_receipt.
    let mut record = Record::control();
    record.hops[0].0 = "crates/runtime/src/iroh_artifact.rs:ArtifactWireLimits::wait".into();
    refused_keeps_58(&[record], &Workspace, "ru053-unconnected-consumer-hop");
    // Connected from the root (fetch reaches wait) but broken before the carrier (wait never reaches it).
    let mut record = Record::control();
    record.hops.insert(
        1,
        (
            "crates/runtime/src/iroh_artifact.rs:ArtifactWireLimits::wait".into(),
            "native/mobile_core/src/artifact_net.rs",
            583,
        ),
    );
    refused_keeps_58(&[record], &Workspace, "ru053-unconnected-consumer-hop");
    // The endpoint fn (verify_chunk_receipt, artifact_grant.rs:765-813) does not contain the carrier line:
    // `!b.is_empty()` at :107 is in `ident`.
    let mut record = Record::control();
    record.carrier = "crates/protocol/src/artifact_grant.rs:107".into();
    record.token = "!b.is_empty()".into();
    refused_keeps_58(&[record], &Workspace, "ru053-carrier-outside-endpoint");
    control_is_admitted();
}

#[test]
fn admission_catalogue_ru053_refuses_unresolved_qualified_hop() {
    // Reviewer probe (rv-logs-cat/unresolved-qualified-hop.diff): `fetch` is ChunkSession's
    // (iroh_artifact.rs:34-53); ArtifactWireLimits (:131-140) defines only `wait`.
    let mut record = Record::control();
    record.hops[0].0 = "crates/runtime/src/iroh_artifact.rs:ArtifactWireLimits::fetch".into();
    refused_keeps_58(&[record], &Workspace, "ru053-unresolved-consumer-hop");
    // The converse: `wait` is not ChunkSession's.
    let mut record = Record::control();
    record.hops[0].0 = "crates/runtime/src/iroh_artifact.rs:ChunkSession::wait".into();
    refused_keeps_58(&[record], &Workspace, "ru053-unresolved-consumer-hop");
    // verify_chunk_receipt is a free fn, not a method of the `impl Grant` in the same file.
    let mut record = Record::control();
    record.hops[1].0 = "crates/protocol/src/artifact_grant.rs:Grant::verify_chunk_receipt".into();
    refused_keeps_58(&[record], &Workspace, "ru053-unresolved-consumer-hop");
    // Positive: the method qualified by its own impl's type resolves and is admitted.
    let mut record = Record::control();
    record.hops[0].0 = "crates/runtime/src/iroh_artifact.rs:ChunkSession::fetch".into();
    admitted_in(&record, &Workspace);
    control_is_admitted();
}

/// The behaviour test's file and the item that starts the referenced test.
const BEHAVIOUR_FILE: &str = "crates/protocol/tests/artifact_refusals.rs";
const BEHAVIOUR_ITEM: &str =
    "#[test]\nfn every_executed_oracle_grant_request_and_receipt_outcome_is_reproduced() {";

#[test]
fn admission_catalogue_ru053_refuses_commented_behaviour_test() {
    let text = Workspace
        .read(BEHAVIOUR_FILE)
        .expect("the behaviour test file");
    let at = text.find(BEHAVIOUR_ITEM).expect("the behaviour test item");
    let (head, item) = text.split_at(at);
    // Reviewer probe (rv-logs-cat/commented-behaviour-source.diff): the whole test in a block comment.
    let commented = Patched {
        path: BEHAVIOUR_FILE,
        text: format!("{head}/*\n{item}*/\n"),
    };
    assert!(commented.text.contains(BEHAVIOUR_ITEM));
    refused_keeps_58(&[Record::control()], &commented, "ru053-stale-pin");
    // The test only inside a (raw) string literal.
    assert!(!item.contains("\"#"));
    let quoted = Patched {
        path: BEHAVIOUR_FILE,
        text: format!("{head}const _ONLY_TEXT: &str = r#\"\n{item}\"#;\n"),
    };
    assert!(quoted.text.contains(BEHAVIOUR_ITEM));
    refused_keeps_58(&[Record::control()], &quoted, "ru053-stale-pin");
    // Positive: the unmodified text, and the real test under a line comment and a nested block comment
    // that themselves spell a test attribute, are admitted.
    let same = Patched {
        path: BEHAVIOUR_FILE,
        text: text.clone(),
    };
    admitted_in(&Record::control(), &same);
    let annotated = Patched {
        path: BEHAVIOUR_FILE,
        text: format!("{head}// #[test]\n/* #[test] /* nested */ fn x() {{}} */\n{item}"),
    };
    admitted_in(&Record::control(), &annotated);
    control_is_admitted();
}

#[test]
fn admission_catalogue_ru053_refuses_guarded_stats_keys() {
    // Reviewer probe (rv-logs-cat/guarded-stats-admission.diff): RU-053 keeps the three guarded stats rows
    // excluded (inventory_r36_stats_forwarded.rs:335-337), whatever the rest of the record says.
    for (key, line) in [
        (
            "mycelium_swarm_directory/stats_docs.py:45:41:validator-keyword:high",
            45,
        ),
        (
            "mycelium_swarm_directory/stats_docs.py:76:35:validator-keyword:high",
            76,
        ),
        (
            "mycelium_swarm_directory/stats_docs.py:80:41:validator-keyword:high",
            80,
        ),
    ] {
        let mut record = Record::control();
        record.key = key;
        record.site = ("mycelium_swarm_directory/stats_docs.py", line);
        record.hops[1].1 = "mycelium_swarm_directory/stats_docs.py";
        record.hops[1].2 = line;
        refused_keeps_58(&[record], &Workspace, "ru053-guarded-key");
    }
    control_is_admitted();
}

/// The consumer file the edge probes patch: `ChunkSession::fetch` calls `self.fetch_once(..)` at :60, and
/// `ChunkSession::fetch_once` calls the protocol `verify_chunk_receipt` at :126 (two lines, :126-127).
const CONSUMER_FILE: &str = "crates/runtime/src/iroh_artifact.rs";
const RECEIPT_CALL: &str = "        protocol::artifact_grant::verify_chunk_receipt(&header[\"receipt\"], request, grant, data)\n            .map_err(|e| fail(e.0))?;\n";
const FETCH_ONCE_CALL: &str =
    "        let result = self.fetch_once(envelope, request, grant, deadline);";
/// A decoy module whose fn has the protocol fn's name and arguments.
const DECOY_MOD: &str = "\nmod decoy {\n    pub fn verify_chunk_receipt(\n        _: &serde_json::Value,\n        _: &ChunkRequest,\n        _: &Grant,\n        _: &[u8],\n    ) -> Result<(), protocol::artifact_grant::GrantError> {\n        Ok(())\n    }\n}\n";

/// A record rooted at `ChunkSession::fetch_once`, whose own body calls the protocol fn at :126: a plain path
/// call, so no reviewed edge is involved and a patched consumer file can still admit it.
fn fetch_once_record() -> Record {
    let mut record = Record::control();
    record.hops[0].0 = format!("{CONSUMER_FILE}:ChunkSession::fetch_once");
    record.calls = vec![format!("{CONSUMER_FILE}:126")];
    record
}

/// The consumer file with its receipt call (:126-127) replaced by exactly two other lines and `tail` appended,
/// so every line number before the tail is unchanged.
fn consumer_with(call: &str, tail: &str) -> Patched {
    let text = Workspace.read(CONSUMER_FILE).expect("the consumer file");
    assert_eq!(text.lines().nth(59), Some(FETCH_ONCE_CALL));
    assert_eq!(text.matches(RECEIPT_CALL).count(), 1);
    let at = text.find(RECEIPT_CALL).expect("the receipt call");
    assert_eq!(
        text[..at].lines().count(),
        125,
        "the receipt call starts at :126"
    );
    assert_eq!(call.matches('\n').count(), 2, "{call}");
    Patched {
        path: CONSUMER_FILE,
        text: format!("{}{tail}", text.replacen(RECEIPT_CALL, call, 1)),
    }
}

/// The workspace with several files' text replaced.
struct PatchedAll(Vec<Patched>);

impl Tree for PatchedAll {
    fn read(&self, path: &str) -> Option<String> {
        match self.0.iter().find(|p| p.path == path) {
            Some(patched) => Some(patched.text.clone()),
            None => Workspace.read(path),
        }
    }
}

/// `path` with the one occurrence of `old` replaced by `new` (same line count, so line numbers hold).
fn replaced(path: &'static str, old: &str, new: &str) -> Patched {
    let text = Workspace.read(path).expect(path);
    assert_eq!(text.matches(old).count(), 1, "{old}");
    assert_eq!(
        old.matches('\n').count(),
        new.matches('\n').count(),
        "{new}"
    );
    Patched {
        path,
        text: text.replacen(old, new, 1),
    }
}

/// `path` with `tail` appended.
fn appended(path: &'static str, tail: &str) -> Patched {
    let text = Workspace.read(path).expect(path);
    Patched {
        path,
        text: format!("{text}{tail}"),
    }
}

#[test]
fn admission_catalogue_ru053_refuses_same_name_wrong_callee() {
    control_is_admitted();
    admitted_in(&fetch_once_record(), &Workspace);
    // A decoy module beside the real call changes nothing: :126 still calls the protocol fn.
    admitted_in(
        &fetch_once_record(),
        &consumer_with(RECEIPT_CALL, DECOY_MOD),
    );
    // `Self::fetch_once(self, ..)` at :60 is a path call to the exact inherent method: proven with no reviewed
    // edge.
    admitted_in(
        &Record::control(),
        &replaced(
            CONSUMER_FILE,
            "self.fetch_once(envelope",
            "Self::fetch_once(self, envelope",
        ),
    );
    // Reviewer probe (rv-logs-cat2/fresh-probes.rs rv_f1_same_name_wrong_callee): `fetch` calls a same-named
    // `decoy::verify_chunk_receipt`, never the protocol fn the record maps it to.
    let decoy = Patched {
        path: CONSUMER_FILE,
        text: "pub fn fetch() { decoy::verify_chunk_receipt(); }\nmod decoy { pub fn verify_chunk_receipt() {} }\n".into(),
    };
    refused_keeps_58(&[Record::control()], &decoy, "ru053-stale-pin");
    let mut record = Record::control();
    record.calls = vec![format!("{CONSUMER_FILE}:1")];
    refused_keeps_58(&[record], &decoy, "ru053-unmapped-edge");
    // In place, every line number kept, the call at :126 names a same-named fn elsewhere: the decoy module's
    // fn by path or through a `use` of that sibling, a local module with the same path tail shadowing the
    // protocol crate, or the same tail under this crate's root.
    for (call, tail) in [
        (
            RECEIPT_CALL.replacen("protocol::artifact_grant::", "decoy::", 1),
            DECOY_MOD.to_string(),
        ),
        (
            RECEIPT_CALL.replacen("protocol::artifact_grant::", "", 1),
            format!("use decoy::verify_chunk_receipt;{DECOY_MOD}"),
        ),
        (
            RECEIPT_CALL.to_string(),
            "mod protocol { pub mod artifact_grant { pub fn verify_chunk_receipt() {} } }\n".into(),
        ),
        (
            RECEIPT_CALL.replacen("protocol::", "crate::protocol::", 1),
            String::new(),
        ),
    ] {
        refused_keeps_58(
            &[fetch_once_record()],
            &consumer_with(&call, &tail),
            "ru053-stale-pin",
        );
    }
    // The call site is not a line of the caller's own body holding that call: the line after fetch_once
    // closes (:130), a line of its body holding no call (:125), the same line number in another file; and an
    // edge with no call site at all.
    for at in [
        vec![format!("{CONSUMER_FILE}:130")],
        vec![format!("{CONSUMER_FILE}:125")],
        vec!["crates/protocol/src/artifact_grant.rs:126".to_string()],
        Vec::new(),
    ] {
        let mut record = fetch_once_record();
        record.calls = at;
        refused_keeps_58(&[record], &Workspace, "ru053-unmapped-edge");
    }
    // `protocol::` names the protocol crate only while the caller's manifest depends on it.
    refused_keeps_58(
        &[fetch_once_record()],
        &replaced(
            "crates/runtime/Cargo.toml",
            "protocol = { path = \"../protocol\" }\n",
            "\n",
        ),
        "ru053-stale-pin",
    );
}

#[test]
fn admission_catalogue_ru053_refuses_uncalled_inner_function() {
    control_is_admitted();
    admitted_in(&fetch_once_record(), &Workspace);
    // Reviewer probe (rv-logs-cat2/fresh-probes.rs rv_f1_uncalled_inner_function): the only call sits in an
    // inner fn that `fetch` never calls.
    let inner = Patched {
        path: CONSUMER_FILE,
        text: "pub fn fetch() { fn never_called() { verify_chunk_receipt(); } }\nfn verify_chunk_receipt() {}\n".into(),
    };
    refused_keeps_58(&[Record::control()], &inner, "ru053-stale-pin");
    let mut record = Record::control();
    record.calls = vec![format!("{CONSUMER_FILE}:1")];
    refused_keeps_58(&[record], &inner, "ru053-unmapped-edge");
    // In place: the real call moved into a nested item of fetch_once's body (inner fn, mod, trait, impl).
    for nested in [
        "        fn never_called(h: &serde_json::Value, r: &ChunkRequest, g: &Grant, d: &[u8]) { let _ = protocol::artifact_grant::verify_chunk_receipt(h, r, g, d); }\n        let _ = (&header, data);\n",
        "        mod never { pub fn called() { let _ = protocol::artifact_grant::verify_chunk_receipt(a(), b(), c(), d()); } }\n        let _ = (&header, data);\n",
        "        trait Never { fn called(&self) { let _ = protocol::artifact_grant::verify_chunk_receipt(a(), b(), c(), d()); } }\n        let _ = (&header, data);\n",
        "        struct Never; impl Never { fn called() { let _ = protocol::artifact_grant::verify_chunk_receipt(a(), b(), c(), d()); } }\n        let _ = (&header, data);\n",
    ] {
        refused_keeps_58(
            &[fetch_once_record()],
            &consumer_with(nested, ""),
            "ru053-stale-pin",
        );
    }
    // Inline, but not provably run: a closure never invoked, an async block never awaited, a const block, a
    // `#[cfg(any())]` statement.
    for deferred in [
        "        let _never = || protocol::artifact_grant::verify_chunk_receipt(&header[\"receipt\"], request, grant, data);\n        let _ = fail;\n",
        "        let _never = async { protocol::artifact_grant::verify_chunk_receipt(&header[\"receipt\"], request, grant, data) };\n        let _ = fail;\n",
        "        let _never = const { protocol::artifact_grant::verify_chunk_receipt(&header[\"receipt\"], request, grant, data) };\n        let _ = fail;\n",
        "        #[cfg(any())] protocol::artifact_grant::verify_chunk_receipt(&header[\"receipt\"], request, grant, data)\n            .map_err(|e| fail(e.0))?;\n",
    ] {
        refused_keeps_58(
            &[fetch_once_record()],
            &consumer_with(deferred, ""),
            "ru053-stale-pin",
        );
    }
}

#[test]
fn admission_catalogue_ru053_refuses_same_named_type_other_module() {
    control_is_admitted();
    let mut own = Record::control();
    own.hops[0].0 = format!("{CONSUMER_FILE}:ChunkSession::fetch");
    admitted_in(&own, &Workspace);
    let original = Workspace.read(CONSUMER_FILE).expect("the consumer file");
    let mut record = Record::control();
    record.hops[0].0 = format!("{CONSUMER_FILE}:ArtifactWireLimits::fetch");
    for tail in [
        // Reviewer probe (rv-logs-cat2/fresh-probes.rs rv_f2_same_named_type_other_module): a decoy module's own
        // `ArtifactWireLimits::fetch`; the root type still has no `fetch`.
        "\nmod decoy { pub struct ArtifactWireLimits; impl ArtifactWireLimits { pub fn fetch() { verify_chunk_receipt(); } } fn verify_chunk_receipt() {} }\n",
        // An impl at the root whose self type is the decoy type, named by path.
        "\nmod decoy { pub struct ArtifactWireLimits; }\nimpl decoy::ArtifactWireLimits { pub fn fetch(&self) { let _ = protocol::artifact_grant::verify_chunk_receipt(a(), b(), c(), d()); } }\n",
        // A trait method of the real root type is not an inherent method.
        "\ntrait Fetch { fn fetch(&self); }\nimpl Fetch for ArtifactWireLimits { fn fetch(&self) { let _ = protocol::artifact_grant::verify_chunk_receipt(a(), b(), c(), d()); } }\n",
    ] {
        let patched = Patched {
            path: CONSUMER_FILE,
            text: format!("{original}{tail}"),
        };
        refused_keeps_58(
            std::slice::from_ref(&record),
            &patched,
            "ru053-unresolved-consumer-hop",
        );
    }
    // A record naming a re-exported path: protocol's lib.rs re-exports `verify_chunk_receipt` but defines no
    // such fn.
    let lib = Workspace
        .read("crates/protocol/src/lib.rs")
        .expect("protocol lib.rs");
    let reexport = Patched {
        path: "crates/protocol/src/lib.rs",
        text: format!("{lib}pub use artifact_grant::verify_chunk_receipt;\n"),
    };
    let mut record = Record::control();
    record.hops[1].0 = "crates/protocol/src/lib.rs:verify_chunk_receipt".into();
    refused_keeps_58(&[record], &reexport, "ru053-unresolved-consumer-hop");
    // A file no module declares is no module path at all, however exact its call.
    let mut orphan = fetch_once_record();
    orphan.hops[0].0 = "crates/runtime/src/orphan.rs:fetch".into();
    orphan.calls = vec!["crates/runtime/src/orphan.rs:1".into()];
    let orphan_file = Patched {
        path: "crates/runtime/src/orphan.rs",
        text: "pub fn fetch() { let _ = protocol::artifact_grant::verify_chunk_receipt(a(), b(), c(), d()); }\n"
            .into(),
    };
    refused_keeps_58(&[orphan], &orphan_file, "ru053-unresolved-consumer-hop");
    // The endpoint's module, or the endpoint fn itself, compiled out.
    for disabled in [
        replaced(
            "crates/protocol/src/lib.rs",
            "pub mod artifact_grant;\n",
            "#[cfg(any())] pub mod artifact_grant;\n",
        ),
        replaced(
            "crates/protocol/src/artifact_grant.rs",
            "pub fn verify_chunk_receipt(\n",
            "#[cfg(any())] pub fn verify_chunk_receipt(\n",
        ),
    ] {
        refused_keeps_58(&[Record::control()], &disabled, "ru053-stale-pin");
    }
}

#[test]
fn admission_catalogue_ru053_refuses_cfg_disabled_behaviour_test() {
    control_is_admitted();
    let text = Workspace
        .read(BEHAVIOUR_FILE)
        .expect("the behaviour test file");
    assert_eq!(text.matches(BEHAVIOUR_ITEM).count(), 1);
    let item_fn = BEHAVIOUR_ITEM.strip_prefix("#[test]\n").expect("#[test]");
    let with = |attributes: &str, prelude: &str| Patched {
        path: BEHAVIOUR_FILE,
        text: text
            .replacen(BEHAVIOUR_ITEM, &format!("{attributes}\n{item_fn}"), 1)
            .replacen("use base64::", &format!("{prelude}use base64::"), 1),
    };
    // Doc comments beside #[test] are fine.
    admitted_in(
        &Record::control(),
        &with("/// Every executed outcome.\n#[test]", ""),
    );
    // Reviewer probe (rv-logs-cat2/fresh-probes.rs rv_f2_disabled_top_level_test) and its relatives: any other
    // attribute beside #[test], `#[cfg_attr(.., test)]` instead of it, a `test` that may not be the built-in
    // one (an imported `test`, a glob import, `tokio::test`), and a file-level `#![cfg(any())]`.
    for (attributes, prelude) in [
        ("#[cfg(any())]\n#[test]", ""),
        ("#[test]\n#[cfg(not(all()))]", ""),
        ("#[test]\n#[ignore]", ""),
        ("#[test]\n#[cfg_attr(all(), ignore)]", ""),
        ("#[test]\n#[should_panic]", ""),
        ("#[test]\n#[doc(hidden)]", ""),
        ("#[cfg_attr(all(), test)]", ""),
        ("#[cfg_attr(any(), test)]", ""),
        ("#[test]", "use protocol::test;\n"),
        ("#[test]", "use protocol::*;\n"),
        ("#[tokio::test]", ""),
        ("#[test]", "#![cfg(any())]\n"),
    ] {
        refused_keeps_58(
            &[Record::control()],
            &with(attributes, prelude),
            "ru053-stale-pin",
        );
    }
    // A `[[test]]` table for the target (here `harness = false`): collection is no longer the default one.
    let manifest = Workspace
        .read("crates/protocol/Cargo.toml")
        .expect("protocol manifest");
    let harness = Patched {
        path: "crates/protocol/Cargo.toml",
        text: format!("{manifest}\n[[test]]\nname = \"artifact_refusals\"\nharness = false\n"),
    };
    refused_keeps_58(&[Record::control()], &harness, "ru053-uncollected-test");
}

#[test]
fn admission_catalogue_ru053_refuses_unsupported_edge_shape() {
    control_is_admitted();
    admitted_in(&fetch_once_record(), &Workspace);
    let receipt = "protocol::artifact_grant::verify_chunk_receipt(&header[\"receipt\"], request, grant, data)";
    for (call, tail) in [
        // A glob import.
        (
            RECEIPT_CALL.replacen("protocol::artifact_grant::", "", 1),
            "use protocol::artifact_grant::*;\n",
        ),
        // An `as` rename of the fn, and of its module.
        (
            RECEIPT_CALL.replacen("protocol::artifact_grant::verify_chunk_receipt", "checked", 1),
            "use protocol::artifact_grant::verify_chunk_receipt as checked;\n",
        ),
        (
            RECEIPT_CALL.replacen("protocol::artifact_grant::", "ag::", 1),
            "use protocol::artifact_grant as ag;\n",
        ),
        // A macro-invoked call.
        (
            RECEIPT_CALL.replacen(receipt, &format!("call!({receipt})"), 1),
            "macro_rules! call { ($e:expr) => { $e }; }\n",
        ),
        // A method call on a receiver no exact rule types, and a qualified-self trait path.
        (
            RECEIPT_CALL.replacen(
                receipt,
                "grant.verify_chunk_receipt(&header[\"receipt\"], request, data)",
                1,
            ),
            "",
        ),
        (
            RECEIPT_CALL.replacen(
                "protocol::artifact_grant::verify_chunk_receipt",
                "<Grant as Verify>::verify_chunk_receipt",
                1,
            ),
            "",
        ),
        // A local binding (a closure) with the callee's name shadowing every item.
        (
            RECEIPT_CALL.replacen(
                "protocol::artifact_grant::",
                "let verify_chunk_receipt = |_: &serde_json::Value, _: &ChunkRequest, _: &Grant, _: &[u8]| -> Result<(), protocol::artifact_grant::GrantError> { loop {} }; ",
                1,
            ),
            "",
        ),
    ] {
        refused_keeps_58(
            &[fetch_once_record()],
            &consumer_with(&call, tail),
            "ru053-stale-pin",
        );
    }
    // What the qualified `protocol::..` path names is no longer proven: a module glob that could bind
    // `protocol`, a renamed dependency, an `extern crate .. as protocol` at the crate root; nor are the body's
    // macros once a `macro_rules!` could shadow `format!` (fetch_once :120) or the body invokes a non-std macro.
    let text = Workspace.read(CONSUMER_FILE).expect("the consumer file");
    for tree in [
        appended(CONSUMER_FILE, "use transport::*;\n"),
        replaced(
            "crates/runtime/Cargo.toml",
            "protocol = { path = \"../protocol\" }",
            "protocol = { package = \"transport\", path = \"../transport\" }",
        ),
        appended(
            "crates/runtime/src/lib.rs",
            "extern crate transport as protocol;\n",
        ),
        appended(
            CONSUMER_FILE,
            "macro_rules! format { ($($t:tt)*) => { String::new() } }\n",
        ),
        Patched {
            path: CONSUMER_FILE,
            text: format!(
                "{}macro_rules! shadow {{ () => {{}} }}\n",
                text.replacen(
                    "        }\n        protocol::artifact_grant",
                    "        } shadow!();\n        protocol::artifact_grant",
                    1
                )
            ),
        },
    ] {
        refused_keeps_58(&[fetch_once_record()], &tree, "ru053-stale-pin");
    }
    // A re-export: `protocol::verify_chunk_receipt` through a `pub use` in protocol's lib.rs.
    let lib = Workspace
        .read("crates/protocol/src/lib.rs")
        .expect("protocol lib.rs");
    let reexport = PatchedAll(vec![
        Patched {
            path: "crates/protocol/src/lib.rs",
            text: format!("{lib}pub use artifact_grant::verify_chunk_receipt;\n"),
        },
        consumer_with(
            &RECEIPT_CALL.replacen("protocol::artifact_grant::", "protocol::", 1),
            "",
        ),
    ]);
    refused_keeps_58(&[fetch_once_record()], &reexport, "ru053-stale-pin");
    // The reviewed edge (`self.fetch_once(..)` at :60) holds only for the consumer file bytes it pins: one
    // more comment line and it no longer applies, while the plain path call at :126 still does.
    let original = Workspace.read(CONSUMER_FILE).expect("the consumer file");
    let touched = Patched {
        path: CONSUMER_FILE,
        text: format!("{original}// reviewed bytes changed\n"),
    };
    admitted_in(&fetch_once_record(), &touched);
    refused_keeps_58(&[Record::control()], &touched, "ru053-stale-pin");
    // An ambiguous bare name: a second `fetch` (a free fn) beside `ChunkSession::fetch`.
    let twice = Patched {
        path: CONSUMER_FILE,
        text: format!("{original}pub fn fetch() {{}}\n"),
    };
    admitted_in(&fetch_once_record(), &twice);
    refused_keeps_58(&[Record::control()], &twice, "ru053-stale-pin");
}

// ---- RU-053 mapping-only records (owner re-cut, INBOX 2026-11-04 04:06) ----

/// The reviewed RU-053 tables, read through the [`Tree`] so a probe can inject an edited copy.
const EDGES_TABLE: &str = epochs::RU053_EDGES_PATH;
const TABLES: [&str; 4] = [
    epochs::RU053_ROOTS_PATH,
    epochs::RU053_EDGES_PATH,
    epochs::RU053_TESTS_PATH,
    epochs::RU053_CRATES_PATH,
];
const PROTOCOL_MANIFEST: &str = "crates/protocol/Cargo.toml";
/// A real call of `ChunkSession::fetch_once` (`self.limits.wait(..)`) that no reviewed edge row lists.
const UNLISTED_CALL_LINE: usize = 118;

/// A tree that serves `files` (and, when set, `metadata`) over `base`.
struct Over<'t> {
    base: &'t dyn Tree,
    files: Vec<Patched>,
    metadata: Option<String>,
}

impl Tree for Over<'_> {
    fn read(&self, path: &str) -> Option<String> {
        match self.files.iter().find(|p| p.path == path) {
            Some(patched) => Some(patched.text.clone()),
            None => self.base.read(path),
        }
    }
    fn metadata(&self) -> Result<String, String> {
        match &self.metadata {
            Some(metadata) => Ok(metadata.clone()),
            None => self.base.metadata(),
        }
    }
}

/// `base` with every reviewed RU-053 row re-pinned to `base`'s bytes, as a re-review of an edited tree would
/// pin them: identities and line numbers kept, each file's sha256 and the line's text recomputed on `base`;
/// a test row follows its unique `#[test]` + `fn` window when lines above it moved.
fn repinned(base: &dyn Tree) -> Over<'_> {
    let files = TABLES
        .into_iter()
        .map(|table| {
            let text = base.read(table).expect(table);
            let mut rows = text.lines();
            let mut out = format!("{}\n", rows.next().expect("header"));
            for row in rows {
                let mut f: Vec<String> = row.split('\t').map(str::to_string).collect();
                let n = f.len();
                if let Some(source) = base.read(&f[n - 4]) {
                    let lines: Vec<&str> = source.split('\n').collect();
                    let mut at: usize = f[n - 3].parse().expect("a line number");
                    if table == epochs::RU053_TESTS_PATH {
                        let window: Vec<usize> = (1..lines.len())
                            .filter(|i| lines[*i] == f[n - 1] && lines[i - 1] == "#[test]")
                            .collect();
                        if let [i] = window[..] {
                            at = i + 1;
                        }
                    }
                    f[n - 3] = at.to_string();
                    f[n - 2] = sha(&source);
                    if let Some(line) = lines.get(at - 1) {
                        f[n - 1] = line.to_string();
                    }
                }
                out.push_str(&format!("{}\n", f.join("\t")));
            }
            Patched {
                path: table,
                text: out,
            }
        })
        .collect();
    Over {
        base,
        files,
        metadata: None,
    }
}

/// The workspace with exactly one row of a reviewed table edited (`edit` returns the new row, or `None` to
/// keep it).
fn edited_rows(table: &'static str, edit: impl Fn(&str) -> Option<String>) -> Over<'static> {
    let text = Workspace.read(table).expect(table);
    let mut edited = 0;
    let text: String = text
        .lines()
        .enumerate()
        .map(|(n, row)| {
            let new = if n == 0 { None } else { edit(row) };
            edited += usize::from(new.is_some());
            format!("{}\n", new.unwrap_or_else(|| row.to_string()))
        })
        .collect();
    assert_eq!(edited, 1, "{table}: exactly one row edited");
    Over {
        base: &Workspace,
        files: vec![Patched { path: table, text }],
        metadata: None,
    }
}

/// The workspace whose cargo metadata gives the behaviour test target `required-features = [feature]` (the
/// actual metadata, that one field set).
fn metadata_requiring(feature: &'static str) -> Over<'static> {
    let mut metadata: serde_json::Value =
        serde_json::from_str(&Workspace.metadata().expect("cargo metadata"))
            .expect("metadata JSON");
    let mut set = 0;
    for package in metadata["packages"].as_array_mut().expect("packages") {
        if package["name"] != "protocol" {
            continue;
        }
        for target in package["targets"].as_array_mut().expect("targets") {
            if target["name"] == "artifact_refusals" {
                target["required-features"] = serde_json::json!([feature]);
                set += 1;
            }
        }
    }
    assert_eq!(set, 1, "the behaviour test target");
    Over {
        base: &Workspace,
        files: Vec::new(),
        metadata: Some(metadata.to_string()),
    }
}

/// Reviewer probe fc19 (rv-logs-catfc/fresh-probes.rs fc19_generic_specialization_trait_dispatch):
/// `Receiver::<u16>::verify_chunk_receipt()` is a `Verify` trait dispatch, not the inherent
/// `Receiver<u8>::verify_chunk_receipt` that holds the carrier.
fn fc19_generic_dispatch() -> (Record, PatchedAll) {
    let mut record = Record::control();
    record.calls = vec![format!("{CONSUMER_FILE}:3")];
    record.hops[1].0 =
        "crates/protocol/src/artifact_grant.rs:Receiver::verify_chunk_receipt".into();
    record.carrier = "crates/protocol/src/artifact_grant.rs:3".into();
    record.token = "!data.is_empty()".into();
    let tree = PatchedAll(vec![
        Patched {
            path: CONSUMER_FILE,
            text: "pub fn fetch() {\n use protocol::artifact_grant::Verify;\n protocol::artifact_grant::Receiver::<u16>::verify_chunk_receipt();\n}\n".into(),
        },
        Patched {
            path: "crates/protocol/src/artifact_grant.rs",
            text: "pub struct Receiver<T>(T);\nimpl Receiver<u8> { pub fn verify_chunk_receipt() { let data: &[u8] = &[];\n let _carrier = !data.is_empty();\n} }\npub trait Verify { fn verify_chunk_receipt(); }\nimpl Verify for Receiver<u16> { fn verify_chunk_receipt() {} }\n".into(),
        },
    ]);
    (record, tree)
}

/// One mapping sub-case: a label, the records and the tree they are checked against.
type Case<'t> = (&'static str, Vec<Record>, &'t dyn Tree);

/// Each case's refusal code (`None`: admitted), all computed before any assertion so a RED run shows every
/// sub-case at once.
fn codes_in(cases: &[Case]) -> Vec<(&'static str, Option<&'static str>)> {
    cases
        .iter()
        .map(|(label, records, tree)| {
            let (catalogue, ru053, pins) = inject(records);
            let inputs = Inputs {
                catalogue: &catalogue,
                ru053: &ru053,
                ru053_pins: &pins,
                tree: *tree,
            };
            (*label, code(epochs::validate_with(&inputs)))
        })
        .collect()
}

/// Every case is refused with its code and keeps the closure at the pinned 58.
fn all_refused_keep_58(cases: &[Case], expected: &[(&'static str, Option<&'static str>)]) {
    assert_eq!(codes_in(cases), expected);
    for ((_, records, tree), (_, code)) in cases.iter().zip(expected) {
        refused_keeps_58(records, *tree, code.expect("a refusal code"));
    }
}

#[test]
fn admission_catalogue_ru053_refuses_stale_edge_pin() {
    // Positive controls: the checked-in reviewed rows admit both records on the unedited tree, and a re-review
    // of the unedited tree reproduces those rows byte for byte.
    control_is_admitted();
    admitted_in(&fetch_once_record(), &Workspace);
    assert_eq!(
        repinned(&Workspace).read(EDGES_TABLE),
        Workspace.read(EDGES_TABLE)
    );
    let original = Workspace.read(CONSUMER_FILE).expect("the consumer file");
    let receipt = RECEIPT_CALL.lines().next().expect("the :126 line");
    // The file's sha256 changed, every pinned line number and text kept.
    let sha_changed = appended(CONSUMER_FILE, "// reviewed bytes changed\n");
    // The :126 line's text changed, every line number kept.
    let text_changed = replaced(
        CONSUMER_FILE,
        receipt,
        &receipt.replacen(
            "verify_chunk_receipt(&header",
            "verify_chunk_receipt( &header",
            1,
        ),
    );
    // Every line moved down by one.
    let line_moved = Patched {
        path: CONSUMER_FILE,
        text: format!("\n{original}"),
    };
    // The carrier's file, the behaviour test's file and the caller crate's manifest changed.
    let carrier_changed = appended(
        "crates/protocol/src/artifact_grant.rs",
        "// reviewed bytes changed\n",
    );
    let test_changed = appended(BEHAVIOUR_FILE, "// reviewed bytes changed\n");
    let crate_changed = appended("crates/runtime/Cargo.toml", "# reviewed bytes changed\n");
    // Rows whose file sha256 is right but whose text (a trailing space) or line (off by one) is not.
    let at_126 = format!("\t{CONSUMER_FILE}\t126\t");
    let row_text = edited_rows(EDGES_TABLE, |row| {
        row.contains(&at_126).then(|| format!("{row} "))
    });
    let row_line = edited_rows(EDGES_TABLE, |row| {
        row.contains(&at_126)
            .then(|| row.replacen(&at_126, &format!("\t{CONSUMER_FILE}\t127\t"), 1))
    });
    let mut follows_row = fetch_once_record();
    follows_row.calls = vec![format!("{CONSUMER_FILE}:127")];
    let cases: [Case; 9] = [
        ("file sha256 changed", vec![Record::control()], &sha_changed),
        (
            "file sha256 changed, plain call",
            vec![fetch_once_record()],
            &sha_changed,
        ),
        (
            "line text changed",
            vec![fetch_once_record()],
            &text_changed,
        ),
        ("line number moved", vec![Record::control()], &line_moved),
        (
            "carrier file changed",
            vec![fetch_once_record()],
            &carrier_changed,
        ),
        (
            "behaviour test file changed",
            vec![fetch_once_record()],
            &test_changed,
        ),
        (
            "caller manifest changed",
            vec![fetch_once_record()],
            &crate_changed,
        ),
        (
            "row text changed, sha256 right",
            vec![fetch_once_record()],
            &row_text,
        ),
        (
            "row line off by one, sha256 right",
            vec![follows_row],
            &row_line,
        ),
    ];
    let expected: Vec<_> = cases
        .iter()
        .map(|(label, ..)| (*label, Some("ru053-stale-pin")))
        .collect();
    all_refused_keep_58(&cases, &expected);
}

#[test]
fn admission_catalogue_ru053_refuses_unmapped_edge() {
    control_is_admitted();
    admitted_in(&fetch_once_record(), &Workspace);
    // Chain gaps: the :126 row starts at fetch_once, not at the root fetch; the :60 row alone ends at
    // fetch_once, not at verify_chunk_receipt.
    let mut from_gap = Record::control();
    from_gap.calls = vec![format!("{CONSUMER_FILE}:126")];
    let mut to_gap = Record::control();
    to_gap.calls = vec![format!("{CONSUMER_FILE}:60")];
    // A real call of fetch_once that the reviewed table does not list.
    let mut unlisted = fetch_once_record();
    unlisted.calls = vec![format!("{CONSUMER_FILE}:{UNLISTED_CALL_LINE}")];
    let (fc19, fc19_tree) = fc19_generic_dispatch();
    let cases: [Case; 4] = [
        ("chain gap at the root", vec![from_gap], &Workspace),
        ("chain gap before the endpoint", vec![to_gap], &Workspace),
        ("edge not in the reviewed table", vec![unlisted], &Workspace),
        ("fc19 generic trait dispatch", vec![fc19], &fc19_tree),
    ];
    all_refused_keep_58(
        &cases,
        &[
            (
                "chain gap at the root",
                Some("ru053-unconnected-consumer-hop"),
            ),
            (
                "chain gap before the endpoint",
                Some("ru053-unconnected-consumer-hop"),
            ),
            (
                "edge not in the reviewed table",
                Some("ru053-unmapped-edge"),
            ),
            (
                "fc19 generic trait dispatch",
                Some("ru053-unresolved-consumer-hop"),
            ),
        ],
    );
}

#[test]
fn admission_catalogue_ru053_refuses_uncollected_test_target() {
    control_is_admitted();
    // Positive: a spaced `[[ test ]]` table that only names the target leaves collection unchanged.
    let named = appended(
        PROTOCOL_MANIFEST,
        "\n[[ test ]]\nname = \"artifact_refusals\"\n",
    );
    admitted_in(&Record::control(), &named);
    // fc18 and fc23 (rv-logs-catfc): a spaced `[[ test ]]` table with `harness = false`, and one needing a
    // defined but non-default feature; `test = false`; an inner `#![cfg(..)]` attribute heading the file.
    let fc18 = appended(
        PROTOCOL_MANIFEST,
        "\n[[ test ]]\nname = \"artifact_refusals\"\nharness = false\n",
    );
    let fc23 = appended(
        PROTOCOL_MANIFEST,
        "\n[features]\nrv-fc-disabled = []\n[[ test ]]\nname = \"artifact_refusals\"\nrequired-features = [\"rv-fc-disabled\"]\n",
    );
    let not_test = appended(
        PROTOCOL_MANIFEST,
        "\n[[test]]\nname = \"artifact_refusals\"\ntest = false\n",
    );
    let behaviour = Workspace
        .read(BEHAVIOUR_FILE)
        .expect("the behaviour test file");
    let inner_cfg = Patched {
        path: BEHAVIOUR_FILE,
        text: format!("#![cfg(not(test))]\n{behaviour}"),
    };
    let needs_feature = metadata_requiring("rv-fc-disabled");
    // Re-reviewed copies: every pin fresh, so only the collection rule can refuse.
    let (fc18_rereviewed, fc23_rereviewed, not_test_rereviewed, inner_cfg_rereviewed) = (
        repinned(&fc18),
        repinned(&fc23),
        repinned(&not_test),
        repinned(&inner_cfg),
    );
    let control = || vec![Record::control()];
    let cases: [Case; 9] = [
        ("fc18 [[ test ]] harness = false", control(), &fc18),
        ("fc18 re-reviewed", control(), &fc18_rereviewed),
        ("fc23 non-default required-features", control(), &fc23),
        ("fc23 re-reviewed", control(), &fc23_rereviewed),
        ("test = false", control(), &not_test),
        ("test = false re-reviewed", control(), &not_test_rereviewed),
        ("inner #![cfg] file", control(), &inner_cfg),
        (
            "inner #![cfg] file re-reviewed",
            control(),
            &inner_cfg_rereviewed,
        ),
        ("metadata required-features", control(), &needs_feature),
    ];
    let uncollected = Some("ru053-uncollected-test");
    all_refused_keep_58(
        &cases,
        &[
            ("fc18 [[ test ]] harness = false", uncollected),
            ("fc18 re-reviewed", uncollected),
            ("fc23 non-default required-features", uncollected),
            ("fc23 re-reviewed", uncollected),
            ("test = false", uncollected),
            ("test = false re-reviewed", uncollected),
            ("inner #![cfg] file", Some("ru053-stale-pin")),
            ("inner #![cfg] file re-reviewed", uncollected),
            ("metadata required-features", uncollected),
        ],
    );
}

#[test]
fn admission_catalogue_ru053_refuses_duplicate_test_reference() {
    // `A` is the control's reviewed behaviour test; `B` a second reviewed test: the reviewed tests table with one
    // more row, pinned to the current bytes of a real top-level `#[test]` of a collected target in the same
    // crate (a fixture re-review; the checked-in table is unchanged).
    let control = Record::control();
    let a = control.behaviour.as_str();
    let (b, b_file, b_fn) = (
        "protocol::performance_contracts::performance_positive_bytes_and_negative_corpus",
        "crates/protocol/tests/performance_contracts.rs",
        "fn performance_positive_bytes_and_negative_corpus() {",
    );
    let source = Workspace.read(b_file).expect("the second test file");
    let lines: Vec<&str> = source.split('\n').collect();
    let at = (1..lines.len())
        .find(|i| lines[*i] == b_fn && lines[i - 1] == "#[test]")
        .expect("a top-level #[test]")
        + 1;
    let tests = Workspace
        .read(epochs::RU053_TESTS_PATH)
        .expect("the reviewed tests table");
    let two = Over {
        base: &Workspace,
        files: vec![Patched {
            path: epochs::RU053_TESTS_PATH,
            text: format!("{tests}{b}\t{b_file}\t{at}\t{}\t{b_fn}\n", sha(&source)),
        }],
        metadata: None,
    };
    // The same class in `consumer_calls`: a reviewed edges table with a cycle (`fetch_once` back to `fetch` on
    // the :118 line), so a chain can list the :60 row twice and stay contiguous.
    let edges = Workspace
        .read(EDGES_TABLE)
        .expect("the reviewed edges table");
    let consumer = Workspace.read(CONSUMER_FILE).expect("the consumer file");
    let back = consumer
        .split('\n')
        .nth(UNLISTED_CALL_LINE - 1)
        .expect("the :118 line");
    let cyclic = Over {
        base: &Workspace,
        files: vec![Patched {
            path: EDGES_TABLE,
            text: format!(
                "{edges}{CONSUMER_FILE}:ChunkSession::fetch_once\t{CONSUMER_FILE}:ChunkSession::fetch\t{CONSUMER_FILE}\t{UNLISTED_CALL_LINE}\t{}\t{back}\n",
                sha(&consumer)
            ),
        }],
        metadata: None,
    };
    let tests_named = |behaviour: &[&str], parity: &[&str]| {
        let mut record = Record::control();
        record.behaviour = behaviour.join(" | ");
        if !parity.is_empty() {
            record.parity = parity.join(" | ");
        }
        record
    };
    let calls_listed = |lines: &[usize]| {
        let mut record = Record::control();
        let steps: Vec<String> = lines
            .iter()
            .map(|n| format!("{CONSUMER_FILE}:{n}"))
            .collect();
        record.calls = vec![steps.join(",")];
        record
    };
    // Positive controls, each admitted with closure 57: singleton behaviour, singleton parity (`A`, and `B` beside
    // behaviour `A`), the two distinct reviewed tests together, and the unrepeated chain over the cyclic table.
    control_is_admitted();
    admitted_in(&tests_named(&[a], &[a]), &Workspace);
    admitted_in(&tests_named(&[a], &[b]), &two);
    admitted_in(&tests_named(&[a, b], &[]), &two);
    admitted_in(&calls_listed(&[60, 126]), &cyclic);
    // Each list names a test at most once (checked before any pin); a chain lists a reviewed row at most once.
    let cases: [Case; 5] = [
        (
            "behaviour A | A (rvm19)",
            vec![tests_named(&[a, a], &[])],
            &Workspace,
        ),
        (
            "parity A | A (rvm20)",
            vec![tests_named(&[a], &[a, a])],
            &Workspace,
        ),
        (
            "behaviour A | B | A",
            vec![tests_named(&[a, b, a], &[])],
            &two,
        ),
        (
            "behaviour A | A | A",
            vec![tests_named(&[a, a, a], &[])],
            &Workspace,
        ),
        (
            "consumer_calls :60,:118,:60,:126 over the cyclic table",
            vec![calls_listed(&[60, UNLISTED_CALL_LINE, 60, 126])],
            &cyclic,
        ),
    ];
    let expected: Vec<_> = cases
        .iter()
        .map(|(label, ..)| (*label, Some("ru053-duplicate-row")))
        .collect();
    all_refused_keep_58(&cases, &expected);
}
