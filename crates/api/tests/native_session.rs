// SPDX-License-Identifier: AGPL-3.0-only
//! Fake monotonic time and host-file/entropy boundary; actual Desktop handlers.
use api::{
    desktop::{Desktop, Service, Session},
    http::{Reply, Request},
    native_session::{NativeLauncher, SessionClock, launch_lifetime, session_lifetime},
    schema::{PublicError, Snapshot},
    status::PublishedStatus,
};
use serde_json::{Map, Value, json};
use std::{
    sync::{Arc, Mutex},
    time::Instant,
};
struct Clock(Mutex<Instant>);
impl SessionClock for Clock {
    fn now(&self) -> Instant {
        *self.0.lock().unwrap()
    }
}
#[derive(Default)]
struct Host {
    issued: usize,
    launch: Option<String>,
    closed: bool,
    fail_write: bool,
    fail_remove: bool,
}
struct Launcher(Arc<Mutex<Host>>);
impl std::ops::Deref for Launcher {
    type Target = Mutex<Host>;
    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
impl NativeLauncher for Launcher {
    fn closed(&self) -> bool {
        self.lock().unwrap().closed
    }
    fn fresh_token(&mut self) -> Result<String, PublicError> {
        let mut host = self.lock().unwrap();
        host.issued += 1;
        Ok(format!("test-token-{}", host.issued))
    }
    fn publish_launch(&mut self, origin: &str, token: &str) -> Result<(), PublicError> {
        assert_eq!(origin, "http://127.0.0.1:12345");
        let mut h = self.lock().unwrap();
        if h.fail_write {
            return Err(PublicError::new(503, "launch_write_failed"));
        }
        h.launch = Some(token.into());
        Ok(())
    }
    fn consume_launch(&mut self) -> Result<(), PublicError> {
        let mut h = self.lock().unwrap();
        if h.fail_remove {
            return Err(PublicError::new(503, "launch_remove_failed"));
        }
        h.launch = None;
        Ok(())
    }
}
struct NoActions;
impl Service for NoActions {
    fn action(&self, _: &str, _: &Map<String, Value>) -> Result<Value, PublicError> {
        panic!("session must not execute backend action")
    }
}
fn app() -> (Desktop<NoActions>, Arc<Clock>, Arc<Mutex<Host>>) {
    let clock = Arc::new(Clock(Mutex::new(Instant::now())));
    let host = Arc::new(Mutex::new(Host::default()));
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
    let session = Session::native(
        "native-secret".into(),
        Box::new(Launcher(host.clone())),
        clock.clone(),
    )
    .unwrap();
    (
        Desktop::new(
            "127.0.0.1:12345".into(),
            Arc::new(NoActions),
            status,
            session,
        ),
        clock,
        host,
    )
}
fn req(path: &str, body: &[u8]) -> Request {
    Request {
        method: "POST".into(),
        target: path.into(),
        headers: vec![
            ("host".into(), "127.0.0.1:12345".into()),
            ("content-length".into(), body.len().to_string()),
            ("content-type".into(), "application/json".into()),
            ("x-native-authorization".into(), "native-secret".into()),
        ],
        body: body.into(),
    }
}
fn sign(app: &Desktop<NoActions>, token: &str) -> Reply {
    let mut r = req("/session", format!("launch={token}").as_bytes());
    r.headers.retain(|(k, _)| k != "content-type");
    r.headers.push((
        "content-type".into(),
        "application/x-www-form-urlencoded".into(),
    ));
    r.headers.push(("origin".into(), "null".into()));
    app.handle(r).unwrap()
}
fn status(app: &Desktop<NoActions>, cookie: &str) -> Reply {
    let mut r = req("/api/status", b"");
    r.method = "GET".into();
    r.headers.push(("cookie".into(), cookie.into()));
    app.handle(r).unwrap()
}
fn error(reply: Reply, status: u16, code: &str) {
    assert_eq!(reply.status, status);
    assert_eq!(
        serde_json::from_slice::<Value>(&reply.body).unwrap(),
        json!({"error":code})
    );
}
#[test]
fn exact_session_expiry_is_not_sliding_and_native_reopen_requires_new_one_use_login() {
    let (app, clock, host) = app();
    // Independent oracle constants, app.py:872 and :1995 @ f4e366e9.
    assert_eq!(launch_lifetime().unwrap().as_secs(), 120);
    assert_eq!(session_lifetime().unwrap().as_secs(), 3600);
    let start = clock.now();
    let r = app.handle(req("/native-session", b"{}")).unwrap();
    assert_eq!(r.status, 200);
    assert_eq!(
        serde_json::from_slice::<Value>(&r.body).unwrap(),
        json!({"launch_file_ready":true})
    );
    assert!(r.header("set-cookie").is_none());
    let launch = host.lock().unwrap().launch.clone().unwrap();
    let login = sign(&app, &launch);
    assert_eq!(login.status, 303);
    assert_eq!(
        login.header("set-cookie"),
        Some("a13session=test-token-2; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600")
    );
    assert!(host.lock().unwrap().launch.is_none());
    error(sign(&app, &launch), 403, "launch_expired");
    let old = "a13session=test-token-2";
    *clock.0.lock().unwrap() = start + session_lifetime().unwrap() - launch_lifetime().unwrap();
    assert_eq!(status(&app, old).status, 200);
    *clock.0.lock().unwrap() = start + session_lifetime().unwrap();
    error(status(&app, old), 401, "open_from_native_launcher");
    assert_eq!(
        app.handle(req("/native-session", b"{}")).unwrap().status,
        200
    );
    error(status(&app, old), 401, "open_from_native_launcher");
    let next = host.lock().unwrap().launch.clone().unwrap();
    assert_ne!(next, launch);
    assert_eq!(sign(&app, &next).status, 303);
    error(status(&app, old), 401, "open_from_native_launcher");
    assert_eq!(status(&app, "a13session=test-token-4").status, 200);
    *clock.0.lock().unwrap() += session_lifetime().unwrap();
    error(
        status(&app, "a13session=test-token-4"),
        401,
        "open_from_native_launcher",
    );
}
#[test]
fn launch_expiry_replacement_auth_precedence_and_closed_state_match_oracle() {
    let (app, clock, host) = app();
    let mut r = req("/native-session", b"invalid");
    r.headers.retain(|(k, _)| k != "x-native-authorization");
    error(app.handle(r).unwrap(), 403, "native_authorization_required");
    let mut r = req("/native-session", b"invalid");
    r.headers.push(("origin".into(), "null".into()));
    error(app.handle(r).unwrap(), 403, "native_only");
    error(
        app.handle(req("/native-session", b"{ }")).unwrap(),
        400,
        "invalid_request",
    );
    assert_eq!(host.lock().unwrap().issued, 0);
    app.handle(req("/native-session", b"{}")).unwrap();
    let expired = host.lock().unwrap().launch.clone().unwrap();
    *clock.0.lock().unwrap() += launch_lifetime().unwrap();
    error(sign(&app, &expired), 403, "launch_expired");
    app.handle(req("/native-session", b"{}")).unwrap();
    let replaced = host.lock().unwrap().launch.clone().unwrap();
    app.handle(req("/native-session", b"{}")).unwrap();
    error(sign(&app, &replaced), 403, "launch_expired");
    let fresh = host.lock().unwrap().launch.clone().unwrap();
    host.lock().unwrap().closed = true;
    error(
        app.handle(req("/native-session", b"{}")).unwrap(),
        409,
        "application_closed",
    );
    error(sign(&app, &fresh), 409, "application_closed");
}
#[test]
fn file_failures_never_return_success_and_failed_consumption_stays_one_use() {
    let (app, _, host) = app();
    host.lock().unwrap().fail_write = true;
    error(
        app.handle(req("/native-session", b"{}")).unwrap(),
        503,
        "launch_write_failed",
    );
    host.lock().unwrap().fail_write = false;
    app.handle(req("/native-session", b"{}")).unwrap();
    let token = host.lock().unwrap().launch.clone().unwrap();
    host.lock().unwrap().fail_remove = true;
    error(sign(&app, &token), 503, "launch_remove_failed");
    host.lock().unwrap().fail_remove = false;
    error(sign(&app, &token), 403, "launch_expired");
}
#[test]
fn native_renewal_crosses_real_http_without_cookie_or_origin_authority() {
    use api::http::serve_connection;
    use budgets::BudgetId;
    use journey_tests::{duration, http::Client};
    use std::{
        net::{Ipv4Addr, TcpListener},
        thread,
    };
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let address = listener.local_addr().unwrap().to_string();
    let origin = format!("http://{address}");
    let clock = Arc::new(Clock(Mutex::new(Instant::now())));
    // This fixture's host permits the random high-port origin; it never opens
    // a browser, touches private user files, or generates real credentials.
    struct HttpHost(Arc<Mutex<Host>>);
    impl NativeLauncher for HttpHost {
        fn closed(&self) -> bool {
            false
        }
        fn fresh_token(&mut self) -> Result<String, PublicError> {
            Launcher(self.0.clone()).fresh_token()
        }
        fn publish_launch(&mut self, origin: &str, token: &str) -> Result<(), PublicError> {
            assert!(origin.starts_with("http://127.0.0.1:"));
            self.0.lock().unwrap().launch = Some(token.into());
            Ok(())
        }
        fn consume_launch(&mut self) -> Result<(), PublicError> {
            Launcher(self.0.clone()).consume_launch()
        }
    }
    let host = Arc::new(Mutex::new(Host::default()));
    let published = Arc::new(PublishedStatus::new());
    published
        .publish(
            1,
            Snapshot::parse(
                serde_json::from_str(include_str!("../contracts/status-example.json")).unwrap(),
            )
            .unwrap(),
        )
        .unwrap();
    let session = Session::native(
        "native-secret".into(),
        Box::new(HttpHost(host.clone())),
        clock.clone(),
    )
    .unwrap();
    let app = Desktop::new(address, Arc::new(NoActions), published, session);
    let job = thread::spawn(move || {
        for _ in 0..8 {
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
    let mut client = Client::new(&origin).unwrap();
    let headers = [
        ("Content-Type", "application/json"),
        ("X-Native-Authorization", "native-secret"),
    ];
    let first = client
        .request(
            "POST",
            "/native-session",
            b"{}",
            &headers,
            false,
            BudgetId::ModelSelection,
        )
        .unwrap();
    assert_eq!(first.status, 200);
    let launch = host.lock().unwrap().launch.clone().unwrap();
    client.sign_in(&launch).unwrap();
    let live = client.get("/api/status").unwrap();
    *clock.0.lock().unwrap() += session_lifetime().unwrap();
    let expired = client.get("/api/status").unwrap();
    let renewed = client
        .request(
            "POST",
            "/native-session",
            b"{}",
            &headers,
            false,
            BudgetId::ModelSelection,
        )
        .unwrap();
    assert_eq!(renewed.status, 200);
    let next = host.lock().unwrap().launch.clone().unwrap();
    client.sign_in(&next).unwrap();
    let current = client.get("/api/status").unwrap();
    let stale = client
        .request(
            "GET",
            "/api/status",
            b"",
            &[("Cookie", "a13session=test-token-2")],
            false,
            BudgetId::ModelSelection,
        )
        .unwrap();
    job.join().unwrap();
    assert_eq!(live.status, 200);
    assert_eq!(expired.status, 401);
    assert_eq!(
        expired.json().unwrap(),
        json!({"error":"open_from_native_launcher"})
    );
    assert_eq!(current.status, 200);
    assert_eq!(stale.status, 401);
}
#[test]
fn launch_form_percent_decoding_matches_native_browser_form() {
    let (app, _, _) = app();
    app.handle(req("/native-session", b"{}")).unwrap();
    assert_eq!(sign(&app, "%74est-token-1").status, 303);
}
