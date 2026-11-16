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
//! address in the current run, signed by that Mac's currently enrolled key
//! (`member_control::joined::verify`), admitted by the run's work transport (the caller's
//! `NetworkChoice::allows`), from the run's selected local address, and only when it is a private
//! local-network address ([`is_local_network`]). Never a scan, a broadcast or
//! a multicast: the bundle's usage text promises that (`desktop::bundle::LOCAL_NETWORK_USAGE`).
use std::net::{IpAddr, SocketAddr, UdpSocket};

/// Start or step 6 refused because a datagram from this Mac (the owner's) to a current joined Mac
/// failed at once with `EHOSTUNREACH`: Local Network not allowed yet, or no route (PHY-04).
pub const PERMISSION_REQUIRED: &str = "local_network_permission_required";
/// Step 6's dial got no answer although this Mac may reach the local network: the joined Mac's own
/// Local Network alert is the first suspect (row 80), its network the second.
pub const JOINED_MAC_SILENT: &str = "joined_mac_did_not_answer";

/// What one datagram told about the permission.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Answer {
    /// The kernel took the datagram: this app may reach the local network.
    Sent,
    /// `EHOSTUNREACH` at once ([`IMMEDIATE`]): the Local Network permission is pending or denied,
    /// or this Mac has no route to that address; on a fresh install the first is the likely cause.
    Blocked,
    /// Not a private local-network address: nothing was sent, nothing learned.
    NotLocal,
    /// Any other failure (its errno): says nothing about the permission.
    Failed(i32),
}

/// How soon a refused send must fail to read as the permission (PHY-04). The measured pending
/// permission failed every `sendto` at once (no packet left: the kernel's policy check answers
/// before any network I/O), while a send that waits on the network fails only after it (an ARP
/// resolution that gives up takes seconds, `EHOSTDOWN` in the measured allowed case). 100 ms is far
/// above the first and far below the second; it does not make `EHOSTUNREACH` exclusive to the
/// permission (an immediate routing refusal looks the same), which is why the refusal's message
/// names both causes ([`owner_message`]).
pub const IMMEDIATE: std::time::Duration = std::time::Duration::from_millis(100);

/// One `send_to`'s result, and how long it took, as an [`Answer`]: only an immediate
/// `EHOSTUNREACH` is evidence of the permission; a slow one is a network fault.
pub fn classify(sent: std::io::Result<usize>, elapsed: std::time::Duration) -> Answer {
    match sent {
        Ok(_) => Answer::Sent,
        Err(e) if e.raw_os_error() == Some(libc::EHOSTUNREACH) && elapsed < IMMEDIATE => {
            Answer::Blocked
        }
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
    /// One datagram from `from` (this run's selected local address) to `to`.
    fn send(&self, from: IpAddr, to: SocketAddr) -> Answer;
}

/// The app's probe: one one-byte UDP datagram from `from` (the run's selected local address, an
/// ephemeral port) to `to`. A QUIC endpoint drops a datagram that short unread; the closed port's
/// ICMP reply comes from the kernel, not the app. Nothing is sent to an address that is not a
/// private local-network one, or of another family than `from`.
pub struct Udp;
impl Probe for Udp {
    fn send(&self, from: IpAddr, to: SocketAddr) -> Answer {
        if !is_local_network(to.ip()) || from.is_ipv4() != to.is_ipv4() {
            return Answer::NotLocal;
        }
        match UdpSocket::bind(SocketAddr::new(from, 0)) {
            Ok(socket) => {
                // PHY-04: the elapsed time is recorded at the send itself.
                let begun = std::time::Instant::now();
                let sent = socket.send_to(&[0], to);
                let elapsed = begun.elapsed();
                if sent.is_err() {
                    eprintln!("local_network_send_failed:{to}:{}us", elapsed.as_micros());
                }
                classify(sent, elapsed)
            }
            Err(e) => Answer::Failed(e.raw_os_error().unwrap_or(0)),
        }
    }
}

const SETTINGS: &str = "System Settings › Privacy & Security › Local Network";

/// The page message of [`PERMISSION_REQUIRED`] for a datagram to the joined Mac at `ip` that
/// failed at once with `EHOSTUNREACH`: what the evidence shows (this Mac's permission OR no route
/// to that Mac), which one is likely, what to allow and where (PHY-04).
pub fn owner_message(ip: IpAddr) -> String {
    format!(
        "macOS has not allowed Mycelium to use the Local Network on THIS Mac (the owner's), or this \
         Mac has no route to the joined Mac at {ip}. On a fresh install the first is the likely \
         cause: answer Allow on the alert asking whether Mycelium may find devices on your local \
         network, or turn Mycelium on in {SETTINGS}; then press Start again. If Mycelium is already \
         on there, check that both Macs are on the same network."
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
    use std::net::{Ipv4Addr, Ipv6Addr};

    /// Proves: the measured signature (an IMMEDIATE EHOSTUNREACH = pending/denied; anything else
    /// is not it). PHY-04: a slow EHOSTUNREACH (an ARP wait, a router's ICMP answer) is a network
    /// fault, never read as the permission.
    #[test]
    fn only_an_immediate_host_unreachable_reads_as_a_blocked_permission() {
        use std::time::Duration;
        let err = |n| Err(std::io::Error::from_raw_os_error(n));
        let at_once = Duration::ZERO;
        assert_eq!(classify(Ok(1), at_once), Answer::Sent);
        assert_eq!(classify(err(libc::EHOSTUNREACH), at_once), Answer::Blocked);
        assert_eq!(
            classify(
                err(libc::EHOSTUNREACH),
                IMMEDIATE - Duration::from_millis(1)
            ),
            Answer::Blocked
        );
        for slow in [IMMEDIATE, Duration::from_secs(3)] {
            assert_eq!(
                classify(err(libc::EHOSTUNREACH), slow),
                Answer::Failed(libc::EHOSTUNREACH),
                "{slow:?}"
            );
        }
        assert_eq!(
            classify(err(libc::EHOSTDOWN), at_once),
            Answer::Failed(libc::EHOSTDOWN)
        );
        assert_eq!(
            classify(err(libc::ENETUNREACH), at_once),
            Answer::Failed(libc::ENETUNREACH)
        );
    }

    /// Proves PHY-04: the refusal names what the evidence shows, not a certainty: macOS has not
    /// allowed Local Network for Mycelium on this Mac OR this Mac has no route to the joined Mac,
    /// the first being the likely cause on a fresh install.
    #[test]
    fn the_permission_refusal_names_the_evidence_not_a_certainty() {
        let ip: IpAddr = "10.160.112.128".parse().unwrap();
        let message = owner_message(ip);
        for part in [
            "THIS Mac",
            "has not allowed",
            "Local Network",
            "or this Mac has no route to the joined Mac at 10.160.112.128",
            "likely",
            "fresh install",
            SETTINGS,
        ] {
            assert!(message.contains(part), "{part}: {message}");
        }
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
            let from = if ip.is_ipv4() {
                IpAddr::from(Ipv4Addr::UNSPECIFIED)
            } else {
                IpAddr::from(Ipv6Addr::UNSPECIFIED)
            };
            assert_eq!(Udp.send(from, (ip, 9).into()), Answer::NotLocal, "{ip}");
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
        assert_eq!(Udp.send(ip, (ip, 9).into()), Answer::Sent);
    }
}
