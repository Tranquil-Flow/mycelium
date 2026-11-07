// SPDX-License-Identifier: AGPL-3.0-only
//! O:path:mycelium_a13_macos/chat.py — controlled backend, real HTTP boundary.
use api::{
    chat::{Chat, ChatError, Service, WithChat, client_key},
    desktop::{Desktop, Session},
    http::{Request, serve_connection},
    schema::{PublicError, Snapshot},
    status::PublishedStatus,
};
use budgets::BudgetId;
use journey_tests::{duration, http::Client};
use serde_json::{Map, Value, json};
use std::{
    net::{Ipv4Addr, TcpListener},
    sync::{Arc, Mutex},
    thread,
    time::Instant,
};

#[derive(Default)]
struct Fake {
    calls: Mutex<Vec<(String, Value)>>,
    target: bool,
}
impl Service for Fake {
    fn is_owner(&self) -> bool {
        !self.target
    }
    fn state(&self) -> Result<Value, ChatError> {
        Ok(json!({"available":false,"busy":false}))
    }
    fn send(&self, client: &str, body: Value, via_relay: bool) -> Result<Value, ChatError> {
        self.calls
            .lock()
            .unwrap()
            .push((format!("{client}:{via_relay}"), body));
        Err(ChatError::new(503, "chat_not_ready"))
    }
    fn events(&self, body: Value) -> Result<Value, ChatError> {
        self.calls.lock().unwrap().push(("events".into(), body));
        Ok(json!({"tokens":["a","b"],"done":true,"terminal":"completed","error":null}))
    }
}
struct NoActions;
impl api::desktop::Service for NoActions {
    fn action(&self, _: &str, _: &Map<String, Value>) -> Result<Value, PublicError> {
        panic!("chat escaped into authenticated action")
    }
}
fn req(method: &str, path: &str, body: &[u8]) -> Request {
    Request {
        method: method.into(),
        target: path.into(),
        headers: vec![
            ("content-type".into(), "application/json".into()),
            ("x-mycelium-chat".into(), "1".into()),
        ],
        body: body.into(),
    }
}
#[test]
fn role_route_header_and_json_precedence() {
    let chat = Chat::new(Fake::default());
    assert_eq!(
        chat.handle(req("GET", "/chat", b""), false).unwrap().status,
        200
    );
    let target = Chat::new(Fake {
        target: true,
        ..Fake::default()
    });
    assert_eq!(
        target
            .handle(req("GET", "/chat", b""), false)
            .unwrap()
            .status,
        404
    );
    let mut r = req("POST", "/chat/api/send", b"not json");
    r.headers.clear();
    let reply = chat.handle(r, false).unwrap();
    assert_eq!(
        serde_json::from_slice::<Value>(&reply.body).unwrap(),
        json!({"error":"chat_header_required"})
    );
    assert_eq!(
        chat.handle(req("POST", "/chat/api/send", b"bad"), false)
            .unwrap()
            .status,
        400
    );
    assert_eq!(
        chat.handle(req("POST", "/chat/api/send", &vec![b' '; 16385]), false)
            .unwrap()
            .status,
        413
    );
    assert_eq!(
        chat.handle(req("POST", "/chat/api/state", b"[]"), false)
            .unwrap()
            .status,
        200
    );
    assert_eq!(
        chat.handle(req("POST", "/chat/api/state", b""), false)
            .unwrap()
            .status,
        200
    );
    assert_eq!(
        chat.handle(req("POST", "/chat/api/cancel", b"{}"), false)
            .unwrap()
            .status,
        404
    );
    assert!(chat.service().calls.lock().unwrap().is_empty());
    // Pinned chat uses last-key-wins json.loads, unlike desktop signed/consent ingress.
    chat.handle(
        req(
            "POST",
            "/chat/api/send",
            br#"{"prompt":"first","prompt":"second"}"#,
        ),
        false,
    )
    .unwrap();
    assert_eq!(
        chat.service().calls.lock().unwrap()[0],
        ("local:false".into(), json!({"prompt":"second"}))
    );
}
#[test]
fn relay_client_key_has_ipv6_prefix_and_no_local_header_trust() {
    for (raw, expected) in [
        ("192.0.2.1", "relay:192.0.2.1"),
        ("::ffff:192.0.2.1", "relay:192.0.2.1"),
        ("2001:db8:abcd:1234::9876", "relay:2001:db8:abcd:1234::/64"),
        ("not-an-ip", "relay:unknown"),
        (" 192.0.2.1", "relay:unknown"),
    ] {
        let mut r = req("POST", "/chat/api/send", b"{}");
        r.headers.push(("cf-connecting-ip".into(), raw.into()));
        assert_eq!(client_key(&r, true), expected);
        assert_eq!(client_key(&r, false), "local");
    }
}
#[test]
fn public_chat_crosses_real_desktop_http_without_session_or_origin() {
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
        Arc::new(WithChat::new(NoActions, Chat::new(Fake::default()))),
        status,
        session,
    );
    let job = thread::spawn(move || {
        for _ in 0..4 {
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
    let c = Client::new(&origin).unwrap();
    let page = c
        .request("GET", "/chat", b"", &[], false, BudgetId::ModelSelection)
        .unwrap();
    assert_eq!(page.status, 200);
    for path in ["/chat/api/state", "/chat/api/events"] {
        let reply = c
            .request(
                "POST",
                path,
                b"{}",
                &[
                    ("Content-Type", "application/json"),
                    ("X-Mycelium-Chat", "1"),
                ],
                false,
                BudgetId::ModelSelection,
            )
            .unwrap();
        assert_eq!(reply.status, 200);
        if path.ends_with("events") {
            assert_eq!(reply.json().unwrap()["tokens"], json!(["a", "b"]));
            assert_eq!(reply.json().unwrap()["terminal"], "completed");
        }
    }
    assert_eq!(
        c.request(
            "GET",
            "/api/status",
            b"",
            &[],
            false,
            BudgetId::ModelSelection
        )
        .unwrap()
        .status,
        401
    );
    job.join().unwrap();
}
