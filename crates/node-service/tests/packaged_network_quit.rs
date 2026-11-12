// SPDX-License-Identifier: AGPL-3.0-only
//! Quit while `/api/network/start` still waits for the quick tunnel's origin (review PH3a F1).
//! v0.1.4 runs Start and `close` under the same `self.guard` (app.py:1344 `_action`, app.py:1831
//! `DemoApp.close`): a relay a Start brings up is closed by the Quit that follows it
//! (`self.public_relay.close()`, app.py:1848-1850), and once closed every action answers
//! `application_closed` (app.py:1345). The packaged Quit must therefore never report `Verified`
//! while a relay child it did not close still runs, and no relay child may outlive it (the child
//! is in its own process group, so the app's exit does not end it).
//!
//! The tunnel is a fake quick-tunnel `/bin/sh` script given through `NetworkConfig.hooks` (never
//! `cloudflared`). It reports its pid on a FIFO the test reads, then blocks on a second FIFO until
//! the test releases it; only then does it print a `trycloudflare.com` origin, and it then blocks
//! until it is terminated. Every launch is appended to a file, so a Start that should start nothing
//! is checked by count.
//!
//! Also here: the test Macs (`support/packaged_macs.rs` `test_network`) cannot start a real
//! `cloudflared` at all.
#![cfg(feature = "desktop-shell")]
#[path = "support/packaged_macs.rs"]
mod macs;
use budgets::BudgetId as B;
use desktop::{
    channel::{ORIGIN, Request},
    core::Core,
    lifecycle::{Cleanup, CleanupError, Poll},
};
use macs::{Mac, millis, state_root};
use node_service::packaged_network::{NetworkConfig, RelayHooks};
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};

/// The fake quick tunnel's files: the script, its two FIFOs and its launch log.
struct FakeTunnel {
    script: PathBuf,
    started: PathBuf,
    release: PathBuf,
    launches: PathBuf,
}
impl FakeTunnel {
    /// `held`: the script reports its pid on `started` and waits for [`FakeTunnel::release`]
    /// before it prints its origin; otherwise it prints its origin at once.
    fn new(base: &Path, held: bool) -> Self {
        let dir = base.join("fake-tunnel");
        std::fs::DirBuilder::new().mode(0o700).create(&dir).unwrap();
        let fake = Self {
            script: dir.join("cloudflared-fake"),
            started: dir.join("started"),
            release: dir.join("release"),
            launches: dir.join("launches"),
        };
        for fifo in [&fake.started, &fake.release] {
            let made = std::process::Command::new("/usr/bin/mkfifo")
                .arg("-m")
                .arg("600")
                .arg(fifo)
                .status()
                .unwrap();
            assert!(made.success(), "mkfifo {}", fifo.display());
        }
        // fd 3 holds the release FIFO open read-write: the script never blocks opening it, and a
        // writer finds a reader for as long as the script lives.
        let hold = if held {
            format!("echo $$ > '{}'\nread _ <&3\n", fake.started.display())
        } else {
            String::new()
        };
        std::fs::write(
            &fake.script,
            format!(
                "#!/bin/sh\nexec 3<>'{release}'\necho $$ >> '{launches}'\n{hold}echo 'INF |  https://b3f-quit-inflight.trycloudflare.com  |'\nread _ <&3\n",
                release = fake.release.display(),
                launches = fake.launches.display(),
            ),
        )
        .unwrap();
        std::fs::set_permissions(&fake.script, std::fs::Permissions::from_mode(0o700)).unwrap();
        fake
    }
    /// The production hooks with this script as the relay client and a minimal environment.
    fn hooks(&self) -> RelayHooks {
        self.hooks_with(|_| {})
    }
    /// [`FakeTunnel::hooks`], then `edit` (a test's clock or release observer).
    fn hooks_with(
        &self,
        edit: impl Fn(&mut api::relay::Hooks) + Send + Sync + 'static,
    ) -> RelayHooks {
        let script = self.script.clone();
        Arc::new(move || {
            let mut hooks = api::relay::Hooks::production(None);
            let script = script.clone();
            hooks.binary = Arc::new(move || Some(script.clone()));
            hooks.env = vec![("PATH".into(), "/usr/bin:/bin".into())];
            edit(&mut hooks);
            hooks
        })
    }
    /// The pid the script reports once it runs (read from its FIFO), within the worker budget.
    fn wait_started(&self) -> i32 {
        let started = self.started.clone();
        let (tx, rx) = std::sync::mpsc::channel();
        let reader = std::thread::spawn(move || {
            let mut text = String::new();
            let read = std::fs::File::open(&started).and_then(|mut f| f.read_to_string(&mut text));
            tx.send(read.map(|_| text)).unwrap_or(());
        });
        let text = rx
            .recv_timeout(millis(B::WorkerStartup))
            .expect("the fake tunnel never started")
            .unwrap();
        reader.join().unwrap();
        text.trim().parse().unwrap()
    }
    /// Let the script print its origin. A script that is gone has no reader: nothing to release.
    fn release(&self) {
        match std::fs::OpenOptions::new()
            .write(true)
            .custom_flags(rustix::fs::OFlags::NONBLOCK.bits() as i32)
            .open(&self.release)
        {
            Ok(mut fifo) => fifo.write_all(b"go\n").unwrap(),
            Err(e) if e.raw_os_error() == Some(rustix::io::Errno::NXIO.raw_os_error()) => {
                eprintln!("release: the fake tunnel already ended (no reader)");
            }
            Err(e) => panic!("release: {e}"),
        }
    }
    /// Every pid the script ran as.
    fn launches(&self) -> Vec<i32> {
        std::fs::read_to_string(&self.launches)
            .unwrap_or_default()
            .lines()
            .map(|line| line.trim().parse().unwrap())
            .collect()
    }
}
fn alive(pid: i32) -> bool {
    rustix::process::Pid::from_raw(pid)
        .is_some_and(|pid| rustix::process::test_kill_process(pid).is_ok())
}
/// Ends a fake this test started that is still running (only after it was observed).
fn kill(pid: i32) {
    if let Some(pid) = rustix::process::Pid::from_raw(pid) {
        rustix::process::kill_process(pid, rustix::process::Signal::KILL).unwrap_or(());
    }
}
fn post(core: &Arc<dyn Core>, path: &str, body: &Value) -> (u16, Value) {
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    core.submit(
        Request {
            window: "main".into(),
            method: "POST".into(),
            uri: format!("{ORIGIN}{path}"),
            headers: vec![
                ("x-mycelium-action".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: serde_json::to_vec(body).unwrap(),
        },
        Box::new(move |reply| tx.send(reply).unwrap()),
    );
    // The Start may wait for the tunnel's whole ready wait.
    let reply = rx
        .recv_timeout(
            millis(B::PyMyceliumA13MacosPublicRelayTemporaryhttpsrelayStartSelfReadyWaitL355)
                + millis(B::WorkerStartup),
        )
        .unwrap();
    (
        reply.status,
        serde_json::from_slice(&reply.body).unwrap_or(Value::Null),
    )
}
/// Polls an already begun Quit to its result within the Quit allowance.
fn finish_quit(mac: &mut Mac, began: Instant) -> Result<Poll, CleanupError> {
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    let deadline = began + Duration::from_millis(allowance.millis());
    loop {
        match mac.cleanup.poll() {
            Ok(Poll::Pending) => {
                assert!(Instant::now() < deadline, "the packaged core did not quit");
                std::thread::sleep(millis(B::OwnerPoll) / 5);
            }
            other => return other,
        }
    }
}

/// Quit begins while the quick-tunnel Start waits for its origin; the tunnel then reports it (a
/// slow tunnel during Quit). Quit must leave no relay child running and must not be `Verified`
/// while one runs; a Start arriving after Quit began is refused and launches nothing.
#[test]
fn quit_closes_a_relay_whose_start_is_still_waiting_for_its_tunnel() {
    let base = state_root("quit-inflight");
    let fake = FakeTunnel::new(&base, true);
    let state = base.join("owner");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&state)
        .unwrap();
    let mut mac = Mac::launch(
        &state,
        NetworkConfig {
            hooks: fake.hooks(),
            ..NetworkConfig::default()
        },
    );
    let body = json!({"relay_acknowledged": true, "max_seconds": 1800});
    let core = mac.core.clone();
    let start_body = body.clone();
    let start = std::thread::spawn(move || post(&core, "/api/network/start", &start_body));
    let pid = fake.wait_started();
    // Quit begins while the Start waits for the tunnel's origin.
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    let began = Instant::now();
    mac.cleanup.begin(allowance).unwrap();
    // A Start after Quit began is refused (`application_closed` once closed, app.py:1345).
    let later = post(&mac.core, "/api/network/start", &body);
    // The tunnel reports its origin now.
    fake.release();
    let quit = finish_quit(&mut mac, began);
    let answered = start.join().unwrap();
    let launches = fake.launches();
    let running: Vec<i32> = launches.iter().copied().filter(|p| alive(*p)).collect();
    for pid in &running {
        kill(*pid);
    }
    drop(mac);
    std::fs::remove_dir_all(&base).unwrap_or_default();
    assert!(
        running.is_empty(),
        "Quit answered {quit:?} with relay children {running:?} still running (fake pid {pid}; \
         the in-flight Start answered {answered:?})"
    );
    assert_eq!(quit, Ok(Poll::Verified), "Quit's result");
    assert_ne!(answered.0, 200, "the in-flight Start: {answered:?}");
    assert!(
        later.0 != 200
            && matches!(
                later.1["error"].as_str(),
                Some("application_stopping" | "application_closed")
            ),
        "a Start after Quit began: {later:?}"
    );
    assert_eq!(
        launches,
        vec![pid],
        "a Start after Quit began launched a tunnel"
    );
}

/// No test here can start a real `cloudflared` (review PH3a Q4: this Mac has one on `PATH`, and
/// the packaged tests run the production hooks): the test Macs' relay client lookup finds nothing,
/// so a quick-tunnel Start that forgot `provider: external` fails closed and no relay runs. The
/// lookup is asserted before any Start, so this test never spawns one itself.
#[test]
fn a_test_mac_cannot_start_a_real_cloudflared() {
    let hooks = (macs::test_network().hooks)();
    assert_eq!(
        (hooks.binary)(),
        None,
        "the test Macs' relay hooks find a relay client"
    );
    let state = state_root("no-cloudflared");
    let mac = Mac::launch(&state, macs::test_network());
    let started = mac.post(
        "/api/network/start",
        json!({"relay_acknowledged": true, "max_seconds": 1800}),
    );
    let network = mac.status()["network"].clone();
    assert_eq!(mac.quit(), Ok(Poll::Verified));
    std::fs::remove_dir_all(&state).unwrap_or_default();
    assert_eq!(
        (started.0, started.1["error"].clone()),
        (409, json!("action_not_completed")),
        "a quick-tunnel Start without a relay client: {}",
        started.1
    );
    assert_ne!(network["state"], "relay_process_running", "{network}");
}

/// Quit while a Start waits for an old relay's release (relay `close`: a Start that finds the
/// relay still closing waits for its child and listener to be released, then brings up a new
/// one). The old relay's window runs out on the relay's injected clock and its close is held
/// between "stopped" and the release; a Start arrives and waits for that release; Quit begins;
/// then the release completes. No new relay child may start, none may outlive Quit, Quit must
/// not be `Verified` while one runs, and a Start after Quit began is refused.
#[test]
fn quit_while_a_start_waits_for_the_old_relays_release_starts_no_new_relay() {
    use api::relay::ReleaseEvent;
    use std::sync::{
        Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc,
    };
    let base = state_root("quit-release");
    let fake = FakeTunnel::new(&base, false);
    let state = base.join("owner");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&state)
        .unwrap();
    // The relays' clock in seconds, moved by the test only.
    let now = Arc::new(AtomicU64::new(0f64.to_bits()));
    let (events, seen) = mpsc::channel::<ReleaseEvent>();
    let events = Arc::new(Mutex::new(events));
    let (open, gate) = mpsc::channel::<()>();
    let gate = Arc::new(Mutex::new(gate));
    let held = Arc::new(AtomicBool::new(false));
    let hooks = {
        let (now, events, gate, held) = (now.clone(), events.clone(), gate.clone(), held.clone());
        fake.hooks_with(move |hooks| {
            let now = now.clone();
            hooks.clock = Arc::new(move || f64::from_bits(now.load(Ordering::SeqCst)));
            let (events, gate, held) = (events.clone(), gate.clone(), held.clone());
            hooks.release_observer = Some(Arc::new(move |event| {
                events.lock().unwrap().send(event).unwrap_or(());
                // The first close is held between "stopped" and its release until the test opens it.
                if event == ReleaseEvent::Stopping && !held.swap(true, Ordering::SeqCst) {
                    gate.lock()
                        .unwrap()
                        .recv_timeout(millis(B::WorkerStartup))
                        .unwrap_or(());
                }
            }));
        })
    };
    let mut mac = Mac::launch(
        &state,
        NetworkConfig {
            hooks,
            ..NetworkConfig::default()
        },
    );
    let body = json!({"relay_acknowledged": true, "max_seconds": 1800});
    let first = mac.post("/api/network/start", body.clone());
    assert_eq!(first.0, 200, "the first Start: {}", first.1);
    let old = fake.launches();
    assert_eq!(old.len(), 1, "{old:?}");
    let next = |what: &str| {
        seen.recv_timeout(millis(B::WorkerStartup))
            .unwrap_or_else(|_| panic!("no release event: {what}"))
    };
    // The window runs out: the relay's lifetime thread closes it and is held before the release.
    now.store(3600f64.to_bits(), Ordering::SeqCst);
    assert_eq!(next("the old relay's close"), ReleaseEvent::Stopping);
    // A Start finds the relay stopped and waits for its release.
    let core = mac.core.clone();
    let start_body = body.clone();
    let start = std::thread::spawn(move || post(&core, "/api/network/start", &start_body));
    assert_eq!(
        next("the Start waiting for the release"),
        ReleaseEvent::AwaitingRelease
    );
    // Quit begins while that Start waits; Quit's own close of that relay waits for it too.
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    let began = Instant::now();
    mac.cleanup.begin(allowance).unwrap();
    let later = post(&mac.core, "/api/network/start", &body);
    assert_eq!(
        next("Quit's close of the started relay"),
        ReleaseEvent::AwaitingRelease
    );
    // The release completes: the waiting Start goes on.
    open.send(()).unwrap();
    let quit = finish_quit(&mut mac, began);
    let answered = start.join().unwrap();
    let launches = fake.launches();
    let running: Vec<i32> = launches.iter().copied().filter(|p| alive(*p)).collect();
    for pid in &running {
        kill(*pid);
    }
    drop(mac);
    std::fs::remove_dir_all(&base).unwrap_or_default();
    assert!(
        running.is_empty(),
        "Quit answered {quit:?} with relay children {running:?} still running (launches \
         {launches:?}; the waiting Start answered {answered:?})"
    );
    assert_eq!(quit, Ok(Poll::Verified), "Quit's result");
    assert_ne!(answered.0, 200, "the waiting Start: {answered:?}");
    assert!(
        later.0 != 200
            && matches!(
                later.1["error"].as_str(),
                Some("application_stopping" | "application_closed")
            ),
        "a Start after Quit began: {later:?}"
    );
    assert_eq!(launches, old, "a Start after Quit began launched a tunnel");
}

/// The env var that turns [`the_packaged_host_never_runs_a_relay_client_found_on_path`] into its
/// own child (the base directory), so `PATH` is set for the child process only.
const PATH_CHILD: &str = "RELF2_PACKAGED_PATH_CLIENT_BASE";

/// Ruling 31 (RELF2, review RELF F12/M1): the PACKAGED app never runs a relay client found on
/// `PATH`. v0.1.4 falls back to `shutil.which('cloudflared')` when its bundled file is not an
/// executable file (`public_relay.py:250-255`, reached by `DemoApp._start_public_relay`,
/// app.py:711-712 -> `TemporaryHttpsRelay.start` :327-328), so a Finder-launched app would run
/// whatever `cloudflared` a `PATH` entry provides; v0.2's packaged host deliberately does not.
/// The app's own composition (`NetworkConfig::default()`), a bundle without a relay client (this
/// test binary's directory has no `native/cloudflared`), and a fake `cloudflared` first on `PATH`:
/// the Start must be refused as the shipped page sees a missing client (409
/// `action_not_completed`, app.py:2010-2013 `public_failure_code`; the private failure record
/// names the relay's `relay_client_installation_required`) and nothing may run.
///
/// Guard (no real `cloudflared` can start): the child's `PATH` is the fake's directory plus
/// `/usr/bin:/bin`, and before any Start it asserts that the development lookup
/// (`installed_relay_binary`) finds exactly the fake and nothing beside the executable.
#[test]
fn the_packaged_host_never_runs_a_relay_client_found_on_path() {
    if let Some(base) = std::env::var_os(PATH_CHILD) {
        return path_client_child(Path::new(&base));
    }
    let base = state_root("path-client");
    let fake = FakeTunnel::new(&base, false);
    let bin = base.join("path-bin");
    std::fs::DirBuilder::new().mode(0o700).create(&bin).unwrap();
    std::os::unix::fs::symlink(&fake.script, bin.join("cloudflared")).unwrap();
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "the_packaged_host_never_runs_a_relay_client_found_on_path",
            "--exact",
            "--nocapture",
            "--test-threads",
            "1",
        ])
        .env(PATH_CHILD, &base)
        .env("PATH", format!("{}:/usr/bin:/bin", bin.display()))
        .output()
        .unwrap();
    let launches = fake.launches();
    for pid in &launches {
        kill(*pid);
    }
    let text = format!(
        "{}{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    std::fs::remove_dir_all(&base).unwrap_or_default();
    assert_eq!(
        launches,
        Vec::<i32>::new(),
        "the packaged host ran the relay client found on PATH:\n{text}"
    );
    assert!(output.status.success(), "child:\n{text}");
}
fn path_client_child(base: &Path) {
    let exe = std::env::current_exe().unwrap();
    let exe_dir = exe.parent().unwrap();
    assert!(
        std::fs::symlink_metadata(exe_dir.join("native/cloudflared")).is_err(),
        "guard: a relay client beside the test executable"
    );
    assert_eq!(
        api::relay::installed_relay_binary(Some(exe_dir)),
        Some(base.join("path-bin/cloudflared")),
        "guard: the only relay client this process can find must be the fake"
    );
    let state = base.join("owner");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&state)
        .unwrap();
    let mac = Mac::launch(&state, NetworkConfig::default());
    let started = mac.post(
        "/api/network/start",
        json!({"relay_acknowledged": true, "max_seconds": 1800}),
    );
    let network = mac.status()["network"].clone();
    let record = std::fs::read(state.join("last-action-error.private.json"))
        .ok()
        .and_then(|raw| serde_json::from_slice::<Value>(&raw).ok());
    assert_eq!(mac.quit(), Ok(Poll::Verified));
    println!(
        "RELF2_PATH_CHILD started={} network={network} record={record:?}",
        started.1
    );
    assert_eq!(
        (started.0, started.1["error"].clone()),
        (409, json!("action_not_completed")),
        "a Start with no bundled relay client: {}",
        started.1
    );
    assert_eq!(
        record
            .as_ref()
            .map(|r| (r["path"].clone(), r["error_code"].clone())),
        Some((
            json!("/api/network/start"),
            json!("relay_client_installation_required")
        )),
        "the private failure record: {record:?}"
    );
    assert_ne!(network["state"], "relay_process_running", "{network}");
}
