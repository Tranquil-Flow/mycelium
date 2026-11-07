// SPDX-License-Identifier: AGPL-3.0-only
//! SE-L10-ADAPTIVE-BOUNDS: the adaptive host policy validator bounds that v0.1.4 enforces in
//! `mycelium_a13_macos/adaptive_host.py`. The L0 census records neither `_TIMING_MIN/_TIMING_MAX`
//! nor the `2 <= authority_observations <= 10` range (the names carry no quantity hint), so the
//! ids are non-`Py` registry entries, like the native-Rust ones, with exact oracle provenance
//! `path:line @ f4e366e9 sha256:<file>; <symbol>`. Expected values are the oracle source text
//! (`git show f4e366e9:mycelium_a13_macos/adaptive_host.py`, sha below), not the table.
use budgets::BudgetId;
use budgets::table::{Provenance, Purpose, Value};
use std::collections::BTreeSet;

const FILE: &str = "mycelium_a13_macos/adaptive_host.py";
const SHA: &str = "dce11fdeccad901f739aff97613cc7b0a9b67bc4263f5fad347a82390585f8fa";

#[derive(Debug, PartialEq, Eq)]
enum Q {
    Ms(u64),
    Count(u64),
}

struct Row {
    id: &'static str,
    quantity: Q,
    line: u32,
    /// Verbatim oracle line (trimmed) at `FILE:line`.
    text: &'static str,
    /// Substring of `text` the value was read from.
    needle: &'static str,
    symbol: &'static str,
}

const ROWS: [Row; 5] = [
    Row {
        id: "AdaptiveHostTimingMin",
        quantity: Q::Ms(1_000),
        line: 61,
        text: "_TIMING_MIN: Final = 1",
        needle: "= 1",
        symbol: "_TIMING_MIN (seconds)",
    },
    Row {
        id: "AdaptiveHostTimingMax",
        quantity: Q::Ms(3_600_000),
        line: 62,
        text: "_TIMING_MAX: Final = 3600",
        needle: "= 3600",
        symbol: "_TIMING_MAX (seconds)",
    },
    Row {
        id: "AdaptiveHostAuthorityObservationsDefault",
        quantity: Q::Count(2),
        line: 59,
        text: "\"authority_observations\": 2,",
        needle: ": 2,",
        symbol: "DEFAULT_TIMINGS[authority_observations]",
    },
    Row {
        id: "AdaptiveHostAuthorityObservationsMin",
        quantity: Q::Count(2),
        line: 131,
        text: "or not 2 <= timings[\"authority_observations\"] <= 10",
        needle: "not 2 <=",
        symbol: "authority_observations lower bound (validate_policy)",
    },
    Row {
        id: "AdaptiveHostAuthorityObservationsMax",
        quantity: Q::Count(10),
        line: 131,
        text: "or not 2 <= timings[\"authority_observations\"] <= 10",
        needle: "<= 10",
        symbol: "authority_observations upper bound (validate_policy)",
    },
];

fn quantity(value: Value) -> Q {
    match value {
        Value::Duration(span) => Q::Ms(span.millis()),
        Value::Count(entries) => Q::Count(entries.get()),
        other => panic!("unexpected value class {other:?}"),
    }
}

#[test]
fn adaptive_host_ids_pin_value_class_purpose_and_exact_oracle_source() {
    for row in &ROWS {
        let entry = BudgetId::from_name(row.id)
            .unwrap_or_else(|| panic!("{} is not registered", row.id))
            .entry();
        assert_eq!(quantity(entry.value), row.quantity, "{}", row.id);
        assert!(matches!(entry.purpose, Purpose::Policy), "{}", row.id);
        let Provenance::Source(source) = entry.provenance else {
            panic!("{}: source constant masquerading as measurement", row.id)
        };
        assert_eq!(
            source,
            format!(
                "{FILE}:{} @ f4e366e9 sha256:{SHA}; {}",
                row.line, row.symbol
            ),
            "{}",
            row.id
        );
        // The pinned text is the declaration the value was read from.
        assert!(row.text.contains(row.needle), "{}", row.id);
    }
}

#[test]
fn adaptive_host_ids_are_non_py_listed_once_and_do_not_shadow_the_py_defaults() {
    let names: BTreeSet<_> = ROWS.iter().map(|row| row.id).collect();
    assert_eq!(names.len(), 5);
    assert!(names.iter().all(|name| !name.starts_with("Py")));
    let listed: Vec<_> = include_str!("../data/legacy-budget-ids.txt")
        .lines()
        .filter(|line| names.contains(line))
        .collect();
    assert_eq!(listed.len(), 5);
    assert_eq!(listed.iter().copied().collect::<BTreeSet<_>>(), names);
}

#[test]
fn adaptive_host_ranges_are_ordered_and_bound_the_registered_default_timings() {
    let ms = |id: &str| match quantity(BudgetId::from_name(id).unwrap().entry().value) {
        Q::Ms(v) => v,
        other => panic!("{id}: {other:?}"),
    };
    let count = |id: &str| match quantity(BudgetId::from_name(id).unwrap().entry().value) {
        Q::Count(v) => v,
        other => panic!("{id}: {other:?}"),
    };
    let (lo, hi) = (ms("AdaptiveHostTimingMin"), ms("AdaptiveHostTimingMax"));
    assert!(lo < hi);
    // Every registered v0.1.4 default timing (lines 49-58) lies inside the validated range.
    let defaults = [
        "PyFinalMyceliumA13MacosAdaptiveHostSampleIntervalSL49C4",
        "PyFinalMyceliumA13MacosAdaptiveHostStaleAfterSL50C4",
        "PyFinalMyceliumA13MacosAdaptiveHostGameDrainSL51C4",
        "PyFinalMyceliumA13MacosAdaptiveHostPressureDrainSL52C4",
        "PyFinalMyceliumA13MacosAdaptiveHostZeroCapacitySL53C4",
        "PyFinalMyceliumA13MacosAdaptiveHostWithdrawSL54C4",
        "PyFinalMyceliumA13MacosAdaptiveHostReducedDwellSL55C4",
        "PyFinalMyceliumA13MacosAdaptiveHostDrainDwellSL56C4",
        "PyFinalMyceliumA13MacosAdaptiveHostRecoverySL57C4",
        "PyFinalMyceliumA13MacosAdaptiveHostCpuSustainSL58C4",
    ];
    for id in defaults {
        let v = ms(id);
        assert!((lo..=hi).contains(&v), "{id} = {v}");
    }
    let (olo, ohi) = (
        count("AdaptiveHostAuthorityObservationsMin"),
        count("AdaptiveHostAuthorityObservationsMax"),
    );
    assert!(olo < ohi);
    assert!((olo..=ohi).contains(&count("AdaptiveHostAuthorityObservationsDefault")));
}
