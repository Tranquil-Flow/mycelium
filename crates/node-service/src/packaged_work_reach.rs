// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XB2 / XA (ruling 29 A+B): this Mac's network reach, a deliberate per-run choice. A private
//! preference file, read each time a run or a work service STARTS (as the interfaces are:
//! [`super::WorkNetwork`]), so a run keeps the answer it started with.
//!
//! - `same_network` (the default, and what a missing file means): exactly the direct-LAN carrier
//!   (`LanPolicy::private_only`, exact pins, no relay, no address lookup).
//! - `relay`: work with a Mac on ANOTHER network. The endpoints register at Mycelium's relay
//!   ([`runtime::iroh_host::MYCELIUM_RELAY_URL`]); iroh uses the relay and hole-punched direct
//!   paths; only the address class is relaxed (`LanPolicy::across_networks`).
//! - `portmapping` (default off, only in a `relay` run): ask the router for a port mapping
//!   (UPnP / PCP / NAT-PMP). Off by default because its SSDP multicast can raise a firewall
//!   prompt on macOS.
//!
//! File: `<network root>/network-reach.json`, a private (0600) canonical JSON object
//! `{"portmapping":false,"protocol":"mycelium.a13_network_reach.v1","reach":"relay"}`. An
//! unreadable or invalid file is ignored by name (`network_reach_ignored:<code>`) and the run
//! stays on the same network: never a relay the user did not choose.
use super::{Resolved, WorkNetwork};
use api::preferences::private_state;
use runtime::iroh_host::{LanPolicy, MYCELIUM_RELAY_URL, RELAY_UNREACHABLE, Reach};
use serde_json::{Value, json};
use std::path::Path;

pub const FILE: &str = "network-reach.json";
pub const PROTOCOL: &str = "mycelium.a13_network_reach.v1";
pub const INVALID: &str = "network_reach_invalid";

/// The user's choice. `Default` = same network, no portmapping.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ReachChoice {
    pub across_networks: bool,
    pub portmapping: bool,
}
impl ReachChoice {
    /// `same_network` / `relay`, as status names it.
    pub fn name(self) -> &'static str {
        if self.across_networks {
            "relay"
        } else {
            "same_network"
        }
    }
    pub fn document(self) -> Value {
        json!({"protocol": PROTOCOL, "reach": self.name(), "portmapping": self.portmapping})
    }
}
fn limit() -> Result<u64, &'static str> {
    private_state::private_json_limit().map_err(|e| e.0)
}
/// The stored choice; a missing file is the default.
pub fn load(root: &Path) -> Result<ReachChoice, &'static str> {
    let Some(map) = private_state::load(&root.join(FILE), limit()?).map_err(|_| INVALID)? else {
        return Ok(ReachChoice::default());
    };
    if map.len() != 3 || map.get("protocol").and_then(Value::as_str) != Some(PROTOCOL) {
        return Err(INVALID);
    }
    let portmapping = map
        .get("portmapping")
        .and_then(Value::as_bool)
        .ok_or(INVALID)?;
    match map.get("reach").and_then(Value::as_str) {
        Some("same_network") => Ok(ReachChoice {
            across_networks: false,
            portmapping,
        }),
        Some("relay") => Ok(ReachChoice {
            across_networks: true,
            portmapping,
        }),
        _ => Err(INVALID),
    }
}
/// Store the choice (atomic, private); the next start reads it.
pub fn save(root: &Path, choice: ReachChoice) -> Result<(), &'static str> {
    private_state::store(&root.join(FILE), &choice.document(), limit()?).map_err(|e| e.0)
}
/// The work transport a start resolves: the app's own reading ([`WorkNetwork::ThisMac`]) under
/// the user's choice; a network a caller configured otherwise is kept as it is.
pub fn effective(root: &Path, configured: &WorkNetwork) -> WorkNetwork {
    if *configured != WorkNetwork::ThisMac {
        return configured.clone();
    }
    match load(root) {
        Ok(choice) if choice.across_networks => WorkNetwork::Across {
            reach: Reach::Shipped,
            portmapping: choice.portmapping,
        },
        Ok(_) => WorkNetwork::ThisMac,
        Err(code) => {
            eprintln!("network_reach_ignored:{code}");
            WorkNetwork::ThisMac
        }
    }
}
/// `status.network_reach`: the choice the next start resolves (`reach`, its relay, portmapping).
pub fn status(root: &Path, configured: &WorkNetwork) -> Value {
    match effective(root, configured) {
        WorkNetwork::Across { reach, portmapping } => json!({
            "reach": "relay",
            "relay_url": reach.relay_url().map(|u| u.to_string()).ok(),
            "portmapping": portmapping,
        }),
        _ => json!({"reach": "same_network", "relay_url": Value::Null, "portmapping": false}),
    }
}
/// Lane XB3 at a run's start: a `relay` run whose relay cannot be reached fails closed with
/// `relay_unreachable` before anything is dialed; a same-network run needs no relay.
pub fn require_relay(resolved: &Resolved) -> Result<(), &'static str> {
    match &resolved.choice.0 {
        Some((interface, policy)) if policy.relay().is_some() => {
            runtime::iroh_host::relay_reachable(*interface, policy)
        }
        _ => Ok(()),
    }
}
/// The plain message of a `relay_unreachable` refusal.
pub fn relay_refusal() -> (&'static str, String) {
    (
        RELAY_UNREACHABLE,
        format!(
            "This Mac cannot reach the Mycelium relay ({MYCELIUM_RELAY_URL}), which a run across \
             networks needs to find the other Mac. Check this Mac's internet connection and start \
             again, or put both Macs on the same network and choose \"same network\"."
        ),
    )
}
/// The policy of a `relay` run's transport (Mycelium's relay unless a test overrides it).
pub(super) fn across_policy(reach: &Reach, portmapping: bool) -> Option<LanPolicy> {
    let relay = reach.relay_url().ok()?;
    Some(
        LanPolicy::private_only()
            .across_networks(relay)
            .with_portmapping(portmapping),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{net::IpAddr, os::unix::fs::PermissionsExt};

    fn root(tag: &str) -> std::path::PathBuf {
        let base = std::env::var_os("TMPDIR").map_or_else(std::env::temp_dir, Into::into);
        let dir = base.join(format!("xb-reach-{tag}-{}", std::process::id()));
        std::fs::remove_dir_all(&dir).ok();
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
        dir
    }
    fn reading(ip: &str) -> api::lan_interface::Selection {
        api::lan_interface::Selection::Lan(ip.parse::<IpAddr>().unwrap())
    }

    /// No file: the same network, exactly today's carrier, named `same_network` in status.
    #[test]
    fn without_a_choice_a_start_stays_on_the_same_network() {
        let dir = root("default");
        assert_eq!(load(&dir), Ok(ReachChoice::default()));
        assert_eq!(effective(&dir, &WorkNetwork::ThisMac), WorkNetwork::ThisMac);
        assert_eq!(
            status(&dir, &WorkNetwork::ThisMac),
            json!({"reach": "same_network", "relay_url": null, "portmapping": false})
        );
        let resolved = WorkNetwork::ThisMac.resolution_with(|| reading("192.168.1.20"));
        assert_eq!(resolved.transport, "lan:192.168.1.20");
        assert_eq!(resolved.reach, "same_network");
        assert_eq!(resolved.choice.reach(), "same_network");
        assert!(!resolved.choice.allows("203.0.113.9:4433".parse().unwrap()));
        assert_eq!(require_relay(&resolved), Ok(()), "no relay needed");
        std::fs::remove_dir_all(&dir).ok();
    }

    /// The user chose `relay`: the next start resolves the cross-network transport on this Mac's
    /// own interface, names it `relay:<ip>` / `relay`, and admits a peer's public address.
    #[test]
    fn a_relay_choice_resolves_the_cross_network_transport_at_the_next_start() {
        let dir = root("relay");
        let chosen = ReachChoice {
            across_networks: true,
            portmapping: false,
        };
        save(&dir, chosen).unwrap();
        assert_eq!(load(&dir), Ok(chosen));
        let network = effective(&dir, &WorkNetwork::ThisMac);
        assert_eq!(
            network,
            WorkNetwork::Across {
                reach: Reach::Shipped,
                portmapping: false
            }
        );
        assert_eq!(
            status(&dir, &WorkNetwork::ThisMac),
            json!({"reach": "relay", "relay_url": "https://relay.mycelium.now/", "portmapping": false})
        );
        let resolved = network.resolution_with(|| reading("172.20.10.4"));
        assert_eq!(resolved.transport, "relay:172.20.10.4");
        assert_eq!(resolved.reach, "relay");
        let Some((ip, policy)) = &resolved.choice.0 else {
            panic!("a relay run binds this Mac's interface: {resolved:?}")
        };
        assert_eq!(ip.to_string(), "172.20.10.4");
        assert_eq!(
            policy.relay().map(|u| u.to_string()).as_deref(),
            Some("https://relay.mycelium.now/")
        );
        assert!(!policy.portmapping(), "portmapping stays off unless chosen");
        assert!(resolved.choice.allows("203.0.113.9:4433".parse().unwrap()));
        // No private network on this Mac: still no run across networks on loopback.
        let none = network.resolution_with(|| {
            api::lan_interface::Selection::LocalOnly(api::lan_interface::NO_PRIVATE_ADDRESS)
        });
        assert_eq!(none.choice.0, None);
        assert_eq!(none.reach, "local_only");
        // Portmapping is the user's second, separate choice.
        save(
            &dir,
            ReachChoice {
                across_networks: true,
                portmapping: true,
            },
        )
        .unwrap();
        let mapped = effective(&dir, &WorkNetwork::ThisMac).resolution_with(|| reading("10.0.0.7"));
        assert!(mapped.choice.0.as_ref().is_some_and(|(_, p)| p.portmapping()));
        std::fs::remove_dir_all(&dir).ok();
    }

    /// A configured network (tests, explicit callers) is never overridden, and an invalid file
    /// keeps the same network.
    #[test]
    fn an_invalid_choice_or_a_configured_network_keeps_the_same_network() {
        let dir = root("invalid");
        save(
            &dir,
            ReachChoice {
                across_networks: true,
                portmapping: false,
            },
        )
        .unwrap();
        let fixed = WorkNetwork::loopback();
        assert_eq!(effective(&dir, &fixed), fixed);
        std::fs::write(dir.join(FILE), br#"{"protocol":"x","reach":"relay"}"#).unwrap();
        std::fs::set_permissions(dir.join(FILE), std::fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(load(&dir), Err(INVALID));
        assert_eq!(effective(&dir, &WorkNetwork::ThisMac), WorkNetwork::ThisMac);
        std::fs::remove_dir_all(&dir).ok();
    }
}
