// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged executable's `api::persistent::Host`: the facts this process really has for the
//! desktop action backend (S1 root A). `PersistentBackend` owns the private preference files
//! (`/api/fast-mode`, `/api/run-model`, app.py:1356-1361 @ f4e366e9) and asks the host only for
//! what v0.1.4 read from `os.environ` and the app object. The owner authority (`/api/create`,
//! authority start/stop, the pairing request/approve/accept seams and the owner's pairing
//! windows) is `api::app_host`, the same code the headless L3 launcher runs, and the host keeps
//! the owner's lease renewed with that library's pass (`_renew_loop`, [`PackagedHost`]); the packaged
//! process has no relay and no public origin, so its seed answers on its own loopback address
//! and a pairing code asked for waits for a relay (app.py:1703-1710). The run model's acquisition
//! (`/api/model/download`, `/api/model/cancel`, the re-open after a run-model choice) is
//! `api::app_host`'s too, over the [`ModelSource`] the packaged composition gives the host, and
//! the download is guarded by this Mac's resource observation against the owner's saved consent
//! (`_resource_check`, [`PackagedHost`]'s `resource_check`). Every other seam keeps the trait
//! default, so an action whose producer is not ported yet (join, consent binding, serving
//! compile) answers the host's 409 `action_not_completed`.
//! `/api/serving/status` keeps the trait default block on purpose: it is v0.1.4's own answer
//! (app.py:1377-1399 @ f4e366e9; `{compilation: null, serving: null, route_ready: false}` until a
//! chain compile starts, chain_compile.py:197), executed on the real packaged process in
//! `test-support/journeys/fixtures/python-v014-boundaries.json` (`no_model_no_execution_proposal`).
use crate::packaged_run::Authority;
use crate::service::NodeService;
use api::{
    acquisition::{Acquisition, HttpTransport, Transport},
    actions::{Action, Backend as _},
    app_host::{self, AcquisitionSite, AcquisitionView},
    persistent::{Host, TOKEN_PATH_ENV},
    preferences::{fast_mode, resource_policy, run_model},
    resources::{self, Observation, Observer, ProbeObserver},
    schema::PublicError,
    swarm::publisher::app::{AppPublisher, OwnerFacts},
    wire::Json,
};
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value};
use std::{
    path::{Path, PathBuf},
    sync::{Arc, Mutex, MutexGuard, PoisonError, mpsc},
    thread::JoinHandle,
    time::Duration,
};

/// v0.1.4's process default for the next run's fast mode, `MYCELIUM_TOKEN_PATH_APP_DEFAULT`
/// (`fast_mode.bind_owner_process`, fast_mode.py:73-92, bound by the dedicated launcher at
/// `__main__.py:176-177`): `installation_role == 'owner' and document(root)['enabled']`, computed
/// at bind and again by `_refresh_fast_mode_process()` after every saved change. This process
/// keeps the value here instead of rewriting its own environment. Its consumer, the granted token
/// path, is off the S1 default path (fast mode defaults to off, fast_mode.py:36-37).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FastModeDefault {
    /// `"on"`/`"off"` as the launcher exports it, or the code `document` raised.
    pub value: Result<&'static str, &'static str>,
    /// Refreshes since the host was built, the bind included.
    pub refreshes: u64,
}
/// Shared view of the [`FastModeDefault`] the host refreshes.
#[derive(Clone)]
pub struct FastModeProcess(Arc<Mutex<FastModeDefault>>);
impl FastModeProcess {
    pub fn read(&self) -> FastModeDefault {
        // A plain value: a panicked writer cannot leave it half-written.
        *self.0.lock().unwrap_or_else(PoisonError::into_inner)
    }
    fn refresh(&self, root: &Path, owner: bool, environment: Option<&str>) {
        // `owner and document(...)`: a target never reads the document.
        let value = if owner {
            fast_mode::document(root, environment).map(|document| {
                if document["enabled"] == true {
                    "on"
                } else {
                    "off"
                }
            })
        } else {
            Ok("off")
        };
        let mut cell = self.0.lock().unwrap_or_else(PoisonError::into_inner);
        cell.value = value;
        cell.refreshes = cell.refreshes.saturating_add(1);
    }
}

/// `DemoApp.acquisition`, `DemoApp._acquisition_active`, `DemoApp.acquisition_policy_error` and
/// `DemoApp.resource_observation` (`None` until the first check: `UNKNOWN_OBSERVATION`, app.py:563).
#[derive(Default)]
struct Acquisitions {
    acquisition: Option<Acquisition>,
    active: bool,
    policy_error: Option<&'static str>,
    observation: Option<Observation>,
}
/// The packaged process's model source: `DemoApp.acquisition` (`_open_acquisition`, app.py:649-672
/// @ f4e366e9) over v0.1.4's `model-source-*` directories in the state root, beside the private
/// `v02-node/`, fetched through one repository transport, and `DemoApp._policy_observer`, this
/// Mac's power, thermal state and network. Clones share one state: the packaged host opens, starts
/// and cancels the attempts through `api::app_host`, and the packaged core closes the acquisition
/// at Quit.
#[derive(Clone)]
pub struct ModelSource {
    sources: PathBuf,
    transport: Arc<dyn Transport>,
    observer: Arc<dyn Observer>,
    state: Arc<Mutex<Acquisitions>>,
}
impl ModelSource {
    /// `sources` is the state root the `model-source-*` directories live in. This Mac is observed
    /// by the helper bundled beside the executable (`native/resource_probe`,
    /// `ProbeObserver::beside_executable`, resources.py `_resource_probe_candidates`); without one
    /// every observation is unknown and the resource guard refuses, as v0.1.4 does when its
    /// observer fails (fail closed).
    pub fn new(sources: PathBuf, transport: Arc<dyn Transport>) -> Self {
        Self {
            sources,
            transport,
            observer: Arc::new(ProbeObserver::beside_executable()),
            state: Arc::default(),
        }
    }
    /// This source with `observer` in place of the bundled helper (a test controls what this Mac
    /// reports, as v0.1.4's tests replace `DemoApp._policy_observer`).
    pub fn with_observer(mut self, observer: Arc<dyn Observer>) -> Self {
        self.observer = observer;
        self
    }
    /// The public Hugging Face hub (`_HTTPRepositoryTransport`), refused before any socket while
    /// `MYCELIUM_OFFLINE` is on (`mycelium_platform_neutral.offline`: `1`/`true`/`yes`/`on`).
    pub fn hub(sources: PathBuf) -> Self {
        let offline = std::env::var_os("MYCELIUM_OFFLINE").is_some_and(|value| {
            matches!(
                value.to_string_lossy().to_ascii_lowercase().as_str(),
                "1" | "true" | "yes" | "on"
            )
        });
        Self::new(sources, Arc::new(HttpTransport::hugging_face(offline)))
    }
    /// This process's acquisition of the model pinned at `revision` prepared it: downloaded,
    /// verified and accepted by the native manifest compile (`prepared_source`'s precondition,
    /// acquisition.py:1177-1184). A status recovered from disk is never `prepared` here.
    pub fn prepared(&self, revision: &str) -> bool {
        self.lock().acquisition.as_ref().is_some_and(|acquisition| {
            acquisition.model().resolved_commit == revision
                && acquisition.verified_snapshot().is_some()
        })
    }
    /// `app.resource_observation`: the last observation the resource guard made, unknown before
    /// the first.
    pub fn resource_observation(&self) -> Observation {
        self.lock()
            .observation
            .clone()
            .unwrap_or_else(Observation::unknown)
    }
    /// The run model's id of the open acquisition (`self.acquisition.model`), none before one.
    fn model_id(&self) -> Option<&'static str> {
        self.lock()
            .acquisition
            .as_ref()
            .map(|acquisition| acquisition.model().model_id)
    }
    /// `app.acquisition_policy_error`: the resource guard's last refusal, cleared by a started
    /// attempt or a run-model choice.
    pub fn policy_error(&self) -> Option<&'static str> {
        self.lock().policy_error
    }
    /// One observation of this Mac, kept as `resource_observation` (`self.resource_observation =
    /// observation`, app.py:941). The helper runs outside the state's lock.
    fn observe(&self) -> Observation {
        let observation = self.observer.observe();
        self.lock().observation = Some(observation.clone());
        observation
    }
    /// `app._resource_check()` (app.py:929-947 @ f4e366e9): this Mac observed now, against the
    /// saved resource policy of the owner whose private root is `root` (`read_resource_policy(
    /// self.root) or {}`), by `resources::policy_violation` (resources.py:152-181).
    fn resource_violation(&self, root: &Path) -> Option<&'static str> {
        let observation = self.observe();
        let policy = resource_policy::read(root)
            .ok()
            .flatten()
            .unwrap_or_else(|| Value::Object(Map::new()));
        resources::policy_violation(&policy, &observation)
    }
    /// One pass of `DemoApp._policy_loop` (app.py:1007-1030 @ f4e366e9) for the owner whose
    /// private root is `root`, over `authority` (none: its state was refused at bind, so there
    /// is no membership). Only an active attempt is judged (`api::app_host`'s shared verdict): an
    /// ended one clears the flag; a lost membership or this Mac no longer satisfying the saved
    /// consent cancels the real worker and keeps the reason as `acquisition_policy_error`. The
    /// membership and this Mac are read with no lock of this source held; the verdict is applied
    /// only to the attempt it judged, if it is still the active one.
    fn policy_tick(&self, root: &Path, authority: Option<&Mutex<Authority>>) {
        let attempt = {
            let state = self.lock();
            state.acquisition.clone().filter(|_| state.active)
        };
        let Some(attempt) = attempt else {
            return;
        };
        let verdict = app_host::acquisition_policy_verdict(
            &attempt.status().state,
            || {
                authority.is_some_and(|authority| {
                    app_host::running(authority)
                        && app_host::membership_state(authority).as_deref() == Some("current")
                })
            },
            || self.resource_violation(root),
        );
        let mut state = self.lock();
        let judged = state.active
            && state
                .acquisition
                .as_ref()
                .is_some_and(|current| current.state_root() == attempt.state_root());
        if judged {
            let Acquisitions {
                acquisition,
                active,
                policy_error,
                ..
            } = &mut *state;
            app_host::apply_policy_verdict(
                &mut AcquisitionView {
                    acquisition,
                    active,
                    policy_error,
                },
                verdict,
            );
        }
    }
    /// Quit's first step, at once: the running attempt is asked to stop, as `close()` sets the
    /// attempt's cancel event before it joins (acquisition.py:1527-1534); `_acquisition_active`
    /// is cleared (`_cancel_acquisition`). Nothing waits here.
    pub(crate) fn cancel(&self) {
        let mut state = self.lock();
        let Acquisitions {
            acquisition,
            active,
            policy_error,
            ..
        } = &mut *state;
        app_host::cancel_acquisition(&mut AcquisitionView {
            acquisition,
            active,
            policy_error,
        });
    }
    /// `self.acquisition.close()` at Quit (`DemoApp.close`, app.py:1845-1847 @ f4e366e9;
    /// acquisition.py:1524-1549): cancel and join the running attempt, persist `closed`, release
    /// the process lock. Blocks up to the library's own join patience; the library's code when it
    /// cannot close. Without an acquisition (it could not be opened) there is nothing to close.
    pub(crate) fn close(&self) -> Result<(), String> {
        let acquisition = {
            let mut state = self.lock();
            state.active = false;
            state.acquisition.clone()
        };
        acquisition.map_or(Ok(()), |acquisition| {
            acquisition.close().map_err(|e| e.code().to_owned())
        })
    }
    /// How long Quit's [`Self::close`] may block: `thread.join(timeout=float(timeout) + 2.0)`
    /// (acquisition.py:1535-1541 @ f4e366e9), the transport's timeout and the join slack.
    pub(crate) fn close_patience(&self) -> Duration {
        let slack = BudgetId::AcquisitionCloseJoinSlack
            .span(Entries::ZERO)
            .map_or_else(
                |_| {
                    // Unknown: Quit starts the close at once.
                    eprintln!("model source: acquisition_close_budget_invalid");
                    Duration::MAX
                },
                |slack| Duration::from_millis(slack.millis()),
            );
        self.transport.timeout().saturating_add(slack)
    }
    fn lock(&self) -> MutexGuard<'_, Acquisitions> {
        // Plain values and a cloneable handle: a panicked holder cannot leave them half-written.
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }
    /// A joined Mac's `_ensure_whole_model` (demo_target.py:600-607): save the owner's announced
    /// choice for this root, open THIS Mac's own acquisition (the target cache) and start it under
    /// the standing consent's cap — the two actions v0.1.4 posted to its own `/api/run-model` and
    /// `/api/model/download`. Returning a refusal here is what used to leave a joined Mac with no
    /// snapshot unable to serve at all (`member_model_download_not_ported`).
    pub fn start_member_whole_model(&self, root: &Path, choice: &Value) -> Result<(), String> {
        let name = choice
            .as_str()
            .ok_or_else(|| "demo_model_choice_unknown".to_owned())?;
        run_model::save_choice(root, name).map_err(|e| e.code.to_owned())?;
        let cap = api::preferences::standing_consent::load(root)
            .ok()
            .flatten()
            .and_then(|grant| grant.get("caps").and_then(|c| c["storage_bytes"].as_u64()))
            .ok_or_else(|| "standing_consent_required".to_owned())?;
        let consent = serde_json::json!({
            "download_acknowledged": true,
            "max_download_bytes": cap,
            "storage_limit_bytes": cap,
        });
        let transport = || self.transport.clone();
        let site = AcquisitionSite {
            root,
            sources: &self.sources,
            target: true,
            run_model_env: None,
            transport: &transport,
        };
        let mut state = self.lock();
        let Acquisitions {
            acquisition,
            active,
            policy_error,
            ..
        } = &mut *state;
        let mut view = AcquisitionView {
            acquisition,
            active,
            policy_error,
        };
        app_host::open_acquisition(&mut view, &site).map_err(|e| e.code.to_owned())?;
        app_host::acquisition_start(&mut view, &consent)
            .map(|_| ())
            .map_err(|e| e.code.to_owned())
    }
    /// One `api::app_host` acquisition step for the owner whose private root is `root`.
    fn step<R>(
        &self,
        root: &Path,
        run_model_env: Option<&str>,
        step: impl FnOnce(&mut AcquisitionView<'_>, &AcquisitionSite<'_>) -> R,
    ) -> R {
        let transport = || self.transport.clone();
        let site = AcquisitionSite {
            root,
            sources: &self.sources,
            target: false,
            run_model_env,
            transport: &transport,
        };
        let mut state = self.lock();
        let Acquisitions {
            acquisition,
            active,
            policy_error,
            ..
        } = &mut *state;
        step(
            &mut AcquisitionView {
                acquisition,
                active,
                policy_error,
            },
            &site,
        )
    }
    /// `app._acquisition_active`. A finished attempt also clears the flag when it is read, so an
    /// answer never waits for the next policy pass (app.py:1016-1018).
    fn active(&self) -> bool {
        let mut state = self.lock();
        let finished = state.acquisition.as_ref().is_some_and(|acquisition| {
            matches!(
                acquisition.status().state.as_str(),
                "prepared" | "cancelled" | "failed"
            )
        });
        if state.active && finished {
            state.active = false;
        }
        state.active
    }
}

/// One of the packaged host's own loops on its named thread (`DemoApp.policy_thread`): `tick`
/// every `period` until it is stopped or the node service's shutdown fence (`self.closed`) is
/// set. The window's Quit stops it at once and reaps it without blocking; dropping the last
/// handle stops it and joins it as [`DropJoin`] says.
pub struct HostLoop {
    /// Dropped to wake the loop (`self.closed.wait(1)` returning).
    stop: Mutex<Option<mpsc::Sender<()>>>,
    thread: Mutex<Option<JoinHandle<()>>>,
    /// Disconnected once the loop's thread has returned (its sender is dropped with it).
    ended: Mutex<mpsc::Receiver<()>>,
    on_drop: DropJoin,
}
/// What dropping the last handle waits for.
#[derive(Clone, Copy)]
enum DropJoin {
    /// Join the loop.
    Always,
    /// Off for a loop whose pass may wait on another process (a directory exchange): it is
    /// stopped and left to end on its own.
    IfEnded,
    /// Join it for at most this long, then leave it (`thread.join(timeout)`).
    Within(Duration),
}
impl HostLoop {
    pub(crate) fn start(
        name: &str,
        period: BudgetId,
        node: &NodeService,
        join_on_drop: bool,
        tick: impl FnMut() + Send + 'static,
    ) -> Result<Arc<Self>, &'static str> {
        let on_drop = if join_on_drop {
            DropJoin::Always
        } else {
            DropJoin::IfEnded
        };
        Self::spawn(name, period, node, on_drop, tick)
    }
    /// [`HostLoop::start`] for a loop whose last handle joins it for at most `join` (the
    /// owner run's status loop: `PublishedStatus.stop` joins for 2 s, published_status.py:78-81).
    pub(crate) fn start_joined_within(
        name: &str,
        period: BudgetId,
        node: &NodeService,
        join: BudgetId,
        tick: impl FnMut() + Send + 'static,
    ) -> Result<Arc<Self>, &'static str> {
        let join = join
            .span(Entries::ZERO)
            .map_err(|_| "host_loop_budget_invalid")?;
        let on_drop = DropJoin::Within(Duration::from_millis(join.millis()));
        Self::spawn(name, period, node, on_drop, tick)
    }
    fn spawn(
        name: &str,
        period: BudgetId,
        node: &NodeService,
        on_drop: DropJoin,
        mut tick: impl FnMut() + Send + 'static,
    ) -> Result<Arc<Self>, &'static str> {
        let period = period
            .span(Entries::ZERO)
            .map_err(|_| "host_loop_budget_invalid")?;
        let period = Duration::from_millis(period.millis());
        let (stop, stopped) = mpsc::channel::<()>();
        let (alive, ended) = mpsc::channel::<()>();
        let node = node.clone();
        let thread = std::thread::Builder::new()
            .name(name.into())
            .spawn(move || {
                // Dropped when this thread returns: the drop's bounded join hears it.
                let _alive = alive;
                while let Err(mpsc::RecvTimeoutError::Timeout) = stopped.recv_timeout(period) {
                    // Quit's fence; a poisoned node counts as closed.
                    if node.closed().unwrap_or(true) {
                        break;
                    }
                    tick();
                }
            })
            .map_err(|_| "host_loop_thread_unavailable")?;
        Ok(Arc::new(Self {
            stop: Mutex::new(Some(stop)),
            thread: Mutex::new(Some(thread)),
            ended: Mutex::new(ended),
            on_drop,
        }))
    }
    /// Wake the loop and end it; a pass in progress finishes first.
    pub fn stop(&self) {
        drop(
            self.stop
                .lock()
                .unwrap_or_else(PoisonError::into_inner)
                .take(),
        );
    }
    /// Reap the loop without blocking: `Ok(true)` once it ended and was joined, `Ok(false)`
    /// while it still runs, the code when it panicked.
    pub fn poll_join(&self) -> Result<bool, &'static str> {
        let mut thread = self.thread.lock().unwrap_or_else(PoisonError::into_inner);
        match thread.take() {
            None => Ok(true),
            Some(running) if !running.is_finished() => {
                *thread = Some(running);
                Ok(false)
            }
            Some(ended) => ended
                .join()
                .map(|()| true)
                .map_err(|_| "host_loop_panicked"),
        }
    }
}
impl Drop for HostLoop {
    fn drop(&mut self) {
        self.stop();
        let thread = self
            .thread
            .get_mut()
            .unwrap_or_else(PoisonError::into_inner)
            .take();
        let join = match self.on_drop {
            DropJoin::Always => true,
            DropJoin::IfEnded => false,
            DropJoin::Within(span) => {
                let ended = self.ended.get_mut().unwrap_or_else(PoisonError::into_inner);
                let within = matches!(
                    ended.recv_timeout(span),
                    Err(mpsc::RecvTimeoutError::Disconnected)
                );
                if !within {
                    eprintln!("packaged host loop: host_loop_join_incomplete");
                }
                within
            }
        };
        if let Some(thread) = thread.filter(|t| join || t.is_finished())
            && thread.join().is_err()
        {
            eprintln!("packaged host loop: host_loop_panicked");
        }
    }
}

/// The owner's listing custody (`DemoApp.swarm_publisher`, `swarm_publish.py` @ f4e366e9, as
/// `api`'s `AppPublisher`) and its own tick loop (`SwarmPublisher.start`, swarm_publish.py:437-448).
/// Quit closes the custody (`close(now, 5 s)`) and waits for the loop only until that deadline.
#[derive(Clone)]
pub struct Publishing {
    pub publisher: Arc<AppPublisher>,
    pub ticks: Arc<HostLoop>,
}
/// The status's `swarm` field (`_swarm_status`, app.py:430-436, and `swarm_publish.status`,
/// swarm_publish.py:524-530 @ f4e366e9) of this process, which is always the owner
/// ([`PackagedHost`]'s `owner`): the custody's own status, or `{enabled: false, mode: null}` for a
/// host without one. Never blocks on directory work.
pub fn swarm_status(publishing: Option<&Publishing>) -> Value {
    publishing.map_or_else(
        || serde_json::json!({"enabled": false, "mode": null}),
        |publishing| publishing.publisher.status_json(),
    )
}
/// Whole seconds of wall time for the signed directory documents.
pub fn unix_seconds() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| i64::try_from(d.as_secs()).unwrap_or(i64::MAX))
}

/// The packaged process as `persistent::Host`. It holds the same [`NodeService`] that is the
/// backend's inner delegate (one shared state), the private state root, the fast-mode default,
/// the owner authority over that root and the loop that keeps the owner's lease renewed.
pub struct PackagedHost {
    node: NodeService,
    root: PathBuf,
    fast_mode: FastModeProcess,
    /// `DemoApp.policy_thread` (`_policy_loop`) over the model source, shared with the window's
    /// Quit (which stops and reaps it). Declared before the authority, like the renewal.
    policy: Option<Arc<HostLoop>>,
    /// `DemoApp.renew_thread` over the authority below, held for its drop. Declared before the
    /// authority: dropping the host ends and joins the loop before the authority (and the seed it
    /// keeps) is released.
    _renewal: Renewal,
    /// `DemoApp.authority` (`api::app_host`), loaded from the private root at bind. A state that
    /// cannot be read or resumed keeps its code: every authority seam refuses with it and nothing
    /// is written over it.
    authority: Result<Arc<Mutex<Authority>>, &'static str>,
    /// `DemoApp.acquisition` over the state root's model sources. `None`: this host was built
    /// without one, and every acquisition seam keeps the trait default.
    model: Option<ModelSource>,
    /// The owner's listing custody. `None`: the swarm routes keep the trait default.
    publishing: Option<Publishing>,
    /// `DemoApp.public_relay`, the pending windows and the join seams (`crate::packaged_network`).
    network: Arc<crate::packaged_network::Network>,
    /// `_renew_loop` for a joined target over the network's member cell (none before
    /// [`PackagedHost::with_network`]). Quit stops and reaps it with the host's other loops.
    member_renewal: Option<Arc<HostLoop>>,
    /// `DemoApp.owner_run` (lane PH3b S3, `crate::packaged_run`); none before [`PackagedHost::with_run`].
    run: Option<crate::packaged_run::OwnerRun>,
    /// `DemoApp.standing_thread` (`_standing_loop`) for a joined target (none before
    /// [`PackagedHost::with_member_agent`]). Quit stops and reaps it with the host's other loops.
    member_agent: Option<Arc<HostLoop>>,
}
/// `DemoApp.renew_thread` (app.py:638-639) running `_renew_loop` (app.py:911-927 @ f4e366e9) for
/// the packaged owner: every period (`RenewLoopSelfClosedWait`, app.py:917) the owner pass
/// `app_host::renew_owner` renews a running owner's lease through its own seed, taking the
/// authority's lock one step at a time. A stopped authority has no seed and no session, so its
/// passes renew nothing. The node service's shutdown fence (`self.closed`) ends the loop, and so
/// does the host's drop, which wakes it at once and joins it.
struct Renewal {
    /// Dropped to wake the loop (`self.closed.wait(20)` returning).
    stop: Option<mpsc::Sender<()>>,
    thread: Option<JoinHandle<()>>,
}
impl Renewal {
    fn idle() -> Self {
        Self {
            stop: None,
            thread: None,
        }
    }
    /// The loop over `authority` (none when the state was refused at bind: no owner to renew).
    fn start(
        authority: Option<&Arc<Mutex<Authority>>>,
        node: &NodeService,
        period: BudgetId,
    ) -> Self {
        let Some(authority) = authority else {
            return Self::idle();
        };
        let Ok(period) = period.span(Entries::ZERO) else {
            eprintln!("owner renewal: owner_renewal_budget_invalid");
            return Self::idle();
        };
        let period = Duration::from_millis(period.millis());
        let (stop, stopped) = mpsc::channel::<()>();
        let (authority, node) = (authority.clone(), node.clone());
        let spawned = std::thread::Builder::new()
            .name("packaged-owner-renewal".into())
            .spawn(move || {
                // `while not self.closed.wait(20)`: the host's drop disconnects `stopped`.
                while let Err(mpsc::RecvTimeoutError::Timeout) = stopped.recv_timeout(period) {
                    // `self.closed.is_set()`: Quit's fence; a poisoned node counts as closed.
                    if node.closed().unwrap_or(true) {
                        break;
                    }
                    app_host::renew_owner(&*authority);
                }
            });
        match spawned {
            Ok(thread) => Self {
                stop: Some(stop),
                thread: Some(thread),
            },
            Err(_) => {
                eprintln!("owner renewal: owner_renewal_thread_unavailable");
                Self::idle()
            }
        }
    }
}
impl Drop for Renewal {
    fn drop(&mut self) {
        drop(self.stop.take());
        if let Some(thread) = self.thread.take()
            && thread.join().is_err()
        {
            eprintln!("owner renewal: renewal_thread_panicked");
        }
    }
}
impl PackagedHost {
    /// Bind the host to `root` (the packaged private state root) and compute the fast-mode
    /// default once, as the launcher's `bind_owner_process` does before the app serves. The
    /// authority is loaded as `DemoApp.__init__` does and, as `DemoApp.start` ->
    /// `authority.start()`, an owner with a swarm brings its seed back; the owner's lease renewal
    /// loop starts with the host (`DemoApp.start` -> `renew_thread.start()`).
    pub fn new(node: NodeService, root: PathBuf) -> Self {
        Self::with_renewal_period(
            node,
            root,
            BudgetId::PyMyceliumA13MacosAppDemoappRenewLoopSelfClosedWaitL917,
        )
    }
    /// [`PackagedHost::new`] with the owner's lease renewal period read from `period` instead
    /// of `RenewLoopSelfClosedWait`: a test runs the same composition on a shorter budget.
    pub fn with_renewal_period(node: NodeService, root: PathBuf, period: BudgetId) -> Self {
        let authority = Authority::load(root.clone()).map(|mut authority| {
            // No relay and no public origin in this process: its seed answers on its own
            // loopback address, never on an origin a saved network file names.
            authority.configured_origin = None;
            let authority = Arc::new(Mutex::new(authority));
            if app_host::resumable(&*authority) {
                // A seed that cannot be reopened must not cost the app its swarm. Discarding the
                // loaded authority here is what made a restart report no swarm at all: the persisted
                // record still answers membership and "Invite a Mac", and the seed is tried again.
                if let Err(error) = app_host::open_owner_seed(&*authority) {
                    eprintln!("l11_seed_reopen_refused:{}", error.code);
                }
            }
            authority
        });
        if let Err(code) = &authority {
            eprintln!("l11_authority_load_refused:{code}");
        }
        let renewal = Renewal::start(authority.as_ref().ok(), &node, period);
        let root_for_network = root.clone();
        let host = Self {
            node,
            root,
            fast_mode: FastModeProcess(Arc::new(Mutex::new(FastModeDefault {
                value: Ok("off"),
                refreshes: 0,
            }))),
            policy: None,
            _renewal: renewal,
            authority,
            model: None,
            publishing: None,
            network: crate::packaged_network::Network::new(
                &root_for_network,
                crate::packaged_network::NetworkConfig::default(),
            ),
            member_renewal: None,
            run: None,
            member_agent: None,
        };
        host.refresh_fast_mode();
        host
    }
    /// [`PackagedHost::new`] with the run model's acquisition over `source`, opened as
    /// `DemoApp.start` does once the authority started (`self._open_acquisition()`, app.py:627).
    /// An acquisition that cannot be opened keeps its code on stderr and answers as none.
    ///
    /// The policy loop starts with it (`DemoApp.start` -> `policy_thread.start()`, app.py:640),
    /// every `PolicyLoopSelfClosedWait`. A host whose loop cannot start keeps no model source
    /// (fail closed: no download runs unguarded; every acquisition seam keeps its default).
    pub fn with_model_source(node: NodeService, root: PathBuf, source: ModelSource) -> Self {
        let mut host = Self::new(node, root);
        let env = host.environment(run_model::ENV);
        if let Err(error) = source.step(&host.root, env.as_deref(), app_host::open_acquisition) {
            eprintln!("model source: {}", error.code);
        }
        let policy = {
            let (source, root) = (source.clone(), host.root.clone());
            let authority = host.authority.as_ref().ok().cloned();
            HostLoop::start(
                "packaged-acquisition-policy",
                BudgetId::PyMyceliumA13MacosAppDemoappPolicyLoopSelfClosedWaitL1009,
                &host.node,
                true,
                move || source.policy_tick(&root, authority.as_deref()),
            )
        };
        match policy {
            Ok(policy) => {
                host.policy = Some(policy);
                host.model = Some(source);
            }
            Err(code) => eprintln!("model source: {code}"),
        }
        host
    }
    /// This host with the owner's listing custody over `publisher` (its state file in the private
    /// root): retained opt-in and withdrawal debt reopen now (`swarm_publish.resume`; a failure is
    /// on stderr and in the custody's status, never a refused start), and its tick loop starts,
    /// one bounded pass every `OwnerPoll` with no lock of this host held across it. The packaged
    /// process has no owner run and no relay, so the facts it ticks with never make a listing
    /// public (`run_state` empty, no route); while the owner never opted in a pass does nothing
    /// (no state, no network). It has no relay either, so `listing_enabled` has no caller here.
    pub fn with_publisher(mut self, publisher: Arc<AppPublisher>) -> Self {
        if let Err(error) = publisher.resume() {
            eprintln!("swarm_publish_resume_failed: {}", error.code);
        }
        let ticks = {
            let (publisher, source) = (publisher.clone(), self.model.clone());
            HostLoop::start(
                "packaged-swarm-publish",
                BudgetId::OwnerPoll,
                &self.node,
                false,
                move || {
                    let model = source.as_ref().and_then(ModelSource::model_id);
                    let facts = OwnerFacts::from_owner_run(true, None, model, None);
                    if let Err(error) = publisher.tick(unix_seconds(), &facts) {
                        // Also shown as `swarm.error_code`; the next pass retries.
                        eprintln!("swarm_publish_tick_failed: {}", error.code);
                    }
                },
            )
        };
        match ticks {
            Ok(ticks) => self.publishing = Some(Publishing { publisher, ticks }),
            Err(code) => eprintln!("swarm_publish_unavailable: {code}"),
        }
        self
    }
    /// This host with its network over `config` (the relay's hooks and the join seams) in place of
    /// the app's own (`crate::packaged_network`); before the host is handed to its services.
    ///
    /// A joined Mac's own lease renewal starts with it (`_renew_loop` for a target, app.py:911-927
    /// @ f4e366e9), every `config.renewal`, as one of the host's loops (Quit stops and reaps it).
    pub fn with_network(mut self, config: crate::packaged_network::NetworkConfig) -> Self {
        let renewal = config.renewal;
        self.network = crate::packaged_network::Network::new(&self.root, config);
        if let Ok(authority) = &self.authority {
            // `authority.start()` of a joined target (app.py:613-626): its retained membership
            // resumes over the member's seed path; then `_resume_leave` (app.py:644): a completed
            // leave stays fenced and left (no renewal), an interrupted one is finished.
            crate::packaged_network::resume_membership(&self.network, authority);
            crate::packaged_network::resume_leave(&self.network, authority, self.model.clone());
            let (network, authority) = (self.network.clone(), authority.clone());
            match HostLoop::start(
                "packaged-member-renewal",
                renewal,
                &self.node,
                true,
                move || network.renew_member(&authority),
            ) {
                Ok(renewing) => self.member_renewal = Some(renewing),
                Err(code) => eprintln!("member renewal: {code}"),
            }
        }
        self.with_member_agent()
    }
    /// `DemoApp.standing_thread` (app.py:642 @ f4e366e9): `_standing_loop` (app.py:1201-1212) every
    /// `StandingLoopSelfClosedWait` (2 s) as one of the host's loops, over this host's authority,
    /// network and model source (`crate::packaged_network::standing_loop`): a joined target's
    /// agent reads the owner's announcement and answers with its own signed report. The agent is
    /// closed when the loop ends (Quit, `demo_app.close` -> `agent.close()`, demo_app.py:205-207).
    pub fn with_member_agent(mut self) -> Self {
        let Ok(authority) = &self.authority else {
            // Silent before: a target whose authority failed to load simply never ran its standing
            // loop, so nothing ever polled the owner and the run reported "N-1 of N Macs reported".
            eprintln!(
                "l11_member_agent_skipped:{}",
                self.authority.as_ref().err().copied().unwrap_or("unknown")
            );
            return self;
        };
        let node = self.node.clone();
        let (resource, acquisition) = (self.model.clone(), self.model.clone());
        let start_source = acquisition.clone();
        let seams = crate::packaged_network::MemberSeams {
            closed: Box::new(move || node.closed().unwrap_or(true)),
            // `app._resource_check()`: this Mac observed now against the saved policy; without
            // a model source the bundled probe observes it (unknown fails closed).
            resource: Box::new(move |root| match &resource {
                Some(source) => source.resource_violation(root),
                None => {
                    let policy = resource_policy::read(root)
                        .ok()
                        .flatten()
                        .unwrap_or_else(|| Value::Object(Map::new()));
                    resources::policy_violation(
                        &policy,
                        &ProbeObserver::beside_executable().observe(),
                    )
                }
            }),
            acquisition: Box::new(move || {
                acquisition
                    .as_ref()
                    .and_then(|source| source.lock().acquisition.clone())
            }),
            start_whole_model: {
                let acquisition = start_source;
                let root = self.root.clone();
                Box::new(move |choice: &Value| {
                    let source = acquisition
                        .as_ref()
                        .ok_or_else(|| "member_model_download_not_ported".to_owned())?;
                    source.start_member_whole_model(&root, choice)
                })
            },
        };
        let member = crate::packaged_network::PackagedMember::new(
            authority.clone(),
            self.network.clone(),
            seams,
        );
        let pass = crate::packaged_network::standing_loop(member);
        match HostLoop::start(
            "packaged-member-standing",
            BudgetId::PyMyceliumA13MacosAppDemoappStandingLoopSelfClosedWaitL1203,
            &self.node,
            true,
            pass,
        ) {
            Ok(standing) => self.member_agent = Some(standing),
            Err(code) => eprintln!("member agent: {code}"),
        }
        self
    }
    /// This host with the owner's run (`DemoApp.owner_run`, lane PH3b S3, `crate::packaged_run`),
    /// installed into `config` for the window's status and Quit: `/api/demo/{start,stop,rebuild}`
    /// run `demo_app.action` over it (demo_app.py:109-133 @ f4e366e9). The run reads this host's
    /// authority, network and model source; the owner's local contributor's lease is renewed every
    /// `RenewLoopSelfClosedWait` (`DemoApp._renew_loop`'s `contributor.renew()`, app.py:911-927) on its
    /// own loop, which Quit stops. A state refused at bind has no run (Start keeps the 409).
    pub fn with_run(mut self, config: crate::packaged_run::RunConfig) -> Self {
        let Ok(authority) = self.authority.clone() else {
            return self;
        };
        let model = self.model.clone();
        let (root, node) = (self.root.clone(), self.node.clone());
        let hooks = crate::packaged_run::ModelHooks {
            acquisition: {
                let model = model.clone();
                Box::new(move || model.as_ref().and_then(|m| m.lock().acquisition.clone()))
            },
            resource_check: {
                let (model, root) = (model.clone(), root.clone());
                Box::new(move || model.as_ref()?.resource_violation(&root))
            },
            // `DemoApp.reverify_model_snapshot` (app.py:976-999 @ f4e366e9): re-hash a snapshot left
            // on disk by an earlier run under the standing storage consent; every power / thermal /
            // network gate stays.
            reverify: {
                let (authority, node) = (authority.clone(), node.clone());
                Box::new(move || {
                    use owner_run::steps::model::Reverify;
                    let Some(model) = model.as_ref() else {
                        eprintln!("l11_reverify_gate:no_model");
                        return Reverify::Failed;
                    };
                    let policy = resource_policy::read(&root).ok().flatten();
                    let current = crate::packaged_network::membership_state(&authority).as_deref()
                        == Some("current");
                    let usable = !node.closed().unwrap_or(true)
                        && policy.is_some()
                        && app_host::running(&*authority)
                        && current
                        && !model.active();
                    if !usable {
                        eprintln!(
                            "l11_reverify_gate:usable=false closed={:?} policy={} running={} current={} active={}",
                            node.closed(),
                            policy.is_some(),
                            app_host::running(&*authority),
                            current,
                            model.active()
                        );
                    }
                    let acquisition = model.lock().acquisition.clone();
                    let missing_acquisition = acquisition.is_none();
                    let missing_policy = policy.is_none();
                    let (Some(policy), Some(acquisition), true) = (policy, acquisition, usable)
                    else {
                        if missing_acquisition {
                            eprintln!("l11_reverify_gate:no_acquisition");
                        } else if missing_policy {
                            eprintln!("l11_reverify_gate:no_policy");
                        }
                        return Reverify::Failed;
                    };
                    if !matches!(
                        acquisition.status().state.as_str(),
                        "closed" | "idle" | "failed" | "cancelled"
                    ) {
                        eprintln!("l11_reverify_gate:status={}", acquisition.status().state);
                        return Reverify::Failed;
                    }
                    if let Some(reason) = model.resource_violation(&root) {
                        eprintln!("l11_reverify_gate:resource={reason}");
                        model.lock().policy_error = Some(reason);
                        return Reverify::Failed;
                    }
                    let storage = policy
                        .get("storage_bytes")
                        .and_then(Value::as_u64)
                        .unwrap_or(0);
                    match acquisition.reverify_closed_snapshot(storage) {
                        Ok(_) => {
                            // The re-hash runs in the background, but the run step refuses at once unless
                            // the status already reads `prepared` (neither prepared nor downloading).
                            // Wait for the re-hash here so a relaunch does not make the owner download a
                            // snapshot that is already on disk.
                            let deadline =
                                std::time::Instant::now() + std::time::Duration::from_secs(90);
                            let mut seen = String::new();
                            loop {
                                let now = acquisition.status().state;
                                if now != seen {
                                    eprintln!("l11_reverify_gate:rehash_state={now}");
                                    seen = now.clone();
                                }
                                if now == "prepared"
                                    || now == "failed"
                                    || now == "cancelled"
                                    || std::time::Instant::now() >= deadline
                                {
                                    break;
                                }
                                std::thread::sleep(std::time::Duration::from_millis(200));
                            }
                            let final_state = acquisition.status().state;
                            let mut state = model.lock();
                            state.policy_error = None;
                            state.active = final_state == "prepared";
                            if final_state == "prepared" {
                                Reverify::Done
                            } else {
                                Reverify::Failed
                            }
                        }
                        Err(error) => {
                            eprintln!("l11_reverify_gate:rehash={error:?}");
                            Reverify::Failed
                        }
                    }
                })
            },
        };
        let host = crate::packaged_run::PackagedRun::new(
            authority,
            self.network.clone(),
            self.node.clone(),
            hooks,
            config.sidecar.clone(),
        );
        let renewal = {
            let host = host.clone();
            HostLoop::start(
                "packaged-contributor-renewal",
                BudgetId::PyMyceliumA13MacosAppDemoappRenewLoopSelfClosedWaitL917,
                &self.node,
                true,
                move || crate::packaged_run::OwnerRun::renew_contributor(&host),
            )
        };
        let renewal = match renewal {
            Ok(renewal) => Some(renewal),
            Err(code) => {
                eprintln!("contributor renewal: {code}");
                None
            }
        };
        self.run = Some(crate::packaged_run::OwnerRun::install(
            host, &config, renewal,
        ));
        self
    }
    /// The host's network (relay, pending windows, join seams), for the action root and the Quit.
    pub fn network(&self) -> Arc<crate::packaged_network::Network> {
        self.network.clone()
    }
    /// The authority, or the code its state was refused with at bind (the relay's Start answers it).
    pub fn authority_result(&self) -> Result<Arc<Mutex<Authority>>, &'static str> {
        self.authority.clone()
    }
    /// The owner's listing custody, for the window's Quit to close.
    pub fn publishing(&self) -> Option<Publishing> {
        self.publishing.clone()
    }
    /// The owner authority this host loaded (`None`: its state was refused at bind), for the
    /// product routes to read the owner's seed (`crate::packaged_product`).
    pub fn authority_handle(&self) -> Option<Arc<Mutex<Authority>>> {
        self.authority.as_ref().ok().cloned()
    }
    /// The host's own loops, for the window's Quit to stop and reap.
    pub fn loops(&self) -> Vec<Arc<HostLoop>> {
        self.policy
            .iter()
            .chain(self.member_renewal.iter())
            .chain(self.member_agent.iter())
            .cloned()
            .collect()
    }
    /// One acquisition step over this host's model source, `None` without one.
    fn acquisition<R>(
        &self,
        step: impl FnOnce(&mut AcquisitionView<'_>, &AcquisitionSite<'_>) -> R,
    ) -> Option<R> {
        let env = self.environment(run_model::ENV);
        self.model
            .as_ref()
            .map(|model| model.step(&self.root, env.as_deref(), step))
    }
    pub fn fast_mode_process(&self) -> FastModeProcess {
        self.fast_mode.clone()
    }
    fn refresh_fast_mode(&self) {
        let environment = self.environment(TOKEN_PATH_ENV);
        self.fast_mode
            .refresh(&self.root, self.owner(), environment.as_deref());
    }
    /// The authority, or the code its state was refused with at bind.
    fn authority(&self) -> Result<&Mutex<Authority>, PublicError> {
        self.authority
            .as_deref()
            .map_err(|code| PublicError::new(409, code))
    }
    /// The authority, `None` when its state was refused at bind.
    fn loaded(&self) -> Option<&Mutex<Authority>> {
        self.authority.as_deref().ok()
    }
}
impl Host for PackagedHost {
    /// `self.closed.is_set()`: the node service's shutdown fence (set by the window's Quit). A
    /// poisoned node counts as closed, so no action runs against it.
    fn closed(&self) -> bool {
        self.node.closed().unwrap_or(true)
    }
    /// `installation_role == 'owner'`. v0.1.4 starts as owner (app.py:537) unless
    /// `installation-role.json` selects a target (app.py:595-611), as the joined Mac's does once it
    /// joined (`_become_target`, app.py:1106-1116; `crate::packaged_network`). A state refused at
    /// bind has no role file read: it answers as the owner it starts as (each authority action
    /// then answers the refusal's code). A poisoned authority is no owner (fail closed).
    fn owner(&self) -> bool {
        self.loaded()
            .is_none_or(|authority| authority.lock().is_ok_and(|a| !a.target))
    }
    /// The code the state was refused with at bind (`Authority::load`).
    fn installation_refused(&self) -> Option<&'static str> {
        self.authority.as_ref().err().copied()
    }
    /// `os.environ.get(name)`. A value that is not UTF-8 stays set (Python decodes it with
    /// `surrogateescape`), so it is never mistaken for an unset variable.
    fn environment(&self, name: &str) -> Option<String> {
        std::env::var_os(name).map(|value| value.to_string_lossy().into_owned())
    }
    /// `_device_memory_bytes()` (app.py:1123-1126): this Mac's physical memory, `None` when
    /// unknown; the network's (the composition's seam, else `hw.memsize`), which the status's
    /// memory choices read too.
    fn device_memory_bytes(&self) -> Option<u64> {
        self.network.device_memory_bytes()
    }
    /// `fast_mode.run_active(app)`. This process has no chain compile/serving thread or
    /// workflow, so it is the owner run alone. A poisoned node counts as active (refuse a change).
    fn run_active(&self) -> bool {
        self.owner_run_active()
    }
    /// `app._acquisition_active` (v0.1.4's `False` without an acquisition, app.py:560).
    fn acquisition_active(&self) -> bool {
        self.model.as_ref().is_some_and(ModelSource::active)
    }
    /// `app._resource_check()` (app.py:929-947 @ f4e366e9), as the headless launcher's: this
    /// Mac's observation from the model source's observer, kept as `resource_observation`, against
    /// the saved resource policy (`read_resource_policy(self.root) or {}`) by
    /// `resources::policy_violation` (resources.py:152-181); the power, thermal and network gates
    /// are never softened. The packaged process has no member-leave fence and no A16 adaptive
    /// admission. A host without a model source observes nothing: its download keeps the trait
    /// default answer.
    fn resource_check(&mut self) -> Option<&'static str> {
        self.model.as_ref()?.resource_violation(&self.root)
    }
    /// `app.owner_run.active()`: the owner's run (`crate::packaged_run`) or the node service's run
    /// still owns its driver.
    fn owner_run_active(&self) -> bool {
        self.run
            .as_ref()
            .is_some_and(crate::packaged_run::OwnerRun::active)
            || self.node.run_active().unwrap_or(true)
    }
    /// `app._refresh_fast_mode_process()`, after the file is written.
    fn fast_mode_changed(&mut self) {
        self.refresh_fast_mode();
    }
    /// `acquisition_policy_error = None; _open_acquisition()` after the run-model choice is saved;
    /// a failed open is the choice's answer (app.py:866-867 @ f4e366e9).
    fn run_model_saved(&mut self) -> Result<(), PublicError> {
        self.acquisition(app_host::run_model_saved)
            .unwrap_or(Ok(()))
    }
    fn set_acquisition_policy_error(&mut self, reason: Option<&'static str>) {
        self.acquisition(|view, _| *view.policy_error = reason);
    }
    /// `self.acquisition.start(grant)` then `self._acquisition_active = True`.
    fn acquisition_start(&mut self, grant: &Value) -> Result<Json, PublicError> {
        self.acquisition(|view, _| app_host::acquisition_start(view, grant))
            .unwrap_or_else(|| Err(PublicError::new(409, "action_not_completed")))
    }
    /// `self._cancel_acquisition(); return self.acquisition.status()`.
    fn acquisition_cancel(&mut self) -> Result<Json, PublicError> {
        self.acquisition(|view, _| app_host::acquisition_cancel(view))
            .unwrap_or_else(|| Err(PublicError::new(409, "action_not_completed")))
    }
    /// `self._cancel_acquisition()` (`/api/stop`, revocation of this Mac).
    fn cancel_acquisition(&mut self) {
        self.acquisition(|view, _| app_host::cancel_acquisition(view));
    }
    /// `if acquisition is None or its state_root differs: self._open_acquisition()`.
    fn reopen_acquisition_if_needed(&mut self) {
        self.acquisition(app_host::reopen_acquisition_if_needed);
    }
    /// `PersistentBackend` hands `/api/demo/{start,stop,rebuild}` to the host
    /// (`demo_app.action`, demo_app.py:109-133 @ f4e366e9): the owner's run (`with_run`, lane PH3b
    /// S3). A Stop also stops the node service's own local run when one is active (the packaged
    /// chat's native run, which the page's Stop ends too). Without a composed run, Stop and Rebuild
    /// are the node service's own and Start keeps the trait default.
    fn demo_action(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        if let Some(run) = &self.run {
            if action == Action::DemoStop && self.node.run_active().unwrap_or(false) {
                let mut node = self.node.clone();
                if let Err(error) = node
                    .prepare(action, body)
                    .and_then(|()| node.execute_json(action, body))
                {
                    eprintln!("node run stop: {}", error.code);
                }
            }
            return run.demo_action(action, body);
        }
        match action {
            Action::DemoStop | Action::DemoRebuild => {
                let mut node = self.node.clone();
                node.prepare(action, body)?;
                node.execute_json(action, body)
            }
            _ => Err(PublicError::new(409, "action_not_completed")),
        }
    }
    /// The authority part of `/api/create` (app.py:1604-1622): a current member is answered
    /// with `authority.status()`, a fresh owner creates the swarm and joins it as member 1.
    fn create_owner(&mut self, consent: &Map<String, Value>) -> Result<Value, PublicError> {
        self.create_owner_json(consent)
            .map(|answer| answer.to_value())
    }
    fn create_owner_json(&mut self, consent: &Map<String, Value>) -> Result<Json, PublicError> {
        app_host::create_owner_json(self.authority()?, consent)
    }
    fn swarm_created(&self) -> bool {
        self.loaded().is_some_and(app_host::swarm_created)
    }
    /// `authority.status().get('membership_state')`: a joined Mac's own (`crate::packaged_network`).
    fn membership_state(&self) -> Option<String> {
        self.loaded()
            .and_then(crate::packaged_network::membership_state)
    }
    fn running(&self) -> bool {
        self.loaded().is_some_and(app_host::running)
    }
    fn stop_authority(&mut self) {
        if let Some(authority) = self.loaded() {
            app_host::stop_authority(authority);
        }
    }
    /// Over the member's seed path (`crate::packaged_network`'s join seams): a joined Mac's Start
    /// resumes its membership there (lane B, C-01).
    fn start_authority(&mut self) -> Result<(), PublicError> {
        app_host::start_authority(&self.network.cell(self.authority()?))
    }
    fn pair_request(&mut self) -> Result<Json, PublicError> {
        app_host::pair_request(self.authority()?)
    }
    fn pair_approve(
        &mut self,
        request: &Value,
        ttl_seconds: &Value,
        quota: &Value,
    ) -> Result<Json, PublicError> {
        app_host::pair_approve(self.authority()?, request, ttl_seconds, quota)
    }
    /// `RemoteMacMember.membership_binding()` of this Mac's live membership session.
    fn membership_binding(&self) -> Option<api::member::Binding> {
        self.loaded().and_then(app_host::membership_binding)
    }
    /// `accept_pairing` over the member's seed path (`crate::packaged_network`'s join seams).
    fn pair_accept(&mut self, handoff: &Value) -> Result<Json, PublicError> {
        app_host::pair_accept(&self.network.cell(self.authority()?), handoff)
    }
    /// The owner's window store. Without a relay origin the persistent backend answers `open`
    /// itself (a pending window, `_open_pending_window`); list/approve/revoke are the store's,
    /// and a code opened while the relay runs is a window on its origin.
    fn owner_window(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        app_host::owner_window(&self.network.cell(self.authority()?), action, body)
    }
    // ---- the network window and the join (lane PH3a, `crate::packaged_network`) ----
    /// The joined Mac's side of one member leave (`member_leave.app_steps`): `crate::packaged_network`.
    fn leave_worker(&mut self) -> Box<dyn api::leave::LeaveWorker> {
        Box::new(crate::packaged_network::PackagedLeave {
            network: self.network.clone(),
            model: self.model.clone(),
        })
    }
    /// `resume_leave` / a smooth leave's begin: `_leave_fence = True` and, once left, `_left = True`.
    fn leave_mark(&mut self, fence: bool, left: bool) {
        self.network.mark(fence, left);
    }
    fn relay_state(&self) -> api::persistent::RelayState {
        self.network.state()
    }
    fn close_stopped_relay(&mut self) {
        self.network.close();
    }
    fn relay_origin(&self) -> Option<String> {
        self.network.origin()
    }
    fn configured_public_origin(&self) -> Option<String> {
        self.loaded().and_then(app_host::configured_public_origin)
    }
    /// `/api/create` with `{relay_acknowledged, max_seconds}`: the relay precedes the swarm.
    fn start_public_relay(
        &mut self,
        relay_acknowledged: &Value,
        max_seconds: &Value,
    ) -> Result<(), PublicError> {
        let body: Map<String, Value> = [
            ("relay_acknowledged".to_owned(), relay_acknowledged.clone()),
            ("max_seconds".to_owned(), max_seconds.clone()),
        ]
        .into_iter()
        .collect();
        let authority = self
            .authority
            .clone()
            .map_err(|code| PublicError::new(409, code))?;
        self.network.start(&authority, &body).map(drop)
    }
    fn abort_public_relay(&mut self) {
        self.network.close();
    }
    fn bind_relay_seed(&mut self) {
        if let Some(authority) = self.loaded() {
            self.network.bind_seed(authority);
        }
    }
    /// `installation_role = 'target'` (the role file is written by the backend first).
    fn adopt_target_role(&mut self) -> Result<(), PublicError> {
        crate::packaged_network::adopt_target_role(self.authority()?);
        self.refresh_fast_mode();
        Ok(())
    }
    fn join_submit(
        &mut self,
        link: &api::persistent::JoinLink,
        request: &Value,
        quota: &Value,
    ) -> Result<(Value, Vec<Value>), PublicError> {
        let base = self.network.seams().join_base.clone();
        api::joined_member::join_submit(&self.root, base.as_deref(), link, request, quota)
    }
    /// `_auto_report(purpose)` (app.py:1138-1163) for this joined Mac, recorded beside the
    /// authority (`last_report`, the next periodic report).
    fn auto_report(&mut self, purpose: &str) {
        let Some(authority) = self.loaded() else {
            return;
        };
        let base = self.network.seams().join_base.clone();
        let record = api::joined_member::auto_report(&self.root, base.as_deref(), purpose);
        authority
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .recorded
            .reported(record, std::time::Instant::now());
    }
    fn join_poll(&mut self, state: &Map<String, Value>) -> Result<Value, PublicError> {
        api::joined_member::join_poll(state, self.network.seams().join_base.as_deref())
    }
    /// `self.standing_error = public_failure_code(error)` after a failed `bind_standing_consent`
    /// (app.py:1802-1806); `error_http` is `None` for a consent error (control_failures.py:24-27).
    fn standing_bind_failed(&mut self, code: &'static str) {
        if let Some(authority) = self.loaded() {
            let mut a = authority.lock().unwrap_or_else(PoisonError::into_inner);
            a.recorded.standing_error = Some(code.to_owned());
            a.recorded.standing_error_http = None;
        }
    }
    fn join_accept(
        &mut self,
        handoff: &Value,
        seed_key_digest: Option<&str>,
    ) -> Result<api::persistent::JoinAccepted, PublicError> {
        let base = self.network.seams().join_base.clone();
        let cell = self.network.cell(self.authority()?);
        api::joined_member::join_accept(&cell, handoff, seed_key_digest, base.as_deref())
    }
    /// `POST /api/swarm/publish` (`swarm_publish.action`, app mode): the explicit opt-in.
    fn swarm_publish(&mut self, body: &Map<String, Value>) -> Result<Json, PublicError> {
        let Some(publishing) = &self.publishing else {
            return Err(PublicError::new(400, "unknown_action"));
        };
        if self.closed() {
            return Err(PublicError::new(400, "application_closed"));
        }
        publishing
            .publisher
            .publish(self.owner(), body)
            .map(Json::from)
    }
    /// `POST /api/swarm/unpublish`: the opt-out; the next pass withdraws a listing.
    fn swarm_unpublish(&mut self, body: &Map<String, Value>) -> Result<Json, PublicError> {
        let Some(publishing) = &self.publishing else {
            return Err(PublicError::new(400, "unknown_action"));
        };
        if self.closed() {
            return Err(PublicError::new(400, "application_closed"));
        }
        publishing
            .publisher
            .unpublish(self.owner(), body)
            .map(Json::from)
    }
    fn live_pairing_windows(&self) -> usize {
        self.loaded().map_or(0, app_host::live_pairing_windows)
    }
    fn coordinator_ready(&self) -> bool {
        self.loaded().is_some_and(app_host::coordinator_ready)
    }
}
