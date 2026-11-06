// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged `mycelium` executable's composition: where the pinned model and the node
//! service's private state live under the per-user state root, and the core the window gets.
//!
//! The model is looked up in v0.1.4's own layout (`model-source-<rev12>/models--Qwen--…/
//! snapshots/<rev>/`), so an install over a v0.1.4 state root finds the model it already
//! downloaded; this build never downloads. Without it the window still opens and every core
//! call answers 503 `model_not_installed`. The node service is SLICE-1's: operator-fixture
//! consent, development qualification identity, local 127.0.0.1 Iroh only. It is a real
//! native service, not a certified release core.
use crate::desktop_host::{self, DesktopServices};
use desktop::{
    channel::Request,
    core::{Completion, Core, Reply},
    lifecycle::{Cleanup, CleanupError, Poll},
    services::ServiceCleanup,
};
use std::{
    num::NonZeroUsize,
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    sync::Arc,
};

/// The four page endpoints (`state`, `send`, `events`, `stop`) the slice smoke served with;
/// the dedicated desktop callback capacity is still an open L1 request.
pub const CALLBACK_CAPACITY: NonZeroUsize = NonZeroUsize::new(4).unwrap();
pub const MODEL_NOT_INSTALLED: &str = "model_not_installed";

pub struct ModelFiles {
    pub checkpoint: PathBuf,
    pub tokenizer: PathBuf,
}

/// `<state>/model-source-<rev12>/models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/<rev>/…`.
pub fn locate_model(state_root: &Path) -> Option<ModelFiles> {
    let revision = backend_mlx::admission::REVISION;
    let snapshot = state_root
        .join(format!("model-source-{}", &revision[..12]))
        .join("models--Qwen--Qwen2.5-0.5B-Instruct/snapshots")
        .join(revision);
    let files = ModelFiles {
        checkpoint: snapshot.join("model.safetensors"),
        tokenizer: snapshot.join("tokenizer.json"),
    };
    (files.checkpoint.is_file() && files.tokenizer.is_file()).then_some(files)
}

/// Private state persists across launches (identities, consent); evidence is fresh per launch.
pub struct Dirs {
    pub private: PathBuf,
    pub evidence: PathBuf,
}
pub fn prepare_dirs(state_root: &Path) -> Result<Dirs, String> {
    let base = state_root.join("v02-node");
    let launch = crate::service::random_id().map_err(|e| e.code.to_string())?;
    let private = base.join("private");
    let evidence_root = base.join("evidence");
    for dir in [&private, &evidence_root] {
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(dir)
            .map_err(|e| format!("state_dir:{:?}", e.kind()))?;
    }
    Ok(Dirs {
        private,
        evidence: evidence_root.join(launch),
    })
}

/// No model: the window opens, the core refuses with a named code, there is nothing to clean.
struct NoModel;
impl Core for NoModel {
    fn submit(&self, _: Request, complete: Completion) {
        complete(Reply::error(503, MODEL_NOT_INSTALLED));
    }
}
pub enum AppCleanup {
    Service(Box<ServiceCleanup<DesktopServices>>),
    Nothing,
}
impl Cleanup for AppCleanup {
    fn bind_clock(&mut self, origin: std::time::Instant) -> Result<(), CleanupError> {
        match self {
            Self::Service(cleanup) => cleanup.bind_clock(origin),
            Self::Nothing => Ok(()),
        }
    }
    fn begin(&mut self, deadline: budgets::Span) -> Result<(), CleanupError> {
        match self {
            Self::Service(cleanup) => cleanup.begin(deadline),
            Self::Nothing => Ok(()),
        }
    }
    fn poll(&mut self) -> Result<Poll, CleanupError> {
        match self {
            Self::Service(cleanup) => cleanup.poll(),
            Self::Nothing => Ok(Poll::Verified),
        }
    }
}

/// [`core`] with the model repository's transport given (stub: not consulted yet).
pub fn core_with(
    state_root: &Path,
    transport: Arc<dyn api::acquisition::Transport>,
) -> Result<(Arc<dyn Core>, AppCleanup), String> {
    drop(transport);
    core(state_root)
}
/// The core and cleanup for the window, from the state root.
pub fn core(state_root: &Path) -> Result<(Arc<dyn Core>, AppCleanup), String> {
    let Some(model) = locate_model(state_root) else {
        eprintln!("{MODEL_NOT_INSTALLED}");
        return Ok((Arc::new(NoModel), AppCleanup::Nothing));
    };
    let dirs = prepare_dirs(state_root)?;
    let (core, cleanup) = desktop_host::compose(
        &model.checkpoint,
        &model.tokenizer,
        &dirs.evidence,
        &dirs.private,
        CALLBACK_CAPACITY,
    )
    .map_err(|e| format!("compose:{e}"))?;
    Ok((core, AppCleanup::Service(Box::new(cleanup))))
}
