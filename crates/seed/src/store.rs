// SPDX-License-Identifier: AGPL-3.0-only
//! Durable seed state on one sqlite file (`SqliteInviteRegistry` + `SqliteSeedState` of v0.1.4, reduced to what
//! this slice commits): the consumed-invite table with v0.1.4's exact DDL, the identity binding
//! (`bind_identity`, `seed_state_identity_mismatch`), the member table, and the exact signed acceptances /
//! renewals the seed answered with (a retry must receive the same bytes).
//!
//! The row tables of v0.1.4 (`seed_members`, `seed_join_acceptances`, ...) are created and migrated exactly as
//! v0.1.4 does on every open ([`crate::v014::initialize`]); the Rust store itself keeps the member table as
//! - `rs_seed_table`: one small snapshot of everything except the two append-only journals
//!   ([`SeedTable::core_snapshot`], marked `"journals": "rows.v1"`);
//! - `rs_seed_renewals`: one row per committed heartbeat renewal, in commit order (v0.1.4
//!   `seed_heartbeat_renewals`, one INSERT per heartbeat in `commit_heartbeat_renewal`, never pruned);
//! - `rs_seed_emitted`: one row per emitted message id (v0.1.4 `seed_emitted_messages`, never pruned).
//!
//! So a member message writes its own rows plus the small snapshot, never the history (lane SN: the single
//! snapshot that also carried both journals reached 15.6 MB and was rewritten under the coordinator lock on every
//! message). A database whose snapshot still carries the journals inline (every Rust seed up to m4/main 7b2c8472)
//! is MIGRATED once at open, in one transaction. A database whose v0.1.4 tables hold state and that has no Rust
//! snapshot yet is IMPORTED once, in one transaction (round 14); after that the Rust tables are authoritative and
//! the v0.1.4 row tables are no longer written (a v0.1.4 seed must not be started on an imported state root).
//! An older Rust seed refuses the migrated database (`seed_state_corrupt`: its snapshot lacks the journals)
//! rather than reading it without its history.
use crate::{coordinator::SeedError, v014};
use budgets::{BudgetId, Entries};
use protocol::seed_members::{JOURNALS_KEY, JOURNALS_ROWS_V1, Journal, SeedTable};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::Value;
use std::{
    fs,
    os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt},
    path::Path,
    time::Duration,
};

#[cfg(test)]
mod real_copy_bench;

const OWNER_DIR_MODE: u32 = 0o700;
const OWNER_FILE_MODE: u32 = 0o600;
const MODE_BITS: u32 = 0o777;

fn refuse<T>(code: &str) -> Result<T, SeedError> {
    Err(SeedError::new(code))
}

/// `_sqlite_state_error_code`: a corrupt or not-a-database file is `seed_state_corrupt`; every other sqlite failure
/// (a busy or locked database, a refused write) is `seed_state_unavailable`.
fn sqlite(error: &rusqlite::Error) -> SeedError {
    let corrupt = matches!(
        error,
        rusqlite::Error::SqliteFailure(f, _)
            if matches!(f.code, rusqlite::ErrorCode::DatabaseCorrupt | rusqlite::ErrorCode::NotADatabase)
    );
    SeedError::new(if corrupt {
        "seed_state_corrupt"
    } else {
        "seed_state_unavailable"
    })
}

fn is_constraint(error: &rusqlite::Error) -> bool {
    matches!(
        error,
        rusqlite::Error::SqliteFailure(f, _) if f.code == rusqlite::ErrorCode::ConstraintViolation
    )
}

/// `SqliteInviteRegistry._prepare_path` (POSIX): no symlink on the path, a private (0700, own uid) parent, a
/// single-link private (0600) database file created exclusively when absent.
fn prepare_path(database: &Path) -> Result<(), SeedError> {
    let invalid = || SeedError::new("invite_registry_path_invalid");
    let permissions = || SeedError::new("invite_registry_permissions_invalid");
    let uid = rustix::process::getuid().as_raw();
    let parent = database.parent().ok_or_else(invalid)?;
    let mut missing = Vec::new();
    let mut nearest = parent;
    while fs::symlink_metadata(nearest).is_err() {
        missing.push(nearest);
        match nearest.parent() {
            Some(up) if up != nearest => nearest = up,
            _ => break,
        }
    }
    for ancestor in nearest.ancestors() {
        let metadata = fs::symlink_metadata(ancestor).map_err(|_| invalid())?;
        if metadata.file_type().is_symlink() {
            return Err(invalid());
        }
    }
    let metadata = fs::symlink_metadata(nearest).map_err(|_| invalid())?;
    if !metadata.is_dir() {
        return Err(invalid());
    }
    for directory in missing.into_iter().rev() {
        fs::DirBuilder::new()
            .mode(OWNER_DIR_MODE)
            .create(directory)
            .map_err(|_| invalid())?;
        fs::set_permissions(directory, fs::Permissions::from_mode(OWNER_DIR_MODE))
            .map_err(|_| invalid())?;
    }
    let metadata = fs::symlink_metadata(parent).map_err(|_| invalid())?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(invalid());
    }
    if metadata.mode() & MODE_BITS != OWNER_DIR_MODE || metadata.uid() != uid {
        return Err(permissions());
    }
    match fs::symlink_metadata(database) {
        Ok(metadata) => {
            if !metadata.is_file() || metadata.file_type().is_symlink() {
                return Err(invalid());
            }
            if metadata.mode() & MODE_BITS != OWNER_FILE_MODE
                || metadata.uid() != uid
                || metadata.nlink() != 1
            {
                return Err(permissions());
            }
        }
        Err(_) => {
            let file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(OWNER_FILE_MODE)
                .custom_flags(rustix::fs::OFlags::NOFOLLOW.bits() as i32)
                .open(database)
                .map_err(|_| invalid())?;
            let metadata = file.metadata().map_err(|_| invalid())?;
            if !metadata.is_file()
                || metadata.mode() & MODE_BITS != OWNER_FILE_MODE
                || metadata.uid() != uid
                || metadata.nlink() != 1
            {
                return Err(permissions());
            }
        }
    }
    Ok(())
}

/// The one INSERT of the consumed-invite table; a primary-key conflict is a replay.
fn insert_consumed(
    tx: &rusqlite::Transaction<'_>,
    nonce: &str,
    consumed_at: f64,
    expires_at: f64,
) -> Result<(), SeedError> {
    match tx.execute(
        "INSERT INTO consumed_invites (nonce, consumed_at, expires_at) VALUES (?, ?, ?)",
        params![nonce, consumed_at, expires_at],
    ) {
        Ok(_) => Ok(()),
        Err(rusqlite::Error::SqliteFailure(f, _))
            if f.code == rusqlite::ErrorCode::ConstraintViolation =>
        {
            refuse("invite_replayed")
        }
        Err(e) => Err(sqlite(&e)),
    }
}

/// What the database is bound to (`bind_identity`).
pub struct Binding<'a> {
    pub swarm_id: &'a str,
    pub seed_node_id: &'a str,
    pub seed_key_digest: &'a str,
}

/// The durable coordinator binding as stored (`SqliteSeedState.identity_binding`).
pub struct StoredBinding {
    pub swarm_id: String,
    pub seed_node_id: String,
    pub seed_key_digest: String,
}

/// `SqliteSeedState.identity_binding`: read the binding of an EXISTING database without creating or changing
/// anything (read-only connection); a missing table, key or a foreign schema is `seed_state_identity_missing`.
pub fn read_binding(database: &Path) -> Result<StoredBinding, SeedError> {
    let connection = Connection::open_with_flags(
        database,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|e| sqlite(&e))?;
    let get = |key: &str| -> Result<String, SeedError> {
        connection
            .query_row(
                "SELECT value FROM seed_metadata WHERE key = ?",
                [key],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|_| SeedError::new("seed_state_identity_missing"))?
            .ok_or_else(|| SeedError::new("seed_state_identity_missing"))
    };
    Ok(StoredBinding {
        swarm_id: get("swarm_id")?,
        seed_node_id: get("seed_node_id")?,
        seed_key_digest: get("seed_key_digest")?,
    })
}

/// `SqliteSeedState(database).authority_state()["authority_generation"]` of an EXISTING database, read-only: the
/// stored table's generation (1 before the first commit).
pub fn read_authority_generation(database: &Path) -> Result<u64, SeedError> {
    let connection = Connection::open_with_flags(
        database,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|e| sqlite(&e))?;
    let text: Option<String> = connection
        .query_row(
            "SELECT snapshot FROM rs_seed_table WHERE id = 1",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| sqlite(&e))?;
    let Some(text) = text else {
        // No Rust snapshot yet: a v0.1.4 database keeps the generation in its metadata (1 before the first commit).
        return Ok(v014::authority_generation(&connection)?.unwrap_or(1));
    };
    let snapshot: Value =
        serde_json::from_str(&text).map_err(|_| SeedError::new("seed_state_corrupt"))?;
    let table = SeedTable::restore(&snapshot, Box::new(String::new))
        .map_err(|_| SeedError::new("seed_state_corrupt"))?;
    table.authority_state()["authority_generation"]
        .as_u64()
        .ok_or_else(|| SeedError::new("seed_state_corrupt"))
}

pub struct Store {
    connection: Connection,
}

/// One invite nonce consumed by a commit.
pub struct Consume<'a> {
    pub nonce: &'a str,
    pub consumed_at: f64,
    pub expires_at: f64,
}

/// One exact signed envelope the seed answered with.
pub struct Envelope<'a> {
    pub kind: &'a str,
    pub key: &'a str,
    pub value: &'a Value,
}

impl Store {
    /// `SqliteSeedState(database)` + `bind_identity`: the path rules, the invitation registry's table, the v0.1.4
    /// schema (created, migrated, validated), the one-time import of a v0.1.4 database, and the identity binding.
    /// `busy` replaces the 30 s busy timeouts of both connections (a bounded wait for tests).
    pub fn open_with(
        database: &Path,
        binding: &Binding<'_>,
        busy: Option<Duration>,
    ) -> Result<Self, SeedError> {
        prepare_path(database)?;
        let connection = initialize(database, busy)?;
        connection
            .execute_batch(
                "CREATE TABLE IF NOT EXISTS rs_seed_table (
                     id INTEGER PRIMARY KEY CHECK (id = 1),
                     snapshot TEXT NOT NULL
                 );
                 CREATE TABLE IF NOT EXISTS rs_seed_envelopes (
                     kind TEXT NOT NULL,
                     key TEXT NOT NULL,
                     envelope TEXT NOT NULL,
                     PRIMARY KEY (kind, key)
                 ) WITHOUT ROWID;
                 CREATE TABLE IF NOT EXISTS rs_seed_renewals (
                     ordinal INTEGER PRIMARY KEY,
                     node_id TEXT NOT NULL,
                     generation INTEGER NOT NULL,
                     heartbeat_message_id TEXT NOT NULL,
                     row TEXT NOT NULL,
                     UNIQUE (node_id, generation, heartbeat_message_id)
                 );
                 CREATE TABLE IF NOT EXISTS rs_seed_emitted (
                     message_id TEXT PRIMARY KEY NOT NULL
                 ) WITHOUT ROWID;",
            )
            .map_err(|e| sqlite(&e))?;
        let mut store = Self { connection };
        store.import_v014()?;
        store.migrate_single_snapshot()?;
        store.bind(binding)?;
        Ok(store)
    }

    /// The one-time migration of a snapshot that still carries both journals inline: its renewal rows (in order)
    /// and emitted ids move to their row tables and the snapshot row is rewritten without them, in ONE
    /// transaction (a failure anywhere leaves the old snapshot as it was; the next open migrates again).
    fn migrate_single_snapshot(&mut self) -> Result<(), SeedError> {
        match read_snapshot(&self.connection)? {
            Some(snapshot) if !is_core(&snapshot) => {}
            _ => return Ok(()),
        }
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| sqlite(&e))?;
        // Another process may have migrated between the read and the reservation.
        if let Some(snapshot) = read_snapshot(&tx)?
            && !is_core(&snapshot)
        {
            let core = install_journals(&tx, &snapshot)?;
            tx.execute(
                "UPDATE rs_seed_table SET snapshot = ? WHERE id = 1",
                [core.to_string()],
            )
            .map_err(|e| sqlite(&e))?;
        }
        tx.commit().map_err(|e| sqlite(&e))
    }

    /// The one-time import of a v0.1.4 database (see the module documentation).
    fn import_v014(&mut self) -> Result<(), SeedError> {
        let held: Option<i64> = self
            .connection
            .query_row("SELECT 1 FROM rs_seed_table WHERE id = 1", [], |row| {
                row.get(0)
            })
            .optional()
            .map_err(|e| sqlite(&e))?;
        if held.is_some() {
            return Ok(());
        }
        let Some(imported) = v014::read(&self.connection)? else {
            return Ok(());
        };
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| sqlite(&e))?;
        let again: Option<i64> = tx
            .query_row("SELECT 1 FROM rs_seed_table WHERE id = 1", [], |row| {
                row.get(0)
            })
            .optional()
            .map_err(|e| sqlite(&e))?;
        if again.is_none() {
            for (kind, key, envelope) in &imported.envelopes {
                tx.execute(
                    "INSERT OR REPLACE INTO rs_seed_envelopes (kind, key, envelope) VALUES (?, ?, ?)",
                    params![kind, key, envelope.to_string()],
                )
                .map_err(|e| sqlite(&e))?;
            }
            let core = install_journals(&tx, &imported.snapshot)?;
            tx.execute(
                "INSERT INTO rs_seed_table (id, snapshot) VALUES (1, ?)",
                [core.to_string()],
            )
            .map_err(|e| sqlite(&e))?;
        }
        tx.commit().map_err(|e| sqlite(&e))
    }

    fn bind(&mut self, binding: &Binding<'_>) -> Result<(), SeedError> {
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| sqlite(&e))?;
        for (key, value) in [
            ("swarm_id", binding.swarm_id),
            ("seed_node_id", binding.seed_node_id),
            ("seed_key_digest", binding.seed_key_digest),
        ] {
            let have: Option<String> = tx
                .query_row(
                    "SELECT value FROM seed_metadata WHERE key = ?",
                    [key],
                    |row| row.get(0),
                )
                .optional()
                .map_err(|e| sqlite(&e))?;
            match have {
                None => {
                    tx.execute(
                        "INSERT INTO seed_metadata (key, value) VALUES (?, ?)",
                        params![key, value],
                    )
                    .map_err(|e| sqlite(&e))?;
                }
                Some(existing) if existing != value => {
                    return refuse("seed_state_identity_mismatch");
                }
                Some(_) => {}
            }
        }
        // `bind_identity`: the authority generation is bound at 1 and must stay a canonical integer >= 1.
        match v014::authority_generation(&tx) {
            Ok(Some(_)) => {}
            Ok(None) => {
                tx.execute(
                    "INSERT INTO seed_metadata (key, value) VALUES ('authority_generation', '1')",
                    [],
                )
                .map_err(|e| sqlite(&e))?;
            }
            Err(error) => return Err(error),
        }
        tx.commit().map_err(|e| sqlite(&e))
    }

    /// The durable `seed_key_digest` metadata: the key this database is bound to (changed only by a completed
    /// rotation).
    pub fn seed_key_digest(&self) -> Result<String, SeedError> {
        self.connection
            .query_row(
                "SELECT value FROM seed_metadata WHERE key = 'seed_key_digest'",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| sqlite(&e))?
            .ok_or_else(|| SeedError::new("seed_state_identity_missing"))
    }

    /// `complete_authority_rotation`'s write: the promoted table and the new `seed_key_digest` metadata in one
    /// transaction.
    pub fn commit_rotation_completion(
        &mut self,
        snapshot: &Value,
        journal: &Journal,
        new_seed_key_digest: &str,
        authority_generation: u64,
    ) -> Result<(), SeedError> {
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| sqlite(&e))?;
        if let Some(stored) = read_snapshot(&tx)?
            && !is_core(&stored)
        {
            install_journals(&tx, &stored)?;
        }
        append_journal(&tx, journal, Rows::Replace).map_err(|e| sqlite(&e))?;
        tx.execute(
            "UPDATE seed_metadata SET value = ? WHERE key = 'seed_key_digest'",
            [new_seed_key_digest],
        )
        .map_err(|e| sqlite(&e))?;
        tx.execute(
            "UPDATE seed_metadata SET value = ? WHERE key = 'authority_generation'",
            [authority_generation.to_string()],
        )
        .map_err(|e| sqlite(&e))?;
        tx.execute(
            "INSERT OR REPLACE INTO rs_seed_table (id, snapshot) VALUES (1, ?)",
            [snapshot.to_string()],
        )
        .map_err(|e| sqlite(&e))?;
        tx.commit().map_err(|e| sqlite(&e))
    }

    /// Hold the same SQLite write reservation as v0.1.4 `member_authority_guard`.
    /// The callback may only do bounded verification/commit, never network/observer work.
    pub fn with_authority<R>(
        &mut self,
        callback: impl FnOnce(Option<Value>) -> Result<R, SeedError>,
    ) -> Result<R, SeedError> {
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| sqlite(&e))?;
        let text: Option<String> = tx
            .query_row(
                "SELECT snapshot FROM rs_seed_table WHERE id = 1",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| sqlite(&e))?;
        let snapshot = text
            .map(|t| serde_json::from_str(&t).map_err(|_| SeedError::new("seed_state_corrupt")))
            .transpose()?;
        match callback(snapshot) {
            Ok(value) => {
                tx.commit().map_err(|e| sqlite(&e))?;
                Ok(value)
            }
            Err(error) => {
                tx.rollback().map_err(|e| sqlite(&e))?;
                Err(error)
            }
        }
    }

    /// The stored snapshot row (the small part of the table; the members `_ensure_current_member` reads).
    pub fn load_core(&self) -> Result<Option<Value>, SeedError> {
        read_snapshot(&self.connection)
    }

    /// The whole durable table: the snapshot row with the renewal rows (commit order) and the emitted ids. A
    /// snapshot that still carries its journals inline is split in memory.
    pub fn load_table(&self) -> Result<Option<Stored>, SeedError> {
        let Some(snapshot) = read_snapshot(&self.connection)? else {
            return Ok(None);
        };
        if !is_core(&snapshot) {
            let (core, journal) = split(&snapshot)?;
            return Ok(Some(Stored {
                core,
                renewals: journal.renewals,
                emitted: journal.emitted,
            }));
        }
        let corrupt = |_| SeedError::new("seed_state_corrupt");
        let mut statement = self
            .connection
            .prepare("SELECT row FROM rs_seed_renewals ORDER BY ordinal")
            .map_err(|e| sqlite(&e))?;
        let renewals = statement
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(|e| sqlite(&e))?
            .map(|text| {
                serde_json::from_str::<Value>(&text.map_err(|e| sqlite(&e))?).map_err(corrupt)
            })
            .collect::<Result<Vec<_>, _>>()?;
        let mut statement = self
            .connection
            .prepare("SELECT message_id FROM rs_seed_emitted")
            .map_err(|e| sqlite(&e))?;
        let emitted = statement
            .query_map([], |row| row.get::<_, String>(0))
            .map_err(|e| sqlite(&e))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| sqlite(&e))?;
        Ok(Some(Stored {
            core: snapshot,
            renewals,
            emitted,
        }))
    }

    pub fn load_envelope(&self, kind: &str, key: &str) -> Result<Option<Value>, SeedError> {
        let text: Option<String> = self
            .connection
            .query_row(
                "SELECT envelope FROM rs_seed_envelopes WHERE kind = ? AND key = ?",
                params![kind, key],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| sqlite(&e))?;
        text.map(|t| serde_json::from_str(&t).map_err(|_| SeedError::new("seed_state_corrupt")))
            .transpose()
    }

    /// `SqliteInviteRegistry.consume` on its own transaction.
    pub fn consume(&mut self, nonce: &str, now: f64, expires_at: f64) -> Result<(), SeedError> {
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| sqlite(&e))?;
        insert_consumed(&tx, nonce, now, expires_at)?;
        tx.commit().map_err(|e| sqlite(&e))
    }

    /// One atomic commit: the snapshot row, the journal rows this operation recorded, the consumed invite (a
    /// replay is `invite_replayed`) and the exact signed envelopes.
    ///
    /// `baseline` is the snapshot row this process last committed or loaded: another process may have committed
    /// since (an operator revoking a member while the seed runs, a second seed process). Only the rows this
    /// operation changed relative to the baseline are written over what the database holds now
    /// ([`merge_snapshot`]); a member row that would move backwards, or a member another process already
    /// added, is `seed_state_member_conflict` as v0.1.4's conditional `save_member` refuses. The journals are
    /// rows, so this operation's renewals and emitted ids are simply added (a renewal another process committed
    /// under the same key is replaced, an emitted id already present is kept), as the single snapshot merged them.
    /// Returns the snapshot row now durable.
    pub fn commit(
        &mut self,
        snapshot: &Value,
        baseline: &Value,
        journal: &Journal,
        consume: Option<&Consume<'_>>,
        envelopes: &[Envelope<'_>],
    ) -> Result<Value, SeedError> {
        let tx = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| sqlite(&e))?;
        let durable = match read_snapshot(&tx)? {
            // Written by an older Rust seed after this one opened: move its journals to rows first.
            Some(stored) if !is_core(&stored) => Some(install_journals(&tx, &stored)?),
            other => other,
        };
        let writes = match &durable {
            // v0.1.4 answers every refused conditional `save_member` (heartbeat, join, resume, advance) alike.
            Some(d) if d != baseline => merge_snapshot(d, baseline, snapshot)
                .map_err(|()| SeedError::new("seed_state_member_conflict"))?,
            _ => snapshot.clone(),
        };
        if let Some(c) = consume {
            insert_consumed(&tx, c.nonce, c.consumed_at, c.expires_at)?;
        }
        // `commit_join` / `commit_resume`: an integrity failure is `seed_join_conflict` / `seed_resume_conflict`;
        // every other write failure is the generic sqlite mapping (heartbeats: `seed_state_unavailable`).
        let conflict = |err: &rusqlite::Error| -> SeedError {
            if is_constraint(err) {
                if consume.is_some() {
                    return SeedError::new("seed_join_conflict");
                }
                if envelopes.iter().any(|e| e.kind == "resume") {
                    return SeedError::new("seed_resume_conflict");
                }
            }
            sqlite(err)
        };
        for e in envelopes {
            tx.execute(
                "INSERT OR REPLACE INTO rs_seed_envelopes (kind, key, envelope) VALUES (?, ?, ?)",
                params![e.kind, e.key, e.value.to_string()],
            )
            .map_err(|err| conflict(&err))?;
        }
        append_journal(&tx, journal, Rows::Replace).map_err(|e| conflict(&e))?;
        tx.execute(
            "INSERT OR REPLACE INTO rs_seed_table (id, snapshot) VALUES (1, ?)",
            [writes.to_string()],
        )
        .map_err(|e| conflict(&e))?;
        tx.commit().map_err(|e| conflict(&e))?;
        Ok(writes)
    }
}

/// The whole durable table as [`Store::load_table`] reads it.
pub struct Stored {
    pub core: Value,
    pub renewals: Vec<Value>,
    pub emitted: Vec<String>,
}

fn read_snapshot(connection: &Connection) -> Result<Option<Value>, SeedError> {
    let text: Option<String> = connection
        .query_row(
            "SELECT snapshot FROM rs_seed_table WHERE id = 1",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| sqlite(&e))?;
    text.map(|t| serde_json::from_str(&t).map_err(|_| SeedError::new("seed_state_corrupt")))
        .transpose()
}

/// Whether a stored snapshot row keeps its journals as rows (else it carries them inline).
fn is_core(snapshot: &Value) -> bool {
    snapshot.get(JOURNALS_KEY).is_some()
}

/// A snapshot with its journals inline, as the snapshot row (journals removed, marked) and the journal rows. The
/// document must restore as a table (the same check an open of the single snapshot made): else
/// `seed_state_corrupt`, nothing written.
fn split(document: &Value) -> Result<(Value, Journal), SeedError> {
    let corrupt = || SeedError::new("seed_state_corrupt");
    SeedTable::restore(document, Box::new(String::new)).map_err(|_| corrupt())?;
    let mut core = document.as_object().cloned().ok_or_else(corrupt)?;
    let renewals = match core.remove("renewals") {
        Some(Value::Array(rows)) => rows,
        _ => return Err(corrupt()),
    };
    let emitted = match core.remove("emitted") {
        Some(Value::Array(ids)) => ids
            .into_iter()
            .map(|id| match id {
                Value::String(id) => Ok(id),
                _ => Err(corrupt()),
            })
            .collect::<Result<Vec<_>, _>>()?,
        _ => return Err(corrupt()),
    };
    core.insert(JOURNALS_KEY.into(), Value::from(JOURNALS_ROWS_V1));
    Ok((Value::Object(core), Journal { renewals, emitted }))
}

/// Move the inline journals of `document` into their row tables (inside the caller's transaction) and return the
/// snapshot row without them. A renewal key repeated in the document keeps its FIRST row, the one the table's
/// lookup answered.
fn install_journals(tx: &Connection, document: &Value) -> Result<Value, SeedError> {
    let (core, journal) = split(document)?;
    append_journal(tx, &journal, Rows::KeepFirst).map_err(|e| sqlite(&e))?;
    Ok(core)
}

#[derive(Clone, Copy, PartialEq)]
enum Rows {
    /// A commit: a renewal row under a key already stored replaces it (the single snapshot's merge did).
    Replace,
    /// A migration: the first row of a key stays.
    KeepFirst,
}

/// Insert journal rows: each renewal row under its key `(node_id, generation, heartbeat_message_id)` after every
/// row already stored, each emitted id once.
fn append_journal(tx: &Connection, journal: &Journal, rows: Rows) -> Result<(), rusqlite::Error> {
    let insert = match rows {
        Rows::Replace => {
            "INSERT INTO rs_seed_renewals (node_id, generation, heartbeat_message_id, row) VALUES (?, ?, ?, ?) \
             ON CONFLICT (node_id, generation, heartbeat_message_id) DO UPDATE SET row = excluded.row"
        }
        Rows::KeepFirst => {
            "INSERT OR IGNORE INTO rs_seed_renewals (node_id, generation, heartbeat_message_id, row) \
             VALUES (?, ?, ?, ?)"
        }
    };
    let mut statement = tx.prepare_cached(insert)?;
    for row in &journal.renewals {
        // `SeedTable::restore` checked the key fields; a generation beyond SQLite's integer range cannot occur.
        let generation = i64::try_from(row["generation"].as_u64().unwrap_or_default())
            .map_err(|_| rusqlite::Error::IntegralValueOutOfRange(1, i64::MAX))?;
        statement.execute(params![
            row["node_id"].as_str().unwrap_or_default(),
            generation,
            row["heartbeat_message_id"].as_str().unwrap_or_default(),
            row.to_string()
        ])?;
    }
    let mut statement =
        tx.prepare_cached("INSERT OR IGNORE INTO rs_seed_emitted (message_id) VALUES (?)")?;
    for id in &journal.emitted {
        statement.execute([id])?;
    }
    Ok(())
}

/// The identity of a row of one snapshot array (the key the v0.1.4 table is unique on).
fn row_key(array: &str, row: &Value) -> String {
    let field = |name: &str| row[name].to_string();
    match array {
        "members" => field("node_id"),
        "replay" => format!(
            "{}|{}|{}",
            field("node_id"),
            field("generation"),
            field("message_id")
        ),
        "rotations" => field("authority_generation"),
        "acks" => format!(
            "{}|{}|{}",
            field("node_id"),
            field("member_generation"),
            field("authority_generation")
        ),
        "assignments" => field("assignment_id"),
        _ => row.to_string(),
    }
}

/// `save_member`'s `ON CONFLICT ... DO UPDATE ... WHERE`: the stored member must keep its key and endpoint, and
/// the new row must be a later generation, or the same incarnation and generation at a heartbeat sequence not
/// below the stored one.
fn member_may_replace(stored: &Value, new: &Value) -> bool {
    stored["verification_key_digest"] == new["verification_key_digest"]
        && stored["endpoint_id"] == new["endpoint_id"]
        && (new["generation"].as_u64() > stored["generation"].as_u64()
            || (new["generation"] == stored["generation"]
                && new["incarnation"] == stored["incarnation"]
                && new["last_heartbeat_sequence"].as_u64()
                    >= stored["last_heartbeat_sequence"].as_u64()))
}

/// Write `current`'s changes relative to `baseline` over `durable` (what the database holds now; snapshot rows,
/// the journals are rows of their own). `Err(())`
/// when a changed member row may not replace the stored one, or a member this process adds already exists.
fn merge_snapshot(durable: &Value, baseline: &Value, current: &Value) -> Result<Value, ()> {
    let mut merged = durable.clone();
    for array in [
        "members",
        "replay",
        "join_rows",
        "resume_rows",
        "assignments",
        "rotations",
        "acks",
    ] {
        let rows = |v: &Value| -> Vec<Value> { v[array].as_array().cloned().unwrap_or_default() };
        let (base_rows, now_rows) = (rows(baseline), rows(current));
        let mut out = rows(durable);
        let keyed =
            |list: &[Value]| -> Vec<String> { list.iter().map(|r| row_key(array, r)).collect() };
        let now_keys = keyed(&now_rows);
        // Rows this process removed (an expired replay id, a pruned row) leave the durable table too.
        for (row, key) in base_rows.iter().zip(keyed(&base_rows)) {
            if !now_keys.contains(&key) {
                out.retain(|r| row_key(array, r) != key && r != row);
            }
        }
        for (row, key) in now_rows.iter().zip(&now_keys) {
            let known = base_rows.iter().find(|b| row_key(array, b) == *key);
            if known == Some(row) {
                continue;
            }
            let position = out.iter().position(|r| row_key(array, r) == *key);
            if array == "members" {
                match (position, known) {
                    (Some(at), Some(_)) if member_may_replace(&out[at], row) => {}
                    (None, _) => {}
                    _ => return Err(()),
                }
            }
            match position {
                Some(at) => out[at] = row.clone(),
                None => out.push(row.clone()),
            }
        }
        merged[array] = Value::Array(out);
    }
    let generation = |v: &Value| v["authority_generation"].as_u64().unwrap_or(1);
    merged["authority_generation"] = Value::from(generation(durable).max(generation(current)));
    Ok(merged)
}

// ------------------------------------------------------------------------------------------ opening

fn busy_span(id: BudgetId, over: Option<Duration>) -> Result<Duration, SeedError> {
    if let Some(duration) = over {
        return Ok(duration);
    }
    id.span(Entries::new(0))
        .map(|s| Duration::from_millis(s.millis()))
        .map_err(|_| SeedError::new("seed_state_unavailable"))
}

/// `SqliteInviteRegistry._initialize` then `SqliteSeedState._initialize`, each on its own connection with its own
/// busy timeout (a locked database is `invite_registry_unavailable` first, as in v0.1.4). Returns the state
/// connection (kept open by the store).
fn initialize(database: &Path, busy: Option<Duration>) -> Result<Connection, SeedError> {
    let registry = || SeedError::new("invite_registry_unavailable");
    let invites = busy_span(
        BudgetId::PyMyceliumInviteSqliteRegistrySqliteinviteregistryConnectTimeoutL116,
        busy,
    )?;
    {
        let connection = Connection::open(database).map_err(|_| registry())?;
        connection.busy_timeout(invites).map_err(|_| registry())?;
        let created = connection
            .execute_batch(
                "PRAGMA trusted_schema = OFF;
                 BEGIN IMMEDIATE;
                 CREATE TABLE IF NOT EXISTS consumed_invites (
                     nonce TEXT PRIMARY KEY NOT NULL,
                     consumed_at REAL NOT NULL,
                     expires_at REAL NOT NULL
                 ) WITHOUT ROWID;
                 COMMIT;",
            )
            .map_err(|_| registry());
        if let Err(error) = created {
            // `connection.rollback()` is a no-op without an open transaction and raises otherwise.
            if !connection.is_autocommit() {
                connection
                    .execute_batch("ROLLBACK")
                    .map_err(|_| SeedError::new("invite_registry_unavailable"))?;
            }
            return Err(error);
        }
    }
    let state = busy_span(
        BudgetId::PyMyceliumSeedStateSqliteseedstateConnectTimeoutL65,
        busy,
    )?;
    let connection = Connection::open(database).map_err(|e| sqlite(&e))?;
    connection.busy_timeout(state).map_err(|e| sqlite(&e))?;
    v014::initialize(&connection)?;
    Ok(connection)
}

/// What `restore_seed_state` reads from a backup database through `SqliteSeedState(database)`: the identity
/// binding, `len(load_members())` and the authority generation.
pub struct Described {
    pub binding: StoredBinding,
    pub member_count: usize,
    pub authority_generation: u64,
}

/// `SqliteSeedState(database)` on an existing database (the path rules, the registry table, the v0.1.4 schema
/// initialised and migrated IN PLACE as v0.1.4 does) and its three reads. A database the Rust store already holds
/// is read from its snapshot; a v0.1.4 database from its row tables. Nothing else is written.
pub fn describe(database: &Path) -> Result<Described, SeedError> {
    prepare_path(database)?;
    let connection = initialize(database, None)?;
    let get = |key: &str| -> Result<String, SeedError> {
        connection
            .query_row(
                "SELECT value FROM seed_metadata WHERE key = ?",
                [key],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|e| sqlite(&e))?
            .ok_or_else(|| SeedError::new("seed_state_identity_missing"))
    };
    let binding = StoredBinding {
        swarm_id: get("swarm_id")?,
        seed_node_id: get("seed_node_id")?,
        seed_key_digest: get("seed_key_digest")?,
    };
    let tables: Option<i64> = connection
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rs_seed_table'",
            [],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| sqlite(&e))?;
    let snapshot: Option<String> = if tables.is_some() {
        connection
            .query_row(
                "SELECT snapshot FROM rs_seed_table WHERE id = 1",
                [],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| sqlite(&e))?
    } else {
        None
    };
    let (member_count, authority_generation) = match snapshot {
        Some(text) => {
            let value: Value =
                serde_json::from_str(&text).map_err(|_| SeedError::new("seed_state_corrupt"))?;
            let table = SeedTable::restore(&value, Box::new(String::new))
                .map_err(|_| SeedError::new("seed_state_corrupt"))?;
            let generation = table.authority_state()["authority_generation"]
                .as_u64()
                .ok_or_else(|| SeedError::new("seed_state_corrupt"))?;
            (table.rows().len(), generation)
        }
        None => (
            v014::member_count(&connection)?,
            v014::authority_generation(&connection)?
                .ok_or_else(|| SeedError::new("seed_state_identity_missing"))?,
        ),
    };
    Ok(Described {
        binding,
        member_count,
        authority_generation,
    })
}
