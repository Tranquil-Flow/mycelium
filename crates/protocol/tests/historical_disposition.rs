// SPDX-License-Identifier: AGPL-3.0-only
//! A disposition is not an invented readiness validator.
use protocol::{canonical, schemas};
use serde_json::Value;

// O:contract:mycelium.deployment_residency_physical_gate.v1
#[test]
fn historical_residency_is_retained_but_cannot_authorize_current_readiness() {
    let disposition: Value = serde_json::from_str(include_str!(
        "../fixtures/round4-residency-disposition.json"
    ))
    .unwrap();
    let bytes = include_bytes!("../fixtures/contracts/deployment-residency-physical-v1.json");
    let value: Value = serde_json::from_slice(bytes).unwrap();
    assert_eq!(
        canonical::sha256_prefixed(bytes),
        format!("sha256:{}", disposition["fixture_sha256"].as_str().unwrap())
    );
    assert_eq!(value["protocol"], disposition["protocol"]);
    assert_eq!(value["route_ready"], false);
    assert_eq!(
        value["source_commit"],
        "5b876403f15a8af75b7453f00a5762c0e5be0ae2"
    );
    assert_eq!(disposition["ships"]["generator_source_data"], true);
    assert_eq!(
        disposition["ships"]["runtime_callers"]
            .as_array()
            .unwrap()
            .len(),
        0
    );
    assert!(schemas::validate(&value, 0).is_err());
    let mut forged = value;
    forged["route_ready"] = true.into();
    assert!(schemas::validate(&forged, 0).is_err());
}
