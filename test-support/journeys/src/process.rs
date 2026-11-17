// SPDX-License-Identifier: AGPL-3.0-only
//! macOS external-process containment. A missing sandbox is BLOCKED, never a skip.
use crate::{Result, duration, http::Client, io, require};
use budgets::BudgetId;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs::{self, File},
    io::{BufRead, BufReader, Read},
    os::unix::{
        fs::{DirBuilderExt, MetadataExt, PermissionsExt},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicUsize, Ordering},
        mpsc,
    },
    thread::{self, JoinHandle},
    time::Instant,
};

pub const ORACLE_SOURCE: &str = "f4e366e98a1f705dfaf182897044500d290dbd7f";
pub const ORACLE_EXE: &str = "8fff41823d357949ffa55c00b3fb8dfb0b4cc541f66343a72f21e3d8dd89186f";
/// v0.1.4's built-in run model (run_model.py:69-75 @ f4e366e9 `demo_default_choice`).
pub const ORACLE_STARTER: &str = "a9-route";
#[derive(Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Target {
    pub implementation: String,
    pub source_sha: String,
    pub executable: PathBuf,
    pub executable_sha256: String,
}
fn lower_hex(raw: &str, length: usize) -> bool {
    raw.len() == length
        && raw
            .bytes()
            .all(|v| v.is_ascii_digit() || (b'a'..=b'f').contains(&v))
}
impl Target {
    /// Where the target keeps its state under the scratch HOME when it is launched without
    /// `--state-root`: v0.1.4's own folder for the certified Python oracle, v0.2's own
    /// `org.mycelium.app/state` for the Rust launcher (rulings 20/25; it never uses v0.1.4's).
    pub fn state_folder(&self) -> &'static str {
        if self.implementation == "python-v0.1.4" {
            "Library/Application Support/Mycelium A13 macOS Demo"
        } else {
            "Library/Application Support/org.mycelium.app/state"
        }
    }
    /// The run model a fresh installation shows (`/run_model/selected` while `/run_model/source`
    /// is `demo_default`): v0.1.4's starter `a9-route` (run_model.py:69-75) for the certified
    /// oracle, v0.2's `qwen3-0.6b` for the Rust launcher (B13, ORCHESTRATOR DECISION 17 Nov 2026 on
    /// ruling 28; `crates/api/contracts/divergences.json` `diverged.run_model_default`).
    pub fn starter_run_model(&self) -> &'static str {
        if self.implementation == "python-v0.1.4" {
            ORACLE_STARTER
        } else {
            "qwen3-0.6b"
        }
    }
    /// A golden's observations as this target reproduces them. Goldens are captured from the
    /// oracle: every observation of the starter (`/run_model/source` `demo_default`) must show the
    /// oracle's starter, which moves to this target's ([`Target::starter_run_model`]); every other
    /// field is compared as recorded.
    pub fn golden_observations(&self, golden: &Value) -> Result<Value> {
        let mut observations = golden.clone();
        for observation in observations
            .as_array_mut()
            .ok_or("golden:observations_not_array")?
        {
            let fields = &mut observation["fields"];
            if fields["/run_model/source"] != "demo_default" {
                continue;
            }
            require(
                fields["/run_model/selected"] == ORACLE_STARTER,
                "golden:starter_not_the_oracle_starter",
            )?;
            fields["/run_model/selected"] = json!(self.starter_run_model());
        }
        Ok(observations)
    }
    pub fn verify(&self) -> Result<()> {
        require(lower_hex(&self.source_sha, 40), "target:invalid_source_sha")?;
        require(
            lower_hex(&self.executable_sha256, 64),
            "target:invalid_executable_sha256",
        )?;
        require(
            matches!(self.implementation.as_str(), "python-v0.1.4" | "rust"),
            "target:implementation",
        )?;
        if self.implementation == "python-v0.1.4" {
            require(
                self.source_sha == ORACLE_SOURCE && self.executable_sha256 == ORACLE_EXE,
                "target:uncertified_oracle",
            )?;
        }
        require(
            self.executable.is_absolute(),
            "target:absolute_executable_required",
        )?;
        require(
            digest_file(&self.executable)? == self.executable_sha256,
            "target:executable_digest_mismatch",
        )
    }
}
pub fn digest_file(path: &Path) -> Result<String> {
    let mut file = File::open(path).map_err(|e| io("digest:open", e))?;
    let mut hash = Sha256::new();
    let mut buf = [0; 65536];
    loop {
        let n = file.read(&mut buf).map_err(|e| io("digest:read", e))?;
        if n == 0 {
            break;
        }
        hash.update(&buf[..n]);
    }
    Ok(format!("{:x}", hash.finalize()))
}
fn quoted(path: &Path) -> Result<String> {
    let raw = path.to_str().ok_or("sandbox:path_utf8")?;
    require(
        path.is_absolute() && !raw.contains(['"', '\\', '\n', '\r']),
        "sandbox:unsafe_path",
    )?;
    Ok(format!("\"{raw}\""))
}
/// The path the kernel sees. Seatbelt matches resolved vnode paths, so a rule naming a
/// symlinked spelling (`~/.hermes` -> elsewhere, `/var` -> `/private/var`) never fires.
/// A path that does not exist yet resolves its deepest existing ancestor.
pub fn kernel_path(path: &Path) -> Result<PathBuf> {
    let mut rest = Vec::new();
    let mut existing = path;
    loop {
        match fs::canonicalize(existing) {
            Ok(real) => return Ok(rest.iter().rev().fold(real, |p, c| p.join(c))),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                rest.push(existing.file_name().ok_or("sandbox:path_unresolvable")?);
                existing = existing.parent().ok_or("sandbox:path_unresolvable")?;
            }
            Err(e) => return Err(io("sandbox:canonicalize", e)),
        }
    }
}
/// Filters naming `path` as given and as resolved; deny rules use both spellings.
pub fn spellings(kind: &str, path: &Path) -> Result<String> {
    let given = quoted(path)?;
    let real = quoted(&kernel_path(path)?)?;
    Ok(if given == real {
        format!("({kind} {given})")
    } else {
        format!("({kind} {given}) ({kind} {real})")
    })
}
pub fn sandbox_profile(home: &Path, scratch: &Path) -> Result<String> {
    let home_q = quoted(home)?;
    quoted(scratch)?;
    // Ownership and the write grant both use the path the kernel will actually see.
    // Only a per-run directory directly inside the resolved base is grantable: never the
    // base itself, HOME, an ancestor or anything elsewhere.
    let scratch_real = kernel_path(scratch)?;
    let base_real = kernel_path(&home.join(SCRATCH_BASE))?;
    require(
        scratch_real.parent() == Some(base_real.as_path()),
        "sandbox:scratch_not_owned",
    )?;
    let scratch_q = quoted(&scratch_real)?;
    let mut policy = format!(
        "(version 1)\n(allow default)\n(deny network*)\n(allow network-outbound (remote ip \"localhost:*\"))\n(allow network-inbound (local ip \"localhost:*\"))\n(allow network-bind (local ip \"localhost:*\"))\n(deny file-write*)\n(allow file-write* (subpath {scratch_q}) (literal \"/dev/null\"))\n"
    );
    // Do not even READ live identity, preferences, installed apps or model caches.
    for relative in [
        "Library",
        "Applications",
        "mycelium-demo-run",
        "mycelium-physical-run",
        ".cache",
        ".mycelium",
        ".mycelium-demo",
        ".mycelium-a13",
    ] {
        policy.push_str(&format!(
            "(deny file-read* {})\n",
            spellings("subpath", &home.join(relative))?
        ));
    }
    // Prefix catches every ~/.mycelium* spelling, not just the known names above.
    let real_home_q = quoted(&kernel_path(home)?)?;
    for spelling in BTreeSet::from([&home_q, &real_home_q]) {
        let raw = spelling.trim_matches('"');
        policy.push_str(&format!(
            "(deny file-read* (regex #\"^{raw}/[.]mycelium\"))\n"
        ));
    }
    Ok(policy)
}

const SCRATCH_BASE: &str = ".hermes/cache/scratch";
/// The dedicated scratch base `<home>/.hermes/cache/scratch`, as the kernel resolves it.
/// A fresh HOME needs no installed Hermes tree: each missing level is created alone, 0700
/// (never `create_dir_all`, never through a dangling link). Every level, created, ordinary or
/// reached through a symlink, must resolve to a directory this user owns that no group or
/// other user can write, and the base must not contain HOME. An unsafe existing level is
/// refused, never repaired. Only per-run directories inside it are granted (`sandbox_profile`).
pub fn scratch_base(home: &Path) -> Result<PathBuf> {
    require(home.is_absolute(), "scratch:home_not_absolute")?;
    let uid = rustix::process::geteuid().as_raw();
    let mut level = home.to_path_buf();
    for name in Path::new(SCRATCH_BASE).components() {
        level.push(name);
        match fs::DirBuilder::new().mode(0o700).create(&level) {
            Err(e) if e.kind() != std::io::ErrorKind::AlreadyExists => {
                return Err(io("scratch:base_create", e));
            }
            _ => {}
        }
        let meta = fs::metadata(&level).map_err(|e| io("scratch:base_inspect", e))?;
        require(meta.is_dir(), "scratch:base_not_directory")?;
        require(meta.uid() == uid, "scratch:base_not_owned")?;
        require(meta.mode() & 0o022 == 0, "scratch:base_writable_by_others")?;
    }
    let base = fs::canonicalize(&level).map_err(|e| io("scratch:base_resolve", e))?;
    require(
        !kernel_path(home)?.starts_with(&base),
        "scratch:base_contains_home",
    )?;
    Ok(base)
}

static NEXT: AtomicUsize = AtomicUsize::new(0);
pub struct Scratch {
    pub root: PathBuf,
    pub home: PathBuf,
    pub state: PathBuf,
    removed: bool,
}
impl Scratch {
    pub fn new(real_home: &Path, previous: Option<&Scratch>) -> Result<Self> {
        // Resolved, so the child never gets a symlinked HOME: the product refuses a state
        // root that does not resolve to itself (v0.1.4 `_private_root`).
        let root = scratch_base(real_home)?.join(format!(
            "v02-l3-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir(&root).map_err(|e| io("scratch:create_new", e))?;
        let mut scratch = Self {
            home: root.join("home"),
            state: root.join("home/Library/Application Support/Mycelium A13 macOS Demo"),
            root,
            removed: false,
        };
        if let Err(error) = scratch.prepare(previous) {
            if let Err(cleanup) = scratch.remove() {
                return Err(format!("{error};{cleanup}"));
            }
            return Err(error);
        }
        Ok(scratch)
    }
    fn prepare(&mut self, previous: Option<&Scratch>) -> Result<()> {
        fs::set_permissions(&self.root, fs::Permissions::from_mode(0o700))
            .map_err(|e| io("scratch:permissions", e))?;
        fs::create_dir(&self.home).map_err(|e| io("scratch:home", e))?;
        fs::create_dir(self.root.join("tmp")).map_err(|e| io("scratch:tmp", e))?;
        if let Some(old) = previous {
            // Opaque retained state from our stopped child only; no record edits/default synthesis.
            fs::rename(old.home.join("Library"), self.home.join("Library"))
                .map_err(|e| io("scratch:retained_state_move", e))?;
        }
        Ok(())
    }
    pub fn remove(&mut self) -> Result<()> {
        if !self.removed {
            fs::remove_dir_all(&self.root).map_err(|e| io("scratch:remove", e))?;
            require(!self.root.exists(), "scratch:leftover")?;
            self.removed = true;
        }
        Ok(())
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        if let Err(error) = self.remove() {
            eprintln!("CLEANUP_FAILED:{error}");
        }
    }
}

pub fn check_host_markers(directory: &Path) -> Result<()> {
    let entries = match fs::read_dir(directory) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(io("host_guard:markers", error)),
    };
    for entry in entries {
        let entry = entry.map_err(|e| io("host_guard:entry", e))?;
        require(
            !entry.file_name().to_string_lossy().ends_with(".busy"),
            "BLOCKED:host_busy_marker",
        )?;
    }
    Ok(())
}

pub fn require_normal_memory_pressure(raw: &str) -> Result<()> {
    require(raw.trim() == "1", "BLOCKED:memory_pressure_not_normal")
}
fn memory_admission() -> Result<()> {
    let output = Command::new("/usr/sbin/sysctl")
        .args(["-n", "kern.memorystatus_vm_pressure_level"])
        .output()
        .map_err(|e| io("host_guard:memory_sysctl", e))?;
    require(
        output.status.success(),
        "BLOCKED:memory_pressure_probe_failed",
    )?;
    let raw =
        std::str::from_utf8(&output.stdout).map_err(|_| "BLOCKED:memory_pressure_encoding")?;
    require_normal_memory_pressure(raw)
}
fn host_admission(marker_root: &Path) -> Result<()> {
    memory_admission()?;
    check_host_markers(marker_root)?;
    let status = Command::new("/usr/bin/pgrep")
        // `[P]` matches the same wrappers but not a sibling guard's own pgrep argv, so two
        // concurrent runners no longer block each other (repo convention, see backend-mlx).
        .args(["-f", "[P]ARENT-cert-.*-when-quiet"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|e| io("host_guard:pgrep", e))?;
    match status.code() {
        Some(1) => Ok(()),
        Some(0) => Err("BLOCKED:certification_waiter".into()),
        _ => Err("BLOCKED:host_guard_pgrep_failed".into()),
    }
}

pub struct App {
    child: Child,
    output: Option<JoinHandle<Result<()>>>,
    pub client: Option<Client>,
    pub scratch: Scratch,
    pub receipt: Value,
    seen: BTreeSet<u32>,
    stopped: bool,
}
impl App {
    pub fn launch(target: &Target, real_home: &Path, previous: Option<&Scratch>) -> Result<Self> {
        Self::launch_with_marker_root(
            target,
            real_home,
            &real_home.join("mycelium-demo-run"),
            previous,
        )
    }
    /// Launch with an explicit busy-marker root. Production callers use `launch`, which
    /// guards the real `<home>/mycelium-demo-run`; tests inject a private directory so the
    /// result never depends on host markers. Any `*.busy` file in the root blocks.
    pub fn launch_with_marker_root(
        target: &Target,
        real_home: &Path,
        marker_root: &Path,
        previous: Option<&Scratch>,
    ) -> Result<Self> {
        target.verify()?; // Every launch, including restarts, hashes original executable bytes.
        require(cfg!(target_os = "macos"), "BLOCKED:macos_sandbox_required")?;
        let mut scratch = Scratch::new(real_home, previous)?;
        scratch.state = scratch.home.join(target.state_folder());
        let policy = sandbox_profile(real_home, &scratch.root)?;
        let profile = scratch.root.join("isolation.sb");
        fs::write(&profile, &policy).map_err(|e| io("sandbox:write", e))?;
        let stderr = File::create(scratch.root.join("stderr.private"))
            .map_err(|e| io("process:stderr", e))?;
        let env = [
            ("HOME", scratch.home.to_string_lossy().into_owned()),
            (
                "TMPDIR",
                scratch.root.join("tmp").to_string_lossy().into_owned(),
            ),
            (
                "CFFIXED_USER_HOME",
                scratch.home.to_string_lossy().into_owned(),
            ),
            ("PATH", "/usr/bin:/bin:/usr/sbin:/sbin".to_owned()),
            ("LANG", "en_US.UTF-8".to_owned()),
        ];
        let mut command = Command::new("/usr/bin/sandbox-exec");
        command
            .arg("-f")
            .arg(&profile)
            .arg(&target.executable)
            .args(["--headless", "--offline"])
            .current_dir(&scratch.home)
            .env_clear()
            .envs(env.clone())
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(stderr)
            .process_group(0);
        // Recheck immediately before EVERY launch, including restarts. A newly busy
        // host is BLOCKED (caller can rerun through the required quiet-host wrapper).
        host_admission(marker_root)?;
        let mut child = command.spawn().map_err(|e| io("process:spawn", e))?;
        let pid = child.id();
        let stdout = match child.stdout.take() {
            Some(pipe) => pipe,
            None => {
                child.kill().map_err(|e| io("process:no_stdout_kill", e))?;
                child.wait().map_err(|e| io("process:no_stdout_wait", e))?;
                return Err("process:no_stdout".into());
            }
        };
        let (sender, receiver) = mpsc::channel();
        let output = thread::spawn(move || {
            // Never retain or print app output: only a closed readiness event crosses this channel.
            let mut reader = BufReader::new(stdout);
            loop {
                let mut line = String::new();
                let n = reader
                    .by_ref()
                    .take(65536)
                    .read_line(&mut line)
                    .map_err(|e| io("process:stdout_read", e))?;
                if n == 0 {
                    return Ok(());
                }
                require(line.ends_with('\n'), "process:stdout_line_too_large")?;
                if let Ok(value) = serde_json::from_str::<Value>(&line)
                    && value["event"] == "launcher_started"
                {
                    sender
                        .send(())
                        .map_err(|_| "process:readiness_receiver_gone")?;
                }
            }
        });
        let mut app = Self {
            child,
            output: Some(output),
            client: None,
            scratch,
            receipt: json!({"pid":pid,"executable_sha256":target.executable_sha256,"source_sha":target.source_sha,"command":["/usr/bin/sandbox-exec","-f",profile,target.executable,"--headless","--offline"],"env_allowlist":env,"sandbox_policy_sha256":format!("{:x}",Sha256::digest(policy.as_bytes())),"readiness_budget":"WorkerStartup","http_budget":"ModelSelection/WorkerStartup","stop_budget":"WorkerStartup","kill_reap_budget":"ModelSelection","network_policy":"deny_all_except_numeric_loopback","file_write_policy":"scratch_only","live_state_read_policy":"denied"}),
            seen: BTreeSet::from([pid]),
            stopped: false,
        };
        let ready = receiver
            .recv_timeout(duration(BudgetId::WorkerStartup)?)
            .map_err(|e| format!("process:readiness:{e:?}"));
        let setup = ready.and_then(|()| app.authenticate());
        if let Err(error) = setup {
            let diagnostic = app.safe_failure_diagnostic();
            let cleanup = app.stop();
            return Err(format!(
                "{error};diagnostic={diagnostic};cleanup={cleanup:?}"
            ));
        }
        app.check_processes()?;
        Ok(app)
    }
    fn safe_failure_diagnostic(&self) -> String {
        match fs::read_to_string(self.scratch.root.join("stderr.private")) {
            Ok(text) => {
                for line in text.lines().rev() {
                    if let Ok(value) = serde_json::from_str::<Value>(line) {
                        let mut fields = serde_json::Map::new();
                        for key in ["event", "code", "error_type"] {
                            if let Some(raw) = value[key].as_str()
                                && raw.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_')
                            {
                                fields.insert(key.to_owned(), json!(raw));
                            }
                        }
                        return Value::Object(fields).to_string();
                    }
                }
                "non_json_stderr_redacted".into()
            }
            Err(e) => io("diagnostic:read", e),
        }
    }
    fn authenticate(&mut self) -> Result<()> {
        let html = fs::read_to_string(self.scratch.state.join("open-demo.html"))
            .map_err(|e| io("launch_form:read", e))?;
        let action = attribute(&html, "action")?;
        let origin = action
            .strip_suffix("/session")
            .ok_or("launch_form:action")?;
        let launch = attribute(&html, "value")?;
        let mut client = Client::new(origin)?;
        client.sign_in(&launch)?;
        require(
            !self.scratch.state.join("open-demo.html").exists(),
            "session:launch_not_consumed",
        )?;
        let status = client.get("/api/status")?;
        require(status.status == 200, "readiness:status_not_200")?;
        self.client = Some(client);
        Ok(())
    }
    pub fn api(&self) -> Result<&Client> {
        // Recheck throughout observations. Cleanup deliberately uses its stored
        // client directly, so pressure refusal cannot suppress exact-owned teardown.
        memory_admission()?;
        self.client
            .as_ref()
            .ok_or_else(|| "process:unauthenticated".into())
    }
    pub fn check_processes(&mut self) -> Result<()> {
        self.seen.extend(group_members(self.child.id())?);
        Ok(())
    }
    fn wait_until(&mut self, budget: BudgetId) -> Result<bool> {
        let deadline = Instant::now() + duration(budget)?;
        loop {
            self.check_processes()?;
            if let Some(status) = self
                .child
                .try_wait()
                .map_err(|e| io("process:try_wait", e))?
            {
                self.receipt["exit_code"] = json!(status.code());
                self.receipt["exit_success"] = json!(status.success());
                return Ok(true);
            }
            if Instant::now() >= deadline {
                return Ok(false);
            }
            thread::park_timeout(
                duration(BudgetId::OwnerPoll)?
                    .min(deadline.saturating_duration_since(Instant::now())),
            );
        }
    }
    pub fn abort_forbidden(&mut self) -> Result<()> {
        self.receipt["forbidden_activity_killed"] = json!(true);
        if self
            .child
            .try_wait()
            .map_err(|e| io("abort:try_wait", e))?
            .is_none()
        {
            self.child
                .kill()
                .map_err(|e| io("abort:kill_exact_pid", e))?;
            require(
                self.wait_until(BudgetId::ModelSelection)?,
                "abort:reap_deadline",
            )?;
        }
        Ok(())
    }
    pub fn stop(&mut self) -> Result<()> {
        if self.stopped {
            return Ok(());
        }
        let mut problems = Vec::new();
        if self
            .child
            .try_wait()
            .map_err(|e| io("stop:try_wait", e))?
            .is_none()
        {
            if let Some(client) = &self.client {
                match client.post("/api/quit", &json!({})) {
                    Ok(response) if response.status == 200 => {
                        self.receipt["quit_http_status"] = json!(200)
                    }
                    Ok(response) => problems.push(format!("stop:quit_status:{}", response.status)),
                    Err(error) => problems.push(error),
                }
            } else {
                let status = Command::new("/bin/kill")
                    .args(["-TERM", &self.child.id().to_string()])
                    .status()
                    .map_err(|e| io("stop:term", e))?;
                if !status.success() {
                    problems.push("stop:term_failed".into());
                }
            }
        }
        let graceful = self.wait_until(BudgetId::WorkerStartup)?;
        self.receipt["forced_kill"] =
            json!(!graceful || self.receipt["forbidden_activity_killed"] == true);
        if !graceful {
            self.child.kill().map_err(|e| io("stop:kill", e))?;
            require(
                self.wait_until(BudgetId::ModelSelection)?,
                "stop:kill_reap_deadline",
            )?;
            problems.push("stop:grace_deadline".into());
        }
        let leftover = group_members(self.child.id())?;
        self.receipt["leftover_pids"] = json!(leftover);
        self.receipt["observed_pids"] = json!(self.seen);
        for pid in leftover {
            let status = Command::new("/bin/kill")
                .args(["-KILL", &pid.to_string()])
                .status()
                .map_err(|e| io("stop:descendant_kill", e))?;
            require(status.success(), "stop:descendant_kill_failed")?;
            problems.push(format!("stop:leftover_descendant:{pid}"));
        }
        let reap_deadline = Instant::now() + duration(BudgetId::ModelSelection)?;
        while !group_members(self.child.id())?.is_empty() {
            require(Instant::now() < reap_deadline, "stop:descendants_remain")?;
            thread::park_timeout(
                duration(BudgetId::OwnerPoll)?
                    .min(reap_deadline.saturating_duration_since(Instant::now())),
            );
        }
        require(!pid_exists(self.child.id())?, "stop:pid_remains")?;
        let output_deadline = Instant::now() + duration(BudgetId::ModelSelection)?;
        while self
            .output
            .as_ref()
            .is_some_and(|output| !output.is_finished())
        {
            require(
                Instant::now() < output_deadline,
                "stop:stdout_drain_deadline",
            )?;
            thread::park_timeout(
                duration(BudgetId::OwnerPoll)?
                    .min(output_deadline.saturating_duration_since(Instant::now())),
            );
        }
        if let Some(output) = self.output.take() {
            match output.join() {
                Ok(Ok(())) => {}
                Ok(Err(error)) => problems.push(error),
                Err(_) => problems.push("stop:stdout_thread_panicked".into()),
            }
        }
        if let Some(client) = &self.client {
            let port = crate::loopback_origin(&client.origin)?;
            match std::net::TcpStream::connect_timeout(
                &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
                duration(BudgetId::ModelSelection)?,
            ) {
                Err(error) if error.kind() == std::io::ErrorKind::ConnectionRefused => {
                    self.receipt["listener_closed"] = json!(true)
                }
                Err(error) => problems.push(io("stop:listener_check", error)),
                Ok(_) => problems.push("stop:listener_remains".into()),
            }
        }
        self.receipt["scratch_file_bytes"] = json!(audit_tree(&self.scratch.root)?);
        self.receipt["ps_verified_absent"] = json!(true);
        let stale = ["open-demo.html", "native-control.json"]
            .into_iter()
            .filter(|name| self.scratch.state.join(name).exists())
            .collect::<Vec<_>>();
        self.receipt["stale_control_files"] = json!(stale);
        if !stale.is_empty() {
            problems.push("stop:stale_control_files".into());
        }
        if self.receipt["exit_success"] != true {
            problems.push("stop:nonzero_exit".into());
        }
        self.stopped = true;
        if problems.is_empty() {
            Ok(())
        } else {
            Err(problems.join(";"))
        }
    }
}
impl Drop for App {
    fn drop(&mut self) {
        if let Err(error) = self.stop() {
            eprintln!("CLEANUP_FAILED:{error}");
        }
    }
}
pub fn audit_tree(root: &Path) -> Result<u64> {
    let mut total = 0_u64;
    let mut pending = vec![root.to_path_buf()];
    while let Some(path) = pending.pop() {
        let meta = fs::symlink_metadata(&path).map_err(|e| io("scratch:audit_metadata", e))?;
        require(!meta.file_type().is_symlink(), "scratch:symlink_forbidden")?;
        if meta.is_dir() {
            for entry in fs::read_dir(&path).map_err(|e| io("scratch:audit_read_dir", e))? {
                pending.push(entry.map_err(|e| io("scratch:audit_entry", e))?.path());
            }
        } else {
            let name = path
                .file_name()
                .and_then(|s| s.to_str())
                .ok_or("scratch:non_utf8_filename")?;
            require(
                ![".safetensors", ".gguf", ".part", ".partial"]
                    .iter()
                    .any(|suffix| name.ends_with(suffix))
                    && !name.starts_with("pytorch_model"),
                "scratch:model_payload_forbidden",
            )?;
            total = total
                .checked_add(meta.len())
                .ok_or("scratch:size_overflow")?;
        }
    }
    Ok(total)
}
fn attribute(html: &str, name: &str) -> Result<String> {
    let key = format!("{name}=\"");
    let (_, rest) = html
        .split_once(&key)
        .ok_or("launch_form:missing_attribute")?;
    Ok(rest
        .split_once('"')
        .ok_or("launch_form:unterminated_attribute")?
        .0
        .to_owned())
}
fn process_rows() -> Result<Vec<(u32, u32)>> {
    let output = Command::new("/bin/ps")
        .args(["-axo", "pid=,pgid="])
        .output()
        .map_err(|e| io("ps:spawn", e))?;
    require(output.status.success(), "ps:failed")?;
    let raw = std::str::from_utf8(&output.stdout).map_err(|_| "ps:utf8")?;
    raw.lines()
        .map(|line| {
            let values = line
                .split_whitespace()
                .map(str::parse::<u32>)
                .collect::<std::result::Result<Vec<_>, _>>()
                .map_err(|_| "ps:row")?;
            require(values.len() == 2, "ps:columns")?;
            Ok((values[0], values[1]))
        })
        .collect()
}
fn group_members(pgid: u32) -> Result<Vec<u32>> {
    Ok(process_rows()?
        .into_iter()
        .filter(|(_, group)| *group == pgid)
        .map(|(pid, _)| pid)
        .collect())
}
fn pid_exists(pid: u32) -> Result<bool> {
    Ok(process_rows()?.iter().any(|(id, _)| *id == pid))
}
