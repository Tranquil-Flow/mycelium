// SPDX-License-Identifier: AGPL-3.0-only
//! A packaged Mac joins a packaged owner's swarm (lane PH3a): v0.1.4's join flow through the routes
//! and actions its page uses (`/api/join/window`, `/api/join/window/poll`, app.py:1742-1813 @
//! f4e366e9) with the join-consent document the page shows (`join_flow.standing_disclosure`,
//! app.py:1214-1237), the role the installation really has (`installation_role`, app.py:595-611,
//! 1106-1116), the joined Mac's own membership, its lease renewal and its leave.
//!
//! Two packaged hosts run in this one test process over two private state roots
//! (`support/packaged_macs.rs`).
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use desktop::lifecycle::Poll;
use macs::{
    Mac, STORAGE, approve_and_join, consent, join_body, member, owner, private, state_root,
};
use serde_json::{Value, json};

/// The member's window shows v0.1.4's join-consent document before anything is chosen; a grant
/// whose caps the consent does not allow is refused by name before any role change; the join
/// with consent waits for the owner, who sees the claim and approves it; the member is then a
/// target with its own current membership, and the owner counts two members.
#[test]
fn packaged_member_joins_with_consent_through_the_owners_window() {
    let base = state_root("join");
    let owner = owner(&base);
    let member = member(&base, &owner);
    let before = member.status();
    assert_eq!(before["installation_role"], "owner", "{before}");
    let disclosure = &before["join_flow"]["standing_disclosure"];
    assert_eq!(
        disclosure["digest"],
        api::preferences::standing_consent::DISCLOSURE_DIGEST,
        "{before}"
    );
    assert_eq!(
        disclosure["text"],
        api::preferences::standing_consent::DISCLOSURE_TEXT,
        "{before}"
    );
    let refused = member.post("/api/join/window", join_body(&member, &owner.link, 30));
    assert_eq!(
        (refused.0, refused.1["error"].clone()),
        (400, json!("standing_consent_caps_invalid"))
    );
    assert_eq!(member.status()["installation_role"], "owner");
    let waiting = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    assert_eq!(waiting.1["state"], "waiting_for_owner", "{}", waiting.1);
    assert_eq!(waiting.1["pinned"], true, "{}", waiting.1);
    let pending = member.status();
    assert_eq!(pending["installation_role"], "target", "{pending}");
    assert_eq!(
        pending["join_flow"]["join_window"]["state"], "waiting_for_owner",
        "{pending}"
    );
    let joined = approve_and_join(&owner, &member);
    assert_eq!(joined["membership_state"], "current", "{joined}");
    assert_eq!(joined["route_ready"], false, "{joined}");
    assert_eq!(joined["report"]["state"], "delivered", "{joined}");
    let after = member.status();
    assert_eq!(after["installation_role"], "target", "{after}");
    assert_eq!(after["authority"]["membership_state"], "current", "{after}");
    assert_eq!(
        after["resource_policy"]["storage_bytes"], STORAGE,
        "{after}"
    );
    assert_eq!(
        after["join_flow"]["standing_consent"]["state"], "granted",
        "{after}"
    );
    let seen = owner.mac.until(
        "owner sees the member",
        |o| o.status(),
        |v| v["authority"]["member_count"] == 2,
    );
    assert_eq!(seen["authority"]["membership_state"], "current", "{seen}");
    // Joined: a second join is `already_joined` (app.py:1747-1748).
    let again = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(
        (again.0, again.1["error"].clone()),
        (400, json!("already_joined"))
    );
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&base).unwrap_or_default();
}

/// v0.1.4's join refusals on the packaged page, each by name: an owner that created a swarm
/// cannot become a target (app.py:1745-1746, also `/api/join/request`), a body without its
/// consent is `consent_required` (:1743-1744), a grant without its three keys is
/// `standing_consent_required` (:1753-1754), a poll with no pending join is `join_window_missing`
/// (:1782), and a retry with another consent is `consent_retry_changed` (:1102-1103).
#[test]
fn packaged_join_refusals_are_v014s() {
    let base = state_root("refusals");
    let owner = owner(&base);
    let refused = |mac: &Mac, path: &str, body: Value| {
        let (status, answer) = mac.post(path, body);
        (
            status,
            answer["error"].as_str().unwrap_or("<answered>").to_owned(),
        )
    };
    let link = owner.link.clone();
    let body = join_body(&owner.mac, &link, 600);
    assert_eq!(
        refused(&owner.mac, "/api/join/window", body.clone()),
        (400, "owner_cannot_become_target".into())
    );
    assert_eq!(
        refused(
            &owner.mac,
            "/api/join/request",
            json!({"consent": consent(STORAGE)})
        ),
        (400, "owner_cannot_become_target".into())
    );
    let member = member(&base, &owner);
    assert_eq!(
        refused(&member, "/api/join/window", json!({"join": link})),
        (400, "consent_required".into())
    );
    let mut partial = join_body(&member, &link, 600);
    partial["standing_consent"] = json!({"acknowledged": true});
    assert_eq!(
        refused(&member, "/api/join/window", partial),
        (400, "standing_consent_required".into())
    );
    assert_eq!(
        refused(&member, "/api/join/window/poll", json!({})),
        (400, "join_window_missing".into())
    );
    // A join that reached the role change keeps its consent: another one is refused.
    let waiting = member.post("/api/join/window", join_body(&member, &link, 600));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    let mut changed = join_body(&member, &link, 600);
    changed["consent"]["storage_bytes"] = json!(STORAGE + 1_000_000_000);
    changed["standing_consent"]["caps"]["storage_bytes"] = json!(STORAGE + 1_000_000_000);
    assert_eq!(
        refused(&member, "/api/join/window", changed),
        (400, "consent_retry_changed".into())
    );
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&base).unwrap_or_default();
}

/// A joined Mac holds no seed authority, whatever its page asks: `/api/create` on a target is
/// refused `owner_role_required` before anything runs (`DemoApp._action`, app.py:1346-1348 @
/// f4e366e9), and its root never gets a seed key or seed state (`seed-identity.json`, `seed-rs/`),
/// after the join or after the attempt (REPORT-laneS1R: a target root must never create them).
#[test]
fn packaged_member_root_never_holds_seed_authority() {
    let base = state_root("no-seed");
    let owner = owner(&base);
    let member = member(&base, &owner);
    let waiting = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    let joined = approve_and_join(&owner, &member);
    assert_eq!(joined["membership_state"], "current", "{joined}");
    let seed_authority = |mac: &Mac| {
        ["seed-rs", "seed-identity.json"]
            .into_iter()
            .filter(|path| private(&mac.state).join(path).exists())
            .collect::<Vec<_>>()
    };
    assert_eq!(seed_authority(&member), Vec::<&str>::new());
    let created = member.post("/api/create", json!({"consent": consent(STORAGE)}));
    assert_eq!(
        (created.0, created.1["error"].clone()),
        (400, json!("owner_role_required")),
        "{}",
        created.1
    );
    assert_eq!(seed_authority(&member), Vec::<&str>::new());
    assert_eq!(member.status()["installation_role"], "target");
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&base).unwrap_or_default();
}
