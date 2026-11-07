// SPDX-License-Identifier: AGPL-3.0-only
use device_harness::harness_budgets::L1;
use device_harness::{Fault, FaultKind, Result, harness_budgets::KeeperLimits, keeper::*};
use serde_json::{Value, json};
use std::collections::VecDeque;
fn limits() -> KeeperLimits {
    KeeperLimits::from_registry(&L1).unwrap()
}
fn status(state: &str) -> Value {
    json!({"swarm":{"swarm_id":"fixture-swarm"},"owner_run":{"state":state,
    "cleanup_unproven":null,"error_code":"rollover_members_changed","rollover":{"state":"failed"}}})
}
struct Fake {
    pages: VecDeque<Result<Value>>,
    state: State,
    events: Vec<String>,
    ages: Vec<Option<u64>>,
    save_error: bool,
    stale_save: bool,
    start_error: bool,
}
impl Fake {
    fn new(pages: Vec<Value>) -> Self {
        Self {
            pages: pages.into_iter().map(Ok).collect(),
            state: State::default(),
            events: vec![],
            ages: vec![Some(0)],
            save_error: false,
            stale_save: false,
            start_error: false,
        }
    }
}
impl Io for Fake {
    fn owner_status(&mut self) -> Result<Value> {
        self.events.push("status".into());
        self.pages.pop_front().expect("unexpected status")
    }
    fn member_clocks(&mut self) -> Result<Vec<MemberClock>> {
        self.events.push("members".into());
        Ok(self
            .ages
            .iter()
            .enumerate()
            .map(|(i, a)| MemberClock {
                peer: format!("fixture-{i}"),
                age_ms: *a,
            })
            .collect())
    }
    fn public_healthy(&mut self) -> Result<bool> {
        Ok(true)
    }
    fn load(&mut self) -> Result<State> {
        self.events.push("load".into());
        Ok(self.state.clone())
    }
    fn save(&mut self, prior: &State, next: &State) -> Result<()> {
        assert_eq!(prior, &self.state);
        self.events.push("save".into());
        if self.save_error {
            return Err(Fault::new("fixture_store_failed", FaultKind::Observation));
        }
        if !self.stale_save {
            self.state = next.clone()
        }
        Ok(())
    }
    fn alert(&mut self, a: Alert) -> Result<()> {
        self.events.push(format!("alert:{}", a.code));
        Ok(())
    }
    fn start(&mut self) -> Result<()> {
        assert!(
            !self.state.recoveries.is_empty(),
            "must durably reserve before POST"
        );
        self.events.push("start".into());
        if self.start_error {
            Err(Fault::new(
                "fixture_start_ambiguous",
                FaultKind::Observation,
            ))
        } else {
            Ok(())
        }
    }
}
#[test]
fn failed_rollover_exact_code_and_explicit_cleanup_proof_only() {
    assert!(recoverable(&status("failed")));
    assert!(recoverable(&status("stopped")));
    for (pointer, value) in [
        ("/owner_run/error_code", json!("model_load_failed")),
        ("/owner_run/error_code", Value::Null),
        ("/owner_run/rollover/state", json!("idle")),
        (
            "/owner_run/cleanup_unproven",
            json!("owner_route_stop_unproven"),
        ),
        ("/owner_run/cleanup_unproven", json!(false)),
        ("/owner_run/state", json!("serving")),
    ] {
        let mut s = status("failed");
        *s.pointer_mut(pointer).unwrap() = value;
        assert!(!recoverable(&s), "{pointer}");
    }
    let mut missing = status("failed");
    missing["owner_run"]
        .as_object_mut()
        .unwrap()
        .remove("cleanup_unproven");
    assert!(!recoverable(&missing)); // missing evidence is not proven cleanup
    assert!(!recoverable(&json!({})));
}
#[test]
fn rolling_budget_survives_health_and_serialized_state_at_exact_window_edge() {
    let mut f = Fake::new(vec![]);
    for at in [0, 60_000, 120_000] {
        f.pages
            .extend([status("failed"), status("failed"), status("starting")].map(Ok));
        let c = check(&mut f, at, "fixture-swarm", limits()).unwrap();
        assert!(c.start_submitted);
        assert!(!c.serving_observed);
        f.state = serde_json::from_str(&serde_json::to_string(&f.state).unwrap()).unwrap();
    }
    assert_eq!(f.state.recoveries, vec![0, 60_000, 120_000]);
    f.pages.push_back(Ok(status("serving")));
    assert_eq!(
        check(&mut f, 200_000, "fixture-swarm", limits())
            .unwrap()
            .decision,
        Decision::Healthy
    );
    f.pages.push_back(Ok(status("failed")));
    assert_eq!(
        check(&mut f, 300_000, "fixture-swarm", limits())
            .unwrap()
            .decision,
        Decision::BudgetExhausted
    );
    assert_eq!(f.events.iter().filter(|s| *s == "start").count(), 3);
    f.pages
        .extend([status("failed"), status("failed"), status("serving")].map(Ok));
    assert!(
        check(&mut f, 21_600_000, "fixture-swarm", limits())
            .unwrap()
            .serving_observed
    );
    assert_eq!(f.state.recoveries, vec![60_000, 120_000, 21_600_000]);
}
#[test]
fn switching_exact_shapes_no_fighting_and_bounded_graces() {
    for s in [
        json!({"owner_run":{"rollover":{"state":"switching"}}}),
        json!({"owner_run":{"rollover":{"state":"backoff"}}}),
        json!({"chat":{"switching_runs":true}}),
        json!({"error":"chat_switching_runs"}),
        json!({"message":"Switching runs"}),
    ] {
        assert!(switching(&s));
    }
    assert!(!switching(&json!({"error":"chat_switching_runs_x"})));
    let mut s = status("stopped");
    s["owner_run"]["rollover"]["state"] = json!("switching");
    let mut p = State::default();
    assert_eq!(
        p.decide(0, &s, false, limits()).unwrap(),
        Decision::Switching
    );
    assert_eq!(
        p.decide(299_999, &s, false, limits()).unwrap(),
        Decision::Switching
    );
    assert_eq!(
        p.decide(300_000, &s, false, limits()).unwrap(),
        Decision::RolloverTimeout
    );
    assert!(p.recoveries.is_empty());
    assert_eq!(
        p.decide(301_000, &status("serving"), false, limits())
            .unwrap(),
        Decision::PublicUnhealthy
    );
    assert_eq!(
        p.decide(302_000, &status("running"), false, limits())
            .unwrap(),
        Decision::Starting
    );
    assert_eq!(
        p.decide(1_202_000, &status("running"), false, limits())
            .unwrap(),
        Decision::StartupTimeout
    );
    assert_eq!(
        p.decide(1_201_999, &s, false, limits()).unwrap_err().code,
        "keeper_clock_rollback"
    );
}
#[test]
fn member_silence_is_alerted_while_owner_serves_never_restarts_member() {
    let mut f = Fake::new(vec![status("serving")]);
    f.ages = vec![Some(300_000), Some(300_001), Some(1_565_000), None];
    let c = check(&mut f, 2_000_000, "fixture-swarm", limits()).unwrap();
    assert_eq!(
        c.alerts.iter().map(|a| a.code).collect::<Vec<_>>(),
        vec!["member_silent", "member_silent", "member_clock_unobserved"]
    );
    assert_eq!(c.alerts[0].peer.as_deref(), Some("fixture-1"));
    assert!(!c.start_submitted);
    assert!(!f.events.iter().any(|s| s == "start"));
    // The effect interface has no stop, restart, re-consent or member action.
}
#[test]
fn member_observation_continues_when_owner_check_fails() {
    let mut f = Fake::new(vec![]);
    f.pages.push_back(Err(Fault::new(
        "owner_status_timeout",
        FaultKind::Observation,
    )));
    f.ages = vec![Some(1_565_000)];
    let c = check(&mut f, 2_000_000, "fixture-swarm", limits()).unwrap();
    assert_eq!(c.decision, Decision::OwnerUnavailable);
    assert!(c.alerts.iter().any(|a| a.code == "member_silent"));
    assert!(c.alerts.iter().any(|a| a.code == "owner_status_timeout"));
}
#[test]
fn recovery_rechecks_swarm_rollover_and_cleanup_before_reservation() {
    for mutation in ["serving", "switching", "cleanup", "swarm"] {
        let mut changed = status("failed");
        match mutation {
            "serving" => changed["owner_run"]["state"] = json!("serving"),
            "switching" => changed["owner_run"]["rollover"]["state"] = json!("switching"),
            "cleanup" => changed["owner_run"]["cleanup_unproven"] = json!("unproven"),
            _ => changed["swarm"]["swarm_id"] = json!("different"),
        }
        let mut f = Fake::new(vec![status("failed"), changed]);
        let c = check(&mut f, 100, "fixture-swarm", limits()).unwrap();
        assert!(!c.start_submitted, "{mutation}");
        assert!(f.state.recoveries.is_empty());
    }
}
#[test]
fn durable_reservation_readback_required_and_ambiguous_start_never_replayed() {
    for stale in [false, true] {
        let mut f = Fake::new(vec![status("failed"), status("failed")]);
        f.save_error = !stale;
        f.stale_save = stale;
        assert!(check(&mut f, 100, "fixture-swarm", limits()).is_err());
        assert!(!f.events.iter().any(|s| s == "start"));
    }
    let mut f = Fake::new(vec![status("failed"), status("failed")]);
    f.start_error = true;
    assert_eq!(
        check(&mut f, 100, "fixture-swarm", limits())
            .unwrap_err()
            .code,
        "fixture_start_ambiguous"
    );
    assert_eq!(f.state.recoveries, vec![100]);
    assert_eq!(f.events.iter().filter(|s| *s == "start").count(), 1);
}
#[test]
fn auto_start_never_overrides_an_operator_stop_even_after_qualifying_failure() {
    // Keep the legacy policy's stopped classification, but the executable
    // auto-Start authority is strictly narrower than that pure classifier.
    for first in ["stopped", "failed"] {
        let mut f = Fake::new(vec![status(first), status("stopped"), status("serving")]);
        let c = check(&mut f, 100, "fixture-swarm", limits()).unwrap();
        assert!(!c.start_submitted, "initial {first}");
        assert!(f.state.recoveries.is_empty());
        assert!(!f.events.iter().any(|e| e == "start"));
    }
}
