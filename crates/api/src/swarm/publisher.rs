// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-app listing lifecycle, based on v0.1.4 swarm_publish.py.
//!
//! Only the closed v1 record/heartbeat/delete documents cross DirectoryPort. The host
//! supplies qualified serving facts, not a general app/status JSON object. Opt-in and
//! withdrawal debt are durable before any network effect. One owner drives this handle;
//! it starts no thread. Call reconcile on transitions and at the source heartbeat cadence.
//! Unlike v0.1.4's best-effort offline beat, stop/unlist deletes the listing and retains
//! debt across restart. Directory deletion retires an ID: a later run gets a fresh ID.
pub mod app;
mod client;
pub mod host;
pub use client::DirectoryClient;

use super::{
    docs,
    json::{J, parse},
    wire,
};
use budgets::{BudgetId, Entries};
use ed25519_dalek::{Signer as _, SigningKey};
use rusqlite::{Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{io::Read, path::Path, sync::Arc};

/// The existing signed protocol is the compatibility version, not the app release.
/// v0.2 and future apps may emit v1 while they implement its semantics. Extra app-version
/// fields are NOT tolerated by the deployed server's exact key sets.
pub const PROTOCOL_VERSION: u32 = 1;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Error {
    pub code: String,
}
impl Error {
    pub fn new(code: &str) -> Self {
        Self { code: code.into() }
    }
    /// Preserve directory refusal codes, including explicit protocol incompatibility.
    pub fn response(status: u16, value: Value) -> Result<Value, Self> {
        if (200..300).contains(&status) && value.is_object() {
            return Ok(value);
        }
        let code = value.get("error").and_then(Value::as_str).filter(|s| {
            !s.is_empty()
                && s.len() <= 80
                && s.as_bytes()[0].is_ascii_lowercase()
                && s.bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
        });
        Err(Self::new(&match code {
            Some("protocol_invalid" | "directory_version_unsupported") => {
                "swarm_directory_version_unsupported".into()
            }
            Some(code) => format!("swarm_directory_{code}"),
            None => format!("swarm_directory_http_{status}"),
        }))
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.code)
    }
}
impl std::error::Error for Error {}
impl From<wire::WireError> for Error {
    fn from(e: wire::WireError) -> Self {
        Self::new(&format!("swarm_{}", e.code))
    }
}

/// No redirects, ambient cookies, proxies, or credentials. Paths are relative to /swarms/api/v1.
pub trait DirectoryPort: Send + Sync {
    fn call(&self, method: &str, path: &str, body: &[u8]) -> Result<Value, Error>;
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Listing {
    pub name: String,
    pub description: String,
    pub access_mode: String,
    /// Required for open access; explicit quotas avoid silently granting new rights.
    pub open_quota: Option<Value>,
}
#[derive(Clone, Debug)]
pub enum OwnerState {
    /// Host must establish owner role + state=serving + route_ready + registry origin.
    Serving {
        model: String,
        members: u32,
        entry_origin: String,
    },
    /// Does not create a new listing; an already published run may report warming.
    Warming {
        members: u32,
    },
    Paused,
    Left,
    Quit,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Unlisted,
    Pending,
    Listed,
    Failed,
    WithdrawPending,
}
#[derive(Clone, Debug, Serialize)]
pub struct Status {
    pub phase: Phase,
    pub enabled: bool,
    pub swarm_id: Option<String>,
    pub protocol_version: u32,
    pub error_code: Option<String>,
    pub next_attempt_at: i64,
}
#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct State {
    listing: Option<Listing>,
    record: Option<Value>,
    record_accepted: bool,
    withdraw: bool,
    last_at: i64,
    last_beat: Option<i64>,
    beat_fields: Option<Value>,
    next_attempt: i64,
    error: Option<String>,
}

pub(crate) fn seconds(id: BudgetId) -> Result<i64, Error> {
    let span = id
        .span(Entries::ZERO)
        .map_err(|_| Error::new("swarm_budget_unavailable"))?;
    i64::try_from(span.millis() / 1000).map_err(|_| Error::new("swarm_budget_unavailable"))
}
pub fn heartbeat_seconds() -> Result<i64, Error> {
    seconds(BudgetId::PyMyceliumA13MacosSwarmPublishHeartbeatSecondsL42)
}
fn storage<T>(r: rusqlite::Result<T>) -> Result<T, Error> {
    r.map_err(|_| Error::new("swarm_publish_storage_failed"))
}
fn value_j(v: &Value) -> Result<J, Error> {
    parse(&serde_json::to_vec(v).map_err(|_| Error::new("swarm_document_invalid"))?)
        .map_err(Into::into)
}

pub struct SwarmPublisher {
    key: SigningKey,
    public: String,
    directory: Arc<dyn DirectoryPort>,
    db: Connection,
    state: State,
}
impl SwarmPublisher {
    pub fn memory(key: SigningKey, directory: Arc<dyn DirectoryPort>) -> Result<Self, Error> {
        Self::from_connection(
            storage(Connection::open_in_memory())?,
            key,
            directory,
            "memory",
        )
    }
    /// Dedicated private SQLite file. A changed key/directory refuses to adopt another
    /// publisher's debt; drain the old handle before changing the configured directory.
    pub fn open(
        path: &Path,
        key: SigningKey,
        directory: Arc<dyn DirectoryPort>,
        origin: &str,
    ) -> Result<Self, Error> {
        use std::os::unix::fs::OpenOptionsExt;
        std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(path)
            .map_err(|_| Error::new("swarm_publish_storage_failed"))?;
        Self::from_connection(storage(Connection::open(path))?, key, directory, origin)
    }
    /// The directory origin an existing private state file is bound to, read without adopting
    /// it, so a host can reopen retained debt at startup with the matching client.
    pub fn bound_origin(path: &Path) -> Result<Option<String>, Error> {
        match std::fs::symlink_metadata(path) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err(Error::new("swarm_publish_storage_failed")),
            Ok(m) if !m.file_type().is_file() => {
                return Err(Error::new("swarm_publish_storage_failed"));
            }
            Ok(_) => {}
        }
        let db = storage(Connection::open_with_flags(
            path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
        ))?;
        storage(
            db.query_row("SELECT origin FROM publisher WHERE id=1", [], |r| r.get(0))
                .optional(),
        )
    }
    fn from_connection(
        db: Connection,
        key: SigningKey,
        directory: Arc<dyn DirectoryPort>,
        origin: &str,
    ) -> Result<Self, Error> {
        let public = wire::key_text(&key.verifying_key().to_bytes())?;
        storage(db.execute_batch("PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS publisher (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, origin TEXT NOT NULL, state TEXT NOT NULL);"))?;
        let saved: Option<(String, String, String)> = storage(
            db.query_row(
                "SELECT owner,origin,state FROM publisher WHERE id=1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional(),
        )?;
        let state = if let Some((owner, destination, text)) = saved {
            if owner != public || destination != origin {
                return Err(Error::new("swarm_publisher_binding_mismatch"));
            }
            let state: State = serde_json::from_str(&text)
                .map_err(|_| Error::new("swarm_publish_state_invalid"))?;
            if let Some(record) = &state.record {
                docs::check_record(&value_j(record)?, &wire::ed25519_verify, true, false)?;
                if record["owner_key"] != public {
                    return Err(Error::new("swarm_publisher_binding_mismatch"));
                }
            }
            state
        } else {
            let state = State::default();
            storage(db.execute(
                "INSERT INTO publisher VALUES(1,?1,?2,?3)",
                rusqlite::params![
                        public,
                        origin,
                        serde_json::to_string(&state)
                            .map_err(|_| Error::new("swarm_publish_state_invalid"))?
                    ],
            ))?;
            state
        };
        Ok(Self {
            key,
            public,
            directory,
            db,
            state,
        })
    }
    fn commit(&mut self, state: State) -> Result<(), Error> {
        let text =
            serde_json::to_string(&state).map_err(|_| Error::new("swarm_publish_state_invalid"))?;
        storage(
            self.db
                .execute("UPDATE publisher SET state=?1 WHERE id=1", [text]),
        )?;
        self.state = state;
        Ok(())
    }
    pub fn status(&self) -> Status {
        let phase = if self.state.withdraw {
            Phase::WithdrawPending
        } else if self.state.error.is_some() {
            Phase::Failed
        } else if self.state.record_accepted && self.state.last_beat.is_some() {
            Phase::Listed
        } else if self.state.listing.is_some() {
            Phase::Pending
        } else {
            Phase::Unlisted
        };
        Status {
            phase,
            enabled: self.state.listing.is_some(),
            swarm_id: self
                .state
                .record
                .as_ref()
                .and_then(|r| r["swarm_id"].as_str())
                .map(str::to_owned),
            protocol_version: PROTOCOL_VERSION,
            error_code: self.state.error.clone(),
            next_attempt_at: self.state.next_attempt,
        }
    }
    /// The sole opt-in port; do not call this merely because a swarm exists. None revokes
    /// locally immediately, even if the following withdrawal cannot reach the directory.
    pub fn set_listing(&mut self, listing: Option<Listing>) -> Result<(), Error> {
        let mut state = self.state.clone();
        if state.listing == listing {
            return Ok(());
        }
        state.listing = listing;
        if state.listing.is_none() && state.record.is_some() {
            state.withdraw = true;
            state.next_attempt = 0;
        }
        self.commit(state)
    }
    fn signed(&self, body: Value) -> Result<Value, Error> {
        let doc = value_j(&body)?;
        let signature = self.key.sign(&wire::signing_bytes(&doc)?);
        let signed = doc.with("sig", J::str(&wire::b64url(&signature.to_bytes())));
        serde_json::from_slice(&wire::canonical(&signed)?)
            .map_err(|_| Error::new("swarm_document_invalid"))
    }
    fn post(&self, path: &str, document: &Value) -> Result<Value, Error> {
        self.directory
            .call("POST", path, &wire::canonical(&value_j(document)?)?)
    }
    fn record(&self, listing: &Listing, model: &str, origin: &str) -> Result<Value, Error> {
        let (id, seq) = if let Some(old) = &self.state.record {
            (
                old["swarm_id"]
                    .as_str()
                    .ok_or_else(|| Error::new("swarm_publish_state_invalid"))?
                    .to_owned(),
                old["seq"]
                    .as_i64()
                    .ok_or_else(|| Error::new("swarm_publish_state_invalid"))?
                    + 1,
            )
        } else {
            let mut random = [0u8; 10];
            std::fs::File::open("/dev/urandom")
                .and_then(|mut f| f.read_exact(&mut random))
                .map_err(|_| Error::new("swarm_entropy_unavailable"))?;
            (
                format!(
                    "sw-{}",
                    random
                        .iter()
                        .map(|b| format!("{b:02x}"))
                        .collect::<String>()
                ),
                1,
            )
        };
        let record = self.signed(
            json!({"protocol":docs::RECORD, "swarm_id":id, "owner_key":self.public,
            "seq":seq, "name":listing.name, "model":model, "description":listing.description,
            "access_mode":listing.access_mode, "open_quota":listing.open_quota,
            "entry":{"key":self.public,"origin":origin}}),
        )?;
        docs::check_record(&value_j(&record)?, &wire::ed25519_verify, true, false)?;
        Ok(record)
    }
    fn changed(&self, listing: &Listing, model: &str, origin: &str) -> bool {
        self.state.record.as_ref().is_none_or(|r| {
            r["name"] != listing.name
                || r["model"] != model
                || r["description"] != listing.description
                || r["access_mode"] != listing.access_mode
                || r["open_quota"] != listing.open_quota.clone().unwrap_or(Value::Null)
                || r["entry"]["origin"] != origin
        })
    }
    /// One bounded work pass. Network failures are UI state (and durable retry debt),
    /// storage/validation failures are errors. Retry never sleeps or spins in the host.
    pub fn reconcile(&mut self, now: i64, owner: &OwnerState) -> Result<Status, Error> {
        if now < 0 {
            return Err(Error::new("swarm_time_invalid"));
        }
        let stop = self.state.listing.is_none()
            || matches!(
                owner,
                OwnerState::Paused | OwnerState::Left | OwnerState::Quit
            );
        if stop && self.state.record.is_some() && !self.state.withdraw {
            let mut state = self.state.clone();
            state.withdraw = true;
            state.next_attempt = 0;
            self.commit(state)?;
        }
        if now < self.state.next_attempt {
            return Ok(self.status());
        }
        let result = if self.state.withdraw {
            self.withdraw()
        } else if !stop {
            self.publish(now, owner)
        } else {
            return Ok(self.unlisted_status());
        };
        if let Err(error) = result {
            let mut state = self.state.clone();
            state.error = Some(error.code);
            // Source retries on each periodic tick; fixed bounded cadence, no tight retry loop.
            state.next_attempt = now.saturating_add(heartbeat_seconds()?);
            self.commit(state)?;
        }
        let mut status = self.status();
        if self.state.record.is_none() && stop && self.state.error.is_none() {
            status.phase = Phase::Unlisted;
        }
        Ok(status)
    }
    fn unlisted_status(&self) -> Status {
        let mut s = self.status();
        s.phase = Phase::Unlisted;
        s
    }
    fn withdraw(&mut self) -> Result<(), Error> {
        let r = self
            .state
            .record
            .as_ref()
            .ok_or_else(|| Error::new("swarm_publish_state_invalid"))?;
        let id = r["swarm_id"]
            .as_str()
            .ok_or_else(|| Error::new("swarm_publish_state_invalid"))?;
        let deletion = self.signed(
            json!({"protocol":docs::DELETE,"swarm_id":id,"owner_key":self.public,
            "seq":r["seq"].as_i64().ok_or_else(|| Error::new("swarm_publish_state_invalid"))? + 1}),
        )?;
        match self.post("/delete", &deletion) {
            Ok(_) => self.confirm_absent(id)?,
            // A delete can succeed while its reply is lost, or a record may never have arrived.
            Err(e) if e.code == "swarm_directory_swarm_unknown" => self.confirm_absent(id)?,
            Err(e) => return Err(e),
        }
        self.commit(State {
            listing: self.state.listing.clone(),
            last_at: self.state.last_at,
            ..State::default()
        })
    }
    // Confirm the target, not just an HTTP success, before retiring durable debt.
    fn confirm_absent(&self, id: &str) -> Result<(), Error> {
        match self.directory.call("GET", &format!("/swarms/{id}"), b"") {
            Err(e) if e.code == "swarm_directory_swarm_unknown" => Ok(()),
            Err(e) => Err(e),
            Ok(_) => Err(Error::new("swarm_withdraw_unconfirmed")),
        }
    }
    fn confirm_record(&self, record: &Value) -> Result<(), Error> {
        let id = record["swarm_id"]
            .as_str()
            .ok_or_else(|| Error::new("swarm_publish_state_invalid"))?;
        let found = self.directory.call("GET", &format!("/swarms/{id}"), b"")?;
        if found["swarm"]["record"] == *record {
            Ok(())
        } else {
            Err(Error::new("swarm_record_unconfirmed"))
        }
    }
    fn publish(&mut self, now: i64, owner: &OwnerState) -> Result<(), Error> {
        let (status, members, model) = match owner {
            OwnerState::Serving {
                model,
                members,
                entry_origin,
            } => {
                let listing = self
                    .state
                    .listing
                    .as_ref()
                    .ok_or_else(|| Error::new("swarm_consent_required"))?;
                if self.changed(listing, model, entry_origin) {
                    let mut state = self.state.clone();
                    state.record = Some(self.record(listing, model, entry_origin)?);
                    state.record_accepted = false;
                    state.last_beat = None;
                    self.commit(state)?;
                }
                ("serving", *members, Some(model.as_str()))
            }
            OwnerState::Warming { members } if self.state.record.is_some() => {
                ("warming", *members, None)
            }
            _ => return Ok(()),
        };
        let record = self
            .state
            .record
            .clone()
            .ok_or_else(|| Error::new("swarm_publish_state_invalid"))?;
        if !self.state.record_accepted {
            match self.post("/records", &record) {
                Ok(_) => self.confirm_record(&record)?,
                Err(e) if e.code == "swarm_directory_record_seq_stale" => {
                    self.confirm_record(&record)?
                }
                Err(e) => return Err(e),
            }
            let mut state = self.state.clone();
            state.record_accepted = true;
            self.commit(state)?;
        }
        let fields = json!({"status":status,"members":members,"served_model":model});
        let interval = heartbeat_seconds()?;
        if self.state.beat_fields.as_ref() == Some(&fields)
            && self
                .state
                .last_beat
                .is_some_and(|at| now < at.saturating_add(interval))
        {
            return Ok(());
        }
        for attempt in 0..2 {
            let at = now.max(self.state.last_at.saturating_add(1)); // protocol sequence, not a wait
            let document = self.signed(json!({"protocol":docs::HEARTBEAT,"swarm_id":record["swarm_id"],
                "signer_key":self.public,"status":status,"members":members,"served_model":model,"at":at}))?;
            docs::check(
                &value_j(&document)?,
                docs::HEARTBEAT,
                &wire::ed25519_verify,
                true,
                false,
            )?;
            let mut state = self.state.clone();
            state.last_at = at;
            self.commit(state)?;
            match self.post("/heartbeat", &document) {
                Ok(_) => {
                    let mut state = self.state.clone();
                    state.last_beat = Some(now);
                    state.beat_fields = Some(fields);
                    state.error = None;
                    state.next_attempt = 0;
                    self.commit(state)?;
                    return Ok(());
                }
                Err(e) if attempt == 0 && e.code == "swarm_directory_heartbeat_stale" => continue,
                Err(e) => return Err(e),
            }
        }
        Err(Error::new("swarm_directory_heartbeat_stale"))
    }
}
