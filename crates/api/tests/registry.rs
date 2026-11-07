// SPDX-License-Identifier: AGPL-3.0-only
//! Registry, bounded history and generation rebinding against
//! mycelium_request_gateway/service.py:440-620,1024-1453 @ f4e366e9.
use api::registry::{AdmissionError, Config, LastEventId, Pull, Registry, Subscription};
use serde_json::{Value, json};

const TOKEN: &str = "owner-token-owner-token-owner-token";
fn reg(buffer: usize, sessions: usize) -> Registry {
    Registry::new(Config::new(buffer, sessions, 0.05).unwrap())
}
fn submit(r: &mut Registry, id: &str, now: f64) -> Result<(), AdmissionError> {
    r.submit(id, Some(TOKEN), now, &mut Vec::new())
}
fn attach(
    r: &mut Registry,
    id: &str,
    last: Option<LastEventId>,
) -> Result<Subscription, AdmissionError> {
    r.subscribe(id, last, Some(TOKEN), 1.0, &mut |_, _, _| Ok(true))
}
fn seq(p: Pull) -> i64 {
    match p {
        Pull::Event(e) => e.sequence,
        other => panic!("{other:?}"),
    }
}
fn code<T: std::fmt::Debug>(r: Result<T, AdmissionError>) -> &'static str {
    r.unwrap_err().0
}
#[test]
fn construction_ranges_and_submit_admission_order() {
    assert_eq!(code(Config::new(1, 4, 0.05)), "invalid_max_buffered_events");
    assert_eq!(
        code(Config::new(65_537, 4, 0.05)),
        "invalid_max_buffered_events"
    );
    assert_eq!(code(Config::new(2, 0, 0.05)), "invalid_max_sessions");
    let mut r = reg(4, 8);
    assert_eq!(
        code(r.submit("a", Some("short"), 0.0, &mut vec![])),
        "invalid_session_token"
    );
    assert_eq!(
        code(r.submit("a", Some(&"x".repeat(257)), 0.0, &mut vec![])),
        "invalid_session_token"
    );
    for bad in ["", "-lead", "has space", &"i".repeat(129)] {
        assert_eq!(
            code(submit(&mut r, bad, 0.0)),
            "invalid_request_id",
            "{bad:?}"
        );
    }
    submit(&mut r, "req.1~a-b", 0.0).unwrap();
    assert_eq!(
        code(submit(&mut r, "req.1~a-b", 0.0)),
        "duplicate_request_id"
    );
    r.close();
    assert_eq!(code(submit(&mut r, "other", 0.0)), "gateway_closed");
    // The accepted event is sequence 0 and counts as buffered.
    assert_eq!(r.buffered_event_count("req.1~a-b").unwrap(), 1);
    assert_eq!(code(r.buffered_event_count("missing")), "unknown_request");
    assert_eq!(code(r.buffered_event_count("")), "unknown_request");
}
#[test]
fn owner_token_is_digest_checked_and_unowned_sessions_accept_any_caller() {
    let mut r = reg(4, 8);
    submit(&mut r, "owned", 0.0).unwrap();
    r.submit("open", None, 0.0, &mut vec![]).unwrap();
    for token in [None, Some("wrong-token-wrong-token-wrong-token")] {
        assert_eq!(
            code(r.subscribe("owned", None, token, 1.0, &mut |_, _, _| Ok(true))),
            "session_owner_mismatch"
        );
        assert_eq!(code(r.cancel("owned", token)), "session_owner_mismatch");
    }
    assert!(
        r.subscribe("open", None, Some("anything"), 1.0, &mut |_, _, _| Ok(true))
            .is_ok()
    );
    assert!(r.cancel("open", None).unwrap());
}
#[test]
fn eviction_prefers_unattached_terminals_then_abandoned_and_protects_fresh_and_attached() {
    let mut r = reg(4, 2);
    submit(&mut r, "a", 0.0).unwrap();
    submit(&mut r, "b", 0.0).unwrap();
    // Both in flight, never subscribed, inside the grace: capacity exhausted, nothing cancelled.
    let mut cancelled = vec![];
    assert_eq!(
        code(r.submit("c", Some(TOKEN), 0.01, &mut cancelled)),
        "gateway_capacity_exhausted"
    );
    assert!(cancelled.is_empty() && r.len() == 2);
    // Past the grace the oldest never-subscribed request is evicted and reported for cancellation.
    r.submit("c", Some(TOKEN), 0.05, &mut cancelled).unwrap();
    assert_eq!(cancelled, ["a"]);
    // An attached non-terminal is never evicted, even long after the grace.
    let mut sub = attach(&mut r, "b", None).unwrap();
    attach(&mut r, "c", None).unwrap();
    assert_eq!(
        code(r.submit("d", Some(TOKEN), 99.0, &mut vec![])),
        "gateway_capacity_exhausted"
    );
    // Detached after subscribing = abandoned: evicted at once (no grace).
    r.close_subscription(&mut sub).unwrap();
    let mut cancelled = vec![];
    r.submit("d", Some(TOKEN), 0.0, &mut cancelled).unwrap();
    assert_eq!(cancelled, ["b"]);
}
#[test]
fn terminal_eviction_rules_cancelled_needs_worker_done_and_attached_is_kept() {
    let mut r = reg(4, 1);
    submit(&mut r, "a", 0.0).unwrap();
    r.append_terminal("a", "cancelled", Value::Null, 0.0)
        .unwrap();
    assert_eq!(code(submit(&mut r, "b", 0.0)), "gateway_capacity_exhausted"); // cancelled, worker still running
    r.mark_worker_done("a").unwrap();
    submit(&mut r, "b", 0.0).unwrap();
    r.append_terminal("b", "completed", json!({}), 0.0).unwrap();
    let mut sub = attach(&mut r, "b", None).unwrap();
    assert_eq!(code(submit(&mut r, "c", 0.0)), "gateway_capacity_exhausted"); // completed but attached
    r.close_subscription(&mut sub).unwrap();
    submit(&mut r, "c", 0.0).unwrap(); // completed + detached: evictable without worker_done
    assert_eq!(code(r.buffered_event_count("b")), "unknown_request");
}
#[test]
fn rebinding_resumes_after_the_acknowledged_cursor_and_bumps_the_generation() {
    let mut r = reg(8, 4);
    submit(&mut r, "r", 0.0).unwrap();
    for n in 0..3 {
        r.append("r", "token", json!({"token_index": n}), 0.0)
            .unwrap();
    }
    let mut first = attach(&mut r, "r", None).unwrap();
    assert_eq!(first.publisher_generation, 1);
    assert_eq!(code(attach(&mut r, "r", None)), "stream_already_attached");
    // Delivery is not acknowledgement: the same event repeats until acked.
    assert_eq!(seq(r.next_event(&mut first).unwrap()), 0);
    assert_eq!(seq(r.next_event(&mut first).unwrap()), 0);
    assert_eq!(code(r.ack(&mut first, 1, 1.0)), "invalid_stream_ack"); // not delivered yet
    r.ack(&mut first, 0, 2.0).unwrap();
    assert_eq!(code(r.ack(&mut first, 0, 2.0)), "invalid_stream_ack"); // not above the cursor
    assert_eq!(seq(r.next_event(&mut first).unwrap()), 1);
    r.ack(&mut first, 1, 3.0).unwrap();
    assert_eq!(r.last_ack_at("r").unwrap(), Some(3.0));
    r.close_subscription(&mut first).unwrap();
    assert_eq!(code(r.next_event(&mut first)), "stream_closed");
    // Rebind with the previous generation and the last acknowledged cursor.
    let mut second = attach(&mut r, "r", Some(LastEventId::Bound(1, 1))).unwrap();
    assert_eq!(second.publisher_generation, 2);
    assert_eq!(r.publisher_generation("r").unwrap(), 2);
    assert_eq!(r.discarded_through("r").unwrap(), 0);
    assert_eq!(r.acknowledged_through("r").unwrap(), 1);
    assert_eq!(r.buffered_event_count("r").unwrap(), 3); // events 1..=3 kept; 0 discarded
    let e = r.next_event(&mut second).unwrap();
    assert_eq!((seq(e), second.publisher_generation), (2, 2));
    // The old subscription can no longer acknowledge anything (generation moved on).
    assert_eq!(code(r.ack(&mut first, 2, 4.0)), "stream_closed");
}
#[test]
fn rebinding_refuses_stale_generations_bad_cursors_and_reclaimed_history() {
    let mut r = reg(8, 4);
    submit(&mut r, "r", 0.0).unwrap();
    for n in 0..4 {
        r.append("r", "token", json!({"token_index": n}), 0.0)
            .unwrap();
    }
    let mut s1 = attach(&mut r, "r", None).unwrap();
    r.close_subscription(&mut s1).unwrap();
    assert_eq!(
        code(attach(&mut r, "r", Some(LastEventId::Bound(0, 0)))),
        "stale_publisher_generation"
    );
    assert_eq!(
        code(attach(&mut r, "r", Some(LastEventId::Bound(5, 0)))),
        "stale_publisher_generation"
    );
    assert_eq!(
        code(attach(&mut r, "r", Some(LastEventId::Bound(-1, 0)))),
        "invalid_last_event_id"
    );
    assert_eq!(
        code(attach(&mut r, "r", Some(LastEventId::Cursor(-2)))),
        "invalid_last_event_id"
    );
    assert_eq!(
        code(attach(&mut r, "r", Some(LastEventId::Cursor(5)))),
        "invalid_last_event_id"
    );
    // A failed attempt never advanced the generation or claimed the stream.
    assert_eq!(r.publisher_generation("r").unwrap(), 1);
    let mut s2 = attach(&mut r, "r", Some(LastEventId::Cursor(3))).unwrap();
    assert_eq!(r.discarded_through("r").unwrap(), 2);
    assert_eq!(seq(r.next_event(&mut s2).unwrap()), 4);
    r.close_subscription(&mut s2).unwrap();
    // Resuming into the reclaimed prefix fails closed, exactly as the source does.
    assert_eq!(
        code(attach(&mut r, "r", Some(LastEventId::Bound(2, 1)))),
        "resume_cursor_expired"
    );
}
#[test]
fn vanished_consumer_drop_moves_only_the_replay_boundary_never_the_ack_watermark() {
    let mut r = reg(4, 4);
    submit(&mut r, "r", 0.0).unwrap();
    for n in 0..3 {
        r.append("r", "token", json!({"token_index": n}), 0.0)
            .unwrap();
    }
    assert_eq!(
        code(r.append("r", "token", json!({}), 0.0)),
        "event_backpressure"
    );
    r.drop_oldest_unacknowledged("r").unwrap();
    assert_eq!(
        (
            r.discarded_through("r").unwrap(),
            r.acknowledged_through("r").unwrap()
        ),
        (0, -1)
    );
    assert_eq!(
        code(attach(&mut r, "r", Some(LastEventId::Cursor(-1)))),
        "resume_cursor_expired"
    );
    // A live subscription whose prefix is reclaimed under it fails instead of skipping sequences.
    let mut sub = attach(&mut r, "r", Some(LastEventId::Cursor(0))).unwrap();
    r.drop_oldest_unacknowledged("r").unwrap();
    r.drop_oldest_unacknowledged("r").unwrap();
    assert_eq!(code(r.next_event(&mut sub)), "resume_cursor_expired");
}
#[test]
fn terminal_trims_only_acknowledged_history_and_never_invents_capacity() {
    let mut r = reg(3, 4);
    submit(&mut r, "r", 0.0).unwrap();
    r.append("r", "token", json!({"token_index": 0}), 0.0)
        .unwrap();
    r.append("r", "token", json!({"token_index": 1}), 0.0)
        .unwrap();
    // Full and nothing acknowledged: the terminal cannot be placed (invariant violation, loud).
    assert_eq!(
        code(r.append_terminal("r", "completed", json!({}), 0.0)),
        "terminal_event_capacity_invariant"
    );
    let mut sub = attach(&mut r, "r", None).unwrap();
    for want in 0..2 {
        assert_eq!(seq(r.next_event(&mut sub).unwrap()), want);
        r.ack(&mut sub, want, 1.0).unwrap();
    }
    r.append_terminal("r", "completed", json!({}), 0.0).unwrap();
    assert_eq!(r.discarded_through("r").unwrap(), 0);
    // The terminal is final: a later terminal or event is ignored/refused, and the stream ends cleanly.
    r.append_terminal("r", "failed", json!({"code": "x"}), 0.0)
        .unwrap();
    assert_eq!(
        code(r.append("r", "token", json!({}), 0.0)),
        "terminal_already_published"
    );
    assert_eq!(seq(r.next_event(&mut sub).unwrap()), 2);
    r.ack(&mut sub, 2, 1.0).unwrap();
    let last = match r.next_event(&mut sub).unwrap() {
        Pull::Event(e) => e,
        other => panic!("{other:?}"),
    };
    assert_eq!((last.sequence, last.kind.as_str()), (3, "completed"));
    r.ack(&mut sub, 3, 1.0).unwrap();
    assert_eq!(r.next_event(&mut sub).unwrap(), Pull::End);
    assert!(!r.cancel("r", Some(TOKEN)).unwrap()); // cancel cannot reverse a completed terminal
}
#[test]
fn generation_sync_failure_detaches_cancels_and_refuses() {
    for outcome in [Ok(false), Err(())] {
        let mut r = reg(4, 4);
        submit(&mut r, "r", 0.0).unwrap();
        let mut seen = vec![];
        let result = r.subscribe("r", None, Some(TOKEN), 1.0, &mut |id, expected, new| {
            seen.push((id.to_string(), expected, new));
            outcome
        });
        assert_eq!(code(result), "publisher_generation_sync_failed");
        assert_eq!(seen, [("r".to_string(), 0, 1)]);
        // Detached again, but the request is cancelled: a second cancel reports it already stopping.
        assert!(!r.cancel("r", Some(TOKEN)).unwrap());
        assert!(attach(&mut r, "r", Some(LastEventId::Bound(1, -1))).is_ok());
    }
}
#[test]
fn cancel_is_one_shot_and_unknown_requests_are_refused() {
    let mut r = reg(4, 4);
    submit(&mut r, "r", 0.0).unwrap();
    assert!(r.cancel("r", Some(TOKEN)).unwrap());
    assert!(!r.cancel("r", Some(TOKEN)).unwrap());
    assert_eq!(code(r.cancel("nope", Some(TOKEN))), "unknown_request");
    assert_eq!(r.maximum_observed_buffered_events("r").unwrap(), 1);
}

/// Round 6: a signed adapter whose immutable grant already carries generation `g` starts the
/// session at `g - 1`, so its first stream is generation `g` (service.py: session starts at 0 and
/// the signed identity at 1, i.e. the real flow is `g = 1`, `initial = 0`).
#[test]
fn session_may_start_at_the_signed_identity_generation_minus_one() {
    let mut r = reg(4, 8);
    r.submit_at_generation("a", Some(TOKEN), 0.0, 2, &mut Vec::new())
        .unwrap();
    assert_eq!(r.publisher_generation("a").unwrap(), 2);
    // a bound cursor must name the session's current generation
    assert_eq!(
        code(attach(&mut r, "a", Some(LastEventId::Bound(0, 0)))),
        "stale_publisher_generation"
    );
    let mut seen = Vec::new();
    let first = r
        .subscribe("a", None, Some(TOKEN), 1.0, &mut |id, expected, new| {
            seen.push((id.to_string(), expected, new));
            Ok(true)
        })
        .unwrap();
    assert_eq!(first.publisher_generation, 3);
    assert_eq!(seen, [("a".to_string(), 2, 3)]);
    // the default admission is unchanged
    submit(&mut r, "b", 0.0).unwrap();
    assert_eq!(r.publisher_generation("b").unwrap(), 0);
}

/// service.py:1286-1297 `_trim_acknowledged_replay_locked`: only acknowledged prefix events are
/// removed, the replay boundary follows, and the unacknowledged tail is never touched.
#[test]
fn trim_acknowledged_removes_only_the_acknowledged_prefix_down_to_the_target() {
    let mut r = reg(8, 8);
    submit(&mut r, "a", 0.0).unwrap();
    for n in 1..=3 {
        r.append("a", "token", json!({"token_index": n}), 0.0)
            .unwrap();
    }
    let mut s = attach(&mut r, "a", None).unwrap();
    for expected in 0..=2 {
        assert_eq!(seq(r.next_event(&mut s).unwrap()), expected);
        r.ack(&mut s, expected, 1.0).unwrap();
    }
    assert_eq!(r.buffered_event_count("a").unwrap(), 4);
    // target 3: one acknowledged event goes, the rest of the prefix stays as replay history
    r.trim_acknowledged("a", 3).unwrap();
    assert_eq!(r.buffered_event_count("a").unwrap(), 3);
    assert_eq!(r.discarded_through("a").unwrap(), 0);
    // target 0: everything acknowledged goes, but sequence 3 was never acknowledged
    r.trim_acknowledged("a", 0).unwrap();
    assert_eq!(r.buffered_event_count("a").unwrap(), 1);
    assert_eq!(r.discarded_through("a").unwrap(), 2);
    assert_eq!(r.acknowledged_through("a").unwrap(), 2);
    assert_eq!(seq(r.next_event(&mut s).unwrap()), 3);
    r.close_subscription(&mut s).unwrap();
    assert_eq!(
        code(attach(&mut r, "a", Some(LastEventId::Bound(1, 1)))),
        "resume_cursor_expired"
    );
    assert_eq!(code(r.trim_acknowledged("missing", 0)), "unknown_request");
}
