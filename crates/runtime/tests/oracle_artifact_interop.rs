// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NET-09 (the artifact ALPN and its durable admission journal against the executed v0.1.4 sidecar, local only).
//!
//! The artifact ALPN (`mycelium.artifact_chunk.v1`) and its durable admission journal against the
//! EXECUTED v0.1.4 sidecar, local only (127.0.0.1, no relay, no address lookup).
//!
//! One scenario of 18 signed chunk requests (granted ranges, replay, budget exhaustion, a grant that
//! began before the ledger's genesis, objects missing/tampered on disk, a wrong transport peer, a
//! malformed and a tampered request, and a sidecar RESTART in the middle: the fence and budgets
//! must survive it) is run against the real oracle binary (`mycelium-iroh-sidecar @ f4e366e9`,
//! release profile, an exact-owned child; needs `L9_ORACLE_SIDECAR`, `#[ignore]`d otherwise),
//! which also RECORDS the outcomes (`L9_WRITE_SERVE_FIXTURE=<path>`), and against the Rust
//! sidecar (`run_sidecar` + `DurableArtifactBackend`, in this process).
//!
//! `fixtures/sidecar-artifact-serve-oracle.json` holds the oracle's recorded outcomes and the
//! journal lines it left (times normalised against the scenario start); the Rust half is checked
//! against it in the default run, and against the live oracle in the ignored test.
#![cfg(all(feature = "iroh-adapter", unix))]
use iroh::{
    Endpoint, EndpointAddr, RelayMode, SecretKey,
    endpoint::{Connection, presets},
};
use protocol::{
    artifact_grant::{self as ag, ChunkRequest, Grant, GrantLimits},
    identity::Identity,
};
use runtime::{
    sidecar_artifact::DurableArtifactBackend,
    sidecar_main::{ArtifactArguments, SidecarConfig, SidecarOutput, artifact_config, run_sidecar},
    sidecar_process::BootstrapSecret,
};
use serde_json::{Value, json};
use std::{
    io::{BufRead, BufReader, Write},
    net::{Ipv4Addr, SocketAddrV4},
    num::NonZeroUsize,
    os::{fd::AsRawFd, unix::fs::PermissionsExt},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{Arc, Mutex, mpsc},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

include!("support/fork_lock.rs");

const SEED_SECRET: [u8; 32] = [0x22; 32];
const SOURCE_SECRET: [u8; 32] = [0x33; 32];
const ENDPOINT_SECRET: [u8; 32] = [0x44; 32];
const BOOTSTRAP: [u8; 32] = [0x4f; 32];
const MEMBER_SECRET: [u8; 32] = [0x11; 32];
const CLIENT_KEY: [u8; 32] = [0x61; 32];
const OTHER_CLIENT_KEY: [u8; 32] = [0x62; 32];

fn wait() -> Duration {
    Duration::from_millis(
        budgets::BudgetId::RouterClose
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis()
            * 2,
    )
}
fn poll() -> Duration {
    Duration::from_millis(
        budgets::BudgetId::RegistryPoll
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis(),
    )
}
fn now_ms() -> u64 {
    u64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis(),
    )
    .unwrap()
}
fn limits() -> GrantLimits {
    runtime::sidecar_artifact::grant_limits().unwrap()
}
fn scratch(label: &str) -> PathBuf {
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let serial = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let base = std::env::var_os("TMPDIR").map_or_else(std::env::temp_dir, PathBuf::from);
    let dir = base.join(format!("l9ai{}-{serial}-{label}", std::process::id()));
    std::fs::create_dir_all(dir.join("objects")).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    dir
}
fn write_private(path: &Path, bytes: &[u8]) {
    std::fs::write(path, bytes).unwrap();
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).unwrap();
}
fn seed() -> Identity {
    Identity::from_secret("seed-endpoint-1", &SEED_SECRET).unwrap()
}
/// The files both sidecars are started with.
fn prepare_files(dir: &Path) {
    write_private(
        &dir.join("seed"),
        serde_json::to_string(&seed().public_key_record())
            .unwrap()
            .as_bytes(),
    );
    write_private(&dir.join("sig"), &SOURCE_SECRET);
    write_private(&dir.join("ep"), &ENDPOINT_SECRET);
}
fn arguments(dir: &Path) -> Vec<String> {
    let path = |name: &str| dir.join(name).to_str().unwrap().to_owned();
    [
        "--artifact-objects",
        &path("objects"),
        "--artifact-seed-key",
        &path("seed"),
        "--artifact-state",
        &path("st"),
        "--artifact-signing-key-file",
        &path("sig"),
        "--endpoint-secret-file",
        &path("ep"),
    ]
    .map(str::to_owned)
    .to_vec()
}

// ---- the two sidecars ------------------------------------------------------------------

trait Running {
    fn address(&self) -> EndpointAddr;
    /// SIGINT / stop, then reap: returns once the process or thread is gone.
    fn stop(self: Box<Self>);
}

struct OracleChild {
    child: Option<Child>,
    readers: Vec<std::thread::JoinHandle<()>>,
    address: EndpointAddr,
}
impl OracleChild {
    fn start(binary: &Path, dir: &Path) -> Box<dyn Running> {
        let (reader, writer) = rustix::pipe::pipe().unwrap();
        let mut writer = std::fs::File::from(writer);
        writer.write_all(&BOOTSTRAP).unwrap();
        drop(writer);
        let number = reader.as_raw_fd();
        let mut command = Command::new(binary);
        command
            .arg("--uds")
            .arg(dir.join("s"))
            .arg("--bootstrap-fd")
            .arg(number.to_string())
            .arg("--local-only")
            .args(arguments(dir))
            .current_dir(dir)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        node::fds::confine_descriptors_except(&mut command, &[number]).unwrap();
        let mut child = {
            let _boundary = fixture_spawn_boundary();
            command.spawn().expect("oracle sidecar spawned")
        };
        drop(reader);
        let stdout = child.stdout.take().unwrap();
        let (lines, ready) = mpsc::channel();
        let out = std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if lines.send(line).is_err() {
                    return;
                }
            }
        });
        let stderr = child.stderr.take().unwrap();
        let err = std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                drop(line);
            }
        });
        let line = ready.recv_timeout(wait()).expect("oracle ready record");
        let record: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(record["event"], "ready", "{record}");
        Box::new(Self {
            child: Some(child),
            readers: vec![out, err],
            address: serde_json::from_value(record["endpoint_addr"].clone()).unwrap(),
        })
    }
}
impl Running for OracleChild {
    fn address(&self) -> EndpointAddr {
        self.address.clone()
    }
    fn stop(mut self: Box<Self>) {
        let mut child = self.child.take().unwrap();
        let pid = rustix::process::Pid::from_raw(i32::try_from(child.id()).unwrap()).unwrap();
        rustix::process::kill_process(pid, rustix::process::Signal::INT).unwrap();
        let started = Instant::now();
        while child.try_wait().unwrap().is_none() {
            if started.elapsed() > wait() {
                child.kill().unwrap();
                child.wait().unwrap();
                break;
            }
            std::thread::sleep(poll());
        }
        assert!(matches!(
            rustix::process::test_kill_process(pid),
            Err(rustix::io::Errno::SRCH)
        ));
        for reader in std::mem::take(&mut self.readers) {
            reader.join().unwrap();
        }
    }
}
impl Drop for OracleChild {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            if child.kill().is_err() {
                eprintln!("oracle child already gone");
            }
            if child.wait().is_err() {
                eprintln!("oracle child not reaped");
            }
        }
    }
}

struct Lines(Mutex<mpsc::Sender<String>>);
impl SidecarOutput for Lines {
    fn stdout_line(&self, line: &str) -> std::io::Result<()> {
        self.0
            .lock()
            .unwrap()
            .send(line.to_owned())
            .map_err(|_| std::io::ErrorKind::BrokenPipe.into())
    }
    fn stderr_line(&self, _line: &str) {}
}
struct RustSide {
    stop: Option<tokio::sync::oneshot::Sender<()>>,
    thread: Option<std::thread::JoinHandle<Result<(), runtime::sidecar_process::SidecarError>>>,
    address: EndpointAddr,
}
impl RustSide {
    fn start(dir: &Path) -> Box<dyn Running> {
        let (lines, ready) = mpsc::channel();
        let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
        let dir = dir.to_path_buf();
        let thread = std::thread::spawn(move || {
            let runtime = tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .enable_all()
                .build()
                .unwrap();
            let path = |name: &str| dir.join(name);
            let artifact = artifact_config(&ArtifactArguments {
                objects: path("objects"),
                seed_key: path("seed"),
                state: path("st"),
                signing_key_file: path("sig"),
            })
            .unwrap();
            let config = SidecarConfig {
                reach: runtime::iroh_host::Reach::Shipped,
                ready_window: None,
                socket: path("s"),
                local_only: true,
                force_relay: false,
                queue_capacity: NonZeroUsize::new(4).unwrap(),
                endpoint_secret: Some(BootstrapSecret::from_bytes(ENDPOINT_SECRET)),
                artifact: Some(artifact),
            };
            // A stand-in capacity (not the shipped `ArtifactMaxFence`): far above the scenario.
            let backend = DurableArtifactBackend {
                fence_capacity: NonZeroUsize::new(1024),
            };
            run_sidecar(
                &runtime,
                config,
                BootstrapSecret::from_bytes(BOOTSTRAP),
                &backend,
                Arc::new(Lines(Mutex::new(lines))),
                async {
                    if stopped.await.is_err() {
                        eprintln!("the stop sender was dropped: stopping");
                    }
                },
            )
        });
        let line = ready.recv_timeout(wait()).expect("rust ready record");
        let record: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(record["event"], "ready", "{record}");
        Box::new(Self {
            stop: Some(stop),
            thread: Some(thread),
            address: serde_json::from_value(record["endpoint_addr"].clone()).unwrap(),
        })
    }
}
impl Running for RustSide {
    fn address(&self) -> EndpointAddr {
        self.address.clone()
    }
    fn stop(mut self: Box<Self>) {
        self.stop.take().unwrap().send(()).unwrap();
        self.thread.take().unwrap().join().unwrap().unwrap();
    }
}

// ---- the client and the scenario ---------------------------------------------------------

async fn client_endpoint(key: &[u8; 32]) -> Endpoint {
    Endpoint::builder(presets::Minimal)
        .relay_mode(RelayMode::Disabled)
        .clear_address_lookup()
        .clear_ip_transports()
        .bind_addr(SocketAddrV4::new(Ipv4Addr::LOCALHOST, 0))
        .unwrap()
        .secret_key(SecretKey::from_bytes(key))
        .bind()
        .await
        .unwrap()
}
struct Reply {
    header: Value,
    data: Vec<u8>,
}
async fn exchange(connection: &Connection, request: &[u8]) -> Reply {
    let (mut send, mut recv) = connection.open_bi().await.unwrap();
    send.write_all(request).await.unwrap();
    send.finish().unwrap();
    let raw = tokio::time::timeout(wait(), recv.read_to_end(1 << 24))
        .await
        .expect("reply within the bound")
        .unwrap();
    let length = u32::from_be_bytes(raw[..4].try_into().unwrap()) as usize;
    Reply {
        header: serde_json::from_slice(&raw[4..4 + length]).unwrap(),
        data: raw[4 + length..].to_vec(),
    }
}

struct World {
    source_id: String,
    source_digest: String,
    member: Identity,
    member_id: String,
    t0: u64,
}
impl World {
    fn grant(&self, id: &str, not_before: u64, total: u64, objects: &[&str]) -> (Value, Grant) {
        let mut objects: Vec<&str> = objects.to_vec();
        objects.sort_unstable();
        let message = json!({
            "protocol": ag::GRANT_PROTOCOL, "grant_id": id, "swarm_id": "swarm-1",
            "seed_node_id": "seed-node-1", "recipient_node_id": "node-phone-1",
            "recipient_endpoint_id": self.member_id,
            "recipient_key_digest": self.member.verification_key_digest(),
            "assignment_id": "route-1",
            "manifest_pack_digest": format!("sha256:{}", "2".repeat(64)),
            "allowed_object_digests": objects,
            "authorized_sources": [{"endpoint_id": self.source_id,
                "verification_key_digest": self.source_digest, "direct_addrs": []}],
            "maximum_total_bytes": total, "maximum_bytes_per_second": 33_554_432_u64,
            "maximum_chunk_bytes": 4_194_304_u64, "issued_at_unix_ms": not_before,
            "not_before_unix_ms": not_before, "expires_at_unix_ms": self.t0 + 600_000,
        });
        let envelope = json!({"protocol": protocol::membership::SIGNED_MESSAGE_PROTOCOL,
            "signature": seed().sign(&message).unwrap(), "message": message,
            "verification_key": seed().public_key_record()});
        let verified =
            ag::verify_grant(&envelope, &seed().public_key_record(), now_ms(), limits()).unwrap();
        (envelope, verified)
    }
    fn request(&self, grant: &Grant, id: &str, digest: &str, offset: u64, length: u64) -> Value {
        ag::sign_chunk_request(
            &self.member,
            grant,
            id,
            id,
            &self.source_id,
            digest,
            offset,
            length,
            self.t0,
            limits(),
        )
        .unwrap()
    }
}
fn wire(value: &Value) -> Vec<u8> {
    protocol::canonical::to_bytes(value).unwrap()
}
/// What a reply means, with the receipt checked as the phone checks it.
fn outcome(step: &str, reply: &Reply, request: Option<(&ChunkRequest, &Grant)>) -> Value {
    match reply.header["status"].as_str().unwrap() {
        "ok" => {
            let (request, grant) = request.expect("an ok reply needs its request");
            ag::verify_chunk_receipt(&reply.header["receipt"], request, grant, &reply.data)
                .unwrap_or_else(|e| panic!("{step}: receipt: {e:?}"));
            json!({"step": step, "outcome": "ok", "length": reply.data.len(),
                "data": ag::range_digest(&reply.data), "receipt": "valid"})
        }
        _ => json!({"step": step, "outcome": "refused", "code": reply.header["code"]}),
    }
}

fn object_bytes() -> Vec<u8> {
    (0..4096_u32).map(|i| (i % 251) as u8).collect()
}

/// The 18-request scenario against one sidecar kind; returns (outcomes, normalised journal lines).
fn scenario(start: &dyn Fn(&Path) -> Box<dyn Running>, label: &str) -> (Vec<Value>, Vec<Value>) {
    let dir = scratch(label);
    prepare_files(&dir);
    let object = object_bytes();
    let digest_a = ag::range_digest(&object);
    std::fs::write(dir.join("objects").join(&digest_a[7..]), &object).unwrap();
    // C: a file whose name says one digest and whose content is another (never served).
    let digest_c = ag::range_digest(b"the expected content of c");
    std::fs::write(
        dir.join("objects").join(&digest_c[7..]),
        b"tampered on disk",
    )
    .unwrap();
    // B: named in a grant, absent on disk.
    let digest_b = ag::range_digest(b"b is not on disk");
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .build()
        .unwrap();
    let mut running = Some(start(&dir));
    let address = running.as_ref().unwrap().address();
    let source_id = address.id.to_string();
    let member_id = SecretKey::from_bytes(&CLIENT_KEY).public().to_string();
    let world = World {
        source_digest: Identity::from_secret(&source_id, &SOURCE_SECRET)
            .unwrap()
            .verification_key_digest(),
        source_id,
        member: Identity::from_secret(&member_id, &MEMBER_SECRET).unwrap(),
        member_id,
        t0: now_ms(),
    };
    let ledger_path = dir.join("st").join("source-ledger.jsonl");
    let genesis = serde_json::from_str::<Value>(
        std::fs::read_to_string(&ledger_path)
            .unwrap()
            .lines()
            .next()
            .unwrap(),
    )
    .unwrap()["genesis_unix_ms"]
        .as_u64()
        .unwrap();
    let (g1_env, g1) = world.grant("grant-1", world.t0, 3000, &[&digest_a]);
    let (g3_env, g3) = world.grant("grant-3", world.t0, 3000, &[&digest_a]);
    let (g2_env, g2) = world.grant("grant-2", genesis - 1000, 3000, &[&digest_a]);
    let (g4_env, g4) = world.grant("grant-4", world.t0, 3000, &[&digest_b, &digest_c]);
    let _ = (g1_env, g3_env, g2_env, g4_env);
    let mut outcomes = Vec::new();
    runtime.block_on(async {
        let client = client_endpoint(&CLIENT_KEY).await;
        let other = client_endpoint(&OTHER_CLIENT_KEY).await;
        let mut connection = client
            .connect(address.clone(), ag::ARTIFACT_CHUNK_ALPN)
            .await
            .unwrap();
        // (step, grant, request id, digest, offset, length)
        let plan: Vec<(&str, &Grant, &str, &str, u64, u64)> = vec![
            ("granted_first", &g1, "r1", &digest_a, 0, 1000),
            ("replay_of_the_first", &g1, "r1", &digest_a, 0, 1000),
            ("granted_second", &g1, "r2", &digest_a, 1000, 1000),
            ("budget_exceeded", &g1, "r3", &digest_a, 2000, 1500),
            (
                "granted_third_fills_the_budget",
                &g1,
                "r4",
                &digest_a,
                2000,
                1000,
            ),
            ("budget_exhausted_by_one_byte", &g1, "r5", &digest_a, 0, 1),
            (
                "other_grant_has_its_own_budget",
                &g3,
                "r6",
                &digest_a,
                0,
                100,
            ),
            ("grant_began_before_genesis", &g2, "r7", &digest_a, 0, 10),
            ("object_missing_on_disk", &g4, "r8", &digest_b, 0, 10),
            (
                "object_content_tampered_on_disk",
                &g4,
                "r9",
                &digest_c,
                0,
                10,
            ),
            ("range_beyond_the_object", &g3, "r10", &digest_a, 4000, 200),
        ];
        for (step, grant, id, digest, offset, length) in &plan {
            let request = world.request(grant, id, digest, *offset, *length);
            let reply = exchange(&connection, &wire(&request)).await;
            let checked = ChunkRequest {
                request_id: (*id).to_owned(),
                source_endpoint_id: world.source_id.clone(),
                object_digest: (*digest).to_owned(),
                offset_bytes: *offset,
                length_bytes: *length,
            };
            outcomes.push(outcome(step, &reply, Some((&checked, grant))));
        }
        // A different transport peer presents the first member's request.
        let intruder = other
            .connect(address.clone(), ag::ARTIFACT_CHUNK_ALPN)
            .await
            .unwrap();
        let request = world.request(&g1, "r11", &digest_a, 0, 10);
        outcomes.push(outcome(
            "wrong_transport_peer",
            &exchange(&intruder, &wire(&request)).await,
            None,
        ));
        intruder.close(0u32.into(), b"done");
        outcomes.push(outcome(
            "not_json",
            &exchange(&connection, b"not json").await,
            None,
        ));
        let mut tampered = world.request(&g3, "r12", &digest_a, 0, 10);
        tampered["message"]["request_id"] = "tampered".into();
        outcomes.push(outcome(
            "tampered_request",
            &exchange(&connection, &wire(&tampered)).await,
            None,
        ));
        // RESTART: the fence and the budgets are durable.
        connection.close(0u32.into(), b"restart");
        running.take().unwrap().stop();
        running = Some(start(&dir));
        let address = running.as_ref().unwrap().address();
        connection = client
            .connect(address, ag::ARTIFACT_CHUNK_ALPN)
            .await
            .unwrap();
        let after: Vec<(&str, &Grant, &str, u64, u64)> = vec![
            ("replay_after_restart", &g1, "r1", 0, 1000),
            ("budget_still_exhausted_after_restart", &g1, "r13", 0, 1),
            ("other_grant_continues_after_restart", &g3, "r14", 100, 100),
            (
                "granted_after_restart_replay_of_the_other",
                &g3,
                "r6",
                0,
                100,
            ),
        ];
        for (step, grant, id, offset, length) in after {
            let request = world.request(grant, id, &digest_a, offset, length);
            let reply = exchange(&connection, &wire(&request)).await;
            let checked = ChunkRequest {
                request_id: id.to_owned(),
                source_endpoint_id: world.source_id.clone(),
                object_digest: digest_a.clone(),
                offset_bytes: offset,
                length_bytes: length,
            };
            outcomes.push(outcome(step, &reply, Some((&checked, grant))));
        }
        connection.close(0u32.into(), b"done");
        client.close().await;
        other.close().await;
    });
    running.take().unwrap().stop();
    drop(runtime);
    // The journal as the sidecar left it, times against the scenario start.
    let text = std::fs::read_to_string(&ledger_path).unwrap();
    let mut lines = text.lines();
    let header: Value = serde_json::from_str(lines.next().unwrap()).unwrap();
    assert_eq!(header["protocol"], "mycelium.artifact_source_ledger.v1");
    let journal: Vec<Value> = lines
        .map(|line| {
            let mut entry: Value = serde_json::from_str(line).unwrap();
            for key in ["e", "x"] {
                entry[key] = (entry[key].as_u64().unwrap() as i64 - world.t0 as i64).into();
            }
            entry
        })
        .collect();
    std::fs::remove_dir_all(&dir).unwrap();
    (outcomes, journal)
}

fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../fixtures/sidecar-artifact-serve-oracle.json"
    ))
    .unwrap()
}

#[test]
fn the_rust_sidecar_serves_the_recorded_oracle_outcomes_and_leaves_the_same_journal() {
    let f = fixture();
    assert!(
        f["provenance"]["oracle"]
            .as_str()
            .unwrap()
            .contains("f4e366e98a1f705dfaf182897044500d290dbd7f")
    );
    let (outcomes, journal) = scenario(&RustSide::start, "rust");
    assert_eq!(Value::from(outcomes), f["outcomes"], "outcomes");
    assert_eq!(Value::from(journal), f["journal"], "journal lines");
}

fn oracle_binary() -> PathBuf {
    let path = PathBuf::from(
        std::env::var("L9_ORACLE_SIDECAR").expect("L9_ORACLE_SIDECAR must name the oracle binary"),
    );
    assert!(path.is_file());
    path
}

#[test]
#[ignore = "needs L9_ORACLE_SIDECAR=<oracle mycelium-iroh-sidecar @ f4e366e9>; run with --ignored"]
fn the_live_oracle_and_the_rust_sidecar_serve_the_same_outcomes_and_leave_the_same_journal() {
    let binary = oracle_binary();
    let (oracle_outcomes, oracle_journal) =
        scenario(&|dir| OracleChild::start(&binary, dir), "oracle");
    if let Some(path) = std::env::var_os("L9_WRITE_SERVE_FIXTURE") {
        let digest = {
            let bytes = std::fs::read(&binary).unwrap();
            ag::range_digest(&bytes)
        };
        let doc = json!({
            "provenance": {
                "oracle": "mycelium-iroh-sidecar RELEASE profile (opt-level 0) of f4e366e98a1f705dfaf182897044500d290dbd7f, --local-only with the four --artifact-* flags",
                "binary_digest": digest,
                "method": "EXECUTED: crates/runtime/tests/oracle_artifact_interop.rs ignored test with L9_WRITE_SERVE_FIXTURE; 18 signed chunk requests over the artifact ALPN, one restart",
            },
            "outcomes": oracle_outcomes,
            "journal": oracle_journal,
        });
        std::fs::write(path, serde_json::to_string_pretty(&doc).unwrap() + "\n").unwrap();
    }
    let (rust_outcomes, rust_journal) = scenario(&RustSide::start, "rust-live");
    assert_eq!(rust_outcomes, oracle_outcomes, "outcomes");
    assert_eq!(rust_journal, oracle_journal, "journal lines");
}
