// SPDX-License-Identifier: AGPL-3.0-only
#[path = "../examples/support/source_bytes.rs"]
mod source_bytes;
use obligations::FilePin;

fn pin() -> FilePin {
    FilePin {
        path: "synthetic.txt".into(),
        size_bytes: 3,
        sha256: "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad".into(),
    }
}
#[test]
fn real_bytes_match_sha256_positive_control() {
    assert_eq!(source_bytes::verify_bytes(&pin(), b"abc"), Ok(()));
}
#[test]
fn same_size_corrupt_source_bytes_are_rejected() {
    assert!(
        source_bytes::verify_bytes(&pin(), b"abd")
            .unwrap_err()
            .starts_with("source_sha256:")
    );
}
#[test]
fn corrupt_recorded_digest_is_rejected() {
    let mut pin = pin();
    pin.sha256 = "0".repeat(64);
    assert!(source_bytes::verify_bytes(&pin, b"abc").is_err());
}
#[test]
fn corrupt_source_size_is_rejected() {
    assert!(
        source_bytes::verify_bytes(&pin(), b"abcd")
            .unwrap_err()
            .starts_with("source_size:")
    );
}
#[test]
fn source_path_escape_is_rejected_before_git() {
    for path in ["../escape", "/absolute", "a:b", "a\\b", "a//b"] {
        assert!(
            source_bytes::read_object(
                std::path::Path::new("/nonexistent"),
                obligations::contracts::ACCEPTED_SOURCE,
                path
            )
            .unwrap_err()
            .starts_with("source_path:")
        );
    }
}
