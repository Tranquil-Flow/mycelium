// SPDX-License-Identifier: AGPL-3.0-only
//! S1 serving root R-C (RED stub): the packaged host still forwards `/chat/api/*` and Stop to
//! [`NodeService`] unchanged. The qualification source is not read and no driver cancel is
//! dispatched through a router port; the tests in `tests/serving_gateway_root.rs` fail on that.
use crate::service::NodeService;
use api::gateway::{Binding, QualificationSource};
use serde_json::{Value, json};
use std::sync::{
    Arc,
    atomic::{AtomicUsize, Ordering},
};

/// `QualificationBinding` read from the node service's qualification document.
pub fn binding(document: &Value) -> Result<Binding, &'static str> {
    let text = |key: &str| {
        document[key]
            .as_str()
            .map(str::to_owned)
            .ok_or("qualification_document_invalid")
    };
    let number = |key: &str| {
        document[key]
            .as_u64()
            .ok_or("qualification_document_invalid")
    };
    Ok(Binding {
        deployment_id: text("deployment_id")?,
        deployment_epoch: number("deployment_epoch")?,
        topology_version: number("topology_version")?,
        path_manifest_digest: text("path_manifest_digest")?,
        qualification_id: text("qualification_id")?,
        model_id: text("model_id")?,
        resolved_commit: text("resolved_commit")?,
        manifest_digest: text("manifest_digest")?,
        qualification_digest: text("qualification_digest")?,
        stage_load_proof_digests: document["stage_load_proof_digests"]
            .as_array()
            .ok_or("qualification_document_invalid")?
            .iter()
            .map(|d| d.as_str().map(str::to_owned))
            .collect::<Option<Vec<_>>>()
            .ok_or("qualification_document_invalid")?,
    })
}
/// The qualification document a native job carries for `binding`.
pub fn document(binding: &Binding) -> Value {
    json!({
        "deployment_id": binding.deployment_id,
        "deployment_epoch": binding.deployment_epoch,
        "topology_version": binding.topology_version,
        "path_manifest_digest": binding.path_manifest_digest,
        "qualification_id": binding.qualification_id,
        "model_id": binding.model_id,
        "resolved_commit": binding.resolved_commit,
        "manifest_digest": binding.manifest_digest,
        "qualification_digest": binding.qualification_digest,
        "stage_load_proof_digests": binding.stage_load_proof_digests,
    })
}
/// How many cancellations the router port handed to the driver.
#[derive(Clone, Default)]
pub struct Ledger {
    cancels: Arc<AtomicUsize>,
}
impl Ledger {
    pub fn driver_cancels(&self) -> usize {
        self.cancels.load(Ordering::SeqCst)
    }
}
/// The packaged host over `node` (RED: the source is ignored).
#[cfg(any(feature = "desktop-shell", feature = "packaged"))]
pub fn packaged_host(
    node: NodeService,
    _source: Arc<dyn QualificationSource>,
    capacity: std::num::NonZeroUsize,
    private: &std::path::Path,
) -> (crate::desktop_host::DesktopServices, Ledger) {
    (
        crate::desktop_host::DesktopServices::new(node, capacity, private),
        Ledger::default(),
    )
}
