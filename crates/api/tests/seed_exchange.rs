// SPDX-License-Identifier: AGPL-3.0-only
//! Real seed HTTP/TLS -> API SignedRoutes -> exchange; no native sessions or boot/readiness claims.
use api::{
    run_exchange::{Announce, MEMBER_REPORT, RunExchange},
    run_exchange_routes::{DISCOVERY, PATH_READ, PATH_REPORT, READ, REPORT, RESPONSE, SeedRoutes},
};
use budgets::{BudgetId, Entries, Span};
use protocol::{TimeLimits, canonical, identity::Identity, managed, membership};
use seed::{Coordinator, Options, server::Server, tls};
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    net::TcpStream,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};
const NOW: f64 = 1000.0;
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
    exchange: Arc<RunExchange>,
}
impl World {
    fn new(name: &str) -> Self {
        let dir = fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!("l10-r22-{name}-{}", std::process::id()));
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
            exchange: Arc::new(RunExchange::new(Box::new(|| NOW))),
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
    fn envelope(&self, path: &str, issued: f64, state: &str) -> Value {
        let member = self.seed.member("node-a").unwrap();
        let mut s = json!({"protocol":if path==PATH_READ {READ} else {REPORT},"request_id":"request-one","swarm_id":"swarm-fixture","sequence":1,"nonce":"nonce-one","issued_at":NOW,"expires_at":NOW+seconds(BudgetId::ControlTtl),"target_node_id":"node-a","target_endpoint_id":self.member.endpoint_id(),"target_key_digest":self.member.verification_key_digest(),"membership_generation":member["generation"],"member_incarnation":member["incarnation"],"run_id":DISCOVERY,"deployment_id":DISCOVERY});
        if path == PATH_REPORT {
            s["report"] = json!({"protocol":MEMBER_REPORT,"run_id":"run-one","deployment_id":"dep-one","state":state,"detail":null,"model":null,"progress":null,"descriptor":null,"issued_at":issued});
        }
        self.sign(s)
    }
    fn sign(&self, statement: Value) -> Value {
        json!({"protocol":managed::SIGNED_ENVELOPE_PROTOCOL,"signature":self.member.sign(&statement).unwrap(),"verification_key":self.member.public_key_record(),"statement":statement})
    }
    fn routes(&self) -> Arc<SeedRoutes> {
        Arc::new(SeedRoutes::new(
            self.exchange.clone(),
            Arc::new(|_, _, _| {}),
            Span::from_millis(60_000),
        ))
    }
    fn verify(&self, body: &Value) -> Value {
        managed::verify(
            body,
            RESPONSE,
            &self.seed.signer().public_key_record(),
            self.seed.signer().endpoint_id(),
            NOW,
            limits(),
        )
        .unwrap()
    }
}
impl Drop for World {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.dir).unwrap();
    }
}
fn response(stream: &mut (impl Read + Write), path: &str, body: &Value) -> (u16, Value) {
    let bytes = canonical::to_bytes(body).unwrap();
    write!(stream,"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",bytes.len()).unwrap();
    stream.write_all(&bytes).unwrap();
    stream.flush().unwrap();
    let mut bytes = Vec::new();
    stream.read_to_end(&mut bytes).unwrap();
    let end = bytes.windows(4).position(|w| w == b"\r\n\r\n").unwrap();
    (
        std::str::from_utf8(&bytes[..end])
            .unwrap()
            .split_whitespace()
            .nth(1)
            .unwrap()
            .parse()
            .unwrap(),
        serde_json::from_slice(&bytes[end + 4..]).unwrap(),
    )
}
fn socket(server: &Server) -> TcpStream {
    let s = TcpStream::connect(server.address()).unwrap();
    s.set_read_timeout(Some(Duration::from_millis(
        BudgetId::WorkerStartup
            .span(Entries::ZERO)
            .unwrap()
            .millis(),
    )))
    .unwrap();
    s
}
fn send(server: &Server, path: &str, body: &Value) -> (u16, Value) {
    response(&mut socket(server), path, body)
}
#[test]
fn mounted_real_http_read_report_ordering_and_authority_denials() {
    let w = World::new("http");
    let observed = Arc::new(Mutex::new(Vec::new()));
    let log = observed.clone();
    let routes = Arc::new(SeedRoutes::new(
        w.exchange.clone(),
        Arc::new(move |n, t, r| log.lock().unwrap().push((n.to_owned(), t, r))),
        Span::from_millis(60_000),
    ));
    let server = Server::bind_plain_with_routes(w.seed.clone(), None, routes).unwrap();
    w.join();
    w.exchange.announce(Announce { phase:"collect".into(),run_id:Some("run-one".into()),deployment_id:Some("dep-one".into()),per_node:json!({"node-a":{"bootstrap_offer_id":"own-offer"},"other":{"bootstrap_offer_id":"private-offer"}}).as_object().unwrap().clone(),..Default::default() }).unwrap();
    let hints = Arc::new(AtomicU64::new(0));
    let calls = hints.clone();
    w.exchange.set_model_source_provider(Some(Box::new(move || {
        calls.fetch_add(1, Ordering::SeqCst);
        Ok(None)
    })));
    let (status, body) = send(&server, PATH_READ, &w.envelope(PATH_READ, NOW, "reported"));
    assert_eq!(status, 200, "{body}");
    let verified = w.verify(&body);
    assert_eq!(verified["announcement"]["bootstrap_offer_id"], "own-offer");
    assert_eq!(verified["expires_at"], NOW + 60.0);
    for (issued, state, want) in [
        (1000.0, "reported", "reported"),
        (1005.0, "preparing", "preparing"),
        (999.0, "idle", "preparing"),
        (1005.0, "stopped", "stopped"),
    ] {
        let (status, body) = send(
            &server,
            PATH_REPORT,
            &w.envelope(PATH_REPORT, issued, state),
        );
        assert_eq!(status, 200, "{body}");
        w.verify(&body);
        assert_eq!(w.exchange.reports()["node-a"]["state"], want);
    }
    assert_eq!(observed.lock().unwrap().len(), 5);
    let before = w.exchange.reports();
    let good = w.envelope(PATH_REPORT, NOW, "idle");
    let mut tamper = good.clone();
    tamper["statement"]["nonce"] = json!("tampered");
    assert_eq!(
        send(&server, PATH_REPORT, &tamper).1["error"]["code"],
        "managed_control_signature_invalid"
    );
    let mut expired = good["statement"].clone();
    expired["issued_at"] = json!(900);
    expired["expires_at"] = json!(960);
    assert_eq!(
        send(&server, PATH_REPORT, &w.sign(expired)).1["error"]["code"],
        "managed_control_message_expired"
    );
    let mut malformed = good.clone();
    malformed["statement"]
        .as_object_mut()
        .unwrap()
        .remove("nonce");
    assert_eq!(
        send(&server, PATH_REPORT, &malformed).1["error"]["code"],
        "managed_control_fields_invalid"
    );
    let expiry = w.seed.member("node-a").unwrap()["lease_expires_at"]
        .as_f64()
        .unwrap();
    w.clock.store(expiry.to_bits(), Ordering::SeqCst);
    assert_eq!(
        send(&server, PATH_REPORT, &good).1["error"]["code"],
        "managed_control_member_stale"
    );
    w.clock.store(NOW.to_bits(), Ordering::SeqCst);
    w.seed
        .advance_member_generation("node-a", 1, "STOPPED")
        .unwrap();
    assert_eq!(
        send(&server, PATH_REPORT, &good).1["error"]["code"],
        "managed_control_member_stale"
    );
    assert_eq!(w.exchange.reports(), before);
    assert_eq!(observed.lock().unwrap().len(), 5);
    assert_eq!(
        hints.load(Ordering::SeqCst),
        5,
        "unauthenticated requests must not prepare external hints"
    );
    assert_eq!(send(&server, "/unknown", &json!({})).0, 404);
    server.close();
}
#[test]
fn slow_answer_preparation_is_outside_authority_and_revalidated_before_report_commit() {
    for expire in [false, true] {
        let w = World::new(if expire {
            "prepare-expiry"
        } else {
            "prepare-revoke"
        });
        let observed = Arc::new(AtomicU64::new(0));
        let seen = observed.clone();
        let routes = Arc::new(SeedRoutes::new(
            w.exchange.clone(),
            Arc::new(move |_, _, _| {
                seen.fetch_add(1, Ordering::SeqCst);
            }),
            Span::from_millis(60_000),
        ));
        let server = Server::bind_plain_with_routes(w.seed.clone(), None, routes).unwrap();
        w.join();
        w.exchange
            .announce(Announce {
                phase: "collect".into(),
                ..Default::default()
            })
            .unwrap();
        let mut statement = w.envelope(PATH_REPORT, NOW, "reported")["statement"].clone();
        statement["expires_at"] = json!(NOW + 1.0);
        let good = w.sign(statement);
        let seed = w.seed.clone();
        let clock = w.clock.clone();
        w.exchange.set_model_source_provider(Some(Box::new(move || {
            // If called under the authority mutex, even this read deadlocks. Then change live authority
            // or clock AFTER first authentication; final guarded verification must refuse before effects.
            assert_eq!(seed.member("node-a").unwrap()["generation"], 1);
            if expire {
                clock.store(
                    (NOW + seconds(BudgetId::ControlClockSkew) + 2.0).to_bits(),
                    Ordering::SeqCst,
                );
            } else {
                seed.advance_member_generation("node-a", 1, "STOPPED")
                    .unwrap();
            }
            Ok(None)
        })));
        let (status, body) = send(&server, PATH_REPORT, &good);
        assert_ne!(status, 200, "{body}");
        assert_eq!(
            body["error"]["code"],
            if expire {
                "managed_control_message_expired"
            } else {
                "managed_control_member_stale"
            }
        );
        assert!(w.exchange.reports().as_object().unwrap().is_empty());
        assert_eq!(observed.load(Ordering::SeqCst), 0);
        server.close();
    }
}

#[test]
fn mounted_api_signed_read_and_report_cross_real_pinned_tls() {
    let w = World::new("tls");
    let material = tls::ensure_local_tls_material(&w.dir.join("tls")).unwrap();
    let server =
        Server::bind_tls_on_with_routes(w.seed.clone(), &material, None, None, w.routes()).unwrap();
    w.join();
    use rustls_pki_types::pem::PemObject;
    let mut roots = rustls::RootCertStore::empty();
    for cert in rustls_pki_types::CertificateDer::pem_file_iter(&material.ca_certificate).unwrap() {
        roots.add(cert.unwrap()).unwrap();
    }
    let config = Arc::new(
        rustls::ClientConfig::builder_with_provider(Arc::new(
            rustls::crypto::ring::default_provider(),
        ))
        .with_safe_default_protocol_versions()
        .unwrap()
        .with_root_certificates(roots)
        .with_no_client_auth(),
    );
    for path in [PATH_READ, PATH_REPORT] {
        let conn = rustls::ClientConnection::new(
            config.clone(),
            rustls_pki_types::ServerName::IpAddress(std::net::Ipv4Addr::LOCALHOST.into()),
        )
        .unwrap();
        let mut stream = rustls::StreamOwned::new(conn, socket(&server));
        let (status, body) = response(&mut stream, path, &w.envelope(path, NOW, "reported"));
        assert_eq!(status, 200, "{body}");
        w.verify(&body);
    }
    assert_eq!(w.exchange.reports()["node-a"]["state"], "reported");
    server.close();
}
#[test]
fn external_observer_runs_after_member_guard_and_can_reenter_seed() {
    let w = World::new("observer");
    let seed = w.seed.clone();
    let routes = Arc::new(SeedRoutes::new(
        w.exchange.clone(),
        Arc::new(move |_, _, _| {
            // Deterministic lock-order witness: this would deadlock under the non-reentrant seed mutex.
            seed.advance_member_generation("node-a", 1, "STOPPED")
                .unwrap();
        }),
        Span::from_millis(60_000),
    ));
    let server = Server::bind_plain_with_routes(w.seed.clone(), None, routes).unwrap();
    w.join();
    let good = w.envelope(PATH_REPORT, NOW, "reported");
    let (status, body) = send(&server, PATH_REPORT, &good);
    assert_eq!(status, 200, "{body}");
    w.verify(&body);
    assert_eq!(w.seed.member("node-a").unwrap()["generation"], 2);
    assert_eq!(w.exchange.reports()["node-a"]["state"], "reported");
    assert_eq!(
        send(&server, PATH_REPORT, &good).1["error"]["code"],
        "managed_control_member_stale"
    );
    server.close();
}

#[test]
fn held_external_callbacks_do_not_retain_seed_authority() {
    for provider in [true, false] {
        let w = World::new(if provider {
            "held-provider"
        } else {
            "held-observer"
        });
        let (entered_tx, entered_rx) = std::sync::mpsc::channel();
        let (release_tx, release_rx) = std::sync::mpsc::channel();
        let release_rx = Arc::new(Mutex::new(release_rx));
        let patience = Duration::from_millis(
            BudgetId::WorkerStartup
                .span(Entries::ZERO)
                .unwrap()
                .millis(),
        );
        let observe: api::run_exchange_routes::ClockObserver = if provider {
            Arc::new(|_, _, _| {})
        } else {
            let tx = entered_tx.clone();
            let rx = release_rx.clone();
            Arc::new(move |_, _, _| {
                tx.send(()).unwrap();
                rx.lock().unwrap().recv_timeout(patience).unwrap();
            })
        };
        let server = Server::bind_plain_with_routes(
            w.seed.clone(),
            None,
            Arc::new(SeedRoutes::new(
                w.exchange.clone(),
                observe,
                Span::from_millis(60_000),
            )),
        )
        .unwrap();
        w.join();
        w.exchange
            .announce(Announce {
                phase: "collect".into(),
                ..Default::default()
            })
            .unwrap();
        if provider {
            w.exchange.set_model_source_provider(Some(Box::new(move || {
                entered_tx.send(()).unwrap();
                release_rx.lock().unwrap().recv_timeout(patience).unwrap();
                Ok(None)
            })));
        }
        let good = w.envelope(PATH_REPORT, NOW, "reported");
        let (early, response, revoked) = std::thread::scope(|scope| {
            let request = scope.spawn(|| send(&server, PATH_REPORT, &good));
            entered_rx.recv_timeout(patience).unwrap();
            let (done_tx, done_rx) = std::sync::mpsc::channel();
            let seed = &w.seed;
            let revoker = scope.spawn(move || {
                let result = seed.advance_member_generation("node-a", 1, "STOPPED");
                done_tx.send(()).unwrap();
                result
            });
            let early = done_rx.recv_timeout(patience).is_ok();
            // Release before any assertion or join, including a deliberately broken guard's timeout.
            let released = release_tx.send(());
            let response = request.join();
            let revoked = revoker.join();
            assert!(released.is_ok());
            (early, response.unwrap(), revoked.unwrap())
        });
        assert!(
            early,
            "authority was retained by held external callback; provider={provider}"
        );
        assert!(revoked.is_ok());
        if provider {
            assert_eq!(response.1["error"]["code"], "managed_control_member_stale");
            assert!(w.exchange.reports().as_object().unwrap().is_empty());
        } else {
            assert_eq!(response.0, 200);
            w.verify(&response.1);
            assert_eq!(w.exchange.reports()["node-a"]["state"], "reported");
        }
        server.close();
    }
}
