// SPDX-License-Identifier: AGPL-3.0-only
//! Seed key rotation against the EXECUTED oracle (FX-L5-seed-rotation-r11: `round11-rotation-probe.py` ->
//! `fixtures/oracle_rotation.json`, v0.1.4 f4e366e9, Python 3.14.8). The probe ran the real `SeedHTTPServer` +
//! `SeedCoordinator` + `SqliteSeedState` on a scratch sqlite database (scripted clock, counting id source), drove
//! `begin_authority_rotation` / `complete_authority_rotation` / `authority_state`, rotation acknowledgements over
//! `/seed/message`, restarts under the old and the new seed key, and `GET /seed/rotation`. Every step is replayed
//! here over a real `Coordinator` and must produce the same codes, authority state, response bytes, id draws and
//! member projections.
use protocol::{canonical, identity::Identity};
use seed::{Coordinator, Options};
use serde_json::Value;
use std::{
    fs,
    io::Cursor,
    os::unix::fs::PermissionsExt,
    path::PathBuf,
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
};

fn fixture() -> Value {
    let doc: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_rotation.json")).unwrap();
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
    /// The seed key the coordinator runs under (`config.signers`), and the last key a restart accepted.
    key: String,
    last_good: String,
    rotation_envelope: Option<Value>,
}

impl World {
    fn new(name: &str, config: &Value, rotation_envelope: Option<Value>) -> Self {
        // The store refuses a symlink anywhere on the path (as `SqliteInviteRegistry` does) and macOS keeps
        // `/var` -> `/private/var`, so resolve the temporary directory first.
        let base = fs::canonicalize(std::env::temp_dir()).unwrap();
        let dir = base.join(format!("l5r11-rot-{}-{name}", std::process::id()));
        fs::remove_dir_all(&dir).unwrap_or(());
        fs::create_dir_all(&dir).unwrap();
        fs::set_permissions(&dir, fs::Permissions::from_mode(0o700)).unwrap();
        let mut world = Self {
            dir,
            config: config.clone(),
            clock: Arc::new(AtomicU64::new(config["now"].as_f64().unwrap().to_bits())),
            counter: Arc::new(Mutex::new((0, Vec::new()))),
            coordinator: None,
            key: "OLD".into(),
            last_good: "OLD".into(),
            rotation_envelope,
        };
        world
            .boot("seed-boot-1", "OLD")
            .unwrap_or_else(|e| panic!("open: {}", e.code));
        world
    }

    fn boot(&mut self, incarnation: &str, key: &str) -> Result<(), seed::SeedError> {
        self.coordinator = None;
        let c = &self.config;
        let byte = c["signers"][key]["byte"].as_u64().unwrap() as u8;
        let signer =
            Identity::from_secret(c["seed_endpoint"].as_str().unwrap(), &[byte; 32]).unwrap();
        assert_eq!(
            signer.verification_key_digest(),
            c["signers"][key]["digest"].as_str().unwrap(),
            "the oracle's key digest for {key}"
        );
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
        )?;
        coordinator.set_rotation_envelope(self.rotation_envelope.clone());
        self.coordinator = Some(Arc::new(coordinator));
        self.key = key.to_owned();
        Ok(())
    }

    fn coordinator(&self) -> &Coordinator {
        self.coordinator.as_ref().unwrap()
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

fn replay(scenario: &Value, config: &Value, rotation_envelope: Option<Value>) -> usize {
    let name = scenario["name"].as_str().unwrap();
    let mut world = World::new(name, config, rotation_envelope);
    let mut checked = 0;
    for step in scenario["steps"].as_array().unwrap() {
        let label = step["label"].as_str().unwrap();
        let at = format!("{name}/{label}");
        match step["op"].as_str().unwrap() {
            "http" => {
                world.set_clock(step["now"].as_f64().unwrap());
                let request = unhex(step["request_hex"].as_str().unwrap());
                let mut out = Vec::new();
                seed::http::serve(world.coordinator(), Cursor::new(request), &mut out).unwrap();
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
                let incarnation = step["incarnation"].as_str().unwrap();
                let wanted = step["signer_key"].as_str().map(str::to_owned);
                let key = wanted.clone().unwrap_or_else(|| world.key.clone());
                let result = world.boot(incarnation, &key);
                let want = &step["result"];
                match (&want["ok"], result) {
                    (Value::Bool(true), Ok(())) => {
                        if wanted.is_some() {
                            world.last_good = key;
                        }
                    }
                    (Value::Bool(false), Err(e)) => {
                        assert_eq!(e.code, want["code"].as_str().unwrap(), "{at}");
                        // The probe keeps its world usable with the key that works.
                        let good = world.last_good.clone();
                        world.boot(incarnation, &good).unwrap();
                    }
                    (want, got) => panic!("{at}: restart want {want} got {got:?}"),
                }
            }
            "rotation_state" | "rotation_begin" | "rotation_complete" => {
                world.set_clock(step["now"].as_f64().unwrap());
                let op = step["op"].as_str().unwrap();
                let args = &step["args"];
                let got = match op {
                    "rotation_begin" => world
                        .coordinator()
                        .begin_authority_rotation(&args["transition"]),
                    "rotation_complete" => world.coordinator().complete_authority_rotation(
                        args["authority_generation"].as_u64().unwrap(),
                        args["new_seed_key_digest"].as_str().unwrap(),
                    ),
                    _ => Ok(()),
                };
                match (&step["result"]["ok"], got) {
                    (Value::Bool(true), Ok(())) => {}
                    (Value::Bool(false), Err(e)) => {
                        assert_eq!(e.code, step["result"]["code"].as_str().unwrap(), "{at}");
                    }
                    (want, got) => panic!("{at}: want {want} got {got:?}"),
                }
                if op == "rotation_complete" && step["result"]["ok"] == true {
                    let digest = args["new_seed_key_digest"].as_str().unwrap();
                    let signers = config["signers"].as_object().unwrap();
                    world.last_good = signers
                        .iter()
                        .find(|(_, v)| v["digest"] == digest)
                        .map(|(k, _)| k.clone())
                        .unwrap();
                }
                let state = world
                    .coordinator()
                    .authority_state()
                    .unwrap_or_else(|e| panic!("{at}: authority_state {}", e.code));
                assert_eq!(
                    String::from_utf8(bytes(&state)).unwrap(),
                    String::from_utf8(bytes(&step["authority_state"])).unwrap(),
                    "{at}: authority state"
                );
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
    let doc = fixture();
    let found = doc["scenarios"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["name"] == name)
        .unwrap();
    let envelope = name
        .ends_with("_present")
        .then(|| doc["config"]["rotation_envelope"].clone());
    replay(found, &doc["config"], envelope)
}

#[test]
fn rotation_state_machine_equals_the_oracle() {
    assert_eq!(scenario("rotation_state"), 29);
}

#[test]
fn rotation_route_without_an_envelope_equals_the_oracle() {
    assert_eq!(scenario("rotation_http_absent"), 5);
}

#[test]
fn rotation_route_with_an_envelope_equals_the_oracle() {
    assert_eq!(scenario("rotation_http_present"), 5);
}
