// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-app custody of the listing publisher (the app hooks of v0.1.4 `swarm_publish.py`:
//! `action`, `status`, `resume`, `close`), for whichever host owns the swarm lifecycle.
//!
//! Opt-in is explicit and off by default: until `/api/swarm/publish` succeeds no key, no state
//! file and no directory client exist, and ticks do nothing. Each listing (swarm id) is signed
//! by its own key, kept inside the private state file (`DB_FILE`, 0600) until the listing's
//! withdrawal is confirmed and then replaced, never the node identity: listings are not
//! linkable by key across swarms or over time. Only lane-P documents cross the directory port;
//! the owner-run facts are reduced to `OwnerView` here, so prompts, participants, registry
//! origins and local paths never reach a listing.
//!
//! Locking: `state` (the binding and its private file) is only ever held for local work, so
//! the owner's actions never wait for a directory. `work` serializes passes and is held across
//! their exchanges, which run without `state`: each exchange is planned and committed under
//! `state`, sent without it, then applied under it again (a rebinding in between ends the
//! pass). `snapshot` is a separate short lock for status reads. On close, passes stop before
//! their next exchange and the quit withdrawal is bounded by the caller's budget; whatever is
//! unfinished stays durable debt for the next start.
use super::{
    DirectoryClient, DirectoryPort, Error, Listing, Phase, Shown, Status, Step, Stored,
    SwarmPublisher,
    host::{OwnerView, PublisherHost, Stop},
    sync_parent,
};
use crate::{
    schema::PublicError,
    swarm::{docs, json::J, wire},
};
use ed25519_dalek::SigningKey;
use serde_json::{Map, Value, json};
use std::{
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::{
        Arc, Condvar, Mutex, MutexGuard, PoisonError,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

/// The B12 installation key file (32 raw Ed25519 seed bytes, 0600). No longer written: a
/// retained B12 listing or debt is withdrawn with it (the key moves into `DB_FILE`), and with
/// neither it is discarded unused.
pub const KEY_FILE: &str = "swarm-publisher.key";
/// The durable publisher state (opt-in, record, withdrawal debt, the current listing key).
pub const DB_FILE: &str = "swarm-publisher.sqlite3";

/// Builds the directory port for a validated origin (default: [`DirectoryClient::new`]).
pub type Connect = Box<dyn Fn(&str) -> Result<Arc<dyn DirectoryPort>, Error> + Send + Sync>;

/// The facts a host knows about its owner run, reduced to what the listing may use.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OwnerFacts {
    pub is_owner: bool,
    pub run_state: String,
    pub route_ready: bool,
    pub registry_origin: Option<String>,
    pub model: Option<String>,
    pub members: u32,
    pub public_entry_origin: Option<String>,
}
impl OwnerFacts {
    /// `swarm_publish.run_state`: the owner_run status document (`owner_run.py:188-214` keys:
    /// `state`, `route_ready`, `registry_origin`, `participants`), the prepared model and the
    /// public relay origin. Only an https relay is a public entry (`_relay_origin`).
    pub fn from_owner_run(
        is_owner: bool,
        owner_run: Option<&Value>,
        model: Option<&str>,
        relay_origin: Option<&str>,
    ) -> Self {
        let run = owner_run.unwrap_or(&Value::Null);
        let text = |key: &str| {
            run.get(key)
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty())
                .map(str::to_owned)
        };
        Self {
            is_owner,
            run_state: text("state").unwrap_or_default(),
            route_ready: run.get("route_ready") == Some(&Value::Bool(true)),
            registry_origin: text("registry_origin"),
            model: model.filter(|m| !m.is_empty()).map(str::to_owned),
            members: run
                .get("participants")
                .and_then(Value::as_array)
                .map_or(0, |p| u32::try_from(p.len()).unwrap_or(u32::MAX)),
            public_entry_origin: relay_origin
                .filter(|o| o.starts_with("https://"))
                .map(str::to_owned),
        }
    }
    fn view(&self) -> OwnerView<'_> {
        OwnerView {
            is_owner: self.is_owner,
            run_state: &self.run_state,
            route_ready: self.route_ready,
            registry_origin: self.registry_origin.as_deref(),
            model: self.model.as_deref(),
            members: self.members,
            public_entry_origin: self.public_entry_origin.as_deref(),
        }
    }
}

struct Bound {
    origin: String,
    host: PublisherHost,
    port: Arc<dyn DirectoryPort>,
    /// Which binding a pass planned against; a rebinding in between ends that pass.
    epoch: u64,
}

/// Serializes directory passes. Unlike a mutex held across I/O, a closing caller can stop
/// waiting for it at a deadline.
struct Work {
    busy: Mutex<bool>,
    idle: Condvar,
}
struct WorkGuard<'a>(&'a Work);
impl Work {
    fn acquire(&self, until: Option<Instant>) -> Option<WorkGuard<'_>> {
        let mut busy = lock(&self.busy);
        while *busy {
            busy = match until {
                None => self.idle.wait(busy).unwrap_or_else(PoisonError::into_inner),
                Some(until) => {
                    let left = until.checked_duration_since(Instant::now())?;
                    if left.is_zero() {
                        return None;
                    }
                    self.idle
                        .wait_timeout(busy, left)
                        .unwrap_or_else(PoisonError::into_inner)
                        .0
                }
            };
        }
        *busy = true;
        Some(WorkGuard(self))
    }
}
impl Drop for WorkGuard<'_> {
    fn drop(&mut self) {
        *lock(&self.0.busy) = false;
        self.0.idle.notify_all();
    }
}

pub struct AppPublisher {
    root: PathBuf,
    connect: Connect,
    state: Mutex<Option<Bound>>,
    work: Work,
    snapshot: Mutex<Value>,
    closing: AtomicBool,
    epochs: AtomicU64,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(PoisonError::into_inner)
}
fn unbound() -> Value {
    json!({"enabled": false, "mode": null})
}
fn refuse(code: &'static str) -> PublicError {
    PublicError::new(400, code)
}
/// Library codes an action may answer with; anything else is a defect, never a new code.
fn public(error: &Error) -> PublicError {
    const INPUT: [&str; 5] = [
        "swarm_name_invalid",
        "swarm_description_invalid",
        "swarm_access_mode_invalid",
        "swarm_open_quota_invalid",
        "swarm_directory_origin_invalid",
    ];
    const STATE: [&str; 5] = [
        "swarm_publish_storage_failed",
        "swarm_publish_state_invalid",
        "swarm_publisher_binding_mismatch",
        "swarm_publisher_key_invalid",
        "swarm_entropy_unavailable",
    ];
    if let Some(code) = INPUT.iter().find(|c| **c == error.code) {
        return refuse(code);
    }
    let code = STATE
        .iter()
        .find(|c| **c == error.code)
        .copied()
        .unwrap_or("action_not_completed");
    PublicError::new(409, code)
}
/// The `swarm` status field: every key of v0.1.4's `SwarmPublisher.status` (swarm_publish.py:456-472
/// @ f4e366e9), which the unchanged page's `swarm.js` reads, then this custody's own phase. The
/// listing key is both the owner and the entry key here (the record's `entry.key`), `attached` is
/// the accepted record, `link` is `docs.swarm_link` once the listing has a swarm id, and `killed`
/// is false: the owner's kill switch is not ported (class E), so no listing is ever killed.
fn status_value(origin: &str, status: &Status, shown: &Shown, failure: Option<&Error>) -> Value {
    let fingerprint = wire::fingerprint(&J::str(&shown.key)).ok();
    let link = status
        .swarm_id
        .as_deref()
        .and_then(|id| docs::swarm_link(origin, id, &shown.key).ok());
    json!({
        "enabled": status.enabled,
        "mode": "app",
        "directory": origin,
        "swarm_id": status.swarm_id,
        "name": shown.name,
        "access_mode": shown.access_mode,
        "owner_fingerprint": fingerprint,
        "entry_fingerprint": fingerprint,
        "attached": shown.attached,
        "entry_origin": shown.entry_origin,
        "heartbeat": shown.heartbeat,
        "link": link,
        "killed": false,
        "phase": if failure.is_some() { Phase::Failed } else { status.phase.clone() },
        "protocol_version": status.protocol_version,
        "error_code": failure.map(|e| e.code.clone()).or_else(|| status.error_code.clone()),
        "next_attempt_at": status.next_attempt_at,
    })
}

impl AppPublisher {
    pub fn new(root: &Path) -> Self {
        Self::with_connect(
            root,
            Box::new(|origin: &str| {
                Ok(Arc::new(DirectoryClient::new(origin)?) as Arc<dyn DirectoryPort>)
            }),
        )
    }
    /// Explicit directory transport (embedded hosts, isolated loopback qualification).
    pub fn with_connect(root: &Path, connect: Connect) -> Self {
        Self {
            root: root.into(),
            connect,
            state: Mutex::new(None),
            work: Work {
                busy: Mutex::new(false),
                idle: Condvar::new(),
            },
            snapshot: Mutex::new(unbound()),
            closing: AtomicBool::new(false),
            epochs: AtomicU64::new(0),
        }
    }
    /// The `swarm` status field. Never blocks on directory work.
    pub fn status_json(&self) -> Value {
        lock(&self.snapshot).clone()
    }
    /// Whether the owner currently wants a listing (for a host's relay-lifetime policy).
    pub fn listing_enabled(&self) -> bool {
        lock(&self.snapshot)["enabled"] == true
    }
    /// Whether a withdrawal is still owed (shown to a member too: it stays this host's debt).
    pub fn withdrawal_pending(&self) -> bool {
        lock(&self.snapshot)["phase"] == "withdraw_pending"
    }
    fn refresh(&self, bound: &Bound, failure: Option<&Error>) {
        *lock(&self.snapshot) = status_value(
            &bound.origin,
            &bound.host.status(),
            &bound.host.shown(),
            failure,
        );
    }
    fn db(&self) -> PathBuf {
        self.root.join(DB_FILE)
    }
    /// The B12 installation key, read as B12 did (regular 0600 file of exactly 32 bytes).
    fn legacy_key(&self) -> Result<SigningKey, Error> {
        let invalid = || Error::new("swarm_publisher_key_invalid");
        let mut file = std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(self.root.join(KEY_FILE))
            .map_err(|_| invalid())?;
        let meta = file.metadata().map_err(|_| invalid())?;
        if !meta.is_file() || meta.mode() & 0o077 != 0 || meta.len() != 32 {
            return Err(invalid());
        }
        let mut seed = [0u8; 32];
        file.read_exact(&mut seed).map_err(|_| invalid())?;
        Ok(SigningKey::from_bytes(&seed))
    }
    /// Once the state file holds the listing key, a B12 key file (or creation residue of one)
    /// signs nothing more.
    fn discard_legacy_key(&self) -> Result<(), Error> {
        let path = self.root.join(KEY_FILE);
        match std::fs::remove_file(&path) {
            Ok(()) => sync_parent(&path),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(Error::new("swarm_publish_storage_failed")),
        }
    }
    fn bind(&self, origin: &str, port: Arc<dyn DirectoryPort>, publisher: SwarmPublisher) -> Bound {
        Bound {
            origin: origin.into(),
            host: PublisherHost::new(publisher),
            port,
            epoch: self.epochs.fetch_add(1, Ordering::SeqCst),
        }
    }
    /// A fresh binding (and listing key) for `origin`, replacing state proven to owe nothing.
    fn create(&self, origin: &str, listing: Option<Listing>) -> Result<Bound, Error> {
        let port = (self.connect)(origin)?;
        let publisher = SwarmPublisher::create_owned(&self.db(), port.clone(), origin, listing)?;
        self.discard_legacy_key()?;
        Ok(self.bind(origin, port, publisher))
    }
    /// Reopen retained state. Interrupted-creation residue and state that never listed anything
    /// are replaced by a fresh binding when their key cannot be used; a record (listed, possibly
    /// listed, or owed a withdrawal) keeps its key or is refused, never discarded.
    fn reopen(
        &self,
        origin: &str,
        obligated: bool,
        listing: Option<Listing>,
        keyed: bool,
    ) -> Result<Bound, Error> {
        let port = (self.connect)(origin)?;
        let opened = if keyed {
            SwarmPublisher::open_owned(&self.db(), port.clone(), origin, None)
        } else if obligated {
            self.legacy_key().and_then(|key| {
                SwarmPublisher::open_owned(&self.db(), port.clone(), origin, Some(key))
            })
        } else {
            // A B12 key that listed nothing retained may still have signed earlier listings.
            Err(Error::new("swarm_publisher_key_invalid"))
        };
        match opened {
            Ok(publisher) => {
                self.discard_legacy_key()?;
                Ok(self.bind(origin, port, publisher))
            }
            Err(error) if obligated => Err(error),
            Err(_) => self.create(origin, listing),
        }
    }
    fn resume_locked(&self, bound: &mut Option<Bound>) -> Result<bool, Error> {
        if bound.is_some() {
            return Ok(true);
        }
        let opened = match SwarmPublisher::inspect(&self.db()) {
            Ok(Stored::Absent | Stored::Empty) => return Ok(false),
            Ok(Stored::Bound {
                origin,
                obligated,
                listing,
                keyed,
            }) => self
                .reopen(&origin, obligated, listing, keyed)
                .map_err(|e| (Some(origin), e)),
            Err(e) => Err((None, e)),
        };
        match opened {
            Ok(b) => {
                self.refresh(&b, None);
                *bound = Some(b);
                Ok(true)
            }
            Err((origin, e)) => {
                *lock(&self.snapshot) = json!({"enabled": false, "mode": "app",
                    "directory": origin, "phase": Phase::Failed, "error_code": e.code});
                Err(e)
            }
        }
    }
    /// Startup (`swarm_publish.resume`): reopen retained opt-in and withdrawal debt, if any.
    /// The host must keep ticking it even when the owner has since opted out.
    pub fn resume(&self) -> Result<bool, Error> {
        self.resume_locked(&mut lock(&self.state))
    }
    /// `POST /api/swarm/publish` (app mode). Records the explicit opt-in durably; the listing
    /// itself is published by a later tick once the run is serving and route-qualified.
    /// Local work only: it never waits for a directory exchange in progress.
    pub fn publish(&self, is_owner: bool, body: &Map<String, Value>) -> Result<Value, PublicError> {
        if !is_owner {
            return Err(refuse("owner_role_required"));
        }
        if body.contains_key("link") {
            // Browser-created swarms (link mode) and the entry gateway are not ported (E).
            return Err(refuse("swarm_link_mode_unsupported"));
        }
        const ALLOWED: [&str; 5] = [
            "directory",
            "name",
            "access_mode",
            "description",
            "open_quota",
        ];
        let text = |key: &str| body.get(key).and_then(Value::as_str);
        let shaped = body.keys().all(|k| ALLOWED.contains(&k.as_str()))
            && ["directory", "name", "access_mode"]
                .iter()
                .all(|k| text(k).is_some())
            && body.get("description").is_none_or(Value::is_string)
            && body
                .get("open_quota")
                .is_none_or(|v| v.is_object() || v.is_null());
        let (Some(directory), Some(name), Some(access_mode), true) =
            (text("directory"), text("name"), text("access_mode"), shaped)
        else {
            return Err(refuse("invalid_request"));
        };
        docs::origin(&J::str(directory), true, false, "directory_origin_invalid")
            .map_err(|e| public(&e.into()))?;
        let listing = Listing {
            name: name.into(),
            description: text("description").unwrap_or_default().into(),
            access_mode: access_mode.into(),
            open_quota: body.get("open_quota").filter(|v| !v.is_null()).cloned(),
        };
        let fields = J::obj(vec![
            ("name", J::str(&listing.name)),
            ("description", J::str(&listing.description)),
            ("access_mode", J::str(&listing.access_mode)),
            (
                "open_quota",
                crate::swarm::json::parse(
                    &serde_json::to_vec(&listing.open_quota)
                        .map_err(|_| refuse("invalid_request"))?,
                )
                .map_err(|_| refuse("swarm_open_quota_invalid"))?,
            ),
        ]);
        docs::check_listing(&fields).map_err(|e| public(&e.into()))?;
        let mut bound = lock(&self.state);
        self.resume_locked(&mut bound).map_err(|e| public(&e))?;
        if let Some(old) = bound.as_ref().filter(|b| b.origin != directory) {
            let status = old.host.status();
            if status.phase == Phase::WithdrawPending {
                return Err(PublicError::new(409, "swarm_withdrawal_pending"));
            }
            if status.swarm_id.is_some() {
                return Err(PublicError::new(
                    409,
                    "swarm_directory_change_requires_unpublish",
                ));
            }
            // Nothing listed and no debt: no exchange can be in flight for the old binding
            // (every exchange needs a record), and it holds no obligation. Replaced below.
            *bound = None;
        }
        if bound.is_none() {
            *bound = Some(
                self.create(directory, Some(listing.clone()))
                    .map_err(|e| public(&e))?,
            );
        }
        let b = bound
            .as_mut()
            .ok_or_else(|| PublicError::new(409, "action_not_completed"))?;
        b.host
            .listing_preference(true, Some(listing))
            .map_err(|e| public(&e))?;
        self.refresh(b, None);
        Ok(self.status_json())
    }
    /// `POST /api/swarm/unpublish` (`{}` or `{"delete": true}`): revoke the opt-in now; the
    /// next tick deletes the listing (debt is durable until the directory confirms absence).
    /// Local work only: it never waits for a directory exchange in progress.
    pub fn unpublish(
        &self,
        is_owner: bool,
        body: &Map<String, Value>,
    ) -> Result<Value, PublicError> {
        if !is_owner {
            return Err(refuse("owner_role_required"));
        }
        if !(body.is_empty() || (body.len() == 1 && body.get("delete") == Some(&Value::Bool(true))))
        {
            return Err(refuse("invalid_request"));
        }
        let mut bound = lock(&self.state);
        if !self.resume_locked(&mut bound).map_err(|e| public(&e))? {
            return Ok(self.status_json());
        }
        let b = bound
            .as_mut()
            .ok_or_else(|| PublicError::new(409, "action_not_completed"))?;
        b.host
            .listing_preference(true, None)
            .map_err(|e| public(&e))?;
        self.refresh(b, None);
        Ok(self.status_json())
    }
    /// One bounded pass after a lifecycle transition and on the host's periodic tick, off the
    /// host's own lock. `None` while never opted in (no state, no network). Once closing, no
    /// exchange is started.
    pub fn tick(&self, now: i64, facts: &OwnerFacts) -> Result<Option<Status>, Error> {
        if self.closing.load(Ordering::SeqCst) {
            return Ok(self.current());
        }
        self.pass(None, |host| host.begin_observe(now, facts.view()))
    }
    /// Before releasing custody on quit/leave/pause: one withdrawal attempt, bounded by the
    /// directory client's own request budget. On an unreachable directory the debt stays in
    /// the state file for the next start.
    pub fn stop(&self, now: i64, event: Stop) -> Result<Option<Status>, Error> {
        self.pass(None, |host| host.begin_stop(now, event))
    }
    /// Quit (`swarm_publish.close`): no new pass starts an exchange; a live listing becomes
    /// durable withdrawal debt at once; then one withdrawal attempt that ends with `budget`,
    /// including any wait for a pass already inside an exchange. Whatever is unfinished stays
    /// debt for the next start. Only a storage failure is an error.
    pub fn close(&self, now: i64, budget: Duration) -> Result<Option<Status>, Error> {
        let until = Instant::now() + budget;
        self.closing.store(true, Ordering::SeqCst);
        {
            let mut bound = lock(&self.state);
            if !self.resume_locked(&mut bound)? {
                return Ok(None);
            }
            let b = bound
                .as_mut()
                .ok_or_else(|| Error::new("swarm_publish_state_invalid"))?;
            let marked = b.host.mark_stop(Stop::Quit);
            self.refresh(b, marked.as_ref().err());
            marked?;
        }
        self.pass(Some(until), |host| host.begin_stop(now, Stop::Quit))
    }
    fn current(&self) -> Option<Status> {
        lock(&self.state).as_ref().map(|b| b.host.status())
    }
    /// One pass: planned and applied under `state`, each exchange sent without it. A pass
    /// with a deadline (`close`) gives up waiting and sending at it; other passes stop before
    /// their next exchange once closing. Every step is durable before its exchange, so a pass
    /// ended early is like one ended by a crash: retried by the next pass or start.
    fn pass(
        &self,
        until: Option<Instant>,
        begin: impl FnOnce(&mut PublisherHost) -> Result<Step, Error>,
    ) -> Result<Option<Status>, Error> {
        let Some(_work) = self.work.acquire(until) else {
            return Ok(self.current());
        };
        let (mut step, port, epoch) = {
            let mut bound = lock(&self.state);
            if !self.resume_locked(&mut bound)? {
                return Ok(None);
            }
            let b = bound
                .as_mut()
                .ok_or_else(|| Error::new("swarm_publish_state_invalid"))?;
            let step = begin(&mut b.host);
            self.refresh(b, step.as_ref().err());
            (step?, b.port.clone(), b.epoch)
        };
        loop {
            // Callers get the custody's current status (as the snapshot), not the pass's
            // own phase adjustment.
            let call = match step {
                Step::Done(_) => return Ok(self.current()),
                Step::Call(call) => call,
            };
            let reply = match until {
                Some(until) if Instant::now() >= until => return Ok(self.current()),
                Some(until) => port.call_until(call.method, &call.path, &call.body, until),
                None if self.closing.load(Ordering::SeqCst) => return Ok(self.current()),
                None => port.call(call.method, &call.path, &call.body),
            };
            let mut bound = lock(&self.state);
            let Some(b) = bound.as_mut().filter(|b| b.epoch == epoch) else {
                return Ok(None);
            };
            let next = b.host.advance(call, reply);
            self.refresh(b, next.as_ref().err());
            step = next?;
        }
    }
}
