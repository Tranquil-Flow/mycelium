// SPDX-License-Identifier: AGPL-3.0-only
//! Current seed authority around the existing durable carrier journal.
use super::*;
use runtime::{
    iroh_artifact::ArtifactError,
    iroh_artifact_source::{AdmittedChunk, ArtifactObjects},
};
use std::sync::Arc;
/// One authenticated offer per source owner. This wrapper must be installed in
/// ArtifactService; merely checking an offer before publishing it is insufficient.
pub struct JoinedSource<S> {
    coordinator: Arc<seed::Coordinator>,
    offer: Value,
    objects: S,
}
impl<S: ArtifactObjects> JoinedSource<S> {
    pub fn new(coordinator: Arc<seed::Coordinator>, offer: Value, objects: S) -> Result<Self> {
        check_owner(&coordinator, &offer)?;
        Ok(Self {
            coordinator,
            offer,
            objects,
        })
    }
}
impl<S: ArtifactObjects> ArtifactObjects for JoinedSource<S> {
    fn verified_size(&self, object: &str) -> Option<u64> {
        if self.offer["message"]["transfer"]["content_digest"] != object
            || check_owner(&self.coordinator, &self.offer).is_err()
        {
            return None;
        }
        self.objects.verified_size(object)
    }
    fn admit_and_read(
        &self,
        grant: &ag::Grant,
        request: &ag::ChunkRequest,
        nonce: &str,
        expiry: u64,
        now: u64,
    ) -> std::result::Result<AdmittedChunk, ArtifactError> {
        let check =
            || check_owner(&self.coordinator, &self.offer).map_err(|e| ArtifactError(e.0.into()));
        check()?;
        if grant.envelope != self.offer["message"]["grant"] {
            return Err(ArtifactError("joined_shard_grant_changed".into()));
        }
        let result = self
            .objects
            .admit_and_read(grant, request, nonce, expiry, now)?;
        check()?;
        Ok(result)
    }
}
