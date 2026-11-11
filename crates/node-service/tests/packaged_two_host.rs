// SPDX-License-Identifier: AGPL-3.0-only
//! PH3a's end-to-end proof, in process and model-free: two packaged Macs (owner and member, two
//! private state roots, `support/packaged_macs.rs`) run v0.1.4's two-Mac journey up to the point
//! where work would start (app.py @ f4e366e9):
//!
//! 1. the owner opens its network window (`/api/network/start`, external provider on a loopback
//!    proxy: `_start_public_relay`, app.py:676-740), creates the swarm and opens a join code;
//! 2. the member's page shows the join-consent document, the member joins with consent through
//!    `/api/join/window` + `/api/join/window/poll` (app.py:1742-1813), the owner approves the claim;
//! 3. both statuses show the membership (owner: two members; member: `target`, its own `current`
//!    membership); the member root holds no seed authority (`seed-rs/`, `seed-identity.json`);
//! 4. the member renews its own lease (`_renew_loop`, app.py:911-927) on the composition's
//!    renewal-period seam;
//! 5. the member leaves (`/api/leave`, member_leave.py:265-292): `left`, the role stays `target`,
//!    and the renewal stops (`_left`, member_leave.py:237-238);
//! 6. the owner revokes its window, both Macs Quit clean and the owner's relay is closed
//!    (`DemoApp.close`, app.py:1848-1850).
//!
//! This is what the one-Mac rehearsal R3 later runs with the real bundle.
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use budgets::BudgetId as B;
use desktop::lifecycle::Poll;
use macs::{
    approve_and_join, join_body, leases, member_renewing, millis, owner, private, state_root,
};
use serde_json::json;
use std::{net::TcpStream, time::Instant};

/// The member's renewal runs on this budget instead of v0.1.4's 20 s.
const RENEWAL: B = B::OwnerPoll;

#[test]
fn two_packaged_macs_join_renew_leave_and_quit() {
    let base = state_root("two-host");
    // 1. The owner: network window, swarm, one join code on the relay origin.
    let owner = owner(&base);
    let member = member_renewing(&base, &owner, RENEWAL);
    // 2. The join-consent document first, then the join with consent and the owner's approval.
    let shown = member.status();
    assert_eq!(shown["installation_role"], "owner", "{shown}");
    assert_eq!(
        shown["join_flow"]["standing_disclosure"]["digest"],
        api::preferences::standing_consent::DISCLOSURE_DIGEST,
        "{shown}"
    );
    let waiting = member.post("/api/join/window", join_body(&member, &owner.link, 600));
    assert_eq!(waiting.0, 200, "join: {}", waiting.1);
    let joined = approve_and_join(&owner, &member);
    assert_eq!(joined["membership_state"], "current", "{joined}");
    // 3. Both statuses show the membership.
    let mine = member.status();
    assert_eq!(mine["installation_role"], "target", "{mine}");
    assert_eq!(mine["authority"]["membership_state"], "current", "{mine}");
    assert_eq!(
        mine["join_flow"]["standing_consent"]["state"], "granted",
        "{mine}"
    );
    let seen = owner.mac.until(
        "the owner sees two members",
        |o| o.status(),
        |v| v["authority"]["member_count"] == 2,
    );
    let owner_node = seen["authority"]["members"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["node_id"].as_str().unwrap().to_owned())
        .collect::<Vec<_>>();
    let rows = leases(&owner.mac);
    assert_eq!(rows.len(), 2, "{rows:?}");
    // The member's row is the one the owner did not create for itself (`owner-swarm.json`).
    let own: serde_json::Value = serde_json::from_slice(
        &std::fs::read(private(&owner.mac.state).join("owner-swarm.json")).unwrap(),
    )
    .unwrap();
    let node = owner_node
        .into_iter()
        .find(|n| own["node_id"] != n.as_str())
        .unwrap();
    for path in ["seed-rs", "seed-identity.json"] {
        assert!(
            !private(&member.state).join(path).exists(),
            "a member root holds {path}"
        );
    }
    // 4. The member renews its own lease.
    let lease = |rows: &[(String, f64)]| rows.iter().find(|(n, _)| *n == node).unwrap().1;
    let first = lease(&rows);
    let deadline = Instant::now() + millis(B::WorkerStartup);
    loop {
        let now = lease(&leases(&owner.mac));
        if now > first {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "the member's lease was not renewed: {first} -> {now}"
        );
        std::thread::sleep(millis(RENEWAL));
    }
    // 5. The member leaves: `left`, still a target, and its renewal stops.
    let left = member.post("/api/leave", json!({"mode": "instant", "confirm": true}));
    assert_eq!(left.0, 200, "leave: {}", left.1);
    assert_eq!(left.1["state"], "left", "{}", left.1);
    let after = member.status();
    assert_eq!(after["installation_role"], "target", "{after}");
    assert_eq!(after["leave"]["state"], "left", "{after}");
    let stopped = lease(&leases(&owner.mac));
    std::thread::sleep(millis(RENEWAL) * 3);
    assert_eq!(
        lease(&leases(&owner.mac)),
        stopped,
        "a member that left still renews its lease"
    );
    // 6. The owner closes its window; both Macs Quit clean and the relay is gone.
    let revoked = owner.mac.post(
        "/api/pair/window/revoke",
        json!({"window_id": owner.window_id}),
    );
    assert_eq!(revoked.0, 200, "revoke: {}", revoked.1);
    let proxy = owner.proxy;
    assert_eq!(member.quit(), Ok(Poll::Verified));
    assert_eq!(owner.mac.quit(), Ok(Poll::Verified));
    assert!(
        TcpStream::connect(("127.0.0.1", proxy)).is_err(),
        "the owner's relay proxy still listens after Quit"
    );
    std::fs::remove_dir_all(&base).unwrap_or_default();
}
