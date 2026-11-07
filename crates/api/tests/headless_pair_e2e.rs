// SPDX-License-Identifier: AGPL-3.0-only
//! Process-level end to end on loopback, two REAL `headless` launchers (Rust owner + Rust member) driven
//! only through their public HTTP API, with the scenario steps of the L3 corpus (`twohost-scenarios.json`
//! `relay-window` + `joined-bringup`: the same paths, bodies and expectations): the owner starts its
//! external relay, creates its swarm (owner collapse `pair_request` -> `approve_pairing` ->
//! `accept_pairing` on `crates/seed`), opens a pairing window, the member submits the join link with
//! standing consent, the owner approves, the member's poll finishes the join against the owner's seed.
//!
//! The only test seams are the oracle's own: the member reaches the relay's loopback proxy instead of the
//! public origin (`MYCELIUM_L10_TEST_JOIN_BASE`, v0.1.4 `_test_join_base_url`) and the owner's local seed
//! instead of the public host (`MYCELIUM_L10_TEST_SEED_DIRECT`, pinned to the owner's local CA). Nothing
//! opens a tunnel; every process is reaped by exact pid and the test asserts the pids are gone.
//!
//! Second test: `POST /api/stop` / `POST /api/start` and a full owner restart replayed against the EXECUTED
//! oracle (`L10-evidence/r16-probe-start-stop.py` -> `fixtures/oracle_start_stop.json`, FX-L10-START-STOP,
//! v0.1.4 f4e366e9, 22 cases): answers, the `authority` block and its key tree, `running`, the relay.
use budgets::{BudgetId, Entries};
use rustix::process::{Pid, Signal, kill_process, test_kill_process};
use rustls::{ServerConnection, StreamOwned};
use serde_json::{Value, json};
use std::{
    fs,
    io::{BufRead, BufReader, Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};

#[path = "support/headless_observation.rs"]
mod headless_observation;
#[path = "support/tiny_repository.rs"]
mod tiny_repository;

const ORIGIN: &str = "https://v02-pair-e2e.example.org";
const STORAGE: u64 = 2_000_000_000;

fn profile_dir() -> PathBuf {
    let exe = std::env::current_exe().expect("exe");
    exe.parent()
        .and_then(Path::parent)
        .expect("profile")
        .to_path_buf()
}
/// The launcher under test is the RELEASE build of the example (the executable the two-Mac window runs):
/// RSA-2048 TLS generation takes 6-23 s in the unoptimised dev profile (SE-L10-TLS-KEYGEN-SLOW) and about a
/// second here, so these process-level tests no longer race the 30 s `WorkerStartup` read budget (which is
/// not relaxed) on a loaded host.
fn exe_dir() -> PathBuf {
    profile_dir().with_file_name("release")
}
fn build() {
    let target = exe_dir();
    let target = target.parent().expect("target");
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../Cargo.toml");
    let mut command = Command::new(std::env::var("CARGO").unwrap_or_else(|_| "cargo".into()));
    command.args([
        "build",
        "--offline",
        "--locked",
        "-j",
        "2",
        "-p",
        "api",
        "--release",
    ]);
    command.args(["--example", "headless"]);
    command.arg("--manifest-path").arg(manifest);
    command.arg("--target-dir").arg(target);
    let output = command.output().expect("cargo is runnable");
    assert!(
        output.status.success(),
        "nested build failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
/// Every test here creates a swarm (RSA-2048 TLS generation) and runs real launchers for a minute: one at a
/// time keeps the host load of this file predictable.
fn one_at_a_time() -> std::sync::MutexGuard<'static, ()> {
    static LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    LOCK.lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}
fn budget(id: BudgetId) -> Duration {
    Duration::from_millis(id.span(Entries::new(0)).expect("budget").millis())
}
fn patience() -> Duration {
    budget(BudgetId::ModelSelection) * 4
}
fn poll_every() -> Duration {
    budget(BudgetId::OwnerPoll)
}
fn free_port() -> u16 {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("bind");
    listener.local_addr().expect("addr").port()
}

/// One launcher process; dropped = SIGTERM then wait (exact pid), the drop asserts the pid is gone.
struct Instance {
    child: Child,
    root: PathBuf,
    port: u16,
    cookie: String,
    env: Vec<(String, String)>,
    /// Set while the state directory must survive this process (a restart).
    keep_state: bool,
}
impl Instance {
    fn launch(root: &Path, env: &[(&str, String)]) -> Self {
        fs::create_dir_all(root).expect("root");
        fs::set_permissions(root, fs::Permissions::from_mode(0o700)).expect("mode");
        let mut command = Command::new(exe_dir().join("examples/headless"));
        if !env
            .iter()
            .any(|(name, _)| *name == "MYCELIUM_L10_TEST_HUB_ORIGIN")
        {
            command.arg("--offline");
        }
        command
            .args(["--headless", "--state-root"])
            .arg(root)
            .env("HOME", root)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::from(
                fs::File::create(root.with_extension("stderr")).expect("stderr file"),
            ));
        for (name, value) in env {
            command.env(name, value);
        }
        let mut child = command.spawn().expect("the headless example was built");
        let mut first = String::new();
        BufReader::new(child.stdout.take().expect("stdout"))
            .read_line(&mut first)
            .expect("first line");
        assert!(
            first.contains("launcher_started"),
            "{first:?} stderr: {}",
            fs::read_to_string(root.with_extension("stderr")).unwrap_or_default()
        );
        let form = fs::read_to_string(root.join("open-demo.html")).expect("launch form");
        let attribute = |name: &str| {
            let key = format!("{name}=\"");
            form.split_once(&key)
                .and_then(|(_, rest)| rest.split_once('"'))
                .map(|(value, _)| value.to_owned())
                .expect("form attribute")
        };
        let action = attribute("action");
        let port: u16 = action
            .strip_prefix("http://127.0.0.1:")
            .and_then(|rest| rest.strip_suffix("/session"))
            .and_then(|p| p.parse().ok())
            .expect("loopback origin");
        let mut me = Self {
            child,
            root: root.to_path_buf(),
            port,
            cookie: String::new(),
            env: env
                .iter()
                .map(|(k, v)| ((*k).to_owned(), v.clone()))
                .collect(),
            keep_state: false,
        };
        let signed_in = me.raw(
            "POST",
            "/session",
            format!("launch={}", attribute("value")).as_bytes(),
            &[
                ("Content-Type", "application/x-www-form-urlencoded"),
                ("Origin", "null"),
            ],
        );
        assert_eq!(signed_in.0, 303, "{signed_in:?}");
        me.cookie = signed_in
            .1
            .lines()
            .find_map(|l| {
                l.to_ascii_lowercase()
                    .starts_with("set-cookie: ")
                    .then(|| &l[12..])
            })
            .and_then(|c| c.split(';').next())
            .expect("session cookie")
            .to_owned();
        me
    }
    fn raw(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        headers: &[(&str, &str)],
    ) -> (u16, String, Vec<u8>) {
        let limit = budget(BudgetId::WorkerStartup);
        let mut stream =
            TcpStream::connect_timeout(&SocketAddr::from((Ipv4Addr::LOCALHOST, self.port)), limit)
                .expect("connect");
        stream.set_read_timeout(Some(limit)).expect("timeout");
        let mut wire = format!(
            "{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\nContent-Length: {}\r\n",
            self.port,
            body.len()
        );
        if !self.cookie.is_empty() {
            wire.push_str(&format!("Cookie: {}\r\n", self.cookie));
        }
        for (key, value) in headers {
            wire.push_str(&format!("{key}: {value}\r\n"));
        }
        wire.push_str("\r\n");
        stream.write_all(wire.as_bytes()).expect("write head");
        stream.write_all(body).expect("write body");
        let mut reply = Vec::new();
        let received = stream.read_to_end(&mut reply);
        if let Err(error) = &received {
            let safe_path = match path {
                "/api/join/window" | "/api/join/window/poll" | "/api/status" => path,
                _ => "other",
            };
            let status = reply
                .split(|b| *b == b' ')
                .nth(1)
                .and_then(|s| std::str::from_utf8(s).ok())
                .and_then(|s| s.parse::<u16>().ok());
            let child_exited = rustix::process::waitid(
                rustix::process::WaitId::Pid(self.pid()),
                rustix::process::WaitIdOptions::EXITED
                    | rustix::process::WaitIdOptions::NOHANG
                    | rustix::process::WaitIdOptions::NOWAIT,
            )
            .map(|v| v.is_some());
            eprintln!(
                "pair-http phase=read path={safe_path} kind={:?} status={status:?} bytes={} complete={} eof=false owned_child_exited={child_exited:?}",
                error.kind(),
                reply.len(),
                api::framing::response_complete(&reply)
            );
        }
        received.expect("read reply");
        let split = reply
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .expect("reply head");
        let head = String::from_utf8_lossy(&reply[..split]).into_owned();
        let status = head
            .split(' ')
            .nth(1)
            .and_then(|s| s.parse().ok())
            .expect("status");
        (status, head, reply[split + 4..].to_vec())
    }
    fn get(&self, path: &str) -> (u16, Value) {
        let (status, _, body) = self.raw("GET", path, &[], &[]);
        (status, serde_json::from_slice(&body).unwrap_or(Value::Null))
    }
    fn post(&self, path: &str, body: &Value) -> (u16, Value) {
        let origin = format!("http://127.0.0.1:{}", self.port);
        let (status, _, reply) = self.raw(
            "POST",
            path,
            body.to_string().as_bytes(),
            &[
                ("Content-Type", "application/json"),
                ("Origin", &origin),
                ("X-Mycelium-Action", "1"),
            ],
        );
        if path == "/api/join/window" {
            let parsed: Value = serde_json::from_slice(&reply).unwrap_or(Value::Null);
            let public_error = parsed["error"].as_str().filter(|code| {
                code.len() <= 80 && code.bytes().all(|b| b.is_ascii_lowercase() || b == b'_')
            });
            eprintln!(
                "pair-http phase=complete path=/api/join/window status={status} public_error={public_error:?} body_bytes={} eof=true",
                reply.len()
            );
        }
        if status >= 400 {
            // The retained private record of the refusal explains it in a failed run.
            let record = fs::read_to_string(self.root.join("last-action-error.private.json"))
                .unwrap_or_default();
            let log = fs::read_to_string(self.root.with_extension("stderr")).unwrap_or_default();
            eprintln!("POST {path} -> {status}: {record}\nstderr: {log}");
        }
        (
            status,
            serde_json::from_slice(&reply).unwrap_or(Value::Null),
        )
    }
    /// Polls `fetch` until `done` (the scenario runner's `until`).
    fn until(
        &self,
        what: &str,
        mut fetch: impl FnMut() -> Value,
        done: impl Fn(&Value) -> bool,
    ) -> Value {
        let deadline = Instant::now() + patience();
        loop {
            let value = fetch();
            if done(&value) {
                return value;
            }
            assert!(Instant::now() < deadline, "{what}: last answer {value}");
            std::thread::sleep(poll_every());
        }
    }
    /// `owner_full_restart`: SIGTERM this launcher (exact pid, state kept), then start a new one on the
    /// same state root.
    fn restart(mut self) -> Self {
        self.keep_state = true;
        let root = self.root.clone();
        let env: Vec<(String, String)> = self.env.clone();
        drop(self);
        let env: Vec<(&str, String)> = env.iter().map(|(k, v)| (k.as_str(), v.clone())).collect();
        Self::launch(&root, &env)
    }
    fn pid(&self) -> Pid {
        Pid::from_raw(i32::try_from(self.child.id()).expect("pid")).expect("nonzero pid")
    }
    /// Every TCP port this process listens on (the app, the relay proxy, the seed).
    fn listening(&self) -> Vec<u16> {
        let out = Command::new("/usr/sbin/lsof")
            .args(["-nP", "-a", "-iTCP", "-sTCP:LISTEN", "-Fn", "-p"])
            .arg(self.child.id().to_string())
            .output()
            .expect("lsof");
        String::from_utf8_lossy(&out.stdout)
            .lines()
            .filter_map(|l| l.strip_prefix('n'))
            .filter_map(|l| l.rsplit(':').next()?.parse().ok())
            .collect()
    }
}
impl Drop for Instance {
    fn drop(&mut self) {
        kill_process(self.pid(), Signal::TERM).unwrap_or_default();
        let deadline = Instant::now() + patience();
        while self.child.try_wait().ok().flatten().is_none() && Instant::now() < deadline {
            std::thread::sleep(poll_every());
        }
        if self.child.try_wait().ok().flatten().is_none() {
            kill_process(self.pid(), Signal::KILL).unwrap_or_default();
            self.child.wait().unwrap_or_default();
        }
        // exact pid gone: signal 0 to a reaped pid is ESRCH
        assert!(
            test_kill_process(self.pid()).is_err(),
            "orphan {}",
            self.child.id()
        );
        if !self.keep_state {
            fs::remove_dir_all(&self.root).unwrap_or_default();
        }
    }
}

fn consent(storage: u64) -> Value {
    json!({
        "peer_visibility": true, "power": "ac_only", "storage_bytes": storage,
        "network": "unmetered_only", "background": false, "diagnostics": false,
        "thermal": "stop_on_serious",
    })
}

/// A TLS-terminating stand-in for the public edge (cloudflared): HTTPS in with the owner's own local
/// certificate, one plain request/response to the relay's loopback proxy behind it. Its address is
/// stable across an owner restart, as the public origin is (the seed's own port is not).
struct Edge {
    address: SocketAddr,
    /// Requests forwarded to the proxy and answered.
    served: Arc<AtomicUsize>,
    stop: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}
impl Edge {
    fn start(material: &seed::tls::Material, proxy: u16) -> Self {
        let config = seed::tls::server_config(material).expect("edge tls config");
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).expect("edge bind");
        listener.set_nonblocking(true).expect("nonblocking");
        let address = listener.local_addr().expect("edge address");
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let served = Arc::new(AtomicUsize::new(0));
        let counter = served.clone();
        let handle = std::thread::spawn(move || {
            let mut connections = Vec::new();
            while !flag.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((tcp, _)) => {
                        let (config, counter) = (config.clone(), counter.clone());
                        connections.push(std::thread::spawn(move || {
                            edge_connection(tcp, config, proxy, &counter);
                        }));
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(poll_every());
                    }
                    Err(_) => break,
                }
            }
            for connection in connections {
                connection.join().unwrap_or_default();
            }
        });
        Self {
            address,
            served,
            stop,
            handle: Some(handle),
        }
    }
}
impl Drop for Edge {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(handle) = self.handle.take() {
            handle.join().unwrap_or_default();
        }
    }
}
fn edge_connection(
    tcp: TcpStream,
    config: Arc<rustls::ServerConfig>,
    proxy: u16,
    served: &AtomicUsize,
) {
    let limit = budget(BudgetId::WorkerStartup);
    if tcp.set_nonblocking(false).is_err() || tcp.set_read_timeout(Some(limit)).is_err() {
        return;
    }
    let Ok(connection) = ServerConnection::new(config) else {
        return;
    };
    let mut client = StreamOwned::new(connection, tcp);
    let mut request = Vec::new();
    let mut chunk = [0u8; 16 * 1024];
    loop {
        match client.read(&mut chunk) {
            Ok(0) | Err(_) => return,
            Ok(n) => request.extend_from_slice(&chunk[..n]),
        }
        let Some(split) = request.windows(4).position(|w| w == b"\r\n\r\n") else {
            continue;
        };
        let head = String::from_utf8_lossy(&request[..split]).to_lowercase();
        let length = head
            .lines()
            .find_map(|l| l.strip_prefix("content-length:"))
            .and_then(|v| v.trim().parse::<usize>().ok())
            .unwrap_or(0);
        if request.len() >= split + 4 + length {
            break;
        }
    }
    let Ok(mut upstream) = TcpStream::connect((Ipv4Addr::LOCALHOST, proxy)) else {
        return;
    };
    if upstream.set_read_timeout(Some(limit)).is_err() || upstream.write_all(&request).is_err() {
        return;
    }
    let mut reply = Vec::new();
    loop {
        match upstream.read(&mut chunk) {
            Ok(0) | Err(_) => break,
            Ok(n) => reply.extend_from_slice(&chunk[..n]),
        }
        if api::framing::response_complete(&reply) {
            break;
        }
    }
    if client.write_all(&reply).is_ok() {
        served.fetch_add(1, Ordering::SeqCst);
        client.conn.send_close_notify();
        client.flush().unwrap_or_default();
    }
}

/// The unchanged `relay-window` + `joined-bringup` steps up to `owner_sees_member` on loopback: returns the
/// owner, the joined member and the relay's proxy port.
fn pair_up(base: &Path) -> (Instance, Instance, u16) {
    let (owner, member, proxy, _) = pair_up_with(base, false);
    (owner, member, proxy)
}
/// `via_edge`: the member reaches the owner's seed through [`Edge`] (the relay's proxy behind a TLS front)
/// instead of the seed's own port, as it does through the public origin.
fn pair_up_with(base: &Path, via_edge: bool) -> (Instance, Instance, u16, Option<Edge>) {
    pair_up_with_owner_env(base, via_edge, &[])
}
fn pair_up_with_owner_env(
    base: &Path,
    via_edge: bool,
    env: &[(&str, String)],
) -> (Instance, Instance, u16, Option<Edge>) {
    // ---- owner: relay (external, no tunnel), create, window       (relay-window steps 1-3, 6)
    let owner = Instance::launch(&base.join("owner"), env);
    let proxy = free_port();
    let (status, started) = owner.post(
        "/api/network/start",
        &json!({"relay_acknowledged": true, "max_seconds": 1800, "provider": "external",
                "public_origin": ORIGIN, "proxy_port": proxy}),
    );
    assert_eq!(status, 200, "{started}");
    assert_eq!(started["state"], "relay_process_running", "{started}");
    let (status, created) = owner.post("/api/create", &json!({"consent": consent(4_000_000_000)}));
    assert_eq!(status, 200, "{created}");
    assert_eq!(created["membership_state"], "current", "{created}");
    let (_, status_doc) = owner.get("/api/status");
    assert_eq!(status_doc["network"]["seed_bound"], true, "{status_doc}");
    assert_eq!(
        status_doc["authority"]["member_count"], 1,
        "the owner collapsed into member 1: {status_doc}"
    );
    assert!(
        !status_doc["authority"]["members"][0]["clock"].is_null(),
        "{status_doc}"
    );
    let (status, opened) = owner.post("/api/pair/window/open", &json!({"ttl_seconds": 600}));
    assert_eq!(status, 200, "{opened}");
    assert_eq!(opened["state"], "open");
    let link = opened["join_link"].as_str().expect("link").to_owned();
    assert!(link.starts_with("mycelium-join:"));
    let window_id = opened["window_id"].clone();
    // ---- the owner's seed port: the third listener (app + proxy + seed)
    let seed_ports: Vec<u16> = owner
        .listening()
        .into_iter()
        .filter(|p| *p != owner.port && *p != proxy)
        .collect();
    assert_eq!(seed_ports.len(), 1, "one seed listener: {seed_ports:?}");
    let ca = base.join("owner/seed-rs/tls/ca.pem");
    assert!(ca.is_file(), "{}", ca.display());

    let edge = via_edge.then(|| {
        Edge::start(
            &seed::tls::Material {
                ca_certificate: ca.clone(),
                server_certificate: base.join("owner/seed-rs/tls/server.pem"),
                server_private_key: base.join("owner/seed-rs/tls/server.key"),
            },
            proxy,
        )
    });
    let seed_address = edge
        .as_ref()
        .map_or(format!("127.0.0.1:{}", seed_ports[0]), |e| {
            e.address.to_string()
        });
    // ---- member: the seams of the oracle's test launcher
    let member = Instance::launch(
        &base.join("member"),
        &[
            (
                "MYCELIUM_L10_TEST_JOIN_BASE",
                format!("http://127.0.0.1:{proxy}"),
            ),
            (
                "MYCELIUM_L10_TEST_SEED_DIRECT",
                format!("{seed_address}|{}", ca.display()),
            ),
        ],
    );
    let (_, member_status) = member.get("/api/status");
    let digest = member_status["join_flow"]["standing_disclosure"]["digest"].clone();
    assert!(digest.is_string(), "{member_status}");
    let caps = |wall: u64| {
        json!({"storage_bytes": STORAGE, "max_memory_bytes": 4_294_967_296u64,
               "max_wall_seconds": wall, "max_commands": 5000})
    };
    let join_body = |wall: u64| {
        json!({"consent": consent(STORAGE),
               "standing_consent": {"acknowledged": true, "disclosure_digest": digest, "caps": caps(wall)},
               "join": link})
    };
    // member_join_bad_caps_refused
    let (status, refused) = member.post("/api/join/window", &join_body(30));
    assert_eq!(
        (status, &refused["error"]),
        (400, &json!("standing_consent_caps_invalid"))
    );
    // joined-bringup: member_join_with_consent
    let (status, waiting) = member.post("/api/join/window", &join_body(600));
    assert_eq!(status, 200, "{waiting}");
    assert_eq!(waiting["state"], "waiting_for_owner", "{waiting}");
    assert_eq!(waiting["pinned"], true, "{waiting}");
    // owner_sees_claim
    let claimed = owner.until(
        "owner sees the claim",
        || owner.post("/api/pair/window/list", &json!({})).1,
        |v| v["windows"][0]["state"] == "claimed",
    );
    assert_eq!(
        claimed["windows"][0]["pending"]["quota"]["storage_bytes"],
        STORAGE
    );
    let request_digest = claimed["windows"][0]["pending"]["request_digest"].clone();
    // owner_approves
    let (status, approved) = owner.post(
        "/api/pair/window/approve",
        &json!({"window_id": window_id, "request_digest": request_digest}),
    );
    assert_eq!(status, 200, "{approved}");
    // member_joined
    let joined = member.until(
        "member joined",
        || member.post("/api/join/window/poll", &json!({})).1,
        |v| v["state"] == "joined",
    );
    assert_eq!(joined["membership_state"], "current", "{joined}");
    assert_eq!(joined["route_ready"], false, "{joined}");
    // member_consent_bound
    let (_, bound) = member.get("/api/status");
    assert_eq!(bound["installation_role"], "target", "{bound}");
    assert_eq!(
        bound["resource_policy"]["storage_bytes"], STORAGE,
        "{bound}"
    );
    // owner_sees_member: two members, both known to the owner's seed
    let seen = owner.until(
        "owner sees the member",
        || owner.get("/api/status").1,
        |v| v["authority"]["member_count"] == 2,
    );
    assert_eq!(seen["authority"]["membership_state"], "current", "{seen}");
    (owner, member, proxy, edge)
}

/// Exercise the actual headless composition root, not a separately mounted test server. The signer is
/// this isolated launcher's real joined identity; this proves signed exchange, NOT a native session.
fn signed_headless_exchange(owner: &Instance, member: &Instance, proxy: u16) -> Value {
    signed_headless_exchange_report(owner, member, proxy, None)
}
type ReportBuilder<'a> =
    &'a dyn Fn(&protocol::identity::Identity, &Value, &Value, &Value, f64) -> Value;
fn signed_headless_exchange_report(
    owner: &Instance,
    member: &Instance,
    proxy: u16,
    report: Option<ReportBuilder<'_>>,
) -> Value {
    use api::run_exchange_routes::{DISCOVERY, PATH_READ, PATH_REPORT, READ, REPORT, RESPONSE};
    use protocol::identity::Identity;
    let identity: Value =
        serde_json::from_slice(&fs::read(member.root.join("target-identity.json")).unwrap())
            .unwrap();
    let secret_text = identity["secret"].as_str().unwrap();
    let mut secret = [0_u8; 32];
    for (i, b) in secret.iter_mut().enumerate() {
        *b = u8::from_str_radix(&secret_text[2 * i..2 * i + 2], 16).unwrap();
    }
    let signer = Identity::from_secret(identity["endpoint_id"].as_str().unwrap(), &secret).unwrap();
    let db = rusqlite::Connection::open_with_flags(
        owner.root.join("seed-rs/seed.sqlite"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    let snapshot: String = db
        .query_row("SELECT snapshot FROM rs_seed_table WHERE id = 1", [], |r| {
            r.get(0)
        })
        .unwrap();
    fn bound(value: &Value, endpoint: &str) -> Option<Value> {
        match value {
            Value::Object(m)
                if m.get("endpoint_id").and_then(Value::as_str) == Some(endpoint)
                    && m.contains_key("lease_expires_at") =>
            {
                Some(value.clone())
            }
            Value::Object(m) => m.values().find_map(|v| bound(v, endpoint)),
            Value::Array(a) => a.iter().find_map(|v| bound(v, endpoint)),
            _ => None,
        }
    }
    let authority = bound(
        &serde_json::from_str(&snapshot).unwrap(),
        signer.endpoint_id(),
    )
    .expect("actual current joined member");
    let swarm: Value =
        serde_json::from_slice(&fs::read(owner.root.join("owner-swarm.json")).unwrap()).unwrap();
    let seed_identity: Value =
        serde_json::from_slice(&fs::read(owner.root.join("seed-identity.json")).unwrap()).unwrap();
    let seed_secret = seed_identity["secret"].as_str().unwrap();
    for (i, b) in secret.iter_mut().enumerate() {
        *b = u8::from_str_radix(&seed_secret[2 * i..2 * i + 2], 16).unwrap();
    }
    let pinned = Identity::from_secret(seed_identity["endpoint_id"].as_str().unwrap(), &secret)
        .unwrap()
        .public_key_record();
    let ports: Vec<_> = owner
        .listening()
        .into_iter()
        .filter(|p| *p != owner.port && *p != proxy)
        .collect();
    assert_eq!(ports.len(), 1);
    use rustls_pki_types::pem::PemObject;
    let mut roots = rustls::RootCertStore::empty();
    for cert in
        rustls_pki_types::CertificateDer::pem_file_iter(owner.root.join("seed-rs/tls/ca.pem"))
            .unwrap()
    {
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
    let now = || {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs_f64()
    };
    let mut announcement = Value::Null;
    for path in [PATH_READ, PATH_REPORT] {
        let at = now();
        let mut statement = json!({"protocol":if path==PATH_READ {READ} else {REPORT},"request_id":"headless-read","swarm_id":swarm["swarm_id"],"sequence":1,"nonce":"headless-nonce","issued_at":at,"expires_at":at+budget(BudgetId::ControlTtl).as_secs_f64(),"target_node_id":authority["node_id"],"target_endpoint_id":signer.endpoint_id(),"target_key_digest":signer.verification_key_digest(),"membership_generation":authority["generation"],"member_incarnation":authority["incarnation"],"run_id":DISCOVERY,"deployment_id":DISCOVERY});
        if path == PATH_REPORT {
            statement["report"] = match report {
                Some(report) => report(&signer, &authority, &swarm, &announcement, at),
                None => {
                    json!({"protocol":api::run_exchange::MEMBER_REPORT,"run_id":null,"deployment_id":null,"state":"idle","detail":null,"model":null,"progress":null,"descriptor":null,"issued_at":at})
                }
            };
        }
        let envelope = json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL,"signature":signer.sign(&statement).unwrap(),"verification_key":signer.public_key_record(),"statement":statement});
        let bytes = protocol::canonical::to_bytes(&envelope).unwrap();
        let tcp = TcpStream::connect((Ipv4Addr::LOCALHOST, ports[0])).unwrap();
        tcp.set_read_timeout(Some(budget(BudgetId::WorkerStartup)))
            .unwrap();
        let conn = rustls::ClientConnection::new(
            config.clone(),
            rustls_pki_types::ServerName::IpAddress(Ipv4Addr::LOCALHOST.into()),
        )
        .unwrap();
        let mut stream = rustls::StreamOwned::new(conn, tcp);
        write!(stream,"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n",bytes.len()).unwrap();
        stream.write_all(&bytes).unwrap();
        stream.flush().unwrap();
        let mut reply = Vec::new();
        stream.read_to_end(&mut reply).unwrap();
        let split = reply.windows(4).position(|w| w == b"\r\n\r\n").unwrap();
        let status = std::str::from_utf8(&reply[..split])
            .unwrap()
            .split_whitespace()
            .nth(1)
            .unwrap();
        let body: Value = serde_json::from_slice(&reply[split + 4..]).unwrap();
        assert_eq!(status, "200", "actual headless seed: {body}");
        let verified = protocol::managed::verify(
            &body,
            RESPONSE,
            &pinned,
            seed_identity["endpoint_id"].as_str().unwrap(),
            now(),
            protocol::TimeLimits {
                max_ttl_seconds: budget(BudgetId::ControlTtl).as_secs_f64(),
                clock_skew_seconds: budget(BudgetId::ControlClockSkew).as_secs_f64(),
            },
        )
        .unwrap();
        assert_eq!(verified["target_node_id"], authority["node_id"]);
        if report.is_none() {
            assert_eq!(verified["announcement"]["phase"], "idle");
        }
        announcement = verified["announcement"].clone();
    }
    let diagnostic = owner.until(
        "authenticated exchange clock is published",
        || owner.get("/api/status").1,
        |status| {
            status["authority"]["members"]
                .as_array()
                .is_some_and(|members| {
                    members.iter().any(|m| {
                        m["node_id"] == authority["node_id"]
                            && m["clock"]["samples"].as_u64().is_some_and(|n| n >= 2)
                    })
                })
        },
    );
    let clock = &diagnostic["authority"]["members"]
        .as_array()
        .unwrap()
        .iter()
        .find(|m| m["node_id"] == authority["node_id"])
        .unwrap()["clock"];
    assert!(clock["offset_seconds"].is_number());
    assert_eq!(clock["fresh"], true);
    announcement
}

#[test]
fn a_rust_member_joins_a_rust_owner_through_the_window_and_the_collapsed_seed() {
    let _serial = one_at_a_time();
    build();
    // The oracle's registry refuses a symlink anywhere on the path (`SqliteInviteRegistry._prepare_path`).
    let base = fs::canonicalize(std::env::temp_dir())
        .expect("tmp")
        .join(format!("l10r16-pair-{}", std::process::id()));
    fs::remove_dir_all(&base).unwrap_or_default();
    let (owner, member, proxy) = pair_up(&base);
    let initial = signed_headless_exchange(&owner, &member, proxy);
    // This fresh isolated root has no prepared model. Exercise the REAL host's owning Runner without
    // authorizing any acquisition or native process: its model step must stop at the source refusal.
    let (code, started_run) = owner.post("/api/demo/start", &json!({}));
    assert_eq!(code, 200, "{started_run}");
    assert_eq!(started_run["state"], "running");
    let (_, immediate) = owner.get("/api/status");
    assert_eq!(
        immediate["owner_run"]["run_id"], started_run["run_id"],
        "POST published before return"
    );
    let refused = owner.until(
        "real Runner refuses absent model",
        || owner.get("/api/status").1,
        |v| v["owner_run"]["state"] == "failed",
    );
    assert_eq!(
        refused["owner_run"]["error_code"],
        "owner_model_not_prepared"
    );
    assert_eq!(refused["owner_run"]["route_ready"], false);
    assert_eq!(owner.post("/api/demo/stop", &json!({})).0, 200);
    // ---- owner-restart-persist / f3 (steps owner_full_restart .. owner_restart_member_preserved)
    let owner = owner.restart();
    let (status, restored) = owner.post(
        "/api/network/start",
        &json!({"relay_acknowledged": true, "max_seconds": 1800, "provider": "external",
                "public_origin": ORIGIN, "proxy_port": proxy}),
    );
    assert_eq!(status, 200, "{restored}");
    assert_eq!(restored["state"], "relay_process_running", "{restored}");
    assert_eq!(
        restored["seed_bound"], true,
        "the restarted owner's seed is bound again: {restored}"
    );
    let (status, started_again) = owner.post("/api/start", &json!({}));
    assert_eq!(status, 200, "{started_again}");
    let kept = owner.until(
        "the restarted owner keeps its member",
        || owner.get("/api/status").1,
        |v| v["authority"]["membership_state"] == "current",
    );
    assert_eq!(kept["authority"]["member_count"], 2, "{kept}");
    let restored_exchange = signed_headless_exchange(&owner, &member, proxy);
    assert_ne!(
        initial["owner_incarnation"],
        restored_exchange["owner_incarnation"]
    );
    let pids = [owner.child.id(), member.child.id()];
    drop(member);
    drop(owner);
    for pid in pids {
        let pid = Pid::from_raw(i32::try_from(pid).expect("pid")).expect("pid");
        assert!(test_kill_process(pid).is_err(), "orphan {pid:?}");
    }
    fs::remove_dir_all(&base).unwrap_or_default();
}

// ---- FX-L10-START-STOP
fn tree(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            Value::Object(map.iter().map(|(k, v)| (k.clone(), tree(v))).collect())
        }
        Value::Array(items) => Value::Array(items.first().map(tree).into_iter().collect()),
        Value::Null => json!("NoneType"),
        Value::Bool(_) => json!("bool"),
        Value::String(_) => json!("str"),
        Value::Number(n) if n.is_f64() => json!("float"),
        Value::Number(_) => json!("int"),
    }
}
/// The fixture's `pick`: the values the probe recorded for a status document.
fn pick(status: &Value) -> Value {
    let paths = [
        "installation_role",
        "running",
        "authority.protocol",
        "authority.started",
        "authority.swarm_created",
        "authority.local_tls",
        "authority.membership_state",
        "authority.member_count",
        "authority.capability_state",
        "authority.capability",
        "authority.membership_generation",
        "authority.lease_state",
        "authority.contribution_enabled",
        "authority.route_ready",
        "authority.members.generation",
        "authority.members.lifecycle_state",
        "network.state",
        "network.seed_bound",
        "network.public_origin",
        "owner_run.state",
    ];
    let mut out = serde_json::Map::new();
    for path in paths {
        let mut current = status.clone();
        for part in path.split('.') {
            current = match current {
                Value::Array(items) => Value::Array(
                    items
                        .iter()
                        .map(|m| m.get(part).cloned().unwrap_or_else(|| json!("<absent>")))
                        .collect(),
                ),
                other => other
                    .get(part)
                    .cloned()
                    .unwrap_or_else(|| json!("<absent>")),
            };
        }
        out.insert(path.to_owned(), current);
    }
    Value::Object(out)
}

#[test]
fn stop_start_and_a_full_restart_equal_the_executed_oracle() {
    let _serial = one_at_a_time();
    build();
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/oracle_start_stop.json")).expect("fixture");
    let cases = fixture["cases"].as_array().expect("cases");
    assert_eq!(cases.len(), 22, "every recorded case is replayed");
    let base = fs::canonicalize(std::env::temp_dir())
        .expect("tmp")
        .join(format!("l10r16-startstop-{}", std::process::id()));
    fs::remove_dir_all(&base).unwrap_or_default();
    let mut app = Some(Instance::launch(&base.join("owner"), &[]));
    let proxy = free_port();
    for case in cases {
        let label = case["label"].as_str().expect("label");
        if label == "restart_status" {
            // the probe's full restart over the same HOME
            app = app.take().map(Instance::restart);
        }
        let running = app.as_ref().expect("instance");
        let path = case["path"].as_str().expect("path");
        let mut request = case["request"].clone();
        if request.is_null() {
            request = json!({});
        }
        if path == "/api/network/start" {
            request["proxy_port"] = json!(proxy);
        }
        let (status, got) = if case["method"] == "GET" {
            running.get(path)
        } else {
            running.post(path, &request)
        };
        assert_eq!(
            status,
            u16::try_from(case["status"].as_u64().expect("status")).expect("u16"),
            "{label}: {got}"
        );
        match case["kind"].as_str().expect("kind") {
            "status" => {
                assert_eq!(pick(&got), case["pick"], "{label}: status values");
                assert_eq!(
                    tree(&got["authority"]),
                    case["tree"]["authority"],
                    "{label}: authority keys"
                );
            }
            _ if path == "/api/network/start" => {
                for key in [
                    "state",
                    "provider",
                    "public_origin",
                    "seed_bound",
                    "relay_host_trust",
                ] {
                    assert_eq!(got[key], case["body"][key], "{label}: {key}");
                }
            }
            _ if path == "/api/pair/window/open" => {
                assert_eq!(tree(&got), tree(&case["body"]), "{label}: keys");
                assert_eq!(got["state"], case["body"]["state"], "{label}");
                assert_eq!(got["tunnel_name"], case["body"]["tunnel_name"], "{label}");
            }
            _ => assert_eq!(got, case["body"], "{label}"),
        }
    }
    drop(app);
    fs::remove_dir_all(&base).unwrap_or_default();
}

// ---- FX-L10-LOCAL-CONTRIBUTOR
/// `[judge-mac-<hex>]` is the certified owner's member id and `remote-mac-<24 hex>` a Mac that joins by key:
/// ids are random per process, so both sides are compared with the ids replaced by their order of appearance.
/// The values this fixture's probe recorded for a status document.
fn pick_contributor(status: &Value) -> Value {
    let paths = [
        "running",
        "authority.started",
        "authority.swarm_created",
        "authority.membership_state",
        "authority.member_count",
        "authority.members.node_id",
        "authority.members.lifecycle_state",
        "authority.members.generation",
        "network.state",
        "owner_run.state",
    ];
    let mut out = serde_json::Map::new();
    for path in paths {
        let mut current = status.clone();
        for part in path.split('.') {
            current = match current {
                Value::Array(items) => Value::Array(
                    items
                        .iter()
                        .map(|m| m.get(part).cloned().unwrap_or_else(|| json!("<absent>")))
                        .collect(),
                ),
                other => other
                    .get(part)
                    .cloned()
                    .unwrap_or_else(|| json!("<absent>")),
            };
        }
        out.insert(path.to_owned(), current);
    }
    Value::Object(out)
}
struct Scrub {
    seen: Vec<String>,
    owner: Option<String>,
}
impl Scrub {
    fn text(&mut self, text: &str) -> String {
        let mut out = String::new();
        let mut rest = text;
        loop {
            let next = ["remote-mac-", "judge-mac-"]
                .iter()
                .filter_map(|p| rest.find(p).map(|i| (i, *p)))
                .min();
            let Some((at, prefix)) = next else {
                out.push_str(rest);
                return out;
            };
            out.push_str(&rest[..at]);
            let tail = &rest[at + prefix.len()..];
            let len = tail.bytes().take_while(u8::is_ascii_hexdigit).count();
            let id = format!("{prefix}{}", &tail[..len]);
            let token = if prefix == "judge-mac-" || self.owner.as_deref() == Some(id.as_str()) {
                "@OWNER".to_owned()
            } else {
                let at = self.seen.iter().position(|s| *s == id).unwrap_or_else(|| {
                    self.seen.push(id.clone());
                    self.seen.len() - 1
                });
                format!("@NODE{}", at + 1)
            };
            out.push_str(&token);
            rest = &tail[len..];
        }
    }
    fn value(&mut self, value: &Value) -> Value {
        serde_json::from_str(&self.text(&value.to_string())).expect("scrubbed json")
    }
    fn projection(&mut self, value: &Value) -> Value {
        // Both coordinators sort by the REAL node id. The oracle's judge-mac owner always sorts
        // before remote-mac, whereas Rust's key-derived owner can sort after its contributor.
        // Check that wire ordering, then move the whole row together when normalising random ids.
        let Some(ids) = value["authority.members.node_id"].as_array() else {
            return self.value(value);
        };
        // The recorder already scrubbed remote ids to @NODE1 in the oracle fixture. Only the live
        // Rust projection still has raw ids (and an explicitly discovered owner identity).
        if self.owner.is_some() {
            assert!(
                ids.windows(2).all(|w| w[0].as_str() <= w[1].as_str()),
                "wire member order"
            );
        }
        let mut value = self.value(value);
        let ids = value["authority.members.node_id"]
            .as_array()
            .expect("normalised ids");
        let mut order: Vec<usize> = (0..ids.len()).collect();
        order.sort_by_key(|i| ids[*i].as_str().expect("node id"));
        for key in [
            "authority.members.node_id",
            "authority.members.lifecycle_state",
            "authority.members.generation",
        ] {
            let rows = value[key].as_array().expect("member column");
            assert_eq!(rows.len(), order.len(), "aligned member columns");
            value[key] = Value::Array(order.iter().map(|i| rows[*i].clone()).collect());
        }
        value
    }
}

/// Proves: B-RUN-08 (the owner's Mac is one logical contributor on ONE physical host: `physical_hosts 1`, answers
/// and refusals of `/api/local-contributor/start|stop`, identity retained across stop/start and restart, taken down
/// and brought back with `/api/stop` + `/api/start`), B-ROLL-01 (the contributor is a second member of the swarm,
/// `NEW` until the renewal loop, executed `status_after_start`). The persisted file NAMES of the contributor's
/// directory are the Rust target layout (as for every Rust member), not v0.1.4's `identity/node.key`: named
/// difference, compared here are its existence, its 0700 mode and that the identity survives.
#[test]
fn the_local_contributor_routes_equal_the_executed_oracle() {
    let _serial = one_at_a_time();
    build();
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/oracle_local_contributor.json"))
            .expect("fixture");
    let cases = fixture["cases"].as_array().expect("cases");
    assert_eq!(cases.len(), 36, "every recorded case is replayed");
    let base = fs::canonicalize(std::env::temp_dir())
        .expect("tmp")
        .join(format!("l10r20-localcontrib-{}", std::process::id()));
    fs::remove_dir_all(&base).unwrap_or_default();
    let mut app = Some(Instance::launch(&base.join("owner"), &[]));
    let proxy = free_port();
    let mut scrub = Scrub {
        seen: Vec::new(),
        owner: None,
    };
    let mut oracle = Scrub {
        seen: Vec::new(),
        owner: None,
    };
    for case in cases {
        let label = case["label"].as_str().expect("label");
        if label == "restart_status" {
            app = app.take().map(Instance::restart);
        }
        let running = app.as_ref().expect("instance");
        let path = case["path"].as_str().expect("path");
        let mut request = case["request"].clone();
        if request.is_null() {
            request = json!({});
        }
        if path == "/api/network/start" {
            request["proxy_port"] = json!(proxy);
        }
        let (status, got) = if case["method"] == "GET" {
            running.get(path)
        } else {
            running.post(path, &request)
        };
        assert_eq!(
            status,
            u16::try_from(case["status"].as_u64().expect("status")).expect("u16"),
            "{label}: {got}"
        );
        if scrub.owner.is_none() && got["authority"]["members"].is_array() {
            scrub.owner = got["authority"]["members"][0]["node_id"]
                .as_str()
                .map(str::to_owned);
        }
        match case["kind"].as_str().expect("kind") {
            "status" => {
                assert_eq!(
                    scrub.projection(&pick_contributor(&got)),
                    oracle.projection(&case["pick"]),
                    "{label}: status values"
                );
            }
            _ if path == "/api/network/start" && status == 200 => {
                for key in ["state", "provider", "public_origin", "seed_bound"] {
                    assert_eq!(got[key], case["body"][key], "{label}: {key}");
                }
            }
            // `/api/start` answers the status document: its values are compared like a status.
            _ if path == "/api/start" => {
                assert_eq!(
                    scrub.projection(&pick_contributor(&got)),
                    oracle.projection(&pick_contributor(&case["body"])),
                    "{label}: status values"
                );
            }
            _ => assert_eq!(scrub.value(&got), oracle.value(&case["body"]), "{label}"),
        }
        if !case["files"].is_null() && status == 200 {
            let dir = base.join("owner").join("local-contributor");
            let meta = fs::metadata(&dir).unwrap_or_else(|e| panic!("{label}: {dir:?}: {e}"));
            assert_eq!(
                meta.permissions().mode() & 0o777,
                0o700,
                "{label}: directory mode"
            );
            assert!(
                fs::read_dir(&dir).expect("dir").next().is_some(),
                "{label}: the contributor keeps its identity and membership in its directory"
            );
        }
    }
    drop(app);
    fs::remove_dir_all(&base).unwrap_or_default();
}

/// Every `lease_expires_at` in the seed's committed member table (`rs_seed_table.snapshot`).
fn leases(database: &Path) -> Vec<f64> {
    fn collect(value: &Value, out: &mut Vec<f64>) {
        match value {
            Value::Object(map) => {
                for (key, inner) in map {
                    match (key.as_str(), inner.as_f64()) {
                        ("lease_expires_at", Some(at)) => out.push(at),
                        _ => collect(inner, out),
                    }
                }
            }
            Value::Array(items) => items.iter().for_each(|i| collect(i, out)),
            _ => {}
        }
    }
    let connection =
        rusqlite::Connection::open_with_flags(database, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .expect("seed database");
    let snapshot: String = connection
        .query_row("SELECT snapshot FROM rs_seed_table WHERE id = 1", [], |r| {
            r.get(0)
        })
        .expect("snapshot");
    let mut out = Vec::new();
    collect(
        &serde_json::from_str(&snapshot).expect("snapshot json"),
        &mut out,
    );
    out
}

/// `DemoApp._renew_loop` (app.py:911-927 @ f4e366e9): every 20 s (`RenewLoopSelfClosedWait`, L917) a
/// running owner renews its own lease through its seed; without it the owner's lease lapses after the
/// coordinator's 300 s while the Mac is perfectly alive.
#[test]
fn the_owner_renews_its_own_lease_in_the_background() {
    let _serial = one_at_a_time();
    build();
    let base = fs::canonicalize(std::env::temp_dir())
        .expect("tmp")
        .join(format!("l10r16-renew-{}", std::process::id()));
    fs::remove_dir_all(&base).unwrap_or_default();
    let owner = Instance::launch(&base.join("owner"), &[]);
    let (status, created) = owner.post("/api/create", &json!({"consent": consent(STORAGE)}));
    assert_eq!(status, 200, "{created}");
    let database = base.join("owner/seed-rs/seed.sqlite");
    let first = leases(&database).into_iter().fold(0.0, f64::max);
    assert!(first > 0.0, "the owner's member row has a lease");
    let period = budget(BudgetId::PyMyceliumA13MacosAppDemoappRenewLoopSelfClosedWaitL917);
    let deadline = Instant::now() + period * 3;
    loop {
        let later = leases(&database).into_iter().fold(0.0, f64::max);
        if later > first {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "no renewal within three loop periods: {first} -> {later}"
        );
        std::thread::sleep(poll_every());
    }
    drop(owner);
    fs::remove_dir_all(&base).unwrap_or_default();
}

/// Every `(node_id, lease_expires_at)` row of the seed's committed member table.
fn lease_rows(database: &Path) -> Vec<(String, f64)> {
    fn collect(value: &Value, out: &mut Vec<(String, f64)>) {
        match value {
            Value::Object(map) => {
                if let (Some(node), Some(at)) = (
                    map.get("node_id").and_then(Value::as_str),
                    map.get("lease_expires_at").and_then(Value::as_f64),
                ) {
                    out.push((node.to_owned(), at));
                }
                map.values().for_each(|inner| collect(inner, out));
            }
            Value::Array(items) => items.iter().for_each(|i| collect(i, out)),
            _ => {}
        }
    }
    let connection =
        rusqlite::Connection::open_with_flags(database, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .expect("seed database");
    let snapshot: String = connection
        .query_row("SELECT snapshot FROM rs_seed_table WHERE id = 1", [], |r| {
            r.get(0)
        })
        .expect("snapshot");
    let mut out = Vec::new();
    collect(
        &serde_json::from_str(&snapshot).expect("snapshot json"),
        &mut out,
    );
    out.sort_by(|a, b| a.0.cmp(&b.0));
    out.dedup_by(|a, b| a.0 == b.0);
    out
}

/// `DemoApp._renew_loop` (app.py:911-927 @ f4e366e9) runs for a joined TARGET too: `authority.renew()` is
/// `RemoteMacMember.renew()`, a forced RUNNING heartbeat to the owner's seed every 20 s. Without it a member
/// that joined for a two-Mac window loses its lease after the coordinator's 300 s while perfectly alive.
/// Both members' rows must advance (the owner's own loop is `the_owner_renews_its_own_lease...`).
#[test]
fn a_joined_member_renews_its_own_lease_in_the_background() {
    let _serial = one_at_a_time();
    build();
    let base = fs::canonicalize(std::env::temp_dir())
        .expect("tmp")
        .join(format!("l10r17-member-renew-{}", std::process::id()));
    fs::remove_dir_all(&base).unwrap_or_default();
    let (owner, member, _proxy) = pair_up(&base);
    let database = base.join("owner/seed-rs/seed.sqlite");
    let first = lease_rows(&database);
    assert_eq!(first.len(), 2, "owner and member rows: {first:?}");
    let period = budget(BudgetId::PyMyceliumA13MacosAppDemoappRenewLoopSelfClosedWaitL917);
    let deadline = Instant::now() + period * 3;
    loop {
        let later = lease_rows(&database);
        let advanced = first
            .iter()
            .all(|(node, at)| later.iter().any(|(n, l)| n == node && l > at));
        if advanced {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "not every member renewed within three loop periods: {first:?} -> {later:?}"
        );
        std::thread::sleep(poll_every());
    }
    drop(member);
    drop(owner);
    fs::remove_dir_all(&base).unwrap_or_default();
}

/// `member-leave` (member_leave.py): "left" means this Mac stopped serving and stopped renewing its lease
/// (`_renew_loop` skips a `_left` app). The new renewal loop of a joined target must honour that: after a
/// smooth leave the member's row in the owner's seed stays where it was while the owner's own row moves on.
#[test]
fn a_member_that_has_left_stops_renewing_its_lease() {
    let _serial = one_at_a_time();
    build();
    let base = fs::canonicalize(std::env::temp_dir())
        .expect("tmp")
        .join(format!("l10r17-left-{}", std::process::id()));
    fs::remove_dir_all(&base).unwrap_or_default();
    let (owner, member, _proxy) = pair_up(&base);
    let (status, answer) = member.post("/api/leave", &json!({"mode": "smooth", "confirm": true}));
    assert_eq!(status, 200, "{answer}");
    let left = member.until(
        "the member has left",
        || member.get("/api/status").1,
        |v| v["leave"]["state"] == "left",
    );
    assert_eq!(left["leave"]["mode"], "smooth", "{left}");
    let database = base.join("owner/seed-rs/seed.sqlite");
    let at_leave = lease_rows(&database);
    assert_eq!(at_leave.len(), 2, "{at_leave:?}");
    let period = budget(BudgetId::PyMyceliumA13MacosAppDemoappRenewLoopSelfClosedWaitL917);
    let deadline = Instant::now() + period * 3;
    // the owner's own loop keeps moving its row: that proves the window was long enough for a member pass
    loop {
        let later = lease_rows(&database);
        let moved: Vec<&String> = at_leave
            .iter()
            .filter(|(node, at)| later.iter().any(|(n, l)| n == node && l > at))
            .map(|(node, _)| node)
            .collect();
        assert!(
            moved.len() <= 1,
            "both rows moved after the member left: {at_leave:?} -> {later:?}"
        );
        if moved.len() == 1 && Instant::now() + period > deadline {
            break;
        }
        assert!(
            Instant::now() < deadline + period,
            "the owner's row never moved: {at_leave:?} -> {later:?}"
        );
        std::thread::sleep(poll_every());
    }
    drop(member);
    drop(owner);
    fs::remove_dir_all(&base).unwrap_or_default();
}

/// `owner-restart-persist` + `f3-owner-restart-rereport` as the real window runs them: the member reaches the
/// owner only through the stable public origin (here a TLS front onto the relay's proxy), the owner process is
/// restarted on the same state, restores its relay and starts; the member's renewal loop then has to reach
/// the NEW seed through the same origin and move its lease again (the seed's own port changed).
#[test]
fn a_joined_member_keeps_renewing_through_an_owner_restart_behind_the_edge() {
    let _serial = one_at_a_time();
    build();
    let base = fs::canonicalize(std::env::temp_dir())
        .expect("tmp")
        .join(format!("l10r17-edge-restart-{}", std::process::id()));
    fs::remove_dir_all(&base).unwrap_or_default();
    let (owner, member, proxy, edge) = pair_up_with(&base, true);
    let database = base.join("owner/seed-rs/seed.sqlite");
    let edge = edge.expect("the edge");
    let joined_through_it = edge.served.load(Ordering::SeqCst);
    assert!(
        joined_through_it >= 1,
        "the member's seed join came through the edge"
    );
    let owner = owner.restart();
    let (status, restored) = owner.post(
        "/api/network/start",
        &json!({"relay_acknowledged": true, "max_seconds": 1800, "provider": "external",
                "public_origin": ORIGIN, "proxy_port": proxy}),
    );
    assert_eq!(status, 200, "{restored}");
    assert_eq!(restored["seed_bound"], true, "{restored}");
    let (status, started) = owner.post("/api/start", &json!({}));
    assert_eq!(status, 200, "{started}");
    let after_restart = lease_rows(&database);
    assert_eq!(after_restart.len(), 2, "{after_restart:?}");
    let period = budget(BudgetId::PyMyceliumA13MacosAppDemoappRenewLoopSelfClosedWaitL917);
    let deadline = Instant::now() + period * 3;
    loop {
        let later = lease_rows(&database);
        let advanced = after_restart
            .iter()
            .all(|(node, at)| later.iter().any(|(n, l)| n == node && l > at));
        if advanced {
            break;
        }
        assert!(
            Instant::now() < deadline,
            "a member did not renew through the restarted owner: {after_restart:?} -> {later:?}\nmember stderr: {}",
            fs::read_to_string(base.join("member").with_extension("stderr")).unwrap_or_default()
        );
        std::thread::sleep(poll_every());
    }
    assert!(
        edge.served.load(Ordering::SeqCst) > joined_through_it,
        "the renewal reached the restarted seed through the edge"
    );
    drop(member);
    drop(owner);
    drop(edge);
    fs::remove_dir_all(&base).unwrap_or_default();
}

/// The unchanged L3 baseline restarts an owner that created its swarm with NO relay (the seed answers on
/// its own loopback address): the restarted launcher must come back, as the oracle does.
#[test]
fn an_owner_without_a_relay_restarts_with_its_swarm() {
    let _serial = one_at_a_time();
    build();
    let base = fs::canonicalize(std::env::temp_dir())
        .expect("tmp")
        .join(format!("l10r16-norelay-{}", std::process::id()));
    fs::remove_dir_all(&base).unwrap_or_default();
    let owner = Instance::launch(&base.join("owner"), &[]);
    let (status, created) = owner.post("/api/create", &json!({"consent": consent(STORAGE)}));
    assert_eq!(status, 200, "{created}");
    let owner = owner.restart();
    let kept = owner.until(
        "the restarted owner keeps its swarm",
        || owner.get("/api/status").1,
        |v| v["authority"]["membership_state"] == "current",
    );
    assert_eq!(kept["authority"]["member_count"], 1, "{kept}");
    assert_eq!(kept["authority"]["membership_generation"], 2, "{kept}");
    drop(owner);
    fs::remove_dir_all(&base).unwrap_or_default();
}
