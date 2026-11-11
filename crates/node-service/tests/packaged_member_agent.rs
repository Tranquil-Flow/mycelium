// SPDX-License-Identifier: AGPL-3.0-only
//! PH3b S2: a joined packaged Mac's standing loop and its member card, in process and model-free
//! (two packaged Macs, `support/packaged_macs.rs`). v0.1.4 @ f4e366e9, reached from `DemoApp`:
//!
//! * `DemoApp.start` starts `standing_thread` (app.py:642): `_standing_loop` (app.py:1201-1212)
//!   every 2 s on a running target -> `_standing_tick` -> `auto_poll_step` (standing_consent.py:
//!   329-370: target, granted grant, running, binding) -> `auto_steps` = `demo_app.auto_step`
//!   (demo_app.py:168-173) -> `DemoTargetAgent.tick` (demo_target.py:389-405) over
//!   `RunExchangeClient(app.authority.peer)` (demo_app.py:155-165): it reads the owner's signed
//!   announcement and sends its own signed report.
//! * `DemoApp.status()['demo_target']` (app.py:1279) = `demo_app.target_status` (demo_app.py:181-197),
//!   rendered by the unchanged run.js:152-190.
//! * Leave (`member_leave.app_steps`, `_left`): the loop no longer answers for this Mac; Quit
//!   (`demo_app.close` -> `agent.close()`, demo_app.py:205-207) ends it with the other loops.
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use budgets::BudgetId as B;
use desktop::lifecycle::Poll;
use macs::{Mac, approve_and_join, join_body, member, millis, owner, private, state_root};
use serde_json::{Value, json};

/// `_standing_loop`'s period (`self.closed.wait(2)`, app.py:1203).
const STANDING: B = B::PyMyceliumA13MacosAppDemoappStandingLoopSelfClosedWaitL1203;

/// The member's node id: the owner's member row that is not its own (`owner-swarm.json`).
fn member_node(owner: &Mac) -> String {
    let own: Value = serde_json::from_slice(
        &std::fs::read(private(&owner.state).join("owner-swarm.json")).unwrap(),
    )
    .unwrap();
    let status = owner.until(
        "the owner sees two members",
        |o| o.status(),
        |v| v["authority"]["member_count"] == 2,
    );
    status["authority"]["members"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["node_id"].as_str().unwrap().to_owned())
        .find(|n| own["node_id"] != n.as_str())
        .unwrap()
}
/// How many signed run-exchange calls of `node` the owner's seed has observed (its member row's
/// `clock` view, `clock_offset.py`; absent before the first).
fn exchange_samples(owner: &Mac, node: &str) -> u64 {
    let status = owner.status();
    status["authority"]["members"]
        .as_array()
        .unwrap()
        .iter()
        .find(|m| m["node_id"] == node)
        .and_then(|m| m["clock"]["samples"].as_u64())
        .unwrap_or(0)
}

#[test]
fn a_joined_packaged_mac_answers_the_owner_exchange_and_shows_its_member_card() {
    let base = state_root("member-agent");
    let owner = owner(&base);
    let member = member(&base, &owner);
    // Before joining this Mac is not a target: no member card (`target_status` answers None).
    assert!(member.status()["demo_target"].is_null());
    let waiting = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    approve_and_join(&owner, &member);
    let node = member_node(&owner.mac);
    // The member's standing loop read the owner's signed announcement (`phase: idle`, as the
    // owner's exchange announces with no run) and the card shows it: joined, idle, not local.
    let card = member.until(
        "the member card shows the owner's announcement",
        |m| m.status(),
        |v| v["demo_target"]["phase"] == "idle",
    );
    let target = &card["demo_target"];
    assert_eq!(target["state"], "joined", "{target}");
    assert_eq!(target["local"], false, "{target}");
    assert_eq!(target["run_id"], Value::Null, "{target}");
    // The owner's seed observed the member's own signed exchange calls (read + report).
    let answered = owner.mac.until(
        "the owner observed the member's signed exchange calls",
        |o| json!(exchange_samples(o, &node)),
        |v| v.as_u64().is_some_and(|n| n >= 2),
    );
    assert!(answered.as_u64().unwrap() >= 2);
    // Leave: `_left`, the loop no longer answers for this Mac.
    let left = member.post("/api/leave", json!({"mode": "instant", "confirm": true}));
    assert_eq!(left.0, 200, "leave: {}", left.1);
    assert_eq!(left.1["state"], "left", "{}", left.1);
    // One pass in flight at the leave may still land; after that nothing does.
    std::thread::sleep(millis(STANDING) * 2);
    let settled = exchange_samples(&owner.mac, &node);
    std::thread::sleep(millis(STANDING) * 3);
    assert_eq!(
        exchange_samples(&owner.mac, &node),
        settled,
        "a member that left still answers the owner's exchange"
    );
    // Both Macs Quit clean: the standing loop ends with the host's other loops.
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&base).unwrap_or_default();
}

/// Withdrawing standing consent (`/api/consent/standing/withdraw`, app.py:1730-1737) stops the
/// standing loop's work for this Mac: `auto_poll_step` returns `standing_consent_withdrawn`
/// before `auto_accept` and the agent (standing_consent.py:351-352), so the member no longer
/// answers the owner's exchange, its card keeps its last state (the shipped loop never ticks
/// the agent again: `waiting_for_consent` is not reached), and the membership stays.
#[test]
fn a_withdrawn_standing_consent_stops_the_members_answers_and_keeps_its_membership() {
    let base = state_root("member-withdraw");
    let owner = owner(&base);
    let member = member(&base, &owner);
    let waiting = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    approve_and_join(&owner, &member);
    let node = member_node(&owner.mac);
    owner.mac.until(
        "the owner observed the member's signed exchange calls",
        |o| json!(exchange_samples(o, &node)),
        |v| v.as_u64().is_some_and(|n| n >= 2),
    );
    let withdrawn = member.post("/api/consent/standing/withdraw", json!({"confirm": true}));
    assert_eq!(withdrawn.0, 200, "withdraw: {}", withdrawn.1);
    assert_eq!(withdrawn.1["state"], "withdrawn", "{}", withdrawn.1);
    std::thread::sleep(millis(STANDING) * 2);
    let settled = exchange_samples(&owner.mac, &node);
    std::thread::sleep(millis(STANDING) * 3);
    assert_eq!(
        exchange_samples(&owner.mac, &node),
        settled,
        "a member whose standing consent is withdrawn still answers the owner's exchange"
    );
    let status = member.status();
    assert_eq!(status["demo_target"]["state"], "joined", "{status}");
    assert_eq!(
        status["join_flow"]["standing_consent"]["state"], "withdrawn",
        "{status}"
    );
    assert_eq!(
        status["authority"]["membership_state"], "current",
        "{status}"
    );
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&base).unwrap_or_default();
}
