// SPDX-License-Identifier: AGPL-3.0-only
use journey_tests::process::{Scratch, check_host_markers};

#[test]
fn launch_admission_refuses_busy_markers_without_reading_them() {
    let home = std::path::PathBuf::from(std::env::var_os("HOME").unwrap());
    let mut scratch = Scratch::new(&home, None).unwrap();
    assert!(check_host_markers(&scratch.root).is_ok());
    assert!(check_host_markers(&scratch.root.join("absent")).is_ok());
    let marker = scratch.root.join("cert.busy");
    std::fs::write(&marker, b"not read").unwrap();
    assert_eq!(
        check_host_markers(&scratch.root).unwrap_err(),
        "BLOCKED:host_busy_marker"
    );
    std::fs::remove_file(marker).unwrap();
    assert!(check_host_markers(&scratch.root).is_ok());
    scratch.remove().unwrap();
}
