// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-app custody of the listing publisher (the app hooks of v0.1.4 `swarm_publish.py`:
//! `action`, `status`, `resume`, `close`), for whichever host owns the swarm lifecycle.
//!
//! Opt-in is explicit and off by default: until `/api/swarm/publish` succeeds no key, no state
//! file and no directory client exist, and ticks do nothing. The listing key is this
//! installation's own (`KEY_FILE`, 0600), never the node identity. Only lane-P documents cross
//! the directory port; the owner-run facts are reduced to `OwnerView` here, so prompts,
//! participants, registry origins and local paths never reach a listing.
//!
//! Locking: `bound` serializes publisher work and is held across one bounded directory pass;
//! `snapshot` is a separate short lock, so a status read never waits for the network.
use super::{
    DirectoryClient, DirectoryPort, Error, Listing, Phase, Status, SwarmPublisher,
    host::{OwnerView, PublisherHost, Stop},
};
use crate::{
    schema::PublicError,
    swarm::{docs, json::J},
};
use ed25519_dalek::SigningKey;
use serde_json::{Map, Value, json};
use std::{
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::{Arc, Mutex, MutexGuard, PoisonError},
};

/// The installation's listing signing key (32 raw Ed25519 seed bytes, 0600).
pub const KEY_FILE: &str = "swarm-publisher.key";
/// The lane-P durable publisher state (opt-in, record, withdrawal debt).
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
}

pub struct AppPublisher {
    root: PathBuf,
    connect: Connect,
    bound: Mutex<Option<Bound>>,
    snapshot: Mutex<Value>,
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
fn status_value(origin: &str, status: &Status, failure: Option<&Error>) -> Value {
    json!({
        "enabled": status.enabled,
        "mode": "app",
        "directory": origin,
        "swarm_id": status.swarm_id,
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
            bound: Mutex::new(None),
            snapshot: Mutex::new(unbound()),
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
    fn refresh(&self, bound: &Bound, failure: Option<&Error>) {
        *lock(&self.snapshot) = status_value(&bound.origin, &bound.host.status(), failure);
    }
    fn key(&self, create: bool) -> Result<SigningKey, Error> {
        let path = self.root.join(KEY_FILE);
        let invalid = || Error::new("swarm_publisher_key_invalid");
        match std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&path)
        {
            Ok(mut file) => {
                let meta = file.metadata().map_err(|_| invalid())?;
                if !meta.is_file() || meta.mode() & 0o077 != 0 || meta.len() != 32 {
                    return Err(invalid());
                }
                let mut seed = [0u8; 32];
                file.read_exact(&mut seed).map_err(|_| invalid())?;
                Ok(SigningKey::from_bytes(&seed))
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound && create => {
                let mut seed = [0u8; 32];
                std::fs::File::open("/dev/urandom")
                    .and_then(|mut f| f.read_exact(&mut seed))
                    .map_err(|_| Error::new("swarm_entropy_unavailable"))?;
                let mut file = std::fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .custom_flags(libc::O_NOFOLLOW)
                    .open(&path)
                    .map_err(|_| Error::new("swarm_publish_storage_failed"))?;
                file.write_all(&seed)
                    .and_then(|()| file.sync_all())
                    .map_err(|_| Error::new("swarm_publish_storage_failed"))?;
                Ok(SigningKey::from_bytes(&seed))
            }
            Err(_) => Err(invalid()),
        }
    }
    fn open(&self, origin: &str, create: bool) -> Result<Bound, Error> {
        let key = self.key(create)?;
        let port = (self.connect)(origin)?;
        let publisher = SwarmPublisher::open(&self.root.join(DB_FILE), key, port, origin)?;
        Ok(Bound {
            origin: origin.into(),
            host: PublisherHost::new(publisher),
        })
    }
    fn resume_locked(&self, bound: &mut Option<Bound>) -> Result<bool, Error> {
        if bound.is_some() {
            return Ok(true);
        }
        let Some(origin) = SwarmPublisher::bound_origin(&self.root.join(DB_FILE))? else {
            return Ok(false);
        };
        match self.open(&origin, false) {
            Ok(b) => {
                self.refresh(&b, None);
                *bound = Some(b);
                Ok(true)
            }
            Err(e) => {
                *lock(&self.snapshot) = json!({"enabled": false, "mode": "app",
                    "directory": origin, "phase": Phase::Failed, "error_code": e.code});
                Err(e)
            }
        }
    }
    /// Startup (`swarm_publish.resume`): reopen retained opt-in and withdrawal debt, if any.
    /// The host must keep ticking it even when the owner has since opted out.
    pub fn resume(&self) -> Result<bool, Error> {
        self.resume_locked(&mut lock(&self.bound))
    }
    /// `POST /api/swarm/publish` (app mode). Records the explicit opt-in durably; the listing
    /// itself is published by a later tick once the run is serving and route-qualified.
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
        let mut bound = lock(&self.bound);
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
            // Nothing listed and no debt: the old binding holds no obligation.
            *bound = None;
            std::fs::remove_file(self.root.join(DB_FILE))
                .map_err(|_| PublicError::new(409, "swarm_publish_storage_failed"))?;
        }
        if bound.is_none() {
            *bound = Some(self.open(directory, true).map_err(|e| public(&e))?);
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
        let mut bound = lock(&self.bound);
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
    /// host's own lock. `None` while never opted in (no state, no network).
    pub fn tick(&self, now: i64, facts: &OwnerFacts) -> Result<Option<Status>, Error> {
        self.with_bound(|host| host.observe(now, facts.view()))
    }
    /// Before releasing custody on quit/leave/pause: one bounded withdrawal attempt. On an
    /// unreachable directory the debt stays in the state file for the next start.
    pub fn stop(&self, now: i64, event: Stop) -> Result<Option<Status>, Error> {
        self.with_bound(|host| host.stop(now, event))
    }
    fn with_bound(
        &self,
        work: impl FnOnce(&mut PublisherHost) -> Result<Status, Error>,
    ) -> Result<Option<Status>, Error> {
        let mut bound = lock(&self.bound);
        if !self.resume_locked(&mut bound)? {
            return Ok(None);
        }
        let b = bound
            .as_mut()
            .ok_or_else(|| Error::new("swarm_publish_state_invalid"))?;
        let result = work(&mut b.host);
        self.refresh(b, result.as_ref().err());
        result.map(|_| Some(b.host.status()))
    }
}
