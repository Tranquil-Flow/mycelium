// SPDX-License-Identifier: AGPL-3.0-only
//! Closed path-manifest wire parsing and binding to independently validated
//! graph facts. Graph validation, reservation expiry and execution stay L7/L9.
use crate::{ContractError, checks::*};
use serde_json::Value;
pub const PROTOCOL: &str = "mycelium.path_manifest.v1";
#[derive(Debug)]
pub struct Manifest(Value);
fn any_integer(v: &Value) -> bool {
    v.as_number().is_some_and(|n| {
        let s = n.to_string();
        let d = s.strip_prefix('-').unwrap_or(&s);
        !d.is_empty() && d.bytes().all(|b| b.is_ascii_digit())
    })
}
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::Number(n) => n.as_f64().is_none_or(|n| n != 0.0),
    }
}
pub fn parse(v: &Value) -> Result<Manifest, ContractError> {
    exact(
        v,
        &[
            "protocol",
            "path_id",
            "path_attempt",
            "request_id",
            "deployment_id",
            "deployment_epoch",
            "topology_version",
            "manifest_digest",
            "ordered_hops",
            "loopback_edge_id",
        ],
        "invalid_manifest_fields",
    )?;
    require(v["protocol"] == PROTOCOL, "unsupported_manifest_protocol")?;
    let hops = v["ordered_hops"]
        .as_array()
        .filter(|a| !a.is_empty())
        .ok_or(ContractError("invalid_manifest_hops"))?;
    for hop in hops {
        exact(
            hop,
            &[
                "stage_id",
                "placement_id",
                "reservation_id",
                "reservation_expires_at",
                "reservation_epoch",
            ],
            "invalid_manifest_hop_fields",
        )?;
    }
    require(
        v["path_id"].as_str().is_some_and(|s| !s.is_empty())
            && v["request_id"].as_str().is_some_and(|s| !s.is_empty())
            && integer(&v["path_attempt"], 0)
            && any_integer(&v["deployment_epoch"])
            && any_integer(&v["topology_version"]),
        "invalid_manifest_identity",
    )?;
    Ok(Manifest(v.clone()))
}
impl Manifest {
    pub fn into_value(self) -> Value {
        self.0
    }
    /// Requires an independently validated execution graph, not a graph supplied
    /// by the manifest sender. This boundary checks manifest relationships only;
    /// do not mistake it for a port of `execution_graph_from_dict` or admission.
    pub fn bind_to_validated_graph(&self, graph: &Value) -> Result<(), ContractError> {
        let v = &self.0;
        for (field, code) in [
            ("deployment_id", "deployment_id_mismatch"),
            ("deployment_epoch", "deployment_epoch_mismatch"),
            ("topology_version", "topology_version_mismatch"),
            ("manifest_digest", "manifest_digest_mismatch"),
        ] {
            require(v[field] == graph[field], code)?;
        }
        let invalid = "invalid_graph_binding";
        let stages = graph["stages"].as_array().ok_or(ContractError(invalid))?;
        let hops = v["ordered_hops"]
            .as_array()
            .ok_or(ContractError("invalid_manifest_hops"))?;
        require(hops.len() == stages.len(), "manifest_stage_count_mismatch")?;
        let placements = stages
            .iter()
            .map(|s| s["placements"].as_array().ok_or(ContractError(invalid)))
            .collect::<Result<Vec<_>, _>>()?;
        for (index, hop) in hops.iter().enumerate() {
            require(
                hop["stage_id"] == stages[index]["stage_id"],
                "manifest_stage_order_mismatch",
            )?;
            require(
                placements
                    .iter()
                    .any(|ps| ps.iter().any(|p| p["placement_id"] == hop["placement_id"])),
                "unknown_manifest_placement",
            )?;
            require(
                placements[index]
                    .iter()
                    .any(|p| p["placement_id"] == hop["placement_id"]),
                "placement_stage_mismatch",
            )?;
            require(truthy(&hop["reservation_id"]), "missing_reservation_id")?;
        }
        let edges = graph["edges"].as_array().ok_or(ContractError(invalid))?;
        for pair in hops.windows(2) {
            require(
                edges.iter().any(|e| {
                    e["from_placement_id"] == pair[0]["placement_id"]
                        && e["to_placement_id"] == pair[1]["placement_id"]
                }),
                "illegal_manifest_edge",
            )?;
        }
        let first = hops.first().ok_or(ContractError("invalid_manifest_hops"))?;
        let last = hops.last().ok_or(ContractError("invalid_manifest_hops"))?;
        let loopbacks = graph["loopback_edges"]
            .as_array()
            .ok_or(ContractError(invalid))?;
        require(
            loopbacks.iter().any(|e| {
                e["from_placement_id"] == last["placement_id"]
                    && e["to_placement_id"] == first["placement_id"]
                    && e["edge_id"] == v["loopback_edge_id"]
            }),
            "illegal_manifest_loopback",
        )
    }
}
