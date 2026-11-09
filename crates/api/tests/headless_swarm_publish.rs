// SPDX-License-Identifier: AGPL-3.0-only
//! The installed headless host publishes through `AppPublisher` against a real loopback
//! directory: the Rust authority, and (explicitly ignored, external checkout) the deployed Python
//! directory started through its unchanged `python -m mycelium_swarm_directory --hosted` entry.
//! This host has no qualified serving route yet (gap rows 3/6), so it must list nothing on its
//! own; a listing left by an earlier process is created with the same library custody code.
#[path = "support/swarm_publisher.rs"]
mod support;
use api::swarm::publisher::{
    DirectoryClient, DirectoryPort, Error,
    app::{AppPublisher, OwnerFacts},
    heartbeat_seconds,
    host::Stop,
};
use budgets::{BudgetId, Entries};
use journey_tests::http::Client;
use serde_json::{Map, Value, json};
use std::{
    fs,
    io::{BufRead, BufReader},
    net::{TcpListener, TcpStream},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

fn profile_dir() -> PathBuf {
    let exe = std::env::current_exe().unwrap();
    exe.parent().unwrap().parent().unwrap().to_path_buf()
}
fn build() {
    let profile = profile_dir();
    let target = profile.parent().unwrap();
    let manifest = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../Cargo.toml");
    let mut command = Command::new(std::env::var("CARGO").unwrap_or_else(|_| "cargo".into()));
    command.args(["build", "--offline", "--locked", "-j", "2", "-p", "api"]);
    command.args(["--example", "headless"]);
    command.arg("--manifest-path").arg(manifest);
    command.arg("--target-dir").arg(target);
    if profile.file_name().is_some_and(|name| name == "release") {
        command.arg("--release");
    }
    let output = command.output().expect("cargo is runnable");
    assert!(
        output.status.success(),
        "nested build failed:\n{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
fn span(id: BudgetId) -> Duration {
    Duration::from_millis(id.span(Entries::new(0)).unwrap().millis())
}
fn patience() -> Duration {
    span(BudgetId::ModelSelection) * 4
}
fn unix_now() -> i64 {
    i64::try_from(
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs(),
    )
    .unwrap()
}
fn fresh_root(base: &Path, label: &str) -> PathBuf {
    let root = base.join(label);
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    root
}
fn publisher_files(root: &Path) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(root)
        .unwrap()
        .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|n| n.starts_with("swarm-publisher"))
        .collect();
    names.sort();
    names
}
fn rows(directory: &str) -> Vec<Value> {
    DirectoryClient::new(directory)
        .unwrap()
        .call("GET", "/swarms", b"")
        .unwrap()["swarms"]
        .as_array()
        .unwrap()
        .clone()
}
fn listing(directory: &str) -> Map<String, Value> {
    json!({"directory": directory, "name": "Headless swarm", "access_mode": "request",
           "description": "Public text only"})
    .as_object()
    .unwrap()
    .clone()
}
fn until(what: &str, mut done: impl FnMut() -> bool) {
    let deadline = Instant::now() + patience();
    while !done() {
        assert!(Instant::now() < deadline, "timed out waiting for {what}");
        thread::sleep(span(BudgetId::OwnerPoll));
    }
}
/// A listing an earlier owner process left behind (it died while listed), made with the same
/// custody code the headless host runs. The run document is a test input with owner_run keys.
fn leave_listing_behind(root: &Path, directory: &str) {
    let p = AppPublisher::new(root);
    p.publish(true, &listing(directory)).unwrap();
    let serving = json!({"state": "serving", "route_ready": true,
                         "registry_origin": "http://127.0.0.1:9", "participants": [{}]});
    let facts = OwnerFacts::from_owner_run(
        true,
        Some(&serving),
        Some("Qwen/Qwen3-0.6B"),
        Some("https://entry.example.org"),
    );
    p.tick(unix_now(), &facts).unwrap();
    assert_eq!(rows(directory).len(), 1);
}

/// A listing whose record is durable but was never confirmed by any directory: the earlier
/// process died after committing it while the directory was offline. Its retry slot has passed.
fn leave_unconfirmed_listing(root: &Path, directory: &str) {
    let offline = AppPublisher::with_connect(
        root,
        Box::new(|_origin: &str| {
            struct Offline;
            impl DirectoryPort for Offline {
                fn call(&self, _: &str, _: &str, _: &[u8]) -> Result<Value, Error> {
                    Err(Error::new("swarm_directory_unreachable"))
                }
            }
            Ok(Arc::new(Offline) as Arc<dyn DirectoryPort>)
        }),
    );
    offline.publish(true, &listing(directory)).unwrap();
    let serving = json!({"state": "serving", "route_ready": true,
                         "registry_origin": "http://127.0.0.1:9", "participants": [{}]});
    let facts = OwnerFacts::from_owner_run(
        true,
        Some(&serving),
        Some("Qwen/Qwen3-0.6B"),
        Some("https://entry.example.org"),
    );
    let past = unix_now() - 2 * heartbeat_seconds().unwrap();
    let status = offline.tick(past, &facts).unwrap().unwrap();
    assert!(status.swarm_id.is_some(), "the record is durable");
}
/// The installation becomes a member of another swarm (the role record `/api/join/*` writes).
fn become_member(root: &Path) {
    api::preferences::private_state::store(
        &root.join("installation-role.json"),
        &json!({"protocol": "mycelium.a13_installation_role.v1", "role": "target"}),
        4096,
    )
    .unwrap();
}
/// A loopback directory that accepts and never answers (the client's registered deadline ends
/// each exchange); every accepted connection is reported.
struct Silent {
    origin: String,
    accepted: mpsc::Receiver<()>,
    stop: Arc<AtomicBool>,
    join: Option<thread::JoinHandle<()>>,
}
impl Silent {
    fn start() -> Self {
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let (send, accepted) = mpsc::channel();
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = stop.clone();
        let join = thread::spawn(move || {
            let mut held = Vec::new();
            for socket in listener.incoming() {
                let socket = socket.unwrap();
                if stopping.load(Ordering::SeqCst) {
                    break;
                }
                held.push(socket);
                if send.send(()).is_err() {
                    eprintln!("silent_directory_observer_gone");
                }
            }
        });
        Self {
            origin,
            accepted,
            stop,
            join: Some(join),
        }
    }
    fn wait_for_exchange(&self) {
        self.accepted
            .recv_timeout(patience())
            .expect("the host is inside a directory exchange");
    }
}
impl Drop for Silent {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        TcpStream::connect(self.origin.strip_prefix("http://").unwrap()).unwrap();
        self.join.take().unwrap().join().unwrap();
    }
}
/// A loopback directory origin that only counts connection attempts (its accept queue).
struct Counting(TcpListener);
impl Counting {
    fn start() -> Self {
        Self(TcpListener::bind(("127.0.0.1", 0)).unwrap())
    }
    fn origin(&self) -> String {
        format!("http://{}", self.0.local_addr().unwrap())
    }
    fn connections(&self) -> usize {
        self.0.set_nonblocking(true).unwrap();
        let mut count = 0;
        loop {
            match self.0.accept() {
                Ok(_) => count += 1,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => return count,
                Err(e) => panic!("accept: {e}"),
            }
        }
    }
}
fn close_budget() -> Duration {
    span(BudgetId::PyMyceliumA13MacosSwarmPublishSwarmpublisherCloseSelfThreadJoinL453)
}

/// The first stdout line within the patience; the reader is joined (its pipe closes on kill).
fn first_line(stdout: std::process::ChildStdout) -> String {
    let (send, receive) = mpsc::channel();
    let reader = thread::spawn(move || {
        let mut line = String::new();
        BufReader::new(stdout).read_line(&mut line).unwrap();
        send.send(line).unwrap();
    });
    let line = receive
        .recv_timeout(patience())
        .expect("first line within the patience");
    reader.join().unwrap();
    line
}
/// One headless process; dropped = killed and reaped by exact pid if it did not exit.
struct Headless {
    child: Child,
    client: Client,
}
impl Headless {
    fn launch(root: &Path) -> Self {
        let mut child = Command::new(profile_dir().join("examples/headless"))
            .args(["--headless", "--offline", "--state-root"])
            .arg(root)
            .env("HOME", root)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("the headless example was built");
        let stdout = child.stdout.take().unwrap();
        let line = first_line(stdout);
        assert!(line.contains("launcher_started"), "{line:?}");
        let form = fs::read_to_string(root.join("open-demo.html")).expect("launch form");
        let attribute = |name: &str| {
            let key = format!("{name}=\"");
            form.split_once(&key)
                .and_then(|(_, rest)| rest.split_once('"'))
                .map(|(value, _)| value.to_owned())
                .expect("form attribute")
        };
        let origin = attribute("action")
            .strip_suffix("/session")
            .unwrap()
            .to_owned();
        let mut client = Client::new(&origin).unwrap();
        client.sign_in(&attribute("value")).unwrap();
        Self { child, client }
    }
    fn swarm(&self) -> Value {
        self.client.get("/api/status").unwrap().json().unwrap()["swarm"].clone()
    }
    fn post(&self, path: &str, body: Value) -> (u16, Value) {
        let reply = self.client.post(path, &body).unwrap();
        (reply.status, reply.json().unwrap())
    }
    fn quit(self) {
        self.quit_timed();
    }
    /// Quit, then the time from the request to the process exit.
    fn quit_timed(mut self) -> Duration {
        let started = Instant::now();
        assert_eq!(self.post("/api/quit", json!({})).0, 200);
        let deadline = Instant::now() + patience();
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                assert!(status.success(), "{status:?}");
                return started.elapsed();
            }
            assert!(
                Instant::now() < deadline,
                "headless did not exit after quit"
            );
            thread::sleep(span(BudgetId::OwnerPoll));
        }
    }
}
impl Drop for Headless {
    fn drop(&mut self) {
        if self.child.try_wait().unwrap().is_none() {
            self.child.kill().unwrap();
            self.child.wait().unwrap();
        }
    }
}

/// The deployed Python directory, started exactly as the deployment starts it.
struct Deployed {
    child: Option<Child>,
    origin: String,
    db: PathBuf,
}
impl Deployed {
    fn start(db: &Path, port: u16) -> Self {
        let checkout = std::env::var_os("MYCELIUM_SWARM_ORACLE")
            .expect("set MYCELIUM_SWARM_ORACLE to the deployed directory copy");
        let python = std::env::var_os("MYCELIUM_SWARM_ORACLE_PYTHON")
            .unwrap_or_else(|| "/usr/bin/python3".into());
        let mut child = Command::new(python)
            .current_dir(checkout)
            .args([
                "-B",
                "-u",
                "-m",
                "mycelium_swarm_directory",
                "--hosted",
                "--db",
            ])
            .arg(db)
            .args(["--port", &port.to_string()])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let line = first_line(stdout);
        let origin = line
            .trim()
            .strip_prefix("swarm directory on ")
            .unwrap_or_else(|| panic!("directory did not start: {line:?}"))
            .to_owned();
        assert!(origin.starts_with("http://127.0.0.1:"), "{origin}");
        Self {
            child: Some(child),
            origin,
            db: db.into(),
        }
    }
    fn stop(&mut self) {
        if let Some(mut child) = self.child.take() {
            child.kill().unwrap();
            child.wait().unwrap();
        }
    }
    fn restart(&mut self) {
        self.stop();
        let port = self.origin.rsplit(':').next().unwrap().parse().unwrap();
        *self = Self::start(&self.db.clone(), port);
    }
}
impl Drop for Deployed {
    fn drop(&mut self) {
        self.stop();
    }
}

enum Directory {
    Rust(support::Server),
    Python(Deployed),
}
impl Directory {
    fn origin(&self) -> String {
        match self {
            Self::Rust(s) => s.origin.clone(),
            Self::Python(d) => d.origin.clone(),
        }
    }
    fn stop(&mut self) {
        match self {
            Self::Rust(s) => s.stop(),
            Self::Python(d) => d.stop(),
        }
    }
    fn restart(&mut self, clock: &Path) {
        match self {
            Self::Rust(s) => {
                support::set_clock(clock, unix_now());
                s.restart();
            }
            Self::Python(d) => d.restart(),
        }
    }
}

fn journey(python: bool) {
    let started = Instant::now();
    build();
    eprintln!("swarm-publish-profile build {:?}", started.elapsed());
    let scratch = support::Scratch::new();
    let base = fs::canonicalize(&scratch.0).unwrap();
    let dir_root = fresh_root(&base, "directory");
    support::set_clock(&dir_root, unix_now());
    let mut directory = if python {
        Directory::Python(Deployed::start(&dir_root.join("directory.sqlite3"), 0))
    } else {
        Directory::Rust(support::Server::start(&dir_root, false))
    };
    let origin = directory.origin();

    // Opt-out (the default): no publisher state, nothing listed.
    let root = fresh_root(&base, "opted-out");
    let app = Headless::launch(&root);
    assert_eq!(app.swarm(), json!({"enabled": false, "mode": null}));
    app.quit();
    assert!(publisher_files(&root).is_empty());
    assert!(rows(&origin).is_empty());

    eprintln!(
        "swarm-publish-profile {:?} before: Explicit opt-in throug",
        started.elapsed()
    );
    // Explicit opt-in through the app route; not serving, so nothing is listed yet: the bound
    // directory is never contacted, before or after the opt-out, nor across a restart.
    let counting = Counting::start();
    let bound = counting.origin();
    let root = fresh_root(&base, "opt-in");
    let app = Headless::launch(&root);
    let (status, reply) = app.post("/api/swarm/publish", Value::Object(listing(&bound)));
    assert_eq!(status, 200, "{reply}");
    assert_eq!(reply["enabled"], true);
    assert_eq!(reply["phase"], "pending");
    until("status shows the opt-in", || {
        app.swarm()["phase"] == "pending"
    });
    assert_eq!(app.swarm()["directory"], bound.as_str());
    let (status, refused) = app.post("/api/swarm/publish", json!({"link": "mycelium-swarm:x"}));
    assert_eq!(
        (status, refused["error"].clone()),
        (400, json!("swarm_link_mode_unsupported"))
    );
    let (status, out) = app.post("/api/swarm/unpublish", json!({}));
    assert_eq!(status, 200, "{out}");
    assert_eq!(out["enabled"], false);
    app.quit();
    let app = Headless::launch(&root);
    until("the opted-out state reopens", || {
        app.swarm()["enabled"] == false && app.swarm()["mode"] == "app"
    });
    // Several tick periods pass with the state reopened and nothing to do.
    let settled = Instant::now() + span(BudgetId::OwnerPoll) * 4;
    until("tick periods pass", || Instant::now() >= settled);
    app.quit();
    assert_eq!(
        counting.connections(),
        0,
        "an unlisted, opted-out host contacts no directory"
    );

    eprintln!(
        "swarm-publish-profile {:?} before: A listing left by a cr",
        started.elapsed()
    );
    // A listing left by a crashed earlier process: startup withdraws it (not serving now).
    let root = fresh_root(&base, "crash-left");
    leave_listing_behind(&root, &origin);
    let app = Headless::launch(&root);
    until("startup withdrawal", || rows(&origin).is_empty());
    until("status shows the withdrawal", || {
        let s = app.swarm();
        s["phase"] == "pending" && s["swarm_id"].is_null() && s["error_code"].is_null()
    });
    app.quit();
    assert!(rows(&origin).is_empty());

    eprintln!(
        "swarm-publish-profile {:?} before: Directory down at star",
        started.elapsed()
    );
    // Directory down at startup: the debt is reported, survives quit, and a retry succeeds.
    let root = fresh_root(&base, "debt");
    leave_listing_behind(&root, &origin);
    directory.stop();
    let app = Headless::launch(&root);
    until("unreachable withdrawal debt", || {
        let s = app.swarm();
        s["phase"] == "withdraw_pending" && s["error_code"] == "swarm_directory_unreachable"
    });
    app.quit();
    let p = AppPublisher::new(&root);
    assert!(p.resume().unwrap());
    assert_eq!(p.status_json()["phase"], "withdraw_pending");
    directory.restart(&dir_root);
    assert_eq!(
        rows(&origin).len(),
        1,
        "the listing outlived the failed withdrawal"
    );
    let retry = unix_now() + heartbeat_seconds().unwrap();
    support::set_clock(&dir_root, retry);
    let stopped = OwnerFacts::from_owner_run(true, None, None, None);
    p.tick(retry, &stopped).unwrap();
    assert!(rows(&origin).is_empty());
    assert!(p.stop(retry, Stop::Quit).unwrap().is_some());

    eprintln!(
        "swarm-publish-profile {:?} before: A member keeps showing",
        started.elapsed()
    );
    // A member keeps showing a withdrawal it still owes from its time as an owner.
    let root = fresh_root(&base, "member-debt");
    leave_listing_behind(&root, &origin);
    become_member(&root);
    directory.stop();
    let app = Headless::launch(&root);
    until("the member's status shows the owed withdrawal", || {
        let s = app.swarm();
        s["phase"] == "withdraw_pending" && s["error_code"] == "swarm_directory_unreachable"
    });
    app.quit();
    directory.restart(&dir_root);
    let retry = unix_now() + heartbeat_seconds().unwrap();
    support::set_clock(&dir_root, retry);
    let p = AppPublisher::new(&root);
    assert!(p.resume().unwrap());
    let member = OwnerFacts::from_owner_run(false, None, None, None);
    p.tick(retry, &member).unwrap();
    assert!(rows(&origin).is_empty());
    eprintln!("swarm-publish-profile done {:?}", started.elapsed());
}

/// A directory that never answers does not hold the owner's actions: an action while the
/// startup withdrawal waits on it answers at once.
#[test]
fn a_silent_directory_does_not_hold_owner_actions() {
    build();
    let scratch = support::Scratch::new();
    let base = fs::canonicalize(&scratch.0).unwrap();
    let silent = Silent::start();
    let prompt = span(BudgetId::OwnerPoll) * 2;
    let root = fresh_root(&base, "slow-action");
    leave_unconfirmed_listing(&root, &silent.origin);
    let app = Headless::launch(&root);
    silent.wait_for_exchange();
    let asked = Instant::now();
    let (status, out) = app.post("/api/swarm/unpublish", json!({}));
    let answered = asked.elapsed();
    eprintln!("swarm-publish-unpublish-during-exchange {answered:?}");
    assert_eq!(status, 200, "{out}");
    assert!(
        answered < prompt,
        "unpublish waited {answered:?} for the directory"
    );
    assert_eq!(out["enabled"], false);
}

/// Quit while the startup withdrawal waits on a directory that never answers is bounded by the
/// close budget; the withdrawal it could not finish stays durable for the next start.
#[test]
fn a_silent_directory_does_not_hold_quit_beyond_the_close_budget() {
    build();
    let scratch = support::Scratch::new();
    let base = fs::canonicalize(&scratch.0).unwrap();
    let silent = Silent::start();
    let root = fresh_root(&base, "slow-quit");
    leave_unconfirmed_listing(&root, &silent.origin);
    let app = Headless::launch(&root);
    silent.wait_for_exchange();
    let quit = app.quit_timed();
    eprintln!("swarm-publish-quit-during-exchange {quit:?}");
    let bound = close_budget() + span(BudgetId::OwnerPoll) * 3;
    assert!(
        quit < bound,
        "quit took {quit:?}, beyond the close budget {:?}",
        close_budget()
    );
    let p = AppPublisher::new(&root);
    assert!(p.resume().unwrap());
    assert_eq!(p.status_json()["phase"], "withdraw_pending");
}

#[test]
fn headless_publishing_against_the_rust_directory_authority() {
    journey(false);
}

#[test]
#[ignore = "requires the deployed Python directory copy + cryptography interpreter; explicit loopback qualification"]
fn headless_publishing_against_the_deployed_python_directory() {
    journey(true);
}
