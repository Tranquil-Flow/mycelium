// SPDX-License-Identifier: AGPL-3.0-only
//! Desktop action backend that owns the private preference files: `/api/fast-mode`
//! (fast_mode.py `choose`) and `/api/run-model` (app.py `_choose_run_model`) persist exactly
//! as v0.1.4 (app.py:1356-1361 @ f4e366e9); every other action is delegated untouched, so the
//! host's own backend keeps its authority, relay and serving effects. The host supplies the
//! facts the source read from `os.environ`/the app object and the effects it triggered
//! (process-default refresh, acquisition re-open).
use crate::{
    actions::{Action, Backend},
    preferences::{fast_mode, resource_policy, run_model, standing_consent},
    schema::PublicError,
    wire::Json,
};
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value};
use std::path::PathBuf;

/// `MYCELIUM_TOKEN_PATH`: the operator override of fast mode (fast_mode.ENV).
pub const TOKEN_PATH_ENV: &str = "MYCELIUM_TOKEN_PATH";

/// Facts and effects the desktop host owns.
/// `MAX_CLOCK_SKEW_SECONDS` (`mycelium_membership.contracts`), from the budget registry.
fn max_clock_skew_seconds() -> f64 {
    use budgets::{BudgetId, Entries};
    BudgetId::PyMyceliumMembershipContractsMaxClockSkewSecondsL31
        .span(Entries::new(0))
        .map_or(0.0, |s| s.millis() as f64 / 1000.0)
}
/// `seed_time_window`: only the allowlisted keys, each a finite float or `null`; the derived
/// `seconds_*` exist only when all three numbers do.
fn seed_window_record(window: &crate::schema::SeedWindow) -> Value {
    let number = |v: Option<f64>| v.map_or(Value::Null, |n| serde_json::json!(n));
    let mut out = Map::new();
    out.insert("now".into(), number(window.now));
    out.insert("issued_at".into(), number(window.issued_at));
    out.insert("expires_at".into(), number(window.expires_at));
    if let Some(before) = window.seconds_before_issued() {
        out.insert("seconds_before_issued".into(), number(Some(before)));
    }
    if let Some(after) = window.seconds_after_expires() {
        out.insert("seconds_after_expires".into(), number(Some(after)));
    }
    out.insert(
        "max_clock_skew_seconds".into(),
        number(Some(max_clock_skew_seconds())),
    );
    Value::Object(out)
}
/// `seed_time_refusal_event(path, public_code, record)`: the one bounded daemon-log line of a seed
/// window refusal, `None` for every other failure.
pub fn seed_time_refusal_event(record: &Value) -> Option<Value> {
    let window = record.get("seed_window")?;
    let mut event = Map::new();
    event.insert("event".into(), "seed_time_refused".into());
    event.insert(
        "path".into(),
        record
            .get("path")
            .cloned()
            .unwrap_or_else(|| "/invalid".into()),
    );
    event.insert(
        "public_code".into(),
        record
            .get("public_code")
            .cloned()
            .unwrap_or_else(|| "action_not_completed".into()),
    );
    if let Some(code) = record.get("error_code") {
        event.insert("error_code".into(), code.clone());
    }
    event.insert(
        "max_clock_skew_seconds".into(),
        serde_json::json!(max_clock_skew_seconds()),
    );
    event.insert("seed_window".into(), window.clone());
    Some(Value::Object(event))
}

pub trait Host: Send {
    /// Transfer a validated, read-only producer handoff into this host. Failure
    /// leaves custody with the caller. This is not a Start/Activate operation.
    fn install_compiled_handoff(
        &mut self,
        _handoff: &mut Option<crate::compiled_handoff::CompiledHandoff>,
    ) -> Result<(), PublicError> {
        Err(PublicError::new(409, "compiled_handoff_unavailable"))
    }
    /// `self.closed.is_set()`.
    fn closed(&self) -> bool;
    /// `installation_role == 'owner'`.
    fn owner(&self) -> bool;
    /// `os.environ.get(name)`; `Some("")` is a set-but-empty variable.
    fn environment(&self, name: &str) -> Option<String>;
    /// `host_memory_bytes()`.
    fn device_memory_bytes(&self) -> Option<u64>;
    /// `fast_mode.run_active(app)`: owner run, chain compile/serving thread or live workflow.
    fn run_active(&self) -> bool;
    /// `app._acquisition_active`.
    fn acquisition_active(&self) -> bool;
    /// `app.owner_run.active()`.
    fn owner_run_active(&self) -> bool;
    /// `app._refresh_fast_mode_process()`, after the file is written.
    fn fast_mode_changed(&mut self) {}
    /// `acquisition_policy_error = None; _open_acquisition()`, after the choice is saved.
    fn run_model_saved(&mut self) {}
    /// `time.time()` for consent timestamps.
    fn now_seconds(&self) -> f64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0.0, |d| d.as_secs_f64())
    }
    /// `self.authority.stop_control()` when this is a joined Mac with an authority.
    fn stop_control(&mut self) {}
    /// `self._start_public_relay({...})` (RED stub: unavailable).
    fn start_public_relay(
        &mut self,
        _relay_acknowledged: &Value,
        _max_seconds: &Value,
    ) -> Result<(), PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// Close a relay this action started.
    fn abort_public_relay(&mut self) {}
    /// `if self.public_relay is not None: self.public_relay.proxy.bind_seed(self.authority.seed_server)`
    /// (`app.py:1612-1613`), after the swarm exists.
    fn bind_relay_seed(&mut self) {}
    /// `app._start_local_contributor()`; a host without one cannot create it (`ValueError`).
    fn local_contributor_start(&mut self) -> Result<Json, PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// `app._stop_local_contributor()`: nothing to stop without one.
    fn local_contributor_stop(&mut self) -> Result<(), PublicError> {
        Ok(())
    }
    /// `(root / 'local-contributor').exists()`.
    fn local_contributor_retained(&self) -> bool {
        false
    }
    /// The authority part of `/api/create` (swarm creation, pairing, `running = True`).
    fn create_owner(&mut self, _consent: &Map<String, Value>) -> Result<Value, PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// `create_owner` for hosts that answer in the source's dict order (ruling (a)); the default
    /// converts the sorted `Value`.
    fn create_owner_json(&mut self, consent: &Map<String, Value>) -> Result<Json, PublicError> {
        self.create_owner(consent).map(Json::from)
    }
    /// `adaptive_owner.apply_first_consent_choice`, after a consent call succeeded.
    fn apply_first_consent_choice(&mut self, _enabled: bool) {}
    /// `authority.status().get('swarm_created')`.
    fn swarm_created(&self) -> bool {
        false
    }
    /// `authority.status().get('membership_state')`.
    fn membership_state(&self) -> Option<String> {
        None
    }
    /// `app.running`.
    fn running(&self) -> bool {
        false
    }
    /// `self.authority.stop(); self.running = False`.
    fn stop_authority(&mut self) {}
    /// `installation_role = 'target'` with a fresh `TargetApplication` (the role file is written
    /// by the caller first).
    fn adopt_target_role(&mut self) -> Result<(), PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// `self.status()`: the full status document (what `POST /api/start` answers).
    fn status_document(&self) -> Json {
        Json::Null
    }
    /// `self.authority.start(); self.running = True`.
    fn start_authority(&mut self) -> Result<(), PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// `if acquisition is None or its state_root differs: self._open_acquisition()`.
    fn reopen_acquisition_if_needed(&mut self) {}
    /// `self.authority.pair_request()`.
    fn pair_request(&mut self) -> Result<Json, PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// `self.authority.approve_pairing(request, ttl_seconds=..., quota=...)`.
    fn pair_approve(
        &mut self,
        _request: &Value,
        _ttl_seconds: &Value,
        _quota: &Value,
    ) -> Result<Json, PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// The application's stdout: one `json.dumps(event, sort_keys=True)` line for the operator's
    /// daemon log (`app.py:2025-2030`: `print(..., flush=True)`).
    fn daemon_log(&mut self, _line: &str) {}
    /// `RemoteMacMember.membership_binding()`: the swarm, node and owner seed key of the live
    /// membership session, `None` until a handoff was accepted by this process.
    fn membership_binding(&self) -> Option<crate::member::Binding> {
        None
    }
    /// `self.authority.accept_pairing(handoff)`.
    fn pair_accept(&mut self, _handoff: &Value) -> Result<Json, PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// `_owner_window_action` for `/api/pair/window/{open,list,approve,revoke}` (body shape
    /// already checked): the window store, relay origin and pending windows are the host's.
    fn owner_window(
        &mut self,
        _action: Action,
        _body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// `pairing.parse_join_text(text, now=...)`: a library `PairingError` (409) on a bad code (ported
    /// in `join_link`; a host only overrides it to fix `now`).
    fn parse_join_text(&self, text: &Value, now: f64) -> Result<JoinLink, PublicError> {
        let now = serde_json::Number::from_f64(now).map_or(Value::Null, Value::Number);
        crate::join_link::parse_join_text(text, &now).map(|(link, _)| link)
    }
    /// `client = JoinWindowClient(origin); report = self._measure_and_sign(client, 'join');
    /// client.submit(window_id, code=, request=, report=, quota=)` over the public HTTPS relay:
    /// the owner's decoded answer and the client's advisory warnings (e.g. `clock_skew`).
    fn join_submit(
        &mut self,
        _link: &JoinLink,
        _request: &Value,
        _quota: &Value,
    ) -> Result<(Value, Vec<Value>), PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// `JoinWindowClient(state.origin).poll(window_id, code=, digest=)`: the owner's decoded
    /// answer, or a library error carrying the closed `pairing_window_*` code.
    fn join_poll(&mut self, _state: &Map<String, Value>) -> Result<Value, PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// The approved branch of the poll: seed-pin check, `accept_pairing`, membership binding,
    /// `running = True`, the automatic join report.
    fn join_accept(
        &mut self,
        _handoff: &Value,
        _seed_key_digest: Option<&str>,
    ) -> Result<JoinAccepted, PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// `self.standing_error = public_failure_code(error)` after a failed `bind_standing_consent`.
    fn standing_bind_failed(&mut self, _code: &'static str) {}
    /// `threading.Thread(target=self.close).start()`; the answer is
    /// `{'stopping': True, 'identity_retained': True, 'evidence_retained': True}`.
    fn quit(&mut self) -> Result<Json, PublicError> {
        Ok(crate::jobj! {
            "stopping" => true, "identity_retained" => true, "evidence_retained" => true,
        })
    }
    /// `self.public_relay` and its `status()['state']`.
    fn relay_state(&self) -> RelayState {
        RelayState::Absent
    }
    /// `self.public_relay.close(); self.public_relay = None` for a relay whose window ran out.
    fn close_stopped_relay(&mut self) {}
    /// `public_relay.status().get('public_origin')`; `None` for an absent or empty origin.
    fn relay_origin(&self) -> Option<String> {
        None
    }
    /// `len([w for w in pairing_windows.list() if w['state'] in {'open','claimed','approved'}])`.
    fn live_pairing_windows(&self) -> usize {
        0
    }
    /// `self.local_contributor is not None`.
    fn local_contributor_running(&self) -> bool {
        false
    }
    /// `authority._network_configuration()['public_origin']`, when configured.
    fn configured_public_origin(&self) -> Option<String> {
        None
    }
    /// `authority.coordinator is not None`.
    fn coordinator_ready(&self) -> bool {
        self.swarm_created()
    }
    /// `secrets.token_hex(bytes)`.
    fn random_hex(&mut self, bytes: usize) -> Result<String, PublicError> {
        let mut raw = vec![0u8; bytes];
        std::io::Read::read_exact(
            &mut std::fs::File::open("/dev/urandom")
                .map_err(|_| PublicError::new(409, "action_not_completed"))?,
            &mut raw,
        )
        .map_err(|_| PublicError::new(409, "action_not_completed"))?;
        Ok(raw.iter().map(|b| format!("{b:02x}")).collect())
    }
    /// The host's side of one member leave (`member_leave.app_steps`): the steps it owns
    /// (`fence`, `unload`, `publish_withdrawal`, `disconnect`, `cancel_work`), the in-flight count
    /// and the wall clock. A smooth leave moves it to the `member-leave` thread, so it must own
    /// (or share) its state.
    fn leave_worker(&mut self) -> Box<dyn crate::leave::LeaveWorker> {
        Box::new(crate::leave::IdleWorker)
    }
    /// `resume_leave`: `_leave_fence = True` and, for a completed leave, `_left = True`.
    fn leave_mark(&mut self, _fence: bool, _left: bool) {}
    /// A republication after a failed action did not succeed (see `Backend::republish_after_failure`).
    fn publication_failed(&mut self, _error: &PublicError) {}
    /// `app._resource_check()`: `None` when the resource policy permits work, else the one guard code
    /// (`resource_power_policy_violation`, ..., or an adaptive reason). A host without an observer says `None`.
    fn resource_check(&mut self) -> Option<&'static str> {
        None
    }
    /// `app.acquisition_policy_error = reason`.
    fn set_acquisition_policy_error(&mut self, _reason: Option<&'static str>) {}
    /// `self.acquisition.start(grant)` then `self._acquisition_active = True`: the status document answered.
    fn acquisition_start(&mut self, _grant: &Value) -> Result<Json, PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// `self._cancel_acquisition(); return self.acquisition.status()`.
    fn acquisition_cancel(&mut self) -> Result<Json, PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// `self._cancel_acquisition()` alone (`/api/stop`, revocation of this Mac).
    fn cancel_acquisition(&mut self) {}
    /// `demo_app.action` for `/api/demo/start|stop|rebuild` (SE-L10-OWNER-RUN-HOST): the host owns the
    /// `OwnerRun` (see [`crate::owner_driver::DemoRuns`]). A host without a run chain answers what the
    /// delegate always did.
    fn demo_action(
        &mut self,
        _action: Action,
        _body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        Err(PublicError::new(409, "action_not_completed"))
    }
    /// The advanced serving block of `/api/serving/status`: `{**cleanup diagnostics,
    /// 'compilation': ..., 'serving': ..., 'route_ready': False}`.
    fn serving_status(&self) -> Json {
        crate::jobj! {
            "compilation" => Json::Null, "serving" => Json::Null, "route_ready" => false,
        }
    }
    /// Stop an actually owned workflow, preserving compilation diagnostics.
    /// None preserves the existing delegate's prepare/execute/publish contract;
    /// Some is the owning host's outcome, never a synthetic cleanup success.
    fn serving_stop(&mut self) -> Option<Result<Json, PublicError>> {
        None
    }
}
/// `self.public_relay`: absent, running (any state but `stopped`) or window-expired.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RelayState {
    Absent,
    Running,
    Stopped,
}
/// `parse_join_text` result: owner origin, window, code and (full link only) the seed pin.
#[derive(Clone, Debug, PartialEq)]
pub struct JoinLink {
    pub origin: String,
    pub window_id: String,
    pub code: String,
    pub seed_key_digest: Option<String>,
}
/// `accept_pairing` + membership binding + automatic report of a joined target.
pub struct JoinAccepted {
    pub membership_state: Value,
    pub generation: Value,
    pub binding: Option<Value>,
    pub report: Value,
}
/// One leave's steps and clock over the host's worker (shared through a cell: `execute` takes both).
struct LeaveRunner {
    worker: std::cell::RefCell<Box<dyn crate::leave::LeaveWorker>>,
    root: PathBuf,
    start: std::time::Instant,
}
struct RunnerSteps<'a>(&'a LeaveRunner);
struct RunnerClock<'a>(&'a LeaveRunner);
impl LeaveRunner {
    fn new(worker: Box<dyn crate::leave::LeaveWorker>, root: &std::path::Path) -> Self {
        Self {
            worker: std::cell::RefCell::new(worker),
            root: root.to_owned(),
            start: std::time::Instant::now(),
        }
    }
    fn steps(&self) -> RunnerSteps<'_> {
        RunnerSteps(self)
    }
    fn clock(&self) -> RunnerClock<'_> {
        RunnerClock(self)
    }
}
impl crate::leave::Steps for RunnerSteps<'_> {
    fn step(&mut self, name: &str) -> Result<(), Option<String>> {
        let mut worker = self.0.worker.borrow_mut();
        if name == "withdraw_consent" {
            return crate::preferences::standing_consent::withdraw(
                &self.0.root,
                worker.now_seconds(),
            )
            .map(|_| ())
            .map_err(|e| Some(e.0.to_owned()));
        }
        worker.step(name)
    }
    fn inflight(&mut self) -> Result<i64, crate::leave::Unobservable> {
        self.0.worker.borrow_mut().inflight()
    }
    fn compute_cleanup(&mut self) -> Option<Result<(), String>> {
        self.0.worker.borrow_mut().compute_cleanup()
    }
}
impl crate::leave::Clock for RunnerClock<'_> {
    fn monotonic(&mut self) -> f64 {
        self.0.start.elapsed().as_secs_f64()
    }
    fn sleep(&mut self, seconds: f64) {
        std::thread::sleep(std::time::Duration::from_secs_f64(seconds));
    }
    fn wall(&mut self) -> f64 {
        self.0.worker.borrow().now_seconds()
    }
}
pub struct PersistentBackend<H: Host, B: Backend> {
    root: PathBuf,
    host: H,
    inner: B,
    pending: crate::windows::PendingWindows,
    /// `app._leave_thread`: the running smooth leave, if any.
    leave_thread: Option<std::thread::JoinHandle<()>>,
}
impl<H: Host, B: Backend> PersistentBackend<H, B> {
    pub fn new(root: PathBuf, host: H, inner: B) -> Self {
        Self {
            root,
            host,
            inner,
            pending: crate::windows::PendingWindows::default(),
            leave_thread: None,
        }
    }
    /// The shared pending-window list (`DemoApp._pending_windows`) a host's status reads.
    pub fn pending_windows(&self) -> crate::windows::PendingWindows {
        self.pending.clone()
    }
    pub fn host(&self) -> &H {
        &self.host
    }
    pub fn inner(&self) -> &B {
        &self.inner
    }
}
/// `join-window.json`: the one pending join request (app.py `_join_state_path`).
const JOIN_STATE: &str = "join-window.json";
const JOIN_STATE_PROTOCOL: &str = "mycelium.a13_join_window_state.v1";
const ROLE_FILE: &str = "installation-role.json";
const ROLE_PROTOCOL: &str = "mycelium.a13_installation_role.v1";
/// `path.unlink(missing_ok=True)`: any other OSError is a builtin failure (409).
fn unlink_missing_ok(path: &std::path::Path) -> Result<(), PublicError> {
    match std::fs::remove_file(path) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(crate::preferences::os_error(&e)),
        _ => Ok(()),
    }
}
/// `identity_fingerprint`: the first 12 hex digits of the key digest, upper case, in groups of 4.
pub fn identity_fingerprint(digest: &Value) -> Result<String, PublicError> {
    let text = digest
        .as_str()
        .and_then(|d| d.strip_prefix("sha256:"))
        .filter(|h| {
            h.len() == 64
                && h.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        });
    let hex = text.ok_or(PublicError::new(409, "pairing_request_invalid"))?;
    let upper = hex[..12].to_ascii_uppercase();
    Ok(format!(
        "{}-{}-{}",
        &upper[..4],
        &upper[4..8],
        &upper[8..12]
    ))
}
impl<H: Host, B: Backend> PersistentBackend<H, B> {
    /// `DemoApp._become_target` (app.py:1097-1122): persist the consent, select the target role
    /// once, make sure the authority runs and the acquisition follows its state root.
    fn become_target(&mut self, consent: &Value) -> Result<(), PublicError> {
        let owner = self.host.owner();
        if owner && self.host.swarm_created() {
            return Err(PublicError::new(400, "owner_cannot_become_target"));
        }
        match resource_policy::read(&self.root)? {
            Some(saved) if &saved != consent => {
                return Err(PublicError::new(400, "consent_retry_changed"));
            }
            Some(_) => {}
            None => resource_policy::write(&self.root, consent)?,
        }
        if owner {
            self.host.stop_authority();
            let record = serde_json::json!({"protocol": ROLE_PROTOCOL, "role": "target"});
            crate::preferences::private_state::store(&self.root.join(ROLE_FILE), &record, 4096)
                .map_err(|e| PublicError::new(409, e.0))?;
            self.host.adopt_target_role()?;
        }
        if !self.host.running() {
            self.host.start_authority()?;
        }
        self.host.reopen_acquisition_if_needed();
        Ok(())
    }
    fn load_join_state(&self) -> Result<Option<Map<String, Value>>, PublicError> {
        let private = crate::preferences::private_state::private_json_limit()
            .map_err(|e| PublicError::new(409, e.0))?;
        let Some(value) =
            crate::preferences::private_state::load(&self.root.join(JOIN_STATE), private)
                .map_err(|e| PublicError::new(409, e.0))?
        else {
            return Ok(None);
        };
        let keys = [
            "protocol",
            "origin",
            "window_id",
            "code",
            "seed_key_digest",
            "request_digest",
            "fingerprint",
        ];
        if value.len() != keys.len()
            || !keys.iter().all(|k| value.contains_key(*k))
            || value["protocol"] != JOIN_STATE_PROTOCOL
        {
            return Err(PublicError::new(400, "join_state_invalid"));
        }
        Ok(Some(value))
    }
    /// `/api/join/window` (app.py:1742-1775).
    fn join_window(&mut self, body: &Map<String, Value>) -> Result<Json, PublicError> {
        let consent = &body["consent"];
        let owner = self.host.owner();
        if owner && self.host.swarm_created() {
            return Err(PublicError::new(400, "owner_cannot_become_target"));
        }
        if !owner && self.host.membership_state().as_deref() == Some("current") {
            return Err(PublicError::new(400, "already_joined"));
        }
        let now = self.host.now_seconds();
        let link = self.host.parse_join_text(&body["join"], now)?;
        // The grant is validated against THIS consent before any role change.
        let standing = body["standing_consent"].as_object();
        let standing = standing
            .filter(|s| {
                s.len() == 3
                    && ["acknowledged", "disclosure_digest", "caps"]
                        .iter()
                        .all(|k| s.contains_key(*k))
            })
            .ok_or(PublicError::new(400, "standing_consent_required"))?;
        let memory = self.host.device_memory_bytes();
        let memory_value = memory.map_or(Value::Null, Value::from);
        protocol::consent::caps(&standing["caps"], consent, &memory_value)
            .map_err(|e| PublicError::new(400, e.0))?;
        self.become_target(consent)?;
        standing_consent::grant(&self.root, &body["standing_consent"], consent, now, memory)
            .map_err(|e| PublicError::new(400, e.0))?;
        let request = self.host.pair_request()?.to_value();
        let storage = &consent["storage_bytes"];
        let quota = serde_json::json!({
            "storage_bytes": storage, "network_bytes_per_day": storage, "background_allowed": false,
        });
        let (answer, warnings) = self.host.join_submit(&link, &request, &quota)?;
        let fingerprint = identity_fingerprint(&request["identity_key_digest"])?;
        if answer["state"] != "waiting_for_owner" || answer["fingerprint"] != fingerprint {
            return Err(PublicError::new(400, "pairing_window_response_invalid"));
        }
        let request_digest = protocol::canonical::digest(&request)
            .map_err(|_| PublicError::new(409, "action_not_completed"))?;
        let record = serde_json::json!({
            "protocol": JOIN_STATE_PROTOCOL, "origin": link.origin, "window_id": link.window_id,
            "code": link.code, "seed_key_digest": link.seed_key_digest,
            "request_digest": request_digest, "fingerprint": fingerprint,
        });
        let private = crate::preferences::private_state::private_json_limit()
            .map_err(|e| PublicError::new(409, e.0))?;
        crate::preferences::private_state::store(&self.root.join(JOIN_STATE), &record, private)
            .map_err(|e| PublicError::new(409, e.0))?;
        let mut view = crate::jobj! {
            "state" => "waiting_for_owner", "fingerprint" => fingerprint.as_str(),
            "pinned" => link.seed_key_digest.is_some(),
        };
        if !warnings.is_empty() {
            view.set("warnings", Json::from(Value::Array(warnings)));
        }
        Ok(view)
    }
    /// `/api/join/window/poll` (app.py:1778-1810).
    fn join_window_poll(&mut self) -> Result<Json, PublicError> {
        let state = self.load_join_state()?;
        let Some(state) = state.filter(|_| !self.host.owner()) else {
            return Err(PublicError::new(400, "join_window_missing"));
        };
        let answer = match self.host.join_poll(&state) {
            Ok(answer) => answer,
            Err(error) => {
                if matches!(
                    error.code,
                    "pairing_window_unknown"
                        | "pairing_window_expired"
                        | "pairing_window_revoked"
                        | "pairing_window_locked"
                        | "pairing_window_request_mismatch"
                ) {
                    unlink_missing_ok(&self.root.join(JOIN_STATE))?;
                }
                return Err(error);
            }
        };
        if answer["state"] == "waiting_for_owner" {
            return Ok(crate::jobj! {
                "state" => "waiting_for_owner", "fingerprint" => &state["fingerprint"],
            });
        }
        let handoff = match answer.get("handoff") {
            Some(handoff) if answer["state"] == "approved" && handoff.is_object() => handoff,
            _ => return Err(PublicError::new(400, "pairing_window_response_invalid")),
        };
        let pin = state["seed_key_digest"].as_str();
        let joined = self.host.join_accept(handoff, pin)?;
        if let Some(binding) = &joined.binding
            && let Err(error) = standing_consent::bind(&self.root, binding, self.host.now_seconds())
        {
            self.host.standing_bind_failed(error.0);
        }
        unlink_missing_ok(&self.root.join(JOIN_STATE))?;
        Ok(crate::jobj! {
            "state" => "joined", "membership_state" => &joined.membership_state,
            "membership_generation" => &joined.generation, "report" => &joined.report,
            "route_ready" => false,
        })
    }
    /// `POST /api/leave` (member_leave.leave_action), after the gates of `prepare`. A leave that is
    /// already running only reports its status. Instant runs here and answers complete; smooth
    /// persists `begin`, raises the fence, drains on a `member-leave` thread and answers with the
    /// status as it is right then. The standing consent withdrawal is this backend's own file work.
    fn leave(&mut self, body: &Map<String, Value>) -> Result<Json, PublicError> {
        let leave = crate::leave::MemberLeave::new(&self.root, crate::leave::Limits::default());
        let status = |leave: &crate::leave::MemberLeave| {
            crate::leave::status_view(leave)
                .map_err(|_| {
                    PublicError::new(409, "action_not_completed")
                        .raised_by(crate::schema::Raised::Builtin("TypeError"))
                })
                .map(Json::from)
        };
        // execute publishes `left` after its last step. The OS thread may still
        // be returning/dropping its worker; that is not an in-progress action.
        // `left` is membership/action completion, never compute-cleanup proof.
        if self.leave_thread.as_ref().is_some_and(|t| !t.is_finished())
            && !matches!(leave.status(), Ok(Some(record)) if record["state"] == "left")
        {
            return status(&leave);
        }
        let smooth = body["mode"].as_str() == Some("smooth");
        let worker = self.host.leave_worker();
        let wall = self.host.now_seconds();
        let previous = match leave.status() {
            Ok(record) => record,
            Err(_) => {
                eprintln!("leave_state_invalid: starting an explicit replacement leave");
                None
            }
        };
        if let Some(record) = previous
            && record["state"] == "left"
            && crate::leave::cleanup_pending(&record)
        {
            let runner = LeaveRunner::new(worker, &self.root);
            leave.execute(record, &mut runner.steps(), &mut runner.clock())?;
            return status(&leave);
        }
        if !smooth {
            let runner = LeaveRunner::new(worker, &self.root);
            leave.execute(
                leave.begin("instant", None, wall)?,
                &mut runner.steps(),
                &mut runner.clock(),
            )?;
            return status(&leave);
        }
        let record = leave.begin("smooth", None, wall)?;
        // `app._leave_fence = True`: new work stops now, before the thread's first tick.
        self.host.leave_mark(true, false);
        // The oracle reads the file right after `thread.start()` and its thread has not saved
        // anything yet (300/300 executed runs): the answer is the `begin` record, read before the
        // drain thread can move the file on.
        let answer = status(&leave)?;
        let root = self.root.clone();
        let background = leave.clone();
        let spawned = std::thread::Builder::new()
            .name("member-leave".to_owned())
            .spawn(move || {
                let runner = LeaveRunner::new(worker, &root);
                if let Err(error) =
                    background.execute(record, &mut runner.steps(), &mut runner.clock())
                {
                    // The source's thread dies with a traceback on stderr; the answer is long gone.
                    eprintln!("member-leave: leave stopped: {}", error.code);
                }
            })
            .map_err(|_| {
                PublicError::new(409, "action_not_completed")
                    .raised_by(crate::schema::Raised::Builtin("RuntimeError"))
            })?;
        self.leave_thread = Some(spawned);
        Ok(answer)
    }
    /// `_owner_window_action` (app.py:1687-1723): a code asked for while no relay runs becomes a
    /// pending window; everything with a relay origin is the host's window store.
    fn owner_window(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        match action {
            Action::PairWindowRevoke => {
                if let Some(id) = body["window_id"].as_str()
                    && self.pending.remove(id)
                {
                    return Ok(crate::jobj! {"window_id" => id, "state" => "revoked"});
                }
                self.host.owner_window(action, body)
            }
            Action::PairWindowOpen => {
                if !self.host.swarm_created() || !self.host.coordinator_ready() {
                    return Err(PublicError::new(400, "pairing_window_swarm_required"));
                }
                if self.host.relay_origin().is_none() {
                    return self.open_pending_window(&body["ttl_seconds"]);
                }
                self.host.owner_window(action, body)
            }
            _ => self.host.owner_window(action, body),
        }
    }
    /// `_open_pending_window` (app.py:803-821): same capacity and TTL bounds as a live window.
    fn open_pending_window(&mut self, ttl: &Value) -> Result<Json, PublicError> {
        let seconds = |id: BudgetId| {
            id.span(Entries::new(0))
                .map(|v| i64::try_from(v.millis() / 1000).unwrap_or(i64::MAX))
                .map_err(|_| PublicError::new(503, "budget_unavailable"))
        };
        let (low, high) = (
            seconds(BudgetId::PyFinalMyceliumA13MacosPairingWindowTtlMinL452C0)?,
            seconds(BudgetId::PyFinalMyceliumA13MacosPairingWindowTtlMaxL453C0)?,
        );
        let ttl = ttl
            .as_i64()
            .filter(|t| (low..=high).contains(t))
            .and_then(|t| u32::try_from(t).ok())
            .ok_or(PublicError::new(400, "pairing_window_ttl_invalid"))?;
        let capacity = BudgetId::PyFinalMyceliumA13MacosPairingMaxWindowsL454C0
            .count()
            .map_err(|_| PublicError::new(503, "budget_unavailable"))?;
        let live = self.host.live_pairing_windows() + self.pending.len();
        if u64::try_from(live).unwrap_or(u64::MAX) >= capacity.get() {
            return Err(PublicError::new(400, "pairing_window_capacity"));
        }
        let pending_id = format!("pending-{}", self.host.random_hex(3)?);
        let view = crate::jobj! {
            "state" => "waiting_for_relay", "pending_id" => pending_id.as_str(),
            "message" => "Ready to share once the relay starts: start the public relay and this code becomes redeemable.",
            "fix" => crate::jobj! {"action" => "start_relay"},
        };
        self.pending.push(crate::windows::PendingWindow {
            pending_id,
            ttl_seconds: ttl,
        });
        Ok(view)
    }
    /// `_start_public_relay` after the shape check (app.py:690-713): the local contributor must
    /// not be running unless an external relay comes back on the address the retained
    /// authority already answers on, then the relay options are validated before any start.
    fn check_network_start(&mut self, body: &Map<String, Value>) -> Result<(), PublicError> {
        let external = body.get("provider").is_some_and(|v| v == "external");
        if self.host.swarm_created() && self.host.local_contributor_running() {
            let unchanged = external
                && self
                    .host
                    .configured_public_origin()
                    .is_some_and(|configured| {
                        crate::join_link::validate_external_origin(
                            body.get("public_origin").unwrap_or(&Value::Null),
                        )
                        .is_ok_and(|origin| origin == configured)
                    });
            if !unchanged {
                return Err(PublicError::new(
                    400,
                    "network_stop_local_contributor_required",
                ));
            }
        }
        if external {
            crate::join_link::RelayOptions::external(body)
                .map_err(|code| PublicError::new(400, code))?;
        }
        Ok(())
    }
    /// `/api/create` (app.py:1589-1625): the relay (if asked) precedes the swarm; the consent is
    /// persisted exactly once (a changed retry is refused) before the authority creates the
    /// swarm; any failure closes a relay this action started.
    fn create(&mut self, body: &Map<String, Value>) -> Result<Json, PublicError> {
        let consent = &body["consent"];
        let started = body.len() == 3;
        if started {
            self.host
                .start_public_relay(&body["relay_acknowledged"], &body["max_seconds"])?;
        }
        let result = (|| {
            match resource_policy::read(&self.root)? {
                Some(saved) if &saved != consent => {
                    return Err(PublicError::new(400, "consent_retry_changed"));
                }
                Some(_) => {}
                None => resource_policy::write(&self.root, consent)?,
            }
            let created = self
                .host
                .create_owner_json(consent.as_object().unwrap_or(&Map::new()))?;
            self.host.bind_relay_seed();
            Ok(created)
        })();
        if result.is_err() && started {
            self.host.abort_public_relay();
        }
        result
    }
    /// `/api/consent/standing/withdraw` (app.py:1730-1736).
    fn withdraw_standing_consent(&mut self) -> Result<Json, PublicError> {
        if !self.host.owner() {
            self.host.stop_control();
        }
        let now = self.host.now_seconds();
        match standing_consent::withdraw(&self.root, now).map_err(|e| PublicError::new(409, e.0))? {
            None => Err(PublicError::new(400, "standing_consent_missing")),
            Some(doc) => Ok(crate::jobj! {
                "state" => &doc["state"], "withdrawn_at" => &doc["withdrawn_at"], "route_ready" => false,
            }),
        }
    }
}
impl<H: Host, B: Backend> PersistentBackend<H, B> {
    /// `/api/model/download` (app.py:1551-1575 @ f4e366e9): the grant is checked against the saved resource
    /// policy, then the resource guard, then one background acquisition starts.
    fn model_download(&mut self, body: &Map<String, Value>) -> Result<Json, PublicError> {
        let demo = |code: &'static str| PublicError::new(400, code);
        let Some(grant) = body["consent"].as_object() else {
            return Err(demo("download_consent_required"));
        };
        let policy = crate::preferences::resource_policy::read(&self.root)?;
        let membership_current = self.host.membership_state().as_deref() == Some("current");
        let Some(policy) = policy.filter(|_| self.host.running() && membership_current) else {
            return Err(demo("membership_required"));
        };
        let storage = policy["storage_bytes"].as_u64().unwrap_or(0);
        // `type(x) is int and 0 < x <= policy['storage_bytes']`: a JSON integer, never a bool or a float.
        let within = |value: &Value| {
            value.as_u64().is_some_and(|v| v > 0 && v <= storage)
                || value
                    .as_i64()
                    .is_some_and(|v| v > 0 && u64::try_from(v).is_ok_and(|v| v <= storage))
        };
        let keys: std::collections::BTreeSet<&str> = grant.keys().map(String::as_str).collect();
        if keys
            != std::collections::BTreeSet::from([
                "download_acknowledged",
                "max_download_bytes",
                "storage_limit_bytes",
            ])
            || grant["download_acknowledged"] != Value::Bool(true)
            || !within(&grant["max_download_bytes"])
            || !within(&grant["storage_limit_bytes"])
        {
            return Err(demo("download_consent_invalid"));
        }
        if let Some(reason) = self.host.resource_check() {
            self.host.set_acquisition_policy_error(Some(reason));
            return Err(demo(reason));
        }
        if self.host.acquisition_active() {
            return Err(demo("acquisition_already_active"));
        }
        let result = self.host.acquisition_start(&Value::Object(grant.clone()))?;
        self.host.set_acquisition_policy_error(None);
        Ok(result)
    }
}
impl<H: Host, B: Backend> Backend for PersistentBackend<H, B> {
    fn prepare(&mut self, action: Action, body: &Map<String, Value>) -> Result<(), PublicError> {
        use Action::*;
        match action {
            FastMode
            | RunModel
            | Create
            | ConsentStandingWithdraw
            | JoinRequest
            | JoinWindow
            | JoinWindowPoll
            | JoinWindowCancel
            | PairRequest
            | PairApprove
            | PairAccept
            | PairWindowOpen
            | PairWindowList
            | PairWindowApprove
            | PairWindowRevoke
            | ServingStatus
            | ServingCompile
            | ServingStart
            | ServingActivate
            | ServingStop
            | ModelDownload
            | ModelCancel
            | Revoke
            | RevokeSelf
            | Start
            | Stop
            | DemoStart
            | DemoStop
            | DemoRebuild
            | LocalContributorStart
            | LocalContributorStop
            | Quit => {
                if self.host.closed() {
                    return Err(PublicError::new(400, "application_closed"));
                }
                // app.py:1346-1348: a target is refused these before any shape check.
                // fast_mode.choose checks the role before the body shape too. Run-model, the
                // standing withdrawal and the join/pair-request routes have no role check.
                if matches!(
                    action,
                    FastMode
                        | Create
                        | PairApprove
                        | PairWindowOpen
                        | PairWindowList
                        | PairWindowApprove
                        | PairWindowRevoke
                        | Revoke
                        | RevokeSelf
                        | DemoStart
                        | DemoStop
                        | DemoRebuild
                ) && !self.host.owner()
                {
                    return Err(PublicError::new(400, "owner_role_required"));
                }
                // app.py:1377-1378: the advanced serving block is owner-only.
                if matches!(
                    action,
                    ServingStatus | ServingCompile | ServingStart | ServingActivate | ServingStop
                ) && !self.host.owner()
                {
                    return Err(PublicError::new(400, "owner_action_required"));
                }
                // These routes were delegated before the owner/closed prefix
                // was mounted. Preserve their preparation after that prefix.
                if matches!(
                    action,
                    ServingCompile | ServingStart | ServingActivate | ServingStop
                ) {
                    self.inner.prepare(action, body)
                } else {
                    Ok(())
                }
            }
            Leave => {
                // app.py:1343-1352 then member_leave.leave_action: closed, role, shape, confirm, mode.
                if self.host.closed() {
                    return Err(PublicError::new(400, "application_closed"));
                }
                if self.host.owner() {
                    return Err(PublicError::new(400, "target_role_required"));
                }
                let shaped =
                    body.contains_key("mode") && body.keys().all(|k| k == "mode" || k == "confirm");
                if !shaped {
                    return Err(PublicError::new(400, "invalid_request"));
                }
                if body.get("confirm") != Some(&Value::Bool(true)) {
                    return Err(PublicError::new(400, "leave_confirmation_required"));
                }
                if !matches!(body["mode"].as_str(), Some("smooth" | "instant")) {
                    return Err(PublicError::new(400, "leave_mode_invalid"));
                }
                Ok(())
            }
            NetworkStart => {
                // app.py:1343, 676-690: the closed check, then the role and the relay that
                // already runs (both refuse before any shape check); a relay whose window ran
                // out is closed and forgotten so the same Start reopens it.
                if self.host.closed() {
                    return Err(PublicError::new(400, "application_closed"));
                }
                if !self.host.owner() {
                    return Err(PublicError::new(400, "network_requires_fresh_owner"));
                }
                match self.host.relay_state() {
                    RelayState::Running => {
                        return Err(PublicError::new(400, "network_requires_fresh_owner"));
                    }
                    RelayState::Stopped => self.host.close_stopped_relay(),
                    RelayState::Absent => {}
                }
                self.inner.prepare(action, body)
            }
            PairPreview => {
                if self.host.closed() {
                    return Err(PublicError::new(400, "application_closed"));
                }
                if !self.host.owner() {
                    return Err(PublicError::new(400, "owner_role_required"));
                }
                self.inner.prepare(action, body)
            }
            _ => self.inner.prepare(action, body),
        }
    }
    fn record_failure(&mut self, path: &str, error: &PublicError) {
        // app.py:2008-2031: the exception class and module are the oracle's (ruling 2).
        let valid_path = path.len() <= 161
            && path.starts_with('/')
            && path[1..]
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_./-".contains(&b));
        let code_ok = |c: &str| {
            let mut bytes = c.bytes();
            bytes.next().is_some_and(|b| b.is_ascii_lowercase())
                && c.len() <= 96
                && bytes.all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
        };
        let public_code = if code_ok(error.code) {
            error.code
        } else {
            "action_not_completed"
        };
        // The producer names its oracle exception; otherwise the closed code's owner does.
        let raised = match error.raised {
            crate::schema::Raised::Unspecified => crate::schema::Raised::for_code(error.code),
            named => named,
        };
        let exception = raised.identity();
        let mut record = serde_json::json!({
            "protocol": "mycelium.a13.action_failure.v1",
            "path": if valid_path { path } else { "/invalid" },
            "public_code": public_code,
            "exception_type": exception.0,
            "exception_module": exception.1,
        });
        // A builtin exception has no `.code`; a library error's closed code is recorded.
        if error.code != "action_not_completed" && code_ok(error.code) {
            record["error_code"] = serde_json::json!(error.code);
        }
        // `seed_time_window(error)`: the numbers of a seed window refusal, on the cause chain.
        if let Some(window) = error.window() {
            record["seed_window"] = seed_window_record(&window);
        }
        let raw = crate::preferences::dumps_sorted(&record);
        // `except BaseException: pass`: recording must never change the answer.
        crate::preferences::private_write(
            &self.root,
            "last-action-error.private.json",
            raw.as_bytes(),
        )
        .unwrap_or(());
        // A seed window refusal also names its numbers in the daemon log (app.py:2025-2030).
        if let Some(event) = seed_time_refusal_event(&record) {
            self.host
                .daemon_log(&crate::preferences::dumps_sorted(&event));
        }
    }
    fn execute(&mut self, action: Action, body: &Map<String, Value>) -> Result<Value, PublicError> {
        self.execute_json(action, body).map(|j| j.to_value())
    }
    fn execute_json(
        &mut self,
        action: Action,
        body: &Map<String, Value>,
    ) -> Result<Json, PublicError> {
        match action {
            Action::FastMode => {
                let env = self.host.environment(TOKEN_PATH_ENV);
                let (owner, active) = (self.host.owner(), self.host.run_active());
                let host = &mut self.host;
                fast_mode::choose_then_json(
                    &self.root,
                    owner,
                    active,
                    env.as_deref(),
                    body,
                    &mut || {
                        host.fast_mode_changed();
                    },
                )
            }
            Action::RunModel => {
                let env = self.host.environment(run_model::ENV);
                let facts = run_model::Facts {
                    env: env.as_deref(),
                    acquisition_active: self.host.acquisition_active(),
                    owner_run_active: self.host.owner_run_active(),
                    device_memory_bytes: self.host.device_memory_bytes(),
                };
                let host = &mut self.host;
                run_model::choose_json(&self.root, &facts, body, &mut || host.run_model_saved())
            }
            Action::Create => self.create(body),
            Action::ConsentStandingWithdraw => self.withdraw_standing_consent(),
            Action::JoinRequest => {
                self.become_target(&body["consent"])?;
                self.host.pair_request()
            }
            Action::JoinWindow => self.join_window(body),
            Action::JoinWindowPoll => self.join_window_poll(),
            Action::JoinWindowCancel => {
                unlink_missing_ok(&self.root.join(JOIN_STATE))?;
                Ok(crate::jobj! {"state" => "cancelled"})
            }
            Action::PairRequest => self.host.pair_request(),
            Action::PairApprove => {
                self.host
                    .pair_approve(&body["request"], &body["ttl_seconds"], &body["quota"])
            }
            Action::PairAccept => self.host.pair_accept(&body["handoff"]),
            Action::PairWindowOpen
            | Action::PairWindowList
            | Action::PairWindowApprove
            | Action::PairWindowRevoke => self.owner_window(action, body),
            Action::Leave => self.leave(body),
            Action::NetworkStart => {
                self.check_network_start(body)?;
                self.inner.execute_json(action, body)
            }
            Action::ServingStatus => Ok(self.host.serving_status()),
            Action::ServingStop => self
                .host
                .serving_stop()
                .unwrap_or_else(|| self.inner.execute_json(action, body)),
            // demo_app.action (demo_app.py:109-133): the ladder and the run are the host's.
            Action::DemoStart | Action::DemoStop | Action::DemoRebuild => {
                self.host.demo_action(action, body)
            }
            Action::ModelDownload => self.model_download(body),
            Action::ModelCancel => self.host.acquisition_cancel(),
            // app.py:1369-1374 @ f4e366e9: the owner's own contributor (no role check: a target is refused by
            // the consent gate of `create_local_contributor`).
            Action::LocalContributorStart => self.host.local_contributor_start(),
            Action::LocalContributorStop => {
                self.host.local_contributor_stop()?;
                Ok(crate::jobj! {
                    "state" => "stopped", "identity_retained" => true, "route_ready" => false,
                })
            }
            Action::Quit => self.host.quit(),
            // app.py:1618-1640 @ f4e366e9. `_cancel_acquisition` / `_stop_local_contributor` have nothing to
            // stop on a host without them; the authority's stop closes its seed runtime.
            Action::Stop => {
                self.host.cancel_acquisition();
                self.host.local_contributor_stop()?;
                self.host.stop_authority();
                Ok(crate::jobj! {
                    "stopped" => true, "identity_retained" => true, "evidence_retained" => true,
                })
            }
            Action::Start => {
                if !self.host.running() {
                    self.host.start_authority()?;
                    self.host.bind_relay_seed();
                    // app.py:1639-1640: an owner whose contributor directory exists brings it back.
                    if self.host.owner() && self.host.local_contributor_retained() {
                        self.host.local_contributor_start()?;
                    }
                }
                Ok(self.host.status_document())
            }
            // Revocation is the authority's: only the closed/role gate and the shape live here.
            Action::Revoke | Action::RevokeSelf => self.inner.execute_json(action, body),
            _ => self.inner.execute_json(action, body),
        }
    }
    fn apply_first_consent_choice(&mut self, enabled: bool) -> Result<(), PublicError> {
        self.host.apply_first_consent_choice(enabled);
        Ok(())
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        self.inner.publish()
    }
    fn republish_after_failure(&mut self, action: Action) {
        // Only an action whose failure leaves visible state behind: `/api/pair/accept` retains the
        // handoff before the transport refusal (pairing_state in the status).
        if action != Action::PairAccept {
            return;
        }
        if let Err(error) = self.inner.publish() {
            // The status keeps its previous snapshot; the action's own error is what is answered.
            self.host.publication_failed(&error);
        }
    }
}
