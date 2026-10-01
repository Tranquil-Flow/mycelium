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

#[test]
fn evidence_export_writes_one_private_bundle_of_the_sealed_observations() {
    let dir = std::env::temp_dir().join(format!("mycelium-node-export-{}", std::process::id()));
    let out = dir.join("exports");
    let _ = std::fs::remove_dir_all(&dir);
    let core = NodeCore::open(&dir.join("node"), Reachability::LocalOnly).unwrap();
    assert_eq!(core.export_evidence(&out).unwrap_err().0, "mobile_evidence_export_empty");

    let envelope = core.runtime_observation(&mycelium_mobile_core::evidence::Metrics::new()).unwrap();
    std::fs::create_dir_all(dir.join("node/evidence")).unwrap();
    std::fs::write(dir.join("node/evidence/runtime-observation-000001.json"), serde_json::to_vec(&envelope).unwrap())
        .unwrap();
    let (path, count) = core.export_evidence(&out).unwrap();
    assert_eq!(count, 1);
    assert!(path.starts_with(&out));
    let name = path.file_name().unwrap().to_str().unwrap().to_owned();
    assert!(name.starts_with("mycelium-evidence-") && name.ends_with(".json"));
    let bundle: serde_json::Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    assert_eq!(bundle["protocol"], mycelium_mobile_core::evidence::EVIDENCE_EXPORT_PROTOCOL);
    assert_eq!(bundle["runtime_build_digest"], mycelium_mobile_core::node::runtime_build_digest());
    assert_eq!(bundle["observations"].as_array().unwrap().len(), 1);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
    }
    core.shutdown();
    let _ = std::fs::remove_dir_all(&dir);
}
