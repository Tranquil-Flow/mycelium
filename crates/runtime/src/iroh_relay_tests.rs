// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XB (ruling 29 B): which relay and address lookup the endpoint builders configure. Read
//! from the builder itself (its `Debug` lists the relay map and the address-lookup services), so
//! no endpoint is bound and nothing leaves the host.
use super::{LanPolicy, NetworkMode, Reach, lan_builder, local_builder, relay_builder};
use crate::{
    iroh_cross::{cross_builder, direct_addr_kind},
    iroh_lan_filter::{Admission, LanTransport},
};
use std::sync::Arc;

fn shape(mode: NetworkMode, reach: &Reach) -> String {
    format!("{:?}", relay_builder(mode, reach).unwrap())
}

/// XB1: a sidecar launched with no network flag (`Production`, the shipped reach) and one forced
/// onto the relay register at Mycelium's relay only: no n0 relay, no n0 DNS / pkarr lookup.
#[test]
fn the_production_endpoint_uses_only_the_mycelium_relay_and_no_address_lookup() {
    for mode in [NetworkMode::Production, NetworkMode::ForceRelay] {
        let shape = shape(mode, &Reach::Shipped);
        assert!(
            shape.contains("relay.mycelium.now"),
            "{mode:?}: the relay map names Mycelium's relay: {shape}"
        );
        assert!(
            !shape.contains("iroh.link"),
            "{mode:?}: no n0 relay and no n0 DNS / pkarr server: {shape}"
        );
        assert!(
            shape.contains("address_lookup: []"),
            "{mode:?}: no address-lookup service: {shape}"
        );
    }
}

/// XB1: a test (or an operator) overrides the relay; the override is the ONLY relay.
#[test]
fn an_overridden_relay_is_the_only_relay() {
    let url: iroh::RelayUrl = "http://127.0.0.1:1".parse().unwrap();
    let shape = shape(NetworkMode::Production, &Reach::Hermetic(url));
    assert!(shape.contains("127.0.0.1:1"), "{shape}");
    assert!(!shape.contains("relay.mycelium.now"), "{shape}");
    assert!(!shape.contains("iroh.link"), "{shape}");
    assert!(shape.contains("address_lookup: []"), "{shape}");
}

fn dead_relay() -> iroh::RelayUrl {
    "http://127.0.0.1:1".parse().unwrap()
}
const LOOPBACK: std::net::IpAddr = std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST);

/// XB3 guard: the cross-network builder has exactly the run's relay and no address lookup, so an
/// unreachable relay has nothing (no n0 relay, DNS or pkarr) to fall back to.
#[test]
fn the_cross_network_builder_has_only_the_run_relay_and_no_address_lookup() {
    let across = LanPolicy::private_only().across_networks(dead_relay());
    let shape = format!("{:?}", cross_builder(LOOPBACK, &across).unwrap());
    assert!(shape.contains("127.0.0.1:1"), "{shape}");
    assert!(!shape.contains("iroh.link"), "{shape}");
    assert!(shape.contains("address_lookup: []"), "{shape}");
}

/// Lane XA: with iroh's `portmapper` feature compiled in, no builder of this host starts a
/// portmapper unless the run's cross-network policy asks for one (default off).
#[test]
fn portmapping_is_off_unless_the_cross_network_policy_asks_for_it() {
    let off = "portmapper_config: Disabled";
    for mode in [NetworkMode::Production, NetworkMode::ForceRelay] {
        assert!(shape(mode, &Reach::Shipped).contains(off), "{mode:?}");
    }
    assert!(
        format!("{:?}", local_builder().unwrap()).contains(off),
        "local only"
    );
    let admission = Admission::new(LanPolicy::with_explicit(vec![LOOPBACK]).unwrap(), 4, 4);
    let transport = Arc::new(LanTransport::bind(LOOPBACK, admission).unwrap());
    assert!(
        format!("{:?}", lan_builder(transport)).contains(off),
        "direct LAN"
    );
    let across = LanPolicy::private_only().across_networks(dead_relay());
    assert!(!across.portmapping(), "default off");
    assert!(
        format!("{:?}", cross_builder(LOOPBACK, &across).unwrap()).contains(off),
        "across networks, default"
    );
    let mapped = across.with_portmapping(true);
    assert!(mapped.portmapping());
    assert!(
        format!("{:?}", cross_builder(LOOPBACK, &mapped).unwrap())
            .contains("portmapper_config: Enabled"),
        "across networks, the user turned portmapping on"
    );
}

/// Lane XA status: a portmapped direct address is named as such, only when the run asked for it.
#[test]
fn status_names_a_portmapped_direct_address() {
    let at = |s: &str| s.parse::<std::net::SocketAddr>().unwrap();
    let (bound, qad) = ([at("192.168.1.20:51000")], [at("203.0.113.9:40001")]);
    assert_eq!(
        direct_addr_kind(at("192.168.1.20:51000"), &bound, &qad, true),
        "local"
    );
    assert_eq!(
        direct_addr_kind(at("203.0.113.9:40001"), &bound, &qad, true),
        "qad"
    );
    assert_eq!(
        direct_addr_kind(at("203.0.113.9:51000"), &bound, &qad, true),
        "portmapped"
    );
    assert_eq!(
        direct_addr_kind(at("203.0.113.9:51000"), &bound, &qad, false),
        "public"
    );
}

/// Lane XB patch 2 (REVIEW XB-01, D4): a relay-mode host dials a peer only through the relay it
/// registers at. A peer address naming any other relay (an n0 relay, say) is refused whole
/// before anything is sent, alone or beside an IP address; its own relay and plain IP addresses
/// are dialled as before.
#[test]
fn a_relay_mode_host_refuses_a_peer_naming_a_foreign_relay() {
    use budgets::{BudgetId, Entries};
    use transport::{Channel, Clock};
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let clock = crate::iroh_cross::ProbeClock::default();
    let after = |id: BudgetId| {
        clock
            .monotonic()
            .checked_add(id.span(Entries::ZERO).unwrap())
            .unwrap()
    };
    let foreign: iroh::RelayUrl = "https://euw1-1.relay.n0.iroh-canary.iroh.link./"
        .parse()
        .unwrap();
    let peer = iroh::SecretKey::from_bytes(&[9; 32]).public();
    let public: std::net::SocketAddr = "203.0.113.9:4433".parse().unwrap();
    for mode in [NetworkMode::Production, NetworkMode::ForceRelay] {
        let mut host = super::LoopbackHost::bind_network(
            &runtime,
            &clock,
            after(BudgetId::WorkerStartup),
            iroh::SecretKey::generate(),
            &[Channel::Router],
            mode,
            &Reach::Hermetic(dead_relay()),
        )
        .unwrap();
        let only = iroh::EndpointAddr::new(peer).with_relay_url(foreign.clone());
        let mixed = iroh::EndpointAddr::new(peer)
            .with_ip_addr(public)
            .with_relay_url(foreign.clone());
        assert!(host.validate_peer(&only).is_err(), "{mode:?}: foreign only");
        assert!(host.validate_peer(&mixed).is_err(), "{mode:?}: mixed");
        let ours = iroh::EndpointAddr::new(peer).with_relay_url(dead_relay());
        assert!(host.validate_peer(&ours).is_ok(), "{mode:?}: our relay");
        let direct = iroh::EndpointAddr::new(peer).with_ip_addr(public);
        assert!(host.validate_peer(&direct).is_ok(), "{mode:?}: an IP address");
        host.close(&clock, after(BudgetId::RouterClose)).unwrap();
    }
}
