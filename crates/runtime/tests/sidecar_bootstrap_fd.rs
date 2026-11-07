// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-HYG-03 (partial: a bootstrap descriptor is adopted from the inherited table; a closed number ends bootstrap_rejected as the release oracle).
//!
//! Seam S-L9-2: the raw `--bootstrap-fd N` of the v0.1.4 sidecar (`sidecar.rs:234-262`)
//! adopted through `node::fds::adopt_inherited_descriptor`, in a real exec'd child that
//! receives the pipe through the single spawn boundary (`confine_descriptors_except`,
//! the `pass_fds` equivalent). The child is this test binary re-executed; it prints one
//! result line (never the secret) and exits.
//!
//! Deliberate deviations from v0.1.4, both recorded by L6 and by the executed oracle
//! (`L9-evidence/r9-oracle-closed-fd.txt`): a closed or never-open number is refused with
//! `invalid_bootstrap_fd` and a non-zero exit instead of the oracle's `SIGABRT` (exit 134),
//! and descriptors 0-2 are refused instead of adopted blindly.
#![cfg(feature = "iroh-adapter")]
use budgets::{BudgetId as B, Entries};
use node::{WorkerAdapter, process::NativeChild};
use runtime::sidecar_process::{
    SECRET_BYTES, SidecarError, read_bootstrap_descriptor, spawn_sidecar, spawn_with_bootstrap,
};
use rustix::pipe::pipe;
use std::{
    io::Write,
    os::fd::{AsRawFd, RawFd},
    process::{Command, Stdio},
    time::{Duration, Instant},
};

fn budget(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
const FD: &str = "L9_BOOT_FD";
const EXPECT: &str = "L9_BOOT_EXPECT";
const TWICE: &str = "L9_BOOT_TWICE";
const OUT: &str = "L9_BOOT_OUT";
const RESULT_FILE: &str = "bootstrap-result.txt";

fn secret() -> [u8; SECRET_BYTES] {
    std::array::from_fn(|index| u8::try_from(index * 7 + 3).unwrap())
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// Child side: not a test unless launched as the bootstrap child.
#[test]
fn bootstrap_child() {
    let Some(fd) = std::env::var(FD).ok().and_then(|v| v.parse::<RawFd>().ok()) else {
        return;
    };
    let expected = std::env::var(EXPECT).unwrap_or_default();
    let twice = std::env::var_os(TWICE).is_some();
    let first = read_bootstrap_descriptor(fd);
    let echo = |line: String| {
        println!("{line}");
        // A launcher-spawned child has null stdio: its verdict is also written to its own
        // working directory (its exclusive root).
        if std::env::var_os(OUT).is_some() {
            let mut previous = std::fs::read_to_string(RESULT_FILE).unwrap_or_default();
            previous.push_str(&line);
            previous.push('\n');
            std::fs::write(RESULT_FILE, previous).unwrap();
        }
    };
    let verdict = match &first {
        Ok(secret) if hex(secret.as_bytes()) == expected => "MATCH".to_owned(),
        Ok(_) => "MISMATCH".to_owned(),
        Err(error) => format!("ERR {}", error.code()),
    };
    echo(format!("RESULT {verdict}"));
    if twice {
        let second = read_bootstrap_descriptor(fd);
        echo(format!(
            "SECOND {}",
            second.map_or_else(|error| format!("ERR {}", error.code()), |_| "OK".into())
        ));
    }
    if fd > 2 {
        // /dev/fd/N resolves only while descriptor N is open in this process.
        let open = std::fs::metadata(format!("/dev/fd/{fd}")).is_ok();
        echo(format!(
            "DESCRIPTOR {}",
            if open { "OPEN" } else { "CLOSED" }
        ));
    }
    std::process::exit(i32::from(first.is_err()) * 2);
}

struct Outcome {
    lines: Vec<String>,
    code: Option<i32>,
}
impl Outcome {
    fn line(&self, prefix: &str) -> Option<&str> {
        self.lines.iter().find_map(|l| l.strip_prefix(prefix))
    }
}

/// Run the child with `number` as its bootstrap descriptor. `pass` are parent descriptors
/// handed to it through the spawn boundary; `stdin_pipe` replaces its stdin.
fn run_child(number: RawFd, pass: &[RawFd], twice: bool, stdin: Stdio) -> Outcome {
    let mut command = Command::new(std::env::current_exe().expect("this test binary"));
    command
        .args([
            "--exact",
            "bootstrap_child",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(FD, number.to_string())
        .env(EXPECT, hex(&secret()))
        .stdin(stdin)
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    if twice {
        command.env(TWICE, "1");
    }
    node::fds::confine_descriptors_except(&mut command, pass).expect("pass list accepted");
    let output = command.output().expect("child ran to completion");
    Outcome {
        lines: String::from_utf8(output.stdout)
            .unwrap()
            .lines()
            // libtest prints "test name ... " without a newline before our line.
            .filter_map(|l| l.find("RESULT ").map(|at| &l[at..]).or(Some(l)))
            .map(str::to_owned)
            .collect(),
        code: output.status.code(),
    }
}
fn loaded_pipe(bytes: &[u8]) -> rustix::fd::OwnedFd {
    let (reader, writer) = pipe().unwrap();
    let mut writer = std::fs::File::from(writer);
    writer.write_all(bytes).unwrap();
    drop(writer);
    reader
}

#[test]
fn a_pipe_passed_through_the_spawn_boundary_is_adopted_read_and_closed() {
    let reader = loaded_pipe(&secret());
    let number = reader.as_raw_fd();
    let outcome = run_child(number, &[number], false, Stdio::null());
    assert_eq!(
        outcome.line("RESULT "),
        Some("MATCH"),
        "{:?}",
        outcome.lines
    );
    assert_eq!(outcome.line("DESCRIPTOR "), Some("CLOSED"));
    assert_eq!(outcome.code, Some(0));
}
#[test]
fn a_pipe_that_is_not_passed_is_a_closed_number_refused_not_aborted() {
    let reader = loaded_pipe(&secret());
    let number = reader.as_raw_fd();
    // Not in the keep list: the boundary confines it, the child sees a closed number.
    let outcome = run_child(number, &[], false, Stdio::null());
    assert_eq!(
        outcome.line("RESULT "),
        Some("ERR invalid_bootstrap_fd"),
        "{:?}",
        outcome.lines
    );
    // The oracle aborts here (SIGABRT, exit 134): a signal exit has no code.
    assert_eq!(outcome.code, Some(2), "refused with a status, not a signal");
}
#[test]
fn short_and_long_secrets_are_refused_with_the_oracle_code() {
    for length in [SECRET_BYTES - 1, SECRET_BYTES + 1, 0] {
        let reader = loaded_pipe(&vec![9; length]);
        let number = reader.as_raw_fd();
        let outcome = run_child(number, &[number], false, Stdio::null());
        assert_eq!(
            outcome.line("RESULT "),
            Some("ERR invalid_bootstrap_length"),
            "length {length}: {:?}",
            outcome.lines
        );
        assert_eq!(outcome.line("DESCRIPTOR "), Some("CLOSED"));
    }
}
#[test]
fn a_descriptor_that_is_not_a_pipe_is_bootstrap_not_pipe() {
    let file = std::fs::File::open("/dev/null").unwrap();
    let regular = {
        let path = std::env::temp_dir().join(format!("v02-l9-boot-{}", std::process::id()));
        std::fs::write(&path, secret()).unwrap();
        let handle = std::fs::File::open(&path).unwrap();
        std::fs::remove_file(&path).unwrap();
        handle
    };
    for handle in [&file, &regular] {
        let number = handle.as_raw_fd();
        let outcome = run_child(number, &[number], false, Stdio::null());
        assert_eq!(
            outcome.line("RESULT "),
            Some("ERR bootstrap_not_pipe"),
            "{:?}",
            outcome.lines
        );
    }
}
#[test]
fn stdio_numbers_and_negative_numbers_are_never_adopted() {
    // stdin is a valid FIFO carrying a good secret: v0.1.4 would adopt it blindly.
    let reader = loaded_pipe(&secret());
    let outcome = run_child(0, &[], false, Stdio::from(reader));
    assert_eq!(
        outcome.line("RESULT "),
        Some("ERR invalid_bootstrap_fd"),
        "{:?}",
        outcome.lines
    );
    for number in [1, 2, -1] {
        let outcome = run_child(number, &[], false, Stdio::null());
        assert_eq!(
            outcome.line("RESULT "),
            Some("ERR invalid_bootstrap_fd"),
            "number {number}: {:?}",
            outcome.lines
        );
    }
}
#[test]
fn the_same_number_is_not_adopted_twice() {
    let reader = loaded_pipe(&secret());
    let number = reader.as_raw_fd();
    let outcome = run_child(number, &[number], true, Stdio::null());
    assert_eq!(
        outcome.line("RESULT "),
        Some("MATCH"),
        "{:?}",
        outcome.lines
    );
    assert_eq!(
        outcome.line("SECOND "),
        Some("ERR invalid_bootstrap_fd"),
        "{:?}",
        outcome.lines
    );
}
#[test]
fn the_error_codes_are_the_oracles() {
    assert_eq!(
        SidecarError::InvalidBootstrapFd.code(),
        "invalid_bootstrap_fd"
    );
    assert_eq!(SidecarError::BootstrapNotPipe.code(), "bootstrap_not_pipe");
}

/// The verdict a launcher-spawned bootstrap child wrote into its own root, after the
/// child is gone and the root has been emptied and removed by the adapter.
fn launched_verdict(root: &std::path::Path, mut child: NativeChild) -> String {
    let end = Instant::now() + budget(B::WorkerStartup);
    loop {
        let result = std::fs::read_to_string(root.join(RESULT_FILE));
        if let Ok(text) = result {
            // Child is done writing once the file ends with the last line it writes.
            if text.contains("DESCRIPTOR ") {
                std::fs::remove_file(root.join(RESULT_FILE)).expect("remove the verdict file");
                loop {
                    let residue = child.observe().expect("observe the exact owned child");
                    if residue.process_gone && residue.resources_gone {
                        return text;
                    }
                    assert!(Instant::now() < end, "bootstrap child not reaped");
                    std::thread::sleep(budget(B::OwnerPoll));
                }
            }
        }
        assert!(Instant::now() < end, "bootstrap child gave no verdict");
        std::thread::sleep(budget(B::OwnerPoll));
    }
}
fn scratch_root(label: &str) -> std::path::PathBuf {
    std::env::temp_dir().join(format!("v02-l9-boot-{label}-{}", std::process::id()))
}
fn bootstrap_command() -> Command {
    let mut command = Command::new(std::env::current_exe().expect("this test binary"));
    command
        .args([
            "--exact",
            "bootstrap_child",
            "--nocapture",
            "--test-threads=1",
        ])
        .env(EXPECT, hex(&secret()))
        .env(OUT, "1");
    command
}
#[test]
fn a_launched_sidecar_receives_the_secret_through_spawn_passing_and_nothing_else() {
    let root = scratch_root("ok");
    let child = spawn_with_bootstrap(&mut bootstrap_command(), &root, &secret(), |command, fd| {
        command.env(FD, fd.to_string());
    })
    .expect("launcher spawns the child");
    let text = launched_verdict(&root, child);
    assert!(text.contains("RESULT MATCH"), "{text}");
    // The pipe's write end was closed by the launcher before the fork: the child saw EOF
    // after exactly 32 bytes (a longer or unterminated read would be invalid_bootstrap_length).
    assert!(text.contains("DESCRIPTOR CLOSED"), "{text}");
}
#[test]
fn the_production_attachment_is_the_oracles_bootstrap_fd_argument() {
    let mut command = Command::new("/usr/bin/true");
    let seen = std::cell::Cell::new(None);
    let root = scratch_root("arg");
    let mut child = spawn_with_bootstrap(&mut command, &root, &secret(), |command, fd| {
        seen.set(Some(fd));
        command.arg("--bootstrap-fd").arg(fd.to_string());
    })
    .expect("spawn");
    let fd = seen
        .get()
        .expect("attach was called with the pipe's number");
    assert!(fd > 2, "never a stdio number");
    assert_eq!(
        command
            .get_args()
            .map(|a| a.to_string_lossy().into_owned())
            .collect::<Vec<_>>(),
        ["--bootstrap-fd".to_owned(), fd.to_string()]
    );
    let end = Instant::now() + budget(B::WorkerStartup);
    loop {
        let residue = child.observe().unwrap();
        if residue.process_gone && residue.resources_gone {
            break;
        }
        assert!(Instant::now() < end);
        std::thread::sleep(budget(B::OwnerPoll));
    }
    // `spawn_sidecar` is the same thing with exactly that attachment.
    let _: fn(&mut Command, &std::path::Path, &[u8; SECRET_BYTES]) -> _ = spawn_sidecar;
}
