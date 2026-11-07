// SPDX-License-Identifier: AGPL-3.0-only
//! Round 7 qualification of v0.1.4's prompt worker-death detection on the desktop path: the
//! frozen chat page drives the real native owner in the debug development shell; the exact
//! pid of one native worker is killed mid-job and the job must fail promptly with v0.1.4's
//! `worker_native_exited`, the probe's Quit must verify and the shell exit 0, and no worker may
//! survive. Development shell, not an installed app. Explicit isolated run only.
#![cfg(all(feature = "desktop-shell", debug_assertions, target_os = "macos"))]
#[path = "support/death.rs"]
mod death;
use budgets::BudgetId as B;
use death::{Case, Kill, Owner, Role, assert_prompt_and_clean, duration, inject};
use serde_json::Value;
use std::{
    os::unix::process::CommandExt,
    path::PathBuf,
    process::{Command, Stdio},
    time::Instant,
};

fn run(case: &Case) {
    let checkpoint = std::env::var("MYCELIUM_SLICE_CHECKPOINT").expect("explicit checkpoint");
    let tokenizer = std::env::var("MYCELIUM_SLICE_TOKENIZER").expect("explicit tokenizer");
    let evidence = PathBuf::from(std::env::var("MYCELIUM_SLICE_EVIDENCE").expect("fresh evidence"))
        .join(case.name);
    std::fs::create_dir_all(&evidence).unwrap();
    node_service::native::pressure().unwrap();
    let private = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "slice-desktop-death-{}",
        node_service::service::random_id().unwrap()
    ));
    let bundle = evidence.join("Slice Development.app");
    std::fs::create_dir_all(bundle.join("Contents/MacOS")).unwrap();
    let executable = bundle.join("Contents/MacOS/mycelium-node");
    std::fs::copy(env!("CARGO_BIN_EXE_mycelium-node"), &executable).unwrap();
    std::fs::write(bundle.join("Contents/Info.plist"), b"<?xml version=\"1.0\"?><plist version=\"1.0\"><dict><key>CFBundleExecutable</key><string>mycelium-node</string><key>CFBundleIdentifier</key><string>org.mycelium.slice-development</string><key>CFBundleName</key><string>Mycelium Slice Development</string><key>CFBundlePackageType</key><string>APPL</string><key>LSMinimumSystemVersion</key><string>15.6.1</string></dict></plist>").unwrap();
    let log_path = evidence.join("desktop.log");
    let log = std::fs::File::create(&log_path).unwrap();
    let native = evidence.join("native");
    let child = Command::new(&executable)
        .arg("desktop-probe")
        .args([&checkpoint, &tokenizer])
        .arg(&native)
        .arg(&private)
        .arg(["state", "send", "events", "stop"].len().to_string())
        .arg("--approve-local-member")
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(log.try_clone().unwrap())
        .stderr(log)
        .spawn()
        .unwrap();
    let mut owner = Owner {
        child,
        log: log_path.clone(),
        begun: Instant::now(),
        workers: Vec::new(),
        natives: Vec::new(),
    };
    let outcome = inject(&mut owner, case, duration(B::MemberChosenWall), true);
    let report = serde_json::json!({
        "case": case.name, "after": case.after, "role": case.role.node(),
        "killed_pid": outcome.killed, "code": outcome.code,
        "killed_to_failure_ms": outcome.failure_after.as_millis(),
        "failure_to_exit_ms": outcome.exit_after_failure.as_millis(),
        "owner_exit_success": outcome.exit_success, "workers": owner.workers,
        "shell_cleanup_verdict": death::cleanup_line(&log_path),
    });
    std::fs::write(
        evidence.join("injected-death.json"),
        serde_json::to_vec_pretty(&report).unwrap(),
    )
    .unwrap();
    assert_prompt_and_clean(&mut owner, case, &outcome, true);
    // v0.1.4 (executed oracle FX-SLICE1-post-worker-death): a worker death never ends the
    // application by itself, but a Quit after it exits 0 once the reaped worker's cleanup is
    // proven. The probe requests that Quit; the shell must verify and exit successfully.
    assert_eq!(
        death::cleanup_line(&log_path).as_deref(),
        Some("L11_CLEANUP:Verified"),
        "case {}: the reaped generation must verify",
        case.name
    );
    assert!(
        outcome.exit_success,
        "case {}: the shell exits 0 after Quit",
        case.name
    );
    let diagnostic: Value = serde_json::from_slice(
        &std::fs::read(native.join("generation-1/native-exit.private.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(diagnostic["error_code"], "worker_native_exited");
    assert_eq!(diagnostic["node_id"], case.role.node());
    assert_eq!(diagnostic["pid"], outcome.killed);
    // The supervisor may reap the worker before the watch looks: then the cause is unknown.
    assert!(
        diagnostic["signal"] == 9
            || (diagnostic["signal"].is_null() && diagnostic["exit_status"].is_null()),
        "{diagnostic}"
    );
    if case.kill == Kill::Worker {
        let settled: Value = serde_json::from_slice(
            &std::fs::read(native.join("generation-1/failed-settle.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(settled["terminal"], "failed", "{settled}");
        assert_eq!(settled["signed_cleanup_quorum"], true);
        assert_eq!(settled["died"]["pid"], outcome.killed);
        assert!(!native.join("generation-1/local-reap.json").exists());
    }
    assert!(
        !private.join("member/staging/model.safetensors").exists(),
        "case {}: staged model copy survived the failed run",
        case.name
    );
    drop(owner);
    std::fs::remove_dir_all(private).unwrap();
    std::fs::remove_dir_all(bundle).unwrap();
}

#[test]
#[ignore = "explicit isolated desktop+native qualification requires local assets"]
fn desktop_member_killed_before_ready_fails_the_job_promptly() {
    run(&Case {
        name: "desktop-member-early",
        kill: Kill::Agent,
        after: "member_registered",
        role: Role::Member,
        workers: 1,
    });
}
#[test]
#[ignore = "explicit isolated desktop+native qualification requires local assets"]
fn desktop_member_worker_killed_before_the_first_token_settles_failed_and_the_shell_exits_zero() {
    // RU-020: the member AGENT survives its dead native worker and signs; the run settles as
    // Failed from the signed quorum, so the shell's Quit verifies without any local-reap fallback.
    run(&Case {
        name: "desktop-member-worker",
        kill: Kill::Worker,
        after: "token_grant",
        role: Role::Member,
        workers: 2,
    });
}
#[test]
#[ignore = "explicit isolated desktop+native qualification requires local assets"]
fn desktop_owner_leaf_killed_after_member_ready_fails_the_job_promptly() {
    run(&Case {
        name: "desktop-owner-leaf",
        kill: Kill::Agent,
        after: "member_ready",
        role: Role::OwnerLeaf,
        workers: 2,
    });
}
