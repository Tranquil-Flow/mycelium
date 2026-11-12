// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-app listing lifecycle, based on v0.1.4 swarm_publish.py.
//!
//! Only the closed v1 record/heartbeat/delete documents cross DirectoryPort. The host
//! supplies qualified serving facts, not a general app/status JSON object. Opt-in and
//! withdrawal debt are durable before any network effect. One owner drives this handle;
//! it starts no thread. Call reconcile on transitions and at the source heartbeat cadence.
//! Unlike v0.1.4's best-effort offline beat, stop/unlist deletes the listing and retains
//! debt across restart. Directory deletion retires an ID: a later run gets a fresh ID.
//!
//! A pass is a sequence of directory exchanges: `begin` plans the first (committing what must
//! be durable before it is sent), the caller sends it, `advance` applies the reply and plans
//! the next. `reconcile` drives a whole pass on this handle; a host that must not hold its own
//! lock across directory I/O (`app::AppPublisher`) sends each exchange without it.
//!
//! Keys: `memory`/`open` sign with the caller's fixed key. `create_owned`/`open_owned` keep a
//! key per listing inside the private state file: one listing (swarm id) is signed by one key,
//! kept until its withdrawal is confirmed, then replaced, so listings are not linkable by key.
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
use std::{
    fs,
    io::Read,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    sync::Arc,
    time::Instant,
};

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
    /// The same exchange, also ended by `deadline` (a caller's close budget). A port without
    /// its own deadline handling refuses an expired deadline and otherwise answers as `call`.
    fn call_until(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        deadline: Instant,
    ) -> Result<Value, Error> {
        if Instant::now() >= deadline {
            return Err(Error::new("swarm_directory_deadline_exceeded"));
        }
        self.call(method, path, body)
    }
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
/// What the owner's `swarm` status shows of the listing besides [`Status`] (`SwarmPublisher.status`,
/// swarm_publish.py:456-472 @ f4e366e9): the listing's name and access mode (the record's once one
/// is signed, else the owner's opted-in choice), the listing key, the record's entry origin, whether
/// the directory accepted the record, and the status the last accepted heartbeat carried.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Shown {
    pub name: Option<String>,
    pub access_mode: Option<String>,
    pub key: String,
    pub entry_origin: Option<String>,
    pub attached: bool,
    pub heartbeat: Option<String>,
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
fn file_storage<T>(r: std::io::Result<T>) -> Result<T, Error> {
    r.map_err(|_| Error::new("swarm_publish_storage_failed"))
}
fn fresh_key() -> Result<SigningKey, Error> {
    let mut seed = [0u8; 32];
    fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut seed))
        .map_err(|_| Error::new("swarm_entropy_unavailable"))?;
    Ok(SigningKey::from_bytes(&seed))
}
fn suffixed(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(suffix);
    name.into()
}
fn remove_if_present(path: &Path) -> Result<(), Error> {
    match fs::remove_file(path) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => {
            Err(Error::new("swarm_publish_storage_failed"))
        }
        _ => Ok(()),
    }
}
/// Make a rename or removal in `path`'s directory durable.
pub(crate) fn sync_parent(path: &Path) -> Result<(), Error> {
    let parent = path
        .parent()
        .ok_or_else(|| Error::new("swarm_publish_storage_failed"))?;
    file_storage(fs::File::open(parent).and_then(|d| d.sync_all()))
}
fn has_seed_column(db: &Connection) -> Result<bool, Error> {
    storage(db.query_row(
        "SELECT COUNT(*) FROM pragma_table_info('publisher') WHERE name='seed'",
        [],
        |r| r.get::<_, i64>(0),
    ))
    .map(|n| n == 1)
}
const OWNED_SCHEMA: &str = "PRAGMA synchronous=FULL; PRAGMA secure_delete=ON; CREATE TABLE publisher (id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, origin TEXT NOT NULL, state TEXT NOT NULL, seed BLOB);";

/// What a private state file holds, read without adopting it.
pub enum Stored {
    Absent,
    /// Provably nothing: an empty file, no publisher table or no row (an interrupted creation).
    /// Nothing can have been listed from it.
    Empty,
    Bound {
        origin: String,
        /// A record exists (listed, possibly listed, or owed a withdrawal).
        obligated: bool,
        listing: Option<Listing>,
        /// The listing key is stored in the file (`create_owned`), not elsewhere.
        keyed: bool,
    },
}

/// Who signs: the caller's fixed key, or one key per listing kept in the state file.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Keying {
    Fixed,
    PerListing,
}

/// One directory exchange of a pass, planned with its preconditions committed. Send it with
/// the port, then hand the reply to `advance`; the plan carries no lock.
pub struct Call {
    pub method: &'static str,
    pub path: String,
    pub body: Vec<u8>,
    op: Op,
    pass: Pass,
}
#[derive(Clone)]
struct Pass {
    now: i64,
    owner: OwnerState,
    stop: bool,
    /// `set_listing` changes since planning: a stale plan neither defers nor extends a pass.
    generation: u64,
}
enum Op {
    Delete { id: String },
    ConfirmAbsent,
    PostRecord { record: Value },
    ConfirmRecord { record: Value },
    Beat { attempt: u8, fields: Value },
}
pub enum Step {
    Done(Status),
    Call(Call),
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
    keying: Keying,
    generation: u64,
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
    /// Inspect a private state file without adopting it. A file that cannot be read as either
    /// provably empty or a parsed binding is an error, never treated as empty.
    pub fn inspect(path: &Path) -> Result<Stored, Error> {
        match fs::symlink_metadata(path) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Stored::Absent),
            Err(_) => return Err(Error::new("swarm_publish_storage_failed")),
            Ok(m) if !m.file_type().is_file() => {
                return Err(Error::new("swarm_publish_storage_failed"));
            }
            Ok(m) if m.len() == 0 => return Ok(Stored::Empty),
            Ok(_) => {}
        }
        // Read-write (never create): a hot journal left by a crash is rolled back first.
        let db = storage(Connection::open_with_flags(
            path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
        ))?;
        let table: Option<String> = storage(
            db.query_row(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='publisher'",
                [],
                |r| r.get(0),
            )
            .optional(),
        )?;
        if table.is_none() {
            return Ok(Stored::Empty);
        }
        let row: Option<(String, String)> = storage(
            db.query_row("SELECT origin,state FROM publisher WHERE id=1", [], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })
            .optional(),
        )?;
        let Some((origin, text)) = row else {
            return Ok(Stored::Empty);
        };
        let state: State =
            serde_json::from_str(&text).map_err(|_| Error::new("swarm_publish_state_invalid"))?;
        // The B12 layout has no seed column: its key lives in the host's key file.
        let keyed = has_seed_column(&db)?
            && storage(db.query_row(
                "SELECT seed IS NOT NULL FROM publisher WHERE id=1",
                [],
                |r| r.get::<_, bool>(0),
            ))?;
        Ok(Stored::Bound {
            origin,
            obligated: state.record.is_some() || state.withdraw,
            listing: state.listing,
            keyed,
        })
    }
    /// Create a private state file with a fresh listing key, atomically: it is written and
    /// synced under a temporary name, then renamed into place and the directory synced. A crash
    /// leaves either the previous file or the complete new one (plus temporary residue that the
    /// next creation removes). Replaces only state the caller has proven owes nothing.
    pub fn create_owned(
        path: &Path,
        directory: Arc<dyn DirectoryPort>,
        origin: &str,
        listing: Option<Listing>,
    ) -> Result<Self, Error> {
        let key = fresh_key()?;
        let public = wire::key_text(&key.verifying_key().to_bytes())?;
        let temporary = suffixed(path, ".tmp");
        for stale in [
            suffixed(&temporary, "-journal"),
            temporary.clone(),
            // A journal of the provably-empty file being replaced must not meet the new one.
            suffixed(path, "-journal"),
        ] {
            remove_if_present(&stale)?;
        }
        file_storage(
            fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(&temporary),
        )?;
        let text = serde_json::to_string(&State {
            listing,
            ..State::default()
        })
        .map_err(|_| Error::new("swarm_publish_state_invalid"))?;
        let db = storage(Connection::open(&temporary))?;
        storage(db.execute_batch(OWNED_SCHEMA))?;
        storage(db.execute(
            "INSERT INTO publisher (id,owner,origin,state,seed) VALUES(1,?1,?2,?3,?4)",
            rusqlite::params![public, origin, text, key.to_bytes().as_slice()],
        ))?;
        storage(db.close().map_err(|(_, e)| e))?;
        file_storage(fs::File::open(&temporary).and_then(|f| f.sync_all()))?;
        file_storage(fs::rename(&temporary, path))?;
        sync_parent(path)?;
        Self::open_owned(path, directory, origin, None)
    }
    /// Reopen a state file holding its own listing key. `legacy` is the B12 installation key
    /// for a file without one (its listing or debt keeps that key; it is moved into the file).
    pub fn open_owned(
        path: &Path,
        directory: Arc<dyn DirectoryPort>,
        origin: &str,
        legacy: Option<SigningKey>,
    ) -> Result<Self, Error> {
        let meta = file_storage(fs::symlink_metadata(path))?;
        if !meta.file_type().is_file() || meta.mode() & 0o077 != 0 {
            return Err(Error::new("swarm_publish_storage_failed"));
        }
        let db = storage(Connection::open_with_flags(
            path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
        ))?;
        storage(db.execute_batch("PRAGMA synchronous=FULL; PRAGMA secure_delete=ON;"))?;
        let keyed = has_seed_column(&db)?;
        let seed: Option<Vec<u8>> = if keyed {
            storage(db.query_row("SELECT seed FROM publisher WHERE id=1", [], |r| r.get(0)))?
        } else {
            None
        };
        let (key, adopt) = match (seed, legacy) {
            (Some(seed), _) => (
                SigningKey::from_bytes(
                    &seed
                        .try_into()
                        .map_err(|_| Error::new("swarm_publisher_key_invalid"))?,
                ),
                false,
            ),
            (None, Some(key)) => (key, true),
            (None, None) => return Err(Error::new("swarm_publisher_key_invalid")),
        };
        // The binding (owner key, origin, state, record signature) is checked before anything
        // is written: a refused file stays byte-identical.
        let mut publisher = Self::from_connection(db, key, directory, origin)?;
        publisher.keying = Keying::PerListing;
        if adopt {
            // The B12 key moves into the binding it already signs, in one transaction.
            let seed = publisher.key.to_bytes();
            let tx = storage(publisher.db.transaction())?;
            if !keyed {
                storage(tx.execute_batch("ALTER TABLE publisher ADD COLUMN seed BLOB;"))?;
            }
            storage(tx.execute("UPDATE publisher SET seed=?1 WHERE id=1", [seed.as_slice()]))?;
            storage(tx.commit())?;
        }
        Ok(publisher)
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
                "INSERT INTO publisher (id,owner,origin,state) VALUES(1,?1,?2,?3)",
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
            keying: Keying::Fixed,
            generation: 0,
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
    /// Commit a state together with a fresh listing key; the old key is overwritten in the same
    /// transaction (secure_delete zeroes the freed bytes).
    fn commit_rekeyed(&mut self, state: State) -> Result<(), Error> {
        let key = fresh_key()?;
        let public = wire::key_text(&key.verifying_key().to_bytes())?;
        let text =
            serde_json::to_string(&state).map_err(|_| Error::new("swarm_publish_state_invalid"))?;
        storage(self.db.execute(
            "UPDATE publisher SET state=?1, owner=?2, seed=?3 WHERE id=1",
            rusqlite::params![text, public, key.to_bytes().as_slice()],
        ))?;
        self.state = state;
        self.key = key;
        self.public = public;
        Ok(())
    }
    /// The port this handle's documents are exchanged with.
    pub fn port(&self) -> Arc<dyn DirectoryPort> {
        self.directory.clone()
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
    /// What the status shows of this listing (read-only; see [`Shown`]).
    pub fn shown(&self) -> Shown {
        let record = self.state.record.as_ref();
        let text = |value: Option<&Value>| value.and_then(Value::as_str).map(str::to_owned);
        let listing = self.state.listing.as_ref();
        Shown {
            name: text(record.map(|r| &r["name"])).or_else(|| listing.map(|l| l.name.clone())),
            access_mode: text(record.map(|r| &r["access_mode"]))
                .or_else(|| listing.map(|l| l.access_mode.clone())),
            key: self.public.clone(),
            entry_origin: text(record.map(|r| &r["entry"]["origin"])),
            attached: record.is_some() && self.state.record_accepted,
            heartbeat: self
                .state
                .last_beat
                .and_then(|_| text(self.state.beat_fields.as_ref().map(|f| &f["status"]))),
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
        self.commit(state)?;
        self.generation = self.generation.wrapping_add(1);
        Ok(())
    }
    fn signed(&self, body: Value) -> Result<Value, Error> {
        let doc = value_j(&body)?;
        let signature = self.key.sign(&wire::signing_bytes(&doc)?);
        let signed = doc.with("sig", J::str(&wire::b64url(&signature.to_bytes())));
        serde_json::from_slice(&wire::canonical(&signed)?)
            .map_err(|_| Error::new("swarm_document_invalid"))
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
    fn stopping(&self, owner: &OwnerState) -> bool {
        self.state.listing.is_none()
            || matches!(
                owner,
                OwnerState::Paused | OwnerState::Left | OwnerState::Quit
            )
    }
    /// The durable part of a stop, without any exchange: a live record becomes withdrawal debt.
    pub fn mark(&mut self, owner: &OwnerState) -> Result<(), Error> {
        if self.stopping(owner) && self.state.record.is_some() && !self.state.withdraw {
            let mut state = self.state.clone();
            state.withdraw = true;
            state.next_attempt = 0;
            self.commit(state)?;
        }
        Ok(())
    }
    /// One bounded work pass. Network failures are UI state (and durable retry debt),
    /// storage/validation failures are errors. Retry never sleeps or spins in the host.
    pub fn reconcile(&mut self, now: i64, owner: &OwnerState) -> Result<Status, Error> {
        let step = self.begin(now, owner)?;
        self.drive(step)
    }
    /// Send each planned exchange through this handle's port until the pass is done.
    pub fn drive(&mut self, mut step: Step) -> Result<Status, Error> {
        loop {
            match step {
                Step::Done(status) => return Ok(status),
                Step::Call(call) => {
                    let reply = self.directory.call(call.method, &call.path, &call.body);
                    step = self.advance(call, reply)?;
                }
            }
        }
    }
    /// Plan a pass: commit what must be durable first, then name its first exchange.
    pub fn begin(&mut self, now: i64, owner: &OwnerState) -> Result<Step, Error> {
        if now < 0 {
            return Err(Error::new("swarm_time_invalid"));
        }
        let stop = self.stopping(owner);
        self.mark(owner)?;
        if now < self.state.next_attempt {
            return Ok(Step::Done(self.status()));
        }
        let pass = Pass {
            now,
            owner: owner.clone(),
            stop,
            generation: self.generation,
        };
        let next = if self.state.withdraw {
            self.delete_call(&pass).map(Some)
        } else if !stop {
            self.publish_step(&pass)
        } else {
            return Ok(Step::Done(self.unlisted_status()));
        };
        self.finish(&pass, next)
    }
    /// Apply one exchange's reply and plan the next.
    pub fn advance(&mut self, call: Call, reply: Result<Value, Error>) -> Result<Step, Error> {
        let next = self.apply(&call.pass, call.op, reply);
        self.finish(&call.pass, next)
    }
    fn finish(&mut self, pass: &Pass, next: Result<Option<Call>, Error>) -> Result<Step, Error> {
        match next {
            Ok(Some(call)) => return Ok(Step::Call(call)),
            Ok(None) => {}
            Err(error) => {
                let mut state = self.state.clone();
                state.error = Some(error.code);
                // Source retries on each periodic tick; fixed bounded cadence, no tight retry
                // loop. An owner preference changed during the exchange is not deferred by it.
                if self.generation == pass.generation {
                    state.next_attempt = pass.now.saturating_add(heartbeat_seconds()?);
                }
                self.commit(state)?;
            }
        }
        let mut status = self.status();
        if self.state.record.is_none() && pass.stop && self.state.error.is_none() {
            status.phase = Phase::Unlisted;
        }
        Ok(Step::Done(status))
    }
    fn unlisted_status(&self) -> Status {
        let mut s = self.status();
        s.phase = Phase::Unlisted;
        s
    }
    fn post_call(&self, pass: &Pass, path: &str, document: &Value, op: Op) -> Result<Call, Error> {
        Ok(Call {
            method: "POST",
            path: path.into(),
            body: wire::canonical(&value_j(document)?)?,
            op,
            pass: pass.clone(),
        })
    }
    fn get_call(pass: &Pass, id: &str, op: Op) -> Call {
        Call {
            method: "GET",
            path: format!("/swarms/{id}"),
            body: Vec::new(),
            op,
            pass: pass.clone(),
        }
    }
    fn delete_call(&self, pass: &Pass) -> Result<Call, Error> {
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
        self.post_call(pass, "/delete", &deletion, Op::Delete { id: id.into() })
    }
    fn apply(
        &mut self,
        pass: &Pass,
        op: Op,
        reply: Result<Value, Error>,
    ) -> Result<Option<Call>, Error> {
        const UNKNOWN: &str = "swarm_directory_swarm_unknown";
        // A plan made before an owner preference change does not extend the pass; the next
        // pass plans from the new preference at once (its retry slot is not deferred).
        let current = self.generation == pass.generation;
        match op {
            // A delete can succeed while its reply is lost, or a record may never have arrived.
            // Confirm the target, not just an HTTP success, before retiring durable debt.
            Op::Delete { id } => match reply {
                Ok(_) => Ok(Some(Self::get_call(pass, &id, Op::ConfirmAbsent))),
                Err(e) if e.code == UNKNOWN => {
                    Ok(Some(Self::get_call(pass, &id, Op::ConfirmAbsent)))
                }
                Err(e) => Err(e),
            },
            Op::ConfirmAbsent => match reply {
                Err(e) if e.code == UNKNOWN => {
                    let state = State {
                        listing: self.state.listing.clone(),
                        last_at: self.state.last_at,
                        ..State::default()
                    };
                    // The withdrawn listing's key is destroyed; the next listing gets its own.
                    match self.keying {
                        Keying::Fixed => self.commit(state)?,
                        Keying::PerListing => self.commit_rekeyed(state)?,
                    }
                    Ok(None)
                }
                Err(e) => Err(e),
                Ok(_) => Err(Error::new("swarm_withdraw_unconfirmed")),
            },
            Op::PostRecord { record } => match reply {
                Ok(_) => Ok(Some(Self::confirm_record_call(pass, record)?)),
                Err(e) if e.code == "swarm_directory_record_seq_stale" => {
                    Ok(Some(Self::confirm_record_call(pass, record)?))
                }
                Err(e) => Err(e),
            },
            Op::ConfirmRecord { record } => {
                if reply?["swarm"]["record"] != record {
                    return Err(Error::new("swarm_record_unconfirmed"));
                }
                let mut state = self.state.clone();
                state.record_accepted = true;
                self.commit(state)?;
                if !current {
                    return Ok(None);
                }
                self.beat_step(pass, 0)
            }
            Op::Beat { attempt, fields } => match reply {
                Ok(_) => {
                    let mut state = self.state.clone();
                    state.last_beat = Some(pass.now);
                    state.beat_fields = Some(fields);
                    state.error = None;
                    state.next_attempt = 0;
                    self.commit(state)?;
                    Ok(None)
                }
                Err(e)
                    if attempt == 0 && e.code == "swarm_directory_heartbeat_stale" && current =>
                {
                    self.beat_call(pass, 1, fields).map(Some)
                }
                Err(e) => Err(e),
            },
        }
    }
    fn confirm_record_call(pass: &Pass, record: Value) -> Result<Call, Error> {
        let id = record["swarm_id"]
            .as_str()
            .ok_or_else(|| Error::new("swarm_publish_state_invalid"))?
            .to_owned();
        Ok(Self::get_call(pass, &id, Op::ConfirmRecord { record }))
    }
    fn publish_step(&mut self, pass: &Pass) -> Result<Option<Call>, Error> {
        match &pass.owner {
            OwnerState::Serving {
                model,
                entry_origin,
                ..
            } => {
                let listing = self
                    .state
                    .listing
                    .clone()
                    .ok_or_else(|| Error::new("swarm_consent_required"))?;
                if self.changed(&listing, model, entry_origin) {
                    let mut state = self.state.clone();
                    state.record = Some(self.record(&listing, model, entry_origin)?);
                    state.record_accepted = false;
                    state.last_beat = None;
                    self.commit(state)?;
                }
            }
            OwnerState::Warming { .. } if self.state.record.is_some() => {}
            _ => return Ok(None),
        }
        let record = self
            .state
            .record
            .clone()
            .ok_or_else(|| Error::new("swarm_publish_state_invalid"))?;
        if !self.state.record_accepted {
            return self
                .post_call(
                    pass,
                    "/records",
                    &record,
                    Op::PostRecord {
                        record: record.clone(),
                    },
                )
                .map(Some);
        }
        self.beat_step(pass, 0)
    }
    fn beat_step(&mut self, pass: &Pass, attempt: u8) -> Result<Option<Call>, Error> {
        let (status, members, model) = match &pass.owner {
            OwnerState::Serving { model, members, .. } => {
                ("serving", *members, Some(model.as_str()))
            }
            OwnerState::Warming { members } => ("warming", *members, None),
            _ => return Ok(None),
        };
        let fields = json!({"status":status,"members":members,"served_model":model});
        let interval = heartbeat_seconds()?;
        if self.state.beat_fields.as_ref() == Some(&fields)
            && self
                .state
                .last_beat
                .is_some_and(|at| pass.now < at.saturating_add(interval))
        {
            return Ok(None);
        }
        self.beat_call(pass, attempt, fields).map(Some)
    }
    fn beat_call(&mut self, pass: &Pass, attempt: u8, fields: Value) -> Result<Call, Error> {
        let swarm_id = self
            .state
            .record
            .as_ref()
            .map(|r| r["swarm_id"].clone())
            .ok_or_else(|| Error::new("swarm_publish_state_invalid"))?;
        let at = pass.now.max(self.state.last_at.saturating_add(1)); // protocol sequence, not a wait
        let document = self.signed(json!({"protocol":docs::HEARTBEAT,"swarm_id":swarm_id,
            "signer_key":self.public,"status":fields["status"],"members":fields["members"],
            "served_model":fields["served_model"],"at":at}))?;
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
        self.post_call(pass, "/heartbeat", &document, Op::Beat { attempt, fields })
    }
}
