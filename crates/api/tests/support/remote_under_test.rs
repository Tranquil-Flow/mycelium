// SPDX-License-Identifier: AGPL-3.0-only
//! Compile the actual driver with its test-only injection enabled. Production code is not copied.
#![allow(dead_code)]
pub use api::joined_owner::link;
fn span(id: budgets::BudgetId) -> Result<std::time::Duration, api::schema::PublicError> {
    id.span(budgets::Entries::ZERO)
        .map(|s| std::time::Duration::from_millis(s.millis()))
        .map_err(|_| api::schema::PublicError::new(409, "budget_unavailable"))
}
#[path = "../../src/joined_owner/remote.rs"]
pub mod remote;
