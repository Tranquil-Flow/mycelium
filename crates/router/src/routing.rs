// SPDX-License-Identifier: AGPL-3.0-only
//! L5 document consumption. Pins MUST come from authenticated current owner state,
//! never from the arriving document. Shape and a self-digest are not authority.
use protocol::{
    ContractError, canonical, execution_graph, path_manifest, provisioning, public, replica,
};
use serde_json::Value;
#[derive(Clone, Copy, Debug)]
pub enum DocumentKind {
    Activation,
    ManualRoute,
    ReplicaPlan,
    ReplicaQualification,
    ReplicaRuntime,
}
impl DocumentKind {
    pub fn protocol(self) -> &'static str {
        match self {
            Self::Activation => "mycelium.deployment_activation.v1",
            Self::ManualRoute => "mycelium.manual_provisioning_route.v1",
            Self::ReplicaPlan => "mycelium.replica_plan.v1",
            Self::ReplicaQualification => "mycelium.replica_qualification.v1",
            Self::ReplicaRuntime => "mycelium.replica_runtime.v1",
        }
    }
}
#[derive(Debug)]
pub struct PinnedDocument(Value);
impl PinnedDocument {
    pub fn verify(
        kind: DocumentKind,
        value: &Value,
        expected: &[u8],
    ) -> Result<Self, ContractError> {
        match kind {
            DocumentKind::Activation => public::activation(value)?,
            DocumentKind::ManualRoute => provisioning::manual_route(value)?,
            _ => replica::validate_as(kind.protocol(), value)?,
        }
        pinned(value, expected)?;
        Ok(Self(value.clone()))
    }
    pub fn value(&self) -> &Value {
        &self.0
    }
}
fn pinned(value: &Value, expected: &[u8]) -> Result<(), ContractError> {
    if canonical::to_bytes(value).map_err(|_| ContractError("route_document_encoding"))? != expected
    {
        return Err(ContractError("route_document_pin_mismatch"));
    }
    Ok(())
}
#[derive(Debug)]
pub struct Route {
    graph: execution_graph::Graph,
    manifest: Value,
    placements: Vec<String>,
    nodes: Vec<String>,
}
impl Route {
    /// Reservation pins are complete independently held hops. A manifest cannot
    /// enlarge their expiry or replace their identity/epoch. Wall time is in the
    /// existing manifest's seconds domain, not a fresh relative lease allowance.
    pub fn admit(
        graph: &Value,
        graph_pin: &[u8],
        manifest: &Value,
        reservations: &[Value],
        now: f64,
    ) -> Result<Self, ContractError> {
        pinned(graph, graph_pin)?;
        let graph = execution_graph::parse(graph)?;
        let m = path_manifest::parse(manifest)?;
        m.bind_to_validated_graph(graph.as_value())?;
        let manifest = m.into_value();
        let hops = manifest["ordered_hops"]
            .as_array()
            .ok_or(ContractError("invalid_manifest_hops"))?;
        if !now.is_finite() || hops != reservations {
            return Err(ContractError("route_reservation_binding"));
        }
        let mut placements = Vec::new();
        let mut nodes = Vec::new();
        for (i, hop) in hops.iter().enumerate() {
            if !hop["reservation_expires_at"]
                .as_f64()
                .is_some_and(|t| t.is_finite() && now < t)
            {
                return Err(ContractError("route_reservation_expired"));
            }
            let placement = hop["placement_id"]
                .as_str()
                .ok_or(ContractError("route_identity_invalid"))?;
            let p = graph.as_value()["stages"][i]["placements"]
                .as_array()
                .and_then(|ps| ps.iter().find(|p| p["placement_id"] == placement))
                .ok_or(ContractError("unknown_manifest_placement"))?;
            let node = p["node_id"]
                .as_str()
                .ok_or(ContractError("route_identity_invalid"))?;
            placements.push(placement.to_owned());
            nodes.push(node.to_owned());
        }
        Ok(Self {
            graph,
            manifest,
            placements,
            nodes,
        })
    }
    /// Bind an owner-pinned activation publication to this installed graph.
    pub fn bind_activation(&self, document: &PinnedDocument) -> Result<(), ContractError> {
        let v = document.value();
        let g = self.graph();
        let active = v["protocol"] == DocumentKind::Activation.protocol()
            && v["candidates"].as_array().is_some_and(|rows| {
                rows.iter().any(|c| {
                    c["state"] == "active"
                        && c["deployment_id"] == g["deployment_id"]
                        && c["model_id"] == g["model_id"]
                        && c["model_revision"] == g["resolved_commit"]
                })
            });
        if !active {
            return Err(ContractError("route_activation_binding"));
        }
        Ok(())
    }
    pub fn bind_manual(&self, document: &PinnedDocument) -> Result<(), ContractError> {
        let v = document.value();
        let g = self.graph();
        if v["protocol"] != DocumentKind::ManualRoute.protocol()
            || v["model"]["model_id"] != g["model_id"]
            || v["model"]["resolved_commit"] != g["resolved_commit"]
            || v["model"]["manifest_digest"] != g["manifest_digest"]
        {
            return Err(ContractError("route_manual_binding"));
        }
        let rows = v["route"]
            .as_array()
            .ok_or(ContractError("route_manual_binding"))?;
        if rows.len() != self.nodes.len()
            || rows.iter().enumerate().any(|(i, r)| {
                r["node_id"] != self.nodes[i] || r["range"] != g["stages"][i]["range"]
            })
        {
            return Err(ContractError("route_manual_binding"));
        }
        Ok(())
    }
    /// Qualification self-digest is already verified by L5; independent document
    /// pin, current lease/reservation and trusted time remain host obligations.
    pub fn bind_replica(
        &self,
        document: &PinnedDocument,
        now_unix_ms: u64,
    ) -> Result<(), ContractError> {
        let v = document.value();
        let g = self.graph();
        if v["protocol"] != DocumentKind::ReplicaQualification.protocol()
            || v["route_ready"] != true
            || v["deployment_id"] != g["deployment_id"]
            || v["deployment_epoch"] != g["deployment_epoch"]
            || v["placement_ids"] != serde_json::json!(self.placements)
            || v["issued_at_unix_ms"]
                .as_u64()
                .is_none_or(|t| t > now_unix_ms)
            || v["expires_at_unix_ms"]
                .as_u64()
                .is_none_or(|t| now_unix_ms >= t)
        {
            return Err(ContractError("route_replica_binding"));
        }
        Ok(())
    }
    pub fn placements(&self) -> &[String] {
        &self.placements
    }
    pub fn nodes(&self) -> &[String] {
        &self.nodes
    }
    pub fn graph(&self) -> &Value {
        self.graph.as_value()
    }
    pub fn manifest(&self) -> &Value {
        &self.manifest
    }
}
