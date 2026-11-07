// SPDX-License-Identifier: AGPL-3.0-only
//! Controlled execution at the real request service boundary; not native/MLX evidence.
use api::{m16_requests::{Dispatch, Execution, Executor, Host, M16Requests, Prepared}, requests::Service, registry::Config, schema::PublicError};
use protocol::execution_graph;
use runtime::{m16_factory::{build_live_m16_runtime, DeviceFacts, FactoryInputs}, m16_coordinator::{SharedClock, SharedIdSource}, m16_ledger::{IdSource, LeaseClock}, m16_router_contracts::{DeviceState, IdMap, PyReal, RequestContext, RouterConfig}};
use serde_json::{Value, json};
use std::{collections::BTreeMap, sync::{Arc, Mutex, atomic::{AtomicU64, AtomicBool, Ordering}}};

struct Clock;
impl LeaseClock for Clock { fn now(&self) -> f64 { 0.0 } }
#[derive(Default)]
struct Ids(AtomicU64);
impl IdSource for Ids { fn new_id(&self, prefix: &str) -> String { format!("{prefix}-{}", self.0.fetch_add(1, Ordering::SeqCst)) } }
fn digest(text: &str) -> String { protocol::canonical::sha256_prefixed(text.as_bytes()) }
fn graph() -> execution_graph::Graph {
    execution_graph::parse(&json!({
        "protocol":"mycelium.execution_graph.v1", "deployment_id":"deployment", "deployment_epoch":1,"topology_version":1,
        "model_id":"model","resolved_commit":"commit","manifest_digest":digest("model"),
        "entry_stage_id":"stage","final_stage_id":"stage","hidden_size":8,"activation_bytes":2,"token_envelope_bytes":16,
        "stages":[{"stage_id":"stage","range":{"start_layer":0,"end_layer_exclusive":1,"layer_count":1},"component_roles":["decoder"],
        "stage_cost":{"prefill_work_units_per_prompt_token":2,"decode_work_units_per_token":1,"kv_bytes_per_context_token":256},
        "placements":[{"placement_id":"placement","node_id":"node","replica_group_id":"group","assignment_id":"assignment","stage_signature":"signature","load_proof_digest":digest("controlled-load"),"runtime_backend":"controlled","runtime_endpoint":"local://controlled"}]}],
        "edges":[],"loopback_edges":[{"edge_id":"loop","from_placement_id":"placement","to_placement_id":"placement","link_id":"link"}]
    })).unwrap()
}
fn inputs() -> FactoryInputs {
    FactoryInputs {
        graph: graph(),
        devices: BTreeMap::from([("node".into(), DeviceFacts { fast_allocatable_bytes: 2_097_152, state: DeviceState {
            node_id:json!("node"),state_seq:PyReal::int(1),last_updated:PyReal::Float(0.0),availability:"ALIVE".into(),
            compute_units_per_second:PyReal::int(1024),free_compute_fraction:PyReal::int(1),available_kv_bytes:PyReal::int(1_048_576),pending_hop_queue_depth:PyReal::int(0),neighbor_rtt_ms:IdMap::new(),neighbor_bandwidth_bytes_per_second:IdMap::new()
        }})]),
        workload_profiles: BTreeMap::from([("interactive".into(), "interactive".into())]),
        clock: Arc::new(Clock) as SharedClock, lease_clock: Arc::new(Clock) as SharedClock,
        id_source: Arc::new(Ids::default()) as SharedIdSource, config: RouterConfig::oracle_default().unwrap(), max_concurrent_requests: 1,
    }
}
struct Authority { ready: AtomicBool, ids: Ids }
impl Host for Authority {
    fn authorized(&self, r: &api::http::Request) -> bool { r.header("authorization") == Some("Bearer controlled") }
    fn qualification(&self) -> Result<Value, PublicError> {
        if !self.ready.load(Ordering::SeqCst) { return Err(PublicError::new(409,"stale_qualification")); }
        Ok(qualification())
    }
    fn prepare(&self, doc: &Value) -> Result<Prepared, PublicError> {
        Ok(Prepared {
            context:RequestContext { request_id:self.ids.new_id("request"), prompt_token_ids:vec![PyReal::int(7)], max_new_tokens:PyReal::from_json(&doc["max_new_tokens"]).unwrap(), expected_new_tokens:PyReal::int(1),qos_class:"interactive".into(),admitted_at:PyReal::Float(0.0),target_ttft_ms:PyReal::int(0),target_tpot_ms:PyReal::int(0),target_tokens_per_second:PyReal::int(1),sampling_seed:PyReal::int(0),generation_config_digest:digest("generation") },
            owner_token: "s".repeat(32), excluded_placements: vec![],
        })
    }
}
fn qualification() -> Value { json!({"qualification_id":"controlled-qualification","qualification_digest":digest("qualification"),"deployment_id":"deployment","deployment_epoch":1,"topology_version":1,"model_id":"model","resolved_commit":"commit","manifest_digest":digest("model"),"path_manifest_digest":digest("qualified-route"),"stage_load_proof_digests":[digest("controlled-load")]}) }
fn document() -> Value { json!({"protocol":"mycelium.request_gateway.v2","prompt":"test","max_new_tokens":4095,"workload_profile_id":"interactive","qos_class":"interactive","qualification":qualification()}) }
#[derive(Default)]
struct Control { dispatched:Vec<Dispatch>, polls:usize, cleanup_calls:usize, clean:bool, refuse:bool }
struct Controlled(Arc<Mutex<Control>>);
impl Executor for Controlled {
    fn start(&mut self, d: &Dispatch) -> Result<(), PublicError> { self.0.lock().unwrap().dispatched.push(d.clone()); Ok(()) }
    fn poll(&mut self, _: &str) -> Result<Execution, PublicError> {
        let mut c=self.0.lock().unwrap(); c.polls+=1;
        Ok(if c.polls==1 { Execution::Token { token_id:9,text:"hello".into() } } else { Execution::Completed })
    }
    fn cleanup(&mut self, _: &str, _: bool) -> Result<bool, PublicError> {
        let mut c=self.0.lock().unwrap(); c.cleanup_calls+=1;
        if c.refuse { Err(PublicError::new(503,"controlled_cleanup_refused")) } else { Ok(c.clean) }
    }
}
fn fixture() -> (M16Requests, Arc<Mutex<Control>>, Arc<Authority>) {
    let control=Arc::new(Mutex::new(Control::default()));
    let host=Arc::new(Authority {ready:AtomicBool::new(true),ids:Ids::default()});
    let service=M16Requests::new(build_live_m16_runtime(inputs()).unwrap(), host.clone(), Box::new(Controlled(control.clone())),Config::new(16,16,0.0).unwrap()).unwrap();
    (service,control,host)
}
#[test]
fn admitted_request_dispatches_and_releases_its_reservations() {
    let (s,c,_)=fixture();
    let baseline=s.placement_status().unwrap();
    let a=s.submit(document()).unwrap();
    let identity=s.route_identity(&a.request_id).unwrap();
    assert!(!s.reservations(&a.request_id).unwrap().is_empty());
    s.tick().unwrap();
    let dispatched=c.lock().unwrap().dispatched.clone();
    assert_eq!(dispatched.len(),1);
    assert_eq!(dispatched[0].identity,identity);
    assert_eq!(dispatched[0].context.request_id,a.request_id);
    assert_eq!(dispatched[0].manifest["path_id"],identity.path_id);
    s.tick().unwrap();
    s.tick().unwrap();
    assert_eq!(s.terminal(&a.request_id).unwrap(),None);
    assert!(!s.reservations(&a.request_id).unwrap().is_empty());
    c.lock().unwrap().clean=true;
    s.tick().unwrap();
    assert_eq!(s.terminal(&a.request_id).unwrap().as_deref(),Some("completed"));
    assert!(s.reservations(&a.request_id).unwrap().is_empty());
    assert_eq!(s.placement_status().unwrap(),baseline);
    let mut stream=s.subscribe(&a.request_id,None,Some(&a.session_token)).unwrap();
    for (sequence,kind) in ["accepted","lifecycle","token","lifecycle","completed"].iter().enumerate() {
        let e=stream.next().unwrap().unwrap();
        assert_eq!(e["sequence"],sequence);
        assert_eq!(e["type"],*kind);
        s.validate_event(&e).unwrap(); stream.ack(sequence as u64).unwrap();
    }
    assert!(stream.next().unwrap().is_none()); stream.close().unwrap();
    assert!(!s.cancel(&a.request_id,Some(&a.session_token)).unwrap());
}
