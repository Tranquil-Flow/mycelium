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
    bootstrap_process::{CleanupFailure, ProcessControl, TrustedBootstrap},
    bootstrap_stage::BootstrapStage,
    installation_descriptor::{self, Failure, Member as DescriptorMember, Paths, Source},
    local_agent::{AgentHost, ExchangeUnavailable, LocalAgent, WholeModel},
    managed_carrier::{HostClock, TargetFence},
    managed_routes::path_for,
    preferences::{private_state, standing_consent},
    run_exchange_client::{ClientError, ExchangeClient, Membership},
    seed::{SeedClient, SeedOptions},
    standing_acceptor::{AcceptFailure, StandingAcceptor, WorkAcceptance},
    target_pump::{StepOutcome, TargetHost, TargetPump},
};
use base64::Engine as _;
use budgets::{BudgetId, Entries, MonoInstant, Span};
use protocol::identity::Identity;
use runtime::managed_owner::OwnerError;
use serde_json::{Map, Value, json};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    thread::JoinHandle,
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
    /// `demo_target._ensure_whole_model` (demo_target.py:543-612): this joined Mac opens its own
    /// acquisition of the owner's announced model and starts it under its standing consent —
    /// `self.app.action('/api/run-model', {'choice'})` then `/api/model/download` with the same
    /// cap. The default refuses by name, so a host without a model source never pretends to fetch.
    fn start_whole_model(&self, _choice: &Value) -> Result<(), String> {
        Err("member_model_download_not_ported".into())
    }
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
    /// `auto_step_context()` + `read_resource_policy(root)` (app.py:1168-1172, :1176-1178): the
    /// policy and the current membership binding the standing acceptor works under, and how the
    /// member's pre-run report reaches the owner. `None`: this host runs no acceptor.
    fn accept_context(&self) -> Option<AcceptContext> {
        None
    }
    /// `_standing_tick`'s `due = time.monotonic() >= self._next_periodic_report` (app.py:1197 @
    /// f4e366e9): the host keeps `_next_periodic_report` (every `_auto_report` moves it). A host
    /// that keeps none sends no periodic report.
    fn periodic_report_due(&self) -> bool {
        false
    }
    /// The end of `_auto_report` (app.py:1161-1162): `self.last_report = record` (shown in
    /// `join_flow.last_report`) and the next periodic report 300 s on.
    fn reported(&self, _record: Value) {}
    /// A packaged member owns an already-enrolled work endpoint. Other hosts keep their
    /// existing behavior; this is not permission to run an operator pairing flow.
    fn work_enabled(&self) -> bool {
        false
    }
    fn work_announcement(
        &self,
        _announcement: crate::run_exchange::owner_pin::VerifiedAnnouncement,
    ) -> Result<Option<Value>, String> {
        Ok(None)
    }
    fn close_work(&self) {}
    /// A composed work service's actual custody report; None keeps legacy agents unchanged.
    fn work_report(&self) -> Result<Option<Value>, String> {
        Ok(None)
    }
    /// `preview_work(actor, offer, stage_acknowledged=True, limits=, resource_policy=)`: no member
    /// work service is composed by default (it is the member work service's, S4b/S4c).
    fn work_preview(
        &self,
        _offer: &Value,
        _limits: &Value,
        _policy: &Value,
    ) -> Result<Value, AcceptFailure> {
        Err(AcceptFailure::code("member_work_unavailable"))
    }
    /// `accept_work(actor, preview, work_acknowledged=True, limits=, resource_policy=)`.
    fn work_accept(
        &self,
        _preview: &Value,
        _limits: &Value,
        _policy: &Value,
    ) -> Result<(), AcceptFailure> {
        Err(AcceptFailure::code("member_work_unavailable"))
    }
    /// Drive a started pump on its own thread until Stop (or its stop command).
    fn spawn(
        &self,
        pump: TargetPump<RemoteTarget<Self>>,
        fence: TargetFence,
        scope: Value,
    ) -> Self::Worker;
}

/// What the standing acceptor of a joined Mac works under (`DemoApp.auto_accept`).
#[derive(Clone, Debug, Default)]
pub struct AcceptContext {
    /// `read_resource_policy(self.root)`.
    pub policy: Option<Value>,
    /// `auto_step_context()['binding']`: the membership binding while it is `current`.
    pub binding: Option<Value>,
    /// The join seam the member's reports go through (`_join_client`'s base).
    pub join_base: Option<String>,
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
    /// `DemoApp.standing` (created on the first acceptor pass).
    acceptor: Option<MemberAcceptor>,
    _host: std::marker::PhantomData<fn(H)>,
}
impl<H: MemberHost> Default for MemberAgent<H> {
    fn default() -> Self {
        Self {
            agent: None,
            client: None,
            announcement: Arc::default(),
            acceptor: None,
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
        let grant = standing_consent::load(&root).ok().flatten();
        let granted = grant
            .as_ref()
            .is_some_and(|g| g.get("state") == Some(&json!("granted")));
        if !target || !running || left || closed || !granted {
            host.close_work();
            if left || closed {
                self.close(host);
            }
            return;
        }
        let context = host.accept_context();
        // `auto_poll_step` (standing_consent.py:357-361 @ f4e366e9): nothing is reported, accepted
        // or ticked unless this Mac is a current member (`not_member`) whose standing consent is
        // bound to THIS membership (`standing_consent_owner_mismatch`). A host that gives no
        // acceptor context (the headless launcher's) keeps its own gates.
        if let Some(context) = &context {
            let Some(binding) = &context.binding else {
                host.close_work();
                return;
            };
            if grant.as_ref().and_then(|g| g.get("binding")) != Some(binding) {
                // Silent before: a grant bound to an earlier membership stopped every pass here,
                // so a joined Mac produced no card and no report while looking healthy.
                eprintln!("l11_member_gate:consent_binding_mismatch");
                host.close_work();
                return;
            }
        }
        let client = match self.client(host, &root) {
            Ok(client) => client,
            Err(error) => {
                eprintln!("member agent: {}", error.0);
                return;
            }
        };
        if host.work_enabled() {
            let publication = match client.lock() {
                Ok(mut exchange) => {
                    crate::run_exchange::owner_pin::VerifiedAnnouncement::read(&mut exchange)
                        .map_err(|e| e.0)
                        .and_then(|a| host.work_announcement(a))
                        .and_then(|d| match d {
                            Some(d) => crate::member_control::joined::inbox::publish(&exchange, &d)
                                .map_err(|e| e.code.to_owned()),
                            None => Ok(()),
                        })
                }
                Err(_) => Err("member_work_exchange_poisoned".into()),
            };
            if let Err(code) = publication {
                host.close_work();
                eprintln!("member_work_publication:{code}");
                // `auto_poll_step` (standing_consent.py:357-363) reports BEFORE it accepts, and a
                // refusal to accept work never suppresses the report. Returning here was the v0.2
                // divergence: a joined Mac whose resource policy denies work (a battery-only Mac
                // with `ac_only`) then never reported in at all, so the owner's run waited on
                // "Joined Macs report in" forever while that Mac looked healthy on its own page.
            }
        }
        // `if report_due: outcome['report'] = app.auto_report('periodic')` (standing_consent.py:
        // 362-363), past the gates above, before the acceptor and the agent; `_auto_report` never
        // raises (app.py:1138-1163).
        if let Some(context) = &context
            && host.periodic_report_due()
        {
            let record =
                crate::joined_member::auto_report(&root, context.join_base.as_deref(), "periodic");
            host.reported(record);
        }
        // `auto_accept` before `auto_steps` (standing_consent.py:363-365); its exception ends the
        // pass (`_standing_loop` records it as `standing_error`, app.py:1210-1212).
        if let Some(context) = context {
            let holds = self.agent.as_mut().is_some_and(LocalAgent::holds_offer);
            let acceptor = self.acceptor.get_or_insert_with(|| {
                MemberAcceptor::new(root.clone(), self.announcement.clone())
            });
            let mut work = MemberWork {
                host: &**host,
                client: client.clone(),
                root: root.clone(),
                join_base: context.join_base.clone(),
            };
            let outcome = acceptor.tick(
                &mut work,
                context.policy.as_ref(),
                context.binding.as_ref(),
                holds,
                &mut || host.resource_check(),
            );
            if let Err(failure) = outcome {
                eprintln!(
                    "member standing acceptor: {}",
                    failure.code.as_deref().unwrap_or("standing_accept_failed")
                );
                // Lane CHATPAGE patch 2 (CP-03): a pending-offer read the owner could not answer
                // (retryable: no offer was read, nothing was accepted) does not end the pass
                // before the agent. v0.2's owner answers a joined Mac's mailbox only under the
                // descriptor that Mac reported for the CURRENT run (`owner_observation` `guarded`;
                // v0.1.4's `_member_guard`, managed_control.py:909-941 @ f4e366e9, checks the
                // membership only), and the agent is what reads the next run's announcement and
                // reports that descriptor (demo_target.py:437-470). Once the agent's last
                // announcement no longer holds the offer (`stopped`, `idle`), ending the pass here
                // kept the agent from ever reading the next run: the joined Mac never reported
                // into it (merge-chat2 ten-row gate, run2 row 3 `two_macs_required`). Any other
                // acceptor failure still ends the pass, as `auto_accept`'s exception does.
                if !failure.retryable {
                    return;
                }
            }
        }
        match host.work_report() {
            Ok(Some(report)) => {
                let sent = locked(&client)
                    .and_then(|mut c| c.report(&report).map(drop).map_err(client_error));
                if let Err(e) = sent {
                    eprintln!("member_work_report_failed:{}", e.0);
                }
                return;
            }
            Ok(None) => {}
            Err(code) => {
                host.close_work();
                eprintln!("member_work_report_failed:{code}");
                return;
            }
        }
        let agent = self.agent.get_or_insert_with(LocalAgent::remote);
        let mut view = MemberView {
            host,
            client,
            announcement: self.announcement.clone(),
            root,
        };
        agent.tick(&mut view);
    }

    /// The agent's card after its last completed pass (`status_snapshot`); none before the first.
    pub fn status(&self) -> Option<Value> {
        self.agent.as_ref().map(LocalAgent::status)
    }

    /// Leave / quit: no worker survives this Mac's membership.
    pub fn close(&mut self, host: &Arc<H>) {
        host.close_work();
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
        // `_ensure_whole_model` (demo_target.py:594-607): this Mac's own acquisition must be the
        // announced model. A Mac left holding an acquisition for a different run model used to keep
        // it and answer `demo_model_mismatch` forever, because the reopen branch below only ran when
        // this Mac had no acquisition at all. v0.1.4 posted the announced choice to `/api/run-model`
        // and reopened its acquisition, so the announced model replaced whatever was held.
        let mut current = self.acquisition().ok();
        let held = current.as_ref().map(|acquisition| {
            let model = acquisition.model();
            (model.model_id.to_owned(), model.resolved_commit.to_owned())
        });
        if held.as_ref() != Some(&want) {
            // `_ensure_whole_model` (demo_target.py:600-607): with no acquisition of its own, or one
            // for another model, a joined Mac STARTS one for the announced model and reports
            // progress. v0.2 refused here instead, so a member could never serve and the owner's
            // run waited on "Joined Macs report in" for a Mac that was healthy but starving.
            let choice = a["model"]["choice"].clone();
            return match self.host.start_whole_model(&choice) {
                Ok(()) => waiting("downloading_model", None, None),
                Err(code) => waiting("error", Some(&code), None),
            };
        }
        let acquisition = current
            .take()
            .unwrap_or_else(|| unreachable!("checked above"));
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
                // The acquisition's snapshot is gone (a wiped or relocated cache). `_ensure_whole_model`
                // has no give-up case: a state that is neither prepared nor acquiring starts again
                // from the pinned source, so a joined Mac re-fetches instead of refusing the run.
                let choice = a["model"]["choice"].clone();
                match self.host.start_whole_model(&choice) {
                    Ok(()) => waiting("downloading_model", None, None),
                    Err(code) => waiting("error", Some(&code), None),
                }
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
    fn member_caps(&mut self) -> Result<Value, Failure> {
        standing_member_caps(&self.root)
    }
    fn storage_cap(&mut self) -> Result<u64, Failure> {
        standing_storage_cap(&self.root)
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

/// `StandingConsentGate.member_caps(app)` (demo_target.py:74-78 @ f4e366e9): the standing caps a
/// joined Mac's announced share must fit (storage, memory, wall).
pub fn standing_member_caps(root: &Path) -> Result<Value, Failure> {
    let grant = standing_consent::load(root)
        .ok()
        .flatten()
        .ok_or_else(|| Failure::new("TypeError", "demo_step_failed"))?;
    let mut caps = Map::new();
    for key in ["storage_bytes", "max_memory_bytes", "max_wall_seconds"] {
        let value = grant["caps"][key]
            .as_u64()
            .ok_or_else(|| Failure::new("KeyError", "demo_step_failed"))?;
        caps.insert(key.into(), json!(value));
    }
    Ok(Value::Object(caps))
}
/// `StandingConsentGate.storage_cap(app)` (demo_target.py:69-72).
fn standing_storage_cap(root: &Path) -> Result<u64, Failure> {
    standing_member_caps(root)?["storage_bytes"]
        .as_u64()
        .ok_or_else(|| Failure::new("KeyError", "demo_step_failed"))
}

/// A joined Mac's automatic work acceptance: `DemoApp.auto_accept` (app.py:1175-1189 @ f4e366e9)
/// over `StandingAcceptor` (standing_consent.py:375-470), whose `limits_for` is the agent's
/// `member_limits(offer)` (demo_target.py:342-349): the owner's announced `work_limits` for the
/// offer's run, read from the announcement the agent last read.
pub struct MemberAcceptor {
    acceptor: StandingAcceptor,
}
impl MemberAcceptor {
    pub fn new(root: PathBuf, announcement: Arc<Mutex<Value>>) -> Self {
        let limits_for = move |offer: &Value| {
            let a = announcement
                .lock()
                .map(|a| a.clone())
                .unwrap_or(Value::Null);
            (offer.is_object()
                && offer.get("run_id") == a.get("run_id")
                && a["work_limits"].is_object())
            .then(|| a["work_limits"].clone())
        };
        Self {
            acceptor: StandingAcceptor::new(root, Some(Box::new(limits_for))),
        }
    }
    /// One `auto_accept`: nothing without a policy or a current binding, nothing for an offer
    /// the agent holds (`holds_offer`: a bootstrap offer, a slice not verified yet), else the
    /// standing acceptor's outcome for the pending offer.
    pub fn tick(
        &mut self,
        work: &mut dyn WorkAcceptance,
        policy: Option<&Value>,
        binding: Option<&Value>,
        holds_offer: bool,
        resource_check: &mut dyn FnMut() -> Option<String>,
    ) -> Result<Option<Value>, AcceptFailure> {
        let (Some(policy), Some(binding)) = (policy, binding) else {
            return Ok(None);
        };
        if holds_offer {
            return Ok(None);
        }
        self.acceptor
            .tick(work, policy, Some(binding), resource_check)
    }
}
/// The member's own collaborators of the standing acceptor (`StandingAcceptor.tick(self.authority,
/// ...)`): its pending offer from the owner's mailbox, its pre-run report (`_auto_report`), and
/// the host's work preview/accept.
struct MemberWork<'a, H: MemberHost> {
    host: &'a H,
    client: Client,
    root: PathBuf,
    join_base: Option<String>,
}
impl<H: MemberHost> WorkAcceptance for MemberWork<'_, H> {
    /// `actor.pending_stage()`: an owner whose mailbox does not exist yet has published nothing.
    fn pending_offer(&mut self) -> Result<Option<Value>, AcceptFailure> {
        let body = match stage_call(
            &self.client,
            STAGE_READ,
            json!({"offer_id": null, "offset": 0}),
        ) {
            Err(OwnerError("managed_owner_unavailable")) => return Ok(None),
            Err(error) => {
                return Err(AcceptFailure {
                    retryable: true,
                    ..AcceptFailure::code(error.0)
                });
            }
            Ok(body) => body,
        };
        Ok((!body["offer"].is_null()).then(|| body["offer"].clone()))
    }
    /// `report=lambda purpose, _offer: self._auto_report(purpose)`: best effort, never raises.
    fn report(&mut self, purpose: &str, _offer: &Value) -> Result<(), AcceptFailure> {
        let record =
            crate::joined_member::auto_report(&self.root, self.join_base.as_deref(), purpose);
        if record["state"] != "delivered" {
            eprintln!("member {purpose} report: {}", record["error"]);
        }
        // `_auto_report` records every report it makes (app.py:1161-1162).
        self.host.reported(record);
        Ok(())
    }
    fn preview(
        &mut self,
        offer: &Value,
        limits: &Value,
        policy: &Value,
    ) -> Result<Value, AcceptFailure> {
        self.host.work_preview(offer, limits, policy)
    }
    fn accept(
        &mut self,
        preview: &Value,
        limits: &Value,
        policy: &Value,
    ) -> Result<(), AcceptFailure> {
        self.host.work_accept(preview, limits, policy)
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
    settle_finished(&**host, &scope)?;
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

/// PR-ASTRA-02: settle an earlier run's FINISHED worker before a new run is admitted.
///
/// v0.1.4's bootstrap branch (demo_target.py:495-512 @ f4e366e9) meets `stage_runtime_active`
/// when the previous run's runtime is still attached, calls `/api/control/stop` (`stop_control`
/// closes and detaches it, target_app.py:280-284) and stages again. Here the pump thread of an
/// earlier scope can end by itself on a terminal step error AFTER its own close removed the stage
/// and ACKed the owner's cleanup: no cleanup request will ever close it again, so its finished
/// worker would otherwise refuse every later Start. It is settled through the worker's own
/// close/retry: the first close joins its thread (returning that thread's terminal error once),
/// the second retries the failure the join retained (`PumpWorker::close`).
///
/// Genuine debt still blocks: a RUNNING worker, a worker of this same scope and a start-failure
/// latch are left to [`start`]'s own refusals, and a worker whose cleanup still fails stays in
/// the slot and its error is returned.
fn settle_finished<H: MemberHost>(host: &H, scope: &Value) -> OResult<()> {
    let lifecycle = host.lifecycle()?;
    let _lifecycle = locked(&lifecycle)?;
    let finished = host.slot(|s| {
        let obsolete = s.failure.is_none()
            && s.worker
                .as_ref()
                .is_some_and(|w| !w.running() && w.scope() != scope);
        if obsolete {
            *s.incarnation = Arc::new(());
            s.worker.take()
        } else {
            None
        }
    })?;
    let Some(mut worker) = finished else {
        return Ok(());
    };
    let settled = match worker.close() {
        Ok(()) => Ok(()),
        Err(ended) => {
            eprintln!("member target settling a finished worker: {}", ended.0);
            worker.close()
        }
    };
    match settled {
        Ok(()) => Ok(()),
        Err(error) => {
            host.slot(|s| *s.worker = Some(worker))?;
            Err(error)
        }
    }
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

// ---- a target pump worker for hosts without their own (the packaged host). The same cadence as
// the headless launcher's `observation::target` worker (f4 managed_runtime.py:155,447-479).

const DELIVERY_WAKE: BudgetId = BudgetId::PyManagedRuntimeDeliveryWakeCeilingL461C16;

/// A member pump's delivery wake (nothing notifies a member pump but its own Stop).
#[derive(Default)]
struct Wake {
    generation: Mutex<u64>,
    changed: std::sync::Condvar,
}
impl Wake {
    fn notify(&self) -> OResult<()> {
        let updated = locked(&self.generation).map(|mut g| *g = g.wrapping_add(1));
        self.changed.notify_all();
        updated
    }
    fn generation(&self) -> OResult<u64> {
        Ok(*locked(&self.generation)?)
    }
    fn wait(&self, generation: u64, fence: &TargetFence) -> OResult<()> {
        let g = locked(&self.generation)?;
        let ceiling = DELIVERY_WAKE
            .span(Entries::ZERO)
            .map_err(|_| OwnerError("budget_unavailable"))?;
        let (g, _) = self
            .changed
            .wait_timeout_while(g, std::time::Duration::from_millis(ceiling.millis()), |g| {
                *g == generation && fence.check().is_ok()
            })
            .map_err(|_| OwnerError("target_wake_poisoned"))?;
        drop(g);
        fence.check()
    }
}
struct Cadence {
    retry: Span,
}
impl Cadence {
    fn new() -> OResult<Self> {
        Ok(Self {
            retry: Self::span(
                BudgetId::PyFinalMyceliumA13MacosManagedRuntimeSelfOutboundRetryBackoffL155C8,
            )?,
        })
    }
    fn span(id: BudgetId) -> OResult<Span> {
        id.span(Entries::ZERO)
            .map_err(|_| OwnerError("budget_unavailable"))
    }
    fn delay(&mut self, retryable: bool) -> OResult<Span> {
        if retryable {
            let budgets::table::Value::Ratio(ratio) =
                BudgetId::PyManagedRuntimeOutboundRetryMultiplierL463C93
                    .entry()
                    .value
            else {
                return Err(OwnerError("budget_unavailable"));
            };
            self.retry = self
                .retry
                .checked_mul(ratio.get())
                .and_then(|v| v.checked_div(1000))
                .map_err(|_| OwnerError("budget_unavailable"))?
                .max(Self::span(
                    BudgetId::PyFinalMyceliumA13MacosManagedRuntimeL463C61ComparisonLower,
                )?)
                .min(Self::span(
                    BudgetId::PyFinalMyceliumA13MacosManagedRuntimeL463C53CeilingOperand,
                )?);
            Ok(self.retry)
        } else {
            self.retry = Self::span(
                BudgetId::PyFinalMyceliumA13MacosManagedRuntimeSelfOutboundRetryBackoffL471C20,
            )?;
            Self::span(DELIVERY_WAKE)
        }
    }
    fn wait(
        &mut self,
        outcome: StepOutcome,
        wake: &Wake,
        generation: u64,
        fence: &TargetFence,
    ) -> OResult<()> {
        let retryable = matches!(outcome, StepOutcome::OutboundRetryable { .. });
        let delay = self.delay(retryable)?;
        if retryable {
            // Source469: ONLY Stop can shorten this wait, never delivery wake.
            fence.wait_for(delay)
        } else {
            wake.wait(generation, fence)
        }
    }
}
/// A member's started pump driven on its own thread ([`spawn_pump`]).
pub struct PumpWorker {
    scope: Value,
    fence: TargetFence,
    process: ProcessControl,
    wake: Arc<Wake>,
    join: Option<JoinHandle<Result<(), CleanupFailure>>>,
    failure: Option<CleanupFailure>,
}
impl MemberWorker for PumpWorker {
    fn running(&self) -> bool {
        self.join.as_ref().is_some_and(|j| !j.is_finished())
    }
    fn scope(&self) -> &Value {
        &self.scope
    }
    fn close(&mut self) -> OResult<()> {
        let cancelled = self.fence.cancel();
        let notified = self.wake.notify();
        let stopped = self.process.cancel().map_err(OwnerError);
        // A failure returned by this join has already attempted cleanup. Retry
        // it only on the next explicit close, never twice in one invocation.
        let retrying = self.join.is_none();
        let joined = match self.join.take() {
            Some(join) => match join
                .join()
                .map_err(|_| OwnerError("target_worker_panicked"))?
            {
                Ok(()) => Ok(()),
                Err(failure) => {
                    let code = failure.code();
                    self.failure = Some(failure);
                    Err(OwnerError(code))
                }
            },
            None => Ok(()),
        };
        let retried = if retrying {
            match self.failure.as_mut() {
                Some(failure) => failure.retry().map_err(OwnerError),
                None => Ok(()),
            }
        } else {
            Ok(())
        };
        if retrying && retried.is_ok() {
            self.failure.take();
        }
        cancelled
            .and(notified)
            .and(stopped)
            .and(joined)
            .and(retried)
    }
}
impl Drop for PumpWorker {
    fn drop(&mut self) {
        if let Err(e) = MemberWorker::close(self) {
            eprintln!("member target worker close: {}", e.0);
        }
    }
}
/// Drive a started pump on its own thread until Stop (or its stop command) and close it there.
pub fn spawn_pump<T: TargetHost + 'static>(
    mut pump: TargetPump<T>,
    fence: TargetFence,
    scope: Value,
) -> PumpWorker {
    let wake = Arc::new(Wake::default());
    let process = pump.process_control();
    let worker_fence = fence.clone();
    let owner_wake = wake.clone();
    let join = std::thread::spawn(move || {
        let result: OResult<()> = (|| {
            let mut cadence = Cadence::new()?;
            loop {
                let generation = wake.generation()?;
                let outcome = pump.step_outcome().map_err(OwnerError)?;
                if matches!(outcome, StepOutcome::Completed { stopped: true }) {
                    while worker_fence.check().is_ok() {
                        let g = wake.generation()?;
                        wake.wait(g, &worker_fence)?;
                    }
                    break;
                }
                cadence.wait(outcome, &wake, generation, &worker_fence)?;
            }
            Ok(())
        })();
        let recorded = if let Err(e) = &result {
            pump.record_failure(e.0)
        } else {
            Ok(())
        };
        if let Err(cleanup) = pump.close() {
            let control = pump.process_control();
            return Err(CleanupFailure::owned(
                cleanup,
                cleanup,
                control,
                move || pump.close(),
            ));
        }
        recorded.map_err(CleanupFailure::from)?;
        if worker_fence.check().is_err() {
            Ok(())
        } else {
            result.map_err(|e| CleanupFailure::from(e.0))
        }
    });
    PumpWorker {
        scope,
        fence,
        process,
        wake: owner_wake,
        join: Some(join),
        failure: None,
    }
}
/// The hello-only program this installation runs: `bootstrap-program.json` when an operator admitted
/// one, else the model-free child sealed into the bundle beside this executable.
pub fn bootstrap_program(root: &Path) -> OResult<TrustedBootstrap> {
    crate::bootstrap_process::installed_program(root).map_err(OwnerError)
}
#[allow(unsafe_code)] // geteuid(2) has no preconditions
fn effective_uid() -> u32 {
    unsafe { libc::geteuid() }
}
/// `installed_sidecar()` (`native_runtime.py:8-22`): the native sidecar beside the executable
/// (`<exe dir>/native/mycelium-iroh-sidecar`), a regular file this user owns, executable and not
/// writable by others.
pub fn installed_sidecar() -> Result<PathBuf, Failure> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    let invalid = || Failure::new("ValueError", "native_sidecar_installation_invalid");
    let candidate = std::env::current_exe()
        .map_err(|_| invalid())?
        .parent()
        .ok_or_else(invalid)?
        .join("native")
        .join("mycelium-iroh-sidecar");
    let path = fs::canonicalize(candidate).map_err(|_| invalid())?;
    let meta = fs::symlink_metadata(&path).map_err(|_| invalid())?;
    let mode = meta.permissions().mode();
    if !meta.is_file() || meta.uid() != effective_uid() || mode & 0o100 == 0 || mode & 0o022 != 0 {
        return Err(invalid());
    }
    Ok(path)
}

#[cfg(test)]
mod settle_tests {
    //! PR-ASTRA-02: which slot contents the next run's admission settles, and which still block.
    use super::*;
    use std::collections::VecDeque;
    struct Fake {
        running: bool,
        scope: Value,
        closes: Arc<Mutex<VecDeque<OResult<()>>>>,
    }
    impl MemberWorker for Fake {
        fn running(&self) -> bool {
            self.running
        }
        fn scope(&self) -> &Value {
            &self.scope
        }
        fn close(&mut self) -> OResult<()> {
            locked(&self.closes)?.pop_front().unwrap_or(Ok(()))
        }
    }
    #[derive(Default)]
    struct Host {
        lifecycle: Arc<Mutex<()>>,
        slot: Mutex<(Option<Fake>, Option<CleanupFailure>, Arc<()>)>,
    }
    impl MemberHost for Host {
        type Worker = Fake;
        fn state(&self) -> OResult<HostState> {
            Ok(HostState {
                root: PathBuf::new(),
                target: true,
                running: true,
                left: false,
                closed: false,
            })
        }
        fn lease(&self) -> Option<f64> {
            None
        }
        fn resource_check(&self) -> Option<String> {
            None
        }
        fn acquisition(&self) -> Option<Acquisition> {
            None
        }
        fn member_seed_options(&self) -> SeedOptions {
            SeedOptions::default()
        }
        fn installed_sidecar(&self) -> Result<PathBuf, Failure> {
            Err(Failure::new("ValueError", "test_no_sidecar"))
        }
        fn lifecycle(&self) -> OResult<Arc<Mutex<()>>> {
            Ok(self.lifecycle.clone())
        }
        fn slot<R>(&self, f: impl FnOnce(Slot<'_, Self::Worker>) -> R) -> OResult<R> {
            let mut slot = locked(&self.slot)?;
            let (worker, failure, incarnation) = &mut *slot;
            Ok(f(Slot {
                worker,
                failure,
                incarnation,
                state: self.state()?,
            }))
        }
        fn program(&self, _root: &Path) -> OResult<TrustedBootstrap> {
            Err(OwnerError("test_no_program"))
        }
        fn spawn(
            &self,
            _pump: TargetPump<RemoteTarget<Self>>,
            _fence: TargetFence,
            _scope: Value,
        ) -> Self::Worker {
            unreachable!("settlement never starts a worker")
        }
    }
    fn run(id: &str) -> Value {
        json!({"run_id": id, "deployment_id": format!("dep-{id}")})
    }
    /// A worker in the slot whose closes answer `closes` in order; returns the close script.
    fn holding(
        host: &Host,
        running: bool,
        scope: Value,
        closes: Vec<OResult<()>>,
    ) -> Arc<Mutex<VecDeque<OResult<()>>>> {
        let script = Arc::new(Mutex::new(VecDeque::from(closes)));
        locked(&host.slot).unwrap().0 = Some(Fake {
            running,
            scope,
            closes: script.clone(),
        });
        script
    }
    fn held(host: &Host) -> bool {
        locked(&host.slot).unwrap().0.is_some()
    }
    #[test]
    fn an_earlier_runs_finished_worker_is_closed_twice_and_leaves_the_slot() {
        let host = Host::default();
        // Its thread ended on a terminal error after its own close ACKed the cleanup: the first
        // close joins and returns that error, the second retries the retained failure.
        let script = holding(
            &host,
            false,
            run("a"),
            vec![Err(OwnerError("offer_transfer_not_authorized")), Ok(())],
        );
        assert_eq!(settle_finished(&host, &run("b")), Ok(()));
        assert!(!held(&host));
        assert!(locked(&script).unwrap().is_empty(), "both closes ran");
    }
    #[test]
    fn genuine_cleanup_debt_stays_in_the_slot_and_still_refuses() {
        let host = Host::default();
        let script = holding(
            &host,
            false,
            run("a"),
            vec![
                Err(OwnerError("bootstrap_stage_cleanup_unproven")),
                Err(OwnerError("bootstrap_stage_cleanup_unproven")),
                Ok(()),
            ],
        );
        assert_eq!(
            settle_finished(&host, &run("b")),
            Err(OwnerError("bootstrap_stage_cleanup_unproven"))
        );
        assert!(held(&host), "unsettled debt keeps its owner");
        assert_eq!(locked(&script).unwrap().len(), 1, "two closes, no more");
    }
    #[test]
    fn a_running_worker_a_same_run_worker_and_a_start_failure_are_left_alone() {
        for (running, scope) in [(true, run("a")), (false, run("b"))] {
            let host = Host::default();
            let script = holding(&host, running, scope, vec![Err(OwnerError("never_closed"))]);
            assert_eq!(settle_finished(&host, &run("b")), Ok(()));
            assert!(held(&host));
            assert_eq!(locked(&script).unwrap().len(), 1, "not closed");
        }
        let host = Host::default();
        let script = holding(
            &host,
            false,
            run("a"),
            vec![Err(OwnerError("never_closed"))],
        );
        locked(&host.slot).unwrap().1 = Some(CleanupFailure::from(
            "control_restart_requires_reconciliation",
        ));
        assert_eq!(settle_finished(&host, &run("b")), Ok(()));
        assert!(held(&host));
        assert_eq!(locked(&script).unwrap().len(), 1, "not closed");
    }
    #[test]
    fn a_same_run_worker_of_another_deployment_or_incarnation_is_left_alone() {
        // PR-ASTRA-05: only a finished worker of a DIFFERENT, validated run_id is settled. A
        // worker of the same run under another deployment or member incarnation, or a scope
        // without a usable run_id, keeps blocking exactly as before (worker and close untouched).
        fn scope(run: Value, deployment: &str, incarnation: &str) -> Value {
            json!({"run_id": run, "deployment_id": deployment, "member_incarnation": incarnation})
        }
        let new = scope(json!("run-a"), "dep-2", "inc-1");
        for (old, wanted) in [
            (scope(json!("run-a"), "dep-1", "inc-1"), new.clone()),
            (scope(json!("run-a"), "dep-2", "inc-0"), new.clone()),
            (scope(Value::Null, "dep-1", "inc-1"), new.clone()),
            (scope(json!(""), "dep-1", "inc-1"), new.clone()),
            (scope(json!(7), "dep-1", "inc-1"), new.clone()),
            (
                scope(json!("run-a"), "dep-1", "inc-1"),
                scope(Value::Null, "dep-2", "inc-1"),
            ),
            (
                scope(json!("run-a"), "dep-1", "inc-1"),
                scope(json!(""), "dep-2", "inc-1"),
            ),
        ] {
            let host = Host::default();
            let script = holding(
                &host,
                false,
                old.clone(),
                vec![Err(OwnerError("never_closed"))],
            );
            assert_eq!(settle_finished(&host, &wanted), Ok(()), "{old} -> {wanted}");
            assert!(held(&host), "{old} -> {wanted}: the worker stays");
            assert_eq!(
                locked(&script).unwrap().len(),
                1,
                "{old} -> {wanted}: not closed"
            );
        }
    }
}
