// SPDX-License-Identifier: AGPL-3.0-only
use journey_tests::process::{Target, sandbox_profile, scratch_base};
use std::path::Path;
use std::sync::Mutex;

// Scratch roots are named by this process id, so the two launch tests (and their
// before/after root listings) must not interleave.
static LAUNCH_TESTS: Mutex<()> = Mutex::new(());

#[test]
fn target_identity_fails_closed_before_spawn() {
    let base = Target {
        implementation: "python-v0.1.4".into(),
        source_sha: "f4e366e98a1f705dfaf182897044500d290dbd7f".into(),
        executable: "/usr/bin/true".into(),
        executable_sha256: "8fff41823d357949ffa55c00b3fb8dfb0b4cc541f66343a72f21e3d8dd89186f"
            .into(),
    };
    assert_eq!(
        base.verify().unwrap_err(),
        "target:executable_digest_mismatch"
    );
    let mut historical = base.clone();
    historical.source_sha = "f5bfb70981569c79cb873afe8e0c5be20342db7e".into();
    historical.executable_sha256 =
        "af681b346ef301c7b36bcdfe2f89b5e71053c349f02d2445cd98bafe5fa3e69c".into();
    assert_eq!(
        historical.verify().unwrap_err(),
        "target:uncertified_oracle"
    );
    let mut superseded = base.clone();
    superseded.source_sha = "ef9ae61de0222c78d83c8133f8f709619394bf51".into();
    superseded.executable_sha256 =
        "bc06dc664353c5dd15b937afd8e5a5f464131c5248fd906a325a642c37e85467".into();
    assert_eq!(
        superseded.verify().unwrap_err(),
        "target:uncertified_oracle"
    );
    let mut invalid = base.clone();
    invalid.source_sha = "repaired-f5bfb709".into();
    assert!(invalid.verify().unwrap_err().contains("source_sha"));
}
#[test]
fn early_child_exit_fails_readiness_and_removes_its_scratch() {
    use journey_tests::process::{App, digest_file};
    let _serial = LAUNCH_TESTS.lock().unwrap_or_else(|e| e.into_inner());
    let home = std::path::PathBuf::from(std::env::var_os("HOME").unwrap());
    let prefix = format!("v02-l3-{}-", std::process::id());
    let roots = || {
        std::fs::read_dir(scratch_base(&home).unwrap())
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|path| {
                path.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with(&prefix)
            })
            .collect::<std::collections::BTreeSet<_>>()
    };
    let before = roots();
    let executable = std::path::PathBuf::from("/usr/bin/true");
    let target = Target {
        implementation: "rust".into(),
        source_sha: "0".repeat(40),
        executable_sha256: digest_file(&executable).unwrap(),
        executable,
    };
    // Hermetic: the busy-marker root is injected, so the test never reads the host's
    // ~/mycelium-demo-run. The marker rule itself is asserted in the next test.
    let markers = tempfile_dir("early-exit-markers");
    let error = App::launch_with_marker_root(&target, &home, &markers, None)
        .err()
        .expect("early exit passed readiness");
    std::fs::remove_dir_all(&markers).unwrap();
    assert!(error.contains("process:readiness"), "{error}");
    assert!(error.contains("cleanup=Ok(())"), "{error}");
    assert_eq!(roots(), before);
}

fn tempfile_dir(label: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("v02-l3-{label}-{}", std::process::id()));
    if dir.exists() {
        std::fs::remove_dir_all(&dir).unwrap();
    }
    std::fs::create_dir(&dir).unwrap();
    dir
}

#[test]
fn injected_busy_marker_blocks_launch_before_spawn_and_leaves_no_scratch() {
    use journey_tests::process::{App, digest_file};
    let _serial = LAUNCH_TESTS.lock().unwrap_or_else(|e| e.into_inner());
    let home = std::path::PathBuf::from(std::env::var_os("HOME").unwrap());
    let prefix = format!("v02-l3-{}-", std::process::id());
    let roots = || {
        std::fs::read_dir(scratch_base(&home).unwrap())
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .filter(|path| {
                path.file_name()
                    .unwrap()
                    .to_string_lossy()
                    .starts_with(&prefix)
            })
            .collect::<std::collections::BTreeSet<_>>()
    };
    let before = roots();
    let markers = tempfile_dir("busy-markers");
    // Any *.busy blocks (including a device-calibration one): the journey launcher stays
    // stricter than the resume guard and never reads the marker.
    std::fs::write(markers.join("DEVICE.busy"), b"not read").unwrap();
    let executable = std::path::PathBuf::from("/usr/bin/true");
    let target = Target {
        implementation: "rust".into(),
        source_sha: "0".repeat(40),
        executable_sha256: digest_file(&executable).unwrap(),
        executable,
    };
    let error = App::launch_with_marker_root(&target, &home, &markers, None)
        .err()
        .expect("busy marker did not block launch");
    std::fs::remove_dir_all(&markers).unwrap();
    assert_eq!(error, "BLOCKED:host_busy_marker");
    assert_eq!(roots(), before);
}

#[test]
fn isolation_profile_never_grants_general_network_or_writes() {
    let profile = sandbox_profile(
        Path::new("/Users/test"),
        Path::new("/Users/test/.hermes/cache/scratch/v02-l3-1"),
    )
    .unwrap();
    assert!(profile.contains("(deny network*)"));
    assert!(profile.contains("(deny file-write*)"));
    assert!(profile.contains("localhost:*"));
    assert!(profile.contains("/Users/test/Library"));
    assert!(profile.contains("/Users/test/Applications"));
    assert!(profile.contains("/Users/test/mycelium-demo-run"));
    assert!(sandbox_profile(Path::new("/Users/te\"st"), Path::new("/tmp/no")).is_err());
    assert_eq!(
        sandbox_profile(Path::new("/Users/test"), Path::new("/tmp/no")).unwrap_err(),
        "sandbox:scratch_not_owned"
    );
}
