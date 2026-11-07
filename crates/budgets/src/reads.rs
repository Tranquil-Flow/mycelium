// SPDX-License-Identifier: AGPL-3.0-only
//! Opt-in, synchronous read instrumentation for deterministic simulation.
//! Typed accessors (including recursive formula reads) report declarations here.
//! Threads have independent scopes; nested scopes report to every active reader.
use crate::table::{BudgetEntry, BudgetId};
use std::{cell::RefCell, collections::BTreeMap};
thread_local! {
    static SCOPES: RefCell<Vec<BTreeMap<BudgetId, BudgetEntry>>> = const { RefCell::new(Vec::new()) };
}
pub(crate) fn record(entry: BudgetEntry) {
    SCOPES.with(|scopes| {
        for scope in scopes.borrow_mut().iter_mut() {
            scope.insert(entry.id, entry);
        }
    });
}
/// Capture the exact distinct declarations read by a synchronous closure.
/// A guard pops the scope even on panic. Async tasks must not cross this boundary.
pub fn capture<T>(f: impl FnOnce() -> T) -> (T, Vec<BudgetEntry>) {
    struct Guard;
    impl Drop for Guard {
        fn drop(&mut self) {
            SCOPES.with(|scopes| {
                scopes.borrow_mut().pop();
            });
        }
    }
    SCOPES.with(|scopes| scopes.borrow_mut().push(BTreeMap::new()));
    let guard = Guard;
    let output = f();
    let reads = SCOPES.with(|scopes| {
        scopes
            .borrow()
            .last()
            .expect("active capture scope")
            .values()
            .copied()
            .collect()
    });
    drop(guard);
    (output, reads)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Entries, Span};
    #[test]
    fn derived_accessors_capture_transitive_declarations() {
        let (_, reads) = capture(|| {
            BudgetId::EffectiveRolloverLead
                .wall_span(Span::from_millis(600_000), Entries::new(2))
                .unwrap()
        });
        let ids: Vec<_> = reads.iter().map(|e| e.id).collect();
        assert_eq!(
            ids,
            vec![
                BudgetId::HalfWallDivisor,
                BudgetId::ExecutionCeilingLead,
                BudgetId::RolloverMargin,
                BudgetId::EffectiveRolloverLead
            ]
        );
    }
    #[test]
    fn nested_thread_and_panic_scopes_do_not_leak() {
        let (_, outer) = capture(|| {
            BudgetId::ReplayKeep.count().unwrap();
            let (_, inner) = capture(|| BudgetId::SavedStorageConsent.bytes().unwrap());
            assert_eq!(inner.len(), 1);
            std::thread::spawn(|| capture(|| BudgetId::OwnerPoll.span(Entries::new(2)).unwrap()))
                .join()
                .unwrap();
            assert!(std::panic::catch_unwind(|| capture(|| panic!("scope control"))).is_err());
        });
        assert_eq!(outer.len(), 2);
        assert_eq!(capture(|| ()).1.len(), 0);
    }
}
