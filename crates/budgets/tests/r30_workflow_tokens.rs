// SPDX-License-Identifier: AGPL-3.0-only
//! Exact source challenge-token limit, not path characters or a generation quota.
use budgets::table::{BudgetEntry, Provenance, Purpose, Value};
use budgets::{BudgetId, Bytes, Entries, Span};
const NAME: &str = "PyManagedWorkflowVerifiedChallengeTokenLimitL206C66";
const ORIGIN: &str = "mycelium_a13_macos/workflow.py:206 @ f4e366e9";
fn contract(e: BudgetEntry) -> Result<Entries, &'static str> {
    if e.id.name() != NAME {
        return Err("identity");
    }
    let Value::Count(n) = e.value else {
        return Err("unit");
    };
    if n.get() != 4096 {
        return Err("value");
    }
    if e.purpose != Purpose::Policy {
        return Err("purpose");
    }
    if e.provenance != Provenance::Source(ORIGIN) {
        return Err("origin");
    }
    Ok(n)
}
#[test]
fn exact_verified_challenge_token_limit_has_its_own_typed_source_identity() {
    let id = BudgetId::from_name(NAME).expect("missing exact workflow206 token-count producer");
    assert_eq!(contract(id.entry()), Ok(Entries::new(4096)));
    assert_eq!(id.count().unwrap().get(), 4096);
    assert!(id.span(Entries::ZERO).is_err());
}
#[test]
fn unrelated_equal_counts_and_wrong_contract_fields_are_refused() {
    let original = BudgetId::from_name(NAME).unwrap().entry();
    for id in [
        BudgetId::PyStagePrepositionPathCharacters,
        BudgetId::ManagedDemoObservedVerifiedTokenCountMax,
    ] {
        assert_eq!(id.count().unwrap().get(), 4096);
        assert_eq!(contract(id.entry()), Err("identity"));
    }
    let mut bad = original;
    bad.value = Value::Duration(Span::from_millis(4096));
    assert_eq!(contract(bad), Err("unit"));
    let mut bad = original;
    bad.value = Value::Storage(Bytes::new(4096));
    assert_eq!(contract(bad), Err("unit"));
    let mut bad = original;
    bad.value = Value::Count(Entries::new(4097));
    assert_eq!(contract(bad), Err("value"));
    let mut bad = original;
    bad.purpose = Purpose::Resource;
    assert_eq!(contract(bad), Err("purpose"));
    let mut bad = original;
    bad.provenance = Provenance::Source("mycelium_a13_macos/workflow.py:211 @ f4e366e9");
    assert_eq!(contract(bad), Err("origin"));
    let mut bad = original;
    bad.provenance = Provenance::Source("mycelium_a13_macos/workflow.py:206 @ ef9ae61d");
    assert_eq!(contract(bad), Err("origin"));
}
