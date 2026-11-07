// SPDX-License-Identifier: AGPL-3.0-only
//! One original invite expiry, not a member lease or seed HTTP envelope.
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
use budget_model::inventory::{adjudication as adj, candidates};
use std::collections::{BTreeMap, BTreeSet};
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
#[path = "common/r23_seed_invite_admission.rs"]
mod r23_seed_invite_admission;
#[path = "common/r24_member_lease_admission.rs"]
mod r24_member_lease_admission;
#[path = "common/r26_seed_inputs_admission.rs"]
mod r26_seed_inputs_admission;
#[path = "common/r28_seed_http_admission.rs"]
mod r28_seed_http_admission;
#[path = "common/r31_seed_admission.rs"]
mod r31_seed_admission;
const KEY: &str = "mycelium_seed/coordinator.py:690:20:keyword:invite_expires_at";
const ID: &str = "PySourceUnboundedR23SeedInviteExpiryL690C20";
const PRIOR_PENDING: &str = include_str!("../data/python-r23-prior-pending.tsv");
const FIXTURE: &str = include_str!("../data/fx-l1b-r23-seed-domain.tsv");
const SOURCE_PINS: [(&str, &str); 5] = [
    (
        "mycelium_invite/bundle.py",
        "8385d7f45fc6e4e5ce3ff17aede81c90abe34c8a6600c65b6022bb4bcb9650dd",
    ),
    (
        "mycelium_invite/sqlite_registry.py",
        "721a13f1cbb4c16e7e034bef4a2dc4ab4ca7a8feab3babf098af3baa213793f0",
    ),
    (
        "mycelium_invite/token.py",
        "0da5ecde07e4c4539db7c9f98b98c269dff1b210ff927180fedc6b8d051ea7bb",
    ),
    (
        "mycelium_seed/coordinator.py",
        "2612a51acce2d43626ec69cd7dc19db080e595915c82030d3ec8d5f7a4456f15",
    ),
    (
        "mycelium_seed/state.py",
        "2d1087ab257241bcc2a130092becbe0ae4748f0df1b89d511c92b2d18e706af5",
    ),
];
fn census() -> BTreeMap<String, budget_model::inventory::Candidate> {
    candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect()
}
#[test]
fn exact_invite_expiry_has_its_own_source_unbounded_contract() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let e = p
        .entries
        .iter()
        .find(|e| e.key == KEY)
        .expect("r23 original invite expiry needs its own proven contract");
    assert_eq!(e.verdict, "source-unbounded");
    assert_eq!(e.target, ID);
    let row = adj::row_of(&p, e, &census(), &adj::registry).unwrap();
    assert_eq!(row.disposition, "source-unbounded");
    assert_eq!(row.relation, ID);
    assert_eq!(row.unit, "not-applicable");
    assert!(row.value.is_none());
    assert!(row.budget.is_empty());
    assert_eq!(
        r23_seed_invite_admission::admitted(&p),
        BTreeSet::from([KEY])
    );
}
#[test]
fn exact_prior_gap_preserves_invite_cohort_and_adds_only_r24_member_keys() {
    use budget_model::inventory::all_reviewed;
    assert_eq!(
        oracle_blobs::sha256_hex(PRIOR_PENDING.as_bytes()),
        "a35defbd06630ef7bd739dbac7d943ceddba258b52846f7a1de7b3017db71d45"
    );
    let before: BTreeSet<_> = PRIOR_PENDING.lines().skip(1).map(str::to_string).collect();
    assert_eq!(before.len(), 76);
    assert!(before.contains(KEY));
    let mut all: BTreeSet<_> = census().into_keys().collect();
    let mut reviewed: BTreeSet<_> = all_reviewed().unwrap().into_iter().map(|r| r.key).collect();
    r25_count_admission::remove_from(&mut all, &mut reviewed);
    r27_delivery_wake_admission::remove_from(&mut all, &mut reviewed);
    r30_workflow_admission::remove_from(&mut all, &mut reviewed);
    r32_verifier_admission::remove_from(&mut all, &mut reviewed);
    r36_stats_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::lane_a::remove_from_reviewed(&mut reviewed);
    r29_retry_admission::remove_from(&mut all, &mut reviewed);
    let after: BTreeSet<_> = all.difference(&reviewed).cloned().collect();
    let p = adj::parse(&adj::TABLES).unwrap();
    let r24 = r24_member_lease_admission::admitted(&p);
    assert!(!r24.contains(KEY));
    let mut admitted: BTreeSet<_> = r24.into_iter().map(str::to_string).collect();
    let mut r26 = r26_seed_inputs_admission::admitted(&p);
    let r28 = r28_seed_http_admission::admitted(&p);
    assert!(r26.is_disjoint(&r28));
    r26.extend(r28);
    let r31 = r31_seed_admission::admitted(&p);
    assert!(r26.is_disjoint(&r31));
    r26.extend(r31);
    assert!(admitted.iter().all(|k| !r26.contains(k.as_str())));
    admitted.extend(r26.iter().map(|k| k.to_string()));
    admitted.insert(KEY.to_string());
    let expected: BTreeSet<_> = before.difference(&admitted).cloned().collect();
    assert_eq!(after, expected);
    assert_eq!(all.len(), 8252);
    assert_eq!(reviewed.len(), 8180 + r26.len());
    assert_eq!(after.len(), 72 - r26.len());
    for key in [
        "mycelium_seed/coordinator.py:1310:20:assignment:request_expiry",
        "mycelium_seed/coordinator.py:1385:20:keyword:previous_deadline",
    ] {
        assert!(
            r26.contains(key) && !after.contains(key),
            "unproven promotion: {key}"
        );
    }
}
#[test]
fn complete_exact_original_proof_and_read_only_source_pins() {
    let proof: String = adj::TABLES
        .proofs_b
        .lines()
        .filter(|l| l.split('\t').next() == Some(KEY))
        .map(|l| format!("{l}\n"))
        .collect();
    assert_eq!(
        oracle_blobs::sha256_hex(proof.as_bytes()),
        "317a7cf2d8f07388afd6db1bef3c02d15efcd2d9e743eb568d5b111e03a5f596"
    );
    oracle_blobs::oracle_dir_or_panic();
    let paths: Vec<_> = SOURCE_PINS.iter().map(|(p, _)| (*p).to_string()).collect();
    let src = oracle_blobs::blobs(&paths);
    for (path, expected) in SOURCE_PINS {
        assert_eq!(
            oracle_blobs::sha256_hex(src[path].as_bytes()),
            expected,
            "{path}"
        );
    }
    let p = adj::parse(&adj::TABLES).unwrap();
    for hop in &p.proofs[KEY] {
        if hop.kind != "scan" {
            let line = oracle_blobs::line_of(&src[&hop.path], hop.line).unwrap();
            assert_eq!(
                oracle_blobs::sha256_hex(line.as_bytes()),
                p.pins[&(hop.path.clone(), hop.line)]
            );
        }
    }
    assert_eq!(
        oracle_blobs::line_of(&src["mycelium_seed/coordinator.py"], 690)
            .unwrap()
            .trim(),
        "invite_expires_at=float(invite[\"expires_at\"]),"
    );
    assert_eq!(
        oracle_blobs::line_of(&src["mycelium_invite/token.py"], 111)
            .unwrap()
            .trim(),
        "\"expires_at\": issued + ttl_seconds,"
    );
}
#[test]
fn missing_origin_numeric_target_and_invented_ceiling_are_refused() {
    let mut p = adj::parse(&adj::TABLES).unwrap();
    let e = p.entries.iter().find(|e| e.key == KEY).unwrap().clone();
    assert!(adj::row_of(&p, &e, &census(), &|_| adj::registry("CancelCleanup")).is_err());
    p.proofs
        .get_mut(KEY)
        .unwrap()
        .retain(|h| h.verdict != "origin");
    assert!(
        adj::row_of(&p, &e, &census(), &adj::registry)
            .unwrap_err()
            .contains("unbounded-proof-origin")
    );
    let before = format!("{KEY}\toccurrence\tmycelium_invite/token.py\t173\texpires_at\trelative");
    let after = before.replace("\trelative", "\tceiling");
    assert_eq!(adj::TABLES.proofs_b.matches(&before).count(), 1);
    let proofs = adj::TABLES.proofs_b.replace(&before, &after);
    let tables = adj::Tables {
        proofs_b: &proofs,
        ..adj::TABLES
    };
    assert!(adj::parse(&tables).is_err());
}
#[test]
fn captured_full_seed_join_controls_keep_invite_and_member_clocks_separate() {
    // Projection of two byte-identical external oracle captures, not an emulation.
    assert_eq!(
        oracle_blobs::sha256_hex(FIXTURE.as_bytes()),
        "18db72176fc1557c818f7e5b06d01542896da8e787a36ac2114156f704a929d8"
    );
    let rows: Vec<_> = FIXTURE
        .lines()
        .skip(1)
        .map(|l| l.split('\t').collect::<Vec<_>>())
        .collect();
    assert_eq!(rows.len(), 17);
    assert!(rows.iter().all(|r| r.len() == 4));
    let huge = rows.iter().find(|r| r[0] == "6:full_join_invite").unwrap();
    assert_eq!(huge[2], "accepted");
    assert!(huge[1].contains("\"ttl_seconds\":1000000000000"));
    assert!(huge[3].contains("\"persisted_invite_expiry\":1000000001000.0"));
    assert!(huge[3].contains("\"lease_expiry\":1300.0"));
    assert!(huge[3].contains("\"exact_retry\":true"));
    assert!(huge[3].contains("\"pruned_at_now\":0"));
    assert!(huge[3].contains("\"pruned_at_expiry\":0"));
    assert!(huge[3].contains("\"pruned_after_expiry\":1"));
    for (name, code) in [
        (
            "8:full_join_invite_wrong-protocol",
            "invite_protocol_invalid",
        ),
        ("9:full_join_invite_wrong-key", "invite_signature_invalid"),
        ("10:full_join_invite_expired", "invite_expired"),
    ] {
        let row = rows.iter().find(|r| r[0] == name).unwrap();
        assert_eq!(row[2], "refused");
        assert_eq!(row[3], format!("\"{code}\""));
    }
    let future = rows
        .iter()
        .find(|r| r[0] == "11:full_join_invite_future-issued")
        .unwrap();
    assert_eq!(future[2], "accepted");
    assert!(future[3].contains("\"persisted_invite_expiry\":2000000000000.0"));
    let equal = rows
        .iter()
        .find(|r| r[0] == "12:full_join_invite_equal-now")
        .unwrap();
    assert_eq!(equal[2], "accepted");
    assert!(equal[3].contains("\"persisted_invite_expiry\":1000.0"));
}
