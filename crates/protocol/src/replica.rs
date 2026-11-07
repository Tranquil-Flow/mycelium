// SPDX-License-Identifier: AGPL-3.0-only
//! Closed replica plan, qualification, runtime and benchmark wire contracts.
//! Source: mycelium_replica_contracts.py:100-704 @ f4e366e9.
//! A valid document is neither execution authorization nor a measured benchmark.
use crate::{ContractError, canonical, checks::*, public::fields};
use serde_json::Value;
use std::collections::BTreeSet;

fn text(v: &Value) -> bool {
    v.as_str().is_some_and(|s| !s.is_empty() && s.len() <= 256)
}
fn int(v: &Value) -> bool {
    v.as_u64().is_some_and(|n| n <= i64::MAX as u64)
}
fn float(v: &Value) -> bool {
    v.is_number() && v.as_f64().is_some_and(f64::is_finite)
}
fn strict_float(v: &Value) -> bool {
    float(v)
        && v.as_number()
            .is_some_and(|n| n.to_string().contains(['.', 'e', 'E']))
}
fn list(v: &Value, min: usize, max: usize, predicate: fn(&Value) -> bool) -> bool {
    v.as_array()
        .is_some_and(|a| (min..=max).contains(&a.len()) && a.iter().all(predicate))
}
fn unique(v: &Value) -> bool {
    v.as_array()
        .is_some_and(|a| (0..a.len()).all(|i| !a[..i].contains(&a[i])))
}
fn schema(v: &Value, spec: &str) -> bool {
    let Some(o) = v.as_object() else {
        return false;
    };
    let rules: Vec<_> = spec.split_whitespace().collect();
    o.len() == rules.len()
        && rules.iter().all(|rule| {
            let Some((field, kind)) = rule.split_once(':') else {
                return false;
            };
            let Some(value) = o.get(field) else {
                return false;
            };
            match kind {
                "text" => text(value),
                "int" => int(value),
                "digest" => digest(value),
                "float" => float(value),
                "bool" => value.is_boolean(),
                "false" => value == false,
                "list" => value.is_array(),
                "object" => value.is_object(),
                "texts" => list(value, 0, 256, text),
                "any" => true,
                _ => one_of(value, &kind.split('|').collect::<Vec<_>>()),
            }
        })
}
fn privacy(v: &Value, depth: usize, left: &mut usize) -> bool {
    if depth > 8 || *left == 0 {
        return false;
    }
    *left -= 1;
    match v {
        Value::Null | Value::Bool(_) => true,
        Value::Number(n) => {
            if n.to_string().contains(['.', 'e', 'E']) {
                float(v)
            } else {
                n.as_i64().is_some()
            }
        }
        Value::String(s) => s.len() <= 256,
        Value::Array(a) => a.len() <= 256 && a.iter().all(|v| privacy(v, depth + 1, left)),
        Value::Object(o) => {
            o.len() <= 256
                && o.iter().all(|(k, v)| {
                    let folded = k.to_lowercase().replace('ſ', "s").replace('ß', "ss");
                    !k.is_empty()
                        && k.len() <= 256
                        && ![
                            "activation",
                            "activations",
                            "address",
                            "addresses",
                            "command",
                            "credential",
                            "credentials",
                            "exception",
                            "kv",
                            "kv_block",
                            "kv_blocks",
                            "logit",
                            "logits",
                            "output",
                            "outputs",
                            "path",
                            "paths",
                            "private_key",
                            "prompt",
                            "prompts",
                            "raw_address",
                            "secret",
                            "secrets",
                            "text",
                            "token_id",
                            "token_ids",
                        ]
                        .contains(&folded.as_str())
                        && privacy(v, depth + 1, left)
                })
        }
    }
}
fn initial(v: &Value, protocol: &str, spec: &str, c: &'static str) -> Result<(), ContractError> {
    require(schema(v, spec) && v["protocol"] == protocol, c)?;
    require(privacy(v, 0, &mut 4096), c)
}
const REASONS: &[&str] = &[
    "artifact_verification_failed",
    "load_proof_failed",
    "startup_challenge_failed",
    "parity_mismatch",
    "memory_budget_exceeded",
    "cleanup_budget_exceeded",
    "directed_link_unqualified",
    "stale_authority",
    "mixed_generation",
    "illegal_track",
    "zero_final_flow",
    "unknown_failure_domain",
    "neutral_marginal_gain",
    "owner_authority_missing",
    "replica_loss",
];
pub fn qualification_digest(v: &Value) -> Result<String, ContractError> {
    let mut v = v.clone();
    require(v.is_object(), "invalid_replica_qualification")?;
    let placeholder = Value::String(format!("sha256:{}", "0".repeat(64)));
    v["qualification_id"] = placeholder.clone();
    v["qualification_digest"] = placeholder;
    canonical::digest(&v).map_err(|_| ContractError("invalid_replica_qualification"))
}
pub fn validate_as(protocol: &str, v: &Value) -> Result<(), ContractError> {
    match protocol {
        "mycelium.replica_plan.v1" => plan(v),
        "mycelium.replica_qualification.v1" => qualification(v),
        "mycelium.replica_runtime.v1" => runtime(v),
        "mycelium.replica_benchmark.v1" => benchmark(v, false),
        "mycelium.replica_benchmark.v2" => benchmark(v, true),
        _ => Err(ContractError("unsupported_contract_protocol")),
    }
}
fn plan(v: &Value) -> Result<(), ContractError> {
    let c = "invalid_replica_plan";
    initial(
        v,
        "mycelium.replica_plan.v1",
        "protocol:any plan_id:text plan_digest:digest deployment_id:text deployment_epoch:int base_qualification_digest:digest issued_at_unix_ms:int model_id:text model_revision:text representation_digest:digest route_generation:int route_ready:false replica_groups:list legal_tracks:list directed_edges:list traffic_fractions:list predicted_marginal_gain_fraction:float uncertainty:object failure_domain_facts:list zero_flow_removals:any rejections:list workload_digest:digest",
        c,
    )?;
    // The oracle iterates zero_flow_removals without requiring a list. Preserve
    // accepted empty mappings and strings as well as ordinary text lists.
    let removed = &v["zero_flow_removals"];
    let removals_ok = match removed {
        Value::Array(a) => a.iter().all(text),
        Value::Object(o) => o.keys().all(|s| !s.is_empty() && s.len() <= 256),
        Value::String(_) => true,
        _ => false,
    };
    require(removals_ok, c)?;
    let groups = v["replica_groups"].as_array().ok_or(ContractError(c))?;
    let tracks = v["legal_tracks"].as_array().ok_or(ContractError(c))?;
    require(groups.len() >= 2 && !tracks.is_empty(), c)?;
    for g in groups {
        require(
            schema(g, "group_id:text layer_range:list")
                && list(&g["layer_range"], 2, 2, int)
                && g["layer_range"][0].as_u64() <= g["layer_range"][1].as_u64(),
            c,
        )?;
    }
    for t in tracks {
        require(
            schema(t, "track_id:text placement_sequence:list")
                && list(&t["placement_sequence"], 2, 64, text)
                && unique(&t["placement_sequence"]),
            c,
        )?;
    }
    let u = &v["uncertainty"];
    require(
        schema(u, "lower:float upper:float") && u["lower"].as_f64() <= u["upper"].as_f64(),
        c,
    )?;
    let mut total = 0.0;
    for f in v["traffic_fractions"].as_array().ok_or(ContractError(c))? {
        require(schema(f, "track_id:text fraction:float"), c)?;
        let n = f["fraction"].as_f64().ok_or(ContractError(c))?;
        require((0.0..=1.0).contains(&n), c)?;
        total += n;
    }
    require((total - 1.0).abs() <= 0.001, c)
}
fn qualification(v: &Value) -> Result<(), ContractError> {
    let c = "invalid_replica_qualification";
    initial(
        v,
        "mycelium.replica_qualification.v1",
        "protocol:any qualification_id:digest qualification_digest:digest deployment_id:text deployment_epoch:int replica_group_id:text placement_id:text placement_ids:list track_id:text traffic_fraction:float qualifier_generation:int issued_at_unix_ms:int expires_at_unix_ms:int evidence_bundle_digest:digest load_proof_digest:digest assignment_digest:digest artifact_verification_digest:digest parity_verified:bool startup_challenge_passed:bool memory_within_bounds:bool cleanup_within_bounds:bool directed_link_qualified:bool workload_envelope_digest:digest rejected_reasons:texts route_ready:bool",
        c,
    )?;
    let placements = v["placement_ids"].as_array().ok_or(ContractError(c))?;
    require(
        list(&v["placement_ids"], 2, 64, text)
            && unique(&v["placement_ids"])
            && placements.contains(&v["placement_id"])
            && v["traffic_fraction"]
                .as_f64()
                .is_some_and(|n| n > 0.0 && n <= 1.0)
            && v["expires_at_unix_ms"].as_u64().is_some_and(|n| n >= 1)
            && v["expires_at_unix_ms"].as_u64() >= v["issued_at_unix_ms"].as_u64(),
        c,
    )?;
    let reasons = v["rejected_reasons"].as_array().ok_or(ContractError(c))?;
    let reason_strings = reasons
        .iter()
        .map(|v| v.as_str().ok_or(ContractError(c)))
        .collect::<Result<Vec<_>, _>>()?;
    require(
        reason_strings.windows(2).all(|w| w[0] < w[1])
            && reason_strings.iter().all(|s| REASONS.contains(s)),
        c,
    )?;
    if v["qualifier_generation"] == 0
        || v["expires_at_unix_ms"].as_u64() <= v["issued_at_unix_ms"].as_u64()
    {
        require(reason_strings.contains(&"stale_authority"), c)?;
    }
    for (field, reason) in [
        ("parity_verified", "parity_mismatch"),
        ("startup_challenge_passed", "startup_challenge_failed"),
        ("memory_within_bounds", "memory_budget_exceeded"),
        ("cleanup_within_bounds", "cleanup_budget_exceeded"),
        ("directed_link_qualified", "directed_link_unqualified"),
    ] {
        require(v[field] == true || reason_strings.contains(&reason), c)?;
    }
    require(v["route_ready"] == reasons.is_empty(), c)?;
    let digest = qualification_digest(v)?;
    require(
        v["qualification_id"] == digest && v["qualification_digest"] == digest,
        c,
    )
}
fn runtime(v: &Value) -> Result<(), ContractError> {
    let c = "invalid_replica_runtime";
    initial(
        v,
        "mycelium.replica_runtime.v1",
        "protocol:any runtime_digest:digest deployment_id:text deployment_epoch:int qualification_digest:digest route_generation:int snapshot_generation:int observed_at_unix_ms:int tracks:list placements:list batch_mode:none|replica_only|tracked admitted_requests:list rejected_admissions:list replica_loss_actions:list",
        c,
    )?;
    for r in v["admitted_requests"].as_array().ok_or(ContractError(c))? {
        require(
            schema(
                r,
                "request_id:text track_id:text placement_sequence:list edge_set:list",
            ) && list(&r["placement_sequence"], 2, 64, text)
                && list(&r["edge_set"], 1, 128, text),
            c,
        )?;
    }
    Ok(())
}
fn benchmark(v: &Value, v2: bool) -> Result<(), ContractError> {
    let c = if v2 {
        "invalid_replica_benchmark_v2"
    } else {
        "invalid_replica_benchmark"
    };
    let spec = if v2 {
        "protocol:any benchmark_run_id:text benchmark_protocol:any benchmark_protocol_digest:digest workload_manifest_digest:digest deployment_id:text route_generation:int started_at_unix_ms:int finished_at_unix_ms:int calibration:any scheduled_pairs:any minimum_valid_pairs:any pair_selection:any material_threshold_fraction:any valid_pair_ids:any excluded_windows:any paired_improvements:any point_estimate_fraction:any paired_95_percent_bootstrap_lower_bound_fraction:any prediction_error_fraction:any decision:material|not_material|inconclusive reasons:texts qualification_claim:false promotion_authorized:false provenance:object"
    } else {
        "protocol:any benchmark_run_id:text benchmark_protocol_digest:digest workload_manifest_digest:digest deployment_id:text deployment_epoch:int route_generation:int started_at_unix_ms:int finished_at_unix_ms:int primary_only_samples:list replicated_samples:list paired_improvements:list point_estimate_fraction:float paired_95_percent_bootstrap_lower_bound_fraction:float prediction_error_fraction:float decision:material|not_material|inconclusive reasons:texts qualification_claim:false promotion_authorized:false provenance:object"
    };
    initial(
        v,
        if v2 {
            "mycelium.replica_benchmark.v2"
        } else {
            "mycelium.replica_benchmark.v1"
        },
        spec,
        c,
    )?;
    require(
        v["finished_at_unix_ms"].as_u64() >= v["started_at_unix_ms"].as_u64()
            && schema(
                &v["provenance"],
                "software_digest:digest configuration_digest:digest instrumentation_digest:digest",
            ),
        c,
    )?;
    if !v2 {
        return require(list(&v["paired_improvements"], 6, 6, float), c);
    }
    require(
        v["benchmark_protocol"] == "mycelium.a5_benchmark_acceptance.v2"
            && v["scheduled_pairs"].as_u64() == Some(16)
            && v["minimum_valid_pairs"].as_u64() == Some(12)
            && v["pair_selection"] == "all_valid_pairs_no_selection_or_trimming"
            && strict_float(&v["material_threshold_fraction"])
            && v["material_threshold_fraction"].as_f64() == Some(0.1),
        c,
    )?;
    calibration(&v["calibration"], c)?;
    let valid = v["valid_pair_ids"].as_array().ok_or(ContractError(c))?;
    let mut valid_ids = BTreeSet::new();
    let mut previous = None;
    for id in valid {
        let id = id.as_u64().filter(|n| *n < 16).ok_or(ContractError(c))?;
        require(previous.is_none_or(|p| id > p) && valid_ids.insert(id), c)?;
        previous = Some(id);
    }
    let excluded = v["excluded_windows"].as_array().ok_or(ContractError(c))?;
    let mut accounted = valid_ids.clone();
    let mut previous = None;
    for w in excluded {
        require(
            schema(
                w,
                "pair_id:int baseline_index:int reason:any baseline_completed:int baseline_offered:int",
            ),
            c,
        )?;
        let id = w["pair_id"]
            .as_u64()
            .filter(|n| *n < 16)
            .ok_or(ContractError(c))?;
        let completed = w["baseline_completed"].as_u64().ok_or(ContractError(c))?;
        let offered = w["baseline_offered"]
            .as_u64()
            .filter(|n| *n > 0)
            .ok_or(ContractError(c))?;
        require(
            previous.is_none_or(|p| id > p)
                && accounted.insert(id)
                && w["baseline_index"].as_u64() == Some(2 * id + id % 2)
                && w["reason"] == "baseline_completion_below_minimum_fraction"
                && completed <= offered
                && u128::from(completed) * 10 < u128::from(offered) * 9,
            c,
        )?;
        previous = Some(id);
    }
    require(
        (accounted.is_empty() || accounted.len() == 16)
            && (!v["calibration"]["selected_offered_arrival_rate_per_second"].is_null()
                || accounted.is_empty()),
        c,
    )?;
    let improvements = v["paired_improvements"]
        .as_array()
        .ok_or(ContractError(c))?;
    require(
        improvements.iter().all(strict_float)
            && (v["prediction_error_fraction"].is_null()
                || strict_float(&v["prediction_error_fraction"])),
        c,
    )?;
    if improvements.is_empty() {
        return require(
            v["decision"] == "inconclusive"
                && v["point_estimate_fraction"].is_null()
                && v["paired_95_percent_bootstrap_lower_bound_fraction"].is_null()
                && v["prediction_error_fraction"].is_null()
                && (valid.len() < 12
                    || v["reasons"]
                        .as_array()
                        .is_some_and(|a| a.contains(&Value::from("nonfinite_measurement")))),
            c,
        );
    }
    require(
        improvements.len() == valid.len()
            && valid.len() >= 12
            && strict_float(&v["point_estimate_fraction"])
            && strict_float(&v["paired_95_percent_bootstrap_lower_bound_fraction"]),
        c,
    )?;
    let point = v["point_estimate_fraction"]
        .as_f64()
        .ok_or(ContractError(c))?;
    let lower = v["paired_95_percent_bootstrap_lower_bound_fraction"]
        .as_f64()
        .ok_or(ContractError(c))?;
    // CPython 3.14 float sum uses compensated accumulation.
    let mut sum: f64 = 0.0;
    let mut correction = 0.0;
    for n in improvements {
        let x = n.as_f64().ok_or(ContractError(c))?;
        let t = sum + x;
        correction += if sum.abs() >= x.abs() {
            (sum - t) + x
        } else {
            (x - t) + sum
        };
        sum = t;
    }
    require(
        (point - (sum + correction) / improvements.len() as f64).abs() <= 1e-9,
        c,
    )?;
    let at_least = |x: f64| x > 0.1 || (x - 0.1).abs() <= 1e-12;
    let expected = if !at_least(point) {
        "not_material"
    } else if !at_least(lower) {
        "inconclusive"
    } else {
        "material"
    };
    require(v["decision"] == expected, c)
}
fn calibration(v: &Value, c: &'static str) -> Result<(), ContractError> {
    fields(
        v,
        "mode scored rate_ladder_per_second trials selected_offered_arrival_rate_per_second",
        c,
    )?;
    let ladder = [4.0, 2.0, 1.0, 0.5, 0.25, 0.125, 0.0625];
    let expected = Value::Array(ladder.iter().copied().map(Value::from).collect());
    require(
        v["mode"] == "baseline"
            && v["scored"] == false
            && python_equal(&v["rate_ladder_per_second"], &expected),
        c,
    )?;
    let trials = v["trials"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= ladder.len())
        .ok_or(ContractError(c))?;
    let mut qualifying = None;
    for (ordinal, t) in trials.iter().enumerate() {
        require(
            qualifying.is_none()
                && schema(
                    t,
                    "ordinal:int offered_arrival_rate_per_second:any request_count:int offered_request_count:int completed:int",
                ),
            c,
        )?;
        let count = t["request_count"].as_u64().ok_or(ContractError(c))?;
        let offered = t["offered_request_count"]
            .as_u64()
            .ok_or(ContractError(c))?;
        let completed = t["completed"].as_u64().ok_or(ContractError(c))?;
        require(
            t["ordinal"].as_u64() == Some(ordinal as u64)
                && python_equal(
                    &t["offered_arrival_rate_per_second"],
                    &Value::from(ladder[ordinal]),
                )
                && count >= 60
                && offered >= count
                && completed <= count,
            c,
        )?;
        if u128::from(completed) * 10 >= u128::from(offered) * 9 {
            qualifying = Some(ladder[ordinal]);
        }
    }
    let selected = &v["selected_offered_arrival_rate_per_second"];
    require(
        match qualifying {
            None => trials.len() == ladder.len() && selected.is_null(),
            Some(q) => strict_float(selected) && selected.as_f64() == Some(q),
        },
        c,
    )
}
