// SPDX-License-Identifier: AGPL-3.0-only
use mycelium_mobile_core as mobile;
#[test]
fn new_evidence_modules_are_shared_and_accept_oracle_documents() {
    // The type assignment is also a compile-time no-wrapper/duplicate check.
    let limits: protocol::gossip::Limits = mobile::gossip::Limits {
        max_record_ttl_ms: 604_800_000,
    };
    let raw = include_bytes!("../../protocol/fixtures/contracts/gossip-evidence-bundle-v1.json");
    let bundle = mobile::strict::object(raw, raw.len()).unwrap();
    mobile::gossip::parse_bundle(&bundle, limits).unwrap();
    let raw = include_bytes!("../../protocol/fixtures/contracts/layer-planner-snapshot-v1.json");
    let snapshot = mobile::strict::object(raw, raw.len()).unwrap();
    mobile::planner_snapshot::validate_binding(&snapshot, &bundle, limits).unwrap();
    let raw = include_bytes!("../../protocol/fixtures/contracts/route-plan-v2.json");
    let route = mobile::strict::object(raw, raw.len()).unwrap();
    assert_eq!(
        mobile::canonical::to_bytes(&mobile::route_contract::plan(&route).unwrap()).unwrap(),
        mobile::canonical::to_bytes(&route).unwrap()
    );
    let raw = include_bytes!("../../protocol/fixtures/contracts/assignment-artifact-cache-v1.json");
    let cache = mobile::strict::object(raw, raw.len()).unwrap();
    mobile::public_evidence::cache(&cache).unwrap();
    let raw = include_bytes!("../../protocol/fixtures/contracts/layer-load-proof-v1.json");
    let proof = mobile::strict::object(raw, raw.len()).unwrap();
    // Fixture-only independent loader state; never build this from a live proof.
    let mut binding = mobile::strict::object(b"{}", 2).unwrap();
    for (bound, field) in [
        ("authenticated_assignment_id", "assignment_id"),
        ("authenticated_load_generation", "load_generation"),
        ("authenticated_loaded_components", "loaded_components"),
        ("authenticated_loaded_range", "loaded_range"),
        ("resolved_aliases", "resolved_component_aliases"),
        (
            "authenticated_resolved_aliases",
            "resolved_component_aliases",
        ),
        ("authenticated_runtime", "runtime"),
        ("authenticated_runtime_identity", "runtime_identity"),
        ("normalized_runtime", "runtime"),
    ] {
        binding[bound] = proof[field].clone();
    }
    mobile::loaded_proof::authenticate(&proof, &binding).unwrap();
}
