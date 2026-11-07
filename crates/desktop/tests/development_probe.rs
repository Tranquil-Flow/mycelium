// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(all(feature = "dev-shell", debug_assertions, target_os = "macos"))]
use desktop::{
    core::Core,
    lifecycle::{Cleanup, CleanupError, Poll},
};
use std::sync::Arc;
struct NeverStarted;
impl Cleanup for NeverStarted {
    fn begin(&mut self, _: budgets::Span) -> Result<(), CleanupError> {
        unreachable!()
    }
    fn poll(&mut self) -> Result<Poll, CleanupError> {
        unreachable!()
    }
}
#[test]
fn composition_can_supply_a_probe_without_modifying_frozen_assets() {
    type Entry = fn(Arc<dyn Core>, NeverStarted, budgets::Span, String) -> Result<(), String>;
    let entry: Entry = desktop::shell::run_development_with_probe::<NeverStarted>;
    // Compile the public composition seam without launching an unowned GUI.
    std::hint::black_box(entry);
}
