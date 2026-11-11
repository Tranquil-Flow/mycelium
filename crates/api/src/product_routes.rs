// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's product evidence routes as a host composes them (B13, gap row 7; PH2 for the packaged
//! host): v0.1.4's product application (`mycelium_product_spine/app.py` @ f4e366e9) as the A13
//! serving stack composed it (`mycelium_live/supervisor.py:2133-2210`, `mycelium_a13_macos/
//! serving.py:842`): members from the owner's seed (`LiveSeedAuthority.current_members`,
//! `route.py:817-827`), the pseudonym salt derived from the seed key for the swarm
//! (`route.py:967-974`), the replay window under `<state>/serving/product`. Only public read APIs of
//! the seed are used (`Coordinator::members`, `authority_state`, `signer`, `swarm_id`).
//!
//! The headless launcher and the packaged executable share this code; each host gives an [`Owner`]:
//! its running owner seed and state root, plus the live sources it has (route status,
//! qualification, assignments, recovery). A source a host does not give publishes
//! `missing`/`unsupported`, as before.
//!
//! B13f: the window is bound to the swarm id (another swarm's window is set aside when the
//! application opens, the set-asides pruned to the newest 3, B15); a refused window answers 503
//! with its typed reason, is remembered with the identity of the file that refused it (no re-read
//! of up to 32 MiB per GET), and stays refused until the owner's `POST /api/v1/product/reset` moves
//! it aside.
use crate::{
    desktop::ProductRoutes,
    http::{Reply, Request},
    product_app::{
        self, AppError, Composition, DEFAULT_REPLAY_LIMIT, ProductApp, STATE_FILE, SetAside,
        SourceError, Sources, StateBinding,
    },
    product_projector::ProductProjector,
    schema::PublicError,
};
use serde_json::{Map, Value};
use std::{
    fs,
    os::unix::fs::MetadataExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{SystemTime, UNIX_EPOCH},
};

/// The running owner seed of a host and the state root its window lives under.
pub struct OwnerSeed {
    pub coordinator: Arc<::seed::Coordinator>,
    pub root: PathBuf,
}

/// What a host gives its product routes. Only [`Owner::seed`] is required; every live source keeps
/// the `Sources` default (not composed) unless the host has it.
pub trait Owner: Send + Sync {
    /// The running owner seed and the state root, or `None` (no swarm, a member, the authority
    /// stopped, or the application closed). A host holds its own lock only to clone the handles.
    fn seed(&self) -> Option<OwnerSeed>;
    /// `assignment_source` (`route.product_assignment_records`, `route.py:6910-6948`).
    fn assignments(&self) -> Option<Result<Vec<Map<String, Value>>, SourceError>> {
        None
    }
    /// `route_source` (`route.public_status`, `mycelium.live_route_status.v1`).
    fn route_status(&self) -> Result<Option<Value>, SourceError> {
        Ok(None)
    }
    /// `qualification_source` (`route_qualification_to_dict(qualification_source.current())`).
    fn qualification(&self) -> Result<Option<Value>, SourceError> {
        Ok(None)
    }
    /// `recovery_source` (the request gateway's `recovery_public_projection`).
    fn recovery(&self) -> Result<Option<Value>, SourceError> {
        Ok(None)
    }
}

fn unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| u64::try_from(d.as_millis()).unwrap_or(u64::MAX))
}

/// The application's sources: `membership_source` from the owner's seed — its member projections
/// plus its authority generation and latest rotation, exactly the keys `current_members()` adds
/// (`route.py:817-827`) — and the host's live sources.
struct SeedSources {
    owner: Arc<dyn Owner>,
}
impl Sources for SeedSources {
    fn members(&self) -> Result<Vec<Map<String, Value>>, SourceError> {
        let seed = self.owner.seed().ok_or(SourceError::Failed)?;
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
    fn assignments(&self) -> Option<Result<Vec<Map<String, Value>>, SourceError>> {
        self.owner.assignments()
    }
    fn route_status(&self) -> Result<Option<Value>, SourceError> {
        self.owner.route_status()
    }
    fn qualification(&self) -> Result<Option<Value>, SourceError> {
        self.owner.qualification()
    }
    fn recovery(&self) -> Result<Option<Value>, SourceError> {
        self.owner.recovery()
    }
}

/// The identity of a stored window file (no read): a refusal is re-evaluated only when it changes.
type FileIdentity = (u64, u64, u64, i64, i64, u32);
fn stored_file(state: &Path) -> Option<FileIdentity> {
    let meta = fs::symlink_metadata(state.join(STATE_FILE)).ok()?;
    Some((
        meta.dev(),
        meta.ino(),
        meta.size(),
        meta.mtime(),
        meta.mtime_nsec(),
        meta.mode(),
    ))
}

/// What the routes hold for the current swarm.
enum Slot {
    Empty,
    Ready(String, Arc<ProductApp>),
    /// The stored window refused the application; `file` is the refusing file's identity.
    Refused {
        swarm: String,
        reason: AppError,
        file: Option<FileIdentity>,
    },
}

/// What a request gets.
enum Built {
    Absent,
    Refused(AppError),
    Ready(Arc<ProductApp>),
}

fn log_set_aside(set_aside: &SetAside) {
    // The daemon log names what was moved and why; nothing else about the window is printed.
    eprintln!(
        "product_evidence_state_set_aside: {} {}{}{}",
        set_aside.reason,
        set_aside.file_name,
        if set_aside.synced {
            ""
        } else {
            " (directory sync failed)"
        },
        if set_aside.pruned {
            ""
        } else {
            " (older set-asides not pruned)"
        }
    );
}

/// The product routes of a host: one application per swarm, built on first use while the owner's
/// seed runs; without it every route answers `503 product_*_unavailable`.
pub struct Routes {
    owner: Arc<dyn Owner>,
    slot: Mutex<Slot>,
}
impl Routes {
    pub fn new(owner: Arc<dyn Owner>) -> Self {
        Self {
            owner,
            slot: Mutex::new(Slot::Empty),
        }
    }
    fn app(&self) -> Built {
        let Some(OwnerSeed { coordinator, root }) = self.owner.seed() else {
            return Built::Absent;
        };
        let swarm = coordinator.swarm_id().to_owned();
        let Ok(mut slot) = self.slot.lock() else {
            return Built::Absent;
        };
        let state = match product_app::private_root(&root) {
            Ok(state) => state,
            Err(refused) => return Built::Refused(refused),
        };
        match &*slot {
            Slot::Ready(bound, app) if *bound == swarm => return Built::Ready(app.clone()),
            Slot::Refused {
                swarm: bound,
                reason,
                file,
            } if *bound == swarm && *file == stored_file(&state) => {
                return Built::Refused(*reason);
            }
            _ => {}
        }
        let built = (|| {
            let salt = protocol::seed_authority::derive_product_pseudonym_salt(
                coordinator.signer(),
                &swarm,
            )
            .map_err(|_| AppError("product_pseudonym_salt_unavailable"))?;
            let projector = ProductProjector::new(&salt)
                .map_err(|_| AppError("product_pseudonym_salt_invalid"))?;
            ProductApp::new(
                projector,
                Arc::new(SeedSources {
                    owner: self.owner.clone(),
                }),
                Box::new(unix_ms),
                DEFAULT_REPLAY_LIMIT,
                Some(StateBinding {
                    root: &state,
                    swarm_id: &swarm,
                }),
            )
        })();
        match built {
            Ok(app) => {
                if let Some(set_aside) = app.set_aside() {
                    log_set_aside(set_aside);
                }
                let app = Arc::new(app);
                *slot = Slot::Ready(swarm, app.clone());
                Built::Ready(app)
            }
            Err(reason) => {
                eprintln!("product_application_unavailable: {}", reason.0);
                *slot = Slot::Refused {
                    swarm,
                    reason,
                    file: stored_file(&state),
                };
                Built::Refused(reason)
            }
        }
    }
}
impl ProductRoutes for Routes {
    fn get(&self, request: &Request) -> Result<Reply, PublicError> {
        let built = self.app();
        let composition = match &built {
            Built::Absent => Composition::Absent,
            Built::Refused(reason) => Composition::Refused(*reason),
            Built::Ready(app) => Composition::Ready(app),
        };
        product_app::handle_composed(composition, request)
    }
    /// The owner's reset of a refused window: moved aside, and the next GET opens a fresh one.
    fn reset(&self, body: &Map<String, Value>) -> Result<Reply, PublicError> {
        if !body.is_empty() {
            return Err(PublicError::new(400, "invalid_request"));
        }
        let Some(OwnerSeed { coordinator, root }) = self.owner.seed() else {
            return product_app::reset_reply(Err(AppError("product_reset_unavailable")));
        };
        let swarm = coordinator.swarm_id().to_owned();
        let mut slot = self
            .slot
            .lock()
            .map_err(|_| PublicError::new(503, "product_reset_unavailable"))?;
        if matches!(&*slot, Slot::Ready(bound, _) if *bound == swarm) {
            return product_app::reset_reply(Err(AppError("product_evidence_state_not_refused")));
        }
        let result = product_app::private_root(&root).and_then(|state| {
            product_app::reset_state(
                StateBinding {
                    root: &state,
                    swarm_id: &swarm,
                },
                DEFAULT_REPLAY_LIMIT,
                unix_ms(),
            )
        });
        if let Ok(set_aside) = &result {
            log_set_aside(set_aside);
            *slot = Slot::Empty;
        } else if matches!(&result, Err(AppError("product_evidence_state_not_refused")))
            && matches!(&*slot, Slot::Refused { swarm: bound, .. } if *bound == swarm)
        {
            // A cached refusal whose window no longer refuses (B15, FU1): it is forgotten, so the
            // next request builds the application again instead of staying refused until a restart.
            *slot = Slot::Empty;
        }
        product_app::reset_reply(result)
    }
}
