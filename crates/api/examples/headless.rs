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
//! State lives under `$HOME/Library/Application Support/Mycelium A13 macOS Demo`
//! (`--state-root` overrides), exactly where the L3 harness expects it.
//! The owner-authority seams (create, authority start/stop, pairing request/approve/accept, the
//! owner's pairing windows) are `api::app_host`, the same code the packaged executable runs.
use api::{
    acquisition::{self, Acquisition, HttpTransport},
    actions::{Action, Backend, Control},
    app_host::{
        self, Authority, HostView, IDENTITY_FILE, NETWORK_FILE, SEED_IDENTITY_FILE,
        build_pair_request, cap, hex, identity, identity_in, now, random,
    },
    authority_view,
    desktop::{Desktop, Session},
    handoff::{self, Coordinator, Entropy},
    http::serve_connection,
    installation_descriptor::{self, Failure, Member as DescriptorMember, Paths, Source},
    jobj,
    joined_member::{self, NoSeedServer, SeedHop, observe_device, planning_caps},
    leave::{
        Clock, LeaveWorker, Limits, MemberLeave, Steps, SystemClock, resume_leave, status_view,
    },
    local_agent::{AgentHost, LocalAgent},
    native_session::{MonotonicClock, NativeLauncher},
    owner_chain::{
        ChainPorts, ModelAdapter, SharedHostChain, acquisition_exception, prepared_source_from,
        unwired,
    },
    owner_driver::{DemoRuns, DemoView, Timing, fresh_identity},
    pairing::SignedBackend,
    persistent::{Host, PersistentBackend, RelayState},
    preferences::{
        fast_mode, private_state, private_write, resource_policy, run_model, standing_consent,
    },
    relay::{EndpointHost, Hooks, LocalHandler, OwnerRelay, Probe, Proxy, SeedBackend},
    resources::{self, Observation, Observer, ProbeObserver},
    run_exchange::{Announce, RunExchange},
    schema::{PublicError, Raised, STATUS_FIELDS, Snapshot},
    seed::SeedOptions,
    slice_fetch::{SliceTransport, SnapshotRangeTransport},
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
use owner_run::{
    action::DemoFacts,
    chain::StepError,
    machine::OwnerRunError,
    steps::{
        contributor::ContributorPort,
        exchange::{Announcement, ClockSkew, MemberRow, ReportsPort},
        model::Reverify,
    },
};
use protocol::identity::Identity;
use serde_json::{Map, Value, json};
use std::{
    collections::BTreeMap,
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
    /// `DemoApp.owner_run` as the status and the fast-mode gate read it (never blocks on the run).
    demo: DemoView,
    /// `DemoApp.local_contributor`: the owner's own member (`local_contributor.py`), renewed by the loop.
    contributor: Option<ContributorState>,
    // Serializes bootstrap side effects and cleanup; never held with Core on entry.
    bootstrap_lifecycle: Arc<Mutex<()>>,
    bootstrap_incarnation: Arc<()>,
    bootstrap: Option<observation::target::Worker>,
    bootstrap_failure: Option<api::bootstrap_process::CleanupFailure>,
    /// `DemoApp.run_exchange`, bound to the seed runtime it was made for (`exchange.mailbox is mailbox`).
    exchange: Option<Arc<RunExchange>>,
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
type SeedRuntime = app_host::SeedRuntime<observation::Managed>;
impl app_host::ManagedSlot for observation::Managed {
    fn close(&self) -> Result<(), &'static str> {
        observation::Managed::close(self).map_err(|error| error.0)
    }
}
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
/// The live side of the owner's local contributor: its node and membership session.
struct ContributorState {
    node: String,
    signer: Arc<Identity>,
    session: Option<api::member::Session>,
    /// The session's lease while the renewal loop has the session out on the wire.
    lease: Option<f64>,
}
type Shared = Arc<Mutex<Core>>;

/// `self.authority.status()` (`api::joined_member`, shared with the packaged host).
fn authority_status(core: &Core) -> Json {
    joined_member::authority_status(&core.authority)
}
/// The same pass for a joined TARGET (`api::app_host::renew_member`, shared with the packaged host).
fn renew_target(core: &Shared) {
    app_host::renew_member(&CoreCell(core.clone()));
}
// ---- the owner's own contributor (local_contributor.py, app.py:882-909 @ f4e366e9)

/// `<root>/local-contributor`: the contributor's own state directory (identity, membership, consent).
fn contributor_dir(root: &Path) -> PathBuf {
    root.join("local-contributor")
}
fn session_lease(session: &Option<api::member::Session>) -> Option<f64> {
    session
        .as_ref()
        .and_then(api::member::Session::lease_expires_at)
}

/// `create_local_contributor(owner_app)` behind `_start_local_contributor`: the consent gate, then the member
/// joins its own owner's seed over the pinned local TLS - a retained membership resumes
/// (`LocalOwnerMember.start`), a first one is paired (`pair_request` -> `approve_pairing(ttl 120)` ->
/// `accept_pairing`). v0.1.4 does not renew at once, so the member stays `NEW` until the renewal loop's first
/// pass (executed: `status_after_start` lifecycle `NEW`).
fn start_local_contributor(core: &Shared) -> Result<Json, PublicError> {
    let lifecycle = core
        .lock()
        .map_err(|_| failure())?
        .bootstrap_lifecycle
        .clone();
    let _lifecycle = lifecycle.lock().map_err(|_| failure())?;
    let (root, runtime, record, already, target, running, has_policy) = {
        let c = core.lock().map_err(|_| failure())?;
        (
            c.root.clone(),
            c.seed.clone(),
            c.swarm.clone(),
            c.contributor.is_some(),
            c.target,
            c.running,
            resource_policy::read(&c.root).ok().flatten(),
        )
    };
    if already {
        return Err(PublicError::new(400, "local_contributor_already_running"));
    }
    observation::target::admit_cleanup_free(&root).map_err(|e| PublicError::new(409, e.0))?;
    let current = HeadlessHost {
        core: core.clone(),
        runs: None,
    }
    .membership_state()
    .as_deref()
        == Some("current");
    // `local_contributor_owner_consent_required` / `local_owner_unavailable` are ValueErrors (409).
    let (Some(policy), Some(runtime), Some(record)) = (has_policy, runtime, record) else {
        return Err(failure());
    };
    if target || !running || !current {
        return Err(failure());
    }
    let dir = contributor_dir(&root);
    fs::create_dir_all(&dir).map_err(|_| failure())?;
    fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700))
        .map_err(|_| failure())?;
    let swarm_id = record["swarm_id"].as_str().ok_or_else(failure)?.to_owned();
    let (signer, _, node) = identity(&dir)?;
    let options = runtime.options();
    let random_hex = |n: usize| random(n).map(|b| hex(&b)).unwrap_or_default();
    let env = api::peer::JoinEnv {
        signer: &signer,
        clock: &now,
        random_hex: &random_hex,
        seed: &options,
    };
    let session = if api::peer::load_membership_state(&dir)?.is_some() {
        match api::peer::start(&dir, &signer, true, &env) {
            Ok(started) => started.session,
            Err(failed) => return Err(failed.error),
        }
    } else {
        let (seed_signer, _, _) = identity_in(&root, SEED_IDENTITY_FILE)?;
        let coordinator = Coordinator {
            signer: seed_signer,
            swarm_id,
            seed_url: runtime.seed_url.clone(),
        };
        let storage = policy.get("storage_bytes").cloned().unwrap_or(Value::Null);
        let quota = json!({"storage_bytes": storage, "network_bytes_per_day": storage, "background_allowed": false});
        let request = build_pair_request(&dir, &dir)?;
        let handoff = handoff::approve_pairing(
            &root,
            true,
            Some(&coordinator),
            &request.to_value(),
            &json!(120),
            &quota,
            now(),
            &Entropy::os,
        )?;
        match api::peer::accept_handoff(&dir, true, &handoff.to_value(), &env) {
            Ok(accepted) => accepted.session,
            Err(error) => return Err(error),
        }
    };
    if session.is_none() {
        return Err(failure());
    }
    let lease = session_lease(&session);
    {
        let mut c = core.lock().map_err(|_| failure())?;
        c.contributor = Some(ContributorState {
            node: node.clone(),
            signer: Arc::new(signer),
            session,
            lease,
        });
    }
    Ok(jobj! {
        "created" => true, "physical_hosts" => 1u64, "node_id" => node,
        "membership_state" => "current", "route_ready" => false,
    })
}

/// One pass of the contributor's half of `DemoApp._renew_loop` (`contributor.renew()`).
fn renew_contributor(core: &Shared) {
    let (root, runtime, session) = {
        let Ok(mut c) = core.lock() else { return };
        if c.closed || !c.running || c.left || c.target {
            return;
        }
        let (Some(runtime), Some(state)) = (c.seed.clone(), c.contributor.as_mut()) else {
            return;
        };
        let Some(session) = state.session.take() else {
            return;
        };
        (c.root.clone(), runtime, session)
    };
    let mut session = session;
    let dir = contributor_dir(&root);
    if let Ok((signer, _, _)) = identity(&dir) {
        let options = runtime.options();
        let random_hex = |n: usize| random(n).map(|b| hex(&b)).unwrap_or_default();
        let env = api::peer::JoinEnv {
            signer: &signer,
            clock: &now,
            random_hex: &random_hex,
            seed: &options,
        };
        if let Err(error) = api::peer::renew(&dir, true, Some(&mut session), &env) {
            eprintln!("contributor renew: {}", error.code);
        }
    }
    if let Ok(mut c) = core.lock()
        && let Some(state) = c.contributor.as_mut()
        && state.session.is_none()
    {
        state.lease = session.lease_expires_at();
        state.session = Some(session);
    }
}

/// `app._stop_local_contributor()`: the membership stays retained, the live session ends.
fn stop_local_contributor(core: &Shared) -> Result<(), PublicError> {
    // target::close puts refused cleanup owners back. Retain their contributor
    // too: reporting stopped or permitting a replacement would erase the debt.
    let lifecycle = core
        .lock()
        .map_err(|_| failure())?
        .bootstrap_lifecycle
        .clone();
    let _lifecycle = lifecycle.lock().map_err(|_| failure())?;
    observation::target::close_locked(core).map_err(|e| PublicError::new(409, e.0))?;
    core.lock().map_err(|_| failure())?.contributor = None;
    Ok(())
}

// ---- the run exchange (demo_app.ensure_exchange, app.py:1245-1250)

/// `ensure_exchange(app)`: an owner with a running seed has one exchange per seed runtime (v0.1.4 keys it on the
/// control mailbox), announcing the run model while idle. The idle refresh is prepared outside any lock and
/// committed under the run's own lock so it can never overwrite a run that started in between.
fn ensure_exchange(core: &Shared) -> Option<Arc<RunExchange>> {
    let (exchange, wanted, demo) = {
        let mut c = core.lock().ok()?;
        if c.target || c.swarm.is_none() || !c.running {
            return None;
        }
        // The seed runtime owns the exact exchange mounted on its server. Never key this on an
        // allocation address: a closed runtime's address can be reused by its replacement.
        let exchange = c.seed.as_ref()?.exchange.clone();
        c.exchange = Some(exchange.clone());
        let document =
            run_model::document_json(&c.root, std::env::var(run_model::ENV).ok().as_deref(), None);
        (exchange, document.ok(), c.demo.clone())
    };
    let phase = exchange.announcement(None).get("phase").cloned();
    let idle = matches!(&phase, Some(Json::Str(p)) if p == "idle" || p == "stopped");
    if idle
        && !demo.active()
        && let Some(document) = wanted
    {
        let model = json!({
            "choice": document.get("selected").map_or(Value::Null, Json::to_value),
            "model_id": document.get("model_id").map_or(Value::Null, Json::to_value),
            "resolved_commit": document.get("resolved_commit").map_or(Value::Null, Json::to_value),
        });
        if let Ok(prepared) = exchange.prepare_announcement(Announce {
            phase: "idle".into(),
            model: Some(model),
            ..Announce::default()
        }) {
            demo.while_inactive(|| exchange.commit_announcement(&prepared, true));
        }
    }
    Some(exchange)
}

#[allow(unsafe_code)] // geteuid(2) has no preconditions
fn effective_uid() -> u32 {
    unsafe { libc::geteuid() }
}
/// `installed_sidecar()` (`native_runtime.py:8-22`): the native sidecar beside the executable, a regular file
/// this user owns, executable and not writable by others. `MYCELIUM_L10_TEST_SIDECAR` is the test launcher's seam.
fn installed_sidecar() -> Result<PathBuf, Failure> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let invalid = || Failure::new("ValueError", "native_sidecar_installation_invalid");
    let candidate = match std::env::var("MYCELIUM_L10_TEST_SIDECAR") {
        Ok(path) => PathBuf::from(path),
        Err(_) => std::env::current_exe()
            .map_err(|_| invalid())?
            .parent()
            .ok_or_else(invalid)?
            .join("native")
            .join("mycelium-iroh-sidecar"),
    };
    let path = fs::canonicalize(candidate).map_err(|_| invalid())?;
    let meta = fs::symlink_metadata(&path).map_err(|_| invalid())?;
    let mode = meta.permissions().mode();
    if !meta.is_file() || meta.uid() != effective_uid() || mode & 0o100 == 0 || mode & 0o022 != 0 {
        return Err(invalid());
    }
    Ok(path)
}

/// The local agent's collaborators over the process core.
struct AgentView<'a> {
    core: &'a Shared,
    exchange: Arc<RunExchange>,
    node: String,
    /// The snapshot `prepared_model` just verified, for the `describe` of the same descriptor.
    prepared: Option<(PathBuf, acquisition::PreparedFacts)>,
}
impl AgentView<'_> {
    fn acquisition(&self) -> Result<Acquisition, Failure> {
        self.core
            .lock()
            .ok()
            .and_then(|c| c.acquisition.clone())
            .ok_or_else(|| Failure::new("AttributeError", "demo_step_failed"))
    }
    /// `_prepare_slice_root(run_id)`: this Mac's empty per-run slice root inside its artifact cache.
    fn prepare_slice_root(&self, run_id: &str) -> Result<String, Failure> {
        use std::os::unix::fs::DirBuilderExt;
        let base = self.acquisition()?.state_root().join("slices");
        let invalid = || Failure::new("ValueError", "slice_destination_invalid");
        fs::DirBuilder::new()
            .recursive(true)
            .mode(0o700)
            .create(&base)
            .map_err(|_| invalid())?;
        for entry in fs::read_dir(&base).map_err(|_| invalid())?.flatten() {
            let kind = entry.file_type();
            if entry.file_name() != run_id && kind.is_ok_and(|t| t.is_dir() && !t.is_symlink()) {
                fs::remove_dir_all(entry.path()).unwrap_or_default();
            }
        }
        let root = base.join(run_id);
        if !root.exists() {
            fs::DirBuilder::new()
                .mode(0o700)
                .create(&root)
                .map_err(|_| invalid())?;
        }
        let meta = fs::symlink_metadata(&root).map_err(|_| invalid())?;
        if meta.file_type().is_symlink()
            || fs::canonicalize(&root).ok().as_deref() != Some(root.as_path())
        {
            return Err(invalid());
        }
        Ok(root.to_string_lossy().into_owned())
    }
}
impl AgentHost for AgentView<'_> {
    fn bootstrap(&mut self, run: &str, deployment: &str, offer: &str) -> Result<(), Failure> {
        observation::bootstrap(self.core, &self.node, run, deployment, offer)
            .map_err(|error| Failure::new("RuntimeError", error.0))
    }
    fn announcement(&mut self) -> Result<Option<Value>, api::local_agent::ExchangeUnavailable> {
        Ok(Some(
            self.exchange.announcement(Some(&self.node)).to_value(),
        ))
    }
    fn member(&mut self) -> Option<String> {
        self.core
            .lock()
            .ok()
            .and_then(|c| c.contributor.as_ref().map(|s| s.node.clone()))
    }
    fn run_active(&mut self) -> bool {
        let demo = self.core.lock().ok().map(|c| c.demo.clone());
        demo.is_some_and(|demo| demo.active())
    }
    fn resource_check(&mut self) -> Option<String> {
        resource_check(self.core).map(str::to_owned)
    }
    fn prepared_model(&mut self) -> Result<(String, String), Failure> {
        let acquisition = self.acquisition()?;
        let prepared = acquisition
            .prepared_source()
            .map_err(|e| Failure::new("AcquisitionError", e.code()))?;
        self.prepared = Some(prepared);
        let model = acquisition.model();
        Ok((model.model_id.to_owned(), model.resolved_commit.to_owned()))
    }
    fn describe(
        &mut self,
        run_id: &str,
        deployment_id: &str,
        slice_manifest_digest: Option<&str>,
    ) -> Result<Value, Failure> {
        let (root, swarm, lease) = {
            let c = self
                .core
                .lock()
                .map_err(|_| Failure::new("RuntimeError", "demo_step_failed"))?;
            let swarm = c
                .swarm
                .as_ref()
                .and_then(|s| s["swarm_id"].as_str().map(str::to_owned));
            (
                c.root.clone(),
                swarm,
                c.contributor.as_ref().and_then(|s| s.lease),
            )
        };
        let dir = contributor_dir(&root);
        let state = api::peer::load_membership_state(&dir)
            .ok()
            .flatten()
            .ok_or_else(|| Failure::new("RemoteMacMemberError", "peer_member_not_current"))?;
        // `staging_control_snapshot`: a stopped or expired member cannot describe itself.
        if lease.is_none_or(|lease| now() >= lease) {
            return Err(Failure::new(
                "RemoteMacMemberError",
                "peer_member_not_current",
            ));
        }
        let (signer, endpoint, node) =
            identity(&dir).map_err(|_| Failure::new("ValueError", "endpoint_identity_invalid"))?;
        let member = DescriptorMember {
            node_id: node,
            endpoint_id: endpoint,
            verification_key_digest: signer.verification_key_digest(),
            generation: state
                .get("membership_generation")
                .and_then(Value::as_u64)
                .unwrap_or(0),
            incarnation: state
                .get("incarnation")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_owned(),
        };
        let source = match slice_manifest_digest {
            Some(digest) => Source::Slice {
                source_root: self.prepare_slice_root(run_id)?,
                manifest_digest: digest.to_owned(),
            },
            None => {
                let (root, facts) = match self.prepared.take() {
                    Some(prepared) => prepared,
                    None => self
                        .acquisition()?
                        .prepared_source()
                        .map_err(|e| Failure::new("AcquisitionError", e.code()))?,
                };
                Source::Whole {
                    source_root: root.to_string_lossy().into_owned(),
                    manifest_digest: facts.manifest_digest,
                }
            }
        };
        let paths = Paths {
            state_root: dir.to_string_lossy().into_owned(),
            endpoint_secret_file: dir.join(IDENTITY_FILE).to_string_lossy().into_owned(),
            python_executable: std::env::current_exe()
                .ok()
                .and_then(|p| fs::canonicalize(p).ok())
                .map_or_else(String::new, |p| p.to_string_lossy().into_owned()),
            sidecar_binary: installed_sidecar()?.to_string_lossy().into_owned(),
        };
        installation_descriptor::installation_descriptor(
            &signer,
            &member,
            swarm.as_deref().unwrap_or_default(),
            run_id,
            deployment_id,
            true,
            &source,
            &paths,
            now(),
        )
    }
    /// `OwnerRunConsent.member_caps`: `planning_caps(root)` (standing caps intersected with the policy).
    fn member_caps(&mut self) -> Result<Value, Failure> {
        let root = self
            .core
            .lock()
            .map_err(|_| Failure::new("RuntimeError", "demo_step_failed"))?
            .root
            .clone();
        Ok(planning_caps(&root).unwrap_or_else(|| json!({})))
    }
    /// `OwnerRunConsent.storage_cap`: `int(read_resource_policy(root)['storage_bytes'])`.
    fn storage_cap(&mut self) -> Result<u64, Failure> {
        let root = self
            .core
            .lock()
            .map_err(|_| Failure::new("RuntimeError", "demo_step_failed"))?
            .root
            .clone();
        resource_policy::read(&root)
            .ok()
            .flatten()
            .and_then(|p| p.get("storage_bytes").and_then(Value::as_u64))
            .ok_or_else(|| Failure::new("KeyError", "demo_step_failed"))
    }
    /// `slice_root(run_id)`: the per-run root `describe` prepared in this Mac's artifact cache.
    fn slice_root(&mut self, run_id: &str) -> Result<PathBuf, Failure> {
        Ok(self.acquisition()?.state_root().join("slices").join(run_id))
    }
    /// `SnapshotRangeTransport(source.source_root, ...)` over this Mac's own verified snapshot. The
    /// compiled-bundle clone shortcut is not wired (the Rust compiler keeps no transfer bundle there),
    /// so the owner's slice is read by range from the snapshot and verified like any other.
    fn slice_transport(&mut self, _announcement: &Value) -> Option<Box<dyn SliceTransport>> {
        let acquisition = self.acquisition().ok()?;
        let (root, _) = acquisition.prepared_source().ok()?;
        let model = acquisition.model();
        Some(Box::new(SnapshotRangeTransport::new(
            root,
            model.model_id,
            model.resolved_commit,
            None,
        )))
    }
    fn sink(&mut self, report: &Value) -> Result<(), api::local_agent::ExchangeUnavailable> {
        self.exchange
            .submit_local(&self.node, report)
            .map_err(|_| api::local_agent::ExchangeUnavailable)
    }
    fn clock(&mut self) -> f64 {
        now()
    }
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
            || core.contributor.is_some()
            || core.compiled_workflow.is_some()
        {
            return Ok(false);
        }
        // Missing memory after restart is insufficient. Require absent durable
        // worker/stage evidence, rejecting unreadable paths and symlink residue.
        let contributor = core.root.join("local-contributor");
        let native = contributor.join("native-bootstrap");
        for path in [&contributor, &native] {
            let absent_or_directory = match fs::symlink_metadata(path) {
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => true,
                Err(_) => return Err("member_compute_ownership_unavailable".into()),
                Ok(metadata) => metadata.is_dir(),
            };
            if !absent_or_directory {
                return Ok(false);
            }
        }
        let native_empty = match fs::read_dir(&native) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => true,
            Err(_) => return Err("member_compute_ownership_unavailable".into()),
            Ok(mut entries) => entries.next().is_none(),
        };
        if !native_empty {
            return Ok(false);
        }
        let previous = private_state::load(&core.root.join("leave-state.json"), cap())
            .map_err(|e| e.0.to_owned())?;
        // A removed stage is not enough to retire already-recorded cleanup debt.
        Ok(previous.is_none_or(|record| {
            record.get("steps").is_some_and(Value::is_array)
                && !api::leave::cleanup_pending(&record)
        }))
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
        self.with(|c| c.demo.clone()).active()
    }
    fn acquisition_active(&self) -> bool {
        self.with(|c| c.acquisition_active)
    }
    fn owner_run_active(&self) -> bool {
        self.with(|c| c.demo.clone()).active()
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
        let demo = self.with(|c| c.demo.clone()).status();
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
    let probe: Probe = Arc::new(|| Ok(false));
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

/// What `demo_app.action` asks the app (`demo_app.py:109-133`), read once under the core lock.
struct HostFacts {
    owner: bool,
    exchange: bool,
    joined_without_relay: u64,
}
impl DemoFacts for HostFacts {
    fn owner(&self) -> bool {
        self.owner
    }
    fn ensure_exchange(&mut self) -> bool {
        self.exchange
    }
    fn members_joined_without_relay(&self) -> Option<u64> {
        Some(self.joined_without_relay)
    }
}
/// `app.installation_role`, `ensure_exchange(app) is not None` (an owner authority with its control mailbox:
/// the swarm exists and its seed coordinator runs) and `members_joined_without_relay()` (members other than
/// the owner while no relay object exists; `app.py:836-842`).
fn demo_facts(core: &Shared) -> HostFacts {
    ensure_exchange(core);
    let c = match core.lock() {
        Ok(c) => c,
        Err(poisoned) => poisoned.into_inner(),
    };
    let owner = !c.target;
    let exchange = owner && c.swarm.is_some() && c.running && c.seed.is_some();
    let joined_without_relay = match (&c.seed, c.relay.state()) {
        (Some(seed), RelayState::Absent) if owner && c.swarm.is_some() => seed
            .coordinator
            .members(None)
            .map_or(0, |m| m.len().saturating_sub(1) as u64),
        _ => 0,
    };
    HostFacts {
        owner,
        exchange,
        joined_without_relay,
    }
}

/// The owner run's view of this process (`AppDemoChain` reads these from `app`): see `api::owner_chain`.
#[derive(Clone)]
struct HeadlessPorts {
    core: Shared,
    /// The in-process agent that answers the owner's announcements as this Mac's member (`chain.agent`).
    agent: Arc<Mutex<Option<LocalAgent>>>,
}
impl HeadlessPorts {
    fn new(core: Shared) -> Self {
        Self {
            core,
            agent: Arc::new(Mutex::new(None)),
        }
    }
    fn exchange(&self) -> Result<Arc<RunExchange>, StepError> {
        ensure_exchange(&self.core)
            .ok_or_else(|| OwnerRunError::new("owner_exchange_unavailable").into())
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
    contributor: HeadlessPorts,
    reports: HeadlessPorts,
    observation: compiler::Observation,
    compiler: compiler::SharedCompiler,
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
        let ports = HeadlessPorts::new(core);
        Self {
            model: ModelAdapter::new(ports.clone()),
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

/// A failure inside a step keeps only its class (`owner_run.py:420-422`): a 400 is a `DemoError`.
fn step_exception(error: PublicError) -> StepError {
    let class = if error.status == 400 {
        "DemoError"
    } else {
        error.raised.identity().0
    };
    StepError::Exception {
        class: class.to_owned(),
    }
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
    fn contributor(&mut self) -> Option<&mut dyn ContributorPort> {
        Some(self)
    }
    fn reports(&mut self) -> Option<&mut dyn ReportsPort> {
        Some(self)
    }
}
impl ContributorPort for HeadlessPorts {
    /// `with app.guard: if app.local_contributor is None: app._start_local_contributor()`.
    fn ensure_local_contributor(&mut self) -> Result<String, StepError> {
        if let Some(node) = self.with(|c| c.contributor.as_ref().map(|s| s.node.clone())) {
            return Ok(node);
        }
        start_local_contributor(&self.core).map_err(step_exception)?;
        self.with(|c| c.contributor.as_ref().map(|s| s.node.clone()))
            .ok_or_else(|| StepError::Exception {
                class: "AttributeError".to_owned(),
            })
    }
    /// `getattr(app, 'run_exchange', None) is not None`.
    fn exchange_available(&self) -> bool {
        self.with(|c| c.exchange.is_some())
    }
    fn attach_agent(&mut self, _local: &str) -> Result<(), StepError> {
        *self
            .agent
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(LocalAgent::new());
        Ok(())
    }
}
impl ReportsPort for HeadlessPorts {
    fn forget_reports(&mut self) {
        if let Ok(exchange) = self.exchange() {
            exchange.forget_reports();
        }
    }
    fn announce(&mut self, announcement: &Announcement) -> Result<(), StepError> {
        let exchange = self.exchange()?;
        let per_node: Map<String, Value> =
            announcement.per_node.iter().flatten().cloned().collect();
        exchange
            .announce(Announce {
                phase: announcement.phase.clone(),
                run_id: Some(announcement.run_id.clone()),
                deployment_id: Some(announcement.deployment_id.clone()),
                model: Some(json!({
                    "choice": api::model_catalog::find(&announcement.model_id, &announcement.resolved_commit)
                        .and_then(api::model_catalog::choice_for_model),
                    "model_id": announcement.model_id,
                    "resolved_commit": announcement.resolved_commit,
                })),
                slice_mode: announcement.slice_mode,
                manifest_digest: Some(announcement.manifest_digest.clone()),
                work_limits: None,
                per_node,
            })
            .map(|_| ())
            .map_err(|_| StepError::Exception {
                class: "ValueError".to_owned(),
            })
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        let reports = self.exchange()?.reports();
        Ok(reports
            .as_object()
            .map(|m| m.iter().map(|(k, v)| (k.clone(), v.clone())).collect())
            .unwrap_or_default())
    }
    fn members(&mut self) -> Vec<MemberRow> {
        let Some(seed) = self.with(|c| c.seed.clone()) else {
            return Vec::new();
        };
        seed.coordinator
            .members(None)
            .unwrap_or_default()
            .iter()
            .map(|m| MemberRow {
                node_id: m["node_id"].as_str().unwrap_or_default().to_owned(),
                lifecycle_state: m["lifecycle_state"].as_str().map(str::to_owned),
                lease_expires_at: m["lease_expires_at"].as_f64(),
            })
            .collect()
    }
    fn own_node_id(&mut self) -> Option<String> {
        self.with(|c| c.owner_session.as_ref().map(|s| s.node_id().to_owned()))
    }
    fn member_identity(&mut self, node: &str) -> Value {
        let member = self
            .with(|c| c.seed.clone())
            .and_then(|seed| seed.coordinator.member(node).ok())
            .unwrap_or(Value::Null);
        json!([
            member["generation"],
            member["incarnation"],
            member["verification_key_digest"],
            member["endpoint_id"]
        ])
    }
    /// Only authenticated signed exchange observations feed these source-median diagnostics.
    fn clock_skew(&mut self, nodes: &[String]) -> Result<Option<ClockSkew>, StepError> {
        let offsets = self.with(|c| c.seed.as_ref().map(|seed| seed.clock_offsets.clone()));
        Ok(offsets.and_then(|offsets| {
            let peers = offsets.exceeded(Some(nodes));
            (!peers.is_empty()).then(|| ClockSkew {
                exceeded: peers,
                tolerance: offsets.tolerance(),
            })
        }))
    }
    fn tick_local(&mut self) {
        let agent = self
            .agent
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        let (Some(mut agent), Ok(exchange)) = (agent, self.exchange()) else {
            return;
        };
        if let Some(node) = self.with(|c| c.contributor.as_ref().map(|s| s.node.clone())) {
            let mut view = AgentView {
                core: &self.core,
                exchange,
                node,
                prepared: None,
            };
            agent.tick(&mut view);
        }
        *self
            .agent
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(agent);
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

fn status_json(core: &Core, demo: Option<Json>) -> Json {
    let root = &core.root;
    let env = |name: &str| std::env::var(name).ok();
    let run =
        run_model::document_json(root, env(run_model::ENV).as_deref(), None).unwrap_or(Json::Null);
    let fast =
        fast_mode::document_json(root, env(fast_mode_env()).as_deref()).unwrap_or(Json::Null);
    let policy = resource_policy::read(root).ok().flatten();
    let role = if core.target { "target" } else { "owner" };
    let contract = jobj! {
        "model_id" => run.get("model_id").cloned().unwrap_or(Json::Null),
        "resolved_commit" => run.get("resolved_commit").cloned().unwrap_or(Json::Null),
    };
    let disclosure = jobj! {
        "text" => standing_consent::DISCLOSURE_TEXT, "digest" => standing_consent::DISCLOSURE_DIGEST,
    };
    let join_flow = if core.target {
        let grant = standing_consent::load(root).ok().flatten();
        let join = private_state::load(&root.join("join-window.json"), cap())
            .ok()
            .flatten();
        jobj! {
            "standing_disclosure" => disclosure,
            "standing_consent" => match grant {
                None => Json::Null,
                Some(g) => jobj! {
                    "state" => &g["state"], "caps" => &g["caps"], "granted_at" => &g["granted_at"],
                    "withdrawn_at" => &g["withdrawn_at"], "last" => Json::Null,
                },
            },
            "standing_error" => Json::Null, "standing_error_http" => Json::Null,
            "join_window" => match join {
                None => Json::Null,
                Some(j) => jobj! {
                    "state" => "waiting_for_owner", "fingerprint" => &j["fingerprint"],
                    "pinned" => !j["seed_key_digest"].is_null(),
                },
            },
            "last_report" => Json::Null,
        }
    } else {
        jobj! {
            "standing_disclosure" => disclosure,
            "pairing_windows" => core.windows.list().unwrap_or(Json::Arr(Vec::new())),
            "pending_windows" => core.pending.json(), "member_reports" => Json::Obj(Vec::new()),
        }
    };
    let values: Vec<(&str, Json)> = vec![
        ("label", LABEL.into()),
        ("role", "not_yet_serving".into()),
        ("model_contract", contract),
        ("run_model", run),
        ("fast_mode", fast),
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
        ("installation_role", role.into()),
        ("resource_policy", policy.map_or(Json::Null, Json::from)),
        ("running", core.running.into()),
        ("authority", authority_status(core)),
        ("renewal_error", Json::Null),
        ("recovery_error", Json::Null),
        (
            "distribution",
            "unsigned development software; not notarized".into(),
        ),
        (
            "leave",
            if core.target {
                let leave = MemberLeave::new(&core.root, Limits::default());
                status_view(&leave).map_or(Json::Null, Json::from)
            } else {
                Json::Null
            },
        ),
        ("join_flow", join_flow),
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
            c.demo.clone(),
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
        (core.demo.clone(), core.revision)
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
    Ok(PathBuf::from(home).join("Library/Application Support/Mycelium A13 macOS Demo"))
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
        demo: DemoView::default(),
        contributor: None,
        bootstrap_lifecycle: Arc::new(Mutex::new(())),
        bootstrap_incarnation: Arc::new(()),
        bootstrap: None,
        bootstrap_failure: None,
        exchange: None,
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
    let (target, root) = match core.lock() {
        Ok(c) => (c.target, c.root.clone()),
        Err(_) => return Ok(()),
    };
    if !target || !root.join("membership.state.json").exists() {
        return Ok(());
    }
    let (signer, _, _) = identity(&root).map_err(|e| e.code.to_owned())?;
    let options = SeedOptions::default();
    let random_hex = |n: usize| random(n).map(|b| hex(&b)).unwrap_or_default();
    let env = api::peer::JoinEnv {
        signer: &signer,
        clock: &now,
        random_hex: &random_hex,
        seed: &options,
    };
    match api::peer::start(&root, &signer, true, &env) {
        Ok(started) => {
            if let Ok(mut c) = core.lock() {
                c.session = started.session;
                c.running = true;
            }
            Ok(())
        }
        Err(failed) => match failed.error.code {
            "peer_transport_unreachable"
            | "seed_http_remote_error"
            | "peer_member_expired"
            | "peer_generation_stale"
            | "peer_member_unknown" => {
                if let Ok(mut c) = core.lock() {
                    c.running = false;
                }
                Ok(())
            }
            code => Err(code.to_owned()),
        },
    }
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
        core.lock().map_err(|_| "core_poisoned")?.demo = chain.view();
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
