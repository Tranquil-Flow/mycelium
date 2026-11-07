// SPDX-License-Identifier: AGPL-3.0-only
//! L1b round 6 R1 review: `bench_entry_token_path.py` forwards `args.hop_ms` / `args.control_ms` to
//! `trial(...)`. Both options have reviewed numeric defaults and a reviewed `0..100` upper guard; the
//! existing `argparse-option` binding family (exact Namespace receiver, unique option, no stores)
//! maps each forwarding keyword to its option default as a derived edge (no value is promoted).
#[path = "common/r32_verifier_admission.rs"]
#[allow(
    dead_code,
    reason = "exact four-discovery addition to the bounds-only gap"
)]
mod r32_verifier_admission;

#[path = "common/r29_retry_admission.rs"]
#[allow(
    dead_code,
    reason = "exact additive manual review outside bounds-only count"
)]
mod r29_retry_admission;
use budget_model::inventory::{Bound, candidate_keys, checked_in, validate};

/// (source key, option default target registry id, unit)
const R1: &[(&str, &str, &str)] = &[
    (
        "scripts/bench_entry_token_path.py:95:63:keyword:hop_ms",
        "PyCliScriptsBenchEntryTokenPathL81C48",
        "milliseconds",
    ),
    (
        "scripts/bench_entry_token_path.py:96:30:keyword:control_ms",
        "PyCliScriptsBenchEntryTokenPathL82C52",
        "milliseconds",
    ),
];

fn rows() -> Vec<Bound> {
    checked_in().unwrap()
}

#[test]
fn argparse_forwarding_keywords_are_reviewed_as_derived_edges_to_the_option_defaults() {
    let rows = rows();
    for (key, id, unit) in R1 {
        let row = rows
            .iter()
            .find(|r| r.key == *key)
            .unwrap_or_else(|| panic!("{key} must be reviewed"));
        assert_eq!(row.disposition, "derived", "{key}");
        assert!(
            row.value.is_none(),
            "{key}: a derived edge carries no value"
        );
        assert_eq!(row.relation, *id, "{key}");
        assert_eq!(row.unit, *unit, "{key}");
        assert!(
            row.review.starts_with("binding-category:argparse-option;"),
            "{key}"
        );
    }
    validate(&rows).unwrap();
}

#[test]
fn wrong_target_wrong_unit_and_value_promotion_are_refused() {
    let base = rows();
    for (key, _, _) in R1 {
        let i = base.iter().position(|r| r.key == *key).unwrap();
        let mut bad = base.clone();
        bad[i].relation = "PyCliScriptsBenchEntryTokenPathL81C48".into();
        if base[i].relation == "PyCliScriptsBenchEntryTokenPathL81C48" {
            bad[i].relation = "PyCliScriptsBenchEntryTokenPathL82C52".into();
        }
        assert!(validate(&bad).is_err(), "{key}: swapped option target");
        let mut bad = base.clone();
        bad[i].unit = "bytes".into();
        assert!(validate(&bad).is_err(), "{key}: unit");
        let mut bad = base.clone();
        bad[i].value = Some(100);
        assert!(validate(&bad).is_err(), "{key}: promoted value");
        let mut bad = base.clone();
        bad[i].relation = "PyUpperGuardScriptsBenchEntryTokenPathL87C11".into();
        assert!(
            validate(&bad).is_err(),
            "{key}: guard instead of the option default"
        );
    }
}

#[test]
fn the_other_bench_forwards_stay_pending() {
    let rows = rows();
    let reviewed: std::collections::BTreeSet<_> = rows.iter().map(|r| r.key.clone()).collect();
    for key in [
        // time.sleep(control_ms / 1000) / time.sleep(hop_ms / 1000): real waits on a local parameter.
        "scripts/bench_entry_token_path.py:28:8:positional-call:time.sleep",
        "scripts/bench_entry_token_path.py:33:8:positional-call:time.sleep",
        "scripts/bench_entry_token_path.py:35:8:positional-call:time.sleep",
    ] {
        assert!(
            !reviewed.contains(key),
            "{key} (a real wait) must stay pending: r2-wait-argument-edge"
        );
    }
    let pending = candidate_keys().unwrap().difference(&reviewed).count();
    assert_eq!(
        pending,
        1299 + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len()
    ); // exact new manual review is outside the bounds-only view // R17 +12 tuple-floor operands; manual reviews separate. // R16 +8 census operands; manual reviews counted separately. // +570 RU-051 keyword operands. // 876 - 2 argparse R1 - 24 source-unbounded - 8 round-7 forward-callers - 9 cli literals - 83 x-exclusions - 4 round-10 field-origin reads - 2 round-11 self-attribute reads (inventory_field_origins) - 2 round-11 OwnerTokenPath constructor forwards - 1 EntryAdmissionPolicy constructor forward - 1 round-11 self-attribute read (inventory_forward_callers, inventory_field_origins) - 6 round-11 slice 5 lease-deadline rows - 1 slice 6 float() forward - 2 slice 6 self-attribute reads - 11 round-12 validated-document-field rows (inventory_validated_document_field) - 4 round-12 RU-033 propagated source-unbounded rows (inventory_unbounded_propagation) + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)
}
