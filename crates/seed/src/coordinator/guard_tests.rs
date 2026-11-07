// SPDX-License-Identifier: AGPL-3.0-only
//! White-box lock witness supplements public HTTP and independent-database tests.
use super::*;
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    sync::{
        Barrier,
        atomic::{AtomicBool, Ordering},
    },
    thread,
};

/// Proves B-AUTH-01/B-SWARM-16: the same mutex used by revoke is held until callback commit.
#[test]
fn held_current_member_callback_serializes_local_revoke() {
    let root = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("l5-r15-local-lock-{}", std::process::id()));
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let now = 1000.0; // Recorded membership fixture instant.
    let seed = Coordinator::open(
        &root.join("seed.sqlite"),
        Identity::from_secret("seed-endpoint", &[41; 32]).unwrap(),
        Options::new("swarm-fixture", "seed-node", "seed-boot").unwrap(),
        Box::new(move || now),
        Box::new(|| "seed-message".into()),
    )
    .unwrap();
    seed.bind_seed_url("https://seed.example.test").unwrap();
    let bundle = seed
        .mint_invite("invite-fixture", seed.limits.max_ttl_seconds as u64)
        .unwrap();
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../protocol/fixtures/contracts/membership-signed-message-v1.json"
    ))
    .unwrap();
    let member = Identity::from_secret("endpoint-node-a", &[42; 32]).unwrap();
    let envelope = membership::sign(&member, &fixture["message"], seed.limits).unwrap();
    seed.accept_join_value(&bundle["token"], &envelope).unwrap();
    let entered = Barrier::new(2);
    let attempted = Barrier::new(2);
    let release = Barrier::new(2);
    let committed = AtomicBool::new(false);
    thread::scope(|scope| {
        let guard = scope.spawn(|| {
            seed.with_current_member("node-a", |_, _| {
                // Direct nonblocking observation, not absence of progress during a sleep.
                let held = seed.inner.try_lock().is_err();
                entered.wait();
                release.wait();
                committed.store(true, Ordering::SeqCst);
                Ok(held)
            })
        });
        entered.wait();
        let revoker = scope.spawn(|| {
            attempted.wait();
            let value = seed
                .advance_member_generation("node-a", 1, "STOPPED")
                .unwrap();
            (value, committed.load(Ordering::SeqCst))
        });
        attempted.wait();
        release.wait();
        assert!(
            guard.join().unwrap().unwrap(),
            "current-member callback must hold coordinator mutex"
        );
        let (revoked, after_commit) = revoker.join().unwrap();
        assert!(after_commit, "revoke returned before callback commit");
        assert_eq!(revoked["generation"], 2);
    });
    drop(seed);
    fs::remove_dir_all(root).unwrap();
}
