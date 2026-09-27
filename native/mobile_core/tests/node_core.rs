// SPDX-License-Identifier: AGPL-3.0-or-later
#![cfg(feature = "net")]
//! NodeCore lifecycle without a seed: durable identity across restarts, fresh
//! incarnation, route_ready false, and fail-closed pairing input.

use mycelium_mobile_core::node::NodeCore;
use mycelium_mobile_core::transport::Reachability;

#[test]
fn identity_is_durable_across_restart_and_bad_qr_fails_closed() {
    let dir = std::env::temp_dir().join(format!("mycelium-node-core-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let first = NodeCore::open(&dir, Reachability::LocalOnly).unwrap();
    let s = first.status();
    assert_eq!(s.phase, "unpaired");
    assert!(!s.route_ready);
    assert_eq!(s.peer_class, "android_native_iroh");
    let id = s.endpoint_id.clone();
    assert_eq!(first.pair_with_channel_qr("not-a-qr", "dev-1").unwrap_err().0, "android_onboarding_qr_invalid");
    assert_eq!(first.status().last_error.as_deref(), Some("android_onboarding_qr_invalid"));
    assert_eq!(first.join().unwrap_err().0, "not_paired");
    first.shutdown();

    let second = NodeCore::open(&dir, Reachability::LocalOnly).unwrap();
    assert_eq!(second.status().endpoint_id, id);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(dir.join("member.key")).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o600);
    }
    second.shutdown();
    let _ = std::fs::remove_dir_all(&dir);
}
