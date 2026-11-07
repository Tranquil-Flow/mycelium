// SPDX-License-Identifier: AGPL-3.0-only
//! The v0.1.4 seed coordinator SERVER side (`mycelium_seed/{coordinator,http,state}.py`,
//! `mycelium_a13_macos/tls.py` @ f4e366e9), owned by L5 (RU-025) and consumed by L10's owner-collapse
//! `DemoAuthority.accept_pairing`. The pure decisions live in `protocol::{seed_coordinator, seed_members}`;
//! this crate adds the stateful, signing, durable, network half.
pub mod coordinator;
pub mod http;
pub mod operator;
pub mod server;
mod store;
pub use store::{Described, describe as describe_state};
pub mod tls;
mod v014;

pub use coordinator::{Clock, Coordinator, IdSource, Options, SeedError, limits};
