// SPDX-License-Identifier: AGPL-3.0-only
//! Whether macOS lets this app reach the local network yet (lane PHY item 2, ledger rows 79, 80).
//!
//! macOS 15+ asks the user once per app ("Local Network", `NSLocalNetworkUsageDescription`) at the
//! app's FIRST packet to another device on the local network, and offers no API to ask the answer.
//! The owner's first such packet used to be its step-6 dial, after the compile, with a 2 s connect
//! allowance: a fresh install lost one whole run on the owner's alert and one on the joined Mac's.
//!
//! How a pending or denied permission shows itself to the process, MEASURED on macOS 26.5.1
//! (25F80, Mac A) with a probe app of its own bundle id (`qualification/lanePHY/lnprobe`, one UDP
//! datagram to port 9 and one non-blocking TCP connect to port 7 per round, to given addresses):
//!
//! - permission not yet answered, peer on the LAN: `sendto` and `connect` fail AT ONCE with
//!   `EHOSTUNREACH` (65, "No route to host"), and `UserEventAgent` logs `Got local network blocked
//!   notification: pid: <pid>`;
//! - the same app and peer after the user allowed it: `sendto` succeeds, the connect stays in
//!   progress (the peer was asleep), and only once ARP gave up both fail `EHOSTDOWN` (64);
//! - this Mac's own address and the default router are not gated: `sendto` succeeds before any answer.
//!
//! So ONE datagram to a joined Mac's address tells the two apart, and that same datagram is what
//! raises the alert. A denied permission was not measured (it would need the user to deny on this
//! Mac); Apple documents the same `EHOSTUNREACH` for it. [`Probe`] is the seam; [`Udp`] the app's.
//!
//! The probe sends only to an address the app was already going to dial: a joined Mac's work
//! address, signed by that Mac's currently enrolled key (`member_control::joined::verify`), and only
//! when it is a private local-network address ([`is_local_network`]). Never a scan, a broadcast or
//! a multicast: the bundle's usage text promises that (`desktop::bundle::LOCAL_NETWORK_USAGE`).
use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr, UdpSocket};

/// Start or step 6 refused because this Mac (the owner's) may not reach the local network yet.
pub const PERMISSION_REQUIRED: &str = "local_network_permission_required";
/// Step 6's dial got no answer although this Mac may reach the local network: the joined Mac's own
/// Local Network alert is the first suspect (row 80), its network the second.
pub const JOINED_MAC_SILENT: &str = "joined_mac_did_not_answer";

/// What one datagram told about the permission.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Answer {
    /// The kernel took the datagram: this app may reach the local network.
    Sent,
    /// `EHOSTUNREACH` at once: the Local Network permission is pending or denied.
    Blocked,
    /// Not a private local-network address: nothing was sent, nothing learned.
    NotLocal,
    /// Any other failure (its errno): says nothing about the permission.
    Failed(i32),
}

/// One `send_to`'s result as an [`Answer`].
pub fn classify(sent: std::io::Result<usize>) -> Answer {
    match sent {
        Ok(_) => Answer::Sent,
        Err(e) if e.raw_os_error() == Some(libc::EHOSTUNREACH) => Answer::Blocked,
        Err(e) => Answer::Failed(e.raw_os_error().unwrap_or(0)),
    }
}

/// A unicast address on a private local network (RFC 1918, IPv4 link-local, IPv6 unique-local or
/// link-local): what the Local Network permission gates and what a joined Mac advertises. Loopback,
/// public, broadcast, multicast and unspecified addresses are not.
pub fn is_local_network(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => (v4.is_private() || v4.is_link_local()) && !v4.is_broadcast(),
        IpAddr::V6(v6) => {
            let first = v6.segments()[0];
            (first & 0xfe00) == 0xfc00 || (first & 0xffc0) == 0xfe80
        }
    }
}

/// Sends the one datagram (the seam a test replaces).
pub trait Probe: Send + Sync {
    fn send(&self, to: SocketAddr) -> Answer;
}

/// The app's probe: one one-byte UDP datagram from an unbound port to `to`. A QUIC endpoint drops
/// a datagram that short unread; the closed port's ICMP reply comes from the kernel, not the app.
pub struct Udp;
impl Probe for Udp {
    fn send(&self, to: SocketAddr) -> Answer {
        if !is_local_network(to.ip()) {
            return Answer::NotLocal;
        }
        let any: SocketAddr = match to {
            SocketAddr::V4(_) => (Ipv4Addr::UNSPECIFIED, 0).into(),
            SocketAddr::V6(_) => (Ipv6Addr::UNSPECIFIED, 0).into(),
        };
        match UdpSocket::bind(any) {
            Ok(socket) => classify(socket.send_to(&[0], to)),
            Err(e) => Answer::Failed(e.raw_os_error().unwrap_or(0)),
        }
    }
}

const SETTINGS: &str = "System Settings › Privacy & Security › Local Network";

/// The page message of [`PERMISSION_REQUIRED`]: which Mac, what to allow, where.
pub fn owner_message() -> String {
    format!(
        "macOS does not let Mycelium reach the joined Mac on your network yet. On THIS Mac (the \
         owner's), answer Allow on the alert asking whether Mycelium may find devices on your local \
         network, or turn Mycelium on in {SETTINGS}; then press Start again."
    )
}

/// The page message of [`JOINED_MAC_SILENT`] for the joined Mac at `ip` (`cause`: the dial's code).
pub fn joined_mac_message(ip: IpAddr, cause: &str) -> String {
    format!(
        "The joined Mac at {ip} did not answer this Mac ({cause}). If the joined Mac shows an alert \
         asking whether Mycelium may find devices on your local network, answer Allow THERE, or turn \
         Mycelium on in {SETTINGS} on the joined Mac; then press Start again. If it shows none, \
         check that both Macs are on the same network."
    )
}

/// The open dial failures whose cause can be a Local Network permission: a connect that never got
/// an answer (rows 79, 80: `control_call_failed:control_connect_timeout`, `Expired`).
pub fn dial_unanswered(cause: &str) -> bool {
    cause.contains("control_connect_timeout")
        || cause.contains("control_connect_failed")
        || cause == "Expired"
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Proves: the measured signature (EHOSTUNREACH = pending/denied; anything else is not it).
    #[test]
    fn only_an_immediate_host_unreachable_reads_as_a_blocked_permission() {
        let err = |n| Err(std::io::Error::from_raw_os_error(n));
        assert_eq!(classify(Ok(1)), Answer::Sent);
        assert_eq!(classify(err(libc::EHOSTUNREACH)), Answer::Blocked);
        assert_eq!(
            classify(err(libc::EHOSTDOWN)),
            Answer::Failed(libc::EHOSTDOWN)
        );
        assert_eq!(
            classify(err(libc::ENETUNREACH)),
            Answer::Failed(libc::ENETUNREACH)
        );
    }

    /// Proves: the probe never leaves the private local network (no loopback, public, broadcast or
    /// multicast address is sent anything).
    #[test]
    fn the_probe_sends_only_to_a_private_unicast_address() {
        for ip in [
            "10.160.112.128",
            "192.168.1.4",
            "172.16.0.9",
            "169.254.3.3",
            "fd00::5",
            "fe80::1",
        ] {
            assert!(is_local_network(ip.parse().unwrap()), "{ip}");
        }
        for ip in [
            "127.0.0.1",
            "8.8.8.8",
            "255.255.255.255",
            "224.0.0.251",
            "0.0.0.0",
            "::1",
            "2001:db8::1",
            "ff02::fb",
        ] {
            let ip: IpAddr = ip.parse().unwrap();
            assert!(!is_local_network(ip), "{ip}");
            assert_eq!(Udp.send((ip, 9).into()), Answer::NotLocal, "{ip}");
        }
    }

    /// Proves: the real probe on this Mac's own LAN address (not gated by macOS) is `Sent`, so the
    /// socket path works end to end. Needs MYCELIUM_LAN_IP; refuses to run without it.
    #[test]
    #[ignore = "needs this host's private LAN interface address in MYCELIUM_LAN_IP"]
    fn on_the_real_lan_one_datagram_to_this_macs_own_address_is_sent() {
        let ip: IpAddr = std::env::var("MYCELIUM_LAN_IP")
            .expect("MYCELIUM_LAN_IP")
            .parse()
            .unwrap();
        assert!(is_local_network(ip), "{ip}");
        assert_eq!(Udp.send((ip, 9).into()), Answer::Sent);
    }
}
