// SPDX-License-Identifier: AGPL-3.0-only
//! The two HTTPS clients a joined member uses through the PUBLIC relay (`JoinWindowClient` for the
//! pairing window, `SeedClient` for `/seed/join` and the heartbeats), against a local TLS server that
//! behaves like a relay edge: chunked and `Content-Length` replies, the `Date` header, and a peer that
//! does NOT hang up after a complete reply although the request said `Connection: close`.
//!
//! v0.1.4 reads a reply by its framing (`http.client`: `Content-Length` or the final chunk) and returns;
//! it never waits for the socket to close (pairing.py:871-905, seed http.py `_request`). A client that
//! reads to EOF would sit until its 10 s timeout on such a peer and fail the join with
//! `pairing_window_timeout`. Expected values: the oracle's framing semantics above (a complete reply is
//! returned as soon as it is complete); the closed codes are the ones of `join_client` / `seed` fixtures.
use api::{
    join_client::JoinWindowClient,
    seed::{SeedClient, SeedOptions},
};
use budgets::{BudgetId, Entries};
use rustls::{ServerConnection, StreamOwned};
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::{SocketAddr, TcpListener},
    path::PathBuf,
    sync::{Arc, OnceLock},
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

fn budget(id: BudgetId) -> Duration {
    Duration::from_millis(id.span(Entries::new(0)).expect("budget").millis())
}
fn poll_every() -> Duration {
    budget(BudgetId::OwnerPoll)
}
/// The client's own timeout (v0.1.4 `JoinWindowClient(timeout=10.0)`: twice the registry's 5 s).
fn client_timeout() -> Duration {
    budget(BudgetId::ModelSelection) * 2
}
/// A complete reply must come back long before the client's own timeout.
fn prompt() -> Duration {
    budget(BudgetId::ModelSelection)
}

struct Tls {
    ca_pem: Vec<u8>,
    config: Arc<rustls::ServerConfig>,
}

fn tls() -> &'static Tls {
    static TLS: OnceLock<Tls> = OnceLock::new();
    TLS.get_or_init(|| {
        let base = std::fs::canonicalize(std::env::temp_dir()).expect("tmp");
        let dir: PathBuf = base.join(format!("l10r17-framing-{}", std::process::id()));
        std::fs::remove_dir_all(&dir).unwrap_or_default();
        std::fs::create_dir_all(&dir).expect("dir");
        std::fs::set_permissions(&dir, std::os::unix::fs::PermissionsExt::from_mode(0o700))
            .expect("mode");
        let material =
            seed::tls::ensure_local_tls_material(&dir.join("tls")).expect("tls material");
        let ca_pem = std::fs::read(&material.ca_certificate).expect("ca");
        let config = seed::tls::server_config(&material).expect("server config");
        std::fs::remove_dir_all(&dir).unwrap_or_default();
        Tls { ca_pem, config }
    })
}

/// One reply per accepted connection; after writing it the server keeps the socket open for `hold`
/// (or until the client hangs up).
fn serve(replies: Vec<Vec<u8>>, hold: Duration) -> (SocketAddr, JoinHandle<()>) {
    let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind");
    let address = listener.local_addr().expect("addr");
    let config = tls().config.clone();
    let handle = thread::spawn(move || {
        for reply in replies {
            let Ok((tcp, _)) = listener.accept() else {
                return;
            };
            tcp.set_read_timeout(Some(budget(BudgetId::ModelSelection)))
                .expect("read timeout");
            let Ok(connection) = ServerConnection::new(config.clone()) else {
                return;
            };
            let mut stream = StreamOwned::new(connection, tcp);
            // The request: head, then Content-Length bytes.
            let mut request = Vec::new();
            let mut chunk = [0u8; 4096];
            loop {
                match stream.read(&mut chunk) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => request.extend_from_slice(&chunk[..n]),
                }
                let Some(split) = request.windows(4).position(|w| w == b"\r\n\r\n") else {
                    continue;
                };
                let head = String::from_utf8_lossy(&request[..split]).to_lowercase();
                let length = head
                    .lines()
                    .find_map(|l| l.strip_prefix("content-length:"))
                    .and_then(|v| v.trim().parse::<usize>().ok())
                    .unwrap_or(0);
                if request.len() >= split + 4 + length {
                    break;
                }
            }
            if stream
                .write_all(&reply)
                .and_then(|()| stream.flush())
                .is_err()
            {
                continue; // the client refused the handshake and is gone
            }
            // Hold the connection open: the peer must not need our hang-up.
            let until = Instant::now() + hold;
            stream
                .sock
                .set_read_timeout(Some(poll_every()))
                .expect("read timeout");
            while Instant::now() < until {
                match stream.read(&mut chunk) {
                    Ok(0) => break,
                    Ok(_) => {}
                    Err(e)
                        if matches!(
                            e.kind(),
                            std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut
                        ) =>
                    {
                        continue;
                    } // still holding the socket open
                    Err(_) => break,
                }
            }
        }
    });
    (address, handle)
}

fn http(status: &str, headers: &[&str], body: &[u8]) -> Vec<u8> {
    let mut out = format!("HTTP/1.1 {status}\r\n").into_bytes();
    for header in headers {
        out.extend_from_slice(header.as_bytes());
        out.extend_from_slice(b"\r\n");
    }
    out.extend_from_slice(b"\r\n");
    out.extend_from_slice(body);
    out
}
fn chunked(body: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    for part in body.chunks(7) {
        out.extend_from_slice(format!("{:x}\r\n", part.len()).as_bytes());
        out.extend_from_slice(part);
        out.extend_from_slice(b"\r\n");
    }
    out.extend_from_slice(b"0\r\n\r\n");
    out
}

/// The peer keeps its socket open much longer than the client would ever wait.
fn hold() -> Duration {
    client_timeout() * 2
}

fn window_client(address: SocketAddr) -> JoinWindowClient {
    JoinWindowClient::new(
        &format!("https://127.0.0.1:{}", address.port()),
        client_timeout(),
    )
    .expect("origin")
    .with_tls_roots_pem(&tls().ca_pem)
}

#[test]
fn a_complete_content_length_reply_returns_without_the_peer_hanging_up() {
    let body = br#"{"state":"open"}"#;
    let reply = http(
        "200 OK",
        &[
            "Content-Type: application/json",
            &format!("Content-Length: {}", body.len()),
        ],
        body,
    );
    let (address, server) = serve(vec![reply], hold());
    let mut client = window_client(address);
    let started = Instant::now();
    let got = client
        .post("/pairing-window/x/poll", b"{}", "application/json")
        .expect("a complete reply");
    assert_eq!(got, body);
    assert!(
        started.elapsed() < prompt(),
        "waited {:?} for a hang-up that v0.1.4 never needs",
        started.elapsed()
    );
    drop(client);
    drop(server);
}

#[test]
fn a_complete_chunked_reply_returns_without_the_peer_hanging_up() {
    let body = br#"{"state":"claimed","detail":"a body spread over several chunks"}"#;
    let mut reply = http(
        "200 OK",
        &[
            "Content-Type: application/json",
            "Transfer-Encoding: chunked",
        ],
        b"",
    );
    reply.extend_from_slice(&chunked(body));
    let (address, server) = serve(vec![reply], hold());
    let mut client = window_client(address);
    let started = Instant::now();
    let got = client
        .post("/pairing-window/x/poll", b"{}", "application/json")
        .expect("a complete chunked reply");
    assert_eq!(got, body);
    assert!(
        started.elapsed() < prompt(),
        "waited {:?}",
        started.elapsed()
    );
    drop(server);
}

#[test]
fn the_owners_closed_code_and_the_date_skew_come_through_tls() {
    let refused = http(
        "409 Conflict",
        &["Content-Type: application/json", "Content-Length: 37"],
        br#"{"error":"pairing_window_code_wrong"}"#,
    );
    // 37 is the exact length of the body above.
    assert_eq!(br#"{"error":"pairing_window_code_wrong"}"#.len(), 37);
    let dated = http(
        "200 OK",
        &["Content-Length: 2", "Date: Wed, 28 Oct 2026 08:00:00 GMT"],
        b"{}",
    );
    let (address, server) = serve(vec![refused, dated], poll_every());
    let mut client = window_client(address).with_clock(Box::new(|| {
        // 2026-10-28 09:00:00 UTC: one hour after the server's Date
        1_793_178_000.0
    }));
    let error = client
        .post("/pairing-window/x/poll", b"{}", "application/json")
        .expect_err("a 409 with the owner's code");
    assert_eq!(error.code, "pairing_window_code_wrong");
    client
        .post("/pairing-window/x/poll", b"{}", "application/json")
        .expect("a dated reply");
    assert_eq!(
        client.warnings(),
        &[json!({"code": "clock_skew", "skew_seconds": -3600})]
    );
    drop(server);
}

#[test]
fn a_certificate_the_system_does_not_trust_is_pairing_window_tls_failed() {
    let (address, server) = serve(
        vec![http("200 OK", &["Content-Length: 2"], b"{}")],
        poll_every(),
    );
    // No test roots: the system Web-PKI store must refuse the installation CA.
    let mut client = JoinWindowClient::new(
        &format!("https://127.0.0.1:{}", address.port()),
        client_timeout(),
    )
    .expect("origin");
    let error = client
        .post("/pairing-window/x/poll", b"{}", "application/json")
        .expect_err("untrusted certificate");
    assert_eq!(error.code, "pairing_window_tls_failed");
    drop(server);
}

#[test]
fn the_seed_client_also_returns_a_complete_reply_without_a_hang_up() {
    let body = br#"{"protocol":"p"}"#;
    let reply = http(
        "200 OK",
        &[
            "Content-Type: application/json",
            &format!("Content-Length: {}", body.len()),
        ],
        body,
    );
    let (address, server) = serve(vec![reply], hold());
    let options = SeedOptions {
        ca_pem: Some(tls().ca_pem.clone()),
        connect_to: Some(address),
        server_name: Some("127.0.0.1".into()),
    };
    let digest = "sha256:".to_owned() + &"ab".repeat(32);
    let client = SeedClient::new(
        &format!("https://127.0.0.1:{}", address.port()),
        "sw-framing",
        &digest,
        &[json!({"verification_key_digest": digest})],
        &options,
    )
    .expect("client");
    let started = Instant::now();
    let answer: Value = client.resume(&json!({"x": 1})).expect("a complete reply");
    assert_eq!(answer, json!({"protocol": "p"}));
    assert!(
        started.elapsed() < prompt(),
        "waited {:?} for a hang-up",
        started.elapsed()
    );
    drop(server);
}
