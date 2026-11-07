// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NET-11 (graph, manifest and reservation pins bind before routing); B-CHAT-20 (active/manual/replica identity cannot be transplanted to another route; partial: hop provisioning is L5/L6).
//!
//! O:contract:mycelium.execution_graph.v1; O:F14. Pure consumer boundaries.
use protocol::canonical;
use router::routing::{DocumentKind, PinnedDocument, Route};
use serde_json::{Value, json};
fn graph() -> Value {
    let rows: Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/expanded-graph-vectors.json"
    ))
    .unwrap();
    rows[0]["value"].clone()
}
fn manifest(g: &Value) -> Value {
    json!({"protocol":"mycelium.path_manifest.v1","path_id":"path","path_attempt":1,
    "request_id":"request","deployment_id":g["deployment_id"],"deployment_epoch":g["deployment_epoch"],
    "topology_version":g["topology_version"],"manifest_digest":g["manifest_digest"],
    "loopback_edge_id":g["loopback_edges"][0]["edge_id"],
    "ordered_hops":g["stages"].as_array().unwrap().iter().map(|s|json!({"stage_id":s["stage_id"],
    "placement_id":s["placements"][0]["placement_id"],"reservation_id":format!("reservation-{}",s["stage_id"]),
    "reservation_epoch":1,"reservation_expires_at":100.0})).collect::<Vec<_>>()})
}
#[test]
fn graph_manifest_and_independent_reservation_pins_bind_before_routing() {
    let g = graph();
    let m = manifest(&g);
    let pin = canonical::to_bytes(&g).unwrap();
    let hops = m["ordered_hops"].as_array().unwrap().clone();
    let route = Route::admit(&g, &pin, &m, &hops, 99.0).unwrap();
    assert_eq!(
        route.placements(),
        &["stage-000-primary", "stage-001-primary"]
    );
    assert_eq!(route.nodes(), &["node-a", "node-b"]);
    assert!(Route::admit(&g, &pin, &m, &hops, 100.0).is_err());
    for f in [
        "reservation_id",
        "reservation_epoch",
        "reservation_expires_at",
    ] {
        let mut bad = m.clone();
        bad["ordered_hops"][0][f] = json!(999);
        assert!(Route::admit(&g, &pin, &bad, &hops, 99.0).is_err(), "{f}");
    }
    let mut changed = g.clone();
    changed["stages"][0]["placements"][0]["assignment_id"] = json!("changed");
    assert!(Route::admit(&changed, &pin, &m, &hops, 99.0).is_err());
    assert!(Route::admit(&g, &pin, &m, &hops[..1], 99.0).is_err());
}
#[test]
fn active_manual_and_replica_identity_cannot_be_transplanted_to_another_route() {
    let g = graph();
    let m = manifest(&g);
    let pin = canonical::to_bytes(&g).unwrap();
    let route = Route::admit(&g, &pin, &m, m["ordered_hops"].as_array().unwrap(), 99.0).unwrap();
    let rows: Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/expanded-public-vectors.json"
    ))
    .unwrap();
    let mut active = rows
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["value"].clone())
        .find(|v| v["candidates"][0]["state"] == "active")
        .unwrap();
    active["candidates"][0]["model_id"] = g["model_id"].clone();
    active["candidates"][0]["model_revision"] = g["resolved_commit"].clone();
    let doc = |kind, v: &Value| {
        PinnedDocument::verify(kind, v, &canonical::to_bytes(v).unwrap()).unwrap()
    };
    route
        .bind_activation(&doc(DocumentKind::Activation, &active))
        .unwrap();
    active["candidates"][0]["deployment_id"] = json!("87654321-1234-5678-1234-567812345678");
    active["candidates"][0]["candidate_id"] = active["candidates"][0]["deployment_id"].clone();
    assert!(
        route
            .bind_activation(&doc(DocumentKind::Activation, &active))
            .is_err()
    );
    let mut manual = json!({"protocol":"mycelium.manual_provisioning_route.v1","ok":true,"claim_boundary":"synthetic component fixture",
        "model":{"model_id":g["model_id"],"resolved_commit":g["resolved_commit"],"manifest_digest":g["manifest_digest"],"num_layers":4},
        "route":[{"node_id":"node-a","range":g["stages"][0]["range"]},{"node_id":"node-b","range":g["stages"][1]["range"]}]});
    route
        .bind_manual(&doc(DocumentKind::ManualRoute, &manual))
        .unwrap();
    manual["route"][0]["node_id"] = json!("foreign");
    assert!(
        route
            .bind_manual(&doc(DocumentKind::ManualRoute, &manual))
            .is_err()
    );
    let rows: Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/expanded-replica-vectors.json"
    ))
    .unwrap();
    let mut q = rows
        .as_array()
        .unwrap()
        .iter()
        .map(|r| r["value"].clone())
        .find(|v| {
            v["protocol"] == DocumentKind::ReplicaQualification.protocol()
                && v["route_ready"] == true
        })
        .unwrap();
    q["deployment_id"] = g["deployment_id"].clone();
    q["placement_ids"] = json!(route.placements());
    q["placement_id"] = json!(route.placements()[0]);
    let digest = protocol::replica::qualification_digest(&q).unwrap();
    q["qualification_digest"] = json!(digest);
    q["qualification_id"] = q["qualification_digest"].clone();
    let qualified = doc(DocumentKind::ReplicaQualification, &q);
    route.bind_replica(&qualified, 10_000).unwrap();
    assert!(route.bind_replica(&qualified, 600_000).is_err());
    q["deployment_epoch"] = json!(2);
    let digest = protocol::replica::qualification_digest(&q).unwrap();
    q["qualification_digest"] = json!(digest);
    q["qualification_id"] = q["qualification_digest"].clone();
    assert!(
        route
            .bind_replica(&doc(DocumentKind::ReplicaQualification, &q), 10_000)
            .is_err()
    );
}
#[test]
fn new_l5_identities_are_schema_checked_and_independently_pinned() {
    for (text, kind) in [
        (
            include_str!("../../protocol/fixtures/expanded-public-vectors.json"),
            DocumentKind::Activation,
        ),
        (
            include_str!("../../protocol/fixtures/expanded-manual-vectors.json"),
            DocumentKind::ManualRoute,
        ),
        (
            include_str!("../../protocol/fixtures/expanded-replica-vectors.json"),
            DocumentKind::ReplicaQualification,
        ),
    ] {
        let rows: Value = serde_json::from_str(text).unwrap();
        let v = rows
            .as_array()
            .unwrap()
            .iter()
            .map(|r| &r["value"])
            .find(|v| v["protocol"] == kind.protocol())
            .unwrap();
        let bytes = canonical::to_bytes(v).unwrap();
        let doc = PinnedDocument::verify(kind, v, &bytes).unwrap();
        assert_eq!(doc.value(), v);
        assert!(PinnedDocument::verify(kind, v, b"{}").is_err());
        let mut invalid = v.clone();
        invalid["protocol"] = json!("not-a-protocol");
        assert!(
            PinnedDocument::verify(kind, &invalid, &canonical::to_bytes(&invalid).unwrap())
                .is_err()
        );
    }
}
