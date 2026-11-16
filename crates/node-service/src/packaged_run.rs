// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's run in the packaged host (lane PH3b S3), as the shipped Mac app runs it (v0.1.4 @
//! f4e366e9): `#demo-start` / `#demo-stop` (run.js:192-193) -> `DemoApp.action` (app.py:1306) ->
//! `_action` (app.py:1362-1365) -> `demo_app.action` (demo_app.py:109-133) -> `OwnerRun`; the page's
//! `status.owner_run` (`DemoApp.status`, app.py:1244-1283 -> `demo_app.owner_status`,
//! demo_app.py:136-150; run.js:94-138); Quit's `demo_app.close` (demo_app.py:199-209, called first by
//! `DemoApp.close`, app.py:1815-1825) and `_stop_local_contributor` (app.py:1842).
//!
//! The run is `api::owner_driver::DemoRuns` over the shared `OwnerChain`; the model step reads this
//! host's acquisition, the contributor and descriptors steps are `api::owner_ports`, the observe
//! and compile steps are `api::owner_observation` / `api::owner_compiler` over this host's
//! [`ChainHost`] (lane PH3b S3c; the same composition the headless launcher runs): the managed owner
//! is bound on this owner's seed (its mailbox serves the seed's managed-control routes, so a joined
//! Mac's agent reaches the run as in the shipped app). The work phase (`slices_ports`/`work_ports`)
//! stays absent (PH3b S4c: `api::joined_owner::work::OwnerWork`) and ends with the closed
//! `owner_run_step_not_ported` refusal naming its seam, never a success.
use crate::{app_host::HostLoop, packaged_network::Network, service::NodeService};
use api::{
    acquisition::Acquisition,
    actions::Action,
    authority_view,
    bootstrap_process::CleanupFailure,
    installation_descriptor::Failure,
    owner_chain::{
        self, ModelAdapter, SharedHostChain, acquisition_exception, prepared_source_from,
    },
    owner_compiler::{Compiler, Observation, ObservedSource, SharedCompiler},
    owner_driver::{DemoRuns, DemoView, Timing, fresh_identity},
    owner_observation::{self, ChainHost, ChainView, target},
    owner_ports::{self, OwnerPorts, RunFields, RunHost, RunView},
    persistent::RelayState,
    preferences::resource_policy,
    schema::PublicError,
    wire::Json,
};
use budgets::{BudgetId, Entries};
use desktop::lifecycle::{CleanupError, Poll};
use owner_run::{
    chain::{PortError, StepError},
    steps::{model::Reverify, observe::ObservePort},
};
use runtime::observe::ObservationContext;
use serde_json::{Map, Value, json};
use std::{
    path::{Path, PathBuf},
    sync::{Arc, Mutex, MutexGuard, PoisonError, TryLockError, Weak},
    thread::JoinHandle,
    time::{Duration, Instant},
};

mod chat;
#[cfg(test)]
mod program_tests;
mod work;
pub use chat::{RunChatRoute, chat_run};

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// The managed control owner this host binds on its owner's seed (`api::owner_observation`).
pub type Managed = owner_observation::Managed<PackagedRun>;
/// The packaged host's owner authority: its seed runtime carries this host's managed owner.
pub type Authority = api::app_host::Authority<Managed>;

/// The run's composition seams and its shared handle: the composition fills it once the host
/// installed the run; the window's status and Quit read it.
#[derive(Clone, Default)]
pub struct RunConfig {
    /// The installed native sidecar the owner's descriptor names (`installed_sidecar`,
    /// native_runtime.py:8-22). `None`: beside the executable, `native/mycelium-iroh-sidecar`.
    pub sidecar: Option<PathBuf>,
    /// Expected installed executable identity from bundle admission. None means
    /// explicitly unavailable, never development environment selection.
    pub native_program: Option<api::native_compiler::NativeProgram>,
    /// Filled by the host once it installed the run.
    pub installed: RunSlot,
    /// TEST ONLY (decision U4, PH3B-COMMON; ruling 14): replaces the run's OBSERVE PORT with a test
    /// double built over this host's real observation backend, to exercise the chain past observe
    /// on one machine. The compile port then reads the double's context. The product composition
    /// always leaves this `None`: the packaged app has no host-identity seam.
    pub observe_test_double: Option<ObserveTestDouble>,
}
/// An observe port a TEST supplies in place of the real one (see [`RunConfig::observe_test_double`]).
pub trait ObserveDouble: ObservePort + Send {
    /// The context its `prepare` / `collect` produced, which the compile port reads.
    fn context(&self) -> Option<&ObservationContext>;
    /// `chain.teardown`'s observation half (the offers' cleanup and retirement).
    fn teardown(&mut self) -> Result<(), String>;
    /// The teardown's last act: end the run's context (`ObserveAdapter::end_run`).
    fn end_run(&mut self);
}
/// Builds a test's [`ObserveDouble`] over the run's real observation backend.
pub type ObserveTestDouble =
    Arc<dyn Fn(owner_observation::Backend<PackagedRun>) -> Box<dyn ObserveDouble> + Send + Sync>;
/// The composed run's shared handle (opaque).
#[derive(Clone, Default)]
pub struct RunSlot(Arc<Mutex<Option<Installed>>>);
impl RunConfig {
    /// The composed run's status view (none before the host installed the run).
    pub fn observed(&self) -> Option<DemoView> {
        self.installed().map(|installed| installed.view)
    }
    fn installed(&self) -> Option<Installed> {
        lock(&self.installed.0).clone()
    }
}
#[derive(Clone)]
struct Installed {
    view: DemoView,
    host: PackagedRun,
    runs: Arc<Mutex<DemoRuns>>,
    renewal: Option<Arc<HostLoop>>,
    /// `DemoApp.status_publisher`'s maintenance (`PublishedStatus._loop` -> `refresh` ->
    /// `run.status()`, published_status.py:32-48, 72-76): the serving run's live checks.
    status: Option<Arc<HostLoop>>,
}

/// What the packaged host's model source answers the run (`app.acquisition`, `_resource_check`,
/// `reverify_model_snapshot`), built by `PackagedHost::with_run` over its own state.
pub struct ModelHooks {
    pub acquisition: Box<dyn Fn() -> Option<Acquisition> + Send + Sync>,
    pub resource_check: Box<dyn Fn() -> Option<&'static str> + Send + Sync>,
    pub reverify: Box<dyn Fn() -> Reverify + Send + Sync>,
}

/// A close that did not prove every member's stage released: the SAME owner objects are kept
/// (the member Work with its returned links, and the local Service whose control loop those
/// links reach) so a later Stop/Quit/close retries exactly the unresolved stages, even after
/// the run's own ports object is gone.
#[derive(Default)]
struct WorkDebt {
    work: Option<api::joined_owner::work::OwnerWork>,
    local: Option<crate::packaged_work::Service>,
    /// A close that failed without a member cleanup report (worker panic): no later call may
    /// declare this owner settled, because no proof of the member stages exists.
    unprovable: Option<String>,
    /// The compile outputs of the runs whose close failed (lane PHY item 1): held, they keep
    /// their run directories' custody, so no retirement pass removes stage shards the retained
    /// local Service may still read; dropped with the debt once it is settled.
    outputs: Vec<api::compiler_inputs::InputArtifact>,
}

/// Retire this owner's dead compile runs from its storage ledger (lane PHY item 1, ledger rows 8
/// and 81): every run no process holds any more loses its stage shards and its charge. Called
/// when a run ends (the chain's teardown: a failed step, Stop, Quit) and at the next Start
/// (`work::Composed::new_run`), which also covers a run a lost process of this build left. A
/// refusal is logged by name and leaves that run charged, and so does a legacy run (one an older
/// build started, which never took the run directory's lock): it never fails the run or its
/// teardown.
fn retire_dead_runs(host: &PackagedRun) {
    let root = host.with(|c| c.authority.root.clone());
    match api::compiler_inputs::retire_dead_runs(&root) {
        Ok(done) => {
            for run in done.retired {
                eprintln!("owner_storage_retired:{run}");
            }
            for (run, code) in done.refused {
                eprintln!("owner_storage_retire_refused:{run}:{code}");
            }
            // A run an older build started (no custody marker): it may still be live in that
            // build's process, so it stays charged and is only named (PHY-01).
            for (run, bytes) in done.legacy {
                eprintln!("owner_storage_legacy_kept:{run}:{bytes}");
            }
        }
        Err(error) => eprintln!("owner_storage_retire_failed:{}", error.0),
    }
}

/// The owner's bootstrap custody beside the run's fields (the headless launcher keeps the same
/// fields in its Core): the contributor incarnation a bootstrap reservation belongs to, the owner's
/// own local target worker, a bootstrap whose cleanup is still unproven, the run's compiler as the
/// compile status reaches it, and the managed-control exchanges' shared resource verdict.
#[derive(Default)]
struct Custody {
    incarnation: Arc<()>,
    bootstrap: Option<target::Worker>,
    failure: Option<CleanupFailure>,
    compiler: Option<Weak<Mutex<Compiler<PackagedRun>>>>,
    work_stop: Option<api::joined_owner::work::Cancel>,
    work_session: Option<crate::packaged_work::SessionCell>,
    /// Member Work / local Service retained by a failed work close (never replaced while owed).
    work_debt: Option<WorkDebt>,
    exchange_resource: Option<(Instant, Option<&'static str>)>,
    /// The run's route as the packaged chat reads it (lane CHAT): set when the run's work is
    /// composed, taken out by its teardown ([`chat::RouteSource`]).
    route: Option<chat::RouteSource>,
    /// The page's route chats as the run's ports cancel them (`DesktopServices::run_chats`, put
    /// here by [`chat_run`]): teardown, ceiling, dead member and rollover (lane CHAT).
    chats: Option<api::chat_service::RunChats>,
}

/// The packaged host as `api::owner_ports::RunHost` and `api::owner_observation::ChainHost`: its
/// authority, the network beside it (relay, `_left`, `_leave_fence`), the node service's shutdown
/// fence (`DemoApp.closed`), the run's own fields and the owner's bootstrap custody.
#[derive(Clone)]
pub struct PackagedRun {
    authority: Arc<Mutex<Authority>>,
    network: Arc<Network>,
    node: NodeService,
    model: Arc<ModelHooks>,
    run: Arc<Mutex<RunFields>>,
    lifecycle: Arc<Mutex<()>>,
    custody: Arc<Mutex<Custody>>,
    sidecar: Option<PathBuf>,
}
/// [`PackagedRun`] without keeping its state alive: the seed-owned managed owner and the
/// observation/compile ports retain only this (no cycle through the seed runtime).
#[derive(Clone)]
pub struct WeakRun {
    authority: Weak<Mutex<Authority>>,
    network: Weak<Network>,
    node: NodeService,
    model: Weak<ModelHooks>,
    run: Weak<Mutex<RunFields>>,
    lifecycle: Weak<Mutex<()>>,
    custody: Weak<Mutex<Custody>>,
    sidecar: Option<PathBuf>,
}
impl PackagedRun {
    /// `_relay_is_serving` (app.py:770-777): the relay's renewal probe reads this host's run state.
    pub fn set_serving(&self, serving: bool) {
        self.network.set_serving(serving);
    }
    pub fn new(
        authority: Arc<Mutex<Authority>>,
        network: Arc<Network>,
        node: NodeService,
        model: ModelHooks,
        sidecar: Option<PathBuf>,
    ) -> Self {
        Self {
            authority,
            network,
            node,
            model: Arc::new(model),
            run: Arc::default(),
            lifecycle: Arc::default(),
            custody: Arc::default(),
            sidecar,
        }
    }
}
impl RunHost for PackagedRun {
    type Managed = Managed;
    /// The network and the fence are read first, then the authority and the run's fields are held
    /// together (order: authority -> run fields, everywhere).
    fn with<R>(&self, f: impl FnOnce(&mut RunView<'_, Self::Managed>) -> R) -> R {
        let closed = self.node.closed().unwrap_or(true);
        let left = self.network.left();
        let relay_absent = matches!(self.network.state(), RelayState::Absent);
        let mut authority = lock(&self.authority);
        let mut run = lock(&self.run);
        let result = f(&mut RunView {
            authority: &mut authority,
            run: &mut run,
            closed,
            left,
            relay_absent,
        });
        // Every contributor mutation (including the renewal's take/restore) passes this host.
        // Keep the preceding lease-bounded snapshot while its Session is in a recorded renewal
        // flight (lane FLK, `api::renewal`), past its deadline too: the flight's thread has
        // neither landed nor unwound and the seed has not refused, so the resident worker keeps
        // the old lease and its consent checks (`Service::live`), as the base kept it; clearing it
        // would end that worker for good while a late landing still renews the Session (FLK2-01).
        // A flight that unwound or that the seed refused ends with no snapshot behind. The
        // registration (`Work::local`) still refuses an overdue flight, as the base did.
        if let Some(cell) = &lock(&self.custody).work_session {
            let mut held = lock(cell);
            if closed || left || !authority.running || authority.target || run.contributor.is_none()
            {
                *held = None;
            } else if let Some(member) = run.contributor.as_ref() {
                match &member.session {
                    Some(session) => *held = Some(session.clone()),
                    None if member.flight.is_some() => {}
                    None => *held = None,
                }
            }
        }
        result
    }
    fn bootstrap_lifecycle(&self) -> Arc<Mutex<()>> {
        self.lifecycle.clone()
    }
    fn membership_current(&self) -> bool {
        crate::packaged_network::membership_state(&self.authority).as_deref() == Some("current")
    }
    /// Durable admission: no unsettled native bootstrap stage of an earlier contributor
    /// (`api::owner_observation::target::admit_cleanup_free`, as the headless launcher).
    fn admit_contributor(&self, root: &Path) -> Result<(), PublicError> {
        target::admit_cleanup_free(root).map_err(|e| PublicError::new(409, e.0))
    }
    /// Close the contributor's own hello worker (the caller holds the bootstrap lifecycle).
    fn close_contributor(&self) -> Result<(), PublicError> {
        target::close_locked(self).map_err(|e| PublicError::new(409, e.0))
    }
    fn resource_check(&self) -> Option<&'static str> {
        (self.model.resource_check)()
    }
    fn acquisition(&self) -> Option<Acquisition> {
        (self.model.acquisition)()
    }
    /// The owner's own contributor stages its hello for the run's bootstrap offer over the
    /// owner's mailbox (`api::owner_observation::bootstrap`, demo_target.py:494-514).
    fn bootstrap(
        &self,
        node: &str,
        run: &str,
        deployment: &str,
        offer: &str,
    ) -> Result<(), Failure> {
        owner_observation::bootstrap(self, node, run, deployment, offer)
            .map_err(|error| Failure::new("RuntimeError", error.0))
    }
    fn sidecar(&self) -> Result<PathBuf, Failure> {
        owner_ports::installed_sidecar(self.sidecar.clone())
    }
    /// The owner's request to clean its own contributor's hello stage
    /// (`api::owner_observation::pending_cleanup`).
    fn pending_cleanup(&self, node: &str) -> Result<Option<Value>, Failure> {
        owner_observation::pending_cleanup(self, node)
            .map_err(|error| Failure::new("RuntimeError", error.0))
    }
    /// Close the contributor's hello worker for that request (`api::owner_observation::cleanup`).
    fn cleanup(&self, node: &str, request: &Value) -> Result<(), Failure> {
        owner_observation::cleanup(self, node, request)
            .map_err(|error| Failure::new("RuntimeError", error.0))
    }
}
impl ChainHost for PackagedRun {
    type Weak = WeakRun;
    fn downgrade(&self) -> WeakRun {
        WeakRun {
            authority: Arc::downgrade(&self.authority),
            network: Arc::downgrade(&self.network),
            node: self.node.clone(),
            model: Arc::downgrade(&self.model),
            run: Arc::downgrade(&self.run),
            lifecycle: Arc::downgrade(&self.lifecycle),
            custody: Arc::downgrade(&self.custody),
            sidecar: self.sidecar.clone(),
        }
    }
    fn upgrade(weak: &WeakRun) -> Option<Self> {
        Some(Self {
            authority: weak.authority.upgrade()?,
            network: weak.network.upgrade()?,
            node: weak.node.clone(),
            model: weak.model.upgrade()?,
            run: weak.run.upgrade()?,
            lifecycle: weak.lifecycle.upgrade()?,
            custody: weak.custody.upgrade()?,
            sidecar: weak.sidecar.clone(),
        })
    }
    /// One snapshot: the fence and the acquisition are read first, then the authority, the run's
    /// fields and the custody are held together (order: authority -> run fields -> custody); a
    /// poisoned lock is `None` (the moved callers keep their own refusal codes).
    fn try_with<R>(&self, f: impl FnOnce(&mut ChainView<'_, Self>) -> R) -> Option<R> {
        let closed = self.node.closed().unwrap_or(true);
        let acquisition = (self.model.acquisition)();
        let mut authority = self.authority.lock().ok()?;
        let mut run = self.run.lock().ok()?;
        let mut custody = self.custody.lock().ok()?;
        let Custody {
            incarnation,
            bootstrap,
            failure,
            compiler,
            ..
        } = &mut *custody;
        Some(f(&mut ChainView {
            authority: &mut authority,
            run: &mut run,
            closed,
            acquisition: &acquisition,
            bootstrap_lifecycle: &self.lifecycle,
            bootstrap_incarnation: incarnation,
            bootstrap,
            bootstrap_failure: failure,
            compiler,
        }))
    }
    /// The resource gate the owner's own target checks on every managed-control exchange, as the
    /// headless launcher's: a leave fence refuses at once; otherwise the exchanges of one policy
    /// interval (`DemoApp._policy_loop`, app.py:1009) share one probe's verdict (v0.1.4's control
    /// pump checks only the member binding per poll, control_pump.py:203).
    fn exchange_resource_check(&self) -> Option<&'static str> {
        if self.network.fenced() {
            return Some("owner_stop");
        }
        let interval = BudgetId::PyMyceliumA13MacosAppDemoappPolicyLoopSelfClosedWaitL1009
            .span(Entries::ZERO)
            .map_or(Duration::ZERO, |span| Duration::from_millis(span.millis()));
        if let Some((at, verdict)) = lock(&self.custody).exchange_resource
            && at.elapsed() < interval
        {
            return verdict;
        }
        let verdict = self.resource_check();
        lock(&self.custody).exchange_resource = Some((Instant::now(), verdict));
        verdict
    }
}

/// The model step's facts (`demo_chain.py:442-507` reads them from `app`).
#[derive(Clone)]
struct ModelPorts(PackagedRun);
impl owner_chain::ChainPorts for ModelPorts {
    /// The packaged two-Mac flow stages by slice: its member side has only the slice stage flavour
    /// (`MemberShard::begin` requires a `mycelium.a13_node_slice_plan.v1` recipe and a
    /// `deployment/<name>` row). The whole-snapshot stage flavour is v0.3 (roadmap item 23), so a
    /// source below the ported 4 GB window still stages by slice here.
    fn slice_staging(&self) -> bool {
        true
    }
    fn owner_and_running(&self) -> bool {
        self.0.with(|c| !c.authority.target && c.authority.running)
    }
    fn swarm_created(&self) -> bool {
        self.0.with(|c| c.authority.owner_authority().is_some())
    }
    fn acquisition_state(&self) -> String {
        let state = self
            .0
            .acquisition()
            .map_or_else(String::new, |a| a.status().state);
        if state.is_empty() {
            eprintln!("l11_acq_state:empty");
        }
        state
    }
    fn reverify(&self) -> Reverify {
        (self.0.model.reverify)()
    }
    fn prepared_source(&self) -> Result<owner_run::steps::model::PreparedSource, StepError> {
        let acquisition = self.0.acquisition().ok_or_else(|| StepError::Exception {
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
        let root = self.0.with(|c| c.authority.root.clone());
        match resource_policy::read(&root).map_err(owner_ports::step_exception)? {
            Some(Value::Object(policy)) => Ok(policy.get("storage_bytes").cloned()),
            _ => Ok(None),
        }
    }
    fn check_rollover_members(&self, _: &Value) -> Result<(), StepError> {
        Err(owner_chain::unwired(
            "model",
            "NP-L10-rollover",
            "SE-L10-ROLLOVER",
        ))
    }
}

/// A test's observe port shared by the observe step and the compile port.
#[derive(Clone)]
struct DoubleObservation(Arc<Mutex<Box<dyn ObserveDouble>>>);
impl ObservePort for DoubleObservation {
    fn prepare(&mut self, run: &str, deployment: &str, cards: &[Value]) -> Result<(), PortError> {
        lock(&self.0).prepare(run, deployment, cards)
    }
    fn rebuild(
        &mut self,
        run: &str,
        deployment: &str,
        cards: &[Value],
    ) -> Result<String, PortError> {
        lock(&self.0).rebuild(run, deployment, cards)
    }
    fn facts_nodes(&mut self) -> Vec<String> {
        lock(&self.0).facts_nodes()
    }
    fn offers(&mut self) -> Vec<(String, String)> {
        lock(&self.0).offers()
    }
    fn collect(&mut self) -> Result<(), PortError> {
        lock(&self.0).collect()
    }
    fn observed_hosts(&mut self) -> Vec<Option<String>> {
        lock(&self.0).observed_hosts()
    }
    fn retire_bootstrap(&mut self) -> Result<(), PortError> {
        lock(&self.0).retire_bootstrap()
    }
}
impl ObservedSource for DoubleObservation {
    fn with_context(
        &self,
        f: &mut dyn FnMut(Option<&ObservationContext>),
    ) -> Result<(), PortError> {
        f(lock(&self.0).context());
        Ok(())
    }
    fn retire_bootstrap(&mut self) -> Result<(), PortError> {
        ObservePort::retire_bootstrap(self)
    }
}
/// The run's observe port: the real one, or a test's double ([`RunConfig::observe_test_double`]).
enum Observe {
    Real(Observation<PackagedRun>),
    TestDouble(DoubleObservation),
}

/// The chain's ports for one run: model, contributor, descriptors, observe and compile (each
/// temporary borrow uses adapters over the host's own state, as the headless launcher's); the
/// work phase stays absent (PH3b S4c).
struct PackagedChainPorts {
    model: ModelAdapter<ModelPorts>,
    contributor: OwnerPorts<PackagedRun>,
    reports: work::Reports,
    work: work::Work,
    observation: Observe,
    compiler: SharedCompiler<PackagedRun>,
}
impl PackagedChainPorts {
    fn new(
        host: &PackagedRun,
        double: Option<&ObserveTestDouble>,
        native_program: Option<api::native_compiler::NativeProgram>,
    ) -> Self {
        let (observation, compiler) = match double {
            None => {
                let observation = Observation(Arc::new(Mutex::new(owner_observation::adapter(
                    host.downgrade(),
                ))));
                let compiler = Compiler::new(host.downgrade(), observation.clone());
                (Observe::Real(observation), compiler)
            }
            Some(double) => {
                let observation =
                    DoubleObservation(Arc::new(Mutex::new(double(owner_observation::Backend {
                        core: host.downgrade(),
                        published: std::collections::BTreeMap::new(),
                    }))));
                let compiler = Compiler::observing(host.downgrade(), Box::new(observation.clone()));
                (Observe::TestDouble(observation), compiler)
            }
        };
        let compiler = Arc::new(Mutex::new(compiler.with_native_program(native_program)));
        lock(&host.custody).compiler = Some(Arc::downgrade(&compiler));
        let ports = OwnerPorts::new(host.clone());
        let work = work::Work::new(host.clone());
        Self {
            model: ModelAdapter::new(ModelPorts(host.clone())),
            contributor: ports.clone(),
            reports: work::Reports {
                inner: ports,
                work: work.clone(),
            },
            work,
            observation,
            compiler: SharedCompiler(compiler),
        }
    }
}
impl PackagedChainPorts {
    /// `chain.teardown(ctx, failed=)` over the run's `ctx.state` (`None`: a teardown without one,
    /// which then announces nothing): every chat, the route's work, the `stopped` announcement,
    /// the compiler's worker, then the bootstrap offers' cleanup and retirement
    /// (`api::owner_observation::Backend::teardown`) and the run's context.
    fn teardown_with(
        &mut self,
        failed: bool,
        state: Option<&owner_run::state::RunState>,
        env: &mut dyn owner_run::chain::StepEnv,
    ) -> Result<(), String> {
        // Every chat FIRST, while the route still takes `DELETE` (demo_chain.py:1326-1338).
        chat::teardown(self.work.host(), failed);
        let work = self.close_work();
        // `stopped` is an instruction, not just status: the joined Macs' agents tear their work
        // down on receipt and their cards read `stopped` (demo_chain.py:1381-1390 @ f4e366e9),
        // announced once the route's close had its allowance, whatever it answered (never a
        // proof of cleanup), before the offers are retired and the run's context ends. Only for
        // a run whose model step ran (`if ctx.state.get('model') is not None`); a failure is
        // recorded `owner_stop_announce_failed` and the teardown goes on (lane CHATPAGE item 3:
        // without it Mac B's card kept `phase: serving` after Stop and Quit).
        if let Some(state) = state.filter(|state| state.model.is_some())
            && let Err(error) =
                owner_run::steps::exchange::announce(&mut self.reports, state, "stopped", None)
        {
            let class = match error {
                owner_run::chain::StepError::Refused(_) => "OwnerRunError".to_owned(),
                owner_run::chain::StepError::Stopped => "_Stopped".to_owned(),
                owner_run::chain::StepError::Exception { class } => class,
            };
            env.record_exception("owner_stop_announce_failed", &class);
        }
        let (compilation, outputs) = {
            let mut compiler = self
                .compiler
                .0
                .lock()
                .map_err(|_| "compiler_state_unavailable".to_owned())?;
            let compilation = compiler.teardown();
            (compilation, compiler.release_outputs())
        };
        // The run's stage shards go with the run, unless its close left work owed: then the
        // debt keeps them (and their charge) until it is settled (lane PHY item 1).
        if work.is_ok() {
            drop(outputs);
            retire_dead_runs(self.work.host());
        } else if let Some(outputs) = outputs {
            lock(&self.work.host().custody)
                .work_debt
                .get_or_insert_with(WorkDebt::default)
                .outputs
                .push(outputs);
        }
        // The context ends with the run whatever its cleanup answered (demo_chain.py:1436): the
        // ports live as long as the host (`work::Composed`), and the next Start prepares anew.
        let observation = match &self.observation {
            Observe::Real(observation) => {
                let mut adapter = observation
                    .0
                    .lock()
                    .map_err(|_| "managed_owner_lock_unavailable".to_owned())?;
                let result = adapter.host_mut().backend.teardown();
                adapter.end_run();
                result
            }
            Observe::TestDouble(double) => {
                let mut double = double
                    .0
                    .lock()
                    .map_err(|_| "managed_owner_lock_unavailable".to_owned())?;
                let result = double.teardown();
                double.end_run();
                result
            }
        };
        work.and(compilation).and(observation)
    }
}
impl owner_run::owner_chain::ChainPorts for PackagedChainPorts {
    /// `chain.teardown` without the run's state: everything but the `stopped` announcement.
    fn teardown(
        &mut self,
        failed: bool,
        env: &mut dyn owner_run::chain::StepEnv,
    ) -> Result<(), String> {
        self.teardown_with(failed, None, env)
    }
    /// `chain.teardown(ctx, failed=)`: what [`OwnerChain`](owner_run::owner_chain::OwnerChain)
    /// calls, with `ctx.state` as the steps left it.
    fn teardown_run(
        &mut self,
        failed: bool,
        state: &owner_run::state::RunState,
        env: &mut dyn owner_run::chain::StepEnv,
    ) -> Result<(), String> {
        self.teardown_with(failed, Some(state), env)
    }
    /// The serving run's probes go to its resident owner workflow (lane SRV), never the trait
    /// defaults, which would never roll over, never reach the ceiling and never fail a serving run.
    fn rollover_due(&mut self, state: &owner_run::state::RunState) -> Option<bool> {
        self.work.rollover_due(state)
    }
    fn ceiling_reached(
        &mut self,
        state: &owner_run::state::RunState,
    ) -> owner_run::machine::CeilingProbe {
        let probe = self.work.ceiling_reached(state);
        chat::ceiling(self.work.host(), &probe);
        probe
    }
    fn monitor(&mut self, state: &owner_run::state::RunState) -> owner_run::machine::MonitorProbe {
        let reports = &mut self.reports;
        let probe = self.work.monitor(state, &mut || {
            owner_run::steps::exchange::ReportsPort::tick_local(reports);
        });
        chat::monitored(self.work.host(), &probe);
        probe
    }
    fn slices_ports(&mut self) -> Option<owner_run::steps::slices::SlicesPorts<'_>> {
        Some(self.slices())
    }
    fn work_ports(&mut self) -> Option<owner_run::steps::publish::WorkPorts<'_>> {
        Some(self.publication())
    }
    fn activation_port(&mut self) -> Option<&mut dyn owner_run::steps::activate::ActivationPort> {
        Some(&mut self.work)
    }
    fn ports(&mut self) -> owner_run::owner_chain::Ports<'_> {
        owner_run::owner_chain::Ports {
            model: &mut self.model,
            contributor: Some(&mut self.contributor),
            reports: Some(&mut self.reports),
            observe: Some(match &mut self.observation {
                Observe::Real(observation) => observation as &mut dyn ObservePort,
                Observe::TestDouble(double) => double,
            }),
            compile: Some(&mut self.compiler),
        }
    }
}

/// `app.owner_run` of the packaged host: installed into `config` for the window's status and Quit.
pub struct OwnerRun {
    host: PackagedRun,
    runs: Arc<Mutex<DemoRuns>>,
}
impl OwnerRun {
    /// The run over `host`, its status handle published in `config`; `renewal` is the contributor's
    /// lease loop (`DemoApp._renew_loop`'s `contributor.renew()`), stopped by Quit.
    pub fn install(host: PackagedRun, config: &RunConfig, renewal: Option<Arc<HostLoop>>) -> Self {
        let chain_host = host.clone();
        let double = config.observe_test_double.clone();
        let native_program = config.native_program.clone();
        Self::install_with(
            host,
            config,
            renewal,
            Box::new(move || {
                Box::new(work::Composed::new(
                    &chain_host,
                    double.as_ref(),
                    native_program.clone(),
                ))
            }),
        )
    }
    /// [`OwnerRun::install`] over the chain `factory` builds for the run (the composition's is
    /// `work::Composed`; a test wraps a real `Composed`).
    fn install_with(
        host: PackagedRun,
        config: &RunConfig,
        renewal: Option<Arc<HostLoop>>,
        factory: Box<dyn FnMut() -> Box<dyn api::owner_driver::Chain> + Send>,
    ) -> Self {
        let runs = DemoRuns::new(
            factory,
            Box::new(|| {
                fresh_identity(api::app_host::now()).unwrap_or_else(|_| {
                    owner_run::machine::new_run_identity([0; 19], "00000000T000000Z")
                })
            }),
            Timing::registered(),
        );
        let view = runs.view();
        host.with(|c| c.run.demo = view.clone());
        // Every second, outside the host's and the run's locks: the run's own thread runs the
        // ceiling, rollover and monitor checks of a serving run. The UI's status reads stay
        // snapshots (`DemoView::status`). Dropped, it is joined for at most 2 s, as
        // `PublishedStatus.stop` joins (`self.thread.join(2)`, published_status.py:78-81).
        let probe = runs.probe();
        let status = match HostLoop::start_joined_within(
            "packaged-owner-run-status",
            BudgetId::PyMyceliumA13MacosPublishedStatusPublishedstatusLoopSelfStopWaitL73,
            &host.node,
            BudgetId::PyMyceliumA13MacosPublishedStatusPublishedstatusStopSelfThreadJoinL81,
            move || probe.refresh(),
        ) {
            Ok(status) => Some(status),
            Err(code) => {
                eprintln!("owner run status: {code}");
                None
            }
        };
        let runs = Arc::new(Mutex::new(runs));
        *lock(&config.installed.0) = Some(Installed {
            view,
            host: host.clone(),
            runs: runs.clone(),
            renewal,
            status,
        });
        Self { host, runs }
    }
    /// `demo_app.action(app, path, body)`: the ladder's facts are read first, then the run.
    pub fn demo_action(
        &self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        let mut facts = owner_ports::demo_facts(&self.host);
        let result = lock(&self.runs).demo_action(action, body, &mut facts);
        // `/api/demo/stop` only stops the run (`OwnerRun.stop`, owner_run.py:275-283 @
        // f4e366e9); the run's own teardown stops its workflow. Quit's rule
        // ([`RunClose::begin_until`]) keeps the Stop prompt: outside the serving step a work call
        // can wait on the workflow's own stop flag, which the run's Stop cannot reach, so there
        // the workflow is stopped at once; in the serving step every wait is on the run's own
        // stop event and the workflow is left to the run's teardown, so a step past its Stop
        // check never sees a workflow Stop it did not cause. The decision reads the Stop's own
        // answer, taken under the run lock with the Stop.
        if action == Action::DemoStop
            && let Ok(answer) = &result
            && answer.to_value()["step"] != "serving"
        {
            self.host.stop_work();
        }
        // `_relay_is_serving` (app.py:770-777): the relay's renewal probe reads the run's state. Set it
        // from the action that just settled, not from a view that may not have caught up yet.
        if result.is_ok() {
            let serving = match action {
                Action::DemoStart => true,
                Action::DemoStop => false,
                _ => self.active(),
            };
            self.host.set_serving(serving);
        }
        result
    }
    /// `app.owner_run.active()`.
    pub fn active(&self) -> bool {
        self.host.with(|c| c.run.demo.clone()).active()
    }
    /// One pass of the contributor's lease renewal.
    pub fn renew_contributor(host: &PackagedRun) {
        owner_ports::renew_contributor(host);
    }
}

/// `status.owner_run` (`demo_app.owner_status`, demo_app.py:136-150): `null` for a joined Mac, the
/// run's snapshot once one was started, else the idle checklist; a read refreshes the idle
/// announcement (`ensure_exchange`). Without a composed run the node's document stays.
pub fn status_overlay(status: &mut Value, config: &RunConfig) {
    let Some(installed) = config.installed() else {
        return;
    };
    let target = installed.host.with(|c| c.authority.target);
    status["owner_run"] = if target {
        Value::Null
    } else {
        // `ensure_exchange(app)`: its result is not part of the document (a failure is `None`).
        drop(owner_ports::ensure_exchange(&installed.host));
        installed
            .view
            .status()
            .unwrap_or_else(authority_view::idle_owner_run)
            .to_value()
    };
}

/// Quit's run step, `demo_app.close` (demo_app.py:199-209 @ f4e366e9), then `workflow.stop()`
/// (app.py:1835-1838) and `_stop_local_contributor` (app.py:1842): the run is stopped as Quit
/// begins and, on its own thread, joined (`run.stop(); run.join(60)`, `DemoAppCloseRunJoin`)
/// before what remains of its workflow is stopped, then the contributor's live session ends.
/// A join that outlasts its 60 s is left to the process exit as v0.1.4 leaves it (stderr
/// `owner_run_join_incomplete`), but not before the workflow Stop: that Stop is issued when the
/// run ends or when Quit's deadline arrives, whichever comes first, and the close is done only
/// once it was ([`WorkStop`]). A failed contributor stop is `local_contributor_stop_failed`.
pub struct RunClose {
    config: RunConfig,
    closing: Closing,
}
enum Closing {
    NotStarted,
    Running {
        worker: JoinHandle<Result<(), &'static str>>,
        until: Instant,
        stop: Arc<WorkStop>,
    },
    Done(Result<(), CleanupError>),
}
/// The close thread, `packaged-owner-run-close`.
fn spawn_close(
    body: impl FnOnce() -> Result<(), &'static str> + Send + 'static,
) -> std::io::Result<JoinHandle<Result<(), &'static str>>> {
    #[cfg(test)]
    if CLOSE_SPAWN_REFUSED.with(|refused| refused.replace(false)) {
        return Err(std::io::Error::other("test: close thread refused"));
    }
    std::thread::Builder::new()
        .name("packaged-owner-run-close".into())
        .spawn(body)
}
#[cfg(test)]
thread_local! {
    /// TEST ONLY (REVIEW-laneSRV-r3-astra R3A-02): the next close thread this thread spawns fails
    /// to start, as a refused thread allocation would.
    static CLOSE_SPAWN_REFUSED: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}
/// TEST ONLY: see [`CLOSE_SPAWN_REFUSED`].
#[cfg(test)]
fn refuse_next_close_spawn() {
    CLOSE_SPAWN_REFUSED.with(|refused| refused.set(true));
}
/// `workflow.stop()` (app.py:1835-1838), issued once: by the close thread when the run ended, or
/// at Quit's deadline, whichever comes first. The flag's lock is held across the Stop (taken after
/// every lifecycle lock is released, it guards nothing else), so `issued` is a receipt; the
/// shell's thread only tries it ([`WorkStop::try_issue`]) and never waits for the close thread.
struct WorkStop {
    host: PackagedRun,
    issued: Mutex<bool>,
}
impl WorkStop {
    fn issue(&self) {
        let mut issued = lock(&self.issued);
        if !*issued {
            self.host.stop_work();
            *issued = true;
        }
    }
    /// [`Self::issue`] without waiting: `false` while the close thread is issuing it.
    fn try_issue(&self) -> Result<bool, CleanupError> {
        let mut issued = match self.issued.try_lock() {
            Ok(issued) => issued,
            Err(TryLockError::WouldBlock) => return Ok(false),
            Err(TryLockError::Poisoned(_)) => {
                // The Stop panicked on the close thread: a close step that raised.
                eprintln!("owner run: demo_run_stop_failed");
                return Err(CleanupError::Poll);
            }
        };
        if !*issued {
            self.host.stop_work();
            *issued = true;
        }
        Ok(true)
    }
}
impl RunClose {
    pub fn new(config: RunConfig) -> Self {
        Self {
            config,
            closing: Closing::NotStarted,
        }
    }
    /// Start the close now (the run's lease loop stops at once), joined for its own allowance.
    pub fn begin(&mut self) {
        self.begin_until(None);
    }
    /// [`Self::begin`] with Quit's deadline `until` (the shell's, `packaged::WindowQuit`): the
    /// run's join and the workflow Stop share that one deadline, nothing is added on top. `None`:
    /// the registered allowance from now.
    pub fn begin_until(&mut self, until: Option<Instant>) {
        if !matches!(self.closing, Closing::NotStarted) {
            return;
        }
        let Some(installed) = self.config.installed() else {
            self.closing = Closing::Done(Ok(()));
            return;
        };
        // `publisher.stop()` first (app.py:1816-1817), then the run is stopped (`run.stop()`,
        // demo_app.py:202) and joined for up to 60 s (`run.join(60)`, :203) BEFORE its workflow
        // (`workflow.stop()`, app.py:1835-1838 runs after `demo_app.close` returned), so a step
        // that already passed its Stop check never sees a workflow Stop it did not cause.
        // One exception keeps Quit prompt: outside the serving step a work call can wait on the
        // workflow's own stop flag, which the run's Stop cannot reach (a slices push waits on
        // `OwnerWork`'s stop, api joined_owner/work.rs), so there the workflow is stopped at once
        // and the join follows. In the serving step (its wait and its hold) every wait is on the
        // run's own stop event and every workflow call is a short read or the in-memory commit,
        // so the join ends with the run's own teardown, as in v0.1.4. The decision reads the
        // Stop's own answer, taken under the run lock with the Stop.
        if let Some(status) = &installed.status {
            status.stop();
        }
        let stopped = lock(&installed.runs).stop();
        let serving = stopped.is_some_and(|answer| answer["step"] == "serving");
        let stop = Arc::new(WorkStop {
            host: installed.host.clone(),
            issued: Mutex::new(false),
        });
        if !serving {
            stop.issue();
        }
        if let Some(renewal) = &installed.renewal {
            renewal.stop();
        }
        let until = match until {
            Some(until) => until,
            None => match BudgetId::PyMyceliumA13MacosDemoAppCloseRunJoinL203.span(Entries::ZERO) {
                Ok(span) => Instant::now() + Duration::from_millis(span.millis()),
                Err(_) => {
                    eprintln!("owner run: owner_run_close_budget_invalid");
                    self.closing = Closing::Done(Err(CleanupError::InvalidBudget));
                    return;
                }
            },
        };
        let closer = stop.clone();
        let spawned = spawn_close(move || {
            if serving {
                // `run.join(60)` holding no lifecycle lock, then `workflow.stop()` on the
                // run's end or on the timeout alike (app.py:1835-1838).
                if !installed.view.join(until) {
                    eprintln!("owner run: owner_run_join_incomplete");
                }
                closer.issue();
            }
            lock(&installed.runs).close();
            owner_ports::stop_local_contributor(&installed.host)
                .map_err(|_| "local_contributor_stop_failed")
        });
        self.closing = match spawned {
            Ok(worker) => Closing::Running {
                worker,
                until,
                stop,
            },
            Err(_) => {
                eprintln!("owner run: demo_run_stop_failed");
                // The closure that held the deferred Stop never ran: issue it here, at once and
                // without a join (app.py:1835-1838 runs after a failed close step too).
                stop.issue();
                Closing::Done(Err(CleanupError::Poll))
            }
        };
    }
    /// The close thread ended: the run was joined (its teardown, `stopped` included, ran), the
    /// runs closed and the contributor stopped, whatever they answered. Not before [`Self::begin`].
    pub fn ended(&self) -> bool {
        match &self.closing {
            Closing::NotStarted => false,
            Closing::Running { worker, .. } => worker.is_finished(),
            Closing::Done(_) => true,
        }
    }
    /// Quit's deadline arrived (`Cleanup::expire`): the workflow Stop is issued now unless it was
    /// (`Pending` while the close thread is issuing it). The rest is left to the process exit.
    pub fn expire(&mut self) -> Result<Poll, CleanupError> {
        if matches!(self.closing, Closing::NotStarted) {
            self.begin();
        }
        match &self.closing {
            Closing::Running { stop, .. } => Ok(if stop.try_issue()? {
                Poll::Verified
            } else {
                Poll::Pending
            }),
            Closing::NotStarted | Closing::Done(_) => Ok(Poll::Verified),
        }
    }
    /// `None` while the close runs within its patience, else its result (kept).
    pub fn poll(&mut self) -> Option<Result<(), CleanupError>> {
        match std::mem::replace(&mut self.closing, Closing::Done(Ok(()))) {
            Closing::NotStarted => {
                self.begin();
                self.poll()
            }
            Closing::Running {
                worker,
                until,
                stop,
            } if !worker.is_finished() => {
                // `run.join(60)` returned with the run still alive: v0.1.4 stops the workflow
                // (app.py:1835-1838) and goes on with the rest; done only once that Stop was.
                let issued = if Instant::now() < until {
                    Ok(false)
                } else {
                    stop.try_issue()
                };
                match issued {
                    Ok(false) => {
                        self.closing = Closing::Running {
                            worker,
                            until,
                            stop,
                        };
                        None
                    }
                    Ok(true) => {
                        eprintln!("owner run: owner_run_join_incomplete");
                        Some(Ok(()))
                    }
                    Err(error) => {
                        self.closing = Closing::Done(Err(error));
                        Some(Err(error))
                    }
                }
            }
            Closing::Running { worker, .. } => {
                let result = match worker.join() {
                    Ok(Ok(())) => Ok(()),
                    Ok(Err(code)) => {
                        eprintln!("owner run: {code}");
                        Err(CleanupError::Poll)
                    }
                    Err(_) => {
                        eprintln!("owner run: demo_run_stop_failed");
                        Err(CleanupError::Poll)
                    }
                };
                self.closing = Closing::Done(result);
                Some(result)
            }
            Closing::Done(result) => {
                self.closing = Closing::Done(result);
                Some(result)
            }
        }
    }
}
