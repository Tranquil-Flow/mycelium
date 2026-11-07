// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NET-09 (the shipped binary's exit codes, stdout and stderr events equal the executed oracle on 36 argv/bootstrap/key-file cases; relay modes and artifact configuration are served).
//!
//! The Rust sidecar entry point against the v0.1.4 sidecar binary's recorded behaviour:
//! `fixtures/sidecar-cli-oracle.json` holds the exit code, stdout and stderr events of the REAL
//! oracle (`mycelium-iroh-sidecar @ f4e366e9`, production build) for 36 argv / bootstrap /
//! key-file cases (probe `L9-evidence/r13-cli-probe.rb`, executed). Two layers:
//! `parse_arguments` accepts/refuses exactly what clap does, and the `mycelium-rs-sidecar`
//! binary, run as an exact child with the same setup, exits and logs identically.
//!
//! A closed `--bootstrap-fd` is NOT a divergence: the SHIPPED (release) oracle ends it with
//! `bootstrap_rejected`, exit 1 (`fixtures/sidecar-artifact-oracle.json`, case
//! `closed_bootstrap_fd`, executed with the release profile). The earlier "the oracle aborts
//! (SIGABRT)" came from a debug-profile build, whose std aborts on an invalid owned descriptor.
//! Not in the table (served since the registry has `SidecarReadyAddressWindow` and
//! `ArtifactMaxFence`, seam SE-L9-3): the non-local-only network modes (against the executed
//! relay-mode oracle, under a deny-outbound Seatbelt profile) and a valid `--artifact-*`
//! configuration (against the executed `fresh_state_dir` case).
#![cfg(all(feature = "iroh-adapter", unix))]
use runtime::sidecar_main::{ArgError, parse_arguments};
use serde_json::Value;
use std::{
    io::{Read, Write},
    os::{fd::AsRawFd, unix::fs::PermissionsExt},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    time::{Duration, Instant},
};

include!("support/fork_lock.rs");

fn fixture() -> Value {
    serde_json::from_str(include_str!("../fixtures/sidecar-cli-oracle.json")).unwrap()
}
fn unhex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}
fn argv(case: &Value, dir: &Path, fd: i32) -> Vec<String> {
    case["argv_shape"]
        .as_array()
        .unwrap()
        .iter()
        .map(|arg| {
            let arg = arg.as_str().unwrap();
            if arg == "<fd>" {
                fd.to_string()
            } else {
                arg.replace("<dir>", dir.to_str().unwrap())
            }
        })
        .collect()
}
fn scratch(label: &str) -> PathBuf {
    let base = std::env::var_os("TMPDIR").map_or_else(std::env::temp_dir, PathBuf::from);
    let dir = base.join(format!("l9cli-{label}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
    dir
}
fn exit_of(case: &Value) -> i64 {
    case["exit"].as_i64().unwrap()
}

#[test]
fn the_fixture_is_the_executed_oracle_table() {
    let f = fixture();
    assert!(
        f["provenance"]["oracle"]
            .as_str()
            .unwrap()
            .contains("f4e366e98a1f705dfaf182897044500d290dbd7f")
    );
    assert_eq!(f["provenance"]["cases"], 36);
    assert_eq!(f["cases"].as_array().unwrap().len(), 36);
}

#[test]
fn parse_arguments_refuses_what_the_oracle_exits_2_on_and_accepts_the_rest() {
    let f = fixture();
    let dir = Path::new("/nonexistent/l9");
    for case in f["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let parsed = parse_arguments(&argv(case, dir, 7));
        match exit_of(case) {
            2 => assert!(
                matches!(parsed, Err(ArgError::Usage(_))),
                "{name}: the oracle exits 2, got {parsed:?}"
            ),
            0 => assert!(
                matches!(parsed, Err(ArgError::Informational(_))),
                "{name}: the oracle exits 0 after printing, got {parsed:?}"
            ),
            _ => assert!(
                parsed.is_ok(),
                "{name}: the oracle parses this and fails later, got {parsed:?}"
            ),
        }
    }
}

fn spawn(binary: &Path, dir: &Path, case: &Value) -> (Child, Option<i32>) {
    spawn_under(&[], binary, dir, case)
}
/// `spawn`, with `prefix` (a program and its arguments) exec-ing the binary, as `sandbox-exec`
/// does: the confined descriptor table passes straight through its `execv`.
fn spawn_under(prefix: &[&str], binary: &Path, dir: &Path, case: &Value) -> (Child, Option<i32>) {
    let mut command = match prefix.split_first() {
        Some((program, rest)) => {
            let mut command = Command::new(program);
            command.args(rest).arg(binary);
            command
        }
        None => Command::new(binary),
    };
    command
        .current_dir(dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    match case["bootstrap_hex"].as_str() {
        Some(hex) => {
            let (reader, writer) = rustix::pipe::pipe().unwrap();
            let mut writer = std::fs::File::from(writer);
            writer.write_all(&unhex(hex)).unwrap();
            drop(writer);
            let number = reader.as_raw_fd();
            command.args(argv(case, dir, number));
            node::fds::confine_descriptors_except(&mut command, &[number]).unwrap();
            let child = {
                let _boundary = fixture_spawn_boundary();
                command.spawn().unwrap()
            };
            drop(reader);
            (child, Some(number))
        }
        None => {
            command.args(argv(case, dir, 3));
            let _boundary = fixture_spawn_boundary();
            (command.spawn().unwrap(), None)
        }
    }
}
/// Waits for an exact child up to `limit`; kills and reaps it past that.
fn finish(mut child: Child, limit: Duration) -> (Option<i32>, String, String) {
    let started = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break Some(status);
        }
        if started.elapsed() > limit {
            child.kill().unwrap();
            child.wait().unwrap();
            break None;
        }
        std::thread::sleep(Duration::from_millis(
            budgets::BudgetId::RegistryPoll
                .span(budgets::Entries::ZERO)
                .unwrap()
                .millis(),
        ));
    };
    let mut out = String::new();
    child
        .stdout
        .take()
        .unwrap()
        .read_to_string(&mut out)
        .unwrap();
    let mut err = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut err)
        .unwrap();
    (status.and_then(|s| s.code()), out, err)
}
fn events(stderr: &str) -> Vec<String> {
    stderr
        .lines()
        .map(str::trim)
        .filter(|line| line.starts_with('{'))
        .map(str::to_owned)
        .collect()
}
/// How long an exact child may take: the router-close budget x4.
fn limit() -> Duration {
    Duration::from_millis(
        budgets::BudgetId::RouterClose
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis()
            * 4,
    )
}
fn rust_binary() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_mycelium-rs-sidecar"))
}

#[test]
fn the_binary_exits_and_logs_like_the_oracle_on_every_recorded_case() {
    let f = fixture();
    let limit = limit();
    let mut checked = 0;
    for case in f["cases"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let dir = scratch(name);
        for file in case["files"].as_array().unwrap() {
            let path = dir.join(file["name"].as_str().unwrap());
            std::fs::write(&path, unhex(file["content_hex"].as_str().unwrap())).unwrap();
            std::fs::set_permissions(
                &path,
                std::fs::Permissions::from_mode(file["mode"].as_u64().unwrap() as u32),
            )
            .unwrap();
        }
        let (child, _) = spawn(&rust_binary(), &dir, case);
        let (code, out, err) = finish(child, limit);
        assert_eq!(
            code.map(i64::from),
            Some(exit_of(case)),
            "{name}: exit; stderr {err:?}"
        );
        let expected: Vec<&str> = case["events"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e.as_str().unwrap())
            .collect();
        // Help and version print differently (the program name); every other case must match.
        if !matches!(name, "help" | "help_short" | "version") {
            assert_eq!(events(&err), expected, "{name}: stderr events");
            assert_eq!(out, case["stdout"].as_str().unwrap(), "{name}: stdout");
        } else {
            assert!(!out.is_empty(), "{name}: prints something");
        }
        std::fs::remove_dir_all(&dir).unwrap();
        checked += 1;
    }
    assert_eq!(checked, 36);
}

#[test]
fn a_closed_bootstrap_descriptor_ends_bootstrap_rejected_exit_1_as_the_release_oracle() {
    let oracle: Value =
        serde_json::from_str(include_str!("../fixtures/sidecar-artifact-oracle.json")).unwrap();
    let case = oracle["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == "closed_bootstrap_fd")
        .expect("the executed closed-descriptor case");
    assert!(
        oracle["provenance"]["oracle"]
            .as_str()
            .unwrap()
            .contains("RELEASE")
    );
    assert_eq!(case["exit"], 1);
    assert_eq!(
        case["events"],
        serde_json::json!(["{\"event\":\"bootstrap_rejected\"}"])
    );
    assert_eq!(case["socket_left_behind"], false);
    let dir = scratch("closed-fd");
    let mut command = Command::new(rust_binary());
    command
        .args(["--uds"])
        .arg(dir.join("s"))
        .args(["--bootstrap-fd", "97", "--local-only"])
        .current_dir(&dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let child = {
        let _boundary = fixture_spawn_boundary();
        command.spawn().unwrap()
    };
    let (code, out, err) = finish(child, limit());
    assert_eq!(code.map(i64::from), case["exit"].as_i64(), "{err}");
    assert!(out.is_empty());
    let expected: Vec<&str> = case["events"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e.as_str().unwrap())
        .collect();
    assert_eq!(events(&err), expected);
    assert!(!dir.join("s").exists(), "no socket was left behind");
    std::fs::remove_dir_all(&dir).unwrap();
}

/// Starts the binary and returns it with its first stdout line (the `ready` line), then stops it
/// with SIGINT as the oracle probes did; returns (exit code, stdout rest, stderr).
fn run_to_ready(child: Child, limit: Duration) -> (Value, Option<i32>, String, String) {
    let mut child = child;
    let stdout = child.stdout.take().unwrap();
    let (sender, lines) = std::sync::mpsc::channel::<String>();
    let reader = std::thread::spawn(move || {
        let mut text = String::new();
        let mut reader = std::io::BufReader::new(stdout);
        let got_line = std::io::BufRead::read_line(&mut reader, &mut text).is_ok();
        if got_line && sender.send(text).is_ok() {
            // Drain the rest so the child never blocks on a full pipe.
            let mut rest = String::new();
            assert!(reader.read_to_string(&mut rest).is_ok());
        }
    });
    let ready = lines.recv_timeout(limit).ok();
    let pid = rustix::process::Pid::from_raw(i32::try_from(child.id()).unwrap()).unwrap();
    rustix::process::kill_process(pid, rustix::process::Signal::INT).unwrap();
    let started = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break Some(status);
        }
        if started.elapsed() > limit {
            child.kill().unwrap();
            child.wait().unwrap();
            break None;
        }
        std::thread::sleep(Duration::from_millis(
            budgets::BudgetId::RegistryPoll
                .span(budgets::Entries::ZERO)
                .unwrap()
                .millis(),
        ));
    };
    assert!(reader.join().is_ok());
    let mut err = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut err)
        .unwrap();
    let line = ready.expect("a ready line before the limit");
    (
        serde_json::from_str(line.trim()).unwrap(),
        status.and_then(|s| s.code()),
        String::new(),
        err,
    )
}

/// Proves: B-NET-09 (production-relay and `--force-relay` modes are served by the shipped binary;
/// the executed oracle waits the ready-address window with no reachable relay, logs
/// `ready_address_incomplete` and still publishes `ready`).
#[test]
fn the_binary_serves_the_relay_modes_as_the_executed_oracle() {
    let relay: Value =
        serde_json::from_str(include_str!("../fixtures/sidecar-relay-oracle.json")).unwrap();
    let profile = relay["provenance"]["sandbox_profile"].as_str().unwrap();
    let limit = limit();
    let dir = scratch("relay-modes");
    let profile_path = dir.join("deny.sb");
    std::fs::write(&profile_path, profile).unwrap();
    std::fs::write(dir.join("key"), [0x44_u8; 32]).unwrap();
    std::fs::set_permissions(dir.join("key"), std::fs::Permissions::from_mode(0o600)).unwrap();
    for case in relay["cases"].as_array().unwrap() {
        let flags: Vec<&str> = case["flags"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| f.as_str().unwrap())
            .collect();
        let mut argv_shape = vec![
            Value::from("--uds"),
            Value::from("<dir>/s"),
            Value::from("--bootstrap-fd"),
            Value::from("<fd>"),
            Value::from("--endpoint-secret-file"),
            Value::from("<dir>/key"),
        ];
        argv_shape.extend(flags.iter().map(|f| Value::from(*f)));
        let shaped =
            serde_json::json!({"argv_shape": argv_shape, "bootstrap_hex": "4f".repeat(32)});
        let (child, _) = spawn_under(
            &[
                "/usr/bin/sandbox-exec",
                "-f",
                profile_path.to_str().unwrap(),
            ],
            &rust_binary(),
            &dir,
            &shaped,
        );
        let started = Instant::now();
        let (ready, code, _, err) = run_to_ready(child, limit);
        let name = format!("{flags:?}");
        assert!(
            started.elapsed()
                >= Duration::from_millis(
                    budgets::BudgetId::SidecarReadyAddressWindow
                        .span(budgets::Entries::ZERO)
                        .unwrap()
                        .millis()
                ),
            "{name}: the ready line waits the 3 s window of the oracle ({:?})",
            started.elapsed()
        );
        assert_eq!(code, Some(0), "{name}: a stop is a clean exit; {err}");
        let expected: Vec<&str> = case["events"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e.as_str().unwrap())
            .collect();
        assert_eq!(events(&err), expected, "{name}: stderr events");
        assert_eq!(ready["event"], "ready", "{name}: {ready}");
        assert_eq!(ready["alpn"], case["ready_alpn"], "{name}: alpn");
        assert_eq!(
            ready["endpoint_id"], case["endpoint_id"],
            "{name}: fixed secret"
        );
        let addrs = ready["endpoint_addr"]["addrs"].as_array().unwrap();
        if flags.is_empty() {
            assert!(!addrs.is_empty(), "{name}: direct addresses are published");
        } else {
            assert_eq!(
                addrs.len() as u64,
                case["addr_count"].as_u64().unwrap(),
                "{name}"
            );
        }
        assert!(
            addrs
                .iter()
                .all(|a| !a["Ip"].as_str().unwrap_or("").starts_with("127.")),
            "{name}: no loopback address (oracle any_loopback = {})",
            case["any_loopback"]
        );
        assert!(!dir.join("s").exists(), "{name}: no socket was left behind");
    }
    std::fs::remove_dir_all(&dir).unwrap();
}

/// Proves: B-NET-09 (a valid `--artifact-*` configuration is served by the shipped binary: the
/// executed oracle logs `address_ready` then `artifact_source_ready`, publishes `ready` and
/// creates the durable ledger with only its genesis header, `File::create` mode; case `fresh_state_dir`).
#[test]
fn the_binary_serves_a_valid_artifact_configuration_as_the_executed_oracle() {
    let oracle: Value =
        serde_json::from_str(include_str!("../fixtures/sidecar-artifact-oracle.json")).unwrap();
    let case = oracle["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == "fresh_state_dir")
        .expect("the executed fresh-state case");
    let limit = limit();
    let dir = scratch("artifact-served");
    std::fs::write(dir.join("seed"), b"{\"protocol\":\"x\",\"key\":\"y\"}").unwrap();
    std::fs::set_permissions(dir.join("seed"), std::fs::Permissions::from_mode(0o600)).unwrap();
    std::fs::write(dir.join("sig"), [0x22_u8; 32]).unwrap();
    std::fs::set_permissions(dir.join("sig"), std::fs::Permissions::from_mode(0o600)).unwrap();
    std::fs::create_dir(dir.join("objects")).unwrap();
    let shaped = serde_json::json!({
        "argv_shape": ["--uds", "<dir>/s", "--bootstrap-fd", "<fd>", "--local-only",
            "--artifact-objects", "<dir>/objects", "--artifact-seed-key", "<dir>/seed",
            "--artifact-state", "<dir>/st", "--artifact-signing-key-file", "<dir>/sig"],
        "bootstrap_hex": "4f".repeat(32),
    });
    let wall = || {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis()
    };
    let before = wall();
    let (child, _) = spawn(&rust_binary(), &dir, &shaped);
    let (ready, code, _, err) = run_to_ready(child, limit);
    let after = wall();
    assert_eq!(code, Some(0), "{err}");
    assert_eq!(ready["event"], "ready");
    let got = events(&err);
    let want: Vec<String> = case["events"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e.as_str().unwrap().to_owned())
        .collect();
    assert_eq!(got.len(), want.len(), "{got:?}");
    assert_eq!(got[0], want[0], "address_ready first");
    // The endpoint id is random without a secret file: compare the event with it masked.
    let masked = |line: &str| {
        let mut value: Value = serde_json::from_str(line).unwrap();
        value["endpoint_id"] = Value::Null;
        value
    };
    assert_eq!(masked(&got[1]), masked(&want[1]), "artifact_source_ready");
    let ledger = std::fs::read_to_string(dir.join("st").join("source-ledger.jsonl")).unwrap();
    // A fresh ledger's genesis is its creation time (the oracle: inside its own window).
    let created: u128 = genesis(&ledger).parse().unwrap();
    assert!(
        (before..=after).contains(&created),
        "{created} not in {before}..{after}"
    );
    let oracle_ledger = case["ledger_after"].as_str().unwrap();
    assert_eq!(
        ledger,
        oracle_ledger.replace(&genesis(oracle_ledger), &created.to_string()),
        "the ledger is the oracle's header, byte for byte"
    );
    // The oracle creates the ledger with `File::create(&tmp)` (`artifact_net.rs:174`), no explicit
    // mode: a reference file created the same way here has the mode the umask gives it. (The
    // fixture's `mode` field is the probe's INPUT mode for a pre-existing ledger, not an output.)
    let reference = dir.join("reference");
    std::fs::File::create(&reference).unwrap();
    let mode_of = |path: &Path| std::fs::metadata(path).unwrap().permissions().mode() & 0o777;
    assert_eq!(
        mode_of(&dir.join("st").join("source-ledger.jsonl")),
        mode_of(&reference),
        "the ledger is created without an explicit mode, as File::create does"
    );
    assert!(!dir.join("s").exists(), "no socket was left behind");
    std::fs::remove_dir_all(&dir).unwrap();
}
/// The genesis a fresh ledger records is its creation time: take it from the header.
fn genesis(ledger: &str) -> String {
    let header: Value = serde_json::from_str(ledger.lines().next().unwrap()).unwrap();
    header["genesis_unix_ms"].to_string()
}
