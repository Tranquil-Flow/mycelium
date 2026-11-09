// SPDX-License-Identifier: AGPL-3.0-only
//! Set-aside durability faults of the stored product window (B15, REVIEW-laneB13f FU1), on the
//! store directly: a directory sync failure cannot be produced through the public routes.
use super::{STATE_FILE, StateBinding, Store, Stored};
use std::{
    fs,
    os::unix::fs::{DirBuilderExt, PermissionsExt},
    path::{Path, PathBuf},
};

struct Root(PathBuf);
impl Drop for Root {
    fn drop(&mut self) {
        // Only the directory this test created is removed.
        if let Err(error) = fs::remove_dir_all(&self.0) {
            eprintln!("scratch root not removed: {error}");
        }
    }
}
fn root(label: &str) -> Root {
    let base = fs::canonicalize(std::env::temp_dir()).unwrap();
    let path = base.join(format!("b15-{label}-{}", std::process::id()));
    fs::DirBuilder::new().mode(0o700).create(&path).unwrap();
    Root(path)
}
fn store(root: &Path) -> Store {
    Store::open(
        StateBinding {
            root,
            swarm_id: "sw-b15aaaaaaaaaaaaaaaaaa",
        },
        8,
    )
    .unwrap()
}
fn failing_sync(_: &Path) -> std::io::Result<()> {
    Err(std::io::Error::other("injected directory sync failure"))
}

/// The rename is the move: once the window has left its name, a failed directory sync must not
/// report it as still in place (the caller would refuse a window that is no longer there, and
/// nothing could reset it until a restart). The moved bytes are kept under the reported name.
#[test]
fn a_directory_sync_failure_after_the_rename_still_reports_the_move() {
    let root = root("sync");
    let store = store(&root.0);
    fs::write(root.0.join(STATE_FILE), b"{not json").unwrap();
    fs::set_permissions(root.0.join(STATE_FILE), fs::Permissions::from_mode(0o600)).unwrap();
    let moved = store.set_aside_synced("reset", 7, failing_sync);
    assert!(
        !root.0.join(STATE_FILE).exists(),
        "the rename did not happen"
    );
    let name = moved.expect("a completed rename must be reported as the move it is");
    assert_eq!(fs::read(root.0.join(&name)).unwrap(), b"{not json");
    assert!(
        matches!(store.load(), Ok(Stored::Window(events)) if events.is_empty()),
        "the next application starts a fresh window"
    );
}

/// Exclusive set-aside (B15, REVIEW-laneB13f FU2): a file already at a set-aside name (another
/// process's, or anything else) is never replaced; the window takes the next suffix.
#[test]
fn a_set_aside_never_replaces_an_existing_file() {
    let root = root("exclusive");
    let store = store(&root.0);
    fs::write(root.0.join(STATE_FILE), b"{not json").unwrap();
    fs::set_permissions(root.0.join(STATE_FILE), fs::Permissions::from_mode(0o600)).unwrap();
    let taken = format!("{STATE_FILE}.reset-7");
    fs::write(root.0.join(&taken), b"older").unwrap();
    let name = store.set_aside("reset", 7).unwrap();
    assert_ne!(name, taken);
    assert_eq!(
        fs::read(root.0.join(&taken)).unwrap(),
        b"older",
        "an existing file was replaced"
    );
    assert_eq!(fs::read(root.0.join(&name)).unwrap(), b"{not json");
}

/// Set-asides are pruned (B15, REVIEW-laneB13f FU3): after a set-aside only the newest
/// `KEEP_SET_ASIDE` set-aside files stay (the one just moved always among them), whatever kind;
/// the active window's name and every other file in the directory are never touched.
#[test]
fn set_asides_are_pruned_to_the_newest_few() {
    let root = root("prune");
    let store = store(&root.0);
    let older = [
        format!("{STATE_FILE}.reset-1"),
        format!("{STATE_FILE}.foreign-swarm-2"),
        format!("{STATE_FILE}.unbound-3-1"),
        format!("{STATE_FILE}.reset-4"),
        format!("{STATE_FILE}.reset-40"),
    ];
    for name in &older {
        fs::write(root.0.join(name), b"old").unwrap();
    }
    let unrelated = [format!("{STATE_FILE}.reset-x"), "notes.json".to_owned()];
    for name in &unrelated {
        fs::write(root.0.join(name), b"keep").unwrap();
    }
    fs::write(root.0.join(STATE_FILE), b"{not json").unwrap();
    fs::set_permissions(root.0.join(STATE_FILE), fs::Permissions::from_mode(0o600)).unwrap();
    // The clock reads earlier than the newest set-aside: the one just moved is kept anyway.
    let moved = store.set_aside("reset", 3).unwrap();
    let mut left: Vec<String> = fs::read_dir(&root.0)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .collect();
    left.sort();
    let mut expected = vec![
        moved,
        format!("{STATE_FILE}.reset-40"),
        format!("{STATE_FILE}.reset-4"),
        unrelated[0].clone(),
        unrelated[1].clone(),
    ];
    expected.sort();
    assert_eq!(
        left, expected,
        "set-asides were not pruned to the newest three"
    );
}
