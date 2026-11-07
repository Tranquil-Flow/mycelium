// SPDX-License-Identifier: AGPL-3.0-only
use device_harness::{Fault, FaultKind, attempt::*, preflight::ProcessIdentity, private_fs::*};
mod support;
use serde_json::{Value, json};
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use support::PrivateRoot as Scratch;
fn process() -> ProcessIdentity {
    ProcessIdentity {
        pid: std::process::id(),
        birth: "birth".into(),
        executable: "exact harness".into(),
    }
}
#[test]
fn attempt_refuses_busy_and_spent_outputs_and_records_exact_identity() {
    let s = Scratch::new("attempt");
    let out = s.join("out");
    let markers = s.join("markers");
    fs::create_dir(&markers).unwrap();
    fs::write(markers.join("OTHER.busy"), b"other").unwrap();
    assert_eq!(
        Attempt::open(
            &out,
            &markers,
            "soak",
            process(),
            Parallel::CertAllowed,
            s.uid()
        )
        .err()
        .unwrap()
        .code,
        "preflight_busy_marker"
    );
    assert!(!markers.join("DEVICE.busy").exists());
    fs::remove_file(markers.join("OTHER.busy")).unwrap();
    fs::write(markers.join("CERT.busy"), b"cert").unwrap();
    assert!(Attempt::open(&out, &markers, "soak", process(), Parallel::Quiet, s.uid()).is_err());
    let a = Attempt::open(
        &out,
        &markers,
        "soak",
        process(),
        Parallel::CertAllowed,
        s.uid(),
    )
    .unwrap();
    let row: Value =
        serde_json::from_slice(&fs::read(out.join("harness-pid.json")).unwrap()).unwrap();
    assert_eq!(row["pid"], std::process::id());
    assert_eq!(
        row["identity"],
        device_harness::package::digest(b"birth exact harness")
    );
    a.finish(&json!({"outcome":"incomplete","error":"operator_stop"}))
        .unwrap();
    assert!(!markers.join("DEVICE.busy").exists());
    assert!(markers.join("CERT.busy").exists());
    assert!(
        Attempt::open(
            &out,
            &markers,
            "soak",
            process(),
            Parallel::CertAllowed,
            s.uid()
        )
        .is_err()
    );
}
#[test]
fn session_rewrite_repairs_mode_and_refuses_links_without_exposing_cookie() {
    let s = Scratch::new("attempt");
    let p = s.join("session");
    fs::write(&p, b"{}").unwrap();
    fs::set_permissions(&p, fs::Permissions::from_mode(0o644)).unwrap();
    let uid = s.uid();
    rewrite_session(&p, uid, b"fixture=private").unwrap();
    assert_eq!(
        fs::metadata(&p).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(read_private(&p, uid, 16384).unwrap(), b"fixture=private");
    let alias = s.join("alias");
    symlink(&p, &alias).unwrap();
    assert!(rewrite_session(&alias, uid, b"wrong").is_err());
    fs::hard_link(&p, s.join("hard")).unwrap();
    assert!(rewrite_session(&p, uid, b"wrong").is_err());
    assert_eq!(fs::read(&p).unwrap(), b"fixture=private");
}
#[test]
fn failed_result_write_still_releases_own_marker_and_preserves_first_output() {
    let s = Scratch::new("attempt");
    let out = s.join("out");
    let markers = s.join("markers");
    let a = Attempt::open(
        &out,
        &markers,
        "stranger",
        process(),
        Parallel::Quiet,
        s.uid(),
    )
    .unwrap();
    fs::write(out.join("RESULT.json"), b"first").unwrap();
    assert!(a.finish(&json!({"error":"second"})).is_err());
    assert_eq!(fs::read(out.join("RESULT.json")).unwrap(), b"first");
    assert!(!markers.join("DEVICE.busy").exists());
}
#[test]
fn outer_effect_fence_is_not_a_candidate_refusal() {
    let failure = Fault::new("operator_stop", FaultKind::Observation);
    assert!(failure.incomplete());
    let s = Scratch::new("attempt");
    let out = s.join("out");
    let markers = s.join("markers");
    let a = Attempt::open(
        &out,
        &markers,
        "stranger",
        process(),
        Parallel::Quiet,
        s.uid(),
    )
    .unwrap();
    let fence = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        a.execute(
            || panic!("OUTER_SAFETY_FENCE"),
            |_| panic!("must not convert a fence to a candidate failure"),
        )
    }));
    assert!(fence.is_err());
    assert!(!out.join("RESULT.json").exists());
    assert!(markers.join("DEVICE.busy").exists());
}
