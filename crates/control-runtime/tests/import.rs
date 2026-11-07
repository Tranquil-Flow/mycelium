// SPDX-License-Identifier: AGPL-3.0-only
// O:path:mycelium_node/identity.py; O:path:mycelium_a13_macos/standing_consent.py; F3
use control_runtime::{Store, import::LegacyState};
use serde_json::{Value, json};
use std::{os::unix::fs::PermissionsExt, path::PathBuf};
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l6-import-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir(&p).unwrap();
        // The fixture owns this newly created directory. macOS TMPDIR commonly
        // uses /var -> /private/var; pass its physical path, not a symlinked
        // ancestor. Production import intentionally refuses every symlink.
        Self(p.canonicalize().unwrap())
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
fn fixture(root: &std::path::Path) {
    std::fs::create_dir(root).unwrap();
    std::fs::set_permissions(root, std::fs::Permissions::from_mode(0o700)).unwrap();
    for (name, bytes) in [
        ("node.key", vec![0x22; 32]),
        (
            "standing-consent.json",
            include_bytes!("fixtures/standing-consent-v014.json").to_vec(),
        ),
    ] {
        let p = root.join(name);
        std::fs::write(&p, bytes).unwrap();
        std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o400)).unwrap();
    }
}
fn binding() -> Value {
    json!({"swarm_id":"swarm-fixture","node_id":"node-member","seed_key_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"})
}
#[test]
fn real_private_files_import_preserves_identity_grant_and_source_bytes() {
    let t = Temp::new();
    let source = t.0.join("legacy");
    fixture(&source);
    let before = std::fs::read(source.join("standing-consent.json")).unwrap();
    let imported = LegacyState::read(
        &source,
        &binding(),
        &json!({"storage_bytes":4_000_000_000_u64}),
        &json!(8_589_934_592_u64),
        budgets::Bytes::new(262144),
    )
    .unwrap();
    let expected = protocol::identity::Identity::from_secret("unused", &[0x22; 32]).unwrap();
    assert_eq!(
        imported.identity().public_key_record(),
        expected.public_key_record()
    );
    assert_eq!(
        imported.identity().endpoint_id(),
        format!(
            "node-identity-{}",
            &expected.verification_key_digest()[7..39]
        )
    );
    let mut store = Store::open(&t.0.join("state.db")).unwrap();
    imported.persist(&mut store).unwrap();
    drop(store);
    let store = Store::open(&t.0.join("state.db")).unwrap();
    let restored = LegacyState::restore(&store).unwrap();
    assert_eq!(
        restored.identity().public_key_record(),
        expected.public_key_record()
    );
    assert_eq!(restored.grant()["caps"]["max_wall_seconds"], 600);
    assert_eq!(restored.grant()["caps"]["storage_bytes"], 2_000_000_000_u64);
    assert_eq!(
        restored
            .admission(
                &binding(),
                &json!({"target_node_id":"node-member","archive_bytes":1}),
                None
            )
            .unwrap(),
        None
    );
    assert!(
        restored
            .admission(
                &binding(),
                &json!({"target_node_id":"node-member","archive_bytes":3_000_000_000_u64}),
                None
            )
            .unwrap()
            .is_some()
    );
    assert_eq!(
        std::fs::read(source.join("standing-consent.json")).unwrap(),
        before
    );
    assert_eq!(
        std::fs::read(source.join("node.key")).unwrap(),
        vec![0x22; 32]
    );
}
#[test]
fn import_refuses_foreign_binding_symlinks_bad_disclosure_and_never_overwrites() {
    let t = Temp::new();
    let source = t.0.join("legacy");
    fixture(&source);
    let load = |b: &Value| {
        LegacyState::read(
            &source,
            b,
            &json!({"storage_bytes":4_000_000_000_u64}),
            &Value::Null,
            budgets::Bytes::new(262144),
        )
    };
    let alias = t.0.join("alias");
    std::os::unix::fs::symlink(&source, &alias).unwrap();
    assert!(
        LegacyState::read(
            &alias,
            &binding(),
            &json!({"storage_bytes":4_000_000_000_u64}),
            &Value::Null,
            budgets::Bytes::new(262144),
        )
        .is_err(),
        "production must still refuse a symlinked import root"
    );
    let ancestor = t.0.join("ancestor");
    std::os::unix::fs::symlink(&t.0, &ancestor).unwrap();
    assert!(
        LegacyState::read(
            &ancestor.join("legacy"),
            &binding(),
            &json!({"storage_bytes":4_000_000_000_u64}),
            &Value::Null,
            budgets::Bytes::new(262144),
        )
        .is_err(),
        "production must still refuse a symlinked ancestor"
    );
    let mut b = binding();
    b["node_id"] = "other".into();
    assert!(load(&b).is_err());
    let state = load(&binding()).unwrap();
    let mut store = Store::open(&t.0.join("state.db")).unwrap();
    state.persist(&mut store).unwrap();
    assert!(state.persist(&mut store).is_err());
    std::fs::rename(source.join("node.key"), source.join("real.key")).unwrap();
    std::os::unix::fs::symlink("real.key", source.join("node.key")).unwrap();
    assert!(load(&binding()).is_err());
    std::fs::remove_file(source.join("node.key")).unwrap();
    std::fs::rename(source.join("real.key"), source.join("node.key")).unwrap();
    let path = source.join("standing-consent.json");
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
    let mut grant: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    grant["disclosure_digest"] = "unknown".into();
    std::fs::write(path, serde_json::to_vec(&grant).unwrap()).unwrap();
    assert!(load(&binding()).is_err());
}
