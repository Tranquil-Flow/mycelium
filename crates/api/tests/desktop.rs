// SPDX-License-Identifier: AGPL-3.0-only
//! O:path:mycelium_a13_macos/app.py, O:escape:F5.
use api::{
    desktop::{Desktop, ProductRoutes, Service, Session},
    http::{Reply, Request},
    schema::{PublicError, Snapshot},
    status::PublishedStatus,
};
use budgets::BudgetId;
use journey_tests::duration;
use serde_json::{Map, Value, json};
use std::{
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Instant,
};

struct Fake {
    calls: AtomicUsize,
}
impl Service for Fake {
    fn action(&self, path: &str, body: &Map<String, Value>) -> Result<Value, PublicError> {
        self.calls.fetch_add(1, Ordering::SeqCst);
        if path == "/api/create" && !body.contains_key("consent") {
            return Err(PublicError::new(400, "consent_required"));
        }
        if path == "/api/fast-mode" {
            return Ok(json!({"enabled":body.get("enabled"),"source":"saved","locked":false}));
        }
        Err(PublicError::new(400, "unknown_action"))
    }
}
fn app() -> Desktop<Fake> {
    Desktop::new(
        "127.0.0.1:12345".into(),
        Arc::new(Fake {
            calls: AtomicUsize::new(0),
        }),
        Arc::new(PublishedStatus::new()),
        Session::new(
            "launch-canary".into(),
            "session-canary".into(),
            Instant::now() + duration(BudgetId::WorkerStartup).unwrap(),
            duration(BudgetId::WorkerStartup).unwrap(),
        )
        .unwrap(),
    )
}
fn request(method: &str, path: &str, body: &[u8]) -> Request {
    Request {
        method: method.into(),
        target: path.into(),
        headers: vec![
            ("host".into(), "127.0.0.1:12345".into()),
            ("cookie".into(), "a13session=session-canary".into()),
            ("origin".into(), "http://127.0.0.1:12345".into()),
            ("x-mycelium-action".into(), "1".into()),
            ("content-type".into(), "application/json".into()),
            ("content-length".into(), body.len().to_string()),
        ],
        body: body.into(),
    }
}
fn code(reply: Reply, status: u16, error: &str) {
    assert_eq!(reply.status, status);
    assert_eq!(
        serde_json::from_slice::<Value>(&reply.body).unwrap(),
        json!({"error":error})
    );
}
fn login(app: &Desktop<Fake>) {
    let mut r = request("POST", "/session", b"launch=launch-canary");
    r.headers.retain(|(k, _)| k != "content-type");
    r.headers.push((
        "content-type".into(),
        "application/x-www-form-urlencoded".into(),
    ));
    assert_eq!(app.handle(r).unwrap().status, 303);
}
#[test]
fn native_reopen_requires_native_authority_not_a_browser_cookie() {
    let a = app();
    code(
        a.handle(request("POST", "/native-session", b"{}")).unwrap(),
        403,
        "native_authorization_required",
    );
    assert_eq!(a.service().calls.load(Ordering::SeqCst), 0);
}
#[test]
fn authentication_precedes_parse_and_actions() {
    let a = app();
    code(
        a.handle(request("GET", "/api/status", b"")).unwrap(),
        401,
        "open_from_native_launcher",
    );
    login(&a);
    let mut r = request("POST", "/api/create", b"not-json");
    r.headers.retain(|(k, _)| k != "origin");
    code(a.handle(r).unwrap(), 403, "origin_rejected");
    assert_eq!(a.service().calls.load(Ordering::SeqCst), 0);
}
#[test]
fn host_path_duplicate_json_and_content_type_guards() {
    let a = app();
    login(&a);
    let mut r = request("GET", "/api/status", b"");
    r.headers.push(("host".into(), "127.0.0.1:12345".into()));
    code(a.handle(r).unwrap(), 403, "host_rejected");
    code(
        a.handle(request("GET", "/api/status?secret=x", b""))
            .unwrap(),
        400,
        "path_rejected",
    );
    for raw in [
        b"{\"a\":1,\"a\":2}".as_slice(),
        b"{\"nested\":{\"x\":0,\"x\":1}}",
        b"[]",
        b"{\"x\":NaN}",
    ] {
        code(
            a.handle(request("POST", "/api/create", raw)).unwrap(),
            400,
            "invalid_request",
        );
    }
    let mut r = request("POST", "/api/create", b"{}");
    r.headers.retain(|(k, _)| k != "content-type");
    code(a.handle(r).unwrap(), 415, "content_type_rejected");
    assert_eq!(a.service().calls.load(Ordering::SeqCst), 0);
}
#[test]
fn launch_is_single_use_and_cookie_is_protected() {
    let a = app();
    login(&a);
    let mut r = request("POST", "/session", b"launch=launch-canary");
    r.headers.retain(|(k, _)| k != "content-type");
    r.headers.push((
        "content-type".into(),
        "application/x-www-form-urlencoded".into(),
    ));
    code(a.handle(r).unwrap(), 403, "launch_expired");
    code(
        a.handle(request("GET", "/api/status", b"")).unwrap(),
        503,
        "status_unavailable",
    );
}
#[test]
fn service_errors_remain_closed_and_headers_do_not_cache() {
    let a = app();
    login(&a);
    let reply = a.handle(request("POST", "/api/create", b"{}")).unwrap();
    assert_eq!(reply.header("cache-control"), Some("no-store"));
    assert_eq!(reply.header("referrer-policy"), Some("no-referrer"));
    code(reply, 400, "consent_required");
    assert_eq!(a.service().calls.load(Ordering::SeqCst), 1);
}
#[test]
fn snapshot_schema_rejects_unexpected_or_missing_top_level_fields() {
    assert!(Snapshot::parse(json!({"secret":"never public"})).is_err());
    let value = include_str!("../contracts/status-example.json");
    let mut document: Value = serde_json::from_str(value).unwrap();
    assert!(Snapshot::parse(document.clone()).is_ok());
    document["native_credential"] = json!("canary");
    assert!(Snapshot::parse(document).is_err());
}
#[test]
fn publication_rejects_stale_completion_and_retains_failed_last_good() {
    let p = PublishedStatus::new();
    let value: Value =
        serde_json::from_str(include_str!("../contracts/status-example.json")).unwrap();
    p.publish(2, Snapshot::parse(value.clone()).unwrap())
        .unwrap();
    assert!(p.publish(1, Snapshot::parse(value).unwrap()).is_err());
    p.record_refresh_failure();
    let snapshot = p.read().unwrap();
    assert_eq!(snapshot["status_snapshot"]["refresh_failed"], true);
    assert!(snapshot["status_snapshot"]["age_seconds"].as_f64().unwrap() >= 0.0);
}

/// Records the product GETs it is handed (B13): the desktop only routes and authenticates them.
struct Product(AtomicUsize);
impl ProductRoutes for Product {
    fn get(&self, request: &Request) -> Result<Reply, PublicError> {
        self.0.fetch_add(1, Ordering::SeqCst);
        Reply::json(200, &json!({"target": request.target}))
    }
}
#[test]
fn product_routes_are_absent_unless_served_and_then_need_the_session() {
    // A desktop without product routes (the packaged host today): unchanged 404 before the session.
    let plain = app();
    let mut anonymous = request("GET", "/api/v1/product/snapshot", b"");
    anonymous.headers.retain(|(k, _)| k != "cookie");
    code(plain.handle(anonymous.clone()).unwrap(), 404, "not_found");
    // With them: protected like /api/status, then handed over without any service call.
    let product = Arc::new(Product(AtomicUsize::new(0)));
    let served = app().with_product(product.clone());
    for path in [
        "/api/v1/product/snapshot",
        "/api/v1/product/events",
        "/api/v1/product/export",
    ] {
        let mut r = anonymous.clone();
        r.target = path.into();
        code(served.handle(r).unwrap(), 401, "open_from_native_launcher");
    }
    assert_eq!(product.0.load(Ordering::SeqCst), 0);
    login(&served);
    for path in [
        "/api/v1/product/snapshot",
        "/api/v1/product/events",
        "/api/v1/product/export",
    ] {
        let reply = served.handle(request("GET", path, b"")).unwrap();
        assert_eq!(reply.status, 200);
        assert_eq!(
            serde_json::from_slice::<Value>(&reply.body).unwrap(),
            json!({"target": path})
        );
        assert!(reply.header("content-security-policy").is_some());
    }
    // Other paths are untouched: an unknown GET is still 404, an action still reaches the service.
    code(
        served
            .handle(request("GET", "/api/v1/product/other", b""))
            .unwrap(),
        404,
        "not_found",
    );
    assert_eq!(product.0.load(Ordering::SeqCst), 3);
    assert_eq!(served.service().calls.load(Ordering::SeqCst), 0);
}
