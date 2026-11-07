// SPDX-License-Identifier: AGPL-3.0-only
//! Raw heartbeat expiry and stored previous deadline are not helper return values.
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
use budget_model::inventory::{adjudication as adj, candidates};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
#[path = "common/r26_seed_inputs_admission.rs"]
mod r26_seed_inputs_admission;
#[path = "common/r28_seed_http_admission.rs"]
mod r28_seed_http_admission;
#[path = "common/r31_seed_admission.rs"]
mod r31_seed_admission;
const KEYS: [&str; 2] = [
    "mycelium_seed/coordinator.py:1310:20:assignment:request_expiry",
    "mycelium_seed/coordinator.py:1385:20:keyword:previous_deadline",
];
#[test]
fn exact_seed_input_domains_have_independent_non_numeric_reviews() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for key in KEYS {
        let e = p
            .entries
            .iter()
            .find(|e| e.key == key)
            .expect("r26 exact raw input needs independent source-domain proof");
        assert_eq!(e.verdict, "source-unbounded");
        let r = adj::row_of(&p, e, &cs, &adj::registry).unwrap();
        assert_eq!(r.unit, "not-applicable");
        assert!(r.value.is_none());
        assert!(r.budget.is_empty());
    }
}

const IDS: [&str; 2] = [
    "PySourceUnboundedR26SeedRequestExpiryL1310C20",
    "PySourceUnboundedR26SeedPreviousDeadlineL1385C20",
];
const PROOF_HASHES: [&str; 2] = [
    "a6719fe779befc722403a7b44d0f83021da1283cfe599dbc708708c3d1c4b99b",
    "3c0d65e8ef800812710d644c1857edb25d09be881cdfd21cfcfc96a74724bd5a",
];
const SOURCE_PINS: [(&str, &str); 4] = [
    (
        "mycelium_membership/contracts.py",
        "df93138166c4d08fd51c5cedb260e50536b3b3343840caeaab9a1a008d018ab2",
    ),
    (
        "mycelium_seed/coordinator.py",
        "2612a51acce2d43626ec69cd7dc19db080e595915c82030d3ec8d5f7a4456f15",
    ),
    (
        "mycelium_seed/placement.py",
        "be0512753894566e18d98a0664b71b2e04f7e8bebab14e99ef5cbf175769a478",
    ),
    (
        "mycelium_seed/state.py",
        "2d1087ab257241bcc2a130092becbe0ae4748f0df1b89d511c92b2d18e706af5",
    ),
];
#[test]
fn complete_raw_validator_store_replay_and_consumer_source_is_pinned() {
    let p = adj::parse(&adj::TABLES).unwrap();
    for (key, expected) in KEYS.into_iter().zip(PROOF_HASHES) {
        let proof: String = adj::TABLES
            .proofs_b
            .lines()
            .filter(|l| l.split('\t').next() == Some(key))
            .map(|l| format!("{l}\n"))
            .collect();
        assert_eq!(oracle_blobs::sha256_hex(proof.as_bytes()), expected);
    }
    oracle_blobs::oracle_dir_or_panic();
    let paths: Vec<_> = SOURCE_PINS.iter().map(|(p, _)| p.to_string()).collect();
    let src = oracle_blobs::blobs(&paths);
    for (path, expected) in SOURCE_PINS {
        assert_eq!(oracle_blobs::sha256_hex(src[path].as_bytes()), expected);
    }
    for key in KEYS {
        for h in &p.proofs[key] {
            if h.kind != "scan" {
                assert_eq!(
                    oracle_blobs::sha256_hex(
                        oracle_blobs::line_of(&src[&h.path], h.line)
                            .unwrap()
                            .as_bytes()
                    ),
                    p.pins[&(h.path.clone(), h.line)]
                );
            } else {
                let found: BTreeSet<_> = src[&h.path]
                    .lines()
                    .enumerate()
                    .filter(|(_, l)| l.contains(&h.needle))
                    .map(|(i, _)| i + 1)
                    .collect();
                let expected: BTreeSet<_> = p.proofs[key]
                    .iter()
                    .filter(|r| r.kind == "occurrence" && r.path == h.path && r.needle == h.needle)
                    .map(|r| r.line)
                    .collect();
                assert_eq!(found, expected);
                assert_eq!(found.len(), h.line);
            }
        }
    }
}
#[test]
fn wrong_id_unit_value_origin_missing_producer_and_ceiling_are_refused() {
    let cs: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for key in KEYS {
        let p = adj::parse(&adj::TABLES).unwrap();
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        assert!(
            adj::row_of(&p, e, &cs, &|_| adj::registry(
                "PySourceUnboundedR23SeedInviteExpiryL690C20"
            ))
            .is_err()
        );
        // Use real registry views: a numeric entry cannot also carry Blocked provenance.
        // Impossible synthetic views are outside registry()'s invariant, not source input.
        assert!(adj::row_of(&p, e, &cs, &|_| adj::registry("CancelCleanup")).is_err());
        let mut view = adj::registry(&e.target).unwrap();
        view.blocked = Some("wrong origin".to_string());
        assert!(adj::row_of(&p, e, &cs, &|_| Some(view.clone())).is_err());
        let mut p = adj::parse(&adj::TABLES).unwrap();
        let e = p.entries.iter().find(|e| e.key == key).unwrap().clone();
        p.proofs
            .get_mut(key)
            .unwrap()
            .retain(|r| r.verdict != "origin");
        assert!(
            adj::row_of(&p, &e, &cs, &adj::registry)
                .unwrap_err()
                .contains("unbounded-proof-origin")
        );
        let occurrence = adj::TABLES
            .proofs_b
            .lines()
            .find(|line| {
                let f: Vec<_> = line.split('\t').collect();
                f[0] == key && f[1] == "occurrence"
            })
            .unwrap();
        let old = occurrence.to_string();
        assert_eq!(adj::TABLES.proofs_b.matches(&old).count(), 1);
        let proofs = adj::TABLES.proofs_b.replace(
            &old,
            &format!("{}\tceiling", old.rsplit_once('\t').unwrap().0),
        );
        assert!(
            adj::parse(&adj::Tables {
                proofs_b: &proofs,
                ..adj::TABLES
            })
            .is_err()
        );
    }
}

#[test]
fn exact_original_candidates_metadata_and_prior_union_are_preserved() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let own = r26_seed_inputs_admission::admitted(&p);
    assert_eq!(own, BTreeSet::from(KEYS));
    let cs: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    for (i, key) in KEYS.into_iter().enumerate() {
        let c = &cs[key];
        assert_eq!(c.scope, "SeedCoordinator._receive_member_message");
        assert_eq!(
            c.expression,
            [
                "float(untrusted_message['expires_at'])",
                "member.lease_expires_at"
            ][i]
        );
        assert_eq!(c.symbol, ["request_expiry", "previous_deadline"][i]);
        let e = p.entries.iter().find(|e| e.key == key).unwrap();
        assert_eq!(e.target, IDS[i]);
        for field in [
            "key",
            "rationale",
            "citations",
            "reviewer",
            "target",
            "verdict",
        ] {
            let mut bad = e.clone();
            match field {
                "key" => bad.key = KEYS[1 - i].to_string(),
                "rationale" => bad.rationale.push_str(" forged"),
                "citations" => bad.citations.push_str(" forged"),
                "reviewer" => bad.reviewer.push_str(" forged"),
                "target" => bad.target = IDS[1 - i].to_string(),
                _ => bad.verdict = "derived".to_string(),
            };
            assert!(!r26_seed_inputs_admission::exact(&bad), "{field}: {key}");
        }
    }
    let prior = include_str!("../data/python-r26-prior-pending.tsv");
    assert_eq!(oracle_blobs::sha256_hex(prior.as_bytes()), PRIOR_HASH);
    let before: BTreeSet<_> = prior.lines().skip(1).map(str::to_string).collect();
    assert_eq!(before.len(), 72);
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
    let after: BTreeSet<_> = all.difference(&reviewed).cloned().collect();
    let mut own: BTreeSet<_> = own.into_iter().map(str::to_string).collect();
    let mut r28 = r28_seed_http_admission::admitted(&p);
    let r31 = r31_seed_admission::admitted(&p);
    assert!(r28.is_disjoint(&r31));
    r28.extend(r31);
    assert!(own.iter().all(|k| !r28.contains(k.as_str())));
    own.extend(r28.iter().map(|k| k.to_string()));
    assert_eq!(after, before.difference(&own).cloned().collect());
    let added = r27_delivery_wake_admission::keys().len()
        + r29_retry_admission::keys().len()
        + r30_workflow_admission::keys().len();
    assert_eq!(
        (all.len(), reviewed.len(), after.len()),
        (8255 + added, 8185 + added + r28.len(), 70 - r28.len())
    );
}
#[test]
fn recorded_real_oracle_keeps_raw_accepted_effective_and_replay_domains_separate() {
    let fixture = include_str!("../data/fx-l1b-r26-seed-inputs.tsv");
    assert_eq!(oracle_blobs::sha256_hex(fixture.as_bytes()), FIXTURE_HASH);
    let rows: BTreeMap<_, _> = fixture
        .lines()
        .skip(1)
        .map(|l| {
            let f: Vec<_> = l.split('\t').collect();
            (f[0], f)
        })
        .collect();
    assert_eq!(rows.len(), 26);
    for (name, outcome, result, raw, previous) in [
        ("fresh", "accepted", "1400.0", "1160.0", "1300.0"),
        ("ttl-edge", "accepted", "1400.0", "4700.0", "1300.0"),
        (
            "ttl-over",
            "refused",
            "membership_ttl_invalid",
            "4700.0001",
            "",
        ),
        (
            "raw-large",
            "refused",
            "membership_ttl_invalid",
            "1e+100",
            "",
        ),
        (
            "raw-negative",
            "refused",
            "membership_time_invalid",
            "-1e+100",
            "",
        ),
        (
            "raw-infinity",
            "refused",
            "membership_time_invalid",
            "inf",
            "",
        ),
        ("raw-nan", "refused", "membership_time_invalid", "nan", ""),
        (
            "raw-string",
            "refused",
            "membership_time_invalid",
            "1160.0",
            "",
        ),
        ("raw-bool", "refused", "membership_time_invalid", "1.0", ""),
        ("raw-null", "refused", "membership_time_invalid", "", ""),
        (
            "raw-overflow",
            "refused",
            "int too large to convert to float",
            "",
            "",
        ),
        ("future-edge", "accepted", "1400.0", "1190.0", "1300.0"),
        (
            "future-over",
            "refused",
            "membership_message_from_future",
            "1190.0",
            "",
        ),
        (
            "expired-fresh",
            "refused",
            "membership_message_expired",
            "1150.0",
            "",
        ),
        ("expiry-equal-now", "accepted", "1400.0", "1100.0", "1300.0"),
        (
            "large-valid-domain",
            "accepted",
            "1000000000300.0",
            "1000000000060.0",
            "1000000000100.0",
        ),
        (
            "negative-valid-domain",
            "accepted",
            "-999999999700.0",
            "-999999999940.0",
            "-999999999900.0",
        ),
        (
            "bad-signature",
            "refused",
            "membership_signature_invalid",
            "1160.0",
            "",
        ),
        (
            "wrong-key",
            "refused",
            "membership_key_pin_mismatch",
            "1160.0",
            "",
        ),
        (
            "previous-large",
            "refused",
            "seed_deadline_unavailable",
            "1160.0",
            "1e+100",
        ),
        (
            "previous-at-ceiling",
            "refused",
            "seed_deadline_unavailable",
            "1160.0",
            "4700.0",
        ),
        (
            "previous-before-ceiling",
            "accepted",
            "4700.0",
            "1160.0",
            "4699.999999999999",
        ),
        (
            "previous-expired",
            "refused",
            "seed_member_lease_expired",
            "1160.0",
            "",
        ),
        (
            "replay-old-request",
            "accepted",
            "1400.0",
            "1160.0,1160.0",
            "1300.0",
        ),
        (
            "replay-large-previous",
            "accepted",
            "1e+100",
            "1160.0,1160.0",
            "1300.0",
        ),
        (
            "replay-expired-renewal",
            "refused",
            "membership_message_expired",
            "1160.0,1160.0",
            "1300.0",
        ),
    ] {
        assert_eq!(
            &rows[name][1..5],
            &[outcome, result, raw, previous],
            "{name}"
        );
    }
}
const PRIOR_HASH: &str = "ebd85ba02d04005ab4afdd8768dd60cb41b52a3414690676305e8aae3324a2eb";
const FIXTURE_HASH: &str = "5ba8c351457043c290bcefa082b86c1b893cd47d645f402e8826462799b07353";
