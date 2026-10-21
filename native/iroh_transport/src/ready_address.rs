// SPDX-License-Identifier: AGPL-3.0-only
//! Address readiness for the sidecar's `ready` event (packet wp2-6).
//!
//! `rca/wp2-iroh-delivery.md` (defect 1): the sidecar published
//! `Endpoint::addr()` immediately after `bind()`, before the endpoint had
//! selected a home relay, and never refreshed it.  The WP2 probe observed
//! `relay_addrs=0` at that moment, so a cross-network peer could be handed a
//! `ready` address with **no relay transport at all**, permanently.
//!
//! This module owns both halves of the fix:
//!
//! * [`await_advertised_address`] waits - bounded - for the endpoint to
//!   advertise a *usable* address set (at least one relay address when relays
//!   are configured), and **names** the outcome when the window expires instead
//!   of publishing an unexamined snapshot;
//! * [`republish_address_changes`] re-advertises the address whenever it
//!   changes, so a laptop that moves networks is not permanently described by
//!   its start-up address (`research/research-iroh.md` §3.5: the peer's
//!   `EndpointAddr` is otherwise a start-up snapshot).
//!
//! Both take a `tokio::sync::watch` channel rather than the `Endpoint` itself.
//! Production bridges `Endpoint::watch_addr()` into that channel
//! (`sidecar::publish_address_updates`), which is what makes the logic here
//! testable offline and deterministically (`tests/ready_address.rs`): no relay,
//! no public hostname, no address that leaves the process.

use std::future::Future;
use std::time::Duration;

use iroh::{EndpointAddr, TransportAddr};
use serde::Serialize;
use tokio::sync::watch;
use tokio::time::timeout;

/// How long the sidecar waits for a usable address set before it publishes
/// `ready`.
///
/// Sized against the pinned transport and the launcher: relay selection is one
/// net-report round plus a WebSocket upgrade (the research note quotes ~1-2 s
/// for DNS + TCP + TLS + upgrade), and the launcher's own start-up budget is
/// 30 s (`physical_inference_node.py`: `startup_timeout=min(command_timeout,
/// 30.0)`).  Three seconds therefore leaves the ordinary case untouched while
/// stopping a relay-less network from silently publishing an address that can
/// never be dialled from another network.
pub const READY_ADDRESS_WINDOW: Duration = Duration::from_secs(3);

/// Named reason recorded when the window expires before a usable address set
/// exists.  `ready` is still published - the endpoint remains dialable by
/// direct address - but the decision is named in the event log instead of being
/// silently wrong (`research/research-iroh.md` §4.7: silence in our own logs).
pub const ADDRESS_INCOMPLETE_REASON: &str = "relay_address_not_advertised";

/// What the readiness gate observed before `ready` was published.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AddressReadiness {
    /// A relay address is advertised: the endpoint registered at a relay, which
    /// is what cross-network dialling needs.
    Online,
    /// Relays are disabled (`--local-only`): the direct address is the whole
    /// address set, so there is nothing to wait for.
    LocalOnly,
    /// Relays are configured but no relay address was advertised inside
    /// [`READY_ADDRESS_WINDOW`].  Published with
    /// [`ADDRESS_INCOMPLETE_REASON`].
    WindowExpired,
}

impl AddressReadiness {
    /// The named reason to record, or `None` when the address set is complete.
    pub const fn reason(self) -> Option<&'static str> {
        match self {
            Self::Online | Self::LocalOnly => None,
            Self::WindowExpired => Some(ADDRESS_INCOMPLETE_REASON),
        }
    }

    /// The event name the sidecar records for this outcome.
    pub const fn event(self) -> &'static str {
        match self {
            Self::Online | Self::LocalOnly => "address_ready",
            Self::WindowExpired => "ready_address_incomplete",
        }
    }
}

/// Does this address set carry what `requires_relay` needs?
fn address_is_usable(address: &EndpointAddr, requires_relay: bool) -> bool {
    if requires_relay {
        address.addrs.iter().any(TransportAddr::is_relay)
    } else {
        !address.addrs.is_empty()
    }
}

/// Wait (bounded) for the endpoint to advertise a usable address set.
///
/// `relay_online` is the endpoint's relay-registration confirmation
/// (`Endpoint::online`).  It must be an already-resolved future when relays are
/// disabled, because `Endpoint::online` never resolves without relays.
///
/// Returns the *live* address - never the one captured at bind time - together
/// with what the gate observed.
pub async fn await_advertised_address<F>(
    addresses: &mut watch::Receiver<EndpointAddr>,
    requires_relay: bool,
    relay_online: F,
    window: Duration,
) -> (EndpointAddr, AddressReadiness)
where
    F: Future<Output = ()>,
{
    let reached = timeout(
        window,
        wait_for_usable(addresses, requires_relay, relay_online),
    )
    .await;
    let advertised = addresses.borrow_and_update().clone();
    let readiness = match reached {
        Ok(()) if requires_relay => AddressReadiness::Online,
        Ok(()) => AddressReadiness::LocalOnly,
        Err(_elapsed) => AddressReadiness::WindowExpired,
    };
    (advertised, readiness)
}

async fn wait_for_usable<F>(
    addresses: &mut watch::Receiver<EndpointAddr>,
    requires_relay: bool,
    relay_online: F,
) where
    F: Future<Output = ()>,
{
    let mut relay_online = Box::pin(relay_online);
    let mut online_confirmed = false;
    loop {
        if address_is_usable(&addresses.borrow_and_update(), requires_relay) {
            return;
        }
        tokio::select! {
            // Joining `online()` as well as the address watcher means the
            // published set is one the endpoint actually confirmed, not merely
            // the last value some background task happened to write.
            () = &mut relay_online, if requires_relay && !online_confirmed => {
                online_confirmed = true;
            }
            changed = addresses.changed() => {
                if changed.is_err() {
                    // The endpoint is gone; report the set we last saw.  The
                    // caller still decides whether that set is complete.
                    return;
                }
            }
        }
    }
}

/// A sink for one re-advertised address line.
pub trait AddressLineSink {
    fn emit(&mut self, line: &str);
}

/// Writes `address_changed` records to the sidecar's stdout, next to `ready`.
pub struct StdoutSink;

impl AddressLineSink for StdoutSink {
    fn emit(&mut self, line: &str) {
        use std::io::Write as _;
        let mut stdout = std::io::stdout().lock();
        if writeln!(stdout, "{line}").is_ok() {
            let _ = stdout.flush();
        }
    }
}

#[derive(Serialize)]
struct AddressChangedEvent<'a> {
    event: &'static str,
    endpoint_id: &'a str,
    endpoint_addr: &'a EndpointAddr,
}

/// The canonical line for one re-advertised address.
pub fn address_changed_line(endpoint_id: &str, address: &EndpointAddr) -> String {
    serde_json::to_string(&AddressChangedEvent {
        event: "address_changed",
        endpoint_id,
        endpoint_addr: address,
    })
    .unwrap_or_else(|_| "{\"event\":\"address_changed\"}".to_owned())
}

/// Re-advertise the endpoint's address whenever it changes.
///
/// The value the `ready` event already carried is never repeated: only real
/// changes are published, so a peer that has the start-up address is told when
/// it stops being true.
///
/// `published` is the address the `ready` event carried, passed in by the
/// caller rather than read from the channel: reading it here would race a
/// change that lands between the ready line and this task's first poll, and
/// that change is exactly the one a peer must not miss.
pub async fn republish_address_changes<S: AddressLineSink>(
    endpoint_id: String,
    mut addresses: watch::Receiver<EndpointAddr>,
    published: EndpointAddr,
    mut sink: S,
) {
    let mut published = published;
    loop {
        let current = addresses.borrow_and_update().clone();
        if current != published {
            published = current.clone();
            sink.emit(&address_changed_line(&endpoint_id, &current));
        }
        if addresses.changed().await.is_err() {
            return;
        }
    }
}
