// SPDX-License-Identifier: AGPL-3.0-only
//! Trusted host admission resolves ancestors; seed descendants stay no-follow.
use api::app_host::{self, Authority, Unmanaged};
use std::{fs, path::PathBuf, sync::Mutex};

struct Root(PathBuf);
impl Root {
    fn new(tag: &str) -> Self {
        let path = std::env::temp_dir().join(format!("app-host-root-{tag}-{}", std::process::id()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

#[test]
fn host_under_symlinked_ancestor_starts_seed() {
    let root = Root::new("ancestor");
    fs::create_dir(root.0.join("physical")).unwrap();
    std::os::unix::fs::symlink(root.0.join("physical"), root.0.join("alias")).unwrap();
    let app = root.0.join("alias/app");
    fs::create_dir(&app).unwrap();
    let host = Mutex::new(Authority::<Unmanaged>::load(app).unwrap());
    let result = app_host::create_owner_json(
        &host,
        serde_json::json!({"storage_bytes":4_000_000_000u64})
            .as_object()
            .unwrap(),
    );
    if result.is_ok() {
        app_host::stop_authority(&host);
    }
    assert!(
        result.is_ok(),
        "seed must start through the host: {result:?}"
    );
    assert_eq!(
        host.lock().unwrap().root,
        root.0.join("physical/app").canonicalize().unwrap()
    );
}

#[test]
fn admitted_host_still_refuses_seed_symlink_below_root() {
    let root = Root::new("child");
    let app = root.0.join("app");
    fs::create_dir(&app).unwrap();
    let host = Mutex::new(Authority::<Unmanaged>::load(app.clone()).unwrap());
    let outside = root.0.join("outside");
    fs::create_dir(&outside).unwrap();
    std::os::unix::fs::symlink(&outside, app.join("seed-rs")).unwrap();
    assert!(
        app_host::create_owner_json(
            &host,
            serde_json::json!({"storage_bytes":4_000_000_000u64})
                .as_object()
                .unwrap()
        )
        .is_err()
    );
    assert!(!outside.join("seed.sqlite").exists());
    assert!(!app_host::coordinator_ready(&host));
}
