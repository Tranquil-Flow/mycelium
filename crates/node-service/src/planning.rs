// SPDX-License-Identifier: AGPL-3.0-only
//! S16: L7 is the sole planning adapter; L6 owns durable reservations.
//! Re-export rather than maintaining a second planner or in-memory release ledger.
pub use planner::control_adapter::{Error, Prepared, RestartBudget, continue_rollover, prepare};
