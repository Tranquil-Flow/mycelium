// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{Fault, FaultKind, Result, effects::Clock, http::*};
use serde_json::{Value, json};
use std::collections::VecDeque;
#[derive(Default)]
struct Tick(u64);
impl Clock for Tick {
    fn now(&self) -> u64 {
        self.0
    }
    fn sleep(&mut self, s: Span) -> Result<()> {
        self.0 += s.millis();
        Ok(())
    }
}
struct Gate(bool);
impl Guard for Gate {
    fn check(&mut self) -> Result<()> {
        if self.0 {
            Ok(())
        } else {
            Err(Fault::new("battery_floor", FaultKind::Admission))
        }
    }
}
struct Fake {
    pages: VecDeque<(u16, Value)>,
    calls: Vec<(&'static str, Method, Option<Value>, bool, Span)>,
}
impl Http for Fake {
    fn call(&mut self, r: Request, b: Span) -> Result<(u16, Value)> {
        self.calls.push((r.path, r.method, r.body, r.chat, b));
        Ok(self.pages.pop_front().expect("unexpected network call"))
    }
}
fn limits() -> ChatLimits {
    ChatLimits {
        total: Span::from_millis(240000),
        request: Span::from_millis(20000),
        poll: Span::from_millis(250),
    }
}
#[test]
fn exact_switching_code_and_explicit_text_only() {
    for page in [
        json!({"error":"chat_switching_runs","retry_after_s":5}),
        json!({"code":"chat_switching_runs"}),
        json!({"done":true,"terminal":"cancelled","error":"chat_switching_runs"}),
        json!({"message":" Switching runs "}),
    ] {
        assert!(switching(&page));
    }
    for s in [
        "chat_not_ready",
        "chat_warming_up",
        "chat_busy",
        "chat_switching_runs_x",
        " chat_switching_runs",
        "owner_run_failed",
        "busy",
        "CHAT_SWITCHING_RUNS",
    ] {
        assert!(!switching(&json!({"error":s})), "{s}");
    }
    assert!(!switching(&json!({"error":null})));
    assert!(!switching(&json!({})));
}
#[test]
fn send_503_switching_and_event_switching_are_explicit_not_generic_503() {
    let mut io = Fake {
        pages: [(503, json!({"error":"chat_switching_runs"}))].into(),
        calls: vec![],
    };
    let c = chat(
        &mut io,
        &mut Tick::default(),
        &mut Gate(true),
        "fixture",
        48,
        limits(),
    )
    .unwrap();
    assert!(c.switching);
    assert!(!c.ok());
    assert_eq!(c.error.as_deref(), Some("switching_runs_message"));
    assert_eq!(io.calls.len(), 1);
    io.pages = [
        (200, json!({"request_id":"fixture"})),
        (
            200,
            json!({"done":true,"terminal":"cancelled","error":"chat_switching_runs"}),
        ),
    ]
    .into();
    assert!(
        chat(
            &mut io,
            &mut Tick::default(),
            &mut Gate(true),
            "fixture",
            48,
            limits()
        )
        .unwrap()
        .switching
    );
    io.pages = [(503, json!({"error":"chat_busy"}))].into();
    assert!(
        !chat(
            &mut io,
            &mut Tick::default(),
            &mut Gate(true),
            "fixture",
            48,
            limits()
        )
        .unwrap()
        .switching
    );
}
#[test]
fn complete_content_required_poll_metrics_no_token_text_in_receipt() {
    let mut io = Fake {
        pages: [
            (200, json!({"request_id":"fixture"})),
            (200, json!({"tokens":["a"],"next":1,"done":false})),
            (
                200,
                json!({"tokens":["PRIVATE-TOKEN"],"next":2,"done":true,"terminal":"completed"}),
            ),
        ]
        .into(),
        calls: vec![],
    };
    let c = chat(
        &mut io,
        &mut Tick::default(),
        &mut Gate(true),
        "PRIVATE-PROMPT",
        48,
        limits(),
    )
    .unwrap();
    assert!(c.ok());
    assert_eq!(c.n_tokens, 2);
    assert_eq!(c.ttft_ms, Some(0));
    assert_eq!(c.decode_tok_s, Some(4.));
    assert_eq!(c.total_ms, 250);
    let evidence = serde_json::to_string(&c).unwrap();
    assert!(!evidence.contains("PRIVATE"));
    assert_eq!(
        io.calls[0],
        (
            "/chat/api/send",
            Method::Post,
            Some(json!({"prompt":"PRIVATE-PROMPT","max_new_tokens":48})),
            true,
            Span::from_millis(20000)
        )
    );
    assert_eq!(
        io.calls[2].2,
        Some(json!({"request_id":"fixture","after":1}))
    );
    io.pages = [
        (200, json!({"request_id":"fixture"})),
        (200, json!({"done":true,"terminal":"completed","tokens":[]})),
    ]
    .into();
    assert!(
        !chat(
            &mut io,
            &mut Tick::default(),
            &mut Gate(true),
            "fixture",
            48,
            limits()
        )
        .unwrap()
        .ok()
    );
}
#[test]
fn chat_guard_observation_loss_is_propagated_incomplete_not_swallowed_product_error() {
    let mut io = Fake {
        pages: VecDeque::new(),
        calls: vec![],
    };
    assert_eq!(
        chat(
            &mut io,
            &mut Tick::default(),
            &mut Gate(false),
            "fixture",
            48,
            limits()
        )
        .unwrap_err()
        .code,
        "battery_floor"
    );
    assert!(io.calls.is_empty());
    struct Lost;
    impl Http for Lost {
        fn call(&mut self, _: Request, _: Span) -> Result<(u16, Value)> {
            Err(Fault::new("ssh_observation_lost", FaultKind::Observation))
        }
    }
    assert!(
        chat(
            &mut Lost,
            &mut Tick::default(),
            &mut Gate(true),
            "fixture",
            48,
            limits()
        )
        .unwrap_err()
        .incomplete()
    );
}
#[test]
fn owner_ruled_send_refusal_keeps_http_and_safe_code_including_switching() {
    // test_harness_hardening.py OwnerRuledSoakTests; no real connector.
    for (http, page, code, switching) in [
        (
            503,
            json!({"error":"chat_not_ready"}),
            json!("chat_not_ready"),
            false,
        ),
        (502, json!({"error":"<html> bad"}), Value::Null, false),
        (
            503,
            json!({"error":"chat_switching_runs"}),
            json!("chat_switching_runs"),
            true,
        ),
        (
            503,
            json!({"message":"Switching runs"}),
            json!("chat_switching_runs"),
            true,
        ),
    ] {
        let mut io = Fake {
            pages: [(http, page)].into(),
            calls: vec![],
        };
        let c = chat(
            &mut io,
            &mut Tick::default(),
            &mut Gate(true),
            "hello",
            48,
            limits(),
        )
        .unwrap();
        assert_eq!(
            c.error.as_deref(),
            Some(if switching {
                "switching_runs_message"
            } else {
                "chat_submission_failed"
            })
        );
        assert_eq!(c.switching, switching);
        let value = serde_json::to_value(c).unwrap();
        assert_eq!(value["submit_http"], http);
        assert_eq!(value["submit_code"], code);
        assert!(!value.to_string().contains("<html>"));
        assert_eq!(io.calls.len(), 1);
    }
}

#[test]
fn body_parity_real_gui_2gb_and_exact_run_options_no_owner_standing_grant() {
    let c = json!({"peer_visibility":true,"power":"battery_allowed","storage_bytes":2000000000u64,"network":"metered_allowed","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
    assert_eq!(consent(), c);
    assert_eq!(
        create_body(true, Span::from_millis(1800000)).unwrap(),
        json!({"consent":c,"relay_acknowledged":true,"max_seconds":1800})
    );
    assert_eq!(
        create_body(false, Span::from_millis(1800000)).unwrap(),
        json!({"consent":c})
    );
    for seconds in [600, 1800, 3600] {
        let b = join_body("PRIVATE-LINK", "d", Span::from_millis(seconds * 1000)).unwrap();
        assert_eq!(
            b,
            json!({"consent":c,"adaptive_sharing":false,"join":"PRIVATE-LINK","standing_consent":{"acknowledged":true,"disclosure_digest":"d","caps":{"storage_bytes":2000000000u64,"max_memory_bytes":4294967296u64,"max_wall_seconds":seconds,"max_commands":5000}}})
        );
    }
    assert!(join_body("PRIVATE-LINK", "d", Span::from_millis(900000)).is_err());
}
