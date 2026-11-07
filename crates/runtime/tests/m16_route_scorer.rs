// SPDX-License-Identifier: AGPL-3.0-only
//! M16 route scorer (CL-L1-07): source parity for `RouteScorer` with no rtt table
//! (mycelium_router/scoring.py:15-275 @ f4e366e9) and the `RouterConfig`, `DeviceState`,
//! `RequestContext` and `ScoreBreakdown` contracts it reads (mycelium_router/contracts.py:108-183,
//! 212-220). The first seven tests' expected values are derived by hand from the oracle lines
//! cited beside them, as f64 expressions written in the oracle's operation order; none is
//! computed by the scorer. The last test replays paired probes recorded from the real oracle
//! (fixtures/m16-route-scorer-oracle.json).
use protocol::execution_graph::{self, Graph};
use runtime::m16_capacity::PyInt;
use runtime::m16_route_scorer::{RouteScorer, ScorerError, TransferProvenance};
use runtime::m16_router_contracts::{
    DeviceState, IdMap, PyReal, RequestContext, RouterConfig, ScoreBreakdown,
};
use serde_json::{Value, json};
// Bit-identical to the oracle's `math.log(2.0)` (0x1.62e42fefa39efp-1).
use std::f64::consts::LN_2;

const INF: f64 = f64::INFINITY;
const NAN: f64 = f64::NAN;

fn f(x: f64) -> PyReal {
    PyReal::Float(x)
}

fn i(n: i64) -> PyReal {
    PyReal::int(n)
}

/// A non-negative Python int from its decimal digits.
fn digits(text: &str) -> PyReal {
    PyReal::signed(false, text.parse::<PyInt>().expect("decimal digits"))
}

/// `10**exponent` as a Python int.
fn big(exponent: usize) -> PyReal {
    digits(&format!("1{}", "0".repeat(exponent)))
}

/// A JSON number literal kept verbatim (an int of any size, or `1e400`).
fn number(text: &str) -> Value {
    serde_json::from_str(text).expect("a JSON number")
}

/// A test-owned spelling of a Python number: its type and exact value (float bits).
fn tag(v: &PyReal) -> String {
    match v {
        PyReal::Bool(b) => format!("bool:{b}"),
        PyReal::Int {
            negative,
            magnitude,
        } => {
            let text = magnitude.to_string();
            let sign = if *negative && text != "0" { "-" } else { "" };
            format!("int:{sign}{text}")
        }
        PyReal::Float(x) => format!("float:{:016x}", x.to_bits()),
    }
}

/// The breakdown's six floats as bits, then its fallback nodes; or the refusal code.
type View = Result<([u64; 6], Vec<Value>), &'static str>;

fn view(got: &Result<ScoreBreakdown, ScorerError>) -> View {
    got.as_ref()
        .map(|b| {
            (
                [
                    b.ttft_ms,
                    b.tpot_ms,
                    b.prefill_transfer_ms,
                    b.decode_transfer_ms,
                    b.total_score,
                    b.confidence,
                ]
                .map(f64::to_bits),
                b.fallback_nodes.clone(),
            )
        })
        .map_err(|error| error.0)
}

fn want(
    ttft: f64,
    tpot: f64,
    prefill_transfer: f64,
    decode_transfer: f64,
    total: f64,
    confidence: f64,
    fallback: Vec<Value>,
) -> View {
    Ok((
        [
            ttft,
            tpot,
            prefill_transfer,
            decode_transfer,
            total,
            confidence,
        ]
        .map(f64::to_bits),
        fallback,
    ))
}

/// scoring.py:265-275: every cost inf, confidence 0.0, no fallback nodes.
fn infeasible() -> View {
    want(INF, INF, INF, INF, INF, 0.0, vec![])
}

fn refused(code: &'static str) -> View {
    Err(code)
}

/// scoring.py:170-172 `math.exp(-math.log(2.0) * age / half_life)`.
fn decay(age: f64, half_life: f64) -> f64 {
    (-LN_2 * age / half_life).exp()
}

/// scoring.py:157-159 with the interactive alpha 0.35 and targets 1000.0 / 100.0.
fn interactive_total(ttft: f64, tpot: f64) -> f64 {
    0.35 * (ttft / 1000.0) + (1.0 - 0.35) * (tpot / 100.0)
}

fn a() -> Value {
    json!("node-a")
}

fn b() -> Value {
    json!("node-b")
}

fn c() -> Value {
    json!("node-c")
}

/// One stage: `(prefill cost, decode cost, [(placement id, node id)])`.
type Stage = (Value, Value, Vec<(Value, Value)>);

fn stage(prefill: Value, decode: Value, placements: &[(&str, Value)]) -> Stage {
    (
        prefill,
        decode,
        placements
            .iter()
            .map(|(placement, node)| (json!(placement), node.clone()))
            .collect(),
    )
}

/// An execution graph document: every placement of a stage feeds every placement of the next
/// and every final placement loops back to every first-stage placement, unless given.
struct Doc {
    stages: Vec<Stage>,
    sizes: (Value, Value, Value),
    edges: Option<Vec<(&'static str, &'static str)>>,
    loopbacks: Option<Vec<(&'static str, &'static str)>>,
}

fn doc(stages: Vec<Stage>) -> Doc {
    Doc {
        stages,
        sizes: (json!(8), json!(2), json!(16)),
        edges: None,
        loopbacks: None,
    }
}

impl Doc {
    /// hidden_size, activation_bytes, token_envelope_bytes.
    fn sizes(mut self, hidden: Value, activation: Value, envelope: Value) -> Self {
        self.sizes = (hidden, activation, envelope);
        self
    }

    fn edges(mut self, edges: Vec<(&'static str, &'static str)>) -> Self {
        self.edges = Some(edges);
        self
    }

    fn loopbacks(mut self, loopbacks: Vec<(&'static str, &'static str)>) -> Self {
        self.loopbacks = Some(loopbacks);
        self
    }

    fn graph(&self) -> Graph {
        let ids = |k: usize| -> Vec<Value> {
            self.stages[k]
                .2
                .iter()
                .map(|(placement, _)| placement.clone())
                .collect()
        };
        let pairs = |list: &Vec<(&str, &str)>| -> Vec<(Value, Value)> {
            list.iter()
                .map(|(from, to)| (json!(from), json!(to)))
                .collect()
        };
        let edges = self.edges.as_ref().map_or_else(
            || {
                (1..self.stages.len())
                    .flat_map(|k| {
                        let next = ids(k);
                        ids(k - 1).into_iter().flat_map(move |from| {
                            next.clone().into_iter().map(move |to| (from.clone(), to))
                        })
                    })
                    .collect()
            },
            pairs,
        );
        let last = self.stages.len() - 1;
        let loopbacks = self.loopbacks.as_ref().map_or_else(
            || {
                ids(last)
                    .into_iter()
                    .flat_map(|from| ids(0).into_iter().map(move |to| (from.clone(), to)))
                    .collect()
            },
            pairs,
        );
        let edge_docs = |prefix: &str, list: Vec<(Value, Value)>| -> Vec<Value> {
            list.into_iter()
                .enumerate()
                .map(|(k, (from, to))| {
                    json!({"edge_id": format!("{prefix}-{k}"), "from_placement_id": from,
                           "to_placement_id": to, "link_id": format!("link-{prefix}-{k}")})
                })
                .collect()
        };
        let stages: Vec<Value> = self
            .stages
            .iter()
            .enumerate()
            .map(|(k, (prefill, decode, placements))| {
                json!({
                    "stage_id": format!("stage-{k}"),
                    "range": {"start_layer": k, "end_layer_exclusive": k + 1, "layer_count": 1},
                    "component_roles": ["decoder"],
                    "stage_cost": {"prefill_work_units_per_prompt_token": prefill,
                                   "decode_work_units_per_token": decode,
                                   "kv_bytes_per_context_token": 0},
                    "placements": placements.iter().enumerate().map(|(j, (placement, node))| json!({
                        "placement_id": placement, "node_id": node,
                        "replica_group_id": format!("group-{k}"),
                        "assignment_id": format!("assignment-{k}-{j}"),
                        "stage_signature": format!("signature-{k}"),
                        "load_proof_digest": format!("proof-{k}-{j}"),
                        "runtime_backend": "numpy",
                        "runtime_endpoint": format!("local://{k}-{j}"),
                    })).collect::<Vec<_>>(),
                })
            })
            .collect();
        let document = json!({
            "protocol": "mycelium.execution_graph.v1",
            "deployment_id": "deployment-m16", "deployment_epoch": 1, "topology_version": 1,
            "model_id": "model-m16", "resolved_commit": "commit-m16", "manifest_digest": "digest-m16",
            "entry_stage_id": "stage-0", "final_stage_id": format!("stage-{last}"),
            "hidden_size": self.sizes.0, "activation_bytes": self.sizes.1,
            "token_envelope_bytes": self.sizes.2,
            "stages": stages, "edges": edge_docs("edge", edges),
            "loopback_edges": edge_docs("loop", loopbacks),
        });
        execution_graph::parse(&document).expect("a valid execution graph")
    }
}

/// Stage 0 (prefill 2, decode 1) on node-a, stage 1 (prefill 3, decode 1) on node-b.
fn two_stage() -> Doc {
    doc(vec![
        stage(json!(2), json!(1), &[("p0", a())]),
        stage(json!(3), json!(1), &[("p1", b())]),
    ])
}

/// One stage (prefill 2, decode 1) on node-a.
fn one_stage() -> Doc {
    doc(vec![stage(json!(2), json!(1), &[("p0", a())])])
}

/// Updated at 100.0, ALIVE, 1024.0 units/s, half free (rate 512.0), no queue, no neighbours.
fn state(node: Value) -> DeviceState {
    DeviceState {
        node_id: node,
        state_seq: i(1),
        last_updated: f(100.0),
        availability: "ALIVE".to_owned(),
        compute_units_per_second: f(1024.0),
        free_compute_fraction: f(0.5),
        available_kv_bytes: i(0),
        pending_hop_queue_depth: i(0),
        neighbor_rtt_ms: IdMap::new(),
        neighbor_bandwidth_bytes_per_second: IdMap::new(),
    }
}

/// Updated at 40.0: 60 s old at now 100.0, so decay(60.0) = 2**-6 < 0.25 falls back.
fn stale(node: Value) -> DeviceState {
    DeviceState {
        last_updated: f(40.0),
        ..state(node)
    }
}

/// A stated rtt (and bandwidth, if given) toward `to`.
fn link(
    mut s: DeviceState,
    to: Value,
    rtt: Option<PyReal>,
    bandwidth: Option<PyReal>,
) -> DeviceState {
    if let Some(rtt) = rtt {
        s.neighbor_rtt_ms.insert(to.clone(), rtt).expect("hashable");
    }
    if let Some(bandwidth) = bandwidth {
        s.neighbor_bandwidth_bytes_per_second
            .insert(to, bandwidth)
            .expect("hashable");
    }
    s
}

fn states(list: Vec<DeviceState>) -> IdMap<DeviceState> {
    let mut map = IdMap::new();
    for s in list {
        map.insert(s.node_id.clone(), s).expect("hashable node id");
    }
    map
}

/// node-a -> node-b: rtt 2.0, 64.0 B/s; node-b -> node-a: rtt 4.0, 8.0 B/s.
fn linked() -> IdMap<DeviceState> {
    states(vec![
        link(state(a()), b(), Some(f(2.0)), Some(f(64.0))),
        link(state(b()), a(), Some(f(4.0)), Some(f(8.0))),
    ])
}

fn request(prompt_len: usize, qos_class: &str, ttft: PyReal, tpot: PyReal) -> RequestContext {
    RequestContext {
        request_id: "request-1".to_owned(),
        prompt_token_ids: (0..prompt_len).map(|t| i(t as i64)).collect(),
        max_new_tokens: i(8),
        expected_new_tokens: i(8),
        qos_class: qos_class.to_owned(),
        admitted_at: f(0.0),
        target_ttft_ms: ttft,
        target_tpot_ms: tpot,
        target_tokens_per_second: f(10.0),
        sampling_seed: i(0),
        generation_config_digest: "sha256:test".to_owned(),
    }
}

/// The gateway's request (mycelium_request_gateway/backend.py:227-239): 4 prompt tokens.
fn interactive() -> RequestContext {
    request(4, "interactive", f(1000.0), f(100.0))
}

fn defaults() -> RouterConfig {
    RouterConfig::oracle_default().expect("oracle defaults")
}

fn ids(list: &[&str]) -> Vec<Value> {
    list.iter().map(|id| json!(id)).collect()
}

fn score(
    config: RouterConfig,
    request: &RequestContext,
    graph: &Graph,
    route: &[Value],
    states: &IdMap<DeviceState>,
    now: PyReal,
) -> View {
    view(&RouteScorer::new(config).score_route(request, graph, route, states, &now))
}

/// [`two_stage`] over [`linked`] at now == last_updated, 4 tokens:
/// stage 0 rate 1024.0 * 0.5 = 512.0, prefill 2*4/512*1000 = 15.625, decode 1/512*1000 =
/// 1.953125; stage 1 (age 0.0 + 15.625/1000 = 0.015625 s) prefill 3*4/512*1000 = 23.4375,
/// decode 1.953125. Payloads 4*8*2 = 64 (prefill), 8*2 = 16 (decode), 16 (token). Links
/// (:263): a->b prefill 2.0/2 + 64/64*1000 = 1001.0, decode 1.0 + 16/64*1000 = 251.0; loopback
/// b->a 4.0/2 + 16/8*1000 = 2002.0. TTFT 39.0625 + 1001.0, TPOT 3.90625 + 2253.0.
fn standard(total: f64) -> View {
    want(
        1040.0625,
        2256.90625,
        1001.0,
        2253.0,
        total,
        decay(0.015625, 10.0),
        vec![],
    )
}

#[test]
fn m16_route_scorer_router_config_defaults() {
    let config = defaults();
    // contracts.py:141-179, each field's oracle default with its Python type.
    let fields: [(&str, &PyReal, PyReal); 34] = [
        ("interactive_alpha", &config.interactive_alpha, f(0.35)),
        ("batch_alpha", &config.batch_alpha, f(0.10)),
        ("stale_after_seconds", &config.stale_after_seconds, f(10.0)),
        (
            "confidence_half_life_seconds",
            &config.confidence_half_life_seconds,
            f(10.0),
        ),
        ("minimum_confidence", &config.minimum_confidence, f(0.25)),
        (
            "conservative_compute_fraction",
            &config.conservative_compute_fraction,
            f(0.10),
        ),
        (
            "conservative_queue_depth",
            &config.conservative_queue_depth,
            i(20),
        ),
        (
            "default_bandwidth_bytes_per_second",
            &config.default_bandwidth_bytes_per_second,
            f(1_000_000.0),
        ),
        ("default_rtt_ms", &config.default_rtt_ms, f(100.0)),
        (
            "interactive_base_priority",
            &config.interactive_base_priority,
            f(100.0),
        ),
        ("batch_base_priority", &config.batch_base_priority, f(10.0)),
        (
            "aging_priority_per_second",
            &config.aging_priority_per_second,
            f(1.0),
        ),
        (
            "batch_aging_multiplier",
            &config.batch_aging_multiplier,
            f(4.0),
        ),
        (
            "maximum_deficit_boost",
            &config.maximum_deficit_boost,
            f(50.0),
        ),
        (
            "maximum_recovery_attempts",
            &config.maximum_recovery_attempts,
            i(3),
        ),
        // :156; the live factory's 3_600.0 is its own override (mycelium_m16_runtime.py:1260).
        (
            "reservation_lease_seconds",
            &config.reservation_lease_seconds,
            f(30.0),
        ),
        (
            "idempotency_retention_seconds",
            &config.idempotency_retention_seconds,
            f(300.0),
        ),
        (
            "maximum_idempotency_entries",
            &config.maximum_idempotency_entries,
            i(10_000),
        ),
        (
            "maximum_pending_hops",
            &config.maximum_pending_hops,
            i(1_024),
        ),
        (
            "maximum_pending_bytes",
            &config.maximum_pending_bytes,
            i(268_435_456),
        ),
        (
            "backpressure_retry_after_seconds",
            &config.backpressure_retry_after_seconds,
            f(0.01),
        ),
        (
            "prefill_chunk_size_tokens",
            &config.prefill_chunk_size_tokens,
            i(0),
        ),
        (
            "maximum_runtime_batch_size",
            &config.maximum_runtime_batch_size,
            i(20),
        ),
        (
            "decode_runtime_batch_size",
            &config.decode_runtime_batch_size,
            i(8),
        ),
        (
            "prefill_runtime_batch_size",
            &config.prefill_runtime_batch_size,
            i(20),
        ),
        (
            "maximum_runtime_batch_bytes",
            &config.maximum_runtime_batch_bytes,
            i(2_400_000),
        ),
        (
            "prefill_collection_window_seconds",
            &config.prefill_collection_window_seconds,
            f(0.002),
        ),
        (
            "live_hop_collection_window_seconds",
            &config.live_hop_collection_window_seconds,
            f(0.002),
        ),
        (
            "live_hop_join_horizon_seconds",
            &config.live_hop_join_horizon_seconds,
            f(5.0),
        ),
        (
            "batch_deadline_guard_seconds",
            &config.batch_deadline_guard_seconds,
            f(0.001),
        ),
        ("batch_bdp_multiplier", &config.batch_bdp_multiplier, f(2.0)),
        (
            "batch_stats_stale_seconds",
            &config.batch_stats_stale_seconds,
            f(10.0),
        ),
        (
            "batch_observation_ewma_alpha",
            &config.batch_observation_ewma_alpha,
            f(0.25),
        ),
        (
            "maximum_batch_decision_history",
            &config.maximum_batch_decision_history,
            i(1_024),
        ),
    ];
    for (name, got, expected) in &fields {
        assert_eq!(tag(got), tag(expected), "RouterConfig.{name}");
    }
    // :181-182 alpha_for: exactly "batch" picks batch_alpha; anything else interactive_alpha.
    for (qos, expected) in [
        ("batch", f(0.10)),
        ("interactive", f(0.35)),
        ("BATCH", f(0.35)),
        ("batch ", f(0.35)),
        ("", f(0.35)),
    ] {
        assert_eq!(
            tag(config.alpha_for(qos)),
            tag(&expected),
            "alpha_for({qos:?})"
        );
    }
    // A config value keeps its Python type; alpha_for hands back the field as set.
    let custom = RouterConfig {
        batch_alpha: i(7),
        interactive_alpha: PyReal::Bool(true),
        ..defaults()
    };
    assert_eq!(tag(custom.alpha_for("batch")), "int:7");
    assert_eq!(tag(custom.alpha_for("interactive")), "bool:true");
    assert_eq!(tag(&RouteScorer::new(custom).config().batch_alpha), "int:7");
    // :120-121 with_availability replaces the availability and nothing else.
    let alive = link(state(a()), b(), Some(f(2.0)), Some(f(64.0)));
    let dead = alive.with_availability("DEAD");
    assert_eq!(dead.availability, "DEAD");
    assert_eq!(alive.availability, "ALIVE");
    assert_eq!(
        DeviceState {
            availability: "ALIVE".to_owned(),
            ..dead.clone()
        },
        alive
    );
    assert_eq!(tag(&dead.compute_units_per_second), tag(&f(1024.0)));
    assert_eq!(
        dead.neighbor_rtt_ms.get(&b()).expect("hashable").map(tag),
        Some(tag(&f(2.0)))
    );
}

#[test]
fn m16_route_scorer_infeasible_routes() {
    let graph = two_stage().graph();
    let req = interactive();
    let run = |route: &[Value], states: &IdMap<DeviceState>| {
        score(defaults(), &req, &graph, route, states, f(100.0))
    };
    // :54-55 placement count != stage count.
    for route in [vec![], ids(&["p0"]), ids(&["p0", "p1", "p1"])] {
        assert_eq!(run(&route, &linked()), infeasible(), "{route:?}");
    }
    // :56-59 an unknown placement id; the first failing lookup in route order decides, and the
    // count check comes first.
    assert_eq!(run(&ids(&["p0", "p9"]), &linked()), infeasible());
    assert_eq!(run(&[json!("p9"), json!(["p1"])], &linked()), infeasible());
    assert_eq!(run(&[json!(["p1"])], &linked()), infeasible());
    assert_eq!(
        run(&[json!("p0"), json!(["p1"])], &linked()),
        refused("m16_route_scorer_unhashable_placement_id")
    );
    // :65-67 a consecutive pair that is not a graph edge.
    assert_eq!(run(&ids(&["p1", "p0"]), &linked()), infeasible());
    let sparse = doc(vec![
        stage(json!(2), json!(1), &[("p0", a())]),
        stage(json!(3), json!(1), &[("p1a", b()), ("p1b", b())]),
    ])
    .edges(vec![("p0", "p1a")])
    .graph();
    assert_eq!(
        score(
            defaults(),
            &req,
            &sparse,
            &ids(&["p0", "p1b"]),
            &linked(),
            f(100.0)
        ),
        infeasible()
    );
    // :68-78 no loopback edge from the last placement to the first; the looped twin scores.
    let one_loop = doc(vec![
        stage(json!(2), json!(1), &[("p0a", a()), ("p0b", a())]),
        stage(json!(3), json!(1), &[("p1", b())]),
    ])
    .loopbacks(vec![("p1", "p0a")])
    .graph();
    assert_eq!(
        score(
            defaults(),
            &req,
            &one_loop,
            &ids(&["p0b", "p1"]),
            &linked(),
            f(100.0)
        ),
        infeasible()
    );
    assert_eq!(
        score(
            defaults(),
            &req,
            &one_loop,
            &ids(&["p0a", "p1"]),
            &linked(),
            f(100.0)
        ),
        standard(interactive_total(1040.0625, 2256.90625))
    );
    let route = ids(&["p0", "p1"]);
    // :88-90 a missing state, or one that is not exactly "ALIVE", at a later stage too.
    let only_a = states(vec![link(state(a()), b(), Some(f(2.0)), Some(f(64.0)))]);
    assert_eq!(run(&route, &only_a), infeasible());
    for availability in ["DEAD", "alive", ""] {
        let s = states(vec![
            link(state(a()), b(), Some(f(2.0)), Some(f(64.0))),
            state(b()).with_availability(availability),
        ]);
        assert_eq!(run(&route, &s), infeasible(), "{availability:?}");
    }
    // :107-109 compute_rate <= 0 (any zero, negative or False compute units).
    for units in [f(0.0), f(-0.0), f(-1.0), i(0), PyReal::Bool(false)] {
        let s = states(vec![
            link(state(a()), b(), Some(f(2.0)), Some(f(64.0))),
            DeviceState {
                compute_units_per_second: units.clone(),
                ..state(b())
            },
        ]);
        assert_eq!(run(&route, &s), infeasible(), "{units:?}");
    }
    // A fallback rate with a conservative fraction of 0 (int) is 0 too.
    let zero_fraction = RouterConfig {
        conservative_compute_fraction: i(0),
        ..defaults()
    };
    let s = states(vec![stale(a()), state(b())]);
    assert_eq!(
        score(zero_fraction, &req, &graph, &route, &s, f(100.0)),
        infeasible()
    );
    // Near miss: a NaN rate is not <= 0, so the route stays feasible and NaN flows on; stage 1's
    // NaN confidence (age NaN) is not below 1.0, so min keeps 1.0 and nothing falls back.
    let s = states(vec![
        DeviceState {
            compute_units_per_second: f(NAN),
            ..link(state(a()), b(), Some(f(2.0)), Some(f(64.0)))
        },
        link(state(b()), a(), Some(f(4.0)), Some(f(8.0))),
    ]);
    assert_eq!(
        run(&route, &s),
        want(NAN, NAN, 1001.0, 2253.0, NAN, 1.0, vec![])
    );
    // An unhashable node id raises at the state lookup (TypeError, :88).
    let listed = doc(vec![
        stage(json!(2), json!(1), &[("p0", json!(["node-a"]))]),
        stage(json!(3), json!(1), &[("p1", b())]),
    ])
    .graph();
    assert_eq!(
        score(defaults(), &req, &listed, &route, &linked(), f(100.0)),
        refused("m16_route_scorer_unhashable_node_id")
    );
}

#[test]
fn m16_route_scorer_cost_model() {
    // :97-122 per stage: rate = units * max(0.001, min(1.0, free)); prefill = work * tokens /
    // rate * 1000.0; decode = work / rate * 1000.0; the queue term max(0, depth) * decode goes
    // into both TTFT and TPOT.
    let queued = states(vec![
        DeviceState {
            pending_hop_queue_depth: i(3),
            ..link(state(a()), b(), Some(f(2.0)), Some(f(64.0)))
        },
        DeviceState {
            compute_units_per_second: f(2048.0),
            free_compute_fraction: f(0.25),
            pending_hop_queue_depth: i(1),
            ..link(state(b()), a(), Some(f(4.0)), Some(f(8.0)))
        },
    ]);
    // Stage 0: rate 512.0, prefill 15.625, decode 1.953125, queue 3 * 1.953125 = 5.859375:
    // TTFT 21.484375, TPOT 7.8125. Stage 1 (age 21.484375 / 1000 s): rate 2048 * 0.25 = 512.0,
    // prefill 23.4375, queue 1: TTFT +25.390625 = 46.875, TPOT +3.90625 = 11.71875. Links as in
    // `standard`: 1001.0 and 2253.0.
    let (ttft, tpot) = (46.875 + 1001.0, 11.71875 + 2253.0);
    assert_eq!(
        score(
            defaults(),
            &interactive(),
            &two_stage().graph(),
            &ids(&["p0", "p1"]),
            &queued,
            f(100.0)
        ),
        want(
            ttft,
            tpot,
            1001.0,
            2253.0,
            interactive_total(ttft, tpot),
            decay(0.0 + 21.484375 / 1000.0, 10.0),
            vec![]
        )
    );
    // The int product comes before the true division (:111-115): (2**53 + 1) * 3 tokens is
    // 27021597764222979, which float() rounds half-even to 27021597764222980.0 (a float product
    // would give 3 * 2**53 = 27021597764222976.0). One node, so the only link (the loopback)
    // is 0.0; queue 0.
    let exact = doc(vec![stage(
        number("9007199254740993"),
        json!(1),
        &[("p0", a())],
    )])
    .graph();
    let unit = states(vec![DeviceState {
        compute_units_per_second: f(1.0),
        free_compute_fraction: f(1.0),
        ..state(a())
    }]);
    let prefill = 27_021_597_764_222_980.0 / 1.0 * 1000.0;
    assert_eq!(
        score(
            defaults(),
            &request(3, "interactive", f(1e30), f(100.0)),
            &exact,
            &ids(&["p0"]),
            &unit,
            f(100.0)
        ),
        want(
            prefill,
            1000.0,
            0.0,
            0.0,
            0.35 * (prefill / 1e30) + (1.0 - 0.35) * (1000.0 / 100.0),
            1.0,
            vec![]
        )
    );
    // An int rate divides an int work once (long_true_divide): the fallback fraction 2 (int)
    // times 3 units (int) is the int 6, and 1 * 1 token / 6 is 1/6. The 60 s old state falls
    // back (decay(60.0) < 0.25) with queue depth 1 (int).
    let int_rate = RouterConfig {
        conservative_compute_fraction: i(2),
        conservative_queue_depth: i(1),
        ..defaults()
    };
    let one = doc(vec![stage(json!(1), json!(1), &[("p0", a())])]).graph();
    let s = states(vec![DeviceState {
        compute_units_per_second: i(3),
        ..stale(a())
    }]);
    let sixth = 1.0 / 6.0 * 1000.0;
    assert_eq!(
        score(
            int_rate,
            &request(1, "interactive", f(1000.0), f(100.0)),
            &one,
            &ids(&["p0"]),
            &s,
            f(100.0)
        ),
        want(
            sixth + sixth,
            sixth + sixth,
            0.0,
            0.0,
            interactive_total(sixth + sixth, sixth + sixth),
            decay(60.0, 10.0),
            vec![a()]
        )
    );
    // max(0.001, min(1.0, free)) and max(0, depth) keep CPython's argument order: min(1.0, nan)
    // is 1.0, max(0, nan) is 0; 0.0 and 0 (int) free become 0.001; True free stays 1.0 (not
    // below it) and a True depth is 1.
    let single = one_stage().graph();
    for (free, depth, rate, queue) in [
        (f(NAN), i(-5), 1024.0, 0.0),
        (f(0.0), f(NAN), 1024.0 * 0.001, 0.0),
        (f(2.0), f(2.5), 1024.0, 2.5),
        (i(0), i(0), 1024.0 * 0.001, 0.0),
        (PyReal::Bool(true), PyReal::Bool(true), 1024.0, 1.0),
    ] {
        let s = states(vec![DeviceState {
            free_compute_fraction: free.clone(),
            pending_hop_queue_depth: depth.clone(),
            ..state(a())
        }]);
        let (prefill, decode) = (8.0 / rate * 1000.0, 1.0 / rate * 1000.0);
        let (ttft, tpot) = (
            0.0 + (prefill + queue * decode),
            0.0 + (decode + queue * decode),
        );
        assert_eq!(
            score(
                defaults(),
                &interactive(),
                &single,
                &ids(&["p0"]),
                &s,
                f(100.0)
            ),
            want(
                ttft,
                tpot,
                0.0,
                0.0,
                interactive_total(ttft, tpot),
                1.0,
                vec![]
            ),
            "free {free:?}, depth {depth:?}"
        );
    }
    // A huge int work meets the float rate through float(): OverflowError (:111-113).
    let huge = doc(vec![stage(
        number(&format!("1{}", "0".repeat(400))),
        json!(1),
        &[("p0", a())],
    )])
    .graph();
    assert_eq!(
        score(
            defaults(),
            &interactive(),
            &huge,
            &ids(&["p0"]),
            &states(vec![state(a())]),
            f(100.0)
        ),
        refused("m16_route_scorer_int_too_large_for_float")
    );
}

/// `n` stages of prefill 1, decode 1 on 60 s old states with free links: each falls back to the
/// rate 1024.0 * 0.1 and queue 20; the age grows by the TTFT so far; confidence is the builtin
/// min of the stage confidences.
fn stale_chain(n: usize, fallback: Vec<Value>) -> View {
    let rate = 1024.0 * 0.1;
    let (prefill, decode) = (4.0 / rate * 1000.0, 1.0 / rate * 1000.0);
    let (mut ttft, mut tpot) = (0.0, 0.0);
    let mut confidence: Option<f64> = None;
    for _ in 0..n {
        let stage_confidence = decay(60.0 + ttft / 1000.0, 10.0);
        // Builtin min: a later value replaces the current one only if strictly below it.
        confidence = match confidence {
            Some(current) if stage_confidence < current => Some(stage_confidence),
            Some(current) => Some(current),
            None => Some(stage_confidence),
        };
        ttft += prefill + 20.0 * decode;
        tpot += decode + 20.0 * decode;
    }
    let confidence = confidence.expect("at least one stage");
    want(
        ttft,
        tpot,
        0.0,
        0.0,
        interactive_total(ttft, tpot),
        confidence,
        fallback,
    )
}

#[test]
fn m16_route_scorer_confidence_fallback() {
    // :91 the TTFT so far is added to the projected age. Stage 0 (age 0.0, confidence 1.0, rate
    // 1.0) costs 8000.0 ms, so stage 1 is 8.0 s old: decay(8.0) = 0.574 is below 0.6 and falls
    // back to the conservative fraction 0.1 and queue depth 20 (:97-106). One node: links 0.0.
    let same = doc(vec![
        stage(json!(2), json!(1), &[("p0", a())]),
        stage(json!(3), json!(1), &[("p1", a())]),
    ])
    .graph();
    let unit = states(vec![DeviceState {
        compute_units_per_second: f(1.0),
        free_compute_fraction: f(1.0),
        ..state(a())
    }]);
    let picky = RouterConfig {
        minimum_confidence: f(0.6),
        ..defaults()
    };
    let (prefill, decode) = (12.0 / (1.0 * 0.1) * 1000.0, 1.0 / (1.0 * 0.1) * 1000.0);
    let ttft = 8000.0 + (prefill + 20.0 * decode);
    let tpot = 1000.0 + (decode + 20.0 * decode);
    assert_eq!(
        score(
            picky,
            &interactive(),
            &same,
            &ids(&["p0", "p1"]),
            &unit,
            f(100.0)
        ),
        want(
            ttft,
            tpot,
            0.0,
            0.0,
            interactive_total(ttft, tpot),
            decay(0.0 + 8000.0 / 1000.0, 10.0),
            vec![a()]
        )
    );
    // :94 exactly at the minimum is not below it: confidence 1.0 against 1.0 or 1 (int) keeps
    // the state's own rate 512.0 (prefill 8/512*1000 = 15.625, decode 1.953125).
    let single = one_stage().graph();
    let fresh = states(vec![state(a())]);
    for minimum in [f(1.0), i(1)] {
        let config = RouterConfig {
            minimum_confidence: minimum,
            ..defaults()
        };
        assert_eq!(
            score(
                config,
                &interactive(),
                &single,
                &ids(&["p0"]),
                &fresh,
                f(100.0)
            ),
            want(
                15.625,
                1.953125,
                0.0,
                0.0,
                interactive_total(15.625, 1.953125),
                1.0,
                vec![]
            )
        );
    }
    // A NaN minimum is never exceeded (an old state keeps its rate); a huge int minimum is above
    // every confidence (a fresh state falls back to 1024 * 0.1 with queue 20).
    let config = RouterConfig {
        minimum_confidence: f(NAN),
        ..defaults()
    };
    assert_eq!(
        score(
            config,
            &interactive(),
            &single,
            &ids(&["p0"]),
            &states(vec![stale(a())]),
            f(100.0)
        ),
        want(
            15.625,
            1.953125,
            0.0,
            0.0,
            interactive_total(15.625, 1.953125),
            decay(60.0, 10.0),
            vec![]
        )
    );
    let config = RouterConfig {
        minimum_confidence: big(400),
        ..defaults()
    };
    let (prefill, decode) = (8.0 / (1024.0 * 0.1) * 1000.0, 1.0 / (1024.0 * 0.1) * 1000.0);
    let (ttft, tpot) = (prefill + 20.0 * decode, decode + 20.0 * decode);
    assert_eq!(
        score(
            config,
            &interactive(),
            &single,
            &ids(&["p0"]),
            &fresh,
            f(100.0)
        ),
        want(
            ttft,
            tpot,
            0.0,
            0.0,
            interactive_total(ttft, tpot),
            1.0,
            vec![a()]
        )
    );
    // :171 the half-life is max(0.001, half_life): 0.0, -5 (int), NaN and 1e-300 all give 0.001;
    // 7 (int) divides as 7.0; a huge int raises (float / int).
    let young = |half_life: PyReal, now: f64| {
        let config = RouterConfig {
            confidence_half_life_seconds: half_life,
            ..defaults()
        };
        let s = states(vec![DeviceState {
            last_updated: f(0.0),
            ..state(a())
        }]);
        score(config, &interactive(), &single, &ids(&["p0"]), &s, f(now))
    };
    let cheap = interactive_total(15.625, 1.953125);
    for half_life in [f(0.0), i(-5), f(NAN), f(1e-300)] {
        assert_eq!(
            young(half_life.clone(), 0.0005),
            want(
                15.625,
                1.953125,
                0.0,
                0.0,
                cheap,
                decay(0.0005, 0.001),
                vec![]
            ),
            "{half_life:?}"
        );
    }
    assert_eq!(
        young(i(7), 3.5),
        want(15.625, 1.953125, 0.0, 0.0, cheap, decay(3.5, 7.0), vec![])
    );
    assert_eq!(
        young(big(400), 1.0),
        refused("m16_route_scorer_int_too_large_for_float")
    );
    // :85, :96, :167 the fallback set: each node once, sorted. Free links (rtt 0.0, infinite
    // bandwidth) make every link 0.0.
    let free_links = RouterConfig {
        default_rtt_ms: f(0.0),
        default_bandwidth_bytes_per_second: f(INF),
        ..defaults()
    };
    let chain = |nodes: &[Value]| -> Graph {
        let names = ["p0", "p1", "p2", "p3"];
        doc(nodes
            .iter()
            .zip(names)
            .map(|(node, placement)| stage(json!(1), json!(1), &[(placement, node.clone())]))
            .collect())
        .graph()
    };
    let abc = states(vec![stale(a()), stale(b()), stale(c())]);
    assert_eq!(
        score(
            free_links.clone(),
            &interactive(),
            &chain(&[c(), a(), b(), a()]),
            &ids(&["p0", "p1", "p2", "p3"]),
            &abc,
            f(100.0)
        ),
        stale_chain(4, vec![a(), b(), c()])
    );
    // Numbers and bools sort by value.
    let numeric = states(vec![
        stale(json!(10)),
        stale(json!(2.5)),
        stale(json!(true)),
    ]);
    assert_eq!(
        score(
            free_links.clone(),
            &interactive(),
            &chain(&[json!(10), json!(2.5), json!(true)]),
            &ids(&["p0", "p1", "p2"]),
            &numeric,
            f(100.0)
        ),
        stale_chain(3, vec![json!(true), json!(2.5), json!(10)])
    );
    // Python-equal ids are one dict key and one set member, spelled as first seen.
    let ones = states(vec![stale(json!(1))]);
    assert_eq!(
        score(
            free_links.clone(),
            &interactive(),
            &chain(&[json!(1), json!(1.0)]),
            &ids(&["p0", "p1"]),
            &ones,
            f(100.0)
        ),
        stale_chain(2, vec![json!(1)])
    );
    // A string next to a number cannot be sorted (TypeError, :167).
    let mixed = states(vec![stale(json!(7)), stale(a())]);
    assert_eq!(
        score(
            free_links,
            &interactive(),
            &chain(&[json!(7), a()]),
            &ids(&["p0", "p1"]),
            &mixed,
            f(100.0)
        ),
        refused("m16_route_scorer_unorderable_node_ids")
    );
    // A negative fallback queue depth makes the next stage's age so negative that math.exp
    // overflows (OverflowError, :172).
    let negative = RouterConfig {
        conservative_queue_depth: i(-10_000_000),
        ..defaults()
    };
    assert_eq!(
        score(
            negative,
            &interactive(),
            &two_stage().graph(),
            &ids(&["p0", "p1"]),
            &states(vec![stale(a()), state(b())]),
            f(100.0)
        ),
        refused("m16_route_scorer_math_range_error")
    );
    // :91 now: max(0.0, nan) is 0.0; an infinite age gives confidence 0.0 and falls back; huge
    // ints subtract exactly (10**400 + 5 - 10**400 = 5); a huge int minus a float raises.
    assert_eq!(
        score(
            defaults(),
            &interactive(),
            &single,
            &ids(&["p0"]),
            &fresh,
            f(NAN)
        ),
        want(15.625, 1.953125, 0.0, 0.0, cheap, 1.0, vec![])
    );
    let (prefill, decode) = (8.0 / (1024.0 * 0.1) * 1000.0, 1.0 / (1024.0 * 0.1) * 1000.0);
    let (ttft, tpot) = (prefill + 20.0 * decode, decode + 20.0 * decode);
    assert_eq!(
        score(
            defaults(),
            &interactive(),
            &single,
            &ids(&["p0"]),
            &fresh,
            f(INF)
        ),
        want(
            ttft,
            tpot,
            0.0,
            0.0,
            interactive_total(ttft, tpot),
            0.0,
            vec![a()]
        )
    );
    let at_big = states(vec![DeviceState {
        last_updated: big(400),
        ..state(a())
    }]);
    let big_plus_five = digits(&format!("1{}5", "0".repeat(399)));
    assert_eq!(
        score(
            defaults(),
            &interactive(),
            &single,
            &ids(&["p0"]),
            &at_big,
            big_plus_five
        ),
        want(15.625, 1.953125, 0.0, 0.0, cheap, decay(5.0, 10.0), vec![])
    );
    assert_eq!(
        score(
            defaults(),
            &interactive(),
            &single,
            &ids(&["p0"]),
            &fresh,
            big(400)
        ),
        refused("m16_route_scorer_int_too_large_for_float")
    );
}

#[test]
fn m16_route_scorer_link_transfer() {
    let graph = two_stage().graph();
    let route = ids(&["p0", "p1"]);
    let run =
        |config: RouterConfig, req: &RequestContext, graph: &Graph, s: &IdMap<DeviceState>| {
            score(config, req, graph, &route, s, f(100.0))
        };
    let req = interactive();
    let confidence = decay(0.015625, 10.0);
    // :124-128, :263 link = rtt/2 + payload/bandwidth*1000 over the prefill (64 B) and decode
    // (16 B) payloads, plus the 16-byte loopback token (`standard` spells the arithmetic out).
    assert_eq!(
        run(defaults(), &req, &graph, &linked()),
        standard(interactive_total(1040.0625, 2256.90625))
    );
    // :250-251 one node: every link 0.0. Compute only: TTFT 39.0625, TPOT 3.90625.
    let same = doc(vec![
        stage(json!(2), json!(1), &[("p0", a())]),
        stage(json!(3), json!(1), &[("p1", a())]),
    ])
    .graph();
    assert_eq!(
        run(defaults(), &req, &same, &states(vec![state(a())])),
        want(
            39.0625,
            3.90625,
            0.0,
            0.0,
            interactive_total(39.0625, 3.90625),
            confidence,
            vec![]
        )
    );
    // :194-198, :258-260 no neighbour entry: the default rtt 100.0 and 1_000_000.0 B/s.
    let prefill_link = 100.0 / 2.0 + 64.0 / 1_000_000.0 * 1000.0;
    let decode_link = 100.0 / 2.0 + 16.0 / 1_000_000.0 * 1000.0;
    let (ttft, tpot) = (
        39.0625 + prefill_link,
        3.90625 + (decode_link + decode_link),
    );
    assert_eq!(
        run(
            defaults(),
            &req,
            &graph,
            &states(vec![state(a()), state(b())])
        ),
        want(
            ttft,
            tpot,
            prefill_link,
            decode_link + decode_link,
            interactive_total(ttft, tpot),
            confidence,
            vec![]
        )
    );
    // :193 a stated rtt is float(rtt): 3 (int) -> 3.0, True -> 1.0; an int bandwidth divides
    // the int payload exactly (64 / 1024 = 0.0625, 16 / 1024 = 0.015625).
    let ints = states(vec![
        link(state(a()), b(), Some(i(3)), Some(i(1024))),
        link(state(b()), a(), Some(PyReal::Bool(true)), None),
    ]);
    let back = 1.0 / 2.0 + 16.0 / 1_000_000.0 * 1000.0;
    let (ttft, tpot) = (39.0625 + 64.0, 3.90625 + (17.125 + back));
    assert_eq!(
        run(defaults(), &req, &graph, &ints),
        want(
            ttft,
            tpot,
            64.0,
            17.125 + back,
            interactive_total(ttft, tpot),
            confidence,
            vec![]
        )
    );
    // :261-262 a bandwidth not above 0 makes the link inf, but the route is still scored (its
    // confidence is kept): 0.0, -2.5, 0 (int), False; and -0.0 as the default.
    let inf_links = want(INF, INF, INF, INF, INF, confidence, vec![]);
    for bandwidth in [f(0.0), f(-2.5), i(0), PyReal::Bool(false)] {
        let s = states(vec![
            link(state(a()), b(), Some(f(2.0)), Some(bandwidth.clone())),
            link(state(b()), a(), Some(f(4.0)), Some(f(8.0))),
        ]);
        assert_eq!(
            run(defaults(), &req, &graph, &s),
            inf_links,
            "{bandwidth:?}"
        );
    }
    let negative_zero = RouterConfig {
        default_bandwidth_bytes_per_second: f(-0.0),
        ..defaults()
    };
    let s = states(vec![
        link(state(a()), b(), Some(f(2.0)), None),
        link(state(b()), a(), Some(f(4.0)), Some(f(8.0))),
    ]);
    assert_eq!(run(negative_zero, &req, &graph, &s), inf_links);
    // Near miss: a NaN bandwidth is not <= 0, so its links are NaN, not inf.
    let s = states(vec![
        link(state(a()), b(), Some(f(2.0)), Some(f(NAN))),
        link(state(b()), a(), Some(f(4.0)), Some(f(8.0))),
    ]);
    assert_eq!(
        run(defaults(), &req, &graph, &s),
        want(NAN, NAN, NAN, NAN, NAN, confidence, vec![])
    );
    // int / int is CPython's correctly rounded true division: a (3 * 2**53 + 3)-byte payload over
    // an int default bandwidth of 3 is 2**53 + 1 exactly, which rounds half-even to 2**53
    // (float / float would give 2**53 + 2). One prompt token: compute TTFT 2/512*1000 +
    // 3/512*1000 = 9.765625, TPOT 3.90625; the 7-byte token gives 7/3 -> 7.0/3.0.
    let wide = two_stage()
        .sizes(number("27021597764222979"), json!(1), json!(7))
        .graph();
    let int_bandwidth = RouterConfig {
        default_bandwidth_bytes_per_second: i(3),
        ..defaults()
    };
    let one_token = request(1, "interactive", f(1e30), f(1e30));
    let payload_link = 100.0 / 2.0 + 9_007_199_254_740_992.0 * 1000.0;
    let token_link = 100.0 / 2.0 + 7.0 / 3.0 * 1000.0;
    let (ttft, tpot) = (
        9.765625 + payload_link,
        3.90625 + (payload_link + token_link),
    );
    let bare = states(vec![state(a()), state(b())]);
    assert_eq!(
        run(int_bandwidth, &one_token, &wide, &bare),
        want(
            ttft,
            tpot,
            payload_link,
            payload_link + token_link,
            0.35 * (ttft / 1e30) + (1.0 - 0.35) * (tpot / 1e30),
            decay(3.90625 / 1000.0, 10.0),
            vec![]
        )
    );
    // ...rounded correctly into the subnormal range too: 3 / (7 * 10**320) is 867.43... units
    // of 2**-1074, so 867 units, and times 1000.0 exactly 867000 units; the 1-byte token gives
    // 289.14... -> 289 -> 289000 units (rtt default 0.0).
    let tiny = two_stage().sizes(json!(3), json!(1), json!(1)).graph();
    let huge_bandwidth = RouterConfig {
        default_bandwidth_bytes_per_second: digits(&format!("7{}", "0".repeat(320))),
        default_rtt_ms: f(0.0),
        ..defaults()
    };
    let (payload_link, token_link) = (f64::from_bits(867_000), f64::from_bits(289_000));
    let one_prompt = request(1, "interactive", f(1000.0), f(100.0));
    let (ttft, tpot) = (
        9.765625 + payload_link,
        3.90625 + (payload_link + token_link),
    );
    assert_eq!(
        run(huge_bandwidth, &one_prompt, &tiny, &bare),
        want(
            ttft,
            tpot,
            payload_link,
            payload_link + token_link,
            interactive_total(ttft, tpot),
            decay(3.90625 / 1000.0, 10.0),
            vec![]
        )
    );
    // Overflow in the oracle's order: float(stated rtt) raises before the bandwidth check
    // (:193 before :261), but a huge default rtt is only divided after it (:263).
    let s = states(vec![
        link(state(a()), b(), Some(big(400)), Some(f(0.0))),
        link(state(b()), a(), Some(f(4.0)), Some(f(8.0))),
    ]);
    assert_eq!(
        run(defaults(), &req, &graph, &s),
        refused("m16_route_scorer_int_too_large_for_float")
    );
    let huge_rtt = RouterConfig {
        default_rtt_ms: big(400),
        ..defaults()
    };
    let closed = states(vec![
        link(state(a()), b(), None, Some(f(0.0))),
        link(state(b()), a(), None, Some(i(-1))),
    ]);
    assert_eq!(
        run(huge_rtt.clone(), &req, &graph, &closed),
        want(INF, INF, INF, INF, INF, confidence, vec![])
    );
    let open = states(vec![link(state(a()), b(), None, Some(f(1.0))), state(b())]);
    assert_eq!(
        run(huge_rtt, &req, &graph, &open),
        refused("m16_route_scorer_int_too_large_for_float")
    );
    // A payload too large for a float: OverflowError of int / int ("integer division result too
    // large for a float") or of int / float, and of a float hidden size times a huge int
    // activation size (:125).
    let ten_400 = number(&format!("1{}", "0".repeat(400)));
    let huge_payload = two_stage()
        .sizes(ten_400.clone(), json!(1), json!(16))
        .graph();
    let unit_bandwidth = RouterConfig {
        default_bandwidth_bytes_per_second: i(1),
        ..defaults()
    };
    assert_eq!(
        run(unit_bandwidth, &req, &huge_payload, &bare),
        refused("m16_route_scorer_int_division_too_large_for_float")
    );
    assert_eq!(
        run(defaults(), &req, &huge_payload, &bare),
        refused("m16_route_scorer_int_too_large_for_float")
    );
    let float_hidden = two_stage().sizes(json!(1.5), ten_400, json!(16)).graph();
    assert_eq!(
        run(defaults(), &req, &float_hidden, &bare),
        refused("m16_route_scorer_int_too_large_for_float")
    );
}

/// A test-owned record of a provenance report: its ids, rtt (type and exact value), source,
/// sample count and measured flag; or the refusal code.
type Report = Result<(Value, Value, Option<String>, &'static str, u64, bool), &'static str>;

fn report(got: Result<TransferProvenance, ScorerError>) -> Report {
    got.map(|p| {
        (
            p.source_node_id,
            p.destination_node_id,
            p.rtt_ms.as_ref().map(tag),
            p.provenance,
            p.sample_count,
            p.is_measured,
        )
    })
    .map_err(|error| error.0)
}

#[test]
fn m16_route_scorer_transfer_provenance() {
    let mut source = state(a());
    for (to, rtt) in [
        (b(), f(2.5)),
        (c(), i(7)),
        (json!(3), big(400)),
        (json!(2.0), f(6.0)),
    ] {
        source.neighbor_rtt_ms.insert(to, rtt).expect("hashable");
    }
    let known = states(vec![source, state(json!(1))]);
    let row = |s: Value, d: Value, rtt: Option<PyReal>, provenance: &'static str| -> Report {
        Ok((s, d, rtt.as_ref().map(tag), provenance, 0, false))
    };
    let scorer = RouteScorer::new(defaults());
    let cases: Vec<(Value, Value, Report)> = vec![
        // :212-220 one node (Python ==, so 1 == 1.0 == True and equal lists, unhashed).
        (a(), a(), row(a(), a(), Some(f(0.0)), "same_node")),
        (
            json!(1),
            json!(1.0),
            row(json!(1), json!(1.0), Some(f(0.0)), "same_node"),
        ),
        (
            json!(true),
            json!(1),
            row(json!(true), json!(1), Some(f(0.0)), "same_node"),
        ),
        (
            json!(["x"]),
            json!(["x"]),
            row(json!(["x"]), json!(["x"]), Some(f(0.0)), "same_node"),
        ),
        // :221-230 no source state.
        (
            json!("node-z"),
            a(),
            row(json!("node-z"), a(), None, "unknown_source_node"),
        ),
        (
            json!(["x"]),
            a(),
            Err("m16_route_scorer_unhashable_node_id"),
        ),
        // :191-193 the stated value as float(), Python-equal keys included (2 finds 2.0).
        (
            a(),
            b(),
            row(a(), b(), Some(f(2.5)), "state_neighbor_rtt_ms"),
        ),
        (
            a(),
            c(),
            row(a(), c(), Some(f(7.0)), "state_neighbor_rtt_ms"),
        ),
        (
            a(),
            json!(2),
            row(a(), json!(2), Some(f(6.0)), "state_neighbor_rtt_ms"),
        ),
        (
            a(),
            json!(3),
            Err("m16_route_scorer_int_too_large_for_float"),
        ),
        (
            a(),
            json!(["node-b"]),
            Err("m16_route_scorer_unhashable_node_id"),
        ),
        // :194-198 otherwise the configured default, as configured.
        (
            a(),
            json!("node-d"),
            row(
                a(),
                json!("node-d"),
                Some(f(100.0)),
                "fallback_default_rtt_ms",
            ),
        ),
        (
            json!(1),
            a(),
            row(json!(1), a(), Some(f(100.0)), "fallback_default_rtt_ms"),
        ),
        (
            json!(1.0),
            a(),
            row(json!(1.0), a(), Some(f(100.0)), "fallback_default_rtt_ms"),
        ),
    ];
    for (s, d, expected) in cases {
        assert_eq!(
            report(scorer.transfer_provenance(&s, &d, &known)),
            expected,
            "{s} -> {d}"
        );
    }
    for default in [i(9), PyReal::Bool(true)] {
        let scorer = RouteScorer::new(RouterConfig {
            default_rtt_ms: default.clone(),
            ..defaults()
        });
        assert_eq!(
            report(scorer.transfer_provenance(&a(), &json!("node-d"), &known)),
            row(
                a(),
                json!("node-d"),
                Some(default),
                "fallback_default_rtt_ms"
            )
        );
    }
}

#[test]
fn m16_route_scorer_qos_total_score() {
    let graph = two_stage().graph();
    let route = ids(&["p0", "p1"]);
    let run = |config: RouterConfig, req: RequestContext| {
        score(config, &req, &graph, &route, &linked(), f(100.0))
    };
    let (ttft, tpot) = (1040.0625, 2256.90625);
    // :156-159 alpha * (ttft / target_ttft) + (1.0 - alpha) * (tpot / target_tpot), alpha 0.10
    // for "batch" and 0.35 for any other class.
    assert_eq!(
        run(defaults(), interactive()),
        standard(0.35 * (ttft / 1000.0) + (1.0 - 0.35) * (tpot / 100.0))
    );
    assert_eq!(
        run(defaults(), request(4, "batch", f(1000.0), f(100.0))),
        standard(0.10 * (ttft / 1000.0) + (1.0 - 0.10) * (tpot / 100.0))
    );
    assert_eq!(
        run(defaults(), request(4, "BATCH", f(1000.0), f(100.0))),
        standard(0.35 * (ttft / 1000.0) + (1.0 - 0.35) * (tpot / 100.0))
    );
    // Int targets divide through float(): 1000 -> 1000.0, 3 -> 3.0.
    assert_eq!(
        run(defaults(), request(4, "interactive", i(1000), i(3))),
        standard(0.35 * (ttft / 1000.0) + (1.0 - 0.35) * (tpot / 3.0))
    );
    // ZeroDivisionError for a zero target of any type (:157, :158).
    for (target_ttft, target_tpot) in [
        (f(0.0), f(100.0)),
        (f(-0.0), f(100.0)),
        (PyReal::Bool(false), f(100.0)),
        (f(1000.0), i(0)),
    ] {
        assert_eq!(
            run(
                defaults(),
                request(4, "interactive", target_ttft.clone(), target_tpot.clone())
            ),
            refused("m16_route_scorer_zero_division"),
            "{target_ttft:?} / {target_tpot:?}"
        );
    }
    // OverflowError where a huge int target or alpha meets a float; the ttft division raises
    // first (:157 evaluates ttft / target before alpha * it).
    assert_eq!(
        run(defaults(), request(4, "interactive", big(400), f(100.0))),
        refused("m16_route_scorer_int_too_large_for_float")
    );
    let huge_alpha = RouterConfig {
        interactive_alpha: big(400),
        ..defaults()
    };
    assert_eq!(
        run(huge_alpha.clone(), interactive()),
        refused("m16_route_scorer_int_too_large_for_float")
    );
    assert_eq!(
        run(huge_alpha, request(4, "interactive", f(0.0), f(100.0))),
        refused("m16_route_scorer_zero_division")
    );
    // A bool alpha is the int 1: 1 * x + (1.0 - 1) * y; an infinite alpha gives inf + -inf = NaN.
    let bool_alpha = RouterConfig {
        batch_alpha: PyReal::Bool(true),
        ..defaults()
    };
    assert_eq!(
        run(bool_alpha, request(4, "batch", f(1000.0), f(100.0))),
        standard(1.0 * (ttft / 1000.0) + 0.0 * (tpot / 100.0))
    );
    let inf_alpha = RouterConfig {
        interactive_alpha: f(INF),
        ..defaults()
    };
    assert_eq!(
        run(inf_alpha, interactive()),
        standard(INF * (ttft / 1000.0) + (1.0 - INF) * (tpot / 100.0))
    );
}

// Paired oracle probes. Every case below was executed through the real v0.1.4 code path at
// f4e366e9 (execution_graph_from_dict, RouteScorer(config) with no rtt table, and for the
// factory cases the DeviceStates and RouterConfig that build_live_m16_runtime hands its
// coordinator) by the probe named in the fixture's provenance (CPython 3.14.2). The fixture
// keeps each value's Python type: {"int": str(n)}, {"bool": b} or {"float": repr(x), "bits"}.
const ORACLE: &str = include_str!("fixtures/m16-route-scorer-oracle.json");

/// A recorded number as the value the oracle held.
fn recorded(tagged: &Value) -> PyReal {
    if let Some(flag) = tagged["bool"].as_bool() {
        return PyReal::Bool(flag);
    }
    if let Some(text) = tagged["int"].as_str() {
        let (negative, magnitude) = text
            .strip_prefix('-')
            .map_or((false, text), |rest| (true, rest));
        return PyReal::signed(negative, magnitude.parse().expect("int digits"));
    }
    let bits = tagged["bits"].as_str().expect("a number tag");
    PyReal::Float(f64::from_bits(
        u64::from_str_radix(bits, 16).expect("hex bits"),
    ))
}

/// The recorded number in [`tag`]'s spelling.
fn recorded_tag(tagged: &Value) -> String {
    tag(&recorded(tagged))
}

fn set_field(config: &mut RouterConfig, name: &str, value: PyReal) {
    let field = match name {
        "interactive_alpha" => &mut config.interactive_alpha,
        "batch_alpha" => &mut config.batch_alpha,
        "confidence_half_life_seconds" => &mut config.confidence_half_life_seconds,
        "minimum_confidence" => &mut config.minimum_confidence,
        "conservative_compute_fraction" => &mut config.conservative_compute_fraction,
        "conservative_queue_depth" => &mut config.conservative_queue_depth,
        "default_bandwidth_bytes_per_second" => &mut config.default_bandwidth_bytes_per_second,
        "default_rtt_ms" => &mut config.default_rtt_ms,
        "reservation_lease_seconds" => &mut config.reservation_lease_seconds,
        other => panic!("unmapped RouterConfig field {other}"),
    };
    *field = value;
}

fn recorded_map(pairs: &Value) -> IdMap<PyReal> {
    let mut map = IdMap::new();
    for pair in pairs.as_array().expect("pairs") {
        map.insert(pair[0].clone(), recorded(&pair[1]))
            .expect("hashable key");
    }
    map
}

fn recorded_states(list: &Value) -> IdMap<DeviceState> {
    let mut map = IdMap::new();
    for entry in list.as_array().expect("states") {
        let (key, doc) = (&entry[0], &entry[1]);
        let s = DeviceState {
            node_id: key.clone(),
            state_seq: i(1),
            last_updated: recorded(&doc["last_updated"]),
            availability: doc["availability"]
                .as_str()
                .expect("availability")
                .to_owned(),
            compute_units_per_second: recorded(&doc["compute_units_per_second"]),
            free_compute_fraction: recorded(&doc["free_compute_fraction"]),
            available_kv_bytes: i(0),
            pending_hop_queue_depth: recorded(&doc["pending_hop_queue_depth"]),
            neighbor_rtt_ms: recorded_map(&doc["neighbor_rtt_ms"]),
            neighbor_bandwidth_bytes_per_second: recorded_map(
                &doc["neighbor_bandwidth_bytes_per_second"],
            ),
        };
        map.insert(key.clone(), s).expect("hashable node id");
    }
    map
}

/// The port's code for the exception the oracle raised, by type, message and raising line.
fn oracle_code(result: &Value) -> &'static str {
    let line = result["oracle_line"].as_str().unwrap_or("");
    match (
        result["exception"].as_str().expect("exception"),
        result["message"].as_str().expect("message"),
    ) {
        ("ZeroDivisionError", _) => "m16_route_scorer_zero_division",
        ("OverflowError", "int too large to convert to float") => {
            "m16_route_scorer_int_too_large_for_float"
        }
        ("OverflowError", "integer division result too large for a float") => {
            "m16_route_scorer_int_division_too_large_for_float"
        }
        ("OverflowError", "math range error") => "m16_route_scorer_math_range_error",
        ("TypeError", _) if line == "mycelium_router/scoring.py:57" => {
            "m16_route_scorer_unhashable_placement_id"
        }
        ("TypeError", _)
            if [":88", ":191", ":221", ":252"]
                .iter()
                .any(|at| line == format!("mycelium_router/scoring.py{at}")) =>
        {
            "m16_route_scorer_unhashable_node_id"
        }
        ("TypeError", _) if line == "mycelium_router/scoring.py:167" => {
            "m16_route_scorer_unorderable_node_ids"
        }
        other => panic!("unmapped oracle exception {other:?} at {line}"),
    }
}

fn expected_score(recorded_score: &Value) -> View {
    if recorded_score.get("exception").is_some() {
        return Err(oracle_code(recorded_score));
    }
    let bits = |name: &str| match recorded(&recorded_score[name]) {
        PyReal::Float(x) => x.to_bits(),
        other => panic!("{name} is not a float: {other:?}"),
    };
    Ok((
        [
            "ttft_ms",
            "tpot_ms",
            "prefill_transfer_ms",
            "decode_transfer_ms",
            "total_score",
            "confidence",
        ]
        .map(bits),
        recorded_score["fallback_nodes"]
            .as_array()
            .expect("fallback nodes")
            .clone(),
    ))
}

fn expected_report(result: &Value) -> Report {
    if result.get("exception").is_some() {
        return Err(oracle_code(result));
    }
    let provenance = match result["provenance"].as_str().expect("provenance") {
        "same_node" => "same_node",
        "unknown_source_node" => "unknown_source_node",
        "state_neighbor_rtt_ms" => "state_neighbor_rtt_ms",
        "fallback_default_rtt_ms" => "fallback_default_rtt_ms",
        other => panic!("unexpected provenance {other}"),
    };
    Ok((
        result["source_node_id"].clone(),
        result["destination_node_id"].clone(),
        (!result["rtt_ms"].is_null()).then(|| recorded_tag(&result["rtt_ms"])),
        provenance,
        result["sample_count"].as_u64().expect("sample count"),
        result["is_measured"].as_bool().expect("is_measured"),
    ))
}

#[test]
fn m16_route_scorer_python_value_domain_parity() {
    let fixture: Value = serde_json::from_str(ORACLE).expect("fixture JSON");
    let provenance = &fixture["provenance"];
    assert_eq!(
        provenance["oracle_commit"],
        json!("f4e366e98a1f705dfaf182897044500d290dbd7f")
    );
    assert_eq!(provenance["interpreter"], json!("CPython 3.14.2"));
    assert_eq!(
        provenance["probe"],
        json!(
            "/Users/joinmac/Projects/mycelium-core-program/status/moonweaver/workers/l1b-m16-route-scorer/evidence/oracle_route_scorer_probe.py"
        )
    );
    let cases = fixture["cases"].as_array().expect("cases");
    assert!(cases.len() >= 20, "{} paired probes", cases.len());
    let (mut scored, mut raised, mut reports) = (0, 0, 0);
    for case in cases {
        let name = case["name"].as_str().expect("case name");
        let document: Value =
            serde_json::from_str(case["graph_json"].as_str().expect("graph text"))
                .expect("graph JSON");
        let graph = execution_graph::parse(&document).expect("the oracle parsed this graph");
        let mut config = defaults();
        for (field, value) in case["config"].as_object().expect("config overrides") {
            set_field(&mut config, field, recorded(value));
        }
        let req = &case["request"];
        let req = request(
            usize::try_from(req["prompt_len"].as_u64().expect("prompt length")).expect("usize"),
            req["qos_class"].as_str().expect("qos class"),
            recorded(&req["target_ttft_ms"]),
            recorded(&req["target_tpot_ms"]),
        );
        let device_states = recorded_states(&case["states"]);
        let route = case["placement_ids"].as_array().expect("route").clone();
        let scorer = RouteScorer::new(config);
        let got = scorer.score_route(
            &req,
            &graph,
            &route,
            &device_states,
            &recorded(&case["now"]),
        );
        let expected = expected_score(&case["score"]);
        if expected.is_ok() {
            scored += 1;
        } else {
            raised += 1;
        }
        assert_eq!(view(&got), expected, "{name}: score_route");
        for query in case["provenance"].as_array().expect("provenance queries") {
            reports += 1;
            assert_eq!(
                report(scorer.transfer_provenance(
                    &query["source"],
                    &query["destination"],
                    &device_states
                )),
                expected_report(&query["result"]),
                "{name}: transfer_provenance({}, {})",
                query["source"],
                query["destination"]
            );
        }
    }
    // Both outcomes and every provenance source are exercised.
    assert!(
        scored >= 20 && raised >= 10 && reports >= 15,
        "{scored} scored, {raised} raised, {reports} reports"
    );
}
