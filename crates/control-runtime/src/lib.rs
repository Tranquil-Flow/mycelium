// SPDX-License-Identifier: AGPL-3.0-only
//! Durable implementation layered over the L5 pure authentication seam.
use control::ManagedContext;
use rusqlite::{Connection, Transaction};
mod activation;
pub mod consent;
pub mod import;
pub mod mailbox;
mod replay;
mod schema;
pub use schema::{backup_verified, rollback_verified};
use serde_json::Value;
use std::path::Path;

#[derive(Debug)]
pub enum Error {
    Refused(&'static str),
    Sql(rusqlite::Error),
    Io(std::io::Error),
    Budget(budgets::Refusal),
}
impl From<rusqlite::Error> for Error {
    fn from(e: rusqlite::Error) -> Self {
        Self::Sql(e)
    }
}
impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Self::Io(e)
    }
}
impl From<budgets::Refusal> for Error {
    fn from(e: budgets::Refusal) -> Self {
        Self::Budget(e)
    }
}
pub type Result<T> = std::result::Result<T, Error>;

struct AuthenticatedStatement {
    statement: Value,
    signer: String,
}
impl AuthenticatedStatement {
    fn statement(&self) -> &Value {
        &self.statement
    }
    fn signer_key_digest(&self) -> &str {
        &self.signer
    }
}
pub struct VerifiedRequest {
    request: AuthenticatedStatement,
    digest: String,
}
impl VerifiedRequest {
    pub fn authenticate(env: &Value, context: &ManagedContext<'_>, now: f64) -> Result<Self> {
        let request =
            control::authenticate_request(env, context, now, control::managed_time_limits()?)
                .map_err(|e| Error::Refused(e.0))?;
        let digest =
            protocol::canonical::digest(env).map_err(|_| Error::Refused("canonical_invalid"))?;
        Ok(Self {
            request: AuthenticatedStatement {
                statement: request.statement().clone(),
                signer: request.signer_key_digest().into(),
            },
            digest,
        })
    }
}

pub struct Store {
    connection: Connection,
}
impl Store {
    pub fn open(path: &Path) -> Result<Self> {
        Self::open_with_backup(path, None)
    }
    /// Nonblocking development opener; production supplies the L1 SQLite wait policy.
    pub fn open_with_backup(path: &Path, backup: Option<&Path>) -> Result<Self> {
        Self::open_with_policy(path, backup, budgets::Span::ZERO)
    }
    /// TODO(L1): bind the managed-control SQLite wait ID after registry integration.
    /// The parent supplies its registry-derived policy; no driver timeout is authority.
    pub fn open_with_policy(
        path: &Path,
        backup: Option<&Path>,
        sqlite_busy: budgets::Span,
    ) -> Result<Self> {
        use std::os::unix::fs::OpenOptionsExt;
        // Parent is an exclusively owned private state directory; reject a leaf
        // symlink and create sensitive SQLite bytes with private mode from birth.
        match std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
        {
            Ok(file) => file.sync_all()?,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                if !std::fs::symlink_metadata(path)?.file_type().is_file() {
                    return Err(Error::Refused("control_database_not_regular"));
                }
            }
            Err(error) => return Err(error.into()),
        }
        let mut connection = Connection::open(path)?;
        connection.busy_timeout(std::time::Duration::from_millis(sqlite_busy.millis()))?;
        connection.execute_batch("PRAGMA foreign_keys=ON; PRAGMA synchronous=FULL;")?;
        schema::initialize(&mut connection, backup)?;
        Ok(Self { connection })
    }
    /// Administrative/testing connection; not an untrusted command dispatch API.
    pub fn connection(&self) -> &Connection {
        &self.connection
    }
    /// The effect, replay result, high-water and retention share one write transaction.
    /// Only database effects belong here; never perform network/process work inside it.
    /// Production seam: sample the trusted clock after SQLite owns the write lock.
    pub fn execute_with_clock(
        &mut self,
        request: &VerifiedRequest,
        clock: impl FnOnce() -> f64,
        effect: impl FnOnce(&Transaction<'_>) -> Result<Vec<u8>>,
    ) -> Result<Vec<u8>> {
        let tx = self
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let response = replay::execute(&tx, request, clock(), effect)?;
        tx.commit()?;
        Ok(response)
    }
    /// Deterministic-clock simulation seam; native composition uses execute_with_clock.
    pub fn execute(
        &mut self,
        request: &VerifiedRequest,
        now: f64,
        effect: impl FnOnce(&Transaction<'_>) -> Result<Vec<u8>>,
    ) -> Result<Vec<u8>> {
        self.execute_with_clock(request, || now, effect)
    }
    /// Versioned singleton state: compare-and-swap fences stale owner instances.
    pub fn load_document(&self, name: &str) -> Result<Option<(u64, Vec<u8>)>> {
        use rusqlite::OptionalExtension;
        Ok(self
            .connection
            .query_row(
                "SELECT revision,document FROM control_state WHERE name=?1",
                [name],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?)
    }
    pub fn save_document(
        &mut self,
        name: &str,
        expected: Option<u64>,
        bytes: &[u8],
    ) -> Result<u64> {
        let next = expected
            .unwrap_or(0)
            .checked_add(1)
            .ok_or(Error::Refused("revision_overflow"))?;
        let tx = self
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let changed = match expected {
            None => tx.execute("INSERT INTO control_state(name,revision,document) VALUES (?1,?2,?3) ON CONFLICT(name) DO NOTHING",rusqlite::params![name,next,bytes])?,
            Some(revision) => tx.execute("UPDATE control_state SET revision=?1,document=?2 WHERE name=?3 AND revision=?4",rusqlite::params![next,bytes,name,revision])?,
        };
        if changed != 1 {
            return Err(Error::Refused("control_state_stale_writer"));
        }
        tx.commit()?;
        Ok(next)
    }
    /// Maintenance only: terminal is not sufficient; run retirement is independent.
    pub fn prune_retired_commands(&mut self, now: f64) -> Result<usize> {
        replay::prune_commands(&self.connection, now)
    }
    pub fn prune(&mut self, signer: &str, now: f64) -> Result<usize> {
        let tx = self
            .connection
            .transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let removed = replay::prune(&tx, signer, now)?;
        tx.commit()?;
        Ok(removed)
    }
}
