// SPDX-License-Identifier: AGPL-3.0-only
//! Lane B, review REVIEW-laneB-release-astra B-02: a joined Mac's reconnect (B10) against a
//! renewal pass already on the wire. v0.1.4 holds the peer's lock across the whole reconnect
//! (`reconnect_peer`, endpoint_recovery.py:91-114 @ f4e366e9) and across the whole renewal
//! (`RemoteMacMember.renew`, peer.py:1037-1060): an older pass ends (heartbeat, its error's
//! translation, its disposition) before the reconnect stops and resumes the membership, never after.
//!
//! The fixture's joined Mac is made to have reached its owner at an old origin (its membership is
//! re-pinned there), and the owner's seed signs the card from it to the address it answers on. The
//! joined Mac's own renewal pass is held after it took its Session, before its heartbeat's lease
//! check; the reconnect runs; the pass is released reading its clock past the old lease (it then
//! records the membership `expired`, peer.rs `translate_with`) or on time (it renews late).
use super::*;

const OLD: &str = "https://v02-lane-b-old.example.org";

/// The joined Mac's authority as `app_host::renew_member` sees it, its renewal clock held on its
/// first read (the Session is out by then) and then reading `ahead` seconds past the wall clock.
struct HeldClock {
    authority: Arc<Mutex<Authority>>,
    network: Arc<Network>,
    gate: Mutex<Option<(std::sync::mpsc::Sender<()>, std::sync::mpsc::Receiver<()>)>>,
    ahead: f64,
}
impl app_host::Cell for HeldClock {
    type Managed = crate::packaged_run::Managed;
    fn with<R>(&self, f: impl FnOnce(&mut app_host::HostView<'_, Self::Managed>) -> R) -> R {
        self.network.cell(&self.authority).with(f)
    }
    fn try_with<R>(
        &self,
        f: impl FnOnce(&mut app_host::HostView<'_, Self::Managed>) -> R,
    ) -> Option<R> {
        self.network.cell(&self.authority).try_with(f)
    }
    fn member_seed_options(&self) -> api::seed::SeedOptions {
        self.network.cell(&self.authority).member_seed_options()
    }
    fn renewal_clock(&self) -> f64 {
        if let Some((at, release)) = lock(&self.gate).take() {
            at.send(()).unwrap_or(());
            release.recv().unwrap_or(());
        }
        app_host::now() + self.ahead
    }
}

/// The membership's generation as its durable state and its status read it.
fn generation(f: &Fixture) -> (Option<String>, Option<u64>) {
    let root = f.root.join("member");
    let live = lock(&f.member_authority)
        .session
        .as_ref()
        .map(|s| (s.generation(), s.lease_expires_at()));
    let (state, generation, _) = peer::status_membership(&root, live, app_host::now());
    (Some(state), generation)
}

/// Reconnect the joined Mac with the owner's card while its renewal pass is held, then release
/// that pass reading its clock `ahead` seconds forward. Returns the reconnect's answer.
fn reconnect_across_a_held_renewal(f: &Fixture, ahead: f64) -> Result<Value, &'static str> {
    let member_root = f.root.join("member");
    peer::repin_owner_seed_url(&member_root, OLD).unwrap();
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    let card = api::endpoint_recovery::endpoint_card(
        seed.coordinator.signer(),
        seed.coordinator.swarm_id(),
        seed.coordinator.seed_node_id(),
        &json!(OLD),
        &json!(seed.seed_url),
        app_host::now(),
    )
    .unwrap();
    let (at, at_gate) = std::sync::mpsc::channel();
    let (release, released) = std::sync::mpsc::channel();
    let held = HeldClock {
        authority: f.member_authority.clone(),
        network: f.network.clone(),
        gate: Mutex::new(Some((at, released))),
        ahead,
    };
    let renewal = std::thread::spawn(move || app_host::renew_member(&held));
    at_gate
        .recv_timeout(Duration::from_secs(60))
        .expect("the renewal took its Session and read its clock");
    let (authority, network) = (f.member_authority.clone(), f.network.clone());
    let options = network.seams().member_seed.clone();
    let reconnect = std::thread::spawn(move || {
        api::endpoint_recovery::reconnect_peer(&network.cell(&authority), &options, &card)
            .map_err(|e| e.code)
    });
    std::thread::sleep(Duration::from_millis(300));
    release.send(()).unwrap_or(());
    renewal.join().expect("the held renewal returned");
    let answered = reconnect.join().expect("the reconnect returned");
    // The rest of `Network::renew_member` after a pass: a membership no longer current closes the
    // joined Mac's work service.
    f.network.sync_work_session(&f.member_authority);
    if crate::packaged_network::membership_state(&f.member_authority).as_deref() != Some("current")
    {
        f.network.close_work();
    }
    answered
}

/// An old pass released past its lease after the reconnect would record `expired` over the
/// resumed membership and close its work. The reconnect waits for it: the new generation is
/// current, no disposition is left, and the next renewal pass renews it.
#[test]
fn an_old_renewal_expiring_late_leaves_the_reconnected_membership_current() {
    let f = Fixture::new();
    let (_, before) = generation(&f);
    let answered = reconnect_across_a_held_renewal(&f, 1.0e7);
    let after = generation(&f);
    let disposition = f.root.join("member/membership.disposition.json").exists();
    f.network.renew_member(&f.member_authority);
    let renewed = generation(&f);
    assert!(
        answered.is_ok()
            && after.0.as_deref() == Some("current")
            && after.1 > before
            && !disposition
            && renewed.0.as_deref() == Some("current"),
        "reconnect {answered:?}; generation {before:?} -> {after:?}, disposition left {disposition}, \
         after the next renewal {renewed:?}"
    );
}

/// The late-success case: an old pass that renews on time ends before the reconnect resumes, so
/// the reconnect's own resume is the last word (a newer generation, current).
#[test]
fn an_old_renewal_succeeding_late_does_not_overwrite_the_reconnected_membership() {
    let f = Fixture::new();
    let (_, before) = generation(&f);
    let answered = reconnect_across_a_held_renewal(&f, 0.0);
    let after = generation(&f);
    f.network.renew_member(&f.member_authority);
    let renewed = generation(&f);
    assert!(
        answered.is_ok()
            && after.0.as_deref() == Some("current")
            && after.1 > before
            && renewed.0.as_deref() == Some("current")
            && renewed.1 == after.1,
        "reconnect {answered:?}; generation {before:?} -> {after:?}, after the next renewal {renewed:?}"
    );
}
