// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's product evidence routes (B13, gap row 7) on this launcher: `api::product_routes`
//! (shared with the packaged executable) over this launcher's owner seed. Members come from the
//! owner's seed through its public read APIs only.
//!
//! Not composed here (absent on this launcher): the live route status, the route qualification,
//! the assignment offers, the request gateway's recovery projection and the transport history.
//! Those sources therefore publish `missing`/`unsupported`.
use super::Shared;
use api::product_routes::{Owner, OwnerSeed, Routes};
use std::sync::Arc;

/// The running owner seed and the state root, or `None` (no swarm, a member, or the authority stopped).
/// Core's lock is held only to clone the handles; no seed call runs under it.
struct HeadlessOwner(Shared);
impl Owner for HeadlessOwner {
    fn seed(&self) -> Option<OwnerSeed> {
        let core = self.0.lock().ok()?;
        if core.target || core.closed {
            return None;
        }
        Some(OwnerSeed {
            coordinator: core.seed.as_ref()?.coordinator.clone(),
            root: core.root.clone(),
        })
    }
}

/// The product routes of this host: one application per swarm, built on first use while the
/// owner's seed runs; without it every route answers `503 product_*_unavailable`.
pub fn routes(core: Shared) -> Routes {
    Routes::new(Arc::new(HeadlessOwner(core)))
}
