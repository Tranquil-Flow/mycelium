// SPDX-License-Identifier: AGPL-3.0-only
//! PH3b S2: a joined Mac's caps and its automatic work acceptance (`api::member_agent`), model-free.
//! v0.1.4 @ f4e366e9, reached from `DemoApp`: `_standing_loop` (app.py:1201-1212) ->
//! `auto_poll_step` (standing_consent.py:329-370) -> `DemoApp.auto_accept` (app.py:1175-1189:
//! no policy or no current binding -> nothing; `holds_offer` -> nothing; else
//! `StandingAcceptor.tick` with `limits_for = demo_target.member_limits`, demo_target.py:342-349)
//! -> `standing_limits(grant, required)` (standing_consent.py:230-261) refuses a share above the
//! caps by name before any report or preview. The member's agent checks the same share against
//! `StandingConsentGate.member_caps` (demo_target.py:74-78, `_share_refusal` :351-367).
use api::{
    local_agent::check_member_caps,
    member_agent::{MemberAcceptor, standing_member_caps},
    preferences::standing_consent::{self, AcceptFailure, WorkAcceptance},
};
use serde_json::{Value, json};
use std::{
    os::unix::fs::DirBuilderExt,
    path::PathBuf,
    sync::{Arc, Mutex},
};

const GIB: u64 = 1 << 30;
const STORAGE: u64 = 2_000_000_000;
const NOW: f64 = 1_000.0;

fn root(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "member-agent-{tag}-{}-{}",
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
/// A granted standing consent bound to a membership (`grant_standing_consent` at join, then
/// `bind_standing_consent` after `accept_pairing`, app.py:1758, :1803-1808); its binding.
fn granted(root: &PathBuf, wall: u64, memory: u64) -> Value {
    let body = json!({"acknowledged": true, "disclosure_digest": standing_consent::DISCLOSURE_DIGEST,
        "caps": {"storage_bytes": STORAGE, "max_memory_bytes": memory,
                 "max_wall_seconds": wall, "max_commands": 5000}});
    standing_consent::grant(root, &body, &policy(), NOW, Some(16 * GIB)).unwrap();
    let binding = json!({"swarm_id": "swarm-1", "node_id": "node-member",
        "seed_key_digest": format!("sha256:{}", "ab".repeat(32))});
    standing_consent::bind(root, &binding, NOW).unwrap();
    binding
}
/// The member's own collaborators, scripted: one pending offer; every call recorded.
#[derive(Default)]
struct Work {
    offer: Option<Value>,
    calls: Vec<Value>,
}
impl WorkAcceptance for Work {
    fn pending_offer(&mut self) -> Result<Option<Value>, AcceptFailure> {
        Ok(self.offer.clone())
    }
    fn report(&mut self, purpose: &str, offer: &Value) -> Result<(), AcceptFailure> {
        self.calls
            .push(json!(["report", purpose, offer["offer_id"]]));
        Ok(())
    }
    fn preview(
        &mut self,
        offer: &Value,
        limits: &Value,
        _policy: &Value,
    ) -> Result<Value, AcceptFailure> {
        self.calls
            .push(json!(["preview", offer["offer_id"], limits]));
        Err(AcceptFailure::code("member_work_unavailable"))
    }
    fn accept(
        &mut self,
        _preview: &Value,
        _limits: &Value,
        _policy: &Value,
    ) -> Result<(), AcceptFailure> {
        self.calls.push(json!(["accept"]));
        Ok(())
    }
    fn clock(&mut self) -> f64 {
        NOW
    }
}
fn offer(id: &str) -> Value {
    json!({"offer_id": id, "run_id": "run-1", "deployment_id": "dep-1",
        "target_node_id": "node-member", "archive_bytes": 1000, "expires_at": NOW + 600.0})
}
/// The owner's work announcement for this member: its share-derived worker limits.
fn work_announcement(wall: u64, rss: u64) -> Value {
    json!({"phase": "work", "run_id": "run-1", "deployment_id": "dep-1",
        "work_limits": {"max_rss_bytes": rss, "max_disk_bytes": 1_000_000,
                        "max_wall_seconds": wall, "max_commands": 5000}})
}

#[test]
fn a_joined_macs_caps_are_its_standing_caps_and_a_larger_share_is_refused_by_name() {
    let root = root("caps");
    granted(&root, 600, 4 * GIB);
    let caps = standing_member_caps(&root).unwrap();
    assert_eq!(
        caps,
        json!({"storage_bytes": STORAGE, "max_memory_bytes": 4 * GIB, "max_wall_seconds": 600})
    );
    let share = json!({"max_rss_bytes": 8 * GIB, "max_disk_bytes": 1_000_000});
    assert_eq!(
        check_member_caps(&share, &caps).unwrap(),
        Some("member_memory_cap_below_share")
    );
    std::fs::remove_dir_all(&root).unwrap_or_default();
}

#[test]
fn the_standing_acceptor_refuses_an_offer_above_the_members_caps_by_name() {
    let root = root("acceptor");
    let binding = granted(&root, 600, 4 * GIB);
    let announcement = Arc::new(Mutex::new(work_announcement(1800, GIB)));
    let mut acceptor = MemberAcceptor::new(root.clone(), announcement.clone());
    let mut work = Work {
        offer: Some(offer("offer-wall")),
        ..Work::default()
    };
    let policy = policy();
    // The share's wall (1800 s) is above the member's 600 s cap: refused before any report.
    let outcome = acceptor
        .tick(&mut work, Some(&policy), Some(&binding), false, &mut || {
            None
        })
        .unwrap();
    assert_eq!(
        outcome,
        Some(json!({"offer_id": "offer-wall", "state": "refused",
            "reason": "member_wall_cap_below_share"}))
    );
    // The share's memory (8 GiB) is above the member's 4 GiB cap.
    *announcement.lock().unwrap() = work_announcement(600, 8 * GIB);
    work.offer = Some(offer("offer-memory"));
    let outcome = acceptor
        .tick(&mut work, Some(&policy), Some(&binding), false, &mut || {
            None
        })
        .unwrap();
    assert_eq!(
        outcome,
        Some(json!({"offer_id": "offer-memory", "state": "refused",
            "reason": "member_memory_cap_below_share"}))
    );
    assert!(work.calls.is_empty(), "{:?}", work.calls);
    // An offer the agent holds (a bootstrap offer, a slice not verified yet) is left to it, and
    // nothing is accepted without a policy or a current membership binding.
    *announcement.lock().unwrap() = work_announcement(600, GIB);
    work.offer = Some(offer("offer-fits"));
    for (policy, binding, holds) in [
        (Some(&policy), Some(&binding), true),
        (None, Some(&binding), false),
        (Some(&policy), None, false),
    ] {
        let outcome = acceptor
            .tick(&mut work, policy, binding, holds, &mut || None)
            .unwrap();
        assert_eq!(outcome, None);
    }
    assert!(work.calls.is_empty(), "{:?}", work.calls);
    // A share within the caps is reported and previewed under exactly its own limits; with no
    // work service it fails by name and is not retried.
    let outcome = acceptor
        .tick(&mut work, Some(&policy), Some(&binding), false, &mut || {
            None
        })
        .unwrap();
    assert_eq!(
        outcome,
        Some(json!({"offer_id": "offer-fits", "state": "failed",
            "reason": "member_work_unavailable"}))
    );
    assert_eq!(
        work.calls,
        vec![
            json!(["report", "pre_run", "offer-fits"]),
            json!(["preview", "offer-fits", {"max_rss_bytes": GIB, "max_disk_bytes": 1_000_000,
                "max_wall_seconds": 600, "max_commands": 5000}]),
        ]
    );
    std::fs::remove_dir_all(&root).unwrap_or_default();
}
