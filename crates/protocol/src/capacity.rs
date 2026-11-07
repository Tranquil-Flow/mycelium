// SPDX-License-Identifier: AGPL-3.0-only
//! Reconstruct capacity claims from bounded recorded observations. Parsing does
//! not measure resources, authenticate a profile, or qualify a route/release.
use crate::{ContractError, canonical, checks::*, public::fields, strict};
use serde_json::{Value, json};
use std::cmp::Ordering;
pub const PROTOCOL: &str = "mycelium.capacity_profile.v1";
pub const MAX_PROFILE_DOCUMENT_BYTES: usize = 256 * 1024;
pub const MAX_CAPACITY_OBSERVATIONS: usize = 256;
const C: &str = "capacity_profile_invalid";
fn cmp_int(a: &Value, b: &Value) -> Ordering {
    let a = a.to_string();
    let b = b.to_string();
    a.len().cmp(&b.len()).then_with(|| a.cmp(&b))
}
fn finite(v: &Value, positive: bool) -> Result<f64, ContractError> {
    let n = number(v, C)?;
    require(if positive { n > 0.0 } else { n >= 0.0 }, C)?;
    Ok(n)
}
fn boundary(points: &[Value], limit: usize, ttft: f64, tpot: f64) -> Result<Value, ContractError> {
    let Some(p) = points.get(limit) else {
        return Ok(json!({"kind":"highest_observed","concurrency":limit}));
    };
    let mut reasons = Vec::new();
    let kind = if p["safe"] == false {
        if p["oom"] == true {
            reasons.push("oom");
        }
        if p["thermal_throttled"] == true {
            reasons.push("thermal_throttled");
        }
        if cmp_int(&p["peak_memory_bytes"], &p["memory_budget_bytes"]) == Ordering::Greater {
            reasons.push("memory_budget_exceeded");
        }
        "first_unsafe_observation"
    } else {
        if number(&p["p95_ttft_ms"], C)? > ttft {
            reasons.push("ttft_p95_slo");
        }
        if number(&p["p95_tpot_ms"], C)? > tpot {
            reasons.push("tpot_p95_slo");
        }
        "first_slo_miss"
    };
    Ok(json!({"kind":kind,"concurrency":p["concurrency"],"reasons":reasons}))
}
fn reconstruct(v: &Value) -> Result<Value, ContractError> {
    fields(
        v,
        "protocol key policy points max_safe_concurrency interactive_concurrency_limit batch_concurrency_limit safety_boundary interactive_boundary evidence_scope qualification_evaluated route_ready release_ready profile_digest",
        C,
    )?;
    fields(
        &v["key"],
        "model_digest source_evidence_digest quantization backend runtime_build hardware_class power_mode context_bucket kv_mode",
        C,
    )?;
    require(
        digest(&v["key"]["model_digest"])
            && digest(&v["key"]["source_evidence_digest"])
            && [
                "quantization",
                "backend",
                "runtime_build",
                "hardware_class",
                "power_mode",
                "context_bucket",
                "kv_mode",
            ]
            .iter()
            .all(|f| {
                v["key"][*f]
                    .as_str()
                    .is_some_and(|s| !s.is_empty() && s.chars().count() <= 256)
            }),
        C,
    )?;
    fields(
        &v["policy"],
        "ttft_p95_slo_ms tpot_p95_slo_ms min_samples",
        C,
    )?;
    let ttft = finite(&v["policy"]["ttft_p95_slo_ms"], true)?;
    let tpot = finite(&v["policy"]["tpot_p95_slo_ms"], true)?;
    require(integer(&v["policy"]["min_samples"], 1), C)?;
    let input = v["points"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= MAX_CAPACITY_OBSERVATIONS)
        .ok_or(ContractError(C))?;
    let mut points = Vec::with_capacity(input.len());
    for (i, p) in input.iter().enumerate() {
        fields(
            p,
            "concurrency sample_count p95_ttft_ms p95_tpot_ms aggregate_output_tps peak_memory_bytes memory_budget_bytes oom thermal_throttled safe interactive_slo_met",
            C,
        )?;
        require(
            p["concurrency"].as_u64() == Some((i + 1) as u64)
                && integer(&p["concurrency"], 1)
                && integer(&p["sample_count"], 1)
                && integer(&p["peak_memory_bytes"], 0)
                && integer(&p["memory_budget_bytes"], 1)
                && p["oom"].is_boolean()
                && p["thermal_throttled"].is_boolean(),
            C,
        )?;
        let safe = p["oom"] == false
            && p["thermal_throttled"] == false
            && cmp_int(&p["peak_memory_bytes"], &p["memory_budget_bytes"]) != Ordering::Greater;
        let metrics = ["p95_ttft_ms", "p95_tpot_ms", "aggregate_output_tps"];
        let missing = metrics.iter().any(|f| p[*f].is_null());
        let mut evaluated = p.clone();
        if missing {
            require(!safe && metrics.iter().all(|f| p[*f].is_null()), C)?;
        } else {
            for f in metrics {
                evaluated[f] = json!(finite(&p[f], safe)?);
            }
        }
        let interactive = if safe {
            require(
                cmp_int(&p["sample_count"], &v["policy"]["min_samples"]) != Ordering::Less,
                C,
            )?;
            number(&evaluated["p95_ttft_ms"], C)? <= ttft
                && number(&evaluated["p95_tpot_ms"], C)? <= tpot
        } else {
            false
        };
        evaluated["safe"] = json!(safe);
        evaluated["interactive_slo_met"] = json!(interactive);
        points.push(evaluated);
    }
    let safe_limit = points.iter().take_while(|p| p["safe"] == true).count();
    let interactive = points[..safe_limit]
        .iter()
        .take_while(|p| p["interactive_slo_met"] == true)
        .count();
    require(interactive > 0, C)?;
    let mut batch = 1;
    let mut best = number(&points[0]["aggregate_output_tps"], C)?;
    for (i, p) in points[..safe_limit].iter().enumerate().skip(1) {
        let rate = number(&p["aggregate_output_tps"], C)?;
        if rate > best {
            batch = i + 1;
            best = rate;
        }
    }
    let mut out = json!({"protocol":PROTOCOL,"key":v["key"],"policy":{"ttft_p95_slo_ms":ttft,"tpot_p95_slo_ms":tpot,"min_samples":v["policy"]["min_samples"]},"points":points,"max_safe_concurrency":safe_limit,"interactive_concurrency_limit":interactive,"batch_concurrency_limit":batch,"safety_boundary":boundary(&points,safe_limit,ttft,tpot)?,"interactive_boundary":boundary(&points,interactive,ttft,tpot)?,"evidence_scope":"bounded_local_samples","qualification_evaluated":false,"route_ready":false,"release_ready":false});
    out["profile_digest"] = json!(canonical::digest(&out).map_err(|_| ContractError(C))?);
    Ok(out)
}
/// Source-compatible raw boundary: exact UTF-8 canonical bytes, duplicate-free
/// object, size bound, and complete reconstruction of every derived claim.
pub fn parse(raw: &[u8]) -> Result<Value, ContractError> {
    let v =
        strict::canonical_object(raw, MAX_PROFILE_DOCUMENT_BYTES).map_err(|_| ContractError(C))?;
    let expected = reconstruct(&v)?;
    require(
        canonical::to_bytes(&expected).map_err(|_| ContractError(C))? == raw,
        C,
    )?;
    Ok(expected)
}
/// Value-based composition must first use the caller's bounded raw ingress.
pub fn validate(v: &Value) -> Result<(), ContractError> {
    parse(&canonical::to_bytes(v).map_err(|_| ContractError(C))?)?;
    Ok(())
}
