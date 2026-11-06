// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged `mycelium` executable's composition: where the pinned model and the node
//! service's private state live under the per-user state root, and the core the window gets.
//!
//! The model is looked up in v0.1.4's own layout (`model-source-<rev12>/models--Qwen--…/
//! snapshots/<rev>/`), so an install over a v0.1.4 state root finds the model it already
//! downloaded, and the packaged host's `/api/model/download` (`api::app_host`'s acquisition, as
//! `_open_acquisition`, app.py:649-672 @ f4e366e9) downloads it into that same layout. The window
//! always opens on the packaged action root; until the pinned snapshot is there the chat answers
//! 503 `model_not_installed`, and once this process's acquisition prepared it the native service
//! is composed over it on demand, without a relaunch. Quit closes the acquisition
//! (`DemoApp.close`, app.py:1845-1847), so the next launch answers `closed`. The node service is
//! SLICE-1's: operator-fixture consent, development qualification identity, local 127.0.0.1 Iroh
//! only. It is a real native service, not a certified release core.
use crate::{
    app_host::ModelSource,
    desktop_host::{DesktopServices, ModelGate},
    service::{Driver, Job, NodeService},
};
use api::{acquisition::Transport, resources::Observer, schema::PublicError};
use desktop::{
    core::Core,
    lifecycle::{Cleanup, CleanupError, Poll},
    services::ServiceCleanup,
};
use std::{
    num::NonZeroUsize,
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    thread::JoinHandle,
};

/// The four page endpoints (`state`, `send`, `events`, `stop`) the slice smoke served with;
/// the dedicated desktop callback capacity is still an open L1 request.
pub const CALLBACK_CAPACITY: NonZeroUsize = NonZeroUsize::new(4).unwrap();
pub const MODEL_NOT_INSTALLED: &str = "model_not_installed";
/// The prepared model is located but its native driver could not be composed (the cause is on
/// stderr); the next chat request tries again.
pub const MODEL_COMPOSE_FAILED: &str = "model_compose_failed";

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

/// The node service's driver in the packaged executable: the native driver over the located
/// model. It is composed at window open when the model is already there (the prepared path), or
/// on the first request after this process's acquisition prepared the pinned snapshot; until
/// then every run and every chat request is refused with `model_not_installed`.
struct NativeOnDemand {
    state_root: PathBuf,
    dirs: Dirs,
    source: ModelSource,
    native: Mutex<Option<Arc<dyn Driver>>>,
}
impl NativeOnDemand {
    /// `desktop_host::compose`'s driver: the launch's fresh evidence directory and the native
    /// driver over `model` and the private state.
    fn compose(&self, model: &ModelFiles) -> crate::loopback::Result<Arc<dyn Driver>> {
        match std::fs::create_dir(&self.dirs.evidence) {
            // A composition that failed after making it is retried in the same directory.
            Err(e) if e.kind() != std::io::ErrorKind::AlreadyExists => return Err(e.into()),
            _ => {}
        }
        crate::native::configured_driver(
            &model.checkpoint,
            &model.tokenizer,
            &self.dirs.evidence,
            &self.dirs.private,
        )
    }
    /// The composed native driver, composed now when the pinned model was prepared and located.
    fn driver(&self) -> Result<Arc<dyn Driver>, PublicError> {
        let mut native = self
            .native
            .lock()
            .map_err(|_| PublicError::new(503, "native_driver_poisoned"))?;
        if let Some(driver) = native.as_ref() {
            return Ok(driver.clone());
        }
        let model = locate_model(&self.state_root)
            .filter(|_| self.source.prepared(backend_mlx::admission::REVISION))
            .ok_or(PublicError::new(503, MODEL_NOT_INSTALLED))?;
        let driver = self.compose(&model).map_err(|e| {
            eprintln!("{MODEL_COMPOSE_FAILED}: {e}");
            PublicError::new(503, MODEL_COMPOSE_FAILED)
        })?;
        *native = Some(driver.clone());
        Ok(driver)
    }
}
impl Driver for NativeOnDemand {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        self.driver()?.run(job)
    }
}
impl ModelGate for NativeOnDemand {
    fn ready(&self) -> Result<(), PublicError> {
        self.driver().map(|_| ())
    }
}
pub enum AppCleanup {
    /// The window's services, then the run model's acquisition.
    Service(Box<ServiceCleanup<DesktopServices>>, ModelClose),
    Nothing,
}
impl Cleanup for AppCleanup {
    fn bind_clock(&mut self, origin: std::time::Instant) -> Result<(), CleanupError> {
        match self {
            Self::Service(cleanup, _) => cleanup.bind_clock(origin),
            Self::Nothing => Ok(()),
        }
    }
    fn begin(&mut self, deadline: budgets::Span) -> Result<(), CleanupError> {
        match self {
            Self::Service(cleanup, model) => {
                cleanup.begin(deadline)?;
                model.begin();
                Ok(())
            }
            Self::Nothing => Ok(()),
        }
    }
    fn poll(&mut self) -> Result<Poll, CleanupError> {
        match self {
            Self::Service(cleanup, model) => match cleanup.poll()? {
                Poll::Verified => model.poll(),
                Poll::Pending => Ok(Poll::Pending),
            },
            Self::Nothing => Ok(Poll::Verified),
        }
    }
}
/// Quit's acquisition step, `DemoApp.close` (app.py:1845-1847 @ f4e366e9):
/// `self.acquisition.close()` cancels and joins the running attempt and persists `closed`, so the
/// next launch answers `closed` with the bytes kept, never a recovered interrupted transfer. The
/// attempt is asked to stop as soon as Quit begins; the close runs on its own thread once the
/// window's services have stopped (no request can start or reopen an attempt after it), and its
/// receipt is read without blocking. A close that fails keeps its code on stderr and leaves the
/// cleanup unproven, as v0.1.4's `cleanup_error = 'acquisition_stop_failed'` makes its launcher
/// report `cleanup_incomplete`.
pub struct ModelClose {
    source: ModelSource,
    close: Closing,
}
enum Closing {
    NotStarted,
    Running(JoinHandle<Result<(), String>>),
    Closed,
    Failed,
}
impl ModelClose {
    fn new(source: ModelSource) -> Self {
        Self {
            source,
            close: Closing::NotStarted,
        }
    }
    fn begin(&self) {
        self.source.cancel();
    }
    fn poll(&mut self) -> Result<Poll, CleanupError> {
        const FAILED: &str = "model source: acquisition_stop_failed";
        match std::mem::replace(&mut self.close, Closing::Failed) {
            Closing::NotStarted => {
                let source = self.source.clone();
                match std::thread::Builder::new()
                    .name("packaged-acquisition-close".into())
                    .spawn(move || source.close())
                {
                    Ok(worker) => {
                        self.close = Closing::Running(worker);
                        Ok(Poll::Pending)
                    }
                    Err(_) => {
                        eprintln!("{FAILED}: acquisition_close_thread_unavailable");
                        Err(CleanupError::Poll)
                    }
                }
            }
            Closing::Running(worker) if !worker.is_finished() => {
                self.close = Closing::Running(worker);
                Ok(Poll::Pending)
            }
            Closing::Running(worker) => match worker.join() {
                Ok(Ok(())) => {
                    self.close = Closing::Closed;
                    Ok(Poll::Verified)
                }
                Ok(Err(code)) => {
                    eprintln!("{FAILED}: {code}");
                    Err(CleanupError::Poll)
                }
                Err(_) => {
                    eprintln!("{FAILED}: acquisition_close_panicked");
                    Err(CleanupError::Poll)
                }
            },
            Closing::Closed => {
                self.close = Closing::Closed;
                Ok(Poll::Verified)
            }
            Closing::Failed => Err(CleanupError::Poll),
        }
    }
}

/// [`core`] with the model repository's transport and this Mac's resource observer given (a test
/// serves a loopback repository and controls the observation).
pub fn core_with(
    state_root: &Path,
    transport: Arc<dyn Transport>,
    observer: Arc<dyn Observer>,
) -> Result<(Arc<dyn Core>, AppCleanup), String> {
    compose(
        state_root,
        ModelSource::new(state_root.to_owned(), transport).with_observer(observer),
    )
}
/// The core and cleanup for the window, from the state root.
pub fn core(state_root: &Path) -> Result<(Arc<dyn Core>, AppCleanup), String> {
    compose(state_root, ModelSource::hub(state_root.to_owned()))
}
fn compose(state_root: &Path, source: ModelSource) -> Result<(Arc<dyn Core>, AppCleanup), String> {
    let dirs = prepare_dirs(state_root)?;
    let private = dirs.private.clone();
    let native = Arc::new(NativeOnDemand {
        state_root: state_root.to_owned(),
        dirs,
        source: source.clone(),
        native: Mutex::new(None),
    });
    match locate_model(state_root) {
        Some(model) => {
            let driver = native.compose(&model).map_err(|e| format!("compose:{e}"))?;
            *native
                .native
                .lock()
                .map_err(|_| "compose:native_driver_poisoned".to_string())? = Some(driver);
        }
        None => eprintln!("{MODEL_NOT_INSTALLED}"),
    }
    let node = NodeService::new(
        native.clone(),
        crate::service::random_id().map_err(|e| format!("compose:{}", e.code))?,
        crate::native::development_qualification().map_err(|e| format!("compose:{e}"))?,
    );
    let services =
        DesktopServices::with_model_source(node, CALLBACK_CAPACITY, &private, source.clone())
            .gated(native);
    let (core, cleanup) = desktop::services::adapt(Arc::new(services), CALLBACK_CAPACITY);
    Ok((
        core,
        AppCleanup::Service(Box::new(cleanup), ModelClose::new(source)),
    ))
}
