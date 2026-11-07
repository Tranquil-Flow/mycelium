// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit dev-WebKit/real-Metal qualification, never an installed application.
#![cfg(all(feature = "desktop-shell", debug_assertions, target_os = "macos"))]
use budgets::{BudgetId as B, Entries};
use node_service::phase::Timeline;
use serde_json::Value;
use std::{
    os::unix::process::CommandExt,
    process::{Command, Stdio},
    time::{Duration, Instant},
};
fn duration(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
#[test]
#[ignore = "explicit isolated desktop+native qualification requires local assets"]
fn frozen_chat_dom_streams_native_token_then_stop_and_quit_prove_cleanup() {
    let checkpoint = std::env::var("MYCELIUM_SLICE_CHECKPOINT").expect("explicit checkpoint");
    let tokenizer = std::env::var("MYCELIUM_SLICE_TOKENIZER").expect("explicit tokenizer");
    let evidence =
        std::path::PathBuf::from(std::env::var("MYCELIUM_SLICE_EVIDENCE").expect("fresh evidence"));
    std::fs::create_dir(&evidence).unwrap();
    node_service::native::pressure().unwrap();
    let private = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "slice-desktop-private-{}",
        node_service::service::random_id().unwrap()
    ));
    let bundle = evidence.join("Slice Development.app");
    std::fs::create_dir_all(bundle.join("Contents/MacOS")).unwrap();
    let executable = bundle.join("Contents/MacOS/mycelium-node");
    std::fs::copy(env!("CARGO_BIN_EXE_mycelium-node"), &executable).unwrap();
    std::fs::write(bundle.join("Contents/Info.plist"), b"<?xml version=\"1.0\"?><plist version=\"1.0\"><dict><key>CFBundleExecutable</key><string>mycelium-node</string><key>CFBundleIdentifier</key><string>org.mycelium.slice-development</string><key>CFBundleName</key><string>Mycelium Slice Development</string><key>CFBundlePackageType</key><string>APPL</string><key>LSMinimumSystemVersion</key><string>15.6.1</string></dict></plist>").unwrap();
    let log = std::fs::File::create(evidence.join("desktop.log")).unwrap();
    let native = evidence.join("native");
    let mut child = Command::new(&executable)
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
    let pid = child.id();
    // This probe executes a native request, not the short member reconnect
    // handshake. Preserve that request's existing L1 wall allowance.
    let begun = Instant::now();
    let deadline = begun + duration(B::MemberChosenWall);
    let deadline_ms = u64::try_from(duration(B::MemberChosenWall).as_millis()).unwrap();
    let elapsed = || u64::try_from(begun.elapsed().as_millis()).unwrap();
    let mut timeline = Timeline::default();
    let write_timeline = |timeline: &Timeline, outcome: &str| {
        std::fs::write(
            evidence.join("phase-timeline.json"),
            serde_json::to_vec_pretty(&timeline.report(outcome, deadline_ms)).unwrap(),
        )
        .unwrap();
    };
    let status = loop {
        // Observation only: the deadline and pressure guard below are unchanged.
        let observed = std::fs::read_to_string(evidence.join("desktop.log")).unwrap_or_default();
        timeline.observe(&observed, elapsed());
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        // A definitive owner/probe failure line ends the wait early; deadline and pressure
        // guard are unchanged and still apply.
        let failed = timeline.failure();
        if failed.is_some()
            || node_service::native::pressure().is_err()
            || Instant::now() >= deadline
        {
            // The unreaped owned leader pins this exact group. No saved PID or
            // name-based authority; children remain in the inherited test group.
            rustix::process::kill_process_group(
                rustix::process::Pid::from_raw(pid as i32).unwrap(),
                rustix::process::Signal::KILL,
            )
            .unwrap();
            child.wait().unwrap();
            let refusal = if failed.is_some() {
                "driver_failed"
            } else if Instant::now() >= deadline {
                "deadline"
            } else {
                "pressure"
            };
            let observed =
                std::fs::read_to_string(evidence.join("desktop.log")).unwrap_or_default();
            timeline.observe(&observed, elapsed());
            write_timeline(&timeline, refusal);
            let stall = timeline.stall();
            panic!(
                "desktop native {refusal} refusal in phase `{}` awaiting `{}` (last seen {:?}, \
                 failure {:?}); evidence retained: phase-timeline.json",
                stall.phase,
                stall.awaiting,
                stall.last_seen,
                timeline.failure()
            );
        }
        std::thread::sleep(duration(B::RegistryPoll));
    };
    let log = std::fs::read_to_string(evidence.join("desktop.log")).unwrap();
    println!("{log}");
    timeline.observe(&log, elapsed());
    write_timeline(&timeline, "exited");
    assert!(status.success(), "desktop exited without clean probe");
    for receipt in [
        "L11_WEBVIEW_INACTIVE_POLICY:Throttle",
        "L11_NATIVE_READY",
        "L11_SLICE_CHAT_DOM_TOKEN",
        "L11_SLICE_STOP_VERIFIED",
        "L11_SLICE_CHAT_DOM_TERMINAL",
        "L11_CLEANUP:Verified",
    ] {
        assert!(log.lines().any(|line| line == receipt), "missing {receipt}");
    }
    assert!(!log.contains("L11_SLICE_FAILED"));
    let result: Value =
        serde_json::from_slice(&std::fs::read(native.join("generation-1/result.json")).unwrap())
            .unwrap();
    assert_eq!(result["cancelled"], true);
    assert_eq!(result["member_reaped"], true);
    assert_eq!(result["planner_active"], 0);
    assert_eq!(result["iroh_activation_transport"], true);
    assert_eq!(result["iroh_control_transport"], "signed-node-control");
    assert_eq!(result["loopback_tcp_control"], false);
    assert_eq!(
        result["artifact"]["member"]["staged_via"],
        "signed-artifact-chunk"
    );
    assert!(!result["text"].as_str().unwrap().is_empty());
    let proof: Value = serde_json::from_slice(
        &std::fs::read(native.join("generation-1/stop-proof.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(proof["cleanup_proven"], true);
    for key in ["member_pid", "owner_leaf_pid"] {
        assert_eq!(
            Command::new("/bin/ps")
                .args([
                    "-p",
                    &result[key].as_u64().unwrap().to_string(),
                    "-o",
                    "pid="
                ])
                .output()
                .unwrap()
                .status
                .code(),
            Some(1)
        );
    }
    std::fs::remove_dir_all(private).unwrap();
    // Keep logs/native evidence, not another copy of the already pinned binary.
    std::fs::remove_dir_all(bundle).unwrap();
}
