// SPDX-License-Identifier: AGPL-3.0-only
//! `RemoteMacMember.status()`'s membership projection (peer.py:395-455 @ f4e366e9) as
//! `api::peer::status_membership` gives it to both desktop hosts, on an injected clock: a member is
//! never `current` once its own lease ran out (REVIEW-laneGR 'Member renewal' (2)), and a recorded
//! terminal disposition (`_translate`, peer.py:299-323: revoked / stale / unknown / expired) wins at
//! once over a live session.
use api::peer::status_membership;
use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};

fn root(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "ph3a-status-membership-{}-{tag}",
        std::process::id()
    ));
    fs::remove_dir_all(&dir).unwrap_or(());
    fs::create_dir_all(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
    dir
}
fn dispose(root: &Path, state: &str) {
    let path = root.join("membership.disposition.json");
    let record = serde_json::json!({
        "protocol": "mycelium.remote_mac_membership_disposition.v1",
        "membership_state": state, "observed_at": 1000.0,
    });
    fs::write(&path, serde_json::to_vec(&record).unwrap()).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
}

#[test]
fn a_live_member_is_current_only_while_its_own_lease_runs() {
    let dir = root("lease");
    assert_eq!(
        status_membership(&dir, None, 1000.0),
        ("absent".into(), None, "none")
    );
    let live = Some((Some(3), Some(1000.0)));
    assert_eq!(
        status_membership(&dir, live, 999.5),
        ("current".into(), Some(3), "fresh")
    );
    // `self._clock() >= lease`: expired at the instant the lease ends, and after it.
    for now in [1000.0, 1000.5, 4000.0] {
        assert_eq!(
            status_membership(&dir, live, now),
            ("expired".into(), Some(3), "expired"),
            "{now}"
        );
    }
    fs::remove_dir_all(&dir).unwrap_or(());
}

#[test]
fn a_recorded_terminal_disposition_wins_over_a_live_lease() {
    let dir = root("disposition");
    let live = Some((Some(3), Some(5000.0)));
    for (state, lease) in [
        ("revoked", "fresh"),
        ("stale", "fresh"),
        ("unknown", "fresh"),
        ("expired", "expired"),
    ] {
        dispose(&dir, state);
        assert_eq!(
            status_membership(&dir, live, 1000.0),
            (state.into(), Some(3), lease),
            "{state}"
        );
    }
    fs::remove_dir_all(&dir).unwrap_or(());
}
