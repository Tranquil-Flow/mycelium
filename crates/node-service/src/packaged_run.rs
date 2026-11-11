// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's run in the packaged host (lane PH3b S3), as the shipped Mac app runs it (v0.1.4 @
//! f4e366e9): `#demo-start` / `#demo-stop` (run.js:192-193) -> `DemoApp.action` (app.py:1306) ->
//! `_action` (app.py:1362-1365) -> `demo_app.action` (demo_app.py:109-133) -> `OwnerRun`; the page's
//! `status.owner_run` (`DemoApp.status`, app.py:1244-1283 -> `demo_app.owner_status`,
//! demo_app.py:136-150; run.js:94-138); Quit's `demo_app.close` (demo_app.py:199-209, called first by
//! `DemoApp.close`, app.py:1815-1825) and `_stop_local_contributor` (app.py:1842).
//!
//! The run is `api::owner_driver::DemoRuns` over the shared `OwnerChain`; the model step reads this
//! host's acquisition, the contributor and descriptors steps are `api::owner_ports` (the same
//! composition the headless launcher runs). The observe and compile steps have no producer in this
//! host yet: they end with the closed `owner_run_step_not_ported` refusal naming their seam, never a
//! success. The work phase (`slices_ports`/`work_ports`) stays absent (PH3b S4a/S4c).
use crate::{app_host::HostLoop, packaged_network::Network, service::NodeService};
use api::{
    acquisition::Acquisition,
    actions::Action,
    app_host::Authority,
    authority_view,
    installation_descriptor::Failure,
    owner_chain::{
        self, ModelAdapter, SharedHostChain, acquisition_exception, prepared_source_from,
    },
    owner_driver::{DemoRuns, DemoView, Timing, fresh_identity},
    owner_ports::{self, OwnerPorts, RunFields, RunHost, RunView},
    persistent::RelayState,
    preferences::resource_policy,
    schema::PublicError,
    wire::Json,
};
use budgets::{BudgetId, Entries};
use desktop::lifecycle::CleanupError;
use owner_run::{chain::StepError, steps::model::Reverify};
use serde_json::{Map, Value, json};
use std::{
    path::{Path, PathBuf},
    sync::{Arc, Mutex, MutexGuard, PoisonError},
    thread::JoinHandle,
    time::{Duration, Instant},
};

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(PoisonError::into_inner)
}

/// The run's composition seams and its shared handle: the composition fills it once the host
/// installed the run; the window's status and Quit read it.
#[derive(Clone, Default)]
pub struct RunConfig {
    /// The installed native sidecar the owner's descriptor names (`installed_sidecar`,
    /// native_runtime.py:8-22). `None`: beside the executable, `native/mycelium-iroh-sidecar`.
    pub sidecar: Option<PathBuf>,
    /// Filled by the host once it installed the run.
    pub installed: RunSlot,
}
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
}

/// What the packaged host's model source answers the run (`app.acquisition`, `_resource_check`,
/// `reverify_model_snapshot`), built by `PackagedHost::with_run` over its own state.
pub struct ModelHooks {
    pub acquisition: Box<dyn Fn() -> Option<Acquisition> + Send + Sync>,
    pub resource_check: Box<dyn Fn() -> Option<&'static str> + Send + Sync>,
    pub reverify: Box<dyn Fn() -> Reverify + Send + Sync>,
}

/// The packaged host as `api::owner_ports::RunHost`: its authority, the network beside it (relay,
/// `_left`), the node service's shutdown fence (`DemoApp.closed`) and the run's own fields.
#[derive(Clone)]
pub struct PackagedRun {
    authority: Arc<Mutex<Authority>>,
    network: Arc<Network>,
    node: NodeService,
    model: Arc<ModelHooks>,
    run: Arc<Mutex<RunFields>>,
    lifecycle: Arc<Mutex<()>>,
    sidecar: Option<PathBuf>,
}
impl PackagedRun {
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
            sidecar,
        }
    }
}
impl RunHost for PackagedRun {
    type Managed = api::app_host::Unmanaged;
    /// The network and the fence are read first, then the authority and the run's fields are held
    /// together (order: authority -> run fields, everywhere).
    fn with<R>(&self, f: impl FnOnce(&mut RunView<'_, Self::Managed>) -> R) -> R {
        let closed = self.node.closed().unwrap_or(true);
        let left = self.network.left();
        let relay_absent = matches!(self.network.state(), RelayState::Absent);
        let mut authority = lock(&self.authority);
        let mut run = lock(&self.run);
        f(&mut RunView {
            authority: &mut authority,
            run: &mut run,
            closed,
            left,
            relay_absent,
        })
    }
    fn bootstrap_lifecycle(&self) -> Arc<Mutex<()>> {
        self.lifecycle.clone()
    }
    fn membership_current(&self) -> bool {
        crate::packaged_network::membership_state(&self.authority).as_deref() == Some("current")
    }
    /// This host never starts the contributor's native bootstrap (no observation producer is
    /// composed here), so there is no bootstrap stage whose cleanup a Start would have to wait for.
    fn admit_contributor(&self, _root: &Path) -> Result<(), PublicError> {
        Ok(())
    }
    /// Nothing native is owned by the contributor here (see [`Self::admit_contributor`]).
    fn close_contributor(&self) -> Result<(), PublicError> {
        Ok(())
    }
    fn resource_check(&self) -> Option<&'static str> {
        (self.model.resource_check)()
    }
    fn acquisition(&self) -> Option<Acquisition> {
        (self.model.acquisition)()
    }
    /// The member's staged hello needs the observation producer this host does not compose yet.
    fn bootstrap(&self, _: &str, _: &str, _: &str, _: &str) -> Result<(), Failure> {
        Err(Failure::new("RuntimeError", "managed_owner_unavailable"))
    }
    fn sidecar(&self) -> Result<PathBuf, Failure> {
        owner_ports::installed_sidecar(self.sidecar.clone())
    }
}

/// The model step's facts (`demo_chain.py:442-507` reads them from `app`).
#[derive(Clone)]
struct ModelPorts(PackagedRun);
impl owner_chain::ChainPorts for ModelPorts {
    fn owner_and_running(&self) -> bool {
        self.0.with(|c| !c.authority.target && c.authority.running)
    }
    fn swarm_created(&self) -> bool {
        self.0.with(|c| c.authority.owner_authority().is_some())
    }
    fn acquisition_state(&self) -> String {
        self.0
            .acquisition()
            .map_or_else(String::new, |a| a.status().state)
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

/// The chain's ports for one run: model, contributor and descriptors; observe and compile absent.
struct PackagedChainPorts {
    model: ModelAdapter<ModelPorts>,
    contributor: OwnerPorts<PackagedRun>,
    reports: OwnerPorts<PackagedRun>,
}
impl owner_run::owner_chain::ChainPorts for PackagedChainPorts {
    fn ports(&mut self) -> owner_run::owner_chain::Ports<'_> {
        owner_run::owner_chain::Ports {
            model: &mut self.model,
            contributor: Some(&mut self.contributor),
            reports: Some(&mut self.reports),
            observe: None,
            compile: None,
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
        let runs = DemoRuns::new(
            Box::new(move || {
                let ports = OwnerPorts::new(chain_host.clone());
                Box::new(SharedHostChain::new(PackagedChainPorts {
                    model: ModelAdapter::new(ModelPorts(chain_host.clone())),
                    contributor: ports.clone(),
                    reports: ports,
                }))
            }),
            Box::new(|| {
                fresh_identity(api::app_host::now()).unwrap_or_else(|_| {
                    owner_run::machine::new_run_identity([0; 19], "00000000T000000Z")
                })
            }),
            Timing::registered(),
        );
        let view = runs.view();
        host.with(|c| c.run.demo = view.clone());
        let runs = Arc::new(Mutex::new(runs));
        *lock(&config.installed.0) = Some(Installed {
            view,
            host: host.clone(),
            runs: runs.clone(),
            renewal,
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
        lock(&self.runs).demo_action(action, body, &mut facts)
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

/// Quit's run step, `demo_app.close` (demo_app.py:199-209 @ f4e366e9) then `_stop_local_contributor`
/// (app.py:1842): on its own thread as Quit begins, the run is stopped and joined
/// (`run.stop(); run.join(60)`, `DemoAppCloseRunJoin`), then the contributor's live session ends.
/// A join that outlasts its 60 s is left to the process exit as v0.1.4 leaves it (stderr
/// `owner_run_join_incomplete`); a failed contributor stop is `local_contributor_stop_failed`.
pub struct RunClose {
    config: RunConfig,
    closing: Closing,
}
enum Closing {
    NotStarted,
    Running {
        worker: JoinHandle<Result<(), &'static str>>,
        until: Instant,
    },
    Done(Result<(), CleanupError>),
}
impl RunClose {
    pub fn new(config: RunConfig) -> Self {
        Self {
            config,
            closing: Closing::NotStarted,
        }
    }
    /// Start the close now (the run's lease loop stops at once).
    pub fn begin(&mut self) {
        if !matches!(self.closing, Closing::NotStarted) {
            return;
        }
        let Some(installed) = self.config.installed() else {
            self.closing = Closing::Done(Ok(()));
            return;
        };
        if let Some(renewal) = &installed.renewal {
            renewal.stop();
        }
        let patience = match BudgetId::PyMyceliumA13MacosDemoAppCloseRunJoinL203.span(Entries::ZERO)
        {
            Ok(span) => Duration::from_millis(span.millis()),
            Err(_) => {
                eprintln!("owner run: owner_run_close_budget_invalid");
                self.closing = Closing::Done(Err(CleanupError::InvalidBudget));
                return;
            }
        };
        let until = Instant::now() + patience;
        let spawned = std::thread::Builder::new()
            .name("packaged-owner-run-close".into())
            .spawn(move || {
                lock(&installed.runs).close();
                owner_ports::stop_local_contributor(&installed.host)
                    .map_err(|_| "local_contributor_stop_failed")
            });
        self.closing = match spawned {
            Ok(worker) => Closing::Running { worker, until },
            Err(_) => {
                eprintln!("owner run: demo_run_stop_failed");
                Closing::Done(Err(CleanupError::Poll))
            }
        };
    }
    /// `None` while the close runs within its patience, else its result (kept).
    pub fn poll(&mut self) -> Option<Result<(), CleanupError>> {
        match std::mem::replace(&mut self.closing, Closing::Done(Ok(()))) {
            Closing::NotStarted => {
                self.begin();
                self.poll()
            }
            Closing::Running { worker, until } if !worker.is_finished() => {
                if Instant::now() < until {
                    self.closing = Closing::Running { worker, until };
                    return None;
                }
                // `run.join(60)` returned with the run still alive: v0.1.4 goes on with the rest.
                eprintln!("owner run: owner_run_join_incomplete");
                Some(Ok(()))
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
