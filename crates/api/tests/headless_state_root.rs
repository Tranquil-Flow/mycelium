// SPDX-License-Identifier: AGPL-3.0-only
//! The headless launcher's default state root is v0.2's own data folder, never the v0.1.4 demo's
//! (rulings 20 and 25: v0.2 is `org.mycelium.app` with data under
//! `$HOME/Library/Application Support/org.mycelium.app/state`, beside v0.1.4 and never touching
//! `$HOME/Library/Application Support/Mycelium A13 macOS Demo`, the live v0.1.4 demo's folder).
//! The launcher runs WITHOUT `--state-root` under a scratch `HOME` only (asserted before launch),
//! until its launch event, then SIGTERM.
use budgets::{BudgetId, Entries};
use rustix::process::{Pid, Signal, kill_process};
use std::{
    fs,
    io::{BufRead, BufReader, Read},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

const V014_FOLDER: &str = "Mycelium A13 macOS Demo";

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
fn patience() -> Duration {
    let millis = BudgetId::ModelSelection
        .span(Entries::new(0))
        .unwrap()
        .millis();
    Duration::from_millis(millis * 4)
}
fn poll() -> Duration {
    let millis = BudgetId::OwnerPoll.span(Entries::new(0)).unwrap().millis();
    Duration::from_millis(millis)
}
/// Every path below `dir` (relative), sorted.
fn tree(dir: &Path, base: &Path, out: &mut Vec<String>) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        out.push(
            path.strip_prefix(base)
                .unwrap()
                .to_string_lossy()
                .into_owned(),
        );
        if entry.file_type().is_ok_and(|t| t.is_dir()) {
            tree(&path, base, out);
        }
    }
}

/// No `--state-root`: the launcher keeps its state in `org.mycelium.app/state` under `HOME`, and
/// nothing named after the v0.1.4 demo's folder is created or named in its output.
#[test]
fn the_default_state_root_is_v02s_own_folder() {
    build();
    let home = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("b3f-state-root-{}", std::process::id()));
    fs::remove_dir_all(&home).unwrap_or(());
    fs::create_dir_all(&home).unwrap();
    fs::set_permissions(&home, fs::Permissions::from_mode(0o700)).unwrap();
    // Never the real HOME: this launch has no `--state-root`.
    let real = std::env::var_os("HOME").map(PathBuf::from);
    assert_ne!(Some(home.clone()), real, "scratch HOME is the real HOME");
    assert!(home.starts_with(fs::canonicalize(std::env::temp_dir()).unwrap()));
    let mut child: Child = Command::new(profile_dir().join("examples/headless"))
        .args(["--headless", "--offline"])
        .env("HOME", &home)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("the headless example was built");
    let mut stdout = BufReader::new(child.stdout.take().unwrap());
    let (ready_send, ready) = mpsc::channel();
    let (send, receive) = mpsc::channel();
    let reader = thread::spawn(move || {
        let mut first = String::new();
        stdout.read_line(&mut first).unwrap_or(0);
        ready_send.send(first.clone()).unwrap_or(());
        let mut rest = String::new();
        stdout.read_to_string(&mut rest).unwrap_or(0);
        send.send(format!("{first}{rest}")).unwrap_or(());
    });
    let announced = ready.recv_timeout(patience()).unwrap_or_default();
    let mut running = Vec::new();
    tree(&home, &home, &mut running);
    let pid = Pid::from_raw(i32::try_from(child.id()).unwrap()).unwrap();
    kill_process(pid, Signal::TERM).unwrap_or(());
    let waited = Instant::now() + patience();
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if Instant::now() > waited {
            child.kill().unwrap_or(());
            child.wait().unwrap_or_default();
            panic!("the launcher ignored SIGTERM");
        }
        thread::sleep(poll());
    };
    let mut stderr = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut stderr)
        .unwrap_or(0);
    reader.join().unwrap_or(());
    let stdout = receive.recv_timeout(patience()).unwrap_or_default();
    let mut after = Vec::new();
    tree(&home, &home, &mut after);
    let state = home.join("Library/Application Support/org.mycelium.app/state");
    let state_mode = fs::metadata(&state).map(|m| m.permissions().mode() & 0o777);
    fs::remove_dir_all(&home).unwrap_or(());
    assert!(
        !running
            .iter()
            .chain(&after)
            .any(|path| path.contains(V014_FOLDER)),
        "the v0.1.4 demo's folder was created: {running:?} {after:?}"
    );
    assert!(
        !stdout.contains(V014_FOLDER) && !stderr.contains(V014_FOLDER),
        "the v0.1.4 demo's folder is named: {stdout} {stderr}"
    );
    assert!(
        announced.contains("launcher_started"),
        "no launch event: {announced:?} {stderr}"
    );
    assert!(
        running
            .iter()
            .any(|p| p == "Library/Application Support/org.mycelium.app/state/native-control.json"),
        "the running launcher's state is not in org.mycelium.app/state: {running:?}"
    );
    assert_eq!(state_mode.ok(), Some(0o700), "state root mode");
    assert!(status.success(), "{status:?} {stderr}");
}
