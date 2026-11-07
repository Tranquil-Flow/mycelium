// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged executable's `api::persistent::Host`: the facts this process really has for the
//! desktop action backend (S1 root A). `PersistentBackend` owns the private preference files
//! (`/api/fast-mode`, `/api/run-model`, app.py:1356-1361 @ f4e366e9) and asks the host only for
//! what v0.1.4 read from `os.environ` and the app object. The owner authority (`/api/create`,
//! authority start/stop, the pairing request/approve/accept seams and the owner's pairing
//! windows) is `api::app_host`, the same code the headless L3 launcher runs, and the host keeps
//! the owner's lease renewed with that library's pass (`_renew_loop`, [`PackagedHost`]); the packaged
//! process has no relay and no public origin, so its seed answers on its own loopback address
//! and a pairing code asked for waits for a relay (app.py:1703-1710). Every other seam keeps the
//! trait default, so an action whose producer is not ported yet (join, consent binding, model
//! download, serving compile) answers the host's 409 `action_not_completed`.
//! `/api/serving/status` keeps the trait default block on purpose: it is v0.1.4's own answer
//! (app.py:1377-1399 @ f4e366e9; `{compilation: null, serving: null, route_ready: false}` until a
//! chain compile starts, chain_compile.py:197), executed on the real packaged process in
//! `test-support/journeys/fixtures/python-v014-boundaries.json` (`no_model_no_execution_proposal`).
use crate::service::NodeService;
use api::{
    actions::{Action, Backend as _},
    app_host::{self, Authority},
    persistent::{Host, TOKEN_PATH_ENV},
    preferences::fast_mode,
    schema::PublicError,
    wire::Json,
};
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value};
use std::{
    path::{Path, PathBuf},
    sync::{Arc, Mutex, PoisonError, mpsc},
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

/// The packaged process as `persistent::Host`. It holds the same [`NodeService`] that is the
/// backend's inner delegate (one shared state), the private state root, the fast-mode default,
/// the owner authority over that root and the loop that keeps the owner's lease renewed.
pub struct PackagedHost {
    node: NodeService,
    root: PathBuf,
    fast_mode: FastModeProcess,
    /// `DemoApp.renew_thread` over the authority below, held for its drop. Declared before the
    /// authority: dropping the host ends and joins the loop before the authority (and the seed it
    /// keeps) is released.
    _renewal: Renewal,
    /// `DemoApp.authority` (`api::app_host`), loaded from the private root at bind. A state that
    /// cannot be read or resumed keeps its code: every authority seam refuses with it and nothing
    /// is written over it.
    authority: Result<Arc<Mutex<Authority>>, &'static str>,
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
        let authority = Authority::load(root.clone()).and_then(|mut authority| {
            // No relay and no public origin in this process: its seed answers on its own
            // loopback address, never on an origin a saved network file names.
            authority.configured_origin = None;
            let authority = Arc::new(Mutex::new(authority));
            if app_host::resumable(&*authority) {
                app_host::open_owner_seed(&*authority).map_err(|error| error.code)?;
            }
            Ok(authority)
        });
        let renewal = Renewal::start(authority.as_ref().ok(), &node, period);
        let host = Self {
            node,
            root,
            fast_mode: FastModeProcess(Arc::new(Mutex::new(FastModeDefault {
                value: Ok("off"),
                refreshes: 0,
            }))),
            _renewal: renewal,
            authority,
        };
        host.refresh_fast_mode();
        host
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
    /// `installation-role.json` selects a target (app.py:595-611). The packaged process has no
    /// target role yet (joining is a later S1 step) and publishes `installation_role: owner`.
    fn owner(&self) -> bool {
        true
    }
    /// `os.environ.get(name)`. A value that is not UTF-8 stays set (Python decodes it with
    /// `surrogateescape`), so it is never mistaken for an unset variable.
    fn environment(&self, name: &str) -> Option<String> {
        std::env::var_os(name).map(|value| value.to_string_lossy().into_owned())
    }
    /// `host_memory_bytes()`: this Mac's physical memory, `None` when unknown. Read with the
    /// same `hw.memsize` probe the native driver uses (native.rs `host`).
    fn device_memory_bytes(&self) -> Option<u64> {
        let output = crate::spawn::confined_output("/usr/sbin/sysctl", &["-n", "hw.memsize"])
            .ok()
            .filter(|output| output.status.success())?;
        String::from_utf8(output.stdout)
            .ok()?
            .trim()
            .parse()
            .ok()
            .filter(|bytes| *bytes > 0)
    }
    /// `fast_mode.run_active(app)`. This process has no chain compile/serving thread or
    /// workflow, so it is the owner run alone. A poisoned node counts as active (refuse a change).
    fn run_active(&self) -> bool {
        self.owner_run_active()
    }
    /// `app._acquisition_active`: this process never starts an acquisition (v0.1.4's value
    /// without one, app.py:560).
    fn acquisition_active(&self) -> bool {
        false
    }
    /// `app.owner_run.active()`: the node service's run still owns its driver.
    fn owner_run_active(&self) -> bool {
        self.node.run_active().unwrap_or(true)
    }
    /// `app._refresh_fast_mode_process()`, after the file is written.
    fn fast_mode_changed(&mut self) {
        self.refresh_fast_mode();
    }
    /// `PersistentBackend` hands `/api/demo/{start,stop,rebuild}` to the host
    /// (`demo_app.action`). Stop and Rebuild are the node service's own (the inner backend:
    /// the same shared service); Start keeps the trait default until the plan path is ported.
    fn demo_action(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
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
    fn membership_state(&self) -> Option<String> {
        self.loaded().and_then(app_host::membership_state)
    }
    fn running(&self) -> bool {
        self.loaded().is_some_and(app_host::running)
    }
    fn stop_authority(&mut self) {
        if let Some(authority) = self.loaded() {
            app_host::stop_authority(authority);
        }
    }
    fn start_authority(&mut self) -> Result<(), PublicError> {
        app_host::start_authority(self.authority()?)
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
    fn pair_accept(&mut self, handoff: &Value) -> Result<Json, PublicError> {
        app_host::pair_accept(self.authority()?, handoff)
    }
    /// The owner's window store. Without a relay origin the persistent backend answers `open`
    /// itself (a pending window, `_open_pending_window`); list/approve/revoke are the store's.
    fn owner_window(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        app_host::owner_window(self.authority()?, action, body)
    }
    fn live_pairing_windows(&self) -> usize {
        self.loaded().map_or(0, app_host::live_pairing_windows)
    }
    fn coordinator_ready(&self) -> bool {
        self.loaded().is_some_and(app_host::coordinator_ready)
    }
}
