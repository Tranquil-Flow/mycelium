// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit REAL-chat gate over a scratch `qualification-probe` bundle (never in the default run):
//! the installed bundle's own executable serves the frozen chat page over SLICE-1's node service
//! (real Iroh control, real Metal on the bundled macOS-14 MLX libraries); the test-only script
//! types the oracle prompt, waits for a native token in the page DOM, presses Stop, waits for the
//! terminal state; then the harness sends an Apple Event quit to the exact child pid and checks
//! the cleanup receipts and that every owned process is gone.
//!
//! ```text
//! MYCELIUM_PROBE_BUNDLE_APP="/path/probe/Mycelium A13 Demo.app" \
//! MYCELIUM_MODEL_SNAPSHOT="<dir with model.safetensors and tokenizer.json>" \
//!   cargo test --release --offline --locked -j 2 -p desktop --features tauri-shell \
//!   --test bundle_chat -- --ignored --nocapture --test-threads=1
//! ```
//!
//! The model files are APFS-cloned (`cp -c`, read-only on the source) into the SCRATCH state
//! root; the real state root, `/Applications` and `~/Applications` are unreachable. The memory
//! pressure level is checked before launch and at every poll; any level but 1 kills the owned
//! process group (our own unreaped child, exact pid).
#![cfg(all(feature = "tauri-shell", target_os = "macos"))]
#[path = "support/chat_answer.rs"]
mod chat_answer;
mod support;
use budgets::BudgetId;
use std::{
    io::{BufRead, BufReader},
    os::unix::process::CommandExt,
    path::PathBuf,
    process::{Command, Stdio},
    sync::mpsc,
    time::Instant,
};
use support::{budget, install_scratch, pressure_level, send_quit};

type Lines = Vec<(u128, String)>;
const REVISION: &str = "7ae557604adf67be50417f59c2c2f167def9a775";

fn alive(pid: u64) -> bool {
    Command::new("/bin/ps")
        .args(["-p", &pid.to_string(), "-o", "pid="])
        .output()
        .unwrap()
        .status
        .success()
}
fn children_of(parent: u32) -> Vec<u32> {
    let out = Command::new("/bin/ps")
        .args(["-A", "-o", "pid=,ppid="])
        .output()
        .unwrap();
    String::from_utf8(out.stdout)
        .unwrap()
        .lines()
        .filter_map(|l| {
            let mut f = l.split_whitespace().map(|v| v.parse::<u32>().ok());
            match (f.next().flatten(), f.next().flatten()) {
                (Some(pid), Some(ppid)) if ppid == parent => Some(pid),
                _ => None,
            }
        })
        .collect()
}

#[test]
#[ignore = "explicit GUI+GPU gate: runs the named scratch probe bundle with real Metal"]
fn installed_bundle_streams_a_native_token_into_the_page_then_stop_and_apple_event_quit_clean_up() {
    let app = PathBuf::from(std::env::var("MYCELIUM_PROBE_BUNDLE_APP").expect("probe bundle"));
    assert!(
        app.starts_with(std::env::var("HOME").unwrap())
            && app.to_string_lossy().contains("/.hermes/cache/scratch/"),
        "only a lane scratch bundle may be launched: {}",
        app.display()
    );
    let snapshot = PathBuf::from(std::env::var("MYCELIUM_MODEL_SNAPSHOT").expect("snapshot dir"));
    assert_eq!(pressure_level(), "1", "memory safety gate before launch");

    let (fake, fake_home, executable) = install_scratch(&app, "v02-l11-chat");
    let state = fake_home.join("Library/Application Support/Mycelium A13 macOS Demo");
    let target = state
        .join(format!("model-source-{}", &REVISION[..12]))
        .join("models--Qwen--Qwen2.5-0.5B-Instruct/snapshots")
        .join(REVISION);
    std::fs::create_dir_all(&target).unwrap();
    for file in ["model.safetensors", "tokenizer.json"] {
        assert!(
            Command::new("/bin/cp")
                .args(["-c"])
                .arg(snapshot.join(file))
                .arg(target.join(file))
                .status()
                .unwrap()
                .success(),
            "clone {file}"
        );
    }
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&state, std::fs::Permissions::from_mode(0o700)).unwrap();
    }

    let begun = Instant::now();
    let mut child = Command::new(&executable)
        .env("HOME", &fake_home)
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let pid = child.id();
    // Optional live copy of both streams (a stalled run is otherwise silent until its deadline).
    let live = std::env::var_os("MYCELIUM_CHAT_LIVE_LOG")
        .map(|p| std::sync::Arc::new(std::sync::Mutex::new(std::fs::File::create(p).unwrap())));
    let (tx, rx) = mpsc::channel();
    let stdout = child.stdout.take().unwrap();
    let out_tx = tx.clone();
    let out_live = live.clone();
    let out_reader = std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            if let Some(file) = &out_live {
                use std::io::Write;
                writeln!(file.lock().unwrap(), "out {line}").unwrap();
            }
            if out_tx.send(("out", line)).is_err() {
                return;
            }
        }
    });
    let stderr = child.stderr.take().unwrap();
    let err_live = live;
    let err_reader = std::thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            if let Some(file) = &err_live {
                use std::io::Write;
                writeln!(file.lock().unwrap(), "err {line}").unwrap();
            }
            if tx.send(("err", line)).is_err() {
                return;
            }
        }
    });
    let kill_group = |child: &mut std::process::Child| {
        if child.try_wait().unwrap().is_none() {
            // The unreaped leader pins this exact group (`process_group(0)`): its own pid is
            // the group id. No saved pid, no name pattern.
            let group = format!("-{}", child.id());
            assert!(
                Command::new("/bin/kill")
                    .args(["-KILL", "--", &group])
                    .status()
                    .unwrap()
                    .success()
            );
            child.wait().unwrap();
        }
    };

    let mut lines: Lines = Vec::new();
    let stderr_lines = std::sync::Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let pressure_max = std::sync::atomic::AtomicU32::new(1);
    let mut poll = |child: &mut std::process::Child, lines: &mut Lines| {
        while let Ok((stream, line)) = rx.try_recv() {
            if stream == "out" {
                lines.push((begun.elapsed().as_millis(), line));
            } else {
                stderr_lines.lock().unwrap().push(line);
            }
        }
        let level = pressure_level().parse::<u32>().unwrap_or(99);
        pressure_max.fetch_max(level, std::sync::atomic::Ordering::AcqRel);
        if level != 1 {
            kill_group(child);
            panic!("memory pressure level {level}: owned group killed");
        }
    };
    // Wall for the whole token path: the existing L1 member-chosen wall, as the slice smoke.
    let deadline = Instant::now() + budget(BudgetId::MemberChosenWall);
    let wait_for = |want: &str,
                    child: &mut std::process::Child,
                    lines: &mut Lines,
                    poll: &mut dyn FnMut(&mut std::process::Child, &mut Lines)| {
        loop {
            poll(child, lines);
            if let Some((_, failed)) = lines.iter().find(|(_, l)| l.contains("FAILED")) {
                let failed = failed.clone();
                kill_group(child);
                panic!(
                    "{failed}; lines {lines:?}; stderr {:?}",
                    stderr_lines.lock().unwrap()
                );
            }
            if lines.iter().any(|(_, l)| l == want) {
                return;
            }
            if child.try_wait().unwrap().is_some() {
                panic!(
                    "exited before {want}; lines {lines:?}; stderr {:?}",
                    stderr_lines.lock().unwrap()
                );
            }
            if Instant::now() >= deadline {
                kill_group(child);
                panic!(
                    "deadline waiting for {want}; lines {lines:?}; stderr {:?}",
                    stderr_lines.lock().unwrap()
                );
            }
            std::thread::sleep(budget(BudgetId::RegistryPoll));
        }
    };
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        for receipt in [
            "L11_NATIVE_READY",
            "L11_WINDOW_VISIBLE:true",
            "L11_PACKAGED_CHAT_PAGE_READY",
            "L11_SLICE_CHAT_DOM_TOKEN",
            "L11_SLICE_STOP_VERIFIED",
            "L11_SLICE_CHAT_DOM_TERMINAL",
        ] {
            wait_for(receipt, &mut child, &mut lines, &mut poll);
        }
        let tokens_at = begun.elapsed();
        send_quit(pid);
        let quit_sent = Instant::now();
        let exit_deadline = quit_sent + budget(BudgetId::StageCleanup);
        let status = loop {
            poll(&mut child, &mut lines);
            if let Some(status) = child.try_wait().unwrap() {
                break status;
            }
            assert!(
                Instant::now() < exit_deadline,
                "no exit within the cleanup budget after the exact-pid Apple Event"
            );
            std::thread::sleep(budget(BudgetId::RegistryPoll));
        };
        let quit_to_exit = quit_sent.elapsed();
        out_reader.join().unwrap();
        err_reader.join().unwrap();
        poll(&mut child, &mut lines);
        assert!(
            status.success(),
            "{status:?}; stderr {:?}",
            stderr_lines.lock().unwrap()
        );
        assert!(
            lines.iter().any(|(_, l)| l == "L11_CLEANUP:Verified"),
            "{lines:?}"
        );
        assert!(
            !lines.iter().any(|(_, l)| l.contains("Failed")),
            "{lines:?}"
        );
        // The owner's own evidence of this launch.
        let evidence_root = state.join("v02-node/evidence");
        let launch = std::fs::read_dir(&evidence_root)
            .unwrap()
            .map(|e| e.unwrap().path())
            .collect::<Vec<_>>();
        assert_eq!(launch.len(), 1, "one launch directory: {launch:?}");
        let generation = launch[0].join("generation-1");
        let result: serde_json::Value =
            serde_json::from_slice(&std::fs::read(generation.join("result.json")).unwrap())
                .unwrap();
        let proof: serde_json::Value =
            serde_json::from_slice(&std::fs::read(generation.join("stop-proof.json")).unwrap())
                .unwrap();
        assert_eq!(result["cancelled"], true, "{result}");
        assert_eq!(result["member_reaped"], true);
        assert_eq!(result["planner_active"], 0);
        assert_eq!(result["iroh_activation_transport"], true);
        assert_eq!(result["iroh_control_transport"], "signed-node-control");
        assert_eq!(result["loopback_tcp_control"], false);
        assert_eq!(
            result["artifact"]["member"]["staged_via"],
            "signed-artifact-chunk"
        );
        // The answer is v0.1.4's: a prefix of its greedy answer to the same question (round 9
        // passed with "\n\n" because only "some token streamed" was asserted).
        let oracle_tokens_checked = chat_answer::check(&result)
            .unwrap_or_else(|reason| panic!("answer is not the oracle's: {reason}; {result}"));
        assert_eq!(proof["cleanup_proven"], true);
        let workers: Vec<u64> = ["member_pid", "owner_leaf_pid"]
            .iter()
            .map(|k| result[k].as_u64().unwrap())
            .collect();
        for worker in &workers {
            assert!(!alive(*worker), "worker {worker} survived");
        }
        assert!(!alive(u64::from(pid)), "app {pid} survived");
        assert!(children_of(pid).is_empty(), "children of {pid} survived");
        // The workers were this very bundle's executable, re-executed.
        println!(
            "BUNDLE_CHAT {}",
            serde_json::json!({
                "pid": pid, "executable": executable, "workers": workers,
                "tokens_at_ms": tokens_at.as_millis(), "quit_to_exit_ms": quit_to_exit.as_millis(),
                "pressure_max": pressure_max.load(std::sync::atomic::Ordering::Acquire),
                "stderr": *stderr_lines.lock().unwrap(), "answer_text": result["text"],
                "answer_token_ids": result["token_ids"], "oracle_tokens_checked": oracle_tokens_checked,
                "stop_proof": proof, "receipts": lines,
            })
        );
    }));
    if let Err(panic) = outcome {
        kill_group(&mut child);
        if let Err(error) = std::fs::remove_dir_all(&fake) {
            eprintln!("cleanup_failed:{:?}", error.kind());
        }
        std::panic::resume_unwind(panic);
    }
    if let Err(error) = std::fs::remove_dir_all(&fake) {
        eprintln!("cleanup_failed:{:?}", error.kind());
    }
}
