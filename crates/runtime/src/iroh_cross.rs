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
use iroh::{Endpoint, EndpointAddr, EndpointId, RelayMode, endpoint::presets};
use std::{
    net::{IpAddr, SocketAddr},
    sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    },
};
use transport::Error;

/// The builder of an endpoint across networks: Mycelium's relay (the policy's), no address
/// lookup, one IP socket on `interface`.
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
        .map_err(|_| Error::Unavailable)
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
