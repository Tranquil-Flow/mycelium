// SPDX-License-Identifier: AGPL-3.0-only
//! O:path:mycelium_request_gateway/asgi.py; real HTTP/SSE, fake request ownership.
use api::{
    http::{Request, Response, serve_response_connection},
    requests::{Accepted, Gateway, Service, Subscription},
    schema::PublicError,
};
use budgets::BudgetId;
use journey_tests::duration;
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::{Ipv4Addr, TcpListener, TcpStream},
    sync::{Arc, Mutex, mpsc},
    thread,
};
struct Fake {
    trace: Arc<Mutex<Vec<String>>>,
    release: Mutex<Option<mpsc::Receiver<()>>>,
}
struct Events {
    trace: Arc<Mutex<Vec<String>>>,
    release: Option<mpsc::Receiver<()>>,
    seq: u64,
}
impl Subscription for Events {
    fn next(&mut self) -> Result<Option<Value>, PublicError> {
        self.seq += 1;
        if self.seq == 2
            && let Some(gate) = self.release.take()
        {
            gate.recv_timeout(duration(BudgetId::ModelSelection).unwrap())
                .unwrap();
        }
        if self.seq > 2 {
            return Ok(None);
        }
        Ok(Some(
            json!({"protocol":"mycelium.request_event.v1","type":if self.seq==1 {"accepted"} else {"completed"},"publisher_generation":1,"sequence":self.seq,"request_id":"r"}),
        ))
    }
    fn ack(&mut self, seq: u64) -> Result<(), PublicError> {
        self.trace.lock().unwrap().push(format!("ack:{seq}"));
        Ok(())
    }
    fn close(&mut self) -> Result<(), PublicError> {
        self.trace.lock().unwrap().push("close".into());
        Ok(())
    }
}
impl Service for Fake {
    fn authorized(&self, r: &Request) -> bool {
        r.header("authorization") == Some("Bearer fake")
    }
    fn health(&self) -> Result<Value, PublicError> {
        Ok(json!({"ok":true}))
    }
    fn qualification(&self) -> Result<Value, PublicError> {
        Err(PublicError::new(400, "route_not_ready"))
    }
    fn recovery(&self) -> Result<Value, PublicError> {
        Ok(json!({"state":"idle"}))
    }
    fn submit(&self, _: Value) -> Result<Accepted, PublicError> {
        Ok(Accepted {
            request_id: "r".into(),
            session_token: "x".repeat(32),
        })
    }
    fn cancel(&self, id: &str, token: Option<&str>) -> Result<bool, PublicError> {
        if token != Some("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx") {
            return Err(PublicError::new(400, "request_owner_mismatch"));
        }
        if id == "missing" {
            return Err(PublicError::new(400, "unknown_request"));
        }
        Ok(false)
    }
    fn subscribe(
        &self,
        id: &str,
        cursor: Option<(u64, u64)>,
        token: Option<&str>,
    ) -> Result<Box<dyn Subscription>, PublicError> {
        assert_eq!(id, "r");
        assert_eq!(cursor, Some((1, 0)));
        assert_eq!(token, Some("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"));
        Ok(Box::new(Events {
            trace: self.trace.clone(),
            release: self.release.lock().unwrap().take(),
            seq: 0,
        }))
    }
    fn validate_event(&self, _: &Value) -> Result<(), PublicError> {
        Ok(())
    } // Deliberately fake L5 seam.
}
fn fake() -> Fake {
    Fake {
        trace: Arc::default(),
        release: Mutex::new(None),
    }
}
fn request(method: &str, path: &str) -> Request {
    Request {
        method: method.into(),
        target: path.into(),
        headers: vec![("authorization".into(), "Bearer fake".into())],
        body: vec![],
    }
}
fn buffered(response: Response) -> api::http::Reply {
    match response {
        Response::Buffered(r) => r,
        Response::Stream { .. } => panic!("unexpected stream"),
    }
}
#[test]
fn authorization_route_method_and_owner_precedence() {
    let app = Gateway::new(fake());
    let mut r = request("POST", "/v1/inference/r/events");
    r.headers.clear();
    let reply = buffered(app.handle(r.clone()).unwrap());
    assert_eq!(reply.status, 401);
    assert_eq!(reply.header("www-authenticate"), Some("Bearer"));
    r.target = "/unknown".into();
    assert_eq!(buffered(app.handle(r).unwrap()).status, 404);
    assert_eq!(
        buffered(app.handle(request("POST", "/healthz")).unwrap()).header("allow"),
        Some("GET")
    );
    assert_eq!(
        buffered(
            app.handle(request("GET", "/v1/qualification/current"))
                .unwrap()
        )
        .status,
        503
    );
    let mut r = request("DELETE", "/v1/inference/r");
    assert_eq!(buffered(app.handle(r.clone()).unwrap()).status, 409);
    r.headers
        .push(("x-mycelium-session".into(), "x".repeat(32)));
    let reply = buffered(app.handle(r).unwrap());
    assert_eq!(reply.status, 200);
    assert_eq!(
        serde_json::from_slice::<Value>(&reply.body).unwrap(),
        json!({"request_id":"r","status":"terminal"})
    );
}
#[test]
fn malformed_cursor_or_owner_never_subscribes() {
    let app = Gateway::new(fake());
    for value in [
        "0:0",
        "1:-1",
        "1:2:3",
        "9223372036854775808:0",
        "1: 1",
        "é:1",
    ] {
        let mut r = request("GET", "/v1/inference/r/events");
        r.headers.push(("last-event-id".into(), value.into()));
        let reply = buffered(app.handle(r).unwrap());
        assert_eq!(reply.status, 409);
        assert_eq!(
            serde_json::from_slice::<Value>(&reply.body).unwrap()["error"],
            "invalid_last_event_id"
        );
    }
    let mut r = request("GET", "/v1/inference/r/events");
    r.headers.extend([
        ("last-event-id".into(), "1:0".into()),
        ("last-event-id".into(), "1:0".into()),
    ]);
    assert_eq!(buffered(app.handle(r).unwrap()).status, 409);
}
#[test]
fn failed_sse_write_does_not_ack_and_subscription_close_is_explicit() {
    struct Broken;
    impl Write for Broken {
        fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
            Err(std::io::ErrorKind::BrokenPipe.into())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            panic!("flush after failed write")
        }
    }
    let fake = fake();
    let trace = fake.trace.clone();
    let app = Gateway::new(fake);
    let mut r = request("GET", "/v1/inference/r/events");
    r.headers.extend([
        ("last-event-id".into(), "1:0".into()),
        ("x-mycelium-session".into(), "x".repeat(32)),
    ]);
    let Response::Stream { mut body, .. } = app.handle(r).unwrap() else {
        panic!("stream required")
    };
    let e = body.write(&mut Broken).unwrap_err();
    assert_eq!(e.code, "stream_write_failed");
    assert_eq!(e.io_kind, Some(std::io::ErrorKind::BrokenPipe));
    assert!(trace.lock().unwrap().is_empty());
    body.close().unwrap();
    assert_eq!(*trace.lock().unwrap(), vec!["close"]);
}

#[test]
fn actual_http_sse_flushes_before_terminal_ack_and_closes_subscription() {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let addr = listener.local_addr().unwrap();
    let (tx, rx) = mpsc::channel();
    let fake = fake();
    let trace = fake.trace.clone();
    *fake.release.lock().unwrap() = Some(rx);
    let gateway = Gateway::new(fake);
    let job = thread::spawn(move || {
        let (stream, _) = listener.accept().unwrap();
        serve_response_connection(
            stream,
            duration(BudgetId::ModelSelection).unwrap(),
            262144,
            |r| gateway.preflight(r),
            |r| gateway.handle(r),
        )
        .unwrap();
    });
    let mut stream = TcpStream::connect(addr).unwrap();
    stream
        .set_read_timeout(Some(duration(BudgetId::ModelSelection).unwrap()))
        .unwrap();
    write!(stream,"GET /v1/inference/r/events HTTP/1.1\r\nHost: {addr}\r\nAuthorization: Bearer fake\r\nX-Mycelium-Session: {}\r\nLast-Event-ID: 1:0\r\n\r\n","x".repeat(32)).unwrap();
    let mut reader = BufReader::new(stream);
    let mut line = String::new();
    let mut headers = String::new();
    loop {
        line.clear();
        reader.read_line(&mut line).unwrap();
        headers.push_str(&line);
        if line == "\r\n" {
            break;
        }
    }
    assert!(headers.contains("200 OK"));
    assert!(headers.contains("text/event-stream"));
    assert!(!headers.to_ascii_lowercase().contains("content-length"));
    assert!(headers.contains("x-accel-buffering: no"));
    let mut first = String::new();
    loop {
        line.clear();
        reader.read_line(&mut line).unwrap();
        first.push_str(&line);
        if line == "\n" {
            break;
        }
    }
    assert!(first.contains("event: accepted"));
    assert!(!job.is_finished());
    tx.send(()).unwrap();
    let mut rest = String::new();
    reader.read_to_string(&mut rest).unwrap();
    drop(reader);
    job.join().unwrap();
    assert!(rest.contains("event: completed"));
    assert!(!rest.contains(&"x".repeat(32)));
    assert_eq!(*trace.lock().unwrap(), vec!["ack:1", "ack:2", "close"]);
}
