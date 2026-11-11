// SPDX-License-Identifier: AGPL-3.0-only
//! A JOINED Mac's own demo agent (`demo_app.target_agent` + `auto_step`, `demo_app.py:155-173 @
//! f4e366e9`): every standing-loop pass (2 s) the member reads the owner's announcement over the
//! signed run exchange, answers it with its own signed report and, when the owner announces a
//! bootstrap offer for it, fetches that offer from the owner's mailbox, stages it and runs the
//! admitted hello-only helper under its own target pump (`/api/staging/accept` +
//! `/api/control/start` of `target_app.py`). The owner's observation then reaches this worker over
//! the owner's mailbox; the owner's cleanup request is answered by closing it (control stop +
//! staging cleanup accept). Nothing here is synthesised for the owner: every report, ACK and
//! native answer is this Mac's own.
//!
//! The process that runs the agent (the headless launcher, the packaged host) is its
//! [`MemberHost`]: its lifetime flags, the member's live lease, its resource gate, its model
//! acquisition, how the member reaches the owner's seed, the native sidecar, and the slot that owns
//! this Mac's one bootstrap worker.
//!
//! NOT ported here: the whole-snapshot download a joined Mac starts in whole mode (S1; a member
//! whose acquisition has no snapshot on disk reports `member_model_download_not_ported`; one on
//! disk is re-hashed, never downloaded), the periodic
//! capability report (`auto_report`), and work acceptance (the standing acceptor; row 3 (d)).
use crate::{
    acquisition::Acquisition,
    app_host::{IDENTITY_FILE, hex, identity, now, random},
    bootstrap_process::{CleanupFailure, TrustedBootstrap},
    bootstrap_stage::BootstrapStage,
    installation_descriptor::{self, Failure, Member as DescriptorMember, Paths, Source},
    local_agent::{AgentHost, ExchangeUnavailable, LocalAgent, WholeModel},
    managed_carrier::{HostClock, TargetFence},
    managed_routes::path_for,
    preferences::{private_state, standing_consent},
    run_exchange_client::{ClientError, ExchangeClient, Membership},
    seed::{SeedClient, SeedOptions},
    target_pump::{TargetHost, TargetPump},
};
use base64::Engine as _;
use budgets::{BudgetId, Entries, MonoInstant};
use protocol::identity::Identity;
use runtime::managed_owner::OwnerError;
use serde_json::{Map, Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};
use transport::Clock as _;

pub type OResult<T> = Result<T, OwnerError>;
type Client = Arc<Mutex<ExchangeClient>>;

const DISCOVERY: &str = "stage-discovery";
const STAGE_READ: &str = "mycelium.managed_stage_read.v1";
const STAGE_ACK: &str = "mycelium.managed_stage_ack.v1";
const STAGE_BYTES: &str = "mycelium.managed_stage_bytes.v1";
const CLEANUP_READ: &str = "mycelium.managed_stage_cleanup_read.v1";
const CLEANUP_ACK: &str = "mycelium.managed_stage_cleanup_ack.v1";
const CLEANUP_STATE: &str = "mycelium.managed_stage_cleanup_state.v1";

/// Closed codes a remote answer may carry into a `&'static` refusal; any other is unknown.
const CODES: &[&str] = &[
    "managed_control_unreachable",
    "managed_control_response_timeout",
    "managed_control_internal_error",
    "managed_control_response_invalid",
    "managed_control_response_signature_invalid",
    "managed_control_message_expired",
    "managed_control_member_stale",
    "managed_control_sequence_replayed",
    "managed_control_nonce_replayed",
    "managed_control_conflicting_retry",
    "managed_control_prepared_request_invalid",
    "managed_control_descriptor_unavailable",
    "managed_control_run_binding_mismatch",
    "managed_control_route_unknown",
    "managed_owner_unavailable",
    "managed_owner_closed",
    "offer_unavailable",
    "offer_cleanup_requested",
    "offer_cleanup_unconfirmed",
    "offer_ack_invalid",
    "offer_ack_conflict",
    "offer_cleanup_ack_invalid",
    "offer_cleanup_ack_conflict",
    "offer_response_invalid",
    "offer_expired",
    "demo_announcement_invalid",
    "demo_report_invalid",
    "seed_http_unreachable",
];
fn intern(code: &str) -> &'static str {
    CODES
        .iter()
        .find(|c| **c == code)
        .copied()
        .unwrap_or("managed_control_remote_error")
}
fn client_error(error: ClientError) -> OwnerError {
    OwnerError(intern(&error.0))
}
fn locked<T>(m: &Mutex<T>) -> OResult<std::sync::MutexGuard<'_, T>> {
    m.lock().map_err(|_| OwnerError("member_state_unavailable"))
}
fn fail(error: OwnerError) -> Failure {
    Failure::new("RuntimeError", error.0)
}
fn nonce() -> Option<String> {
    random(16).ok().map(|b| hex(&b))
}

/// The host process's lifetime flags, as one snapshot of its state.
#[derive(Clone, Debug)]
pub struct HostState {
    /// The member's private state root.
    pub root: PathBuf,
    /// `installation_role == 'target'`.
    pub target: bool,
    /// `DemoApp.running`.
    pub running: bool,
    /// `_left` (member leave).
    pub left: bool,
    /// `self.closed` (Quit).
    pub closed: bool,
}
impl HostState {
    /// No new bootstrap worker may start (or be kept) under these flags.
    fn fenced(&self) -> bool {
        self.closed || !self.running || !self.target || self.left
    }
}
/// The host's one bootstrap worker slot, with the host's flags as the same lock holds them.
pub struct Slot<'a, W> {
    pub worker: &'a mut Option<W>,
    pub failure: &'a mut Option<CleanupFailure>,
    /// Replaced to invalidate any reservation of the previous incarnation.
    pub incarnation: &'a mut Arc<()>,
    pub state: HostState,
}
/// A started target pump on its own thread (the host's own worker type).
pub trait MemberWorker: Send + 'static {
    /// The pump thread is still driving its worker.
    fn running(&self) -> bool;
    fn scope(&self) -> &Value;
    fn close(&mut self) -> OResult<()>;
}
/// What the agent reads and owns outside itself.
pub trait MemberHost: Send + Sync + Sized + 'static {
    type Worker: MemberWorker;
    /// One snapshot of the host's flags: `managed_owner_closed` once the host is gone,
    /// `member_state_unavailable` when its state cannot be read.
    fn state(&self) -> OResult<HostState>;
    /// The member's live lease (`session_view` while the renewal has it on the wire, else the session's).
    fn lease(&self) -> Option<f64>;
    /// `app._resource_check()`: a member leave fence first, then this Mac's resource gate.
    fn resource_check(&self) -> Option<String>;
    /// `DemoApp.acquisition`.
    fn acquisition(&self) -> Option<Acquisition>;
    /// How this member reaches the owner's seed.
    fn member_seed_options(&self) -> SeedOptions;
    /// `installed_sidecar()` (`native_runtime.py:8-22`).
    fn installed_sidecar(&self) -> Result<PathBuf, Failure>;
    /// Serializes bootstrap side effects and cleanup; never held with the state lock on entry.
    fn lifecycle(&self) -> OResult<Arc<Mutex<()>>>;
    /// Run `f` on the worker slot under the host's state lock.
    fn slot<R>(&self, f: impl FnOnce(Slot<'_, Self::Worker>) -> R) -> OResult<R>;
    /// The admitted hello-only helper program this installation runs (`bootstrap-program.json`).
    fn program(&self, root: &Path) -> OResult<TrustedBootstrap>;
    /// Drive a started pump on its own thread until Stop (or its stop command).
    fn spawn(
        &self,
        pump: TargetPump<RemoteTarget<Self>>,
        fence: TargetFence,
        scope: Value,
    ) -> Self::Worker;
}

/// This member's retained membership (`membership.state.json`), the source of its binding.
fn membership(root: &Path) -> OResult<Map<String, Value>> {
    crate::peer::load_membership_state(root)
        .ok()
        .flatten()
        .ok_or(OwnerError("membership_required"))
}
/// The membership identity a client was made for; a change (resume, new generation) makes a new one.
fn membership_key(m: &Map<String, Value>) -> Value {
    json!([
        m.get("node_id"),
        m.get("membership_generation"),
        m.get("incarnation"),
        m.get("seed_url")
    ])
}

/// The member's standing-loop state: the agent, its exchange client and the last announcement.
pub struct MemberAgent<H: MemberHost> {
    agent: Option<LocalAgent>,
    client: Option<(Value, Client)>,
    announcement: Arc<Mutex<Value>>,
    _host: std::marker::PhantomData<fn(H)>,
}
impl<H: MemberHost> Default for MemberAgent<H> {
    fn default() -> Self {
        Self {
            agent: None,
            client: None,
            announcement: Arc::default(),
            _host: std::marker::PhantomData,
        }
    }
}

/// `StandingConsentGate.allows` (`demo_target.py:44-66`): the join lane's grant, bound to THIS
/// membership, then the resource gates.
fn standing_refusal<H: MemberHost>(host: &H) -> Option<String> {
    grant_refusal(host).or_else(|| host.resource_check())
}
/// The grant half of the gate (file reads only): the target pump checks it on every exchange; the
/// resource observation (a probe per call) is the agent's, once per standing-loop pass.
fn grant_refusal<H: MemberHost>(host: &H) -> Option<String> {
    let root = host.state().ok()?.root;
    let grant = match standing_consent::load(&root) {
        Ok(Some(grant)) => grant,
        Ok(None) => return Some("standing_consent_missing".into()),
        Err(_) => return Some("standing_consent_invalid".into()),
    };
    if grant.get("state") != Some(&json!("granted"))
        || grant.get("binding").is_none_or(Value::is_null)
    {
        return Some(if grant.get("state") == Some(&json!("withdrawn")) {
            "standing_consent_withdrawn".into()
        } else {
            "standing_consent_unbound".into()
        });
    }
    let Ok(m) = membership(&root) else {
        return Some("membership_required".into());
    };
    let binding = json!({"swarm_id": m.get("swarm_id"), "node_id": m.get("node_id"),
        "seed_key_digest": m.get("seed_key_digest")});
    if grant.get("binding") != Some(&binding) {
        return Some("standing_consent_owner_mismatch".into());
    }
    None
}

impl<H: MemberHost> MemberAgent<H> {
    /// `RunExchangeClient(app.authority.peer)` for the CURRENT membership.
    fn client(&mut self, host: &H, root: &Path) -> OResult<Client> {
        let m = membership(root)?;
        let key = membership_key(&m);
        if let Some((current, client)) = &self.client
            && *current == key
        {
            return Ok(client.clone());
        }
        let text = |k: &str| {
            m.get(k)
                .and_then(Value::as_str)
                .map(str::to_owned)
                .ok_or(OwnerError("membership_required"))
        };
        let records: Vec<Value> = m
            .get("seed_key_records")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let origin = crate::join_link::canonical_https_origin(&json!(text("seed_url")?))
            .map_err(|_| OwnerError("peer_seed_origin_invalid"))?;
        let seed = SeedClient::new(
            &origin,
            &text("swarm_id")?,
            &text("seed_key_digest")?,
            &records,
            &host.member_seed_options(),
        )
        .map_err(|_| OwnerError("peer_seed_origin_invalid"))?;
        let (signer, _, node) =
            identity(root).map_err(|_| OwnerError("endpoint_identity_invalid"))?;
        if Some(&json!(node)) != m.get("node_id") {
            return Err(OwnerError("peer_identity_mismatch"));
        }
        let member = Membership {
            swarm_id: text("swarm_id")?,
            owner_node_id: text("seed_node_id")?,
            owner_key: records
                .first()
                .cloned()
                .ok_or(OwnerError("membership_required"))?,
            node_id: node,
            generation: m
                .get("membership_generation")
                .and_then(Value::as_u64)
                .ok_or(OwnerError("membership_required"))?,
            incarnation: text("incarnation")?,
        };
        let client = Arc::new(Mutex::new(ExchangeClient::new(
            seed,
            Arc::new(signer),
            member,
            Box::new(now),
            Box::new(nonce),
        )));
        self.client = Some((key, client.clone()));
        Ok(client)
    }

    /// One standing-loop pass (`run_auto_step` -> `auto_step`).
    pub fn tick(&mut self, host: &Arc<H>) {
        let Ok(HostState {
            root,
            target,
            running,
            left,
            closed,
        }) = host.state()
        else {
            return;
        };
        // `run_auto_step`: a joined target with a granted, bound standing consent only.
        let granted = standing_consent::load(&root)
            .ok()
            .flatten()
            .is_some_and(|g| g.get("state") == Some(&json!("granted")));
        if !target || !running || left || closed || !granted {
            if left || closed {
                self.close(host);
            }
            return;
        }
        let client = match self.client(host, &root) {
            Ok(client) => client,
            Err(error) => {
                eprintln!("member agent: {}", error.0);
                return;
            }
        };
        let agent = self.agent.get_or_insert_with(LocalAgent::remote);
        let mut view = MemberView {
            host,
            client,
            announcement: self.announcement.clone(),
            root,
        };
        agent.tick(&mut view);
    }

    /// Leave / quit: no worker survives this Mac's membership.
    pub fn close(&mut self, host: &Arc<H>) {
        if let Some(agent) = self.agent.as_mut() {
            agent.close();
        }
        if let Err(error) = close_worker(&**host) {
            eprintln!("member target close: {}", error.0);
        }
    }
}

/// Close this Mac's bootstrap worker (its pump removes the stage and ACKs the cleanup).
fn close_worker<H: MemberHost>(host: &H) -> OResult<()> {
    let lifecycle = host.lifecycle()?;
    let _lifecycle = locked(&lifecycle)?;
    let (worker, failure) = host.slot(|s| {
        *s.incarnation = Arc::new(());
        (s.worker.take(), s.failure.take())
    })?;
    let mut result = Ok(());
    if let Some(mut worker) = worker
        && let Err(error) = worker.close()
    {
        host.slot(|s| *s.worker = Some(worker))?;
        result = Err(error);
    }
    if let Some(mut failure) = failure
        && let Err(code) = failure.retry()
    {
        host.slot(|s| *s.failure = Some(failure))?;
        result = Err(OwnerError(code));
    }
    result
}

struct MemberView<'a, H: MemberHost> {
    host: &'a Arc<H>,
    client: Client,
    announcement: Arc<Mutex<Value>>,
    root: PathBuf,
}

impl<H: MemberHost> MemberView<'_, H> {
    fn acquisition(&self) -> Result<Acquisition, Failure> {
        self.host
            .acquisition()
            .ok_or_else(|| Failure::new("AttributeError", "demo_step_failed"))
    }
    /// `<peer state>/serving/slices/<run>`: this Mac's empty per-run slice root.
    fn prepare_slice_root(&self, run_id: &str) -> Result<String, Failure> {
        use std::os::unix::fs::DirBuilderExt;
        let base = self.root.join("serving").join("slices");
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
    /// One signed staging request (`ManagedOfferClient._read` / `cleanup_status`), verified.
    fn staging(&self, protocol: &str, extra: Value) -> OResult<Value> {
        stage_call(&self.client, protocol, extra)
    }
}

/// A signed staging statement for this member, POSTed to its route and verified.
fn stage_call(client: &Client, protocol: &str, extra: Value) -> OResult<Value> {
    let client = locked(client)?;
    let now = client.now();
    let ttl = BudgetId::PyQuantityMyceliumA13MacosManagedOffersL371C28
        .span(Entries::ZERO)
        .map_err(|_| OwnerError("budget_unavailable"))?;
    let request_id = client.nonce().map_err(client_error)?;
    let mut statement = Value::Object(client.binding(DISCOVERY, DISCOVERY));
    for (k, v) in json!({"protocol": protocol, "swarm_id": client.member().swarm_id,
        "request_id": request_id, "sequence": 1, "nonce": client.nonce().map_err(client_error)?,
        "issued_at": now, "expires_at": now + ttl.millis() as f64 / 1000.0})
    .as_object()
    .into_iter()
    .flatten()
    .chain(extra.as_object().into_iter().flatten())
    {
        statement[k] = v.clone();
    }
    let path = path_for(protocol).ok_or(OwnerError("managed_control_route_unknown"))?;
    let envelope = client.sign(&statement).map_err(client_error)?;
    let response = client.post(path, &envelope).map_err(client_error)?;
    let answer = if matches!(protocol, CLEANUP_READ | CLEANUP_ACK) {
        CLEANUP_STATE
    } else {
        STAGE_BYTES
    };
    let body = client
        .verified(&response, answer, &request_id, DISCOVERY, DISCOVERY)
        .map_err(client_error)?;
    let offer = &body["offer"];
    if !offer.is_null()
        && crate::run_exchange_client::BINDING
            .iter()
            .any(|k| offer[*k] != statement[*k])
    {
        return Err(OwnerError("offer_response_invalid"));
    }
    if let Some(offset) = statement.get("offset")
        && body["offset"] != *offset
    {
        return Err(OwnerError("offer_response_invalid"));
    }
    Ok(body)
}

impl<H: MemberHost> AgentHost for MemberView<'_, H> {
    fn announcement(&mut self) -> Result<Option<Value>, ExchangeUnavailable> {
        let read = locked(&self.client)
            .map_err(|_| ExchangeUnavailable)?
            .read();
        match read {
            Ok(announcement) => {
                if let Ok(mut last) = self.announcement.lock() {
                    *last = announcement.clone();
                }
                Ok(Some(announcement))
            }
            Err(error) => {
                eprintln!("member exchange read: {}", error.0);
                Err(ExchangeUnavailable)
            }
        }
    }
    fn member(&mut self) -> Option<String> {
        membership(&self.root)
            .ok()?
            .get("node_id")
            .and_then(Value::as_str)
            .map(str::to_owned)
    }
    fn run_active(&mut self) -> bool {
        false
    }
    fn standing_refusal(&mut self) -> Option<String> {
        standing_refusal(&**self.host)
    }
    fn resource_check(&mut self) -> Option<String> {
        self.host.resource_check()
    }
    fn prepared_model(&mut self) -> Result<(String, String), Failure> {
        let acquisition = self.acquisition()?;
        acquisition
            .prepared_source()
            .map_err(|e| Failure::new("AcquisitionError", e.code()))?;
        let model = acquisition.model();
        Ok((model.model_id.to_owned(), model.resolved_commit.to_owned()))
    }
    /// `_ensure_whole_model(a)`: the announced model must be prepared on this Mac. A complete
    /// snapshot already on disk is re-hashed here under the standing storage cap (a grant of one
    /// network byte: nothing is downloaded); fetching a missing snapshot is S1's download.
    fn whole_model(&mut self, a: &Value) -> Result<WholeModel, Failure> {
        let want = (
            a["model"]["model_id"]
                .as_str()
                .unwrap_or_default()
                .to_owned(),
            a["model"]["resolved_commit"]
                .as_str()
                .unwrap_or_default()
                .to_owned(),
        );
        let waiting = |state: &str, detail: Option<&str>, progress: Option<Value>| {
            Ok(WholeModel::Waiting {
                state: state.into(),
                detail: detail.map(str::to_owned),
                progress,
            })
        };
        let Ok(acquisition) = self.acquisition() else {
            return waiting("error", Some("member_model_download_not_ported"), None);
        };
        let model = acquisition.model();
        if (model.model_id.to_owned(), model.resolved_commit.to_owned()) != want {
            return waiting("error", Some("demo_model_mismatch"), None);
        }
        let status = acquisition.status();
        let progress =
            json!({"done_bytes": status.downloaded_bytes, "total_bytes": status.total_bytes});
        match status.state.as_str() {
            "prepared" => return Ok(WholeModel::Ready),
            "starting" | "resolving_metadata" | "downloading" => {
                return waiting("downloading_model", None, Some(progress));
            }
            "verifying" | "compiling" => {
                let detail = if status.state == "verifying" {
                    "model_verifying"
                } else {
                    "model_compiling"
                };
                return waiting("downloading_model", Some(detail), Some(progress));
            }
            _ => {}
        }
        if let Some(reason) = standing_refusal(&**self.host) {
            let state = if reason.starts_with("standing_consent") {
                "waiting_for_consent"
            } else {
                "idle"
            };
            return waiting(state, Some(&reason), None);
        }
        let cap = standing_consent::load(&self.root)
            .ok()
            .flatten()
            .and_then(|g| g.get("caps").and_then(|c| c["storage_bytes"].as_u64()))
            .ok_or_else(|| Failure::new("KeyError", "demo_step_failed"))?;
        match acquisition.reverify_closed_snapshot(cap) {
            Ok(_) => waiting(
                "downloading_model",
                Some("model_verifying"),
                Some(json!({"done_bytes": 0, "total_bytes": 0})),
            ),
            Err(error) if error.code() == "source_not_on_disk" => {
                waiting("error", Some("member_model_download_not_ported"), None)
            }
            Err(error) => waiting("error", Some(error.code()), None),
        }
    }
    fn describe(
        &mut self,
        run_id: &str,
        deployment_id: &str,
        slice_manifest_digest: Option<&str>,
    ) -> Result<Value, Failure> {
        let not_current = || Failure::new("RemoteMacMemberError", "peer_member_not_current");
        let state = membership(&self.root).map_err(|_| not_current())?;
        let lease = self.host.lease();
        if lease.is_none_or(|lease| now() >= lease) {
            return Err(not_current());
        }
        let (signer, endpoint, node) = identity(&self.root)
            .map_err(|_| Failure::new("ValueError", "endpoint_identity_invalid"))?;
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
                let (root, facts) = self
                    .acquisition()?
                    .prepared_source()
                    .map_err(|e| Failure::new("AcquisitionError", e.code()))?;
                Source::Whole {
                    source_root: root.to_string_lossy().into_owned(),
                    manifest_digest: facts.manifest_digest,
                }
            }
        };
        let paths = Paths {
            state_root: self.root.to_string_lossy().into_owned(),
            endpoint_secret_file: self.root.join(IDENTITY_FILE).to_string_lossy().into_owned(),
            python_executable: std::env::current_exe()
                .ok()
                .and_then(|p| fs::canonicalize(p).ok())
                .map_or_else(String::new, |p| p.to_string_lossy().into_owned()),
            sidecar_binary: self
                .host
                .installed_sidecar()?
                .to_string_lossy()
                .into_owned(),
        };
        let swarm = state
            .get("swarm_id")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_owned();
        installation_descriptor::installation_descriptor(
            &signer,
            &member,
            &swarm,
            run_id,
            deployment_id,
            true,
            &source,
            &paths,
            now(),
        )
    }
    fn bootstrap(&mut self, run: &str, deployment: &str, offer: &str) -> Result<(), Failure> {
        bootstrap(self, run, deployment, offer).map_err(fail)
    }
    fn pending_cleanup(&mut self) -> Result<Option<Value>, Failure> {
        let body = match self.staging(CLEANUP_READ, json!({"offer_id": null})) {
            // An owner whose mailbox does not exist yet has published nothing.
            Err(OwnerError("managed_owner_unavailable")) => return Ok(None),
            other => other.map_err(fail)?,
        };
        Ok((!body["offer"].is_null()).then(|| body["offer"].clone()))
    }
    /// `/api/control/stop` then `/api/staging/cleanup/accept` (`_cleanup_if_requested`).
    fn cleanup(&mut self, request: &Value) -> Result<(), Failure> {
        let had_worker = self
            .host
            .slot(|s| s.worker.is_some() || s.failure.is_some())
            .unwrap_or(false);
        close_worker(&**self.host).map_err(fail)?;
        if had_worker {
            // The closed pump removed its stage and ACKed this cleanup itself.
            return Ok(());
        }
        // Nothing was staged for this offer here: confirm its cleanup (nothing to remove).
        let ack = json!({"protocol": "mycelium.controller_remote_cleanup_ack.v1",
            "node_id": request["target_node_id"], "staging_root": stage_root(&self.root),
            "removed": true});
        self.staging(
            CLEANUP_ACK,
            json!({"offer_id": request["offer_id"], "ack": ack}),
        )
        .map(drop)
        .map_err(fail)
    }
    fn stop_native(&mut self) -> Result<(), Failure> {
        close_worker(&**self.host).map_err(fail)
    }
    fn native_control(&mut self) -> Result<Value, Failure> {
        self.host
            .slot(|s| match (&*s.worker, &*s.failure) {
                (Some(worker), _) if worker.running() => json!({"state": "running"}),
                (_, Some(failure)) => json!({"state": "blocked", "error_code": failure.code()}),
                (Some(_), None) => json!({"state": "stopped"}),
                (None, None) => json!({}),
            })
            .map_err(|_| Failure::new("RuntimeError", "demo_step_failed"))
    }
    fn pending_offer(&mut self) -> Result<Option<Value>, Failure> {
        let body = match self.staging(STAGE_READ, json!({"offer_id": null, "offset": 0})) {
            Err(OwnerError("managed_owner_unavailable")) => return Ok(None),
            other => other.map_err(fail)?,
        };
        Ok((!body["offer"].is_null()).then(|| body["offer"].clone()))
    }
    fn sink(&mut self, report: &Value) -> Result<(), ExchangeUnavailable> {
        let sent = locked(&self.client)
            .map_err(|_| ExchangeUnavailable)?
            .report(report);
        sent.map(drop).map_err(|error| {
            eprintln!("member exchange report: {}", error.0);
            ExchangeUnavailable
        })
    }
    fn clock(&mut self) -> f64 {
        now()
    }
}

/// Where this Mac stages a bootstrap offer.
fn native_root(root: &Path) -> PathBuf {
    root.join("native-bootstrap")
}
fn stage_root(root: &Path) -> PathBuf {
    native_root(root).join("stage")
}

/// `/api/staging/accept` + `/api/control/start` for the owner's announced bootstrap offer: fetch
/// the offer from the owner's mailbox, check it is exactly the model-free hello archive of this
/// run, stage it and start the admitted helper under this Mac's own target pump.
fn bootstrap<H: MemberHost>(
    view: &MemberView<'_, H>,
    run: &str,
    deployment: &str,
    offer_id: &str,
) -> OResult<()> {
    let (scope, offer) = {
        let body = view.staging(STAGE_READ, json!({"offer_id": null, "offset": 0}))?;
        let client = locked(&view.client)?;
        let scope = Value::Object(client.binding(run, deployment));
        (scope, body["offer"].clone())
    };
    let wanted = runtime::observe::bootstrap_archive(run, deployment);
    if offer["offer_id"] != offer_id
        || offer["expires_at"].as_f64().is_none_or(|t| t <= now())
        || scope
            .as_object()
            .ok_or(OwnerError("offer_response_invalid"))?
            .iter()
            .any(|(k, v)| offer[k] != *v)
    {
        return Err(OwnerError("offer_response_invalid"));
    }
    if offer.as_object().is_none_or(|o| o.len() != 11)
        || offer["archive_bytes"].as_u64() != Some(wanted.len() as u64)
        || offer["archive_digest"] != protocol::artifact_grant::range_digest(&wanted)
    {
        return Err(OwnerError("offer_archive_digest_mismatch"));
    }
    let chunk = BudgetId::PyMyceliumA13MacosManagedOffersChunkL31
        .bytes()
        .map_err(|_| OwnerError("budget_unavailable"))?
        .get();
    let mut archive = Vec::with_capacity(wanted.len());
    while archive.len() < wanted.len() {
        let read = view.staging(
            STAGE_READ,
            json!({"offer_id": offer_id, "offset": archive.len() as u64}),
        )?;
        if read["offer"] != offer {
            return Err(OwnerError("offer_changed"));
        }
        let data = base64::engine::general_purpose::STANDARD
            .decode(
                read["data"]
                    .as_str()
                    .ok_or(OwnerError("offer_response_invalid"))?,
            )
            .map_err(|_| OwnerError("offer_response_invalid"))?;
        if data.len() as u64 != chunk.min((wanted.len() - archive.len()) as u64) {
            return Err(OwnerError("offer_response_invalid"));
        }
        archive.extend(data);
    }
    if archive != wanted
        || protocol::artifact_grant::range_digest(&archive) != offer["archive_digest"]
    {
        return Err(OwnerError("offer_archive_digest_mismatch"));
    }
    start(view, scope, offer, archive)
}

fn start<H: MemberHost>(
    view: &MemberView<'_, H>,
    scope: Value,
    offer: Value,
    archive: Vec<u8>,
) -> OResult<()> {
    let host = view.host;
    let lifecycle = host.lifecycle()?;
    let _lifecycle = locked(&lifecycle)?;
    // `false`: the same worker already runs this exact offer, nothing to start.
    let admitted = host.slot(|s| {
        if s.failure.is_some() {
            return Err(OwnerError("control_restart_requires_reconciliation"));
        }
        if let Some(worker) = s.worker.as_ref() {
            return if *worker.scope() == scope && worker.running() {
                Ok(false)
            } else {
                Err(OwnerError("stage_runtime_active"))
            };
        }
        if s.state.fenced() {
            return Err(OwnerError("bootstrap_start_fenced"));
        }
        Ok(true)
    })??;
    if !admitted {
        return Ok(());
    }
    let program = host.program(&view.root)?;
    let root = native_root(&view.root);
    admit_member_stage(&root)?;
    fs::create_dir_all(&root).map_err(|_| OwnerError("bootstrap_stage_create_failed"))?;
    fs::set_permissions(&root, std::os::unix::fs::PermissionsExt::from_mode(0o700))
        .map_err(|_| OwnerError("bootstrap_stage_create_failed"))?;
    let stage = BootstrapStage::create(&stage_root(&view.root), &scope, &offer, &archive)
        .map_err(OwnerError)?;
    let signer = Arc::new(
        identity(&view.root)
            .map_err(|_| OwnerError("endpoint_identity_invalid"))?
            .0,
    );
    let (swarm, owner, owner_key) = {
        let client = locked(&view.client)?;
        let m = client.member();
        (
            m.swarm_id.clone(),
            m.owner_node_id.clone(),
            m.owner_key.clone(),
        )
    };
    let target = RemoteTarget {
        host: host.clone(),
        client: view.client.clone(),
        announcement: view.announcement.clone(),
        root: view.root.clone(),
        clock: HostClock::default(),
        signer,
        scope: scope.clone(),
        offer: offer.clone(),
        swarm,
        owner,
        owner_key,
    };
    let fence = TargetFence::default();
    let pump = match TargetPump::start(
        &root,
        target,
        scope.clone(),
        offer,
        stage,
        &program,
        fence.clone(),
    ) {
        Ok(pump) => pump,
        Err(failure) => {
            let code = failure.code();
            host.slot(|s| *s.failure = Some(failure))?;
            return Err(OwnerError(code));
        }
    };
    let worker = host.spawn(pump, fence, scope);
    let refused = host.slot(|s| {
        if s.worker.is_some() || s.state.fenced() {
            Some(worker)
        } else {
            *s.worker = Some(worker);
            None
        }
    })?;
    if let Some(mut worker) = refused {
        worker.close()?;
        return Err(OwnerError("bootstrap_start_fenced"));
    }
    Ok(())
}

/// A new stage needs a cleanly closed previous one (never reconstruct authority from a saved PID).
fn admit_member_stage(root: &Path) -> OResult<()> {
    let limit = private_state::private_json_limit().map_err(|e| OwnerError(e.0))?;
    let state =
        private_state::load(&root.join("state.json"), limit).map_err(|e| OwnerError(e.0))?;
    let stage_present = !matches!(
        fs::symlink_metadata(root.join("stage")),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound
    );
    if stage_present || state.is_some_and(|s| s.get("phase") != Some(&json!("closed"))) {
        return Err(OwnerError("control_restart_requires_reconciliation"));
    }
    Ok(())
}

/// The member's target pump host: its mailbox is the OWNER's, reached over the seed's signed
/// managed-control routes. Authority is this Mac's current membership and standing consent.
pub struct RemoteTarget<H: MemberHost> {
    host: Arc<H>,
    client: Client,
    announcement: Arc<Mutex<Value>>,
    root: PathBuf,
    clock: HostClock,
    signer: Arc<Identity>,
    scope: Value,
    offer: Value,
    swarm: String,
    owner: String,
    owner_key: Value,
}
impl<H: MemberHost> transport::Clock for RemoteTarget<H> {
    fn monotonic(&self) -> MonoInstant {
        self.clock.monotonic()
    }
    fn wall_seconds(&self) -> f64 {
        self.clock.wall_seconds()
    }
}
impl<H: MemberHost> RemoteTarget<H> {
    /// The membership the pump was started for is still this Mac's, and (`active`) the owner still
    /// announces this run under a standing consent that allows it.
    fn current(&self, active: bool) -> OResult<()> {
        let state = match self.host.state() {
            Err(OwnerError("managed_owner_closed")) => {
                return Err(OwnerError("managed_owner_closed"));
            }
            other => other,
        };
        // A removal ACK (cleanup) is still owed while this process quits or leaves.
        if active && state?.fenced() {
            return Err(OwnerError("managed_owner_closed"));
        }
        let m = membership(&self.root)?;
        if m.get("node_id") != Some(&self.scope["target_node_id"])
            || m.get("membership_generation") != Some(&self.scope["membership_generation"])
            || m.get("incarnation") != Some(&self.scope["member_incarnation"])
        {
            return Err(OwnerError("managed_control_member_stale"));
        }
        if active {
            if let Some(code) = grant_refusal(&*self.host) {
                eprintln!("member target refused: {code}");
                return Err(OwnerError("offer_transfer_not_authorized"));
            }
            let a = locked(&self.announcement)?.clone();
            if a["run_id"] != self.scope["run_id"]
                || a["deployment_id"] != self.scope["deployment_id"]
            {
                return Err(OwnerError("offer_transfer_not_authorized"));
            }
            if self.offer["expires_at"]
                .as_f64()
                .is_none_or(|t| t <= self.clock.wall_seconds())
            {
                return Err(OwnerError("offer_expired"));
            }
        }
        Ok(())
    }
}
impl<H: MemberHost> TargetHost for RemoteTarget<H> {
    fn check(&self, scope: &Value) -> crate::target_pump::Result<()> {
        if *scope != self.scope {
            return Err("managed_control_run_binding_mismatch");
        }
        self.current(true).map_err(|e| e.0)
    }
    fn signer(&self) -> &Identity {
        &self.signer
    }
    fn owner_key(&self) -> Value {
        self.owner_key.clone()
    }
    fn owner_node(&self) -> String {
        self.owner.clone()
    }
    fn swarm(&self) -> String {
        self.swarm.clone()
    }
    fn nonce(&self) -> crate::target_pump::Result<String> {
        nonce().ok_or("managed_control_entropy_unavailable")
    }
    fn exchange(
        &self,
        envelope: &Value,
        _end: MonoInstant,
        fence: &TargetFence,
    ) -> crate::target_pump::Result<Value> {
        fence.check().map_err(|e| e.0)?;
        self.current(true).map_err(|e| e.0)?;
        let path = envelope["statement"]["protocol"]
            .as_str()
            .and_then(path_for)
            .ok_or("managed_control_route_unknown")?;
        let response = locked(&self.client)
            .map_err(|e| e.0)?
            .post(path, envelope)
            .map_err(|e| {
                eprintln!("member target exchange {path}: {}", e.0);
                intern(&e.0)
            })?;
        fence.check().map_err(|e| e.0)?;
        self.current(true).map_err(|e| e.0)?;
        Ok(response)
    }
    fn stage_ack(
        &self,
        offer: &Value,
        root: &Path,
        removed: bool,
    ) -> crate::target_pump::Result<()> {
        if *offer != self.offer {
            return Err("offer_changed");
        }
        self.current(!removed).map_err(|e| e.0)?;
        let ack = if removed {
            json!({"protocol":"mycelium.controller_remote_cleanup_ack.v1","node_id":self.scope["target_node_id"],"staging_root":root,"removed":true})
        } else {
            json!({"protocol":"mycelium.controller_remote_stage_ack.v1","node_id":self.scope["target_node_id"],"staging_root":root,"archive_digest":offer["archive_digest"],"archive_size_bytes":offer["archive_bytes"]})
        };
        let (protocol, extra) = if removed {
            (
                CLEANUP_ACK,
                json!({"offer_id": offer["offer_id"], "ack": ack}),
            )
        } else {
            (
                STAGE_ACK,
                json!({"offer_id": offer["offer_id"], "offset": 0, "ack": ack}),
            )
        };
        let body = stage_call(&self.client, protocol, extra).map_err(|e| e.0)?;
        if body["offer"] != *offer || (removed && body["ack"] != ack) {
            return Err("offer_response_invalid");
        }
        self.current(!removed).map_err(|e| e.0)
    }
}
