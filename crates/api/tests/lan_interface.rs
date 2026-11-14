// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged work transport's address rule (`api::lan_interface::select`, lane LAN): only a
//! private LAN address the direct-LAN policy admits, never a public, CGNAT/tailnet, loopback or
//! tunnel address; the default route's source first; otherwise one non-bridge private network or a
//! named refusal, never a guess; deterministic whatever the input order. The kernel readings are
//! checked through pure decoders (raw sockaddr bytes, a routing-table dump) and against this Mac's
//! own `route`/`ipconfig`/`ifconfig` answers.
use api::lan_interface::{
    AMBIGUOUS_PRIVATE_INTERFACES, InterfaceAddress, NO_PRIVATE_ADDRESS, ONLY_BRIDGE_INTERFACES,
    Selection, bridge_interfaces, default_route_source, default_route_source_in,
    interface_addresses, select, sockaddr_ip, this_mac,
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
        bridge: false,
    }
}
fn tunnel(name: &str, address: &str) -> InterfaceAddress {
    InterfaceAddress {
        point_to_point: true,
        ..up(name, address)
    }
}
/// A bridge this Mac hosts (VM / Internet Sharing `bridge100`, Thunderbolt `bridge0`).
fn bridge(name: &str, address: &str) -> InterfaceAddress {
    InterfaceAddress {
        bridge: true,
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

/// Proves: LAN-A2 (one private Wi-Fi address is chosen, with or without a default route).
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

/// Proves: LAN-A2 (never a public, CGNAT/tailnet, loopback, tunnel or down address).
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

/// Proves: LAN-A2 (rule a: the default route's source wins over other private interfaces,
/// a bridge included, when it is a candidate).
#[test]
fn the_default_routes_source_wins_over_other_private_interfaces() {
    // Internet sharing's bridge sorts before en0 by name; the default route is on en0.
    let addresses = [
        bridge("bridge100", "192.168.2.1"),
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
    // The default route itself on a bridge (this Mac reaches its network through it): rule a.
    assert_eq!(
        chosen(&addresses, Some(ip("192.168.2.1"))),
        Selection::Lan(ip("192.168.2.1"))
    );
}

/// Proves: LAN-A2 (rule b: with the default route on a tunnel or outside the candidates, the one
/// remaining private network is used).
#[test]
fn a_default_route_that_is_not_a_candidate_falls_back_to_the_one_private_network() {
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

/// Proves: LAN-A2 = F1 (the reviewers' exact cases: a VM or Internet Sharing bridge never wins
/// by name; a Mac with only a bridge refuses by name).
#[test]
fn a_vm_or_internet_sharing_bridge_never_wins_by_name() {
    // F1 inputs: bridge100 (Virtualization.framework / Internet Sharing) and Wi-Fi, under a
    // full-tunnel VPN (default source on the tunnel) and with no default route at all.
    let vpn = [
        bridge("bridge100", "192.168.64.1"),
        up("en0", "192.168.1.23"),
        tunnel("utun4", "10.8.0.2"),
    ];
    assert_eq!(
        chosen(&vpn, Some(ip("10.8.0.2"))),
        Selection::Lan(ip("192.168.1.23"))
    );
    assert_eq!(chosen(&vpn, None), Selection::Lan(ip("192.168.1.23")));
    // Wi-Fi down at start, only the VM bridge up: no network the other Mac is on.
    assert_eq!(
        chosen(&[bridge("bridge100", "192.168.64.1")], None),
        Selection::LocalOnly(ONLY_BRIDGE_INTERFACES)
    );
    assert_eq!(
        chosen(
            &[
                bridge("bridge100", "192.168.64.1"),
                tunnel("utun4", "10.8.0.2")
            ],
            Some(ip("10.8.0.2"))
        ),
        Selection::LocalOnly(ONLY_BRIDGE_INTERFACES)
    );
    // Wi-Fi self-assigned plus the Thunderbolt bridge, no default route: bridge0 sorted first by
    // name before; the Wi-Fi link-local address now.
    assert_eq!(
        chosen(
            &[bridge("bridge0", "169.254.200.1"), up("en0", "169.254.7.9")],
            None
        ),
        Selection::Lan(ip("169.254.7.9"))
    );
}

/// Proves: LAN-A2 (rule c: two private networks and no usable default route is a named refusal,
/// never a choice by interface name, class or address).
#[test]
fn two_private_networks_without_a_usable_default_route_are_refused_by_name() {
    let ambiguous = Selection::LocalOnly(AMBIGUOUS_PRIVATE_INTERFACES);
    // Wi-Fi and an iPhone USB tether, the default route on a VPN.
    assert_eq!(
        chosen(
            &[
                up("en0", "192.168.1.23"),
                up("en7", "172.20.10.2"),
                tunnel("utun4", "10.8.0.2")
            ],
            Some(ip("10.8.0.2"))
        ),
        ambiguous
    );
    assert_eq!(
        chosen(&[up("en5", "169.254.3.4"), up("en7", "192.168.1.9")], None),
        ambiguous
    );
    assert_eq!(
        chosen(&[up("en0", "fd00::5"), up("en9", "169.254.3.4")], None),
        ambiguous
    );
    assert_eq!(
        chosen(&[up("en1", "10.0.0.2"), up("en0", "192.168.1.9")], None),
        ambiguous
    );
    // Two addresses of the same class on one interface: which the other Mac reaches is unknown.
    assert_eq!(
        chosen(&[up("en0", "192.168.1.9"), up("en0", "10.0.0.2")], None),
        ambiguous
    );
    assert_eq!(
        chosen(&[up("en0", "fd00::5"), up("en0", "fd00::6")], None),
        ambiguous
    );
}

/// Proves: LAN-R2-A2 (two private IPv4 addresses on ONE interface are as ambiguous as two
/// interfaces, whatever their classes: an RFC 1918 alias beside a link-local address may be a
/// network the other Mac does not share, so neither wins by class).
#[test]
fn two_ipv4_addresses_on_one_interface_without_a_usable_default_route_are_refused_by_name() {
    let ambiguous = Selection::LocalOnly(AMBIGUOUS_PRIVATE_INTERFACES);
    for addresses in [
        [up("en0", "169.254.3.4"), up("en0", "10.0.0.7")],
        [up("en0", "10.0.0.7"), up("en0", "169.254.3.4")],
        [up("en0", "10.0.0.7"), up("en0", "192.168.1.9")],
    ] {
        assert_eq!(chosen(&addresses, None), ambiguous, "{addresses:?}");
    }
    // Still refused with IPv6 unique-local addresses beside them.
    assert_eq!(
        chosen(
            &[
                up("en0", "169.254.3.4"),
                up("en0", "fd00::5"),
                up("en0", "10.0.0.7")
            ],
            None
        ),
        ambiguous
    );
}

/// Proves: LAN-A2 and LAN-R2-A2 (one interface with ONE IPv4 address and IPv6 unique-local ones:
/// the IPv4 address, a family preference of the one-family transport, not proof of one network).
#[test]
fn one_network_prefers_ipv4_over_ipv6_unique_local() {
    assert_eq!(
        chosen(&[up("en0", "fd12:3456::7")], None),
        Selection::Lan(ip("fd12:3456::7"))
    );
    assert_eq!(
        chosen(&[up("en0", "fd00::5"), up("en0", "192.168.178.20")], None),
        Selection::Lan(ip("192.168.178.20"))
    );
    assert_eq!(
        chosen(&[up("en0", "169.254.3.4"), up("en0", "fd00::5")], None),
        Selection::Lan(ip("169.254.3.4"))
    );
    assert_eq!(
        chosen(
            &[
                up("en0", "fd00::5"),
                up("en0", "192.168.178.20"),
                up("en0", "fd00::6")
            ],
            None
        ),
        Selection::Lan(ip("192.168.178.20"))
    );
}

/// Proves: LAN-A2 (the answer does not depend on the order the kernel lists interfaces in).
#[test]
fn the_choice_does_not_depend_on_the_interface_order() {
    let addresses = vec![
        up("en0", "fd00::5"),
        bridge("bridge100", "192.168.2.1"),
        up("en0", "192.168.1.23"),
        tunnel("utun5", "100.84.252.4"),
        up("en5", "169.254.10.3"),
    ];
    for default_source in [
        None,
        Some(ip("192.168.1.23")),
        Some(ip("100.84.252.4")),
        Some(ip("192.168.2.1")),
    ] {
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

/// A `sockaddr_in` as the kernel lays it out: length, family, port and address in network order.
fn sockaddr_in(octets: [u8; 4], port: u16) -> Vec<u8> {
    let mut out = vec![16, libc::AF_INET as u8];
    out.extend(port.to_be_bytes());
    out.extend(octets);
    out.extend([0; 8]);
    out
}

/// Proves: F3 (the kernel's network-order bytes become the address they spell; a byte-order slip
/// reads 10.160.112.32 as the public 32.112.160.10 and is caught here, without any host state).
#[test]
fn raw_sockaddr_bytes_decode_in_network_order() {
    assert_eq!(
        sockaddr_ip(&sockaddr_in([10, 160, 112, 32], 61700)),
        Some(ip("10.160.112.32"))
    );
    assert_eq!(
        sockaddr_ip(&sockaddr_in([192, 168, 64, 1], 0)),
        Some(ip("192.168.64.1"))
    );
    let mut v6 = vec![28, libc::AF_INET6 as u8, 0x1f, 0x90, 0, 0, 0, 0];
    v6.extend(
        "fd12:3456::7"
            .parse::<std::net::Ipv6Addr>()
            .unwrap()
            .octets(),
    );
    v6.extend([0, 0, 0, 4]);
    assert_eq!(sockaddr_ip(&v6), Some(ip("fd12:3456::7")));
    // An AF_LINK entry, a truncated sockaddr, an empty one: no address.
    let link = [20, libc::AF_LINK as u8, 14, 0, 6, 3, 6, 0];
    assert_eq!(sockaddr_ip(&link), None);
    assert_eq!(sockaddr_ip(&sockaddr_in([10, 0, 0, 1], 9)[..6]), None);
    assert_eq!(sockaddr_ip(&[]), None);
}

const RTF_UP: i32 = 0x1;
const RTF_GATEWAY: i32 = 0x2;
const RTF_IFSCOPE: i32 = 0x0100_0000;
/// One `rt_msghdr` record of a `NET_RT_DUMP` with the sockaddrs at slots DST(0), GATEWAY(1),
/// NETMASK(2) and IFA(5), each padded to 4 bytes as the kernel writes them.
fn route(version: u8, flags: i32, dst: [u8; 4], mask: Option<[u8; 4]>, ifa: [u8; 4]) -> Vec<u8> {
    let header = std::mem::size_of::<libc::rt_msghdr>();
    let mut body = sockaddr_in(dst, 0);
    body.extend(sockaddr_in([10, 160, 112, 78], 0));
    let mut present = 0b10_0011;
    match mask {
        // A /0 mask is a zero-length sockaddr: 4 bytes of padding.
        Some([0, 0, 0, 0]) => {
            body.extend([0; 4]);
            present |= 0b100;
        }
        Some(octets) => {
            body.extend([8, 0xff, 0, 0]);
            body.extend(octets);
            present |= 0b100;
        }
        None => {}
    }
    body.extend(sockaddr_in(ifa, 0));
    let mut record = vec![0u8; header];
    let length = u16::try_from(header + body.len()).unwrap();
    record[..2].copy_from_slice(&length.to_ne_bytes());
    record[2] = version;
    record[3] = 4; // RTM_GET
    let flags_at = std::mem::offset_of!(libc::rt_msghdr, rtm_flags);
    let addrs_at = std::mem::offset_of!(libc::rt_msghdr, rtm_addrs);
    record[flags_at..flags_at + 4].copy_from_slice(&flags.to_ne_bytes());
    record[addrs_at..addrs_at + 4].copy_from_slice(&i32::to_ne_bytes(present));
    record.extend(body);
    record
}

/// Proves: LAN-A5 = F5 (the default route's source comes from the routing table itself: the
/// primary, unscoped, up route to 0.0.0.0/0 and its interface address; scoped VPN defaults, host
/// routes and other prefixes are not it; malformed records never panic).
#[test]
fn the_default_routes_source_is_read_from_a_routing_table_dump() {
    let v = libc::RTM_VERSION as u8;
    let zero = Some([0, 0, 0, 0]);
    let primary = route(v, RTF_UP | RTF_GATEWAY, [0; 4], zero, [10, 160, 112, 32]);
    let scoped_vpn = route(v, RTF_UP | RTF_IFSCOPE, [0; 4], zero, [100, 84, 252, 4]);
    let subnet = route(
        v,
        RTF_UP,
        [10, 160, 112, 0],
        Some([255, 255, 255, 0]),
        [10, 160, 112, 32],
    );
    let host = route(v, RTF_UP, [0; 4], None, [192, 168, 9, 9]);
    let table = [scoped_vpn.clone(), subnet.clone(), primary.clone(), host].concat();
    assert_eq!(default_route_source_in(&table), Some(ip("10.160.112.32")));
    // No primary default route (Wi-Fi gone, only the VPN's scoped one): none.
    assert_eq!(
        default_route_source_in(&[scoped_vpn.clone(), subnet.clone()].concat()),
        None
    );
    // A route that is not up, or a record of another version, is not the default route.
    let down = route(v, RTF_GATEWAY, [0; 4], zero, [192, 168, 1, 2]);
    let old = route(v - 1, RTF_UP, [0; 4], zero, [192, 168, 1, 3]);
    assert_eq!(default_route_source_in(&[down, old].concat()), None);
    // Two primary defaults that disagree: no guess.
    let other = route(v, RTF_UP | RTF_GATEWAY, [0; 4], zero, [192, 168, 1, 23]);
    assert_eq!(
        default_route_source_in(&[primary.clone(), other].concat()),
        None
    );
    // Truncated input ends the walk without panicking.
    for cut in [0, 1, 5, primary.len() - 1] {
        assert_eq!(default_route_source_in(&primary[..cut]), None, "cut {cut}");
    }
    assert_eq!(
        default_route_source_in(&[primary.clone(), primary[..7].to_vec()].concat()),
        Some(ip("10.160.112.32"))
    );
}

/// `route -n get default`'s interface, and that interface's IPv4 address from `ipconfig`: this
/// Mac's answer from tools that share no code with `api::lan_interface`.
fn default_route_by_the_system_tools() -> Option<(String, IpAddr)> {
    let route = std::process::Command::new("/sbin/route")
        .args(["-n", "get", "default"])
        .output()
        .unwrap();
    let text = String::from_utf8(route.stdout).unwrap();
    let interface = text
        .lines()
        .find_map(|l| l.trim().strip_prefix("interface: "))?
        .to_owned();
    let address = std::process::Command::new("/usr/sbin/ipconfig")
        .args(["getifaddr", &interface])
        .output()
        .unwrap();
    let address = String::from_utf8(address.stdout).unwrap();
    Some((interface, address.trim().parse().ok()?))
}

/// One interface as `ifconfig -a` prints it.
struct Shown {
    name: String,
    /// `UP` and `RUNNING` among its flags.
    up: bool,
    loopback: bool,
    point_to_point: bool,
    addresses: Vec<IpAddr>,
}
/// This Mac's interfaces as `/sbin/ifconfig -a` prints them: an answer that shares no code with
/// `api::lan_interface` (no `getifaddrs` reader of ours).
fn interfaces_by_ifconfig() -> Vec<Shown> {
    let out = std::process::Command::new("/sbin/ifconfig")
        .arg("-a")
        .output()
        .unwrap();
    assert!(out.status.success(), "ifconfig -a");
    let text = String::from_utf8(out.stdout).unwrap();
    let mut shown: Vec<Shown> = Vec::new();
    for line in text.lines() {
        if !line.starts_with(['\t', ' ']) {
            // `en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500`
            let Some((name, rest)) = line.split_once(": flags=") else {
                continue;
            };
            let flags = rest
                .split_once('<')
                .and_then(|(_, f)| f.split_once('>'))
                .map_or("", |(f, _)| f);
            let has = |flag: &str| flags.split(',').any(|f| f == flag);
            shown.push(Shown {
                name: name.to_owned(),
                up: has("UP") && has("RUNNING"),
                loopback: has("LOOPBACK"),
                point_to_point: has("POINTOPOINT"),
                addresses: Vec::new(),
            });
        } else if let Some(current) = shown.last_mut() {
            let mut words = line.split_whitespace();
            let address = match words.next() {
                Some("inet" | "inet6") => words.next(),
                _ => None,
            };
            if let Some(address) = address
                .and_then(|a| a.split('%').next())
                .and_then(|a| a.parse().ok())
            {
                current.addresses.push(address);
            }
        }
    }
    shown
}
/// An address the rule may choose, by the address alone: RFC 1918, IPv4 link-local or IPv6
/// unique-local.
fn choosable(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => v4.is_private() || v4.is_link_local(),
        IpAddr::V6(v6) => (v6.segments()[0] & 0xfe00) == 0xfc00,
    }
}

/// Proves: F3, F8 (d) and LAN-R2-A3 (this host's own reading, decided from the OS's own tools,
/// never from the reader under test: every private address `ifconfig` shows on an up,
/// non-loopback, non-tunnel interface must be in `interface_addresses()`; when `route`/`ipconfig`
/// put the default route on one of them, the work transport binds exactly that address, whatever
/// the interface's name; loopback is accepted only as the rule's named answer, and
/// `no_private_address` only on a Mac that shows no private address at all).
#[test]
fn this_macs_choice_is_its_default_routes_private_address() {
    let shown = interfaces_by_ifconfig();
    assert!(
        shown.iter().any(|i| i.loopback),
        "ifconfig must show the loopback interface"
    );
    let eligible: Vec<(String, IpAddr)> = shown
        .iter()
        .filter(|i| i.up && !i.loopback && !i.point_to_point)
        .flat_map(|i| {
            i.addresses
                .iter()
                .filter(|a| choosable(**a))
                .map(|a| (i.name.clone(), *a))
        })
        .collect();
    let system = default_route_by_the_system_tools();
    let read = interface_addresses().unwrap();
    let choice = this_mac();
    eprintln!("this Mac: default route {system:?}, private {eligible:?}, choice {choice:?}");
    for (name, address) in &eligible {
        assert!(
            read.iter().any(|a| &a.name == name
                && a.ip == *address
                && a.up
                && !a.loopback
                && !a.point_to_point),
            "ifconfig shows {name} {address}, the interface reader does not: {read:?}"
        );
    }
    match system.filter(|default| eligible.contains(default)) {
        // The OS puts the default route on a private, non-tunnel interface: this Mac MUST have
        // a LAN answer, and it is that address.
        Some((_, address)) => {
            assert_eq!(default_route_source(), Some(address));
            assert_eq!(choice, Selection::Lan(address));
            if let Ok(lan) = std::env::var("MYCELIUM_LAN_IP")
                && lan == address.to_string()
            {
                assert_eq!(choice, Selection::Lan(ip(&lan)));
            }
        }
        // No private address at all: the only Mac that may answer "no private address".
        None if eligible.is_empty() => {
            assert_eq!(choice, Selection::LocalOnly(NO_PRIVATE_ADDRESS));
        }
        // Private addresses but no private default route: one of them by the fallback rule, or
        // the rule's named refusal between them; never "no private address".
        None => match choice {
            Selection::Lan(address) => assert!(
                eligible.iter().any(|(_, a)| *a == address),
                "{address} is not a private address ifconfig shows"
            ),
            Selection::LocalOnly(reason) => assert!(
                [AMBIGUOUS_PRIVATE_INTERFACES, ONLY_BRIDGE_INTERFACES].contains(&reason),
                "{reason}"
            ),
        },
    }
}

/// Proves: LAN-A2 (the interface TYPE is read from the kernel: every interface `ifconfig` shows
/// with a bridge configuration is a bridge to `api::lan_interface`, and no other one is).
#[test]
fn bridges_are_recognised_by_type_not_by_name() {
    let names = std::process::Command::new("/sbin/ifconfig")
        .arg("-l")
        .output()
        .unwrap();
    let names = String::from_utf8(names.stdout).unwrap();
    let mut expected = std::collections::BTreeSet::new();
    for name in names.split_whitespace() {
        let shown = std::process::Command::new("/sbin/ifconfig")
            .arg(name)
            .output()
            .unwrap();
        let shown = String::from_utf8(shown.stdout).unwrap();
        if shown.contains("\tConfiguration:\n") && shown.contains("root id") {
            expected.insert(name.to_owned());
        }
    }
    eprintln!("bridges on this Mac: {expected:?}");
    assert_eq!(bridge_interfaces().unwrap(), expected);
    for address in interface_addresses().unwrap() {
        assert_eq!(
            address.bridge,
            expected.contains(&address.name),
            "{address:?}"
        );
    }
}
