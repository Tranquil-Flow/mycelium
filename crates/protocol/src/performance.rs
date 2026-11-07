// SPDX-License-Identifier: AGPL-3.0-only
//! Closed budget documents, not measurements or runtime policy installation.
use crate::{
    ContractError,
    checks::*,
    public::{fields, safe_integer},
};
use serde_json::Value;
use std::collections::BTreeSet;
const C: &str = "performance_budget_invalid";
fn id(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.len() <= 128
            && s.as_bytes()[0].is_ascii_alphanumeric()
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._:~-".contains(&b))
    })
}
fn number(v: &Value, positive: bool) -> bool {
    v.as_f64()
        .is_some_and(|n| n.is_finite() && if positive { n > 0.0 } else { n >= 0.0 })
}
fn latency(v: &Value) -> bool {
    fields(v, "maximum_p50 maximum_p95", C).is_ok()
        && number(&v["maximum_p50"], true)
        && number(&v["maximum_p95"], true)
        && v["maximum_p50"].as_f64() <= v["maximum_p95"].as_f64()
}
fn integer_map(v: &Value) -> bool {
    v.as_object().is_some_and(|o| {
        !o.is_empty()
            && o.len() <= 256
            && o.iter()
                .all(|(k, v)| id(&Value::String(k.clone())) && safe_integer(v, 1))
    })
}
/// Validate the explicitly selected source version; never infer a version from
/// a malformed or contradictory document's fields.
pub fn validate_as(protocol: &str, v: &Value) -> Result<(), ContractError> {
    require(v.get("protocol").is_some_and(|p| p == protocol), C)?;
    match protocol {
        "mycelium.performance_budget.v1" => {
            fields(
                v,
                "protocol budget_id workload_label minimum_sample_size ttft_ms tpot_ms minimum_output_tokens_per_second maximum_peak_rss_bytes_by_member maximum_frames_per_request_by_stage execution_scope queueing_budget_state",
                C,
            )?;
            require(
                id(&v["budget_id"])
                    && id(&v["workload_label"])
                    && safe_integer(&v["minimum_sample_size"], 1)
                    && latency(&v["ttft_ms"])
                    && latency(&v["tpot_ms"])
                    && number(&v["minimum_output_tokens_per_second"], true)
                    && integer_map(&v["maximum_peak_rss_bytes_by_member"])
                    && integer_map(&v["maximum_frames_per_request_by_stage"])
                    && v["execution_scope"] == "sequential_observed"
                    && v["queueing_budget_state"] == "deferred_to_m16",
                C,
            )
        }
        "mycelium.performance_budget.v2" => {
            fields(
                v,
                "protocol budget_id profile_id minimum_sample_size ttft_ms_maximum tpot_ms_maximum minimum_output_tokens_per_second maximum_frames_per_request maximum_relative_model_error execution_scope peak_memory_budget_state energy_thermal_budget_state reconnect_budget_state queueing_budget_state admission_latency_budget_state concurrency_budget_state batch_shape_budget_state",
                C,
            )?;
            fields(
                &v["maximum_relative_model_error"],
                "ttft tpot throughput",
                C,
            )?;
            require(
                id(&v["budget_id"])
                    && id(&v["profile_id"])
                    && safe_integer(&v["minimum_sample_size"], 1)
                    && safe_integer(&v["maximum_frames_per_request"], 1)
                    && [
                        "ttft_ms_maximum",
                        "tpot_ms_maximum",
                        "minimum_output_tokens_per_second",
                    ]
                    .iter()
                    .all(|f| number(&v[*f], true))
                    && ["ttft", "tpot", "throughput"]
                        .iter()
                        .all(|f| number(&v["maximum_relative_model_error"][*f], false))
                    && v["execution_scope"] == "sequential_observed"
                    && [
                        "peak_memory_budget_state",
                        "energy_thermal_budget_state",
                        "reconnect_budget_state",
                    ]
                    .iter()
                    .all(|f| v[*f] == "approved_exclusion")
                    && [
                        "queueing_budget_state",
                        "admission_latency_budget_state",
                        "concurrency_budget_state",
                        "batch_shape_budget_state",
                    ]
                    .iter()
                    .all(|f| v[*f] == "deferred_to_m16"),
                C,
            )
        }
        "mycelium.performance_budget.v3" => {
            fields(
                v,
                "protocol budget_id profile_id evidence_scope observed_request_count dimensions overall_state",
                C,
            )?;
            require(
                id(&v["budget_id"])
                    && id(&v["profile_id"])
                    && v["evidence_scope"] == "concurrent_physical_observed"
                    && safe_integer(&v["observed_request_count"], 1),
                C,
            )?;
            let a = v["dimensions"].as_array().ok_or(ContractError(C))?;
            let required: BTreeSet<_> = [
                "admission_latency_p95_ms",
                "queue_wait_p95_ms",
                "maximum_queue_depth",
                "completed_requests",
                "interactive_latency_regression_ratio",
                "batch_starvation_interval_ms",
                "cancellation_release_latency_ms",
                "runtime_batch_size",
            ]
            .into_iter()
            .collect();
            require(a.len() == required.len(), C)?;
            let mut found = BTreeSet::new();
            let mut failed = false;
            let mut excluded = false;
            for d in a {
                fields(
                    d,
                    "dimension state bound observed unit evidence_digest reason",
                    C,
                )?;
                found.insert(d["dimension"].as_str().ok_or(ContractError(C))?);
                require(
                    one_of(&d["state"], &["met", "failed", "approved_exclusion"])
                        && number(&d["bound"], false)
                        && number(&d["observed"], false)
                        && id(&d["unit"])
                        && digest(&d["evidence_digest"])
                        && d["reason"].as_str().is_some_and(|s| !s.is_empty()),
                    C,
                )?;
                failed |= d["state"] == "failed";
                excluded |= d["state"] == "approved_exclusion";
                if d["dimension"] == "runtime_batch_size" && d["state"] == "approved_exclusion" {
                    require(d["observed"].as_f64() == Some(1.0), C)?;
                }
            }
            require(
                found == required
                    && v["overall_state"]
                        == if failed {
                            "failed"
                        } else if excluded {
                            "met_with_approved_exclusions"
                        } else {
                            "met"
                        },
                C,
            )
        }
        _ => Err(ContractError("unsupported_contract_protocol")),
    }
}
