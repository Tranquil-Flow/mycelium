// SPDX-License-Identifier: AGPL-3.0-only
//! Clean host adapter for the reserved headless composition root.
//!
//! After owner lifecycle transitions and on its periodic tick, pass an OwnerView to
//! `observe`. The caller owns serialization and scheduling; no background task or
//! authority is invented here. Listing preference must come from an explicit owner
//! action, not membership, relay start, or entry reachability alone.
use super::{Call, DirectoryPort, Error, Listing, OwnerState, Status, Step, SwarmPublisher};
use serde_json::Value;
use std::sync::Arc;

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
    pub fn shown(&self) -> super::Shown {
        self.publisher.shown()
    }
    pub fn observe(&mut self, now: i64, view: OwnerView<'_>) -> Result<Status, Error> {
        let step = self.begin_observe(now, view)?;
        self.publisher.drive(step)
    }
    /// `observe` as a pass whose exchanges the caller sends (see `SwarmPublisher::begin`).
    pub fn begin_observe(&mut self, now: i64, view: OwnerView<'_>) -> Result<Step, Error> {
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
        self.publisher.begin(now, &owner)
    }
    /// Invoke before releasing publisher custody on quit/leave/pause. On unreachable
    /// directory the file still owns debt; the next startup must open and tick it.
    pub fn stop(&mut self, now: i64, event: Stop) -> Result<Status, Error> {
        let step = self.begin_stop(now, event)?;
        self.publisher.drive(step)
    }
    /// `stop` as a pass whose exchanges the caller sends.
    pub fn begin_stop(&mut self, now: i64, event: Stop) -> Result<Step, Error> {
        let state = self.mark_stop(event)?;
        self.publisher.begin(now, &state)
    }
    /// Only the durable part of `stop` (no exchange): a live listing becomes withdrawal debt.
    pub fn mark_stop(&mut self, event: Stop) -> Result<OwnerState, Error> {
        let state = match event {
            Stop::Pause => OwnerState::Paused,
            Stop::Leave => {
                self.publisher.set_listing(None)?;
                OwnerState::Left
            }
            Stop::Quit => OwnerState::Quit,
        };
        self.publisher.mark(&state)?;
        Ok(state)
    }
    /// Apply a reply to an exchange planned by `begin_*`/`advance`.
    pub fn advance(&mut self, call: Call, reply: Result<Value, Error>) -> Result<Step, Error> {
        self.publisher.advance(call, reply)
    }
    pub fn port(&self) -> Arc<dyn DirectoryPort> {
        self.publisher.port()
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stop {
    Pause,
    Leave,
    Quit,
}
