// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XB (ruling 29 B): which relay and address lookup the endpoint builders configure. Read
//! from the builder itself (its `Debug` lists the relay map and the address-lookup services), so
//! no endpoint is bound and nothing leaves the host.
use super::{NetworkMode, Reach, relay_builder};

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
