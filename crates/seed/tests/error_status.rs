// SPDX-License-Identifier: AGPL-3.0-only
//! The seed's HTTP status map for application-route refusals, over the real plain server.
//!
//! v0.1.4 `mycelium_seed/http.py:311-328` (`_error_status`) for every seed code, except the managed-control
//! capacity refusal: the combined seed handler answers it `503` (`mycelium_a13_macos/tls.py:438-441`; owner
//! ruling 16). Each code here is the error of an application route, so only the status map decides the answer.
use seed::{Coordinator, Options, SeedError, http::RouteExtension, server::Server};
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    net::TcpStream,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    sync::Arc,
    time::Duration,
};

const PREFIX: &str = "/managed-control/status-probe/";

/// Answers `POST /managed-control/status-probe/<code>` with the refusal `<code>`.
struct Refuse;
impl RouteExtension for Refuse {
    fn post(&self, _: &Coordinator, path: &str, _: &Value) -> Option<Result<Value, SeedError>> {
        path.strip_prefix(PREFIX)
            .map(|code| Err(SeedError::new(code)))
    }
}

struct Dir(PathBuf);
impl Drop for Dir {
    fn drop(&mut self) {
        if let Err(e) = fs::remove_dir_all(&self.0) {
            eprintln!("status fixture cleanup: {e}");
        }
    }
}

fn status_line(server: &Server, code: &str) -> (u16, String, Value) {
    let body = b"{}";
    let mut stream = TcpStream::connect(server.address()).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_millis(
            budgets::BudgetId::PyMyceliumSeedHttpSeedhttpclientInitTimeoutL831
                .span(budgets::Entries::ZERO)
                .unwrap()
                .millis(),
        )))
        .unwrap();
    let request = format!(
        "POST {PREFIX}{code} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );
    stream.write_all(request.as_bytes()).unwrap();
    stream.write_all(body).unwrap();
    let mut bytes = Vec::new();
    stream.read_to_end(&mut bytes).unwrap();
    let end = bytes.windows(4).position(|w| w == b"\r\n\r\n").unwrap();
    let head = std::str::from_utf8(&bytes[..end]).unwrap();
    let first = head.lines().next().unwrap().to_owned();
    let status = first.split_whitespace().nth(1).unwrap().parse().unwrap();
    (
        status,
        first,
        serde_json::from_slice(&bytes[end + 4..]).unwrap(),
    )
}

#[test]
fn only_the_managed_control_capacity_refusal_answers_503() {
    let dir = Dir(fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("sl-seed-status-{}", std::process::id())));
    fs::create_dir(&dir.0).unwrap();
    fs::set_permissions(&dir.0, fs::Permissions::from_mode(0o700)).unwrap();
    let mut key = [0; 32];
    fs::File::open("/dev/urandom")
        .unwrap()
        .read_exact(&mut key)
        .unwrap();
    let seed = Arc::new(
        Coordinator::open(
            &dir.0.join("seed.sqlite"),
            protocol::identity::Identity::from_secret("seed-endpoint", &key).unwrap(),
            Options::new("swarm-status", "seed-node", "seed-boot").unwrap(),
            Box::new(|| 1000.0),
            Box::new(|| "seed-message".into()),
        )
        .unwrap(),
    );
    let server = Server::bind_plain_with_routes(seed, None, Arc::new(Refuse)).unwrap();
    // (code, v0.1.4 status): one row per branch of `_error_status`, the managed codes the
    // member-facing routes refuse with, and the capacity refusal of ruling 16.
    let table: &[(&str, u16)] = &[
        ("managed_control_capacity_exhausted", 503),
        ("membership_key_pin_invalid", 401),
        ("membership_signature_invalid", 401),
        ("membership_contract_upgrade_required", 426),
        ("membership_message_expired", 400),
        ("seed_node_key_conflict", 409),
        ("seed_join_mismatch", 400),
        ("seed_http_frame_too_large", 413),
        ("invite_replayed", 409),
        ("seed_assignment_exists", 409),
        ("seed_message_replayed", 409),
        ("seed_member_unknown", 404),
        ("seed_assignment_unknown", 404),
        ("managed_control_signature_invalid", 401),
        ("managed_control_key_pin_mismatch", 401),
        ("managed_control_member_stale", 400),
        ("managed_control_route_unknown", 400),
        ("managed_control_json_invalid", 400),
        ("managed_control_response_invalid", 400),
        ("managed_owner_unavailable", 400),
        ("managed_control_replayed", 400),
        ("managed_control_expired", 400),
        ("seed_member_lease_expired", 400),
        ("seed_http_request_invalid", 400),
    ];
    for (code, expected) in table {
        let (status, line, body) = status_line(&server, code);
        assert_eq!(
            (status, &body),
            (
                *expected,
                &json!({"protocol":"mycelium.seed.http_error.v1","error":{"code":code}})
            ),
            "{code}: {line}"
        );
    }
    let (_, line, _) = status_line(&server, "managed_control_capacity_exhausted");
    assert_eq!(line, "HTTP/1.1 503 Service Unavailable");
    server.close();
}
