// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{Fault, FaultKind, Result, host_state::*, stranger::CacheRole};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::{Path, PathBuf},
};
#[derive(Clone)]
enum Entry {
    Dir,
    File(Vec<u8>),
    Link,
}
#[derive(Default)]
struct Memory {
    tree: BTreeMap<PathBuf, Entry>,
    running: bool,
    mutations: Vec<String>,
    break_clone: bool,
}
fn fault() -> Fault {
    Fault::new("fixture_missing", FaultKind::Observation)
}
impl Memory {
    fn dir(&mut self, p: &str) {
        self.tree.insert(p.into(), Entry::Dir);
    }
    fn file(&mut self, p: &str, b: &[u8]) {
        self.tree.insert(p.into(), Entry::File(b.to_vec()));
    }
    fn cache(&mut self, p: &str, complete: bool) {
        self.dir(p);
        self.file(&format!("{p}/status.json"),&serde_json::to_vec(&json!({"state":"closed","error_code":null,"total_bytes":999604126,"downloaded_bytes":if complete{999604126}else{0}})).unwrap());
        self.file(&format!("{p}/weights"), b"fixture bytes");
    }
    fn copy(&mut self, from: &Path, to: &Path, remove: bool) -> Result<()> {
        let rows = self
            .tree
            .iter()
            .filter(|(p, _)| p.starts_with(from))
            .map(|(p, v)| (p.clone(), v.clone()))
            .collect::<Vec<_>>();
        if rows.is_empty() {
            return Err(fault());
        }
        for (p, v) in rows {
            self.tree.insert(to.join(p.strip_prefix(from).unwrap()), v);
            if remove {
                self.tree.remove(&p);
            }
        }
        Ok(())
    }
}
impl StateIo for Memory {
    fn app_running(&mut self) -> Result<bool> {
        Ok(self.running)
    }
    fn kind(&mut self, p: &Path) -> Result<Kind> {
        Ok(match self.tree.get(p) {
            None => Kind::Missing,
            Some(Entry::Dir) => Kind::Directory,
            Some(Entry::File(_)) => Kind::File,
            Some(Entry::Link) => Kind::Symlink,
        })
    }
    fn children(&mut self, p: &Path) -> Result<Vec<String>> {
        Ok(self
            .tree
            .keys()
            .filter(|x| x.parent() == Some(p))
            .map(|x| x.file_name().unwrap().to_string_lossy().into())
            .collect())
    }
    fn cache_status(&mut self, p: &Path) -> Result<Option<Value>> {
        match self.tree.get(&p.join("status.json")) {
            Some(Entry::File(b)) => Ok(Some(serde_json::from_slice(b).unwrap())),
            _ => Ok(None),
        }
    }
    fn rename(&mut self, a: &Path, b: &Path) -> Result<()> {
        self.mutations.push("rename".into());
        self.copy(a, b, true)
    }
    fn mkdir_private(&mut self, p: &Path) -> Result<()> {
        self.mutations.push("mkdir0600-parent0700".into());
        self.tree.insert(p.into(), Entry::Dir);
        Ok(())
    }
    fn clone_tree(&mut self, a: &Path, b: &Path, _: Span) -> Result<()> {
        self.mutations.push("clone".into());
        self.copy(a, b, false)?;
        if self.break_clone {
            self.tree.remove(&b.join("status.json"));
        }
        Ok(())
    }
}
fn state() -> &'static Path {
    Path::new("/fixture/state")
}
fn source() -> &'static Path {
    Path::new("/fixture/state.pre-v02-attempt")
}
fn displaced() -> &'static Path {
    Path::new("/fixture/state.seed-displaced-attempt")
}
fn bound() -> Span {
    Span::from_millis(600000)
}
fn fixture() -> Memory {
    let mut m = Memory::default();
    m.dir("/fixture/state");
    m.file("/fixture/state/authority/secret", b"PRIVATE");
    m.cache("/fixture/state/model-source-complete", true);
    m.cache("/fixture/state/model-source-incomplete", false);
    m.dir("/fixture/state/target/serving");
    m.cache("/fixture/state/target/serving/model-source-member", true);
    m.file("/fixture/state/target/peer-secret", b"PRIVATE");
    m
}
#[test]
fn fresh_moves_all_old_state_never_deletes_and_refuses_running_app() {
    let mut m = fixture();
    let old = m.tree.len();
    assert_eq!(
        fresh(&mut m, state(), source()).unwrap(),
        Some(source().into())
    );
    assert_eq!(m.tree.len(), old);
    assert!(!m.tree.contains_key(state()));
    assert!(m.tree.contains_key(&source().join("authority/secret")));
    let mut m = fixture();
    m.running = true;
    assert!(fresh(&mut m, state(), source()).is_err());
    assert!(m.mutations.is_empty());
    assert!(m.tree.contains_key(state()));
}
#[test]
fn owner_clone_only_complete_sources_identity_never_copied_and_idempotent() {
    let mut m = fixture();
    fresh(&mut m, state(), source()).unwrap();
    m.dir("/fixture/state");
    let r = seed(
        &mut m,
        state(),
        source(),
        CacheRole::Owner,
        displaced(),
        bound(),
    )
    .unwrap();
    assert_eq!(r.cloned, vec!["model-source-complete"]);
    assert!(
        m.tree
            .contains_key(&source().join("model-source-complete/weights"))
    );
    assert!(!m.tree.contains_key(&state().join("authority/secret")));
    assert!(
        !m.tree
            .contains_key(&state().join("model-source-incomplete"))
    );
    assert!(
        seed(
            &mut m,
            state(),
            source(),
            CacheRole::Owner,
            displaced(),
            bound()
        )
        .unwrap()
        .cloned
        .is_empty()
    );
}
#[test]
fn incomplete_bootstrap_cache_moves_outside_state_and_clone_failure_never_passes() {
    let mut m = fixture();
    fresh(&mut m, state(), source()).unwrap();
    m.dir("/fixture/state");
    m.cache("/fixture/state/model-source-complete", false);
    m.file("/fixture/state/model-source-complete/acquisition.lock", b"");
    let r = seed(
        &mut m,
        state(),
        source(),
        CacheRole::Owner,
        displaced(),
        bound(),
    )
    .unwrap();
    assert_eq!(r.displaced.len(), 1);
    assert!(!r.displaced[0].starts_with(state()));
    assert!(
        m.tree
            .contains_key(&r.displaced[0].join("acquisition.lock"))
    );
    let mut m = fixture();
    fresh(&mut m, state(), source()).unwrap();
    m.dir("/fixture/state");
    m.break_clone = true;
    assert!(
        seed(
            &mut m,
            state(),
            source(),
            CacheRole::Owner,
            displaced(),
            bound()
        )
        .is_err()
    );
}
#[test]
fn target_clone_requires_join_created_root_and_copies_no_owner_or_peer_identity() {
    let mut m = fixture();
    fresh(&mut m, state(), source()).unwrap();
    m.dir("/fixture/state");
    assert!(
        seed(
            &mut m,
            state(),
            source(),
            CacheRole::Target,
            displaced(),
            bound()
        )
        .is_err()
    );
    m.dir("/fixture/state/target/serving");
    let r = seed(
        &mut m,
        state(),
        source(),
        CacheRole::Target,
        displaced(),
        bound(),
    )
    .unwrap();
    assert_eq!(r.cloned, vec!["model-source-member"]);
    assert!(!m.tree.contains_key(&state().join("model-source-complete")));
    assert!(!m.tree.contains_key(&state().join("target/peer-secret")));
}
#[test]
fn wrong_set_aside_source_symlink_and_existing_destination_refused_before_write() {
    let mut m = fixture();
    m.dir("/fixture/random");
    assert!(
        seed(
            &mut m,
            state(),
            Path::new("/fixture/random"),
            CacheRole::Owner,
            displaced(),
            bound()
        )
        .is_err()
    );
    assert!(m.mutations.is_empty());
    m.tree.insert(source().into(), Entry::Link);
    assert!(fresh(&mut m, state(), source()).is_err());
    assert!(m.mutations.is_empty());
}
#[test]
fn clone_command_is_explicit_apfs_preserving_copy_not_shell_or_delete() {
    let c = clone_command(
        Path::new("/fixture/source with spaces"),
        Path::new("/fixture/dest"),
    )
    .unwrap();
    assert_eq!(c.program, "/bin/cp");
    assert_eq!(
        c.args,
        vec!["-cRp", "/fixture/source with spaces", "/fixture/dest"]
    );
}
