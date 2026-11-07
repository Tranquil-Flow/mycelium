// SPDX-License-Identifier: AGPL-3.0-only
//! O:path:mycelium_a13_macos/app.py — pure action adapter with fake effects.
use api::{
    actions::{Action, Backend, Control},
    desktop::{Desktop, Service, Session},
    http::serve_connection,
    schema::{PublicError, Snapshot},
    status::PublishedStatus,
};
use budgets::BudgetId;
use journey_tests::{compare, duration, http::Client, project};
use serde_json::{Map, Value, json};
use std::{
    net::{Ipv4Addr, TcpListener},
    sync::{Arc, Mutex},
    thread,
    time::Instant,
};
#[derive(Default)]
struct Fake {
    trace: Arc<Mutex<Vec<String>>>,
    target: bool,
    fail_publish: bool,
}
impl Backend for Fake {
    fn prepare(&mut self, action: Action, _: &Map<String, Value>) -> Result<(), PublicError> {
        self.trace
            .lock()
            .unwrap()
            .push(format!("prepare:{action:?}"));
        if self.target {
            return Err(PublicError::new(400, "owner_role_required"));
        }
        Ok(())
    }
    fn execute(&mut self, action: Action, body: &Map<String, Value>) -> Result<Value, PublicError> {
        self.trace
            .lock()
            .unwrap()
            .push(format!("execute:{action:?}"));
        match action {
            Action::RunModel => {
                Ok(json!({"selected":body["choice"],"source":"saved","locked":false}))
            }
            Action::FastMode => {
                Ok(json!({"enabled":body["enabled"],"source":"saved","locked":false}))
            }
            Action::ServingStatus => {
                Ok(json!({"compilation":null,"serving":null,"route_ready":false}))
            }
            _ => Ok(json!({"state":"stopped","route_ready":false})),
        }
    }
    fn publish(&mut self) -> Result<(), PublicError> {
        self.trace.lock().unwrap().push("publish".into());
        if self.fail_publish {
            Err(PublicError::new(503, "status_unavailable"))
        } else {
            Ok(())
        }
    }
}
fn body(value: Value) -> Map<String, Value> {
    value.as_object().unwrap().clone()
}
#[test]
fn typed_routes_refuse_unknown_and_invalid_shapes_before_effects() {
    let fake = Fake::default();
    let trace = fake.trace.clone();
    let c = Control::new(fake);
    for (path, document, code) in [
        ("/api/not-real", json!({}), "unknown_action"),
        ("/api/fast-mode", json!({"enabled":1}), "invalid_request"),
        (
            "/api/run-model",
            json!({"choice":"x","extra":true}),
            "invalid_request",
        ),
        ("/api/demo/stop", json!({"extra":true}), "invalid_request"),
        (
            "/api/serving/status",
            json!({"extra":true}),
            "invalid_request",
        ),
        (
            "/api/serving/compile",
            json!({}),
            "model_preparation_consent_required",
        ),
        (
            "/api/network/start",
            json!({"relay_acknowledged":1,"max_seconds":duration(BudgetId::RelayWindow).unwrap().as_secs()}),
            "network_consent_required",
        ),
        (
            "/api/network/reconnect",
            json!({"card":{},"relay_acknowledged":false}),
            "network_consent_required",
        ),
    ] {
        assert_eq!(c.action(path, &body(document)).unwrap_err().code, code);
    }
    assert!(
        trace
            .lock()
            .unwrap()
            .iter()
            .all(|v| v.starts_with("prepare:"))
    );
    let target = Control::new(Fake {
        target: true,
        ..Fake::default()
    });
    assert_eq!(
        target
            .action("/api/fast-mode", &body(json!({"enabled":"bad"})))
            .unwrap_err()
            .code,
        "owner_role_required"
    );
}
#[test]
fn valid_actions_publish_before_success_and_publish_failure_is_not_success() {
    let fake = Fake::default();
    let trace = fake.trace.clone();
    let c = Control::new(fake);
    for (path, document) in [
        ("/api/demo/start", json!({})),
        ("/api/demo/rebuild", json!({})),
        ("/api/demo/stop", json!({})),
        (
            "/api/network/start",
            json!({"relay_acknowledged":true,"max_seconds":duration(BudgetId::RelayWindow).unwrap().as_secs()}),
        ),
        ("/api/serving/status", json!({})),
        ("/api/fast-mode", json!({"enabled":true})),
    ] {
        trace.lock().unwrap().clear();
        c.action(path, &body(document)).unwrap();
        let t = trace.lock().unwrap();
        assert_eq!(t.len(), 3);
        assert!(t[0].starts_with("prepare:"));
        assert!(t[1].starts_with("execute:"));
        assert_eq!(t[2], "publish");
    }
    let broken = Control::new(Fake {
        fail_publish: true,
        ..Fake::default()
    });
    assert_eq!(
        broken
            .action("/api/demo/stop", &Map::new())
            .unwrap_err()
            .status,
        503
    );
}
#[test]
fn network_window_is_integer_and_registry_bounded_with_external_shape() {
    let c = Control::new(Fake::default());
    let lo = duration(BudgetId::RelayMinimumWindow).unwrap().as_secs();
    let hi = duration(BudgetId::QuickRelayMaximumWindow)
        .unwrap()
        .as_secs();
    for value in [json!(lo), json!(hi)] {
        c.action(
            "/api/network/start",
            &body(json!({"relay_acknowledged":true,"max_seconds":value})),
        )
        .unwrap();
    }
    for value in [
        json!(lo - 1),
        json!(hi + 1),
        json!(hi as f64),
        json!(true),
        json!(null),
    ] {
        assert_eq!(
            c.action(
                "/api/network/start",
                &body(json!({"relay_acknowledged":true,"max_seconds":value}))
            )
            .unwrap_err()
            .code,
            "network_consent_required"
        );
    }
    c.action("/api/network/start",&body(json!({"relay_acknowledged":true,"max_seconds":hi,"provider":"external","public_origin":"https://example.invalid","proxy_port":1234,"auto_renew":true}))).unwrap();
    // Restart may reuse saved external address; start may not omit it.
    let saved = body(json!({"relay_acknowledged":true,"max_seconds":hi,"provider":"external"}));
    assert!(c.action("/api/network/start", &saved).is_err());
    c.action("/api/network/restart", &saved).unwrap();
}
#[test]
fn four_more_unchanged_l3_observations_over_real_rust_http() {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let host = listener.local_addr().unwrap().to_string();
    let origin = format!("http://{host}");
    let status = Arc::new(PublishedStatus::new());
    status
        .publish(
            1,
            Snapshot::parse(
                serde_json::from_str(include_str!("../contracts/status-example.json")).unwrap(),
            )
            .unwrap(),
        )
        .unwrap();
    let session = Session::new(
        "launch".into(),
        "session".into(),
        Instant::now() + duration(BudgetId::WorkerStartup).unwrap(),
        duration(BudgetId::WorkerStartup).unwrap(),
    )
    .unwrap();
    let app = Desktop::new(
        host,
        Arc::new(Control::new(Fake::default())),
        status,
        session,
    );
    let job = thread::spawn(move || {
        for _ in 0..5 {
            let (stream, _) = listener.accept().unwrap();
            serve_connection(
                stream,
                duration(BudgetId::ModelSelection).unwrap(),
                |r| app.preflight(r),
                |r| app.handle(r),
            )
            .unwrap();
        }
    });
    let mut c = Client::new(&origin).unwrap();
    c.sign_in("launch").unwrap();
    let replies = [
        (
            "stats_not_exposed_on_private_app",
            c.get("/api/stats").unwrap(),
        ),
        (
            "choose_model_without_download",
            c.post("/api/run-model", &json!({"choice":"qwen3-0.6b"}))
                .unwrap(),
        ),
        (
            "fast_mode_saved",
            c.post("/api/fast-mode", &json!({"enabled":true})).unwrap(),
        ),
        (
            "normal_mode_saved",
            c.post("/api/fast-mode", &json!({"enabled":false})).unwrap(),
        ),
    ];
    job.join().unwrap();
    let golden: Value = serde_json::from_str(include_str!(
        "../../../test-support/journeys/fixtures/python-v014.json"
    ))
    .unwrap();
    let mut expected = Vec::new();
    let mut actual = Vec::new();
    for (id, reply) in replies {
        let old = golden["observations"]
            .as_array()
            .unwrap()
            .iter()
            .find(|v| v["id"] == id)
            .unwrap()
            .clone();
        let keys = old["fields"]
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>();
        let mut got = old.clone();
        got["status"] = json!(reply.status);
        got["fields"] = project(&reply.json().unwrap(), &keys).unwrap();
        expected.push(old);
        actual.push(got);
    }
    compare(&json!(expected), &json!(actual)).unwrap();
    eprintln!(
        "L3_ADDITIONAL_OBSERVATIONS=4; IDS=stats_not_exposed_on_private_app,choose_model_without_download,fast_mode_saved,normal_mode_saved; BACKEND=fake; PERSISTENCE=NOT_QUALIFIED"
    );
}
