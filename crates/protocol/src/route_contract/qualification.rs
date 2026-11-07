// SPDX-License-Identifier: AGPL-3.0-only
use crate::{ContractError, checks::*, public::fields};
use serde_json::Value;
const FIELDS: &str = "protocol qualification_id issued_at_unix_ms evidence_class route_ready reason_codes deployment_id deployment_epoch topology_version placement_provenance model_id resolved_commit manifest_digest gossip_snapshot_digest gossip_signature_digest planner_snapshot_digest route_plan_digest assignments_digest provisioning_reports_digest load_proofs_digest load_proof_signatures_digest execution_graph_digest path_manifest_digest reservations_digest stage_bindings endpoint_set_digest process_set_digest tensor_scope_digest transport_digest timing_evidence_digest token_parity_digest numeric_parity_digest execution_trace_digest kv_ownership_digest lifecycle_evidence_digest negative_runs_digest source_provenance_digest source_manifest_digest environment_digest contract_manifest_digest dependency_lock_digests evidence_manifest_digest qualified_by claim_boundary";
fn nonblank(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        s.chars()
            .any(|c| !c.is_whitespace() && !matches!(c, '\u{1c}'..='\u{1f}'))
    })
}
/// Parse a compatibility record only. Ready records always require the separate
/// qualifier authority; digest-shaped assertions do not grant that authority.
pub fn unqualified(v: &Value) -> Result<Value, ContractError> {
    let m = v
        .as_object()
        .ok_or(ContractError("invalid_contract_document"))?;
    require(
        v["route_ready"] != true,
        "qualified_record_requires_qualifier",
    )?;
    require(
        FIELDS.split_whitespace().all(|f| m.contains_key(f)),
        "missing_contract_field",
    )?;
    require(
        m.len() == FIELDS.split_whitespace().count(),
        "unknown_contract_field",
    )?;
    let reasons = v["reason_codes"]
        .as_array()
        .ok_or(ContractError("invalid_reason_codes"))?;
    let locks = v["dependency_lock_digests"]
        .as_array()
        .ok_or(ContractError("invalid_dependency_lock_digests"))?;
    let stages = v["stage_bindings"]
        .as_array()
        .ok_or(ContractError("invalid_stage_bindings"))?;
    for s in stages {
        fields(
            s,
            "stage_id placement_id assignment_id node_id stage_signature load_proof_digest stage_probe_result_digest endpoint_id process_id process_host_id tensor_scope_digest reservation_id",
            "invalid_stage_binding",
        )?;
    }
    require(v["route_ready"].is_boolean(), "invalid_route_ready")?;
    require(
        v["protocol"] == "mycelium.route_qualification.v1",
        "unsupported_qualification_protocol",
    )?;
    macro_rules! strings {($($f:literal),*)=>{$(require(nonblank(&v[$f]),concat!("invalid_",$f))?;)*};}
    strings!(
        "qualification_id",
        "deployment_id",
        "model_id",
        "resolved_commit",
        "claim_boundary"
    );
    macro_rules! ints {($($f:literal),*)=>{$(require(integer(&v[$f],0),concat!("invalid_",$f))?;)*};}
    ints!("issued_at_unix_ms", "deployment_epoch", "topology_version");
    require(
        super::hashable(&v["placement_provenance"]),
        "invalid_contract_field",
    )?;
    require(
        one_of(
            &v["placement_provenance"],
            &["frozen_fixture", "planner_v2"],
        ),
        "invalid_placement_provenance",
    )?;
    require(
        super::hashable(&v["evidence_class"]),
        "invalid_contract_field",
    )?;
    require(
        one_of(
            &v["evidence_class"],
            &["physical_qualification", "synthetic_test_fixture"],
        ),
        "invalid_evidence_class",
    )?;
    macro_rules! digests {($($f:literal),*)=>{$(require(digest(&v[$f]),concat!("invalid_",$f))?;)*};}
    digests!(
        "manifest_digest",
        "gossip_snapshot_digest",
        "gossip_signature_digest",
        "planner_snapshot_digest",
        "route_plan_digest",
        "assignments_digest",
        "provisioning_reports_digest",
        "load_proofs_digest",
        "load_proof_signatures_digest",
        "execution_graph_digest",
        "path_manifest_digest",
        "reservations_digest",
        "endpoint_set_digest",
        "process_set_digest",
        "tensor_scope_digest",
        "transport_digest",
        "timing_evidence_digest",
        "token_parity_digest",
        "numeric_parity_digest",
        "execution_trace_digest",
        "kv_ownership_digest",
        "lifecycle_evidence_digest",
        "negative_runs_digest",
        "source_provenance_digest",
        "source_manifest_digest",
        "environment_digest",
        "contract_manifest_digest",
        "evidence_manifest_digest"
    );
    require(
        !locks.is_empty() && locks.iter().all(digest),
        "invalid_dependency_lock_digests",
    )?;
    require(
        reasons.iter().all(super::hashable),
        "invalid_contract_field",
    )?;
    require(
        reasons
            .iter()
            .enumerate()
            .all(|(i, r)| r.as_str().is_some_and(|s| !s.is_empty()) && !reasons[..i].contains(r)),
        "invalid_reason_codes",
    )?;
    for s in stages {
        require(
            [
                "stage_id",
                "placement_id",
                "assignment_id",
                "node_id",
                "endpoint_id",
                "process_host_id",
                "reservation_id",
            ]
            .iter()
            .all(|f| nonblank(&s[*f])),
            "invalid_stage_binding",
        )?;
        require(digest(&s["stage_signature"]), "invalid_stage_signature")?;
        require(digest(&s["load_proof_digest"]), "invalid_load_proof_digest")?;
        require(
            digest(&s["stage_probe_result_digest"]),
            "invalid_stage_probe_result_digest",
        )?;
        require(
            digest(&s["tensor_scope_digest"]),
            "invalid_tensor_scope_digest",
        )?;
        require(
            integer(&s["process_id"], 1)
                || s["process_id"].as_str().is_some_and(|p| {
                    p.starts_with("inc-") && p.len() > 4 && s["process_host_id"] == s["node_id"]
                }),
            "invalid_process_id",
        )?;
    }
    require(!reasons.is_empty(), "unqualified_record_missing_reasons")?;
    require(
        v["qualified_by"].is_null(),
        "unqualified_record_has_authority",
    )?;
    Ok(v.clone())
}
