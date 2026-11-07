// SPDX-License-Identifier: AGPL-3.0-only
//! The concrete signed directory behind the real HTTP boundary (server.py:144-224 @ f4e366e9):
//! exact `/config`, QR/link routes, CSP variants and the full signed write path over TCP.
use api::{
    directory::Directory,
    http::{Reply, Request, serve_connection},
    swarm::{
        core::{Config, Directory as Core},
        limits::Limits,
        service::{NativeQr, QrSvg, SwarmService, helper_download},
        store::MemoryStore,
        wire::ed25519_verify,
    },
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use ed25519_dalek::{Signer as _, SigningKey};
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::{Ipv4Addr, TcpListener, TcpStream},
    sync::Arc,
    thread,
};

const ORIGIN: &str = "https://swarms.mycelium.now";
struct FixedQr;
impl QrSvg for FixedQr {
    fn svg(&self, text: &str) -> Result<Vec<u8>, api::schema::PublicError> {
        Ok(format!("<svg>{text}</svg>").into_bytes())
    }
}
fn core(loopback: bool) -> Core<MemoryStore> {
    Core::new(
        MemoryStore::new(),
        Box::new(ed25519_verify),
        Box::new(|| 1_800_000_000),
        Config {
            limits: Limits::source().unwrap(),
            allow_loopback_entries: loopback,
            allow_private_entries: false,
        },
    )
    .unwrap()
}
fn app<Q: QrSvg>(loopback: bool, qr: Q) -> Directory<SwarmService<MemoryStore, Q>> {
    Directory::new(SwarmService::new(
        core(loopback),
        ORIGIN,
        helper_download(),
        qr,
    ))
}
fn req(method: &str, target: &str, body: &[u8]) -> Request {
    let mut headers = vec![("content-length".to_string(), body.len().to_string())];
    if method == "POST" {
        headers.push(("content-type".into(), "application/json".into()));
        headers.push(("x-mycelium-swarms".into(), "1".into()));
    }
    Request {
        method: method.into(),
        target: target.into(),
        headers,
        body: body.into(),
    }
}
fn text(r: &Reply) -> String {
    String::from_utf8(r.body.clone()).unwrap()
}
fn signed_record() -> Vec<u8> {
    let sk = SigningKey::from_bytes(&[1; 32]);
    let owner = format!(
        "ed25519:{}",
        URL_SAFE_NO_PAD.encode(sk.verifying_key().to_bytes())
    );
    let body = json!({"protocol":"mycelium.swarm.record.v1","swarm_id":"sw-aaaaaaaaaaaaaaaaaaaa","owner_key":owner,"seq":1,"name":"Net","model":"Qwen/Qwen3-0.6B","description":"","access_mode":"request","open_quota":null,"entry":null});
    let mut message = b"mycelium-swarm-sig-v1\n".to_vec();
    message.extend(serde_json::to_vec(&body).unwrap());
    let mut doc = body;
    doc["sig"] = json!(URL_SAFE_NO_PAD.encode(sk.sign(&message).to_bytes()));
    serde_json::to_vec(&doc).unwrap()
}
#[test]
fn config_is_exact_unlimited_and_in_source_key_order() {
    let a = app(false, NativeQr);
    for _ in 0..300 {
        assert_eq!(
            a.handle(req("GET", "/swarms/api/v1/config", b""))
                .unwrap()
                .status,
            200
        );
    }
    let r = a.handle(req("GET", "/swarms/api/v1/config", b"")).unwrap();
    assert_eq!(r.header("content-type"), Some("application/json"));
    assert_eq!(
        text(&r),
        format!(
            "{{\"directory\":\"{ORIGIN}\",\"privacy\":{},\"demo\":{},\"download\":{{\"available\":false,\"url\":null,\"sha256\":null,\"version\":\"0.1.0-preview.2\",\"notarized\":false}},\"limits\":{{\"max_prompt_chars\":2000,\"max_new_tokens\":256}}}}",
            serde_json::to_string(api::swarm::core::PRIVACY_NOTE).unwrap(),
            serde_json::to_string(api::swarm::service::DEMO_LABEL).unwrap()
        )
    );
}
#[test]
fn csp_adds_loopback_connect_source_only_when_the_directory_allows_loopback_entries() {
    let strict = app(false, NativeQr)
        .handle(req("GET", "/swarms/api/v1/config", b""))
        .unwrap();
    let dev = app(true, NativeQr)
        .handle(req("GET", "/swarms/api/v1/config", b""))
        .unwrap();
    assert!(
        strict
            .header("content-security-policy")
            .unwrap()
            .contains("connect-src 'self' https:; object-src")
    );
    assert!(
        dev.header("content-security-policy")
            .unwrap()
            .contains("connect-src 'self' https: http://127.0.0.1:*; object-src")
    );
}
#[test]
fn signed_write_then_listing_link_and_head_over_the_boundary() {
    let a = app(false, FixedQr);
    let wrote = a
        .handle(req("POST", "/swarms/api/v1/records", &signed_record()))
        .unwrap();
    assert_eq!(wrote.status, 200, "{}", text(&wrote));
    assert!(text(&wrote).starts_with("{\"swarm\":{\"swarm_id\":\"sw-aaaaaaaaaaaaaaaaaaaa\""));
    let list = a.handle(req("GET", "/swarms/api/v1/swarms", b"")).unwrap();
    assert!(text(&list).starts_with("{\"swarms\":[{\"swarm_id\":\"sw-aaaaaaaaaaaaaaaaaaaa\""));
    let head = a.handle(req("HEAD", "/swarms/api/v1/swarms", b"")).unwrap();
    assert!(head.body.is_empty());
    assert_eq!(
        head.header("content-length"),
        Some(list.body.len().to_string().as_str())
    );
    let svg = a
        .handle(req(
            "GET",
            "/swarms/api/v1/swarms/sw-aaaaaaaaaaaaaaaaaaaa/link.svg",
            b"",
        ))
        .unwrap();
    assert_eq!(svg.header("content-type"), Some("image/svg+xml"));
    let link = text(&svg)
        .trim_start_matches("<svg>")
        .trim_end_matches("</svg>")
        .to_string();
    // The link is exactly mycelium-swarm:<b64url(canonical({directory, owner_key, protocol, swarm_id}))>.
    let encoded = link.strip_prefix("mycelium-swarm:").unwrap();
    let decoded = String::from_utf8(URL_SAFE_NO_PAD.decode(encoded).unwrap()).unwrap();
    assert!(decoded.starts_with(&format!(
        "{{\"directory\":\"{ORIGIN}\",\"owner_key\":\"ed25519:"
    )));
    assert!(decoded.ends_with(
        ",\"protocol\":\"mycelium.swarm.link.v1\",\"swarm_id\":\"sw-aaaaaaaaaaaaaaaaaaaa\"}"
    ));
    let unknown = a
        .handle(req(
            "GET",
            "/swarms/api/v1/swarms/sw-bbbbbbbbbbbbbbbbbbbb/link.svg",
            b"",
        ))
        .unwrap();
    assert_eq!(
        (unknown.status, text(&unknown)),
        (404, "{\"error\":\"swarm_unknown\"}".to_string())
    );
    // The real renderer: the exact bytes server.py's qr_svg produces for this swarm's link
    // (`L10-evidence/r7-probe-qr-directory.py`, executed oracle).
    let oracle: Value =
        serde_json::from_str(include_str!("fixtures/oracle_qr_directory.json")).unwrap();
    let native = app(false, NativeQr);
    native
        .handle(req("POST", "/swarms/api/v1/records", &signed_record()))
        .unwrap();
    let served = native
        .handle(req(
            "GET",
            "/swarms/api/v1/swarms/sw-aaaaaaaaaaaaaaaaaaaa/link.svg",
            b"",
        ))
        .unwrap();
    assert_eq!(served.status, 200);
    assert_eq!(served.header("content-type"), Some("image/svg+xml"));
    assert_eq!(text(&served), oracle["svg"].as_str().unwrap());
    let unknown = native
        .handle(req(
            "GET",
            "/swarms/api/v1/swarms/sw-bbbbbbbbbbbbbbbbbbbb/link.svg",
            b"",
        ))
        .unwrap();
    assert_eq!(
        (unknown.status, text(&unknown)),
        (404, "{\"error\":\"swarm_unknown\"}".to_string())
    );
}
#[test]
fn transport_ordering_keeps_closed_public_errors() {
    let a = app(false, NativeQr);
    let bad = a
        .handle(req("POST", "/swarms/api/v1/records", b"{\"a\":1,\"a\":2}"))
        .unwrap();
    assert_eq!(
        (bad.status, text(&bad)),
        (400, "{\"error\":\"json_duplicate_key\"}".to_string())
    );
    let mut chunked = req("POST", "/swarms/api/v1/records", b"{}");
    chunked
        .headers
        .push(("transfer-encoding".into(), "chunked".into()));
    let both = a.handle(chunked).unwrap();
    assert_eq!(
        (both.status, text(&both)),
        (400, "{\"error\":\"frame_invalid\"}".to_string())
    );
    let wrong = a.handle(req("GET", "/swarms/api/v1/records", b"")).unwrap();
    assert_eq!(wrong.status, 405);
}
#[test]
fn signed_record_over_real_tcp_loopback() {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let address = listener.local_addr().unwrap();
    let a = Arc::new(app(false, NativeQr));
    let server = {
        let a = a.clone();
        thread::spawn(move || {
            for _ in 0..2 {
                let (stream, _) = listener.accept().unwrap();
                serve_connection(
                    stream,
                    journey_tests::duration(budgets::BudgetId::ModelSelection).unwrap(),
                    |r| a.preflight(r),
                    |r| a.handle(r),
                )
                .unwrap();
            }
        })
    };
    let body = signed_record();
    let mut wire = format!("POST /swarms/api/v1/records HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nX-Mycelium-Swarms: 1\r\nContent-Length: {}\r\n\r\n", body.len()).into_bytes();
    wire.extend(&body);
    let mut out = String::new();
    let mut s = TcpStream::connect(address).unwrap();
    s.write_all(&wire).unwrap();
    s.shutdown(std::net::Shutdown::Write).unwrap();
    s.read_to_string(&mut out).unwrap();
    assert!(out.starts_with("HTTP/1.1 200"), "{out}");
    let mut s = TcpStream::connect(address).unwrap();
    s.write_all(b"GET /swarms/api/v1/swarms HTTP/1.1\r\nHost: x\r\n\r\n")
        .unwrap();
    s.shutdown(std::net::Shutdown::Write).unwrap();
    let mut listing = String::new();
    s.read_to_string(&mut listing).unwrap();
    server.join().unwrap();
    assert!(
        listing.contains("{\"swarms\":[{\"swarm_id\":\"sw-aaaaaaaaaaaaaaaaaaaa\""),
        "{listing}"
    );
    let parsed: Value = serde_json::from_str(listing.split_once("\r\n\r\n").unwrap().1).unwrap();
    assert_eq!(parsed["swarms"].as_array().map(Vec::len), Some(1));
}

/// INTEGRATOR RULING 2 (v0.1.4 parity): a storage exception is not caught anywhere in
/// directory.py/server.py; the handler thread dies, `handle_error` is a no-op and the socket is
/// closed with no bytes written. Measured on the unmodified oracle (Python 3.14.7, a store whose
/// every call raises `sqlite3.OperationalError`): `L10-evidence/r6-oracle-store.txt` — GET
/// /swarms, GET /swarms/<id>, GET .../link.svg and a signed POST all return NO BYTES, while
/// GET /config still answers 200 and the server keeps serving.
mod store_failure {
    use super::*;
    use api::swarm::{
        json::J,
        store::{StoreError, StoreResult},
    };
    struct Broken;
    impl api::swarm::store::Store for Broken {
        fn get(&self, _: &str, _: &str) -> StoreResult<Option<J>> {
            Err(StoreError("store_sqlite_failed"))
        }
        fn put(&self, _: &str, _: &str, _: &J) -> StoreResult<()> {
            Err(StoreError("store_sqlite_failed"))
        }
        fn delete(&self, _: &str, _: &str) -> StoreResult<()> {
            Err(StoreError("store_sqlite_failed"))
        }
        fn scan(&self, _: &str, _: &str) -> StoreResult<Vec<(String, J)>> {
            Err(StoreError("store_sqlite_failed"))
        }
    }
    fn broken_app() -> Directory<SwarmService<Broken, FixedQr>> {
        Directory::new(SwarmService::new(
            Core::new(
                Broken,
                Box::new(ed25519_verify),
                Box::new(|| 1_800_000_000),
                Config {
                    limits: Limits::source().unwrap(),
                    allow_loopback_entries: false,
                    allow_private_entries: false,
                },
            )
            .unwrap(),
            ORIGIN,
            helper_download(),
            FixedQr,
        ))
    }
    /// One real TCP exchange; returns (bytes the client received, what the server loop returned).
    fn exchange(wire: Vec<u8>) -> (Vec<u8>, bool) {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let a = Arc::new(broken_app());
        let server = {
            let a = a.clone();
            thread::spawn(move || {
                let (stream, _) = listener.accept().unwrap();
                serve_connection(
                    stream,
                    journey_tests::duration(budgets::BudgetId::ModelSelection).unwrap(),
                    |r| a.preflight(r),
                    |r| a.handle(r),
                )
                .is_err()
            })
        };
        let mut s = TcpStream::connect(address).unwrap();
        s.write_all(&wire).unwrap();
        s.shutdown(std::net::Shutdown::Write).unwrap();
        let mut out = Vec::new();
        if let Err(error) = s.read_to_end(&mut out) {
            // a reset after the drop is the same observable as a clean close: no bytes
            assert_eq!(error.kind(), std::io::ErrorKind::ConnectionReset);
        }
        (out, server.join().unwrap())
    }
    fn get(path: &str) -> Vec<u8> {
        format!("GET {path} HTTP/1.1\r\nHost: x\r\n\r\n").into_bytes()
    }
    #[test]
    fn storage_failure_writes_no_bytes_on_every_store_touching_route() {
        let body = signed_record();
        let mut post = format!("POST /swarms/api/v1/records HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nX-Mycelium-Swarms: 1\r\nContent-Length: {}\r\n\r\n", body.len()).into_bytes();
        post.extend(&body);
        for (name, wire) in [
            ("list", get("/swarms/api/v1/swarms")),
            ("one", get("/swarms/api/v1/swarms/sw-aaaaaaaaaaaaaaaaaaaa")),
            (
                "link",
                get("/swarms/api/v1/swarms/sw-aaaaaaaaaaaaaaaaaaaa/link.svg"),
            ),
            ("signed post", post),
        ] {
            let (received, failed) = exchange(wire);
            assert!(
                received.is_empty(),
                "{name}: v0.1.4 writes nothing, got {:?}",
                String::from_utf8_lossy(&received)
            );
            assert!(failed, "{name}: the connection handler reports the failure");
        }
    }
    #[test]
    fn storage_failure_does_not_affect_store_free_routes() {
        let (received, failed) = exchange(get("/swarms/api/v1/config"));
        let text = String::from_utf8(received).unwrap();
        assert!(text.starts_with("HTTP/1.1 200"), "{text}");
        assert!(!failed);
    }
    #[test]
    fn transport_free_handle_surfaces_the_failure_instead_of_a_reply() {
        let a = broken_app();
        let error = a
            .handle(req("GET", "/swarms/api/v1/swarms", b""))
            .expect_err("no reply may be produced for a storage failure");
        assert!(error.is_dropped(), "{error}");
    }
}
