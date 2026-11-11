// SPDX-License-Identifier: AGPL-3.0-only
//! Test gaps the PH3b S3 review left as surviving mutants (REVIEW-lanePH3B-S3 F6; lane S4c-a
//! item 6), on two packaged Macs in this one process (`support/packaged_macs.rs`), loopback only.
//! v0.1.4 @ f4e366e9, reached from `DemoApp` (app.py:505):
//! * `#demo-start` (run.js:192) -> `DemoApp.action` (app.py:1306) -> `_action` (:1362-1365) ->
//!   `demo_app.action` (demo_app.py:109-113): a Mac that is not the owner refuses
//!   `owner_role_required` (HTTP 400, app.py:2011) before anything else.
//! * GET /api/status -> `DemoApp.status` (app.py:1260, :1278) -> `owner_status`
//!   (demo_app.py:136-138): `owner_run` is `None` on a joined Mac (run.js:94 then shows no run card).
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use desktop::lifecycle::Poll;
use macs::{approve_and_join, join_body, member, owner, state_root};
use serde_json::{Value, json};

/// A joined Mac has no owner run in its status and its Start is refused `owner_role_required`;
/// the owner it joined shows its own (idle) run.
#[test]
fn a_joined_mac_shows_no_owner_run_and_cannot_start_one() {
    let base = state_root("joined-no-run");
    let owner = owner(&base);
    let member = member(&base, &owner);
    let asked = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(asked.0, 200, "join: {}", asked.1);
    approve_and_join(&owner, &member);
    let status = member.status();
    assert_eq!(status["installation_role"], "target", "{status}");
    assert_eq!(status["owner_run"], Value::Null, "{status}");
    let (code, refused) = member.post("/api/demo/start", json!({}));
    assert_eq!(
        (code, refused["error"].clone()),
        (400, json!("owner_role_required")),
        "{refused}"
    );
    let (code, refused) = member.post("/api/demo/stop", json!({}));
    assert_eq!(
        (code, refused["error"].clone()),
        (400, json!("owner_role_required")),
        "{refused}"
    );
    let shown = owner.mac.status();
    assert_eq!(shown["owner_run"]["state"], "idle", "{shown}");
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&base).unwrap_or_default();
}
