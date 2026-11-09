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
//! NOT ported here: the whole-snapshot download a joined Mac starts in whole mode (S1; a member
//! whose acquisition has no snapshot on disk reports `member_model_download_not_ported`; one on
//! disk is re-hashed, never downloaded), the periodic
//! capability report (`auto_report`), and work acceptance (the standing acceptor; row 3 (d)).
use super::*;
use api::{
    bootstrap_stage::BootstrapStage,
    local_agent::{ExchangeUnavailable, WholeModel},
    managed_carrier::{HostClock, TargetFence},
    managed_routes::path_for,
    run_exchange_client::{ClientError, ExchangeClient, Membership},
    seed::SeedClient,
    target_pump::{TargetHost, TargetPump},
};
use budgets::MonoInstant;
use observation::target::{self, Wake};
use runtime::managed_owner::OwnerError;
use transport::Clock as _;

type OResult<T> = Result<T, OwnerError>;
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

/// This member's retained membership (`membership.state.json`), the source of its binding.
fn membership(root: &Path) -> OResult<Map<String, Value>> {
    api::peer::load_membership_state(root)
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
#[derive(Default)]
pub(super) struct MemberAgent {
    agent: Option<LocalAgent>,
    client: Option<(Value, Client)>,
    announcement: Arc<Mutex<Value>>,
}

/// `StandingConsentGate.allows` (`demo_target.py:44-66`): the join lane's grant, bound to THIS
/// membership, then the resource gates.
fn standing_refusal(core: &Shared) -> Option<String> {
    grant_refusal(core).or_else(|| resource_check(core).map(str::to_owned))
}
/// The grant half of the gate (file reads only): the target pump checks it on every exchange; the
/// resource observation (a probe per call) is the agent's, once per standing-loop pass.
fn grant_refusal(core: &Shared) -> Option<String> {
    let root = core.lock().ok()?.root.clone();
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

impl MemberAgent {
    /// `RunExchangeClient(app.authority.peer)` for the CURRENT membership.
    fn client(&mut self, root: &Path) -> OResult<Client> {
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
        let origin = api::join_link::canonical_https_origin(&json!(text("seed_url")?))
            .map_err(|_| OwnerError("peer_seed_origin_invalid"))?;
        let seed = SeedClient::new(
            &origin,
            &text("swarm_id")?,
            &text("seed_key_digest")?,
            &records,
            &member_seed_options(),
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
    pub(super) fn tick(&mut self, core: &Shared) {
        let Ok((root, target, running, left, closed)) = core
            .lock()
            .map(|c| (c.root.clone(), c.target, c.running, c.left, c.closed))
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
                self.close(core);
            }
            return;
        }
        let client = match self.client(&root) {
            Ok(client) => client,
            Err(error) => {
                eprintln!("member agent: {}", error.0);
                return;
            }
        };
        let agent = self.agent.get_or_insert_with(LocalAgent::remote);
        let mut view = MemberView {
            core,
            client,
            announcement: self.announcement.clone(),
            root,
        };
        agent.tick(&mut view);
    }

    /// Leave / quit: no worker survives this Mac's membership.
    pub(super) fn close(&mut self, core: &Shared) {
        if let Some(agent) = self.agent.as_mut() {
            agent.close();
        }
        if let Err(error) = close_worker(core) {
            eprintln!("member target close: {}", error.0);
        }
    }
}

/// Close this Mac's bootstrap worker (its pump removes the stage and ACKs the cleanup).
fn close_worker(core: &Shared) -> OResult<()> {
    let lifecycle = locked(core)?.bootstrap_lifecycle.clone();
    let _lifecycle = locked(&lifecycle)?;
    let (worker, failure) = {
        let mut c = locked(core)?;
        c.bootstrap_incarnation = Arc::new(());
        (c.bootstrap.take(), c.bootstrap_failure.take())
    };
    let mut result = Ok(());
    if let Some(mut worker) = worker
        && let Err(error) = worker.close()
    {
        locked(core)?.bootstrap = Some(worker);
        result = Err(error);
    }
    if let Some(mut failure) = failure
        && let Err(code) = failure.retry()
    {
        locked(core)?.bootstrap_failure = Some(failure);
        result = Err(OwnerError(code));
    }
    result
}

struct MemberView<'a> {
    core: &'a Shared,
    client: Client,
    announcement: Arc<Mutex<Value>>,
    root: PathBuf,
}

impl MemberView<'_> {
    fn acquisition(&self) -> Result<Acquisition, Failure> {
        self.core
            .lock()
            .ok()
            .and_then(|c| c.acquisition.clone())
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
        && api::run_exchange_client::BINDING
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

impl AgentHost for MemberView<'_> {
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
        standing_refusal(self.core)
    }
    fn resource_check(&mut self) -> Option<String> {
        resource_check(self.core).map(str::to_owned)
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
        if let Some(reason) = standing_refusal(self.core) {
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
        let lease = self.core.lock().ok().and_then(|c| {
            c.session_view.and_then(|(_, lease)| lease).or_else(|| {
                c.session
                    .as_ref()
                    .and_then(api::member::Session::lease_expires_at)
            })
        });
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
            sidecar_binary: installed_sidecar()?.to_string_lossy().into_owned(),
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
            .core
            .lock()
            .is_ok_and(|c| c.bootstrap.is_some() || c.bootstrap_failure.is_some());
        close_worker(self.core).map_err(fail)?;
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
        close_worker(self.core).map_err(fail)
    }
    fn native_control(&mut self) -> Result<Value, Failure> {
        let c = self
            .core
            .lock()
            .map_err(|_| Failure::new("RuntimeError", "demo_step_failed"))?;
        Ok(match (&c.bootstrap, &c.bootstrap_failure) {
            (Some(worker), _) if worker.running() => json!({"state": "running"}),
            (_, Some(failure)) => json!({"state": "blocked", "error_code": failure.code()}),
            (Some(_), None) => json!({"state": "stopped"}),
            (None, None) => json!({}),
        })
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
fn bootstrap(view: &MemberView<'_>, run: &str, deployment: &str, offer_id: &str) -> OResult<()> {
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

fn start(view: &MemberView<'_>, scope: Value, offer: Value, archive: Vec<u8>) -> OResult<()> {
    let core = view.core;
    let lifecycle = locked(core)?.bootstrap_lifecycle.clone();
    let _lifecycle = locked(&lifecycle)?;
    {
        let c = locked(core)?;
        if c.bootstrap_failure.is_some() {
            return Err(OwnerError("control_restart_requires_reconciliation"));
        }
        if let Some(worker) = c.bootstrap.as_ref() {
            return if *worker.scope() == scope && worker.running() {
                Ok(())
            } else {
                Err(OwnerError("stage_runtime_active"))
            };
        }
        if c.closed || !c.running || !c.target || c.left {
            return Err(OwnerError("bootstrap_start_fenced"));
        }
    }
    let program = target::program(&view.root)?;
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
    let host = RemoteTarget {
        core: Arc::downgrade(core),
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
        host,
        scope.clone(),
        offer,
        stage,
        &program,
        fence.clone(),
    ) {
        Ok(pump) => pump,
        Err(failure) => {
            let code = failure.code();
            locked(core)?.bootstrap_failure = Some(failure);
            return Err(OwnerError(code));
        }
    };
    let mut worker = target::spawn(pump, fence, Arc::new(Wake::default()), scope);
    let mut c = locked(core)?;
    if c.bootstrap.is_some() || c.closed || !c.running || !c.target || c.left {
        drop(c);
        worker.close()?;
        return Err(OwnerError("bootstrap_start_fenced"));
    }
    c.bootstrap = Some(worker);
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
struct RemoteTarget {
    core: std::sync::Weak<Mutex<Core>>,
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
impl transport::Clock for RemoteTarget {
    fn monotonic(&self) -> MonoInstant {
        self.clock.monotonic()
    }
    fn wall_seconds(&self) -> f64 {
        self.clock.wall_seconds()
    }
}
impl RemoteTarget {
    /// The membership the pump was started for is still this Mac's, and (`active`) the owner still
    /// announces this run under a standing consent that allows it.
    fn current(&self, active: bool) -> OResult<()> {
        let core = self
            .core
            .upgrade()
            .ok_or(OwnerError("managed_owner_closed"))?;
        // A removal ACK (cleanup) is still owed while this process quits or leaves.
        if active {
            let c = locked(&core)?;
            if c.closed || !c.running || !c.target || c.left {
                return Err(OwnerError("managed_owner_closed"));
            }
        }
        let m = membership(&self.root)?;
        if m.get("node_id") != Some(&self.scope["target_node_id"])
            || m.get("membership_generation") != Some(&self.scope["membership_generation"])
            || m.get("incarnation") != Some(&self.scope["member_incarnation"])
        {
            return Err(OwnerError("managed_control_member_stale"));
        }
        if active {
            if let Some(code) = grant_refusal(&core) {
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
impl TargetHost for RemoteTarget {
    fn check(&self, scope: &Value) -> api::target_pump::Result<()> {
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
    fn nonce(&self) -> api::target_pump::Result<String> {
        nonce().ok_or("managed_control_entropy_unavailable")
    }
    fn exchange(
        &self,
        envelope: &Value,
        _end: MonoInstant,
        fence: &TargetFence,
    ) -> api::target_pump::Result<Value> {
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
    fn stage_ack(&self, offer: &Value, root: &Path, removed: bool) -> api::target_pump::Result<()> {
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
