// SPDX-License-Identifier: AGPL-3.0-only
//! Static exact source operand contracts; no API retry loop or native enforcement.
use budgets::table::{BudgetEntry, Phase, Provenance, Purpose, Value};
use budgets::{BudgetId, Entries, Permille, Span};
const NAME: &str = "PyManagedRuntimeOutboundRetryMultiplierL463C93";
const ORIGIN: &str = "mycelium_a13_macos/managed_runtime.py:463 @ f4e366e9";
fn contract(e: BudgetEntry) -> Result<Permille, &'static str> {
    if e.id.name() != NAME {
        return Err("identity");
    }
    let Value::Ratio(r) = e.value else {
        return Err("unit");
    };
    if r.get() != 2000 {
        return Err("value");
    }
    if e.purpose != Purpose::Policy {
        return Err("purpose");
    }
    if e.provenance != Provenance::Source(ORIGIN) {
        return Err("origin");
    }
    Ok(r)
}
#[test]
fn exact_retry_multiplier_is_a_dimensionless_source_ratio() {
    let id = BudgetId::from_name(NAME).expect("missing exact463 multiplier producer");
    assert_eq!(contract(id.entry()), Ok(Permille::new(2000)));
    assert!(id.count().is_err());
    assert!(id.span(Entries::ZERO).is_err());
}
#[test]
fn initializer_reset_floor_and_ceiling_keep_distinct_identity_and_authority() {
    let cases = [
        (
            BudgetId::PyFinalMyceliumA13MacosManagedRuntimeSelfOutboundRetryBackoffL155C8,
            100,
            Purpose::Operational(Phase::Unclassified),
            "mycelium_a13_macos/managed_runtime.py:155 @ ef9ae61d",
        ),
        (
            BudgetId::PyFinalMyceliumA13MacosManagedRuntimeSelfOutboundRetryBackoffL471C20,
            100,
            Purpose::Operational(Phase::Unclassified),
            "mycelium_a13_macos/managed_runtime.py:471 @ ef9ae61d",
        ),
        (
            BudgetId::PyFinalMyceliumA13MacosManagedRuntimeL463C61ComparisonLower,
            100,
            Purpose::Policy,
            ORIGIN,
        ),
        (
            BudgetId::PyFinalMyceliumA13MacosManagedRuntimeL463C53CeilingOperand,
            1000,
            Purpose::Policy,
            ORIGIN,
        ),
    ];
    for (id, ms, purpose, origin) in cases {
        let e = id.entry();
        assert_eq!(e.value, Value::Duration(Span::from_millis(ms)));
        assert_eq!(e.purpose, purpose);
        assert_eq!(e.provenance, Provenance::Source(origin));
    }
    let mut prior = cases[0].0.span(Entries::ZERO).unwrap();
    let ratio = contract(BudgetId::from_name(NAME).unwrap().entry()).unwrap();
    let floor = cases[2].0.span(Entries::ZERO).unwrap();
    let cap = cases[3].0.span(Entries::ZERO).unwrap();
    // Re-expression of the externally executed original AST, NOT a product retry implementation.
    let mut actual = Vec::new();
    for _ in 0..5 {
        prior = prior
            .checked_mul(ratio.get())
            .unwrap()
            .checked_div(1000)
            .unwrap()
            .max(floor)
            .min(cap);
        actual.push(prior.millis());
    }
    assert_eq!(actual, [200, 400, 800, 1000, 1000]);
    prior = cases[1].0.span(Entries::ZERO).unwrap();
    assert_eq!(
        prior
            .checked_mul(ratio.get())
            .unwrap()
            .checked_div(1000)
            .unwrap()
            .max(floor)
            .min(cap)
            .millis(),
        200
    );
    assert_ne!(cases[0].0, cases[1].0);
    assert_ne!(cases[1].0, cases[2].0);
}
#[test]
fn wrong_unit_value_origin_purpose_and_identity_are_refused() {
    let original = BudgetId::from_name(NAME).unwrap().entry();
    let mut e = original;
    e.value = Value::Count(Entries::new(2));
    assert_eq!(contract(e), Err("unit"));
    let mut e = original;
    e.value = Value::Duration(Span::from_millis(2000));
    assert_eq!(contract(e), Err("unit"));
    let mut e = original;
    e.value = Value::Ratio(Permille::new(1000));
    assert_eq!(contract(e), Err("value"));
    let mut e = original;
    e.purpose = Purpose::Resource;
    assert_eq!(contract(e), Err("purpose"));
    let mut e = original;
    e.provenance = Provenance::Source("mycelium_a13_macos/managed_runtime.py:471 @ f4e366e9");
    assert_eq!(contract(e), Err("origin"));
    let mut e = original;
    e.provenance = Provenance::Source("mycelium_a13_macos/managed_runtime.py:463 @ ef9ae61d");
    assert_eq!(contract(e), Err("origin"));
    let mut e = original;
    e.id = BudgetId::PyManagedRuntimeDeliveryWakeCeilingL461C16;
    assert_eq!(contract(e), Err("identity"));
}
