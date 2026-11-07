// SPDX-License-Identifier: AGPL-3.0-only
use device_harness::retention::*;
use std::collections::{BTreeMap, BTreeSet};
fn set(xs: &[&str]) -> BTreeSet<String> {
    xs.iter().map(|s| (*s).into()).collect()
}
fn graph() -> BTreeMap<String, Node> {
    let mut g = BTreeMap::new();
    for (name, edges, protected, active) in [
        ("offline", vec!["native", "npm", "model"], true, false),
        ("native", vec!["native-target"], false, false),
        ("native-target", vec![], false, false),
        ("npm", vec![], false, false),
        ("model", vec!["shard"], false, false),
        ("shard", vec![], false, false),
        ("rollback", vec!["old-artifact"], true, false),
        ("old-artifact", vec![], false, false),
        ("continuity", vec!["old-receipt"], true, false),
        ("old-receipt", vec![], false, false),
        ("active", vec!["worktree"], false, true),
        ("worktree", vec![], false, false),
        ("discardable", vec![], false, false),
    ] {
        g.insert(
            name.into(),
            Node {
                digest: "a".repeat(64),
                dependencies: set(&edges),
                protected,
                active_lease: active,
                restore_verified: true,
            },
        );
    }
    g
}
fn offline(g: &BTreeMap<String, Node>) -> bool {
    [
        "offline",
        "native",
        "native-target",
        "npm",
        "model",
        "shard",
    ]
    .iter()
    .all(|k| g.contains_key(*k))
}
#[test]
fn cleanup_plan_preserves_native_npm_model_rollback_continuity_and_active_closures() {
    let g = graph();
    assert!(offline(&g));
    let p = plan(&g, &set(&["offline"]), &set(&["discardable"])).unwrap();
    assert!(!p.deletion_authorized);
    let mut fixture = g.clone();
    for k in &p.archive_candidates {
        fixture.remove(k);
    }
    assert!(offline(&fixture));
    assert_eq!(p.keep, fixture.keys().cloned().collect());
    for k in g.keys().filter(|k| k.as_str() != "discardable") {
        assert!(
            plan(&g, &set(&["offline"]), &set(&[k])).is_err(),
            "protected closure {k}"
        );
    }
}
#[test]
fn dangling_reference_unknown_root_missing_restore_and_same_digest_alias_refuse() {
    let mut g = graph();
    g.get_mut("discardable")
        .unwrap()
        .dependencies
        .insert("absent".into());
    assert!(plan(&g, &set(&["offline"]), &set(&["discardable"])).is_err());
    let mut g = graph();
    g.get_mut("discardable").unwrap().restore_verified = false;
    assert!(plan(&g, &set(&["offline"]), &set(&["discardable"])).is_err());
    assert!(plan(&graph(), &set(&["absent"]), &set(&[])).is_err());
    assert!(plan(&graph(), &set(&["offline"]), &set(&["absent"])).is_err());
    // Explicit resolved aliases are graph edges; identical bytes alone do not imply inode identity.
    let mut g = graph();
    g.get_mut("native")
        .unwrap()
        .dependencies
        .insert("discardable".into());
    assert!(plan(&g, &set(&["offline"]), &set(&["discardable"])).is_err());
}
#[test]
fn cyclic_dependencies_terminate_without_losing_protected_reachability() {
    let mut g = graph();
    g.get_mut("native-target")
        .unwrap()
        .dependencies
        .insert("native".into());
    let p = plan(&g, &set(&["offline"]), &set(&["discardable"])).unwrap();
    assert!(p.keep.contains("native-target"));
    assert!(p.keep.contains("native"));
    assert!(plan(&g, &set(&["offline"]), &set(&["native-target"])).is_err());
}
