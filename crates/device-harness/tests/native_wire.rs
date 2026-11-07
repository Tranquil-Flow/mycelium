// SPDX-License-Identifier: AGPL-3.0-only
//! Native loopback wire against a REAL listener owned by this test (ephemeral 127.0.0.1
//! port; no proxy, DNS, app, device or remote peer). The raw request bytes are captured so
//! the exact framing, and the absence of any redirect follow, are asserted on the wire.
use budgets::{BudgetId, Bytes, Entries, Span};
use device_harness::{native_io::Wire, native_session::Request, native_wire::NativeWire};
use std::{
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddrV4, TcpListener, TcpStream},
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};

fn wait_for(id: BudgetId) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}

struct Server {
    port: u16,
    seen: Arc<Mutex<Vec<Vec<u8>>>>,
    accepted: Arc<AtomicUsize>,
    write_errors: Arc<Mutex<Vec<String>>>,
    thread: Option<JoinHandle<()>>,
}
impl Server {
    /// One scripted raw reply per accepted connection; `None` = read the request and stall
    /// until the client gives up.
    fn new(replies: Vec<Option<Vec<u8>>>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let seen = Arc::new(Mutex::new(vec![]));
        let accepted = Arc::new(AtomicUsize::new(0));
        let write_errors = Arc::new(Mutex::new(vec![]));
        let (s, a, w) = (seen.clone(), accepted.clone(), write_errors.clone());
        let thread = std::thread::spawn(move || {
            for reply in replies {
                let (mut conn, _) = listener.accept().unwrap();
                a.fetch_add(1, Ordering::SeqCst);
                conn.set_read_timeout(Some(wait_for(BudgetId::WorkerStartup)))
                    .unwrap();
                let mut raw = vec![];
                let mut buf = [0u8; 4096];
                loop {
                    let n = conn.read(&mut buf).unwrap_or(0);
                    raw.extend_from_slice(&buf[..n]);
                    if n == 0 || complete(&raw) {
                        break;
                    }
                }
                s.lock().unwrap().push(raw);
                match reply {
                    Some(bytes) => {
                        // A client that already refused an oversize reply closes early.
                        if let Err(e) = conn.write_all(&bytes) {
                            w.lock().unwrap().push(e.to_string());
                        }
                    }
                    None => while conn.read(&mut buf).unwrap_or(0) > 0 {},
                }
            }
        });
        Self {
            port,
            seen,
            accepted,
            write_errors,
            thread: Some(thread),
        }
    }
    fn address(&self) -> SocketAddrV4 {
        SocketAddrV4::new(Ipv4Addr::LOCALHOST, self.port)
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        // Feed throw-away connections so a still-waiting accept loop can finish.
        if let Some(t) = self.thread.take() {
            while !t.is_finished() {
                if TcpStream::connect(self.address()).is_err() {
                    break;
                }
                std::thread::sleep(wait_for(BudgetId::OwnerPoll));
            }
            if t.join().is_err() && !std::thread::panicking() {
                panic!("fixture server thread panicked");
            }
        }
    }
}
fn complete(raw: &[u8]) -> bool {
    let Some(end) = raw.windows(4).position(|w| w == b"\r\n\r\n") else {
        return false;
    };
    let head = String::from_utf8_lossy(&raw[..end]).to_ascii_lowercase();
    let want = head
        .lines()
        .find_map(|l| l.strip_prefix("content-length: "))
        .and_then(|v| v.trim().parse::<usize>().ok())
        .unwrap_or(0);
    raw.len() >= end + 4 + want
}
fn http(status: &str, headers: &[(&str, &str)], body: &[u8]) -> Option<Vec<u8>> {
    let mut out = format!("HTTP/1.1 {status}\r\n");
    for (k, v) in headers {
        out += &format!("{k}: {v}\r\n");
    }
    out += &format!(
        "Content-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    let mut bytes = out.into_bytes();
    bytes.extend_from_slice(body);
    Some(bytes)
}
fn request(port: u16, method: &'static str, path: &'static str, body: &[u8]) -> Request {
    Request {
        port,
        path,
        method,
        headers: [
            ("Content-Type".to_string(), "application/json".to_string()),
            (
                "X-Native-Authorization".to_string(),
                "native-fixture".to_string(),
            ),
        ]
        .into(),
        body: body.to_vec(),
        follow_redirects: false,
    }
}
fn wire() -> NativeWire {
    NativeWire::new(Bytes::new(65_536)).unwrap()
}
fn bound() -> Span {
    Span::from_millis(5_000)
}

#[test]
fn exact_request_framing_and_json_reply() {
    let server = Server::new(vec![http(
        "200 OK",
        &[("Content-Type", "application/json")],
        br#"{"launch_file_ready":true}"#,
    )]);
    let reply = wire()
        .send(
            server.address(),
            request(server.port, "POST", "/native-session", b"{}"),
            bound(),
        )
        .unwrap();
    assert_eq!(reply.http, 200);
    assert_eq!(reply.json["launch_file_ready"], true);
    assert!(server.write_errors.lock().unwrap().is_empty());
    let raw = String::from_utf8(server.seen.lock().unwrap()[0].clone()).unwrap();
    assert_eq!(
        raw,
        format!(
            "POST /native-session HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\nContent-Length: 2\r\nContent-Type: application/json\r\nX-Native-Authorization: native-fixture\r\n\r\n{{}}",
            server.port
        )
    );
}

#[test]
fn redirect_is_returned_not_followed_and_set_cookie_headers_are_kept() {
    let server = Server::new(vec![
        http(
            "303 See Other",
            &[
                ("Location", "/"),
                ("Set-Cookie", "a13session=abc; HttpOnly; SameSite=Strict"),
            ],
            b"",
        ),
        http("200 OK", &[], b"{}"),
    ]);
    let mut req = request(server.port, "POST", "/session", b"launch=x");
    req.headers.clear();
    req.headers.insert(
        "Content-Type".into(),
        "application/x-www-form-urlencoded".into(),
    );
    req.headers.insert("Origin".into(), "null".into());
    let reply = wire().send(server.address(), req, bound()).unwrap();
    assert_eq!(reply.http, 303);
    assert!(reply.json.is_null(), "empty body is JSON null");
    assert!(reply.headers.iter().any(|(k, v)| {
        k.eq_ignore_ascii_case("set-cookie") && v.starts_with("a13session=abc;")
    }));
    std::thread::sleep(wait_for(BudgetId::OwnerPoll));
    assert_eq!(
        server.accepted.load(Ordering::SeqCst),
        1,
        "Location never requested"
    );
}

#[test]
fn unsafe_requests_are_refused_before_any_connection() {
    let server = Server::new(vec![]);
    for a in [
        SocketAddrV4::new(Ipv4Addr::new(127, 0, 0, 2), server.port),
        SocketAddrV4::new(Ipv4Addr::new(10, 0, 0, 1), server.port),
        SocketAddrV4::new(Ipv4Addr::UNSPECIFIED, server.port),
    ] {
        assert_eq!(
            wire()
                .send(
                    a,
                    request(server.port, "POST", "/native-session", b"{}"),
                    bound()
                )
                .err()
                .unwrap()
                .code,
            "native_wire_request_refused"
        );
    }
    let with = |k: &str, v: &str| {
        let mut r = request(server.port, "POST", "/x", b"");
        r.headers.insert(k.into(), v.into());
        r
    };
    let mut redirecting = request(server.port, "POST", "/x", b"{}");
    redirecting.follow_redirects = true;
    let cases = vec![
        redirecting,
        request(server.port.wrapping_add(1), "POST", "/x", b"{}"),
        request(server.port, "DELETE", "/x", b""),
        request(server.port, "POST", "/x y", b""),
        request(server.port, "POST", "/x\r\nHost: evil", b""),
        request(server.port, "POST", "http://evil/x", b""),
        with("Bad\r\nHeader", "v"),
        with("X-A", "v\r\nX-Injected: 1"),
        with("Host", "evil"),
        with("Content-Length", "999"),
        with("Transfer-Encoding", "chunked"),
        with("Proxy-Authorization", "x"),
        with("Connection", "keep-alive"),
    ];
    for r in cases {
        let (m, p) = (r.method, r.path);
        assert_eq!(
            wire()
                .send(server.address(), r, bound())
                .err()
                .unwrap()
                .code,
            "native_wire_request_refused",
            "{m} {p}"
        );
    }
    assert_eq!(server.accepted.load(Ordering::SeqCst), 0);
}

#[test]
fn malformed_oversize_and_ambiguous_replies_fail_closed() {
    let big = vec![b'a'; 70_000];
    let mut endless = b"HTTP/1.1 200 OK\r\nX: ".to_vec();
    endless.extend_from_slice(&big);
    let cases: Vec<(Option<Vec<u8>>, &str)> = vec![
        (
            Some(b"garbage\r\n\r\n".to_vec()),
            "native_wire_response_invalid",
        ),
        (
            Some(b"HTTP/1.1 20 OK\r\nContent-Length: 0\r\n\r\n".to_vec()),
            "native_wire_response_invalid",
        ),
        (
            http("200 OK", &[], b"not json"),
            "native_wire_response_invalid",
        ),
        (
            http("200 OK", &[("Transfer-Encoding", "chunked")], b"{}"),
            "native_wire_response_invalid",
        ),
        (
            Some(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}".to_vec()),
            "native_wire_response_invalid",
        ),
        (
            Some(b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\nConnection: close\r\n\r\n{}".to_vec()),
            "native_wire_response_truncated",
        ),
        (http("200 OK", &[], &big), "native_wire_response_too_large"),
        (Some(endless), "native_wire_response_too_large"),
    ];
    for (reply, code) in cases {
        let server = Server::new(vec![reply]);
        let err = wire()
            .send(
                server.address(),
                request(server.port, "POST", "/native-session", b"{}"),
                bound(),
            )
            .err()
            .unwrap();
        assert_eq!(err.code, code);
    }
}

#[test]
fn stalled_peer_and_refused_connection_are_bounded() {
    let server = Server::new(vec![None]);
    let stall_bound = Span::from_millis(300);
    let started = Instant::now();
    let err = wire()
        .send(
            server.address(),
            request(server.port, "POST", "/native-session", b"{}"),
            stall_bound,
        )
        .err()
        .unwrap();
    assert_eq!(err.code, "native_io_timeout");
    // Returned by its own deadline, not by the peer or a hung read.
    assert!(
        started.elapsed().as_millis() < u128::from(stall_bound.millis()) * 5,
        "{:?}",
        started.elapsed()
    );
    drop(server);
    // Closed port: nothing listening any more.
    let closed = {
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        l.local_addr().unwrap().port()
    };
    let err = wire()
        .send(
            SocketAddrV4::new(Ipv4Addr::LOCALHOST, closed),
            request(closed, "POST", "/native-session", b"{}"),
            bound(),
        )
        .err()
        .unwrap();
    assert_eq!(err.code, "native_wire_connect_failed");
}

#[test]
fn zero_response_cap_is_invalid() {
    assert_eq!(
        NativeWire::new(Bytes::ZERO).err().unwrap().code,
        "native_file_budget_invalid"
    );
}

#[test]
fn source_unbounded_response_limit_reads_what_the_bounded_wire_refuses() {
    // ~200 KB JSON: over the 64 KiB synthetic cap, accepted when the registry says
    // v0.1.4 never bounded it (RU-014, V03-L12-1).
    let mut body = br#"{"pad":""#.to_vec();
    body.extend(std::iter::repeat_n(b'a', 200_000));
    body.extend_from_slice(br#""}"#);
    let server = Server::new(vec![http("200 OK", &[], &body), http("200 OK", &[], &body)]);
    let refused = wire()
        .send(
            server.address(),
            request(server.port, "POST", "/x", b"{}"),
            bound(),
        )
        .err()
        .unwrap();
    assert_eq!(refused.code, "native_wire_response_too_large");
    let mut unbounded =
        NativeWire::new(device_harness::harness_budgets::SOURCE_UNBOUNDED_READ).unwrap();
    let reply = unbounded
        .send(
            server.address(),
            request(server.port, "POST", "/x", b"{}"),
            bound(),
        )
        .unwrap();
    assert_eq!(reply.json["pad"].as_str().unwrap().len(), 200_000);
}
