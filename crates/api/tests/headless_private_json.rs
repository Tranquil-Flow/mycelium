// SPDX-License-Identifier: AGPL-3.0-only
//! The headless launcher reads its private state through the SAME registry-backed cap as the
//! oracle (`authority._MAX_PRIVATE_JSON_BYTES` = 256 KiB, authority.py:96 @ f4e366e9): a document
//! larger than 64 KiB and up to the cap is accepted; one byte more is `authority_state_invalid`
//! (the oracle's `_load_private_json` refuses `st_size > max_bytes`), exactly as `private_state::load`.
use budgets::{BudgetId, Entries};
use std::{
    fs,
    io::{BufRead, BufReader, Read},
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::mpsc,
    thread,
    time::Duration,
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
fn cap() -> u64 {
    BudgetId::PyMyceliumA13MacosAuthorityMaxPrivateJsonBytesL96
        .bytes()
        .unwrap()
        .get()
}
fn patience() -> Duration {
    let millis = BudgetId::ModelSelection
        .span(Entries::new(0))
        .unwrap()
        .millis();
    Duration::from_millis(millis * 4)
}
/// A canonical (sorted keys, no whitespace) 0600 object of exactly `size` bytes.
fn state_root(label: &str, size: u64) -> PathBuf {
    // `_private_root` refuses a non-canonical state root (macOS `TMPDIR` is under the `/var` symlink).
    let root = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("l10r11-cap-{}-{label}", std::process::id()));
    fs::remove_dir_all(&root).unwrap_or(());
    fs::create_dir_all(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let frame = r#"{"pad":"","swarm_id":"swarm-cap"}"#.len() as u64;
    let pad = "x".repeat(usize::try_from(size - frame).unwrap());
    let document = format!(r#"{{"pad":"{pad}","swarm_id":"swarm-cap"}}"#);
    assert_eq!(document.len() as u64, size);
    let path = root.join("owner-swarm.json");
    fs::write(&path, document).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    root
}
fn launch(root: &Path) -> Child {
    Command::new(profile_dir().join("examples/headless"))
        .args(["--headless", "--offline", "--state-root"])
        .arg(root)
        .env("HOME", root)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("the headless example was built")
}
/// First stdout line (the launch event) or the exit with its stderr, within the patience.
enum Outcome {
    Started(String),
    Failed(Option<i32>, String),
}
fn outcome(mut child: Child) -> Outcome {
    let stdout = child.stdout.take().unwrap();
    let (send, receive) = mpsc::channel();
    let reader = thread::spawn(move || {
        let mut line = String::new();
        BufReader::new(stdout).read_line(&mut line).unwrap_or(0);
        send.send(line).unwrap_or(());
    });
    let line = receive.recv_timeout(patience()).unwrap_or_default();
    if line.contains("launcher_started") {
        child.kill().unwrap_or(());
        child.wait().unwrap_or_default();
        reader.join().unwrap_or(());
        return Outcome::Started(line);
    }
    let mut stderr = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut stderr)
        .unwrap_or(0);
    let status = child.wait().unwrap();
    reader.join().unwrap_or(());
    Outcome::Failed(status.code(), stderr)
}

#[test]
fn a_document_between_64_kib_and_the_cap_is_accepted() {
    build();
    let size = cap() / 4 + 1; // one byte over 64 KiB
    for (label, bytes) in [("over64k", size), ("atcap", cap())] {
        let root = state_root(label, bytes);
        match outcome(launch(&root)) {
            Outcome::Started(line) => assert!(line.contains("rust-headless"), "{line}"),
            Outcome::Failed(code, stderr) => panic!("{label}: refused ({code:?}): {stderr}"),
        }
        fs::remove_dir_all(&root).unwrap_or(());
    }
}

#[test]
fn a_document_over_the_cap_is_refused_as_authority_state_invalid() {
    build();
    let root = state_root("overcap", cap() + 1);
    match outcome(launch(&root)) {
        Outcome::Started(line) => panic!("accepted an oversize document: {line}"),
        Outcome::Failed(code, stderr) => {
            assert_eq!(code, Some(3), "{stderr}");
            assert!(stderr.contains("authority_state_invalid"), "{stderr}");
        }
    }
    fs::remove_dir_all(&root).unwrap_or(());
}
