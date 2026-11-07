// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "iroh-loopback")]
//! Round 6: the artifact carrier and NodeControl limits come from the source-specific
//! registry ids (v0.1.4 `artifact_net.rs` / `sidecar.rs`), never from `WorkerStartup` or a
//! structural serial `1`. Each value is pinned here against the registry entry, so a change
//! to either side fails.
use budgets::BudgetId as B;
use node_service::{artifact, control};
use serde_json::json;

fn wire(request: u64, header: u64) -> serde_json::Value {
    json!({"request_bytes": request, "header_bytes": header})
}

#[test]
fn artifact_waits_are_the_read_idle_and_io_ids_not_worker_startup() {
    let limits =
        artifact::serve_limits(artifact::wire_limits(&wire(1_400, 1_900)).unwrap()).unwrap();
    let ms = |id: B| id.span(budgets::Entries::ZERO).unwrap().millis();
    assert_eq!(limits.read.millis(), ms(B::ArtifactReadTimeout));
    assert_eq!(limits.idle.millis(), ms(B::ArtifactIdleTimeout));
    assert_eq!(limits.wire.io.millis(), ms(B::ArtifactIoTimeout));
    assert_eq!(
        (
            limits.read.millis(),
            limits.idle.millis(),
            limits.wire.io.millis()
        ),
        (10_000, 20_000, 30_000),
        "v0.1.4 ArtifactLimits::default() read/idle and IO_TIMEOUT"
    );
    assert_ne!(limits.read.millis(), ms(B::WorkerStartup));
}

#[test]
fn artifact_slots_and_wire_caps_are_the_registered_source_limits() {
    let limits =
        artifact::serve_limits(artifact::wire_limits(&wire(1_400, 1_900)).unwrap()).unwrap();
    assert_eq!(
        limits.connections,
        B::ArtifactMaxConnections.count().unwrap()
    );
    assert_eq!(
        limits.per_peer,
        B::ArtifactMaxConnectionsPerPeer.count().unwrap()
    );
    assert_eq!(limits.blocking, B::ArtifactMaxBlockingWork.count().unwrap());
    assert_eq!(
        (
            limits.connections.get(),
            limits.per_peer.get(),
            limits.blocking.get()
        ),
        (16, 2, 4)
    );
    assert_eq!(
        limits.wire.request_bytes,
        B::ArtifactMaxRequestBytes.bytes().unwrap()
    );
    assert_eq!(
        limits.wire.header_bytes,
        B::ArtifactMaxHeaderBytes.bytes().unwrap()
    );
    assert_eq!(
        (
            limits.wire.request_bytes.get(),
            limits.wire.header_bytes.get()
        ),
        (262_144, 65_536)
    );
}

#[test]
fn an_offer_whose_exemplar_exceeds_a_source_cap_is_refused() {
    let request_cap = B::ArtifactMaxRequestBytes.bytes().unwrap().get();
    let header_cap = B::ArtifactMaxHeaderBytes.bytes().unwrap().get();
    assert!(artifact::wire_limits(&wire(request_cap, header_cap)).is_ok());
    assert!(artifact::wire_limits(&wire(request_cap + 1, 10)).is_err());
    assert!(artifact::wire_limits(&wire(10, header_cap + 1)).is_err());
    assert!(artifact::wire_limits(&wire(0, 10)).is_err());
}

#[test]
fn node_control_call_slots_are_the_registered_concurrency() {
    assert_eq!(
        control::call_slots().unwrap(),
        B::NodeControlCallConcurrency.count().unwrap()
    );
    assert_eq!(control::call_slots().unwrap().get(), 4);
}
