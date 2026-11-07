// SPDX-License-Identifier: AGPL-3.0-only
//! L1b round 6, owner ruling 2026-10-28 (option a): runtime-supplied request/config/policy fields for which
//! v0.1.4 ships no ceiling are registered as explicit *source-unbounded* ids (`Value::Blocked`, provenance
//! `Blocked("v0.1.4 ships no ceiling at path:line ...")`, tagged V03). The port accepts exactly what v0.1.4
//! accepts: no invented ceiling, no new refusal. A row whose field has any v0.1.4 range check is NOT this family.
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
use budget_model::inventory::{Bound, candidate_keys, checked_in, unbounded, validate};
use budgets::{
    BudgetId,
    table::{Provenance, Value},
};
use std::collections::BTreeSet;

/// (key, family, field, registry id)
const ROWS: &[(&str, &str, &str, &str)] = &[
    (
        "mycelium_a10_batch_policy_control.py:100:8:assignment:timeout",
        "o1",
        "timeout_s",
        "PySourceUnboundedMyceliumA10BatchPolicyControlL100C8",
    ),
    (
        "mycelium_a10_verify_control.py:109:8:assignment:timeout",
        "o1",
        "timeout_s",
        "PySourceUnboundedMyceliumA10VerifyControlL109C8",
    ),
    (
        "mycelium_a13_macos/app.py:765:20:dict-field:max_renewed_seconds",
        "o2",
        "max_renewed_seconds",
        "PySourceUnboundedMyceliumA13MacosAppL765C20",
    ),
    (
        "mycelium_a7_parity_measurement.py:170:4:assignment:timeout",
        "o2",
        "worker_timeout_s",
        "PySourceUnboundedMyceliumA7ParityMeasurementL170C4",
    ),
    (
        "mycelium_a7_parity_measurement.py:234:12:keyword:reserved_kv_bytes",
        "o2",
        "reserved_kv_bytes",
        "PySourceUnboundedMyceliumA7ParityMeasurementL234C12",
    ),
    (
        "mycelium_a7_parity_measurement.py:237:12:keyword:ttl_ms",
        "o2",
        "ttl_ms",
        "PySourceUnboundedMyceliumA7ParityMeasurementL237C12",
    ),
    (
        "mycelium_a7_parity_measurement.py:254:3:assignment:timeout",
        "o2",
        "worker_timeout_s",
        "PySourceUnboundedMyceliumA7ParityMeasurementL254C3",
    ),
    (
        "mycelium_a7_parity_measurement.py:269:6:keyword:reserved_kv_bytes",
        "o2",
        "reserved_kv_bytes",
        "PySourceUnboundedMyceliumA7ParityMeasurementL269C6",
    ),
    (
        "mycelium_a7_parity_measurement.py:272:6:keyword:ttl_ms",
        "o2",
        "ttl_ms",
        "PySourceUnboundedMyceliumA7ParityMeasurementL272C6",
    ),
    (
        "mycelium_a7_physical_measurement.py:1573:12:keyword:ready_timeout_s",
        "o2",
        "ready_timeout_s",
        "PySourceUnboundedMyceliumA7PhysicalMeasurementL1573C12",
    ),
    (
        "mycelium_a7_physical_measurement.py:1574:12:keyword:stop_timeout_s",
        "o2",
        "stop_timeout_s",
        "PySourceUnboundedMyceliumA7PhysicalMeasurementL1574C12",
    ),
    (
        "mycelium_live/local_preparer.py:1573:20:dict-field:maximum_bytes_per_second",
        "o3",
        "aggregate_bytes_per_second",
        "PySourceUnboundedMyceliumLiveLocalPreparerL1573C20",
    ),
    (
        "mycelium_live/member_transport.py:1043:20:dict-field:maximum_bytes_per_second",
        "o3",
        "aggregate_bytes_per_second",
        "PySourceUnboundedMyceliumLiveMemberTransportL1043C20",
    ),
    (
        "mycelium_service_package.py:197:8:dict-field:ThrottleInterval",
        "o2",
        "restart_delay_seconds",
        "PySourceUnboundedMyceliumServicePackageL197C8",
    ),
    (
        "mycelium_service_package.py:512:4:assignment:delay_ms",
        "o2",
        "restart_delay_seconds",
        "PySourceUnboundedMyceliumServicePackageL512C4",
    ),
    (
        "mycelium_service_package.py:646:12:dict-field:stop_timeout_seconds",
        "o2",
        "stop_timeout_seconds",
        "PySourceUnboundedMyceliumServicePackageL646C12",
    ),
];

fn rows() -> Vec<Bound> {
    checked_in().unwrap()
}

#[test]
fn the_16_source_unbounded_rows_are_reviewed_with_blocked_v03_ids() {
    let all = rows();
    assert_eq!(ROWS.len(), 16, "24 round-6 rows minus the 8 RU-044 reverts");
    for (key, _, field, id) in ROWS {
        let row = all
            .iter()
            .find(|r| r.key == *key)
            .unwrap_or_else(|| panic!("{key} must be reviewed"));
        assert_eq!(row.disposition, "source-unbounded", "{key}");
        assert!(row.value.is_none() && row.budget.is_empty(), "{key}");
        assert_eq!(row.unit, "not-applicable", "{key}");
        assert_eq!(row.relation, *id, "{key}");
        assert!(row.review.starts_with("source-unbounded:v03;"), "{key}");
        let entry = BudgetId::from_name(id).unwrap().entry();
        assert_eq!(entry.value, Value::Blocked, "{id}");
        let Provenance::Blocked(text) = entry.provenance else {
            panic!("{id} provenance must be Blocked")
        };
        assert!(
            text.starts_with(&format!(
                "v0.1.4 ships no ceiling at {}:{} @ ef9ae61d; field {field};",
                row.path, row.line
            )),
            "{id}: {text}"
        );
        assert!(text.contains("V03"), "{id}");
        assert!(!unbounded::field_has_census_range_check(field), "{field}");
    }
    let ids: BTreeSet<_> = ROWS.iter().map(|r| r.3).collect();
    assert_eq!(ids.len(), 16, "one id per site, no sharing");
    validate(&all).unwrap();
}

#[test]
fn families_split_as_the_forecast_said() {
    let count = |f: &str| ROWS.iter().filter(|r| r.1 == f).count();
    assert_eq!(
        (count("o1"), count("o2"), count("o3")),
        (2, 12, 2),
        "(4, 16, 4) at round 6 minus the 8 RU-044 reverts"
    );
}

#[test]
fn neighbours_with_a_v014_range_check_are_not_this_family() {
    // Range-checked fields: app.py 30 <= max_seconds <= 1800 (:704,:1449), storage_bytes <= storage_consent_ceiling()
    // (:251, :1563, :1564), a7_node_wiring max_checkpoint_bytes <= MAX_CHECKPOINT_BYTES.
    for field in [
        "max_seconds",
        "storage_bytes",
        "max_checkpoint_bytes",
        "max_retained_checkpoints",
    ] {
        assert!(unbounded::field_has_census_range_check(field), "{field}");
    }
    // Zero-floor and membership checks are not ceilings: worker_timeout_s <= 0 and `key in {...ack_timeout_seconds}`.
    for field in ["worker_timeout_s", "ack_timeout_seconds"] {
        assert!(!unbounded::field_has_census_range_check(field), "{field}");
    }
    // The five storage_bytes reads stay pending (they bind to the range check, R2).
    let reviewed: BTreeSet<_> = rows().into_iter().map(|r| r.key).collect();
    for key in [
        "mycelium_a13_macos/app.py:994:58:keyword:storage_limit_bytes",
        "mycelium_a13_macos/standing_consent.py:226:8:assignment:caps['storage_bytes']",
    ] {
        assert!(
            !reviewed.contains(key),
            "{key} has a v0.1.4 range check; must stay pending"
        );
    }
}

#[test]
fn mutations_are_refused() {
    let base = rows();
    for (key, _, _, id) in ROWS {
        let i = base.iter().position(|r| r.key == *key).unwrap();
        let mut bad = base.clone();
        let other = ROWS.iter().find(|r| r.3 != *id).unwrap().3;
        bad[i].relation = other.into();
        assert!(validate(&bad).is_err(), "{key}: another site's id");
        let mut bad = base.clone();
        bad[i].relation = "CorrectedRestart".into();
        assert!(
            validate(&bad).is_err(),
            "{key}: a different Blocked id (wrong site provenance)"
        );
        let mut bad = base.clone();
        bad[i].relation = "RouterClose".into();
        assert!(validate(&bad).is_err(), "{key}: a valued id");
        let mut bad = base.clone();
        bad[i].value = Some(1);
        assert!(validate(&bad).is_err(), "{key}: value promotion");
        let mut bad = base.clone();
        bad[i].disposition = "not-a-bound".into();
        assert!(
            validate(&bad).is_err(),
            "{key}: relabelled disposition keeps the source-unbounded review"
        );
        let mut bad = base.clone();
        bad[i].review = "source-unbounded:v03; edited".into();
        bad[i].expression.push(' ');
        assert!(validate(&bad).is_err(), "{key}: edited expression");
    }
    // A neighbour outside the pinned table cannot join: clone a member for a range-checked site.
    let member = base.iter().find(|r| r.key == ROWS[0].0).unwrap().clone();
    let mut bad = base.clone();
    let mut neighbour = member;
    neighbour.key = "mycelium_a13_macos/app.py:713:42:keyword:max_seconds".into();
    bad.push(neighbour);
    assert!(
        validate(&bad).is_err(),
        "range-checked neighbour must be refused"
    );
}

#[test]
fn total_pending_shrinks_by_exactly_16() {
    let reviewed: BTreeSet<_> = rows().into_iter().map(|r| r.key).collect();
    let pending = candidate_keys().unwrap().difference(&reviewed).count();
    assert_eq!(
        pending,
        1299 + r29_retry_admission::keys().len() + r32_verifier_admission::keys().len()
    ); // exact new manual review is outside the bounds-only view // R17 +12 tuple-floor operands; manual reviews separate. // R16 +8 census operands; manual reviews counted separately. // +570 RU-051 keyword operands. // - 4 round-10 field-origin reads - 2 round-11 self-attribute reads (inventory_field_origins) - 2 round-11 OwnerTokenPath constructor forwards - 1 EntryAdmissionPolicy constructor forward - 1 round-11 self-attribute read (inventory_forward_callers, inventory_field_origins) - 6 round-11 slice 5 lease-deadline rows - 1 slice 6 float() forward - 2 slice 6 self-attribute reads - 11 round-12 validated-document-field rows (inventory_validated_document_field) - 4 round-12 RU-033 propagated source-unbounded rows (inventory_unbounded_propagation) + 8 RU-044 reverted rows + 3 RU-044 discovery rows (round 13, inventory_ru044_revert) + 6 round-14 census rows (inventory_discovery_extension_r4)
}
