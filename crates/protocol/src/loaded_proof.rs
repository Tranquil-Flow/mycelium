// SPDX-License-Identifier: AGPL-3.0-only
//! Pure authentication of a load proof against independently held loader facts.
//! This neither loads weights nor authenticates tensors/probes or physical state.
use crate::{ContractError, canonical, checks::*, public::fields};
use serde_json::Value;
fn canonical(v: &Value, code: &'static str) -> Result<Vec<u8>, ContractError> {
    canonical::to_ascii_bytes(v).map_err(|_| ContractError(code))
}
fn uuid(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        s.len() == 36
            && s.bytes().enumerate().all(|(i, b)| {
                if [8, 13, 18, 23].contains(&i) {
                    b == b'-'
                } else {
                    b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
                }
            })
    })
}
fn aliases(v: &Value) -> Result<Vec<u8>, ContractError> {
    const C: &str = "resolved_aliases_mismatch";
    let m = v.as_object().ok_or(ContractError(C))?;
    for (source, a) in m {
        fields(a, "target_component tensor_keys", C)?;
        require(
            !source.is_empty()
                && a["target_component"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty()),
            C,
        )?;
        let keys = a["tensor_keys"].as_array().ok_or(ContractError(C))?;
        require(
            !keys.is_empty()
                && keys.iter().enumerate().all(|(i, k)| {
                    k.as_str().is_some_and(|s| !s.is_empty()) && !keys[..i].contains(k)
                }),
            C,
        )?;
    }
    canonical(v, C)
}
/// `binding` must come from retained loader state, NEVER from this untrusted
/// proof. Required keys mirror the source's named authentication arguments.
/// `normalized_runtime` must already have passed the owning backend validator.
pub fn authenticate(proof: &Value, binding: &Value) -> Result<(), ContractError> {
    fields(
        binding,
        "authenticated_assignment_id authenticated_load_generation authenticated_loaded_components authenticated_loaded_range resolved_aliases authenticated_resolved_aliases authenticated_runtime authenticated_runtime_identity normalized_runtime",
        "load_proof_binding_invalid",
    )?;
    require(
        proof.is_object() && proof["protocol"] == "mycelium.layer_load_proof.v1",
        "invalid_loaded_stage_proof",
    )?;
    require(
        proof["route_ready"] == false,
        "invalid_loaded_stage_route_claim",
    )?;
    require(
        uuid(&proof["assignment_id"])
            && proof["assignment_id"] == binding["authenticated_assignment_id"],
        "assignment_id_mismatch",
    )?;
    require(
        integer(&proof["load_generation"], 0)
            && integer(&binding["authenticated_load_generation"], 0)
            && proof["load_generation"] == binding["authenticated_load_generation"],
        "load_generation_mismatch",
    )?;
    let components = &proof["loaded_components"];
    canonical(components, "loaded_components_mismatch")?;
    require(
        components
            .as_array()
            .is_some_and(|a| a.iter().all(Value::is_string))
            && python_equal(components, &binding["authenticated_loaded_components"]),
        "loaded_components_mismatch",
    )?;
    let range = &proof["loaded_range"];
    canonical(range, "loaded_range_mismatch")?;
    require(
        range.is_object() && python_equal(range, &binding["authenticated_loaded_range"]),
        "loaded_range_mismatch",
    )?;
    let a = aliases(&proof["resolved_component_aliases"])?;
    let b = aliases(&binding["resolved_aliases"])?;
    let c = aliases(&binding["authenticated_resolved_aliases"])?;
    require(a == b && b == c, "resolved_aliases_mismatch")?;
    let runtime = &binding["normalized_runtime"];
    let identity = &proof["runtime_identity"];
    canonical(runtime, "runtime_identity_mismatch")?;
    canonical(identity, "runtime_identity_mismatch")?;
    require(
        python_equal(runtime, &binding["authenticated_runtime"]),
        "runtime_identity_mismatch",
    )?;
    fields(
        identity,
        "backend dtype quantization architecture backend_version device",
        "runtime_identity_mismatch",
    )?;
    require(
        python_equal(identity, &binding["authenticated_runtime_identity"]),
        "runtime_identity_mismatch",
    )?;
    require(
        runtime.is_object()
            && ["backend", "dtype", "quantization", "architecture"]
                .iter()
                .all(|f| python_equal(&identity[*f], &runtime[*f])),
        "runtime_identity_mismatch",
    )?;
    require(
        ["backend_version", "device"]
            .iter()
            .all(|f| identity[*f].as_str().is_some_and(|s| !s.is_empty())),
        "runtime_identity_mismatch",
    )?;
    Ok(())
}
