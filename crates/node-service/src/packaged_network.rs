// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged host's network window and its joined-member side (lane PH3a), composed from the
//! same library code the headless launcher runs (`api::relay::OwnerRelay`, `api::app_host`,
//! `api::joined_member`). v0.1.4 @ f4e366e9:
//!
//! * `DemoApp.public_relay` and `_start_public_relay` (app.py:676-740): the temporary relay, the
//!   owner's seed bound behind it (`proxy.bind_seed`, app.py:1612-1613, :1636-1637), the owner routes
//!   behind it (`_bind_public_routes`, app.py:1070-1079: pairing windows, member-report inbox, link
//!   probe) and the codes asked for before it existed (`_materialize_pending_windows`, :823-834).
//!   The relay closes itself when its window runs out (the same Start reopens it) and at Quit
//!   (`DemoApp.close`, app.py:1848-1850).
//! * The joined Mac's authority view ([`PackagedCell`]): the relay origin, `_left` and how a joining
//!   member reaches the owner's seed. The per-host seams are v0.1.4's own constructor seams
//!   (`DemoApp(_test_join_base_url=...)`, app.py:506-513): production leaves them empty (public
//!   Web-PKI path); a test points the member at the owner's loopback relay proxy and local seed.
//! * `_renew_loop` for a joined target (app.py:911-927, `RemoteMacMember.renew`), skipped once the
//!   Mac left (`_left`), and the member leave's steps (`member_leave.app_steps`, member_leave.py:192-260).
use crate::{app_host::ModelSource, router_port::ServingActions};
use api::{
    actions::{Action, Backend},
    app_host::{self, Authority, Cell, HostView, NETWORK_FILE, RelayOrigin, Unmanaged, cap},
    joined_member,
    persistent::RelayState,
    preferences::private_state,
    relay::{EndpointHost, Hooks, OwnerRelay, Probe, Proxy, SeedBackend, StartGuard},
    schema::{PublicError, Raised},
    seed::SeedOptions,
    windows::PendingWindows,
    wire::Json,
};
use serde_json::{Map, Value, json};
use std::{
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex, MutexGuard, PoisonError,
        atomic::{AtomicBool, Ordering},
    },
};

/// How the relay is made: `Hooks::production` beside the executable in the app, a test's own clock
/// and lifetime poll in a test (the relay provider stays the external one on loopback there).
pub type RelayHooks = Arc<dyn Fn() -> Hooks + Send + Sync>;

/// The join flow's per-host seams (v0.1.4 `_test_join_base_url`, app.py:506-513). Default: none,
/// the member reaches the owner's relay and seed over the public Web-PKI path.
#[derive(Clone, Debug, Default)]
pub struct JoinSeams {
    /// `_test_join_base_url`: the member's window, probe and report requests go to this loopback
    /// relay proxy instead of the public HTTPS origin.
    pub join_base: Option<String>,
    /// How a joining (or renewing) member reaches the owner's seed.
    pub member_seed: SeedOptions,
}
/// What the packaged composition gives the network: the relay's hooks, the join seams and the
/// period of a joined member's own lease renewal.
#[derive(Clone)]
pub struct NetworkConfig {
    pub hooks: RelayHooks,
    pub seams: JoinSeams,
    /// `_renew_loop`'s period for a joined target (`self.closed.wait(20)`, app.py:917): the app's
    /// `RenewLoopSelfClosedWait`; a test runs the same loop on a shorter budget.
    pub renewal: budgets::BudgetId,
}
impl Default for NetworkConfig {
    /// The app's own: the bundled `native/cloudflared` next to the executable, no seams, 20 s.
    fn default() -> Self {
        Self {
            renewal: budgets::BudgetId::PyMyceliumA13MacosAppDemoappRenewLoopSelfClosedWaitL917,
            hooks: Arc::new(|| {
                Hooks::production(
                    std::env::current_exe()
                        .ok()
                        .and_then(|exe| exe.parent().map(Path::to_path_buf)),
                )
            }),
            seams: JoinSeams::default(),
        }
    }
}

/// `DemoApp.public_relay`, the pending windows the persistent backend keeps
/// (`DemoApp._pending_windows`), the join seams and the member's `_leave_fence` / `_left`.
pub struct Network {
    root: PathBuf,
    relay: Mutex<OwnerRelay>,
    hooks: RelayHooks,
    pending: Mutex<PendingWindows>,
    seams: JoinSeams,
    fenced: AtomicBool,
    left: AtomicBool,
    /// Quit's hold on a relay a Start is still bringing up (`self.guard`, app.py:1344, 1831).
    start_guard: StartGuard,
}
fn lock<T>(cell: &Mutex<T>) -> MutexGuard<'_, T> {
    // Plain values and handles: a panicked holder cannot leave them half-written.
    cell.lock().unwrap_or_else(PoisonError::into_inner)
}
/// `public_relay.status().get('public_origin')` as the library's authority view reads it.
struct Origin(Option<String>);
impl RelayOrigin for Origin {
    fn origin(&self) -> Option<String> {
        self.0.clone()
    }
}
impl Network {
    pub fn new(root: &Path, config: NetworkConfig) -> Arc<Self> {
        Arc::new(Self {
            root: root.to_owned(),
            relay: Mutex::new(Self::fresh(root, &config.hooks)),
            hooks: config.hooks,
            pending: Mutex::default(),
            seams: config.seams,
            fenced: AtomicBool::new(false),
            left: AtomicBool::new(false),
            start_guard: StartGuard::default(),
        })
    }
    fn fresh(root: &Path, hooks: &RelayHooks) -> OwnerRelay {
        let hooks = hooks.clone();
        OwnerRelay::new(root, move || hooks())
    }
    /// The persistent backend's own pending-window list (`DemoApp._pending_windows`), shared.
    pub fn share_pending(&self, pending: PendingWindows) {
        *lock(&self.pending) = pending;
    }
    pub fn seams(&self) -> &JoinSeams {
        &self.seams
    }
    /// The authority as the library's seams see it from this host.
    pub fn cell<'a>(&'a self, authority: &'a Mutex<Authority>) -> PackagedCell<'a> {
        PackagedCell {
            authority,
            network: self,
        }
    }
    pub fn state(&self) -> RelayState {
        lock(&self.relay).state()
    }
    pub fn origin(&self) -> Option<String> {
        lock(&self.relay).origin()
    }
    /// The status's `network` block (`{'state': 'private'}` without a relay).
    pub fn status(&self) -> Json {
        lock(&self.relay).network_status()
    }
    /// `self.public_relay.close(); self.public_relay = None` (a window that ran out, or Quit).
    pub fn close(&self) {
        let mut relay = std::mem::replace(
            &mut *lock(&self.relay),
            Self::fresh(&self.root, &self.hooks),
        );
        relay.close();
    }
    /// Quit's relay step (`DemoApp.close`, app.py:1831, 1848-1850): a relay a Start is still
    /// bringing up is closed at once and no later Start brings one up (`application_closed`),
    /// then the relay this host keeps is closed.
    pub fn quit(&self) {
        self.start_guard.close();
        self.close();
    }
    /// `_leave_fence`: no new work once a leave began.
    pub fn fenced(&self) -> bool {
        self.fenced.load(Ordering::SeqCst)
    }
    /// `_left`: the renewal loop skips a Mac that left, so its lease ages out.
    pub fn left(&self) -> bool {
        self.left.load(Ordering::SeqCst)
    }
    pub fn mark(&self, fence: bool, left: bool) {
        if fence {
            self.fenced.store(true, Ordering::SeqCst);
        }
        if left {
            self.left.store(true, Ordering::SeqCst);
        }
    }
    /// `self.public_relay.proxy.bind_seed(self.authority.seed_server)` once the swarm exists
    /// (app.py:1612-1613) or the authority started again (app.py:1636-1637).
    pub fn bind_seed(&self, authority: &Mutex<Authority>) {
        let (seed, swarm) = {
            let a = lock(authority);
            (a.seed.clone(), a.owner_authority().is_some())
        };
        lock(&self.relay).bind_seed(joined_member::seed_backend(seed, swarm));
    }
    /// One pass of `_renew_loop` for a joined target (`authority.renew()` = `RemoteMacMember.renew`)
    /// over this host's member cell (its seed path, `_left`).
    pub fn renew_member(&self, authority: &Mutex<Authority>) {
        app_host::renew_member(&self.cell(authority));
    }
    /// `_start_public_relay` after the persistent backend's closed/role/shape checks: the relay is
    /// taken out of this host while it starts, so the endpoint seams (which lock the authority)
    /// never meet the relay's own lock.
    pub fn start(
        &self,
        authority: &Arc<Mutex<Authority>>,
        body: &serde_json::Map<String, Value>,
    ) -> Result<Json, PublicError> {
        let mut relay = std::mem::replace(
            &mut *lock(&self.relay),
            Self::fresh(&self.root, &self.hooks),
        );
        relay.set_start_guard(self.start_guard.clone());
        // `_relay_is_serving`: the packaged host has no owner run, so the relay never extends
        // itself; it closes at the end of the granted window.
        let probe: Probe = Arc::new(|| Ok(false));
        let outcome = relay.start_public_relay(
            body,
            &mut Endpoint {
                authority: authority.clone(),
                network: self,
            },
            probe,
        );
        *lock(&self.relay) = relay;
        outcome
    }
}

/// `_become_target`'s role switch (app.py:1106-1116 @ f4e366e9), after the backend stopped the owner
/// authority and wrote `installation-role.json`: this Mac's authority is the joining target's from now
/// on (its peer state in the same private root, as the headless launcher keeps it).
pub fn adopt_target_role(authority: &Mutex<Authority>) {
    let mut a = lock(authority);
    a.target = true;
    a.running = false;
}

/// `authority.status().get('membership_state')`: the owner authority's, or a joined target's own
/// (`TargetApplication.status()`, peer.py:395-455), as its status shows it.
pub fn membership_state(authority: &Mutex<Authority>) -> Option<String> {
    let target = lock(authority).target;
    if !target {
        return app_host::membership_state(authority);
    }
    let status = joined_member::target_status(&lock(authority));
    match status.get("membership_state") {
        Some(Json::Str(state)) => Some(state.clone()),
        _ => None,
    }
}

/// The packaged authority as `api::app_host`'s [`Cell`]: the relay origin and `_left` beside it,
/// and how a joining member reaches the owner's seed.
pub struct PackagedCell<'a> {
    authority: &'a Mutex<Authority>,
    network: &'a Network,
}
impl Cell for PackagedCell<'_> {
    type Managed = Unmanaged;
    fn with<R>(&self, f: impl FnOnce(&mut HostView<'_, Unmanaged>) -> R) -> R {
        // The relay's lock is released before the authority's is taken.
        let origin = Origin(self.network.origin());
        let left = self.network.left();
        let mut authority = lock(self.authority);
        f(&mut HostView::new(&mut authority, &origin, false, left))
    }
    fn try_with<R>(&self, f: impl FnOnce(&mut HostView<'_, Unmanaged>) -> R) -> Option<R> {
        let origin = Origin(self.network.origin());
        let left = self.network.left();
        let mut authority = self.authority.lock().ok()?;
        Some(f(&mut HostView::new(&mut authority, &origin, false, left)))
    }
    fn member_seed_options(&self) -> SeedOptions {
        self.network.seams.member_seed.clone()
    }
}

/// The authority seams of `_start_public_relay` on this host.
struct Endpoint<'a> {
    authority: Arc<Mutex<Authority>>,
    network: &'a Network,
}
impl Endpoint<'_> {
    fn cell(&self) -> PackagedCell<'_> {
        self.network.cell(&self.authority)
    }
}
impl EndpointHost for Endpoint<'_> {
    fn swarm_created(&self) -> bool {
        app_host::swarm_created(&self.cell())
    }
    fn configured_public_origin(&self) -> Option<String> {
        app_host::configured_public_origin(&self.cell())
    }
    /// `authority.configure_public_endpoint(public_origin=, port=, acknowledged=True)`: the origin
    /// is kept across restarts (`_network_configuration`).
    fn configure_public_endpoint(&mut self, origin: &str, _port: u16) -> Result<(), PublicError> {
        let root = {
            let mut a = lock(&self.authority);
            a.configured_origin = Some(origin.to_owned());
            a.root.clone()
        };
        private_state::store(
            &root.join(NETWORK_FILE),
            &json!({"public_origin": origin}),
            cap(),
        )
        .map_err(|e| PublicError::new(409, e.0).raised_by(Raised::Authority))
    }
    /// A swarm that exists answers on the new origin: its seed is reopened bound to it.
    fn attach_public_endpoint(&mut self, origin: &str, port: u16) -> Result<(), PublicError> {
        self.configure_public_endpoint(origin, port)?;
        let old = lock(&self.authority).seed.take();
        if let Some(old) = old {
            old.close();
            app_host::open_owner_seed(&self.cell())?;
        }
        Ok(())
    }
    fn relocate_authority(&mut self, origin: &str, port: u16) -> Result<(), PublicError> {
        self.attach_public_endpoint(origin, port)
    }
    fn seed_backend(&self) -> Option<Arc<dyn SeedBackend>> {
        let a = lock(&self.authority);
        joined_member::seed_backend(a.seed.clone(), a.owner_authority().is_some())
    }
    fn set_running(&mut self) {
        lock(&self.authority).running = true;
    }
    /// `_bind_public_routes`: the pairing windows, the member-report inbox and the link probe.
    fn bind_public_routes(&mut self, proxy: &Arc<Proxy>) {
        let authority = self.authority.clone();
        proxy.bind_local(Some(joined_member::public_route::<Unmanaged>(Arc::new(
            move || {
                let a = lock(&authority);
                (a.windows.clone(), a.seed.clone())
            },
        ))));
    }
    /// `_materialize_pending_windows`: codes asked for before the relay exist from now on.
    fn materialize_pending_windows(&mut self) {
        let (windows, origin, seed) = {
            let a = lock(&self.authority);
            (
                a.windows.clone(),
                a.configured_origin.clone(),
                a.seed.clone(),
            )
        };
        let (Some(origin), Some(seed)) = (origin, seed) else {
            return;
        };
        let pending = lock(&self.network.pending).clone();
        let digest = seed.coordinator.signer().verification_key_digest();
        for item in pending.drain() {
            // capacity or an invalid TTL: the owner can open a fresh code (app.py:833-834)
            if windows
                .open(
                    &Value::String(origin.clone()),
                    &Value::String(digest.clone()),
                    &json!(item.ttl_seconds),
                )
                .is_err()
            {
                continue;
            }
        }
    }
}

/// A joined Mac's side of one member leave (`member_leave.app_steps`, member_leave.py:192-260):
/// `fence` stops new work, `disconnect` stops the lease renewal (`_left`), `cancel_work` cancels
/// the model download (`_cancel_acquisition`). The packaged member owns no compute in this lane
/// (no remote target pump), so `unload`/`publish_withdrawal` succeed only while that is durably
/// proven (`joined_member::none_owned_on_disk`); otherwise the step fails and its debt is kept.
pub struct PackagedLeave {
    pub(crate) network: Arc<Network>,
    pub(crate) model: Option<ModelSource>,
}
impl PackagedLeave {
    fn none_owned(&self) -> Result<bool, String> {
        joined_member::none_owned_on_disk(&self.network.root)
    }
}
impl api::leave::LeaveWorker for PackagedLeave {
    fn step(&mut self, name: &str) -> Result<(), Option<String>> {
        if matches!(name, "unload" | "publish_withdrawal") && !self.none_owned().map_err(Some)? {
            return Err(Some(if name == "publish_withdrawal" {
                "member_withdrawal_publication_unavailable".into()
            } else {
                "member_compute_cleanup_unavailable".into()
            }));
        }
        match name {
            "fence" => self.network.mark(true, false),
            "disconnect" => self.network.mark(false, true),
            "cancel_work" => {
                if let Some(model) = &self.model {
                    model.cancel();
                }
            }
            _ => {}
        }
        Ok(())
    }
    fn inflight(&mut self) -> Result<i64, api::leave::Unobservable> {
        match self.none_owned() {
            Ok(true) => Ok(0),
            Ok(false) | Err(_) => Err(api::leave::Unobservable),
        }
    }
    fn compute_cleanup(&mut self) -> Option<Result<api::leave::ComputeCleanup, String>> {
        Some(self.none_owned().and_then(|empty| {
            if empty {
                Ok(api::leave::ComputeCleanup::NoneOwned)
            } else {
                Err("member_compute_cleanup_unavailable".into())
            }
        }))
    }
    fn now_seconds(&self) -> f64 {
        app_host::now()
    }
}

/// `DemoApp.start` -> `authority.start()` for a joined target (app.py:613-626 @ f4e366e9): the
/// retained membership resumes over the member's seed path (generation + 1). A dead relay or a lapsed
/// membership leaves the Mac started but not running (`recovery_error`); any other refusal keeps its
/// code on stderr and the Mac not running (v0.1.4 refuses to start the app then).
pub fn resume_membership(network: &Arc<Network>, authority: &Mutex<Authority>) {
    let cell = network.cell(authority);
    let options = network.seams.member_seed.clone();
    if let Err(code) = joined_member::resume_membership(&cell, &options) {
        eprintln!("member_resume_failed: {code}");
    }
}

/// `member_leave.resume_leave` at start for a joined Mac (member_leave.py:295-306 @ f4e366e9): a
/// completed leave keeps the Mac fenced and left, an interrupted one is resumed over the same steps.
/// A failure keeps its code on stderr (the leave's own record keeps the debt).
pub fn resume_leave(
    network: &Arc<Network>,
    authority: &Mutex<Authority>,
    model: Option<ModelSource>,
) {
    struct Resume {
        leave: PackagedLeave,
    }
    impl api::leave::Steps for Resume {
        fn step(&mut self, name: &str) -> Result<(), Option<String>> {
            if name == "withdraw_consent" {
                let now = api::leave::LeaveWorker::now_seconds(&self.leave);
                return api::preferences::standing_consent::withdraw(&self.leave.network.root, now)
                    .map(|_| ())
                    .map_err(|e| Some(e.0.to_owned()));
            }
            api::leave::LeaveWorker::step(&mut self.leave, name)
        }
        fn inflight(&mut self) -> Result<i64, api::leave::Unobservable> {
            api::leave::LeaveWorker::inflight(&mut self.leave)
        }
        fn compute_cleanup(&mut self) -> Option<Result<api::leave::ComputeCleanup, String>> {
            api::leave::LeaveWorker::compute_cleanup(&mut self.leave)
        }
        fn mark(&mut self, fence: bool, left: bool) {
            self.leave.network.mark(fence, left);
        }
    }
    let (target, root) = {
        let a = lock(authority);
        (a.target, a.root.clone())
    };
    if !target {
        return;
    }
    let leave = api::leave::MemberLeave::new(&root, api::leave::Limits::default());
    let mut steps = Resume {
        leave: PackagedLeave {
            network: network.clone(),
            model,
        },
    };
    if let Err(error) =
        api::leave::resume_leave(&leave, &mut steps, &mut api::leave::SystemClock::default())
    {
        eprintln!("member_leave_resume_failed: {error:?}");
    }
}

/// The parts of v0.1.4's `status()` (app.py:1263-1283) the packaged status takes from this host:
/// `network`, `installation_role`, `resource_policy`, `authority`, `join_flow` (the join-consent
/// document, `standing_disclosure`, for both roles) and `leave`.
pub fn status_overlay(status: &mut Value, authority: Option<&Mutex<Authority>>, network: &Network) {
    let pending = lock(&network.pending).clone();
    status["network"] = network.status().to_value();
    let Some(authority) = authority else {
        // The authority's state was refused at bind (an unreadable or invalid role file among
        // them, app.py:595-611): no role is claimed for it.
        status["installation_role"] = Value::Null;
        return;
    };
    let a = lock(authority);
    status["installation_role"] = json!(if a.target { "target" } else { "owner" });
    status["resource_policy"] = api::preferences::resource_policy::read(&a.root)
        .ok()
        .flatten()
        .unwrap_or(Value::Null);
    status["authority"] = joined_member::authority_status(&a).to_value();
    status["join_flow"] =
        joined_member::join_flow_status(&a.root, a.target, &a.windows, &pending).to_value();
    status["leave"] = if a.target {
        let leave = api::leave::MemberLeave::new(&a.root, api::leave::Limits::default());
        api::leave::status_view(&leave).map_or(Value::Null, |view| Json::from(view).to_value())
    } else {
        Value::Null
    };
}

/// The packaged action root's delegate: the node service's serving actions, with
/// `/api/network/start` answered by this host's relay (`_start_public_relay`, app.py:676-740) once
/// the persistent backend ran its closed/role/running-relay checks and the shape check.
pub struct PackagedActions {
    serving: ServingActions,
    network: Arc<Network>,
    /// `None`: the authority's state was refused at bind; the Start answers its code.
    authority: Result<Arc<Mutex<Authority>>, &'static str>,
}
impl PackagedActions {
    pub fn new(
        serving: ServingActions,
        network: Arc<Network>,
        authority: Result<Arc<Mutex<Authority>>, &'static str>,
    ) -> Self {
        Self {
            serving,
            network,
            authority,
        }
    }
}
impl Backend for PackagedActions {
    fn prepare(&mut self, action: Action, body: &Map<String, Value>) -> Result<(), PublicError> {
        if action == Action::NetworkStart {
            return Ok(());
        }
        self.serving.prepare(action, body)
    }
    fn execute(&mut self, action: Action, body: &Map<String, Value>) -> Result<Value, PublicError> {
        self.execute_json(action, body)
            .map(|answer| answer.to_value())
    }
    fn execute_json(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        if action == Action::NetworkStart {
            let authority = self
                .authority
                .as_ref()
                .map_err(|code| PublicError::new(409, code))?;
            return self.network.start(authority, body);
        }
        self.serving.execute_json(action, body)
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        self.serving.publish()
    }
}

/// Quit's relay step, `self.public_relay.close()` in `DemoApp.close` (app.py:1848-1850 @ f4e366e9):
/// started as Quit begins, on its own thread (a quick tunnel's child is terminated and waited for),
/// and awaited no longer than the relay's own close budgets (`TemporaryHTTPSRelay.close`'s process
/// waits and reader join, public_relay.py:446-448). A close still running then is reported
/// `relay_stop_failed` and left to the process exit.
pub struct RelayClose {
    network: Arc<Network>,
    closing: RelayClosing,
}
enum RelayClosing {
    NotStarted,
    Running {
        worker: std::thread::JoinHandle<()>,
        until: std::time::Instant,
    },
    /// Past the deadline: the close is left to the process exit (the handle stays owned here).
    Late(std::thread::JoinHandle<()>),
    Done(Result<(), desktop::lifecycle::CleanupError>),
}
impl RelayClose {
    pub fn new(network: Arc<Network>) -> Self {
        Self {
            network,
            closing: RelayClosing::NotStarted,
        }
    }
    /// The relay's own close budgets, summed.
    fn allowance() -> Result<std::time::Duration, desktop::lifecycle::CleanupError> {
        use budgets::{BudgetId as B, Entries};
        [
            B::PyMyceliumA13MacosPublicRelayTemporaryhttpsrelayCloseSelfProcessWaitL446,
            B::PyMyceliumA13MacosPublicRelayTemporaryhttpsrelayCloseSelfProcessWaitL447,
            B::PyMyceliumA13MacosPublicRelayTemporaryhttpsrelayCloseSelfReaderJoinL448,
            B::PyMyceliumA13MacosPublicRelaySeedrelayproxyCloseSelfThreadJoinL239,
        ]
        .into_iter()
        .try_fold(std::time::Duration::ZERO, |total, id| {
            id.span(Entries::ZERO)
                .map(|span| total + std::time::Duration::from_millis(span.millis()))
                .map_err(|_| desktop::lifecycle::CleanupError::InvalidBudget)
        })
    }
    /// Start the close at once (Quit's begin).
    pub fn begin(&mut self) {
        if !matches!(self.closing, RelayClosing::NotStarted) {
            return;
        }
        let allowance = match Self::allowance() {
            Ok(allowance) => allowance,
            Err(error) => {
                eprintln!("relay_stop_failed: relay_close_budget_invalid");
                self.closing = RelayClosing::Done(Err(error));
                return;
            }
        };
        let network = self.network.clone();
        self.closing = match std::thread::Builder::new()
            .name("packaged-relay-close".into())
            .spawn(move || network.quit())
        {
            Ok(worker) => RelayClosing::Running {
                worker,
                until: std::time::Instant::now() + allowance,
            },
            Err(_) => {
                eprintln!("relay_stop_failed: relay_close_thread_unavailable");
                RelayClosing::Done(Err(desktop::lifecycle::CleanupError::Poll))
            }
        };
    }
    /// `None` while the close runs, else its result (kept).
    pub fn poll(&mut self) -> Option<Result<(), desktop::lifecycle::CleanupError>> {
        self.begin();
        match std::mem::replace(&mut self.closing, RelayClosing::Done(Ok(()))) {
            RelayClosing::Running { worker, until }
                if !worker.is_finished() && std::time::Instant::now() < until =>
            {
                self.closing = RelayClosing::Running { worker, until };
                None
            }
            RelayClosing::Running { worker, .. } if !worker.is_finished() => {
                eprintln!("relay_stop_failed: relay_close_deadline");
                self.closing = RelayClosing::Late(worker);
                Some(Err(desktop::lifecycle::CleanupError::Deadline))
            }
            RelayClosing::Late(worker) => {
                self.closing = RelayClosing::Late(worker);
                Some(Err(desktop::lifecycle::CleanupError::Deadline))
            }
            RelayClosing::Running { worker, .. } => {
                let result = worker.join().map_err(|_| {
                    eprintln!("relay_stop_failed: relay_close_panicked");
                    desktop::lifecycle::CleanupError::Poll
                });
                self.closing = RelayClosing::Done(result);
                Some(result)
            }
            RelayClosing::NotStarted => None,
            RelayClosing::Done(result) => {
                self.closing = RelayClosing::Done(result);
                Some(result)
            }
        }
    }
}
