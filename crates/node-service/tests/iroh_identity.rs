// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "iroh-loopback")]
use node_service::{enrollment, iroh_frames, service};
use std::os::unix::fs::PermissionsExt;
fn root() -> std::path::PathBuf {
    let path = std::env::temp_dir()
        .canonicalize()
        .unwrap()
        .join(format!("slice-iroh-key-{}", service::random_id().unwrap()));
    std::fs::create_dir(&path).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
    path
}
#[test]
fn endpoint_secret_persists_independently_of_membership_signing_identity() {
    let root = root();
    let signing = enrollment::identity(&root).unwrap();
    let first = iroh_frames::endpoint_secret(&root).unwrap();
    let second = iroh_frames::endpoint_secret(&root).unwrap();
    assert_eq!(first.public(), second.public());
    assert!(
        first.to_bytes() != *signing.secret_bytes(),
        "endpoint and membership keys must be independent"
    );
    let before = std::fs::read(root.join("iroh-endpoint/node.key")).unwrap();
    assert_eq!(
        std::fs::metadata(root.join("iroh-endpoint/node.key"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    std::fs::set_permissions(
        root.join("iroh-endpoint/node.key"),
        std::fs::Permissions::from_mode(0o644),
    )
    .unwrap();
    assert!(iroh_frames::endpoint_secret(&root).is_err());
    assert!(
        std::fs::read(root.join("iroh-endpoint/node.key")).unwrap() == before,
        "refusal must not rotate the key"
    );
    std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn endpoint_secret_refuses_symlinked_private_state() {
    let root = root();
    let foreign = root.join("foreign");
    std::fs::create_dir(&foreign).unwrap();
    std::os::unix::fs::symlink(&foreign, root.join("iroh-endpoint")).unwrap();
    assert!(iroh_frames::endpoint_secret(&root).is_err());
    assert!(!foreign.join("node.key").exists());
    std::fs::remove_dir_all(root).unwrap();
}
