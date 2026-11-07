// SPDX-License-Identifier: AGPL-3.0-only
//! Clean host adapter for the reserved headless composition root.
//!
//! After owner lifecycle transitions and on its periodic tick, pass an OwnerView to
//! `observe`. The caller owns serialization and scheduling; no background task or
//! authority is invented here. Listing preference must come from an explicit owner
//! action, not membership, relay start, or entry reachability alone.
use super::{Error, Listing, OwnerState, Status, SwarmPublisher};

pub struct OwnerView<'a> {
    pub is_owner: bool,
    pub run_state: &'a str,
    pub route_ready: bool,
    pub registry_origin: Option<&'a str>,
    pub model: Option<&'a str>,
    pub members: u32,
    pub public_entry_origin: Option<&'a str>,
}
pub struct PublisherHost {
    publisher: SwarmPublisher,
}
impl PublisherHost {
    pub fn new(publisher: SwarmPublisher) -> Self {
        Self { publisher }
    }
    pub fn listing_preference(
        &mut self,
        is_owner: bool,
        preference: Option<Listing>,
    ) -> Result<(), Error> {
        if !is_owner {
            return Err(Error::new("swarm_owner_role_required"));
        }
        self.publisher.set_listing(preference)
    }
    pub fn status(&self) -> Status {
        self.publisher.status()
    }
    pub fn observe(&mut self, now: i64, view: OwnerView<'_>) -> Result<Status, Error> {
        let owner = if !view.is_owner {
            self.publisher.set_listing(None)?;
            OwnerState::Left
        } else if view.run_state == "serving"
            && view.route_ready
            && view.registry_origin.is_some_and(|s| !s.is_empty())
        {
            match (view.model, view.public_entry_origin) {
                (Some(model), Some(origin)) => OwnerState::Serving {
                    model: model.into(),
                    members: view.members,
                    entry_origin: origin.into(),
                },
                _ => OwnerState::Paused,
            }
        } else if view.run_state == "running" {
            OwnerState::Warming {
                members: view.members,
            }
        } else {
            OwnerState::Paused
        };
        self.publisher.reconcile(now, &owner)
    }
    /// Invoke before releasing publisher custody on quit/leave/pause. On unreachable
    /// directory the file still owns debt; the next startup must open and tick it.
    pub fn stop(&mut self, now: i64, event: Stop) -> Result<Status, Error> {
        let state = match event {
            Stop::Pause => OwnerState::Paused,
            Stop::Leave => {
                self.publisher.set_listing(None)?;
                OwnerState::Left
            }
            Stop::Quit => OwnerState::Quit,
        };
        self.publisher.reconcile(now, &state)
    }
}
pub enum Stop {
    Pause,
    Leave,
    Quit,
}
