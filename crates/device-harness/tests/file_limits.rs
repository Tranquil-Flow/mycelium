// SPDX-License-Identifier: AGPL-3.0-only
//! S14 file/wire caps come only from the L1 registry; absent ids fail closed.
use budgets::Bytes;
use device_harness::{
    Fault, FaultKind, Result,
    harness_budgets::{self, Ceiling, L1, Registry},
};

#[test]
fn control_file_cap_is_the_registered_v014_native_control_limit() {
    // mycelium_a13_macos/app.py:306 reads native-control.json with max_bytes=4096.
    let id = budgets::BudgetId::from_name(harness_budgets::CONTROL_FILE_BYTES).unwrap();
    assert!(matches!(
        id.entry().provenance,
        budgets::table::Provenance::Source(s) if s.contains("app.py:306")
    ));
    assert_eq!(
        L1.bytes(harness_budgets::CONTROL_FILE_BYTES).unwrap(),
        Bytes::new(4096)
    );
}

#[test]
fn unregistered_launch_session_and_response_caps_fail_closed_with_no_numeric_fallback() {
    for name in [
        harness_budgets::LAUNCH_FILE_BYTES,
        harness_budgets::SESSION_RECORD_BYTES,
        harness_budgets::LOOPBACK_RESPONSE_BYTES,
    ] {
        // Until L1b registers the ids requested in S25, the registry has none of them.
        if budgets::BudgetId::from_name(name).is_none() {
            assert_eq!(
                L1.bytes(name).unwrap_err().code,
                "harness_budget_unregistered"
            );
        }
    }
    if budgets::BudgetId::from_name(harness_budgets::LAUNCH_FILE_BYTES).is_none() {
        assert_eq!(
            harness_budgets::file_limits(&L1).err().unwrap().code,
            "harness_budget_unregistered"
        );
    }
    assert_eq!(
        L1.bytes("HarnessMinimumSoak").unwrap_err().code,
        "harness_budget_wrong_unit"
    );
}

struct Recorded(&'static [(&'static str, u64)]);
impl Registry for Recorded {
    fn span(&self, _: &str) -> Result<budgets::Span> {
        Err(Fault::new("unexpected_span", FaultKind::Input))
    }
    fn count(&self, _: &str) -> Result<u64> {
        Err(Fault::new("unexpected_count", FaultKind::Input))
    }
    fn bytes(&self, name: &str) -> Result<Bytes> {
        self.0
            .iter()
            .find(|(n, _)| *n == name)
            .map(|(_, v)| Bytes::new(*v))
            .ok_or(Fault::new(
                "harness_budget_unregistered",
                FaultKind::Admission,
            ))
    }
}

#[test]
fn limits_resolve_exact_registered_names_and_reject_zero() {
    let all: &'static [(&str, u64)] = &[
        (harness_budgets::CONTROL_FILE_BYTES, 11),
        (harness_budgets::LAUNCH_FILE_BYTES, 22),
        (harness_budgets::SESSION_RECORD_BYTES, 33),
        (harness_budgets::LOOPBACK_RESPONSE_BYTES, 44),
    ];
    let limits = harness_budgets::file_limits(&Recorded(all)).unwrap();
    assert_eq!(
        (limits.control, limits.launch, limits.session),
        (Bytes::new(11), Bytes::new(22), Bytes::new(33))
    );
    assert_eq!(
        harness_budgets::loopback_response_limit(&Recorded(all)).unwrap(),
        Bytes::new(44)
    );
    for missing in 0..all.len() {
        let partial: Vec<_> = all
            .iter()
            .enumerate()
            .filter(|(i, _)| *i != missing)
            .map(|(_, e)| *e)
            .collect();
        let partial: &'static [(&str, u64)] = Box::leak(partial.into_boxed_slice());
        let result = (
            harness_budgets::file_limits(&Recorded(partial)).err(),
            harness_budgets::loopback_response_limit(&Recorded(partial)).err(),
        );
        assert!(
            result.0.is_some() || result.1.is_some(),
            "missing {missing}"
        );
    }
    let zero: &'static [(&str, u64)] = &[
        (harness_budgets::CONTROL_FILE_BYTES, 0),
        (harness_budgets::LAUNCH_FILE_BYTES, 22),
        (harness_budgets::SESSION_RECORD_BYTES, 33),
        (harness_budgets::LOOPBACK_RESPONSE_BYTES, 44),
    ];
    assert_eq!(
        harness_budgets::file_limits(&Recorded(zero))
            .err()
            .unwrap()
            .code,
        "harness_budget_invalid"
    );
}

/// RU-014 / SE-S25: ids v0.1.4 never bounded are registered source-unbounded
/// (`Value::Blocked`, "v0.1.4 ships no ceiling ... V03: ..."). The harness reads them
/// exactly as v0.1.4 does (no ceiling of its own), tagged V03-L12-1, and never invents one.
struct Unbounded;
impl Registry for Unbounded {
    fn span(&self, _: &str) -> Result<budgets::Span> {
        Err(Fault::new("unexpected_span", FaultKind::Input))
    }
    fn count(&self, _: &str) -> Result<u64> {
        Err(Fault::new("unexpected_count", FaultKind::Input))
    }
    fn bytes(&self, name: &str) -> Result<Bytes> {
        match name {
            harness_budgets::CONTROL_FILE_BYTES => Ok(Bytes::new(4096)),
            _ => Err(Fault::new("harness_budget_wrong_unit", FaultKind::Input)),
        }
    }
    fn ceiling(&self, name: &str) -> Result<Ceiling> {
        match name {
            harness_budgets::CONTROL_FILE_BYTES => Ok(Ceiling::Bounded(Bytes::new(4096))),
            harness_budgets::LAUNCH_FILE_BYTES
            | harness_budgets::SESSION_RECORD_BYTES
            | harness_budgets::LOOPBACK_RESPONSE_BYTES => Ok(Ceiling::SourceUnbounded),
            _ => Err(Fault::new(
                "harness_budget_unregistered",
                FaultKind::Admission,
            )),
        }
    }
}

#[test]
fn source_unbounded_ids_resolve_to_the_named_v014_read_everything_limit() {
    let limits = harness_budgets::file_limits(&Unbounded).unwrap();
    assert_eq!(
        limits.control,
        Bytes::new(4096),
        "the one bounded slot stays bounded"
    );
    assert_eq!(limits.launch, harness_budgets::SOURCE_UNBOUNDED_READ);
    assert_eq!(limits.session, harness_budgets::SOURCE_UNBOUNDED_READ);
    assert_eq!(
        harness_budgets::loopback_response_limit(&Unbounded).unwrap(),
        harness_budgets::SOURCE_UNBOUNDED_READ
    );
    assert_eq!(harness_budgets::SOURCE_UNBOUNDED_READ, Bytes::new(u64::MAX));
    assert_eq!(harness_budgets::V03_TAG, "V03-L12-1");
}

#[test]
fn l1_maps_only_v014_no_ceiling_blocked_rows_to_source_unbounded() {
    // A real, already registered row of exactly this class (L1b round 5).
    assert_eq!(
        L1.ceiling("PySourceUnboundedMyceliumA13MacosAppL765C20")
            .unwrap(),
        Ceiling::SourceUnbounded
    );
    // A Storage row is Bounded with its registered value.
    assert_eq!(
        L1.ceiling(harness_budgets::CONTROL_FILE_BYTES).unwrap(),
        Ceiling::Bounded(Bytes::new(4096))
    );
    // A Blocked row that is NOT "v0.1.4 ships no ceiling" (no p99) is refused, not read unbounded.
    assert_eq!(
        L1.ceiling("GuaranteedClose").unwrap_err().code,
        "harness_budget_blocked"
    );
    // Wrong unit and unknown ids keep their codes.
    assert_eq!(
        L1.ceiling("HarnessMinimumSoak").unwrap_err().code,
        "harness_budget_wrong_unit"
    );
    assert_eq!(
        L1.ceiling("NoSuchBudgetId").unwrap_err().code,
        "harness_budget_unregistered"
    );
    // The three S25 ids: absent today (fail closed) or registered source-unbounded/bounded.
    for name in [
        harness_budgets::LAUNCH_FILE_BYTES,
        harness_budgets::SESSION_RECORD_BYTES,
        harness_budgets::LOOPBACK_RESPONSE_BYTES,
    ] {
        match L1.ceiling(name) {
            Ok(_) => assert!(budgets::BudgetId::from_name(name).is_some()),
            Err(e) => assert_eq!(e.code, "harness_budget_unregistered"),
        }
    }
}

#[test]
fn a_source_unbounded_value_never_substitutes_for_a_bounded_read_of_the_wrong_unit() {
    struct Mixed;
    impl Registry for Mixed {
        fn span(&self, _: &str) -> Result<budgets::Span> {
            Err(Fault::new("unexpected_span", FaultKind::Input))
        }
        fn count(&self, _: &str) -> Result<u64> {
            Err(Fault::new("unexpected_count", FaultKind::Input))
        }
        fn bytes(&self, _: &str) -> Result<Bytes> {
            Err(Fault::new(
                "harness_budget_unregistered",
                FaultKind::Admission,
            ))
        }
        fn ceiling(&self, name: &str) -> Result<Ceiling> {
            if name == harness_budgets::CONTROL_FILE_BYTES {
                // The control slot has a registered v0.1.4 ceiling: unbounded is NOT acceptable.
                Ok(Ceiling::SourceUnbounded)
            } else {
                Ok(Ceiling::Bounded(Bytes::new(1)))
            }
        }
    }
    assert_eq!(
        harness_budgets::file_limits(&Mixed).err().unwrap().code,
        "harness_budget_invalid",
        "control has a v0.1.4 ceiling (4096); unbounded there would loosen it"
    );
}
