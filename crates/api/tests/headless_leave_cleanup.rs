// SPDX-License-Identifier: AGPL-3.0-only
//! Real headless stop/leave cleanup. Setup follows support/headless_observation.rs.
use budgets::{BudgetId, Entries};
use rustix::process::{Pid, Signal, kill_process, test_kill_process};
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpListener, TcpStream},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};

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
    let measured = Instant::now();
    let target = exe_dir();
    let target = target.parent().expect("target");
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../Cargo.toml");
    let mut command = Command::new(std::env::var("CARGO").unwrap_or_else(|_| "cargo".into()));
    command.args(["build", "--offline", "--locked", "-p", "api", "--release"]);
    command.args(["--example", "headless"]);
    command.arg("--manifest-path").arg(manifest);
    command.arg("--target-dir").arg(target);
    let output = command.output().expect("cargo is runnable");
    eprintln!("leave-profile build {:?}", measured.elapsed());
    assert!(
        output.status.success(),
        "nested build failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
/// Each test owns real launcher processes and durable membership; serialize
/// within this binary so their setup and cleanup do not contend with each other.
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

fn startup_line(
    mut reader: impl Read + std::os::fd::AsFd,
    limit: Duration,
) -> std::io::Result<String> {
    // Nonblocking pipe reads enforce the deadline even for a partial line; no
    // detached reader thread can outlive a timed-out launcher's cleanup guard.
    rustix::io::ioctl_fionbio(&reader, true)?;
    let deadline = Instant::now() + limit;
    let mut first = Vec::new();
    loop {
        if Instant::now() >= deadline {
            return Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "launcher_startup_timeout",
            ));
        }
        let mut byte = [0];
        match reader.read(&mut byte) {
            Ok(0) => break,
            Ok(_) => {
                first.push(byte[0]);
                if byte[0] == b'\n' {
                    break;
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(
                    poll_every().min(deadline.saturating_duration_since(Instant::now())),
                );
            }
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        }
    }
    String::from_utf8(first)
        .map_err(|error| std::io::Error::new(std::io::ErrorKind::InvalidData, error))
}

#[test]
fn startup_observation_has_a_local_deadline() {
    let (read, write) = std::os::unix::net::UnixStream::pair().unwrap();
    let (settled, done) = std::sync::mpsc::sync_channel(1);
    let producer = std::thread::spawn(move || {
        // The producer holds an incomplete line until the observer settles, or
        // the independent budget guard fires on the unfixed blocking observer.
        match done.recv_timeout(budget(BudgetId::WorkerStartup)) {
            Ok(()) | Err(std::sync::mpsc::RecvTimeoutError::Timeout) => drop(write),
            Err(error) => panic!("startup observer disconnected: {error}"),
        }
    });
    let result = startup_line(read, budget(BudgetId::OwnerPoll));
    let signalled = settled.send(());
    producer.join().unwrap();
    assert!(
        matches!(result, Err(ref e) if e.kind() == std::io::ErrorKind::TimedOut),
        "missing bounded startup observation: {result:?}; signal={signalled:?}"
    );
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
        Self::launch_observing(root, env, |child| {
            startup_line(
                child.stdout.take().expect("stdout"),
                budget(BudgetId::WorkerStartup),
            )
        })
    }
    fn launch_observing(
        root: &Path,
        env: &[(&str, String)],
        observe: impl FnOnce(&mut Child) -> std::io::Result<String>,
    ) -> Self {
        let measured = Instant::now();
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
        let child = command.spawn().expect("the headless example was built");
        // Own the process before observing stdout, parsing the form or signing in.
        let mut me = Self {
            child,
            root: root.to_path_buf(),
            port: 0,
            cookie: String::new(),
            env: env
                .iter()
                .map(|(k, v)| ((*k).to_owned(), v.clone()))
                .collect(),
            keep_state: false,
        };
        let first = observe(&mut me.child).expect("bounded first line");
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
        me.port = port;
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
        eprintln!("leave-profile launch {:?}", measured.elapsed());
        me
    }
    fn raw(
        &self,
        method: &str,
        path: &str,
        body: &[u8],
        headers: &[(&str, &str)],
    ) -> (u16, String, Vec<u8>) {
        let measured = Instant::now();
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
        eprintln!(
            "leave-profile http {method} {path} {:?}",
            measured.elapsed()
        );
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
        fetch: impl FnMut() -> Value,
        done: impl Fn(&Value) -> bool,
    ) -> Value {
        self.until_with(what, patience(), fetch, done)
    }
    fn until_with(
        &self,
        what: &str,
        allowance: Duration,
        mut fetch: impl FnMut() -> Value,
        done: impl Fn(&Value) -> bool,
    ) -> Value {
        let deadline = Instant::now() + allowance;
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
        let measured = Instant::now();
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
        eprintln!("leave-profile reap {:?}", measured.elapsed());
        if !self.keep_state {
            fs::remove_dir_all(&self.root).unwrap_or_default();
        }
    }
}

#[test]
fn startup_observation_timeout_reaps_owned_launcher() {
    let _serial = one_at_a_time();
    build();
    let root = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("lane-a-startup-timeout-{}", std::process::id()));
    let mut pid = None;
    let failure = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        Instance::launch_observing(&root, &[], |child| {
            pid = Pid::from_raw(child.id().try_into().unwrap());
            let mut stdout = child.stdout.take().unwrap();
            let first = startup_line(&mut stdout, budget(BudgetId::WorkerStartup))?;
            assert!(first.contains("launcher_started"));
            // A real live launcher, with the startup notification withheld from
            // the harness. It emits no second line: only the local deadline ends it.
            startup_line(stdout, budget(BudgetId::OwnerPoll))
        })
    }));
    assert!(failure.is_err(), "startup silence must fail admission");
    assert_eq!(
        test_kill_process(pid.unwrap()),
        Err(rustix::io::Errno::SRCH)
    );
    assert!(!root.exists(), "guard cleans up even before sign-in");
}

fn consent(storage: u64) -> Value {
    json!({
        "peer_visibility": true, "power": "ac_only", "storage_bytes": storage,
        "network": "unmetered_only", "background": false, "diagnostics": false,
        "thermal": "stop_on_serious",
    })
}

fn pair_up_with_owner_env(base: &Path, env: &[(&str, String)]) -> (Instance, Instance, u16) {
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

    let seed_address = format!("127.0.0.1:{}", seed_ports[0]);
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
    (owner, member, proxy)
}

/// Exercise the actual headless composition root, not a separately mounted test server. The signer is
/// this isolated launcher's real joined identity; this proves signed exchange, NOT a native session.
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

use api::installation_descriptor::{self, Member, Paths, Source};
struct Repository {
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}
impl Drop for Repository {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.take() {
            thread.join().expect("owned repository joined");
        }
    }
}
fn mounted_cleanup() -> (PathBuf, Instance, Instance, Repository) {
    mounted_cleanup_with_env(&[])
}
fn mounted_cleanup_with_env(extra: &[(&str, String)]) -> (PathBuf, Instance, Instance, Repository) {
    build();
    let base = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("lane-a-cleanup-{}", std::process::id()));
    assert!(!base.exists(), "fresh fixture state");
    fs::create_dir(&base).unwrap();
    fs::set_permissions(&base, fs::Permissions::from_mode(0o700)).unwrap();
    let oracle: Value =
        serde_json::from_str(include_str!("fixtures/oracle_resources.json")).unwrap();
    let observation = &oracle["policy_cases"][0]["observation"];
    let probe = base.join("probe.sh");
    fs::write(&probe, format!("#!/bin/sh\nprintf '%s' '{observation}'\n")).unwrap();
    fs::set_permissions(&probe, fs::Permissions::from_mode(0o700)).unwrap();
    let (files, _) = tiny_repository::tiny_snapshot();
    let (port, stop, thread) = tiny_repository::serve_repository(files);
    let repository = Repository {
        stop,
        thread: Some(thread),
    };
    let env = [
        (
            "MYCELIUM_L10_TEST_RESOURCE_PROBE",
            probe.display().to_string(),
        ),
        (
            "MYCELIUM_L10_TEST_HUB_ORIGIN",
            format!("http://127.0.0.1:{port}"),
        ),
        // Existing file-admission seam only; never executed as a native worker.
        (
            "MYCELIUM_L10_TEST_SIDECAR",
            exe_dir().join("examples/headless").display().to_string(),
        ),
    ];
    let mut env = env.to_vec();
    env.extend_from_slice(extra);
    let (owner, member, proxy) = pair_up_with_owner_env(&base, &env);
    {
        let config = std::env::var("MYCELIUM_BOOTSTRAP_TEST_CONFIG")
            .expect("explicit artifact qualification config");
        let admission: Value = serde_json::from_slice(&fs::read(config).unwrap()).unwrap();
        // The parent's receipt carries admission metadata; the production
        // program configuration has exactly these four fields.
        let document = json!({
            "protocol":"mycelium.bootstrap_program.v1",
            "path":admission["path"],
            "sha256":admission["sha256"],
            "source_sha":admission["source_sha"],
        });
        api::preferences::private_state::store(
            &owner.root.join("bootstrap-program.json"),
            &document,
            api::preferences::private_state::private_json_limit().unwrap(),
        )
        .unwrap();
    }
    let grant = json!({"consent":{"download_acknowledged":true,"max_download_bytes":STORAGE,"storage_limit_bytes":4_000_000_000u64}});
    // Retain the existing bounded power-policy retry; the prior failure did not establish its cause.
    let mut retries = 0;
    let (code, answer) = loop {
        let response = owner.post("/api/model/download", &grant);
        if response.0 == 200
            || retries >= 5
            || response.1["error"] != "resource_power_policy_violation"
        {
            break response;
        }
        retries += 1;
        std::thread::sleep(budget(BudgetId::OwnerPoll) * 8);
    };
    assert_eq!(code, 200, "{answer}");
    let prepared = owner.until(
        "actual public acquisition prepared",
        || owner.get("/api/status").1,
        |s| {
            matches!(
                s["acquisition"]["state"].as_str(),
                Some("prepared" | "failed")
            )
        },
    );
    assert_eq!(prepared["acquisition"]["state"], "prepared", "{prepared}");
    let (code, started) = owner.post("/api/demo/start", &json!({}));
    assert_eq!(code, 200, "{started}");
    assert_eq!(started["state"], "running");
    owner.until(
        "actual descriptors stage",
        || owner.get("/api/status").1,
        |s| s["owner_run"]["step"] == "descriptors",
    );
    let report = |signer: &protocol::identity::Identity,
                  current: &Value,
                  swarm: &Value,
                  announcement: &Value,
                  at: f64| {
        assert_eq!(announcement["phase"], "collect");
        let run = announcement["run_id"].as_str().unwrap();
        let deployment = announcement["deployment_id"].as_str().unwrap();
        let binding = Member {
            node_id: current["node_id"].as_str().unwrap().into(),
            endpoint_id: signer.endpoint_id().into(),
            verification_key_digest: signer.verification_key_digest(),
            generation: current["generation"].as_u64().unwrap(),
            incarnation: current["incarnation"].as_str().unwrap().into(),
        };
        let card = installation_descriptor::installation_descriptor(
            signer,
            &binding,
            swarm["swarm_id"].as_str().unwrap(),
            run,
            deployment,
            true,
            &Source::Whole {
                source_root: base.join("CONTROLLED-member-source").display().to_string(),
                manifest_digest: announcement["manifest_digest"].as_str().unwrap().into(),
            },
            &Paths {
                state_root: member.root.display().to_string(),
                endpoint_secret_file: member
                    .root
                    .join("target-identity.json")
                    .display()
                    .to_string(),
                python_executable: exe_dir().join("examples/headless").display().to_string(),
                sidecar_binary: exe_dir().join("examples/headless").display().to_string(),
            },
            at,
        )
        .unwrap();
        json!({"protocol":api::run_exchange::MEMBER_REPORT,"run_id":run,"deployment_id":deployment,"state":"reported","detail":null,
            "model":{"model_id":announcement["model"]["model_id"],"resolved_commit":announcement["model"]["resolved_commit"]},"progress":null,"descriptor":card,"issued_at":at})
    };
    signed_headless_exchange_report(&owner, &member, proxy, Some(&report));
    (base, owner, member, repository)
}

#[test]
#[ignore = "requires explicitly pinned admitted model-free Rust helper artifact"]
fn contributor_stop_fences_admitted_bootstrap_before_stage() {
    let _serial = one_at_a_time();
    let barrier = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
    barrier.set_nonblocking(true).unwrap();
    let (base, owner, member, repository) = mounted_cleanup_with_env(&[(
        "MYCELIUM_L10_TEST_BOOTSTRAP_BEFORE_STAGE",
        barrier.local_addr().unwrap().to_string(),
    )]);
    let deadline = Instant::now() + budget(BudgetId::WorkerStartup);
    let mut held = loop {
        match barrier.accept() {
            Ok((stream, _)) => break stream,
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(
                    Instant::now() < deadline,
                    "bootstrap did not reach held seam"
                );
                std::thread::sleep(poll_every());
            }
            Err(error) => panic!("bootstrap barrier: {error}"),
        }
    };
    held.set_read_timeout(Some(budget(BudgetId::WorkerStartup)))
        .unwrap();
    held.set_write_timeout(Some(budget(BudgetId::WorkerStartup)))
        .unwrap();
    let mut entered = [0; 4];
    held.read_exact(&mut entered).unwrap();
    assert_eq!(&entered, b"held");
    let native = owner.root.join("local-contributor/native-bootstrap");
    assert!(
        !native.exists(),
        "held before any stage/state/spawn side effect"
    );
    let stopped = owner.post("/api/local-contributor/stop", &json!({}));
    assert_eq!(stopped.0, 200, "{stopped:?}");
    assert_eq!(stopped.1["state"], "stopped");
    held.write_all(&[1]).unwrap();
    held.set_nonblocking(true).unwrap();
    let deadline = Instant::now() + budget(BudgetId::WorkerStartup);
    loop {
        assert!(
            !native.exists(),
            "bootstrap created stage/state after successful Stop"
        );
        match held.read(&mut [0]) {
            Ok(0) => break,
            Ok(_) => panic!("unexpected startup seam bytes"),
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                assert!(
                    Instant::now() < deadline,
                    "startup must settle, not merely pause"
                );
                std::thread::sleep(poll_every());
            }
            Err(error) => panic!("startup settlement: {error}"),
        }
    }
    // Creation leaves the native root/state behind even if the worker later closes.
    // Its absence at settled EOF rules out a transient post-Stop stage or spawn.
    assert!(
        !native.exists(),
        "bootstrap created stage/state after successful Stop"
    );
    let helpers = Command::new("/usr/bin/pgrep")
        .args([
            "-P",
            &owner.child.id().to_string(),
            "-f",
            "mycelium-hello-bootstrap",
        ])
        .output()
        .unwrap();
    assert_eq!(
        helpers.status.code(),
        Some(1),
        "post-Stop helper PID: {:?}",
        helpers.stdout
    );
    assert_eq!(owner.post("/api/local-contributor/stop", &json!({})).0, 200);
    drop(member);
    drop(owner);
    drop(repository);
    fs::remove_dir_all(base).unwrap();
}

struct StagePermissions(PathBuf);
impl Drop for StagePermissions {
    fn drop(&mut self) {
        if self.0.exists() {
            fs::set_permissions(&self.0, fs::Permissions::from_mode(0o700)).expect("restore stage");
        }
    }
}
#[test]
#[ignore = "requires explicitly pinned admitted model-free Rust helper artifact"]
fn contributor_stop_retains_cleanup_debt_until_real_helper_reap() {
    let _serial = one_at_a_time();
    let (base, owner, member, repository) = mounted_cleanup();
    let path = owner
        .root
        .join("local-contributor/native-bootstrap/state.json");
    let observed = owner.until(
        "real helper running",
        || {
            let native = fs::read(&path)
                .ok()
                .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
                .unwrap_or(Value::Null);
            json!({"native":native,"status":owner.get("/api/status").1})
        },
        |s| s["native"]["phase"] == "running" || s["status"]["owner_run"]["state"] == "failed",
    );
    assert_eq!(observed["native"]["phase"], "running", "{observed}");
    let state = &observed["native"];
    let pid = Pid::from_raw(
        state["process"]["pid"]
            .as_u64()
            .unwrap()
            .try_into()
            .unwrap(),
    )
    .unwrap();
    assert!(test_kill_process(pid).is_ok());
    let stage = PathBuf::from(state["stage_root"].as_str().unwrap());
    let permissions = StagePermissions(stage.clone());
    fs::set_permissions(&stage, fs::Permissions::from_mode(0o500)).unwrap();
    let stopped = owner.post("/api/local-contributor/stop", &json!({}));
    let debt: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    assert_eq!(
        stopped.0, 409,
        "cleanup must refuse rather than erase contributor: {stopped:?}"
    );
    let again = owner.post("/api/local-contributor/stop", &json!({}));
    assert_eq!(
        again.0, 409,
        "debt must survive another failed retry: {again:?}"
    );
    assert_eq!(again.1["error"], "bootstrap_stage_cleanup_unproven");
    let replacement = owner.post("/api/local-contributor/start", &json!({}));
    assert_eq!(
        replacement.0, 400,
        "retained contributor cannot be replaced: {replacement:?}"
    );
    assert_eq!(replacement.1["error"], "local_contributor_already_running");
    drop(permissions);
    assert_eq!(stopped.1["error"], "bootstrap_stage_cleanup_unproven");
    assert_eq!(debt["phase"], "cleanup_unproven", "{debt}");
    assert!(stage.exists(), "real staged bytes still owed");
    assert_eq!(test_kill_process(pid), Err(rustix::io::Errno::SRCH));
    let retried = owner.post("/api/local-contributor/stop", &json!({}));
    assert_eq!(retried.0, 200, "{retried:?}");
    assert_eq!(retried.1["state"], "stopped");
    let closed: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    assert_eq!(closed["phase"], "closed");
    assert!(!stage.exists());
    assert_eq!(test_kill_process(pid), Err(rustix::io::Errno::SRCH));
    assert_eq!(owner.post("/api/local-contributor/stop", &json!({})).0, 200);
    drop(member);
    drop(owner);
    drop(repository);
    fs::remove_dir_all(base).unwrap();
}

#[test]
#[ignore = "requires explicitly pinned admitted model-free Rust helper artifact"]
fn contributor_restart_keeps_unreconciled_stage_debt_visible() {
    let _serial = one_at_a_time();
    let (base, owner, member, repository) = mounted_cleanup();
    let path = owner
        .root
        .join("local-contributor/native-bootstrap/state.json");
    let state = owner.until(
        "real helper before restart",
        || {
            fs::read(&path)
                .ok()
                .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
                .unwrap_or(Value::Null)
        },
        |s| s["phase"] == "running",
    );
    let pid = Pid::from_raw(
        state["process"]["pid"]
            .as_u64()
            .unwrap()
            .try_into()
            .unwrap(),
    )
    .unwrap();
    assert!(test_kill_process(pid).is_ok());
    let stage = PathBuf::from(state["stage_root"].as_str().unwrap());
    let permissions = StagePermissions(stage.clone());
    fs::set_permissions(&stage, fs::Permissions::from_mode(0o500)).unwrap();
    assert_eq!(owner.post("/api/local-contributor/stop", &json!({})).0, 409);
    assert_eq!(test_kill_process(pid), Err(rustix::io::Errno::SRCH));
    let owner = owner.restart();
    drop(permissions);
    assert!(stage.exists(), "restart must not erase the cleanup debt");
    let stopped = owner.post("/api/local-contributor/stop", &json!({}));
    assert_eq!(
        stopped.0, 409,
        "lost in-memory owner cannot prove cleanup: {stopped:?}"
    );
    assert_eq!(
        stopped.1["error"],
        "control_restart_requires_reconciliation"
    );
    assert_eq!(test_kill_process(pid), Err(rustix::io::Errno::SRCH));
    assert!(stage.exists());
    let debt = fs::read(&path).unwrap();
    let membership = fs::read(owner.root.join("local-contributor/membership.state.json")).unwrap();
    let started = owner.post("/api/local-contributor/start", &json!({}));
    assert_eq!(
        started.0, 409,
        "restart Start must refuse debt: {started:?}"
    );
    assert_eq!(
        started.1["error"],
        "control_restart_requires_reconciliation"
    );
    assert_eq!(fs::read(&path).unwrap(), debt);
    assert_eq!(
        fs::read(owner.root.join("local-contributor/membership.state.json")).unwrap(),
        membership
    );
    assert!(stage.exists());
    assert_eq!(test_kill_process(pid), Err(rustix::io::Errno::SRCH));
    drop(member);
    drop(owner);
    drop(repository);
    fs::remove_dir_all(base).unwrap();
}

#[test]
fn member_without_owned_compute_leaves_with_legacy_success_and_positive_proof() {
    let _serial = one_at_a_time();
    build();
    for mode in ["instant", "smooth"] {
        let base = fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!("lane-a-empty-leave-{mode}-{}", std::process::id()));
        fs::create_dir(&base).unwrap();
        let (owner, member, _) = pair_up_with_owner_env(&base, &[]);
        // Reopen real durable membership before leaving: missing memory alone is
        // not evidence, but this member has never acquired a worker or stage.
        let member = member.restart();
        let requested = member.post("/api/leave", &json!({"mode":mode,"confirm":true}));
        assert_eq!(requested.0, 200, "{requested:?}");
        let left = member.until_with(
            "empty owned set needs no unknown-work drain",
            budget(BudgetId::WorkerStartup),
            || member.get("/api/status").1["leave"].clone(),
            |s| s["state"] == "left",
        );
        let mut names = vec!["fence"];
        if mode == "smooth" {
            names.extend(["unload", "publish_withdrawal"]);
        }
        names.extend(["withdraw_consent", "disconnect", "cancel_work"]);
        let expected = json!({"state":"left","mode":mode,"fallback":null,
            "handoff":"unavailable","compute_cleanup":{"state":"none_owned","code":null},
            "steps":names.into_iter().map(|name| json!({"name":name,"ok":true,"code":null})).collect::<Vec<_>>()});
        assert_eq!(left, expected);
        journey_tests::boundaries::validate_leave(&left, mode).unwrap();
        let mut unproven = left.clone();
        unproven["compute_cleanup"] =
            json!({"state":"unproven","code":"member_compute_cleanup_unavailable"});
        assert!(journey_tests::boundaries::validate_leave(&unproven, mode).is_err());
        let member = member.restart();
        assert_eq!(member.get("/api/status").1["leave"], expected);
        drop(member);
        drop(owner);
        fs::remove_dir_all(base).unwrap();
    }
}

#[test]
fn membership_leave_reports_unproven_compute_before_and_after_restart() {
    membership_leave("instant");
}
#[test]
fn smooth_membership_leave_has_bounded_unknown_drain_not_compute_proof() {
    membership_leave("smooth");
}
fn membership_leave(mode: &str) {
    let _serial = one_at_a_time();
    build();
    let base = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("lane-a-membership-leave-{}", std::process::id()));
    fs::create_dir(&base).unwrap();
    let (owner, member, _) = pair_up_with_owner_env(&base, &[]);
    // Unlike the empty-set case, this member has real staged bootstrap bytes.
    // Use the production stage constructor, not a fabricated cleanup record.
    let native = member.root.join("local-contributor/native-bootstrap");
    fs::create_dir_all(&native).unwrap();
    let identity = protocol::identity::Identity::from_secret("leave-stage", &[32; 32]).unwrap();
    let scope = json!({"target_node_id":"target","target_endpoint_id":identity.endpoint_id(),
        "target_key_digest":identity.verification_key_digest(),"membership_generation":1,
        "member_incarnation":"incarnation","run_id":"run","deployment_id":"deployment"});
    let archive = runtime::observe::bootstrap_archive("run", "deployment");
    let mut offer = scope.clone();
    offer["archive_bytes"] = json!(archive.len());
    offer["archive_digest"] = json!(protocol::artifact_grant::range_digest(&archive));
    let stage = api::bootstrap_stage::BootstrapStage::create(
        &native.join("stage"),
        &scope,
        &offer,
        &archive,
    )
    .unwrap();
    let requested = member.post("/api/leave", &json!({"mode":mode,"confirm":true}));
    assert_eq!(requested.0, 200, "{requested:?}");
    let left = if mode == "smooth" {
        assert_eq!(requested.1["state"], "draining");
        let result = member.until_with(
            "bounded unknown drain",
            budget(BudgetId::PyMyceliumA13MacosMemberLeaveLeaveSmoothCapSL34)
                + budget(BudgetId::WorkerStartup),
            || member.get("/api/status").1["leave"].clone(),
            |s| s["state"] == "left",
        );
        assert_eq!(result["fallback"], "stall");
        (200, result)
    } else {
        requested
    };
    assert_eq!(left.1["state"], "left");
    assert_eq!(left.1["compute_cleanup"]["state"], "unproven", "{left:?}");
    assert_eq!(
        left.1["compute_cleanup"]["code"],
        "member_compute_cleanup_unavailable"
    );
    let consent = fs::read(member.root.join("standing-consent.json")).unwrap();
    let record: Value =
        serde_json::from_slice(&fs::read(member.root.join("leave-state.json")).unwrap()).unwrap();
    let retried = member.post("/api/leave", &json!({"mode":"instant","confirm":true}));
    assert_eq!(retried.0, 200);
    assert_eq!(
        retried.1["steps"], left.1["steps"],
        "retry updates debt, never duplicates successful steps"
    );
    drop(stage);
    assert!(!native.join("stage").exists());
    let member = member.restart();
    // Even with the stage gone and memory lost, retained leave debt forbids
    // relabelling the cleanup as none_owned.
    assert_eq!(
        fs::read(member.root.join("standing-consent.json")).unwrap(),
        consent
    );
    let resumed: Value =
        serde_json::from_slice(&fs::read(member.root.join("leave-state.json")).unwrap()).unwrap();
    assert_eq!(
        resumed["started_at"], record["started_at"],
        "same leave obligation across retry/restart"
    );
    let status = member.get("/api/status").1;
    assert_eq!(
        status["leave"]["compute_cleanup"]["state"], "unproven",
        "{status}"
    );
    drop(member);
    drop(owner);
    fs::remove_dir_all(base).unwrap();
}
