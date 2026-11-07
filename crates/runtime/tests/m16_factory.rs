// SPDX-License-Identifier: AGPL-3.0-only
//! Factory composition with controlled observations; no physical qualification claim.
use budgets::{BudgetId, Entries};
use protocol::execution_graph;
use runtime::{
    m16_factory::{DeviceFacts, FactoryInputs, build_live_m16_runtime},
    m16_ledger::{IdSource, LeaseClock},
    m16_router_contracts::{DeviceState, IdMap, PyReal, RequestContext, RouterConfig},
};
use serde_json::json;
use std::{
    collections::BTreeMap,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};
fn latency(id: BudgetId) -> PyReal {
    PyReal::from_json(&json!(id.span(Entries::new(0)).unwrap().millis())).unwrap()
}
struct Clock;
impl LeaseClock for Clock {
    fn now(&self) -> f64 {
        0.0
    }
}
#[derive(Default)]
struct Ids(AtomicU64);
impl IdSource for Ids {
    fn new_id(&self, prefix: &str) -> String {
        format!("{prefix}-{}", self.0.fetch_add(1, Ordering::SeqCst))
    }
}
fn inputs() -> FactoryInputs {
    let graph=execution_graph::parse(&json!({
        "protocol":"mycelium.execution_graph.v1","deployment_id":"controlled","deployment_epoch":1,"topology_version":1,
        "model_id":"model","resolved_commit":"commit","manifest_digest":"controlled-manifest",
        "entry_stage_id":"stage","final_stage_id":"stage","hidden_size":8,"activation_bytes":2,"token_envelope_bytes":16,
        "stages":[{"stage_id":"stage","range":{"start_layer":0,"end_layer_exclusive":1,"layer_count":1},"component_roles":["decoder"],
            "stage_cost":{"prefill_work_units_per_prompt_token":2,"decode_work_units_per_token":1,"kv_bytes_per_context_token":256},
            "placements":[{"placement_id":"placement","node_id":"node","replica_group_id":"group","assignment_id":"assignment","stage_signature":"signature","load_proof_digest":"controlled-load","runtime_backend":"controlled","runtime_endpoint":"local://controlled"}]}],
        "edges":[],"loopback_edges":[{"edge_id":"loop","from_placement_id":"placement","to_placement_id":"placement","link_id":"link"}]
    })).unwrap();
    FactoryInputs {
        graph,
        devices: BTreeMap::from([(
            "node".into(),
            DeviceFacts {
                fast_allocatable_bytes: 2_097_152,
                state: DeviceState {
                    node_id: json!("node"),
                    state_seq: PyReal::int(1),
                    last_updated: PyReal::Float(0.0),
                    availability: "ALIVE".into(),
                    compute_units_per_second: PyReal::int(1024),
                    free_compute_fraction: PyReal::int(1),
                    available_kv_bytes: PyReal::int(1_048_576),
                    pending_hop_queue_depth: PyReal::int(0),
                    neighbor_rtt_ms: IdMap::new(),
                    neighbor_bandwidth_bytes_per_second: IdMap::new(),
                },
            },
        )]),
        workload_profiles: BTreeMap::from([("interactive".into(), "interactive".into())]),
        clock: Arc::new(Clock),
        lease_clock: Arc::new(Clock),
        id_source: Arc::new(Ids::default()),
        config: RouterConfig::oracle_default().unwrap(),
        max_concurrent_requests: 1,
    }
}
fn request(id: &str) -> RequestContext {
    RequestContext {
        request_id: id.into(),
        prompt_token_ids: vec![PyReal::int(7)],
        max_new_tokens: PyReal::int(4095),
        expected_new_tokens: PyReal::int(1),
        qos_class: "interactive".into(),
        admitted_at: PyReal::Float(0.0),
        target_ttft_ms: latency(BudgetId::PyFinalMyceliumRequestGatewayBackendTargetTtftMsL234C12),
        target_tpot_ms: latency(BudgetId::PyFinalMyceliumRequestGatewayBackendTargetTpotMsL235C12),
        target_tokens_per_second: PyReal::int(1),
        sampling_seed: PyReal::int(0),
        generation_config_digest: "controlled-generation".into(),
    }
}
#[test]
fn factory_composes_capacity_route_scheduler_and_release() {
    let runtime = build_live_m16_runtime(inputs()).unwrap();
    let c = &runtime.coordinator;
    let baseline = c.placement_status().unwrap();
    let manifest = c.admit(&request("one"), "interactive", &[]).unwrap();
    assert_eq!(manifest.ordered_hops[0].placement_id, "placement");
    assert_eq!(c.request_reservations("one").len(), 1);
    assert_eq!(
        c.admit(&request("two"), "interactive", &[])
            .unwrap_err()
            .code(),
        "resource_unavailable"
    );
    assert!(c.request_reservations("two").is_empty());
    assert_eq!(c.next_dispatch().unwrap().as_deref(), Some("one"));
    assert!(c.next_dispatch().unwrap().is_none());
    c.mark_phase("one", "cleanup").unwrap();
    assert_ne!(c.placement_status().unwrap(), baseline);
    c.complete("one", "completed").unwrap();
    assert_eq!(c.placement_status().unwrap(), baseline);
    c.admit(&request("two"), "interactive", &[]).unwrap();
    assert!(c.cancel("two"));
    assert_eq!(c.placement_status().unwrap(), baseline);
}
#[test]
fn missing_mismatched_or_inflated_device_facts_are_refused() {
    let mut i = inputs();
    i.devices.clear();
    assert_eq!(
        build_live_m16_runtime(i).err().unwrap().code(),
        "m16_device_facts_missing"
    );
    let mut i = inputs();
    i.devices.get_mut("node").unwrap().state.node_id = json!("other");
    assert_eq!(
        build_live_m16_runtime(i).err().unwrap().code(),
        "m16_device_facts_invalid"
    );
    let mut i = inputs();
    i.devices.get_mut("node").unwrap().fast_allocatable_bytes = 1;
    assert_eq!(
        build_live_m16_runtime(i).err().unwrap().code(),
        "m16_capacity_exceeds_device_facts"
    );
}
#[test]
fn invalid_workload_or_concurrency_is_not_replaced_with_defaults() {
    let mut i = inputs();
    i.workload_profiles.clear();
    assert_eq!(
        build_live_m16_runtime(i).err().unwrap().code(),
        "m16_workload_profiles_invalid"
    );
    let mut i = inputs();
    i.max_concurrent_requests = 0;
    assert_eq!(
        build_live_m16_runtime(i).err().unwrap().code(),
        "invalid_max_concurrent_requests"
    );
}
#[test]
fn unavailable_device_is_not_synthesized_as_alive() {
    let mut i = inputs();
    i.devices.get_mut("node").unwrap().state.availability = "DEAD".into();
    let r = build_live_m16_runtime(i).unwrap();
    assert_eq!(
        r.coordinator
            .admit(&request("one"), "interactive", &[])
            .unwrap_err()
            .code(),
        "resource_unavailable"
    );
    assert!(r.coordinator.request_reservations("one").is_empty());
}
