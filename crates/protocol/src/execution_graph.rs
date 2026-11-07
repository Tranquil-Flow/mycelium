// SPDX-License-Identifier: AGPL-3.0-only
//! Oracle execution-graph parsing and structural validation. This preserves the
//! source's open objects and numeric coercions; it is not runtime authorization.
//! Source: mycelium_router/{serialization,validation}.py @ f4e366e9.
use crate::{ContractError, checks::*};
use serde_json::{Map, Value};
use std::cmp::Ordering;

#[derive(Debug, Clone)]
pub struct Graph(Value);
impl Graph {
    pub fn as_value(&self) -> &Value {
        &self.0
    }
    pub fn into_value(self) -> Value {
        self.0
    }
}
pub(crate) fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::Number(n) => n.as_f64().is_none_or(|f| f != 0.0),
    }
}
fn int_text(v: &Value) -> Option<String> {
    if let Some(b) = v.as_bool() {
        return Some(u8::from(b).to_string());
    }
    // json.loads reads the integer -0, JSON's one signed integer zero, as int 0 (RU-001).
    let s = v.as_number().map(ToString::to_string)?;
    (!s.contains(['.', 'e', 'E'])).then(|| if s == "-0" { "0".to_owned() } else { s })
}
fn float(v: &Value) -> Option<f64> {
    v.as_bool()
        .map(|b| u8::from(b) as f64)
        .or_else(|| v.as_f64())
}
fn cmp_int(a: &str, b: &str) -> Ordering {
    let an = a.starts_with('-');
    let bn = b.starts_with('-');
    if an != bn {
        return bn.cmp(&an);
    }
    let a = a.trim_start_matches('-');
    let b = b.trim_start_matches('-');
    let o = a.len().cmp(&b.len()).then_with(|| a.cmp(b));
    if an { o.reverse() } else { o }
}
pub(crate) fn numeric_cmp(a: &Value, b: &Value) -> Option<Ordering> {
    match (int_text(a), int_text(b)) {
        (Some(a), Some(b)) => Some(cmp_int(&a, &b)),
        (Some(a), None) => {
            let f = float(b)?;
            if f.is_infinite() {
                return Some(if f > 0.0 {
                    Ordering::Less
                } else {
                    Ordering::Greater
                });
            }
            let floor = f.floor();
            let floor = if floor == 0.0 {
                "0".to_owned()
            } else {
                format!("{floor:.0}")
            };
            let o = cmp_int(&a, &floor);
            Some(if o == Ordering::Equal && f.fract() != 0.0 {
                Ordering::Less
            } else {
                o
            })
        }
        (None, Some(_)) => numeric_cmp(b, a).map(Ordering::reverse),
        (None, None) => float(a)?.partial_cmp(&float(b)?),
    }
}
pub(crate) fn range_count(
    start: &Value,
    end: &Value,
    count: &Value,
) -> Result<bool, ContractError> {
    if let (Some(s), Some(e)) = (int_text(start), int_text(end)) {
        // Validation has established 0 <= start < end. Subtract decimal digits
        // without truncating Python's arbitrary-precision layer indices.
        let mut result = Vec::new();
        let mut borrow = 0i16;
        let mut ss = s.bytes().rev();
        for d in e.bytes().rev() {
            let n = i16::from(d - b'0') - i16::from(ss.next().unwrap_or(b'0') - b'0') - borrow;
            borrow = i16::from(n < 0);
            result.push((if n < 0 { n + 10 } else { n }) as u8 + b'0');
        }
        while result.len() > 1 && result.last() == Some(&b'0') {
            result.pop();
        }
        result.reverse();
        let text: String = result.into_iter().map(char::from).collect();
        let value: Value = serde_json::from_str(&text)
            .map_err(|_| ContractError("invalid_graph_numeric_result"))?;
        Ok(python_equal(&value, count))
    } else {
        Ok(float(start).zip(float(end)).is_some_and(|(s, e)| {
            Value::from(e - s).is_number() && python_equal(&Value::from(e - s), count)
        }))
    }
}
fn selected(v: &Value, names: &str) -> Result<Value, ContractError> {
    let o = v
        .as_object()
        .ok_or(ContractError("missing_contract_field"))?;
    let mut out = Map::new();
    for n in names.split_whitespace() {
        out.insert(
            n.to_owned(),
            o.get(n)
                .ok_or(ContractError("missing_contract_field"))?
                .clone(),
        );
    }
    Ok(Value::Object(out))
}
// Python tuple(iterable) accepts arrays, strings and mapping keys.
fn iterable(v: &Value) -> Result<Vec<Value>, ContractError> {
    match v {
        Value::Array(a) => Ok(a.clone()),
        Value::Object(o) => Ok(o.keys().map(|k| Value::String(k.clone())).collect()),
        Value::String(s) => Ok(s.chars().map(|c| Value::String(c.to_string())).collect()),
        _ => Err(ContractError("missing_contract_field")),
    }
}
fn hashable(v: &Value) -> Result<(), ContractError> {
    require(!v.is_array() && !v.is_object(), "invalid_graph_field_type")
}
fn unique_insert<'a>(
    seen: &mut Vec<&'a Value>,
    v: &'a Value,
    code: &'static str,
) -> Result<(), ContractError> {
    hashable(v)?;
    require(!seen.iter().any(|x| python_equal(x, v)), code)?;
    seen.push(v);
    Ok(())
}
fn compare(
    a: &Value,
    b: &Value,
    minimum: Ordering,
    code: &'static str,
) -> Result<(), ContractError> {
    let order = numeric_cmp(a, b).ok_or(ContractError("invalid_graph_field_type"))?;
    require(order >= minimum, code)
}
fn edges(v: &Value) -> Result<Value, ContractError> {
    Ok(Value::Array(
        iterable(v)?
            .iter()
            .map(|e| selected(e, "edge_id from_placement_id to_placement_id link_id"))
            .collect::<Result<_, _>>()?,
    ))
}
pub fn parse(v: &Value) -> Result<Graph, ContractError> {
    require(
        v["protocol"] == "mycelium.execution_graph.v1",
        "unsupported_graph_protocol",
    )?;
    // Complete construction precedes all semantic checks, as in the oracle.
    let mut graph = selected(
        v,
        "protocol deployment_id deployment_epoch topology_version model_id resolved_commit manifest_digest entry_stage_id final_stage_id hidden_size activation_bytes token_envelope_bytes stages edges loopback_edges",
    )?;
    let mut stages = Vec::new();
    for s in iterable(&graph["stages"])? {
        let mut stage = selected(&s, "stage_id range component_roles stage_cost placements")?;
        stage["range"] = selected(&s["range"], "start_layer end_layer_exclusive layer_count")?;
        stage["stage_cost"] = selected(
            &s["stage_cost"],
            "prefill_work_units_per_prompt_token decode_work_units_per_token kv_bytes_per_context_token",
        )?;
        stage["component_roles"] = Value::Array(iterable(&s["component_roles"])?);
        let mut placements = Vec::new();
        for p in iterable(&s["placements"])? {
            let mut placement = selected(
                &p,
                "placement_id node_id replica_group_id assignment_id stage_signature load_proof_digest runtime_backend runtime_endpoint",
            )?;
            placement["lifecycle_state"] = p
                .get("lifecycle_state")
                .cloned()
                .unwrap_or(Value::from("ACTIVE"));
            placements.push(placement);
        }
        stage["placements"] = Value::Array(placements);
        stages.push(stage);
    }
    graph["stages"] = Value::Array(stages);
    graph["edges"] = edges(&graph["edges"])?;
    graph["loopback_edges"] = edges(&graph["loopback_edges"])?;
    validate(&graph)?;
    Ok(Graph(graph))
}
fn validate(g: &Value) -> Result<(), ContractError> {
    require(truthy(&g["deployment_id"]), "missing_deployment_id")?;
    for (f, code, min) in [
        (
            "deployment_epoch",
            "invalid_deployment_epoch",
            Ordering::Equal,
        ),
        (
            "topology_version",
            "invalid_topology_version",
            Ordering::Equal,
        ),
    ] {
        compare(&g[f], &Value::from(0), min, code)?;
    }
    require(truthy(&g["manifest_digest"]), "missing_manifest_digest")?;
    require(truthy(&g["resolved_commit"]), "missing_resolved_commit")?;
    for (f, code, min) in [
        ("hidden_size", "invalid_hidden_size", Ordering::Greater),
        (
            "activation_bytes",
            "invalid_activation_bytes",
            Ordering::Greater,
        ),
        (
            "token_envelope_bytes",
            "invalid_token_envelope_bytes",
            Ordering::Equal,
        ),
    ] {
        compare(&g[f], &Value::from(0), min, code)?;
    }
    let stages = g["stages"]
        .as_array()
        .ok_or(ContractError("missing_stages"))?;
    require(!stages.is_empty(), "missing_stages")?;
    let mut ids = Vec::new();
    let mut placement_ids = Vec::new();
    let mut placement_stages = Vec::new();
    for (i, s) in stages.iter().enumerate() {
        unique_insert(&mut ids, &s["stage_id"], "duplicate_stage_id")?;
        let range = &s["range"];
        compare(
            &range["start_layer"],
            &Value::from(0),
            Ordering::Equal,
            "negative_layer_start",
        )?;
        compare(
            &range["end_layer_exclusive"],
            &range["start_layer"],
            Ordering::Greater,
            "empty_or_reversed_layer_range",
        )?;
        require(
            range_count(
                &range["start_layer"],
                &range["end_layer_exclusive"],
                &range["layer_count"],
            )?,
            "range_count_mismatch",
        )?;
        let ps = s["placements"]
            .as_array()
            .ok_or(ContractError("stage_without_placements"))?;
        require(!ps.is_empty(), "stage_without_placements")?;
        for (f, code, min) in [
            (
                "prefill_work_units_per_prompt_token",
                "invalid_prefill_cost",
                Ordering::Greater,
            ),
            (
                "decode_work_units_per_token",
                "invalid_decode_cost",
                Ordering::Greater,
            ),
            (
                "kv_bytes_per_context_token",
                "invalid_kv_cost",
                Ordering::Equal,
            ),
        ] {
            compare(&s["stage_cost"][f], &Value::from(0), min, code)?;
        }
        if i > 0 {
            require(
                python_equal(
                    &range["start_layer"],
                    &stages[i - 1]["range"]["end_layer_exclusive"],
                ),
                "layer_gap_or_overlap",
            )?;
        }
        for p in ps {
            unique_insert(
                &mut placement_ids,
                &p["placement_id"],
                "duplicate_placement_id",
            )?;
            placement_stages.push((&p["placement_id"], i));
            for (f, code) in [
                ("node_id", "missing_node_id"),
                ("assignment_id", "missing_assignment_id"),
                ("stage_signature", "missing_stage_signature"),
                ("load_proof_digest", "missing_load_proof"),
            ] {
                require(truthy(&p[f]), code)?;
            }
            require(
                one_of(&p["lifecycle_state"], &["ACTIVE", "RETIRING", "STANDBY"]),
                "invalid_placement_lifecycle",
            )?;
        }
        require(
            ps.iter().any(|p| p["lifecycle_state"] != "STANDBY"),
            "stage_without_serving_placement",
        )?;
    }
    require(
        python_equal(&g["entry_stage_id"], &stages[0]["stage_id"]),
        "invalid_entry_stage",
    )?;
    require(
        python_equal(&g["final_stage_id"], &stages[stages.len() - 1]["stage_id"]),
        "invalid_final_stage",
    )?;
    let mut edge_ids = Vec::new();
    let mut pairs: Vec<(&Value, &Value)> = Vec::new();
    let es = g["edges"]
        .as_array()
        .ok_or(ContractError("missing_contract_field"))?;
    for e in es {
        unique_insert(&mut edge_ids, &e["edge_id"], "duplicate_edge_id")?;
        hashable(&e["from_placement_id"])?;
        hashable(&e["to_placement_id"])?;
        let from = placement_stages
            .iter()
            .find(|(p, _)| python_equal(p, &e["from_placement_id"]));
        let to = placement_stages
            .iter()
            .find(|(p, _)| python_equal(p, &e["to_placement_id"]));
        let (Some((_, from)), Some((_, to))) = (from, to) else {
            return Err(ContractError("unknown_edge_placement"));
        };
        require(*to == from + 1, "non_adjacent_stage_edge")?;
        require(
            !pairs.iter().any(|(a, b)| {
                python_equal(a, &e["from_placement_id"]) && python_equal(b, &e["to_placement_id"])
            }),
            "duplicate_edge_pair",
        )?;
        pairs.push((&e["from_placement_id"], &e["to_placement_id"]));
    }
    let mut sources = Vec::new();
    for e in g["loopback_edges"]
        .as_array()
        .ok_or(ContractError("missing_contract_field"))?
    {
        unique_insert(&mut edge_ids, &e["edge_id"], "duplicate_edge_id")?;
        hashable(&e["from_placement_id"])?;
        hashable(&e["to_placement_id"])?;
        require(
            placement_stages
                .iter()
                .any(|(p, i)| *i == stages.len() - 1 && python_equal(p, &e["from_placement_id"]))
                && placement_stages
                    .iter()
                    .any(|(p, i)| *i == 0 && python_equal(p, &e["to_placement_id"])),
            "invalid_loopback",
        )?;
        sources.push(&e["from_placement_id"]);
    }
    for (p, i) in placement_stages {
        if i == stages.len() - 1 {
            require(
                sources.iter().any(|s| python_equal(s, p)),
                "missing_loopback",
            )?;
        }
    }
    Ok(())
}
