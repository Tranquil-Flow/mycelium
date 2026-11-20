// SPDX-License-Identifier: AGPL-3.0-only
//! Across networks (lane XB2, ruling 29 A+B): the endpoint shape and peer gate of a direct-LAN
//! choice whose [`LanPolicy`] crosses networks ([`LanPolicy::across_networks`],
//! [`NetworkMode::CrossNetwork`]). A deliberate per-run choice: a same-network run binds exactly
//! today's direct-LAN carrier.
//!
//! Shape: ONE iroh IP socket on the run's interface address (never a wildcard), Mycelium's relay
//! as the only relay, no address lookup (no n0 relay, DNS or pkarr; peers come from signed
//! documents), portmapping only when the run's policy asks for it (lane XA). iroh then picks the
//! path itself: the relay as rendezvous and fallback, a hole-punched (or portmapped, or same-LAN)
//! direct path once one validates (`iroh-1.0.2/src/lib.rs:66-72`).
//!
//! What stays from the direct-LAN carrier's protections, and what does not:
//! - Peer keys: a host that set its admitted keys ([`CrossGate::set_keys`], from signed data)
//!   closes any other key's handshake (`LAN_KEY_REFUSED`) before handing it to a caller, and
//!   counts it (`lan_dropped_datagrams`); without keys the caller verifies the key, exactly as
//!   on the LAN.
//! - Dial targets: a peer is dialled only by its endpoint id from signed data, at its signed
//!   addresses (any unicast class now; never unspecified, multicast, broadcast or port 0) plus
//!   this host's OWN relay; anything else is refused before any packet and counted
//!   (`lan_refused_datagrams`, the `refused_out` of the logs).
//! - Relaxed: the address class, and with it the pre-send per-datagram filter
//!   (`iroh_lan_filter`), which cannot sit under iroh's own IP transport. Hole punching sends
//!   QUIC path probes to candidates an AUTHENTICATED peer announces in-band
//!   (`noq-proto n0_nat_traversal`, bounded attempts per round); that is the B8f surface, now
//!   reachable by an admitted key. An unadmitted key's connection is closed by the gate above.
use crate::iroh_host::LanPolicy;
use iroh::{
    Endpoint, EndpointAddr, EndpointId, RelayMode,
    endpoint::{PortmapperConfig, presets},
};
use std::{
    net::{IpAddr, SocketAddr},
    sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    },
};
use transport::Error;

/// The builder of an endpoint across networks: Mycelium's relay (the policy's), no address
/// lookup, one IP socket on `interface`, the router asked for a port mapping only when the run
/// chose it (lane XA, default off).
pub(crate) fn cross_builder(
    interface: IpAddr,
    policy: &LanPolicy,
) -> Result<iroh::endpoint::Builder, Error> {
    let relay = policy.relay().ok_or(Error::Unavailable)?;
    Endpoint::builder(presets::Minimal)
        .clear_address_lookup()
        .relay_mode(RelayMode::Custom(iroh::RelayMap::from(relay.clone())))
        .clear_ip_transports()
        .bind_addr(SocketAddr::new(interface, 0))
        .map(|builder| builder.path_selector(std::sync::Arc::new(RelayFirst)))
        .map(|builder| {
            builder.portmapper_config(if policy.portmapping() {
                PortmapperConfig::default()
            } else {
                PortmapperConfig::Disabled
            })
        })
        .map_err(|_| Error::Unavailable)
}

/// Across networks the RELAY carries the run's traffic on both ends (lane XC). iroh's default
/// selector treats the relay as a backup and moves application data to any IP path the moment one
/// has stats. Across two NATs such a path can work one way only: on hardware (rv52 hotspot, B on a
/// mobile hotspot, A behind a home router) the member selected a hole-punched IP path to the
/// owner's public address while the owner kept only the relay, so the member's reply (and its
/// close) never reached the owner (`control_read_timeout`; owner paths `[*relay]`, member paths
/// `[relay | *ip:<owner public>]`). The connection now stays on the relay whenever one is open,
/// which is what the choice promises ("through the Mycelium relay") and what v0.1.4 did across
/// networks; an IP path is used only when no relay path exists (the loopback tests' dead relay).
#[derive(Debug)]
pub(crate) struct RelayFirst;
impl iroh::endpoint::transports::PathSelector for RelayFirst {
    fn select(
        &self,
        ctx: &iroh::endpoint::transports::PathSelectionContext<'_>,
    ) -> iroh::endpoint::transports::PathSelection {
        let mut selection = iroh::endpoint::transports::PathSelection::none();
        let paths: Vec<_> = ctx.paths().filter(|p| p.stats().is_some()).collect();
        let relay: Vec<bool> = paths.iter().map(|p| p.network_path().is_relay()).collect();
        let current = ctx
            .current()
            .and_then(|c| paths.iter().position(|p| p.network_path() == c));
        if let Some(index) = relay_first(&relay, current) {
            selection.set(&paths[index]);
        }
        selection
    }
}
/// [`RelayFirst`]'s choice over the open paths (`relay[i]`: path `i` is a relay path) and the
/// index of the current selection among them: `None` keeps the current selection. A relay path
/// wins over any IP path; with no relay path the current IP path stays, else the first one.
pub(crate) fn relay_first(relay: &[bool], current: Option<usize>) -> Option<usize> {
    let first_relay = relay.iter().position(|r| *r);
    match current {
        Some(c) if relay[c] => None,
        Some(_) => first_relay,
        None => first_relay.or_else(|| (!relay.is_empty()).then_some(0)),
    }
}

/// Lane XA status (patch 2, REVIEW XB-07): the kind of one of this endpoint's direct addresses.
/// `local` is a bound socket, `qad` the public address the relay's QUIC address discovery
/// reported, `qad_local_port` that public IP with a bound port (iroh's own `Qad4LocalPort`
/// candidate). Any other address is `portmapped` ONLY with mapping evidence (`mapping_granted`:
/// the router granted this endpoint a mapping); without it, `unknown`.
pub(crate) fn direct_addr_kind(
    addr: SocketAddr,
    bound: &[SocketAddr],
    qad: &[SocketAddr],
    mapping_granted: bool,
) -> &'static str {
    if bound.contains(&addr) {
        "local"
    } else if qad.contains(&addr) {
        "qad"
    } else if qad.iter().any(|q| q.ip() == addr.ip())
        && bound.iter().any(|b| b.port() == addr.port())
    {
        "qad_local_port"
    } else if mapping_granted {
        "portmapped"
    } else {
        "unknown"
    }
}
/// The monotonic clock of a relay probe ([`crate::iroh_host::relay_reachable`]).
#[derive(Debug)]
pub(crate) struct ProbeClock(std::time::Instant);
impl Default for ProbeClock {
    fn default() -> Self {
        Self(std::time::Instant::now())
    }
}
impl transport::Clock for ProbeClock {
    fn monotonic(&self) -> budgets::MonoInstant {
        budgets::MonoInstant::new(
            budgets::ClockDomain(1),
            u64::try_from(self.0.elapsed().as_millis()).unwrap_or(u64::MAX),
        )
    }
    fn wall_seconds(&self) -> f64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0.0, |elapsed| elapsed.as_secs_f64())
    }
}
/// The peer gate of one endpoint across networks.
#[derive(Debug)]
pub(crate) struct CrossGate {
    policy: LanPolicy,
    keys: Mutex<Option<Vec<EndpointId>>>,
    refused_in: AtomicU64,
    refused_out: AtomicU64,
}
impl CrossGate {
    pub(crate) fn new(policy: LanPolicy) -> Self {
        Self {
            policy,
            keys: Mutex::new(None),
            refused_in: AtomicU64::new(0),
            refused_out: AtomicU64::new(0),
        }
    }
    pub(crate) fn policy(&self) -> &LanPolicy {
        &self.policy
    }
    pub(crate) fn relay(&self) -> Result<&iroh::RelayUrl, Error> {
        self.policy.relay().ok_or(Error::Unavailable)
    }
    /// Replace the admitted peer keys (from signed data). Empty is refused, as on the LAN.
    pub(crate) fn set_keys(&self, keys: &[EndpointId]) -> Result<(), Error> {
        if keys.is_empty() {
            return Err(Error::Authentication);
        }
        *self.keys.lock().map_err(|_| Error::Unavailable)? = Some(keys.to_vec());
        Ok(())
    }
    /// `None` when no keys are set; otherwise whether `key` is one of them. A poisoned table
    /// admits nothing.
    pub(crate) fn admits_key(&self, key: &EndpointId) -> Option<bool> {
        match self.keys.lock() {
            Ok(keys) => keys.as_ref().map(|k| k.contains(key)),
            Err(_) => Some(false),
        }
    }
    /// The address iroh dials for a validated `peer`: its signed IP addresses plus this host's
    /// own relay (both ends share Mycelium's relay; relays do not chain).
    pub(crate) fn target(&self, peer: &EndpointAddr) -> Result<EndpointAddr, Error> {
        Ok(peer
            .ip_addrs()
            .fold(EndpointAddr::new(peer.id), |addr, socket| {
                addr.with_ip_addr(*socket)
            })
            .with_relay_url(self.relay()?.clone()))
    }
    pub(crate) fn refuse_in(&self) {
        self.refused_in.fetch_add(1, Ordering::Relaxed);
    }
    pub(crate) fn refuse_out(&self) {
        self.refused_out.fetch_add(1, Ordering::Relaxed);
    }
    /// Handshakes refused under an unadmitted key.
    pub(crate) fn refused_in(&self) -> u64 {
        self.refused_in.load(Ordering::Relaxed)
    }
    /// Dials refused before any packet (an address outside the policy).
    pub(crate) fn refused_out(&self) -> u64 {
        self.refused_out.load(Ordering::Relaxed)
    }
}

#[cfg(test)]
mod relay_first_tests {
    use super::relay_first;
    #[test]
    fn across_networks_the_relay_wins_over_any_ip_path_and_ip_is_used_only_without_one() {
        // rv52 hotspot: the member had [relay, *ip] and kept sending on the one-way IP path.
        assert_eq!(
            relay_first(&[true, false], Some(1)),
            Some(0),
            "an IP selection moves to the relay"
        );
        assert_eq!(relay_first(&[false, true], Some(0)), Some(1));
        assert_eq!(
            relay_first(&[true, false], Some(0)),
            None,
            "a relay selection stays"
        );
        assert_eq!(
            relay_first(&[false, true], None),
            Some(1),
            "no selection: the relay first"
        );
        // No relay path open (the loopback tests' dead relay): IP is allowed and sticky.
        assert_eq!(relay_first(&[false, false], None), Some(0));
        assert_eq!(relay_first(&[false, false], Some(1)), None);
        assert_eq!(relay_first(&[], None), None);
    }
}
