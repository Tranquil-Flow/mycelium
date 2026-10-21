// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "net")]
//! NodeCore lifecycle without a seed: durable identity across restarts, fresh
//! incarnation, route_ready false, and fail-closed pairing input.

use mycelium_mobile_core::node::NodeCore;
use mycelium_mobile_core::transport::Reachability;

#[test]
fn q2_profile_is_explicit_and_reference_pairing_semantics_survive() {
    use mycelium_mobile_core::{node::NodeProfile, onboarding::{self,PairingSession}, identity::Identity};
    let id=Identity::from_secret("synthetic-endpoint", &[42;32]).unwrap();
    let request = |class| PairingSession::with_recipient_for_class(&id,"synthetic-device",100.0,60,"nonce-1",
        x25519_dalek::StaticSecret::from([43;32]),class).unwrap().request;
    assert_eq!(NodeProfile::Reference.peer_class(),"android_native_iroh");
    assert_eq!(NodeProfile::Reference.backend(),"mobile-rust-reference");
    assert_eq!(NodeProfile::Qwen2.peer_class(),"android_native_qwen2_iroh");
    assert_eq!(NodeProfile::Qwen2.backend(),"mobile-rust-qwen2");
    let reference=request(NodeProfile::Reference.peer_class());
    let q2=request(NodeProfile::Qwen2.peer_class());
    assert_ne!(reference["signature"],q2["signature"]);
    onboarding::verify_request(&q2,101.0).unwrap();
    onboarding::verify_request(&reference,101.0).unwrap();
}

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

#[test]
fn a_failed_pairing_returns_the_node_to_its_previous_phase() {
    use base64::Engine;
    let dir = std::env::temp_dir().join(format!("mycelium-node-pairfail-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    // A closed loopback port: the pairing POST fails like a 502 from a closed channel.
    let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let port = closed.local_addr().unwrap().port();
    drop(closed);
    let channel = serde_json::json!({
        "expires_at": 4_000_000_000u64,
        "pairing_url": format!("http://127.0.0.1:{port}/pair"),
        "protocol": "mycelium.pairing_channel.v1",
    });
    let bytes = mycelium_mobile_core::canonical::to_bytes(&channel).unwrap();
    let qr = format!("mycelium:{}", base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes));
    let core = NodeCore::open(&dir, Reachability::LocalOnly).unwrap();
    let err = core.pair_with_channel_qr(&qr, "dev-1").unwrap_err().0;
    assert!(err.starts_with("pairing_channel_unreachable:"), "{err}");
    let s = core.status();
    assert_eq!(s.phase, "unpaired");
    assert_eq!(s.last_error.as_deref(), Some(err.as_str()));
    core.shutdown();
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn shell_sampled_power_and_thermal_readings_reach_the_sealed_observation() {
    use mycelium_mobile_core::evidence::Metrics;
    let dir = std::env::temp_dir().join(format!("mycelium-node-platform-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let core = NodeCore::open(&dir, Reachability::LocalOnly).unwrap();
    let mut sample = Metrics::new();
    sample.insert("battery_level_percent".into(), Some(64.0));
    sample.insert("charging".into(), Some(0.0));
    sample.insert("battery_temperature_celsius".into(), Some(33.5));
    sample.insert("thermal_status".into(), Some(1.0));
    core.set_platform_metrics(sample);
    let metrics = core.platform_metrics();
    assert_eq!(metrics["battery_level_percent"], Some(64.0));
    assert_eq!(metrics["charging"], Some(0.0));
    assert_eq!(metrics["thermal_status"], Some(1.0));
    // The host's own readable thermal zones (if any) may be hotter than the battery.
    assert!(metrics["thermal_max_celsius"].is_some_and(|t| t >= 33.5));
    let envelope = core.runtime_observation(&metrics).unwrap();
    let observed = &envelope["message"]["metrics"];
    assert_eq!(observed["battery_level_percent"], 64.0);
    assert_eq!(observed["charging"], 0.0);
    assert!(observed["thermal_max_celsius"].is_number());
    core.shutdown();
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn serving_intent_survives_a_restart_and_defaults_to_not_serving() {
    let dir = std::env::temp_dir().join(format!("mycelium-node-serving-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    let first = NodeCore::open(&dir, Reachability::LocalOnly).unwrap();
    assert!(!first.serving_intent());
    assert!(!first.status().serving_intent);
    assert!(first.set_serving_intent(true).unwrap().serving_intent);
    first.shutdown();

    let second = NodeCore::open(&dir, Reachability::LocalOnly).unwrap();
    assert!(second.serving_intent(), "a restarted node remembers the user chose to serve");
    assert!(!second.set_serving_intent(false).unwrap().serving_intent);
    second.shutdown();
    let third = NodeCore::open(&dir, Reachability::LocalOnly).unwrap();
    assert!(!third.serving_intent());
    third.shutdown();

    // A corrupt record never turns serving on.
    std::fs::write(dir.join("serving.json"), b"{\"serving\": true}").unwrap();
    let fourth = NodeCore::open(&dir, Reachability::LocalOnly).unwrap();
    assert!(!fourth.serving_intent());
    fourth.shutdown();
    let _ = std::fs::remove_dir_all(&dir);
}
