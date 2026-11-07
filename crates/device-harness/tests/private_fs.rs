// SPDX-License-Identifier: AGPL-3.0-only
use device_harness::{observe::SnapshotStore, private_fs::*};
mod support;
use serde_json::json;
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use support::PrivateRoot as Scratch;
#[test]
fn private_join_exact_0600_single_link_owner_bound_and_symlink_refusal() {
    let s = Scratch::new("fs");
    let p = s.join("join.private");
    create_private(&p, b" PRIVATE-LINK ").unwrap();
    assert_eq!(
        fs::metadata(&p).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(read_join(&p, s.uid()).unwrap(), "PRIVATE-LINK");
    let alias = s.join("alias");
    symlink(&p, &alias).unwrap();
    assert!(read_join(&alias, s.uid()).is_err());
    assert!(read_join(&p, s.uid() + 1).is_err());
    fs::set_permissions(&p, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(read_join(&p, s.uid()).is_err());
    fs::set_permissions(&p, fs::Permissions::from_mode(0o600)).unwrap();
    fs::hard_link(&p, s.join("hard")).unwrap();
    assert!(read_join(&p, s.uid()).is_err());
}
#[test]
fn join_16384_byte_bound_empty_and_invalid_utf8_fail_closed() {
    let s = Scratch::new("fs");
    for (i, bytes, ok) in [
        (0, vec![b'a'; 16384], true),
        (1, vec![b'a'; 16385], false),
        (2, vec![], false),
        (3, vec![b' '; 4], false),
        (4, vec![255], false),
    ] {
        let p = s.join(i.to_string());
        create_private(&p, &bytes).unwrap();
        assert_eq!(read_join(&p, s.uid()).is_ok(), ok);
    }
}
#[test]
fn first_snapshot_create_never_overwrites_existing_file_or_follows_link() {
    let s = Scratch::new("fs");
    let p = s.join("failure.json");
    let mut store = FileSnapshot { path: p.clone() };
    assert!(!store.exists().unwrap());
    store.create_first(&json!({"failure":"first"})).unwrap();
    assert!(store.exists().unwrap());
    assert!(store.create_first(&json!({"failure":"second"})).is_err());
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&fs::read(&p).unwrap()).unwrap(),
        json!({"failure":"first"})
    );
    let alias = s.join("alias");
    symlink(&p, &alias).unwrap();
    assert!(create_private(&alias, b"second").is_err());
}
#[test]
fn marker_release_only_unlinks_its_exact_inode() {
    let s = Scratch::new("fs");
    let p = s.join("DEVICE.busy");
    let lease = MarkerLease::acquire(&p, &json!({"pid":123})).unwrap();
    assert!(MarkerLease::acquire(&p, &json!({"pid":456})).is_err());
    fs::rename(&p, s.join("original")).unwrap();
    create_private(&p, b"replacement").unwrap();
    assert!(lease.close().is_err());
    assert_eq!(fs::read(&p).unwrap(), b"replacement");
    fs::remove_file(&p).unwrap();
    let lease = MarkerLease::acquire(&p, &json!({"pid":123})).unwrap();
    lease.close().unwrap();
    assert!(!p.exists());
}
