// SPDX-License-Identifier: AGPL-3.0-only
//! M16 resource ledger (CL-L1-07): source parity for `_Reservation` and
//! `_ResourceLedgerCapacityPort`, mycelium_m16_runtime.py:140-346 @ f4e366e9, with
//! `ReservationRequest`/`ReservationResult`/`ReservationCommitResult` (mycelium_router/
//! contracts.py:186-208). The first seven tests' expected values are derived by hand from the
//! cited oracle lines in the comments; none is computed by the ledger. The last test replays
//! probes recorded from the real oracle (fixtures/m16-ledger-oracle.json).
use protocol::execution_graph::{self, Graph};
use runtime::m16_capacity::{
    PlacementCapacity, ProjectedNodes, PyInt, PyNumber, live_placement_capacities,
};
use runtime::m16_ledger::{
    FreeBytes, IdSource, LeaseClock, LedgerError, PlacementStatus, Reservation,
    ReservationCommitResult, ReservationRequest, ReservationResult, ResourceLedger,
};
use serde_json::{Value, json};
use std::cell::{Cell, RefCell};
use std::collections::VecDeque;

/// A settable lease clock (starts at 0.0).
#[derive(Default)]
struct Clock(Cell<f64>);

impl LeaseClock for Clock {
    fn now(&self) -> f64 {
        self.0.get()
    }
}

/// `new(prefix)` gives `prefix-1`, `prefix-2`, ... unless ids are scripted.
#[derive(Default)]
struct Ids {
    count: Cell<u64>,
    scripted: RefCell<Option<VecDeque<String>>>,
}

impl IdSource for Ids {
    fn new_id(&self, prefix: &str) -> String {
        if let Some(queue) = self.scripted.borrow_mut().as_mut() {
            return queue.pop_front().expect("a scripted id");
        }
        let n = self.count.get() + 1;
        self.count.set(n);
        format!("{prefix}-{n}")
    }
}

type Ledger<'a> = ResourceLedger<&'a Clock, &'a Ids>;

/// A valid execution graph document: one stage per `(kv_bytes_per_context_token, placements)`,
/// each placement `(placement_id, node_id)`; every final-stage placement loops back to the first.
fn document(activation: Value, epoch: Value, stages: &[(Value, &[(Value, Value)])]) -> Value {
    let last = stages.len() - 1;
    let first = stages[0].1[0].0.clone();
    let mut stage_values = Vec::new();
    let mut loopbacks = Vec::new();
    for (i, (kv, placements)) in stages.iter().enumerate() {
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
        "deployment_epoch": epoch,
        "topology_version": 1,
        "model_id": "model-m16",
        "resolved_commit": "commit-m16",
        "manifest_digest": "digest-m16",
        "entry_stage_id": "stage-0",
        "final_stage_id": format!("stage-{last}"),
        "hidden_size": 8,
        "activation_bytes": activation,
        "token_envelope_bytes": 0,
        "stages": stage_values,
        "edges": [],
        "loopback_edges": loopbacks,
    })
}

fn parse(value: &Value) -> Graph {
    execution_graph::parse(value).expect("valid execution graph")
}

fn graph(activation: Value, stages: &[(Value, &[(Value, Value)])]) -> Graph {
    parse(&document(activation, json!(1), stages))
}

/// One stage holding `p0` on `node-a`, KV cost 8.
fn one(activation: Value) -> Graph {
    graph(activation, &[(json!(8), &[(json!("p0"), json!("node-a"))])])
}

fn int(n: u64) -> PyNumber {
    PyNumber::from(n)
}

fn digits(text: &str) -> PyInt {
    text.parse().expect("decimal literal")
}

fn cap(id: Value, memory: u64, kv: u64, workspace: u64, maximum: u64) -> PlacementCapacity {
    PlacementCapacity {
        placement_id: id,
        memory_capacity_bytes: int(memory),
        kv_capacity_bytes: int(kv),
        workspace_capacity_bytes: int(workspace),
        maximum_reservations: maximum,
    }
}

/// Request `request_id` for `kv` bytes on `p0`: path `path-1`, attempt 0, epoch 1, lease 100.0.
fn request(request_id: &str, kv: PyNumber) -> ReservationRequest {
    ReservationRequest {
        request_id: request_id.to_owned(),
        path_id: "path-1".to_owned(),
        path_attempt: 0,
        placement_id: json!("p0"),
        kv_bytes: kv,
        deployment_epoch: json!(1),
        lease_expires_at: 100.0,
    }
}

fn on(placement: Value, request_id: &str, kv: u64) -> ReservationRequest {
    ReservationRequest {
        placement_id: placement,
        ..request(request_id, int(kv))
    }
}

/// contracts.py:197-202, accepted.
fn ok(id: &str, epoch: Value, expires_at: f64) -> Result<ReservationResult, LedgerError> {
    Ok(ReservationResult {
        accepted: true,
        reservation_id: id.to_owned(),
        reason: "",
        deployment_epoch: epoch,
        expires_at,
    })
}

/// contracts.py:197-202, refused: the defaults "", -1 and 0.0.
fn no(reason: &'static str) -> Result<ReservationResult, LedgerError> {
    Ok(ReservationResult {
        accepted: false,
        reservation_id: String::new(),
        reason,
        deployment_epoch: json!(-1),
        expires_at: 0.0,
    })
}

const COMMITTED: ReservationCommitResult = ReservationCommitResult {
    accepted: true,
    reason: "",
};

fn not_committed(reason: &'static str) -> ReservationCommitResult {
    ReservationCommitResult {
        accepted: false,
        reason,
    }
}

/// A record of `request_id` on `placement` with KV 8 under activation 10: workspace 10 * 2 = 20,
/// memory 8 + 20 = 28 (:227-228); path `path-1`, attempt 0, epoch 1, neither committed nor
/// released.
fn record(id: &str, request_id: &str, placement: &str, expires_at: f64) -> Reservation {
    Reservation {
        reservation_id: id.to_owned(),
        request_id: request_id.to_owned(),
        path_id: "path-1".to_owned(),
        path_attempt: 0,
        placement_id: json!(placement),
        deployment_epoch: json!(1),
        kv_bytes: int(8),
        workspace_bytes: int(20),
        memory_bytes: int(28),
        expires_at,
        committed: false,
        released: false,
    }
}

type Column = (PyInt, PyNumber, FreeBytes);

fn status(
    id: Value,
    node: &str,
    memory: Column,
    kv: Column,
    workspace: Column,
    active: u64,
    maximum: u64,
) -> PlacementStatus {
    PlacementStatus {
        placement_id: id,
        node_id: json!(node),
        memory_capacity_bytes: memory.0,
        reserved_memory_bytes: memory.1,
        free_memory_bytes: memory.2,
        kv_capacity_bytes: kv.0,
        reserved_kv_bytes: kv.1,
        free_kv_bytes: kv.2,
        workspace_capacity_bytes: workspace.0,
        reserved_workspace_bytes: workspace.1,
        free_workspace_bytes: workspace.2,
        active_reservations: active,
        maximum_reservations: maximum,
    }
}

/// An all-int column: capacity, reserved, free.
fn ints([capacity, reserved, free]: [u64; 3]) -> Column {
    (
        PyInt::from(capacity),
        int(reserved),
        FreeBytes::Int(PyInt::from(free)),
    )
}

fn row(
    id: Value,
    node: &str,
    memory: [u64; 3],
    kv: [u64; 3],
    workspace: [u64; 3],
    active: u64,
    maximum: u64,
) -> PlacementStatus {
    status(
        id,
        node,
        ints(memory),
        ints(kv),
        ints(workspace),
        active,
        maximum,
    )
}

const MISMATCH: LedgerError = LedgerError("m16_capacity_placement_mismatch");
const INVALID: LedgerError = LedgerError("m16_capacity_invalid");

#[test]
fn m16_ledger_capacity_validation() {
    let clock = Clock::default();
    let ids = Ids::default();
    let build = |g: &Graph, capacities: &[PlacementCapacity]| {
        ResourceLedger::new(g, capacities, &clock, &ids).err()
    };
    let two = graph(
        json!(10),
        &[(
            json!(8),
            &[
                (json!("p0"), json!("node-a")),
                (json!("p1"), json!("node-b")),
            ],
        )],
    );
    let p0 = cap(json!("p0"), 100, 60, 50, 8);
    let p1 = cap(json!("p1"), 100, 60, 50, 8);
    // :172-174 the capacity ids must be the graph's placement-id set: one missing, one extra, one
    // renamed id are each refused.
    assert_eq!(build(&two, std::slice::from_ref(&p0)), Some(MISMATCH));
    assert_eq!(
        build(
            &two,
            &[p0.clone(), p1.clone(), cap(json!("p2"), 100, 60, 50, 8)]
        ),
        Some(MISMATCH)
    );
    assert_eq!(
        build(&two, &[p0.clone(), cap(json!("P1"), 100, 60, 50, 8)]),
        Some(MISMATCH)
    );
    // The set is compared before any value is read (:172-174 precede :176-193).
    assert_eq!(
        build(&two, &[cap(json!("p0"), 0, 60, 50, 8)]),
        Some(MISMATCH)
    );
    // Set equality: the mapping's order does not matter.
    assert_eq!(build(&two, &[p1.clone(), p0.clone()]), None);
    // Ids are dict keys: Python-equal ids are one key (1 == 1.0 == True), "1" is not 1.
    let numeric = graph(
        json!(10),
        &[(
            json!(8),
            &[(json!(1), json!("node-a")), (json!(2.5), json!("node-b"))],
        )],
    );
    for key in [json!(1), json!(1.0), json!(true)] {
        assert_eq!(
            build(
                &numeric,
                &[cap(key, 100, 60, 50, 8), cap(json!(2.5), 100, 60, 50, 8)]
            ),
            None
        );
    }
    assert_eq!(
        build(
            &numeric,
            &[
                cap(json!("1"), 100, 60, 50, 8),
                cap(json!(2.5), 100, 60, 50, 8)
            ]
        ),
        Some(MISMATCH)
    );
    // :186-191 each of the four values must be an int above 0 ...
    for zeroed in [
        cap(json!("p0"), 0, 60, 50, 8),
        cap(json!("p0"), 100, 0, 50, 8),
        cap(json!("p0"), 100, 60, 0, 8),
        cap(json!("p0"), 100, 60, 50, 0),
    ] {
        assert_eq!(build(&two, &[zeroed, p1.clone()]), Some(INVALID));
    }
    // ... and an int: a float is refused even when it is integral (isinstance(100.0, int) is
    // False), in any of the three byte values.
    for float in [
        PlacementCapacity {
            memory_capacity_bytes: PyNumber::Float(100.0),
            ..p0.clone()
        },
        PlacementCapacity {
            kv_capacity_bytes: PyNumber::Float(60.0),
            ..p0.clone()
        },
        PlacementCapacity {
            workspace_capacity_bytes: PyNumber::Float(50.5),
            ..p0.clone()
        },
    ] {
        assert_eq!(build(&two, &[p1.clone(), float]), Some(INVALID));
    }
    // Ints are unbounded: 2**70 is a valid capacity.
    let huge = PlacementCapacity {
        memory_capacity_bytes: PyNumber::Int(digits("1180591620717411303424")),
        ..p0.clone()
    };
    assert_eq!(build(&two, &[huge, p1.clone()]), None);

    // The real factory path (:1252-1262): live_placement_capacities' int outputs are accepted.
    let defaults = one(json!(2));
    let rows = live_placement_capacities(&defaults, &ProjectedNodes::new()).expect("factory");
    assert_eq!(build(&defaults, &rows), None);
    // The factory passes a float through and the ledger refuses it (reviewer followup on
    // :183-193). Activation 600_000.0 with allocatable 2_097_152 on one placement: per_placement
    // max(1_048_576, 2_097_152 // 1) = 2_097_152; workspace max(max(1_200_000.0, 1),
    // 2_097_152 // 4) = 1_200_000.0 (a float); memory max(2_097_152, 1_200_000.0 + 1_048_576)
    // = 2_248_576.0.
    let float_graph = one(json!(600_000.0));
    let nodes: ProjectedNodes = [(
        "node-a".to_owned(),
        json!({"node_id": "node-a", "fast_allocatable_bytes": 2_097_152})
            .as_object()
            .expect("object")
            .clone(),
    )]
    .into();
    let floats = live_placement_capacities(&float_graph, &nodes).expect("factory");
    assert_eq!(
        floats[0].workspace_capacity_bytes,
        PyNumber::Float(1_200_000.0)
    );
    assert_eq!(
        floats[0].memory_capacity_bytes,
        PyNumber::Float(2_248_576.0)
    );
    assert_eq!(build(&float_graph, &floats), Some(INVALID));
}

#[test]
fn m16_ledger_reserve_accounting() {
    let clock = Clock::default();
    let ids = Ids::default();
    // Activation 10: each reservation's workspace is 10 * 2 = 20 (:227), its memory kv + 20.
    let g = one(json!(10));
    let mut ledger: Ledger =
        ResourceLedger::new(&g, &[cap(json!("p0"), 200, 60, 50, 8)], &clock, &ids).expect("valid");
    assert_eq!(
        ledger.reserve(&request("r1", int(30))),
        ok("reservation-1", json!(1), 100.0)
    );
    assert_eq!(
        ledger.placement_status(),
        Ok(vec![row(
            json!("p0"),
            "node-a",
            [200, 50, 150],
            [60, 30, 30],
            [50, 20, 30],
            1,
            8
        )])
    );
    // :231-232 KV alone over capacity: 30 + 31 = 61 > 60 (workspace 40 and memory 50 + 51 = 101
    // still fit).
    assert_eq!(
        ledger.reserve(&request("r2", int(31))),
        no("resource_unavailable")
    );
    // KV exactly at capacity is accepted (60 > 60 is False); the refusal drew no id (:239).
    assert_eq!(
        ledger.reserve(&request("r2", int(30))),
        ok("reservation-2", json!(1), 100.0)
    );
    // :233-234 workspace alone over capacity: 40 + 20 = 60 > 50 (KV 60 + 0 and memory 100 + 20
    // fit).
    assert_eq!(
        ledger.reserve(&request("r3", int(0))),
        no("resource_unavailable")
    );
    // :198-199 an id the capacities do not hold.
    assert_eq!(
        ledger.reserve(&on(json!("p9"), "r3", 0)),
        no("unknown_placement")
    );
    assert_eq!(
        ledger.placement_status(),
        Ok(vec![row(
            json!("p0"),
            "node-a",
            [200, 100, 100],
            [60, 60, 0],
            [50, 40, 10],
            2,
            8
        )])
    );

    // :235-236 memory alone over capacity: 50 + (0 + 20) = 70 > 50 with KV 30 <= 100 and
    // workspace 40 <= 100; memory exactly at capacity (30 + 20 = 50) is accepted.
    let memory_ids = Ids::default();
    let mut memory: Ledger = ResourceLedger::new(
        &g,
        &[cap(json!("p0"), 50, 100, 100, 8)],
        &clock,
        &memory_ids,
    )
    .expect("valid");
    assert_eq!(
        memory.reserve(&request("r1", int(30))),
        ok("reservation-1", json!(1), 100.0)
    );
    assert_eq!(
        memory.reserve(&request("r2", int(0))),
        no("resource_unavailable")
    );
    assert_eq!(
        memory.placement_status(),
        Ok(vec![row(
            json!("p0"),
            "node-a",
            [50, 50, 0],
            [100, 30, 70],
            [100, 20, 80],
            1,
            8
        )])
    );

    // A float activation size gives a float workspace, 1.5 * 2 = 3.0, and float memory,
    // 5 + 3.0 = 8.0 (:227-228). The sums keep those types: memory sum([8.0]) = 8.0 and
    // 100 - 8.0 = 92.0, KV 5 and 60 - 5 = 55, workspace 3.0 and 50 - 3.0 = 47.0 (:318-333).
    let float_graph = one(json!(1.5));
    let float_ids = Ids::default();
    let mut float: Ledger = ResourceLedger::new(
        &float_graph,
        &[cap(json!("p0"), 100, 60, 50, 8)],
        &clock,
        &float_ids,
    )
    .expect("valid");
    assert_eq!(
        float.reserve(&request("r1", int(5))),
        ok("reservation-1", json!(1), 100.0)
    );
    assert_eq!(
        float.request_reservations("r1"),
        vec![Reservation {
            kv_bytes: int(5),
            workspace_bytes: PyNumber::Float(3.0),
            memory_bytes: PyNumber::Float(8.0),
            ..record("reservation-1", "r1", "p0", 100.0)
        }]
    );
    assert_eq!(
        float.placement_status(),
        Ok(vec![status(
            json!("p0"),
            "node-a",
            (
                PyInt::from(100),
                PyNumber::Float(8.0),
                FreeBytes::Float(92.0)
            ),
            ints([60, 5, 55]),
            (
                PyInt::from(50),
                PyNumber::Float(3.0),
                FreeBytes::Float(47.0)
            ),
            1,
            8
        )])
    );
    // A bool activation size is an int: True * 2 = 2, memory 5 + 2 = 7.
    let bool_graph = one(json!(true));
    let bool_ids = Ids::default();
    let mut boolean: Ledger = ResourceLedger::new(
        &bool_graph,
        &[cap(json!("p0"), 100, 60, 50, 8)],
        &clock,
        &bool_ids,
    )
    .expect("valid");
    assert_eq!(
        boolean.reserve(&request("r1", int(5))),
        ok("reservation-1", json!(1), 100.0)
    );
    assert_eq!(
        boolean.placement_status(),
        Ok(vec![row(
            json!("p0"),
            "node-a",
            [100, 7, 93],
            [60, 5, 55],
            [50, 2, 48],
            1,
            8
        )])
    );
}

#[test]
fn m16_ledger_maximum_reservations() {
    let clock = Clock::default();
    let ids = Ids::default();
    let g = one(json!(10));
    let mut ledger: Ledger =
        ResourceLedger::new(&g, &[cap(json!("p0"), 1000, 1000, 1000, 2)], &clock, &ids)
            .expect("valid");
    assert_eq!(
        ledger.reserve(&request("r1", int(8))),
        ok("reservation-1", json!(1), 100.0)
    );
    assert_eq!(
        ledger.reserve(&request("r2", int(8))),
        ok("reservation-2", json!(1), 100.0)
    );
    // :230 len(active) >= maximum_reservations: the third is refused although bytes remain.
    assert_eq!(
        ledger.reserve(&request("r3", int(8))),
        no("resource_unavailable")
    );
    // Committed and expired records stay active until released (:340-345 filters on
    // `released` only).
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-1"]), &json!(1)),
        COMMITTED
    );
    clock.0.set(500.0);
    assert_eq!(
        ledger.reserve(&request("r3", int(8))),
        no("resource_unavailable")
    );
    assert_eq!(
        ledger.placement_status(),
        Ok(vec![row(
            json!("p0"),
            "node-a",
            [1000, 56, 944],
            [1000, 16, 984],
            [1000, 40, 960],
            2,
            2
        )])
    );
    // A release frees the slot. A new reservation is not checked against the clock: lease
    // 100.0 at now 500.0 is accepted as it is (:225-258 never read the clock).
    ledger.release(&ids_of(&["reservation-2"]));
    assert_eq!(
        ledger.reserve(&request("r3", int(8))),
        ok("reservation-3", json!(1), 100.0)
    );
    assert_eq!(
        ledger.reserve(&request("r4", int(8))),
        no("resource_unavailable")
    );
    assert_eq!(
        ledger.placement_status(),
        Ok(vec![row(
            json!("p0"),
            "node-a",
            [1000, 56, 944],
            [1000, 16, 984],
            [1000, 40, 960],
            2,
            2
        )])
    );
}

fn ids_of(list: &[&str]) -> Vec<String> {
    list.iter().map(|id| (*id).to_owned()).collect()
}

#[test]
fn m16_ledger_reserve_idempotent_and_conflict() {
    let clock = Clock::default();
    let ids = Ids::default();
    let g = one(json!(10));
    let mut ledger: Ledger =
        ResourceLedger::new(&g, &[cap(json!("p0"), 1000, 1000, 1000, 8)], &clock, &ids)
            .expect("valid");
    let first = request("r1", int(8));
    assert_eq!(ledger.reserve(&first), ok("reservation-1", json!(1), 100.0));
    // :200-224 a live record with the same request, path, attempt and placement is returned as
    // stored (its id, epoch and expiry), even for a new lease; no record is added.
    assert_eq!(
        ledger.reserve(&ReservationRequest {
            lease_expires_at: 200.0,
            ..first.clone()
        }),
        ok("reservation-1", json!(1), 100.0)
    );
    // Python `!=`: 8.0 == 8, 1.0 == 1 and True == 1, so these are the same reservation; the
    // stored epoch 1 is returned.
    for same in [
        ReservationRequest {
            kv_bytes: PyNumber::Float(8.0),
            ..first.clone()
        },
        ReservationRequest {
            deployment_epoch: json!(1.0),
            ..first.clone()
        },
        ReservationRequest {
            deployment_epoch: json!(true),
            ..first.clone()
        },
    ] {
        assert_eq!(ledger.reserve(&same), ok("reservation-1", json!(1), 100.0));
    }
    // :213-218 another epoch or KV size conflicts.
    for conflict in [
        ReservationRequest {
            deployment_epoch: json!(2),
            ..first.clone()
        },
        ReservationRequest {
            kv_bytes: int(9),
            ..first.clone()
        },
        ReservationRequest {
            kv_bytes: PyNumber::Float(8.5),
            ..first.clone()
        },
    ] {
        assert_eq!(ledger.reserve(&conflict), no("conflicting_reservation"));
    }
    assert_eq!(
        ledger.placement_status(),
        Ok(vec![row(
            json!("p0"),
            "node-a",
            [1000, 28, 972],
            [1000, 8, 992],
            [1000, 20, 980],
            1,
            8
        )])
    );
    // :204-208 attempt, path and request are all part of the match: each is new.
    assert_eq!(
        ledger.reserve(&ReservationRequest {
            path_attempt: 1,
            ..first.clone()
        }),
        ok("reservation-2", json!(1), 100.0)
    );
    assert_eq!(
        ledger.reserve(&ReservationRequest {
            path_id: "path-2".to_owned(),
            ..first.clone()
        }),
        ok("reservation-3", json!(1), 100.0)
    );
    assert_eq!(
        ledger.reserve(&request("r2", int(8))),
        ok("reservation-4", json!(1), 100.0)
    );
    // :216 expires_at <= now: at exactly 100.0 the stored record is no longer live and the
    // same request conflicts; one ulp earlier it is still returned.
    clock.0.set(100.0);
    assert_eq!(ledger.reserve(&first), no("conflicting_reservation"));
    clock.0.set(f64::from_bits(100.0_f64.to_bits() - 1));
    assert_eq!(ledger.reserve(&first), ok("reservation-1", json!(1), 100.0));
    // :204 a released record no longer matches: the same request is a new reservation.
    ledger.release(&ids_of(&["reservation-1"]));
    assert_eq!(ledger.reserve(&first), ok("reservation-5", json!(1), 100.0));
    assert_eq!(
        ledger.placement_status(),
        Ok(vec![row(
            json!("p0"),
            "node-a",
            [1000, 112, 888],
            [1000, 32, 968],
            [1000, 80, 920],
            4,
            8
        )])
    );
    // The match precedes the capacity check (:200-224 before :225-238): on a full placement the
    // stored reservation is still returned and a conflict still reported; only a new one is
    // refused for capacity.
    let full_ids = Ids::default();
    let mut full: Ledger = ResourceLedger::new(
        &g,
        &[cap(json!("p0"), 1000, 1000, 1000, 1)],
        &clock,
        &full_ids,
    )
    .expect("valid");
    assert_eq!(full.reserve(&first), ok("reservation-1", json!(1), 100.0));
    assert_eq!(full.reserve(&first), ok("reservation-1", json!(1), 100.0));
    assert_eq!(
        full.reserve(&ReservationRequest {
            kv_bytes: int(9),
            ..first.clone()
        }),
        no("conflicting_reservation")
    );
    assert_eq!(
        full.reserve(&ReservationRequest {
            path_attempt: 1,
            ..first.clone()
        }),
        no("resource_unavailable")
    );
}

#[test]
fn m16_ledger_commit_rules() {
    let clock = Clock::default();
    let ids = Ids::default();
    let g = one(json!(10));
    let mut ledger: Ledger =
        ResourceLedger::new(&g, &[cap(json!("p0"), 1000, 1000, 1000, 8)], &clock, &ids)
            .expect("valid");
    for (request_id, lease) in [("r1", 50.0), ("r2", 50.0), ("r3", 20.0)] {
        ledger
            .reserve(&ReservationRequest {
                lease_expires_at: lease,
                ..request(request_id, int(8))
            })
            .expect("reserve");
    }
    let one_epoch = json!(1);
    // :266-267 an empty or duplicated set is refused first, before any lookup.
    assert_eq!(
        ledger.commit(&[], &one_epoch),
        not_committed("reservation_set_invalid")
    );
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-1", "reservation-1"]), &one_epoch),
        not_committed("reservation_set_invalid")
    );
    assert_eq!(
        ledger.commit(&ids_of(&["missing", "missing"]), &one_epoch),
        not_committed("reservation_set_invalid")
    );
    // :268-270 an unknown id.
    assert_eq!(
        ledger.commit(&ids_of(&["missing"]), &one_epoch),
        not_committed("unknown_reservation")
    );
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-1", "missing"]), &one_epoch),
        not_committed("unknown_reservation")
    );
    // An unknown id is reported before a released one.
    ledger.release(&ids_of(&["reservation-2"]));
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-2", "missing"]), &one_epoch),
        not_committed("unknown_reservation")
    );
    // :271-278 released, another epoch, or expired is not live.
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-1", "reservation-2"]), &one_epoch),
        not_committed("reservation_not_live")
    );
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-1"]), &json!(2)),
        not_committed("reservation_not_live")
    );
    // Python `!=`: epoch 1.0 is the stored 1.
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-1"]), &json!(1.0)),
        COMMITTED
    );
    // :279-280 committed twice.
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-1"]), &one_epoch),
        not_committed("reservation_already_committed")
    );
    // expires_at 20.0 <= now 20.0 is not live; not-live is reported before already-committed.
    clock.0.set(20.0);
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-1", "reservation-3"]), &one_epoch),
        not_committed("reservation_not_live")
    );
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-3"]), &one_epoch),
        not_committed("reservation_not_live")
    );
    // :281-284 all or nothing: a refused set commits none of its members.
    for request_id in ["r4", "r5"] {
        ledger
            .reserve(&ReservationRequest {
                lease_expires_at: 50.0,
                ..request(request_id, int(8))
            })
            .expect("reserve");
    }
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-4", "reservation-1"]), &one_epoch),
        not_committed("reservation_already_committed")
    );
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-5", "reservation-2"]), &one_epoch),
        not_committed("reservation_not_live")
    );
    assert_eq!(
        ledger.request_reservations("r4"),
        vec![record("reservation-4", "r4", "p0", 50.0)]
    );
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-4", "reservation-5"]), &one_epoch),
        COMMITTED
    );
    assert_eq!(
        ledger.request_reservations("r5"),
        vec![Reservation {
            committed: true,
            ..record("reservation-5", "r5", "p0", 50.0)
        }]
    );
    assert_eq!(
        ledger.request_reservations("r1"),
        vec![Reservation {
            committed: true,
            ..record("reservation-1", "r1", "p0", 50.0)
        }]
    );
}

#[test]
fn m16_ledger_release_and_request_reservations() {
    let clock = Clock::default();
    let ids = Ids::default();
    let g = graph(
        json!(10),
        &[
            (json!(8), &[(json!("p0"), json!("node-a"))]),
            (json!(8), &[(json!("p1"), json!("node-b"))]),
        ],
    );
    let mut ledger: Ledger = ResourceLedger::new(
        &g,
        &[
            cap(json!("p0"), 1000, 1000, 1000, 8),
            cap(json!("p1"), 1000, 1000, 1000, 8),
        ],
        &clock,
        &ids,
    )
    .expect("valid");
    let second_attempt = ReservationRequest {
        path_attempt: 1,
        ..on(json!("p1"), "r1", 8)
    };
    for (r, id) in [
        (on(json!("p1"), "r1", 8), "reservation-1"),
        (on(json!("p0"), "r2", 8), "reservation-2"),
        (on(json!("p0"), "r1", 8), "reservation-3"),
        (second_attempt, "reservation-4"),
    ] {
        assert_eq!(ledger.reserve(&r), ok(id, json!(1), 100.0));
    }
    let attempt_one = Reservation {
        path_attempt: 1,
        ..record("reservation-4", "r1", "p1", 100.0)
    };
    // :301-306 the request's unreleased records in reservation order.
    assert_eq!(
        ledger.request_reservations("r1"),
        vec![
            record("reservation-1", "r1", "p1", 100.0),
            record("reservation-3", "r1", "p0", 100.0),
            attempt_one.clone(),
        ]
    );
    // Committed and expired records are still listed: only `released` filters.
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-3"]), &json!(1)),
        COMMITTED
    );
    clock.0.set(150.0);
    let committed = Reservation {
        committed: true,
        ..record("reservation-3", "r1", "p0", 100.0)
    };
    assert_eq!(
        ledger.request_reservations("r1"),
        vec![
            record("reservation-1", "r1", "p1", 100.0),
            committed.clone(),
            attempt_one.clone(),
        ]
    );
    // :286-290 release marks known ids and skips unknown ones; releasing again changes nothing.
    ledger.release(&ids_of(&["reservation-1", "missing"]));
    ledger.release(&ids_of(&["reservation-1"]));
    assert_eq!(
        ledger.request_reservations("r1"),
        vec![committed, attempt_one]
    );
    // :292-299 release_request releases every unreleased record of that request only.
    ledger.release_request("r9");
    ledger.release_request("r1");
    assert_eq!(ledger.request_reservations("r1"), Vec::new());
    assert_eq!(
        ledger.request_reservations("r2"),
        vec![record("reservation-2", "r2", "p0", 100.0)]
    );
    assert_eq!(
        ledger.placement_status(),
        Ok(vec![
            row(
                json!("p0"),
                "node-a",
                [1000, 28, 972],
                [1000, 8, 992],
                [1000, 20, 980],
                1,
                8
            ),
            row(
                json!("p1"),
                "node-b",
                [1000, 0, 1000],
                [1000, 0, 1000],
                [1000, 0, 1000],
                0,
                8
            ),
        ])
    );
    // A released request can reserve again: a new record, appended.
    assert_eq!(
        ledger.reserve(&on(json!("p1"), "r1", 8)),
        ok("reservation-5", json!(1), 100.0)
    );
    assert_eq!(
        ledger.request_reservations("r1"),
        vec![record("reservation-5", "r1", "p1", 100.0)]
    );
}

#[test]
fn m16_ledger_placement_status() {
    let clock = Clock::default();
    let ids = Ids::default();
    let g = graph(
        json!(10),
        &[
            (
                json!(8),
                &[
                    (json!("p-b"), json!("node-b")),
                    (json!("p-a"), json!("node-a")),
                ],
            ),
            (json!(8), &[(json!("p-c"), json!("node-c"))]),
        ],
    );
    let mut ledger: Ledger = ResourceLedger::new(
        &g,
        &[
            cap(json!("p-c"), 300, 30, 30, 3),
            cap(json!("p-b"), 200, 120, 120, 2),
            cap(json!("p-a"), 100, 60, 50, 1),
        ],
        &clock,
        &ids,
    )
    .expect("valid");
    // :315 sorted by placement id, whatever the graph or mapping order; each with its node.
    assert_eq!(
        ledger.placement_status(),
        Ok(vec![
            row(
                json!("p-a"),
                "node-a",
                [100, 0, 100],
                [60, 0, 60],
                [50, 0, 50],
                0,
                1
            ),
            row(
                json!("p-b"),
                "node-b",
                [200, 0, 200],
                [120, 0, 120],
                [120, 0, 120],
                0,
                2
            ),
            row(
                json!("p-c"),
                "node-c",
                [300, 0, 300],
                [30, 0, 30],
                [30, 0, 30],
                0,
                3
            ),
        ])
    );
    // p-b: KV 5 (memory 25) committed and KV 0 (memory 20); p-a: KV 1 (memory 21).
    assert_eq!(
        ledger.reserve(&on(json!("p-b"), "r1", 5)),
        ok("reservation-1", json!(1), 100.0)
    );
    assert_eq!(
        ledger.commit(&ids_of(&["reservation-1"]), &json!(1)),
        COMMITTED
    );
    assert_eq!(
        ledger.reserve(&on(json!("p-b"), "r2", 0)),
        ok("reservation-2", json!(1), 100.0)
    );
    assert_eq!(
        ledger.reserve(&on(json!("p-a"), "r3", 1)),
        ok("reservation-3", json!(1), 100.0)
    );
    let p_c = row(
        json!("p-c"),
        "node-c",
        [300, 0, 300],
        [30, 0, 30],
        [30, 0, 30],
        0,
        3,
    );
    assert_eq!(
        ledger.placement_status(),
        Ok(vec![
            row(
                json!("p-a"),
                "node-a",
                [100, 21, 79],
                [60, 1, 59],
                [50, 20, 30],
                1,
                1
            ),
            row(
                json!("p-b"),
                "node-b",
                [200, 45, 155],
                [120, 5, 115],
                [120, 40, 80],
                2,
                2
            ),
            p_c.clone(),
        ])
    );
    // Released records leave the sums; committed ones stay.
    ledger.release_request("r2");
    assert_eq!(
        ledger.placement_status(),
        Ok(vec![
            row(
                json!("p-a"),
                "node-a",
                [100, 21, 79],
                [60, 1, 59],
                [50, 20, 30],
                1,
                1
            ),
            row(
                json!("p-b"),
                "node-b",
                [200, 25, 175],
                [120, 5, 115],
                [120, 20, 100],
                1,
                2
            ),
            p_c,
        ])
    );

    // Numeric ids sort by Python `<` (-1 < True < 2.5 < 10). A request for 1 reserves on the
    // `True` key; the status names the mapping's key, the record keeps the request's 1.
    let numeric = graph(
        json!(10),
        &[(
            json!(8),
            &[
                (json!(10), json!("node-10")),
                (json!(-1), json!("node-m1")),
                (json!(2.5), json!("node-2p5")),
                (json!(true), json!("node-true")),
            ],
        )],
    );
    let numeric_ids = Ids::default();
    let mut numbers: Ledger = ResourceLedger::new(
        &numeric,
        &[
            cap(json!(10), 100, 60, 50, 8),
            cap(json!(-1), 100, 60, 50, 8),
            cap(json!(2.5), 100, 60, 50, 8),
            cap(json!(true), 100, 60, 50, 8),
        ],
        &clock,
        &numeric_ids,
    )
    .expect("valid");
    assert_eq!(
        numbers.reserve(&on(json!(1), "r1", 8)),
        ok("reservation-1", json!(1), 100.0)
    );
    let empty =
        |id: Value, node: &str| row(id, node, [100, 0, 100], [60, 0, 60], [50, 0, 50], 0, 8);
    assert_eq!(
        numbers.placement_status(),
        Ok(vec![
            empty(json!(-1), "node-m1"),
            row(
                json!(true),
                "node-true",
                [100, 28, 72],
                [60, 8, 52],
                [50, 20, 30],
                1,
                8
            ),
            empty(json!(2.5), "node-2p5"),
            empty(json!(10), "node-10"),
        ])
    );
    assert_eq!(
        numbers.request_reservations("r1")[..]
            .iter()
            .map(|r| r.placement_id.clone())
            .collect::<Vec<_>>(),
        vec![json!(1)]
    );
    // :315 `sorted` raises TypeError for a string next to a number.
    let mixed = graph(
        json!(10),
        &[(
            json!(8),
            &[(json!("p0"), json!("node-a")), (json!(7), json!("node-b"))],
        )],
    );
    let mixed_ids = Ids::default();
    let mixed_ledger: Ledger = ResourceLedger::new(
        &mixed,
        &[
            cap(json!("p0"), 100, 60, 50, 8),
            cap(json!(7), 100, 60, 50, 8),
        ],
        &clock,
        &mixed_ids,
    )
    .expect("valid");
    assert_eq!(
        mixed_ledger.placement_status(),
        Err(LedgerError("m16_ledger_unorderable_placement_ids"))
    );
    // One id needs no comparison: a lone null id is reported.
    let null = graph(json!(10), &[(json!(8), &[(json!(null), json!("node-a"))])]);
    let null_ids = Ids::default();
    let null_ledger: Ledger = ResourceLedger::new(
        &null,
        &[cap(json!(null), 100, 60, 50, 8)],
        &clock,
        &null_ids,
    )
    .expect("valid");
    assert_eq!(
        null_ledger.placement_status(),
        Ok(vec![empty(json!(null), "node-a")])
    );
    // Unbounded ints: capacities 2**70 / 2**70 / 2**69 with KV 2**69 reserved:
    // memory 2**70 - (2**69 + 20), KV 2**70 - 2**69, workspace 2**69 - 20.
    let huge_ids = Ids::default();
    let two_70 = digits("1180591620717411303424");
    let two_69 = digits("590295810358705651712");
    let mut huge: Ledger = ResourceLedger::new(
        &one(json!(10)),
        &[PlacementCapacity {
            placement_id: json!("p0"),
            memory_capacity_bytes: PyNumber::Int(two_70.clone()),
            kv_capacity_bytes: PyNumber::Int(two_70.clone()),
            workspace_capacity_bytes: PyNumber::Int(two_69.clone()),
            maximum_reservations: 64,
        }],
        &clock,
        &huge_ids,
    )
    .expect("valid");
    assert_eq!(
        huge.reserve(&request("r1", PyNumber::Int(two_69.clone()))),
        ok("reservation-1", json!(1), 100.0)
    );
    assert_eq!(
        huge.placement_status(),
        Ok(vec![status(
            json!("p0"),
            "node-a",
            (
                two_70.clone(),
                PyNumber::Int(digits("590295810358705651732")),
                FreeBytes::Int(digits("590295810358705651692"))
            ),
            (
                two_70,
                PyNumber::Int(two_69.clone()),
                FreeBytes::Int(two_69.clone())
            ),
            (
                two_69,
                int(20),
                FreeBytes::Int(digits("590295810358705651692"))
            ),
            1,
            64
        )])
    );
}

// Paired oracle probes. Every case below was executed through the real v0.1.4 ledger at
// f4e366e9 (graph parser, `build_live_m16_runtime` capacities for the factory cases, and
// `_ResourceLedgerCapacityPort` driven with a settable clock and a counting id source) by the
// probe named in the fixture's provenance (CPython 3.14.2, the oracle's CI interpreter). The
// fixture keeps each value's Python type: {"int": str(n)} or {"float": repr(x)}.
const ORACLE: &str = include_str!("fixtures/m16-ledger-oracle.json");

fn tag_number(tag: &Value) -> PyNumber {
    match (tag["int"].as_str(), tag["float"].as_str()) {
        (Some(text), None) => PyNumber::Int(digits(text)),
        (None, Some(text)) => PyNumber::Float(text.parse().expect("float repr")),
        _ => panic!("not a number tag: {tag}"),
    }
}

fn tag_float(tag: &Value) -> f64 {
    tag["float"]
        .as_str()
        .expect("float tag")
        .parse()
        .expect("float repr")
}

fn number_doc(n: &PyNumber) -> Value {
    match n {
        PyNumber::Int(n) => json!({"int": n.to_string()}),
        PyNumber::Float(x) => json!({"float": format!("{x:?}")}),
    }
}

fn free_doc(free: &FreeBytes) -> Value {
    match free {
        FreeBytes::Int(n) => json!({"int": n.to_string()}),
        FreeBytes::NegativeInt(n) => json!({"int": format!("-{n}")}),
        FreeBytes::Float(x) => json!({"float": format!("{x:?}")}),
    }
}

/// The oracle's record with each float tag re-printed by the same formatter as the port's
/// output, so equal text means equal bits.
fn normalized(expected: &Value) -> Value {
    match expected {
        Value::Object(map) if map.len() == 1 && map.get("float").is_some_and(Value::is_string) => {
            json!({"float": format!("{:?}", tag_float(expected))})
        }
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(key, value)| (key.clone(), normalized(value)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.iter().map(normalized).collect()),
        other => other.clone(),
    }
}

/// The port's code for the exception the oracle raised in `op`.
fn expected(op: &str, result: &Value) -> Value {
    let Some(exception) = result.get("exception").and_then(Value::as_str) else {
        return normalized(result);
    };
    let code = match (exception, op) {
        ("ValueError", _) => result["message"].as_str().expect("message"),
        ("OverflowError", _) => "m16_ledger_int_too_large_for_float",
        ("TypeError", "reserve") => "m16_ledger_unhashable_placement_id",
        ("TypeError", "placement_status") => "m16_ledger_unorderable_placement_ids",
        other => panic!("unmapped oracle exception {other:?}"),
    };
    json!({"code": code})
}

fn strings(list: &Value) -> Vec<String> {
    list.as_array()
        .expect("id list")
        .iter()
        .map(|id| id.as_str().expect("string id").to_owned())
        .collect()
}

fn reservation_doc(r: &Reservation) -> Value {
    json!({
        "reservation_id": r.reservation_id,
        "request_id": r.request_id,
        "path_id": r.path_id,
        "path_attempt": r.path_attempt,
        "placement_id": r.placement_id,
        "deployment_epoch": r.deployment_epoch,
        "kv_bytes": number_doc(&r.kv_bytes),
        "workspace_bytes": number_doc(&r.workspace_bytes),
        "memory_bytes": number_doc(&r.memory_bytes),
        "expires_at": json!({"float": format!("{:?}", r.expires_at)}),
        "committed": r.committed,
        "released": r.released,
    })
}

fn status_doc(s: &PlacementStatus) -> Value {
    json!({
        "placement_id": s.placement_id,
        "node_id": s.node_id,
        "memory_capacity_bytes": {"int": s.memory_capacity_bytes.to_string()},
        "reserved_memory_bytes": number_doc(&s.reserved_memory_bytes),
        "free_memory_bytes": free_doc(&s.free_memory_bytes),
        "kv_capacity_bytes": {"int": s.kv_capacity_bytes.to_string()},
        "reserved_kv_bytes": number_doc(&s.reserved_kv_bytes),
        "free_kv_bytes": free_doc(&s.free_kv_bytes),
        "workspace_capacity_bytes": {"int": s.workspace_capacity_bytes.to_string()},
        "reserved_workspace_bytes": number_doc(&s.reserved_workspace_bytes),
        "free_workspace_bytes": free_doc(&s.free_workspace_bytes),
        "active_reservations": s.active_reservations,
        "maximum_reservations": s.maximum_reservations,
    })
}

fn capacity_doc(c: &PlacementCapacity) -> Value {
    json!([
        c.placement_id,
        number_doc(&c.memory_capacity_bytes),
        number_doc(&c.kv_capacity_bytes),
        number_doc(&c.workspace_capacity_bytes),
        c.maximum_reservations,
    ])
}

fn run(ledger: &mut Ledger, clock: &Clock, op: &Value) -> Value {
    let code = |error: LedgerError| json!({"code": error.0});
    match op["op"].as_str().expect("op name") {
        "now" => {
            clock.0.set(tag_float(&op["at"]));
            Value::Null
        }
        "reserve" => {
            let r = &op["request"];
            let request = ReservationRequest {
                request_id: r["request_id"].as_str().expect("request id").to_owned(),
                path_id: r["path_id"].as_str().expect("path id").to_owned(),
                path_attempt: r["path_attempt"].as_u64().expect("attempt"),
                placement_id: r["placement_id"].clone(),
                kv_bytes: tag_number(&r["kv_bytes"]),
                deployment_epoch: r["deployment_epoch"].clone(),
                lease_expires_at: tag_float(&r["lease_expires_at"]),
            };
            match ledger.reserve(&request) {
                Ok(result) => json!({
                    "accepted": result.accepted,
                    "reservation_id": result.reservation_id,
                    "reason": result.reason,
                    "deployment_epoch": result.deployment_epoch,
                    "expires_at": {"float": format!("{:?}", result.expires_at)},
                }),
                Err(error) => code(error),
            }
        }
        "commit" => {
            let result = ledger.commit(&strings(&op["ids"]), &op["deployment_epoch"]);
            json!({"accepted": result.accepted, "reason": result.reason})
        }
        "release" => {
            ledger.release(&strings(&op["ids"]));
            Value::Null
        }
        "release_request" => {
            ledger.release_request(op["request_id"].as_str().expect("request id"));
            Value::Null
        }
        "request_reservations" => Value::Array(
            ledger
                .request_reservations(op["request_id"].as_str().expect("request id"))
                .iter()
                .map(reservation_doc)
                .collect(),
        ),
        "placement_status" => match ledger.placement_status() {
            Ok(rows) => Value::Array(rows.iter().map(status_doc).collect()),
            Err(error) => code(error),
        },
        other => panic!("unknown op {other}"),
    }
}

fn capacities(case: &Value, graph: &Graph) -> Vec<PlacementCapacity> {
    let spec = &case["capacities"];
    if let Some(rows) = spec["rows"].as_array() {
        return rows
            .iter()
            .map(|row| PlacementCapacity {
                placement_id: row[0].clone(),
                memory_capacity_bytes: tag_number(&row[1]),
                kv_capacity_bytes: tag_number(&row[2]),
                workspace_capacity_bytes: tag_number(&row[3]),
                maximum_reservations: row[4].as_u64().expect("maximum"),
            })
            .collect();
    }
    let nodes: ProjectedNodes = spec["factory"]["nodes"]
        .as_array()
        .expect("projected nodes")
        .iter()
        .map(|node| {
            (
                node["node_id"].as_str().expect("node id").to_owned(),
                node.as_object().expect("node").clone(),
            )
        })
        .collect();
    let rows = live_placement_capacities(graph, &nodes).expect("factory capacities");
    // The factory hands the ledger what the oracle's factory handed its ledger (:1252).
    assert_eq!(
        Value::Array(rows.iter().map(capacity_doc).collect()),
        normalized(&case["factory_capacities"]),
        "{}: factory capacities",
        case["name"]
    );
    rows
}

#[test]
fn m16_ledger_python_value_domain_parity() {
    let fixture: Value = serde_json::from_str(ORACLE).expect("fixture JSON");
    let provenance = &fixture["provenance"];
    assert_eq!(
        provenance["oracle_commit"],
        json!("f4e366e98a1f705dfaf182897044500d290dbd7f")
    );
    assert_eq!(provenance["interpreter"], json!("CPython 3.14.2"));
    let cases = fixture["cases"].as_array().expect("cases");
    assert!(cases.len() >= 20, "{} paired probes", cases.len());
    for case in cases {
        let name = case["name"].as_str().expect("case name");
        let graph = parse(&case["graph"]);
        let rows = capacities(case, &graph);
        let clock = Clock::default();
        let ids = Ids::default();
        if let Some(scripted) = case["ids"].as_array() {
            *ids.scripted.borrow_mut() = Some(strings(&Value::Array(scripted.clone())).into());
        }
        let built = ResourceLedger::new(&graph, &rows, &clock, &ids);
        let construct = match &built {
            Ok(_) => Value::Null,
            Err(error) => json!({"code": error.0}),
        };
        assert_eq!(
            construct,
            expected("construct", &case["construct"]),
            "{name}: construct"
        );
        let ops = case["ops"].as_array().expect("ops");
        let results = case["results"].as_array().expect("results");
        let Ok(mut ledger) = built else {
            assert_eq!(
                results.len(),
                0,
                "{name}: no ops after a refused construction"
            );
            continue;
        };
        assert_eq!(ops.len(), results.len(), "{name}: one result per op");
        for (index, (op, result)) in ops.iter().zip(results).enumerate() {
            let kind = op["op"].as_str().expect("op name");
            assert_eq!(
                run(&mut ledger, &clock, op),
                expected(kind, result),
                "{name}: op {index} {op}"
            );
        }
    }
}
