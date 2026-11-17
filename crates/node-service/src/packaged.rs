// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged `mycelium` executable's composition: where the node service's private state and
//! the run model live under the per-user state root, and the core the window gets.
//!
//! The run model is fetched by the packaged host's `/api/model/download` (`api::app_host`'s
//! acquisition, as `_open_acquisition`, app.py:649-672 @ f4e366e9) into v0.1.4's own layout
//! (`model-source-<rev12>/models--Qwen--…/snapshots/<rev>/`), so an install over a v0.1.4 state
//! root finds the model it already downloaded. The window always opens on the packaged action
//! root, and every `/chat/*` request goes through the shipped chat service and the owner run's
//! route (`crate::packaged_run::chat_run`, lane CHAT; owner ruling 14 Nov: one chat path, as
//! v0.1.4, whose chat needs a run across two Macs): this executable composes no one-Mac native
//! model, whatever is on disk. The owner's swarm listing custody (`swarm_publish.py`, opt-in only)
//! lives beside the private state. Quit closes the custody and the acquisition (`DemoApp.close`,
//! app.py:1815-1847), so the next launch answers `closed`, and reports the first step that failed
//! after every step ran. The node service keeps SLICE-1's action and status roles (operator-fixture
//! consent, development qualification identity); its driver runs nothing ([`NoLocalChat`]).
use crate::{
    app_host::{ModelSource, Publishing},
    desktop_host::DesktopServices,
    packaged_network::NetworkConfig,
    service::{Driver, Job, NodeService},
};
use api::{acquisition::Transport, resources::Observer, schema::PublicError};
use budgets::{BudgetId, Entries};
use desktop::{
    core::Core,
    lifecycle::{Cleanup, CleanupError, Poll},
    services::ServiceCleanup,
};
use std::{
    num::NonZeroUsize,
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
    sync::Arc,
    thread::JoinHandle,
    time::{Duration, Instant},
};

/// The four page endpoints (`state`, `send`, `events`, `stop`) the slice smoke served with;
/// the dedicated desktop callback capacity is still an open L1 request.
pub const CALLBACK_CAPACITY: NonZeroUsize = NonZeroUsize::new(4).unwrap();
/// The window core's own bound on callbacks in flight: the admitted ones ([`CALLBACK_CAPACITY`])
/// plus as many chat-state callbacks and as many status/Stop/Quit callbacks, which
/// `DesktopServices` bounds apart.
pub const SERVICE_CAPACITY: NonZeroUsize =
    CALLBACK_CAPACITY.saturating_mul(NonZeroUsize::new(3).unwrap());

/// Private state persists across launches (identities, consent); v0.2's evidence lives beside it.
pub struct Dirs {
    pub private: PathBuf,
    /// The owner's per-chat timing file (lane CHAT): JSONL `{ttft_ms, decode_millitokens_s,
    /// request_ms, run_id}` per completed chat (the fields of chat.py:392-395, which v0.1.4 keeps
    /// in memory only), appended across launches in v0.2's own evidence root
    /// `<state>/v02-node/evidence/`, never in v0.1.4's folder.
    pub chat_timing: PathBuf,
}
pub fn prepare_dirs(state_root: &Path) -> Result<Dirs, String> {
    let base = state_root.join("v02-node");
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
        chat_timing: evidence_root.join("chat-timing.jsonl"),
    })
}

/// The packaged node service's driver. The packaged app runs no one-Mac native chat (owner ruling
/// 14 Nov): every `/chat/*` request is the run route's (`DesktopServices::with_chat_run` answers
/// every chat route once a run is composed, and the composition always composes one), and nothing
/// else submits a job to the node service, so this driver is never asked; a job that still
/// arrived is refused with the shipped not-ready code, never run on a native model.
struct NoLocalChat;
impl Driver for NoLocalChat {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        Err(PublicError::new(503, "chat_not_ready"))
    }
}
pub enum AppCleanup {
    /// The window's services, then the owner's listing custody and the run model's acquisition.
    Service(Box<WindowQuit>),
    Nothing,
}
/// Quit's steps over the window's services, the owner's listing custody and the run model's
/// acquisition, run as v0.1.4's `DemoApp.close` runs its own (app.py:1815-1847 @ f4e366e9): a
/// step that fails records the failure (`cleanup_error = ...`) and the next step still runs. The
/// attempt is asked to stop and the listing's tick loop to end as Quit begins, whatever the
/// services answered; once the services finished, verified or failed, the custody is closed
/// (`swarm_publish.close`) and the acquisition is closed, each on its own thread; Quit then
/// reports the first failure in that order (exit 3), else `Verified`. Services still running when
/// only the closes' budget is left of the allowance (v0.1.4's `run.join(60)` ran out) do not hold
/// the closes back: they run, a failed one is reported, else the services' deadline is.
pub struct WindowQuit {
    services: ServiceCleanup<DesktopServices>,
    /// The services that cleanup wraps: their own begin still runs when the cleanup refused it.
    window: Arc<DesktopServices>,
    /// The shell's clock origin and Quit's deadline on it (v0.1.4's `run.join(60)` allowance).
    origin: Option<Instant>,
    deadline: Option<Instant>,
    /// How long the closes may take (the longer of the two): they start this long before the
    /// deadline at the latest, whatever the services still do.
    close_budget: Duration,
    /// The services' cleanup has its result (verified or failed): nothing polls them again.
    services_done: bool,
    swarm: Option<SwarmClose>,
    model: ModelClose,
    /// `self.public_relay.close()` (app.py:1848-1850), after the run's join as in v0.1.4
    /// ([`WindowQuit::gate_relay`]).
    relay: crate::packaged_network::RelayClose,
    /// `demo_app.close` and `_stop_local_contributor` (`crate::packaged_run`), started as Quit
    /// begins, before every other step.
    run: Option<crate::packaged_run::RunClose>,
    /// The first step that failed, reported once every step ran.
    failed: Option<CleanupError>,
}
impl WindowQuit {
    fn keep(&mut self, error: CleanupError) {
        self.failed.get_or_insert(error);
    }
    /// Lane CHATPAGE (REVIEW-laneCHATPAGE-astra CP-01; patch 3, REVIEW-laneCHATPAGE-p2-astra
    /// CP2-01/CP2-02): the relay is the joined Macs' transport to the owner's exchange, and v0.1.4
    /// closes it only after `demo_app.close` stopped and joined the run (app.py:1818-1825, run
    /// `stop(); join(60)` at demo_app.py:199-203, the relay at app.py:1848-1850 @ f4e366e9), the
    /// teardown having announced `stopped` (demo_chain.py:1381-1390). So its close starts once the
    /// run's close ended, or once the run's join allowance (Quit's own deadline, the same 60 s) ran
    /// out, never earlier; without a run step at once. Nothing waits for any Mac to read
    /// `stopped`: that is best effort, as in v0.1.4.
    fn gate_relay(&mut self) {
        let ended = self
            .run
            .as_ref()
            .is_none_or(crate::packaged_run::RunClose::ended);
        let expired = self
            .deadline
            .is_some_and(|deadline| Instant::now() >= deadline);
        if ended || expired {
            self.relay.begin();
        }
    }
    /// The closes must start now to end within the allowance (also when it is unknown).
    fn closes_due(&self) -> bool {
        self.deadline
            .and_then(|deadline| deadline.checked_sub(self.close_budget))
            .is_none_or(|start| Instant::now() >= start)
    }
}
impl Cleanup for AppCleanup {
    fn bind_clock(&mut self, origin: std::time::Instant) -> Result<(), CleanupError> {
        match self {
            Self::Service(quit) => {
                quit.origin = Some(origin);
                quit.services.bind_clock(origin)
            }
            Self::Nothing => Ok(()),
        }
    }
    fn begin(&mut self, deadline: budgets::Span) -> Result<(), CleanupError> {
        match self {
            Self::Service(quit) => {
                quit.deadline = quit.origin.and_then(|origin| {
                    origin.checked_add(Duration::from_millis(deadline.millis()))
                });
                // The run's join ends at this same deadline (one allowance, not two).
                if let Some(run) = &mut quit.run {
                    run.begin_until(quit.deadline);
                }
                // A refused begin is kept, not returned: the shell stops polling a Quit whose
                // begin failed, and the acquisition must still be closed. The services' own steps
                // (admission fence, loops, live request, node fence) still run.
                if let Err(error) = quit.services.begin(deadline) {
                    eprintln!("window cleanup: begin failed: {error:?}");
                    quit.keep(error);
                    quit.services_done = true;
                    if let Err(also) = desktop::services::Services::begin_shutdown(
                        &*quit.window,
                        quit.deadline.unwrap_or_else(Instant::now),
                    ) {
                        eprintln!("window cleanup: services begin failed: {also:?}");
                    }
                }
                if let Some(swarm) = &quit.swarm {
                    swarm.publishing.ticks.stop();
                }
                quit.model.begin();
                quit.gate_relay();
                Ok(())
            }
            Self::Nothing => Ok(()),
        }
    }
    fn poll(&mut self) -> Result<Poll, CleanupError> {
        match self {
            Self::Service(quit) => {
                quit.gate_relay();
                if !quit.services_done {
                    match quit.services.poll() {
                        // v0.1.4 joins the run for its allowance, then closes whatever the join
                        // found (app.py:1835-1847): the closes start in time to end within it.
                        Ok(Poll::Pending) if !quit.closes_due() => return Ok(Poll::Pending),
                        Ok(Poll::Pending) => {}
                        Ok(Poll::Verified) => quit.services_done = true,
                        Err(error) => {
                            quit.keep(error);
                            quit.services_done = true;
                        }
                    }
                }
                // Both close on their own threads at once; neither waits for the other.
                let swarm = quit.swarm.as_mut().map_or(Some(Ok(())), SwarmClose::poll);
                let model = quit.model.poll();
                let relay = quit.relay.poll();
                let run = quit.run.as_mut().map_or(Some(Ok(())), |run| run.poll());
                let (Some(swarm), Some(relay), Some(run)) = (swarm, relay, run) else {
                    return Ok(Poll::Pending);
                };
                if model == Ok(Poll::Pending) {
                    return Ok(Poll::Pending);
                }
                for step in [
                    run.map(|()| Poll::Verified),
                    swarm.map(|()| Poll::Verified),
                    model,
                    relay.map(|()| Poll::Verified),
                ] {
                    if let Err(error) = step {
                        quit.keep(error);
                    }
                }
                // The closes ran with the services still running: a failed close is reported now;
                // else the services keep the rest of the allowance, then their deadline is reported.
                if !quit.services_done && quit.failed.is_none() {
                    if quit
                        .deadline
                        .is_some_and(|deadline| Instant::now() < deadline)
                    {
                        return Ok(Poll::Pending);
                    }
                    return Err(CleanupError::Deadline);
                }
                quit.failed.map_or(Ok(Poll::Verified), Err)
            }
            Self::Nothing => Ok(Poll::Verified),
        }
    }
    /// At the deadline the window may exit only once the run's workflow Stop was issued
    /// (`workflow.stop()` after `run.join(60)`, app.py:1835-1838): issued now unless it was. The
    /// run's join allowance ran out, so the relay's close starts now if it did not (v0.1.4 goes on
    /// to `public_relay.close()` after the join, app.py:1848-1850), and the window exits only once
    /// it ended or its own close budgets ran out (`RelayClose::poll`). A relay close that failed or
    /// overran those budgets is reported (`relay_stop_failed`, exit 3: REVIEW-laneCHATPAGE-p3-astra
    /// CP3-01); only the run's join running out is the exit-0 `Deadline`.
    ///
    /// Lane B, B5/QV (row 44): the workflow Stop runs on its own thread, so one that never returns
    /// cannot hold the window: it is waited for within the same post-deadline window as the relay's
    /// close (the relay's own close budgets from the deadline), then reported
    /// `Unproven("workflow_stop_unreturned")`.
    fn expire(&mut self) -> Result<Poll, CleanupError> {
        match self {
            Self::Service(quit) => {
                quit.relay.begin();
                let run = quit
                    .run
                    .as_mut()
                    .map_or(Ok(Poll::Verified), crate::packaged_run::RunClose::expire)?;
                let relay = quit.relay.poll();
                if run == Poll::Pending {
                    let bound = quit
                        .deadline
                        .zip(crate::packaged_network::RelayClose::allowance().ok());
                    if bound
                        .is_none_or(|(deadline, allowance)| Instant::now() >= deadline + allowance)
                    {
                        eprintln!("owner run: workflow_stop_unreturned");
                        return Err(CleanupError::Unproven("workflow_stop_unreturned"));
                    }
                    return Ok(Poll::Pending);
                }
                match relay {
                    None => Ok(Poll::Pending),
                    Some(Err(error)) => Err(error),
                    Some(Ok(())) => Ok(Poll::Verified),
                }
            }
            Self::Nothing => Ok(Poll::Verified),
        }
    }
}
#[cfg(test)]
impl AppCleanup {
    /// TEST ONLY (lane B, CP3-01): the relay step's close thread waits for the returned sender
    /// (a send or its drop) before it closes anything.
    pub(crate) fn hold_relay_close(&mut self) -> std::sync::mpsc::Sender<()> {
        let (release, held) = std::sync::mpsc::channel();
        if let Self::Service(quit) = self {
            quit.relay.hold(held);
        }
        release
    }
}
/// Quit's listing step, `swarm_publish.close` (swarm_publish.py:450-455 @ f4e366e9, called from
/// `DemoApp.close` before the acquisition, app.py:1824-1829), as the headless launcher runs it
/// (reconcile/laneB12f.md): `close(now, budget)` on its own thread (no new exchange starts, a live
/// listing becomes durable withdrawal debt, one withdrawal attempt bounded by the budget,
/// `SwarmpublisherCloseSelfThreadJoin` 5 s); its result is kept; the tick loop is reaped once it
/// ended, waited for only until the close deadline, and one still inside a directory exchange is
/// left to the process exit (its state is durable before the exchange). Only a storage failure
/// fails the close (stderr `swarm_publish_stop_failed: <code>`).
struct SwarmClose {
    publishing: Publishing,
    closing: SwarmClosing,
}
enum SwarmClosing {
    NotStarted,
    Running {
        worker: JoinHandle<Result<(), String>>,
        until: Instant,
    },
    /// The close has its result; the tick loop is awaited until `until`.
    Waiting {
        result: Result<(), CleanupError>,
        until: Instant,
    },
    Done(Result<(), CleanupError>),
}
impl SwarmClose {
    /// `None` while pending, else the step's result (kept: every later poll answers it again).
    fn poll(&mut self) -> Option<Result<(), CleanupError>> {
        match std::mem::replace(&mut self.closing, SwarmClosing::Done(Ok(()))) {
            SwarmClosing::NotStarted => {
                let budget = match BudgetId::PyMyceliumA13MacosSwarmPublishSwarmpublisherCloseSelfThreadJoinL453
                    .span(Entries::ZERO)
                {
                    Ok(budget) => Duration::from_millis(budget.millis()),
                    Err(_) => {
                        eprintln!("swarm_publish_stop_failed: swarm_close_budget_invalid");
                        self.closing = SwarmClosing::Done(Err(CleanupError::InvalidBudget));
                        return Some(Err(CleanupError::InvalidBudget));
                    }
                };
                let until = Instant::now() + budget;
                let publisher = self.publishing.publisher.clone();
                match std::thread::Builder::new()
                    .name("packaged-swarm-close".into())
                    .spawn(move || {
                        publisher
                            .close(crate::app_host::unix_seconds(), budget)
                            .map(|_closed| ())
                            .map_err(|error| error.code)
                    }) {
                    Ok(worker) => {
                        self.closing = SwarmClosing::Running { worker, until };
                        None
                    }
                    Err(_) => {
                        eprintln!("swarm_publish_stop_failed: swarm_close_thread_unavailable");
                        self.closing = SwarmClosing::Waiting {
                            result: Err(CleanupError::Poll),
                            until,
                        };
                        None
                    }
                }
            }
            SwarmClosing::Running { worker, until } if !worker.is_finished() => {
                self.closing = SwarmClosing::Running { worker, until };
                None
            }
            SwarmClosing::Running { worker, until } => {
                let result = match worker.join() {
                    Ok(Ok(())) => Ok(()),
                    Ok(Err(code)) => {
                        eprintln!("swarm_publish_stop_failed: {code}");
                        Err(CleanupError::Poll)
                    }
                    Err(_) => {
                        eprintln!("swarm_publish_stop_failed: swarm_close_panicked");
                        Err(CleanupError::Poll)
                    }
                };
                self.closing = SwarmClosing::Waiting { result, until };
                self.poll()
            }
            SwarmClosing::Waiting { result, until } => match self.publishing.ticks.poll_join() {
                Ok(false) if Instant::now() < until => {
                    self.closing = SwarmClosing::Waiting { result, until };
                    None
                }
                // Ended and joined, or still inside an exchange at the deadline: left to exit.
                Ok(_) => {
                    self.closing = SwarmClosing::Done(result);
                    Some(result)
                }
                Err(code) => {
                    eprintln!("swarm_publish_stop_failed: {code}");
                    let result = result.and(Err(CleanupError::Poll));
                    self.closing = SwarmClosing::Done(result);
                    Some(result)
                }
            },
            SwarmClosing::Done(result) => {
                self.closing = SwarmClosing::Done(result);
                Some(result)
            }
        }
    }
}
/// Quit's acquisition step, `DemoApp.close` (app.py:1845-1847 @ f4e366e9):
/// `self.acquisition.close()` cancels and joins the running attempt and persists `closed`, so the
/// next launch answers `closed` with the bytes kept, never a recovered interrupted transfer. The
/// attempt is asked to stop as soon as Quit begins; the close runs on its own thread once the
/// window's services finished, verified or failed (no new request is admitted then), and its
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
    core_with_network(state_root, transport, observer, NetworkConfig::default())
}
/// [`core_with`] with the host's network over `network`: the relay's hooks and v0.1.4's join seams
/// (`DemoApp(_test_join_base_url=...)`, a test's loopback relay and the owner's local seed).
pub fn core_with_network(
    state_root: &Path,
    transport: Arc<dyn Transport>,
    observer: Arc<dyn Observer>,
    network: NetworkConfig,
) -> Result<(Arc<dyn Core>, AppCleanup), String> {
    compose(
        state_root,
        ModelSource::new(state_root.to_owned(), transport).with_observer(observer),
        network,
        RunConfig::default(),
    )
}
pub use crate::packaged_run::RunConfig;
/// [`core_with_network`] with the owner run's composition seams given.
pub fn core_with_run(
    state_root: &Path,
    transport: Arc<dyn Transport>,
    observer: Arc<dyn Observer>,
    network: NetworkConfig,
    run: RunConfig,
) -> Result<(Arc<dyn Core>, AppCleanup), String> {
    compose(
        state_root,
        ModelSource::new(state_root.to_owned(), transport).with_observer(observer),
        network,
        run,
    )
}
/// Bind the current installed executable to the entry already checked by bundle
/// admission. Expected bytes come from that startup manifest, never a spawn-time
/// measurement. The locally installed app is the trust root, not a publisher pin.
pub fn admitted_native_program(
    verified: &desktop::package::VerifiedBundle,
    entry: &desktop::package::Entry,
    executable: &Path,
) -> Result<api::native_compiler::NativeProgram, String> {
    if entry.kind != desktop::package::Kind::Executable
        || entry.path != "Contents/MacOS/mycelium"
        || executable != verified.executable()
    {
        return Err("native_executable_admission_mismatch".into());
    }
    api::native_compiler::NativeProgram {
        binary: executable.into(),
        expected: api::compiled_handoff::Pin {
            bytes: entry.bytes,
            sha256: entry.sha256.clone(),
        },
    }
    .admit(&|| false)
    .map_err(|error| error.0.into())
}

/// Production entrypoint after bundle admission, with explicit native identity.
pub fn core_with_native_program(
    state_root: &Path,
    native_program: api::native_compiler::NativeProgram,
) -> Result<(Arc<dyn Core>, AppCleanup), String> {
    compose(
        state_root,
        ModelSource::hub(state_root.to_owned()),
        NetworkConfig::default(),
        RunConfig {
            native_program: Some(native_program),
            ..RunConfig::default()
        },
    )
}

/// The core and cleanup for the window, from the state root. No native compiler
/// program is available unless the admitted executable entrypoint supplies it.
pub fn core(state_root: &Path) -> Result<(Arc<dyn Core>, AppCleanup), String> {
    compose(
        state_root,
        ModelSource::hub(state_root.to_owned()),
        NetworkConfig::default(),
        RunConfig::default(),
    )
}
fn compose(
    state_root: &Path,
    source: ModelSource,
    network: NetworkConfig,
    run: RunConfig,
) -> Result<(Arc<dyn Core>, AppCleanup), String> {
    let dirs = prepare_dirs(state_root)?;
    let private = dirs.private.clone();
    // The chat's timing file is admitted before anything is composed (lane CHAT): a refused path
    // fails the composition by name, and `with_chat_run` below admits the same path again.
    let timing = dirs.chat_timing.clone();
    if let Some(node_root) = private.parent() {
        api::chat_service::TimingFile::under(node_root, &timing)
            .map_err(|e| format!("compose:{}", e.code))?;
    }
    let node = NodeService::new(
        Arc::new(NoLocalChat),
        crate::service::random_id().map_err(|e| format!("compose:{}", e.code))?,
        crate::native::development_qualification().map_err(|e| format!("compose:{e}"))?,
    );
    // `DemoApp.swarm_publisher`: the listing custody's state beside the owner's private state.
    let publisher = Arc::new(api::swarm::publisher::app::AppPublisher::new(&private));
    // `DemoApp.owner_run` (lane PH3b S3, `crate::packaged_run`): composed with the host; Quit
    // stops and joins it first (`DemoApp.close` -> `demo_app.close`, app.py:1815-1825).
    let services = DesktopServices::packaged_with_run(
        node,
        CALLBACK_CAPACITY,
        &private,
        source.clone(),
        publisher,
        network,
        run.clone(),
    );
    // `app.chat` over `app.owner_run` (chat.py:513-518, app.py:1926): every `/chat/*` request goes
    // through the shipped chat service and the run's route, always (lane CHAT, ledger row 17).
    let services = crate::packaged_run::chat_run(services, &run, timing)
        .map_err(|e| format!("compose:{}", e.code))?;
    let (core, mut cleanup) = assemble(services, source);
    close_run_first(&mut cleanup, run);
    Ok((core, cleanup))
}
/// Quit's run step (`crate::packaged_run::RunClose`) over the composed `run`, begun before every
/// other step of the window's Quit.
pub(crate) fn close_run_first(cleanup: &mut AppCleanup, run: RunConfig) {
    if let AppCleanup::Service(quit) = cleanup {
        quit.run = Some(crate::packaged_run::RunClose::new(run));
    }
}
/// The window's core and its Quit cleanup over composed `services` and the run model's `source`
/// (the same source the services' packaged host was given): the composition's last step, also
/// what a test runs over services it composed with its own driver.
pub fn assemble(services: DesktopServices, source: ModelSource) -> (Arc<dyn Core>, AppCleanup) {
    let swarm = services.publishing().map(|publishing| SwarmClose {
        publishing,
        closing: SwarmClosing::NotStarted,
    });
    let swarm_budget = swarm.as_ref().map_or(Duration::ZERO, |_| {
        BudgetId::PyMyceliumA13MacosSwarmPublishSwarmpublisherCloseSelfThreadJoinL453
            .span(Entries::ZERO)
            .map_or_else(
                |_| {
                    // Unknown: the closes start at once.
                    eprintln!("window cleanup: swarm_close_budget_invalid");
                    Duration::MAX
                },
                |budget| Duration::from_millis(budget.millis()),
            )
    });
    let close_budget = swarm_budget.max(source.close_patience());
    let relay = crate::packaged_network::RelayClose::new(services.network());
    let window = Arc::new(services);
    let (core, cleanup) = desktop::services::adapt(window.clone(), SERVICE_CAPACITY);
    (
        core,
        AppCleanup::Service(Box::new(WindowQuit {
            services: cleanup,
            window,
            origin: None,
            deadline: None,
            close_budget,
            services_done: false,
            swarm,
            model: ModelClose::new(source),
            relay,
            run: None,
            failed: None,
        })),
    )
}
