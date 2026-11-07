// SPDX-License-Identifier: AGPL-3.0-only
// O:contract:mycelium.deployment_activation.v1; F3
use control_runtime::Store;
use serde_json::Value;
#[test]
fn activation_publication_validates_l5_identity_and_fences_restart_writers() {
    let root = std::env::temp_dir().join(format!("v02-l6-activation-{}", std::process::id()));
    std::fs::create_dir(&root).unwrap();
    let mut store = Store::open(&root.join("state.db")).unwrap();
    let value: Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/contracts/deployment-activation-v1.json"
    ))
    .unwrap();
    let revision = store.publish_activation(&value, None).unwrap();
    let mut bad = value.clone();
    bad["protocol"] = "wrong".into();
    assert!(store.publish_activation(&bad, Some(revision)).is_err());
    assert_eq!(
        store.load_document("activation").unwrap().unwrap().0,
        revision
    );
    let mut next = value.clone();
    next["generation"] = serde_json::json!(value["generation"].as_u64().unwrap() + 1);
    let mut restarted = Store::open(&root.join("state.db")).unwrap();
    let new_revision = restarted.publish_activation(&next, Some(revision)).unwrap();
    let mut future = next.clone();
    future["generation"] = serde_json::json!(next["generation"].as_u64().unwrap() + 1);
    assert!(
        store.publish_activation(&future, Some(revision)).is_err(),
        "a newer wire generation cannot authorize a stale durable writer"
    );
    assert!(store.publish_activation(&value, Some(revision)).is_err());
    assert!(
        store
            .publish_activation(&value, Some(new_revision))
            .is_err()
    );
    assert_eq!(
        restarted.load_document("activation").unwrap().unwrap().1,
        protocol::canonical::to_bytes(&next).unwrap()
    );
    drop(store);
    drop(restarted);
    std::fs::remove_dir_all(root).unwrap();
}
