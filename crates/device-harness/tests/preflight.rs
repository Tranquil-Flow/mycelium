// SPDX-License-Identifier: AGPL-3.0-only
use device_harness::{admission::*, preflight::*};
use serde_json::json;
fn good() -> Preflight {
    Preflight {
        power: Power {
            source: PowerSource::Ac,
            percent: None,
        },
        network: NetworkClass::Unmetered,
        consent: NetworkConsent::UnmeteredOnly,
        free_bytes: 25 * 1024 * 1024 * 1024,
        memory_level: 1,
        current: Some("0.1.3".into()),
        previous: Some("0.1.2".into()),
        program_directories: ["0.1.3".into(), "0.1.2".into()].into(),
        busy: vec![],
        owned_marker: None,
        attempt_spent: false,
        state_readable: true,
    }
}
#[test]
fn clean_state_resource_boundary_and_unknown_records() {
    assert!(check(&good(), "0.2.0").is_ok());
    let mut p = good();
    p.free_bytes -= 1;
    assert_eq!(
        check(&p, "0.2.0").unwrap_err().code,
        "resource_admission_failed"
    );
    p = good();
    p.memory_level = 2;
    assert!(check(&p, "0.2.0").is_err());
    p = good();
    p.program_directories.insert("0.2.0".into());
    assert_eq!(
        check(&p, "0.2.0").unwrap_err().code,
        "preflight_target_version_stray"
    );
    p = good();
    p.program_directories.insert("old".into());
    assert_eq!(
        check(&p, "0.2.0").unwrap_err().code,
        "preflight_stray_programs"
    );
    p = good();
    p.state_readable = false;
    assert!(check(&p, "0.2.0").is_err());
    p = good();
    p.attempt_spent = true;
    assert!(check(&p, "0.2.0").is_err());
}
#[test]
fn owned_marker_exemption_requires_name_device_inode_not_just_pid() {
    let marker = Marker {
        name: "DEVICE.busy".into(),
        device: 1,
        inode: 2,
    };
    let mut p = good();
    p.busy.push(marker.clone());
    assert!(check(&p, "0.2.0").is_err());
    p.owned_marker = Some(marker);
    assert!(check(&p, "0.2.0").is_ok());
    p.busy[0].inode = 3;
    assert!(check(&p, "0.2.0").is_err());
    // Python Attempt allows CERT; host preflight does not. Preserve strict read-only preflight.
    p.busy[0].name = "CERT.busy".into();
    assert!(check(&p, "0.2.0").is_err());
}
fn identity() -> FreshIdentity {
    let p = ProcessIdentity {
        pid: 123,
        birth: "boot and birth".into(),
        executable: "/fixture/app/exe".into(),
    };
    FreshIdentity {
        launch_epoch_ms: 1000,
        control_modified_ms: 1000,
        expected: p.clone(),
        listener: Some(p),
        control_port: 1234,
        status_port: 1234,
        status_http: 200,
        status_observed_ms: 1001,
        expected_exe_sha256: "a".repeat(64),
        observed_exe_sha256: "a".repeat(64),
    }
}
#[test]
fn stale_readiness_tcp_listener_alone_or_wrong_process_cannot_pass() {
    assert!(fresh(&identity()).is_ok());
    let mut v = identity();
    v.control_modified_ms = 999;
    assert!(fresh(&v).is_err());
    v = identity();
    v.listener = None;
    assert!(fresh(&v).is_err());
    v = identity();
    v.listener.as_mut().unwrap().birth = "reused pid".into();
    assert!(fresh(&v).is_err());
    v = identity();
    v.status_port += 1;
    assert!(fresh(&v).is_err());
    v = identity();
    v.status_http = 503;
    assert!(fresh(&v).is_err());
    v = identity();
    v.status_observed_ms = 999;
    assert!(fresh(&v).is_err());
    v = identity();
    v.observed_exe_sha256 = "b".repeat(64);
    assert!(fresh(&v).is_err());
    v = identity();
    v.expected_exe_sha256 = " a".repeat(32);
    v.observed_exe_sha256 = v.expected_exe_sha256.clone();
    assert!(fresh(&v).is_err());
}
#[test]
fn different_exe_same_previous_version_must_be_set_aside_before_install() {
    let mut v = InstallState {
        target: "0.2.0".into(),
        current: "0.1.3".into(),
        previous: Some("0.2.0".into()),
        expected: "a".repeat(64),
        existing: Some("b".repeat(64)),
        app_running: false,
    };
    assert_eq!(
        install_decision(&v).unwrap(),
        InstallDecision::SetAsidePrevious {
            version: "0.2.0".into(),
            expected_old_digest: "b".repeat(64)
        }
    );
    v.current = "0.2.0".into();
    assert!(install_decision(&v).is_err());
    v.current = "0.1.3".into();
    v.previous = None;
    assert!(install_decision(&v).is_err());
    v.previous = Some("0.2.0".into());
    v.existing = Some("a".repeat(64));
    assert_eq!(
        install_decision(&v).unwrap(),
        InstallDecision::AlreadyIdentical
    );
    v.existing = None;
    assert_eq!(install_decision(&v).unwrap(), InstallDecision::Install);
    v.app_running = true;
    assert!(install_decision(&v).is_err());
}
#[test]
fn starter_cache_and_disk_cache_have_distinct_exact_assertions() {
    for state in ["prepared", "closed", "failed", "idle"] {
        for error in [json!(null), json!("prepared_source_revalidation_required")] {
            assert!(cached(&json!({"state":state,"downloaded_bytes":999604126,"total_bytes":999604126,"error_code":error})).is_ok());
        }
    }
    for (state, done, total, accepted) in [
        ("closed", 999604126, 999604126, true),
        ("prepared", 5, 5, true),
        ("closed", 0, 0, false),
        ("closed", 10, 999604126, false),
        ("downloading", 5, 5, false),
    ] {
        assert_eq!(
            cache_complete(
                &json!({"state":state,"downloaded_bytes":done,"total_bytes":total,"error_code":null})
            ),
            accepted
        );
    }
    for a in [
        json!({"state":"closed","downloaded_bytes":0,"total_bytes":0}),
        json!({"state":"closed","downloaded_bytes":5,"total_bytes":5}),
        json!({"state":"closed","downloaded_bytes":999604126,"total_bytes":999604126,"error_code":"x"}),
    ] {
        assert!(cached(&a).is_err());
    }
}
