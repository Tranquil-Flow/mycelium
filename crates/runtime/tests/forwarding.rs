// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-CHAT-13 (signed retirement unblocks control only after the separate native disposition; a verified ack reaches native without closing the stream; persistence/clear failure stays retryable).
//!
use budgets::{BudgetId, Entries};
use protocol::{
    ContractError, TimeLimits,
    progress::{self, ExpectedProgress, VerifiedDecision},
};
use runtime::forwarding::*;
use serde_json::Value;
fn fixture(cancelled: bool) -> Value {
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../../protocol/fixtures/f4-progress-vectors.json"
    ))
    .unwrap();
    rows.into_iter()
        .find(|r| (r["name"] == progress::CANCELLED) == cancelled)
        .unwrap()
}
fn decision(row: &Value) -> VerifiedDecision {
    let c = &row["context"];
    progress::verify_response(
        &row["value"],
        &ExpectedProgress {
            owner_key: &c["key"],
            request_id: c["request_id"].as_str().unwrap(),
            swarm_id: c["swarm_id"].as_str().unwrap(),
            owner_node_id: c["owner_node_id"].as_str().unwrap(),
            binding: &c["binding"],
            command_id: c["command_id"].as_str().unwrap(),
            receipt: &c["receipt"],
            now: c["now"].as_f64().unwrap(),
        },
        TimeLimits {
            max_ttl_seconds: BudgetId::ControlTtl.span(Entries::ZERO).unwrap().millis() as f64
                / 1000.0,
            clock_skew_seconds: BudgetId::ControlClockSkew
                .span(Entries::ZERO)
                .unwrap()
                .millis() as f64
                / 1000.0,
        },
    )
    .unwrap()
}
#[derive(Default)]
struct Effects {
    events: Vec<&'static str>,
    fail: Option<&'static str>,
}
impl Effects {
    fn call(&mut self, phase: &'static str) -> Result<(), ContractError> {
        self.events.push(phase);
        if self.fail == Some(phase) {
            Err(ContractError("injected_failure"))
        } else {
            Ok(())
        }
    }
}
impl ForwardEffects for Effects {
    fn persist(&mut self, _: &Value) -> Result<(), ContractError> {
        self.call("persist")
    }
    fn stop_reader(&mut self) -> Result<(), ContractError> {
        self.call("stop_reader")
    }
    fn clear_outbound(&mut self) -> Result<(), ContractError> {
        self.call("clear_outbound")
    }
    fn native_ack(&mut self, ack: &Value) -> Result<(), ContractError> {
        assert_eq!(
            ack["statement"]["protocol"],
            progress::ACK,
            "cancelled response reached native eviction"
        );
        self.call("native_ack")
    }
}
#[test]
fn signed_retirement_unblocks_control_only_after_separate_native_disposition() {
    let row = fixture(true);
    let mut f = Forwarding::new("command-1", &row["context"]["receipt"]).unwrap();
    let mut e = Effects::default();
    f.apply(decision(&row), &mut e).unwrap();
    assert_eq!(e.events, vec!["persist", "stop_reader", "clear_outbound"]);
    assert_eq!(f.snapshot()["terminal"], false);
    assert!(!f.snapshot()["pending"].is_null());
    assert!(!f.ready_to_ack(None));
    assert!(!f.ready_to_ack(Some("claimed")));
    assert!(f.ready_to_ack(Some("executed")));
    assert!(f.ready_to_ack(Some("reconciliation_required")));
}
#[test]
fn persistence_and_clear_failure_remain_retryable_without_native_eviction() {
    for phase in ["persist", "stop_reader", "clear_outbound"] {
        let row = fixture(true);
        let mut f = Forwarding::new("command-1", &row["context"]["receipt"]).unwrap();
        let mut e = Effects {
            fail: Some(phase),
            ..Effects::default()
        };
        assert_eq!(
            f.apply(decision(&row), &mut e).unwrap_err().0,
            "injected_failure"
        );
        assert!(!f.ready_to_ack(Some("executed")));
        if phase == "persist" {
            assert!(f.snapshot()["owner_ack"].is_null());
        }
        e.fail = None;
        f.apply(decision(&row), &mut e).unwrap();
        assert!(f.ready_to_ack(Some("executed")));
        assert!(!e.events.contains(&"native_ack"));
    }
}
#[test]
fn ordinary_verified_ack_reaches_native_without_closing_the_stream() {
    let row = fixture(false);
    let mut f = Forwarding::new("command-1", &row["context"]["receipt"]).unwrap();
    let mut e = Effects::default();
    f.apply(decision(&row), &mut e).unwrap();
    assert_eq!(e.events, vec!["persist", "native_ack", "clear_outbound"]);
    assert!(!f.ready_to_ack(Some("executed")));
}
