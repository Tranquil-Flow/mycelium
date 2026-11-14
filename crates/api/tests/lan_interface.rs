// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged work transport's address rule (`api::lan_interface::select`, lane LAN): only a
//! private LAN address the direct-LAN policy admits, never a public, CGNAT/tailnet, loopback or
//! tunnel address; the default route's source first; deterministic whatever the input order.
use api::lan_interface::{
    InterfaceAddress, NO_PRIVATE_ADDRESS, Selection, interface_addresses, select, this_mac,
};
use runtime::iroh_host::LanPolicy;
use std::net::IpAddr;

fn ip(text: &str) -> IpAddr {
    text.parse().unwrap()
}
fn up(name: &str, address: &str) -> InterfaceAddress {
    InterfaceAddress {
        name: name.into(),
        ip: ip(address),
        up: true,
        loopback: false,
        point_to_point: false,
    }
}
fn tunnel(name: &str, address: &str) -> InterfaceAddress {
    InterfaceAddress {
        point_to_point: true,
        ..up(name, address)
    }
}
fn loopback(address: &str) -> InterfaceAddress {
    InterfaceAddress {
        loopback: true,
        ..up("lo0", address)
    }
}
/// Every LAN answer must be one the bound policy admits.
fn chosen(addresses: &[InterfaceAddress], default_source: Option<IpAddr>) -> Selection {
    let selection = select(addresses, default_source);
    if let Selection::Lan(address) = selection {
        assert!(
            LanPolicy::private_only().allows(address),
            "{address} is outside LanPolicy::private_only"
        );
        assert!(
            addresses.iter().any(|a| a.ip == address),
            "{address} is not one of the given interface addresses"
        );
    }
    selection
}

#[test]
fn one_private_wifi_address_is_chosen() {
    assert_eq!(
        chosen(&[loopback("127.0.0.1"), up("en0", "192.168.1.23")], None),
        Selection::Lan(ip("192.168.1.23"))
    );
    assert_eq!(
        chosen(&[up("en0", "10.160.112.32")], Some(ip("10.160.112.32"))),
        Selection::Lan(ip("10.160.112.32"))
    );
    assert_eq!(
        chosen(&[up("en0", "172.16.4.2")], None),
        Selection::Lan(ip("172.16.4.2"))
    );
}

#[test]
fn never_public_cgnat_tailnet_loopback_tunnel_or_down_addresses() {
    let refused = [
        loopback("127.0.0.1"),
        loopback("127.103.250.137"),
        loopback("::1"),
        // Tailscale: CGNAT on a point-to-point utun, and CGNAT on its own.
        tunnel("utun5", "100.84.252.4"),
        up("en9", "100.64.0.1"),
        up("en9", "100.127.255.254"),
        // A VPN that hands out a private address is still a tunnel, not the shared network.
        tunnel("utun3", "10.8.0.2"),
        tunnel("ppp0", "192.168.50.2"),
        // Public addresses, even on the Wi-Fi interface.
        up("en0", "17.5.7.8"),
        up("en0", "8.8.8.8"),
        up("en0", "2001:db8::5"),
        // IPv6 link-local needs a scope id the bind does not carry.
        up("en0", "fe80::1"),
        up("en0", "0.0.0.0"),
        up("en0", "::"),
        up("en0", "224.0.0.251"),
        up("en0", "ff02::fb"),
        up("en0", "255.255.255.255"),
        InterfaceAddress {
            up: false,
            ..up("en0", "192.168.1.23")
        },
    ];
    for address in &refused {
        assert_eq!(
            chosen(std::slice::from_ref(address), Some(address.ip)),
            Selection::LocalOnly(NO_PRIVATE_ADDRESS),
            "{address:?} must never carry the work transport"
        );
    }
    assert_eq!(
        chosen(&refused, Some(ip("100.84.252.4"))),
        Selection::LocalOnly(NO_PRIVATE_ADDRESS)
    );
    assert_eq!(chosen(&[], None), Selection::LocalOnly(NO_PRIVATE_ADDRESS));
}

#[test]
fn the_default_routes_source_wins_over_other_private_interfaces() {
    // Internet sharing's bridge sorts before en0 by name; the default route is on en0.
    let addresses = [
        up("bridge100", "192.168.2.1"),
        up("en0", "10.160.112.32"),
        up("en5", "169.254.10.3"),
    ];
    assert_eq!(
        chosen(&addresses, Some(ip("10.160.112.32"))),
        Selection::Lan(ip("10.160.112.32"))
    );
    assert_eq!(
        chosen(&addresses, Some(ip("169.254.10.3"))),
        Selection::Lan(ip("169.254.10.3"))
    );
}

#[test]
fn a_default_route_that_is_not_a_candidate_falls_back_to_the_fixed_order() {
    // Full-tunnel VPN: the default route's source is the tunnel's own address.
    let addresses = [
        tunnel("utun3", "10.8.0.2"),
        up("en0", "192.168.1.23"),
        tunnel("utun5", "100.84.252.4"),
    ];
    assert_eq!(
        chosen(&addresses, Some(ip("10.8.0.2"))),
        Selection::Lan(ip("192.168.1.23"))
    );
    assert_eq!(
        chosen(&addresses, Some(ip("100.84.252.4"))),
        Selection::Lan(ip("192.168.1.23"))
    );
    assert_eq!(
        chosen(&addresses, Some(ip("192.0.2.77"))),
        Selection::Lan(ip("192.168.1.23"))
    );
}

#[test]
fn rfc1918_before_ipv4_link_local_before_ipv6_unique_local_then_name_then_address() {
    assert_eq!(
        chosen(&[up("en5", "169.254.3.4"), up("en7", "192.168.1.9")], None),
        Selection::Lan(ip("192.168.1.9"))
    );
    assert_eq!(
        chosen(&[up("en0", "fd00::5"), up("en9", "169.254.3.4")], None),
        Selection::Lan(ip("169.254.3.4"))
    );
    assert_eq!(
        chosen(&[up("en0", "fd12:3456::7")], None),
        Selection::Lan(ip("fd12:3456::7"))
    );
    assert_eq!(
        chosen(&[up("en1", "10.0.0.2"), up("en0", "192.168.1.9")], None),
        Selection::Lan(ip("192.168.1.9"))
    );
    assert_eq!(
        chosen(&[up("en0", "192.168.1.9"), up("en0", "10.0.0.2")], None),
        Selection::Lan(ip("10.0.0.2"))
    );
}

#[test]
fn the_choice_does_not_depend_on_the_interface_order() {
    let addresses = vec![
        up("en0", "fd00::5"),
        up("bridge100", "192.168.2.1"),
        up("en0", "192.168.1.23"),
        tunnel("utun5", "100.84.252.4"),
        up("en5", "169.254.10.3"),
    ];
    for default_source in [None, Some(ip("192.168.1.23")), Some(ip("100.84.252.4"))] {
        let expected = chosen(&addresses, default_source);
        assert_ne!(expected, Selection::LocalOnly(NO_PRIVATE_ADDRESS));
        // Every rotation and its reverse.
        for shift in 0..addresses.len() {
            let mut order = addresses.clone();
            order.rotate_left(shift);
            assert_eq!(chosen(&order, default_source), expected, "{order:?}");
            order.reverse();
            assert_eq!(chosen(&order, default_source), expected, "{order:?}");
        }
    }
}

/// This host's own reading: whatever it chooses is one of its interface addresses and admitted.
#[test]
fn this_macs_choice_is_one_of_its_own_admitted_addresses() {
    let addresses = interface_addresses().unwrap();
    assert!(
        addresses.iter().any(|a| a.loopback),
        "getifaddrs must report the loopback interface: {addresses:?}"
    );
    match this_mac() {
        Selection::Lan(address) => {
            assert!(LanPolicy::private_only().allows(address), "{address}");
            assert!(!address.is_loopback(), "{address}");
            assert!(
                addresses
                    .iter()
                    .any(|a| a.ip == address && a.up && !a.point_to_point && !a.loopback),
                "{address} is not an up, non-tunnel interface address of this Mac"
            );
        }
        Selection::LocalOnly(reason) => assert_eq!(reason, NO_PRIVATE_ADDRESS),
    }
}
