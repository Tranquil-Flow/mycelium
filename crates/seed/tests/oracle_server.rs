// SPDX-License-Identifier: AGPL-3.0-only
//! The seed coordinator SERVER side against the EXECUTED oracle (FX-L5-seed-server-r10:
//! `round10-seed-server-probe.py` -> `fixtures/oracle_seed_server.json`, v0.1.4 f4e366e9, Python 3.14.8).
//! The probe ran the real `SeedHTTPServer` + `SeedCoordinator` on a scratch sqlite database with a scripted
//! clock and counting id source, sending RAW request bytes and recording the exact response bytes. Every step
//! is replayed here through `seed::http::serve` over a real `Coordinator` (durable sqlite in a scratch dir) and
//! must produce the same status line, headers (minus Date), body bytes, id draws and member projections.
use protocol::{canonical, identity::Identity};
use rustls::{
    ClientConfig, ClientConnection, RootCertStore, StreamOwned, crypto::ring as ring_provider,
};
use rustls_pki_types::{CertificateDer, ServerName, pem::PemObject};
use seed::{Coordinator, Options, server::Server, tls};
use serde_json::Value;
use std::{
    fs,
    io::{Cursor, Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpStream},
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

fn fixture() -> Value {
    let doc: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_seed_server.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    doc
}

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len() / 2)
        .map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).unwrap())
        .collect()
}

fn bytes(v: &Value) -> Vec<u8> {
    canonical::to_bytes(v).unwrap()
}

struct World {
    dir: PathBuf,
    config: Value,
    clock: Arc<AtomicU64>,
    counter: Arc<Mutex<(u64, Vec<String>)>>,
    coordinator: Option<Arc<Coordinator>>,
    /// Real TLS: the installation-local material, the running server and a pinned client configuration.
    tls: Option<TlsLeg>,
}

struct TlsLeg {
    material: tls::Material,
    client: Arc<ClientConfig>,
    server: Option<Server>,
}

impl World {
    fn new(name: &str, config: &Value, over_tls: bool) -> Self {
        let mode = if over_tls { "tls" } else { "direct" };
        // The store refuses a symlink anywhere on the path (as `SqliteInviteRegistry` does) and macOS keeps
        // `/var` -> `/private/var`, so resolve the temporary directory first.
        let base = fs::canonicalize(std::env::temp_dir()).unwrap();
        let dir = base.join(format!("l5r10-{}-{name}-{mode}", std::process::id()));
        fs::remove_dir_all(&dir).unwrap_or(());
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        let mut world = Self {
            dir,
            config: config.clone(),
            clock: Arc::new(AtomicU64::new(config["now"].as_f64().unwrap().to_bits())),
            counter: Arc::new(Mutex::new((0, Vec::new()))),
            coordinator: None,
            tls: None,
        };
        if over_tls {
            let material = tls::ensure_local_tls_material(&world.dir.join("tls")).unwrap();
            let mut roots = RootCertStore::empty();
            for cert in CertificateDer::pem_file_iter(&material.ca_certificate).unwrap() {
                roots.add(cert.unwrap()).unwrap();
            }
            let client =
                ClientConfig::builder_with_provider(Arc::new(ring_provider::default_provider()))
                    .with_safe_default_protocol_versions()
                    .unwrap()
                    .with_root_certificates(roots)
                    .with_no_client_auth();
            world.tls = Some(TlsLeg {
                material,
                client: Arc::new(client),
                server: None,
            });
        }
        world.boot("seed-boot-1");
        world
    }

    fn boot(&mut self, incarnation: &str) {
        if let Some(leg) = self.tls.as_mut()
            && let Some(server) = leg.server.take()
        {
            server.close();
        }
        self.coordinator = None;
        let c = &self.config;
        let signer = Identity::from_secret(
            c["seed_endpoint"].as_str().unwrap(),
            &[c["seed_byte"].as_u64().unwrap() as u8; 32],
        )
        .unwrap();
        let options = Options {
            swarm_id: c["swarm_id"].as_str().unwrap().into(),
            seed_node_id: c["seed_node_id"].as_str().unwrap().into(),
            incarnation: incarnation.into(),
            seed_url: Some(c["seed_url"].as_str().unwrap().into()),
            lease_seconds: c["lease_seconds"].as_f64().unwrap(),
            message_ttl_seconds: c["message_ttl_seconds"].as_f64().unwrap(),
            keepalive_interval_seconds: c["keepalive_interval_seconds"].as_f64().unwrap(),
            evidence_freshness_seconds: c["evidence_freshness_seconds"].as_f64().unwrap(),
        };
        let clock = self.clock.clone();
        let counter = self.counter.clone();
        let coordinator = Coordinator::open(
            &self.dir.join("seed.sqlite"),
            signer,
            options,
            Box::new(move || f64::from_bits(clock.load(Ordering::SeqCst))),
            Box::new(move || {
                let mut state = counter.lock().unwrap();
                let id = format!("seed-msg-{}", state.0);
                state.0 += 1;
                state.1.push(id.clone());
                id
            }),
        )
        .unwrap_or_else(|e| panic!("open: {}", e.code));
        let coordinator = Arc::new(coordinator);
        if let Some(leg) = self.tls.as_mut() {
            leg.server = Some(
                Server::bind_tls(
                    coordinator.clone(),
                    &leg.material,
                    Some(self.config["seed_url"].as_str().unwrap()),
                )
                .unwrap_or_else(|e| panic!("bind: {}", e.code)),
            );
        }
        self.coordinator = Some(coordinator);
    }

    fn coordinator(&self) -> &Coordinator {
        self.coordinator.as_ref().unwrap()
    }

    /// Send raw request bytes to the live TLS server (certificate verified against the pinned CA for the
    /// IP `127.0.0.1`) and read the connection's whole output.
    fn over_tls(&self, request: &[u8], half_close: bool) -> Vec<u8> {
        let leg = self.tls.as_ref().unwrap();
        let address: SocketAddr = leg.server.as_ref().unwrap().address();
        let connection = ClientConnection::new(
            leg.client.clone(),
            ServerName::IpAddress(Ipv4Addr::LOCALHOST.into()),
        )
        .unwrap();
        let tcp = TcpStream::connect(address).unwrap();
        // The client timeout of `SeedHTTPClient` (`http.py:831`).
        let timeout = budgets::BudgetId::PyMyceliumSeedHttpSeedhttpclientInitTimeoutL831
            .span(budgets::Entries::new(0))
            .unwrap();
        tcp.set_read_timeout(Some(Duration::from_millis(timeout.millis())))
            .unwrap();
        let mut stream = StreamOwned::new(connection, tcp);
        stream.write_all(request).unwrap();
        stream.flush().unwrap();
        if half_close {
            stream.conn.send_close_notify();
            stream.flush().unwrap();
        }
        let mut out = Vec::new();
        // The server closes with close_notify, but a peer that just drops the TCP stream reads as EOF too.
        if let Err(e) = stream.read_to_end(&mut out) {
            assert_eq!(e.kind(), std::io::ErrorKind::UnexpectedEof, "tls read: {e}");
        }
        out
    }

    fn set_clock(&self, now: f64) {
        self.clock.store(now.to_bits(), Ordering::SeqCst);
        self.counter.lock().unwrap().1.clear();
    }

    fn drawn(&self) -> Vec<String> {
        self.counter.lock().unwrap().1.clone()
    }
}

fn expected_ids(step: &Value) -> Vec<String> {
    step["ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_owned())
        .collect()
}

fn check_members(world: &World, step: &Value, at: &str) {
    for (node, expected) in step["members"].as_object().unwrap() {
        let got = match world.coordinator().member(node) {
            Ok(v) => v,
            Err(e) => serde_json::json!({ "error": e.code }),
        };
        assert_eq!(
            String::from_utf8(bytes(&got)).unwrap(),
            String::from_utf8(bytes(expected)).unwrap(),
            "{at}: member {node}"
        );
    }
}

/// Split a connection's output into responses by `Content-Length` framing (every response has one);
/// `Date` headers are dropped.
fn split_responses(mut wire: &[u8]) -> Vec<(String, Vec<String>, Vec<u8>)> {
    let mut out = Vec::new();
    while !wire.is_empty() {
        let at = wire
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .expect("response head");
        let head = String::from_utf8_lossy(&wire[..at]).into_owned();
        let mut lines = head.split("\r\n").map(str::to_owned);
        let status = lines.next().unwrap();
        let headers: Vec<String> = lines
            .filter(|l| !l.to_ascii_lowercase().starts_with("date:"))
            .collect();
        let length = headers
            .iter()
            .find_map(|h| h.strip_prefix("Content-Length: "))
            .map_or(wire.len() - at - 4, |n| n.parse().unwrap());
        out.push((status, headers, wire[at + 4..at + 4 + length].to_vec()));
        wire = &wire[at + 4 + length..];
    }
    out
}

fn replay(scenario: &Value, config: &Value, over_tls: bool) -> usize {
    let name = scenario["name"].as_str().unwrap();
    let mut world = World::new(name, config, over_tls);
    let mut checked = 0;
    for step in scenario["steps"].as_array().unwrap() {
        let label = step["label"].as_str().unwrap();
        let at = format!("{name}/{label}");
        match step["op"].as_str().unwrap() {
            "http" => {
                world.set_clock(step["now"].as_f64().unwrap());
                let request = unhex(step["request_hex"].as_str().unwrap());
                let out = if world.tls.is_some() {
                    world.over_tls(&request, step["half_close"] == true)
                } else {
                    let mut out = Vec::new();
                    seed::http::serve(world.coordinator(), Cursor::new(request), &mut out).unwrap();
                    out
                };
                let got = split_responses(&out);
                let want = step["responses"].as_array().unwrap();
                assert_eq!(got.len(), want.len(), "{at}: response count");
                for (i, ((status, headers, body), want)) in got.iter().zip(want).enumerate() {
                    assert_eq!(
                        status,
                        want["status_line"].as_str().unwrap(),
                        "{at}#{i}: status"
                    );
                    let want_headers: Vec<String> = want["headers"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|h| h.as_str().unwrap().to_owned())
                        .collect();
                    assert_eq!(headers, &want_headers, "{at}#{i}: headers");
                    assert_eq!(
                        body,
                        &unhex(want["body_hex"].as_str().unwrap()),
                        "{at}#{i}: body {}",
                        String::from_utf8_lossy(body)
                    );
                }
            }
            "mint" => {
                world.set_clock(step["now"].as_f64().unwrap());
                let got = world
                    .coordinator()
                    .mint_invite(
                        step["nonce"].as_str().unwrap(),
                        step["ttl_seconds"].as_u64().unwrap(),
                    )
                    .unwrap_or_else(|e| panic!("{at}: {}", e.code));
                assert_eq!(bytes(&got), bytes(&step["bundle"]), "{at}: bundle");
            }
            "mint_foreign" => {
                let over = &step["overrides"];
                let signer = Identity::from_secret(
                    config["seed_endpoint"].as_str().unwrap(),
                    &[config["seed_byte"].as_u64().unwrap() as u8; 32],
                )
                .unwrap();
                let got = protocol::invite::mint_bundle(
                    &signer,
                    over["swarm_id"]
                        .as_str()
                        .unwrap_or(config["swarm_id"].as_str().unwrap()),
                    over["seed_url"]
                        .as_str()
                        .unwrap_or(config["seed_url"].as_str().unwrap()),
                    &serde_json::json!(3000),
                    step["nonce"].as_str().unwrap(),
                    step["now"].as_f64().unwrap(),
                )
                .unwrap_or_else(|e| panic!("{at}: {}", e.0));
                assert_eq!(bytes(&got), bytes(&step["bundle"]), "{at}: foreign bundle");
                checked += 1;
                continue;
            }
            "advance" => {
                world.set_clock(step["now"].as_f64().unwrap());
                let got = world.coordinator().advance_member_generation(
                    step["node"].as_str().unwrap(),
                    step["expected_generation"].as_u64().unwrap(),
                    step["lifecycle_state"].as_str().unwrap(),
                );
                let want = &step["result"];
                if want["ok"] == true {
                    let got = got.unwrap_or_else(|e| panic!("{at}: {}", e.code));
                    assert_eq!(bytes(&got), bytes(&want["projection"]), "{at}: projection");
                } else if want.get("python_exception").is_some() {
                    assert!(got.is_err(), "{at}: a Python accident must be refused");
                } else {
                    assert_eq!(
                        got.unwrap_err().code,
                        want["code"].as_str().unwrap(),
                        "{at}"
                    );
                }
            }
            "consume" => {
                world.set_clock(step["now"].as_f64().unwrap());
                let got = world.coordinator().consume_invite(
                    step["nonce"].as_str().unwrap(),
                    step["now"].as_f64().unwrap(),
                    step["expires_at"].as_f64().unwrap(),
                );
                match (&step["result"]["ok"], got) {
                    (Value::Bool(true), Ok(())) => {}
                    (Value::Bool(false), Err(e)) => {
                        assert_eq!(e.code, step["result"]["code"].as_str().unwrap(), "{at}");
                    }
                    (want, got) => panic!("{at}: want {want} got {got:?}"),
                }
            }
            "restart" => {
                world.set_clock(step["now"].as_f64().unwrap());
                world.boot(step["incarnation"].as_str().unwrap());
            }
            other => panic!("unknown op {other}"),
        }
        assert_eq!(world.drawn(), expected_ids(step), "{at}: ids drawn");
        check_members(&world, step, &at);
        checked += 1;
    }
    fs::remove_dir_all(&world.dir).unwrap_or(());
    checked
}

fn scenario(name: &str) -> usize {
    scenario_on(name, false)
}

fn scenario_on(name: &str, over_tls: bool) -> usize {
    let doc = fixture();
    let found = doc["scenarios"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["name"] == name)
        .unwrap();
    replay(found, &doc["config"], over_tls)
}

#[test]
fn http_framing_and_routing_equal_the_oracle() {
    assert_eq!(scenario("framing"), 46);
}

#[test]
fn pairing_join_flow_equals_the_oracle() {
    assert_eq!(scenario("join_flow"), 40);
}

#[test]
fn lease_renewal_and_member_messages_equal_the_oracle() {
    assert_eq!(scenario("member_flow"), 31);
}

#[test]
fn revocation_and_restart_equal_the_oracle() {
    assert_eq!(scenario("revocation_restart"), 30);
}

/// The whole join + renewal + restart stack through a real TLS server: installation-local material, the
/// oracle's recorded request bytes, rustls verifying the certificate chain against the pinned CA.
#[test]
fn member_flow_over_real_tls_equals_the_oracle() {
    assert_eq!(scenario_on("member_flow", true), 31);
}

#[test]
fn revocation_and_restart_over_real_tls_equal_the_oracle() {
    assert_eq!(scenario_on("revocation_restart", true), 30);
}
