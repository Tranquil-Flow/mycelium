// SPDX-License-Identifier: AGPL-3.0-only
//! O:path:mycelium_swarm_directory/server.py — fake store/QR, real HTTP.
use api::{
    directory::{Directory, Service, client_key},
    http::{Request, serve_connection},
    schema::PublicError,
};
use budgets::BudgetId;
use journey_tests::duration;
use serde_json::{Map, Value, json};
use std::{
    io::{Read, Write},
    net::{Ipv4Addr, TcpListener, TcpStream},
    sync::Mutex,
    thread,
};
const ID: &str = "sw-abcdefghijklmnopqrst";
struct Fake {
    seen: Mutex<Vec<String>>,
}
impl Service for Fake {
    fn get(&self, _: &str) -> Result<Value, PublicError> {
        panic!("must pass client context")
    }
    fn signed(&self, _: &str, _: &Map<String, Value>) -> Result<Value, PublicError> {
        panic!("must pass client context")
    }
    fn get_for_client(&self, path: &str, client: &str) -> Result<Value, PublicError> {
        self.seen.lock().unwrap().push(client.into());
        Ok(json!({"path":path}))
    }
    fn signed_for_client(
        &self,
        path: &str,
        _: &Map<String, Value>,
        client: &str,
    ) -> Result<Value, PublicError> {
        self.seen.lock().unwrap().push(format!("{client}:{path}"));
        Err(PublicError::new(403, "signature_invalid"))
    }
    fn link_svg(&self, id: &str, client: &str) -> Result<Vec<u8>, PublicError> {
        assert_eq!(id, ID);
        assert_eq!(client, "ip:127.0.0.1");
        Ok(b"<svg>fake renderer</svg>".to_vec())
    }
}
fn req(method: &str, path: &str) -> Request {
    Request {
        method: method.into(),
        target: path.into(),
        headers: vec![],
        body: vec![],
    }
}
#[test]
fn qr_dispatch_and_trusted_proxy_identity_follow_directory_not_chat_policy() {
    let d = Directory::new(Fake {
        seen: Mutex::new(vec![]),
    });
    let path = format!("/swarms/api/v1/swarms/{ID}/link.svg");
    let get = d
        .handle_from(req("GET", &path), Ipv4Addr::LOCALHOST.into(), false)
        .unwrap();
    assert_eq!(get.status, 200);
    assert_eq!(get.header("content-type"), Some("image/svg+xml"));
    let head = d
        .handle_from(req("HEAD", &path), Ipv4Addr::LOCALHOST.into(), false)
        .unwrap();
    assert!(head.body.is_empty());
    assert_eq!(
        head.header("content-length"),
        Some(get.body.len().to_string().as_str())
    );
    assert_eq!(
        d.handle_from(
            req("GET", "/swarms/api/v1/swarms/bad/link.svg"),
            Ipv4Addr::LOCALHOST.into(),
            false
        )
        .unwrap()
        .status,
        404
    );
    let mut r = req("GET", "/swarms/api/v1/swarms");
    r.headers
        .push(("cf-connecting-ip".into(), "2001:db8::1234".into()));
    assert_eq!(
        client_key(&r, Ipv4Addr::LOCALHOST.into(), false),
        "ip:127.0.0.1"
    );
    assert_eq!(
        client_key(&r, Ipv4Addr::LOCALHOST.into(), true),
        "ip:2001:db8::1234"
    ); // Directory retains spelling; no chat /64 bucketing.
    d.handle_from(r, Ipv4Addr::LOCALHOST.into(), true).unwrap();
    assert_eq!(*d.service().seen.lock().unwrap(), vec!["ip:2001:db8::1234"]);
}
#[test]
fn native_directory_refuses_credentials_before_waiting_for_posted_body() {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    let addr = listener.local_addr().unwrap();
    let d = Directory::new(Fake {
        seen: Mutex::new(vec![]),
    });
    let job = thread::spawn(move || {
        let (stream, peer) = listener.accept().unwrap();
        serve_connection(
            stream,
            duration(BudgetId::ModelSelection).unwrap(),
            |r| d.preflight(r),
            |r| d.handle_from(r, peer.ip(), false),
        )
        .unwrap();
        assert!(d.service().seen.lock().unwrap().is_empty());
    });
    let mut c = TcpStream::connect(addr).unwrap();
    c.set_read_timeout(Some(duration(BudgetId::OwnerPoll).unwrap()))
        .unwrap();
    // Deliberately withhold body: response must precede it.
    write!(c,"POST /swarms/api/v1/records HTTP/1.1\r\nHost: {addr}\r\nContent-Type: application/json\r\nX-Mycelium-Swarms: 1\r\nCookie: fake\r\nContent-Length: 40\r\n\r\n").unwrap();
    let mut raw = String::new();
    c.read_to_string(&mut raw).unwrap();
    assert!(raw.contains("400 Bad Request"));
    assert!(raw.contains("credentials_refused"));
    drop(c);
    job.join().unwrap();
}
