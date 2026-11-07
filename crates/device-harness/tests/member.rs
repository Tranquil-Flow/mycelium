// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{
    Result,
    effects::Clock,
    http::{Http, Method, Request},
    member::*,
};
use serde_json::{Value, json};
use std::{cell::Cell, rc::Rc};
#[derive(Clone, Default)]
struct Time(Rc<Cell<u64>>);
impl Clock for Time {
    fn now(&self) -> u64 {
        self.0.get()
    }
    fn sleep(&mut self, s: Span) -> Result<()> {
        self.0.set(self.now() + s.millis());
        Ok(())
    }
}
struct Fake {
    time: Time,
    calls: Vec<(String, Option<Value>, u64)>,
    reads: usize,
    polls: usize,
    terminal: bool,
    never: bool,
}
impl Http for Fake {
    fn call(&mut self, r: Request, b: Span) -> Result<(u16, Value)> {
        assert_eq!(
            r.method,
            if r.path == "/api/status" {
                Method::Get
            } else {
                Method::Post
            }
        );
        self.calls.push((r.path.into(), r.body, b.millis()));
        let v = match r.path {
            "/api/status" => {
                self.reads += 1;
                json!({"join_flow":{"standing_disclosure":{"digest":"dddd"}},"authority":{"membership_state":if self.reads>=3&&!self.never{"current"}else{"absent"}},"grant":"PRIVATE-LINK"})
            }
            "/api/join/window/poll" => {
                self.polls += 1;
                assert_eq!(self.polls, 1);
                if self.terminal {
                    return Ok((410, json!({"error":"expired","grant":"PRIVATE-LINK"})));
                }
                json!({"state":"joined"})
            }
            _ => json!({"state":"waiting_for_owner"}),
        };
        self.time.0.set(self.time.now() + 1250.min(b.millis()));
        Ok((200, v))
    }
}
fn fake() -> Fake {
    Fake {
        time: Time::default(),
        calls: vec![],
        reads: 0,
        polls: 0,
        terminal: false,
        never: false,
    }
}
#[test]
fn owner_status_retains_1250_ms_latency_without_sensitive_snapshot() {
    let mut f = fake();
    let clock = f.time.clone();
    let row = observed_status(&mut f, &clock, Span::from_millis(20000)).unwrap();
    assert_eq!(row.latency_ms, 1250);
    assert!(
        !serde_json::to_string(&row)
            .unwrap()
            .contains("PRIVATE-LINK")
    );
}
fn limits() -> Limits {
    Limits {
        total: Span::from_millis(20000),
        request: Span::from_millis(20000),
        poll: Span::from_millis(100),
        run_wall: Span::from_millis(600000),
    }
}
#[test]
fn consumed_join_polls_once_waits_for_publication_and_retains_latency() {
    let mut f = fake();
    let mut t = f.time.clone();
    let r = join(&mut f, &mut t, "PRIVATE-LINK", limits()).unwrap();
    assert_eq!(r.membership_state, "current");
    assert_eq!(f.polls, 1);
    assert_eq!(r.status_latencies_ms, vec![1250; 3]);
    assert!(
        f.calls
            .iter()
            .any(|(p, b, _)| p == "/api/join/window/poll" && b == &Some(json!({})))
    );
    let body = &f
        .calls
        .iter()
        .find(|(p, _, _)| p == "/api/join/window")
        .unwrap()
        .1;
    assert_eq!(
        body.as_ref().unwrap()["standing_consent"]["caps"]["max_wall_seconds"],
        600
    );
    assert!(!f.calls.iter().any(|(p, _, _)| p == "/api/model/download"));
    assert!(!serde_json::to_string(&r).unwrap().contains("PRIVATE-LINK"));
}
#[test]
fn terminal_error_and_six_second_timeout_are_bounded() {
    for terminal in [false, true] {
        let mut f = fake();
        f.terminal = terminal;
        f.never = true;
        let mut t = f.time.clone();
        let mut l = limits();
        l.total = Span::from_millis(6000);
        assert!(join(&mut f, &mut t, "PRIVATE-LINK", l).is_err());
        // Requests receive remaining time, including the last partial request.
        assert!(f.calls.iter().all(|(_, _, bound)| *bound <= 6000));
        assert!(t.now() <= 6000);
    }
}
#[test]
fn member_start_and_capability_have_exact_empty_bodies() {
    let mut f = fake();
    for action in [Action::Start, Action::Capability] {
        perform(&mut f, action, Span::from_millis(20000)).unwrap();
    }
    assert!(
        f.calls
            .iter()
            .any(|(p, b, _)| p == "/api/start" && b == &Some(json!({})))
    );
    assert!(
        f.calls
            .iter()
            .any(|(p, b, _)| p == "/api/capability/report" && b == &Some(json!({})))
    );
}
#[test]
fn request_budget_default_and_both_explicit_overrides() {
    let default = Span::from_millis(20000);
    assert_eq!(
        [
            status_bound(default, None, None),
            status_bound(default, Some(Span::from_millis(7000)), None),
            status_bound(default, None, Some(Span::from_millis(11000)))
        ]
        .map(|s| s.millis()),
        [20000, 7000, 11000]
    );
}
