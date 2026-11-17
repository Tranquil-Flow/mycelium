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
use crate::{
    app_host::ModelSource,
    packaged_run::{Authority, Managed},
    router_port::ServingActions,
};
mod work;
use api::{
    actions::{Action, Backend},
    app_host::{self, Cell, HostView, NETWORK_FILE, RelayOrigin, cap},
    app_status, endpoint_recovery, joined_member,
    persistent::RelayState,
    preferences::private_state,
    relay::{EndpointHost, Hooks, OwnerRelay, Probe, Proxy, RichHandler, SeedBackend, StartGuard},
    schema::{PublicError, Raised},
    seed::SeedOptions,
    windows::PendingWindows,
    wire::Json,
};
use serde_json::{Map, Value, json};
use std::{
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex, MutexGuard, OnceLock, PoisonError,
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
    /// The installed native sidecar a joined Mac names in its own descriptors (`installed_sidecar`,
    /// native_runtime.py:8-22 @ f4e366e9): `None` is the app's own, beside the executable
    /// (`native/mycelium-iroh-sidecar`); a test names an owned executable file it never starts.
    pub sidecar: Option<PathBuf>,
    /// v0.1.4's `_device_memory_bytes()` test seam (app.py:1123-1126 @ f4e366e9): the RAM that
    /// bounds this Mac's per-run memory consent. `None` (the app's own): this Mac's `hw.memsize`.
    pub device_memory_bytes: Option<u64>,
    /// Where the work endpoints bind: the app's own is this Mac's private LAN address, read each
    /// time a work service or an owner run starts ([`crate::packaged_work::WorkNetwork::ThisMac`]);
    /// a test or caller that passes a [`crate::packaged_work::WorkNetwork::Fixed`] choice keeps it.
    pub work_network: crate::packaged_work::WorkNetwork,
    /// The stage-worker program bundle admission captured (lane BND, REPORT-laneTEN D2): the
    /// work services this host starts (a joined Mac's member work, the owner's own contributor)
    /// launch exactly it for every stage, never `MYCELIUM_COMPILER_NATIVE_BINARY`. The app's
    /// composition sets it from `RunConfig::native_program`
    /// (`DesktopServices::packaged_with_run` overwrites whatever is here); only a test that builds a
    /// [`Network`] itself sets it directly. `None`: every stage prepare refuses
    /// `native_binary_pin_required`.
    pub native_program: Option<api::native_compiler::NativeProgram>,
    /// Lane XB patch 2 (D4): the one relay this Mac's endpoints register at in a run across
    /// networks, and the only relay a signed run may name: Mycelium's ([`Reach::Shipped`]); a
    /// test overrides it with a dead loopback relay so nothing leaves the host.
    ///
    /// [`Reach::Shipped`]: runtime::iroh_host::Reach::Shipped
    pub relay: runtime::iroh_host::Reach,
}
impl Default for NetworkConfig {
    /// The app's own: the bundled `native/cloudflared` next to the executable, no seams, 20 s.
    fn default() -> Self {
        Self {
            renewal: budgets::BudgetId::PyMyceliumA13MacosAppDemoappRenewLoopSelfClosedWaitL917,
            hooks: Arc::new(|| Hooks::pinned(desktop::package::bundled_relay_client())),
            seams: JoinSeams::default(),
            sidecar: None,
            device_memory_bytes: None,
            work_network: crate::packaged_work::WorkNetwork::default(),
            native_program: None,
            relay: runtime::iroh_host::Reach::Shipped,
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
    /// `DemoApp.demo_target`'s last card (`status_snapshot`), written by the standing loop.
    demo_target: Mutex<Option<Value>>,
    /// [`NetworkConfig::sidecar`].
    sidecar: Option<PathBuf>,
    /// `_device_memory_bytes()`: the composition's seam, else this Mac's RAM, read once.
    device_memory: OnceLock<Option<u64>>,
    /// Quit's hold on a relay a Start is still bringing up (`self.guard`, app.py:1344, 1831).
    start_guard: StartGuard,
    /// A member renewal pass is on the wire (`RemoteMacMember.renew` holds the peer lock across
    /// it, peer.py:1037-1060); the leave's `disconnect` waits for it.
    renewing: Mutex<bool>,
    renewed: std::sync::Condvar,
    work: Mutex<Option<crate::packaged_work::Service>>,
    work_session: crate::packaged_work::SessionCell,
    work_network: crate::packaged_work::WorkNetwork,
    /// Lane XB patch 2: this Mac's reach state (its relay, the owner run's choice, the active
    /// endpoints' mode, a joined Mac's named refusal): `crate::packaged_work::reach`.
    pub(crate) reach: crate::packaged_work::reach::State,
    /// [`NetworkConfig::native_program`].
    native_program: Option<api::native_compiler::NativeProgram>,
    work_closed: AtomicBool,
    /// `_relay_is_serving` (app.py:770-777): raised by the run chain while an owner run is active, so
    /// a relay may extend its window only then. The packaged host has no swarm publisher yet, so the
    /// published-listing half of the v0.1.4 predicate has nothing to read; it arrives with one.
    serving: std::sync::Arc<AtomicBool>,
    /// The owner's chat behind the relay (`_bind_public_routes`'s `bind_chat`, app.py:1073-1077):
    /// the run's chat service with the relay's client key (lane CHATS item 2). `None` until the
    /// composition gives one (`DesktopServices::with_chat_run`).
    relay_chat: Mutex<Option<RichHandler>>,
    /// The owner run's fields (`DemoApp.local_contributor`), bound by the packaged run host: the
    /// relay restart refuses to relocate the seed under an enrolled contributor (lane B, B-01).
    run_fields: Mutex<std::sync::Weak<Mutex<api::owner_ports::RunFields>>>,
}
fn lock<T>(cell: &Mutex<T>) -> MutexGuard<'_, T> {
    // Plain values and handles: a panicked holder cannot leave them half-written.
    cell.lock().unwrap_or_else(PoisonError::into_inner)
}
/// A member renewal pass on the wire ([`Network::renew_member`]): ends on every exit, unwinding
/// included, and wakes a leave waiting for it.
struct RenewalPass<'a>(&'a Network);
impl Drop for RenewalPass<'_> {
    fn drop(&mut self) {
        *lock(&self.0.renewing) = false;
        self.0.renewed.notify_all();
    }
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
            demo_target: Mutex::default(),
            sidecar: config.sidecar,
            device_memory: config
                .device_memory_bytes
                .map_or_else(OnceLock::new, |bytes| OnceLock::from(Some(bytes))),
            start_guard: StartGuard::default(),
            renewing: Mutex::new(false),
            renewed: std::sync::Condvar::new(),
            work: Mutex::default(),
            work_session: Arc::new(Mutex::new(None)),
            work_network: config.work_network,
            reach: crate::packaged_work::reach::State::new(config.relay),
            native_program: config.native_program,
            work_closed: AtomicBool::new(false),
            serving: std::sync::Arc::new(AtomicBool::new(false)),
            relay_chat: Mutex::new(None),
            run_fields: Mutex::new(std::sync::Weak::new()),
        })
    }
    /// The owner run's fields this host's relay reads for `local_contributor` (lane B, B-01).
    pub fn bind_run_fields(&self, run: &Arc<Mutex<api::owner_ports::RunFields>>) {
        *lock(&self.run_fields) = Arc::downgrade(run);
    }
    /// `self.local_contributor is not None`: the owner's contributor is enrolled (its state is
    /// present, whether or not a renewal has its Session out).
    pub fn local_contributor_present(&self) -> bool {
        let run = lock(&self.run_fields).upgrade();
        run.is_some_and(|run| lock(&run).contributor.is_some())
    }
    /// The chat the relay's `/chat` and `/chat/api/*` answer from (`bind_chat`, app.py:1073-1077),
    /// bound at every relay start ([`EndpointHost::bind_public_routes`]). The packaged composition
    /// gives it before the window opens (`packaged::compose` -> `chat_run`), so before any relay.
    pub fn bind_relay_chat(&self, handler: Option<RichHandler>) {
        *lock(&self.relay_chat) = handler;
    }
    /// The handler [`Network::bind_relay_chat`] keeps (what the relay's `/chat` routes call).
    pub fn relay_chat(&self) -> Option<RichHandler> {
        lock(&self.relay_chat).clone()
    }
    /// The signal `_relay_is_serving` answers from: the run chain raises it for the length of a run.
    pub fn set_serving(&self, serving: bool) {
        self.serving
            .store(serving, std::sync::atomic::Ordering::SeqCst);
    }
    /// The probe a relay reads when it decides whether its window may extend. Cloned into the relay's
    /// lifetime thread, so it shares the flag rather than borrowing this host.
    fn serving_probe(&self) -> Probe {
        let flag = self.serving.clone();
        std::sync::Arc::new(move || Ok(flag.load(std::sync::atomic::Ordering::SeqCst)))
    }
    /// `_device_memory_bytes()` (app.py:1123-1126 @ f4e366e9): the RAM that bounds this Mac's
    /// per-run memory consent, for the join's caps check and the status's memory choices alike: the
    /// composition's seam, else `host_memory_bytes()` (this Mac's `hw.memsize`, read once).
    pub fn device_memory_bytes(&self) -> Option<u64> {
        *self.device_memory.get_or_init(host_memory_bytes)
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
    ///
    /// Lane B, B5/QV (owner ruling 15 Nov, row 57): `Err` names a work service whose close did
    /// not prove its member custody (a stage it still holds): Quit must not say `Verified` then.
    pub fn quit(&self) -> Result<(), String> {
        self.work_closed.store(true, Ordering::SeqCst);
        let work = self.close_work_proven();
        self.start_guard.close();
        self.close();
        work
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
        if fence || left {
            self.close_work();
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
        // Marked before the pass reads `_left`: a leave that set it either sees this pass and
        // waits for it, or this pass sees `_left` and does not start. Cleared by `RenewalPass`'s
        // drop, so a pass that unwinds never leaves a leave waiting on it (lane FLK, FLK-02).
        *lock(&self.renewing) = true;
        let _pass = RenewalPass(self);
        app_host::renew_member(&self.cell(authority));
        self.sync_work_session(authority);
        if membership_state(authority).as_deref() != Some("current") {
            self.close_work();
        }
    }
    /// The leave's `disconnect` after `_left` (member_leave.py:237-252 @ f4e366e9: its
    /// `peer.set_contribution_enabled` takes the lock a renewal holds across its seed round
    /// trip): a pass already on the wire ends first, so no lease moves once the leave answered.
    /// Bounded by the member seed's request budget (peer.py:117), its heartbeat and answer
    /// (`api::renewal::allowance`, also a renewal flight's deadline).
    fn await_renewal_pass(&self) {
        let bound = api::renewal::allowance();
        let renewing = lock(&self.renewing);
        let (renewing, _) = self
            .renewed
            .wait_timeout_while(renewing, bound, |renewing| *renewing)
            .unwrap_or_else(PoisonError::into_inner);
        if *renewing {
            eprintln!("member_leave_renewal_still_running");
        }
    }
    /// `/api/network/restart` (app.py:1444-1475 @ f4e366e9, lane B B10) as [`Network::start`]
    /// runs the Start: the relay is taken out of this host while the new one starts.
    pub fn restart(
        &self,
        authority: &Arc<Mutex<Authority>>,
        body: &serde_json::Map<String, Value>,
    ) -> Result<Json, PublicError> {
        let mut relay = std::mem::replace(
            &mut *lock(&self.relay),
            Self::fresh(&self.root, &self.hooks),
        );
        relay.set_start_guard(self.start_guard.clone());
        let probe = self.serving_probe();
        let outcome = relay.restart_public_relay(
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
    /// `/api/network/recovery-card` (app.py:1436-1442, `refresh_endpoint_card`,
    /// endpoint_recovery.py:117-142 @ f4e366e9, lane B B10): the owner's persisted card reissued
    /// for the origin its seed answers on now.
    pub fn recovery_card(
        &self,
        authority: &Arc<Mutex<Authority>>,
        body: &serde_json::Map<String, Value>,
    ) -> Result<Json, PublicError> {
        let (seed, root) = {
            let a = lock(authority);
            (a.seed.clone(), a.root.clone())
        };
        let configured = app_host::configured_public_origin(&self.cell(authority));
        let old = endpoint_recovery::load_card(&root)?;
        let (Some(old), Some(configured), Some(seed)) = (old, configured, seed) else {
            // `endpoint_recovery_requires_retained_authority` (a `ValueError`)
            return Err(endpoint_recovery::value_error());
        };
        let c = &seed.coordinator;
        let card = endpoint_recovery::refresh_endpoint_card(
            c.signer(),
            c.swarm_id(),
            c.seed_node_id(),
            &configured,
            &old,
            body.get("previous_origin"),
            app_host::now(),
        )?;
        endpoint_recovery::store_card(&root, &card)?;
        Ok(Json::from(json!({"recovery_card": card})))
    }
    /// `/api/network/reconnect` (app.py:1427-1435 @ f4e366e9, lane B B10): the joined Mac follows
    /// its owner's seed to the origin the card names, over its own seed path.
    pub fn reconnect(
        &self,
        authority: &Arc<Mutex<Authority>>,
        body: &serde_json::Map<String, Value>,
    ) -> Result<Json, PublicError> {
        let options = self.seams.member_seed.clone();
        endpoint_recovery::reconnect_peer(&self.cell(authority), &options, &body["card"])
            .map(Json::from)
    }
    /// Quit has closed this host's relay start (`application_closed` for every later action).
    pub fn closed(&self) -> bool {
        self.start_guard.closed()
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
        // `_relay_is_serving` (app.py:770-777): the relay extends its own window only while an owner
        // run is active. This host reports the run chain's signal; it used to answer false always,
        // which is why the card could promise a renewal every 30 minutes and show 0 renewals while
        // the tunnel closed mid-run at its first deadline.
        let probe = self.serving_probe();
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
    target_membership_state(&lock(authority))
}
/// A joined target's own `membership_state` (`TargetApplication.status()`) over an authority the
/// caller already holds.
fn target_membership_state(a: &Authority) -> Option<String> {
    let status = joined_member::target_status(a);
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
    type Managed = Managed;
    fn with<R>(&self, f: impl FnOnce(&mut HostView<'_, Managed>) -> R) -> R {
        // The relay's lock is released before the authority's is taken.
        let origin = Origin(self.network.origin());
        let left = self.network.left();
        let mut authority = lock(self.authority);
        f(&mut HostView::new(&mut authority, &origin, false, left))
    }
    fn try_with<R>(&self, f: impl FnOnce(&mut HostView<'_, Managed>) -> R) -> Option<R> {
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
    /// The running seed is closed and opened again on the configured origin.
    fn reopen_seed(&self) -> Result<(), PublicError> {
        let old = lock(&self.authority).seed.take();
        if let Some(old) = old {
            old.close();
            app_host::open_owner_seed(&self.cell())?;
        }
        Ok(())
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
        self.reopen_seed()
    }
    /// `endpoint_recovery.relocate_authority` (endpoint_recovery.py:39-57 @ f4e366e9, lane B B10):
    /// a retained configuration and seed sign the card from the old origin to the new one; the new
    /// configuration and the card are persisted, then the seed is reopened on the new origin.
    fn relocate_authority(
        &mut self,
        origin: &str,
        port: u16,
    ) -> Result<Option<Value>, PublicError> {
        let previous = self.configured_public_origin();
        let (seed, root) = {
            let a = lock(&self.authority);
            (a.seed.clone(), a.root.clone())
        };
        let (Some(previous), Some(seed)) = (previous, seed) else {
            return Err(
                PublicError::new(409, "network_recovery_requires_retained_authority")
                    .raised_by(Raised::Authority),
            );
        };
        let c = &seed.coordinator;
        let card = endpoint_recovery::endpoint_card(
            c.signer(),
            c.swarm_id(),
            c.seed_node_id(),
            &Value::from(previous),
            &Value::from(origin),
            app_host::now(),
        )?;
        self.configure_public_endpoint(origin, port)?;
        // Identity, journals and the card stay inspectable whatever the reopen does.
        endpoint_recovery::store_card(&root, &card)?;
        self.reopen_seed()?;
        Ok(Some(card))
    }
    fn seed_backend(&self) -> Option<Arc<dyn SeedBackend>> {
        let a = lock(&self.authority);
        joined_member::seed_backend(a.seed.clone(), a.owner_authority().is_some())
    }
    fn set_running(&mut self) {
        lock(&self.authority).running = true;
    }
    /// `_bind_public_routes` (app.py:1070-1079): the pairing windows, the member-report inbox and
    /// the link probe, and the owner's chat (`bind_chat`, :1073-1077, lane CHATS item 2). The
    /// swarm entry (:1078-1079) is not bound: the packaged host has no published swarm gateway.
    fn bind_public_routes(&mut self, proxy: &Arc<Proxy>) {
        let authority = self.authority.clone();
        proxy.bind_local(Some(joined_member::public_route::<Managed>(Arc::new(
            move || {
                let a = lock(&authority);
                (a.windows.clone(), a.seed.clone())
            },
        ))));
        proxy.bind_chat(self.network.relay_chat());
    }
    /// `_materialize_pending_windows`: codes asked for before the relay exist from now on.
    fn local_contributor_present(&self) -> bool {
        self.network.local_contributor_present()
    }
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
            "disconnect" => {
                self.network.mark(false, true);
                self.network.await_renewal_pass();
            }
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

/// `host_memory_bytes()` (app.py:267-273): this Mac's physical memory, `None` when unknown. Read
/// with the same `hw.memsize` probe the native driver uses (native.rs `host`).
fn host_memory_bytes() -> Option<u64> {
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

/// The parts of v0.1.4's `status()` (app.py:1244-1283) the packaged status takes from this host:
/// `network`, and from the authority, through the builder the headless launcher shares
/// (`api::app_status::shared`): `role`, `model_contract`, `run_model` (with this Mac's memory
/// choices), `fast_mode`, `installation_role`, `resource_policy`, `running` (the app's own flag,
/// replacing the node service's native-request flag), `authority`, `renewal_error`, `leave` and
/// `join_flow`. A document that is not a JSON object is left as it is, never indexed into a panic
/// (REVIEW-lanePH2 F8).
pub fn status_overlay(status: &mut Value, authority: Option<&Mutex<Authority>>, network: &Network) {
    let Some(fields) = status.as_object_mut() else {
        return;
    };
    let pending = lock(&network.pending).clone();
    fields.insert("network".into(), network.status().to_value());
    // Lane XB patch 2 (D1, XB-06): the ACTIVE endpoints' reach, the owner's choice for its
    // next run, this Mac's portmapping preference and a named error.
    fields.insert("network_reach".into(), network.reach.status(&network.root));
    let Some(authority) = authority else {
        // The authority's state was refused at bind (an unreadable or invalid role file among
        // them, app.py:595-611): no role is claimed for it.
        status["installation_role"] = Value::Null;
        return;
    };
    // `os.environ.get(name)`, as the host's own `environment` reads it.
    let env = |name: &str| std::env::var_os(name).map(|v| v.to_string_lossy().into_owned());
    let (run_model_env, fast_mode_env) = (
        env(api::preferences::run_model::ENV),
        env(api::persistent::TOKEN_PATH_ENV),
    );
    let facts = app_status::Facts {
        run_model_env: run_model_env.as_deref(),
        fast_mode_env: fast_mode_env.as_deref(),
        device_memory_bytes: network.device_memory_bytes(),
    };
    let a = lock(authority);
    for (key, value) in app_status::shared(&a, &pending, &facts) {
        fields.insert(key.into(), value.to_value());
    }
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
        if matches!(
            action,
            Action::NetworkStart
                | Action::NetworkRestart
                | Action::NetworkRecoveryCard
                | Action::NetworkReconnect
        ) {
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
        // Lane B, B10: the endpoint recovery routes (app.py:1427-1475 @ f4e366e9). Each refuses
        // the other role (and a restart without a swarm) as its consent refusal, as the source.
        if matches!(
            action,
            Action::NetworkRestart | Action::NetworkRecoveryCard | Action::NetworkReconnect
        ) {
            if self.network.closed() {
                return Err(PublicError::new(400, "application_closed"));
            }
            let authority = self
                .authority
                .as_ref()
                .map_err(|code| PublicError::new(409, code))?;
            let target = lock(authority).target;
            let swarm = app_host::swarm_created(&self.network.cell(authority));
            let allowed = match action {
                Action::NetworkReconnect => target,
                Action::NetworkRestart => !target && swarm,
                _ => !target,
            };
            if !allowed {
                return Err(PublicError::new(400, "network_consent_required"));
            }
            return match action {
                Action::NetworkRestart => self.network.restart(authority, body),
                Action::NetworkRecoveryCard => self.network.recovery_card(authority, body),
                _ => self.network.reconnect(authority, body),
            };
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
    /// TEST ONLY (lane B, CP3-01): the close thread waits for this before it closes anything, as
    /// a tunnel child still unreaped after its terminate/wait and kill/wait would hold it.
    #[cfg(test)]
    held: Option<std::sync::mpsc::Receiver<()>>,
}
enum RelayClosing {
    NotStarted,
    Running {
        worker: std::thread::JoinHandle<Result<(), String>>,
        until: std::time::Instant,
    },
    /// Past the deadline: the close is left to the process exit (the handle stays owned here).
    Late(std::thread::JoinHandle<Result<(), String>>),
    Done(Result<(), desktop::lifecycle::CleanupError>),
}
/// The relay step's close did not end within its own close budgets (REVIEW-laneCHATPAGE-p3-astra
/// CP3-01): v0.1.4's `public_relay.close()` raises then (its `process.wait(5)` after `kill()`,
/// public_relay.py:445-447 @ f4e366e9), `DemoApp.close` records `relay_stop_failed`
/// (app.py:1848-1850) and the launcher exits 3 (`cleanup_incomplete`, __main__.py:219-223). It is
/// not the run's join running out (`Deadline`, exit 0).
const RELAY_STOP_FAILED: desktop::lifecycle::CleanupError =
    desktop::lifecycle::CleanupError::Unproven("relay_stop_failed");
impl RelayClose {
    pub fn new(network: Arc<Network>) -> Self {
        Self {
            network,
            closing: RelayClosing::NotStarted,
            #[cfg(test)]
            held: None,
        }
    }
    /// TEST ONLY: see [`RelayClose::held`].
    #[cfg(test)]
    pub(crate) fn hold(&mut self, release: std::sync::mpsc::Receiver<()>) {
        self.held = Some(release);
    }
    /// The relay's own close budgets, summed.
    pub(crate) fn allowance() -> Result<std::time::Duration, desktop::lifecycle::CleanupError> {
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
    /// Start the close now (Quit: once the run's close ended or its allowance ran out,
    /// `packaged::WindowQuit::gate_relay`, as v0.1.4 closes its relay after `demo_app.close`).
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
        #[cfg(test)]
        let held = self.held.take();
        self.closing = match std::thread::Builder::new()
            .name("packaged-relay-close".into())
            .spawn(move || {
                #[cfg(test)]
                if let Some(held) = held {
                    held.recv().unwrap_or(());
                }
                network.quit()
            }) {
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
    /// `None` until the close was started and while it runs, else its result (kept).
    pub fn poll(&mut self) -> Option<Result<(), desktop::lifecycle::CleanupError>> {
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
                Some(Err(RELAY_STOP_FAILED))
            }
            RelayClosing::Late(worker) => {
                self.closing = RelayClosing::Late(worker);
                Some(Err(RELAY_STOP_FAILED))
            }
            RelayClosing::Running { worker, .. } => {
                let result = match worker.join() {
                    Ok(Ok(())) => Ok(()),
                    // Lane B, B5/QV: the joined Mac's work service still holds a stage (its
                    // close printed `packaged_work_cleanup_unproven:<code>`).
                    Ok(Err(_)) => Err(desktop::lifecycle::CleanupError::Unproven(
                        "member_work_cleanup_unproven",
                    )),
                    Err(_) => {
                        eprintln!("relay_stop_failed: relay_close_panicked");
                        Err(desktop::lifecycle::CleanupError::Poll)
                    }
                };
                self.closing = RelayClosing::Done(result);
                Some(result)
            }
            RelayClosing::NotStarted => {
                self.closing = RelayClosing::NotStarted;
                None
            }
            RelayClosing::Done(result) => {
                self.closing = RelayClosing::Done(result);
                Some(result)
            }
        }
    }
}

// ---- PH3b S2: the joined Mac's standing loop (`DemoApp.standing_thread`) and its member card.

/// `app._resource_check()`'s verdict for the private root it is given.
pub type ResourceGate = Box<dyn Fn(&Path) -> Option<&'static str> + Send + Sync>;
/// What the packaged member agent reads from its host besides the authority and the network.
pub struct MemberSeams {
    /// `self.closed`: Quit's fence (the node service's shutdown).
    pub closed: Box<dyn Fn() -> bool + Send + Sync>,
    /// `app._resource_check()`'s observation of this Mac against the saved policy of `root`.
    pub resource: ResourceGate,
    /// `DemoApp.acquisition`.
    pub acquisition: Box<dyn Fn() -> Option<api::acquisition::Acquisition> + Send + Sync>,
    /// `_ensure_whole_model` (demo_target.py:600-607): start this Mac's own acquisition of the
    /// owner's announced model, so a joined Mac with no snapshot fetches one instead of refusing.
    pub start_whole_model: StartWholeModel,
}
/// The `start_whole_model` seam: the announced model choice in, the start's refusal out.
type StartWholeModel = Box<dyn Fn(&Value) -> Result<(), String> + Send + Sync>;
/// The member's one bootstrap worker slot (`app.authority`'s control worker on a joined Mac).
#[derive(Default)]
struct MemberSlot {
    worker: Option<api::member_agent::PumpWorker>,
    failure: Option<api::bootstrap_process::CleanupFailure>,
    incarnation: Arc<()>,
}
/// The packaged host as `api::member_agent`'s host: the authority's role, `running` and live
/// lease, the network's `_left`/`_leave_fence` and seed path, the node's Quit fence, the model
/// source's resource gate and acquisition, and the member's own bootstrap worker.
pub struct PackagedMember {
    authority: Arc<Mutex<Authority>>,
    network: Arc<Network>,
    seams: MemberSeams,
    lifecycle: Arc<Mutex<()>>,
    slot: Mutex<MemberSlot>,
    /// TEST ONLY (lane FLK, FLK-01): run once inside [`MemberHost::accept_context`], after it
    /// read this Mac's membership state, so a test can start a renewal exactly there.
    #[cfg(test)]
    pub(crate) inside_accept_context: Mutex<Option<Box<dyn FnOnce() + Send>>>,
}
impl PackagedMember {
    pub fn new(
        authority: Arc<Mutex<Authority>>,
        network: Arc<Network>,
        seams: MemberSeams,
    ) -> Arc<Self> {
        Arc::new(Self {
            authority,
            network,
            seams,
            lifecycle: Arc::default(),
            slot: Mutex::default(),
            #[cfg(test)]
            inside_accept_context: Mutex::default(),
        })
    }
    fn state(&self) -> api::member_agent::HostState {
        let (root, target, running) = {
            let a = lock(&self.authority);
            (a.root.clone(), a.target, a.running)
        };
        api::member_agent::HostState {
            root,
            target,
            running,
            left: self.network.left(),
            closed: (self.seams.closed)(),
        }
    }
}
impl api::member_agent::MemberHost for PackagedMember {
    type Worker = api::member_agent::PumpWorker;
    fn state(&self) -> api::member_agent::OResult<api::member_agent::HostState> {
        Ok(PackagedMember::state(self))
    }
    fn lease(&self) -> Option<f64> {
        let a = lock(&self.authority);
        a.session_view.and_then(|(_, lease)| lease).or_else(|| {
            a.session
                .as_ref()
                .and_then(api::member::Session::lease_expires_at)
        })
    }
    /// `DemoApp._resource_check` (app.py:929-947): a member leave fence refuses first.
    fn resource_check(&self) -> Option<String> {
        if self.network.fenced() {
            return Some("owner_stop".into());
        }
        let root = lock(&self.authority).root.clone();
        (self.seams.resource)(&root).map(str::to_owned)
    }
    fn acquisition(&self) -> Option<api::acquisition::Acquisition> {
        (self.seams.acquisition)()
    }
    fn start_whole_model(&self, choice: &Value) -> Result<(), String> {
        (self.seams.start_whole_model)(choice)
    }
    fn member_seed_options(&self) -> SeedOptions {
        self.network.seams.member_seed.clone()
    }
    fn installed_sidecar(&self) -> Result<PathBuf, api::installation_descriptor::Failure> {
        api::owner_ports::installed_sidecar(self.network.sidecar.clone())
    }
    fn lifecycle(&self) -> api::member_agent::OResult<Arc<Mutex<()>>> {
        Ok(self.lifecycle.clone())
    }
    fn slot<R>(
        &self,
        f: impl FnOnce(api::member_agent::Slot<'_, Self::Worker>) -> R,
    ) -> api::member_agent::OResult<R> {
        let mut slot = lock(&self.slot);
        let state = PackagedMember::state(self);
        let MemberSlot {
            worker,
            failure,
            incarnation,
        } = &mut *slot;
        Ok(f(api::member_agent::Slot {
            worker,
            failure,
            incarnation,
            state,
        }))
    }
    fn program(
        &self,
        root: &Path,
    ) -> api::member_agent::OResult<api::bootstrap_process::TrustedBootstrap> {
        api::member_agent::bootstrap_program(root)
    }
    /// `auto_step_context()` (app.py:1168-1172): the binding only while this Mac is a target
    /// whose membership is `current`; the saved resource policy; the member's join seam.
    fn accept_context(&self) -> Option<api::member_agent::AcceptContext> {
        // The renewal loop holds the live Session for one seed heartbeat
        // (`app_host::renew_member`, `session_view` set meanwhile). For exactly that flight the
        // work session keeps the preceding snapshot (`Network::sync_work_session`), the same
        // membership: its binding stands in. Without it every standing pass that ran during a
        // renewal closed this Mac's work service (`member_agent` closes work without a binding),
        // and the owner's run kept dialling the closed address (lane FLK, rows 6-7).
        // The membership state, the live Session or the in-flight snapshot are read in ONE
        // acquisition of the authority (lane FLK patch 2, FLK-01): a renewal that starts between
        // separate reads would leave "current" without its binding and close the work service.
        // Lock order authority -> work session, as `Network::sync_work_session`. An owner host
        // has no joined Session (`session` is a target's), so it never has a binding here.
        let (root, binding) = {
            let a = lock(&self.authority);
            let current = a.target && target_membership_state(&a).as_deref() == Some("current");
            let binding = match &a.session {
                _ if !current => None,
                Some(session) => api::peer::membership_binding(Some(session)),
                // FLK-02: only a renewal flight that still stands in (not one that unwound or ran
                // past its deadline: the base closed the work service then, and so does this).
                None if a
                    .member_flight
                    .as_ref()
                    .is_some_and(api::renewal::Flight::stands_in) =>
                {
                    api::peer::membership_binding(lock(&self.network.work_session).as_ref())
                }
                None => None,
            };
            (a.root.clone(), binding)
        };
        #[cfg(test)]
        if let Some(hook) = lock(&self.inside_accept_context).take() {
            hook();
        }
        let binding = binding.map(|b| {
            json!({"swarm_id": b.swarm_id, "node_id": b.node_id,
                "seed_key_digest": b.seed_key_digest})
        });
        Some(api::member_agent::AcceptContext {
            policy: api::preferences::resource_policy::read(&root)
                .ok()
                .flatten(),
            binding,
            join_base: self.network.seams.join_base.clone(),
        })
    }
    /// `DemoApp._next_periodic_report` lives beside `last_report` in the authority's record.
    fn periodic_report_due(&self) -> bool {
        lock(&self.authority)
            .recorded
            .periodic_report_due(std::time::Instant::now())
    }
    fn reported(&self, record: Value) {
        lock(&self.authority)
            .recorded
            .reported(record, std::time::Instant::now());
    }
    fn work_enabled(&self) -> bool {
        true
    }
    fn work_announcement(
        &self,
        announcement: api::run_exchange::owner_pin::VerifiedAnnouncement,
    ) -> Result<Option<Value>, String> {
        self.announce_work(announcement)
    }
    fn close_work(&self) {
        self.network.close_work();
    }
    fn work_report(&self) -> Result<Option<Value>, String> {
        // Lane XB patch 2 (D3): without a service, a named network refusal is reported.
        lock(&self.network.work).as_ref().map_or_else(
            || Ok(self.network.reach.refusal_report()),
            crate::packaged_work::Service::report,
        )
    }
    fn work_preview(
        &self,
        offer: &Value,
        limits: &Value,
        policy: &Value,
    ) -> Result<Value, api::standing_acceptor::AcceptFailure> {
        lock(&self.network.work)
            .as_ref()
            .ok_or_else(|| api::standing_acceptor::AcceptFailure::code("member_work_unavailable"))?
            .preview(offer, limits, policy)
            .map_err(|code| api::standing_acceptor::AcceptFailure::code(&code))
    }
    fn work_accept(
        &self,
        preview: &Value,
        limits: &Value,
        policy: &Value,
    ) -> Result<(), api::standing_acceptor::AcceptFailure> {
        lock(&self.network.work)
            .as_ref()
            .ok_or_else(|| api::standing_acceptor::AcceptFailure::code("member_work_unavailable"))?
            .accept(preview, limits, policy)
            .map_err(|code| api::standing_acceptor::AcceptFailure::code(&code))
    }
    fn spawn(
        &self,
        pump: api::target_pump::TargetPump<api::member_agent::RemoteTarget<Self>>,
        fence: api::managed_carrier::TargetFence,
        scope: Value,
    ) -> Self::Worker {
        api::member_agent::spawn_pump(pump, fence, scope)
    }
}

/// One `_standing_loop` (app.py:1201-1212 @ f4e366e9): the member's agent passes and the card
/// they leave; the agent is closed when the loop ends (`demo_app.close` -> `agent.close()`).
struct Standing {
    member: Arc<PackagedMember>,
    agent: api::member_agent::MemberAgent<PackagedMember>,
}
impl Standing {
    fn pass(&mut self) {
        self.agent.tick(&self.member);
        // `_record_status`: the card shows the last completed pass.
        // Log-only, on change: a joined Mac that never reports looks identical in the owner's run
        // to a Mac that is not there at all, so record what this pass actually produced.
        if let Some(card) = self.agent.status() {
            let mut slot = lock(&self.member.network.demo_target);
            if slot.as_ref() != Some(&card) {
                eprintln!("l11_standing_card:{card}");
            }
            *slot = Some(card);
        }
    }
}
impl Drop for Standing {
    fn drop(&mut self) {
        self.agent.close(&self.member);
    }
}
/// `DemoApp._standing_loop`'s pass for a joined Mac (`_standing_tick` -> `auto_poll_step` ->
/// `demo_app.auto_step`, demo_app.py:168-173), as one closure for the host's own loop.
pub fn standing_loop(member: Arc<PackagedMember>) -> impl FnMut() + Send + 'static {
    let mut standing = Standing {
        member,
        agent: api::member_agent::MemberAgent::default(),
    };
    move || standing.pass()
}
/// `DemoApp.status()['demo_target']` (app.py:1279) = `demo_app.target_status` (demo_app.py:181-197
/// @ f4e366e9) for a joined Mac: no agent yet -> `joined` / `not_joined` by the membership; else the
/// agent's last card, `idle` shown as `joined` while the membership is current. An owner's card is
/// its run's own contributor (none without a run).
pub fn member_status(status: &mut Value, authority: Option<&Mutex<Authority>>, network: &Network) {
    let Some(authority) = authority else {
        return;
    };
    if !lock(authority).target {
        return;
    }
    let current = membership_state(authority).as_deref() == Some("current");
    let card = lock(&network.demo_target).clone();
    status["demo_target"] = match card {
        None => json!({"state": if current { "joined" } else { "not_joined" }, "detail": null,
            "progress": null, "phase": null, "run_id": null, "model_id": null, "local": false}),
        Some(mut card) => {
            if card["state"] == "idle" && current {
                card["state"] = json!("joined");
            }
            card
        }
    };
    // Lane XB patch 2 (D3): a named network refusal of this Mac's work shows on its card.
    network.reach.card(&mut status["demo_target"]);
}

#[cfg(test)]
mod serving_probe_tests {
    //! `_relay_is_serving` answered false unconditionally, so the packaged app's relay could never
    //! extend its window: the card promised a renewal every 30 minutes and the tunnel closed at its
    //! first deadline, mid-run. The probe must follow the run's own signal instead.
    #[test]
    fn the_relay_probe_reports_serving_only_while_a_run_is_active() {
        let root = std::env::temp_dir().join(format!("net-probe-{}", std::process::id()));
        std::fs::create_dir_all(&root).expect("scratch root");
        let network = super::Network::new(&root, super::NetworkConfig::default());
        let probe = network.serving_probe();
        assert!(!probe().expect("probe"), "no run has started");
        network.set_serving(true);
        assert!(
            probe().expect("probe"),
            "an active run may extend the window"
        );
        network.set_serving(false);
        assert!(!probe().expect("probe"), "the run ended");
        std::fs::remove_dir_all(&root).unwrap_or(());
    }
}
