// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-CHAT-21 (retry needs the exact cleanup, excludes failed placements, keeps the total cap, per-phase expiry; partial: no live Mac loss); B-CHAT-19 (stale, reordered, overflowed and cancelled outputs never publish).
//!
//! O:F1 O:F3 O:F4 O:F15. Deterministic full-context recovery controls.
use budgets::{BudgetId, ClockDomain, Entries, MonoInstant};
use router::recovery::*;
fn at(t: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(1), t)
}
fn identity(n: u64) -> Identity {
    Identity {
        request: "r".into(),
        deployment: "d".into(),
        epoch: 1,
        topology: 1,
        request_attempt: n,
        path: format!("p{n}"),
        path_attempt: n,
        publisher_generation: 1,
        cancellation_generation: 0,
        command: format!("command-{n}"),
        qualification_digest: format!("sha256:{}", "a".repeat(64)),
        path_digest: format!("sha256:{n:064x}"),
        absolute_deadline_ms: 1_000_000,
    }
}
fn proof(id: &Identity) -> router::cleanup::CleanupProof {
    use router::cleanup::*;
    use serde_json::{Value, json};
    let rows: Value = serde_json::from_str(include_str!(
        "../../protocol/fixtures/continuation-cancel-vectors.json"
    ))
    .unwrap();
    let row = &rows[0];
    let c = &row["context"];
    let p = CleanupPeer {
        node: c["node_id"].as_str().unwrap().into(),
        host: c["host_id"].as_str().unwrap().into(),
        process: c["process_id"].clone(),
        run: c["run_id"].as_str().unwrap().into(),
        deployment: id.deployment.clone(),
        endpoint: c["endpoint_id"].as_str().unwrap().into(),
        key: c["key"].clone(),
        generation: json!(5),
    };
    let mut response: Value = serde_json::from_str(row["envelope_utf8"].as_str().unwrap()).unwrap();
    response["result"]["observation"]["deployment_id"] = json!(id.deployment);
    response["result"]["observation"]["details"]["request_id"] = json!(id.request);
    for (k, v) in id.subject().as_object().unwrap() {
        response["result"]["observation"]["details"]["request_cleanup"][k] = v.clone();
    }
    response["result"]["signature"] =
        protocol::identity::Identity::from_secret("endpoint-phone", &[9; 32])
            .unwrap()
            .sign(&response["result"]["observation"])
            .unwrap();
    let mut round = CleanupRound::new(
        id.subject(),
        vec![p.clone()],
        at(0)
            .checked_add(BudgetId::CancelAndCleanup.span(Entries::ZERO).unwrap())
            .unwrap(),
    )
    .unwrap();
    round.accept(&p.node, &response, || (at(0), true)).unwrap();
    round.finish(|| Ok(()), || (at(0), true)).unwrap()
}
fn setup() -> Recovery {
    Recovery::new(
        Checkpoint {
            source: identity(0),
            prompt: vec![10, 11],
            committed: vec![12, 13],
            output_limit: 3,
            source_placements: vec!["old".into()],
            expires: at(1_000_000),
        },
        at(0),
        Policy::registry().unwrap(),
    )
    .unwrap()
}
fn start(r: &mut Recovery, n: u64, t: u64) {
    r.reserve(identity(n), vec![format!("new{n}")], at(t))
        .unwrap();
    r.qualified(at(t), &identity(0)).unwrap();
    r.fenced(at(t), &identity(0)).unwrap();
}
#[test]
fn recovery_replays_complete_context_and_publishes_only_once_after_fresh_cutover() {
    let mut r = setup();
    start(&mut r, 1, 0);
    assert_eq!(r.replay_context(), vec![10, 11, 12, 13]);
    r.token(&identity(1), 0, 14, at(1)).unwrap();
    r.token(&identity(1), 1, 15, at(2)).unwrap();
    assert!(r.published().is_empty());
    r.executed(&identity(1), at(3)).unwrap();
    assert!(r.commit(&identity(0), false, at(4)).is_err());
    assert!(r.published().is_empty());
    r.cleanup(&identity(1), proof(&identity(1)), at(0)).unwrap();
    r.commit(&identity(0), true, at(5)).unwrap();
    assert_eq!(r.published(), &[(2, 14), (3, 15)]);
    assert_eq!(r.phase(), Phase::Completed);
    assert!(r.commit(&identity(0), true, at(6)).is_err());
    let mut r = setup();
    start(&mut r, 1, 0);
    r.executed(&identity(1), at(0)).unwrap();
    assert!(r.cleanup(&identity(1), proof(&identity(0)), at(0)).is_err());
    r.cleanup(&identity(1), proof(&identity(1)), at(0)).unwrap();
    let expiry = BudgetId::CancelAndCleanup
        .span(Entries::ZERO)
        .unwrap()
        .millis();
    assert!(r.commit(&identity(0), true, at(expiry)).is_err());
    assert!(r.published().is_empty());
}
#[test]
fn retry_needs_exact_cleanup_excludes_failed_placements_and_keeps_total_cap() {
    let mut r = setup();
    start(&mut r, 1, 0);
    r.token(&identity(1), 0, 99, at(1)).unwrap();
    r.fail(Failure::Execution);
    assert!(r.retry(&identity(0), at(2)).is_err());
    assert!(r.cleanup(&identity(2), proof(&identity(2)), at(0)).is_err());
    r.cleanup(&identity(1), proof(&identity(1)), at(0)).unwrap();
    r.retry(&identity(0), at(2)).unwrap();
    assert!(r.reserve(identity(2), vec!["new1".into()], at(3)).is_err());
    assert!(
        r.reserve(identity(1), vec!["unseen".into()], at(3))
            .is_err()
    );
    start(&mut r, 2, 3);
    r.fail(Failure::Execution);
    r.cleanup(&identity(2), proof(&identity(2)), at(0)).unwrap();
    assert!(r.retry(&identity(0), at(4)).is_err());
    assert_eq!(r.attempts(), 2);
    assert!(r.published().is_empty());
    assert_eq!(
        r.total_deadline(),
        at(
            BudgetId::PyMyceliumLiveRouterPortRecoveryphasedeadlinesTotalMsL263
                .span(Entries::ZERO)
                .unwrap()
                .millis()
        )
    );
}
#[test]
fn terminal_refusal_cannot_be_downgraded_to_retryable_execution_failure() {
    for failure in [
        Failure::Cancelled,
        Failure::Timeout,
        Failure::Authority,
        Failure::Output,
        Failure::Cleanup,
    ] {
        let mut r = setup();
        start(&mut r, 1, 0);
        r.fail(failure);
        r.fail(Failure::Execution);
        r.cleanup(&identity(1), proof(&identity(1)), at(0)).unwrap();
        assert!(r.retry(&identity(0), at(1)).is_err(), "{failure:?}");
        let terminal = r.settle_failure(&identity(0), at(1)).unwrap();
        assert_eq!(
            terminal,
            if failure == Failure::Cancelled {
                Phase::Cancelled
            } else {
                Phase::Failed
            }
        );
    }
}
#[test]
fn stale_reordered_overflow_and_cancelled_outputs_never_publish() {
    for fault in 0..4 {
        let mut r = setup();
        start(&mut r, 1, 0);
        match fault {
            0 => assert!(r.token(&identity(0), 0, 14, at(1)).is_err()),
            1 => assert!(r.token(&identity(1), 1, 14, at(1)).is_err()),
            2 => {
                for i in 0..3 {
                    r.token(&identity(1), i, 14, at(1)).unwrap();
                }
                assert!(r.token(&identity(1), 3, 14, at(1)).is_err());
            }
            _ => {
                r.cancel();
                assert!(r.token(&identity(1), 0, 14, at(1)).is_err());
            }
        }
        assert!(r.published().is_empty());
        assert!(r.commit(&identity(0), true, at(2)).is_err());
    }
}
#[test]
fn phase_expiry_clock_changes_and_stale_settlement_refuse_without_renewal() {
    for budget in [
        BudgetId::PyMyceliumLiveRouterPortRecoveryphasedeadlinesQualificationMsL259,
        BudgetId::PyMyceliumLiveRouterPortRecoveryphasedeadlinesPrefillMsL260,
        BudgetId::PyMyceliumLiveRouterPortRecoveryphasedeadlinesProgressMsL261,
        BudgetId::PyMyceliumLiveRouterPortRecoveryphasedeadlinesCutoverMsL262,
    ] {
        let mut r = setup();
        r.reserve(identity(1), vec!["new".into()], at(0)).unwrap();
        let bound = budget.span(Entries::ZERO).unwrap().millis();
        if budget != BudgetId::PyMyceliumLiveRouterPortRecoveryphasedeadlinesQualificationMsL259 {
            r.qualified(at(0), &identity(0)).unwrap();
            r.fenced(at(0), &identity(0)).unwrap();
            if budget == BudgetId::PyMyceliumLiveRouterPortRecoveryphasedeadlinesProgressMsL261 {
                r.token(&identity(1), 0, 14, at(0)).unwrap();
            }
            if budget == BudgetId::PyMyceliumLiveRouterPortRecoveryphasedeadlinesCutoverMsL262 {
                r.executed(&identity(1), at(0)).unwrap();
            }
        }
        assert!(r.check(at(bound)).is_err());
        assert!(r.retry(&identity(0), at(bound)).is_err());
    }
    let mut r = setup();
    start(&mut r, 1, 0);
    assert!(r.check(MonoInstant::new(ClockDomain(2), 0)).is_err());
    let mut r = setup();
    start(&mut r, 1, 0);
    r.executed(&identity(1), at(1)).unwrap();
    r.cleanup(&identity(1), proof(&identity(1)), at(0)).unwrap();
    let mut stale = identity(0);
    stale.cancellation_generation += 1;
    assert!(r.commit(&stale, true, at(2)).is_err());
    assert!(r.published().is_empty());
}
