// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's product evidence routes (B13, gap row 7): v0.1.4's product application
//! (`mycelium_product_spine/app.py` @ f4e366e9) as the A13 serving stack composed it
//! (`mycelium_live/supervisor.py:2133-2210`, `mycelium_a13_macos/serving.py:842`): members from the
//! owner's seed (`LiveSeedAuthority.current_members`, `route.py:817-827`), the pseudonym salt derived
//! from the seed key for the swarm (`route.py:967-974`), the replay window under
//! `<state>/serving/product`. Only public read APIs of the seed are used (`Coordinator::members`,
//! `authority_state`, `signer`, `swarm_id`).
//!
//! Not composed here (reserved or absent; see REPORT-laneB13 for the exact calls Evis adds): the live
//! route status, the route qualification, the assignment offers, the request gateway's recovery
//! projection and the transport history. Those sources therefore publish `missing`/`unsupported`.
use super::{SeedRuntime, Shared};
use api::{
    desktop::ProductRoutes,
    http::{Reply, Request},
    product_app::{self, DEFAULT_REPLAY_LIMIT, ProductApp, SourceError, Sources},
    product_projector::ProductProjector,
    schema::PublicError,
};
use serde_json::{Map, Value};
use std::{
    path::PathBuf,
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};

/// The running owner seed and the state root, or `None` (no swarm, a member, or the authority stopped).
/// Core's lock is held only to clone the handles; no seed call runs under it.
fn owner_seed(core: &Shared) -> Option<(Arc<SeedRuntime>, PathBuf)> {
    let core = core.lock().ok()?;
    if core.target || core.closed {
        return None;
    }
    Some((core.seed.clone()?, core.root.clone()))
}

fn unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
}

/// `membership_source`: the seed's member projections plus its authority generation and latest
/// rotation, exactly the keys `current_members()` adds (`route.py:817-827`).
struct SeedMembers {
    core: Shared,
}
impl Sources for SeedMembers {
    fn members(&self) -> Result<Vec<Map<String, Value>>, SourceError> {
        let (seed, _) = owner_seed(&self.core).ok_or(SourceError::Failed)?;
        let rows = seed
            .coordinator
            .members(None)
            .map_err(|_| SourceError::Failed)?;
        let authority = seed
            .coordinator
            .authority_state()
            .map_err(|_| SourceError::Failed)?;
        let rotation = &authority["rotation"];
        let status = rotation["status"]
            .as_str()
            .map_or(Value::Null, |s| Value::String(s.to_lowercase()));
        let observed = rotation["initiated_at"].clone();
        rows.into_iter()
            .map(|row| {
                let Value::Object(mut row) = row else {
                    return Err(SourceError::Failed);
                };
                row.insert(
                    "authority_generation".into(),
                    authority["authority_generation"].clone(),
                );
                row.insert("rotation_status".into(), status.clone());
                row.insert("rotation_observed_at".into(), observed.clone());
                Ok(row)
            })
            .collect()
    }
}

/// The product routes of this host: one application per swarm, built on first use while the
/// owner's seed runs; without it every route answers `503 product_*_unavailable`.
pub struct Routes {
    core: Shared,
    app: Mutex<Option<(String, Arc<ProductApp>)>>,
}
impl Routes {
    pub fn new(core: Shared) -> Self {
        Self {
            core,
            app: Mutex::new(None),
        }
    }
    fn app(&self) -> Option<Arc<ProductApp>> {
        let (seed, root) = owner_seed(&self.core)?;
        let swarm = seed.coordinator.swarm_id().to_owned();
        let mut slot = self.app.lock().ok()?;
        if let Some((bound, app)) = slot.as_ref()
            && *bound == swarm
        {
            return Some(app.clone());
        }
        let built = (|| {
            let salt = protocol::seed_authority::derive_product_pseudonym_salt(
                seed.coordinator.signer(),
                &swarm,
            )
            .map_err(|_| "product_pseudonym_salt_unavailable")?;
            let projector =
                ProductProjector::new(&salt).map_err(|_| "product_pseudonym_salt_invalid")?;
            let state = product_app::private_root(&root).map_err(|e| e.0)?;
            ProductApp::new(
                projector,
                Arc::new(SeedMembers {
                    core: self.core.clone(),
                }),
                Box::new(unix_ms),
                DEFAULT_REPLAY_LIMIT,
                Some(&state),
            )
            .map_err(|e| e.0)
        })();
        match built {
            Ok(app) => {
                let app = Arc::new(app);
                *slot = Some((swarm, app.clone()));
                Some(app)
            }
            Err(code) => {
                // The routes answer 503 product_*_unavailable; the reason stays local.
                eprintln!("product_application_unavailable: {code}");
                None
            }
        }
    }
}
impl ProductRoutes for Routes {
    fn get(&self, request: &Request) -> Result<Reply, PublicError> {
        let app = self.app();
        product_app::handle(app.as_deref(), request)
    }
}
