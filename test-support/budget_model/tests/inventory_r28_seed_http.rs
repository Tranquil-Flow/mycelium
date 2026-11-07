// SPDX-License-Identifier: AGPL-3.0-only
//! Authenticated absolute expiry, distinct from the bounded convenience minter.
#[path = "common/oracle_blobs.rs"]
#[allow(dead_code, reason = "shared read-only oracle helper")]
mod oracle_blobs;
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
use budget_model::inventory::{adjudication as adj, candidates};
use std::collections::{BTreeMap, BTreeSet};
const KEY: &str = "mycelium_seed/http.py:968:8:assignment:expires";
const ID: &str = "PySourceUnboundedR28SeedHttpExpiryL968C8";
#[test]
fn exact_authenticated_seed_expiry_requires_its_own_non_numeric_review() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let e = p
        .entries
        .iter()
        .find(|e| e.key == KEY)
        .expect("r28 HTTP968 requires independent authenticated-domain proof");
    assert_eq!(e.verdict, "source-unbounded");
    assert_eq!(e.target, ID);
    let row = adj::row_of(&p, e, &cs, &adj::registry).unwrap();
    assert_eq!(row.unit, "not-applicable");
    assert!(row.value.is_none() && row.budget.is_empty());
    assert_eq!(cs[KEY].scope, "SeedHTTPClient._verify_seed_envelope");
    assert_eq!(cs[KEY].expression, "statement.get('expires_at')");
    assert_eq!(cs[KEY].symbol, "expires");
}
#[test]
fn full_verifier_producer_callers_diagnostics_and_occurrences_are_pinned() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let proof: String = adj::TABLES
        .proofs_b
        .lines()
        .filter(|l| l.split('\t').next() == Some(KEY))
        .map(|l| format!("{l}\n"))
        .collect();
    assert_eq!(oracle_blobs::sha256_hex(proof.as_bytes()), PROOF_HASH);
    oracle_blobs::oracle_dir_or_panic();
    let src = oracle_blobs::blobs(
        &SOURCE_PINS
            .iter()
            .map(|(p, _)| p.to_string())
            .collect::<Vec<_>>(),
    );
    for (path, hash) in SOURCE_PINS {
        assert_eq!(oracle_blobs::sha256_hex(src[path].as_bytes()), hash);
    }
    for h in &p.proofs[KEY] {
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
            let expected: BTreeSet<_> = p.proofs[KEY]
                .iter()
                .filter(|r| r.kind == "occurrence" && r.path == h.path && r.needle == h.needle)
                .map(|r| r.line)
                .collect();
            assert_eq!(found, expected);
            assert_eq!(found.len(), h.line);
        }
    }
    let http = &src["mycelium_seed/http.py"];
    assert_eq!(
        http.lines()
            .filter(|l| l.contains("self._verify_seed_envelope("))
            .count(),
        2
    );
    assert!(http.contains("or float(now) > float(expires)"));
    assert!(http.contains("or float(now) + MAX_CLOCK_SKEW_SECONDS < float(issued)"));
    assert!(http.contains("if receipt[\"accepted_message_id\"] != expected_message_id:"));
}
#[test]
fn wrong_registry_origin_missing_proof_and_forged_admission_are_refused() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let cs: BTreeMap<_, _> = candidates()
        .unwrap()
        .into_iter()
        .map(|c| (c.key.clone(), c))
        .collect();
    let e = p.entries.iter().find(|e| e.key == KEY).unwrap();
    for name in [
        "CancelCleanup",
        "PySourceUnboundedR26SeedRequestExpiryL1310C20",
    ] {
        assert!(adj::row_of(&p, e, &cs, &|_| adj::registry(name)).is_err());
    }
    let mut view = adj::registry(ID).unwrap();
    view.blocked = Some("wrong origin".into());
    assert!(adj::row_of(&p, e, &cs, &|_| Some(view.clone())).is_err());
    for role in ["origin", "read"] {
        let mut bad = adj::parse(&adj::TABLES).unwrap();
        bad.proofs
            .get_mut(KEY)
            .unwrap()
            .retain(|h| h.verdict != role);
        assert!(
            adj::row_of(&bad, e, &cs, &adj::registry)
                .unwrap_err()
                .contains(&format!("unbounded-proof-{role}"))
        );
    }
    let mut bad = adj::parse(&adj::TABLES).unwrap();
    bad.proofs
        .get_mut(KEY)
        .unwrap()
        .retain(|h| h.kind != "scan");
    assert!(
        adj::row_of(&bad, e, &cs, &adj::registry)
            .unwrap_err()
            .contains("unbounded-proof-scan-missing")
    );
    let occurrence = adj::TABLES
        .proofs_b
        .lines()
        .find(|l| l.starts_with(&format!("{KEY}\toccurrence\t")))
        .unwrap();
    let changed = adj::TABLES.proofs_b.replace(
        occurrence,
        &format!("{}\tceiling", occurrence.rsplit_once('\t').unwrap().0),
    );
    assert!(
        adj::parse(&adj::Tables {
            proofs_b: &changed,
            ..adj::TABLES
        })
        .is_err()
    );
    for field in [
        "key",
        "target",
        "rationale",
        "citations",
        "reviewer",
        "verdict",
    ] {
        let mut bad = e.clone();
        match field {
            "key" => bad.key.push('x'),
            "target" => bad.target.push('x'),
            "rationale" => bad.rationale.push('x'),
            "citations" => bad.citations.push('x'),
            "reviewer" => bad.reviewer.push('x'),
            _ => bad.verdict = "derived".into(),
        }
        assert!(!r28_seed_http_admission::exact(&bad), "{field}");
    }
}
#[test]
fn exact_one_key_delta_keeps_other_four_and_all_old_pending() {
    let p = adj::parse(&adj::TABLES).unwrap();
    let own = r28_seed_http_admission::admitted(&p);
    assert_eq!(own, BTreeSet::from([KEY]));
    let prior = include_str!("../data/python-r28-prior-pending.tsv");
    assert_eq!(oracle_blobs::sha256_hex(prior.as_bytes()), PRIOR_HASH);
    let before: BTreeSet<_> = prior.lines().skip(1).map(str::to_string).collect();
    assert_eq!(before.len(), 70);
    let mut all = budget_model::inventory::candidate_keys().unwrap();
    let mut reviewed: BTreeSet<_> = budget_model::inventory::all_reviewed()
        .unwrap()
        .into_iter()
        .map(|r| r.key)
        .collect();
    // Historical pre-R31 view; live closure is independently pinned by inventory_r31_seed_remaining.
    let r31 = r31_seed_admission::admitted(
        &budget_model::inventory::adjudication::parse(
            &budget_model::inventory::adjudication::TABLES,
        )
        .unwrap(),
    );
    for key in r31 {
        assert!(reviewed.remove(key));
    }
    r32_verifier_admission::remove_from(&mut all, &mut reviewed);
    r36_stats_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::remove_from_reviewed(&mut reviewed);
    r50_recovery_forward_admission::lane_a::remove_from_reviewed(&mut reviewed);
    let after: BTreeSet<_> = all.difference(&reviewed).cloned().collect();
    let own = own.into_iter().map(str::to_string).collect();
    assert_eq!(after, before.difference(&own).cloned().collect());
    assert_eq!(
        (all.len(), reviewed.len(), after.len()),
        (
            8256 + r29_retry_admission::keys().len() + r30_workflow_admission::keys().len(),
            8187 + r29_retry_admission::keys().len() + r30_workflow_admission::keys().len(),
            69
        )
    );
    for key in [
        "mycelium_seed/operator.py:641:19:comparison:member['lease_expires_at']",
        "mycelium_seed/plan_binding.py:105:12:keyword:expires_at",
        "mycelium_seed/state.py:827:16:dict-field:lease_expires_at",
        "mycelium_seed/state.py:830:16:dict-field:next_heartbeat_due_at",
    ] {
        assert!(after.contains(key));
    }
}
#[test]
fn real_signed_canonical_oracle_distinguishes_horizon_freshness_and_replay() {
    let fixture = include_str!("../data/fx-l1b-r28-seed-http.tsv");
    assert_eq!(oracle_blobs::sha256_hex(fixture.as_bytes()), FIXTURE_HASH);
    let rows: BTreeMap<_, _> = fixture
        .lines()
        .skip(1)
        .map(|l| {
            let f: Vec<_> = l.split('\t').collect();
            (f[0], f)
        })
        .collect();
    assert_eq!(rows.len(), 25);
    for (name, outcome, value, raw) in CASES {
        assert_eq!(&rows[*name][1..4], &[*outcome, *value, *raw], "{name}");
    }
    assert_eq!(CASES.len(), rows.len());
}
// Immutable independently executed oracle values and literal source pins follow.
const PROOF_HASH: &str = "3ef0de528f1150fe8ce54b8ef48c0f967b7c27247fb5a61e61cb4aae63075b4a";
const PRIOR_HASH: &str = "073a7bc19c2f857b6251b42a6885021b468f541d69b7f3bbe93cd523974eac18";
const FIXTURE_HASH: &str = "a1f77ca1faa8776a5a9091e14ddf1a685238b63a8dac935e6e9bcd57a34382a9";
const SOURCE_PINS: [(&str, &str); 2] = [
    (
        "mycelium_seed/http.py",
        "3e9100411634e2bd7f68fc45a90a3e55fb17b2a297f69b6b8f319592ae0dcc8b",
    ),
    (
        "mycelium_seed/coordinator.py",
        "2612a51acce2d43626ec69cd7dc19db080e595915c82030d3ec8d5f7a4456f15",
    ),
];
const CASES: &[(&str, &str, &str, &str)] = &[
    ("ordinary", "accepted", "1060.0", "1060.0"),
    ("large-horizon", "accepted", "1e+100", "1e+100"),
    (
        "large-epoch",
        "accepted",
        "1000000000060.0",
        "1000000000060.0",
    ),
    (
        "negative-epoch",
        "accepted",
        "-999999999940.0",
        "-999999999940.0",
    ),
    ("expiry-equals-now", "accepted", "1000.0", "1000.0"),
    ("expired", "refused", "seed_http_seed_time_invalid", "999.0"),
    ("skew-edge", "accepted", "1060.0", "1060.0"),
    (
        "skew-over",
        "refused",
        "seed_http_seed_time_invalid",
        "1060.0",
    ),
    ("expiry-before-issued", "accepted", "1010.0", "1010.0"),
    (
        "signed-string",
        "refused",
        "seed_http_seed_time_invalid",
        "'1060'",
    ),
    (
        "signed-bool",
        "refused",
        "seed_http_seed_time_invalid",
        "True",
    ),
    (
        "signed-null",
        "refused",
        "seed_http_seed_time_invalid",
        "None",
    ),
    (
        "signed-overflow",
        "refused",
        "int too large to convert to float",
        "10000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000",
    ),
    (
        "nonfinite-infinity",
        "signer-or-wire-refused",
        "invalid_signed_statement: noncanonical_json",
        "",
    ),
    (
        "nonfinite-nan",
        "signer-or-wire-refused",
        "invalid_signed_statement: noncanonical_json",
        "",
    ),
    (
        "bad-signature",
        "refused",
        "seed_http_seed_signature_invalid",
        "",
    ),
    ("wrong-key", "refused", "seed_http_seed_pin_mismatch", ""),
    ("wrong-swarm", "refused", "seed_http_seed_pin_mismatch", ""),
    ("receipt-large", "accepted", "1e+100", "1e+100"),
    ("receipt-replay", "accepted", "1060.0", "1060.0"),
    ("receipt-replay-again", "accepted", "1060.0", "1060.0"),
    ("receipt-replay-at-expiry", "accepted", "1060.0", "1060.0"),
    (
        "receipt-replay-after-expiry",
        "refused",
        "seed_http_seed_time_invalid",
        "1060.0",
    ),
    ("real-coordinator-identity", "accepted", "1060.0", "1060.0"),
    ("real-coordinator-receipt", "accepted", "1060.0", "1060.0"),
];
