// SPDX-License-Identifier: AGPL-3.0-only
//! Lane B, review REVIEW-laneB-release-astra B-01: `/api/network/restart` (B10) under the owner's
//! own contributor. v0.1.4 refuses `network_stop_local_contributor_required` when a contributor is
//! present and the (merged) external origin is not the one the retained authority answers on,
//! BEFORE it closes the old relay or relocates the seed (app.py:1450-1453 @ f4e366e9). An external
//! restart on the configured origin needs no relocation and proceeds; once the contributor is
//! stopped a relocation proceeds. Driven through the packaged dispatch (`PackagedActions`) over
//! the fixture's owner, whose contributor is the product's own (`start_local_contributor`).
use super::*;
use api::actions::{Action, Backend};

const PUBLIC: &str = "https://v02-lane-b-restart.example.org";
const MOVED: &str = "https://v02-lane-b-restart-moved.example.org";

fn free_port() -> u16 {
    let _forks = api::relay::fork_boundary();
    std::net::TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap()
}
fn actions(f: &Fixture) -> crate::packaged_network::PackagedActions {
    crate::packaged_network::PackagedActions::new(
        crate::router_port::ServingActions::new(
            f.host.node.clone(),
            Err(PublicError::new(409, "lane_b_no_serving")),
        ),
        f.host.network.clone(),
        Ok(f.host.authority.clone()),
    )
}
fn restart(f: &Fixture, origin: &str) -> Result<Value, &'static str> {
    let body = json!({"relay_acknowledged": true, "max_seconds": 1800, "provider": "external",
                      "public_origin": origin, "proxy_port": free_port()});
    actions(f)
        .execute_json(Action::NetworkRestart, body.as_object().unwrap())
        .map(|answer| answer.to_value())
        .map_err(|e| e.code)
}
/// The owner answers on PUBLIC (its retained configuration), its contributor enrolled.
fn on_public() -> (
    Fixture,
    Arc<api::app_host::SeedRuntime<crate::packaged_run::Managed>>,
) {
    let f = Fixture::new();
    let seed = f.host.with(|c| {
        c.authority.configured_origin = Some(PUBLIC.into());
        c.authority.seed.clone().unwrap()
    });
    assert!(f.host.with(|c| c.run.contributor.is_some()));
    (f, seed)
}
fn same_seed(
    f: &Fixture,
    seed: &Arc<api::app_host::SeedRuntime<crate::packaged_run::Managed>>,
) -> bool {
    f.host.with(|c| {
        c.authority
            .seed
            .as_ref()
            .is_some_and(|s| Arc::ptr_eq(s, seed))
    })
}

#[test]
fn a_restart_to_a_new_origin_under_a_live_contributor_is_refused_before_anything_changes() {
    let (f, seed) = on_public();
    let refused = restart(&f, MOVED);
    let card = f.root.join("owner").join(api::endpoint_recovery::CARD_FILE);
    let after = (
        same_seed(&f, &seed),
        f.host.network.state() == api::persistent::RelayState::Absent,
        f.host.with(|c| {
            (
                c.run.contributor.is_some(),
                c.authority.configured_origin.clone(),
            )
        }),
        card.exists(),
    );
    f.host.network.close();
    assert_eq!(
        (refused, after),
        (
            Err("network_stop_local_contributor_required"),
            (true, true, (true, Some(PUBLIC.to_owned())), false)
        )
    );
}

#[test]
fn a_restart_on_the_configured_origin_proceeds_under_the_contributor_without_relocating() {
    let (f, seed) = on_public();
    let restarted = restart(&f, PUBLIC);
    let kept = (
        same_seed(&f, &seed),
        f.host.with(|c| c.run.contributor.is_some()),
    );
    f.host.network.close();
    let restarted = restarted.expect("restart on the configured origin");
    assert_eq!(
        (
            &restarted["network"]["public_origin"],
            &restarted["recovery_card"],
            kept
        ),
        (&json!(PUBLIC), &Value::Null, (true, true))
    );
}

#[test]
fn a_restart_to_a_new_origin_after_the_contributor_stopped_relocates_with_a_card() {
    let (f, seed) = on_public();
    owner_ports::stop_local_contributor(&f.host).unwrap();
    let restarted = restart(&f, MOVED);
    let moved = !same_seed(&f, &seed);
    f.host.network.close();
    let restarted = restarted.expect("restart after the contributor stopped");
    let statement = &restarted["recovery_card"]["statement"];
    assert_eq!(
        (
            &statement["previous_origin"],
            &statement["public_origin"],
            moved
        ),
        (&json!(PUBLIC), &json!(MOVED), true)
    );
}
