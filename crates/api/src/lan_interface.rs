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
use std::net::{IpAddr, Ipv4Addr, SocketAddr, UdpSocket};

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

/// Every address of every interface of this Mac (`getifaddrs(3)`), IPv4 and IPv6 only.
#[allow(unsafe_code)] // getifaddrs(3)/freeifaddrs(3): a list the kernel allocates and we free once
pub fn interface_addresses() -> Result<Vec<InterfaceAddress>, &'static str> {
    let mut head: *mut libc::ifaddrs = std::ptr::null_mut();
    // SAFETY: `head` is a valid out-pointer; on success it owns a list freed below exactly once.
    if unsafe { libc::getifaddrs(&mut head) } != 0 {
        return Err(INTERFACES_UNREADABLE);
    }
    let mut out = Vec::new();
    let mut cursor = head;
    while !cursor.is_null() {
        // SAFETY: `cursor` is a node of the list getifaddrs returned and has not been freed.
        let entry = unsafe { &*cursor };
        cursor = entry.ifa_next;
        if entry.ifa_addr.is_null() || entry.ifa_name.is_null() {
            continue;
        }
        // SAFETY: `ifa_addr` is non-null and points to a sockaddr whose family says its layout.
        let ip = unsafe {
            match i32::from((*entry.ifa_addr).sa_family) {
                libc::AF_INET => {
                    let v4 = &*entry.ifa_addr.cast::<libc::sockaddr_in>();
                    IpAddr::from(Ipv4Addr::from(u32::from_be(v4.sin_addr.s_addr)))
                }
                libc::AF_INET6 => {
                    let v6 = &*entry.ifa_addr.cast::<libc::sockaddr_in6>();
                    IpAddr::from(v6.sin6_addr.s6_addr)
                }
                _ => continue,
            }
        };
        // SAFETY: `ifa_name` is a non-null NUL-terminated interface name owned by the list.
        let name = unsafe { std::ffi::CStr::from_ptr(entry.ifa_name) }
            .to_string_lossy()
            .into_owned();
        let flags = entry.ifa_flags;
        let has = |flag: i32| flags & flag as u32 != 0;
        out.push(InterfaceAddress {
            name,
            ip,
            up: has(libc::IFF_UP) && has(libc::IFF_RUNNING),
            loopback: has(libc::IFF_LOOPBACK),
            point_to_point: has(libc::IFF_POINTOPOINT),
        });
    }
    // SAFETY: `head` came from a successful getifaddrs and is freed once; no reference outlives it.
    unsafe { libc::freeifaddrs(head) };
    Ok(out)
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
