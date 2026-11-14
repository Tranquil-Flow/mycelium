// SPDX-License-Identifier: AGPL-3.0-only
//! Which of this Mac's own interface addresses the packaged work transport binds (lane LAN).
//!
//! The direct-LAN carrier (`runtime::iroh_host::LoopbackHost::bind_lan`) needs ONE private interface
//! address. [`select`] is the pure rule over a list of interface addresses; [`this_mac`] reads the
//! list from the kernel (`getifaddrs(3)`) and asks the routing table which source address the
//! default route would use. Nothing here sends a packet, resolves a name or contacts a peer.
//!
//! The rule, in order:
//! 1. Candidates: interfaces that are up and running, not loopback and not point-to-point (VPN and
//!    tailnet tunnels such as `utun*` are point-to-point: a private address on a tunnel is not the
//!    network the other Mac shares), whose address is RFC 1918, IPv4 link-local (169.254/16) or
//!    IPv6 unique-local (fc00::/7), and which `LanPolicy::private_only` admits. Never a public,
//!    CGNAT/tailnet (100.64/10), loopback, unspecified, multicast or IPv6 link-local address (the
//!    last needs a scope id the bind does not carry).
//! 2. The candidate whose address is the default route's source wins: that is the network this Mac
//!    actually uses, so it is the one a second Mac on the same Wi-Fi or Ethernet shares.
//! 3. Otherwise: RFC 1918 before IPv4 link-local before IPv6 unique-local, then interface name, then
//!    address (byte order). The input order never matters.
//! 4. No candidate: [`Selection::LocalOnly`] with the reason; the transport stays on 127.0.0.1.
use runtime::iroh_host::LanPolicy;
use std::{
    collections::BTreeSet,
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, UdpSocket},
};

/// One address of one interface, as `getifaddrs(3)` reports it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct InterfaceAddress {
    pub name: String,
    pub ip: IpAddr,
    /// `IFF_UP` and `IFF_RUNNING`.
    pub up: bool,
    /// `IFF_LOOPBACK`.
    pub loopback: bool,
    /// `IFF_POINTOPOINT` (VPN and tailnet tunnels).
    pub point_to_point: bool,
    /// The interface's type is `IFT_BRIDGE` (`if_data.ifi_type` of its `AF_LINK` entry): a VM,
    /// Internet Sharing or Thunderbolt bridge.
    pub bridge: bool,
}

/// The work transport's network for this start.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Selection {
    /// Bind the direct-LAN carrier on this private interface address.
    Lan(IpAddr),
    /// Stay on 127.0.0.1 (`NetworkMode::LocalOnly`), for this reason.
    LocalOnly(&'static str),
}

/// No interface carries an admissible private address.
pub const NO_PRIVATE_ADDRESS: &str = "no_private_address";
/// The kernel's interface list could not be read.
pub const INTERFACES_UNREADABLE: &str = "interfaces_unreadable";
/// The default route is not on a private interface and more than one private network remains.
pub const AMBIGUOUS_PRIVATE_INTERFACES: &str = "ambiguous_private_interfaces";
/// The only private addresses are on bridges this Mac hosts (VMs, Internet Sharing).
pub const ONLY_BRIDGE_INTERFACES: &str = "only_bridge_interfaces";

/// `IFT_BRIDGE` (`<net/if_types.h>`: 0xd1, "Transparent bridge interface"); not in `libc`.
const IFT_BRIDGE: u8 = 0xd1;

/// RFC 1918 = 0, IPv4 link-local = 1, IPv6 unique-local = 2; anything else is not a candidate.
fn class(ip: IpAddr) -> Option<u8> {
    match ip {
        IpAddr::V4(v4) if v4.is_private() => Some(0),
        IpAddr::V4(v4) if v4.is_link_local() => Some(1),
        IpAddr::V6(v6) if (v6.segments()[0] & 0xfe00) == 0xfc00 => Some(2),
        _ => None,
    }
}

/// The pure selection rule (module note). `default_source` is the address the default route
/// would send from, if the routing table has one.
pub fn select(addresses: &[InterfaceAddress], default_source: Option<IpAddr>) -> Selection {
    addresses
        .iter()
        .filter(|a| a.up && !a.loopback && !a.point_to_point && admitted(a.ip))
        .filter_map(|a| Some((default_source != Some(a.ip), class(a.ip)?, &a.name, a.ip)))
        .min()
        .map_or(Selection::LocalOnly(NO_PRIVATE_ADDRESS), |(.., ip)| {
            Selection::Lan(ip)
        })
}

/// The IP address of a raw `sockaddr_in` / `sockaddr_in6` (`sa_len`, `sa_family`, then the
/// family's fields in network byte order), or `None` for any other family or a short buffer.
pub fn sockaddr_ip(sockaddr: &[u8]) -> Option<IpAddr> {
    let len = usize::from(*sockaddr.first()?);
    let family = i32::from(*sockaddr.get(1)?);
    let bytes = sockaddr.get(..len)?;
    match family {
        libc::AF_INET => {
            let octets: [u8; 4] = bytes.get(4..8)?.try_into().ok()?;
            Some(IpAddr::V4(Ipv4Addr::from(octets)))
        }
        libc::AF_INET6 => {
            let octets: [u8; 16] = bytes.get(8..24)?.try_into().ok()?;
            Some(IpAddr::V6(Ipv6Addr::from(octets)))
        }
        _ => None,
    }
}

/// The source address of the primary IPv4 default route in a routing-table dump (RED scaffold:
/// not read yet).
pub fn default_route_source_in(_dump: &[u8]) -> Option<IpAddr> {
    None
}

/// The source address the kernel would use for the default route, or none. A UDP `connect` only
/// consults the routing table: it sends no datagram. The destination is TEST-NET-1 (RFC 5737,
/// 192.0.2.1), reserved for documentation and never assigned, so no local route can match it
/// more specifically than the default route.
pub fn default_route_source() -> Option<IpAddr> {
    let socket = UdpSocket::bind(SocketAddr::from((Ipv4Addr::UNSPECIFIED, 0))).ok()?;
    socket
        .connect(SocketAddr::from((Ipv4Addr::new(192, 0, 2, 1), 9)))
        .ok()?;
    let ip = socket.local_addr().ok()?.ip();
    (!ip.is_unspecified()).then_some(ip)
}

/// Every address of every interface of this Mac (`getifaddrs(3)`), IPv4 and IPv6 only, each
/// marked with its interface's type read from that interface's `AF_LINK` entry in the same list.
pub fn interface_addresses() -> Result<Vec<InterfaceAddress>, &'static str> {
    read_interfaces().map(|(addresses, _)| addresses)
}

/// The names of this Mac's bridge interfaces (`IFT_BRIDGE`), with or without an address.
pub fn bridge_interfaces() -> Result<BTreeSet<String>, &'static str> {
    read_interfaces().map(|(_, bridges)| bridges)
}

/// One `getifaddrs(3)` snapshot: the IP addresses and the bridge interface names.
#[allow(unsafe_code)] // getifaddrs(3)/freeifaddrs(3): a list the kernel allocates and we free once
fn read_interfaces() -> Result<(Vec<InterfaceAddress>, BTreeSet<String>), &'static str> {
    let mut head: *mut libc::ifaddrs = std::ptr::null_mut();
    // SAFETY: `head` is a valid out-pointer; on success it owns a list freed below exactly once.
    if unsafe { libc::getifaddrs(&mut head) } != 0 {
        return Err(INTERFACES_UNREADABLE);
    }
    let mut out = Vec::new();
    let mut bridges = BTreeSet::new();
    let mut cursor = head;
    while !cursor.is_null() {
        // SAFETY: `cursor` is a node of the list getifaddrs returned and has not been freed.
        let entry = unsafe { &*cursor };
        cursor = entry.ifa_next;
        if entry.ifa_addr.is_null() || entry.ifa_name.is_null() {
            continue;
        }
        // SAFETY: `ifa_name` is a non-null NUL-terminated interface name owned by the list.
        let name = unsafe { std::ffi::CStr::from_ptr(entry.ifa_name) }
            .to_string_lossy()
            .into_owned();
        // SAFETY: `ifa_addr` is non-null and points to a sockaddr of `sa_len` bytes (its first
        // two bytes are `sa_len` and `sa_family`), owned by the list.
        let sockaddr = unsafe {
            let length = usize::from((*entry.ifa_addr).sa_len);
            std::slice::from_raw_parts(entry.ifa_addr.cast::<u8>(), length)
        };
        if sockaddr.get(1).map(|f| i32::from(*f)) == Some(libc::AF_LINK) {
            // The link entry's `ifa_data` is the interface's `if_data`; its `ifi_type` is the
            // interface type. (A bridge's `sockaddr_dl.sdl_type` says IFT_ETHER on macOS 26.)
            if !entry.ifa_data.is_null() {
                // SAFETY: for an AF_LINK entry `ifa_data` points to the list's `struct if_data`.
                let data = unsafe { &*entry.ifa_data.cast::<libc::if_data>() };
                if data.ifi_type == IFT_BRIDGE {
                    bridges.insert(name);
                }
            }
            continue;
        }
        let Some(ip) = sockaddr_ip(sockaddr) else {
            continue;
        };
        let flags = entry.ifa_flags;
        let has = |flag: i32| flags & flag as u32 != 0;
        out.push(InterfaceAddress {
            name,
            ip,
            up: has(libc::IFF_UP) && has(libc::IFF_RUNNING),
            loopback: has(libc::IFF_LOOPBACK),
            point_to_point: has(libc::IFF_POINTOPOINT),
            bridge: false,
        });
    }
    // SAFETY: `head` came from a successful getifaddrs and is freed once; nothing borrowed from
    // the list outlives it (names and addresses were copied).
    unsafe { libc::freeifaddrs(head) };
    for address in &mut out {
        address.bridge = bridges.contains(&address.name);
    }
    Ok((out, bridges))
}

/// [`select`] over this Mac's interfaces and default route, read now.
pub fn this_mac() -> Selection {
    match interface_addresses() {
        Ok(addresses) => select(&addresses, default_route_source()),
        Err(reason) => Selection::LocalOnly(reason),
    }
}

/// True when the direct-LAN policy the packaged transport binds with admits `ip`.
pub fn admitted(ip: IpAddr) -> bool {
    LanPolicy::private_only().allows(ip)
}
