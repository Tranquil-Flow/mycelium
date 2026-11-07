// SPDX-License-Identifier: AGPL-3.0-only
//! Framing/terminal-order contract, not runtime cancellation or product SSE proof.
use api::schema::PublicError;
use api::sse::{EventStream, EventValidator};
use serde_json::{Value, json};
struct FakeProtocol;
impl EventValidator for FakeProtocol {
    fn validate(&self, v: &Value) -> Result<(), PublicError> {
        // Controlled boundary only. Integrator must wire protocol::request::event.
        if v["protocol"] != "mycelium.request_event.v1" || !v["request_id"].is_string() {
            return Err(PublicError::new(502, "invalid_stream_event"));
        }
        Ok(())
    }
}
fn event(sequence: u64, kind: &str) -> Value {
    json!({"protocol":"mycelium.request_event.v1","request_id":"canary","publisher_generation":1,"sequence":sequence,"type":kind})
}
#[test]
fn frames_preserve_identity_utf8_and_terminal_order() {
    let mut stream = EventStream::new(FakeProtocol, vec![]);
    let mut wire = Vec::new();
    stream.write(&mut wire, event(0, "accepted")).unwrap();
    let mut token = event(1, "token");
    token["text"] = json!("héllo\nworld");
    token["token_index"] = json!(0);
    stream.write(&mut wire, token).unwrap();
    stream.write(&mut wire, event(2, "completed")).unwrap();
    let before = wire.clone();
    assert!(stream.write(&mut wire, event(3, "cancelled")).is_err());
    assert_eq!(wire, before);
    let frames = journey_tests::parse_sse(std::str::from_utf8(&wire).unwrap()).unwrap();
    assert_eq!(frames.len(), 3);
    assert_eq!(frames[0]["id"], "1:0");
    assert_eq!(frames[1]["event"], "token");
    assert_eq!(frames[2]["event"], "completed");
    assert_eq!(
        serde_json::from_str::<Value>(frames[1]["data"].as_str().unwrap()).unwrap()["text"],
        "héllo\nworld"
    );
}
#[test]
fn malformed_or_credential_bearing_event_is_refused_before_any_bytes() {
    let mut stream = EventStream::new(FakeProtocol, vec![b"service-token-canary".to_vec()]);
    let mut wire = Vec::new();
    let mut token = event(0, "token");
    token["text"] = json!("service-token-canary");
    assert_eq!(
        stream.write(&mut wire, token).unwrap_err().code,
        "credential_leak_blocked"
    );
    assert!(wire.is_empty());
    let mut bad = event(0, "bad\nevent");
    assert!(stream.write(&mut wire, bad.clone()).is_err());
    assert!(wire.is_empty());
    bad["protocol"] = json!("unsupported");
    assert!(stream.write(&mut wire, bad).is_err());
    assert!(wire.is_empty());
}
#[test]
fn write_failure_is_observable_and_fences_later_terminal() {
    struct Broken;
    impl std::io::Write for Broken {
        fn write(&mut self, _: &[u8]) -> std::io::Result<usize> {
            Err(std::io::ErrorKind::BrokenPipe.into())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut stream = EventStream::new(FakeProtocol, vec![]);
    let failure = stream.write(&mut Broken, event(0, "accepted")).unwrap_err();
    assert_eq!(failure.io_kind, Some(std::io::ErrorKind::BrokenPipe));
    let mut wire = Vec::new();
    assert!(stream.write(&mut wire, event(1, "completed")).is_err());
    assert!(wire.is_empty());
}
#[test]
fn real_tcp_flush_delivers_first_frame_before_terminal_is_released() {
    use std::{
        io::{BufRead, BufReader, Write},
        net::{Ipv4Addr, TcpListener, TcpStream},
        sync::mpsc,
        thread,
    };
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let addr = listener.local_addr().unwrap();
    let (release, held) = mpsc::channel();
    let worker = thread::spawn(move || {
        let (mut socket, _) = listener.accept().unwrap();
        socket
            .set_write_timeout(Some(
                journey_tests::duration(budgets::BudgetId::ModelSelection).unwrap(),
            ))
            .unwrap();
        let mut stream = EventStream::new(FakeProtocol, vec![]);
        socket
            .write_all(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n",
            )
            .unwrap();
        stream.write(&mut socket, event(0, "accepted")).unwrap();
        held.recv().unwrap();
        stream.write(&mut socket, event(1, "completed")).unwrap();
    });
    let socket = TcpStream::connect(addr).unwrap();
    socket
        .set_read_timeout(Some(
            journey_tests::duration(budgets::BudgetId::ModelSelection).unwrap(),
        ))
        .unwrap();
    let mut reader = BufReader::new(socket);
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        if line == "\r\n" {
            break;
        }
    }
    let mut first = String::new();
    loop {
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        first.push_str(&line);
        if line == "\n" {
            break;
        }
    }
    assert!(first.contains("event: accepted\n"));
    assert!(!worker.is_finished());
    release.send(()).unwrap();
    let mut rest = String::new();
    std::io::Read::read_to_string(&mut reader, &mut rest).unwrap();
    worker.join().unwrap();
    let frames = journey_tests::parse_sse(&(first + &rest)).unwrap();
    assert_eq!(frames[1]["event"], "completed");
}
#[test]
fn reordered_duplicate_or_other_request_events_are_not_published() {
    let mut stream = EventStream::new(FakeProtocol, vec![]);
    let mut wire = Vec::new();
    stream.write(&mut wire, event(3, "token")).unwrap(); // resumed stream may start after zero
    let before = wire.clone();
    assert!(stream.write(&mut wire, event(3, "token")).is_err());
    let mut wrong = event(4, "token");
    wrong["request_id"] = json!("other");
    assert!(stream.write(&mut wire, wrong).is_err());
    assert_eq!(wire, before);
}
