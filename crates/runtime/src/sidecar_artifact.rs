// SPDX-License-Identifier: AGPL-3.0-only
//! The production [`ArtifactBackend`] of the sidecar entry point: the signed artifact service
//! over the durable admission journal (`ArtifactSource::with_endpoint`,
//! `native/mobile_core/src/artifact_net.rs:339-355 @ f4e366e9`).
//!
//! Every bound is a budget-registry value; the live-fence capacity is
//! `ArtifactMaxFence` (`artifact_net.rs:94 MAX_FENCE`, seam SE-L9-3). A caller may still supply
//! its own capacity (tests); [`DurableArtifactBackend::shipped`] reads the registry and, if the
//! id were refused, serves nothing (`ArtifactSource`, the entry ends `fatal`).
use crate::{
    artifact_journal::{DurableConfig, DurableObjects},
    iroh_artifact::{ArtifactError, ArtifactWireLimits},
    iroh_artifact_source::{ArtifactServeLimits, ArtifactService},
    iroh_pool::ConnectionService,
    sidecar_main::{ArtifactBackend, ArtifactBuild},
    sidecar_process::SidecarError,
};
use budgets::{BudgetId as B, Entries, Span};
use protocol::artifact_grant::GrantLimits;
use std::{
    num::NonZeroUsize,
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

pub struct DurableArtifactBackend {
    /// Live fence entries per source; `None` refuses to serve.
    pub fence_capacity: Option<NonZeroUsize>,
}
impl DurableArtifactBackend {
    /// The shipped bounds: the fence capacity is the registry's `ArtifactMaxFence`.
    pub fn shipped() -> Self {
        Self {
            fence_capacity: B::ArtifactMaxFence
                .count()
                .ok()
                .and_then(|entries| usize::try_from(entries.get()).ok())
                .and_then(NonZeroUsize::new),
        }
    }
}

fn millis(id: B) -> Result<u64, SidecarError> {
    id.span(Entries::ZERO)
        .map(|span| span.millis())
        .map_err(|_| SidecarError::ArtifactSource)
}
fn span(id: B) -> Result<Span, SidecarError> {
    id.span(Entries::ZERO)
        .map_err(|_| SidecarError::ArtifactSource)
}
fn count(id: B) -> Result<Entries, SidecarError> {
    id.count().map_err(|_| SidecarError::ArtifactSource)
}
/// The grant verifier's bounds, from the registry (the same four ids the owner side uses).
pub fn grant_limits() -> Result<GrantLimits, SidecarError> {
    Ok(GrantLimits {
        max_grant_ttl_ms: millis(B::PyMyceliumMobileArtifactGrantMaxGrantTtlMsL38)?,
        max_request_ttl_ms: millis(B::PyMyceliumMobileArtifactGrantMaxRequestTtlMsL39)?,
        request_ttl_ms: millis(B::PyFinalMyceliumMobileArtifactGrantTtlMsL309C18)?,
        clock_skew_ms: millis(B::PyMyceliumMobileArtifactGrantClockSkewMsL40)?,
    })
}
/// `artifact_net.rs:58-92`: the wire caps and the serving bounds.
pub fn serve_limits() -> Result<ArtifactServeLimits, SidecarError> {
    let bytes = |id: B| id.bytes().map_err(|_| SidecarError::ArtifactSource);
    Ok(ArtifactServeLimits {
        wire: ArtifactWireLimits {
            request_bytes: bytes(B::ArtifactMaxRequestBytes)?,
            header_bytes: bytes(B::ArtifactMaxHeaderBytes)?,
            io: span(B::ArtifactIoTimeout)?,
        },
        connections: count(B::ArtifactMaxConnections)?,
        per_peer: count(B::ArtifactMaxConnectionsPerPeer)?,
        blocking: count(B::ArtifactMaxBlockingWork)?,
        read: span(B::ArtifactReadTimeout)?,
        idle: span(B::ArtifactIdleTimeout)?,
        maximum_pace: span(B::ArtifactMaxPace)?,
    })
}
fn wall_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX)
        })
}

impl ArtifactBackend for DurableArtifactBackend {
    fn serve(&self, build: ArtifactBuild) -> Result<Arc<dyn ConnectionService>, SidecarError> {
        let fence_capacity = self.fence_capacity.ok_or(SidecarError::ArtifactSource)?;
        let limits = grant_limits()?;
        // The journal opens (and creates or compacts the ledger) before anything else can fail,
        // as `ArtifactSource::with_endpoint` does.
        let objects = DurableObjects::open(DurableConfig {
            objects: build.objects,
            state_dir: build.state_dir,
            fence_capacity: fence_capacity.get(),
            clock_skew_ms: limits.clock_skew_ms,
            now: Arc::new(wall_ms),
        })
        .map_err(|_: ArtifactError| SidecarError::ArtifactSource)?;
        let service = ArtifactService::new(
            build.signer,
            build.seed_key,
            limits,
            Arc::new(objects),
            serve_limits()?,
            Arc::new(wall_ms),
        )
        .map_err(|_: ArtifactError| SidecarError::ArtifactSource)?;
        Ok(Arc::new(service))
    }
}
