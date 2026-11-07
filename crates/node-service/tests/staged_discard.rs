// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "iroh-loopback")]
//! Round 7 (parent disk finding): a run that fails leaves the member's staged model copy (~942 MB)
//! behind, because the supervisor kills the member and its removal-on-drop never runs. The owner
//! therefore discards the staged copy after any failed run, keeping the evidence of what it was
//! (size and pack digest, and whether it matched the offered source). Integrity checking of the
//! staging itself is untouched: the copy is still received chunk by chunk over the signed carrier.
use node_service::artifact::{self, Source};
use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf};

struct Temp(PathBuf);
impl Temp {
    fn new(name: &str) -> Self {
        let path =
            std::env::temp_dir().join(format!("v02-slice-discard-{}-{name}", std::process::id()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
    fn private_dir(&self, relative: &str) -> PathBuf {
        let dir = self.0.join(relative);
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        dir
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn bytes(length: usize) -> Vec<u8> {
    (0..length).map(|i| (i % 251) as u8).collect()
}

#[test]
fn a_complete_staged_copy_is_removed_and_recorded_as_matching_the_source() {
    let temp = Temp::new("complete");
    let payload = bytes(10_000);
    let source_path = temp.0.join("source.safetensors");
    fs::write(&source_path, &payload).unwrap();
    let source = Source::measure(&source_path).unwrap();
    let state = temp.private_dir("member");
    let staging = temp.private_dir("member/staging");
    fs::write(staging.join("model.safetensors"), &payload).unwrap();
    let record = temp.0.join("staged-discard.json");

    let outcome = artifact::discard_staged(&state, &source.pack_digest, &record)
        .unwrap()
        .expect("a staged copy existed");
    assert!(
        !staging.join("model.safetensors").exists(),
        "copy must be gone"
    );
    assert_eq!(outcome["size"], payload.len());
    assert_eq!(outcome["pack_digest"], source.pack_digest);
    assert_eq!(outcome["matches_source"], true);
    assert_eq!(outcome["removed"], true);
    let retained: serde_json::Value = serde_json::from_slice(&fs::read(&record).unwrap()).unwrap();
    assert_eq!(retained, outcome);
    assert!(
        source_path.exists(),
        "the read-only source is never touched"
    );
}

#[test]
fn a_partial_staged_copy_is_removed_and_recorded_as_not_matching() {
    let temp = Temp::new("partial");
    let payload = bytes(10_000);
    let source_path = temp.0.join("source.safetensors");
    fs::write(&source_path, &payload).unwrap();
    let source = Source::measure(&source_path).unwrap();
    let state = temp.private_dir("member");
    let staging = temp.private_dir("member/staging");
    fs::write(staging.join("model.safetensors"), &payload[..4_000]).unwrap();
    let record = temp.0.join("staged-discard.json");

    let outcome = artifact::discard_staged(&state, &source.pack_digest, &record)
        .unwrap()
        .unwrap();
    assert!(!staging.join("model.safetensors").exists());
    assert_eq!(outcome["size"], 4_000);
    assert_eq!(outcome["matches_source"], false);
    assert_ne!(outcome["pack_digest"], source.pack_digest);
}

#[test]
fn nothing_staged_means_nothing_to_discard_and_no_record() {
    let temp = Temp::new("none");
    let state = temp.private_dir("member");
    let record = temp.0.join("staged-discard.json");
    assert!(
        artifact::discard_staged(&state, "sha256:unused", &record)
            .unwrap()
            .is_none()
    );
    assert!(!record.exists());
}
