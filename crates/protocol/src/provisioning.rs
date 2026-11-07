// SPDX-License-Identifier: AGPL-3.0-only
//! Pure manual-route validation and preparation authorization derivation.
//! Source: route_contract.py:21-88; mycelium_live/preparation.py:103-344 @ f4e366e9.
//! Inputs must come from authenticated, current owner/catalog state. These helpers
//! perform no staging, downloads, membership admission or durable authorization.
use crate::{ContractError, canonical, checks::*, execution_graph::numeric_cmp, public::fields};
use serde_json::{Value, json};
use std::cmp::Ordering;
fn nonempty(v: &Value) -> bool {
    v.as_str().is_some_and(|s| !s.is_empty())
}
fn signed_integer(v: &Value) -> bool {
    v.as_number().is_some_and(|n| {
        let s = n.to_string();
        let s = s.strip_prefix('-').unwrap_or(&s);
        !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
    })
}
fn revision(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        s.len() == 40
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
/// Validate the source's open manual-provisioning contract, not a Planner route.
pub fn manual_route(v: &Value) -> Result<(), ContractError> {
    crate::assignment::manual_route(v)
        .map_err(|_| ContractError("invalid_manual_provisioning_route"))
}
/// Derive a detached preparation record from caller-owned operation and decision.
/// `now_unix_ms` is an injected JSON number to preserve pinned oracle comparisons;
/// no host clock or authorization timeout is selected here.
pub fn authorization(
    operation: &Value,
    decision: &Value,
    now_unix_ms: &Value,
) -> Result<Value, ContractError> {
    let dc = "model_representation_decision_invalid";
    let v2 = decision["protocol"] == "mycelium.model_representation_decision.v2";
    require(
        v2 || decision["protocol"] == "mycelium.model_representation_decision.v1",
        dc,
    )?;
    fields(
        decision,
        if v2 {
            "protocol model_id revision source_quantization serving_dtype serving_quantization representation_digest conversion_authorized source_artifact_digest quantizer download_authorized"
        } else {
            "protocol model_id revision source_quantization serving_dtype serving_quantization representation_digest conversion_authorized"
        },
        dc,
    )?;
    require(
        decision["model_id"]
            .as_str()
            .is_some_and(|s| !s.is_empty() && s.chars().count() <= 256)
            && revision(&decision["revision"])
            && nonempty(&decision["source_quantization"])
            && nonempty(&decision["serving_dtype"])
            && nonempty(&decision["serving_quantization"])
            && digest(&decision["representation_digest"])
            && decision["conversion_authorized"].is_boolean(),
        dc,
    )?;
    require(
        !v2 || (digest(&decision["source_artifact_digest"])
            && nonempty(&decision["quantizer"])
            && decision["download_authorized"] == false),
        dc,
    )?;
    require(
        operation["protocol"] == "mycelium.model_operation.v1",
        "model_operation_unavailable",
    )?;
    let oc = "model_operation_invalid";
    let catalog = &operation["catalog"];
    let entries = if catalog.is_object() {
        &catalog["entries"]
    } else {
        &operation["entries"]
    };
    let entries = entries.as_array().ok_or(ContractError(oc))?;
    let reports = operation["feasibility_reports"]
        .as_array()
        .ok_or(ContractError(oc))?;
    let matching = |v: &&Value| {
        v.is_object()
            && v["model_id"] == decision["model_id"]
            && v["revision"] == decision["revision"]
    };
    let entries: Vec<_> = entries.iter().filter(matching).collect();
    require(entries.len() == 1, "model_identity_unknown")?;
    let entry = entries[0];
    require(entry["state"] == "compatible", "model_not_compatible")?;
    let mismatch = "model_representation_decision_mismatch";
    if v2 {
        let n = entry["serving_representations"].as_array().map_or(0, |a| {
            a.iter()
                .filter(|r| {
                    r.is_object()
                        && r["quantization"] == decision["serving_quantization"]
                        && r["runtime_dtype"] == decision["serving_dtype"]
                        && r["representation_digest"] == decision["representation_digest"]
                        && r["quantizer"] == decision["quantizer"]
                })
                .count()
        });
        require(
            entry["artifact_digest"] == decision["source_artifact_digest"] && n == 1,
            mismatch,
        )?;
    }
    let reports: Vec<_> = reports.iter().filter(matching).collect();
    require(reports.len() == 1, "model_capacity_not_evaluated")?;
    let report = reports[0];
    require(
        report["state"] == "feasible" && report["provisioning_authorized"] == true,
        "model_does_not_fit",
    )?;
    let valid = &report["evidence_valid_until_unix_ms"];
    require(signed_integer(valid), "model_capacity_stale")?;
    let order =
        numeric_cmp(valid, now_unix_ms).ok_or(ContractError("invalid_preparation_clock"))?;
    require(order != Ordering::Less, "model_capacity_stale")?;
    let sc = "model_stage_plan_invalid";
    let stages = report["stages"]
        .as_array()
        .filter(|a| !a.is_empty())
        .ok_or(ContractError(sc))?;
    let mut cursor = Value::from(0);
    let mut frozen = Vec::new();
    for (i, s) in stages.iter().enumerate() {
        require(
            s.is_object()
                && python_equal(&s["stage_index"], &Value::from(i))
                && signed_integer(&s["start_layer"])
                && signed_integer(&s["end_layer_exclusive"])
                && s["start_layer"] == cursor
                && numeric_cmp(&s["end_layer_exclusive"], &s["start_layer"])
                    == Some(Ordering::Greater)
                && s["node_id"].is_string()
                && s["backend"].is_string()
                && s["decode_mode"].is_string()
                && s["assignment_files"]
                    .as_array()
                    .is_some_and(|a| a.iter().all(nonempty))
                && signed_integer(&s["assignment_artifact_bytes"]),
            sc,
        )?;
        frozen.push(json!({"stage_index":i,"node_id":s["node_id"],"start_layer":s["start_layer"],"end_layer_exclusive":s["end_layer_exclusive"],"backend":s["backend"],"decode_mode":s["decode_mode"],"assignment_files":s["assignment_files"],"assignment_artifact_bytes":s["assignment_artifact_bytes"]}));
        cursor = s["end_layer_exclusive"].clone();
    }
    require(
        digest(&operation["operation_digest"])
            && digest(&report["feasibility_digest"])
            && digest(&report["representation_digest"])
            && nonempty(&report["source_quantization"])
            && nonempty(&report["serving_dtype"])
            && nonempty(&report["serving_quantization"])
            && signed_integer(&report["evidence_generation"])
            && signed_integer(&operation["catalog_generation"]),
        oc,
    )?;
    for f in [
        "source_quantization",
        "serving_dtype",
        "serving_quantization",
        "representation_digest",
    ] {
        require(decision[f] == report[f], mismatch)?;
    }
    require(
        !v2 || report["artifact_digest"] == decision["source_artifact_digest"],
        mismatch,
    )?;
    require(
        report["source_quantization"] == report["serving_quantization"]
            || decision["conversion_authorized"] == true,
        "model_representation_conversion_not_authorized",
    )?;
    let authority = &report["representation_authority"];
    let inherited = if authority.is_object()
        && authority["kind"] == "approved_existing_immutable_representation"
    {
        &authority["owner_decision_digest"]
    } else {
        &Value::Null
    };
    let owner = if inherited.is_null() {
        Value::String(canonical::digest(decision).map_err(|_| ContractError(oc))?)
    } else {
        require(digest(inherited), oc)?;
        inherited.clone()
    };
    let mut binding = json!({"feasibility_digest":report["feasibility_digest"],"owner_decision_digest":owner,"representation_digest":report["representation_digest"]});
    if v2 {
        binding["source_artifact_digest"] = decision["source_artifact_digest"].clone();
        binding["quantizer"] = decision["quantizer"].clone();
    }
    let mut out = json!({"protocol":"mycelium.model_preparation_authorization.v2","catalog_generation":operation["catalog_generation"],"operation_digest":operation["operation_digest"],"owner_decision_digest":owner,"preparation_binding_digest":canonical::digest(&binding).map_err(|_|ContractError(oc))?,"authorized_at_unix_ms":now_unix_ms,"evidence_valid_until_unix_ms":valid,"stages":frozen,"download_authorized":false});
    for f in [
        "model_id",
        "revision",
        "source_quantization",
        "serving_dtype",
        "serving_quantization",
        "representation_digest",
        "conversion_authorized",
    ] {
        out[f] = decision[f].clone();
    }
    for f in ["feasibility_digest", "evidence_generation"] {
        out[f] = report[f].clone();
    }
    if v2 {
        out["source_artifact_digest"] = decision["source_artifact_digest"].clone();
        out["quantizer"] = decision["quantizer"].clone();
    }
    Ok(out)
}
