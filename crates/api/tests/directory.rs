// SPDX-License-Identifier: AGPL-3.0-only
//! O:path:mycelium_swarm_directory/server.py; fake signature/storage seam only.
use api::{
    directory::{Directory, Service},
    http::{Reply, Request},
    schema::PublicError,
};
use serde_json::{Map, Value, json};
use std::sync::atomic::{AtomicUsize, Ordering};
struct Fake {
    calls: AtomicUsize,
}
impl Service for Fake {
    fn get(&self, path: &str) -> Result<Value, PublicError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if path == "/swarms" {
            Ok(json!({"swarms":[],"privacy":"fake seam","now":0}))
        } else {
            Err(PublicError::new(404, "swarm_unknown"))
        }
    }
    fn signed(&self, _path: &str, _document: &Map<String, Value>) -> Result<Value, PublicError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        Err(PublicError::new(403, "signature_invalid"))
    }
}
fn request(method: &str, path: &str, body: &[u8]) -> Request {
    Request {
        method: method.into(),
        target: path.into(),
        body: body.into(),
        headers: vec![
            (
                "content-type".into(),
                "application/json; charset=utf-8".into(),
            ),
            ("x-mycelium-swarms".into(), "1".into()),
            ("content-length".into(), body.len().to_string()),
        ],
    }
}
fn app() -> Directory<Fake> {
    Directory::new(Fake {
        calls: AtomicUsize::new(0),
    })
}
fn code(r: Reply, status: u16, code: &str) {
    assert_eq!(r.status, status);
    assert_eq!(
        serde_json::from_slice::<Value>(&r.body).unwrap(),
        json!({"error":code})
    );
}
#[test]
fn signed_routes_are_total_and_never_accept_unsigned_success() {
    let app = app();
    for path in [
        "records",
        "delete",
        "heartbeat",
        "access-requests",
        "owner-inbox",
        "grants",
        "deny",
        "my-grants",
        "entry-offers",
    ] {
        code(
            app.handle(request("POST", &format!("/swarms/api/v1/{path}"), b"{}"))
                .unwrap(),
            403,
            "signature_invalid",
        );
    }
    assert_eq!(app.service().calls.load(Ordering::SeqCst), 9);
}
#[test]
fn credential_header_and_path_refusals_precede_service() {
    let app = app();
    for header in ["cookie", "authorization"] {
        let mut r = request("POST", "/swarms/api/v1/records", b"{}");
        r.headers.push((header.into(), "canary-do-not-echo".into()));
        code(app.handle(r).unwrap(), 400, "credentials_refused");
    }
    let mut r = request("POST", "/swarms/api/v1/records", b"{}");
    r.headers.retain(|(k, _)| k != "x-mycelium-swarms");
    code(app.handle(r).unwrap(), 403, "header_required");
    code(
        app.handle(request("GET", "/swarms/api/v1/swarms?secret=x", b""))
            .unwrap(),
        404,
        "route_unknown",
    );
    assert_eq!(app.service().calls.load(Ordering::SeqCst), 0);
}
#[test]
fn get_head_and_method_refusal_preserve_contract() {
    let app = app();
    let get = app
        .handle(request("GET", "/swarms/api/v1/swarms", b""))
        .unwrap();
    let head = app
        .handle(request("HEAD", "/swarms/api/v1/swarms", b""))
        .unwrap();
    assert_eq!(get.status, 200);
    assert_eq!(head.status, 200);
    assert!(head.body.is_empty());
    assert_eq!(
        head.header("content-length"),
        Some(get.body.len().to_string().as_str())
    );
    code(
        app.handle(request("GET", "/swarms/api/v1/records", b""))
            .unwrap(),
        405,
        "method_not_allowed",
    );
    code(
        app.handle(request("OPTIONS", "/swarms/api/v1/swarms", b""))
            .unwrap(),
        405,
        "method_not_allowed",
    );
    code(
        app.handle(request(
            "GET",
            "/swarms/api/v1/swarms/sw-aaaaaaaaaaaaaaaaaaaa",
            b"",
        ))
        .unwrap(),
        404,
        "swarm_unknown",
    );
}
#[test]
fn native_head_preserves_get_length_without_body_or_duplicate_headers() {
    use std::{
        io::{Read, Write},
        net::{Ipv4Addr, TcpListener, TcpStream},
        thread,
    };
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let address = listener.local_addr().unwrap();
    let expected = app()
        .handle(request("GET", "/swarms/api/v1/swarms", b""))
        .unwrap()
        .body
        .len();
    let server = thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        api::http::serve_connection(
            stream,
            journey_tests::duration(budgets::BudgetId::ModelSelection).unwrap(),
            |_| Ok(None),
            |r| app().handle(r),
        )
        .unwrap();
    });
    let mut stream = TcpStream::connect(address).unwrap();
    stream
        .set_read_timeout(Some(
            journey_tests::duration(budgets::BudgetId::ModelSelection).unwrap(),
        ))
        .unwrap();
    stream
        .write_all(b"HEAD /swarms/api/v1/swarms HTTP/1.1\r\nHost: localhost\r\n\r\n")
        .unwrap();
    stream.shutdown(std::net::Shutdown::Write).unwrap();
    let mut wire = String::new();
    stream.read_to_string(&mut wire).unwrap();
    server.join().unwrap();
    assert!(wire.starts_with("HTTP/1.1 200"), "{wire}");
    let (headers, body) = wire.split_once("\r\n\r\n").unwrap();
    assert!(body.is_empty());
    assert_eq!(
        headers
            .to_ascii_lowercase()
            .matches("content-length:")
            .count(),
        1
    );
    assert!(
        headers
            .to_ascii_lowercase()
            .contains(&format!("content-length: {expected}\r\n"))
    );
}
#[test]
fn numeric_directory_refusals_do_not_turn_into_private_serde_map_errors() {
    let app = app();
    for (raw, expected) in [
        (br#"{"ttl":1.25}"#.as_slice(), "json_float_refused"),
        (br#"{"ttl":1e2}"#, "json_float_refused"),
        (br#"{"ttl":9007199254740992}"#, "integer_out_of_range"),
        (br#"{"ttl":-9007199254740992}"#, "integer_out_of_range"),
    ] {
        code(
            app.handle(request("POST", "/swarms/api/v1/records", raw))
                .unwrap(),
            400,
            expected,
        );
    }
    assert_eq!(app.service().calls.load(Ordering::SeqCst), 0);
}
#[test]
fn malformed_and_overlong_body_do_not_reach_signature_service() {
    let app = app();
    code(
        app.handle(request(
            "POST",
            "/swarms/api/v1/records",
            b"{\"x\":1,\"x\":2}",
        ))
        .unwrap(),
        400,
        "json_duplicate_key",
    );
    code(
        app.handle(request("POST", "/swarms/api/v1/records", &vec![b'a'; 8193]))
            .unwrap(),
        413,
        "body_too_large",
    );
    assert_eq!(app.service().calls.load(Ordering::SeqCst), 0);
}
