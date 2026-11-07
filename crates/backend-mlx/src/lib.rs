// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit-feature Apple backend. No feature means no native availability claim.
pub mod admission;
pub mod lifecycle;
#[cfg(feature = "metal")]
mod loader;
pub mod moe_admission;
#[cfg(feature = "metal")]
mod native;
pub mod pins;
pub mod process;
pub mod resources;
pub mod rpath;
pub mod source_image;
#[cfg(feature = "metal")]
pub use native::{Array, Context};
#[cfg(feature = "metal")]
mod worker;
#[cfg(feature = "metal")]
pub use worker::{MlxBackend, Session, StateSnapshot, Worker, payload_id};
