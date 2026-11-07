// SPDX-License-Identifier: AGPL-3.0-only
//! Foundation adapter contracts, not production storage or durability qualification.
use std::collections::BTreeMap;
mod filesystem;
mod sqlite;
pub use filesystem::Filesystem;
pub use sqlite::Sqlite;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Code {
    Closed,
    Capacity,
    Commit,
    Read,
    Open,
    Cleanup,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    pub code: Code,
    pub cause: String,
}
impl Error {
    pub fn new(code: Code, cause: impl std::fmt::Display) -> Self {
        Self {
            code,
            cause: cause.to_string(),
        }
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?}:{}", self.code, self.cause)
    }
}
impl std::error::Error for Error {}
pub type Result<T> = std::result::Result<T, Error>;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Fault {
    None,
    Commit,
    Cleanup,
}
/// Atomic batch visibility; failure leaves the previous committed image intact.
/// Cleanup closes first, attempts owned cleanup, and is retryable and idempotent.
/// A closed adapter cannot be reopened (reopen is a live-handle restart probe).
pub trait Store {
    fn commit(&mut self, batch: &[(u64, Vec<u8>)]) -> Result<()>;
    fn get(&self, key: u64) -> Result<Option<Vec<u8>>>;
    fn remove(&mut self, key: u64) -> Result<()>;
    fn reopen(&mut self) -> Result<()>;
    fn fault(&mut self, fault: Fault) -> Result<()>;
    fn cleanup(&mut self) -> Result<()>;
    fn pending(&self) -> bool;
}
#[derive(Default)]
pub struct Fake {
    data: BTreeMap<u64, Vec<u8>>,
    cap: u64,
    closed: bool,
    fault: Option<Fault>,
    cleanup_pending: bool,
}
impl Fake {
    pub fn new(cap: u64) -> Self {
        Self {
            cap,
            ..Self::default()
        }
    }
    fn open(&self) -> Result<()> {
        if self.closed {
            Err(Error::new(Code::Closed, "closed"))
        } else {
            Ok(())
        }
    }
}
impl Store for Fake {
    fn commit(&mut self, batch: &[(u64, Vec<u8>)]) -> Result<()> {
        self.open()?;
        let mut staged = self.data.clone();
        for (key, value) in batch {
            if self.fault == Some(Fault::Commit) && *key == 2 {
                return Err(Error::new(Code::Commit, "injected_disk_failure"));
            }
            staged.insert(*key, value.clone());
        }
        let size = staged
            .values()
            .try_fold(0u64, |n, v| n.checked_add(v.len() as u64))
            .ok_or_else(|| Error::new(Code::Capacity, "logical_byte_overflow"))?;
        if size > self.cap {
            return Err(Error::new(Code::Capacity, "logical_byte_cap"));
        }
        self.data = staged;
        Ok(())
    }
    fn get(&self, key: u64) -> Result<Option<Vec<u8>>> {
        self.open()?;
        Ok(self.data.get(&key).cloned())
    }
    fn remove(&mut self, key: u64) -> Result<()> {
        self.open()?;
        self.data.remove(&key);
        Ok(())
    }
    fn reopen(&mut self) -> Result<()> {
        self.open()
    }
    fn fault(&mut self, fault: Fault) -> Result<()> {
        self.fault = Some(fault);
        Ok(())
    }
    fn cleanup(&mut self) -> Result<()> {
        self.closed = true;
        self.data.clear();
        self.cleanup_pending = self.fault == Some(Fault::Cleanup);
        if self.cleanup_pending {
            Err(Error::new(Code::Cleanup, "directory_not_empty"))
        } else {
            Ok(())
        }
    }
    fn pending(&self) -> bool {
        self.cleanup_pending
    }
}
