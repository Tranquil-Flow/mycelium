// SPDX-License-Identifier: AGPL-3.0-only
//! PH3b S3c item 5 (REVIEW-lanePH3B-S2 F1): a joined Mac's standing acceptor never lets a share
//! through when its own persisted standing caps are not plain integers. v0.1.4 @ f4e366e9,
//! reached from `DemoApp._standing_loop` (app.py:1201-1212) -> `auto_poll_step`
//! (standing_consent.py:329-370) -> `DemoApp.auto_accept` (app.py:1175-1189) ->
//! `StandingAcceptor.tick` (standing_consent.py:400-470) -> `standing_limits(grant, required=)`
//! (:230-261): `wall > caps['max_wall_seconds']` and `need_rss > rss` compare a float cap
//! numerically (600.0 refuses a 3600 s share: `member_wall_cap_below_share`) and RAISE on a string
//! cap (`TypeError`, outside `except StandingConsentError`): the tick fails, nothing is reported,
//! previewed or accepted, and the offer is not marked handled. Unknown or malformed caps fail
//! CLOSED here too.
use api::preferences::{
    private_state,
    standing_consent::{self, AcceptFailure, StandingAcceptor, WorkAcceptance},
};
use serde_json::{Value, json};
use std::{
    os::unix::fs::DirBuilderExt,
    path::{Path, PathBuf},
};

const GIB: u64 = 1 << 30;
const STORAGE: u64 = 2_000_000_000;
const NOW: f64 = 1_000.0;

fn root(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "standing-caps-{tag}-{}-{}",
        std::process::id(),
        api::app_host::hex(&api::app_host::random(8).unwrap())
    ));
    std::fs::DirBuilder::new().mode(0o700).create(&dir).unwrap();
    dir
}
fn policy() -> Value {
    json!({"peer_visibility": true, "power": "ac_only", "storage_bytes": STORAGE,
        "network": "unmetered_only", "background": false, "diagnostics": false,
        "thermal": "stop_on_serious"})
}
/// A granted, bound standing consent (600 s, 4 GiB), then one persisted cap rewritten in place as
/// the reviewer's probe did (the file stays a valid grant document: `load` checks keys, not types).
fn granted_with(root: &Path, cap: &str, value: Value) -> Value {
    let body = json!({"acknowledged": true, "disclosure_digest": standing_consent::DISCLOSURE_DIGEST,
        "caps": {"storage_bytes": STORAGE, "max_memory_bytes": 4 * GIB,
                 "max_wall_seconds": 600, "max_commands": 5000}});
    standing_consent::grant(root, &body, &policy(), NOW, Some(16 * GIB)).unwrap();
    let binding = json!({"swarm_id": "swarm-1", "node_id": "node-member",
        "seed_key_digest": format!("sha256:{}", "ab".repeat(32))});
    standing_consent::bind(root, &binding, NOW).unwrap();
    let path = root.join(standing_consent::FILE);
    let limit = private_state::private_json_limit().unwrap();
    let mut document = private_state::load(&path, limit).unwrap().unwrap();
    document["caps"][cap] = value;
    private_state::store(&path, &Value::Object(document), limit).unwrap();
    binding
}
/// An accept-capable member: preview and accept succeed, every call recorded.
#[derive(Default)]
struct Member {
    calls: Vec<Value>,
}
impl WorkAcceptance for Member {
    fn pending_offer(&mut self) -> Result<Option<Value>, AcceptFailure> {
        Ok(Some(json!({"offer_id": "offer-1", "run_id": "run-1", "deployment_id": "dep-1",
            "target_node_id": "node-member", "archive_bytes": 1000, "expires_at": NOW + 600.0})))
    }
    fn report(&mut self, purpose: &str, offer: &Value) -> Result<(), AcceptFailure> {
        self.calls
            .push(json!(["report", purpose, offer["offer_id"]]));
        Ok(())
    }
    fn preview(
        &mut self,
        _offer: &Value,
        limits: &Value,
        policy: &Value,
    ) -> Result<Value, AcceptFailure> {
        self.calls.push(json!(["preview", limits]));
        Ok(json!({"protocol": "mycelium.a13.work_preview.v1", "route_ready": false,
            "limits": limits, "consent_digest": format!("sha256:{}", "66".repeat(32)),
            "node_id": "node-member", "resource_policy": policy}))
    }
    fn accept(
        &mut self,
        _preview: &Value,
        limits: &Value,
        _policy: &Value,
    ) -> Result<(), AcceptFailure> {
        self.calls.push(json!(["accept", limits]));
        Ok(())
    }
    fn clock(&mut self) -> f64 {
        NOW
    }
}

/// One tick of the acceptor over a grant whose `cap` is `value`, for a share of `share`.
fn tick(
    tag: &str,
    cap: &str,
    value: Value,
    share: Value,
) -> (Result<Option<Value>, AcceptFailure>, Vec<Value>) {
    let root = root(tag);
    let binding = granted_with(&root, cap, value);
    let mut acceptor = StandingAcceptor::new(
        root.clone(),
        Some(Box::new(move |_offer: &Value| Some(share.clone()))),
    );
    let mut member = Member::default();
    let outcome = acceptor.tick(&mut member, &policy(), Some(&binding), &mut || None);
    std::fs::remove_dir_all(&root).unwrap_or_default();
    (outcome, member.calls)
}
fn wall_share() -> Value {
    json!({"max_rss_bytes": GIB, "max_disk_bytes": 1_000_000, "max_wall_seconds": 3600,
        "max_commands": 5000})
}
fn memory_share() -> Value {
    json!({"max_rss_bytes": 64 * GIB, "max_disk_bytes": 1_000_000, "max_wall_seconds": 600,
        "max_commands": 5000})
}

#[test]
fn a_float_cap_compares_numerically_and_refuses_the_larger_share_by_name() {
    // `3600 > 600.0` and `68719476736 > 4294967296.0` in v0.1.4.
    for (tag, cap, value, share, reason) in [
        (
            "wall-float",
            "max_wall_seconds",
            json!(600.0),
            wall_share(),
            "member_wall_cap_below_share",
        ),
        (
            "memory-float",
            "max_memory_bytes",
            json!(4_294_967_296.0),
            memory_share(),
            "member_memory_cap_below_share",
        ),
    ] {
        let (outcome, calls) = tick(tag, cap, value, share);
        assert_eq!(
            outcome.expect("the tick runs"),
            Some(json!({"offer_id": "offer-1", "state": "refused", "reason": reason})),
            "{tag}"
        );
        assert_eq!(calls, Vec::<Value>::new(), "{tag}: no report, preview or accept");
    }
}

#[test]
fn a_string_cap_fails_the_tick_closed_and_accepts_nothing() {
    // `3600 > "600"` / `68719476736 > "4294967296"` raise TypeError in v0.1.4: the pass fails.
    for (tag, cap, value, share) in [
        ("wall-string", "max_wall_seconds", json!("600"), wall_share()),
        (
            "memory-string",
            "max_memory_bytes",
            json!("4294967296"),
            memory_share(),
        ),
    ] {
        let (outcome, calls) = tick(tag, cap, value, share);
        assert!(
            outcome.is_err(),
            "{tag}: a malformed cap must fail the tick, got {outcome:?} after {calls:?}"
        );
        assert_eq!(calls, Vec::<Value>::new(), "{tag}: no report, preview or accept");
    }
}
