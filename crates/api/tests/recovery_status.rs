// SPDX-License-Identifier: AGPL-3.0-only
//! B10 (owner ruling 16 Nov (3 a), ledger rows 74 c, 93): `status.recovery_error` as v0.1.4 keeps
//! it (`DemoApp.recovery_error`, app.py:557, :617-625, :1273, :1434, :1636 @ f4e366e9). A joined
//! Mac whose start finds a dead relay or a lapsed membership stays started but not running and
//! records the code; the status carries it right after `renewal_error` (the page's joined-Mac relay
//! line reads `value.recovery_error || value.renewal_error`, `assets/app.js`). Before this, v0.2's
//! status had no `recovery_error` key at all (`contracts/status-example.json:16` has it).
//!
//! Model-free status part only: the recovery CARD (`/api/network/recovery-card` on the owner,
//! `/api/network/reconnect` on the joined Mac, `endpoint_recovery.py`) needs the relay and the
//! packaged network action root, which lane C may not edit tonight (REPORT-laneC-release.md, B10).
use api::{
    app_host::{Authority, Unmanaged},
    app_status, joined_member,
    windows::PendingWindows,
};
use std::{fs, path::PathBuf};

struct Root(PathBuf);
impl Root {
    fn new(tag: &str) -> Self {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir()
            .canonicalize()
            .unwrap()
            .join(format!("b10-{tag}-{}-{stamp}", std::process::id()));
        fs::create_dir(&root).unwrap();
        Self(root)
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap_or_default();
    }
}

#[test]
fn the_status_carries_recovery_error_right_after_renewal_error() {
    let root = Root::new("status");
    let mut authority = Authority::<Unmanaged>::load(root.0.clone()).unwrap();
    let pending = PendingWindows::default();
    let facts = app_status::Facts::default();
    let keys = |a: &Authority<Unmanaged>| -> Vec<&'static str> {
        app_status::shared(a, &pending, &facts)
            .into_iter()
            .map(|(k, _)| k)
            .collect()
    };
    let order = keys(&authority);
    let renewal = order.iter().position(|k| *k == "renewal_error").unwrap();
    assert_eq!(
        order.get(renewal + 1),
        Some(&"recovery_error"),
        "v0.1.4's key order (app.py:1272-1273): {order:?}"
    );
    let value = |a: &Authority<Unmanaged>| {
        app_status::shared(a, &pending, &facts)
            .into_iter()
            .find(|(k, _)| *k == "recovery_error")
            .map(|(_, v)| v.to_value())
            .unwrap()
    };
    assert_eq!(value(&authority), serde_json::Value::Null);
    authority.recorded.recovery_error = Some("peer_transport_unreachable");
    assert_eq!(value(&authority), "peer_transport_unreachable");
}

#[test]
fn a_dead_relay_or_a_lapsed_membership_is_a_recovery_error_anything_else_refuses_the_start() {
    for code in [
        "peer_transport_unreachable",
        "seed_http_remote_error",
        "peer_member_expired",
        "peer_generation_stale",
        "peer_member_unknown",
    ] {
        assert_eq!(joined_member::recovery_code(code), Some(code));
    }
    for code in ["peer_member_revoked", "seed_identity_mismatch", "", "x"] {
        assert_eq!(joined_member::recovery_code(code), None, "{code}");
    }
}
