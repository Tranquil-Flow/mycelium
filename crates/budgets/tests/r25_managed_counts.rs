// SPDX-License-Identifier: AGPL-3.0-only
//! STATIC exact producer contracts; consumer enforcement belongs to the API lane.
use budgets::table::{BudgetEntry, Provenance, Purpose, Value};
use budgets::{BudgetId, Bytes, Entries};
const SPECS: [(&str, u64, &str); 3] = [
    (
        "PyManagedControlMaxActiveWork",
        1,
        "mycelium_a13_macos/managed_control.py:104 @ f4e366e9",
    ),
    (
        "PyManagedControlMaxActiveControl",
        2,
        "mycelium_a13_macos/managed_control.py:105 @ f4e366e9",
    ),
    (
        "PyStagePrepositionPathCharacters",
        4096,
        "mycelium_a13_macos/managed_offers.py:61 @ f4e366e9",
    ),
];
fn contract(e: BudgetEntry, spec: (&str, u64, &'static str)) -> Result<Entries, &'static str> {
    if e.id.name() != spec.0 {
        return Err("identity");
    }
    let Value::Count(n) = e.value else {
        return Err("unit");
    };
    if n != Entries::new(spec.1) {
        return Err("value");
    }
    if e.purpose != Purpose::Resource {
        return Err("purpose");
    }
    if e.provenance != Provenance::Source(spec.2) {
        return Err("origin");
    }
    Ok(n)
}
#[test]
fn exact_three_source_count_producers_exist() {
    for spec in SPECS {
        let id = BudgetId::from_name(spec.0).expect("missing exact source-specific Count producer");
        assert_eq!(contract(id.entry(), spec), Ok(Entries::new(spec.1)));
        let value: Entries = id.count().unwrap();
        assert_eq!(value, Entries::new(spec.1));
    }
}
#[test]
fn equal_value_foreign_ids_and_independent_metadata_mutants_are_refused() {
    let foreign = [
        "PyMyceliumA13MacosTokenProgressPumpPumptokenprogressOpenMaxsizeL85",
        "ArtifactMaxConnectionsPerPeer",
        "SeedReplayIds",
    ];
    for (spec, other) in SPECS.into_iter().zip(foreign) {
        let id = BudgetId::from_name(spec.0).expect("missing exact producer");
        let wrong = BudgetId::from_name(other).unwrap();
        assert_eq!(wrong.count(), id.count());
        assert_eq!(contract(wrong.entry(), spec), Err("identity"));
        let original = id.entry();
        let mut bad = original;
        bad.value = Value::Storage(Bytes::new(spec.1));
        assert_eq!(contract(bad, spec), Err("unit"));
        let mut bad = original;
        bad.value = Value::Count(Entries::new(spec.1 + 1));
        assert_eq!(contract(bad, spec), Err("value"));
        let mut bad = original;
        bad.purpose = Purpose::Policy;
        assert_eq!(contract(bad, spec), Err("purpose"));
        let mut bad = original;
        bad.provenance = Provenance::Source("mycelium_a13_macos/managed_offers.py:219 @ f4e366e9");
        assert_eq!(contract(bad, spec), Err("origin"));
    }
    let observed = BudgetId::ManagedDemoObservedVerifiedTokenCountMax;
    assert_eq!(observed.count(), Ok(Entries::new(4096)));
    assert_eq!(contract(observed.entry(), SPECS[2]), Err("identity"));
}
