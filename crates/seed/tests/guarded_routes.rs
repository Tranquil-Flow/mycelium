// SPDX-License-Identifier: AGPL-3.0-only
//! L5 route-extension contract fixture, NOT api::SignedRoutes composition.
//! Uses real seed membership, SQLite, Ed25519, HTTP/TLS and signed read/report shapes.
//! API owns full report validation/ordering/announcement and the production mount.
use budgets::{BudgetId, Entries};
use protocol::{TimeLimits, canonical, identity::Identity, managed, membership};
use seed::{Coordinator, Options, SeedError, http::RouteExtension, server::Server, tls};
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    net::TcpStream,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    sync::{
        Arc, Barrier, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    thread,
    time::Duration,
};

const READ: &str = "/managed-control/demo-run/read";
const REPORT: &str = "/managed-control/demo-run/report";
const NOW: f64 = 1000.0; // Existing executed seed fixture instant, not a timeout.
fn seconds(id: BudgetId) -> f64 {
    id.span(Entries::ZERO).unwrap().millis() as f64 / 1000.0
}
fn limits() -> TimeLimits {
    TimeLimits {
        max_ttl_seconds: seconds(BudgetId::ControlTtl),
        clock_skew_seconds: seconds(BudgetId::ControlClockSkew),
    }
}
fn fresh(endpoint: &str) -> Identity {
    let mut key = [0; 32];
    fs::File::open("/dev/urandom")
        .unwrap()
        .read_exact(&mut key)
        .unwrap();
    Identity::from_secret(endpoint, &key).unwrap()
}
struct World {
    dir: PathBuf,
    clock: Arc<AtomicU64>,
    seed: Arc<Coordinator>,
    member: Identity,
}
impl World {
    fn new(name: &str) -> Self {
        let dir = fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!("l5-r15-{name}-{}", std::process::id()));
        fs::create_dir(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        let clock = Arc::new(AtomicU64::new(NOW.to_bits()));
        let c = clock.clone();
        let seed = Arc::new(
            Coordinator::open(
                &dir.join("seed.sqlite"),
                fresh("seed-endpoint"),
                Options::new("swarm-fixture", "seed-node", "seed-boot").unwrap(),
                Box::new(move || f64::from_bits(c.load(Ordering::SeqCst))),
                Box::new(|| "seed-message".into()),
            )
            .unwrap(),
        );
        Self {
            dir,
            clock,
            seed,
            member: fresh("endpoint-node-a"),
        }
    }
    fn join(&self) {
        let bundle = self
            .seed
            .mint_invite("invite-fixture", seconds(BudgetId::ControlTtl) as u64)
            .unwrap();
        let fixture: Value = serde_json::from_str(include_str!(
            "../../protocol/fixtures/contracts/membership-signed-message-v1.json"
        ))
        .unwrap();
        let envelope = membership::sign(
            &self.member,
            &fixture["message"],
            seed::coordinator::limits().unwrap(),
        )
        .unwrap();
        self.seed
            .accept_join_value(&bundle["token"], &envelope)
            .unwrap();
    }
    fn envelope(&self, path: &str) -> Value {
        let member = self.seed.member("node-a").unwrap();
        let mut s = json!({"protocol":if path==READ {"mycelium.a13_demo_run_read.v1"} else {"mycelium.a13_demo_run_report.v1"}, "request_id":"request-one", "swarm_id":"swarm-fixture", "sequence":1,"nonce":"nonce-one","issued_at":NOW,"expires_at":NOW+seconds(BudgetId::ControlTtl),"target_node_id":"node-a","target_endpoint_id":self.member.endpoint_id(),"target_key_digest":self.member.verification_key_digest(),"membership_generation":member["generation"],"member_incarnation":member["incarnation"],"run_id":"demo-run-discovery","deployment_id":"demo-run-discovery"});
        if path == REPORT {
            // Valid oracle report shape (L10 r21 oracle_signed_exchange.json report case).
            s["report"] = json!({"protocol":"mycelium.a13_demo_member_report.v1","state":"reported","run_id":"run-one","deployment_id":"deployment-one","detail":null,"model":null,"progress":null,"descriptor":null,"issued_at":NOW});
        }
        self.sign(s)
    }
    fn sign(&self, statement: Value) -> Value {
        json!({"protocol":managed::SIGNED_ENVELOPE_PROTOCOL,"signature":self.member.sign(&statement).unwrap(),"verification_key":self.member.public_key_record(),"statement":statement})
    }
}
impl Drop for World {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.dir).unwrap();
    }
}

#[derive(Default)]
struct Routes {
    reports: Mutex<Vec<Value>>,
    observed: AtomicU64,
    prepare: Option<(Arc<Barrier>, Arc<Barrier>)>,
    observer: Option<(Arc<Barrier>, Arc<Barrier>)>,
}
impl RouteExtension for Routes {
    fn post(
        &self,
        coordinator: &Coordinator,
        path: &str,
        envelope: &Value,
    ) -> Option<Result<Value, SeedError>> {
        let protocol = match path {
            READ => "mycelium.a13_demo_run_read.v1",
            REPORT => "mycelium.a13_demo_run_report.v1",
            _ => return None,
        };
        if let Some((entered, release)) = &self.prepare {
            entered.wait();
            release.wait();
        }
        let node = envelope["statement"]["target_node_id"]
            .as_str()
            .unwrap_or("");
        // Preparation above is deliberately outside authority. Only verification + in-memory commit below.
        let result = coordinator.with_current_member(node, |member, now| {
            for (field, request) in [("node_id","target_node_id"),("endpoint_id","target_endpoint_id"),("verification_key_digest","target_key_digest"),("generation","membership_generation"),("incarnation","member_incarnation")] {
                if member[field] != envelope["statement"][request] { return Err(SeedError::new("managed_control_member_stale")); }
            }
            if now >= member["lease_expires_at"].as_f64().unwrap() || !matches!(member["lifecycle_state"].as_str(),Some("NEW"|"CONFIGURED"|"RUNNING"|"DRAINING"|"STOPPING"|"STOPPED")) || !matches!(member["peer_class"].as_str(),Some("mac_mlx_iroh"|"windows_native_iroh"|"linux_native_iroh")) { return Err(SeedError::new("managed_control_member_stale")); }
            let verified = managed::verify(envelope,protocol,&envelope["verification_key"],member["endpoint_id"].as_str().unwrap(),now,limits())?;
            if envelope["verification_key"]["verification_key_digest"] != member["verification_key_digest"] { return Err(SeedError::new("managed_control_key_pin_mismatch")); }
            if verified["swarm_id"] != coordinator.swarm_id() { return Err(SeedError::new("managed_control_authority_binding_invalid")); }
            if path==REPORT { self.reports.lock().unwrap().push(verified["report"].clone()); }
            // Typed test injection of the oracle's managed_control.py:1006 min(60,300) cap.
            let ttl = budgets::Span::from_millis(60_000);
            let mut response = json!({"protocol":"mycelium.a13_demo_run_state.v1","request_id":verified["request_id"],"swarm_id":coordinator.swarm_id(),"owner_node_id":coordinator.seed_node_id(),"owner_endpoint_id":coordinator.signer().endpoint_id(),"issued_at":now,"expires_at":now+(ttl.millis() as f64/1000.0),"announcement":{"protocol":"mycelium.a13_demo_run_announcement.v1","phase":"idle","run_id":null,"deployment_id":null,"model":null,"model_source":null,"slice_mode":false,"manifest_digest":null,"work_limits":null,"node_slice_plan":null,"member_resources":null,"owner_incarnation":"fixture-owner-boot","issued_at":now}});
            for field in ["target_node_id","target_endpoint_id","target_key_digest","membership_generation","member_incarnation","run_id","deployment_id"] {
                response[field] = verified[field].clone();
            }
            Ok(json!({"protocol":managed::SIGNED_ENVELOPE_PROTOCOL,"signature":coordinator.signer().sign(&response).unwrap(),"verification_key":coordinator.signer().public_key_record(),"statement":response}))
        });
        if result.is_ok() {
            // Real external/slow observer belongs here, never under the member authority guard.
            if let Some((entered, release)) = &self.observer {
                entered.wait();
                release.wait();
            }
            self.observed.fetch_add(1, Ordering::SeqCst);
        }
        Some(result)
    }
}
fn raw(path: &str, body: &Value) -> Vec<u8> {
    let bytes = canonical::to_bytes(body).unwrap();
    let mut out = format!("POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",bytes.len()).into_bytes();
    out.extend(bytes);
    out
}
fn response(stream: &mut (impl Read + Write), request: &[u8]) -> (u16, Value) {
    stream.write_all(request).unwrap();
    stream.flush().unwrap();
    let mut bytes = Vec::new();
    stream.read_to_end(&mut bytes).unwrap();
    let end = bytes.windows(4).position(|w| w == b"\r\n\r\n").unwrap();
    let header = std::str::from_utf8(&bytes[..end]).unwrap();
    (
        header.split_whitespace().nth(1).unwrap().parse().unwrap(),
        serde_json::from_slice(&bytes[end + 4..]).unwrap(),
    )
}
fn send(server: &Server, path: &str, body: &Value) -> (u16, Value) {
    let mut stream = TcpStream::connect(server.address()).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_millis(
            BudgetId::PyMyceliumSeedHttpSeedhttpclientInitTimeoutL831
                .span(Entries::ZERO)
                .unwrap()
                .millis(),
        )))
        .unwrap();
    response(&mut stream, &raw(path, body))
}

/// Proves B-AUTH-01 (transport/auth subset), existing binds remain unmounted.
#[test]
fn default_unmounted_and_opt_in_signed_read_report() {
    let w = World::new("default");
    let server = Server::bind_plain(w.seed.clone(), None).unwrap();
    w.join();
    assert_eq!(send(&server, READ, &w.envelope(READ)).0, 404);
    server.close();
    let w = World::new("mounted");
    let routes = Arc::new(Routes::default());
    let server = Server::bind_plain_with_routes(w.seed.clone(), None, routes.clone()).unwrap();
    w.join();
    for path in [READ, REPORT] {
        let (status, body) = send(&server, path, &w.envelope(path));
        assert_eq!(status, 200, "{body}");
        managed::verify(
            &body,
            "mycelium.a13_demo_run_state.v1",
            &w.seed.signer().public_key_record(),
            w.seed.signer().endpoint_id(),
            NOW,
            limits(),
        )
        .unwrap();
    }
    assert_eq!(routes.reports.lock().unwrap().len(), 1);
    assert_eq!(routes.observed.load(Ordering::SeqCst), 2);
    assert_eq!(send(&server, "/unknown", &json!({})).0, 404);
    // Extension cannot override existing seed routes.
    assert_eq!(
        send(&server, "/seed/join", &json!({})).1["error"]["code"],
        "seed_http_body_invalid"
    );
    server.close();
}

/// Proves B-AUTH-01 stale-generation/lease/signature negatives before callback effect.
#[test]
fn refusals_have_no_report_or_observer_effect() {
    let w = World::new("refusals");
    let routes = Arc::new(Routes::default());
    let server = Server::bind_plain_with_routes(w.seed.clone(), None, routes.clone()).unwrap();
    w.join();
    let good = w.envelope(REPORT);
    let mut tamper = good.clone();
    tamper["statement"]["nonce"] = json!("tampered");
    assert_eq!(
        send(&server, REPORT, &tamper).1["error"]["code"],
        "managed_control_signature_invalid"
    );
    let mut wrong = good["statement"].clone();
    wrong["membership_generation"] = json!(2);
    assert_eq!(
        send(&server, REPORT, &w.sign(wrong)).1["error"]["code"],
        "managed_control_member_stale"
    );
    let expiry = w.seed.member("node-a").unwrap()["lease_expires_at"]
        .as_f64()
        .unwrap();
    w.clock.store(expiry.to_bits(), Ordering::SeqCst);
    assert_eq!(
        send(&server, REPORT, &good).1["error"]["code"],
        "managed_control_member_stale"
    );
    w.clock.store(NOW.to_bits(), Ordering::SeqCst);
    w.seed
        .advance_member_generation("node-a", 1, "STOPPED")
        .unwrap();
    assert_eq!(
        send(&server, REPORT, &good).1["error"]["code"],
        "managed_control_member_stale"
    );
    assert!(routes.reports.lock().unwrap().is_empty());
    assert_eq!(routes.observed.load(Ordering::SeqCst), 0);
    assert_eq!(
        w.seed
            .with_current_member("missing", |_, _| Ok(()))
            .unwrap_err()
            .code,
        "seed_member_unknown"
    );
    server.close();
}

/// Proves B-AUTH-01: revocation during slow preparation is visible at commit, no stale snapshot authority.
#[test]
fn revoke_while_preparing_refuses_when_callback_enters_guard() {
    let w = World::new("prepare");
    let entered = Arc::new(Barrier::new(2));
    let release = Arc::new(Barrier::new(2));
    let routes = Arc::new(Routes {
        prepare: Some((entered.clone(), release.clone())),
        ..Default::default()
    });
    let server = Server::bind_plain_with_routes(w.seed.clone(), None, routes.clone()).unwrap();
    w.join();
    let body = w.envelope(REPORT);
    thread::scope(|scope| {
        let worker = scope.spawn(|| send(&server, REPORT, &body));
        entered.wait();
        w.seed
            .advance_member_generation("node-a", 1, "STOPPED")
            .unwrap();
        release.wait();
        assert_eq!(
            worker.join().unwrap().1["error"]["code"],
            "managed_control_member_stale"
        );
    });
    assert!(routes.reports.lock().unwrap().is_empty());
    server.close();
}

/// Proves B-AUTH-01 guard boundary: external observers do not stall member revocation.
#[test]
fn held_external_observer_does_not_hold_authority() {
    let w = World::new("observer");
    let entered = Arc::new(Barrier::new(2));
    let release = Arc::new(Barrier::new(2));
    let routes = Arc::new(Routes {
        observer: Some((entered.clone(), release.clone())),
        ..Default::default()
    });
    let server = Server::bind_plain_with_routes(w.seed.clone(), None, routes.clone()).unwrap();
    w.join();
    let body = w.envelope(REPORT);
    thread::scope(|scope| {
        let worker = scope.spawn(|| send(&server, REPORT, &body));
        entered.wait();
        assert_eq!(
            w.seed
                .advance_member_generation("node-a", 1, "STOPPED")
                .unwrap()["generation"],
            2
        );
        release.wait();
        assert_eq!(worker.join().unwrap().0, 200);
    });
    assert_eq!(routes.reports.lock().unwrap().len(), 1);
    server.close();
}

/// Proves B-AUTH-01/B-FAIL-16: bounded callback is inside a real durable write reservation.
#[test]
fn held_callback_blocks_durable_writer_then_releases_on_success_and_error() {
    let w = World::new("durable");
    let server = Server::bind_plain(w.seed.clone(), None).unwrap();
    w.join();
    let connection = rusqlite::Connection::open(w.dir.join("seed.sqlite")).unwrap();
    connection.busy_timeout(Duration::ZERO).unwrap();
    for fail in [false, true] {
        let result = w.seed.with_current_member("node-a", |member, now| {
            assert_eq!(member["generation"], 1);
            assert_eq!(now, NOW);
            let e = connection
                .execute_batch("BEGIN IMMEDIATE")
                .expect_err("guard must reserve durable authority");
            assert_eq!(
                e.sqlite_error_code(),
                Some(rusqlite::ErrorCode::DatabaseBusy)
            );
            if fail {
                Err(SeedError::new("fixture_refusal"))
            } else {
                Ok(())
            }
        });
        assert_eq!(result.is_err(), fail);
        connection
            .execute_batch("BEGIN IMMEDIATE; ROLLBACK")
            .unwrap();
    }
    server.close();
}

/// Proves B-AUTH-01/B-SWARM-16: stale process cannot authorize using its old local member snapshot.
#[test]
fn durable_revoke_is_seen_by_already_running_coordinator() {
    let w = World::new("stale");
    let server = Server::bind_plain(w.seed.clone(), None).unwrap();
    w.join();
    // Real persisted update through an independent SQL connection models the operator writer's commit.
    let connection = rusqlite::Connection::open(w.dir.join("seed.sqlite")).unwrap();
    let text: String = connection
        .query_row("SELECT snapshot FROM rs_seed_table WHERE id=1", [], |r| {
            r.get(0)
        })
        .unwrap();
    let mut table: Value = serde_json::from_str(&text).unwrap();
    table["members"][0]["generation"] = json!(2);
    connection
        .execute(
            "UPDATE rs_seed_table SET snapshot=? WHERE id=1",
            [table.to_string()],
        )
        .unwrap();
    assert_eq!(w.seed.member("node-a").unwrap()["generation"], 1);
    assert_eq!(
        w.seed
            .with_current_member("node-a", |_, _| Ok(()))
            .unwrap_err()
            .code,
        "seed_state_member_stale"
    );
    server.close();
}

/// Proves B-AUTH-01/B-SWARM-16: all eight authority fields match the executed oracle.
#[test]
fn durable_authority_fields_match_executed_oracle() {
    let fixture: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_guard_r15.json")).unwrap();
    for (index, row) in fixture["rows"].as_array().unwrap().iter().enumerate() {
        let w = World::new(&format!("oracle-{index}"));
        w.seed.bind_seed_url("https://seed.example.test").unwrap();
        w.join();
        if let Some(field) = row["field"].as_str() {
            let connection = rusqlite::Connection::open(w.dir.join("seed.sqlite")).unwrap();
            let text: String = connection
                .query_row("SELECT snapshot FROM rs_seed_table WHERE id=1", [], |r| {
                    r.get(0)
                })
                .unwrap();
            let mut table: Value = serde_json::from_str(&text).unwrap();
            table["members"][0][field] = row["value"].clone();
            connection
                .execute(
                    "UPDATE rs_seed_table SET snapshot=? WHERE id=1",
                    [table.to_string()],
                )
                .unwrap();
        }
        let result = w.seed.with_current_member("node-a", |member, _| {
            Ok(json!({"generation":member["generation"],"node_id":member["node_id"]}))
        });
        let got = match result {
            Ok(value) => value,
            Err(error) => json!({"error":error.code}),
        };
        assert_eq!(got, row["expected"], "field {}", row["field"]);
    }
}

/// Proves B-AUTH-01/B-FAIL-16: actual independent Coordinator revoke cannot commit during a held guard.
#[test]
fn held_callback_serializes_real_second_coordinator_revoke() {
    let w = World::new("two-coordinators");
    w.seed.bind_seed_url("https://seed.example.test").unwrap();
    w.join();
    let clock = w.clock.clone();
    let writer = Coordinator::open_with_busy_timeout(
        &w.dir.join("seed.sqlite"),
        Identity::from_secret(
            w.seed.signer().endpoint_id(),
            &w.seed.signer().secret_bytes(),
        )
        .unwrap(),
        Options::new("swarm-fixture", "seed-node", "other-seed-boot").unwrap(),
        Box::new(move || f64::from_bits(clock.load(Ordering::SeqCst))),
        Box::new(|| "other-message".into()),
        Duration::ZERO,
    )
    .unwrap();
    let entered = Barrier::new(2);
    let release = Barrier::new(2);
    thread::scope(|scope| {
        let guarded = scope.spawn(|| {
            w.seed.with_current_member("node-a", |_, _| {
                entered.wait();
                release.wait();
                Ok(())
            })
        });
        entered.wait();
        // Zero SQLite busy timeout is a deterministic lock observation, not a sleep/race assertion.
        let refusal = writer.advance_member_generation("node-a", 1, "STOPPED");
        release.wait();
        guarded.join().unwrap().unwrap();
        assert_eq!(refusal.unwrap_err().code, "seed_state_unavailable");
        assert_eq!(
            writer
                .advance_member_generation("node-a", 1, "STOPPED")
                .unwrap()["generation"],
            2
        );
    });
    assert_eq!(
        w.seed
            .with_current_member("node-a", |_, _| Ok(()))
            .unwrap_err()
            .code,
        "seed_state_member_stale"
    );
}

/// Proves B-AUTH-01: the trusted clock is sampled inside the durable reservation, never before waiting.
#[test]
fn clock_observer_sees_authority_reserved_and_invalid_clock_refuses_callback() {
    let w = World::new("clock");
    w.seed.bind_seed_url("https://seed.example.test").unwrap();
    w.join();
    let db = w.dir.join("seed.sqlite");
    let probe_db = db.clone();
    let clock = w.clock.clone();
    let guarded = Coordinator::open(
        &db,
        Identity::from_secret(
            w.seed.signer().endpoint_id(),
            &w.seed.signer().secret_bytes(),
        )
        .unwrap(),
        Options::new("swarm-fixture", "seed-node", "clock-boot").unwrap(),
        Box::new(move || {
            let connection = rusqlite::Connection::open(&probe_db).unwrap();
            connection.busy_timeout(Duration::ZERO).unwrap();
            let attempt = connection.execute_batch("BEGIN IMMEDIATE");
            assert_eq!(
                attempt.unwrap_err().sqlite_error_code(),
                Some(rusqlite::ErrorCode::DatabaseBusy)
            );
            f64::from_bits(clock.load(Ordering::SeqCst))
        }),
        Box::new(|| "clock-message".into()),
    )
    .unwrap();
    let expiry = w.seed.member("node-a").unwrap()["lease_expires_at"]
        .as_f64()
        .unwrap();
    w.clock.store(expiry.to_bits(), Ordering::SeqCst);
    guarded
        .with_current_member("node-a", |member, now| {
            assert_eq!(now, expiry);
            assert_eq!(member["lease_expires_at"], now);
            Ok(())
        })
        .unwrap();
    w.clock.store(f64::NAN.to_bits(), Ordering::SeqCst);
    assert_eq!(
        guarded
            .with_current_member::<()>("node-a", |_, _| panic!("invalid clock entered callback"))
            .unwrap_err()
            .code,
        "seed_clock_invalid"
    );
}

/// Proves B-NET-11/B-AUTH-01: extension traverses existing real pinned loopback TLS, same signer.
#[test]
fn signed_extension_uses_existing_tls_server() {
    let w = World::new("tls");
    let material = tls::ensure_local_tls_material(&w.dir.join("tls")).unwrap();
    let routes = Arc::new(Routes::default());
    let server =
        Server::bind_tls_on_with_routes(w.seed.clone(), &material, None, None, routes).unwrap();
    w.join();
    use rustls_pki_types::pem::PemObject;
    let mut roots = rustls::RootCertStore::empty();
    for cert in rustls_pki_types::CertificateDer::pem_file_iter(&material.ca_certificate).unwrap() {
        roots.add(cert.unwrap()).unwrap();
    }
    let config = rustls::ClientConfig::builder_with_provider(Arc::new(
        rustls::crypto::ring::default_provider(),
    ))
    .with_safe_default_protocol_versions()
    .unwrap()
    .with_root_certificates(roots)
    .with_no_client_auth();
    let connection = rustls::ClientConnection::new(
        Arc::new(config),
        rustls_pki_types::ServerName::IpAddress(std::net::Ipv4Addr::LOCALHOST.into()),
    )
    .unwrap();
    let mut stream =
        rustls::StreamOwned::new(connection, TcpStream::connect(server.address()).unwrap());
    let (status, body) = response(&mut stream, &raw(READ, &w.envelope(READ)));
    assert_eq!(status, 200, "{body}");
    managed::verify(
        &body,
        "mycelium.a13_demo_run_state.v1",
        &w.seed.signer().public_key_record(),
        w.seed.signer().endpoint_id(),
        NOW,
        limits(),
    )
    .unwrap();
    server.close();
}
