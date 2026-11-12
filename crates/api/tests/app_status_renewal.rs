// SPDX-License-Identifier: AGPL-3.0-only
//! `status.renewal_error` as the SHIPPED v0.1.4 app keeps it (`DemoApp._renew_loop`, app.py:911-927
//! @ f4e366e9, thread started by `DemoApp.start`, :638-639): a pass that fails while the app is
//! running records `renewal_unavailable`, the next successful pass clears it to `None`, and a pass
//! the loop skips (not running) leaves it as it was. The owner's own lease here (`authority.renew()`
//! = `app_host::renew_owner`, the pass both hosts run); a joined member's in the packaged test
//! `node-service/tests/packaged_status.rs`.
//!
//! The failure is a real one on the pass's own path: the owner's signing identity cannot be read
//! (its file made unreadable, then restored), so the heartbeat cannot be signed.
use api::app_host::{self, Authority, IDENTITY_FILE, Unmanaged};
use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf, sync::Mutex};

struct Root(PathBuf);
impl Root {
    fn new(tag: &str) -> Self {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir()
            .canonicalize()
            .unwrap()
            .join(format!("app-status-{tag}-{}-{stamp}", std::process::id()));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap_or_default();
    }
}
fn renewal_error(host: &Mutex<Authority<Unmanaged>>) -> Option<&'static str> {
    host.lock().unwrap().recorded.renewal_error
}

#[test]
fn a_failed_owner_renewal_shows_renewal_unavailable_until_the_next_pass_succeeds() {
    let root = Root::new("owner-renewal");
    let host = Mutex::new(Authority::<Unmanaged>::load(root.0.clone()).unwrap());
    let created = app_host::create_owner_json(
        &host,
        serde_json::json!({"storage_bytes": 4_000_000_000u64})
            .as_object()
            .unwrap(),
    );
    assert!(created.is_ok(), "create: {created:?}");
    app_host::renew_owner(&host);
    assert_eq!(renewal_error(&host), None, "a renewed lease shows no error");

    let identity = host.lock().unwrap().root.join(IDENTITY_FILE);
    fs::set_permissions(&identity, fs::Permissions::from_mode(0o000)).unwrap();
    app_host::renew_owner(&host);
    assert_eq!(
        renewal_error(&host),
        Some("renewal_unavailable"),
        "a failed pass while running"
    );
    // `if not self.running: continue`: a skipped pass changes nothing.
    host.lock().unwrap().running = false;
    app_host::renew_owner(&host);
    assert_eq!(renewal_error(&host), Some("renewal_unavailable"));
    host.lock().unwrap().running = true;

    fs::set_permissions(&identity, fs::Permissions::from_mode(0o600)).unwrap();
    app_host::renew_owner(&host);
    assert_eq!(renewal_error(&host), None, "the next success clears it");
    app_host::stop_authority(&host);
}
