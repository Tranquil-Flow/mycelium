// SPDX-License-Identifier: AGPL-3.0-only
//! M16 hop scheduler (CL-L1-07): source parity for `HopScheduler`, `DuplicateHopError` and
//! `BackpressureError` (mycelium_router/scheduler.py:16-302 @ f4e366e9), the contracts
//! `HopWorkItem`, `RuntimeBatchKey`, `RuntimeBatch` and `BatchDecision` (contracts.py:244-262,
//! 285-333) and `hop_identity` (idempotency.py:98-111), used as the M16 coordinator uses them
//! (mycelium_m16_runtime.py:394, admit :444-480, next_dispatch :500-506, cancel :587). The first
//! seven tests' expected values are derived by hand from the oracle lines cited beside them;
//! none is computed by the scheduler. The last test replays paired probes recorded from the
//! real oracle (fixtures/m16-hop-scheduler-oracle.json).
use protocol::execution_graph::{self, Graph};
use runtime::m16_capacity::{PlacementCapacity, PyNumber};
use runtime::m16_hop_scheduler::{
    BYTE_LIMIT, EMPTY_HOP_QUEUE, HopScheduler, INT_TOO_LARGE_FOR_FLOAT, NAN_ENQUEUED_AT,
    SLICE_INDEX_NOT_INT, SchedulerError, UNHASHABLE_PLACEMENT_ID, UNORDERED_BATCH_PRIORITY,
    UNSUPPORTED_PAYLOAD, WORK_LIMIT,
};
use runtime::m16_ledger::{IdSource, LeaseClock, ResourceLedger};
use runtime::m16_path_builder::{PathBuilderError, ProgressivePathBuilder, RoutePolicy};
use runtime::m16_route_scorer::RouteScorer;
use runtime::m16_router_contracts::{
    BatchDecision, DeviceState, HopPayload, HopWorkItem, IdMap, IdSet, PathManifest, PyJson,
    PyReal, PyStr, RequestContext, RouterConfig, RouterContractError, RuntimeBatch,
    RuntimeBatchKey, hop_identity,
};
use serde_json::{Value, json};
use std::cell::Cell;
use std::collections::BTreeSet;

const INF: f64 = f64::INFINITY;
const NAN: f64 = f64::NAN;

fn f(x: f64) -> PyReal {
    PyReal::Float(x)
}

fn i(n: i64) -> PyReal {
    PyReal::int(n)
}

/// A JSON literal as the carrier holds it (Python's `json.loads` of its text).
fn pj(v: Value) -> PyJson {
    PyJson::from_json(&v).expect("a JSON value")
}

/// `10**exponent` as a Python int.
fn big(exponent: usize) -> PyReal {
    PyReal::signed(
        false,
        format!("1{}", "0".repeat(exponent))
            .parse()
            .expect("decimal digits"),
    )
}

/// `RouterConfig()` (contracts.py:141-179).
fn config() -> RouterConfig {
    RouterConfig::oracle_default().expect("oracle defaults")
}

fn scheduler(config: RouterConfig) -> HopScheduler {
    HopScheduler::new(config).expect("the scheduler's budget rows")
}

/// A DECODE hop of `request` on `path`, attempt 1, token 0, hop 0, on `placement-a`,
/// interactive, deficit 0.0, enqueued at 0.0, its idempotency key the joined identity
/// (idempotency.py:114-126), no payload; every other field its default.
fn work(request: &str, path: &str) -> HopWorkItem {
    HopWorkItem::new(
        request,
        path,
        i(1),
        "DECODE",
        i(0),
        i(0),
        pj(json!("placement-a")),
        "interactive",
        0.0,
        0.0,
        &format!("{request}:{path}:1:DECODE:0:0"),
        HopPayload::None,
    )
}

fn at(mut item: HopWorkItem, enqueued_at: f64) -> HopWorkItem {
    item.enqueued_at = enqueued_at;
    item
}

fn tokens(n: i64) -> HopPayload {
    HopPayload::Tuple((0..n).map(i).collect())
}

fn with_payload(mut item: HopWorkItem, payload: HopPayload) -> HopWorkItem {
    item.payload = payload;
    item
}

fn keyed(mut item: HopWorkItem, key: &RuntimeBatchKey) -> HopWorkItem {
    item.batch_key = Some(key.clone());
    item
}

fn batch_key(placement: Value) -> RuntimeBatchKey {
    RuntimeBatchKey::new(
        pj(json!("deployment-m16")),
        pj(json!(1)),
        pj(json!("commit-m16")),
        pj(json!("digest-m16")),
        pj(placement),
        pj(json!("assignment-0")),
        pj(json!("signature-0")),
        pj(json!("proof-0")),
        pj(json!("numpy")),
        "DECODE",
        pj(json!(8)),
        pj(json!(2)),
        i(1),
    )
}

fn duplicate(key: &str) -> Result<(), SchedulerError> {
    Err(SchedulerError::Duplicate {
        idempotency_key: key.to_owned(),
    })
}

fn backpressure(reason: &'static str, retry: PyReal) -> Result<(), SchedulerError> {
    Err(SchedulerError::Backpressure {
        reason,
        retry_after_seconds: retry,
    })
}

/// `(queue_depth, queued_payload_bytes, seen_key_count)`.
fn counts(s: &HopScheduler) -> (usize, u64, usize) {
    (
        s.queue_depth(),
        s.queued_payload_bytes(),
        s.seen_key_count(),
    )
}

fn keys(items: &[HopWorkItem]) -> Vec<&str> {
    items
        .iter()
        .map(|item| item.idempotency_key.as_str())
        .collect()
}

/// `pop_next` as the popped item's idempotency key.
fn popped(s: &mut HopScheduler, now: f64) -> Result<String, SchedulerError> {
    s.pop_next(now).map(|item| item.idempotency_key)
}

/// `pop_batch` as (the batch key's placement, the items' idempotency keys, the decision).
type BatchView = (Option<PyJson>, Vec<String>, Option<BatchDecision>);

fn batch_view(batch: Result<RuntimeBatch, SchedulerError>) -> Result<BatchView, SchedulerError> {
    batch.map(|b| {
        (
            b.compatibility_key().map(|key| key.placement_id.clone()),
            b.items()
                .iter()
                .map(|item| item.idempotency_key.clone())
                .collect(),
            b.decision().cloned(),
        )
    })
}

fn view(placement: Option<&str>, items: &[&str], decision: Option<BatchDecision>) -> BatchView {
    (
        placement.map(|p| pj(json!(p))),
        items.iter().map(|key| (*key).to_owned()).collect(),
        decision,
    )
}

fn decision() -> BatchDecision {
    BatchDecision {
        action: "dispatch".to_owned(),
        phase: "DECODE".to_owned(),
        batch_size: 2,
        target_items: 2,
        available_items: 3,
        predicted_payload_bytes: 64,
        predicted_transfer_ms: 0.125,
        ready_at: 12.5,
        reason: "target_reached".to_owned(),
    }
}

fn bits(r: Result<f64, SchedulerError>) -> Result<u64, SchedulerError> {
    r.map(f64::to_bits)
}

fn depths(s: &HopScheduler) -> Result<Vec<(Value, u64)>, SchedulerError> {
    s.placement_depths().map(|map| {
        map.entries()
            .iter()
            .map(|(id, n)| (value_doc(id, Spelling::Plain), *n))
            .collect()
    })
}

fn path_ids(ids: &[&str]) -> BTreeSet<String> {
    ids.iter().map(|id| (*id).to_owned()).collect()
}

#[test]
fn m16_hop_scheduler_contracts() {
    // contracts.py:298-318: twelve required fields; the defaults prefill_chunk_token_count 0,
    // batch_key None, deadline_at inf, position 0, emits_token False, terminal False and
    // lease_expires_at inf.
    let item = HopWorkItem::new(
        "request-1",
        "path-1",
        i(2),
        "REQUEST",
        i(-1),
        i(0),
        pj(json!("p0")),
        "batch",
        0.25,
        7.5,
        "m16:request-1:path-1",
        tokens(3),
    );
    assert_eq!(
        (
            item.request_id.as_str(),
            item.path_id.as_str(),
            &item.path_attempt,
            item.phase.as_str(),
            &item.token_index,
            &item.hop_index,
            &item.placement_id,
            item.qos_class.as_str(),
            item.deficit_ratio.to_bits(),
            item.enqueued_at.to_bits(),
            item.idempotency_key.as_str(),
            &item.payload,
        ),
        (
            "request-1",
            "path-1",
            &i(2),
            "REQUEST",
            &i(-1),
            &i(0),
            &pj(json!("p0")),
            "batch",
            0.25f64.to_bits(),
            7.5f64.to_bits(),
            "m16:request-1:path-1",
            &HopPayload::Tuple(vec![i(0), i(1), i(2)]),
        )
    );
    assert_eq!(
        (
            &item.prefill_chunk_token_count,
            item.batch_key.is_none(),
            item.deadline_at.to_bits(),
            &item.position,
            item.emits_token,
            item.terminal,
            &item.lease_expires_at,
        ),
        (&i(0), true, INF.to_bits(), &i(0), false, false, &f(INF))
    );

    // contracts.py:259-260: speculative_role "NONE", speculative_width 0.
    let key = batch_key(json!("placement-a"));
    assert_eq!(
        (key.speculative_role.as_str(), key.speculative_width),
        ("NONE", 0)
    );
    assert_eq!(
        (key.phase.as_str(), &key.token_span, &key.hidden_size),
        ("DECODE", &i(1), &pj(json!(8)))
    );
    // The dataclass `==` compares field by field with Python `==`: 1 == 1.0 == True, '1' != 1.
    let epoch = |e: Value| RuntimeBatchKey {
        deployment_epoch: pj(e),
        ..key.clone()
    };
    assert_eq!(epoch(json!(1.0)), key);
    assert_eq!(epoch(json!(true)), key);
    assert_ne!(batch_key(json!("1")), batch_key(json!(1)));
    assert_eq!(batch_key(json!(1)), batch_key(json!(1.0)));
    assert_ne!(
        RuntimeBatchKey {
            speculative_role: "TARGET_VERIFY".to_owned(),
            ..key.clone()
        },
        key
    );

    // contracts.py:327-333 RuntimeBatch.__post_init__.
    let other = batch_key(json!("placement-b"));
    let a = work("a", "p");
    let b = work("b", "p");
    let refusal = |code| Err(RouterContractError(code));
    assert_eq!(
        RuntimeBatch::new(None, vec![], None),
        refusal("empty_runtime_batch")
    );
    assert_eq!(
        RuntimeBatch::new(Some(key.clone()), vec![], Some(decision())),
        refusal("empty_runtime_batch")
    );
    // A batch without a key holds exactly one item (:332-333).
    assert_eq!(
        RuntimeBatch::new(None, vec![a.clone(), b.clone()], None),
        refusal("incompatible_runtime_batch")
    );
    // Every item's batch key is the batch's (:330-331).
    assert_eq!(
        RuntimeBatch::new(
            Some(key.clone()),
            vec![keyed(a.clone(), &key), keyed(b.clone(), &other)],
            None
        ),
        refusal("incompatible_runtime_batch")
    );
    assert_eq!(
        RuntimeBatch::new(Some(key.clone()), vec![a.clone()], None),
        refusal("incompatible_runtime_batch")
    );
    assert_eq!(
        RuntimeBatch::new(None, vec![keyed(a.clone(), &key)], None),
        refusal("incompatible_runtime_batch")
    );
    // The contract itself allows one request twice, and a Python-equal key; the decision is
    // carried as given.
    let pair = RuntimeBatch::new(
        Some(key.clone()),
        vec![keyed(a.clone(), &key), keyed(a.clone(), &key)],
        Some(decision()),
    )
    .map(|batch| {
        (
            batch.compatibility_key().cloned(),
            batch.items().to_vec(),
            batch.decision().cloned(),
        )
    });
    assert_eq!(
        pair,
        Ok((
            Some(key.clone()),
            vec![keyed(a.clone(), &key), keyed(a.clone(), &key)],
            Some(decision())
        ))
    );
    let hidden_float = RuntimeBatchKey {
        hidden_size: pj(json!(8.0)),
        ..key.clone()
    };
    assert!(RuntimeBatch::new(Some(hidden_float), vec![keyed(a.clone(), &key)], None).is_ok());
    assert_eq!(
        RuntimeBatch::new(None, vec![a.clone()], None).map(RuntimeBatch::into_items),
        Ok(vec![a.clone()])
    );
    // BatchDecision fields (contracts.py:285-295) keep their values, floats bit for bit.
    let d = decision();
    assert_eq!(
        (
            d.action.as_str(),
            d.phase.as_str(),
            d.batch_size,
            d.target_items,
            d.available_items,
            d.predicted_payload_bytes,
            d.predicted_transfer_ms.to_bits(),
            d.ready_at.to_bits(),
            d.reason.as_str()
        ),
        (
            "dispatch",
            "DECODE",
            2,
            2,
            3,
            64,
            0.125f64.to_bits(),
            12.5f64.to_bits(),
            "target_reached"
        )
    );

    // idempotency.py:98-111: the tuple, not the joined key. ("a:b", "c") and ("a", "b:c") join to
    // the same "a:b:c:1:DECODE:0:0" but are two identities.
    let left = work("a:b", "c");
    let right = work("a", "b:c");
    assert_eq!(left.idempotency_key, right.idempotency_key);
    assert_eq!(
        hop_identity(&left),
        (
            "a:b".to_owned(),
            "c".to_owned(),
            i(1),
            "DECODE".to_owned(),
            i(0),
            i(0)
        )
    );
    assert_eq!(
        hop_identity(&right),
        (
            "a".to_owned(),
            "b:c".to_owned(),
            i(1),
            "DECODE".to_owned(),
            i(0),
            i(0)
        )
    );
    assert_eq!(
        hop_identity(&item),
        (
            "request-1".to_owned(),
            "path-1".to_owned(),
            i(2),
            "REQUEST".to_owned(),
            i(-1),
            i(0)
        )
    );
}

#[test]
fn m16_hop_scheduler_enqueue_backpressure() {
    // Duplicate by hop identity (:50-51): the new item's idempotency key is reported, and a
    // different identity with the same key string is accepted.
    {
        let mut s = scheduler(config());
        let first = HopWorkItem {
            idempotency_key: "k1".to_owned(),
            ..work("a", "p")
        };
        assert_eq!(s.enqueue(first.clone()), Ok(()));
        let same_identity = HopWorkItem {
            idempotency_key: "k2".to_owned(),
            payload: tokens(2),
            ..first.clone()
        };
        assert_eq!(s.enqueue(same_identity), duplicate("k2"));
        let same_key = HopWorkItem {
            idempotency_key: "k1".to_owned(),
            ..work("b", "p")
        };
        assert_eq!(s.enqueue(same_key), Ok(()));
        // ids containing ':' share a joined key, not an identity.
        assert_eq!(s.enqueue(work("x:y", "z")), Ok(()));
        assert_eq!(s.enqueue(work("x", "y:z")), Ok(()));
        assert_eq!(counts(&s), (4, 0, 4));
    }
    // work_limit at len(queue) >= max(0, maximum_pending_hops) (:53-54), with the default retry
    // max(0.001, 0.01) = 0.01 (:52, contracts.py:161). Nothing is recorded on a refusal.
    {
        let mut c = config();
        c.maximum_pending_hops = i(2);
        let mut s = scheduler(c);
        assert_eq!(s.enqueue(with_payload(work("a", "p"), tokens(1))), Ok(()));
        assert_eq!(s.enqueue(with_payload(work("b", "p"), tokens(1))), Ok(()));
        assert_eq!(
            s.enqueue(with_payload(work("c", "p"), tokens(1))),
            backpressure(WORK_LIMIT, f(0.01))
        );
        assert_eq!(counts(&s), (2, 8, 2));
        // The refused identity was not recorded: once a slot frees it is accepted.
        assert_eq!(popped(&mut s, 0.0), Ok("a:p:1:DECODE:0:0".to_owned()));
        assert_eq!(s.enqueue(with_payload(work("c", "p"), tokens(1))), Ok(()));
        assert_eq!(counts(&s), (2, 8, 3));
    }
    // max(0, x): a float 1.5 admits two, True one, NaN and -3 none (:53).
    for (limit, admitted) in [
        (f(1.5), 2),
        (PyReal::Bool(true), 1),
        (f(NAN), 0),
        (i(-3), 0),
    ] {
        let mut c = config();
        c.maximum_pending_hops = limit.clone();
        let mut s = scheduler(c);
        let results: Vec<_> = ["a", "b", "c"]
            .iter()
            .map(|r| s.enqueue(work(r, "p")))
            .collect();
        let mut expected = vec![Ok(()); admitted];
        expected.resize(3, backpressure(WORK_LIMIT, f(0.01)));
        assert_eq!(results, expected, "maximum_pending_hops {limit:?}");
    }
    // Payload byte accounting (:265-275): None 0, bytes their length, a tuple or list of ints
    // (bools excluded) 4 per element, anything else TypeError.
    {
        let mut s = scheduler(config());
        let payloads = [
            (HopPayload::None, Ok(())),
            (HopPayload::Bytes(vec![1, 2, 3, 4, 5]), Ok(())),
            (HopPayload::Tuple(vec![i(1), i(2), i(3)]), Ok(())),
            (HopPayload::List(vec![i(1), i(2)]), Ok(())),
            (HopPayload::Tuple(vec![]), Ok(())),
            (HopPayload::Tuple(vec![big(30), i(-1)]), Ok(())),
            (
                HopPayload::Tuple(vec![PyReal::Bool(true), i(2)]),
                Err(SchedulerError::Type(UNSUPPORTED_PAYLOAD)),
            ),
            (
                HopPayload::List(vec![f(1.0)]),
                Err(SchedulerError::Type(UNSUPPORTED_PAYLOAD)),
            ),
            (
                HopPayload::Other("'text'".to_owned()),
                Err(SchedulerError::Type(UNSUPPORTED_PAYLOAD)),
            ),
        ];
        for (n, (payload, expected)) in payloads.into_iter().enumerate() {
            let item = with_payload(work(&format!("r{n}"), "p"), payload);
            assert_eq!(s.enqueue(item), expected, "payload {n}");
        }
        // 0 + 5 + 12 + 8 + 0 + 8; the three refusals recorded nothing.
        assert_eq!(counts(&s), (6, 33, 6));
    }
    // byte_limit at queued + size > max(0, maximum_pending_bytes) (:56-60): exactly 16 fits.
    {
        let mut c = config();
        c.maximum_pending_bytes = i(16);
        let mut s = scheduler(c);
        assert_eq!(s.enqueue(with_payload(work("a", "p"), tokens(2))), Ok(()));
        assert_eq!(s.enqueue(with_payload(work("b", "p"), tokens(2))), Ok(()));
        assert_eq!(s.enqueue(work("c", "p")), Ok(()));
        assert_eq!(
            s.enqueue(with_payload(work("d", "p"), HopPayload::Bytes(vec![0]))),
            backpressure(BYTE_LIMIT, f(0.01))
        );
        assert_eq!(counts(&s), (3, 16, 3));
    }
    for (limit, first, second) in [
        // 8 + 1 > 8.5.
        (f(8.5), tokens(2), Err(())),
        // max(0, True) is True: one byte fits, two do not.
        (PyReal::Bool(true), HopPayload::Bytes(vec![0]), Err(())),
        // max(0, NaN) is 0: an empty payload fits, one byte does not.
        (f(NAN), HopPayload::Bytes(vec![]), Err(())),
        (f(9.0), tokens(2), Ok(())),
    ] {
        let mut c = config();
        c.maximum_pending_bytes = limit.clone();
        let mut s = scheduler(c);
        assert_eq!(s.enqueue(with_payload(work("a", "p"), first)), Ok(()));
        let result = s.enqueue(with_payload(work("b", "p"), HopPayload::Bytes(vec![0])));
        assert_eq!(
            result,
            second.map_err(|()| SchedulerError::Backpressure {
                reason: BYTE_LIMIT,
                retry_after_seconds: f(0.01),
            }),
            "maximum_pending_bytes {limit:?}"
        );
    }
    // The retry floor max(0.001, backpressure_retry_after_seconds) (:52) as Python's max
    // returns it: 0.001 for 0, -1.0, 0.0002 and NaN; an int 5 stays the int 5, True stays True.
    for (retry, expected) in [
        (i(0), f(0.001)),
        (f(-1.0), f(0.001)),
        (f(0.0002), f(0.001)),
        (f(NAN), f(0.001)),
        (f(0.001), f(0.001)),
        (i(5), i(5)),
        (PyReal::Bool(true), PyReal::Bool(true)),
        (f(2.5), f(2.5)),
    ] {
        let mut c = config();
        c.maximum_pending_hops = i(0);
        c.backpressure_retry_after_seconds = retry.clone();
        let mut s = scheduler(c);
        assert_eq!(
            s.enqueue(work("a", "p")),
            backpressure(WORK_LIMIT, expected),
            "retry {retry:?}"
        );
        assert_eq!(counts(&s), (0, 0, 0));
    }
    // Check order (:49-60): duplicate before work_limit, work_limit before the payload type,
    // the payload type before byte_limit.
    {
        let mut c = config();
        c.maximum_pending_hops = i(1);
        c.maximum_pending_bytes = i(0);
        let mut s = scheduler(c);
        assert_eq!(s.enqueue(work("a", "p")), Ok(()));
        assert_eq!(s.enqueue(work("a", "p")), duplicate("a:p:1:DECODE:0:0"));
        let text = with_payload(work("b", "p"), HopPayload::Other("'text'".to_owned()));
        assert_eq!(s.enqueue(text.clone()), backpressure(WORK_LIMIT, f(0.01)));
        assert_eq!(counts(&s), (1, 0, 1));
        assert_eq!(popped(&mut s, 0.0), Ok("a:p:1:DECODE:0:0".to_owned()));
        assert_eq!(
            s.enqueue(text),
            Err(SchedulerError::Type(UNSUPPORTED_PAYLOAD))
        );
        assert_eq!(
            s.enqueue(with_payload(work("c", "p"), tokens(1))),
            backpressure(BYTE_LIMIT, f(0.01))
        );
        assert_eq!(counts(&s), (0, 0, 1));
    }
    // Declared V03: a NaN enqueued_at is refused before any check (no v0.1.4 producer stamps
    // one; its idempotency entry would sort unordered).
    {
        let mut s = scheduler(config());
        assert_eq!(
            s.enqueue(at(work("a", "p"), NAN)),
            Err(SchedulerError::Unsupported(NAN_ENQUEUED_AT))
        );
        assert_eq!(s.enqueue(at(work("a", "p"), INF)), Ok(()));
        assert_eq!(counts(&s), (1, 0, 1));
    }
}

#[test]
fn m16_hop_scheduler_effective_priority() {
    // scheduler.py:69-84 with the defaults (contracts.py:150-154): interactive base 100.0,
    // batch base 10.0, aging 1.0 per second, batch aging x4.0, deficit boost 50.0.
    let s = scheduler(config());
    let qos = |class: &str, enqueued_at: f64, deficit: f64| HopWorkItem {
        qos_class: class.to_owned(),
        deficit_ratio: deficit,
        ..at(work("r", "p"), enqueued_at)
    };
    let cases = [
        // Base by qos class: only "batch" takes the batch base.
        (qos("interactive", 0.0, 0.0), 0.0, 100.0),
        (qos("batch", 0.0, 0.0), 0.0, 10.0),
        (qos("other", 0.0, 0.0), 0.0, 100.0),
        // Aging: age * 1.0 * 1.0 for interactive work, * 4.0 for batch work.
        (qos("interactive", 10.0, 0.0), 12.5, 102.5),
        (qos("batch", 10.0, 0.0), 12.5, 20.0),
        // A negative age clamps to 0.0, as does inf - inf; an infinite age ages infinitely.
        (qos("interactive", 10.0, 0.0), 4.0, 100.0),
        (qos("interactive", INF, 0.0), INF, 100.0),
        (qos("interactive", 0.0, 0.0), INF, INF),
        (qos("interactive", 0.0, 0.0), NAN, 100.0),
        // Deficit max(0.0, min(1.0, d)) * 50.0: NaN and inf give 1.0, negatives 0.0.
        (qos("interactive", 0.0, 0.5), 0.0, 125.0),
        (qos("interactive", 0.0, 2.0), 0.0, 150.0),
        (qos("interactive", 0.0, -0.5), 0.0, 100.0),
        (qos("interactive", 0.0, NAN), 0.0, 150.0),
        (qos("interactive", 0.0, INF), 0.0, 150.0),
        (qos("interactive", 0.0, -INF), 0.0, 100.0),
        (qos("batch", 0.0, 1.0), 2.0, 10.0 + 2.0 * 4.0 + 50.0),
    ];
    for (n, (item, now, expected)) in cases.iter().enumerate() {
        assert_eq!(
            bits(s.effective_priority(item, *now)),
            Ok(expected.to_bits()),
            "case {n}"
        );
    }
    // Int and bool fields meet the float age and deficit through float() (:81-84):
    // 7 + 2.0 * True * 1.0 + 0.25 * 2 = 9.5; batch 10.0 + 2.0 * True * 3 + 0.5 = 16.5.
    let mut c = config();
    c.interactive_base_priority = i(7);
    c.batch_aging_multiplier = i(3);
    c.aging_priority_per_second = PyReal::Bool(true);
    c.maximum_deficit_boost = i(2);
    let s = scheduler(c);
    assert_eq!(
        bits(s.effective_priority(&qos("interactive", 0.0, 0.25), 2.0)),
        Ok(9.5f64.to_bits())
    );
    assert_eq!(
        bits(s.effective_priority(&qos("batch", 0.0, 0.25), 2.0)),
        Ok(16.5f64.to_bits())
    );
    // An int past the double range raises OverflowError, only for the qos class that reads it.
    let mut c = config();
    c.batch_base_priority = big(400);
    let s = scheduler(c);
    assert_eq!(
        bits(s.effective_priority(&qos("batch", 0.0, 0.0), 0.0)),
        Err(SchedulerError::Overflow(INT_TOO_LARGE_FOR_FLOAT))
    );
    assert_eq!(
        bits(s.effective_priority(&qos("interactive", 0.0, 0.0), 0.0)),
        Ok(100.0f64.to_bits())
    );
    let mut c = config();
    c.batch_aging_multiplier = big(400);
    let s = scheduler(c);
    assert_eq!(
        bits(s.effective_priority(&qos("batch", 0.0, 0.0), 0.0)),
        Err(SchedulerError::Overflow(INT_TOO_LARGE_FOR_FLOAT))
    );
    assert_eq!(
        bits(s.effective_priority(&qos("interactive", 0.0, 0.0), 5.0)),
        Ok(105.0f64.to_bits())
    );
    // An infinite aging rate: age 0 * inf is NaN; a positive age gives inf.
    let mut c = config();
    c.aging_priority_per_second = f(INF);
    let s = scheduler(c);
    assert!(
        s.effective_priority(&qos("interactive", 0.0, 0.0), 0.0)
            .is_ok_and(f64::is_nan)
    );
    assert_eq!(
        bits(s.effective_priority(&qos("interactive", 0.0, 0.0), 1.0)),
        Ok(INF.to_bits())
    );
}

#[test]
fn m16_hop_scheduler_pop_order_and_batches() {
    // Equal priorities (all interactive at 0.0) are broken by request_id, path_id,
    // token_index, hop_index, then the queue offset (:226-235); min keeps the first of equal
    // keys (:168-171).
    {
        let mut s = scheduler(config());
        let mut hop = |request: &str, path: &str, token: i64, hop: i64, phase: &str, attempt| {
            let item = HopWorkItem {
                token_index: i(token),
                hop_index: i(hop),
                phase: phase.to_owned(),
                path_attempt: i(attempt),
                idempotency_key: format!("{request}:{path}:{attempt}:{phase}:{token}:{hop}"),
                ..work(request, path)
            };
            assert_eq!(s.enqueue(item), Ok(()));
        };
        hop("r2", "p1", 0, 0, "DECODE", 1);
        hop("r1", "p2", 0, 0, "DECODE", 1);
        hop("r1", "p1", 3, 0, "DECODE", 1);
        hop("r1", "p1", 2, 5, "DECODE", 1);
        hop("r1", "p1", 2, 4, "PREFILL", 1);
        hop("r1", "p1", 2, 4, "DECODE", 1);
        hop("r1", "p1", 2, 4, "DECODE", 0);
        let order: Vec<_> = (0..8).map(|_| popped(&mut s, 0.0)).collect();
        let expected: Vec<Result<String, SchedulerError>> = [
            "r1:p1:1:PREFILL:2:4",
            "r1:p1:1:DECODE:2:4",
            "r1:p1:0:DECODE:2:4",
            "r1:p1:1:DECODE:2:5",
            "r1:p1:1:DECODE:3:0",
            "r1:p2:1:DECODE:0:0",
            "r2:p1:1:DECODE:0:0",
        ]
        .iter()
        .map(|key| Ok((*key).to_owned()))
        .chain([Err(SchedulerError::Index(EMPTY_HOP_QUEUE))])
        .collect();
        assert_eq!(order, expected);
        assert_eq!(
            batch_view(s.pop_batch(0.0, None, None, None)),
            Err(SchedulerError::Index(EMPTY_HOP_QUEUE))
        );
        assert_eq!(s.batch_candidates(0.0), Ok(vec![]));
    }
    // The priority decides before the ids: interactive 100.0 beats batch 10.0 until batch
    // aging (x4.0) overtakes it (10 + 30 * 4 = 130 at now 0.0 for an item enqueued at -30.0).
    {
        let mut s = scheduler(config());
        let batch = |item: HopWorkItem| HopWorkItem {
            qos_class: "batch".to_owned(),
            ..item
        };
        assert_eq!(s.enqueue(batch(work("a", "p"))), Ok(()));
        assert_eq!(s.enqueue(work("z", "p")), Ok(()));
        assert_eq!(s.enqueue(batch(at(work("b", "p"), -30.0))), Ok(()));
        let order: Vec<_> = (0..3).map(|_| popped(&mut s, 0.0)).collect();
        assert_eq!(
            order,
            vec![
                Ok("b:p:1:DECODE:0:0".to_owned()),
                Ok("z:p:1:DECODE:0:0".to_owned()),
                Ok("a:p:1:DECODE:0:0".to_owned())
            ]
        );
    }
    // The anchor's batch key (:168-186): a keyless anchor is popped alone; a keyed anchor takes
    // every queued item with its key, in key order, one item per request.
    {
        let k = batch_key(json!("placement-a"));
        let k2 = batch_key(json!("placement-b"));
        let mut s = scheduler(config());
        let item = |request: &str, key: &str| HopWorkItem {
            idempotency_key: key.to_owned(),
            ..work(request, "p")
        };
        let later = HopWorkItem {
            token_index: i(1),
            ..item("r1", "x1b")
        };
        let batch_r4 = HopWorkItem {
            qos_class: "batch".to_owned(),
            ..item("r4", "x4")
        };
        for queued in [
            keyed(item("r3", "x3"), &k),
            keyed(item("r1", "x1"), &k),
            keyed(later, &k),
            keyed(item("r2", "x2"), &k2),
            item("r0", "x0"),
            keyed(batch_r4, &k),
        ] {
            assert_eq!(s.enqueue(queued), Ok(()));
        }
        // batch_candidates names the next batch and removes nothing (:125-147).
        assert_eq!(
            s.batch_candidates(0.0).map(|c| keys(&c).join(",")),
            Ok("x0".to_owned())
        );
        assert_eq!(counts(&s), (6, 0, 6));
        assert_eq!(
            batch_view(s.pop_batch(0.0, None, None, Some(decision()))),
            Ok(view(None, &["x0"], Some(decision())))
        );
        assert_eq!(
            s.batch_candidates(0.0).map(|c| keys(&c).join(",")),
            Ok("x1,x3,x4".to_owned())
        );
        // A pop keeps the popped item's idempotency entry.
        assert_eq!(counts(&s), (5, 0, 6));
        assert_eq!(
            batch_view(s.pop_batch(0.0, None, None, None)),
            Ok(view(Some("placement-a"), &["x1", "x3", "x4"], None))
        );
        assert_eq!(
            batch_view(s.pop_batch(0.0, None, None, None)),
            Ok(view(Some("placement-a"), &["x1b"], None))
        );
        assert_eq!(
            batch_view(s.pop_batch(0.0, None, None, None)),
            Ok(view(Some("placement-b"), &["x2"], None))
        );
        assert_eq!(counts(&s), (0, 0, 6));
    }
    // maximum = max(1, maximum_items or maximum_runtime_batch_size) slices the members
    // (:160-186): the config's 2, then maximum_items 0 -> 1, then 10**30 takes all.
    let k = batch_key(json!("placement-a"));
    let five = |s: &mut HopScheduler| {
        for r in ["r1", "r2", "r3", "r4", "r5"] {
            assert_eq!(s.enqueue(keyed(work(r, "p"), &k)), Ok(()));
        }
    };
    {
        let mut c = config();
        c.maximum_runtime_batch_size = i(2);
        let mut s = scheduler(c);
        five(&mut s);
        // The candidates are not sliced.
        assert_eq!(s.batch_candidates(0.0).map(|c| c.len()), Ok(5));
        let sizes: Vec<_> = [None, Some(i(0)), Some(big(30))]
            .iter()
            .map(|maximum| batch_view(s.pop_batch(0.0, maximum.as_ref(), None, None)).map(|v| v.1))
            .collect();
        let owned = |ks: &[&str]| -> Vec<String> {
            ks.iter().map(|r| format!("{r}:p:1:DECODE:0:0")).collect()
        };
        assert_eq!(
            sizes,
            vec![
                Ok(owned(&["r1", "r2"])),
                Ok(owned(&["r3"])),
                Ok(owned(&["r4", "r5"]))
            ]
        );
    }
    // A float maximum reaches the slice only under a keyed anchor: TypeError, nothing removed
    // (:186). max(1, 1.0) and max(1, True) are the int 1.
    {
        let mut c = config();
        c.maximum_runtime_batch_size = f(2.5);
        let mut s = scheduler(c);
        assert_eq!(s.enqueue(work("r0", "p")), Ok(()));
        assert_eq!(
            batch_view(s.pop_batch(0.0, None, None, None)),
            Ok(view(None, &["r0:p:1:DECODE:0:0"], None))
        );
        five(&mut s);
        assert_eq!(
            batch_view(s.pop_batch(0.0, None, None, None)),
            Err(SchedulerError::Type(SLICE_INDEX_NOT_INT))
        );
        assert_eq!(
            batch_view(s.pop_batch(0.0, Some(&f(3.0)), None, None)),
            Err(SchedulerError::Type(SLICE_INDEX_NOT_INT))
        );
        assert_eq!(counts(&s), (5, 0, 6));
        assert_eq!(
            batch_view(s.pop_batch(0.0, Some(&f(1.0)), None, None)).map(|v| v.1),
            Ok(vec!["r1:p:1:DECODE:0:0".to_owned()])
        );
        assert_eq!(
            batch_view(s.pop_batch(0.0, Some(&PyReal::Bool(true)), None, None)).map(|v| v.1),
            Ok(vec!["r2:p:1:DECODE:0:0".to_owned()])
        );
        // pop_next passes maximum_items=1, so the float config never reaches it.
        assert_eq!(popped(&mut s, 0.0), Ok("r3:p:1:DECODE:0:0".to_owned()));
        assert_eq!(counts(&s), (2, 0, 6));
    }
    // The byte bound max(1, maximum_bytes) keeps at least the first item and stops at the
    // first that does not fit (:187-197).
    {
        let mut s = scheduler(config());
        for (r, payload) in [
            ("r1", tokens(4)),
            ("r2", tokens(2)),
            ("r3", tokens(2)),
            ("r4", HopPayload::None),
        ] {
            assert_eq!(
                s.enqueue(keyed(with_payload(work(r, "p"), payload), &k)),
                Ok(())
            );
        }
        assert_eq!(
            batch_view(s.pop_batch(0.0, None, Some(&i(1)), None)).map(|v| v.1),
            Ok(vec!["r1:p:1:DECODE:0:0".to_owned()])
        );
        assert_eq!(counts(&s), (3, 16, 4));
        assert_eq!(
            batch_view(s.pop_batch(0.0, None, Some(&f(15.5)), None)).map(|v| v.1),
            Ok(vec!["r2:p:1:DECODE:0:0".to_owned()])
        );
        assert_eq!(
            batch_view(s.pop_batch(0.0, None, Some(&i(8)), None)).map(|v| v.1),
            Ok(vec![
                "r3:p:1:DECODE:0:0".to_owned(),
                "r4:p:1:DECODE:0:0".to_owned()
            ])
        );
        assert_eq!(counts(&s), (0, 0, 4));
    }
    // Declared V03: a NaN priority among two or more compatible items is refused (CPython's
    // sort leaves their order to its algorithm); alone, or under min, it is exact.
    {
        let mut c = config();
        c.aging_priority_per_second = f(INF);
        let mut s = scheduler(c);
        assert_eq!(s.enqueue(keyed(work("r1", "p"), &k)), Ok(()));
        assert_eq!(s.enqueue(keyed(work("r2", "p"), &k)), Ok(()));
        assert_eq!(
            s.batch_candidates(0.0).map(|c| c.len()),
            Err(SchedulerError::Unsupported(UNORDERED_BATCH_PRIORITY))
        );
        assert_eq!(
            batch_view(s.pop_batch(0.0, None, None, None)),
            Err(SchedulerError::Unsupported(UNORDERED_BATCH_PRIORITY))
        );
        // At now 1.0 both priorities are inf: ordered, tie broken by request id.
        assert_eq!(
            batch_view(s.pop_batch(1.0, None, None, None)).map(|v| v.1),
            Ok(vec![
                "r1:p:1:DECODE:0:0".to_owned(),
                "r2:p:1:DECODE:0:0".to_owned()
            ])
        );
        assert_eq!(s.enqueue(at(work("a", "p"), 0.0)), Ok(()));
        assert_eq!(s.enqueue(at(work("b", "p"), -1.0)), Ok(()));
        // a's NaN in first place is kept by min although b's inf priority is higher.
        assert_eq!(popped(&mut s, 0.0), Ok("a:p:1:DECODE:0:0".to_owned()));
    }
}

#[test]
fn m16_hop_scheduler_take_withdraw_release() {
    // take (:89-104) matches by hop identity, removes the queued item, subtracts its bytes and
    // keeps its idempotency entry.
    {
        let mut s = scheduler(config());
        let b = with_payload(work("b", "p"), tokens(2));
        assert_eq!(s.enqueue(with_payload(work("a", "p"), tokens(1))), Ok(()));
        assert_eq!(s.enqueue(b.clone()), Ok(()));
        let probe = HopWorkItem {
            idempotency_key: "different".to_owned(),
            payload: HopPayload::None,
            ..b.clone()
        };
        assert_eq!(s.take(&probe), Some(b.clone()));
        assert_eq!(counts(&s), (1, 4, 2));
        assert_eq!(s.enqueue(b.clone()), duplicate("b:p:1:DECODE:0:0"));
        assert_eq!(s.take(&b), None);
        assert_eq!(s.take(&work("zz", "p")), None);
        assert_eq!(counts(&s), (1, 4, 2));
    }
    // withdraw (:106-123) removes, subtracts the bytes and drops the entry; nothing queued,
    // nothing dropped.
    {
        let mut s = scheduler(config());
        let b = with_payload(work("b", "p"), tokens(2));
        assert_eq!(s.enqueue(with_payload(work("a", "p"), tokens(1))), Ok(()));
        assert_eq!(s.enqueue(b.clone()), Ok(()));
        assert!(s.withdraw(&b));
        assert_eq!(counts(&s), (1, 4, 1));
        assert_eq!(s.enqueue(b.clone()), Ok(()));
        assert!(!s.withdraw(&work("zz", "p")));
        assert_eq!(counts(&s), (2, 12, 2));
        // A popped item's entry is not queued, so withdraw keeps it.
        assert_eq!(popped(&mut s, 0.0), Ok("a:p:1:DECODE:0:0".to_owned()));
        assert!(!s.withdraw(&work("a", "p")));
        assert_eq!(counts(&s), (1, 8, 2));
    }
    // release_path (:237-247) drops the path's queued items and every entry it recorded
    // (popped ones too) and recomputes the bytes; the status views follow (:249-263).
    {
        let mut s = scheduler(config());
        let placed = |r: &str, path: &str, payload: HopPayload, placement: &str| HopWorkItem {
            placement_id: pj(json!(placement)),
            ..with_payload(work(r, path), payload)
        };
        for item in [
            placed("a", "p1", tokens(1), "pa"),
            placed("b", "p2", tokens(2), "pb"),
            placed("c", "p1", tokens(3), "pb"),
            placed("d", "p2", HopPayload::Bytes(vec![0]), "pa"),
        ] {
            assert_eq!(s.enqueue(item), Ok(()));
        }
        assert_eq!(popped(&mut s, 0.0), Ok("a:p1:1:DECODE:0:0".to_owned()));
        assert_eq!(counts(&s), (3, 21, 4));
        assert_eq!(s.queued_path_ids(), path_ids(&["p1", "p2"]));
        assert_eq!(depths(&s), Ok(vec![(json!("pb"), 2), (json!("pa"), 1)]));
        s.release_path("p1");
        assert_eq!(counts(&s), (2, 9, 2));
        assert_eq!(s.queued_path_ids(), path_ids(&["p2"]));
        assert_eq!(depths(&s), Ok(vec![(json!("pb"), 1), (json!("pa"), 1)]));
        // p1's entries are gone (a re-enqueue is new); p2's stay.
        assert_eq!(s.enqueue(with_payload(work("a", "p1"), tokens(1))), Ok(()));
        assert_eq!(
            s.enqueue(with_payload(work("b", "p2"), tokens(2))),
            duplicate("b:p2:1:DECODE:0:0")
        );
        s.release_path("missing");
        assert_eq!(counts(&s), (3, 13, 3));
        s.release_path("p2");
        s.release_path("p1");
        assert_eq!(counts(&s), (0, 0, 0));
        assert_eq!(s.queued_path_ids(), path_ids(&[]));
        assert_eq!(depths(&s), Ok(vec![]));
    }
    // placement_depths is dict(Counter(placement_id)) (:257-259): Python-equal ids are one key
    // under their first spelling (1 == 1.0 == True), '1' is another; a list id is unhashable.
    {
        let mut s = scheduler(config());
        for (r, placement) in [
            ("a", json!(1)),
            ("b", json!(1.0)),
            ("c", json!(true)),
            ("d", json!("1")),
            ("e", json!(2)),
        ] {
            let item = HopWorkItem {
                placement_id: pj(placement),
                ..work(r, "p")
            };
            assert_eq!(s.enqueue(item), Ok(()));
        }
        assert_eq!(
            depths(&s),
            Ok(vec![(json!(1), 3), (json!("1"), 1), (json!(2), 1)])
        );
        let listed = HopWorkItem {
            placement_id: pj(json!(["p"])),
            ..work("f", "p")
        };
        assert_eq!(s.enqueue(listed), Ok(()));
        assert_eq!(
            depths(&s),
            Err(SchedulerError::Type(UNHASHABLE_PLACEMENT_ID))
        );
        let empty: IdMap<u64, PyJson> = IdMap::new();
        assert_eq!(scheduler(config()).placement_depths(), Ok(empty));
    }
}

#[test]
fn m16_hop_scheduler_idempotency_retention() {
    // Retention (:283-290): an inactive entry older than max(0.0, retention) by strict `>` is
    // dropped at the next enqueue, at that item's enqueued_at.
    {
        let mut c = config();
        c.idempotency_retention_seconds = f(10.0);
        let mut s = scheduler(c);
        assert_eq!(s.enqueue(at(work("a", "p"), 0.0)), Ok(()));
        assert_eq!(popped(&mut s, 0.0), Ok("a:p:1:DECODE:0:0".to_owned()));
        assert_eq!(
            s.enqueue(at(work("a", "p"), 10.0)),
            duplicate("a:p:1:DECODE:0:0")
        );
        let next_double = f64::from_bits(10.0f64.to_bits() + 1);
        assert_eq!(s.enqueue(at(work("a", "p"), next_double)), Ok(()));
        assert_eq!(counts(&s), (1, 0, 1));
    }
    // An int retention compares exactly with the float age; NaN and negatives clamp to 0.0.
    for (retention, kept_at, dropped_at) in [
        (i(3), 3.0, 3.0000000000000004),
        (f(NAN), 0.0, 5e-324),
        (f(-5.0), 0.0, 5e-324),
        (PyReal::Bool(true), 1.0, 1.0000000000000002),
    ] {
        let mut c = config();
        c.idempotency_retention_seconds = retention.clone();
        let mut s = scheduler(c);
        assert_eq!(s.enqueue(at(work("a", "p"), 0.0)), Ok(()));
        assert_eq!(popped(&mut s, 0.0), Ok("a:p:1:DECODE:0:0".to_owned()));
        assert_eq!(
            s.enqueue(at(work("a", "p"), kept_at)),
            duplicate("a:p:1:DECODE:0:0"),
            "retention {retention:?}"
        );
        assert_eq!(
            s.enqueue(at(work("a", "p"), dropped_at)),
            Ok(()),
            "retention {retention:?}"
        );
    }
    // Eviction runs at the new item's enqueued_at, before the checks: an earlier stamp evicts
    // nothing, and a refused enqueue still evicts.
    {
        let mut c = config();
        c.idempotency_retention_seconds = f(1.0);
        c.maximum_pending_hops = i(2);
        let mut s = scheduler(c);
        assert_eq!(s.enqueue(at(work("a", "p"), 100.0)), Ok(()));
        assert_eq!(popped(&mut s, 100.0), Ok("a:p:1:DECODE:0:0".to_owned()));
        assert_eq!(s.enqueue(at(work("b", "p"), 50.0)), Ok(()));
        assert_eq!(counts(&s), (1, 0, 2));
        assert_eq!(s.enqueue(at(work("c", "p"), 101.5)), Ok(()));
        assert_eq!(counts(&s), (2, 0, 2));
        assert_eq!(
            s.enqueue(at(work("a", "p"), 50.0)),
            backpressure(WORK_LIMIT, f(0.01))
        );
        assert_eq!(counts(&s), (2, 0, 2));
    }
    {
        let mut c = config();
        c.idempotency_retention_seconds = f(300.0);
        c.maximum_pending_hops = i(1);
        let mut s = scheduler(c);
        assert_eq!(s.enqueue(at(work("a", "p"), 0.0)), Ok(()));
        assert_eq!(popped(&mut s, 0.0), Ok("a:p:1:DECODE:0:0".to_owned()));
        assert_eq!(s.enqueue(at(work("b", "p"), 1.0)), Ok(()));
        assert_eq!(counts(&s), (1, 0, 2));
        assert_eq!(
            s.enqueue(at(work("c", "p"), 400.0)),
            backpressure(WORK_LIMIT, f(0.01))
        );
        assert_eq!(counts(&s), (1, 0, 1));
        // a's entry is gone: no longer a duplicate, so the limit answers.
        assert_eq!(
            s.enqueue(at(work("a", "p"), 400.0)),
            backpressure(WORK_LIMIT, f(0.01))
        );
    }
    // Over max(1, maximum_idempotency_entries), inactive entries go oldest-first by
    // (seen_at, identity) (:292-302): b (5.0, "b") before c (5.0, "c") before a (7.0).
    {
        let mut c = config();
        c.maximum_idempotency_entries = i(3);
        let mut s = scheduler(c);
        assert_eq!(s.enqueue(at(work("c", "p"), 5.0)), Ok(()));
        assert_eq!(s.enqueue(at(work("b", "p"), 5.0)), Ok(()));
        assert_eq!(s.enqueue(at(work("a", "p"), 7.0)), Ok(()));
        for _ in 0..3 {
            assert!(s.pop_next(10.0).is_ok());
        }
        assert_eq!(counts(&s), (0, 0, 3));
        assert_eq!(s.enqueue(at(work("d", "p"), 8.0)), Ok(()));
        assert_eq!(counts(&s), (1, 0, 3));
        assert_eq!(
            s.enqueue(at(work("c", "p"), 8.0)),
            duplicate("c:p:1:DECODE:0:0")
        );
        assert_eq!(
            s.enqueue(at(work("a", "p"), 8.0)),
            duplicate("a:p:1:DECODE:0:0")
        );
        assert_eq!(s.enqueue(at(work("b", "p"), 8.0)), Ok(()));
        // c (5.0) is now the oldest inactive entry and goes.
        assert_eq!(counts(&s), (2, 0, 3));
        assert_eq!(s.enqueue(at(work("c", "p"), 8.0)), Ok(()));
    }
    // Active (queued) entries are never evicted, even over the maximum.
    {
        let mut c = config();
        c.maximum_idempotency_entries = i(1);
        let mut s = scheduler(c);
        for (r, t) in [("a", 0.0), ("b", 1.0), ("c", 2.0)] {
            assert_eq!(s.enqueue(at(work(r, "p"), t)), Ok(()));
        }
        assert_eq!(counts(&s), (3, 0, 3));
        assert_eq!(popped(&mut s, 3.0), Ok("a:p:1:DECODE:0:0".to_owned()));
        assert_eq!(s.enqueue(at(work("d", "p"), 4.0)), Ok(()));
        assert_eq!(counts(&s), (3, 0, 3));
        assert_eq!(s.enqueue(at(work("a", "p"), 5.0)), Ok(()));
    }
    // len > max(1, x): two inactive entries, then c; 1.5, True, 0 and NaN keep one entry,
    // 2 and 2.5 two.
    for (maximum, kept) in [
        (f(1.5), 1),
        (PyReal::Bool(true), 1),
        (i(0), 1),
        (f(NAN), 1),
        (i(2), 2),
        (f(2.5), 2),
    ] {
        let mut c = config();
        c.maximum_idempotency_entries = maximum.clone();
        let mut s = scheduler(c);
        assert_eq!(s.enqueue(at(work("a", "p"), 0.0)), Ok(()));
        assert_eq!(s.enqueue(at(work("b", "p"), 1.0)), Ok(()));
        assert!(s.pop_next(2.0).is_ok());
        assert!(s.pop_next(2.0).is_ok());
        assert_eq!(s.enqueue(at(work("c", "p"), 3.0)), Ok(()));
        assert_eq!(counts(&s), (1, 0, kept), "maximum {maximum:?}");
    }
}

// The M16 composition (mycelium_m16_runtime.py:383-394): the real ledger on the lease clock,
// one id source for ledger and builder, RoutePolicy(RouteScorer(config)) and
// HopScheduler(config) with the factory's RouterConfig(reservation_lease_seconds=3_600.0)
// (:1260).

/// A settable lease clock.
#[derive(Default)]
struct Clock(Cell<f64>);

impl LeaseClock for Clock {
    fn now(&self) -> f64 {
        self.0.get()
    }
}

/// `new(prefix)` gives `prefix-1`, `prefix-2`, ... across prefixes.
#[derive(Default)]
struct Ids(Cell<u64>);

impl IdSource for Ids {
    fn new_id(&self, prefix: &str) -> String {
        let n = self.0.get() + 1;
        self.0.set(n);
        format!("{prefix}-{n}")
    }
}

/// stage-0 [p0 on node-a], stage-1 [p1 on node-b], prefill 2, decode 1, KV 0 and 10 per token,
/// edge p0->p1, loopback p1->p0.
fn two_stage_graph() -> Graph {
    let placement = |k: usize, id: &str, node: &str| {
        json!({
            "placement_id": id, "node_id": node, "replica_group_id": format!("group-{k}"),
            "assignment_id": format!("assignment-{k}"), "stage_signature": format!("signature-{k}"),
            "load_proof_digest": format!("proof-{k}"), "runtime_backend": "numpy",
            "runtime_endpoint": format!("local://{k}"),
        })
    };
    let stage = |k: usize, kv: u64, p: Value| {
        json!({
            "stage_id": format!("stage-{k}"),
            "range": {"start_layer": k, "end_layer_exclusive": k + 1, "layer_count": 1},
            "component_roles": ["decoder"],
            "stage_cost": {"prefill_work_units_per_prompt_token": 2,
                           "decode_work_units_per_token": 1, "kv_bytes_per_context_token": kv},
            "placements": [p],
        })
    };
    let doc = json!({
        "protocol": "mycelium.execution_graph.v1",
        "deployment_id": "deployment-m16", "deployment_epoch": 1, "topology_version": 1,
        "model_id": "model-m16", "resolved_commit": "commit-m16", "manifest_digest": "digest-m16",
        "entry_stage_id": "stage-0", "final_stage_id": "stage-1",
        "hidden_size": 8, "activation_bytes": 2, "token_envelope_bytes": 16,
        "stages": [stage(0, 0, placement(0, "p0", "node-a")),
                   stage(1, 10, placement(1, "p1", "node-b"))],
        "edges": [{"edge_id": "edge-0", "from_placement_id": "p0", "to_placement_id": "p1",
                   "link_id": "link-edge-0"}],
        "loopback_edges": [{"edge_id": "loop-0", "from_placement_id": "p1",
                            "to_placement_id": "p0", "link_id": "link-loop-0"}],
    });
    execution_graph::parse(&doc).expect("a valid execution graph")
}

fn device(node: &str) -> DeviceState {
    DeviceState {
        node_id: json!(node),
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

fn devices() -> IdMap<DeviceState> {
    let mut map = IdMap::new();
    for node in ["node-a", "node-b"] {
        map.insert(json!(node), device(node))
            .expect("hashable node id");
    }
    map
}

/// The gateway's request shape (mycelium_request_gateway/backend.py:227-239): 4 prompt tokens.
fn request(id: &str) -> RequestContext {
    RequestContext {
        request_id: id.to_owned(),
        prompt_token_ids: (10..14).map(i).collect(),
        max_new_tokens: i(8),
        expected_new_tokens: i(8),
        qos_class: "interactive".to_owned(),
        admitted_at: f(0.0),
        target_ttft_ms: f(1000.0),
        target_tpot_ms: f(100.0),
        target_tokens_per_second: f(10.0),
        sampling_seed: i(0),
        generation_config_digest: "sha256:test".to_owned(),
    }
}

type Builder<'a> = ProgressivePathBuilder<ResourceLedger<&'a Clock, &'a Ids>, &'a Ids>;

/// admit's route loop (:424-443): start, advance to complete, lock.
fn route(
    b: &mut Builder,
    graph: &Graph,
    request: &RequestContext,
) -> Result<PathManifest, PathBuilderError> {
    let mut build = b.start(request, graph, 0, IdSet::new(), IdSet::new(), IdSet::new());
    while !b.is_complete(&build) {
        build = b.advance(&build, &devices(), 100.0, Some(5.0))?;
    }
    b.lock(&build, Some(100.0), Some(5.0))
}

/// Builtin `min` over the hops' expiries: the first unless a later one is strictly less.
fn min_expiry(manifest: &PathManifest) -> f64 {
    let mut least = manifest.ordered_hops[0].reservation_expires_at;
    for hop in &manifest.ordered_hops[1..] {
        if hop.reservation_expires_at < least {
            least = hop.reservation_expires_at;
        }
    }
    least
}

/// :452-467 the REQUEST item, field for field.
fn request_item(request: &RequestContext, manifest: &PathManifest, queued_at: f64) -> HopWorkItem {
    HopWorkItem {
        lease_expires_at: f(min_expiry(manifest)),
        ..HopWorkItem::new(
            &request.request_id,
            &manifest.path_id,
            PyReal::signed(false, manifest.path_attempt.into()),
            "REQUEST",
            i(-1),
            i(0),
            pj(manifest.ordered_hops[0].placement_id.clone()),
            &request.qos_class,
            0.0,
            queued_at,
            &format!("m16:{}:{}", request.request_id, manifest.path_id),
            HopPayload::Tuple(request.prompt_token_ids.clone()),
        )
    }
}

#[test]
fn m16_hop_scheduler_coordinator_request_flow() {
    let graph = two_stage_graph();
    let (clock, ids) = (Clock::default(), Ids::default());
    clock.0.set(5.0);
    let plenty = 1_000_000_000_000u64;
    let caps: Vec<PlacementCapacity> = ["p0", "p1"]
        .iter()
        .map(|id| PlacementCapacity {
            placement_id: json!(id),
            memory_capacity_bytes: PyNumber::from(plenty),
            kv_capacity_bytes: PyNumber::from(plenty),
            workspace_capacity_bytes: PyNumber::from(plenty),
            maximum_reservations: 64,
        })
        .collect();
    // The factory's config with one pending hop, so the second admission meets the limit.
    let mut config = config();
    config.reservation_lease_seconds = f(3600.0);
    config.maximum_pending_hops = i(1);
    let ledger = ResourceLedger::new(&graph, &caps, &clock, &ids).expect("a ledger");
    let mut builder = ProgressivePathBuilder::new(
        RoutePolicy::new(RouteScorer::new(config.clone())),
        ledger,
        &ids,
        None,
    );
    let mut s = scheduler(config);

    // admit request-1: path-1 holds reservation-2 (p0) and reservation-3 (p1), each expiring
    // at lease_now 5.0 + 3600.0 (routing.py:188, :208; recorded by the ledger,
    // mycelium_m16_runtime.py:239-250), so lease_expires_at = 3605.0.
    let first = request("request-1");
    let m1 = route(&mut builder, &graph, &first).expect("request-1 routes");
    assert_eq!(
        (
            m1.path_id.as_str(),
            m1.path_attempt,
            min_expiry(&m1).to_bits()
        ),
        ("path-1", 0, 3605.0f64.to_bits())
    );
    let item1 = request_item(&first, &m1, 100.0);
    assert_eq!(
        (
            item1.idempotency_key.as_str(),
            &item1.placement_id,
            &item1.token_index,
            &item1.hop_index,
            &item1.lease_expires_at,
        ),
        (
            "m16:request-1:path-1",
            &pj(json!("p0")),
            &i(-1),
            &i(0),
            &f(3605.0)
        )
    );
    assert_eq!(s.enqueue(item1.clone()), Ok(()));
    // status (:623-625): one queued hop of 4 prompt tokens * 4 bytes.
    assert_eq!(counts(&s), (1, 16, 1));
    assert_eq!(s.queued_path_ids(), path_ids(&["path-1"]));
    assert_eq!(depths(&s), Ok(vec![(json!("p0"), 1)]));

    // admit request-2 past maximum_pending_hops: work_limit with the configured retry
    // max(0.001, 0.01) = 0.01; admit releases the request's reservations (:468-470).
    let second = request("request-2");
    let m2 = route(&mut builder, &graph, &second).expect("request-2 routes");
    assert_eq!(m2.path_id, "path-4");
    assert_eq!(
        s.enqueue(request_item(&second, &m2, 100.5)),
        backpressure(WORK_LIMIT, f(0.01))
    );
    builder.capacity_mut().release_request("request-2");
    assert!(
        builder
            .capacity()
            .request_reservations("request-2")
            .is_empty()
    );
    assert_eq!(counts(&s), (1, 16, 1));

    // next_dispatch (:500-506): depth > 0, so pop_next(now) returns the REQUEST item.
    assert_eq!(s.queue_depth(), 1);
    assert_eq!(s.pop_next(101.0), Ok(item1.clone()));
    assert_eq!(counts(&s), (0, 0, 1));
    assert_eq!(s.queue_depth(), 0);

    // request-2 admitted again on path-7, then cancelled (:582-590): release_path empties the
    // queue's depth and bytes and drops its entry.
    let m3 = route(&mut builder, &graph, &second).expect("request-2 routes again");
    assert_eq!(m3.path_id, "path-7");
    assert_eq!(s.enqueue(request_item(&second, &m3, 102.0)), Ok(()));
    assert_eq!(counts(&s), (1, 16, 2));
    s.release_path(&m3.path_id);
    builder.capacity_mut().release_request("request-2");
    assert_eq!(counts(&s), (0, 0, 1));
    assert_eq!(s.queued_path_ids(), path_ids(&[]));
    assert!(
        builder
            .capacity()
            .request_reservations("request-2")
            .is_empty()
    );
    // The dispatched item's entry stays: the same REQUEST hop is a duplicate.
    assert_eq!(s.enqueue(item1), duplicate("m16:request-1:path-1"));
}

// Paired oracle probes. Every case below was executed through the real v0.1.4 code path at
// f4e366e9 (HopScheduler(RouterConfig(**overrides)) and the contracts) by the probe named in the
// fixture's provenance (CPython 3.14.2). The fixture keeps each number's Python type:
// {"int": str(n)}, {"bool": b} or {"float": repr(x), "bits"}.
const ORACLE: &str = include_str!("fixtures/m16-hop-scheduler-oracle.json");

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

fn recorded_f64(tagged: &Value) -> f64 {
    match recorded(tagged) {
        PyReal::Float(x) => x,
        other => panic!("not a float: {other:?}"),
    }
}

/// A test-owned spelling of a Python number: its type and exact value (float bits).
fn tag(v: &PyReal) -> Value {
    json!(match v {
        PyReal::Bool(b) => format!("bool:{b}"),
        PyReal::Int {
            negative,
            magnitude,
        } => {
            let digits = magnitude.to_string();
            let sign = if *negative && digits != "0" { "-" } else { "" };
            format!("int:{sign}{digits}")
        }
        PyReal::Float(x) => format!("float:{:016x}", x.to_bits()),
    })
}

fn tag_f64(x: f64) -> Value {
    tag(&PyReal::Float(x))
}

/// The fixture's document with every number tag in [`tag`]'s spelling.
fn normalized(v: &Value) -> Value {
    match v {
        Value::Object(map)
            if map.contains_key("bits") || map.contains_key("int") || map.contains_key("bool") =>
        {
            tag(&recorded(v))
        }
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(key, item)| (key.clone(), normalized(item)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.iter().map(normalized).collect()),
        other => other.clone(),
    }
}

/// A recorded number of the hop fields the relay copies from a peer's frame (path_attempt,
/// token_index, hop_index, prefill_chunk_token_count, position, lease_expires_at and the batch
/// key's token_span): an int in the i64 range is a plain JSON integer, every other number (a
/// bool, a float, a wider int) carries its type tag.
fn number(v: &Value) -> PyReal {
    if v.is_object() {
        recorded(v)
    } else {
        PyReal::from_json(v).expect("a number")
    }
}

/// A Python int's decimal digits with its sign.
fn int_text(v: &PyReal) -> Option<String> {
    match v {
        PyReal::Int {
            negative,
            magnitude,
        } => Some(format!("{}{magnitude}", if *negative { "-" } else { "" })),
        _ => None,
    }
}

/// The Rust carrier of one of those fields: the value it holds for a recorded number (`Err`
/// names a number it cannot hold, which the replay reports as an unrepresentable op), and the
/// spelling of what it holds in the fixture's form.
trait Carrier: Sized {
    fn carry(v: PyReal) -> Result<Self, String>;
    fn spelled(&self) -> Value;
}

impl Carrier for PyReal {
    fn carry(v: PyReal) -> Result<Self, String> {
        Ok(v)
    }

    fn spelled(&self) -> Value {
        match int_text(self).and_then(|digits| digits.parse::<i64>().ok()) {
            Some(n) => json!(n),
            None => tag(self),
        }
    }
}

impl Carrier for i64 {
    fn carry(v: PyReal) -> Result<Self, String> {
        int_text(&v)
            .and_then(|digits| digits.parse().ok())
            .ok_or_else(|| format!("{v:?} is not an i64"))
    }

    fn spelled(&self) -> Value {
        json!(self)
    }
}

impl Carrier for u64 {
    fn carry(v: PyReal) -> Result<Self, String> {
        int_text(&v)
            .and_then(|digits| digits.parse().ok())
            .ok_or_else(|| format!("{v:?} is not a u64"))
    }

    fn spelled(&self) -> Value {
        json!(self)
    }
}

impl Carrier for f64 {
    fn carry(v: PyReal) -> Result<Self, String> {
        match v {
            PyReal::Float(x) => Ok(x),
            other => Err(format!("{other:?} is not a float")),
        }
    }

    fn spelled(&self) -> Value {
        tag_f64(*self)
    }
}

fn carried<T: Carrier>(v: &Value) -> Result<T, String> {
    T::carry(number(v))
}

fn text(v: &Value) -> &str {
    v.as_str().expect("a string field")
}

fn int_field(v: &Value) -> i64 {
    v.as_i64().expect("an int field")
}

fn recorded_payload(spec: &Value) -> HopPayload {
    if spec.get("none").is_some() {
        return HopPayload::None;
    }
    if let Some(hex) = spec["bytes"].as_str() {
        let bytes = (0..hex.len())
            .step_by(2)
            .map(|k| u8::from_str_radix(&hex[k..k + 2], 16).expect("hex"))
            .collect();
        return HopPayload::Bytes(bytes);
    }
    if let Some(values) = spec["seq"].as_array() {
        let values = values.iter().map(recorded).collect();
        return if spec["kind"] == "tuple" {
            HopPayload::Tuple(values)
        } else {
            HopPayload::List(values)
        };
    }
    HopPayload::Other(text(&spec["str"]).to_owned())
}

fn payload_doc(p: &HopPayload) -> Value {
    let seq = |values: &[PyReal], kind: &str| json!({"seq": values.iter().map(tag).collect::<Vec<_>>(), "kind": kind});
    match p {
        HopPayload::None => json!({"none": true}),
        HopPayload::Bytes(bytes) => {
            json!({"bytes": bytes.iter().map(|b| format!("{b:02x}")).collect::<String>()})
        }
        HopPayload::Tuple(values) => seq(values, "tuple"),
        HopPayload::List(values) => seq(values, "list"),
        HopPayload::Other(label) => json!({"str": label}),
    }
}

/// How a case spells the decoded values (the batch key's eleven graph-derived fields, the item's
/// `placement_id` and the `placement_depths` keys): the 82 earlier records and the reviewer's as
/// Python's `json.dumps` writes them, a NaN or infinity (which JSON cannot hold) with its number
/// tag; the key-value round's records (`"key_values": "typed"`) as typed Python values,
/// `{"none": true}`, a number tag, `{"str": s}`, `{"list": [...]}` and `{"dict": [[key,
/// value], ...]}` in insertion order.
#[derive(Clone, Copy)]
enum Spelling {
    Plain,
    Typed,
}

fn spelling_of(case: &Value) -> Spelling {
    match case.get("key_values").and_then(Value::as_str) {
        None => Spelling::Plain,
        Some("typed") => Spelling::Typed,
        Some(other) => panic!("unknown key_values spelling {other}"),
    }
}

/// A recorded `str` (a typed value's text or a dict key): a JSON string, or `{"code_points":
/// [n, ...]}` for a `str` holding a surrogate code point (U+D800..U+DFFF), which JSON text holds
/// only as a lone `\uXXXX` escape that serde_json does not read; `Err` where the carrier cannot
/// hold it (a point past U+10FFFF, which no `str` holds).
fn recorded_str(v: &Value) -> Result<PyStr, String> {
    if let Some(s) = v.as_str() {
        return Ok(PyStr::new(s));
    }
    let points = v["code_points"]
        .as_array()
        .expect("a str or its code points");
    PyStr::from_code_points(
        points
            .iter()
            .map(|n| u32::try_from(n.as_u64().expect("a code point")).expect("a code point")),
    )
    .ok_or_else(|| format!("no str has the code points {points:?}"))
}

/// A recorded decoded value as the oracle held it.
fn recorded_value(v: &Value, spelling: Spelling) -> Result<PyJson, String> {
    let each = |items: &Value| -> Result<Vec<PyJson>, String> {
        items
            .as_array()
            .expect("an array")
            .iter()
            .map(|item| recorded_value(item, spelling))
            .collect()
    };
    Ok(match (spelling, v) {
        (_, Value::Object(map)) if ["bits", "int", "bool"].iter().any(|t| map.contains_key(*t)) => {
            PyJson::Num(recorded(v))
        }
        (Spelling::Plain, Value::Array(_)) => PyJson::List(each(v)?),
        (Spelling::Plain, Value::Object(map)) => PyJson::Dict(
            map.iter()
                .map(|(key, item)| Ok((PyStr::new(key), recorded_value(item, spelling)?)))
                .collect::<Result<_, String>>()?,
        ),
        (Spelling::Plain, other) => pj(other.clone()),
        (Spelling::Typed, Value::Object(map)) if map.contains_key("none") => PyJson::None,
        (Spelling::Typed, Value::Object(map)) if map.contains_key("str") => {
            PyJson::str(text(&map["str"]))
        }
        (Spelling::Typed, Value::Object(map)) if map.contains_key("code_points") => {
            PyJson::Str(recorded_str(v)?)
        }
        (Spelling::Typed, Value::Object(map)) if map.contains_key("list") => {
            PyJson::List(each(&map["list"])?)
        }
        (Spelling::Typed, Value::Object(map)) if map.contains_key("dict") => PyJson::Dict(
            map["dict"]
                .as_array()
                .expect("dict entries")
                .iter()
                .map(|entry| {
                    Ok((
                        recorded_str(&entry[0])?,
                        recorded_value(&entry[1], spelling)?,
                    ))
                })
                .collect::<Result<_, String>>()?,
        ),
        (Spelling::Typed, other) => panic!("not a typed value: {other}"),
    })
}

/// Python's `repr` of a finite float (the shortest digits that round-trip; positional for a
/// decimal exponent in -4..16, with `.0` on an integral value, else `d.ddde+XX`), the text
/// `json.dumps` writes.
fn py_float_repr(x: f64) -> String {
    let scientific = format!("{x:e}");
    let (mantissa, exponent) = scientific.split_once('e').expect("an exponent");
    let exponent: i32 = exponent.parse().expect("exponent digits");
    let (sign, mantissa) = mantissa
        .strip_prefix('-')
        .map_or(("", mantissa), |rest| ("-", rest));
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let body = if (-4..16).contains(&exponent) {
        let point = exponent + 1;
        if point <= 0 {
            format!("0.{}{digits}", "0".repeat(point.unsigned_abs() as usize))
        } else if point as usize >= digits.len() {
            format!("{digits}{}.0", "0".repeat(point as usize - digits.len()))
        } else {
            format!(
                "{}.{}",
                &digits[..point as usize],
                &digits[point as usize..]
            )
        }
    } else {
        let lead = if digits.len() > 1 {
            format!("{}.{}", &digits[..1], &digits[1..])
        } else {
            digits
        };
        let sign = if exponent < 0 { '-' } else { '+' };
        format!("{lead}e{sign}{:02}", exponent.unsigned_abs())
    };
    format!("{sign}{body}")
}

/// A `str` as the probe spells it: its text, or `{"code_points": [...]}` for one holding a
/// surrogate code point.
fn str_doc(s: &PyStr) -> Value {
    s.to_text()
        .map_or_else(|| json!({ "code_points": s.code_points() }), Value::String)
}

/// A plain-spelled dict key (the earlier records hold no surrogate; one would show as its
/// `Debug` text and mismatch).
fn plain_key(key: &PyStr) -> String {
    key.to_text().unwrap_or_else(|| format!("{key:?}"))
}

/// A decoded value in the case's spelling (number tags in [`tag`]'s form, as [`normalized`]
/// writes the fixture's).
fn value_doc(v: &PyJson, spelling: Spelling) -> Value {
    let each = |items: &[PyJson]| -> Vec<Value> {
        items.iter().map(|item| value_doc(item, spelling)).collect()
    };
    match (spelling, v) {
        (Spelling::Plain, PyJson::None) => Value::Null,
        (Spelling::Plain, PyJson::Num(PyReal::Bool(b))) => json!(b),
        (Spelling::Plain, PyJson::Num(n @ PyReal::Int { .. })) => {
            serde_json::from_str(&int_text(n).expect("an int")).expect("int digits")
        }
        (Spelling::Plain, PyJson::Num(PyReal::Float(x))) if x.is_finite() => {
            serde_json::from_str(&py_float_repr(*x)).expect("a float literal")
        }
        (Spelling::Plain, PyJson::Num(n)) | (Spelling::Typed, PyJson::Num(n)) => tag(n),
        (Spelling::Plain, PyJson::Str(s)) => str_doc(s),
        (Spelling::Plain, PyJson::List(items)) => Value::Array(each(items)),
        (Spelling::Plain, PyJson::Dict(entries)) => Value::Object(
            entries
                .iter()
                .map(|(key, item)| (plain_key(key), value_doc(item, spelling)))
                .collect(),
        ),
        (Spelling::Typed, PyJson::None) => json!({"none": true}),
        (Spelling::Typed, PyJson::Str(s)) => match s.to_text() {
            Some(text) => json!({ "str": text }),
            None => str_doc(s),
        },
        (Spelling::Typed, PyJson::List(items)) => json!({ "list": each(items) }),
        (Spelling::Typed, PyJson::Dict(entries)) => json!({
            "dict": entries
                .iter()
                .map(|(key, item)| json!([str_doc(key), value_doc(item, spelling)]))
                .collect::<Vec<_>>()
        }),
    }
}

fn recorded_key(spec: &Value, spelling: Spelling) -> Result<Option<RuntimeBatchKey>, String> {
    if spec.is_null() {
        return Ok(None);
    }
    let value = |name: &str| recorded_value(&spec[name], spelling);
    Ok(Some(RuntimeBatchKey {
        speculative_role: spec.get("speculative_role").map_or("NONE", text).to_owned(),
        speculative_width: spec.get("speculative_width").map_or(0, int_field),
        ..RuntimeBatchKey::new(
            value("deployment_id")?,
            value("deployment_epoch")?,
            value("model_commit")?,
            value("manifest_digest")?,
            value("placement_id")?,
            value("assignment_id")?,
            value("stage_signature")?,
            value("load_proof_digest")?,
            value("runtime_backend")?,
            text(&spec["phase"]),
            value("hidden_size")?,
            value("activation_bytes")?,
            carried(&spec["token_span"])?,
        )
    }))
}

fn key_doc(k: Option<&RuntimeBatchKey>, spelling: Spelling) -> Value {
    let v = |value: &PyJson| value_doc(value, spelling);
    k.map_or(Value::Null, |k| {
        json!({
            "deployment_id": v(&k.deployment_id), "deployment_epoch": v(&k.deployment_epoch),
            "model_commit": v(&k.model_commit), "manifest_digest": v(&k.manifest_digest),
            "placement_id": v(&k.placement_id), "assignment_id": v(&k.assignment_id),
            "stage_signature": v(&k.stage_signature),
            "load_proof_digest": v(&k.load_proof_digest),
            "runtime_backend": v(&k.runtime_backend), "phase": k.phase,
            "hidden_size": v(&k.hidden_size), "activation_bytes": v(&k.activation_bytes),
            "token_span": k.token_span.spelled(), "speculative_role": k.speculative_role,
            "speculative_width": k.speculative_width,
        })
    })
}

fn recorded_decision(spec: &Value) -> Option<BatchDecision> {
    (!spec.is_null()).then(|| BatchDecision {
        action: text(&spec["action"]).to_owned(),
        phase: text(&spec["phase"]).to_owned(),
        batch_size: int_field(&spec["batch_size"]),
        target_items: int_field(&spec["target_items"]),
        available_items: int_field(&spec["available_items"]),
        predicted_payload_bytes: int_field(&spec["predicted_payload_bytes"]),
        predicted_transfer_ms: recorded_f64(&spec["predicted_transfer_ms"]),
        ready_at: recorded_f64(&spec["ready_at"]),
        reason: text(&spec["reason"]).to_owned(),
    })
}

fn decision_doc(d: Option<&BatchDecision>) -> Value {
    d.map_or(Value::Null, |d| {
        json!({
            "action": d.action, "phase": d.phase, "batch_size": d.batch_size,
            "target_items": d.target_items, "available_items": d.available_items,
            "predicted_payload_bytes": d.predicted_payload_bytes,
            "predicted_transfer_ms": tag_f64(d.predicted_transfer_ms),
            "ready_at": tag_f64(d.ready_at), "reason": d.reason,
        })
    })
}

fn recorded_item(spec: &Value, spelling: Spelling) -> Result<HopWorkItem, String> {
    let mut item = HopWorkItem::new(
        text(&spec["request_id"]),
        text(&spec["path_id"]),
        carried(&spec["path_attempt"])?,
        text(&spec["phase"]),
        carried(&spec["token_index"])?,
        carried(&spec["hop_index"])?,
        recorded_value(&spec["placement_id"], spelling)?,
        text(&spec["qos_class"]),
        recorded_f64(&spec["deficit_ratio"]),
        recorded_f64(&spec["enqueued_at"]),
        text(&spec["idempotency_key"]),
        recorded_payload(&spec["payload"]),
    );
    if let Some(v) = spec.get("prefill_chunk_token_count") {
        item.prefill_chunk_token_count = carried(v)?;
    }
    if let Some(v) = spec.get("batch_key") {
        item.batch_key = recorded_key(v, spelling)?;
    }
    if let Some(v) = spec.get("deadline_at") {
        item.deadline_at = recorded_f64(v);
    }
    if let Some(v) = spec.get("position") {
        item.position = carried(v)?;
    }
    if let Some(v) = spec.get("emits_token") {
        item.emits_token = v.as_bool().expect("a bool");
    }
    if let Some(v) = spec.get("terminal") {
        item.terminal = v.as_bool().expect("a bool");
    }
    if let Some(v) = spec.get("lease_expires_at") {
        item.lease_expires_at = carried(v)?;
    }
    Ok(item)
}

fn item_doc(item: &HopWorkItem, spelling: Spelling) -> Value {
    json!({
        "request_id": item.request_id, "path_id": item.path_id,
        "path_attempt": item.path_attempt.spelled(), "phase": item.phase,
        "token_index": item.token_index.spelled(), "hop_index": item.hop_index.spelled(),
        "placement_id": value_doc(&item.placement_id, spelling), "qos_class": item.qos_class,
        "deficit_ratio": tag_f64(item.deficit_ratio), "enqueued_at": tag_f64(item.enqueued_at),
        "idempotency_key": item.idempotency_key, "payload": payload_doc(&item.payload),
        "prefill_chunk_token_count": item.prefill_chunk_token_count.spelled(),
        "batch_key": key_doc(item.batch_key.as_ref(), spelling),
        "deadline_at": tag_f64(item.deadline_at), "position": item.position.spelled(),
        "emits_token": item.emits_token, "terminal": item.terminal,
        "lease_expires_at": item.lease_expires_at.spelled(),
    })
}

fn batch_doc(b: &RuntimeBatch, spelling: Spelling) -> Value {
    json!({
        "compatibility_key": key_doc(b.compatibility_key(), spelling),
        "items": b.items().iter().map(|i| item_doc(i, spelling)).collect::<Vec<_>>(),
        "decision": decision_doc(b.decision()),
    })
}

/// The oracle's exception for each refusal (test-owned: class and `str(exc)`).
fn raised_doc(e: &SchedulerError) -> Value {
    let message = |m: &str| -> String {
        match m {
            SLICE_INDEX_NOT_INT => {
                "slice indices must be integers or None or have an __index__ method".to_owned()
            }
            INT_TOO_LARGE_FOR_FLOAT => "int too large to convert to float".to_owned(),
            other => other.to_owned(),
        }
    };
    match e {
        SchedulerError::Duplicate { idempotency_key } => {
            json!({"type": "DuplicateHopError", "message": idempotency_key})
        }
        SchedulerError::Backpressure {
            reason,
            retry_after_seconds,
        } => json!({
            "type": "BackpressureError", "reason": reason,
            "retry_after": tag(retry_after_seconds), "message": format!("backpressure:{reason}"),
        }),
        SchedulerError::Index(code) => json!({"type": "IndexError", "message": message(code)}),
        SchedulerError::Type(code) => json!({"type": "TypeError", "message": message(code)}),
        SchedulerError::Overflow(code) => {
            json!({"type": "OverflowError", "message": message(code)})
        }
        SchedulerError::Contract(code) => json!({"type": "ValueError", "message": code}),
        SchedulerError::Unsupported(code) => json!({"type": "Unsupported", "message": code}),
    }
}

fn outcome(r: Result<Value, Value>) -> Value {
    match r {
        Ok(v) => json!({"ok": v}),
        Err(e) => json!({"raised": e}),
    }
}

fn now_of(op: &Value) -> f64 {
    recorded_f64(&op["now"])
}

fn optional_number(v: Option<&Value>) -> Option<PyReal> {
    v.filter(|v| !v.is_null()).map(recorded)
}

fn state_doc(s: &HopScheduler, spelling: Spelling) -> Value {
    let depths = match s.placement_depths() {
        Ok(map) => json!(
            map.entries()
                .iter()
                .map(|(id, n)| json!([value_doc(id, spelling), n]))
                .collect::<Vec<_>>()
        ),
        Err(e) => raised_doc(&e),
    };
    json!({
        "depth": s.queue_depth(), "bytes": s.queued_payload_bytes(),
        "seen": s.seen_key_count(), "path_ids": s.queued_path_ids(),
        "placement_depths": depths,
    })
}

/// One op's outcome; an op whose recorded input this carrier cannot hold is reported as
/// `{"unrepresentable": why}`.
fn run_op(s: &mut HopScheduler, op: &Value, spelling: Spelling) -> Value {
    replay_op(s, op, spelling).unwrap_or_else(|why| json!({ "unrepresentable": why }))
}

fn replay_op(s: &mut HopScheduler, op: &Value, spelling: Spelling) -> Result<Value, String> {
    let err = |e: SchedulerError| raised_doc(&e);
    let result = match text(&op["op"]) {
        "enqueue" => s
            .enqueue(recorded_item(&op["item"], spelling)?)
            .map(|()| Value::Null)
            .map_err(err),
        "pop_next" => s
            .pop_next(now_of(op))
            .map(|i| item_doc(&i, spelling))
            .map_err(err),
        "pop_batch" => {
            let maximum_items = optional_number(op.get("maximum_items"));
            let maximum_bytes = optional_number(op.get("maximum_bytes"));
            s.pop_batch(
                now_of(op),
                maximum_items.as_ref(),
                maximum_bytes.as_ref(),
                op.get("decision").and_then(recorded_decision),
            )
            .map(|b| batch_doc(&b, spelling))
            .map_err(err)
        }
        "batch_candidates" => s
            .batch_candidates(now_of(op))
            .map(|items| {
                json!(
                    items
                        .iter()
                        .map(|i| item_doc(i, spelling))
                        .collect::<Vec<_>>()
                )
            })
            .map_err(err),
        "take" => Ok(s
            .take(&recorded_item(&op["item"], spelling)?)
            .map_or(Value::Null, |i| item_doc(&i, spelling))),
        "withdraw" => Ok(json!(s.withdraw(&recorded_item(&op["item"], spelling)?))),
        "release_path" => {
            s.release_path(text(&op["path_id"]));
            Ok(Value::Null)
        }
        "effective_priority" => s
            .effective_priority(&recorded_item(&op["item"], spelling)?, now_of(op))
            .map(tag_f64)
            .map_err(err),
        "state" => Ok(state_doc(s, spelling)),
        "hop_identity" => {
            let (request, path, attempt, phase, token, hop) =
                hop_identity(&recorded_item(&op["item"], spelling)?);
            Ok(json!([
                request,
                path,
                attempt.spelled(),
                phase,
                token.spelled(),
                hop.spelled()
            ]))
        }
        "runtime_batch" => RuntimeBatch::new(
            recorded_key(&op["key"], spelling)?,
            op["items"]
                .as_array()
                .expect("items")
                .iter()
                .map(|i| recorded_item(i, spelling))
                .collect::<Result<_, _>>()?,
            op.get("decision").and_then(recorded_decision),
        )
        .map(|b| batch_doc(&b, spelling))
        .map_err(|e| json!({"type": "ValueError", "message": e.0})),
        other => panic!("unknown op {other}"),
    };
    Ok(outcome(result))
}

/// A RouterConfig field by its dataclass name (contracts.py:141-179).
fn config_field<'a>(c: &'a mut RouterConfig, name: &str) -> &'a mut PyReal {
    match name {
        "interactive_base_priority" => &mut c.interactive_base_priority,
        "batch_base_priority" => &mut c.batch_base_priority,
        "aging_priority_per_second" => &mut c.aging_priority_per_second,
        "batch_aging_multiplier" => &mut c.batch_aging_multiplier,
        "maximum_deficit_boost" => &mut c.maximum_deficit_boost,
        "idempotency_retention_seconds" => &mut c.idempotency_retention_seconds,
        "maximum_idempotency_entries" => &mut c.maximum_idempotency_entries,
        "maximum_pending_hops" => &mut c.maximum_pending_hops,
        "maximum_pending_bytes" => &mut c.maximum_pending_bytes,
        "backpressure_retry_after_seconds" => &mut c.backpressure_retry_after_seconds,
        "maximum_runtime_batch_size" => &mut c.maximum_runtime_batch_size,
        other => panic!("unmapped RouterConfig field {other}"),
    }
}

#[test]
fn m16_hop_scheduler_python_value_domain_parity() {
    let fixture: Value = serde_json::from_str(ORACLE).expect("the oracle fixture");
    let provenance = &fixture["provenance"];
    assert_eq!(
        provenance["oracle_commit"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    assert_eq!(provenance["python"], "3.14.2");
    let cases = fixture["cases"].as_array().expect("cases");
    assert!(cases.len() >= 20);
    assert_eq!(provenance["case_count"], cases.len());
    let mut ops = 0;
    for case in cases {
        let mut c = config();
        for (name, value) in case["config"].as_object().expect("config") {
            *config_field(&mut c, name) = recorded(value);
        }
        let mut s = scheduler(c);
        let spelling = spelling_of(case);
        let replay: Vec<Value> = case["ops"]
            .as_array()
            .expect("ops")
            .iter()
            .map(|op| run_op(&mut s, op, spelling))
            .collect();
        ops += replay.len();
        assert_eq!(
            Value::Array(replay),
            normalized(&case["results"]),
            "case {}: {}",
            case["name"],
            case["note"]
        );
    }
    assert_eq!(provenance["op_count"], ops);
}
