// SPDX-License-Identifier: AGPL-3.0-only
//! Rebuild candidate reports from recorded observations. This verifies arithmetic
//! and document relationships, not measured performance or promotion authority.
use crate::{ContractError, checks::*, execution_graph::numeric_cmp, performance, public::fields};
use serde_json::{Map, Value, json};
use std::{cmp::Ordering, collections::BTreeSet};
pub const PROTOCOL: &str = "mycelium.candidate_promotion_report.v1";
const C: &str = "candidate_promotion_report_invalid";
fn nonempty(v: &Value) -> bool {
    v.as_str().is_some_and(|s| !s.is_empty())
}
fn counts(v: &Value) -> bool {
    v.as_object().is_some_and(|o| {
        !o.is_empty() && o.len() <= 256 && o.iter().all(|(k, v)| !k.is_empty() && integer(v, 0))
    })
}
fn greater(a: &Value, b: &Value) -> bool {
    numeric_cmp(a, b) == Some(Ordering::Greater)
}
fn latency(observations: &[Value], field: &str) -> Result<Value, ContractError> {
    let mut values = observations
        .iter()
        .map(|o| number(&o[field], C))
        .collect::<Result<Vec<_>, _>>()?;
    values.sort_by(f64::total_cmp);
    let n = values.len();
    let median = if n % 2 == 0 {
        (values[n / 2 - 1] + values[n / 2]) / 2.0
    } else {
        values[n / 2]
    };
    require(median.is_finite(), C)?;
    let index = (0.95 * n as f64).ceil() as usize - 1;
    Ok(json!({"p50":median,"p95":values[index]}))
}
fn maximums(
    observations: &[Value],
    budget: &Value,
    input: &str,
    output: &str,
) -> Result<Value, ContractError> {
    let mut out = Map::new();
    for key in budget[output].as_object().ok_or(ContractError(C))?.keys() {
        let mut max = json!(0);
        for row in observations {
            if let Some(v) = row[input].get(key)
                && greater(v, &max)
            {
                max = v.clone();
            }
        }
        out.insert(key.clone(), max);
    }
    Ok(Value::Object(out))
}
/// Return the source-normalized report after independently rebuilding every
/// metric, ordered rejection reason, decision and route-ready disclaimer.
pub fn parse(v: &Value) -> Result<Value, ContractError> {
    fields(
        v,
        "protocol candidate_deployment_id incumbent_deployment_id planner_snapshot_digest placement_provenance performance_budget observations metrics decision reasons route_ready",
        C,
    )?;
    let budget = &v["performance_budget"];
    performance::validate_as("mycelium.performance_budget.v1", budget)
        .map_err(|_| ContractError(C))?;
    require(
        [
            "candidate_deployment_id",
            "incumbent_deployment_id",
            "planner_snapshot_digest",
        ]
        .iter()
        .all(|f| nonempty(&v[*f]))
            && v["candidate_deployment_id"] != v["incumbent_deployment_id"]
            && v["planner_snapshot_digest"]
                .as_str()
                .is_some_and(|s| s.starts_with("sha256:") && s.chars().count() == 71),
        C,
    )?;
    let input = v["observations"]
        .as_array()
        .filter(|a| (2..=256).contains(&a.len()))
        .ok_or(ContractError(C))?;
    let mut seen = BTreeSet::new();
    let mut observations = Vec::with_capacity(input.len());
    for row in input {
        fields(
            row,
            "case_id completed quality_passed negative_check_passed ttft_ms tpot_ms output_tokens_per_second peak_rss_bytes_by_member frames_per_request_by_stage",
            C,
        )?;
        require(
            nonempty(&row["case_id"])
                && seen.insert(row["case_id"].as_str().ok_or(ContractError(C))?)
                && ["completed", "quality_passed", "negative_check_passed"]
                    .iter()
                    .all(|f| row[*f].is_boolean())
                && counts(&row["peak_rss_bytes_by_member"])
                && counts(&row["frames_per_request_by_stage"]),
            C,
        )?;
        let mut out = row.clone();
        for f in ["ttft_ms", "tpot_ms", "output_tokens_per_second"] {
            let n = number(&row[f], C)?;
            require(n > 0.0, C)?;
            out[f] = json!(n);
        }
        observations.push(out);
    }
    let mut throughput = number(&observations[0]["output_tokens_per_second"], C)?;
    for row in &observations {
        throughput = throughput.min(number(&row["output_tokens_per_second"], C)?);
    }
    let metrics = json!({"sample_size":observations.len(),"ttft_ms":latency(&observations,"ttft_ms")?,"tpot_ms":latency(&observations,"tpot_ms")?,"minimum_output_tokens_per_second":throughput,"maximum_peak_rss_bytes_by_member":maximums(&observations,budget,"peak_rss_bytes_by_member","maximum_peak_rss_bytes_by_member")?,"maximum_frames_per_request_by_stage":maximums(&observations,budget,"frames_per_request_by_stage","maximum_frames_per_request_by_stage")?});
    let mut reasons = Vec::new();
    if greater(&budget["minimum_sample_size"], &json!(observations.len())) {
        reasons.push("minimum_sample_size_not_met".to_owned());
    }
    if observations.iter().any(|o| {
        ["completed", "quality_passed", "negative_check_passed"]
            .iter()
            .any(|f| o[*f] == false)
    }) {
        reasons.push("canary_check_failed".to_owned());
    }
    for phase in ["ttft_ms", "tpot_ms"] {
        for (metric, bound) in [("p50", "maximum_p50"), ("p95", "maximum_p95")] {
            if greater(&metrics[phase][metric], &budget[phase][bound]) {
                reasons.push(format!("{phase}_{metric}_exceeded"));
            }
        }
    }
    if greater(
        &budget["minimum_output_tokens_per_second"],
        &metrics["minimum_output_tokens_per_second"],
    ) {
        reasons.push("minimum_output_tokens_per_second_not_met".to_owned());
    }
    for field in [
        "maximum_peak_rss_bytes_by_member",
        "maximum_frames_per_request_by_stage",
    ] {
        if budget[field]
            .as_object()
            .ok_or(ContractError(C))?
            .iter()
            .any(|(key, bound)| greater(&metrics[field][key], bound))
        {
            reasons.push(format!("{field}_exceeded"));
        }
    }
    let out = json!({"protocol":PROTOCOL,"candidate_deployment_id":v["candidate_deployment_id"],"incumbent_deployment_id":v["incumbent_deployment_id"],"planner_snapshot_digest":v["planner_snapshot_digest"],"placement_provenance":"planner_v2","performance_budget":budget,"observations":observations,"metrics":metrics,"decision":if reasons.is_empty(){"promote"}else{"reject"},"reasons":reasons,"route_ready":false});
    require(python_equal(v, &out), C)?;
    Ok(out)
}
