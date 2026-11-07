// SPDX-License-Identifier: AGPL-3.0-only
//! M16 live placement capacities (CL-L1-07): source parity for the capacity part of
//! `build_live_m16_runtime`, mycelium_m16_runtime.py:1181-1230 @ f4e366e9. Every expected byte
//! count is derived by hand from the cited oracle lines in the comments; none is computed by the
//! factory or read from the budget table. No BEHAVIOURS.tsv row covers this factory yet.
use protocol::execution_graph::{self, Graph};
use runtime::m16_capacity::{
    CapacityError, PlacementCapacity, ProjectedNodes, PyNumber, live_placement_capacities,
};
use serde_json::{Value, json};

/// A valid execution graph document: one stage per `(kv_bytes_per_context_token, placements)`,
/// each placement `(placement_id, node_id)`; every final-stage placement loops back to the first.
fn graph_value(activation_bytes: Value, stages: &[(Value, &[(&str, &str)])]) -> Value {
    let last = stages.len() - 1;
    let first = stages[0].1[0].0;
    let mut stage_values = Vec::new();
    let mut loopbacks = Vec::new();
    for (i, (kv, placements)) in stages.iter().enumerate() {
        let mut placement_values = Vec::new();
        for (placement_id, node_id) in placements.iter() {
            placement_values.push(json!({
                "placement_id": placement_id,
                "node_id": node_id,
                "replica_group_id": format!("group-{i}"),
                "assignment_id": format!("assignment-{placement_id}"),
                "stage_signature": format!("signature-{i}"),
                "load_proof_digest": format!("proof-{placement_id}"),
                "runtime_backend": "numpy",
                "runtime_endpoint": format!("local://{placement_id}"),
            }));
            if i == last {
                loopbacks.push(json!({
                    "edge_id": format!("loop-{placement_id}"),
                    "from_placement_id": placement_id,
                    "to_placement_id": first,
                    "link_id": "link-loop",
                }));
            }
        }
        stage_values.push(json!({
            "stage_id": format!("stage-{i}"),
            "range": {"start_layer": i, "end_layer_exclusive": i + 1, "layer_count": 1},
            "component_roles": ["decoder"],
            "stage_cost": {
                "prefill_work_units_per_prompt_token": 1,
                "decode_work_units_per_token": 1,
                "kv_bytes_per_context_token": kv,
            },
            "placements": placement_values,
        }));
    }
    json!({
        "protocol": "mycelium.execution_graph.v1",
        "deployment_id": "deployment-m16",
        "deployment_epoch": 1,
        "topology_version": 1,
        "model_id": "model-m16",
        "resolved_commit": "commit-m16",
        "manifest_digest": "digest-m16",
        "entry_stage_id": "stage-0",
        "final_stage_id": format!("stage-{last}"),
        "hidden_size": 8,
        "activation_bytes": activation_bytes,
        "token_envelope_bytes": 0,
        "stages": stage_values,
        "edges": [],
        "loopback_edges": loopbacks,
    })
}

fn parse(value: &Value) -> Graph {
    execution_graph::parse(value).expect("valid execution graph")
}

fn graph(activation_bytes: Value, stages: &[(Value, &[(&str, &str)])]) -> Graph {
    parse(&graph_value(activation_bytes, stages))
}

fn projected(entries: &[(&str, Value)]) -> ProjectedNodes {
    entries
        .iter()
        .map(|(node_id, entry)| {
            let entry = entry.as_object().expect("projected node entry").clone();
            ((*node_id).to_owned(), entry)
        })
        .collect()
}

fn allocatable(node_id: &str, bytes: Value) -> ProjectedNodes {
    projected(&[(
        node_id,
        json!({"node_id": node_id, "fast_allocatable_bytes": bytes}),
    )])
}

fn cap(placement_id: &str, memory: u64, kv: u64, workspace: u64) -> PlacementCapacity {
    row(
        json!(placement_id),
        memory.into(),
        kv.into(),
        workspace.into(),
    )
}

/// A capacity record with any dict key and Python-typed values; 64 reservations (:1230).
fn row(
    placement_id: Value,
    memory: PyNumber,
    kv: PyNumber,
    workspace: PyNumber,
) -> PlacementCapacity {
    PlacementCapacity {
        placement_id,
        memory_capacity_bytes: memory,
        kv_capacity_bytes: kv,
        workspace_capacity_bytes: workspace,
        maximum_reservations: 64,
    }
}

fn big(text: &str) -> Value {
    serde_json::from_str(text).expect("JSON integer literal")
}

#[test]
fn m16_capacities_default_allocatable_single_placement() {
    let g = graph(json!(2), &[(json!(8), &[("p0", "node-a")])]);
    // :1192-1193 no projected entry for node-a -> allocatable 536_870_912
    // :1196 per_placement = max(1_048_576, 536_870_912 // 1) = 536_870_912
    // :1220-1221 workspace = max(max(2 * 2, 1), 536_870_912 // 4) = 134_217_728
    // :1222-1225 kv = max(8 * 4_096 = 32_768, 536_870_912 // 2) = 268_435_456
    // :1227 memory = max(536_870_912, 134_217_728 + 268_435_456 = 402_653_184) = 536_870_912
    let want = vec![cap("p0", 536_870_912, 268_435_456, 134_217_728)];
    assert_eq!(
        live_placement_capacities(&g, &ProjectedNodes::new()),
        Ok(want.clone())
    );
    // Another node's projected entry never applies to node-a.
    let other = allocatable("node-b", json!(1_073_741_824));
    assert_eq!(live_placement_capacities(&g, &other), Ok(want));
}

#[test]
fn m16_capacities_two_placements_split() {
    // node-a and node-b each hold one placement in each of two stages.
    let g = graph(
        json!(2),
        &[
            (json!(8), &[("p0", "node-a"), ("p1", "node-b")]),
            (json!(16), &[("p2", "node-a"), ("p3", "node-b")]),
        ],
    );
    let nodes = allocatable("node-b", json!(1_073_741_824));
    // node-a, :1193 default: per_placement = max(1_048_576, 536_870_912 // 2) = 268_435_456
    //   workspace = max(4, 268_435_456 // 4) = 67_108_864
    //   kv = max(8 * 4_096 = 32_768 | 16 * 4_096 = 65_536, 268_435_456 // 2) = 134_217_728
    //   memory = max(268_435_456, 67_108_864 + 134_217_728 = 201_326_592) = 268_435_456
    // node-b, projected: per_placement = max(1_048_576, 1_073_741_824 // 2) = 536_870_912
    //   workspace = 134_217_728; kv = 268_435_456; memory = max(536_870_912, 402_653_184)
    // :1191/:1218 order: node-a's placements (graph order), then node-b's.
    assert_eq!(
        live_placement_capacities(&g, &nodes),
        Ok(vec![
            cap("p0", 268_435_456, 134_217_728, 67_108_864),
            cap("p2", 268_435_456, 134_217_728, 67_108_864),
            cap("p1", 536_870_912, 268_435_456, 134_217_728),
            cap("p3", 536_870_912, 268_435_456, 134_217_728),
        ])
    );
    // Any id the parser admits is a dict key as given; the product path (registry.py:153-167)
    // applies no string-id check (oracle: reviewer probes numeric-node-id, numeric-placement-id).
    // Node 7 has no projected entry, so :1193 defaults it; placement 9 stays the int key 9.
    let mut numeric_node = graph_value(json!(2), &[(json!(8), &[("p0", "node-a")])]);
    numeric_node["stages"][0]["placements"][0]["node_id"] = json!(7);
    assert_eq!(
        live_placement_capacities(&parse(&numeric_node), &ProjectedNodes::new()),
        Ok(vec![cap("p0", 536_870_912, 268_435_456, 134_217_728)])
    );
    let mut numeric_placement = graph_value(json!(2), &[(json!(8), &[("p0", "node-a")])]);
    numeric_placement["stages"][0]["placements"][0]["placement_id"] = json!(9);
    numeric_placement["loopback_edges"][0]["from_placement_id"] = json!(9);
    numeric_placement["loopback_edges"][0]["to_placement_id"] = json!(9);
    assert_eq!(
        live_placement_capacities(&parse(&numeric_placement), &ProjectedNodes::new()),
        Ok(vec![row(
            json!(9),
            n(536_870_912),
            n(268_435_456),
            n(134_217_728)
        )])
    );
}

#[test]
fn m16_capacities_invalid_allocatable_defaults() {
    let g = graph(json!(2), &[(json!(8), &[("p0", "node-a")])]);
    // :1194-1195 anything but a positive Python int (bool excluded) -> 536_870_912; the
    // capacities then equal the default case: workspace 134_217_728, kv 268_435_456,
    // memory 536_870_912.
    let want = Ok(vec![cap("p0", 536_870_912, 268_435_456, 134_217_728)]);
    for raw in [
        json!(true),
        json!(false),
        json!(0),
        json!(-1),
        json!(1.5),
        json!(2.0),
        json!(1_073_741_824.0),
        json!("1073741824"),
        json!(null),
        json!([1_073_741_824]),
    ] {
        assert_eq!(
            live_placement_capacities(&g, &allocatable("node-a", raw.clone())),
            want,
            "{raw}"
        );
    }
    // :1193 the projected entry lacks the key.
    let missing = projected(&[("node-a", json!({"node_id": "node-a"}))]);
    assert_eq!(live_placement_capacities(&g, &missing), want);
    // A positive integer is never defaulted, however large (reviewer probe allocatable-above-u64):
    // 2**64 // 1; workspace 2**62, kv 2**63, memory max(2**64, 2**62 + 2**63) = 2**64.
    let huge = allocatable("node-a", big("18446744073709551616"));
    assert_eq!(
        live_placement_capacities(&g, &huge),
        Ok(vec![row(
            json!("p0"),
            int("18446744073709551616"),
            n(9_223_372_036_854_775_808),
            n(4_611_686_018_427_387_904)
        )])
    );
}

#[test]
fn m16_capacities_per_placement_floor() {
    let four = [
        ("p0", "node-a"),
        ("p1", "node-a"),
        ("p2", "node-a"),
        ("p3", "node-a"),
    ];
    let g = graph(json!(2), &[(json!(8), &four)]);
    let at = |memory, kv, workspace| {
        ["p0", "p1", "p2", "p3"]
            .into_iter()
            .map(|p| cap(p, memory, kv, workspace))
            .collect::<Vec<_>>()
    };
    // 3 MiB: :1196 per_placement = max(1_048_576, 3_145_728 // 4 = 786_432) = 1_048_576
    //   workspace = max(4, 1_048_576 // 4) = 262_144; kv = max(32_768, 1_048_576 // 2) = 524_288
    //   memory = max(1_048_576, 262_144 + 524_288 = 786_432) = 1_048_576
    // 4 MiB - 1: 4_194_303 // 4 = 1_048_575 -> the floor 1_048_576 again
    for bytes in [3_145_728u64, 4_194_303] {
        assert_eq!(
            live_placement_capacities(&g, &allocatable("node-a", json!(bytes))),
            Ok(at(1_048_576, 524_288, 262_144)),
            "{bytes}"
        );
    }
    // Just above the floor, floor division: 4_194_311 // 4 = 1_048_577 (remainder 3)
    //   workspace = 1_048_577 // 4 = 262_144; kv = 1_048_577 // 2 = 524_288
    //   memory = max(1_048_577, 786_432) = 1_048_577
    assert_eq!(
        live_placement_capacities(&g, &allocatable("node-a", json!(4_194_311))),
        Ok(at(1_048_577, 524_288, 262_144))
    );
    // allocatable 1 is a valid int (not defaulted): max(1_048_576, 1 // 1) = 1_048_576
    let single = graph(json!(2), &[(json!(8), &[("p0", "node-a")])]);
    assert_eq!(
        live_placement_capacities(&single, &allocatable("node-a", json!(1))),
        Ok(vec![cap("p0", 1_048_576, 524_288, 262_144)])
    );
}

#[test]
fn m16_capacities_kv_dominated() {
    let nodes = allocatable("node-a", json!(1_073_741_824));
    let g = graph(json!(2), &[(json!(300_000), &[("p0", "node-a")])]);
    // per_placement = 1_073_741_824; workspace = max(4, 268_435_456) = 268_435_456
    // :1223 kv = max(300_000 * 4_096 = 1_228_800_000, 536_870_912) = 1_228_800_000
    // :1227 memory = max(1_073_741_824, 268_435_456 + 1_228_800_000) = 1_497_235_456
    assert_eq!(
        live_placement_capacities(&g, &nodes),
        Ok(vec![cap("p0", 1_497_235_456, 1_228_800_000, 268_435_456)])
    );
    // :1219 each placement takes its own stage's cost (node-a holds one placement per stage):
    // per_placement = 1_073_741_824 // 2 = 536_870_912; workspace = 134_217_728
    //   p0: kv = max(1_228_800_000, 268_435_456); memory = 134_217_728 + 1_228_800_000
    //   p1: kv = max(8 * 4_096, 268_435_456) = 268_435_456; memory = max(536_870_912, 402_653_184)
    let two = graph(
        json!(2),
        &[
            (json!(300_000), &[("p0", "node-a")]),
            (json!(8), &[("p1", "node-a")]),
        ],
    );
    assert_eq!(
        live_placement_capacities(&two, &nodes),
        Ok(vec![
            cap("p0", 1_363_017_728, 1_228_800_000, 134_217_728),
            cap("p1", 536_870_912, 268_435_456, 134_217_728),
        ])
    );
    // A bool or float kv cost is a Python number (oracle old-kv-*@1073741824): True * 4_096 =
    // 4_096, 6_144.0 and 8_192.0 all lose to the int per_placement // 2 = 536_870_912;
    // workspace 268_435_456; memory max(1_073_741_824, 805_306_368).
    for raw in [json!(true), json!(1.5), json!(2.0)] {
        let g = graph(json!(2), &[(raw.clone(), &[("p0", "node-a")])]);
        assert_eq!(
            live_placement_capacities(&g, &nodes),
            Ok(vec![cap("p0", 1_073_741_824, 536_870_912, 268_435_456)]),
            "{raw}"
        );
    }
    // Ints are unbounded (reviewer probes kv-multiply-overflow, kv-sum-overflow): 2**52 * 4_096 =
    // 2**64 and (2**52 - 1) * 4_096 = 18_446_744_073_709_547_520; memory adds workspace 134_217_728.
    for (kv, kv_bytes, memory) in [
        (
            4_503_599_627_370_496u64,
            int("18446744073709551616"),
            int("18446744073843769344"),
        ),
        (
            4_503_599_627_370_495,
            n(18_446_744_073_709_547_520),
            int("18446744073843765248"),
        ),
    ] {
        let g = graph(json!(2), &[(json!(kv), &[("p0", "node-a")])]);
        assert_eq!(
            live_placement_capacities(&g, &ProjectedNodes::new()),
            Ok(vec![row(json!("p0"), memory, kv_bytes, n(134_217_728))]),
            "{kv}"
        );
    }
}

#[test]
fn m16_capacities_activation_dominated() {
    let nodes = allocatable("node-a", json!(2_097_152));
    let g = graph(json!(600_000), &[(json!(8), &[("p0", "node-a")])]);
    // per_placement = 2_097_152
    // :1220-1221 workspace = max(max(600_000 * 2 = 1_200_000, 1), 2_097_152 // 4 = 524_288)
    // kv = max(32_768, 1_048_576) = 1_048_576
    // :1227 memory = max(2_097_152, 1_200_000 + 1_048_576) = 2_248_576
    assert_eq!(
        live_placement_capacities(&g, &nodes),
        Ok(vec![cap("p0", 2_248_576, 1_048_576, 1_200_000)])
    );
    // A bool or float activation size is a Python number (oracle old-activation-*@2097152):
    // True * 2 = 2 and 2.5 * 2 = 5.0 lose to 2_097_152 // 4 = 524_288 (int outputs); 600_000.0 * 2
    // = 1_200_000.0 wins, so workspace and memory (1_200_000.0 + 1_048_576) are floats.
    for (raw, want) in [
        (json!(true), cap("p0", 2_097_152, 1_048_576, 524_288)),
        (json!(2.5), cap("p0", 2_097_152, 1_048_576, 524_288)),
        (
            json!(600_000.0),
            row(
                json!("p0"),
                float(2_248_576.0),
                n(1_048_576),
                float(1_200_000.0),
            ),
        ),
    ] {
        let g = graph(raw.clone(), &[(json!(8), &[("p0", "node-a")])]);
        assert_eq!(
            live_placement_capacities(&g, &nodes),
            Ok(vec![want]),
            "{raw}"
        );
    }
    // Ints are unbounded (oracle old-activation-2p63/2p64@2097152): workspace 2**64 and 2**65;
    // memory adds kv 1_048_576.
    for (raw, workspace, memory) in [
        (
            json!(9_223_372_036_854_775_808u64),
            int("18446744073709551616"),
            int("18446744073710600192"),
        ),
        (
            big("18446744073709551616"),
            int("36893488147419103232"),
            int("36893488147420151808"),
        ),
    ] {
        let g = graph(raw.clone(), &[(json!(8), &[("p0", "node-a")])]);
        assert_eq!(
            live_placement_capacities(&g, &nodes),
            Ok(vec![row(json!("p0"), memory, n(1_048_576), workspace)]),
            "{raw}"
        );
    }
}

#[test]
fn m16_capacities_maximum_reservations_64() {
    let g = graph(
        json!(2),
        &[
            (json!(8), &[("p0", "node-a"), ("p1", "node-b")]),
            (json!(16), &[("p2", "node-c")]),
        ],
    );
    let nodes = projected(&[
        ("node-b", json!({"fast_allocatable_bytes": 3_145_728})),
        ("node-c", json!({"fast_allocatable_bytes": true})),
    ]);
    let got = live_placement_capacities(&g, &nodes).expect("capacities");
    // :1230 every placement admits 64 reservations, whatever its allocatable regime.
    let reservations: Vec<u64> = got.iter().map(|c| c.maximum_reservations).collect();
    assert_eq!(reservations, vec![64, 64, 64]);
    // node-a :1193 default and node-c :1195 default (bool): 536_870_912 per placement;
    // node-b: per_placement = 3_145_728; workspace = 786_432; kv = max(32_768, 1_572_864);
    //   memory = max(3_145_728, 786_432 + 1_572_864 = 2_359_296) = 3_145_728
    assert_eq!(
        got,
        vec![
            cap("p0", 536_870_912, 268_435_456, 134_217_728),
            cap("p1", 3_145_728, 1_572_864, 786_432),
            cap("p2", 536_870_912, 268_435_456, 134_217_728),
        ]
    );
}

// Python value-domain and large-integer parity. Each case's expected outcome was recorded from the
// real oracle at f4e366e9 (worker probe target/rc-logs-a2/m16/oracle_m16_a2.py: the `capacities`
// dict that build_live_m16_runtime hands to its coordinator at :1252, read through a recording
// wrapper around the real class; reviewer case names are from rv-l1b-m16-live-capacities
// probe-comparison.json) and checked by hand against :1181-1230. Kinds matter: `n`/`int` are
// Python ints, `float` Python floats.

/// A graph document like `graph_value`, with any JSON placement and node ids.
fn document(activation: Value, stages: &[(Value, &[(Value, Value)])]) -> Value {
    let mut value = graph_value(activation, &[(json!(8), &[("p0", "node-a")])]);
    let last = stages.len() - 1;
    let first = stages[0].1[0].0.clone();
    let mut stage_values = Vec::new();
    let mut loopbacks = Vec::new();
    for (i, (kv, placements)) in stages.iter().enumerate() {
        let mut stage = value["stages"][0].clone();
        stage["stage_id"] = json!(format!("stage-{i}"));
        stage["range"] = json!({"start_layer": i, "end_layer_exclusive": i + 1, "layer_count": 1});
        stage["stage_cost"]["kv_bytes_per_context_token"] = kv.clone();
        let mut placement_values = Vec::new();
        for (j, (placement_id, node_id)) in placements.iter().enumerate() {
            placement_values.push(json!({
                "placement_id": placement_id,
                "node_id": node_id,
                "replica_group_id": format!("group-{i}"),
                "assignment_id": format!("assignment-{i}-{j}"),
                "stage_signature": format!("signature-{i}"),
                "load_proof_digest": format!("proof-{i}-{j}"),
                "runtime_backend": "numpy",
                "runtime_endpoint": format!("local://{i}-{j}"),
            }));
            if i == last {
                loopbacks.push(json!({
                    "edge_id": format!("loop-{j}"),
                    "from_placement_id": placement_id,
                    "to_placement_id": first,
                    "link_id": "link-loop",
                }));
            }
        }
        stage["placements"] = json!(placement_values);
        stage_values.push(stage);
    }
    value["final_stage_id"] = json!(format!("stage-{last}"));
    value["stages"] = json!(stage_values);
    value["loopback_edges"] = json!(loopbacks);
    value
}

/// One stage holding `p0` on `node-a`.
fn single(activation: Value, kv: Value) -> Value {
    document(activation, &[(kv, &[(json!("p0"), json!("node-a"))])])
}

/// One stage with the given `(placement_id, node_id)` pairs and KV cost 8.
fn ids(pairs: &[(Value, Value)]) -> Value {
    document(json!(2), &[(json!(8), pairs)])
}

/// A JSON number literal kept exactly as written (the parser enables arbitrary precision).
fn literal(text: &str) -> Value {
    serde_json::from_str(text).expect("JSON number literal")
}

fn n(value: u64) -> PyNumber {
    PyNumber::from(value)
}

fn int(digits: &str) -> PyNumber {
    PyNumber::Int(digits.parse().expect("decimal literal"))
}

fn float(value: f64) -> PyNumber {
    PyNumber::Float(value)
}

/// The default-allocatable single-placement record (:1193): 536_870_912 / 268_435_456 / 134_217_728.
fn default_row(placement_id: Value) -> PlacementCapacity {
    row(placement_id, n(536_870_912), n(268_435_456), n(134_217_728))
}

#[derive(Debug, PartialEq)]
enum Outcome {
    /// `execution_graph::parse` refuses the document before the factory runs.
    Parser(&'static str),
    Factory(Result<Vec<PlacementCapacity>, CapacityError>),
}

fn outcome(document: &Value, nodes: &ProjectedNodes) -> Outcome {
    match execution_graph::parse(document) {
        Err(error) => Outcome::Parser(error.0),
        Ok(graph) => Outcome::Factory(live_placement_capacities(&graph, nodes)),
    }
}

fn accepted(rows: Vec<PlacementCapacity>) -> Outcome {
    Outcome::Factory(Ok(rows))
}

fn refused(code: &'static str) -> Outcome {
    Outcome::Factory(Err(CapacityError(code)))
}

#[test]
fn m16_capacities_python_value_domain_parity() {
    let none = ProjectedNodes::new;
    let p0 = || json!("p0");
    let cases: Vec<(&str, Value, ProjectedNodes, Outcome)> = vec![
        // Reviewer `bool-activation`: :1220 True * 2 = 2 (bool is int); 536_870_912 // 4 dominates.
        (
            "bool-activation",
            single(json!(true), json!(8)),
            none(),
            accepted(vec![default_row(p0())]),
        ),
        // Reviewer `fractional-activation`: max(5.0, 1) = 5.0 < 134_217_728, workspace stays int.
        (
            "fractional-activation",
            single(json!(2.5), json!(8)),
            none(),
            accepted(vec![default_row(p0())]),
        ),
        // Reviewer `integral-float-activation`: 1_200_000.0 < 134_217_728.
        (
            "integral-float-activation",
            single(json!(600_000.0), json!(8)),
            none(),
            accepted(vec![default_row(p0())]),
        ),
        // Reviewer `bool-kv`: :1223 True * 4_096 = 4_096 < 268_435_456.
        (
            "bool-kv",
            single(json!(2), json!(true)),
            none(),
            accepted(vec![default_row(p0())]),
        ),
        // Reviewer `false-kv`: False >= 0 passes validation.py:71; False * 4_096 = 0.
        (
            "false-kv",
            single(json!(2), json!(false)),
            none(),
            accepted(vec![default_row(p0())]),
        ),
        // Reviewer `fractional-kv` / `integral-float-kv`: 6_144.0 and 8_192.0 lose to the int half.
        (
            "fractional-kv",
            single(json!(2), json!(1.5)),
            none(),
            accepted(vec![default_row(p0())]),
        ),
        (
            "integral-float-kv",
            single(json!(2), json!(2.0)),
            none(),
            accepted(vec![default_row(p0())]),
        ),
        // Reviewer `numeric-node-id`: node 7 is a dict key like any other; no projection matches it.
        (
            "numeric-node-id",
            ids(&[(json!("p0"), json!(7))]),
            none(),
            accepted(vec![default_row(p0())]),
        ),
        // Reviewer `numeric-placement-id` / `empty-placement-id`: the key is the id as given.
        (
            "numeric-placement-id",
            ids(&[(json!(9), json!("node-a"))]),
            none(),
            accepted(vec![default_row(json!(9))]),
        ),
        (
            "empty-placement-id",
            ids(&[(json!(""), json!("node-a"))]),
            none(),
            accepted(vec![default_row(json!(""))]),
        ),
        // a2-false-activation: False > 0 fails in both parsers (validation.py:48,
        // execution_graph.rs:224-237); never reaches the factory.
        (
            "false-activation",
            single(json!(false), json!(8)),
            none(),
            Outcome::Parser("invalid_activation_bytes"),
        ),
        // a2-float-tie-workspace: :1221 max(134_217_728.0, 536_870_912 // 4 = 134_217_728) keeps
        // the first (float) argument on the tie; memory max(536_870_912, 402_653_184.0) is the int.
        (
            "float-tie-workspace",
            single(json!(67_108_864.0), json!(8)),
            none(),
            accepted(vec![row(
                p0(),
                n(536_870_912),
                n(268_435_456),
                float(134_217_728.0),
            )]),
        ),
        // a2-float-tie-kv: :1222-1225 max(65_536.0 * 4_096 = 268_435_456.0, 268_435_456) -> float.
        (
            "float-tie-kv",
            single(json!(2), json!(65_536.0)),
            none(),
            accepted(vec![row(
                p0(),
                n(536_870_912),
                float(268_435_456.0),
                n(134_217_728),
            )]),
        ),
        // a2-int-valued-float-activation-wins: per_placement 2_097_152; workspace
        // max(1_200_000.0, 524_288) = 1_200_000.0; memory max(2_097_152, 1_200_000.0 + 1_048_576).
        (
            "int-valued-float-activation-wins",
            single(json!(600_000.0), json!(8)),
            allocatable("node-a", json!(2_097_152)),
            accepted(vec![row(
                p0(),
                float(2_248_576.0),
                n(1_048_576),
                float(1_200_000.0),
            )]),
        ),
        // a2-half-activation-inner-tie: :1220 max(1.0, 1) = 1.0 (first on the tie), then
        // 134_217_728 dominates; a2-activation-2.0-vs-2: 4.0 likewise.
        (
            "half-activation-inner-tie",
            single(json!(0.5), json!(8)),
            none(),
            accepted(vec![default_row(p0())]),
        ),
        (
            "activation-2.0",
            single(json!(2.0), json!(8)),
            none(),
            accepted(vec![default_row(p0())]),
        ),
        // a2-kv-half / a2-kv-negative-zero-float: 2_048.0 and -0.0 lose to the int half.
        (
            "kv-half",
            single(json!(2), json!(0.5)),
            none(),
            accepted(vec![default_row(p0())]),
        ),
        (
            "kv-negative-zero-float",
            single(json!(2), json!(-0.0)),
            none(),
            accepted(vec![default_row(p0())]),
        ),
        // a2-kv-float-wins: kv 300_000.5 * 4_096 = 1_228_802_048.0 > 536_870_912; memory
        // 268_435_456 + 1_228_802_048.0 = 1_497_237_504.0.
        (
            "kv-float-wins",
            single(json!(2), json!(300_000.5)),
            allocatable("node-a", json!(1_073_741_824)),
            accepted(vec![row(
                p0(),
                float(1_497_237_504.0),
                float(1_228_802_048.0),
                n(268_435_456),
            )]),
        ),
        // a2-activation-float-inf: 1e308 * 2 overflows to inf (Python float multiply does not raise).
        (
            "activation-float-inf",
            single(json!(1e308), json!(8)),
            none(),
            accepted(vec![row(
                p0(),
                float(f64::INFINITY),
                n(268_435_456),
                float(f64::INFINITY),
            )]),
        ),
        // a2-kv-float-inf: 1e305 * 4_096 = inf; memory 134_217_728 + inf = inf.
        (
            "kv-float-inf",
            single(json!(2), json!(1e305)),
            none(),
            accepted(vec![row(
                p0(),
                float(f64::INFINITY),
                float(f64::INFINITY),
                n(134_217_728),
            )]),
        ),
        // a2-exact-compare-workspace: per_placement 36_028_797_018_963_972, // 4 = 2**53 + 1 >
        // 2**52 * 2.0 = 2**53 exactly (an int -> float cast would tie and keep the float).
        (
            "exact-compare-workspace",
            single(json!(4_503_599_627_370_496.0), json!(8)),
            allocatable("node-a", json!(36_028_797_018_963_972u64)),
            accepted(vec![row(
                p0(),
                n(36_028_797_018_963_972),
                n(18_014_398_509_481_986),
                n(9_007_199_254_740_993),
            )]),
        ),
        // a2-exact-compare-kv: 2**41 * 4_096 = 2**53 (float) < per_placement // 2 = 2**53 + 1.
        (
            "exact-compare-kv",
            single(json!(2), json!(2_199_023_255_552.0)),
            allocatable("node-a", json!(18_014_398_509_481_986u64)),
            accepted(vec![row(
                p0(),
                n(18_014_398_509_481_986),
                n(9_007_199_254_740_993),
                n(4_503_599_627_370_496),
            )]),
        ),
        // a2-int-id-next-to-str-id: int node 7 never matches the string projection key "7"
        // (:1170-1178 keys only str ids); node-b's projection applies (3_145_728 per placement).
        (
            "int-id-next-to-str-id",
            ids(&[(json!("p0"), json!(7)), (json!("p1"), json!("node-b"))]),
            projected(&[
                ("7", json!({"fast_allocatable_bytes": 1_073_741_824})),
                ("node-b", json!({"fast_allocatable_bytes": 3_145_728})),
            ]),
            accepted(vec![
                default_row(p0()),
                row(json!("p1"), n(3_145_728), n(1_572_864), n(786_432)),
            ]),
        ),
        // a2-equal-numeric-node-ids-group: 1 == True == 1.0 is one dict key (three placements,
        // 536_870_912 // 3 = 178_956_970); the string "1" is another.
        (
            "equal-numeric-node-ids-group",
            ids(&[
                (json!("p0"), json!(1)),
                (json!("p1"), json!(true)),
                (json!("p2"), json!(1.0)),
                (json!("p3"), json!("1")),
            ]),
            none(),
            accepted(vec![
                row(p0(), n(178_956_970), n(89_478_485), n(44_739_242)),
                row(json!("p1"), n(178_956_970), n(89_478_485), n(44_739_242)),
                row(json!("p2"), n(178_956_970), n(89_478_485), n(44_739_242)),
                default_row(json!("p3")),
            ]),
        ),
        // a2-odd-placement-ids: True, 2.5 and None are hashable keys kept as given.
        (
            "odd-placement-ids",
            ids(&[
                (json!(true), json!("node-a")),
                (json!(2.5), json!("node-a")),
                (json!(null), json!("node-b")),
            ]),
            none(),
            accepted(vec![
                row(json!(true), n(268_435_456), n(134_217_728), n(67_108_864)),
                row(json!(2.5), n(268_435_456), n(134_217_728), n(67_108_864)),
                default_row(json!(null)),
            ]),
        ),
        // a2-list-node-id / a2-object-node-id: :1184 setdefault raises TypeError (unhashable).
        (
            "list-node-id",
            ids(&[(json!("p0"), json!(["x"]))]),
            none(),
            refused("m16_capacity_unhashable_node_id"),
        ),
        (
            "object-node-id",
            ids(&[(json!("p0"), json!({"a": 1}))]),
            none(),
            refused("m16_capacity_unhashable_node_id"),
        ),
        // V03 (RU-001), pre-existing parser bounds, never written by v0.1.4's json.dumps: a float
        // literal past f64 (Python reads 1e400 as inf; the oracle seam computes inf) has no finite
        // as_f64 (serde_json 1.0.154 number.rs:170), so compare() refuses it
        // (execution_graph.rs:37-41,53-81,149-157,224-238).
        (
            "v03-activation-1e400",
            single(literal("1e400"), json!(8)),
            none(),
            Outcome::Parser("invalid_graph_field_type"),
        ),
        // The integer literal -0 is Python's int 0 (json.loads), so the parser takes it as 0
        // (execution_graph.rs:29-36) and the factory returns the oracle product's default
        // capacities, as for `false-kv` (mycelium_m16_runtime.py:1181-1230; l1b-m16-path-builder-a2
        // evidence live-capacities-negative-zero.json, CPython 3.14.2, one hop admitted).
        (
            "kv-negative-zero-int",
            single(json!(2), literal("-0")),
            none(),
            accepted(vec![default_row(p0())]),
        ),
    ];
    for (name, document, nodes, want) in cases {
        assert_eq!(outcome(&document, &nodes), want, "{name}");
    }
}

#[test]
fn m16_capacities_large_integer_parity() {
    let none = ProjectedNodes::new;
    let p0 = || json!("p0");
    let on_a = |count: usize| -> Vec<(Value, Value)> {
        (0..count)
            .map(|i| (json!(format!("p{i}")), json!("node-a")))
            .collect()
    };
    let rows = |count: usize,
                memory: PyNumber,
                kv: PyNumber,
                workspace: PyNumber|
     -> Vec<PlacementCapacity> {
        (0..count)
            .map(|i| {
                row(
                    json!(format!("p{i}")),
                    memory.clone(),
                    kv.clone(),
                    workspace.clone(),
                )
            })
            .collect()
    };
    let ten_400 = format!("1{}", "0".repeat(400));
    let cases: Vec<(&str, Value, ProjectedNodes, Outcome)> = vec![
        // Reviewer `activation-multiply-overflow`: 2**63 * 2 = 2**64; memory 2**64 + 268_435_456.
        (
            "activation-multiply-overflow",
            single(big("9223372036854775808"), json!(8)),
            none(),
            accepted(vec![row(
                p0(),
                int("18446744073977987072"),
                n(268_435_456),
                int("18446744073709551616"),
            )]),
        ),
        // Reviewer `activation-above-u64`: 2**64 * 2 = 2**65.
        (
            "activation-above-u64",
            single(big("18446744073709551616"), json!(8)),
            none(),
            accepted(vec![row(
                p0(),
                int("36893488147687538688"),
                n(268_435_456),
                int("36893488147419103232"),
            )]),
        ),
        // Reviewer `kv-multiply-overflow`: 2**52 * 4_096 = 2**64; memory + 134_217_728.
        (
            "kv-multiply-overflow",
            single(json!(2), json!(4_503_599_627_370_496u64)),
            none(),
            accepted(vec![row(
                p0(),
                int("18446744073843769344"),
                int("18446744073709551616"),
                n(134_217_728),
            )]),
        ),
        // Reviewer `kv-sum-overflow`: (2**52 - 1) * 4_096 fits u64, the sum does not.
        (
            "kv-sum-overflow",
            single(json!(2), json!(4_503_599_627_370_495u64)),
            none(),
            accepted(vec![row(
                p0(),
                int("18446744073843765248"),
                n(18_446_744_073_709_547_520),
                n(134_217_728),
            )]),
        ),
        // Reviewer `allocatable-above-u64`: per_placement 2**64; workspace 2**62, kv 2**63.
        (
            "allocatable-above-u64",
            single(json!(2), json!(8)),
            allocatable("node-a", big("18446744073709551616")),
            accepted(vec![row(
                p0(),
                int("18446744073709551616"),
                n(9_223_372_036_854_775_808),
                n(4_611_686_018_427_387_904),
            )]),
        ),
        // Reviewer `allocatable-above-u64-split-fits`: 2**64 // 2 = 2**63 per placement.
        (
            "allocatable-above-u64-split-fits",
            ids(&on_a(2)),
            allocatable("node-a", big("18446744073709551616")),
            accepted(rows(
                2,
                n(9_223_372_036_854_775_808),
                n(4_611_686_018_427_387_904),
                n(2_305_843_009_213_693_952),
            )),
        ),
        // a2-allocatable-u64max-one: u64::MAX // 1; // 4 and // 2 floor.
        (
            "allocatable-u64max-one",
            single(json!(2), json!(8)),
            allocatable("node-a", json!(u64::MAX)),
            accepted(vec![row(
                p0(),
                n(u64::MAX),
                n(9_223_372_036_854_775_807),
                n(4_611_686_018_427_387_903),
            )]),
        ),
        // a2-allocatable-u64max-three / a2-allocatable-2p64-three: (2**64 - 1) // 3 and 2**64 // 3
        // are both 6_148_914_691_236_517_205.
        (
            "allocatable-u64max-three",
            ids(&on_a(3)),
            allocatable("node-a", json!(u64::MAX)),
            accepted(rows(
                3,
                n(6_148_914_691_236_517_205),
                n(3_074_457_345_618_258_602),
                n(1_537_228_672_809_129_301),
            )),
        ),
        (
            "allocatable-2p64-three",
            ids(&on_a(3)),
            allocatable("node-a", big("18446744073709551616")),
            accepted(rows(
                3,
                n(6_148_914_691_236_517_205),
                n(3_074_457_345_618_258_602),
                n(1_537_228_672_809_129_301),
            )),
        ),
        // a2-activation-u64max: (2**64 - 1) * 2 = 36_893_488_147_419_103_230.
        (
            "activation-u64max",
            single(json!(u64::MAX), json!(8)),
            none(),
            accepted(vec![row(
                p0(),
                int("36893488147687538686"),
                n(268_435_456),
                int("36893488147419103230"),
            )]),
        ),
        // a2-kv-u64max: (2**64 - 1) * 4_096 = 75_557_863_725_914_323_415_040.
        (
            "kv-u64max",
            single(json!(2), json!(u64::MAX)),
            none(),
            accepted(vec![row(
                p0(),
                int("75557863725914457632768"),
                int("75557863725914323415040"),
                n(134_217_728),
            )]),
        ),
        // a2-activation-10p400: workspace 2 * 10**400; memory 2 * 10**400 + 268_435_456.
        (
            "activation-10p400",
            single(literal(&ten_400), json!(8)),
            none(),
            accepted(vec![row(
                p0(),
                int(&format!("2{}268435456", "0".repeat(391))),
                n(268_435_456),
                int(&format!("2{}", "0".repeat(400))),
            )]),
        ),
        // a2-allocatable-10p40-split: one node, a placement in each of two stages:
        // (10**40 + 7) // 2 = 5 * 10**39 + 3; // 2 and // 4 floor again.
        (
            "allocatable-10p40-split",
            document(
                json!(3),
                &[
                    (json!(8), &[(json!("p0"), json!("node-a"))]),
                    (json!(9), &[(json!("p1"), json!("node-a"))]),
                ],
            ),
            allocatable("node-a", big("10000000000000000000000000000000000000007")),
            accepted(rows(
                2,
                int("5000000000000000000000000000000000000003"),
                int("2500000000000000000000000000000000000001"),
                int("1250000000000000000000000000000000000000"),
            )),
        ),
        // a2-int-plus-float-rounding: :1227 int + float converts the int (2**54 + 2 rounds half to
        // even, to 2**54) before adding kv 1e15 * 4_096 = 4.096e18.
        (
            "int-plus-float-rounding",
            single(big("9007199254740993"), json!(1e15)),
            none(),
            accepted(vec![row(
                p0(),
                float(4.114_014_398_509_482e18),
                float(4.096e18),
                n(18_014_398_509_481_986),
            )]),
        ),
        // a2-float-boundary-converts: workspace 2**1024 - 2**970 - 2 converts to f64::MAX at :1227
        // (kv 1e7 * 4_096 = 40_960_000_000.0 vanishes in the sum).
        (
            "float-boundary-converts",
            single(
                big(
                    "89884656743115790396864485702651707539967066355018913468086889490222484146382375473324508988793603548165143208346443955473277773925970201315328744335752910340954451000354191838136927422908855765882237865135034927785683479811421457409930417468237646359537084222182755352171355779849754046521440088952087248895",
                ),
                json!(1e7),
            ),
            none(),
            accepted(vec![row(
                p0(),
                float(f64::MAX),
                float(40_960_000_000.0),
                int(
                    "179769313486231580793728971405303415079934132710037826936173778980444968292764750946649017977587207096330286416692887910946555547851940402630657488671505820681908902000708383676273854845817711531764475730270069855571366959622842914819860834936475292719074168444365510704342711559699508093042880177904174497790",
                ),
            )]),
        ),
        // a2-float-boundary-overflows: 2**1024 - 2**970 rounds to 2**1024; :1227 raises
        // OverflowError (int too large to convert to float).
        (
            "float-boundary-overflows",
            single(
                big(
                    "89884656743115790396864485702651707539967066355018913468086889490222484146382375473324508988793603548165143208346443955473277773925970201315328744335752910340954451000354191838136927422908855765882237865135034927785683479811421457409930417468237646359537084222182755352171355779849754046521440088952087248896",
                ),
                json!(1e7),
            ),
            none(),
            refused("m16_capacity_int_too_large_for_float"),
        ),
        // a2-int-overflow-to-float: workspace max(1e308 * 2 = inf, 10**400 // 4) = inf, kv
        // 10**400 // 2 (int); :1227 inf + int raises OverflowError.
        (
            "int-overflow-to-float",
            single(json!(1e308), json!(8)),
            allocatable("node-a", literal(&ten_400)),
            refused("m16_capacity_int_too_large_for_float"),
        ),
    ];
    for (name, document, nodes, want) in cases {
        assert_eq!(outcome(&document, &nodes), want, "{name}");
    }
}
