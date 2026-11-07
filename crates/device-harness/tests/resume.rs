// SPDX-License-Identifier: AGPL-3.0-only
//! Transcriptions: test_device_tools.py restart tests (source pins in lane receipts).
use budgets::Span;
use device_harness::{Fault, FaultKind, Host, Result, effects::Clock, resume::*};
use serde_json::{Value, json};
use std::collections::VecDeque;
#[derive(Default)]
struct Fake {
    now: u64,
    statuses: VecDeque<Value>,
    actions: Vec<(Host, String, Value)>,
    refused: bool,
    down: bool,
    late: bool,
}
impl Clock for Fake {
    fn now(&self) -> u64 {
        self.now
    }
    fn sleep(&mut self, s: Span) -> Result<()> {
        self.now += s.millis();
        Ok(())
    }
}
fn status(serving: bool) -> Value {
    json!({"network":{"state":"running"},"owner_run":{"state":if serving {"serving"}else{"idle"},"route_ready":serving,"participants":[{"node_id":"a"},{"node_id":"b"}]}})
}
impl RestartIo for Fake {
    fn owner_lifecycle(&mut self, action: OwnerAction, _: Span) -> Result<()> {
        self.actions
            .push((Host::Owner, format!("{action:?}"), json!({})));
        Ok(())
    }
    fn status(&mut self, host: Host, _: Span) -> Result<Value> {
        self.actions.push((host, "status".into(), json!({})));
        if self.down {
            return Err(Fault::new("status_refused", FaultKind::Product));
        }
        if self.late {
            self.now += 13000;
        }
        Ok(if host == Host::Owner {
            self.statuses.pop_front().unwrap_or_else(|| status(true))
        } else {
            json!({"membership_state":"current","lease_state":"fresh","capability_state":"self_report_accepted"})
        })
    }
    fn owner_post(&mut self, path: &'static str, body: Value, _: Span) -> Result<()> {
        self.actions.push((Host::Owner, path.into(), body));
        if self.refused {
            Err(Fault::new("action_refused", FaultKind::Product))
        } else {
            Ok(())
        }
    }
}
fn limits() -> Limits {
    Limits {
        total: Span::from_millis(12000),
        request: Span::from_millis(20000),
        poll: Span::from_millis(5000),
        relay_wall: Span::from_millis(1800000),
    }
}
#[test]
fn user_resume_starts_relay_and_run_on_owner_only() {
    let mut f = Fake {
        statuses: [
            json!({"network":{"state":"private"},"owner_run":{"state":"idle"}}),
            status(true),
        ]
        .into(),
        ..Fake::default()
    };
    let r = restart(&mut f, "user", "quick", limits()).unwrap();
    assert!(r.recovered);
    assert_eq!(r.resume, "user");
    assert_eq!(r.owner_actions, ["network_start", "demo_start"]);
    let posts: Vec<_> = f
        .actions
        .iter()
        .filter(|(_, p, _)| p.starts_with('/'))
        .map(|(_, p, b)| (p.as_str(), b.clone()))
        .collect();
    assert_eq!(
        posts,
        [
            (
                "/api/network/start",
                json!({"relay_acknowledged":true,"max_seconds":1800})
            ),
            ("/api/demo/start", json!({}))
        ]
    );
    assert!(
        f.actions
            .iter()
            .all(|(h, a, _)| *h == Host::Owner || a == "status")
    );
    assert!(f.actions.iter().any(|(_, a, _)| a == "Stop"));
    assert!(f.actions.iter().any(|(_, a, _)| a == "Launch"));
    assert!(!r.release_qualified);
}
#[test]
fn running_relay_and_serving_run_are_not_restarted() {
    let mut f = Fake::default();
    let r = restart(&mut f, "user", "quick", limits()).unwrap();
    assert!(r.recovered);
    assert!(r.owner_actions.is_empty());
    assert!(!f.actions.iter().any(|(_, a, _)| a.starts_with('/')));
}
#[test]
fn user_refusal_is_not_recovery() {
    let mut f = Fake {
        statuses: [json!({"network":{"state":"stopped"}})].into(),
        refused: true,
        ..Fake::default()
    };
    let r = restart(&mut f, "user", "quick", limits()).unwrap();
    assert!(!r.recovered);
    assert_eq!(r.error.as_deref(), Some("action_refused"));
    assert!(r.owner_actions.is_empty());
}
#[test]
fn invalid_modes_refuse_before_effects() {
    for (resume, relay, code) in [
        ("repair", "quick", "invalid_restart_resume_mode"),
        ("user", "other", "invalid_restart_relay_mode"),
    ] {
        let mut f = Fake::default();
        assert_eq!(
            restart(&mut f, resume, relay, limits()).unwrap_err().code,
            code
        );
        assert!(f.actions.is_empty());
    }
}
#[test]
fn automatic_timeout_is_twelve_seconds_without_member_repair() {
    let mut f = Fake {
        down: true,
        ..Fake::default()
    };
    let r = restart(&mut f, "automatic", "quick", limits()).unwrap();
    assert!(!r.recovered);
    assert_eq!(f.now, 12000);
    assert_eq!(r.recovery_ms, 12000);
    assert_eq!(
        f.actions
            .iter()
            .filter(|(_, a, _)| a != "status")
            .map(|(_, a, _)| a.as_str())
            .collect::<Vec<_>>(),
        ["Stop", "Launch", "SignIn"]
    );
    assert!(r.owner_actions.is_empty());
    assert!(!r.observation_errors.is_empty());
}
#[test]
fn demo_relay_exact_body_and_quick_defaults() {
    let expected = json!({"relay_acknowledged":true,"max_seconds":1800,"provider":"external","public_origin":"https://demo.mycelium.now","proxy_port":4385,"cloudflare_front":true,"long_lived":true});
    assert_eq!(relay_body("demo", limits().relay_wall).unwrap(), expected);
    assert_eq!(
        Options::default(),
        Options {
            resume: "user".into(),
            relay: "quick".into()
        }
    );
    let mut f = Fake {
        statuses: [json!({"network":{"state":"stopped"}}), status(true)].into(),
        ..Fake::default()
    };
    let r = restart(&mut f, "user", "demo", limits()).unwrap();
    assert!(r.recovered);
    assert_eq!(
        f.actions
            .iter()
            .find(|(_, p, _)| p == "/api/network/start")
            .unwrap()
            .2,
        expected
    );
}
#[test]
fn restart_cli_options_preserve_defaults_and_reject_unknown_or_duplicate_flags() {
    assert_eq!(Options::parse(&[]).unwrap(), Options::default());
    for resume in ["user", "automatic"] {
        for relay in ["quick", "demo"] {
            assert_eq!(
                Options::parse(&["--restart-relay", relay, "--restart-resume", resume]).unwrap(),
                Options {
                    resume: resume.into(),
                    relay: relay.into()
                }
            );
        }
    }
    for bad in [
        vec!["--restart-resume", "repair"],
        vec!["--restart-relay", "other"],
        vec!["--restart-resume"],
        vec!["--unknown", "user"],
        vec!["--restart-resume", "user", "--restart-resume", "automatic"],
    ] {
        assert!(Options::parse(&bad).is_err(), "{bad:?}");
    }
}
#[test]
fn late_success_cannot_escape_restart_deadline() {
    let mut f = Fake {
        late: true,
        ..Fake::default()
    };
    let r = restart(&mut f, "automatic", "quick", limits()).unwrap();
    assert!(!r.recovered);
    assert_eq!(r.error.as_deref(), Some("restart_observation_timeout"));
    assert!(!f.actions.iter().any(|(h, _, _)| *h == Host::Member));
}
