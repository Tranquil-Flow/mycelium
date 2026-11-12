// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-side composition of the joined route as product code (gap row 6).
//!
//! Before B16 this composition lived only in the owner test driver
//! (`tests/support/joined_route_process.rs`). It is the owner half of
//! admit member -> transfer -> member native prepare -> route -> chat ->
//! Stop/leave, built only from existing verified pieces:
//! - [`epoch::Epoch`]: one single-use route epoch (seed-signed
//!   `mycelium.joined_route.v1` -> [`crate::m16_route::RouteHost`] ->
//!   carrier executor -> M16 runtime -> request service -> loopback HTTP
//!   gateway + pump thread). `close` joins every owned thread and frees the
//!   gateway port.
//! - [`codec::NativeCodec`]: the selected snapshot's tokenizer through the
//!   pinned native binary's private `tokenize` helper (api links no tokenizer).
//!
//! No signed or wire format is added or changed here; every document is still
//! issued and verified by `joined_shard`/`m16_route`. route_ready stays false.
use crate::schema::PublicError;
use budgets::{BudgetId as B, Entries};
use runtime::m16_ledger::{IdSource, LeaseClock};
use std::{
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};

pub mod codec;
pub mod consent;
pub mod epoch;
pub mod link;
pub mod remote;
pub mod source;
pub mod work;

fn fail(code: &'static str) -> PublicError {
    PublicError::new(409, code)
}
/// A typed budget as a std duration; an unreadable table is a typed refusal.
pub(crate) fn span(id: B) -> Result<Duration, PublicError> {
    id.span(Entries::ZERO)
        .map(|s| Duration::from_millis(s.millis()))
        .map_err(|_| fail("budget_unavailable"))
}
/// Wall clock for the M16 ledger and lease clocks.
pub struct Wall;
impl LeaseClock for Wall {
    fn now(&self) -> f64 {
        crate::app_host::now()
    }
}
/// Monotonic request/placement ids of one epoch's M16 runtime.
#[derive(Default)]
pub struct Ids(AtomicU64);
impl IdSource for Ids {
    fn new_id(&self, prefix: &str) -> String {
        format!("{prefix}-{}", self.0.fetch_add(1, Ordering::SeqCst))
    }
}
