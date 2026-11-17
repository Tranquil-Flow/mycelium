// SPDX-License-Identifier: AGPL-3.0-only
//! Headless launcher for the unchanged external L3 journeys: `headless --headless --offline`.
//!
//! This binds the real `api` desktop boundary (native session, `Desktop`, the typed action
//! `Control`, the persistent preference/consent/join/pair backend, the ordered status) to a
//! minimal desktop HOST that owns what the Rust workspace does not have yet: one local
//! identity, the owner's one-member swarm record, the pairing request signature, the
//! acquisition lifecycle marker and process lifetime. It does NOT contain a swarm authority,
//! relay, TLS seed, pairing-window store, encrypted handoff, leave workflow, planner or model
//! runtime: those host seams answer `409 action_not_completed` (see `crates/api/README.md`).
//! State lives under v0.2's own data folder, `$HOME/Library/Application
//! Support/org.mycelium.app/state` (rulings 20 and 25: never the v0.1.4 demo's folder);
//! `--state-root` overrides it, as every test does; the L3 journey harness launches without it
//! under a scratch HOME and reads this default (test-support/journeys `Target::state_folder`).
//! The owner-authority seams (create, authority start/stop, pairing request/approve/accept, the
//! owner's pairing windows) are `api::app_host`, the same code the packaged executable runs.
use api::{
    acquisition::{self, Acquisition, HttpTransport},
    actions::{Action, Backend, Control},
    app_host::{self, Authority, HostView, NETWORK_FILE, cap, now, random},
    authority_view,
    desktop::{Desktop, Session},
    http::serve_connection,
    installation_descriptor::Failure,
    jobj,
    joined_member::{self, NoSeedServer, SeedHop},
    leave::{Clock, LeaveWorker, Limits, MemberLeave, Steps, SystemClock, resume_leave},
    native_session::{MonotonicClock, NativeLauncher},
    owner_chain::{
        ChainPorts, ModelAdapter, SharedHostChain, acquisition_exception, prepared_source_from,
        unwired,
    },
    owner_driver::{DemoRuns, Timing, fresh_identity},
    owner_ports::{self, OwnerPorts, RunFields, RunHost, RunView, contributor_dir, step_exception},
    pairing::SignedBackend,
    persistent::{Host, PersistentBackend, RelayState},
    preferences::{private_state, private_write, resource_policy, run_model},
    relay::{EndpointHost, Hooks, LocalHandler, OwnerRelay, Probe, Proxy, SeedBackend},
    resources::{self, Observation, Observer, ProbeObserver},
    run_exchange::RunExchange,
    schema::{PublicError, Raised, STATUS_FIELDS, Snapshot},
    seed::SeedOptions,
    status::PublishedStatus,
    swarm::publisher::app::{AppPublisher, OwnerFacts},
    windows::PendingWindows,
    wire::Json,
};
#[path = "headless/compiler.rs"]
mod compiler;
#[path = "headless/listener_wait.rs"]
mod listener_wait;
#[path = "headless/member_agent.rs"]
mod member_agent;
#[path = "headless/observation.rs"]
mod observation;
#[path = "headless/product.rs"]
mod product;
#[cfg(test)]
use api::app_host::start_seed;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use budgets::{BudgetId, Entries};
use owner_run::{chain::StepError, steps::model::Reverify};
#[cfg(test)]
use protocol::identity::Identity;
use serde_json::{Map, Value, json};
#[cfg(test)]
use std::collections::BTreeMap;
use std::{
    fs,
    io::{Read, Write},
    net::{Ipv4Addr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::{Arc, Mutex, mpsc},
    thread,
    time::{Duration, Instant},
};

const LABEL: &str = "macOS A13 hackathon demo";
const LAUNCH_FILE: &str = "open-demo.html";
const CONTROL_FILE: &str = "native-control.json";

fn span(id: BudgetId) -> Duration {
    id.span(Entries::new(0))
        .map_or(Duration::ZERO, |v| Duration::from_millis(v.millis()))
}
fn failure() -> PublicError {
    PublicError::new(409, "action_not_completed")
}

/// What the process knows; shared by the host, the status builder and the lifetime loop.
/// The owner authority (root, role, swarm, seed, memberships, windows) is the library's
/// [`Authority`]; the core reads its fields as its own (`Deref`).
struct Core {
    authority: Authority<observation::Managed>,
    closed: bool,
    /// `DemoApp._leave_fence` / `_left` (member leave).
    fenced: bool,
    left: bool,
    /// `DemoApp.acquisition`: the bounded model snapshot download (`api::acquisition`).
    acquisition: Option<Acquisition>,
    /// `DemoApp._acquisition_active`.
    acquisition_active: bool,
    /// `DemoApp.acquisition_policy_error`.
    acquisition_policy_error: Option<&'static str>,
    /// `DemoApp.resource_observation`: the last observation `_resource_check` made.
    resource_observation: Value,
    observer: Arc<dyn Observer>,
    /// The verdict a managed-control exchange last probed, and when (see [`exchange_resource_check`]).
    exchange_resource: Option<(std::time::Instant, Option<&'static str>)>,
    /// `--offline` / `MYCELIUM_OFFLINE`: Hugging Face is never contacted.
    offline: bool,
    revision: u64,
    /// `DemoApp._pending_windows`, shared with the persistent backend.
    pending: PendingWindows,
    /// `DemoApp.public_relay`.
    relay: OwnerRelay,
    /// `DemoApp.swarm_publisher` (`swarm_publish.py`): explicit opt-in listing custody. Its own
    /// locks; never held together with Core's (see `swarm_publish_tick`).
    swarm_publisher: Arc<AppPublisher>,
    /// `DemoApp.owner_run` as the status and the fast-mode gate read it (never blocks on the run),
    /// `DemoApp.local_contributor` (the owner's own member, renewed by the loop) and
    /// `DemoApp.run_exchange` (`api::owner_ports`).
    run: RunFields,
    // Serializes bootstrap side effects and cleanup; never held with Core on entry.
    bootstrap_lifecycle: Arc<Mutex<()>>,
    bootstrap_incarnation: Arc<()>,
    bootstrap: Option<observation::target::Worker>,
    bootstrap_failure: Option<api::bootstrap_process::CleanupFailure>,
    compiler: Option<std::sync::Weak<Mutex<compiler::Compiler>>>,
    /// Actual prepared runtime and its sole retained-input custody owner.
    compiled_workflow: Option<api::compiled_handoff::CompiledHandoff>,
    /// Compilation diagnostics survive Stop, without retaining source handles.
    compiled_receipt: Option<Value>,
}
impl std::ops::Deref for Core {
    type Target = Authority<observation::Managed>;
    fn deref(&self) -> &Self::Target {
        &self.authority
    }
}
impl std::ops::DerefMut for Core {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.authority
    }
}
/// The owner's seed runtime, with the observation mount's managed owner.
#[cfg(test)]
type SeedRuntime = app_host::SeedRuntime<observation::Managed>;
/// The core as the library authority's cell: the authority is the core's own state; the relay,
/// `closed`, `left` and the loopback test seed path of a joining member are this launcher's.
struct CoreCell(Shared);
/// One snapshot of the core for the library seams: its authority with the relay, `closed` and
/// `left` beside it, as the core's own lock holds them.
fn view(core: &mut Core) -> HostView<'_, observation::Managed> {
    let Core {
        authority,
        relay,
        closed,
        left,
        ..
    } = core;
    HostView::new(authority, relay, *closed, *left)
}
impl app_host::Cell for CoreCell {
    type Managed = observation::Managed;
    fn with<R>(&self, f: impl FnOnce(&mut HostView<'_, observation::Managed>) -> R) -> R {
        match self.0.lock() {
            Ok(mut core) => f(&mut view(&mut core)),
            Err(poisoned) => f(&mut view(&mut poisoned.into_inner())),
        }
    }
    fn try_with<R>(
        &self,
        f: impl FnOnce(&mut HostView<'_, observation::Managed>) -> R,
    ) -> Option<R> {
        self.0.lock().ok().map(|mut core| f(&mut view(&mut core)))
    }
    fn member_seed_options(&self) -> SeedOptions {
        member_seed_options()
    }
}
type Shared = Arc<Mutex<Core>>;

/// The same pass for a joined TARGET (`api::app_host::renew_member`, shared with the packaged host).
fn renew_target(core: &Shared) {
    app_host::renew_member(&CoreCell(core.clone()));
}
/// The owner-run composition over this core (`api::owner_ports`, shared with the packaged host).
#[derive(Clone)]
struct HeadlessRun(Shared);
impl RunHost for HeadlessRun {
    type Managed = observation::Managed;
    fn with<R>(&self, f: impl FnOnce(&mut RunView<'_, observation::Managed>) -> R) -> R {
        let mut guard = match self.0.lock() {
            Ok(core) => core,
            Err(poisoned) => poisoned.into_inner(),
        };
        let Core {
            authority,
            run,
            closed,
            left,
            relay,
            ..
        } = &mut *guard;
        f(&mut RunView {
            relay_absent: matches!(relay.state(), RelayState::Absent),
            authority,
            run,
            closed: *closed,
            left: *left,
        })
    }
    fn bootstrap_lifecycle(&self) -> Arc<Mutex<()>> {
        match self.0.lock() {
            Ok(core) => core.bootstrap_lifecycle.clone(),
            Err(poisoned) => poisoned.into_inner().bootstrap_lifecycle.clone(),
        }
    }
    fn membership_current(&self) -> bool {
        app_host::membership_state(&CoreCell(self.0.clone())).as_deref() == Some("current")
    }
    fn admit_contributor(&self, root: &Path) -> Result<(), PublicError> {
        observation::target::admit_cleanup_free(root).map_err(|e| PublicError::new(409, e.0))
    }
    fn close_contributor(&self) -> Result<(), PublicError> {
        observation::target::close_locked(self).map_err(|e| PublicError::new(409, e.0))
    }
    fn resource_check(&self) -> Option<&'static str> {
        resource_check(&self.0)
    }
    fn acquisition(&self) -> Option<Acquisition> {
        self.0.lock().ok().and_then(|c| c.acquisition.clone())
    }
    fn bootstrap(
        &self,
        node: &str,
        run: &str,
        deployment: &str,
        offer: &str,
    ) -> Result<(), Failure> {
        observation::bootstrap(self, node, run, deployment, offer)
            .map_err(|error| Failure::new("RuntimeError", error.0))
    }
    fn sidecar(&self) -> Result<PathBuf, Failure> {
        installed_sidecar()
    }
    fn pending_cleanup(&self, node: &str) -> Result<Option<Value>, Failure> {
        api::owner_observation::pending_cleanup(self, node)
            .map_err(|error| Failure::new("RuntimeError", error.0))
    }
    fn cleanup(&self, node: &str, request: &Value) -> Result<(), Failure> {
        api::owner_observation::cleanup(self, node, request)
            .map_err(|error| Failure::new("RuntimeError", error.0))
    }
}
/// `installed_sidecar()` (`api::owner_ports`); `MYCELIUM_L10_TEST_SIDECAR` is the test launcher's seam.
fn installed_sidecar() -> Result<PathBuf, Failure> {
    owner_ports::installed_sidecar(std::env::var_os("MYCELIUM_L10_TEST_SIDECAR").map(PathBuf::from))
}
fn start_local_contributor(core: &Shared) -> Result<Json, PublicError> {
    owner_ports::start_local_contributor(&HeadlessRun(core.clone()))
}
fn renew_contributor(core: &Shared) {
    owner_ports::renew_contributor(&HeadlessRun(core.clone()));
}
fn stop_local_contributor(core: &Shared) -> Result<(), PublicError> {
    owner_ports::stop_local_contributor(&HeadlessRun(core.clone()))
}
fn ensure_exchange(core: &Shared) -> Option<Arc<RunExchange>> {
    owner_ports::ensure_exchange(&HeadlessRun(core.clone()))
}

/// `DemoApp._public_route` for the pairing window, the link probe and the member-report inbox
/// (`api::joined_member`, shared with the packaged host) over this core's window store and seed.
fn public_route(core: Shared) -> LocalHandler {
    joined_member::public_route(Arc::new(move || match core.lock() {
        Ok(c) => (c.windows.clone(), c.seed.clone()),
        Err(poisoned) => {
            let c = poisoned.into_inner();
            (c.windows.clone(), c.seed.clone())
        }
    }))
}
/// `MYCELIUM_L10_TEST_JOIN_BASE`: the oracle's `_test_join_base_url` seam (this example is the test
/// launcher, not the product).
fn test_join_base() -> Option<String> {
    std::env::var("MYCELIUM_L10_TEST_JOIN_BASE").ok()
}
/// The seed options of a joining member: the public Web-PKI path, or - for the loopback test launcher -
/// the owner's local seed directly (`MYCELIUM_L10_TEST_SEED_DIRECT=<host:port>|<ca.pem path>`).
fn member_seed_options() -> SeedOptions {
    let Ok(spec) = std::env::var("MYCELIUM_L10_TEST_SEED_DIRECT") else {
        return SeedOptions::default();
    };
    let Some((address, ca)) = spec.split_once('|') else {
        return SeedOptions::default();
    };
    SeedOptions {
        ca_pem: fs::read(ca).ok(),
        connect_to: address.parse().ok(),
        server_name: Some("127.0.0.1".into()),
    }
}

struct HeadlessHost {
    core: Shared,
    /// `DemoApp.owner_run` and its driver; absent in the throwaway hosts that only read the core.
    runs: Option<Arc<Mutex<DemoRuns>>>,
}
/// Membership departure is not owned-worker cleanup. Only a durably empty
/// owned set can succeed without a joined-worker cleanup port.
struct HeadlessLeave {
    core: Shared,
}
impl HeadlessLeave {
    fn none_owned(&self) -> Result<bool, String> {
        let core = self
            .core
            .lock()
            .map_err(|_| "member_leave_state_unavailable".to_owned())?;
        if core.bootstrap.is_some()
            || core.bootstrap_failure.is_some()
            || core.run.contributor.is_some()
            || core.compiled_workflow.is_some()
        {
            return Ok(false);
        }
        // Missing memory after restart is insufficient: the durable half
        // (`api::joined_member`, shared with the packaged host).
        let root = core.root.clone();
        drop(core);
        joined_member::none_owned_on_disk(&root)
    }
}
impl LeaveWorker for HeadlessLeave {
    fn step(&mut self, name: &str) -> Result<(), Option<String>> {
        if matches!(name, "unload" | "cancel_work" | "publish_withdrawal")
            && !self.none_owned().map_err(Some)?
        {
            return Err(Some(if name == "publish_withdrawal" {
                "member_withdrawal_publication_unavailable".into()
            } else {
                "member_compute_cleanup_unavailable".into()
            }));
        }
        let mut core = self
            .core
            .lock()
            .map_err(|_| Some("member_leave_state_unavailable".into()))?;
        match name {
            "fence" => core.fenced = true,
            "disconnect" => core.left = true,
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
        SystemClock::default().wall()
    }
}
impl HeadlessHost {
    fn with<R>(&self, f: impl FnOnce(&mut Core) -> R) -> R {
        match self.core.lock() {
            Ok(mut core) => f(&mut core),
            Err(poisoned) => f(&mut poisoned.into_inner()),
        }
    }
    /// The owner authority seams run in `api::app_host` over this core.
    fn cell(&self) -> CoreCell {
        CoreCell(self.core.clone())
    }
}
impl Host for HeadlessHost {
    fn install_compiled_handoff(
        &mut self,
        handoff: &mut Option<api::compiled_handoff::CompiledHandoff>,
    ) -> Result<(), PublicError> {
        // Hashing retained payloads never holds the public status/state lock.
        // It looks at that state once per chunk: a quit, leave or role change
        // that lands meanwhile refuses within one chunk, not after every pin.
        let input = handoff
            .as_ref()
            .ok_or_else(|| PublicError::new(409, "compiled_handoff_absent"))?;
        let stopped = || {
            self.core
                .lock()
                .map_or(true, |c| c.closed || c.target || c.fenced)
        };
        input.revalidate_until(&stopped).map_err(|e| {
            if e.code == "compiled_handoff_stopped" {
                PublicError::new(409, "compiled_handoff_not_installable")
            } else {
                PublicError::new(409, "compiled_handoff_invalid")
            }
        })?;
        let mut core = self.core.lock().map_err(|_| failure())?;
        if core.closed
            || core.target
            || core.fenced
            || core.compiled_workflow.is_some()
            || input.view().is_stopped()
        {
            return Err(PublicError::new(409, "compiled_handoff_not_installable"));
        }
        // No fallible work after taking custody. This mount does not invoke
        // publication, prepare, run or activation against historical members.
        core.compiled_receipt = Some(input.progress_identity().clone());
        core.compiled_workflow = handoff.take();
        Ok(())
    }
    fn serving_status(&self) -> Json {
        let retained = self.with(|core| core.compiled_receipt.as_ref().map(|receipt| {
            jobj! {
                "compilation" => Json::from(receipt.clone()),
                "serving" => core.compiled_workflow.as_ref().map_or(Json::Null, |w| Json::from(w.status())),
                "route_ready" => false,
            }
        }));
        retained.unwrap_or_else(|| compiler::status(&self.core))
    }
    fn serving_stop(&mut self) -> Option<Result<Json, PublicError>> {
        // Dropping the actual owner fences its runtime view and releases both
        // Controller/Launch source handles. Observation/bootstrap is distinct.
        let workflow = self.with(|core| core.compiled_workflow.take());
        drop(workflow);
        Some(Ok(self.serving_status()))
    }
    fn leave_worker(&mut self) -> Box<dyn LeaveWorker> {
        Box::new(HeadlessLeave {
            core: self.core.clone(),
        })
    }
    fn leave_mark(&mut self, fence: bool, left: bool) {
        self.with(|c| {
            c.fenced |= fence;
            c.left |= left;
        });
    }
    fn closed(&self) -> bool {
        self.with(|c| c.closed)
    }
    fn owner(&self) -> bool {
        self.with(|c| !c.target)
    }
    fn environment(&self, name: &str) -> Option<String> {
        std::env::var(name).ok()
    }
    fn device_memory_bytes(&self) -> Option<u64> {
        let out = std::process::Command::new("/usr/sbin/sysctl")
            .args(["-n", "hw.memsize"])
            .output()
            .ok()?;
        String::from_utf8(out.stdout).ok()?.trim().parse().ok()
    }
    fn run_active(&self) -> bool {
        self.with(|c| c.run.demo.clone()).active()
    }
    fn acquisition_active(&self) -> bool {
        self.with(|c| c.acquisition_active)
    }
    fn owner_run_active(&self) -> bool {
        self.with(|c| c.run.demo.clone()).active()
    }
    fn demo_action(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        let runs = self.runs.clone().ok_or_else(failure)?;
        let mut facts = demo_facts(&self.core);
        let mut runs = runs.lock().map_err(|_| failure())?;
        runs.demo_action(action, body, &mut facts)
    }
    fn run_model_saved(&mut self) -> Result<(), PublicError> {
        // acquisition_policy_error = None; _open_acquisition(): selection never starts a download;
        // a failed open is the choice's answer (app.py:866-867).
        self.with(|c| with_acquisition(c, app_host::run_model_saved))
    }
    fn resource_check(&mut self) -> Option<&'static str> {
        resource_check(&self.core)
    }
    fn set_acquisition_policy_error(&mut self, reason: Option<&'static str>) {
        self.with(|c| c.acquisition_policy_error = reason);
    }
    fn acquisition_start(&mut self, grant: &Value) -> Result<Json, PublicError> {
        self.with(|c| with_acquisition(c, |view, _| app_host::acquisition_start(view, grant)))
    }
    fn acquisition_cancel(&mut self) -> Result<Json, PublicError> {
        self.with(|c| with_acquisition(c, |view, _| app_host::acquisition_cancel(view)))
    }
    fn cancel_acquisition(&mut self) {
        self.with(|c| with_acquisition(c, |view, _| app_host::cancel_acquisition(view)));
    }
    fn swarm_created(&self) -> bool {
        app_host::swarm_created(&self.cell())
    }
    fn membership_state(&self) -> Option<String> {
        app_host::membership_state(&self.cell())
    }
    fn running(&self) -> bool {
        app_host::running(&self.cell())
    }
    fn stop_authority(&mut self) {
        app_host::stop_authority(&self.cell());
    }
    fn status_document(&self) -> Json {
        let demo = self.with(|c| c.run.demo.clone()).status();
        self.with(|c| status_json(c, demo))
    }
    fn adopt_target_role(&mut self) -> Result<(), PublicError> {
        self.with(|c| c.target = true);
        Ok(())
    }
    fn start_authority(&mut self) -> Result<(), PublicError> {
        app_host::start_authority(&self.cell())
    }
    fn reopen_acquisition_if_needed(&mut self) {
        // `if acquisition is None or its state_root differs: self._open_acquisition()`.
        self.with(|c| with_acquisition(c, app_host::reopen_acquisition_if_needed));
    }
    /// Target side of `authority.pair_request()` (authority.py:841-871).
    fn pair_request(&mut self) -> Result<Json, PublicError> {
        app_host::pair_request(&self.cell())
    }
    /// Owner side of `/api/pair/approve`: the oracle's approvals store and encrypted handoff.
    fn pair_approve(
        &mut self,
        request: &Value,
        ttl_seconds: &Value,
        quota: &Value,
    ) -> Result<Json, PublicError> {
        app_host::pair_approve(&self.cell(), request, ttl_seconds, quota)
    }
    /// Target side of `/api/pair/accept`: `RemoteMacMember.accept_handoff` (`api::peer`).
    fn pair_accept(&mut self, handoff_value: &Value) -> Result<Json, PublicError> {
        app_host::pair_accept(&self.cell(), handoff_value)
    }
    fn daemon_log(&mut self, line: &str) {
        // `print(json.dumps(event, sort_keys=True), flush=True)`: the app's stdout is the daemon log.
        let mut out = std::io::stdout();
        writeln!(out, "{line}")
            .and_then(|()| out.flush())
            .unwrap_or(());
    }
    fn membership_binding(&self) -> Option<api::member::Binding> {
        app_host::membership_binding(&self.cell())
    }
    /// The owner side of `/api/create` (authority.create_swarm → pair_request → approve_pairing →
    /// accept_pairing collapse to "this Mac is member 1").
    fn create_owner_json(&mut self, consent: &Map<String, Value>) -> Result<Json, PublicError> {
        app_host::create_owner_json(&self.cell(), consent)
    }
    fn relay_state(&self) -> RelayState {
        self.with(|c| c.relay.state())
    }
    fn close_stopped_relay(&mut self) {
        self.with(|c| c.relay.close());
    }
    fn relay_origin(&self) -> Option<String> {
        self.with(|c| c.relay.origin())
    }
    fn configured_public_origin(&self) -> Option<String> {
        app_host::configured_public_origin(&self.cell())
    }
    /// `POST /api/create` with `{relay_acknowledged, max_seconds}`: the quick-tunnel relay precedes the swarm.
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
        start_relay(&self.core, &body).map(drop)
    }
    fn abort_public_relay(&mut self) {
        self.with(|c| c.relay.close());
    }
    fn local_contributor_start(&mut self) -> Result<Json, PublicError> {
        start_local_contributor(&self.core)
    }
    fn local_contributor_stop(&mut self) -> Result<(), PublicError> {
        stop_local_contributor(&self.core)
    }
    fn local_contributor_retained(&self) -> bool {
        self.with(|c| contributor_dir(&c.root).exists())
    }
    fn bind_relay_seed(&mut self) {
        self.with(|c| {
            let backend = c
                .seed
                .clone()
                .map(|s| Arc::new(SeedHop(s)) as Arc<dyn SeedBackend>);
            c.relay
                .bind_seed(backend.or_else(|| Some(Arc::new(NoSeedServer))));
        });
    }
    fn live_pairing_windows(&self) -> usize {
        app_host::live_pairing_windows(&self.cell())
    }
    fn coordinator_ready(&self) -> bool {
        app_host::coordinator_ready(&self.cell())
    }
    fn owner_window(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        app_host::owner_window(&self.cell(), action, body)
    }
    /// The member's join: measure the relay link, sign the report, post the request to the owner's window.
    fn join_submit(
        &mut self,
        link: &api::persistent::JoinLink,
        request: &Value,
        quota: &Value,
    ) -> Result<(Value, Vec<Value>), PublicError> {
        let root = self.with(|c| c.root.clone());
        joined_member::join_submit(&root, test_join_base().as_deref(), link, request, quota)
    }
    fn join_poll(&mut self, state: &Map<String, Value>) -> Result<Value, PublicError> {
        joined_member::join_poll(state, test_join_base().as_deref())
    }
    /// The approved branch: seed pin check, `accept_pairing`, membership binding, `running = True`, the join report.
    fn join_accept(
        &mut self,
        handoff_value: &Value,
        pin: Option<&str>,
    ) -> Result<api::persistent::JoinAccepted, PublicError> {
        joined_member::join_accept(
            &self.cell(),
            handoff_value,
            pin,
            test_join_base().as_deref(),
        )
    }
    fn swarm_publish(&mut self, body: &Map<String, Value>) -> Result<Json, PublicError> {
        let (closed, owner, publisher) =
            self.with(|c| (c.closed, !c.target, c.swarm_publisher.clone()));
        if closed {
            return Err(PublicError::new(400, "application_closed"));
        }
        publisher.publish(owner, body).map(Json::from)
    }
    fn swarm_unpublish(&mut self, body: &Map<String, Value>) -> Result<Json, PublicError> {
        let (closed, owner, publisher) =
            self.with(|c| (c.closed, !c.target, c.swarm_publisher.clone()));
        if closed {
            return Err(PublicError::new(400, "application_closed"));
        }
        publisher.unpublish(owner, body).map(Json::from)
    }
    fn quit(&mut self) -> Result<Json, PublicError> {
        let workflow = self.with(|c| {
            c.closed = true;
            c.compiled_workflow.take()
        });
        drop(workflow);
        // `DemoApp.close`: the run is stopped and its thread ends before the process does.
        if let Some(runs) = &self.runs {
            runs.lock().map(|mut r| r.close()).unwrap_or(());
        }
        Ok(jobj! {"stopping" => true, "identity_retained" => true, "evidence_retained" => true})
    }
}

/// The authority seams of `_start_public_relay`: the owner's swarm record and the (absent) seed server.
struct Endpoint {
    core: Shared,
}
impl Endpoint {
    fn with<R>(&self, f: impl FnOnce(&mut Core) -> R) -> R {
        match self.core.lock() {
            Ok(mut core) => f(&mut core),
            Err(poisoned) => f(&mut poisoned.into_inner()),
        }
    }
}
impl EndpointHost for Endpoint {
    fn swarm_created(&self) -> bool {
        app_host::swarm_created(&CoreCell(self.core.clone()))
    }
    fn configured_public_origin(&self) -> Option<String> {
        app_host::configured_public_origin(&CoreCell(self.core.clone()))
    }
    fn configure_public_endpoint(&mut self, origin: &str, _port: u16) -> Result<(), PublicError> {
        let root = self.with(|c| {
            c.configured_origin = Some(origin.to_owned());
            c.root.clone()
        });
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
        let old = self.with(|c| c.seed.take());
        if let Some(old) = old {
            old.close();
            app_host::open_owner_seed(&CoreCell(self.core.clone()))?;
        }
        Ok(())
    }
    fn relocate_authority(&mut self, origin: &str, port: u16) -> Result<(), PublicError> {
        self.attach_public_endpoint(origin, port)
    }
    fn seed_backend(&self) -> Option<Arc<dyn SeedBackend>> {
        self.with(|c| {
            c.seed
                .clone()
                .map(|s| Arc::new(SeedHop(s)) as Arc<dyn SeedBackend>)
                .or_else(|| {
                    c.owner_authority()
                        .map(|_| Arc::new(NoSeedServer) as Arc<dyn SeedBackend>)
                })
        })
    }
    fn set_running(&mut self) {
        self.with(|c| c.running = true);
    }
    fn bind_public_routes(&mut self, proxy: &Arc<Proxy>) {
        proxy.bind_local(Some(public_route(self.core.clone())));
    }
    /// `_materialize_pending_windows`: codes asked for before the relay exist from now on.
    fn materialize_pending_windows(&mut self) {
        let (pending, windows, origin, seed) = self.with(|c| {
            (
                c.pending.clone(),
                c.windows.clone(),
                c.configured_origin.clone(),
                c.seed.clone(),
            )
        });
        let (Some(origin), Some(seed)) = (origin, seed) else {
            return;
        };
        let digest = seed.coordinator.signer().verification_key_digest();
        for item in pending.drain() {
            // capacity or an invalid TTL: the owner can open a fresh code
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
/// `DemoApp._start_public_relay` on this host. The relay is taken out of the core while it starts, so the
/// endpoint seams (which lock the core) never meet the relay's own locks.
fn start_relay(core: &Shared, body: &Map<String, Value>) -> Result<Json, PublicError> {
    let mut endpoint = Endpoint { core: core.clone() };
    // `_relay_is_serving` (app.py:769-776): the window renews while an owner run is active or a
    // swarm listing is enabled (its listing points at this relay). Both are read from their own
    // short-held cells (the run view installed at launch, the publisher's status snapshot), never
    // the core lock, so a long action holding the core cannot starve the relay's renewal.
    let (demo, publisher) = endpoint.with(|c| (c.run.demo.clone(), c.swarm_publisher.clone()));
    let probe: Probe = Arc::new(move || Ok(demo.active() || publisher.listing_enabled()));
    let root = endpoint.with(|c| c.root.clone());
    let mut relay =
        endpoint.with(|c| std::mem::replace(&mut c.relay, OwnerRelay::new(&root, relay_hooks)));
    let outcome = relay.start_public_relay(body, &mut endpoint, probe);
    endpoint.with(|c| c.relay = relay);
    outcome
}
fn relay_hooks() -> Hooks {
    Hooks::production(std::env::current_exe().ok().and_then(|exe| {
        // the packaged app's bundled `native/cloudflared` sits next to the executable's resources
        exe.parent().map(Path::to_path_buf)
    }))
}

fn demo_facts(core: &Shared) -> owner_ports::HostFacts {
    owner_ports::demo_facts(&HeadlessRun(core.clone()))
}

/// The owner run's view of this process (`AppDemoChain` reads these from `app`): see `api::owner_chain`.
#[derive(Clone)]
struct HeadlessPorts {
    core: Shared,
}
impl HeadlessPorts {
    fn new(core: Shared) -> Self {
        Self { core }
    }
    fn with<R>(&self, f: impl FnOnce(&mut Core) -> R) -> R {
        match self.core.lock() {
            Ok(mut core) => f(&mut core),
            Err(poisoned) => f(&mut poisoned.into_inner()),
        }
    }
}
/// The actual owning Runner receives the shared OwnerChain. Each temporary
/// Ports borrow uses independent adapters over the existing host-owned core and
/// local agent. No callback captures a Host, Runner, or these temporary borrows.
struct HeadlessChainPorts {
    model: ModelAdapter<HeadlessPorts>,
    contributor: OwnerPorts<HeadlessRun>,
    reports: OwnerPorts<HeadlessRun>,
    observation: compiler::Observation<HeadlessRun>,
    compiler: compiler::SharedCompiler<HeadlessRun>,
}
impl HeadlessChainPorts {
    fn new(core: Shared) -> Self {
        let observation = compiler::Observation(Arc::new(Mutex::new(observation::adapter(
            Arc::downgrade(&core),
        ))));
        let compiler = Arc::new(Mutex::new(compiler::Compiler::new(
            Arc::downgrade(&core),
            observation.clone(),
        )));
        core.lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .compiler = Some(Arc::downgrade(&compiler));
        let ports = OwnerPorts::new(HeadlessRun(core.clone()));
        Self {
            model: ModelAdapter::new(HeadlessPorts::new(core)),
            contributor: ports.clone(),
            reports: ports,
            observation,
            compiler: compiler::SharedCompiler(compiler),
        }
    }
}
impl owner_run::owner_chain::ChainPorts for HeadlessChainPorts {
    fn teardown(&mut self, _: bool, _: &mut dyn owner_run::chain::StepEnv) -> Result<(), String> {
        let compilation = self
            .compiler
            .0
            .lock()
            .map_err(|_| "compiler_state_unavailable".to_owned())?
            .teardown();
        let observation = self
            .observation
            .0
            .lock()
            .map_err(|_| "managed_owner_lock_unavailable".to_owned())?
            .host_mut()
            .backend
            .teardown();
        compilation.and(observation)
    }
    fn ports(&mut self) -> owner_run::owner_chain::Ports<'_> {
        owner_run::owner_chain::Ports {
            model: &mut self.model,
            contributor: Some(&mut self.contributor),
            reports: Some(&mut self.reports),
            observe: Some(&mut self.observation),
            compile: Some(&mut self.compiler),
        }
    }
    // slices_ports/work_ports intentionally retain default None: Core owns a
    // local source manifest compiler, NOT a compiled ManagedServingWorkflow
    // or its complete source-bound node_slice_plans producer. Installation
    // Source enums, receipt records and manifest digests cannot substitute.
}

impl ChainPorts for HeadlessPorts {
    fn owner_and_running(&self) -> bool {
        self.with(|c| !c.target && c.running)
    }
    fn swarm_created(&self) -> bool {
        self.with(|c| c.owner_authority().is_some())
    }
    fn acquisition_state(&self) -> String {
        self.with(|c| {
            c.acquisition
                .as_ref()
                .map_or_else(String::new, |a| a.status().state)
        })
    }
    /// `DemoApp.reverify_model_snapshot` (`app.py:976-999`): re-hash a snapshot left on disk by an earlier
    /// run under the standing storage consent; every power / thermal / network gate stays.
    fn reverify(&self) -> Reverify {
        let (policy, acquisition) = {
            let host = HeadlessHost {
                core: self.core.clone(),
                runs: None,
            };
            let current = host.membership_state().as_deref() == Some("current");
            self.with(|c| {
                let policy = resource_policy::read(&c.root).ok().flatten();
                let usable = !c.closed
                    && policy.is_some()
                    && c.running
                    && c.acquisition.is_some()
                    && !c.acquisition_active
                    && current;
                (
                    policy.filter(|_| usable),
                    c.acquisition.clone().filter(|_| usable),
                )
            })
        };
        let (Some(policy), Some(acquisition)) = (policy, acquisition) else {
            return Reverify::Failed;
        };
        if !matches!(
            acquisition.status().state.as_str(),
            "closed" | "idle" | "failed" | "cancelled"
        ) {
            return Reverify::Failed;
        }
        if let Some(reason) = resource_check(&self.core) {
            self.with(|c| c.acquisition_policy_error = Some(reason));
            return Reverify::Failed;
        }
        let storage = policy
            .get("storage_bytes")
            .and_then(Value::as_u64)
            .unwrap_or(0);
        match acquisition.reverify_closed_snapshot(storage) {
            Ok(_) => {
                self.with(|c| {
                    c.acquisition_policy_error = None;
                    c.acquisition_active = true;
                });
                Reverify::Done
            }
            Err(_) => Reverify::Failed,
        }
    }
    fn prepared_source(&self) -> Result<owner_run::steps::model::PreparedSource, StepError> {
        let acquisition =
            self.with(|c| c.acquisition.clone())
                .ok_or_else(|| StepError::Exception {
                    class: "AttributeError".to_owned(),
                })?;
        let (_, facts) = acquisition
            .prepared_source()
            .map_err(|e| acquisition_exception(&e))?;
        Ok(prepared_source_from(
            acquisition.model(),
            facts.manifest_digest,
            json!(facts.source_bytes),
        ))
    }
    fn storage_consent_cap(&self) -> Result<Option<Value>, StepError> {
        let root = self.with(|c| c.root.clone());
        match resource_policy::read(&root).map_err(step_exception)? {
            Some(Value::Object(policy)) => Ok(policy.get("storage_bytes").cloned()),
            _ => Ok(None),
        }
    }
    fn check_rollover_members(&self, _: &Value) -> Result<(), StepError> {
        Err(unwired("model", "NP-L10-rollover", "SE-L10-ROLLOVER"))
    }
}
/// Everything the host above does not serve. It publishes the status after each action.
struct Rest {
    core: Shared,
    published: Arc<PublishedStatus>,
}
impl Backend for Rest {
    fn prepare(&mut self, _: Action, _: &Map<String, Value>) -> Result<(), PublicError> {
        Ok(())
    }
    fn execute(&mut self, action: Action, body: &Map<String, Value>) -> Result<Value, PublicError> {
        self.execute_json(action, body).map(|json| json.to_value())
    }
    fn execute_json(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        match action {
            Action::NetworkStart => start_relay(&self.core, body),
            Action::ServingCompile => compiler::public_compile(&self.core, body),
            _ => Err(failure()),
        }
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        publish(&self.core, &self.published)
    }
}

/// `DemoApp.status()` (app.py:1244-1283 @ f4e366e9): the fields both hosts build from the
/// authority come from the shared builder (`api::app_status::shared`, also the packaged host's
/// status overlay); the rest are this launcher's own.
fn status_json(core: &Core, demo: Option<Json>) -> Json {
    let env = |name: &str| std::env::var(name).ok();
    let (run_model_env, fast_mode_env) = (env(run_model::ENV), env(fast_mode_env()));
    let facts = api::app_status::Facts {
        run_model_env: run_model_env.as_deref(),
        fast_mode_env: fast_mode_env.as_deref(),
        device_memory_bytes: host_memory_bytes(),
    };
    let mut shared: std::collections::BTreeMap<&str, Json> =
        api::app_status::shared(&core.authority, &core.pending, &facts)
            .into_iter()
            .collect();
    let mut take = |key: &str| shared.remove(key).unwrap_or(Json::Null);
    let values: Vec<(&str, Json)> = vec![
        ("label", LABEL.into()),
        ("role", take("role")),
        ("model_contract", take("model_contract")),
        ("run_model", take("run_model")),
        ("fast_mode", take("fast_mode")),
        ("network", core.relay.network_status()),
        (
            "acquisition",
            core.acquisition
                .as_ref()
                .map_or(Json::Null, |a| Json::from(&a.status_value())),
        ),
        (
            "resource_observation",
            Json::from(&core.resource_observation),
        ),
        (
            "acquisition_policy_error",
            core.acquisition_policy_error.map_or(Json::Null, Json::from),
        ),
        ("installation_role", take("installation_role")),
        ("resource_policy", take("resource_policy")),
        ("running", take("running")),
        ("authority", take("authority")),
        ("renewal_error", take("renewal_error")),
        ("recovery_error", take("recovery_error")),
        (
            "distribution",
            "unsigned development software; not notarized".into(),
        ),
        ("leave", take("leave")),
        ("join_flow", take("join_flow")),
        ("adaptive_host", Json::Null),
        ("adaptive_host_publication", Json::Null),
        (
            "sharing",
            jobj! {"state" => "idle", "line" => "Not sharing yet"},
        ),
        ("relay_wait", Json::Null),
        (
            "owner_run",
            if core.target {
                Json::Null
            } else {
                demo.unwrap_or_else(authority_view::idle_owner_run)
            },
        ),
        (
            "demo_target",
            if core.target {
                jobj! {"state" => "not_joined"}
            } else {
                Json::Null
            },
        ),
        ("chat", Json::Null),
        (
            "swarm",
            // A member has no listing of its own (`swarm_publish.status` is None), but a
            // withdrawal it still owes from its time as an owner stays visible until done.
            if core.target && !core.swarm_publisher.withdrawal_pending() {
                Json::Null
            } else {
                Json::from(core.swarm_publisher.status_json())
            },
        ),
        (
            "serving",
            jobj! {
                "state" => "not_yet_serving", "route_ready" => false,
                "next_action" => "Native route preparation and exact model execution approval required.",
            },
        ),
        ("peer_liveness", Json::Null),
        ("test_seams_active", false.into()),
        ("test_seams", Json::Null),
    ];
    debug_assert_eq!(values.len(), STATUS_FIELDS.len());
    Json::Obj(values.into_iter().map(|(k, v)| (k.to_owned(), v)).collect())
}
/// `host_memory_bytes()` (app.py:267-273): this Mac's RAM for the status's memory choices, read
/// once with the `hw.memsize` probe the join's caps check uses (`Host::device_memory_bytes`).
fn host_memory_bytes() -> Option<u64> {
    static MEMORY: std::sync::OnceLock<Option<u64>> = std::sync::OnceLock::new();
    *MEMORY.get_or_init(|| {
        let out = std::process::Command::new("/usr/sbin/sysctl")
            .args(["-n", "hw.memsize"])
            .output()
            .ok()?;
        String::from_utf8(out.stdout).ok()?.trim().parse().ok()
    })
}
/// Whole seconds of wall time for signed directory documents.
fn unix_seconds() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| i64::try_from(d.as_secs()).unwrap_or(i64::MAX))
}
/// `SwarmPublisher.tick` (swarm_publish.py:344-357): the owner facts are read under Core's lock,
/// the run through its own view (run -> host order, as `publish`), and the bounded directory
/// pass runs with no Core lock held. Nothing happens while the owner never opted in.
fn swarm_publish_tick(core: &Shared) {
    let Ok((publisher, owner, demo, model, relay)) = core.lock().map(|c| {
        (
            c.swarm_publisher.clone(),
            !c.target,
            c.run.demo.clone(),
            c.acquisition.as_ref().map(|a| a.model().model_id),
            c.relay.origin(),
        )
    }) else {
        eprintln!("swarm_publish_tick_failed: core_poisoned");
        return;
    };
    let run = demo.status().map(|json| json.to_value());
    let facts = OwnerFacts::from_owner_run(owner, run.as_ref(), model, relay.as_deref());
    if let Err(error) = publisher.tick(unix_seconds(), &facts) {
        // Also shown as `swarm.error_code`; the next tick retries at the bounded cadence.
        eprintln!("swarm_publish_tick_failed: {}", error.code);
    }
}
fn fast_mode_env() -> &'static str {
    api::persistent::TOKEN_PATH_ENV
}
fn publish(core: &Shared, published: &PublishedStatus) -> Result<(), PublicError> {
    // `DemoApp.status()` -> `ensure_exchange(self)`: a status read refreshes the idle announcement's model.
    ensure_exchange(core);
    // Reserve ordering before reading the run, so a delayed old snapshot cannot receive a newer
    // publication revision after a mutating POST has already published its transition.
    let (demo, revision) = {
        let mut core = core
            .lock()
            .map_err(|_| PublicError::new(503, "status_unavailable"))?;
        core.revision += 1;
        (core.run.demo.clone(), core.revision)
    };
    let demo = demo.status();
    let json = {
        let core = core
            .lock()
            .map_err(|_| PublicError::new(503, "status_unavailable"))?;
        status_json(&core, demo)
    };
    published.publish(revision, Snapshot::parse_json(json)?)
}

/// Native-launch authority: the one-use `open-demo.html` form (app.py:866-880).
struct Launcher {
    core: Shared,
}
impl NativeLauncher for Launcher {
    fn closed(&self) -> bool {
        self.core.lock().map_or(true, |c| c.closed)
    }
    fn fresh_token(&mut self) -> Result<String, PublicError> {
        Ok(URL_SAFE_NO_PAD.encode(random(32)?))
    }
    fn publish_launch(&mut self, origin: &str, token: &str) -> Result<(), PublicError> {
        let root = self.core.lock().map_err(|_| failure())?.root.clone();
        let html = format!(
            "<!doctype html><meta charset=\"utf-8\"><meta name=\"referrer\" content=\"no-referrer\">\
             <title>Open macOS A13 demo</title><h1>Open your private demo</h1>\
             <p>This local file authorizes one browser session. It expires in two minutes.</p>\
             <form method=\"post\" action=\"{origin}/session\">\
             <input type=\"hidden\" name=\"launch\" value=\"{token}\">\
             <button type=\"submit\">Open private demo</button></form>"
        );
        private_write(&root, LAUNCH_FILE, html.as_bytes())
    }
    fn consume_launch(&mut self) -> Result<(), PublicError> {
        let root = self.core.lock().map_err(|_| failure())?.root.clone();
        match fs::remove_file(root.join(LAUNCH_FILE)) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(failure()),
            _ => Ok(()),
        }
    }
}

/// The core's acquisition fields and where `_open_acquisition` (`api::app_host`) puts the run model's
/// acquisition: its private root, the owner's `model-source*` beside it, `MYCELIUM_A13_RUN_MODEL`, and the
/// repository transport. Test seam (like MYCELIUM_L10_TEST_RESOURCE_PROBE): a loopback repository instead of the
/// hub, so a process-level test can run a download to `prepared` without a network. Never set in a window run.
fn with_acquisition<R>(
    core: &mut Core,
    step: impl FnOnce(&mut app_host::AcquisitionView<'_>, &app_host::AcquisitionSite<'_>) -> R,
) -> R {
    let env = std::env::var(run_model::ENV).ok();
    let offline = core.offline;
    let transport = move || -> Arc<dyn acquisition::Transport> {
        Arc::new(match std::env::var("MYCELIUM_L10_TEST_HUB_ORIGIN") {
            Ok(origin) => HttpTransport::loopback_for_tests(&origin),
            Err(_) => HttpTransport::hugging_face(offline),
        })
    };
    let Core {
        authority,
        acquisition,
        acquisition_active,
        acquisition_policy_error,
        ..
    } = core;
    let site = app_host::AcquisitionSite {
        root: &authority.root,
        sources: &authority.root,
        target: authority.target,
        run_model_env: env.as_deref(),
        transport: &transport,
    };
    step(
        &mut app_host::AcquisitionView {
            acquisition,
            active: acquisition_active,
            policy_error: acquisition_policy_error,
        },
        &site,
    )
}
/// `DemoApp._resource_check` (app.py:929-947): the observation is made outside the app lock; a member leave
/// fence refuses first. The A16 adaptive admission is off in this host.
fn resource_check(core: &Shared) -> Option<&'static str> {
    let (fenced, observer, root) = {
        let c = core.lock().ok()?;
        (c.fenced, c.observer.clone(), c.root.clone())
    };
    if fenced {
        return Some("owner_stop");
    }
    let observation = observer.observe();
    let policy = resource_policy::read(&root)
        .ok()
        .flatten()
        .unwrap_or(json!({}));
    if let Ok(mut c) = core.lock() {
        c.resource_observation = observation.to_value();
    }
    resources::policy_violation(&policy, &observation)
}
/// The resource gate the owner's own target checks on every managed-control exchange.
///
/// v0.1.4's control pump checks only the member binding per poll (`control_pump.py:203`); power,
/// thermal and network are observed by the agent pass, the staging authorizer and the worker's
/// guard. A probe per exchange (two per poll) made polls take seconds, so the exchanges of one
/// policy interval (`DemoApp._policy_loop`, app.py:1009) share one probe's verdict. A member leave
/// fence still refuses at once.
fn exchange_resource_check(core: &Shared) -> Option<&'static str> {
    let interval = span(BudgetId::PyMyceliumA13MacosAppDemoappPolicyLoopSelfClosedWaitL1009);
    // A poisoned lock is not a verdict: probe afresh.
    if let Ok(c) = core.lock() {
        if c.fenced {
            return Some("owner_stop");
        }
        if let Some((at, verdict)) = c.exchange_resource
            && at.elapsed() < interval
        {
            return verdict;
        }
    }
    let verdict = resource_check(core);
    if let Ok(mut c) = core.lock() {
        c.exchange_resource = Some((std::time::Instant::now(), verdict));
    }
    verdict
}
/// `DemoApp._policy_loop` (app.py:1007-1030), the acquisition half: a finished attempt clears the active flag;
/// a lost membership or a policy violation cancels the real worker, not only the display.
fn acquisition_policy_tick(core: &Shared) {
    let (active, acquisition) = match core.lock() {
        Ok(c) => (c.acquisition_active, c.acquisition.clone()),
        Err(_) => return,
    };
    let Some(acquisition) = acquisition.filter(|_| active) else {
        return;
    };
    let host = HeadlessHost {
        core: core.clone(),
        runs: None,
    };
    // The verdict shared with the packaged executable (`app_host::acquisition_policy_verdict`).
    match app_host::acquisition_policy_verdict(
        &acquisition.status().state,
        || host.running() && host.membership_state().as_deref() == Some("current"),
        || resource_check(core),
    ) {
        app_host::PolicyVerdict::Keep => {}
        app_host::PolicyVerdict::Finished => {
            if let Ok(mut c) = core.lock() {
                c.acquisition_active = false;
            }
        }
        app_host::PolicyVerdict::Cancel(reason) => {
            acquisition.cancel();
            if let Ok(mut c) = core.lock() {
                c.acquisition_policy_error = Some(reason);
                c.acquisition_active = false;
            }
        }
    }
}
/// `--offline` / `--no-offline` over `MYCELIUM_OFFLINE` (`mycelium_platform_neutral.offline`); default online.
fn offline_requested() -> bool {
    let mut explicit = None;
    for arg in std::env::args().skip(1) {
        match arg.as_str() {
            "--offline" => explicit = Some(true),
            "--no-offline" => explicit = Some(false),
            _ => {}
        }
    }
    explicit.unwrap_or_else(|| {
        std::env::var("MYCELIUM_OFFLINE")
            .is_ok_and(|v| matches!(v.to_ascii_lowercase().as_str(), "1" | "true" | "yes" | "on"))
    })
}
/// The resource observer: the native helper beside the executable. `MYCELIUM_L10_TEST_RESOURCE_PROBE=<path>`
/// is the test seam (the oracle's tests inject `_policy_observer` the same way): it replaces the candidates.
fn resource_observer() -> Arc<dyn Observer> {
    if let Some(path) = std::env::var_os("MYCELIUM_L10_TEST_RESOURCE_PROBE") {
        return Arc::new(ProbeObserver {
            candidates: vec![PathBuf::from(path)],
        });
    }
    Arc::new(ProbeObserver::beside_executable())
}

fn state_root() -> Result<PathBuf, String> {
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        if arg == "--state-root" {
            return args
                .next()
                .map(PathBuf::from)
                .ok_or("state_root_missing".into());
        }
    }
    let home = std::env::var_os("HOME").ok_or("HOME_missing")?;
    Ok(PathBuf::from(home).join("Library/Application Support/org.mycelium.app/state"))
}
fn load_core(root: PathBuf) -> Result<Core, String> {
    fs::create_dir_all(&root).map_err(|e| format!("state_root:{e}"))?;
    fs::set_permissions(&root, std::os::unix::fs::PermissionsExt::from_mode(0o700))
        .map_err(|e| format!("state_root_mode:{e}"))?;
    let authority = Authority::load(root).map_err(str::to_owned)?;
    let root = authority.root.clone();
    let mut core = Core {
        relay: OwnerRelay::new(&root, relay_hooks),
        swarm_publisher: Arc::new(AppPublisher::new(&root)),
        authority,
        fenced: false,
        left: false,
        closed: false,
        acquisition: None,
        acquisition_active: false,
        acquisition_policy_error: None,
        resource_observation: Observation::unknown().to_value(),
        observer: resource_observer(),
        exchange_resource: None,
        offline: offline_requested(),
        revision: 0,
        pending: PendingWindows::default(),
        run: RunFields::default(),
        bootstrap_lifecycle: Arc::new(Mutex::new(())),
        bootstrap_incarnation: Arc::new(()),
        bootstrap: None,
        bootstrap_failure: None,
        compiler: None,
        compiled_workflow: None,
        compiled_receipt: None,
    };
    // `DemoApp.start` -> `_open_acquisition()`.
    with_acquisition(&mut core, app_host::open_acquisition).map_err(|e| e.code.to_owned())?;
    Ok(core)
}

/// `DemoApp.start`: a target finishes an interrupted leave and keeps a completed one fenced
/// (app.py:644, `_resume_leave`); a leave that cannot resume never blocks start.
fn resume_interrupted_leave(core: &Shared) {
    struct Resume<'a>(&'a Shared, PathBuf);
    impl Steps for Resume<'_> {
        fn step(&mut self, name: &str) -> Result<(), Option<String>> {
            if name == "withdraw_consent" {
                let now = SystemClock::default().wall();
                return api::preferences::standing_consent::withdraw(&self.1, now)
                    .map(|_| ())
                    .map_err(|e| Some(e.0.to_owned()));
            }
            HeadlessLeave {
                core: self.0.clone(),
            }
            .step(name)
        }
        fn compute_cleanup(&mut self) -> Option<Result<api::leave::ComputeCleanup, String>> {
            HeadlessLeave {
                core: self.0.clone(),
            }
            .compute_cleanup()
        }
        fn mark(&mut self, fence: bool, left: bool) {
            if let Ok(mut c) = self.0.lock() {
                c.fenced |= fence;
                c.left |= left;
            }
        }
    }
    let (target, root) = match core.lock() {
        Ok(c) => (c.target, c.root.clone()),
        Err(_) => return,
    };
    if target {
        let leave = MemberLeave::new(&root, Limits::default());
        if let Err(error) = resume_leave(
            &leave,
            &mut Resume(core, root.clone()),
            &mut SystemClock::default(),
        ) {
            eprintln!("member_leave_resume_failed: {error:?}");
        }
    }
}

/// `DemoApp.start` (app.py:612-624): a target with a retained membership resumes it
/// (`RemoteMacMember.start` -> `_resume`). A dead relay or a lapsed membership must not remove the
/// local recovery surface: the app starts without a member (`running = False`); any other failure
/// stops the launch with its code.
fn resume_membership(core: &Shared) -> Result<(), String> {
    // `api::joined_member`, shared with the packaged host; this launcher resumes over the public path.
    joined_member::resume_membership(&CoreCell(core.clone()), &SeedOptions::default())
}

/// The same lazy owner factory is used by the process and its lifetime check.
/// Core keeps only DemoView; the owning driver remains outside Core.
fn owner_runs(core: &Shared) -> DemoRuns {
    let chain_core = core.clone();
    DemoRuns::new(
        Box::new(move || {
            Box::new(SharedHostChain::new(HeadlessChainPorts::new(
                chain_core.clone(),
            )))
        }),
        Box::new(|| {
            fresh_identity(now()).unwrap_or_else(|_| {
                owner_run::machine::new_run_identity([0; 19], "00000000T000000Z")
            })
        }),
        Timing::registered(),
    )
}

#[cfg(test)]
#[path = "headless/chain_tests.rs"]
mod chain_tests;
#[cfg(test)]
#[path = "headless/handoff_tests.rs"]
mod handoff_tests;
#[cfg(test)]
#[path = "headless/relay_tests.rs"]
mod relay_tests;

fn run() -> Result<(), String> {
    let root = state_root()?;
    let core: Shared = Arc::new(Mutex::new(load_core(root)?));
    let root = core.lock().map_err(|_| "core_poisoned")?.root.clone();
    resume_membership(&core)?;
    // `DemoApp.start` -> `authority.start()`: an owner with a swarm brings its seed back.
    let cell = CoreCell(core.clone());
    if app_host::resumable(&cell) {
        app_host::open_owner_seed(&cell).map_err(|e| e.code.to_owned())?;
    }
    let published = Arc::new(PublishedStatus::new());
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).map_err(|e| format!("bind:{e}"))?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    let host = format!("127.0.0.1:{port}");
    let credential = URL_SAFE_NO_PAD.encode(random(32).map_err(|e| e.to_string())?);
    resume_interrupted_leave(&core);
    // `swarm_publish.resume`: retained opt-in and withdrawal debt reopen before serving starts;
    // a failure is shown in the status (`swarm.error_code`) and never blocks the start.
    let swarm_publisher = core
        .lock()
        .map_err(|_| "core_poisoned")?
        .swarm_publisher
        .clone();
    if let Err(error) = swarm_publisher.resume() {
        eprintln!("swarm_publish_resume_failed: {}", error.code);
    }
    let runs = {
        let chain = owner_runs(&core);
        core.lock().map_err(|_| "core_poisoned")?.run.demo = chain.view();
        Arc::new(Mutex::new(chain))
    };
    let persistent = PersistentBackend::new(
        root.clone(),
        HeadlessHost {
            core: core.clone(),
            runs: Some(runs),
        },
        Rest {
            core: core.clone(),
            published: published.clone(),
        },
    );
    core.lock().map_err(|_| "core_poisoned")?.pending = persistent.pending_windows();
    let backend = SignedBackend::new(persistent, now);
    let session = Session::native(
        credential.clone(),
        Box::new(Launcher { core: core.clone() }),
        Arc::new(MonotonicClock),
    )
    .map_err(|e| e.to_string())?;
    let app = Arc::new(
        Desktop::new(
            host.clone(),
            Arc::new(Control::new(backend)),
            published.clone(),
            session,
        )
        .with_product(Arc::new(product::routes(core.clone()))),
    );
    publish(&core, &published).map_err(|e| e.to_string())?;
    private_write(
        &root,
        CONTROL_FILE,
        format!("{{\"port\": {port}, \"credential\": \"{credential}\"}}").as_bytes(),
    )
    .map_err(|e| e.to_string())?;

    // `done.wait(.25)` of the main loop: a signal closes the application like a quit.
    let signals = {
        let core = core.clone();
        thread::spawn(move || {
            while core.lock().is_ok_and(|c| !c.closed) {
                if stop_signal::requested() {
                    if let Ok(mut c) = core.lock() {
                        c.closed = true;
                    }
                    return;
                }
                thread::sleep(span(BudgetId::OwnerPoll));
            }
        })
    };
    stop_signal::install();
    // `PublishedStatus.start()` (app.py:631): a maintenance publisher keeps the status current for
    // work that finishes after its action answered (a smooth leave draining on its own thread).
    let maintenance = {
        let (core, published) = (core.clone(), published.clone());
        thread::spawn(move || {
            let mut last_policy = std::time::Instant::now();
            while core.lock().is_ok_and(|c| !c.closed) {
                thread::sleep(span(BudgetId::OwnerPoll));
                // `_policy_loop` waits one second between observations.
                if last_policy.elapsed()
                    >= span(BudgetId::PyMyceliumA13MacosAppDemoappPolicyLoopSelfClosedWaitL1009)
                {
                    last_policy = std::time::Instant::now();
                    acquisition_policy_tick(&core);
                }
                publish(&core, &published).unwrap_or(());
            }
        })
    };
    // `DemoApp._renew_loop` (app.py:911-927): the lease (owner's or joined target's) is renewed every 20 s.
    let renewals = {
        let core = core.clone();
        thread::spawn(move || {
            let period = span(BudgetId::PyMyceliumA13MacosAppDemoappRenewLoopSelfClosedWaitL917);
            let mut last = std::time::Instant::now();
            while core.lock().is_ok_and(|c| !c.closed) {
                thread::sleep(span(BudgetId::OwnerPoll));
                if last.elapsed() >= period {
                    last = std::time::Instant::now();
                    app_host::renew_owner(&CoreCell(core.clone()));
                    renew_contributor(&core);
                    renew_target(&core);
                }
            }
        })
    };
    // `DemoApp._standing_loop` (app.py:1201-1212): a joined Mac's click-free steps every 2 s, here
    // its own demo agent (`demo_app.auto_step`). Its worker never outlives the loop.
    let standing = {
        let core = core.clone();
        thread::spawn(move || {
            let period =
                span(BudgetId::PyMyceliumA13MacosAppDemoappStandingLoopSelfClosedWaitL1203);
            let mut agent = member_agent::MemberAgent::default();
            let mut last = std::time::Instant::now();
            while core.lock().is_ok_and(|c| !c.closed) {
                thread::sleep(span(BudgetId::OwnerPoll));
                if last.elapsed() >= period {
                    last = std::time::Instant::now();
                    agent.tick(&core);
                }
            }
            agent.close(&core);
        })
    };
    // `SwarmPublisher.start` (swarm_publish.py:437-448): its own loop, so a slow directory never
    // delays the status, renewals or the listener. Reconcile itself bounds the network cadence.
    // Its end is announced by dropping `ended` (also on a panic), so quit can wait for it
    // with a deadline instead of a join that would wait out an exchange.
    let (swarm_ticks, swarm_ticks_ended) = {
        let core = core.clone();
        let (ended, swarm_ticks_ended) = mpsc::channel::<()>();
        let swarm_ticks = thread::spawn(move || {
            let _ended = ended;
            while core.lock().is_ok_and(|c| !c.closed) {
                swarm_publish_tick(&core);
                thread::sleep(span(BudgetId::OwnerPoll));
            }
        });
        (swarm_ticks, swarm_ticks_ended)
    };
    listener.set_nonblocking(true).map_err(|e| e.to_string())?;
    let budget = span(BudgetId::ModelSelection);
    let serve = {
        let (core, app) = (core.clone(), app.clone());
        thread::spawn(move || -> Result<(), String> {
            // Every live connection thread is retained and joined before the listener thread
            // ends, so the quit answer is on the wire before the process exits.
            let mut connections: Vec<thread::JoinHandle<()>> = Vec::new();
            // A listener that fails after a quit closed the core still ended by that quit.
            let unless_quit = |code: String| {
                if core.lock().map_or(true, |c| c.closed) {
                    Ok(())
                } else {
                    Err(code)
                }
            };
            // Test seam (like MYCELIUM_L10_TEST_RESOURCE_PROBE): `<errno>[*<count>]` drops the
            // first `count` (default 1) accepted connections after the launch request and replaces
            // each by this errno, so a test reaches a listener's error paths.
            let (injected, mut injections) = std::env::var("MYCELIUM_L10_TEST_ACCEPT_ERRNO")
                .ok()
                .and_then(|seam| {
                    let (errno, count) = seam.split_once('*').unwrap_or((seam.as_str(), "1"));
                    Some((errno.parse::<i32>().ok()?, count.parse::<usize>().ok()?))
                })
                .map_or((None, 0), |(errno, count)| (Some(errno), count));
            let mut served = 0_usize;
            // v0.1.4 parity (`socketserver` swallows accept errors and keeps serving): a transient
            // accept error is recorded once per episode; exhaustion is retried after one poll, an
            // error of one pending connection at once.
            let mut deferred = false;
            loop {
                let mut accepted = listener.accept();
                if accepted.is_ok() {
                    // The first connection is the launcher's own launch request.
                    served += 1;
                    if served >= 2
                        && injections > 0
                        && let Some(errno) = injected
                    {
                        injections -= 1;
                        accepted = Err(std::io::Error::from_raw_os_error(errno));
                    }
                }
                match accepted {
                    Ok((stream, _)) => {
                        deferred = false;
                        // Ended connections are joined as new ones arrive: an ended thread
                        // that is never joined keeps its stack mapped (one per status poll).
                        let (ended, live): (Vec<_>, Vec<_>) =
                            connections.drain(..).partition(|c| c.is_finished());
                        connections = live;
                        for handle in ended {
                            handle.join().unwrap_or(());
                        }
                        // Accepted sockets must block (the listener is polled non-blocking).
                        if stream.set_nonblocking(false).is_err() {
                            continue;
                        }
                        let app = app.clone();
                        connections.push(thread::spawn(move || {
                            serve_connection(
                                stream,
                                budget,
                                |r| app.preflight(r),
                                |r| app.handle(r),
                            )
                            .unwrap_or(());
                        }));
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        deferred = false;
                        if core.lock().map_or(true, |c| c.closed) {
                            for handle in connections {
                                handle.join().unwrap_or(());
                            }
                            return Ok(());
                        }
                        if let Err(error) =
                            listener_wait::wait(&listener, span(BudgetId::OwnerPoll))
                        {
                            return unless_quit(listener_wait::failure_code(
                                "listener_wait_failed",
                                &error,
                            ));
                        }
                    }
                    Err(error) => match listener_wait::accept_failure(&error) {
                        listener_wait::AcceptFailure::Fatal(code) => return unless_quit(code),
                        // That pending connection is gone; the next one is accepted at once.
                        listener_wait::AcceptFailure::Connection(code) => {
                            if !deferred {
                                deferred = true;
                                eprintln!("listener_accept_deferred: {code}");
                            }
                            // A stream of aborting clients cannot hold off a quit.
                            if core.lock().map_or(true, |c| c.closed) {
                                for handle in connections {
                                    handle.join().unwrap_or(());
                                }
                                return Ok(());
                            }
                        }
                        listener_wait::AcceptFailure::Transient(code) => {
                            if !deferred {
                                deferred = true;
                                eprintln!("listener_accept_deferred: {code}");
                            }
                            // A quit still ends the listener while accepts keep failing.
                            if core.lock().map_or(true, |c| c.closed) {
                                for handle in connections {
                                    handle.join().unwrap_or(());
                                }
                                return Ok(());
                            }
                            thread::sleep(span(BudgetId::OwnerPoll));
                        }
                    },
                }
            }
        })
    };
    // Ask our own listener for the first one-use launch file, as the native launcher would.
    request_launch(port, &credential).map_err(|e| format!("launch:{e}"))?;
    println!(
        "{}",
        json!({"event": "launcher_started", "distribution": "unsigned_development_demo",
               "serving_role": "not_yet_serving", "host": "rust-headless"})
    );
    std::io::stdout().flush().map_err(|e| e.to_string())?;
    // From here on every step of the host's cleanup runs, whatever failed before it: a panicked
    // helper thread or a failed step is recorded, and the FIRST failure is what the exit reports
    // (B15; a later cleanup error never masks the close's code).
    let mut failed: Option<String> = None;
    let mut keep = |step: Result<(), String>| {
        if let Err(code) = step
            && failed.is_none()
        {
            failed = Some(code);
        }
    };
    // However the listener ended (a quit, a failed accept or wait, a panic), the core is closed
    // now so the helper threads end too (B15f, REVIEW-laneB15 F2: a failure used to leave them
    // running until a signal). A poisoned core is closed all the same.
    let served = serve.join();
    lock_even_poisoned(&core).0.closed = true;
    match served {
        Ok(served) => keep(served),
        Err(_) => keep(Err("serve_thread_panicked".to_owned())),
    }
    // `swarm_publish.close` once quit stopped the run: no further directory exchange starts, a
    // live listing becomes durable withdrawal debt, and one withdrawal attempt ends within the
    // close budget (`thread.join(5)`), including any wait for a tick inside an exchange. An
    // unreachable or slow directory is not a failure here: the next start retries the debt.
    // Only a storage failure fails the close (a listing state refused at startup fails every
    // close).
    let close_budget =
        span(BudgetId::PyMyceliumA13MacosSwarmPublishSwarmpublisherCloseSelfThreadJoinL453);
    let close_until = Instant::now() + close_budget;
    keep(
        swarm_publisher
            .close(unix_seconds(), close_budget)
            .map(|_closed| ())
            .map_err(|e| format!("swarm_publish_stop_failed:{}", e.code)),
    );
    for (thread, panicked) in [
        (maintenance, "maintenance_thread_panicked"),
        (renewals, "renewal_thread_panicked"),
        (standing, "standing_thread_panicked"),
        (signals, "signal_thread_panicked"),
    ] {
        keep(thread.join().map_err(|_| panicked.to_owned()));
    }
    // The tick thread ends at its next loop check and is awaited until the close deadline
    // (`thread.join(5)` shares the close budget). It is joined only once its loop has ended, so
    // the join never waits on an exchange. One still inside a directory exchange at the
    // deadline (its state durable before the exchange) is ended by the process exit, as a crash
    // would be; nothing is written for it, since a closing launcher writes nothing to stderr.
    let ended =
        swarm_ticks_ended.recv_timeout(close_until.saturating_duration_since(Instant::now()));
    if matches!(ended, Err(mpsc::RecvTimeoutError::Disconnected)) {
        keep(
            swarm_ticks
                .join()
                .map_err(|_| "swarm_publish_thread_panicked".to_owned()),
        );
    }
    // Close: the acquisition is cancelled, joined and recorded `closed` (it stays closed across a restart);
    // transient control files go away.
    // A poisoned core is reported, and what it holds is still closed (B15f, REVIEW-laneB15 F4).
    let (acquisition, poisoned) = {
        let (mut c, poisoned) = lock_even_poisoned(&core);
        (c.acquisition.take(), poisoned)
    };
    if poisoned {
        keep(Err("core_poisoned".to_owned()));
    }
    if let Some(acquisition) = acquisition {
        keep(
            acquisition
                .close()
                .map_err(|e| format!("acquisition_stop_failed:{}", e.code())),
        );
    }
    for name in [LAUNCH_FILE, CONTROL_FILE] {
        match fs::remove_file(root.join(name)) {
            Err(e) if e.kind() != std::io::ErrorKind::NotFound => {
                keep(Err(format!("cleanup:{e}")));
            }
            _ => {}
        }
    }
    failed.map_or(Ok(()), Err)
}
/// `cell`'s guard even when a panicked holder poisoned it, and whether it was poisoned: quit
/// cleanup still closes what the core holds (B15f, REVIEW-laneB15 F4) and reports the poison.
fn lock_even_poisoned<T>(cell: &Mutex<T>) -> (std::sync::MutexGuard<'_, T>, bool) {
    match cell.lock() {
        Ok(guard) => (guard, false),
        Err(poisoned) => (poisoned.into_inner(), true),
    }
}
#[cfg(test)]
mod quit_tests {
    use super::lock_even_poisoned;
    use std::sync::{Arc, Mutex};

    /// B15f (REVIEW-laneB15 F4): a core poisoned by a panicked holder still yields what it
    /// holds to the quit cleanup (here the acquisition slot), and the poison is reported.
    #[test]
    fn a_poisoned_core_still_gives_up_what_it_holds() {
        let core = Arc::new(Mutex::new(Some("acquisition")));
        let holder = core.clone();
        let panicked = std::thread::spawn(move || {
            let _held = holder.lock().unwrap();
            panic!("a helper panicked while holding the core");
        })
        .join();
        assert!(panicked.is_err() && core.is_poisoned());
        let (taken, poisoned) = {
            let (mut slot, poisoned) = lock_even_poisoned(&core);
            (slot.take(), poisoned)
        };
        assert_eq!(
            taken,
            Some("acquisition"),
            "the poisoned core kept its acquisition"
        );
        assert!(poisoned, "the poison was not reported");
        let healthy = Mutex::new(Some(1));
        let (slot, poisoned) = lock_even_poisoned(&healthy);
        assert_eq!((*slot, poisoned), (Some(1), false));
    }
}
fn request_launch(port: u16, credential: &str) -> std::io::Result<()> {
    let mut stream = TcpStream::connect((Ipv4Addr::LOCALHOST, port))?;
    stream.set_read_timeout(Some(span(BudgetId::ModelSelection)))?;
    write!(
        stream,
        "POST /native-session HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n\
         X-Native-Authorization: {credential}\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}"
    )?;
    let mut reply = String::new();
    stream.read_to_string(&mut reply)?;
    if reply.starts_with("HTTP/1.1 200") {
        Ok(())
    } else {
        Err(std::io::Error::other("native_session_refused"))
    }
}

/// `signal.signal(SIGTERM/SIGINT, stop)` of `__main__.py:186-188`: the handler only raises a flag
/// that the main loop polls. This is the one audited unsafe boundary of this example: `signal(2)`
/// with a handler that performs a single atomic store (async-signal-safe).
mod stop_signal {
    use std::sync::atomic::{AtomicBool, Ordering};
    static REQUESTED: AtomicBool = AtomicBool::new(false);
    extern "C" fn handler(_signal: libc::c_int) {
        REQUESTED.store(true, Ordering::SeqCst);
    }
    #[allow(unsafe_code)]
    pub fn install() {
        for signal in [libc::SIGTERM, libc::SIGINT] {
            // SAFETY: `handler` is an `extern "C" fn(c_int)` that only stores to an atomic.
            unsafe {
                libc::signal(
                    signal,
                    handler as extern "C" fn(libc::c_int) as libc::sighandler_t,
                );
            }
        }
    }
    pub fn requested() -> bool {
        REQUESTED.load(Ordering::SeqCst)
    }
}

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(code) => {
            let safe: String = code
                .chars()
                .map(|c| {
                    if c.is_ascii_alphanumeric() || c == '_' {
                        c
                    } else {
                        '_'
                    }
                })
                .take(96)
                .collect();
            eprintln!(
                "{}",
                json!({"event": "launcher_failed", "code": safe, "state_retained": true})
            );
            std::process::ExitCode::from(3)
        }
    }
}
