// SPDX-License-Identifier: AGPL-3.0-only
use api::{
    http::{Reply, serve_connection},
    swarm::publisher::{DirectoryClient, DirectoryPort},
};
use budgets::{BudgetId, Entries};
use serde_json::json;
use std::{
    net::TcpListener,
    sync::{Arc, Mutex},
    time::Duration,
};

#[test]
fn publisher_announces_app_release_without_changing_signed_schema_or_following_redirects() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let origin = format!("http://{}", listener.local_addr().unwrap());
    let headers = Arc::new(Mutex::new(Vec::new()));
    let received = headers.clone();
    let target = format!("{origin}/must-not-follow");
    let server = std::thread::spawn(move || {
        let (socket, _) = listener.accept().unwrap();
        let budget = Duration::from_millis(
            BudgetId::PyMyceliumA13MacosSwarmPublishDirectoryclientInitTimeoutL54
                .span(Entries::ZERO)
                .unwrap()
                .millis(),
        );
        serve_connection(
            socket,
            budget,
            |_| Ok(None),
            |request| {
                *received.lock().unwrap() = request.headers;
                let mut reply = Reply::json(302, &json!({})).unwrap();
                reply.headers.push(("location".into(), target));
                Ok(reply)
            },
        )
        .unwrap();
    });
    let response = DirectoryClient::new(&origin)
        .unwrap()
        .call("POST", "/records", b"{}");
    server.join().unwrap();
    let headers = headers.lock().unwrap();
    let get = |key| {
        headers
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    };
    let agent = format!("Mycelium/{}", env!("CARGO_PKG_VERSION"));
    assert_eq!(get("user-agent"), Some(agent.as_str()));
    assert_eq!(get("x-mycelium-swarms"), Some("1"));
    assert_eq!(get("cookie"), None);
    assert_eq!(get("authorization"), None);
    assert_eq!(response.unwrap_err().code, "swarm_directory_http_302");
}

#[test]
fn directory_origins_and_request_paths_are_closed_before_connecting() {
    for origin in [
        "http://example.org",
        "https://user:secret@example.org",
        "https://example.org/path",
        "http://192.168.1.4:8000",
        "https://example.org#fragment",
    ] {
        assert!(DirectoryClient::new(origin).is_err(), "{origin}");
    }
    let client = DirectoryClient::new("http://127.0.0.1:12345").unwrap();
    for path in [
        "/records\r\nInjected: yes",
        "/records?token=secret",
        "/records#frag",
        "https://example.org",
    ] {
        assert_eq!(
            client.call("POST", path, b"{}").unwrap_err().code,
            "swarm_directory_route_invalid"
        );
    }
}
