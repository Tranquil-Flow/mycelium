// SPDX-License-Identifier: AGPL-3.0-only
use crate::{Code, Error, Fault, Result, Store};
use std::{
    collections::BTreeMap,
    fs,
    io::{ErrorKind, Write},
    path::{Path, PathBuf},
};

pub(crate) fn remove_file(path: &Path) -> std::io::Result<()> {
    match fs::remove_file(path) {
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(()),
        other => other,
    }
}
fn remove_dir(path: &Path) -> std::io::Result<()> {
    match fs::remove_dir(path) {
        Err(e) if e.kind() == ErrorKind::NotFound => Ok(()),
        result => result,
    }
}
pub(crate) fn fault_cleanup(root: &Path, fault: Fault) -> Result<()> {
    let blocker = root.join("foreign-cleanup-blocker");
    if fault == Fault::Cleanup {
        fs::write(blocker, b"not owned by normal cleanup").map_err(|e| Error::new(Code::Cleanup, e))
    } else {
        remove_file(&blocker).map_err(|e| Error::new(Code::Cleanup, e))
    }
}
/// Attempt every named owned resource even if an earlier deletion failed.
/// Never recursively remove a root: unexpected foreign files block cleanup.
pub(crate) fn cleanup(root: &Path, owned: &[&str]) -> Result<()> {
    let mut failures = Vec::new();
    for name in owned {
        if let Err(e) = remove_file(&root.join(name)) {
            failures.push(format!("remove:{name}:{e}"));
        }
    }
    if let Err(e) = remove_dir(root) {
        failures.push(format!("remove_root:{e}"));
    }
    if failures.is_empty() {
        Ok(())
    } else {
        Err(Error::new(Code::Cleanup, failures.join(";")))
    }
}
pub struct Filesystem {
    root: PathBuf,
    cap: u64,
    closed: bool,
    fault: Fault,
    cleanup_pending: bool,
}
impl Filesystem {
    pub fn new(root: PathBuf, cap: u64) -> Result<Self> {
        fs::create_dir(&root).map_err(|e| Error::new(Code::Open, e))?;
        Ok(Self {
            root,
            cap,
            closed: false,
            fault: Fault::None,
            cleanup_pending: false,
        })
    }
    fn open(&self) -> Result<()> {
        if self.closed {
            Err(Error::new(Code::Closed, "closed"))
        } else {
            Ok(())
        }
    }
    fn image(&self) -> Result<BTreeMap<u64, Vec<u8>>> {
        self.open()?;
        match fs::read(self.root.join("image.json")) {
            Ok(bytes) => serde_json::from_slice(&bytes).map_err(|e| Error::new(Code::Read, e)),
            Err(e) if e.kind() == ErrorKind::NotFound => Ok(BTreeMap::new()),
            Err(e) => Err(Error::new(Code::Read, e)),
        }
    }
    fn persist(&self, image: &BTreeMap<u64, Vec<u8>>, inject: bool) -> Result<()> {
        let total = image
            .values()
            .try_fold(0u64, |n, v| n.checked_add(v.len() as u64))
            .ok_or_else(|| Error::new(Code::Capacity, "logical_byte_overflow"))?;
        if total > self.cap {
            return Err(Error::new(Code::Capacity, "logical_byte_cap"));
        }
        let bytes = serde_json::to_vec(image).map_err(|e| Error::new(Code::Commit, e))?;
        let pending = self.root.join("pending.json");
        let result = (|| -> std::io::Result<()> {
            let mut file = fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&pending)?;
            file.write_all(&bytes)?;
            file.sync_all()?;
            drop(file);
            let target = if inject {
                "blocked-target"
            } else {
                "image.json"
            };
            fs::rename(&pending, self.root.join(target))?;
            Ok(())
        })();
        if let Err(error) = result {
            return match remove_file(&pending) {
                Ok(()) => Err(Error::new(Code::Commit, error)),
                Err(cleanup) => Err(Error::new(
                    Code::Commit,
                    format!("{error}; pending_cleanup:{cleanup}"),
                )),
            };
        }
        // Atomic visibility / graceful reopen corpus, not power-loss durability:
        // no claim of parent-directory fsync or crash-safe rename on every OS.
        Ok(())
    }
}
impl Store for Filesystem {
    fn commit(&mut self, batch: &[(u64, Vec<u8>)]) -> Result<()> {
        let mut image = self.image()?;
        for (key, value) in batch {
            image.insert(*key, value.clone());
        }
        self.persist(
            &image,
            self.fault == Fault::Commit && batch.iter().any(|(k, _)| *k == 2),
        )
    }
    fn get(&self, key: u64) -> Result<Option<Vec<u8>>> {
        Ok(self.image()?.get(&key).cloned())
    }
    fn remove(&mut self, key: u64) -> Result<()> {
        let mut image = self.image()?;
        image.remove(&key);
        self.persist(&image, false)
    }
    fn reopen(&mut self) -> Result<()> {
        self.image().map(|_| ())
    }
    fn fault(&mut self, fault: Fault) -> Result<()> {
        fault_cleanup(&self.root, fault)?;
        let blocked = self.root.join("blocked-target");
        if fault == Fault::Commit {
            fs::create_dir(&blocked).map_err(|e| Error::new(Code::Commit, e))?;
        } else {
            remove_dir(&blocked).map_err(|e| Error::new(Code::Commit, e))?;
        }
        self.fault = fault;
        Ok(())
    }
    fn cleanup(&mut self) -> Result<()> {
        self.closed = true;
        let result = cleanup(&self.root, &["image.json", "pending.json"]);
        self.cleanup_pending = result.is_err();
        result
    }
    fn pending(&self) -> bool {
        self.cleanup_pending || self.root.join("pending.json").exists()
    }
}
