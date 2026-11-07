// SPDX-License-Identifier: AGPL-3.0-only
//! Proves: B-NET-02 (quick relay: the oracle's argv and scrubbed environment, the public address read from the tunnel log with an overlong line ending the scan, a second start refused); B-HYG-02 / B-FAIL-03 (stop is Ok only when the pid is gone and its directories removed; residue is never a clean stop).
//!
//! The quick-tunnel `RelayResource` over `NativeChild` (L3C gap, L9 row): the host starts the
//! tunnel client through L6's single spawn boundary and `stop` returns Ok only when the exact
//! pid is gone and every directory it owned is removed; a signal sent is not a stop. The
//! tunnel client in these tests is a fixture child (`/bin/sleep`), never the real
//! `cloudflared`. Expected values: `fixtures/relay-provider-events-oracle.json` is the output of
//! the oracle's own `provider_event` (`L9-evidence/r13-relay-probe.py`), and the argv/env are
//! `mycelium_a13_macos/public_relay.py:333-342 @ f4e366e9`.
#![cfg(unix)]
use budgets::{ClockDomain, MonoInstant};
use runtime::{
    relay::{QuickRelay, RelayResource},
    relay_process::{NativeRelay, RelayLimits, provider_origin, quick_tunnel_command},
};
use serde_json::Value;
use std::{
    ffi::OsString,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::Command,
    time::Duration,
};

fn base(label: &str) -> PathBuf {
    let root = std::env::var_os("TMPDIR").map_or_else(std::env::temp_dir, PathBuf::from);
    root.join(format!("l9relay-{label}-{}", std::process::id()))
}
/// The oracle's bounds from the registry (wait 5 s after the kill, polled at the relay poll).
fn limits() -> RelayLimits {
    RelayLimits::shipped().unwrap()
}
fn gone(pid: u32) -> bool {
    let pid = rustix::process::Pid::from_raw(i32::try_from(pid).unwrap()).unwrap();
    matches!(
        rustix::process::test_kill_process(pid),
        Err(rustix::io::Errno::SRCH)
    )
}
fn sleeper() -> Command {
    let mut command = Command::new("/bin/sleep");
    command.arg("300");
    command
}
fn now() -> MonoInstant {
    MonoInstant::new(ClockDomain(5), 0)
}

#[test]
fn the_origin_scan_equals_the_oracles_provider_event_on_every_executed_line() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../fixtures/relay-provider-events-oracle.json"
    ))
    .unwrap();
    assert_eq!(fixture["provenance"]["cases"], 18);
    for case in fixture["cases"].as_array().unwrap() {
        let line = case["line"].as_str().unwrap();
        if case.get("provider").is_some() {
            continue; // a non-cloudflare provider yields no event; the scan is cloudflare's
        }
        let expected = case["event"]["origin"].as_str().map(str::to_owned);
        assert_eq!(provider_origin(line), expected, "{line:?}");
    }
}

#[test]
fn the_tunnel_command_is_the_oracles_argv_with_the_scrubbed_environment() {
    let parent_env: Vec<(OsString, OsString)> = [
        ("PATH", "/usr/bin"),
        ("TUNNEL_TOKEN", "secret"),
        ("CLOUDFLARE_API_KEY", "secret"),
        ("CF_ACCOUNT", "secret"),
        ("NO_TLS_VERIFY", "1"),
        ("LANG", "C"),
        ("HOME", "/Users/someone"),
    ]
    .iter()
    .map(|(k, v)| (OsString::from(k), OsString::from(v)))
    .collect();
    let command = quick_tunnel_command(
        Path::new("/opt/cloudflared"),
        "http://127.0.0.1:4455",
        Path::new("/iso/home"),
        Path::new("/iso/home/relay.log"),
        parent_env,
    );
    assert_eq!(command.get_program(), "/opt/cloudflared");
    let args: Vec<String> = command
        .get_args()
        .map(|a| a.to_string_lossy().into_owned())
        .collect();
    // public_relay.py:339-341, plus the log file the spawn boundary needs (it nulls stdio).
    assert_eq!(
        args,
        [
            "--no-autoupdate",
            "tunnel",
            "--url",
            "http://127.0.0.1:4455",
            "--protocol",
            "http2",
            "--metrics",
            "127.0.0.1:0",
            "--grace-period",
            "5s",
            "--management-diagnostics=false",
            "--logfile",
            "/iso/home/relay.log",
        ]
    );
    let env: std::collections::BTreeMap<String, String> = command
        .get_envs()
        .filter_map(|(k, v)| {
            Some((
                k.to_string_lossy().into_owned(),
                v?.to_string_lossy().into_owned(),
            ))
        })
        .collect();
    assert_eq!(env.get("HOME").map(String::as_str), Some("/iso/home"));
    assert_eq!(env.get("PATH").map(String::as_str), Some("/usr/bin"));
    assert_eq!(env.get("LANG").map(String::as_str), Some("C"));
    for scrubbed in [
        "TUNNEL_TOKEN",
        "CLOUDFLARE_API_KEY",
        "CF_ACCOUNT",
        "NO_TLS_VERIFY",
    ] {
        assert!(
            !env.contains_key(scrubbed),
            "{scrubbed} reached the tunnel client"
        );
    }
}

#[test]
fn stop_is_ok_only_when_the_pid_is_gone_and_every_directory_is_removed() {
    let base = base("stop");
    let mut relay = NativeRelay::start(&mut sleeper(), &base, limits()).unwrap();
    let pid = relay.pid();
    assert!(!gone(pid), "the tunnel client runs");
    assert!(base.exists() && relay.home().exists());
    assert_eq!(
        std::fs::metadata(relay.home())
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o700
    );
    let home = relay.home().to_path_buf();
    relay.stop().unwrap();
    assert!(
        gone(pid),
        "stop returned Ok while pid {pid} still exists (signal sent is not stopped)"
    );
    assert!(!base.exists(), "the base directory outlived stop");
    assert!(!home.exists(), "the isolated home outlived stop");
    // Stopping again is a no-op, not an error and not a second kill.
    relay.stop().unwrap();
}

#[test]
fn a_child_that_already_exited_is_stopped_and_cleaned() {
    let base = base("exited");
    let mut command = Command::new("/usr/bin/true");
    let mut relay = NativeRelay::start(&mut command, &base, limits()).unwrap();
    let pid = relay.pid();
    // Give the instantly-exiting child time to exit (one relay poll interval).
    std::thread::sleep(Duration::from_millis(
        budgets::BudgetId::RelayPoll
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis(),
    ));
    relay.stop().unwrap();
    assert!(gone(pid));
    assert!(!base.exists());
}

#[test]
fn residue_in_the_exclusive_root_is_never_reported_as_a_clean_stop() {
    let base = base("residue");
    let mut relay = NativeRelay::start(&mut sleeper(), &base, limits()).unwrap();
    let pid = relay.pid();
    let intruder = base.join("relay").join("left-behind");
    std::fs::write(&intruder, b"x").unwrap();
    let refused = relay.stop();
    assert!(refused.is_err(), "residue accepted as a clean stop");
    assert!(
        gone(pid),
        "the process is stopped even though the resource is not clean"
    );
    assert!(
        intruder.exists(),
        "foreign residue is reported, not deleted"
    );
    // Once the residue is removed by its owner, the same stop proves the rest.
    std::fs::remove_file(&intruder).unwrap();
    relay.stop().unwrap();
    assert!(!base.exists());
}

#[test]
fn the_quick_relay_stop_is_the_resources_proof() {
    let base = base("quick");
    let relay = NativeRelay::start(&mut sleeper(), &base, limits()).unwrap();
    let pid = relay.pid();
    let mut quick =
        QuickRelay::new(now(), "https://x.trycloudflare.com".to_owned(), relay).unwrap();
    assert!(!quick.clean());
    quick.stop().unwrap();
    assert!(quick.clean());
    assert!(gone(pid) && !base.exists());
}

#[test]
fn a_second_start_over_an_existing_base_is_refused_before_anything_runs() {
    let base = base("exists");
    std::fs::create_dir_all(&base).unwrap();
    let refused = NativeRelay::start(&mut sleeper(), &base, limits());
    assert!(refused.is_err());
    std::fs::remove_dir(&base).unwrap();
}

#[test]
fn the_origin_is_read_from_the_tunnel_log_and_an_overlong_line_ends_the_scan() {
    let dir = base("log");
    std::fs::create_dir(&dir).unwrap();
    let log = dir.join("relay.log");
    assert_eq!(
        runtime::relay_process::origin_from_log(&log, 65_536),
        Ok(None)
    );
    std::fs::write(
        &log,
        "starting\nINF |  https://tidy-quartz-9.trycloudflare.com  |\nlater https://other.trycloudflare.com\n",
    )
    .unwrap();
    assert_eq!(
        runtime::relay_process::origin_from_log(&log, 65_536),
        Ok(Some("https://tidy-quartz-9.trycloudflare.com".to_owned()))
    );
    std::fs::write(&log, "x".repeat(70_000)).unwrap();
    assert_eq!(
        runtime::relay_process::origin_from_log(&log, 65_536),
        Err("relay_log_line_too_long")
    );
    std::fs::remove_dir_all(&dir).unwrap();
}
