// SPDX-License-Identifier: AGPL-3.0-only
//! Original signing allocation is shared, not copied or newly generated.
use protocol::identity::{self, Identity};
use seed::{Coordinator, Options};
use serde_json::json;
use std::{fs, path::PathBuf, sync::Arc};

struct Temp(PathBuf);
impl Drop for Temp {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

#[test]
fn shared_signer_is_original_allocation_and_survives_coordinator_drop() {
    let root = fs::canonicalize(std::env::temp_dir()).unwrap();
    let dir = Temp(root.join(format!("seed-shared-signer-{}", std::process::id())));
    fs::create_dir(&dir.0).unwrap();
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(&dir.0, fs::Permissions::from_mode(0o700)).unwrap();
    let original = Identity::from_secret("endpoint-seed", &[0x11; 32]).unwrap();
    let statement = json!({"probe":"original-seed-signing-capability"});
    let key = original.public_key_record();
    let signature = original.sign(&statement).unwrap();
    let coordinator = Coordinator::open(
        &dir.0.join("seed.db"),
        original,
        Options::new("swarm-fixture", "node-owner", "incarnation-seed").unwrap(),
        Box::new(|| 1000.0),
        Box::new(|| panic!("sharing must not generate IDs")),
    )
    .unwrap();
    let shared = coordinator.shared_signer();
    let retained = coordinator.shared_signer();
    assert!(Arc::ptr_eq(&shared, &retained));
    assert!(std::ptr::eq(coordinator.signer(), shared.as_ref()));
    assert_eq!(shared.public_key_record(), key);
    assert_eq!(shared.sign(&statement).unwrap(), signature);
    assert_eq!(coordinator.signer().sign(&statement).unwrap(), signature);
    drop(coordinator);
    assert!(Arc::ptr_eq(&shared, &retained));
    assert_eq!(retained.sign(&statement).unwrap(), signature);
    identity::verify(&statement, &retained.sign(&statement).unwrap(), &key).unwrap();
    let wrong = Identity::from_secret("endpoint-seed", &[0x22; 32]).unwrap();
    assert!(identity::verify(&statement, &signature, &wrong.public_key_record()).is_err());
}
