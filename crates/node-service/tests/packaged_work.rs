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
            start_whole_model: Box::new(|_| Err("member_model_download_not_ported".to_string())),
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
            bridge: false,
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
/// Lane LAN round 2 (REVIEW-laneLAN F6, LAN-A3): an owner run names, before any dial, why the two
/// Macs cannot meet. This Mac's own reading that found no single private network carries its
/// reason (a fixed choice never does); a joined Mac that offers work only on its loopback while
/// this run is on the LAN, a family this run's one socket cannot reach, and a LAN address against
/// a loopback-only run are each refused with their own code and a message naming the side and the
/// cause.
/// Proves: F6 and LAN-A3 (named refusals: owner_work_network_unavailable,
/// member_work_network_mismatch, member_work_family_mismatch).
#[cfg(feature = "desktop-shell")]
#[test]
fn a_run_names_why_the_two_macs_cannot_meet() {
    use api::lan_interface::{
        AMBIGUOUS_PRIVATE_INTERFACES, NO_PRIVATE_ADDRESS, ONLY_BRIDGE_INTERFACES, Selection,
    };
    use node_service::packaged_work::{NetworkChoice, WorkNetwork, owner_network_refusal};
    use runtime::iroh_host::LanPolicy;
    use std::net::SocketAddr;
    let at = |text: &str| text.parse::<SocketAddr>().unwrap();
    // The reason travels with the resolution; a fixed choice is deliberate and carries none.
    for reason in [
        NO_PRIVATE_ADDRESS,
        AMBIGUOUS_PRIVATE_INTERFACES,
        ONLY_BRIDGE_INTERFACES,
    ] {
        let read = WorkNetwork::Read(
            Arc::new(move || Selection::LocalOnly(reason)),
            LanPolicy::private_only(),
        );
        let resolved = read.resolution();
        assert_eq!(resolved.choice, NetworkChoice(None));
        assert_eq!(resolved.local_only, Some(reason));
        let (code, message) = owner_network_refusal(reason);
        assert_eq!(code, "owner_work_network_unavailable");
        assert!(message.contains(reason), "{message}");
        assert!(message.contains("This Mac"), "names the side: {message}");
        assert!(message.len() <= 600, "{message}");
    }
    assert_eq!(WorkNetwork::loopback().resolution().local_only, None);
    let lan = NetworkChoice(Some((
        "192.168.1.23".parse().unwrap(),
        LanPolicy::private_only(),
    )));
    // Member on its loopback, this run on the LAN.
    assert!(!lan.allows(at("127.0.0.1:61700")));
    let (code, message) = lan.address_refusal(at("127.0.0.1:61700"));
    assert_eq!(code, "member_work_network_mismatch");
    assert!(
        message.contains("joined Mac") && message.contains("127.0.0.1"),
        "{message}"
    );
    assert!(message.contains("192.168.1.23"), "{message}");
    // Address family: an IPv4 run holds one IPv4 socket and cannot reach an IPv6-only member.
    assert!(
        !lan.allows(at("[fd00::5]:61700")),
        "IPv6 against an IPv4 run"
    );
    let (code, message) = lan.address_refusal(at("[fd00::5]:61700"));
    assert_eq!(code, "member_work_family_mismatch");
    assert!(
        message.contains("fd00::5") && message.contains("192.168.1.23"),
        "{message}"
    );
    let ula = NetworkChoice(Some((
        "fd00::1".parse().unwrap(),
        LanPolicy::private_only(),
    )));
    assert!(
        !ula.allows(at("192.168.1.40:61700")),
        "IPv4 against an IPv6 run"
    );
    assert!(ula.allows(at("[fd00::5]:61700")));
    assert_eq!(
        ula.address_refusal(at("192.168.1.40:61700")).0,
        "member_work_family_mismatch"
    );
    // A loopback-only run (a caller's fixed choice) against a member on the LAN.
    let (code, message) = NetworkChoice(None).address_refusal(at("192.168.1.40:61700"));
    assert_eq!(code, "member_work_network_mismatch");
    assert!(message.contains("192.168.1.40"), "{message}");
    // Anything else keeps the round-1 code, now with the address.
    let (code, message) = lan.address_refusal(at("17.5.7.8:61700"));
    assert_eq!(code, "member_work_address_refused");
    assert!(message.contains("17.5.7.8"), "{message}");
    assert!(lan.allows(at("192.168.1.40:61700")));
}
/// The joined Mac of the network tests: a packaged member whose work transport reads
/// 127.0.0.1 as "this Mac's interface" (nothing leaves the host) and whose relay is a dead
/// loopback port.
#[cfg(feature = "desktop-shell")]
struct ReachMember {
    p: fixture::Joined,
    authority: Arc<Mutex<node_service::packaged_run::Authority>>,
    network: Arc<node_service::packaged_network::Network>,
    member: Arc<node_service::packaged_network::PackagedMember>,
    agent: api::member_agent::MemberAgent<node_service::packaged_network::PackagedMember>,
}
#[cfg(feature = "desktop-shell")]
impl ReachMember {
    fn new() -> Self {
        Self::reading(api::lan_interface::Selection::Lan(std::net::IpAddr::from([
            127, 0, 0, 1,
        ])))
    }
    /// A joined Mac whose reading of its interfaces answers `selection` every time.
    fn reading(selection: api::lan_interface::Selection) -> Self {
        use node_service::{
            packaged_network::{MemberSeams, Network, NetworkConfig, PackagedMember},
            packaged_run::Authority,
            packaged_work::WorkNetwork,
        };
        use runtime::iroh_host::{LanPolicy, Reach};
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
        let loopback = std::net::IpAddr::from([127, 0, 0, 1]);
        let network = Network::new(
            &p.member_root,
            NetworkConfig {
                seams: node_service::packaged_network::JoinSeams {
                    member_seed: p.seed.options(),
                    ..Default::default()
                },
                work_network: WorkNetwork::Read(
                    Arc::new(move || selection.clone()),
                    LanPolicy::with_explicit(vec![loopback]).unwrap(),
                ),
                relay: Reach::Hermetic("http://127.0.0.1:1".parse().unwrap()),
                ..Default::default()
            },
        );
        let authority = Arc::new(Mutex::new(authority));
        let member = PackagedMember::new(
            authority.clone(),
            network.clone(),
            MemberSeams {
                closed: Box::new(|| false),
                resource: Box::new(|_| None),
                acquisition: Box::new(|| None),
                start_whole_model: Box::new(
                    |_| Err("member_model_download_not_ported".to_string()),
                ),
            },
        );
        Self {
            p,
            authority,
            network,
            member,
            agent: api::member_agent::MemberAgent::default(),
        }
    }
    fn tick(&mut self) {
        self.agent.tick(&self.member);
    }
    /// `status.network_reach` and the joined Mac's own card, as its window reads them.
    fn status(&self) -> (serde_json::Value, serde_json::Value) {
        let mut status = json!({});
        node_service::packaged_network::status_overlay(
            &mut status,
            Some(&self.authority),
            &self.network,
        );
        node_service::packaged_network::member_status(
            &mut status,
            Some(&self.authority),
            &self.network,
        );
        (
            status["network_reach"].clone(),
            status["demo_target"].clone(),
        )
    }
    /// The owner's view of this Mac's last report.
    fn report(&self) -> serde_json::Value {
        self.p.seed.exchange.reports()[self.p.session.node_id()].clone()
    }
    fn close(mut self) {
        self.agent.close(&self.member);
        self.network.quit();
    }
}
/// Lane XB patch 2 (decision D1, REVIEW XB-03/04/06): the OWNER chooses each run's network
/// reach and signs it in the run announcement; the joined Mac binds in exactly that mode, and its
/// own preference file decides nothing (here it says `relay` with portmapping: ignored). Status
/// names the ACTIVE work service's mode, not a file. A signed relay that is not this Mac's
/// configured relay is refused by name before anything binds (D4), on the joined Mac's card and
/// in its report to the owner (D3: named, never a timeout).
#[cfg(feature = "desktop-shell")]
#[test]
fn a_joined_mac_binds_in_the_mode_the_owner_signed_and_names_a_foreign_relay() {
    use api::run_exchange::work_network::RunReach;
    use node_service::packaged_work::reach;
    let mut m = ReachMember::new();
    reach::save(
        &m.p.member_root,
        reach::ReachChoice {
            across_networks: true,
            portmapping: true,
        },
    )
    .unwrap();
    // The owner signed nothing: the same network, the LAN carrier.
    m.tick();
    let (reach_status, _) = m.status();
    assert_eq!(
        reach_status["active"]["reach"], "same_network",
        "{reach_status}"
    );
    assert_eq!(
        reach_status["active"]["portmapping"], false,
        "{reach_status}"
    );
    assert_eq!(
        reach_status["error"],
        serde_json::Value::Null,
        "{reach_status}"
    );
    assert!(
        m.p.seed
            .work_members
            .get(
                &m.p.seed.coordinator,
                m.p.session.node_id(),
                m.p.owner.public(),
                "run-s4cb2",
                "dep-s4cb2",
                &|a| a.ip().is_loopback(),
            )
            .is_ok(),
        "the joined Mac's LAN work endpoint is published"
    );
    // The owner signs a relay that is not this Mac's: refused by name, nothing bound.
    m.p.seed.exchange.set_work_network(RunReach::Relay(
        "https://relay.example.org".parse().unwrap(),
    ));
    m.tick();
    let (reach_status, card) = m.status();
    assert_eq!(
        reach_status["error"], "work_network_relay_mismatch",
        "{reach_status}"
    );
    assert_eq!(
        reach_status["active"],
        serde_json::Value::Null,
        "{reach_status}"
    );
    assert_eq!(card["state"], "error", "{card}");
    assert_eq!(card["detail"], "work_network_relay_mismatch", "{card}");
    let report = m.report();
    assert_eq!(report["state"], "error", "{report}");
    assert_eq!(report["detail"], "work_network_relay_mismatch", "{report}");
    m.close();
}
/// Lane XB patch 2 (decision D3, REVIEW XB-02): in a run across networks a joined Mac checks
/// ITS OWN relay connection (both work endpoints, bounded by the ready-address window) before it
/// announces ready. With the relay unreachable it publishes no work endpoint and names
/// `relay_unreachable` on its card and in its report to the owner, so the owner's run fails by
/// that name instead of timing out on a Mac that looked available.
#[cfg(feature = "desktop-shell")]
#[test]
fn a_joined_mac_whose_relay_is_unreachable_refuses_by_name_before_announcing_ready() {
    use api::run_exchange::work_network::RunReach;
    let mut m = ReachMember::new();
    m.p.seed
        .exchange
        .set_work_network(RunReach::Relay("http://127.0.0.1:1".parse().unwrap()));
    let started = std::time::Instant::now();
    m.tick();
    let elapsed = started.elapsed();
    let (reach_status, card) = m.status();
    assert_eq!(reach_status["error"], "relay_unreachable", "{reach_status}");
    assert_eq!(
        reach_status["active"],
        serde_json::Value::Null,
        "{reach_status}"
    );
    assert_eq!(card["state"], "error", "{card}");
    assert_eq!(card["detail"], "relay_unreachable", "{card}");
    let report = m.report();
    assert_eq!(
        (report["state"].as_str(), report["detail"].as_str()),
        (Some("error"), Some("relay_unreachable")),
        "{report}"
    );
    assert!(
        m.p.seed
            .work_members
            .get(
                &m.p.seed.coordinator,
                m.p.session.node_id(),
                m.p.owner.public(),
                "run-s4cb2",
                "dep-s4cb2",
                &|_| true,
            )
            .is_err(),
        "no work endpoint is announced ready"
    );
    assert!(
        elapsed < std::time::Duration::from_secs(15),
        "bounded: {elapsed:?}"
    );
    m.close();
}
/// Lane XB patch 3 (REVIEW XB2-01, decisions D1/D3): a run the owner signed across networks
/// never becomes a loopback-only work service on the joined Mac. Whatever reading of its
/// interfaces leaves this Mac without a usable network (no private address, an ambiguous / VPN
/// selection, only bridge interfaces), the signed relay mode cannot be bound: the joined Mac
/// refuses `work_network_unavailable` on its card and in its report to the owner, binds nothing
/// and publishes no work endpoint (never a silent local-only service).
#[cfg(feature = "desktop-shell")]
#[test]
fn a_signed_relay_run_never_binds_a_loopback_only_service_on_the_joined_mac() {
    use api::{lan_interface, run_exchange::work_network::RunReach};
    for reason in [
        lan_interface::NO_PRIVATE_ADDRESS,
        lan_interface::AMBIGUOUS_PRIVATE_INTERFACES,
        lan_interface::ONLY_BRIDGE_INTERFACES,
        lan_interface::INTERFACES_UNREADABLE,
    ] {
        let mut m = ReachMember::reading(lan_interface::Selection::LocalOnly(reason));
        // Its own relay (the test's dead loopback port): the signed relay is this Mac's.
        m.p.seed
            .exchange
            .set_work_network(RunReach::Relay("http://127.0.0.1:1".parse().unwrap()));
        m.tick();
        let (reach_status, card) = m.status();
        assert_eq!(
            reach_status["error"], "work_network_unavailable",
            "{reason}: {reach_status}"
        );
        assert_eq!(
            reach_status["active"],
            serde_json::Value::Null,
            "{reason}: {reach_status}"
        );
        assert_eq!(card["state"], "error", "{reason}: {card}");
        assert_eq!(card["detail"], "work_network_unavailable", "{reason}: {card}");
        let report = m.report();
        assert_eq!(
            (report["state"].as_str(), report["detail"].as_str()),
            (Some("error"), Some("work_network_unavailable")),
            "{reason}: {report}"
        );
        assert!(
            m.p.seed
                .work_members
                .get(
                    &m.p.seed.coordinator,
                    m.p.session.node_id(),
                    m.p.owner.public(),
                    "run-s4cb2",
                    "dep-s4cb2",
                    &|_| true,
                )
                .is_err(),
            "{reason}: no work endpoint is published"
        );
        m.close();
    }
}
/// Lane XB patch 3 (REVIEW XB2-05): the owner's portmapping preference is frozen with its run,
/// like the run's reach: a change saved while the run is active (during the model or the compile)
/// applies to the next run, never to endpoints the running run binds later (its compose).
#[cfg(feature = "desktop-shell")]
#[test]
fn the_owners_portmapping_is_frozen_with_its_run() {
    use node_service::{
        packaged_network::{Network, NetworkConfig},
        packaged_work::{WorkNetwork, reach},
    };
    use runtime::iroh_host::LanPolicy;
    let root = std::env::temp_dir().join(format!("xb3-owner-pm-{}", std::process::id()));
    std::fs::remove_dir_all(&root).ok();
    std::fs::create_dir_all(&root).unwrap();
    let root = root.canonicalize().unwrap();
    let loopback = std::net::IpAddr::from([127, 0, 0, 1]);
    let network = Network::new(
        &root,
        NetworkConfig {
            work_network: WorkNetwork::Read(
                Arc::new(move || api::lan_interface::Selection::Lan(loopback)),
                LanPolicy::with_explicit(vec![loopback]).unwrap(),
            ),
            ..Default::default()
        },
    );
    let portmapping = |network: &Network| {
        let resolved = network.owner_run_network().unwrap().resolution();
        let (_, policy) = resolved.choice.0.expect("bound across networks");
        assert!(policy.relay().is_some(), "{policy:?}");
        policy.portmapping()
    };
    reach::save(
        &root,
        reach::ReachChoice {
            across_networks: true,
            portmapping: false,
        },
    )
    .unwrap();
    network.begin_owner_run().unwrap();
    assert!(!portmapping(&*network));
    // Saved during the run: the running run keeps portmapping off.
    reach::save(
        &root,
        reach::ReachChoice {
            across_networks: true,
            portmapping: true,
        },
    )
    .unwrap();
    assert!(!portmapping(&*network), "the running run's value is frozen");
    network.end_owner_run();
    network.begin_owner_run().unwrap();
    assert!(portmapping(&*network), "the next run reads it");
    network.end_owner_run();
    network.quit();
    std::fs::remove_dir_all(&root).ok();
}
