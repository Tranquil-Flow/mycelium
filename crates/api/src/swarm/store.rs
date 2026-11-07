// SPDX-License-Identifier: AGPL-3.0-only
//! Four-method KV storage behind the directory, store.py @ f4e366e9.
//! Values are stored as `json.dumps(value, sort_keys=True)` text, so a v0.1.4
//! SQLite file opens unchanged (same `kv(tbl, k, v)` schema and ordering).
use super::json::{J, dumps_stored, parse_stored};
use rusqlite::{Connection, params};
use std::{collections::BTreeMap, path::Path, sync::Mutex};

pub const TABLES: [&str; 6] = ["swarm", "tomb", "request", "grant", "offer", "nonce"];
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StoreError(pub &'static str);
pub type StoreResult<T> = Result<T, StoreError>;

pub trait Store: Send + Sync {
    fn get(&self, table: &str, key: &str) -> StoreResult<Option<J>>;
    fn put(&self, table: &str, key: &str, value: &J) -> StoreResult<()>;
    fn delete(&self, table: &str, key: &str) -> StoreResult<()>;
    /// Sorted by key; prefix match on characters.
    fn scan(&self, table: &str, prefix: &str) -> StoreResult<Vec<(String, J)>>;
}
fn table(name: &str) -> StoreResult<()> {
    if TABLES.contains(&name) {
        Ok(())
    } else {
        Err(StoreError("store_table_invalid"))
    }
}
fn decode(text: &str) -> StoreResult<J> {
    parse_stored(text).map_err(|_| StoreError("store_value_corrupt"))
}
#[derive(Default)]
pub struct MemoryStore {
    data: Mutex<BTreeMap<(String, String), String>>,
}
impl MemoryStore {
    pub fn new() -> Self {
        Self::default()
    }
}
impl Store for MemoryStore {
    fn get(&self, t: &str, key: &str) -> StoreResult<Option<J>> {
        table(t)?;
        let data = self
            .data
            .lock()
            .map_err(|_| StoreError("store_lock_poisoned"))?;
        data.get(&(t.to_string(), key.to_string()))
            .map(|v| decode(v))
            .transpose()
    }
    fn put(&self, t: &str, key: &str, value: &J) -> StoreResult<()> {
        table(t)?;
        let mut data = self
            .data
            .lock()
            .map_err(|_| StoreError("store_lock_poisoned"))?;
        data.insert((t.to_string(), key.to_string()), dumps_stored(value));
        Ok(())
    }
    fn delete(&self, t: &str, key: &str) -> StoreResult<()> {
        table(t)?;
        let mut data = self
            .data
            .lock()
            .map_err(|_| StoreError("store_lock_poisoned"))?;
        data.remove(&(t.to_string(), key.to_string()));
        Ok(())
    }
    fn scan(&self, t: &str, prefix: &str) -> StoreResult<Vec<(String, J)>> {
        table(t)?;
        let data = self
            .data
            .lock()
            .map_err(|_| StoreError("store_lock_poisoned"))?;
        data.iter()
            .filter(|((tb, k), _)| tb == t && k.starts_with(prefix))
            .map(|((_, k), v)| Ok((k.clone(), decode(v)?)))
            .collect()
    }
}
pub struct SqliteStore {
    db: Mutex<Connection>,
}
fn sql(_: rusqlite::Error) -> StoreError {
    StoreError("store_sqlite_failed")
}
impl SqliteStore {
    /// Creates a missing file atomically with mode 0600 (`O_EXCL`, so a symlink
    /// at the path is refused) and tightens existing WAL side files.
    pub fn open(path: &Path) -> StoreResult<Self> {
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
        if !path.exists() {
            std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(path)
                .map_err(|_| StoreError("store_create_failed"))?;
        }
        let db = Connection::open(path).map_err(sql)?;
        db.query_row("PRAGMA journal_mode=WAL", [], |r| r.get::<_, String>(0))
            .map_err(sql)?;
        db.execute_batch(
            "PRAGMA synchronous=NORMAL; CREATE TABLE IF NOT EXISTS kv (tbl TEXT NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL, PRIMARY KEY (tbl, k)) WITHOUT ROWID",
        )
        .map_err(sql)?;
        for suffix in ["-wal", "-shm"] {
            let mut side = path.as_os_str().to_owned();
            side.push(suffix);
            let side = std::path::PathBuf::from(side);
            if side.exists() {
                std::fs::set_permissions(&side, std::fs::Permissions::from_mode(0o600))
                    .map_err(|_| StoreError("store_chmod_failed"))?;
            }
        }
        Ok(Self { db: Mutex::new(db) })
    }
    fn conn(&self) -> StoreResult<std::sync::MutexGuard<'_, Connection>> {
        self.db
            .lock()
            .map_err(|_| StoreError("store_lock_poisoned"))
    }
}
impl Store for SqliteStore {
    fn get(&self, t: &str, key: &str) -> StoreResult<Option<J>> {
        table(t)?;
        let db = self.conn()?;
        let mut stmt = db
            .prepare_cached("SELECT v FROM kv WHERE tbl=?1 AND k=?2")
            .map_err(sql)?;
        let mut rows = stmt.query(params![t, key]).map_err(sql)?;
        match rows.next().map_err(sql)? {
            Some(row) => decode(&row.get::<_, String>(0).map_err(sql)?).map(Some),
            None => Ok(None),
        }
    }
    fn put(&self, t: &str, key: &str, value: &J) -> StoreResult<()> {
        table(t)?;
        self.conn()?
            .execute(
                "INSERT OR REPLACE INTO kv (tbl, k, v) VALUES (?1, ?2, ?3)",
                params![t, key, dumps_stored(value)],
            )
            .map_err(sql)?;
        Ok(())
    }
    fn delete(&self, t: &str, key: &str) -> StoreResult<()> {
        table(t)?;
        self.conn()?
            .execute("DELETE FROM kv WHERE tbl=?1 AND k=?2", params![t, key])
            .map_err(sql)?;
        Ok(())
    }
    fn scan(&self, t: &str, prefix: &str) -> StoreResult<Vec<(String, J)>> {
        table(t)?;
        let db = self.conn()?;
        let mut stmt = db
            .prepare_cached("SELECT k, v FROM kv WHERE tbl=?1 AND substr(k, 1, ?2)=?3 ORDER BY k")
            .map_err(sql)?;
        let length = i64::try_from(prefix.chars().count())
            .map_err(|_| StoreError("store_prefix_invalid"))?;
        let rows = stmt
            .query_map(params![t, length, prefix], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(sql)?;
        let mut out = Vec::new();
        for row in rows {
            let (k, v) = row.map_err(sql)?;
            out.push((k, decode(&v)?));
        }
        Ok(out)
    }
}
