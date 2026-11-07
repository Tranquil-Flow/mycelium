// SPDX-License-Identifier: AGPL-3.0-only
//! O:path:mycelium_swarm_directory/server.py — renderer seam, not QR algorithm proof.
use api::{
    directory::{Directory, Service},
    http::serve_connection,
    schema::PublicError,
};
use budgets::BudgetId;
use journey_tests::{duration, http::Client};
use serde_json::{Map, Value};
use std::{
    io::{Read, Write},
    net::{Ipv4Addr, TcpListener, TcpStream},
    thread,
};
struct Renderer;
impl Service for Renderer {
    fn get(&self, _: &str) -> Result<Value, PublicError> {
        panic!("unexpected lookup")
    }
    fn signed(&self, _: &str, _: &Map<String, Value>) -> Result<Value, PublicError> {
        panic!("unexpected mutation")
    }
    fn link_svg(&self, id: &str, client: &str) -> Result<Vec<u8>, PublicError> {
        assert_eq!(id, "sw-abcdefghijklmnopqrst");
        assert_eq!(client, "ip:127.0.0.1");
        Ok(b"<svg>controlled-renderer</svg>".to_vec())
    }
}
#[test]
fn svg_get_and_head_cross_real_http_with_security_headers_and_no_json_wrapping() {
    let l = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let addr = l.local_addr().unwrap();
    let job = thread::spawn(move || {
        let app = Directory::new(Renderer);
        for _ in 0..2 {
            let (s, peer) = l.accept().unwrap();
            serve_connection(
                s,
                duration(BudgetId::ModelSelection).unwrap(),
                |r| app.preflight(r),
                |r| app.handle_from(r, peer.ip(), false),
            )
            .unwrap();
        }
    });
    let c = Client::new(&format!("http://{addr}")).unwrap();
    let path = "/swarms/api/v1/swarms/sw-abcdefghijklmnopqrst/link.svg";
    let get = c
        .request("GET", path, b"", &[], false, BudgetId::ModelSelection)
        .unwrap();
    assert_eq!(get.status, 200);
    assert_eq!(get.body, b"<svg>controlled-renderer</svg>");
    // Unchanged L3 client intentionally supports only its journey methods.
    let mut socket = TcpStream::connect(addr).unwrap();
    socket
        .set_read_timeout(Some(duration(BudgetId::ModelSelection).unwrap()))
        .unwrap();
    write!(socket, "HEAD {path} HTTP/1.1\r\nHost: {addr}\r\n\r\n").unwrap();
    let mut raw = String::new();
    socket.read_to_string(&mut raw).unwrap();
    drop(socket);
    let (head, body) = raw.split_once("\r\n\r\n").unwrap();
    assert!(head.starts_with("HTTP/1.1 200 OK"));
    assert!(body.is_empty());
    assert_eq!(
        get.headers.get("content-type").map(String::as_str),
        Some("image/svg+xml")
    );
    assert!(
        head.to_ascii_lowercase()
            .contains(&format!("content-length: {}", get.body.len()))
    );
    assert_eq!(
        get.headers
            .get("cross-origin-opener-policy")
            .map(String::as_str),
        Some("same-origin")
    );
    job.join().unwrap();
}
