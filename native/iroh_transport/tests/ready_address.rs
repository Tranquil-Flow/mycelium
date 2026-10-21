// SPDX-License-Identifier: AGPL-3.0-only
//! wp2-6 RED/GREEN: the `ready` event must never advertise a stale address set.
//!
//! The defect (`rca/wp2-iroh-delivery.md`, defect 1): the sidecar publishes
//! `endpoint.addr()` immediately after `bind()`, before the endpoint has
//! selected a relay, and never refreshes it.  The WP2 probe observed
//! `relay_addrs=0` at that moment, so a cross-network peer can be handed an
//! address set with no relay at all, forever.
//!
//! These tests drive the address-readiness gate and the address republisher
//! directly over a `tokio::sync::watch` channel, so they are fully offline and
//! deterministic: no relay, no socket, no address that leaves the process.

use std::future::{pending, ready};
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use iroh::{EndpointAddr, EndpointId, RelayUrl, SecretKey, TransportAddr};
use mycelium_iroh_transport::ready_address::{
    ADDRESS_INCOMPLETE_REASON, AddressLineSink, AddressReadiness, READY_ADDRESS_WINDOW,
    await_advertised_address, republish_address_changes,
};
use tokio::sync::watch;

/// Bounded wait for a republished line, so a failed expectation cannot hang.
const LINE_WAIT: Duration = Duration::from_secs(2);

fn endpoint_id(seed: u8) -> EndpointId {
    SecretKey::from_bytes(&[seed; 32]).public()
}

/// The address an endpoint advertises before `online()`: direct only.
fn direct_only_address(seed: u8) -> EndpointAddr {
    let mut addr = EndpointAddr::new(endpoint_id(seed));
    addr.addrs.insert(TransportAddr::Ip(
        "127.0.0.1:59928".parse::<SocketAddr>().unwrap(),
    ));
    addr
}

/// The address it advertises once a relay registration completed.
fn relay_address(seed: u8) -> EndpointAddr {
    let mut addr = direct_only_address(seed);
    addr.addrs.insert(TransportAddr::Relay(
        "http://127.0.0.1:1/".parse::<RelayUrl>().unwrap(),
    ));
    addr
}

fn relay_only_address(seed: u8) -> EndpointAddr {
    let mut addr = EndpointAddr::new(endpoint_id(seed));
    addr.addrs.insert(TransportAddr::Relay(
        "http://127.0.0.1:1/".parse::<RelayUrl>().unwrap(),
    ));
    addr
}

#[derive(Clone, Default)]
struct CollectingSink(Arc<Mutex<Vec<String>>>);

impl CollectingSink {
    fn lines(&self) -> Vec<String> {
        self.0.lock().unwrap().clone()
    }

    /// Poll until the republisher produced `count` lines, or fail the wait.
    async fn wait_for_lines(&self, count: usize) -> Vec<String> {
        let started = Instant::now();
        while started.elapsed() < LINE_WAIT {
            let lines = self.lines();
            if lines.len() >= count {
                return lines;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
        self.lines()
    }
}

impl AddressLineSink for CollectingSink {
    fn emit(&mut self, line: &str) {
        self.0.lock().unwrap().push(line.to_owned());
    }
}

// --------------------------------------------------------------------------- #
// The readiness gate.
// --------------------------------------------------------------------------- #

#[tokio::test]
async fn ready_address_waits_for_the_relay_address_instead_of_the_bind_snapshot() {
    let (sender, mut addresses) = watch::channel(direct_only_address(1));
    let publisher = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(20)).await;
        let _ = sender.send(relay_address(1));
        // Keep the channel alive: a dropped sender would look like a closed
        // endpoint rather than a slow one.
        pending::<()>().await;
    });

    let (advertised, readiness) = await_advertised_address(
        &mut addresses,
        // Relays are configured (production mode): the advertised set must name
        // one, so the direct-only bind snapshot is not good enough.
        true,
        pending(),
        READY_ADDRESS_WINDOW,
    )
    .await;

    publisher.abort();
    assert!(
        advertised.addrs.iter().any(TransportAddr::is_relay),
        "the ready event advertised relay_addrs=0, the defect: {advertised:?}"
    );
    assert_eq!(readiness, AddressReadiness::Online);
    assert_eq!(readiness.reason(), None);
}

#[tokio::test]
async fn ready_address_never_publishes_an_unlabelled_stale_snapshot() {
    // Relays are configured, the endpoint never registers one, and the address
    // never gains a relay entry: the gate must say so by name instead of
    // advertising the bind snapshot as if it were complete.
    let (_sender, mut addresses) = watch::channel(direct_only_address(2));

    let (advertised, readiness) =
        await_advertised_address(&mut addresses, true, pending(), Duration::from_millis(50)).await;

    assert_eq!(readiness, AddressReadiness::WindowExpired);
    assert_eq!(readiness.reason(), Some(ADDRESS_INCOMPLETE_REASON));
    assert_eq!(advertised, direct_only_address(2));
}

#[tokio::test]
async fn ready_address_is_the_live_value_once_the_endpoint_reports_online() {
    let (sender, mut addresses) = watch::channel(direct_only_address(3));
    let publisher = tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(5)).await;
        let _ = sender.send(relay_only_address(3));
        pending::<()>().await;
    });

    let (advertised, readiness) =
        await_advertised_address(&mut addresses, true, ready(()), READY_ADDRESS_WINDOW).await;

    publisher.abort();
    assert_eq!(readiness, AddressReadiness::Online);
    assert_eq!(advertised, relay_only_address(3));
}

#[tokio::test]
async fn local_only_ready_address_does_not_wait_for_relays() {
    // `--local-only` disables relays: `Endpoint::online` never resolves there,
    // so the gate must be satisfied by the direct address and return promptly.
    let (_sender, mut addresses) = watch::channel(direct_only_address(4));
    let started = Instant::now();

    let (advertised, readiness) =
        await_advertised_address(&mut addresses, false, pending(), READY_ADDRESS_WINDOW).await;

    assert_eq!(readiness, AddressReadiness::LocalOnly);
    assert_eq!(readiness.reason(), None);
    assert_eq!(advertised, direct_only_address(4));
    assert!(
        started.elapsed() < Duration::from_secs(1),
        "local-only start-up must not wait for a relay window: {:?}",
        started.elapsed()
    );
}

// --------------------------------------------------------------------------- #
// The republisher: an address that changes is re-advertised, not frozen.
// --------------------------------------------------------------------------- #

#[tokio::test]
async fn address_change_is_republished_once_per_new_value() {
    let (sender, addresses) = watch::channel(direct_only_address(5));
    let sink = CollectingSink::default();
    let publisher = tokio::spawn(republish_address_changes(
        endpoint_id(5).to_string(),
        addresses,
        direct_only_address(5),
        sink.clone(),
    ));

    // The value the `ready` event already advertised must not be repeated.
    tokio::time::sleep(Duration::from_millis(25)).await;
    assert!(sink.lines().is_empty());

    let _ = sender.send(relay_address(5));
    let lines = sink.wait_for_lines(1).await;
    assert_eq!(lines.len(), 1, "one change must produce exactly one line");

    // An unchanged value is not a change.
    let _ = sender.send(relay_address(5));
    tokio::time::sleep(Duration::from_millis(25)).await;
    assert_eq!(sink.lines().len(), 1, "a repeated value must not republish");

    let _ = sender.send(relay_only_address(5));
    let lines = sink.wait_for_lines(2).await;
    publisher.abort();
    assert_eq!(lines.len(), 2);

    let document: serde_json::Value =
        serde_json::from_str(&lines[1]).expect("the line is canonical JSON");
    assert_eq!(document["event"], "address_changed");
    assert_eq!(document["endpoint_id"], endpoint_id(5).to_string());
    assert_eq!(
        document["endpoint_addr"]["id"],
        endpoint_id(5).to_string(),
        "the re-advertised entry must name the same identity"
    );
    assert!(
        document["endpoint_addr"]["addrs"]
            .as_array()
            .is_some_and(|addrs| !addrs.is_empty()),
        "a re-advertisement must carry the address set: {document}"
    );
}

#[tokio::test]
async fn republisher_stops_when_the_endpoint_is_dropped() {
    let (sender, addresses) = watch::channel(direct_only_address(6));
    let sink = CollectingSink::default();
    let publisher = tokio::spawn(republish_address_changes(
        endpoint_id(6).to_string(),
        addresses,
        direct_only_address(6),
        sink.clone(),
    ));

    drop(sender);
    tokio::time::timeout(LINE_WAIT, publisher)
        .await
        .expect("the republisher must stop when the endpoint goes away")
        .expect("the republisher task must not panic");
    assert!(sink.lines().is_empty());
}
