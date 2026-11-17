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

/// Lane XA status, patch 2 (REVIEW XB-07): a direct address is named `portmapped` only with
/// mapping evidence (the router granted a mapping), never because portmapping was merely asked
/// for. iroh's QAD address with the bound port (its `Qad4LocalPort`) is `qad_local_port`, and a
/// residual address without evidence is `unknown`.
#[test]
fn status_names_a_portmapped_direct_address_only_with_mapping_evidence() {
    let at = |s: &str| s.parse::<std::net::SocketAddr>().unwrap();
    let (bound, qad) = ([at("192.168.1.20:51000")], [at("203.0.113.9:40001")]);
    let kind = |addr: &str, granted: bool| direct_addr_kind(at(addr), &bound, &qad, granted);
    assert_eq!(kind("192.168.1.20:51000", true), "local");
    assert_eq!(kind("203.0.113.9:40001", true), "qad");
    // The QAD address with the local port is iroh's own candidate, mapping or not.
    assert_eq!(kind("203.0.113.9:51000", true), "qad_local_port");
    assert_eq!(kind("203.0.113.9:51000", false), "qad_local_port");
    // Portmapping asked for but the router refused (no mapping granted): not `portmapped`.
    assert_eq!(kind("198.51.100.7:61000", false), "unknown");
    // The router granted a mapping: the residual address is the mapping.
    assert_eq!(kind("198.51.100.7:61000", true), "portmapped");
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

/// The relays a builder's final configuration registers at, read from its `Debug` (the relay
/// map's `RelayUrl("…")` entries), and whether its portmapper is off.
fn configured(builder: &iroh::endpoint::Builder) -> (std::collections::BTreeSet<String>, bool) {
    let shape = format!("{builder:?}");
    let relays = shape
        .split("RelayUrl(\"")
        .skip(1)
        .filter_map(|rest| rest.split('"').next())
        .map(str::to_owned)
        .collect();
    let off = shape.matches("portmapper_config: Disabled").count() == 1
        && !shape.contains("portmapper_config: Enabled");
    (relays, off)
}

/// Lane XB patch 2 (REVIEW XB-09): the default-off guard is structural. EVERY product endpoint
/// shape ([`NetworkMode::ALL`]) is built through the one product builder ([`super::mode_builder`],
/// which `bind` uses) and its FINAL configuration is read: portmapping off (a later override or an
/// explicit `Enabled` would show), and exactly its own relay set: Mycelium's in the relay modes,
/// the run's across networks, none on the LAN or local only. Only a cross-network policy that
/// asked for portmapping turns it on.
#[test]
fn every_product_endpoint_shape_is_built_with_portmapping_off_and_only_its_relay() {
    use super::{NetworkMode as M, mode_builder};
    let mycelium: std::collections::BTreeSet<String> =
        ["https://relay.mycelium.now/".to_owned()].into();
    let run: std::collections::BTreeSet<String> = [dead_relay().to_string()].into();
    let across = LanPolicy::private_only().across_networks(dead_relay());
    for (at, mode) in M::ALL.into_iter().enumerate() {
        assert_eq!(mode.listed(), at, "{mode:?} is listed once, in order");
        let admission = Admission::new(LanPolicy::with_explicit(vec![LOOPBACK]).unwrap(), 4, 4);
        let transport = Arc::new(LanTransport::bind(LOOPBACK, admission).unwrap());
        let builder = mode_builder(
            mode,
            &Reach::Shipped,
            Some((LOOPBACK, &across)),
            Some(transport),
        )
        .unwrap();
        let (relays, off) = configured(&builder);
        assert!(off, "{mode:?}: portmapping off: {builder:?}");
        let expected = match mode {
            M::Production | M::ForceRelay => mycelium.clone(),
            M::CrossNetwork => run.clone(),
            M::LocalOnly | M::DirectLan => Default::default(),
        };
        assert_eq!(relays, expected, "{mode:?}: exactly its relay set");
    }
    // The one opt-in: a cross-network policy whose Mac chose portmapping.
    let mapped = across.clone().with_portmapping(true);
    let builder = mode_builder(M::CrossNetwork, &Reach::Shipped, Some((LOOPBACK, &mapped)), None)
        .unwrap();
    assert!(!configured(&builder).1, "the chosen portmapping is on");
    // The local mailbox carrier's builder (api::managed_carrier) is the local-only one.
    assert_eq!(configured(&super::local_builder().unwrap()), (Default::default(), true));
}
