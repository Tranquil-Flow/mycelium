// SPDX-License-Identifier: AGPL-3.0-only
//! Existing Rust harness policy only; no product ceiling or measured latency.
use budgets::{BudgetId, Entries, Span};
use std::time::Duration;

pub const SESSION_TIMEOUT_ID: BudgetId = BudgetId::ManagedOwnerFactoryFixtureSessionTimeout;
pub const BARRIER_WAIT_ID: BudgetId = BudgetId::ManagedOwnerFixtureBarrierWait;

pub fn session_timeout() -> Span {
    SESSION_TIMEOUT_ID.span(Entries::new(0)).unwrap()
}
pub fn session_seconds() -> f64 {
    Duration::from_millis(session_timeout().millis()).as_secs_f64()
}
pub fn barrier_wait() -> Duration {
    Duration::from_millis(BARRIER_WAIT_ID.span(Entries::new(0)).unwrap().millis())
}
