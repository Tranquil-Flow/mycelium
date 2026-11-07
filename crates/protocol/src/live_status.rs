// SPDX-License-Identifier: AGPL-3.0-only
//! Pure `PhysicalLiveRoute.public_status` projection at the pinned oracle.
//! Lifecycle, liveness and swarm metrics are independently observed inputs, not
//! manufactured by this projection. It never authenticates observations or runs IO.
use crate::{
    ContractError, canonical,
    checks::{integer, python_equal},
    execution_graph::{numeric_cmp, truthy},
};
use serde_json::{Map, Value, json};
use std::{
    cmp::Ordering,
    collections::{BTreeMap, BTreeSet},
};
const C: ContractError = ContractError("live_route_projection_invalid");
type Result<T> = std::result::Result<T, ContractError>;
/// Source-specific registry inputs; no operational policy defaults.
pub struct Limits {
    pub fatal_code_chars: usize,
    pub cooperative_work_ms: f64,
    pub cleanup_ms: u64,
    pub maximum_concurrent_requests: u64,
    pub a10_intervals: usize,
    pub a10_observations: usize,
}
fn obj(v: &Value) -> Result<&Map<String, Value>> {
    v.as_object().ok_or(C)
}
fn field<'a>(v: &'a Value, k: &str) -> Result<&'a Value> {
    v.get(k).ok_or(C)
}
fn get<'a>(v: &'a Value, k: &str, default: &'a Value) -> Result<&'a Value> {
    Ok(obj(v)?.get(k).unwrap_or(default))
}
fn map_or_empty(v: &Value) -> Value {
    if v.is_object() { v.clone() } else { json!({}) }
}
fn list(v: &Value) -> Result<Vec<Value>> {
    match v {
        Value::Array(a) => Ok(a.clone()),
        Value::Object(m) => Ok(m.keys().map(|s| json!(s)).collect()),
        Value::String(s) => Ok(s.chars().map(|c| json!(c.to_string())).collect()),
        _ => Err(C),
    }
}
fn int(v: &Value) -> Result<Value> {
    crate::seed_transition::coerced_integer(v).map_err(|_| C)
}
fn dictionary(v: &Value) -> Result<Value> {
    if v.is_object() {
        return Ok(v.clone());
    }
    let mut out = Map::new();
    for item in list(v)? {
        let pair = list(&item)?;
        if pair.len() != 2 {
            return Err(C);
        }
        out.insert(pair[0].as_str().ok_or(C)?.into(), pair[1].clone());
    }
    Ok(Value::Object(out))
}
fn string(v: &Value) -> Result<String> {
    Ok(match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Number(n) => {
            if n.as_str().contains(['.', 'e', 'E']) {
                canonical::python_float_repr(n.as_f64().ok_or(C)?).map_err(|_| C)?
            } else {
                n.to_string()
            }
        }
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(repr).collect::<Result<Vec<_>>>()?.join(", ")
        ),
        Value::Object(m) => format!(
            "{{{}}}",
            m.iter()
                .map(|(k, v)| Ok(format!("{}: {}", crate::gossip::repr::string(k), repr(v)?)))
                .collect::<Result<Vec<_>>>()?
                .join(", ")
        ),
    })
}
fn repr(v: &Value) -> Result<String> {
    if let Some(s) = v.as_str() {
        Ok(crate::gossip::repr::string(s))
    } else {
        string(v)
    }
}
fn sorted(v: &Value) -> Result<Vec<Value>> {
    let mut a = list(v)?;
    let mut bad = false;
    a.sort_by(|a, b| {
        if let (Some(a), Some(b)) = (a.as_str(), b.as_str()) {
            a.cmp(b)
        } else if let Some(o) = numeric_cmp(a, b) {
            o
        } else {
            bad = true;
            Ordering::Equal
        }
    });
    if bad { Err(C) } else { Ok(a) }
}
fn maximum(a: Value, b: Value) -> Value {
    if numeric_cmp(&a, &b) == Some(Ordering::Less) {
        b
    } else {
        a
    }
}
// Aggregate counters are nonnegative Python integers, not bounded machine sums.
fn add(a: &Value, b: &Value) -> Result<Value> {
    let a = a.to_string();
    let b = b.to_string();
    let mut out = Vec::new();
    let mut carry = 0;
    let mut ai = a.bytes().rev();
    let mut bi = b.bytes().rev();
    loop {
        let x = ai.next();
        let y = bi.next();
        if x.is_none() && y.is_none() && carry == 0 {
            break;
        }
        let n = x.map_or(0, |x| x - b'0') + y.map_or(0, |x| x - b'0') + carry;
        out.push(b'0' + n % 10);
        carry = n / 10;
    }
    out.reverse();
    serde_json::from_slice(&out).map_err(|_| C)
}
const TRIPLE: [&str; 3] = [
    "applied_operation_count",
    "prefill_operation_count",
    "decode_operation_count",
];
fn counter_material(o: &Value) -> Result<Option<Value>> {
    let d = if o.is_object() {
        o.get("details").cloned().unwrap_or(json!({}))
    } else {
        json!({})
    };
    if !d.is_object() {
        return Ok(None);
    }
    let t = map_or_empty(&d["transport"]);
    let tc = map_or_empty(&d["transport_counters"]);
    let r = map_or_empty(&d["runtime"]);
    if !truthy(&t) && !truthy(&tc) && !truthy(&r) {
        return Ok(None);
    }
    let mut m = json!({"frames_sent":int(t.get("remote_frames_sent").unwrap_or(tc.get("remote_frames_sent").unwrap_or(&json!(0))))?,"frames_received":int(t.get("remote_frames_received").unwrap_or(tc.get("remote_frames_received").unwrap_or(&json!(0))))?});
    if TRIPLE.iter().all(|k| integer(&r[*k], 0)) {
        for k in TRIPLE {
            m[k] = r[k].clone();
        }
    }
    Ok(Some(m))
}
fn peer_counters(snapshot: &Value, health: &Value) -> Result<Value> {
    let materials = [counter_material(snapshot)?, counter_material(health)?];
    let mut selected = None;
    for m in materials
        .iter()
        .flatten()
        .filter(|m| m.get(TRIPLE[0]).is_some())
    {
        if selected.is_none_or(|s: &Value| {
            numeric_cmp(&s[TRIPLE[0]], &m[TRIPLE[0]]) == Some(Ordering::Less)
        }) {
            selected = Some(m)
        }
    }
    let chosen = selected.or_else(|| materials.iter().flatten().next());
    let mut out = json!({});
    for k in [
        "frames_sent",
        "frames_received",
        TRIPLE[0],
        TRIPLE[1],
        TRIPLE[2],
    ] {
        out[k] = chosen.and_then(|m| m.get(k)).cloned().unwrap_or(json!(0));
    }
    Ok(out)
}
fn aggregate_observation(o: &Value) -> Result<Value> {
    let d = get(o, "details", &json!({}))?.clone();
    if !d.is_object() {
        return Ok(json!({"frames_sent":0,"frames_received":0,"applied_operation_count":0}));
    }
    let t = map_or_empty(&d["transport"]);
    let tc = map_or_empty(&d["transport_counters"]);
    let r = map_or_empty(&d["runtime"]);
    let rc = map_or_empty(&d["runtime_counters"]);
    Ok(
        json!({"frames_sent":int(t.get("remote_frames_sent").unwrap_or(tc.get("remote_frames_sent").unwrap_or(&json!(0))))?,"frames_received":int(t.get("remote_frames_received").unwrap_or(tc.get("remote_frames_received").unwrap_or(&json!(0))))?,"applied_operation_count":int(r.get("applied_operation_count").unwrap_or(rc.get("applied_operation_count").unwrap_or(&json!(0))))?}),
    )
}
fn health(v: &Value, l: &Limits) -> Value {
    let d = map_or_empty(&v["details"]);
    let fatal = &d["transport_fatal_error"];
    json!({"data_plane_health_observed":truthy(v),"sidecar_process_alive":d["sidecar_process"]["alive"].as_bool(),"transport_running":d["transport_running"].as_bool(),"transport_fatal":fatal.is_object(),"transport_fatal_code":if fatal.is_object(){json!(fatal["code"].as_str().filter(|s|!s.is_empty()).map(|s|s.chars().take(l.fatal_code_chars).collect::<String>()).unwrap_or("transport_failed".into()))}else{Value::Null}})
}
fn int_le(v: &Value, max: u64) -> bool {
    integer(v, 0) && numeric_cmp(v, &json!(max)) != Some(Ordering::Greater)
}
fn interruptibility(v: &Value, l: &Limits) -> Value {
    if !v.is_object() || v["reason"] == "runtime_unavailable_or_not_cooperative" {
        return Value::Null;
    }
    if v["work_unit"] != "bounded_qwen2_replay_chunk" {
        if v["runtime_backend"] == "mobile-rust-qwen2" {
            return Value::Null;
        }
        if v.get("materialization").is_some() || v["work_unit"] == "transformer_stage" {
            let valid = match v["materialization"].as_str() {
                Some("per_layer") => v["work_unit"] == "transformer_layer",
                Some("per_stage") => {
                    v["work_unit"] == "transformer_stage" && v["runtime_backend"] == "mlx"
                }
                _ => false,
            };
            if !valid {
                return Value::Null;
            }
            if v["cooperative_bound_candidate"] == true
                && (!integer(&v["observed_work_unit_count"], 1)
                    || !v["maximum_observed_work_unit_ms"]
                        .as_f64()
                        .is_some_and(|n| n >= 0.0 && n < l.cooperative_work_ms))
            {
                return Value::Null;
            }
        }
        return v.clone();
    }
    let fields = "runtime_backend decode_mode work_unit maximum_observed_work_unit_ms observed_work_unit_count maximum_total_cleanup_ms physical_proof_required backend_candidate cooperative_bound_candidate";
    let n = &v["maximum_observed_work_unit_ms"];
    let numeric = n.is_null()
        || if n
            .as_number()
            .is_some_and(|n| !n.as_str().contains(['.', 'e', 'E']))
        {
            int_le(n, (1u64 << 53) - 1)
        } else {
            n.as_f64().is_some_and(|n| n.is_finite() && n >= 0.0)
        };
    if fields.split_whitespace().any(|k| v.get(k).is_none())
        || v["runtime_backend"] != "mobile-rust-qwen2"
        || v["decode_mode"] != "complete_context_replay"
        || v["cooperative_interruption"] != true
        || v["request_scoped_cleanup"] != true
        || v["shared_process_termination_used"] != false
        || v["bound_kind"] != "source_work_not_wall_time"
        || !v["maximum_total_cleanup_ms"].is_null()
        || v["cooperative_bound_candidate"] != false
        || v["physical_proof_required"] != true
        || !v["backend_candidate"].is_boolean()
        || !int_le(&v["observed_work_unit_count"], (1u64 << 53) - 1)
        || !numeric
    {
        return Value::Null;
    }
    Value::Object(
        fields
            .split_whitespace()
            .map(|k| (k.into(), v[k].clone()))
            .collect(),
    )
}
fn exact(v: &Value, fields: &str) -> bool {
    v.as_object().is_some_and(|m| {
        m.len() == fields.split_whitespace().count()
            && fields.split_whitespace().all(|k| m.contains_key(k))
    })
}
fn pseudo(v: &Value, empty: bool) -> bool {
    v.as_str().is_some_and(|s| {
        (empty && s.is_empty()) || (s.starts_with("sha256:") && s.chars().count() == 71)
    })
}
fn finite(v: &Value) -> Option<f64> {
    v.as_f64().filter(|n| n.is_finite())
}
fn publication(v: &Value, l: &Limits) -> Value {
    let valid = || -> Option<()> {
        if !exact(
            v,
            "protocol clock route_path_ref route_attempt_ref stage_count intervals dropped_interval_count runtime_batch_observations dropped_observation_count bounds",
        ) || v["protocol"] != "mycelium.a10_live_publication.v1"
        {
            return None;
        }
        let c = &v["clock"];
        if !exact(c, "domain_ref monotonic_anchor wall_anchor resolution")
            || !pseudo(&c["domain_ref"], false)
            || finite(&c["monotonic_anchor"]).is_none()
            || finite(&c["wall_anchor"]).is_none()
            || finite(&c["resolution"])? <= 0.0
        {
            return None;
        }
        if !pseudo(&v["route_path_ref"], false)
            || !pseudo(&v["route_attempt_ref"], false)
            || !integer(&v["stage_count"], 1)
        {
            return None;
        }
        let intervals = v["intervals"].as_array()?;
        if intervals.len() > l.a10_intervals {
            return None;
        }
        let mut refs = BTreeSet::new();
        for i in intervals {
            if !exact(
                i,
                "interval_ref hop_kind request_ref microbatch_ref stage_index phase started_at finished_at clock_resolution input_digest output_digest runtime_batch_ref",
            ) {
                return None;
            }
            let compute = i["hop_kind"] == "compute";
            if !compute && i["hop_kind"] != "commit" {
                return None;
            }
            let r = i["interval_ref"].as_str()?;
            if r.is_empty()
                || !refs.insert(r)
                || !pseudo(&i["request_ref"], false)
                || !pseudo(&i["microbatch_ref"], false)
                || !pseudo(&i["input_digest"], true)
                || !pseudo(&i["output_digest"], true)
            {
                return None;
            }
            let index = &i["stage_index"];
            if !(integer(index, 0) || index.as_i64() == Some(-1))
                || numeric_cmp(index, &v["stage_count"]) != Some(Ordering::Less)
                || (compute != integer(index, 0))
            {
                return None;
            }
            if finite(&i["finished_at"])? < finite(&i["started_at"])?
                || finite(&i["clock_resolution"]).is_none()
                || !i["phase"].is_string()
                || !i["runtime_batch_ref"].is_string()
            {
                return None;
            }
        }
        if !integer(&v["dropped_interval_count"], 0) || !integer(&v["dropped_observation_count"], 0)
        {
            return None;
        }
        let observations = v["runtime_batch_observations"].as_array()?;
        if observations.len() > l.a10_observations
            || observations
                .iter()
                .any(|o| !o.is_object() || o["protocol"] != "mycelium.runtime_batch_observation.v1")
        {
            return None;
        }
        if !python_equal(
            &v["bounds"],
            &json!({"maximum_intervals":l.a10_intervals,"maximum_observations":l.a10_observations}),
        ) {
            return None;
        }
        Some(())
    };
    if valid().is_some() {
        v.clone()
    } else {
        Value::Null
    }
}
/// `input` is a detached snapshot held under the consumer's route lock AFTER
/// lost-process and replica-loss reconciliation. Liveness, route_alive and
/// swarm_hops must come from their real independent owners. No submitted status
/// object is accepted as authority; counters and identity are recomputed here.
pub fn project(v: &Value, l: &Limits) -> Result<Value> {
    for k in "graph plan peers snapshots health endpoints fatal recent_inferences incidents qualification replica_qualifications replica_loss placement topology token_path_enabled route_alive swarm_hops liveness".split_whitespace(){field(v,k)?;}
    let g = field(v, "graph")?;
    let mut stages = Vec::new();
    let mut by_node: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    for s in list(field(g, "stages")?)? {
        obj(&s)?;
        obj(field(&s, "layer_range")?)?;
        for p in list(field(&s, "placements")?)? {
            let n = field(&p, "node_id")?;
            if n.is_array() || n.is_object() {
                return Err(C);
            }
            let projection = json!({"stage_id":field(&s,"stage_id")?,"placement_id":field(&p,"placement_id")?,"node_id":n,"runtime_backend":field(&p,"runtime_backend")?,"start_layer":field(&s["layer_range"],"start_layer")?,"end_layer_exclusive":field(&s["layer_range"],"end_layer_exclusive")?,"component_roles":list(field(&s,"component_roles")?)?});
            stages.push(projection.clone());
            by_node.entry(n.to_string()).or_default().push(projection);
        }
    }
    let mut peers = Vec::new();
    let mut modes = BTreeSet::new();
    let mut aggregate =
        json!({"frames_sent":0,"frames_received":0,"applied_operation_count":0,"fatal":v["fatal"]});
    for n in sorted(&v["peers"])? {
        let n = n.as_str().ok_or(C)?;
        let empty = json!({});
        let snapshot = get(&v["snapshots"], n, &empty)?;
        let h = get(&v["health"], n, &empty)?;
        let d = get(snapshot, "details", &empty)?;
        let r = get(d, "runtime", &empty)?;
        obj(r)?;
        let mode = r["mode"].as_str();
        if let Some(m) = mode {
            modes.insert(m.to_owned());
        }
        let arch = r["architecture"].as_str();
        let resources = &d["host_resources"];
        let supported = arch
            .and_then(|a| {
                resources
                    .get("decode_modes_by_architecture")
                    .and_then(|m| m.get(a))
            })
            .and_then(Value::as_array)
            .map(|a| a.iter().map(string).collect::<Result<Vec<_>>>())
            .transpose()?
            .unwrap_or_default();
        let placements = by_node
            .get(&json!(n).to_string())
            .cloned()
            .unwrap_or_default();
        let mut pc = Map::new();
        for p in &placements {
            if r["placement_counters"].is_object()
                && (p["placement_id"].is_array() || p["placement_id"].is_object())
            {
                return Err(C);
            }
            let Some(id) = p["placement_id"].as_str() else {
                continue;
            };
            if let Some(counts) = r["placement_counters"].get(id).filter(|v| v.is_object()) {
                let fields = [
                    "prefill_operation_count",
                    "decode_operation_count",
                    "active_state_count",
                    "active_kv_bytes",
                ];
                if fields.iter().all(|k| integer(&counts[*k], 0)) {
                    pc.insert(
                        id.into(),
                        Value::Object(
                            fields
                                .iter()
                                .map(|k| ((*k).into(), counts[*k].clone()))
                                .collect(),
                        ),
                    );
                }
            }
        }
        let mut peer = peer_counters(snapshot, h)?;
        obj(&peer)?;
        peer.as_object_mut()
            .ok_or(C)?
            .extend(obj(&health(h, l))?.clone());
        peer["node_id"] = json!(n);
        peer["placements"] = json!(placements);
        peer["placement_counters"] = json!(pc);
        peer["decode_mode"] = json!(mode);
        peer["architecture"] = json!(arch);
        peer["supported_decode_modes"] = json!(supported);
        for (output, input) in [
            ("active_kv_state_count", "active_state_count"),
            ("active_kv_bytes", "active_kv_bytes"),
            ("peak_kv_bytes", "peak_kv_bytes"),
            ("prefill_input_token_count", "prefill_input_token_count"),
            ("decode_input_token_count", "decode_input_token_count"),
            ("activation_output_bytes", "activation_output_bytes"),
            ("retained_result_count", "retained_result_count"),
        ] {
            peer[output] = int(r.get(input).unwrap_or(&json!(0)))?;
        }
        peer["current_position"] = r
            .get("current_position")
            .filter(|n| {
                n.as_number()
                    .is_some_and(|n| !n.as_str().contains(['.', 'e', 'E']))
            })
            .cloned()
            .unwrap_or(Value::Null);
        peer["release_state"] = json!(string(r.get("release_state").unwrap_or(&json!("unknown")))?);
        peer["last_release_reason"] = json!(r["last_release_reason"].as_str());
        let mut releases = Map::new();
        if let Some(m) = r["release_counts"].as_object() {
            for (k, v) in m {
                releases.insert(k.clone(), int(v)?);
            }
        }
        peer["release_counts"] = json!(releases);
        peer["interruptibility"] = interruptibility(&d["interruptibility"], l);
        peer["a10_publication"] = publication(&d["a10_publication"], l);
        peers.push(peer);
        let a = aggregate_observation(snapshot)?;
        let b = aggregate_observation(h)?;
        for k in ["frames_sent", "frames_received", "applied_operation_count"] {
            aggregate[k] = add(
                &aggregate[k],
                &maximum(json!(0), maximum(a[k].clone(), b[k].clone())),
            )?;
        }
    }
    let mut endpoints = Vec::new();
    for n in sorted(&v["endpoints"])? {
        let n = n.as_str().ok_or(C)?;
        endpoints.push(field(&v["endpoints"][n], "endpoint_id")?.clone());
    }
    let deployment = field(&v["plan"], "deployment_id")?;
    let version = field(g, "topology_version")?;
    let identity =
        json!({"deployment_id":deployment,"topology_version":version,"endpoints":endpoints});
    let identity = canonical::to_ascii_bytes(&identity).map_err(|_| C)?;
    let qualification = if v["qualification"].is_null() {
        json!({"protocol":"mycelium.product_concurrency_liveness_qualification.v1","deployment_id":field(g,"deployment_id")?,"qualification_digest":canonical::sha256_prefixed(b"a4-physical-proof-unavailable"),"maximum_concurrent_requests":l.maximum_concurrent_requests,"cancellation_and_cleanup_bound_ms":l.cleanup_ms,"cooperative_interruption_proven":false,"request_scoped_cleanup_proven":false,"shared_process_termination_used":false,"publisher_generation_fencing_proven":false,"scoped_liveness_proven":false,"eligible":false,"evidence_digest":canonical::sha256_prefixed(b"a4-physical-evidence-unavailable")})
    } else {
        dictionary(&v["qualification"])?
    };
    let replicas = list(&v["replica_qualifications"])?
        .iter()
        .map(dictionary)
        .collect::<Result<Vec<_>>>()?;
    let mut out = json!({"protocol":"mycelium.live_route_status.v1","route_alive":v["route_alive"],"simulated":false,"token_path_enabled":v["token_path_enabled"]==true,"swarm_hops":v["swarm_hops"],"route_identity_digest":canonical::sha256_prefixed(&identity),"deployment_id":deployment,"model_id":field(g,"model_id")?,"topology_version":version,"decode_mode":if modes.len()==1{modes.first().map(String::as_str).ok_or(C)?}else{"mixed"},"counters":aggregate,"stages":stages,"peers":peers,"recent_inferences":list(&v["recent_inferences"] )?,"incidents":list(&v["incidents"] )?,"liveness":v["liveness"],"concurrency_liveness_qualification":qualification,"replica_track_qualification":replicas,"replica_loss_placement_ids":sorted(&v["replica_loss"] )?});
    for k in ["placement", "topology"] {
        if !v[k].is_null() {
            out[k] = v[k].clone();
        }
    }
    Ok(out)
}
