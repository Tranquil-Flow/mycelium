// SPDX-License-Identifier: AGPL-3.0-only
use api::{
    schema::{PublicError, Snapshot},
    status::PublishedStatus,
};
use runtime::published::Published;
pub fn refresh(
    source: &Published<Snapshot>,
    target: &PublishedStatus,
    _revision: u64,
) -> Result<(), PublicError> {
    let snapshot = source.try_snapshot().map_err(|_| {
        target.record_refresh_failure();
        PublicError::new(503, "status_unavailable")
    })?;
    target.publish(_revision, snapshot.value)
}

/// Separate publication lock; never held during planner/native/process effects.
pub struct Projection {
    source: Published<Snapshot>,
    target: PublishedStatus,
    revision: std::sync::Mutex<u64>,
}
impl Projection {
    pub fn new(initial: Snapshot) -> Self {
        Self {
            source: Published::new(0, initial),
            target: PublishedStatus::new(),
            revision: std::sync::Mutex::new(0),
        }
    }
    pub fn publish(&self, value: Snapshot) -> Result<(), PublicError> {
        let mut revision = self
            .revision
            .lock()
            .map_err(|_| PublicError::new(503, "status_unavailable"))?;
        *revision = revision
            .checked_add(1)
            .ok_or(PublicError::new(503, "status_revision_overflow"))?;
        self.source
            .replace(*revision, value)
            .map_err(|_| PublicError::new(503, "status_unavailable"))?;
        refresh(&self.source, &self.target, *revision)
    }
    pub fn read(&self) -> Result<serde_json::Value, PublicError> {
        self.target.read()
    }
}
