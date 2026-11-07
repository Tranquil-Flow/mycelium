// SPDX-License-Identifier: AGPL-3.0-only
//! STATIC original-source producer contract; not API enforcement or measured wake latency.
use budgets::table::{BudgetEntry, Provenance, Purpose, Value};
use budgets::{BudgetId, Entries, Span};
const NAME: &str = "PyManagedRuntimeDeliveryWakeCeilingL461C16";
const ORIGIN: &str = "mycelium_a13_macos/managed_runtime.py:461 @ f4e366e9";
fn contract(e: BudgetEntry) -> Result<Span, &'static str> {
    if e.id.name() != NAME {
        return Err("identity");
    }
    let Value::Duration(span) = e.value else {
        return Err("unit");
    };
    if span.millis() != 50 {
        return Err("value");
    }
    if e.purpose != Purpose::Policy {
        return Err("purpose");
    }
    if e.provenance != Provenance::Source(ORIGIN) {
        return Err("origin");
    }
    Ok(span)
}
#[test]
fn exact_delivery_wake_source_ceiling_is_typed_fifty_milliseconds() {
    let id = BudgetId::from_name(NAME)
        .expect("missing exact managed_runtime.py:461 delivery-wake producer");
    let span: Span = id.span(Entries::new(0)).unwrap();
    assert_eq!(contract(id.entry()), Ok(span));
    assert_eq!(span.millis(), 50);
    assert_eq!(
        std::time::Duration::from_millis(span.millis()).as_secs_f64(),
        0.05
    );
}
#[test]
fn independent_contract_fields_and_equal_value_wrong_origin_are_refused() {
    let id = BudgetId::from_name(NAME).expect("missing exact delivery-wake producer");
    let foreign = BudgetId::PyFinalMyceliumNodeProcessL2963C65CeilingOperand;
    assert_eq!(id.span(Entries::new(0)), foreign.span(Entries::new(0)));
    assert_eq!(contract(foreign.entry()), Err("identity"));
    let original = id.entry();
    let mut bad = original;
    bad.value = Value::Count(Entries::new(50));
    assert_eq!(contract(bad), Err("unit"));
    let mut bad = original;
    bad.value = Value::Duration(Span::from_millis(51));
    assert_eq!(contract(bad), Err("value"));
    let mut bad = original;
    bad.purpose = Purpose::Resource;
    assert_eq!(contract(bad), Err("purpose"));
    let mut bad = original;
    bad.provenance = Provenance::Source("mycelium_a13_macos/managed_runtime.py:463 @ f4e366e9");
    assert_eq!(contract(bad), Err("origin"));
    let mut bad = original;
    bad.provenance = Provenance::Source("mycelium_a13_macos/managed_runtime.py:461 @ ef9ae61d");
    assert_eq!(contract(bad), Err("origin"));
    let mut bad = original;
    bad.provenance = Provenance::Blocked("unbounded");
    assert_eq!(contract(bad), Err("origin"));
}
