// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XB patch 2 (decision D1, REVIEW XB-04): the owner's per-run network reach rides the
//! run announcement a joined Mac verifies (the seed-signed read response) before it binds its
//! carriers. Real seed, real signed read, no network beyond loopback, no model.
#[path = "support/work_member.rs"]
mod fixture;
use api::run_exchange::work_network::{FIELD, RunReach};

/// A run across networks: the joined Mac's verified read carries the owner's relay choice. A
/// same-network run publishes no key at all (the announcement keeps its exact bytes), and a
/// later same-network run withdraws it.
#[test]
fn the_owners_reach_reaches_the_joined_mac_inside_the_signed_announcement() {
    let p = fixture::Joined::new();
    let same = p.announced();
    assert!(same.announcement().get(FIELD).is_none(), "{:?}", same.announcement());
    assert_eq!(RunReach::from_announcement(&same), Ok(RunReach::SameNetwork));
    let relay: iroh::RelayUrl = "https://relay.mycelium.now".parse().unwrap();
    p.seed.exchange.set_work_network(RunReach::Relay(relay.clone()));
    let across = p.announced();
    assert_eq!(
        RunReach::from_announcement(&across),
        Ok(RunReach::Relay(relay)),
        "{:?}",
        across.announcement()
    );
    p.seed.exchange.set_work_network(RunReach::SameNetwork);
    let again = p.announced();
    assert!(again.announcement().get(FIELD).is_none());
    let strip = |v: &serde_json::Value| {
        let mut v = v.clone();
        for key in ["issued_at", "model_source"] {
            v.as_object_mut().unwrap().remove(key);
        }
        v
    };
    assert_eq!(strip(again.announcement()), strip(same.announcement()), "exact bytes");
}
