// SPDX-License-Identifier: AGPL-3.0-only
//! Pinned public evidence grammars. These validators do not observe resources,
//! qualify a physical gate, run a planner, or grant execution authority.
use crate::{ContractError, canonical, checks::*, performance, public::fields};
use serde_json::{Value, json};
use std::cmp::Ordering;
const C: &str = "public_evidence_invalid";
fn array(v: &Value) -> Result<&Vec<Value>, ContractError> {
    v.as_array().ok_or(ContractError(C))
}
fn cmp(a: &Value, b: &Value) -> Ordering {
    let a = a.to_string();
    let b = b.to_string();
    a.len().cmp(&b.len()).then_with(|| a.cmp(&b))
}
fn prefix_digest(v: &Value) -> bool {
    v.as_str()
        .is_some_and(|s| s.starts_with("sha256:") && s.chars().count() == 71)
}
fn privacy(v: &Value, forbidden: &[&str]) -> Result<(), ContractError> {
    match v {
        Value::Object(m) => {
            for (k, v) in m {
                require(!forbidden.contains(&k.as_str()), C)?;
                privacy(v, forbidden)?;
            }
        }
        Value::Array(a) => {
            for v in a {
                privacy(v, forbidden)?;
            }
        }
        _ => {}
    }
    Ok(())
}
fn finite_tree(v: &Value) -> Result<(), ContractError> {
    canonical::to_bytes(v).map_err(|_| ContractError(C))?;
    Ok(())
}
fn hashable(v: &Value) -> bool {
    !v.is_object() && !v.is_array()
}
fn unique(values: &[Value]) -> bool {
    values
        .iter()
        .enumerate()
        .all(|(i, v)| hashable(v) && !values[..i].iter().any(|p| python_equal(p, v)))
}
fn contains(values: &[Value], v: &Value) -> bool {
    values.iter().any(|x| python_equal(x, v))
}
pub fn cache(v: &Value) -> Result<(), ContractError> {
    fields(
        v,
        "protocol max_bytes entry_count used_bytes pinned_object_count",
        C,
    )?;
    require(
        v["protocol"] == "mycelium.assignment_artifact_cache.v1"
            && [
                "max_bytes",
                "entry_count",
                "used_bytes",
                "pinned_object_count",
            ]
            .iter()
            .all(|f| integer(&v[*f], 0))
            && integer(&v["max_bytes"], 1)
            && cmp(&v["used_bytes"], &v["max_bytes"]) != Ordering::Greater
            && cmp(&v["pinned_object_count"], &v["entry_count"]) != Ordering::Greater,
        C,
    )
}
fn add_count(n: &Value, count: usize) -> Result<Value, ContractError> {
    let s = n.to_string();
    let mut digits = s.into_bytes();
    let mut carry = count;
    for d in digits.iter_mut().rev() {
        let sum = usize::from(*d - b'0') + carry;
        *d = b'0' + (sum % 10) as u8;
        carry = sum / 10;
    }
    while carry > 0 {
        digits.insert(0, b'0' + (carry % 10) as u8);
        carry /= 10;
    }
    serde_json::from_slice(&digits).map_err(|_| ContractError(C))
}
pub fn materialization(v: &Value) -> Result<(), ContractError> {
    fields(
        v,
        "protocol assignment_id objects opened_object_ids cache_entry_count unassigned_object_count route_ready",
        C,
    )?;
    require(
        v["protocol"] == "mycelium.assignment_materialization.v1"
            && v["route_ready"] == false
            && v["assignment_id"].as_str().is_some_and(|s| !s.is_empty()),
        C,
    )?;
    let objects = array(&v["objects"])?;
    require(!objects.is_empty(), C)?;
    let mut ids = Vec::new();
    for o in objects {
        fields(o, "relative_path object_id tensor_digest size_bytes", C)?;
        let path = o["relative_path"].as_str().ok_or(ContractError(C))?;
        // PurePosixPath('.') has no parts and is accepted by the pinned source.
        require(
            !path.is_empty()
                && !path.starts_with('/')
                && (path == "."
                    || path
                        .split('/')
                        .all(|p| !p.is_empty() && p != "." && p != "..")),
            C,
        )?;
        require(
            prefix_digest(&o["object_id"])
                && prefix_digest(&o["tensor_digest"])
                && integer(&o["size_bytes"], 1),
            C,
        )?;
        ids.push(o["object_id"].clone());
    }
    require(
        unique(&ids)
            && python_equal(&v["opened_object_ids"], &json!(ids))
            && integer(&v["cache_entry_count"], 0)
            && integer(&v["unassigned_object_count"], 0),
        C,
    )?;
    require(
        v["cache_entry_count"] == add_count(&v["unassigned_object_count"], objects.len())?,
        C,
    )
}
pub fn placement(v: &Value) -> Result<(), ContractError> {
    fields(
        v,
        "protocol snapshot_digest evidence_bundle_digest snapshot_generation authority_generation verification_key_digest valid_until_unix_ms placement_provenance decode_mode quantization nodes links exclusions ab_deltas promotion route_ready",
        C,
    )?;
    require(
        v["protocol"] == "mycelium.m13_placement_projection.v1"
            && v["placement_provenance"] == "planner_v2"
            && v["route_ready"] == false
            && [
                "snapshot_digest",
                "evidence_bundle_digest",
                "verification_key_digest",
            ]
            .iter()
            .all(|f| digest(&v[*f]))
            && [
                "snapshot_generation",
                "authority_generation",
                "valid_until_unix_ms",
            ]
            .iter()
            .all(|f| integer(&v[*f], 0))
            && !array(&v["nodes"])?.is_empty(),
        C,
    )?;
    for f in ["links", "exclusions", "ab_deltas"] {
        array(&v[f])?;
    }
    privacy(
        v,
        &[
            "prompt",
            "response",
            "token_ids",
            "activation",
            "artifact_root",
            "private_key",
        ],
    )?;
    finite_tree(v)
}
pub fn topology(v: &Value) -> Result<(), ContractError> {
    fields(
        v,
        "protocol measurement_source decision allocation edges exclusions promotion route_ready",
        C,
    )?;
    require(
        v["protocol"] == "mycelium.m14_topology_projection.v1"
            && v["measurement_source"] == "iroh_activation_plane"
            && v["route_ready"] == false
            && v["decision"].is_object(),
        C,
    )?;
    let opened = array(&v["decision"]["opened_order"])?;
    require(opened.len() >= 3 && unique(opened), C)?;
    array(&v["allocation"])?;
    array(&v["exclusions"])?;
    let edges = array(&v["edges"])?;
    let pairs: Vec<_> = edges
        .iter()
        .filter(|e| e.is_object())
        .map(|e| (&e["src"], &e["dst"]))
        .collect();
    require(
        pairs.iter().all(|(s, d)| {
            hashable(s)
                && hashable(d)
                && contains(opened, s)
                && contains(opened, d)
                && !python_equal(s, d)
        }),
        C,
    )?;
    for s in opened {
        for d in opened {
            if !python_equal(s, d) {
                require(
                    pairs
                        .iter()
                        .any(|(a, b)| python_equal(s, a) && python_equal(d, b)),
                    C,
                )?;
            }
        }
    }
    privacy(
        v,
        &[
            "prompt",
            "response",
            "token_ids",
            "activation",
            "artifact_root",
            "private_key",
            "endpoint_addr",
        ],
    )?;
    finite_tree(v)
}
pub fn runtime(v: &Value) -> Result<(), ContractError> {
    fields(
        v,
        "protocol generated_at_monotonic_s deployment_id deployment_epoch topology_version graph_digest queue placements requests incidents batch_state claim_boundary performance_budgets",
        C,
    )?;
    require(v["protocol"] == "mycelium.concurrent_request_runtime.v1", C)?;
    fields(
        &v["queue"],
        "depth maximum_items queued_bytes maximum_bytes interactive_depth batch_depth active_request_ids maximum_active_requests",
        C,
    )?;
    let placements = array(&v["placements"])?;
    require(!placements.is_empty(), C)?;
    for p in placements {
        fields(
            p,
            "placement_id node_id memory_capacity_bytes reserved_memory_bytes free_memory_bytes kv_capacity_bytes reserved_kv_bytes free_kv_bytes workspace_capacity_bytes reserved_workspace_bytes free_workspace_bytes active_reservations maximum_reservations",
            C,
        )?;
    }
    let requests = array(&v["requests"])?;
    require(requests.len() <= 1024, C)?;
    for r in requests {
        fields(
            r,
            "request_id workload_profile_id qos_class phase path_id path_attempt path_manifest_digest topology_version path_state candidate_placement_ids placement_ids reservation_count admitted_at_monotonic_s queued_at_monotonic_s dispatch_at_monotonic_s terminal_at_monotonic_s queue_wait_ms terminal_state",
            C,
        )?;
    }
    let incidents = array(&v["incidents"])?;
    require(incidents.len() <= 256, C)?;
    for r in incidents {
        fields(
            r,
            "incident_id kind request_id scope state observed_at_monotonic_s retry_after_seconds",
            C,
        )?;
    }
    fields(
        &v["batch_state"],
        "mode maximum_runtime_batch_size observed_batches continuous_batching pipeline_overlap",
        C,
    )?;
    let budgets = array(&v["performance_budgets"])?;
    require(budgets.len() <= 16, C)?;
    for b in budgets {
        performance::validate_as("mycelium.performance_budget.v3", b)?;
    }
    privacy(
        v,
        &[
            "prompt",
            "response",
            "prompt_text",
            "response_text",
            "token_ids",
            "tokens",
            "activation",
            "activations",
            "tensor",
            "tensors",
            "kv_content",
            "credential",
            "secret",
            "runtime_endpoint",
            "endpoint_addr",
            "private_address",
            "artifact_root",
        ],
    )?;
    finite_tree(v)
}
fn observation(v: &Value) -> Result<(), ContractError> {
    fields(
        v,
        "profile_id request_id selected_candidate_id context_tokens output_tokens runtime_backends topology_version placement counters_before counters_after prediction observed signed_error absolute_relative_error budget_results overall_state",
        C,
    )?;
    let backends = array(&v["runtime_backends"])?;
    require(
        !backends.is_empty()
            && backends
                .iter()
                .all(|v| v.as_str().is_some_and(|s| !s.is_empty())),
        C,
    )?;
    let places = array(&v["placement"])?;
    require(!places.is_empty(), C)?;
    for p in places {
        fields(p, "node_id start end", C)?;
        let start = if p["start"].is_boolean() {
            json!(u8::from(p["start"] == true))
        } else {
            p["start"].clone()
        };
        let end = if p["end"].is_boolean() {
            json!(u8::from(p["end"] == true))
        } else {
            p["end"].clone()
        };
        require(
            p["node_id"].is_string()
                && integer(&start, 0)
                && integer(&end, 0)
                && cmp(&end, &start) == Ordering::Greater,
            C,
        )?;
    }
    for f in ["counters_before", "counters_after"] {
        fields(
            &v[f],
            "frames_sent frames_received applied_operation_count",
            C,
        )?;
    }
    fields(
        &v["prediction"],
        "ttft_ms tpot_ms output_goodput_tps scenario_id",
        C,
    )?;
    for f in ["observed", "signed_error"] {
        fields(&v[f], "ttft_ms tpot_ms output_goodput_tps", C)?;
    }
    fields(&v["absolute_relative_error"], "ttft tpot throughput", C)?;
    fields(
        &v["budget_results"],
        "ttft tpot throughput frames model_ttft model_tpot model_throughput peak_memory energy_thermal reconnect queueing admission_latency concurrency batch_shape",
        C,
    )?;
    require(one_of(&v["overall_state"], &["met", "failed"]), C)
}
fn py_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        _ => v.to_string(),
    }
}
pub fn comparison(v: &Value) -> Result<(), ContractError> {
    fields(
        v,
        "protocol planner_snapshot_digest evidence_bundle_digest profiles comparisons performance_budgets observations calibration_state deferred_to_m16 route_ready claim_boundary",
        C,
    )?;
    require(
        v["protocol"] == "mycelium.m15_plan_comparison.v1"
            && v["route_ready"] == false
            && one_of(
                &v["calibration_state"],
                &["predicted_unobserved", "partially_observed", "observed"],
            )
            && v["deferred_to_m16"]
                == json!([
                    "admission_latency",
                    "batch_shape",
                    "concurrency",
                    "queueing"
                ])
            && prefix_digest(&v["planner_snapshot_digest"])
            && prefix_digest(&v["evidence_bundle_digest"]),
        C,
    )?;
    let profiles = array(&v["profiles"])?;
    let comparisons = array(&v["comparisons"])?;
    require(
        !profiles.is_empty() && profiles.len() == comparisons.len(),
        C,
    )?;
    for (p, c) in profiles.iter().zip(comparisons) {
        require(
            p.is_object() && c.is_object() && python_equal(&p["profile_id"], &c["profile_id"]),
            C,
        )?;
        let candidates = array(&c["candidates"])?;
        require(
            candidates.iter().all(|i| {
                i.is_object()
                    && one_of(
                        &i["policy_id"],
                        &["balanced", "decode_tpot", "prefill_ttft"],
                    )
            }) && ["balanced", "decode_tpot", "prefill_ttft"]
                .iter()
                .all(|p| candidates.iter().any(|i| i["policy_id"] == *p)),
            C,
        )?;
        let selected: Vec<_> = candidates
            .iter()
            .filter(|i| i["selected"] == true)
            .collect();
        require(
            selected.len() == 1
                && python_equal(&selected[0]["candidate_id"], &c["selected_candidate_id"]),
            C,
        )?;
        let in_pareto = match c.get("pareto_candidate_ids") {
            None => false,
            Some(Value::Array(a)) => contains(a, &c["selected_candidate_id"]),
            Some(Value::String(s)) => c["selected_candidate_id"]
                .as_str()
                .is_some_and(|id| s.contains(id)),
            Some(Value::Object(m)) => c["selected_candidate_id"]
                .as_str()
                .is_some_and(|id| m.contains_key(id)),
            _ => false,
        };
        require(in_pareto, C)?;
    }
    let budgets = array(&v["performance_budgets"])?;
    let observations = array(&v["observations"])?;
    for b in budgets {
        performance::validate_as("mycelium.performance_budget.v2", b)?;
    }
    if v["calibration_state"] == "predicted_unobserved" {
        require(budgets.is_empty() && observations.is_empty(), C)?;
    } else if v["calibration_state"] == "observed" {
        for o in observations {
            observation(o)?;
        }
        let ids = |a: &[Value]| {
            let mut ids: Vec<_> = a.iter().map(|r| py_string(&r["profile_id"])).collect();
            ids.sort();
            ids
        };
        require(
            profiles.iter().all(|p| p.get("profile_id").is_some())
                && ids(profiles) == ids(budgets)
                && ids(profiles) == ids(observations),
            C,
        )?;
    }
    privacy(
        v,
        &[
            "prompt",
            "response",
            "prompt_text",
            "response_text",
            "token_ids",
            "tokens",
            "activation",
            "activations",
            "tensor",
            "tensors",
            "kv",
            "private_key",
            "credential",
            "artifact_root",
            "trace_path",
        ],
    )?;
    finite_tree(v)
}
fn relaxed_hex_digest(v: &Value) -> bool {
    let Some(s) = v
        .as_str()
        .filter(|s| s.starts_with("sha256:") && s.chars().count() == 71)
    else {
        return false;
    };
    let normalized: String = s[7..]
        .trim()
        .chars()
        .map(|c| {
            let cp = c as u32;
            crate::seed_transition::DECIMAL_ZEROES
                .iter()
                .find(|z| cp >= **z && cp - **z < 10)
                .map_or(c, |z| char::from(b'0' + (cp - z) as u8))
        })
        .collect();
    let mut s = normalized.as_str();
    if s.starts_with(['+', '-']) {
        s = &s[1..];
    }
    if s.starts_with("0x") || s.starts_with("0X") {
        s = &s[2..];
        if s.starts_with('_') {
            s = &s[1..];
        }
    }
    !s.is_empty()
        && s.as_bytes().iter().enumerate().all(|(i, b)| {
            b.is_ascii_hexdigit()
                || (*b == b'_'
                    && i > 0
                    && i + 1 < s.len()
                    && s.as_bytes()[i - 1].is_ascii_hexdigit()
                    && s.as_bytes()[i + 1].is_ascii_hexdigit())
        })
}
fn difference_equal(actual: &Value, a: &Value, b: &Value) -> Result<bool, ContractError> {
    if integer(a, 0) && integer(b, 0) {
        let order = cmp(a, b);
        let (large, small) = if order == Ordering::Less {
            (b, a)
        } else {
            (a, b)
        };
        let mut digits = large.to_string().into_bytes();
        let s = small.to_string();
        let mut small = s.bytes().rev();
        let mut borrow = 0i16;
        for d in digits.iter_mut().rev() {
            let n = i16::from(*d - b'0') - small.next().map_or(0, |b| i16::from(b - b'0')) - borrow;
            *d = b'0' + n.rem_euclid(10) as u8;
            borrow = i16::from(n < 0);
        }
        let first = digits
            .iter()
            .position(|b| *b != b'0')
            .unwrap_or(digits.len() - 1);
        let mut result = digits[first..].to_vec();
        if order == Ordering::Less {
            result.insert(0, b'-');
        }
        let expected: Value = serde_json::from_slice(&result).map_err(|_| ContractError(C))?;
        Ok(python_equal(actual, &expected))
    } else {
        Ok(python_equal(actual, &json!(number(a, C)? - number(b, C)?)))
    }
}
pub fn kv_gate(v: &Value) -> Result<(), ContractError> {
    const G: &str = "m23_kv_evidence_invalid";
    let check = || -> Result<(), ContractError> {
        fields(
            v,
            "protocol generated_at_unix_ms replay_capture_digest kv_capture_digest gates implemented performance_qualified promotion_state measurements claim_boundary evidence_digest",
            C,
        )?;
        require(
            v["protocol"] == "mycelium.m23_heterogeneous_kv_gate.v1"
                && integer(&v["generated_at_unix_ms"], 1)
                && relaxed_hex_digest(&v["replay_capture_digest"])
                && relaxed_hex_digest(&v["kv_capture_digest"]),
            C,
        )?;
        fields(
            &v["gates"],
            "same_route_model_stages_hosts same_prompt_and_budget exact_output_parity one_token_decode_every_stage all_stages_advanced_physical_counters kv_active_then_terminally_released no_fatal_or_cleanup_failure measured_tpot_improvement",
            C,
        )?;
        let gates = v["gates"].as_object().ok_or(ContractError(C))?;
        require(gates.values().all(Value::is_boolean), C)?;
        let m = &v["measurements"];
        fields(
            m,
            "replay_tpot_ms kv_tpot_ms tpot_delta_ms tpot_improvement_ratio replay_activation_output_bytes kv_activation_output_bytes activation_byte_delta replay_total_ms kv_total_ms",
            C,
        )?;
        for n in m.as_object().ok_or(ContractError(C))?.values() {
            number(n, C)?;
        }
        for f in [
            "replay_tpot_ms",
            "kv_tpot_ms",
            "replay_activation_output_bytes",
            "kv_activation_output_bytes",
            "replay_total_ms",
            "kv_total_ms",
        ] {
            require(number(&m[f], C)? >= 0.0, C)?;
        }
        let a = number(&m["tpot_delta_ms"], C)?;
        let b = number(&m["kv_tpot_ms"], C)? - number(&m["replay_tpot_ms"], C)?;
        require(
            (a - b).abs() <= 1e-9f64.max(1e-12 * a.abs()).max(1e-12 * b.abs())
                && difference_equal(
                    &m["activation_byte_delta"],
                    &m["kv_activation_output_bytes"],
                    &m["replay_activation_output_bytes"],
                )?,
            C,
        )?;
        let implemented = gates
            .iter()
            .filter(|(k, _)| k.as_str() != "measured_tpot_improvement")
            .all(|(_, v)| v == true);
        let performance = implemented && gates["measured_tpot_improvement"] == true;
        require(
            v["implemented"] == json!(implemented)
                && v["performance_qualified"] == json!(performance)
                && v["promotion_state"]
                    == if performance {
                        "qualified"
                    } else if implemented {
                        "implemented_not_performance_qualified"
                    } else {
                        "withheld"
                    }
                && v["claim_boundary"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty() && s.chars().count() <= 512)
                && relaxed_hex_digest(&v["evidence_digest"]),
            C,
        )?;
        let mut unsigned = v.clone();
        unsigned
            .as_object_mut()
            .ok_or(ContractError(C))?
            .remove("evidence_digest")
            .ok_or(ContractError(C))?;
        require(
            v["evidence_digest"]
                == canonical::sha256_prefixed(
                    &canonical::to_ascii_bytes(&unsigned).map_err(|_| ContractError(C))?,
                ),
            C,
        )
    };
    check().map_err(|_| ContractError(G))
}
