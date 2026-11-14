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
    use node_service::{
        packaged_network::{MemberSeams, Network, NetworkConfig, PackagedMember},
        packaged_run::Authority,
    };
    let p = fixture::Joined::new();
    p.announced();
    api::preferences::private_state::store(
        &p.member_root.join("installation-role.json"),
        &json!({"protocol":"mycelium.a13_installation_role.v1","role":"target"}),
        app_host::cap(),
    )
    .unwrap();
    let mut authority = Authority::load(p.member_root.clone()).unwrap();
    authority.target = true;
    authority.running = true;
    authority.session = Some(p.session.clone());
    let network = Network::new(
        &p.member_root,
        NetworkConfig {
            seams: node_service::packaged_network::JoinSeams {
                member_seed: p.seed.options(),
                ..Default::default()
            },
            // The endpoint this test dials is on loopback, whatever this host's interfaces are.
            work_network: node_service::packaged_work::WorkNetwork::loopback(),
            ..Default::default()
        },
    );
    let member = PackagedMember::new(
        Arc::new(Mutex::new(authority)),
        network.clone(),
        MemberSeams {
            closed: Box::new(|| false),
            resource: Box::new(|_| None),
            acquisition: Box::new(|| None),
            start_whole_model: Box::new(|_| {
                Err("member_model_download_not_ported".to_string())
            }),
            },
    );
    let mut agent = MemberAgent::default();
    agent.tick(&member);
    let result = p.seed.work_members.get(
        &p.seed.coordinator,
        p.session.node_id(),
        p.owner.public(),
        "run-s4cb2",
        "dep-s4cb2",
        &|a| a.ip().is_loopback(),
    );
    assert!(
        result.is_ok(),
        "packaged standing producer missing: {result:?}"
    );
    let mut link =
        RemoteMember::already_joined(p.owner.clone(), &p.seed.coordinator, &result.unwrap(), None)
            .unwrap();
    let wait = std::time::Duration::from_millis(
        budgets::BudgetId::RouterClose
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis(),
    );
    assert_eq!(
        link.call(json!({"command":"status"}), wait).unwrap()["joined"],
        true
    );
    assert_eq!(
        link.call(json!({"command":"pair_request"}), wait)
            .unwrap_err()
            .0,
        "member_already_joined"
    );
    drop(link);
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
    let address = link.call(json!({"command":"carrier_address"}), wait);
    assert!(
        address.is_ok(),
        "epoch consumer needs an enrolled carrier advertisement: {address:?}"
    );
    let address = address.unwrap();
    assert_eq!(
        address["advert"]["message"]["member"]["node_id"],
        p.session.node_id()
    );
    assert_eq!(
        address["endpoint_id"],
        verified.registration()["message"]["carrier_endpoint_id"]
    );
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
/// Lane LAN: the app's own composition (`NetworkConfig::default()`, what every production
/// constructor passes) puts the work transport on this Mac's private LAN address when it has one,
/// through the pure rule (`api::lan_interface::select`), and stays 127.0.0.1-only when it has none.
/// An explicit choice is kept as given and never reads the interfaces.
#[cfg(feature = "desktop-shell")]
#[test]
fn the_app_default_work_transport_follows_this_macs_private_address() {
    use api::lan_interface::{InterfaceAddress, NO_PRIVATE_ADDRESS, Selection, select};
    use node_service::{
        packaged_network::{Network, NetworkConfig},
        packaged_work::{NetworkChoice, WorkNetwork},
    };
    use runtime::iroh_host::LanPolicy;
    use std::net::{IpAddr, SocketAddr};
    let ip = |text: &str| text.parse::<IpAddr>().unwrap();
    let at = |text: &str| SocketAddr::new(ip(text), 61700);
    let interface =
        |name: &str, address: &str, loopback: bool, point_to_point: bool| InterfaceAddress {
            name: name.into(),
            ip: ip(address),
            up: true,
            loopback,
            point_to_point,
        };
    let root = std::env::temp_dir().join(format!(
        "lan-default-config-{}",
        app_host::hex(&app_host::random(8).unwrap())
    ));
    std::fs::create_dir(&root).unwrap();
    let network = Network::new(&root, NetworkConfig::default());
    let default = network.work_network().clone();
    network.quit();
    std::fs::remove_dir_all(&root).unwrap();
    assert_eq!(default, WorkNetwork::ThisMac);
    // A Mac on Wi-Fi with Tailscale up: the Wi-Fi address, never the tailnet or loopback one.
    let wifi = [
        interface("lo0", "127.0.0.1", true, false),
        interface("utun5", "100.84.252.4", false, true),
        interface("en0", "192.168.1.23", false, false),
    ];
    let (choice, transport) = default.resolve_with(|| select(&wifi, Some(ip("192.168.1.23"))));
    assert_eq!(
        choice,
        NetworkChoice(Some((ip("192.168.1.23"), LanPolicy::private_only())))
    );
    assert_eq!(transport, "lan:192.168.1.23");
    assert!(choice.allows(at("192.168.1.40")));
    assert!(!choice.allows(at("127.0.0.1")));
    assert!(!choice.allows(at("100.84.252.4")));
    assert!(!choice.allows(at("17.5.7.8")));
    // No private address: LocalOnly, and the reason is named.
    let none = [
        interface("lo0", "127.0.0.1", true, false),
        interface("utun5", "100.84.252.4", false, true),
        interface("en0", "17.5.7.8", false, false),
    ];
    let (choice, transport) = default.resolve_with(|| select(&none, Some(ip("17.5.7.8"))));
    assert_eq!(choice, NetworkChoice(None));
    assert_eq!(transport, format!("local_only:{NO_PRIVATE_ADDRESS}"));
    assert!(choice.allows(at("127.0.0.1")));
    assert!(!choice.allows(at("192.168.1.40")));
    let (choice, transport) =
        default.resolve_with(|| Selection::LocalOnly(api::lan_interface::INTERFACES_UNREADABLE));
    assert_eq!(choice, NetworkChoice(None));
    assert_eq!(transport, "local_only:interfaces_unreadable");
    // An explicit choice is exactly what the caller passed.
    let read = || -> Selection { panic!("an explicit choice never reads the interfaces") };
    assert_eq!(
        WorkNetwork::loopback().resolve_with(read),
        (NetworkChoice(None), "local_only:explicit".to_owned())
    );
    let lan = NetworkChoice(Some((ip("10.160.112.32"), LanPolicy::private_only())));
    assert_eq!(
        WorkNetwork::Fixed(lan.clone()).resolve_with(read),
        (lan, "lan:10.160.112.32".to_owned())
    );
}
