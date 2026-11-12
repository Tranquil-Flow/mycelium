// SPDX-License-Identifier: AGPL-3.0-only
//! The page's top-level `running` is the APPLICATION's own running flag, as the shipped app
//! publishes it (lane PH3b S4c-a item 1; REVIEW-lanePH3B-S1 finding 2). v0.1.4 @ f4e366e9:
//! `DemoApp` (app.py:505) -> GET /api/status -> `DemoApp.status` (app.py:1244-1283) ->
//! `'running': self.running` (:1272). `self.running` is False at construction (:546), True once
//! `self.authority.start()` returned at launch (:614-615; False when a target's membership is lost,
//! :626), False / True around `_become_target` (:1107-1119), False at `/api/stop` (:1630), True at
//! `/api/start` (:1634-1635) and at the join (:1808).
//!
//! The page reads it in app.js only (crates/desktop/assets, byte-identical to v0.1.4 web/):
//! `Application` rung (:209 Started / Stopped), Check / accept cleanup (:181-182), Start control
//! (:185), Check / accept stage (:187-188), Report this Mac capability (:207), Download / resume
//! source (:231, :235). Before this fix the packaged status carried the node service's native-request
//! flag there, so an idle packaged window read "Stopped" and disabled all of them.
//!
//! The same document's `role` is v0.1.4's constant `not_yet_serving` (app.py:1263), shown as the
//! serving line (`#serving-state`, app.js:227); the packaged page showed the node service's
//! `development_owner`.
//!
//! Two packaged hosts in this one process (`support/packaged_macs.rs`), loopback only.
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use desktop::lifecycle::Poll;
use macs::{approve_and_join, join_body, member, owner, state_root};
use serde_json::json;

/// An idle owner (relay, swarm, open code; no chat, no run) is running; so is the Mac that joined
/// it. `/api/stop` stops the joined Mac (`running` false, the rung reads Stopped) and `/api/start`
/// brings it back (`running` true), as the shipped app.
#[test]
fn the_page_reads_the_applications_running_flag_on_both_macs() {
    let base = state_root("running");
    let owner = owner(&base);
    let shown = owner.mac.status();
    // `'role':'not_yet_serving'` (app.py:1263), shown by `#serving-state` (app.js:227).
    assert_eq!(shown["role"], "not_yet_serving", "{shown}");
    assert_eq!(
        shown["running"], true,
        "an idle owner app is running: {shown}"
    );
    let member = member(&base, &owner);
    let fresh = member.status();
    assert_eq!(
        fresh["running"], true,
        "a fresh install (owner role, authority started) is running: {fresh}"
    );
    let asked = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(asked.0, 200, "join: {}", asked.1);
    let joined = approve_and_join(&owner, &member);
    assert_eq!(joined["state"], "joined", "{joined}");
    let after = member.status();
    assert_eq!(after["running"], true, "a joined Mac is running: {after}");
    assert_eq!(after["authority"]["membership_state"], "current", "{after}");

    let stopped = member.post("/api/stop", json!({}));
    assert_eq!(stopped.0, 200, "stop: {}", stopped.1);
    let paused = member.status();
    assert_eq!(paused["running"], false, "after /api/stop: {paused}");
    let started = member.post("/api/start", json!({}));
    assert_eq!(started.0, 200, "start: {}", started.1);
    let again = member.status();
    assert_eq!(again["running"], true, "after /api/start: {again}");
    // S4c-a item 2: `/api/start` of a target with a membership binding reports to the owner
    // ("Reconnect can be clicked while this member is still running. The owner's in-memory report
    // inbox may have restarted alone", app.py:1641-1643 -> `_auto_report('periodic')`), recorded as
    // `join_flow.last_report` (:1161, :1236). The join's own report came first.
    assert_eq!(
        after["join_flow"]["last_report"]["purpose"], "join",
        "{after}"
    );
    let report = &again["join_flow"]["last_report"];
    assert_eq!(report["purpose"], "periodic", "after /api/start: {again}");
    assert_eq!(report["state"], "delivered", "{report}");

    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&base).unwrap_or_default();
}
