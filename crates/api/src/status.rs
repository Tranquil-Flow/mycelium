// SPDX-License-Identifier: AGPL-3.0-only
//! Last complete display observation, never admission authority. Collection,
//! backend effects, serialization and projection destruction are outside locks.
use crate::{
    schema::{PublicError, Snapshot},
    wire::Json,
};
use serde_json::Value;
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Instant,
};
struct Publication {
    sampled: Instant,
    snapshot: Snapshot,
}
#[derive(Default)]
struct State {
    value: Option<Arc<Publication>>,
    settled: u64,
    issued: u64,
    generation: Option<u64>,
    failed: bool,
}
#[derive(Default)]
pub struct PublishedStatus {
    state: Mutex<State>,
    legacy_refused: AtomicBool,
}
fn unavailable() -> PublicError {
    PublicError::new(503, "status_unavailable")
}
fn stale() -> PublicError {
    PublicError::new(409, "stale_status_publication")
}
fn publication(snapshot: Snapshot) -> Arc<Publication> {
    Arc::new(Publication {
        sampled: Instant::now(),
        snapshot,
    })
}
impl PublishedStatus {
    pub fn new() -> Self {
        Self::default()
    }
    /// Legacy single-generation producer. Do not mix with StatusOwner. New
    /// lifecycle drivers must use generation-bound collection tickets instead.
    pub fn publish(&self, revision: u64, snapshot: Snapshot) -> Result<(), PublicError> {
        let fresh = publication(snapshot);
        let mut state = self.state.lock().map_err(|_| unavailable())?;
        if state.generation.is_some() {
            return Err(PublicError::new(409, "status_owner_required"));
        }
        if state.value.is_some() && state.settled >= revision {
            return Err(stale());
        }
        state.settled = revision;
        state.failed = false;
        let previous = state.value.replace(fresh);
        drop(state);
        drop(previous);
        Ok(())
    }
    /// Legacy failure without a collection ticket cannot mutate managed status.
    /// Misuse/poison is observable via legacy_write_refused(), never hidden.
    pub fn record_refresh_failure(&self) {
        match self.state.lock() {
            Ok(mut state) if state.generation.is_none() => state.failed = true,
            _ => self.legacy_refused.store(true, Ordering::Release),
        }
    }
    pub fn legacy_write_refused(&self) -> bool {
        self.legacy_refused.load(Ordering::Acquire)
    }
    /// The published snapshot with `status_snapshot` set last (published_status.py:145-148:
    /// `value['status_snapshot'] = {...}` appends, or replaces in place if present).
    pub fn read_json(&self) -> Result<Json, PublicError> {
        let (publication, failed) = {
            let state = self.state.lock().map_err(|_| unavailable())?;
            (state.value.clone().ok_or_else(unavailable)?, state.failed)
        };
        let mut value = publication.snapshot.document();
        let age = (publication.sampled.elapsed().as_secs_f64() * 1000.0).round() / 1000.0;
        value.set(
            "status_snapshot",
            crate::jobj! {"age_seconds" => age, "refresh_failed" => failed},
        );
        Ok(value)
    }
    pub fn read(&self) -> Result<Value, PublicError> {
        self.read_json().map(|j| j.to_value())
    }
}
/// Non-forgeable, one-use collection identity. Mint BEFORE reading the source.
/// Revisions order collection starts, not completions or lifecycle generations.
pub struct Collection {
    owner: Arc<()>,
    generation: u64,
    revision: u64,
}
pub struct StatusOwner {
    identity: Arc<()>,
    target: Arc<PublishedStatus>,
}
/// A failed collection can race a newer publication. Keep both the source
/// failure and failed settlement; neither is an authority to erase newer data.
#[derive(Debug, Eq, PartialEq)]
pub struct RefreshError {
    pub primary: PublicError,
    pub settlement: Option<PublicError>,
}
impl From<PublicError> for RefreshError {
    fn from(primary: PublicError) -> Self {
        Self {
            primary,
            settlement: None,
        }
    }
}
impl StatusOwner {
    pub fn new(generation: u64) -> Result<Self, PublicError> {
        if generation == 0 {
            return Err(stale());
        }
        Ok(Self {
            identity: Arc::new(()),
            target: Arc::new(PublishedStatus {
                state: Mutex::new(State {
                    generation: Some(generation),
                    ..State::default()
                }),
                legacy_refused: AtomicBool::new(false),
            }),
        })
    }
    pub fn status(&self) -> Arc<PublishedStatus> {
        self.target.clone()
    }
    /// One production collection transaction, with no status lock across read.
    /// The closure returns the generation ATOMICALLY sampled with its snapshot.
    pub fn refresh(
        &self,
        read: impl FnOnce() -> Result<(u64, Snapshot), PublicError>,
    ) -> Result<(), RefreshError> {
        let ticket = self.begin()?;
        match read() {
            Ok((generation, snapshot)) => self
                .complete(ticket, generation, snapshot)
                .map_err(Into::into),
            Err(primary) => {
                let settlement = self.fail(ticket).err();
                Err(RefreshError {
                    primary,
                    settlement,
                })
            }
        }
    }
    pub fn begin(&self) -> Result<Collection, PublicError> {
        let mut state = self.target.state.lock().map_err(|_| unavailable())?;
        state.issued = state.issued.checked_add(1).ok_or_else(unavailable)?;
        Ok(Collection {
            owner: self.identity.clone(),
            generation: state.generation.ok_or_else(unavailable)?,
            revision: state.issued,
        })
    }
    /// Only the lifecycle authority calls this after authenticating a successor.
    /// It fences old collectors immediately, not at the first successor publish.
    pub fn advance(&self, generation: u64) -> Result<(), PublicError> {
        let mut state = self.target.state.lock().map_err(|_| unavailable())?;
        if state.generation.is_none_or(|old| generation <= old) {
            return Err(stale());
        }
        state.generation = Some(generation);
        Ok(())
    }
    fn check(&self, state: &State, ticket: &Collection) -> Result<(), PublicError> {
        if !Arc::ptr_eq(&self.identity, &ticket.owner)
            || state.generation != Some(ticket.generation)
            || ticket.revision <= state.settled
        {
            return Err(stale());
        }
        Ok(())
    }
    /// Supply the generation read atomically with the source snapshot. Never
    /// relabel an old runtime::Published snapshot using the collector's ticket.
    pub fn complete(
        &self,
        ticket: Collection,
        source_generation: u64,
        snapshot: Snapshot,
    ) -> Result<(), PublicError> {
        if source_generation != ticket.generation {
            return Err(stale());
        }
        let fresh = publication(snapshot);
        let mut state = self.target.state.lock().map_err(|_| unavailable())?;
        self.check(&state, &ticket)?;
        state.settled = ticket.revision;
        state.failed = false;
        let previous = state.value.replace(fresh);
        drop(state);
        drop(previous);
        Ok(())
    }
    /// A newer failed collection fences older success/failure, while retaining
    /// last-good display bytes. Failure and snapshot are read coherently.
    pub fn fail(&self, ticket: Collection) -> Result<(), PublicError> {
        let mut state = self.target.state.lock().map_err(|_| unavailable())?;
        self.check(&state, &ticket)?;
        state.settled = ticket.revision;
        state.failed = true;
        Ok(())
    }
}
