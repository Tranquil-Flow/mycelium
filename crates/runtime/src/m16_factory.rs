// SPDX-License-Identifier: AGPL-3.0-only
//! Live M16 composition from a parsed graph and explicitly supplied device facts.
//!
//! Unlike the Python convenience factory, absent observations are not replaced with
//! healthy devices or invented network rates. The existing capacity producer owns
//! sizing, and the coordinator owns scorer/policy, builder, scheduler and ledger.
//! Qualification, tokenization, clock domains and id entropy belong to the host.
use crate::{
    m16_capacity::{CapacityError, ProjectedNodes, PyNumber, live_placement_capacities},
    m16_coordinator::{
        CoordinatorError, CoordinatorInputs, M16RuntimeCoordinator, SharedClock, SharedIdSource,
    },
    m16_router_contracts::{DeviceState, IdMap, PyReal, RouterConfig},
};
use protocol::execution_graph::Graph;
use serde_json::{Value, json};
use std::collections::BTreeMap;

pub struct DeviceFacts {
    pub state: DeviceState,
    /// Current allocatable bytes, not total installed memory.
    pub fast_allocatable_bytes: u64,
}
pub struct FactoryInputs {
    pub graph: Graph,
    pub devices: BTreeMap<String, DeviceFacts>,
    pub workload_profiles: BTreeMap<String, String>,
    pub clock: SharedClock,
    pub lease_clock: SharedClock,
    pub id_source: SharedIdSource,
    pub config: RouterConfig,
    pub max_concurrent_requests: u64,
}
#[derive(Debug)]
pub enum FactoryError {
    Facts(&'static str),
    Capacity(CapacityError),
    Coordinator(CoordinatorError),
}
impl FactoryError {
    pub fn code(&self) -> &str {
        match self {
            Self::Facts(code) => code,
            Self::Capacity(e) => e.0,
            Self::Coordinator(e) => e.code(),
        }
    }
}
/// Transfer this owner into the request adapter; do not separately drive its coordinator.
pub struct LiveM16Runtime {
    pub coordinator: M16RuntimeCoordinator,
    pub graph: Graph,
    pub clock: SharedClock,
}
pub fn build_live_m16_runtime(input: FactoryInputs) -> Result<LiveM16Runtime, FactoryError> {
    let bad = FactoryError::Facts;
    let mut states = IdMap::new();
    let mut projected = ProjectedNodes::new();
    let mut placement_nodes = BTreeMap::new();
    for stage in input.graph.as_value()["stages"]
        .as_array()
        .ok_or(bad("m16_factory_invalid_graph"))?
    {
        for p in stage["placements"]
            .as_array()
            .ok_or(bad("m16_factory_invalid_graph"))?
        {
            let node = p["node_id"]
                .as_str()
                .ok_or(bad("m16_factory_invalid_node_id"))?;
            let id = p["placement_id"]
                .as_str()
                .ok_or(bad("m16_factory_invalid_placement_id"))?;
            let facts = input
                .devices
                .get(node)
                .ok_or(bad("m16_device_facts_missing"))?;
            if facts.state.node_id != p["node_id"] || facts.fast_allocatable_bytes == 0 {
                return Err(bad("m16_device_facts_invalid"));
            }
            states
                .insert(p["node_id"].clone(), facts.state.clone())
                .map_err(|_| bad("m16_factory_invalid_node_id"))?;
            projected.insert(
                node.into(),
                serde_json::Map::from_iter([(
                    "fast_allocatable_bytes".into(),
                    json!(facts.fast_allocatable_bytes),
                )]),
            );
            placement_nodes.insert(id.to_owned(), node.to_owned());
        }
    }
    let capacities =
        live_placement_capacities(&input.graph, &projected).map_err(FactoryError::Capacity)?;
    // The legacy producer can inflate a small projection to its workspace/KV floor.
    // Do not promote that planning estimate to observed physical capacity.
    let mut totals = BTreeMap::<String, u64>::new();
    for c in &capacities {
        let node = placement_nodes
            .get(
                c.placement_id
                    .as_str()
                    .ok_or(bad("m16_factory_invalid_placement_id"))?,
            )
            .ok_or(bad("m16_factory_invalid_placement_id"))?;
        let PyNumber::Int(memory) = &c.memory_capacity_bytes else {
            return Err(bad("m16_capacity_invalid"));
        };
        let memory: u64 = memory
            .to_string()
            .parse()
            .map_err(|_| bad("m16_capacity_invalid"))?;
        let total = totals.entry(node.clone()).or_default();
        *total = total
            .checked_add(memory)
            .ok_or(bad("m16_capacity_invalid"))?;
        if *total > input.devices[node].fast_allocatable_bytes {
            return Err(bad("m16_capacity_exceeds_device_facts"));
        }
    }
    let mut args = CoordinatorInputs::new(
        input.graph.clone(),
        states,
        capacities,
        input.workload_profiles,
        input.clock.clone(),
        input.id_source,
        input.config,
    );
    args.lease_clock = Some(input.lease_clock);
    args.max_concurrent_requests = Some(
        PyReal::from_json(&Value::from(input.max_concurrent_requests))
            .ok_or(bad("invalid_max_concurrent_requests"))?,
    );
    Ok(LiveM16Runtime {
        coordinator: M16RuntimeCoordinator::new(args).map_err(FactoryError::Coordinator)?,
        graph: input.graph,
        clock: input.clock,
    })
}
