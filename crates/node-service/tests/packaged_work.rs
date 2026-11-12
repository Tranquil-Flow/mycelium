// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "iroh-loopback")]
#[path = "../../api/tests/support/work_member.rs"]
mod fixture;
use api::{
    app_host,
    joined_owner::{link::MemberLink, remote::RemoteMember},
    member_control::joined,
};
use node_service::packaged_work::Service;
use serde_json::json;
use std::sync::{Arc, Mutex};
#[cfg(feature = "desktop-shell")]
#[test]
fn packaged_standing_pass_publishes_real_work_endpoint_from_renewed_membership() {
    use api::member_agent::MemberAgent;
    use node_service::{packaged_network::{Network, NetworkConfig, PackagedMember, MemberSeams}, packaged_run::Authority};
    let p = fixture::Joined::new();
    p.announced();
    api::preferences::private_state::store(&p.member_root.join("installation-role.json"),
        &json!({"protocol":"mycelium.a13_installation_role.v1","role":"target"}), app_host::cap()).unwrap();
    let mut authority = Authority::load(p.member_root.clone()).unwrap();
    authority.target = true;
    authority.running = true;
    authority.session = Some(p.session.clone());
    let network = Network::new(&p.member_root, NetworkConfig { seams: node_service::packaged_network::JoinSeams { member_seed: p.seed.options(), ..Default::default() }, ..Default::default() });
    let member = PackagedMember::new(Arc::new(Mutex::new(authority)), network.clone(), MemberSeams { closed: Box::new(|| false), resource: Box::new(|_| None), acquisition: Box::new(|| None) });
    let mut agent = MemberAgent::default();
    agent.tick(&member);
    let result = p.seed.work_members.get(&p.seed.coordinator, p.session.node_id(), p.owner.public(), "run-s4cb2", "dep-s4cb2", &|a| a.ip().is_loopback());
    assert!(result.is_ok(), "packaged standing producer missing: {result:?}");
    agent.close(&member);
    network.quit();
}
#[test]
fn enrolled_packaged_control_dispatches_without_pair_request_and_closes_all_owned_loops() {
    let p = fixture::Joined::new();
    let result = Service::start(
        p.member_root.clone(),
        Arc::new(Mutex::new(Some(p.session.clone()))),
        p.signer.clone(),
        p.announced(),
    );
    assert!(
        result.is_ok(),
        "real packaged producer refused: {:?}",
        result.err()
    );
    let mut service = result.unwrap();
    let document = service.registration().unwrap();
    let verified = joined::verify(
        &p.seed.coordinator,
        &document,
        p.owner.public(),
        "run-s4cb2",
        "dep-s4cb2",
        &|a| a.ip().is_loopback(),
        app_host::now(),
    )
    .unwrap();
    let mut link =
        RemoteMember::already_joined(p.owner.clone(), &p.seed.coordinator, &verified, None)
            .unwrap();
    let wait = std::time::Duration::from_millis(
        budgets::BudgetId::RouterClose
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis(),
    );
    let status = link.call(json!({"command":"status"}), wait).unwrap();
    assert_eq!(status["joined"], true);
    assert_eq!(status["native_owned"], false);
    let consent = link
        .call(json!({"command":"consent_status"}), wait)
        .unwrap();
    assert_eq!(consent["consent"]["state"], "granted");
    assert_eq!(
        link.call(json!({"command":"consent","policy":p.policy}), wait)
            .unwrap_err()
            .0,
        "member_local_consent_required"
    );
    assert_eq!(
        link.call(json!({"command":"pair_request"}), wait)
            .unwrap_err()
            .0,
        "member_already_joined"
    );
    drop(link);
    service.close().unwrap();
    assert_eq!(service.registration().unwrap_err(), "member_work_closed");
    service.close().unwrap();
}
