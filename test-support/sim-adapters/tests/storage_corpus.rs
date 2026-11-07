// SPDX-License-Identifier: AGPL-3.0-only
//! Foundation corpus supporting O:path:mycelium_physical_runner/state_store.py,
//! O:path:mycelium_physical_runner/private_fs.py; not complete port evidence.
use sim_adapters::{Code, Fake, Fault, Store};

/// Independent contract exercised unchanged against every adapter. No adapter
/// implementation is the oracle; the expected byte strings/errors are literal.
fn corpus(mut store: impl Store, cap: u64) {
    assert_eq!(store.get(7).unwrap(), None);
    store.commit(&[(1, b"old".to_vec())]).unwrap();
    assert_eq!(store.get(1).unwrap(), Some(b"old".to_vec()));
    store.commit(&[(1, b"old".to_vec())]).unwrap(); // idempotent duplicate
    store.reopen().unwrap();
    assert_eq!(store.get(1).unwrap(), Some(b"old".to_vec()));
    store.fault(Fault::Commit).unwrap();
    let error = store
        .commit(&[(1, b"new".to_vec()), (2, b"second".to_vec())])
        .unwrap_err();
    assert_eq!(error.code, Code::Commit);
    assert!(!error.cause.is_empty());
    assert_eq!(store.get(1).unwrap(), Some(b"old".to_vec()));
    assert_eq!(store.get(2).unwrap(), None); // first statement must roll back too
    assert!(!store.pending());
    store.reopen().unwrap();
    assert_eq!(store.get(1).unwrap(), Some(b"old".to_vec()));
    store.fault(Fault::None).unwrap();
    store
        .commit(&[(1, b"new".to_vec()), (2, b"second".to_vec())])
        .unwrap();
    store.reopen().unwrap();
    assert_eq!(store.get(1).unwrap(), Some(b"new".to_vec()));
    assert_eq!(store.get(2).unwrap(), Some(b"second".to_vec()));
    assert_eq!(
        store
            .commit(&[(3, vec![0; usize::try_from(cap).unwrap()])])
            .unwrap_err()
            .code,
        Code::Capacity
    );
    assert_eq!(store.get(3).unwrap(), None);
    store.remove(2).unwrap();
    store.remove(2).unwrap();
    store.reopen().unwrap();
    assert_eq!(store.get(2).unwrap(), None);
    store.commit(&[(u64::MAX, Vec::new())]).unwrap();
    assert_eq!(store.get(u64::MAX).unwrap(), Some(Vec::new()));
    store.fault(Fault::Cleanup).unwrap();
    assert_eq!(store.cleanup().unwrap_err().code, Code::Cleanup);
    assert!(store.pending());
    assert_eq!(store.get(1).unwrap_err().code, Code::Closed);
    assert_eq!(
        store.commit(&[(3, b"late".to_vec())]).unwrap_err().code,
        Code::Closed
    );
    assert_eq!(store.reopen().unwrap_err().code, Code::Closed);
    store.fault(Fault::None).unwrap();
    store.cleanup().unwrap();
    assert!(!store.pending());
    store.cleanup().unwrap();
}
fn cap() -> u64 {
    budgets::BudgetId::ReplayPage.bytes().unwrap().get()
}
#[test]
fn fake_storage_corpus() {
    corpus(Fake::new(cap()), cap());
}

struct Root(std::path::PathBuf);
impl Root {
    fn new(label: &str) -> Self {
        static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
        let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "v02-l2c-storage-{label}-{}-{n}",
            std::process::id()
        ));
        assert!(!root.exists());
        Self(root)
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        if self.0.exists() {
            std::fs::remove_dir_all(&self.0).expect("owned test root cleanup");
        }
    }
}
#[test]
fn filesystem_storage_corpus() {
    let root = Root::new("filesystem");
    corpus(
        sim_adapters::Filesystem::new(root.0.clone(), cap()).unwrap(),
        cap(),
    );
    assert!(!root.0.exists(), "adapter must remove its owned resources");
}
#[test]
fn sqlite_storage_corpus() {
    // libsqlite3-sys 0.30.1 sqlite3/sqlite3.h:149; bundled, not host SQLite.
    assert_eq!(rusqlite::version(), "3.46.0");
    let root = Root::new("sqlite");
    corpus(
        sim_adapters::Sqlite::new(root.0.clone(), cap()).unwrap(),
        cap(),
    );
    assert!(
        !root.0.exists(),
        "database, journal and root must be removed"
    );
}
#[test]
fn real_adapters_refuse_existing_roots_without_erasing_foreign_data() {
    let root = Root::new("existing");
    std::fs::create_dir(&root.0).unwrap();
    std::fs::write(root.0.join("foreign"), b"preserve").unwrap();
    assert!(sim_adapters::Filesystem::new(root.0.clone(), cap()).is_err());
    assert!(sim_adapters::Sqlite::new(root.0.clone(), cap()).is_err());
    assert_eq!(std::fs::read(root.0.join("foreign")).unwrap(), b"preserve");
}

fn capacity_boundary(mut store: impl Store) {
    let bytes = vec![7; usize::try_from(cap()).unwrap()];
    store.commit(&[(1, bytes.clone())]).unwrap();
    assert_eq!(store.get(1).unwrap(), Some(bytes.clone()));
    assert_eq!(
        store.commit(&[(2, vec![8])]).unwrap_err().code,
        Code::Capacity
    );
    store.reopen().unwrap();
    assert_eq!(store.get(1).unwrap(), Some(bytes));
    assert_eq!(store.get(2).unwrap(), None);
    store.remove(1).unwrap();
    store.commit(&[(2, vec![8]), (2, vec![9])]).unwrap();
    assert_eq!(store.get(2).unwrap(), Some(vec![9]));
    store.cleanup().unwrap();
}
#[test]
fn all_storage_adapters_enforce_exact_logical_capacity_and_last_write() {
    capacity_boundary(Fake::new(cap()));
    let fs = Root::new("capacity-fs");
    capacity_boundary(sim_adapters::Filesystem::new(fs.0.clone(), cap()).unwrap());
    assert!(!fs.0.exists());
    let db = Root::new("capacity-db");
    capacity_boundary(sim_adapters::Sqlite::new(db.0.clone(), cap()).unwrap());
    assert!(!db.0.exists());
}
struct Liar {
    inner: Fake,
    hide_cleanup: bool,
}
impl Store for Liar {
    fn commit(&mut self, b: &[(u64, Vec<u8>)]) -> sim_adapters::Result<()> {
        self.inner.commit(b)
    }
    fn get(&self, k: u64) -> sim_adapters::Result<Option<Vec<u8>>> {
        self.inner.get(k)
    }
    fn remove(&mut self, k: u64) -> sim_adapters::Result<()> {
        self.inner.remove(k)
    }
    fn reopen(&mut self) -> sim_adapters::Result<()> {
        self.inner.reopen()
    }
    fn fault(&mut self, f: Fault) -> sim_adapters::Result<()> {
        self.inner
            .fault(if !self.hide_cleanup && f == Fault::Commit {
                Fault::None
            } else {
                f
            })
    }
    fn cleanup(&mut self) -> sim_adapters::Result<()> {
        if self.hide_cleanup {
            Ok(())
        } else {
            self.inner.cleanup()
        }
    }
    fn pending(&self) -> bool {
        self.inner.pending()
    }
}
#[test]
fn unchanged_corpus_rejects_fake_that_hides_commit_failure() {
    assert!(
        std::panic::catch_unwind(|| corpus(
            Liar {
                inner: Fake::new(cap()),
                hide_cleanup: false
            },
            cap()
        ))
        .is_err()
    );
}
#[test]
fn unchanged_corpus_rejects_fake_that_hides_cleanup_failure() {
    assert!(
        std::panic::catch_unwind(|| corpus(
            Liar {
                inner: Fake::new(cap()),
                hide_cleanup: true
            },
            cap()
        ))
        .is_err()
    );
}

#[test]
fn storage_foundation_obligations_are_registered() {
    let (registry, _) = obligations::checked_in().unwrap();
    for id in [
        "O:path:mycelium_physical_runner/state_store.py",
        "O:path:mycelium_physical_runner/private_fs.py",
    ] {
        assert!(registry.obligations.iter().any(|o| o.id == id));
    }
}
