// SPDX-License-Identifier: AGPL-3.0-only
//! Native control lifecycle, driven synchronously by effects and observations.
//! Network authentication and model/planner adapters remain composition seams.
pub mod continuation;
pub use continuation::*;
pub mod domains;
pub mod fds;
pub mod guardian;
pub mod lifecycle;
pub mod process;
pub mod recovery;
pub mod types;
pub mod worker;
pub use lifecycle::*;
pub use types::*;
pub use worker::*;
