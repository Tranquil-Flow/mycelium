// SPDX-License-Identifier: AGPL-3.0-only
use api::swarm::publisher::{DirectoryClient, DirectoryPort};
use budgets::{BudgetId, Entries};
use rustls::{RootCertStore, ServerConfig, ServerConnection, StreamOwned};
use rustls_pki_types::{CertificateDer, PrivateKeyDer, pem::PemObject};
use serde_json::Value;
use std::{
    io::{self, Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant},
};

fn budget() -> Duration {
    Duration::from_millis(
        BudgetId::PyMyceliumA13MacosSwarmPublishDirectoryclientInitTimeoutL54
            .span(Entries::ZERO)
            .unwrap()
            .millis(),
    )
}
fn tls() -> (RootCertStore, Arc<ServerConfig>) {
    let doc: Value = serde_json::from_str(include_str!("fixtures/publisher_tls.json")).unwrap();
    let mut roots = RootCertStore::empty();
    roots
        .add(CertificateDer::from_pem_slice(doc["ca_pem"].as_str().unwrap().as_bytes()).unwrap())
        .unwrap();
    let cert = CertificateDer::from_pem_slice(doc["server_cert_pem"].as_str().unwrap().as_bytes())
        .unwrap();
    let key =
        PrivateKeyDer::from_pem_slice(doc["server_key_pem"].as_str().unwrap().as_bytes()).unwrap();
    let config =
        ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(vec![cert], key)
            .unwrap();
    (roots, Arc::new(config))
}
fn read_request(stream: &mut impl Read) {
    let mut raw = Vec::new();
    let mut buf = [0; 4096];
    while !raw.windows(4).any(|w| w == b"\r\n\r\n") {
        let n = stream.read(&mut buf).unwrap();
        assert_ne!(n, 0);
        raw.extend_from_slice(&buf[..n]);
    }
}
// An interruptible fault schedule, not a readiness sleep. Every interval derives
// from the real product budget; no shortened timeout or wall-clock override.
fn stopped(stop: &mpsc::Receiver<()>, delay: Duration) -> bool {
    match stop.recv_timeout(delay) {
        Ok(()) | Err(mpsc::RecvTimeoutError::Disconnected) => true,
        Err(mpsc::RecvTimeoutError::Timeout) => false,
    }
}
fn assert_deadline(result: Result<Value, api::swarm::publisher::Error>, elapsed: Duration) {
    assert_eq!(
        result.unwrap_err().code,
        "swarm_directory_deadline_exceeded"
    );
    assert!(
        elapsed < budget() + budget() / 4,
        "deadline overrun: {elapsed:?}"
    );
}

#[test]
fn late_plain_http_completion_is_not_accepted() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let origin = format!("http://{}", listener.local_addr().unwrap());
    let client = DirectoryClient::new(&origin).unwrap();
    let (done, stop) = mpsc::channel();
    let server = thread::spawn(move || {
        let (mut tcp, _) = listener.accept().unwrap();
        tcp.set_read_timeout(Some(budget())).unwrap();
        read_request(&mut tcp);
        if stopped(&stop, budget() * 3 / 4) {
            return;
        }
        tcp.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{")
            .unwrap();
        if stopped(&stop, budget() / 2) {
            return;
        }
        if let Err(e) = tcp.write_all(b"}") {
            eprintln!("late_plain_peer_closed: {e}");
        }
    });
    let start = Instant::now();
    let result = client.call("GET", "/records", b"");
    let elapsed = start.elapsed();
    drop(done);
    server.join().unwrap();
    assert_deadline(result, elapsed);
}

#[test]
fn trickled_handshake_bytes_cannot_extend_the_absolute_deadline() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let (roots, _) = tls();
    let client =
        DirectoryClient::with_transport("https://directory.example.org", roots, move |_, _| {
            Ok(vec![address])
        })
        .unwrap();
    let (done, stop) = mpsc::channel();
    let server = thread::spawn(move || {
        let (mut tcp, _) = listener.accept().unwrap();
        tcp.set_read_timeout(Some(budget())).unwrap();
        let mut hello = [0; 4096];
        assert!(tcp.read(&mut hello).unwrap() > 0);
        // Partial TLS record header: rustls keeps doing underlying reads.
        for byte in [0x16, 0x03, 0x03] {
            if let Err(e) = tcp.write_all(&[byte]) {
                eprintln!("handshake_peer_closed: {e}");
                return;
            }
            if stopped(&stop, budget() * 3 / 4) {
                return;
            }
        }
    });
    let start = Instant::now();
    let result = client.call("GET", "/records", b"");
    let elapsed = start.elapsed();
    drop(done);
    server.join().unwrap();
    assert_deadline(result, elapsed);
}

#[test]
fn trickled_tls_record_after_verified_handshake_cannot_extend_deadline() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let (roots, config) = tls();
    let client =
        DirectoryClient::with_transport("https://directory.example.org", roots, move |_, _| {
            Ok(vec![address])
        })
        .unwrap();
    let (done, stop) = mpsc::channel();
    let (handshake, verified) = mpsc::channel();
    let server = thread::spawn(move || {
        let (tcp, _) = listener.accept().unwrap();
        tcp.set_read_timeout(Some(budget())).unwrap();
        tcp.set_write_timeout(Some(budget())).unwrap();
        let mut stream = StreamOwned::new(ServerConnection::new(config).unwrap(), tcp);
        read_request(&mut stream);
        handshake.send(()).unwrap();
        stream
            .conn
            .writer()
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            .unwrap();
        let mut record = Vec::new();
        while stream.conn.wants_write() {
            stream.conn.write_tls(&mut record).unwrap();
        }
        assert!(record.len() > 3);
        for byte in &record[..3] {
            if let Err(e) = stream.sock.write_all(&[*byte]) {
                eprintln!("tls_record_peer_closed: {e}");
                return;
            }
            if stopped(&stop, budget() * 3 / 4) {
                return;
            }
        }
    });
    let start = Instant::now();
    let result = client.call("GET", "/records", b"");
    let elapsed = start.elapsed();
    drop(done);
    server.join().unwrap();
    verified
        .try_recv()
        .expect("verified TLS handshake and real HTTP request");
    assert_deadline(result, elapsed);
}

#[test]
fn verified_tls_healthy_response_still_succeeds() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let (roots, config) = tls();
    let client =
        DirectoryClient::with_transport("https://directory.example.org", roots, move |_, _| {
            Ok(vec![address])
        })
        .unwrap();
    let server = thread::spawn(move || {
        let (tcp, _) = listener.accept().unwrap();
        tcp.set_read_timeout(Some(budget())).unwrap();
        let mut stream = StreamOwned::new(ServerConnection::new(config).unwrap(), tcp);
        read_request(&mut stream);
        stream
            .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            .unwrap();
        stream.flush().unwrap();
    });
    let result = client.call("GET", "/records", b"");
    server.join().unwrap();
    assert_eq!(result.unwrap(), serde_json::json!({}));
}

#[test]
fn blocked_resolution_retains_one_worker_and_refuses_later_attempts() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let address = listener.local_addr().unwrap();
    let calls = Arc::new(AtomicUsize::new(0));
    let (finished, exited) = mpsc::channel();
    let client = Arc::new(
        DirectoryClient::with_transport(&format!("http://{address}"), RootCertStore::empty(), {
            let calls = calls.clone();
            move |_, _| -> io::Result<_> {
                calls.fetch_add(1, Ordering::SeqCst);
                // Controlled DNS backend: a real TCP exchange held by the test.
                let mut socket = TcpStream::connect(address)?;
                socket.write_all(b"resolve!")?;
                let mut release = [0];
                socket.read_exact(&mut release)?;
                finished.send(()).unwrap();
                Ok(vec![address])
            }
        })
        .unwrap(),
    );
    let first = thread::spawn({
        let client = client.clone();
        move || client.call("GET", "/records", b"")
    });
    let (mut held, _) = listener.accept().unwrap();
    held.set_read_timeout(Some(budget())).unwrap();
    let mut query = [0; 8];
    held.read_exact(&mut query).unwrap();
    assert_eq!(&query, b"resolve!");
    let first = first.join().unwrap();
    let second = client.call("GET", "/records", b"");
    let spawned = calls.load(Ordering::SeqCst);
    held.write_all(b"!").unwrap();
    for _ in 1..spawned {
        let (mut tcp, _) = listener.accept().unwrap();
        tcp.write_all(b"!").unwrap();
    }
    for _ in 0..spawned {
        exited.recv_timeout(budget()).unwrap();
    }
    assert_eq!(spawned, 1, "timed-out resolver workers must not accumulate");
    assert!(first.is_err());
    assert_eq!(
        second.unwrap_err().code,
        "swarm_directory_resolution_pending"
    );
}
