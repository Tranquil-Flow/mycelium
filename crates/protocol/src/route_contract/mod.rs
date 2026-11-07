// SPDX-License-Identifier: AGPL-3.0-only
//! Planner intent and unqualified compatibility records; never live admission.
mod qualification;
mod sum;
use crate::{
    ContractError,
    checks::*,
    execution_graph::{numeric_cmp, truthy},
    public::fields,
};
pub use qualification::unqualified;
use serde_json::{Value, json};
use std::cmp::Ordering;
const C: &str = "route_plan_invalid";
fn selected(v: &Value, names: &str) -> Result<Value, ContractError> {
    let mut out = json!({});
    for n in names.split_whitespace() {
        out[n] = v.get(n).ok_or(ContractError(C))?.clone();
    }
    Ok(out)
}
fn iterable(v: &Value) -> Result<Vec<Value>, ContractError> {
    match v {
        Value::Array(a) => Ok(a.clone()),
        Value::String(s) => Ok(s.chars().map(|c| json!(c.to_string())).collect()),
        Value::Object(m) => Ok(m.keys().map(|k| json!(k)).collect()),
        _ => Err(ContractError(C)),
    }
}
fn compare(a: &Value, b: &Value, minimum: Ordering) -> Result<(), ContractError> {
    require(numeric_cmp(a, b).is_some_and(|o| o >= minimum), C)
}
fn hashable(v: &Value) -> bool {
    !v.is_array() && !v.is_object()
}
fn number(v: &Value) -> Result<f64, ContractError> {
    if let Some(b) = v.as_bool() {
        Ok(f64::from(u8::from(b)))
    } else {
        crate::checks::number(v, C)
    }
}
fn forbidden(v: &Value) -> bool {
    match v {
        Value::Object(m) => m.iter().any(|(k, v)| {
            [
                "loaded",
                "ready",
                "leased",
                "load_proof",
                "runtime_ready",
                "weights_loaded",
            ]
            .contains(&k.to_lowercase().as_str())
                || forbidden(v)
        }),
        Value::Array(a) => a.iter().any(forbidden),
        _ => false,
    }
}
/// Complete source parser with detached normalized defaults and structural
/// validation. Open outer/item fields remain open where the source selects them.
pub fn plan(v: &Value) -> Result<Value, ContractError> {
    let mut out = selected(
        v,
        "model snapshot_digest placements legal_tracks forward_edges loopbacks provenance workload_name metrics",
    )?;
    for (f, default) in [
        ("diagnostics", json!({})),
        ("handoff_state", json!("placement_intent_only")),
        ("protocol", json!("mycelium.route_plan.v2")),
    ] {
        out[f] = v.get(f).cloned().unwrap_or(default);
    }
    let model = &v["model"];
    fields(
        model,
        "model_id revision weight_digest architecture num_layers hidden_size dtype_bytes kv_heads head_dim weight_bytes",
        C,
    )?;
    require(
        truthy(&model["model_id"])
            && truthy(&model["revision"])
            && model["weight_digest"].as_str().is_some_and(|s| {
                s.len() == 71
                    && s.starts_with("sha256:")
                    && s[7..].bytes().all(|b| b.is_ascii_hexdigit())
            }),
        C,
    )?;
    for f in [
        "num_layers",
        "hidden_size",
        "dtype_bytes",
        "kv_heads",
        "head_dim",
        "weight_bytes",
    ] {
        compare(&model[f], &json!(0), Ordering::Greater)?;
    }
    let mut placements = Vec::new();
    for p in iterable(&v["placements"])? {
        let p = selected(
            &p,
            "placement_id replica_group_id node_id layer_range primary service_capacity_rps",
        )?;
        fields(&p["layer_range"], "start end", C)?;
        compare(&p["layer_range"]["start"], &json!(0), Ordering::Equal)?;
        compare(
            &p["layer_range"]["end"],
            &p["layer_range"]["start"],
            Ordering::Greater,
        )?;
        require(
            ["placement_id", "replica_group_id", "node_id"]
                .iter()
                .all(|f| truthy(&p[*f])),
            C,
        )?;
        compare(&p["service_capacity_rps"], &json!(0), Ordering::Equal)?;
        placements.push(p);
    }
    let mut tracks = Vec::new();
    for t in iterable(&v["legal_tracks"])? {
        let mut x = selected(&t, "track_id placement_ids traffic_fraction")?;
        x["placement_ids"] = json!(iterable(&t["placement_ids"])?);
        x["cost_ms"] = t.get("cost_ms").cloned().unwrap_or(json!(0.0));
        require(truthy(&x["track_id"]) && truthy(&x["placement_ids"]), C)?;
        compare(&x["traffic_fraction"], &json!(0), Ordering::Equal)?;
        compare(&json!(1), &x["traffic_fraction"], Ordering::Equal)?;
        compare(&x["cost_ms"], &json!(0), Ordering::Equal)?;
        tracks.push(x);
    }
    let mut edges = Vec::new();
    for e in iterable(&v["forward_edges"])? {
        let mut x = selected(&e, "src_placement_id dst_placement_id capacity_rps cost_ms")?;
        x["kind"] = e.get("kind").cloned().unwrap_or(json!("forward"));
        require(
            truthy(&x["src_placement_id"])
                && truthy(&x["dst_placement_id"])
                && !python_equal(&x["src_placement_id"], &x["dst_placement_id"])
                && one_of(&x["kind"], &["forward", "loopback"]),
            C,
        )?;
        compare(&x["capacity_rps"], &json!(0), Ordering::Equal)?;
        compare(&x["cost_ms"], &json!(0), Ordering::Equal)?;
        edges.push(x);
    }
    let mut loops = Vec::new();
    for e in iterable(&v["loopbacks"])? {
        let x = selected(
            &e,
            "src_placement_id dst_placement_id payload_bytes cost_ms",
        )?;
        compare(&x["payload_bytes"], &json!(0), Ordering::Greater)?;
        compare(&x["cost_ms"], &json!(0), Ordering::Equal)?;
        loops.push(x);
    }
    let mut provenance = if let Some(m) = v["provenance"].as_object() {
        m.clone()
    } else {
        let mut m = serde_json::Map::new();
        for pair in iterable(&v["provenance"])? {
            let p = iterable(&pair)?;
            require(p.len() == 2, C)?;
            let key = p[0].as_str().ok_or(ContractError(C))?;
            m.insert(key.to_owned(), p[1].clone());
        }
        m
    };
    provenance.remove("elapsed_ms");
    let explored = provenance
        .get("explored_candidates")
        .cloned()
        .unwrap_or(json!(0));
    // Python evaluates setdefault's default even when candidate_budget is present.
    let candidate =
        crate::seed_transition::coerced_integer(&explored).map_err(|_| ContractError(C))?;
    let candidate = if numeric_cmp(&candidate, &json!(1)).is_some_and(|o| o == Ordering::Greater) {
        candidate
    } else {
        json!(1)
    };
    provenance
        .entry("candidate_budget".to_owned())
        .or_insert(candidate);
    provenance
        .entry("budget_exhausted".to_owned())
        .or_insert(json!(false));
    let provenance = Value::Object(provenance);
    fields(
        &provenance,
        "mode globally_exact explored_candidates candidate_node_count candidate_budget budget_exhausted",
        C,
    )?;
    require(truthy(&provenance["mode"]), C)?;
    compare(
        &provenance["explored_candidates"],
        &json!(0),
        Ordering::Equal,
    )?;
    compare(
        &provenance["candidate_node_count"],
        &json!(0),
        Ordering::Equal,
    )?;
    compare(
        &provenance["candidate_budget"],
        &json!(0),
        Ordering::Greater,
    )?;
    compare(
        &provenance["candidate_budget"],
        &provenance["explored_candidates"],
        Ordering::Equal,
    )?;
    require(
        !(truthy(&provenance["globally_exact"]) && truthy(&provenance["budget_exhausted"])),
        C,
    )?;
    require(
        out["protocol"] == "mycelium.route_plan.v2"
            && out["handoff_state"] == "placement_intent_only"
            && truthy(&out["snapshot_digest"])
            && !forbidden(&out["metrics"])
            && !forbidden(&out["diagnostics"])
            && !placements.is_empty(),
        C,
    )?;
    let mut groups: Vec<(Value, Vec<usize>)> = Vec::new();
    for (i, p) in placements.iter().enumerate() {
        require(
            hashable(&p["placement_id"])
                && hashable(&p["replica_group_id"])
                && !placements[..i]
                    .iter()
                    .any(|q| python_equal(&p["placement_id"], &q["placement_id"])),
            C,
        )?;
        if let Some((_, members)) = groups
            .iter_mut()
            .find(|(g, _)| python_equal(g, &p["replica_group_id"]))
        {
            members.push(i);
        } else {
            groups.push((p["replica_group_id"].clone(), vec![i]));
        }
    }
    // Stable insertion sort can propagate Python's incomparable-key refusal.
    for i in 1..groups.len() {
        let mut j = i;
        while j > 0 {
            let a = &placements[groups[j - 1].1[0]]["layer_range"]["start"];
            let b = &placements[groups[j].1[0]]["layer_range"]["start"];
            let mut order = numeric_cmp(a, b).ok_or(ContractError(C))?;
            if order == Ordering::Equal {
                order = if let (Some(a), Some(b)) = (groups[j - 1].0.as_str(), groups[j].0.as_str())
                {
                    a.cmp(b)
                } else {
                    numeric_cmp(&groups[j - 1].0, &groups[j].0).ok_or(ContractError(C))?
                };
            }
            if order != Ordering::Greater {
                break;
            }
            groups.swap(j - 1, j);
            j -= 1;
        }
    }
    let mut expected = json!(0);
    for (_, members) in &groups {
        let range = &placements[members[0]]["layer_range"];
        require(
            members
                .iter()
                .all(|i| python_equal(&placements[*i]["layer_range"], range))
                && python_equal(&range["start"], &expected),
            C,
        )?;
        expected = range["end"].clone();
        let sum = sum::sum(members.iter().map(|i| placements[*i]["primary"].clone()))?;
        require(python_equal(&sum, &json!(1)), C)?;
    }
    require(python_equal(&expected, &model["num_layers"]), C)?;
    let group_index = |id: &Value| -> Result<usize, ContractError> {
        require(hashable(id), C)?;
        let i = placements
            .iter()
            .position(|p| python_equal(&p["placement_id"], id))
            .ok_or(ContractError(C))?;
        groups
            .iter()
            .position(|(_, m)| m.contains(&i))
            .ok_or(ContractError(C))
    };
    for e in &edges {
        require(
            group_index(&e["src_placement_id"])? + 1 == group_index(&e["dst_placement_id"])?,
            C,
        )?;
    }
    for e in &loops {
        require(
            group_index(&e["src_placement_id"])? + 1 == groups.len()
                && group_index(&e["dst_placement_id"])? == 0,
            C,
        )?;
    }
    if !tracks.is_empty() {
        let sum = number(&sum::sum(
            tracks.iter().map(|t| t["traffic_fraction"].clone()),
        )?)?;
        require((sum - 1.0).abs() <= 1e-12, C)?;
    }
    for t in &tracks {
        let ids = t["placement_ids"].as_array().ok_or(ContractError(C))?;
        require(ids.len() == groups.len(), C)?;
        for (i, id) in ids.iter().enumerate() {
            require(group_index(id)? == i, C)?;
        }
        for pair in ids.windows(2) {
            require(
                edges.iter().any(|e| {
                    python_equal(&e["src_placement_id"], &pair[0])
                        && python_equal(&e["dst_placement_id"], &pair[1])
                }),
                C,
            )?;
        }
        if ids.len() > 1 {
            require(
                loops.iter().any(|e| {
                    python_equal(&e["src_placement_id"], &ids[ids.len() - 1])
                        && python_equal(&e["dst_placement_id"], &ids[0])
                }),
                C,
            )?;
        }
    }
    if truthy(&provenance["globally_exact"]) {
        require(
            one_of(
                &provenance["mode"],
                &[
                    "exact_joint_enumeration",
                    "exact_topology_enumeration",
                    "held_karp",
                ],
            ),
            C,
        )?;
    }
    out["placements"] = json!(placements);
    out["legal_tracks"] = json!(tracks);
    out["forward_edges"] = json!(edges);
    out["loopbacks"] = json!(loops);
    out["provenance"] = provenance;
    Ok(out)
}
