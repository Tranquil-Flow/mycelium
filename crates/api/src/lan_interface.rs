// SPDX-License-Identifier: AGPL-3.0-only
//! Which of this Mac's own interface addresses the packaged work transport binds (lane LAN).
//!
//! The direct-LAN carrier (`runtime::iroh_host::LoopbackHost::bind_lan`) needs ONE private interface
//! address. [`select`] is the pure rule over a list of interface addresses; [`this_mac`] reads the
//! list from the kernel (`getifaddrs(3)`) and reads the default route's source address from the
//! kernel's routing table (`sysctl(3)` `NET_RT_DUMP`, the table `netstat -rn` prints). Nothing here
//! opens a socket of any kind, sends a packet, resolves a name or contacts a peer: both reads copy
//! kernel tables into this process. (Round 1 asked the kernel with a UDP socket bound on 0.0.0.0 and
//! connected to 192.0.2.1, the one wildcard bind on the work path, REVIEW-laneLAN F5 / LAN-A5; it
//! is gone, so the work path has no exception to the no-wildcard rule of `runtime::iroh_host`.)
//!
//! The rule (owner ruling for this release: both Macs are on one ordinary private network), in
//! order:
//! 1. Candidates: interfaces that are up and running, not loopback and not point-to-point (VPN and
//!    tailnet tunnels such as `utun*` are point-to-point: a private address on a tunnel is not the
//!    network the other Mac shares), whose address is RFC 1918, IPv4 link-local (169.254/16) or
//!    IPv6 unique-local (fc00::/7), and which `LanPolicy::private_only` admits. Never a public,
//!    CGNAT/tailnet (100.64/10), loopback, unspecified, multicast or IPv6 link-local address (the
//!    last needs a scope id the bind does not carry).
//! 2. The candidate whose address is the source of the primary IPv4 default route wins: that is
//!    the network this Mac actually uses, so it is the one a second Mac on the same Wi-Fi or
//!    Ethernet shares.
//! 3. Otherwise (no default route, or its source is not a candidate: a full-tunnel VPN, Wi-Fi gone)
//!    no guess is made between networks. Bridges are set aside by their interface TYPE
//!    (`IFT_BRIDGE`, the `AF_LINK` entry's `if_data.ifi_type` in the same `getifaddrs` list, never
//!    the name): a VM or Internet Sharing bridge (`bridge100`, usually 192.168.64.1) is a network
//!    this Mac hosts, not one the other Mac is on. Exactly one remaining interface: its address
//!    (IPv4 before IPv6 unique-local on that one link; two addresses of the best class on it are
//!    still ambiguous). Several interfaces: [`AMBIGUOUS_PRIVATE_INTERFACES`]. Only bridges:
//!    [`ONLY_BRIDGE_INTERFACES`]. None: [`NO_PRIVATE_ADDRESS`].
//! 4. A refusal is [`Selection::LocalOnly`] with its reason: the transport stays on 127.0.0.1, and
//!    an owner run with a joined Mac refuses by that name before it dials anything.
use runtime::iroh_host::LanPolicy;
use std::{
    collections::BTreeSet,
    net::{IpAddr, Ipv4Addr, Ipv6Addr},
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
/// `RTF_IFSCOPE` (`<net/route.h>`): a route scoped to one interface, not the primary one.
const RTF_IFSCOPE: i32 = 0x0100_0000;
/// `RTAX_DST`, `RTAX_NETMASK`, `RTAX_IFA` (`<net/route.h>`): sockaddr slots of a route message.
const RTAX_DST: usize = 0;
const RTAX_NETMASK: usize = 2;
const RTAX_IFA: usize = 5;

/// RFC 1918 = 0, IPv4 link-local = 1, IPv6 unique-local = 2; anything else is not a candidate.
fn class(ip: IpAddr) -> Option<u8> {
    match ip {
        IpAddr::V4(v4) if v4.is_private() => Some(0),
        IpAddr::V4(v4) if v4.is_link_local() => Some(1),
        IpAddr::V6(v6) if (v6.segments()[0] & 0xfe00) == 0xfc00 => Some(2),
        _ => None,
    }
}

/// The pure selection rule (module note). `default_source` is the source address of the primary
/// IPv4 default route, if the routing table has one.
pub fn select(addresses: &[InterfaceAddress], default_source: Option<IpAddr>) -> Selection {
    let candidates: Vec<&InterfaceAddress> = addresses
        .iter()
        .filter(|a| a.up && !a.loopback && !a.point_to_point && admitted(a.ip))
        .filter(|a| class(a.ip).is_some())
        .collect();
    if let Some(source) = default_source
        && candidates.iter().any(|a| a.ip == source)
    {
        return Selection::Lan(source);
    }
    if candidates.is_empty() {
        return Selection::LocalOnly(NO_PRIVATE_ADDRESS);
    }
    let shared: Vec<&InterfaceAddress> = candidates.into_iter().filter(|a| !a.bridge).collect();
    let Some(first) = shared.first() else {
        return Selection::LocalOnly(ONLY_BRIDGE_INTERFACES);
    };
    if shared.iter().any(|a| a.name != first.name) {
        return Selection::LocalOnly(AMBIGUOUS_PRIVATE_INTERFACES);
    }
    // One network: its best address class, if exactly one address has it.
    let best = shared.iter().filter_map(|a| class(a.ip)).min();
    let mut best = shared.iter().filter(|a| class(a.ip) == best);
    match (best.next(), best.next()) {
        (Some(only), None) => Selection::Lan(only.ip),
        _ => Selection::LocalOnly(AMBIGUOUS_PRIVATE_INTERFACES),
    }
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

/// The source address of the primary IPv4 default route in a routing-table dump (the bytes of
/// `sysctl` `CTL_NET.PF_ROUTE.0.AF_INET.NET_RT_DUMP`: `rt_msghdr` records, each followed by the
/// sockaddrs its `rtm_addrs` names, every one padded to 4 bytes). The default route is the up,
/// unscoped route to 0.0.0.0 with a zero netmask; its interface address (`RTAX_IFA`) is the source
/// the kernel uses for it. None, or several that disagree: `None`. A truncated record ends the walk.
pub fn default_route_source_in(dump: &[u8]) -> Option<IpAddr> {
    let header = std::mem::size_of::<libc::rt_msghdr>();
    let int_at = |record: &[u8], offset: usize| -> Option<i32> {
        Some(i32::from_ne_bytes(
            record.get(offset..offset + 4)?.try_into().ok()?,
        ))
    };
    let mut found: Option<IpAddr> = None;
    let mut rest = dump;
    while rest.len() >= header {
        let length = usize::from(u16::from_ne_bytes([rest[0], rest[1]]));
        if length < header || length > rest.len() {
            break;
        }
        let (record, next) = rest.split_at(length);
        rest = next;
        if i32::from(record[2]) != libc::RTM_VERSION {
            continue;
        }
        let flags = int_at(record, std::mem::offset_of!(libc::rt_msghdr, rtm_flags))?;
        let present = int_at(record, std::mem::offset_of!(libc::rt_msghdr, rtm_addrs))?;
        if flags & libc::RTF_UP == 0 || flags & RTF_IFSCOPE != 0 {
            continue;
        }
        let mut slots: [Option<&[u8]>; 8] = [None; 8];
        let mut at = header;
        let mut whole = true;
        for (index, slot) in slots.iter_mut().enumerate() {
            if present & (1 << index) == 0 {
                continue;
            }
            let Some(sockaddr) = record
                .get(at)
                .and_then(|len| record.get(at..at + usize::from(*len)))
            else {
                whole = false;
                break;
            };
            *slot = Some(sockaddr);
            // route(8)'s `ROUNDUP`: padded to 4 bytes; a zero-length sockaddr still takes 4.
            at += match sockaddr.len() {
                0 => 4,
                len => 1 + ((len - 1) | 3),
            };
        }
        let mask_zero = slots[RTAX_NETMASK].is_some_and(|m| m.iter().skip(2).all(|b| *b == 0));
        let to_any = slots[RTAX_DST].and_then(sockaddr_ip) == Some(Ipv4Addr::UNSPECIFIED.into());
        let Some(source) = slots[RTAX_IFA].and_then(sockaddr_ip) else {
            continue;
        };
        if !(whole && mask_zero && to_any) {
            continue;
        }
        if found.is_some_and(|f| f != source) {
            return None;
        }
        found = Some(source);
    }
    found
}

/// The source address of this Mac's primary IPv4 default route, read from the kernel's routing
/// table ([`default_route_source_in`] over `sysctl` `NET_RT_DUMP`). No socket is opened.
#[allow(unsafe_code)] // sysctl(3): a size query, then a copy into a buffer this function owns
pub fn default_route_source() -> Option<IpAddr> {
    let mut mib = [
        libc::CTL_NET,
        libc::PF_ROUTE,
        0,
        libc::AF_INET,
        libc::NET_RT_DUMP,
        0,
    ];
    let names = libc::c_uint::try_from(mib.len()).ok()?;
    // The table can grow between the size query and the copy: retry with headroom.
    for _ in 0..4 {
        let mut size: libc::size_t = 0;
        // SAFETY: `mib` holds `names` ints; a null `oldp` with a valid `oldlenp` asks for the size
        // only; nothing is set (null `newp`, length 0).
        let asked = unsafe {
            libc::sysctl(
                mib.as_mut_ptr(),
                names,
                std::ptr::null_mut(),
                &mut size,
                std::ptr::null_mut(),
                0,
            )
        };
        if asked != 0 {
            return None;
        }
        let mut buffer = vec![0u8; size.saturating_add(size / 2).saturating_add(1024)];
        let mut filled: libc::size_t = buffer.len();
        // SAFETY: `buffer` is writable for `filled` bytes; the kernel writes at most that many and
        // stores the count written in `filled`; nothing is set (null `newp`, length 0).
        let copied = unsafe {
            libc::sysctl(
                mib.as_mut_ptr(),
                names,
                buffer.as_mut_ptr().cast(),
                &mut filled,
                std::ptr::null_mut(),
                0,
            )
        };
        if copied == 0 {
            buffer.truncate(filled);
            return default_route_source_in(&buffer);
        }
        if std::io::Error::last_os_error().raw_os_error() != Some(libc::ENOMEM) {
            return None;
        }
    }
    None
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
