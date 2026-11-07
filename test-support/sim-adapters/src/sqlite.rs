// SPDX-License-Identifier: AGPL-3.0-only
use crate::{Code, Error, Fault, Result, Store, filesystem};
use rusqlite::{Connection, OptionalExtension, params};
use std::path::PathBuf;

pub struct Sqlite {
    root: PathBuf,
    connection: Option<Connection>,
    cap: u64,
    closed: bool,
    cleanup_pending: bool,
}
impl Sqlite {
    pub fn new(root: PathBuf, cap: u64) -> Result<Self> {
        std::fs::create_dir(&root).map_err(|e| Error::new(Code::Open, e))?;
        let mut store = Self {
            root,
            connection: None,
            cap,
            closed: false,
            cleanup_pending: false,
        };
        store.connect()?;
        Ok(store)
    }
    fn connect(&mut self) -> Result<()> {
        let connection = Connection::open(self.root.join("store.sqlite"))
            .map_err(|e| Error::new(Code::Open, e))?;
        connection.execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS kv (k BLOB PRIMARY KEY NOT NULL, value BLOB NOT NULL);")
            .map_err(|e| Error::new(Code::Open, e))?;
        self.connection = Some(connection);
        Ok(())
    }
    fn open(&self) -> Result<&Connection> {
        if self.closed {
            return Err(Error::new(Code::Closed, "closed"));
        }
        self.connection
            .as_ref()
            .ok_or_else(|| Error::new(Code::Open, "missing_connection"))
    }
    fn disconnect(&mut self) -> Result<()> {
        if let Some(connection) = self.connection.take()
            && let Err((connection, error)) = connection.close()
        {
            self.connection = Some(connection);
            return Err(Error::new(Code::Cleanup, error));
        }
        Ok(())
    }
}
impl Store for Sqlite {
    fn commit(&mut self, batch: &[(u64, Vec<u8>)]) -> Result<()> {
        self.open()?;
        let tx = self
            .connection
            .as_mut()
            .ok_or_else(|| Error::new(Code::Open, "missing_connection"))?
            .transaction()
            .map_err(|e| Error::new(Code::Commit, e))?;
        let result = (|| -> Result<()> {
            for (key, value) in batch {
                tx.execute("INSERT INTO kv(k,value) VALUES (?1,?2) ON CONFLICT(k) DO UPDATE SET value=excluded.value", params![key.to_be_bytes().as_slice(), value])
                    .map_err(|e| Error::new(Code::Commit, e))?;
            }
            let total: u64 = tx
                .query_row("SELECT COALESCE(SUM(length(value)),0) FROM kv", [], |row| {
                    row.get(0)
                })
                .map_err(|e| Error::new(Code::Commit, e))?;
            if total > self.cap {
                return Err(Error::new(Code::Capacity, "logical_byte_cap"));
            }
            Ok(())
        })();
        match result {
            Ok(()) => tx.commit().map_err(|e| Error::new(Code::Commit, e)),
            Err(error) => match tx.rollback() {
                Ok(()) => Err(error),
                Err(rollback) => Err(Error::new(
                    Code::Commit,
                    format!("{error};rollback:{rollback}"),
                )),
            },
        }
    }
    fn get(&self, key: u64) -> Result<Option<Vec<u8>>> {
        self.open()?
            .query_row(
                "SELECT value FROM kv WHERE k=?1",
                [key.to_be_bytes().as_slice()],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| Error::new(Code::Read, e))
    }
    fn remove(&mut self, key: u64) -> Result<()> {
        self.open()?
            .execute("DELETE FROM kv WHERE k=?1", [key.to_be_bytes().as_slice()])
            .map_err(|e| Error::new(Code::Commit, e))?;
        Ok(())
    }
    fn reopen(&mut self) -> Result<()> {
        self.open()?;
        self.disconnect()?;
        self.connect()
    }
    fn fault(&mut self, fault: Fault) -> Result<()> {
        filesystem::fault_cleanup(&self.root, fault)?;
        if !self.closed {
            let connection = self.open()?;
            connection
                .execute_batch("DROP TRIGGER IF EXISTS injected_disk_failure;")
                .map_err(|e| Error::new(Code::Commit, e))?;
            if fault == Fault::Commit {
                // A real SQLite statement abort after key 1 has been written in
                // the transaction. The batch MUST explicitly roll it back.
                connection.execute_batch("CREATE TRIGGER injected_disk_failure BEFORE INSERT ON kv WHEN NEW.k=X'0000000000000002' BEGIN SELECT RAISE(ABORT,'injected_disk_failure'); END;")
                    .map_err(|e| Error::new(Code::Commit, e))?;
            }
        }
        Ok(())
    }
    fn cleanup(&mut self) -> Result<()> {
        self.closed = true;
        let close = self.disconnect();
        let files = filesystem::cleanup(
            &self.root,
            &[
                "store.sqlite",
                "store.sqlite-journal",
                "store.sqlite-wal",
                "store.sqlite-shm",
            ],
        );
        let result = match (close, files) {
            (Ok(()), Ok(())) => Ok(()),
            (Err(e), Ok(())) | (Ok(()), Err(e)) => Err(e),
            (Err(a), Err(b)) => Err(Error::new(Code::Cleanup, format!("{a};{b}"))),
        };
        self.cleanup_pending = result.is_err();
        result
    }
    fn pending(&self) -> bool {
        self.cleanup_pending
    }
}
