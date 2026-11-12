// SPDX-License-Identifier: AGPL-3.0-only
//! Real UDS + owned child, with a separate phase barrier (no timing sleeps).
use super::*;
use std::io::{Read, Write};
use std::os::unix::net::UnixListener;

#[test]
#[ignore = "subprocess fixture, invoked only with its private socket"]
fn fake_worker_process() {
    let path = std::env::var("B5_FAKE_SOCKET").unwrap();
    let mode = std::env::var("B5_FAKE_MODE").unwrap();
    let mut wire = UnixStream::connect(&path).unwrap();
    let mut phase = UnixStream::connect(format!("{path}.phase")).unwrap();
    let guard = duration(BudgetId::ModelSelection).unwrap();
    wire.set_write_timeout(Some(guard)).unwrap();
    phase
        .set_read_timeout(Some(duration(BudgetId::SidecarReceivePollWait).unwrap()))
        .unwrap();
    if mode != "blocked_write" {
        if let Some(session) = reset_reply(&mode) {
            // EP0: answer exactly one session reset; any other request is
            // refused so a wrong command can never pass as a reset.
            let mut link = Link::new(wire.try_clone().unwrap(), guard).unwrap();
            let (head, body) = link.receive().unwrap();
            let reply = if head == json!({"command":stage_contract::native_session::COMMAND})
                && body.is_empty()
            {
                json!({"ok":true,"session":session})
            } else {
                json!({"error":"fake_unexpected_request"})
            };
            link.send(&reply, &[]).unwrap();
            return;
        }
        Link::new(wire.try_clone().unwrap(), guard)
            .unwrap()
            .receive()
            .unwrap();
    } else {
        // Receipt of the first byte proves send has entered the socket, rather
        // than letting cancellation win before the backpressured write starts.
        wire.set_read_timeout(Some(guard)).unwrap();
        wire.read_exact(&mut [0]).unwrap();
    }
    let mut frame = Vec::new();
    frame.extend(11_u32.to_be_bytes());
    frame.extend(b"{\"ok\":true}");
    frame.extend(16_u32.to_be_bytes());
    frame.extend([7; 16]);
    if mode == "healthy" {
        wire.write_all(&frame).unwrap();
        return;
    }
    if mode == "healthy_twice" {
        wire.write_all(&frame).unwrap();
        Link::new(wire.try_clone().unwrap(), guard)
            .unwrap()
            .receive()
            .unwrap();
        wire.write_all(&frame).unwrap();
        return;
    }
    let (prefix, trickle) = match mode.as_str() {
        "trickled_header" => (1, true),
        "trickled_body" => (20, true),
        "stalled_frame" => (2, false),
        "blocked_write" => (0, false),
        _ => panic!("unknown mode"),
    };
    wire.write_all(&frame[..prefix]).unwrap();
    phase.write_all(&[1]).unwrap();
    let end = Instant::now() + guard;
    let mut offset = prefix;
    while Instant::now() < end {
        match phase.read(&mut [0]) {
            Ok(0) => return,
            Ok(_) => panic!("unexpected control data"),
            Err(e)
                if matches!(
                    e.kind(),
                    std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock
                ) =>
            {
                eprintln!("fake_worker_trickle_tick: {e}");
            }
            Err(e) => panic!("control failed: {e}"),
        }
        if trickle && offset < frame.len() {
            if let Err(e) = wire.write_all(&frame[offset..offset + 1]) {
                eprintln!("fake peer retired: {e}");
                return;
            }
            offset += 1;
        }
    }
}

fn scenario(mode: &str, cancel_request: bool) {
    let root = std::env::temp_dir().join(format!(
        "b5-io-{}-{}",
        std::process::id(),
        crate::app_host::hex(&crate::app_host::random(8).unwrap())
    ));
    std::fs::create_dir(&root).unwrap();
    // macOS UDS paths are short; bind after entering a short /tmp-free relative path
    // via canonical parent: the default TMPDIR itself fits with short names.
    let socket = root.join("s");
    let phase_path = root.join("s.phase");
    let listener = UnixListener::bind(&socket).unwrap();
    let phase_listener = UnixListener::bind(&phase_path).unwrap();
    let child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--ignored",
            "--exact",
            "native_compiler::worker::tests::fake_worker_process",
            "--nocapture",
        ])
        .env("B5_FAKE_SOCKET", &socket)
        .env("B5_FAKE_MODE", mode)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    let pid = child.id();
    let (stream, _) = listener.accept().unwrap();
    let (mut phase, _) = phase_listener.accept().unwrap();
    phase
        .set_read_timeout(Some(duration(BudgetId::ModelSelection).unwrap()))
        .unwrap();
    let cancel = Arc::new(AtomicBool::new(false));
    let mut worker = Worker::from_child(child, stream, cancel.clone()).unwrap();
    let payload = if mode == "blocked_write" {
        vec![0; protocol::router_wire::MAX_PAYLOAD_BYTES]
    } else {
        vec![]
    };
    let started = Instant::now();
    let deadline = started
        + duration(if cancel_request {
            BudgetId::MemberChosenWall
        } else {
            BudgetId::RegistryPoll
        })
        .unwrap();
    let join = std::thread::spawn(move || {
        let result = worker.request_until(&json!({"command":"step"}), &payload, deadline);
        drop(worker); // production owner must kill AND reap before returning custody
        result
    });
    if mode != "healthy" {
        phase.read_exact(&mut [0]).unwrap();
        if cancel_request {
            cancel.store(true, Ordering::SeqCst);
        }
    }
    let interrupted = Instant::now();
    let result = join.join().unwrap();
    let elapsed = if cancel_request {
        interrupted.elapsed()
    } else {
        started.elapsed()
    };
    // waitpid ECHILD distinguishes reaped from merely killed/zombie.
    let pid = rustix::process::Pid::from_raw(pid as i32).unwrap();
    assert_eq!(
        rustix::process::waitpid(Some(pid), rustix::process::WaitOptions::NOHANG).unwrap_err(),
        rustix::io::Errno::CHILD
    );
    assert_eq!(
        rustix::process::test_kill_process(pid),
        Err(rustix::io::Errno::SRCH)
    );
    drop(phase);
    drop(listener);
    drop(phase_listener);
    std::fs::remove_dir_all(&root).unwrap();
    if mode == "healthy" {
        assert_eq!(result.unwrap(), (json!({"ok":true}), vec![7; 16]));
    } else {
        assert_eq!(
            result.unwrap_err().0,
            if cancel_request {
                "compiler_stopped"
            } else {
                "native_worker_deadline"
            },
            "{mode}"
        );
    }
    assert!(
        elapsed < duration(BudgetId::SidecarLocalHandshake).unwrap(),
        "{mode} took {elapsed:?}"
    );
}

macro_rules! case {
    ($name:ident, $mode:literal, $cancel:literal) => {
        #[test]
        fn $name() {
            scenario($mode, $cancel);
        }
    };
}
case!(deadline_trickled_header_reaps, "trickled_header", false);
case!(deadline_trickled_body_reaps, "trickled_body", false);
case!(deadline_stalled_frame_reaps, "stalled_frame", false);
case!(deadline_blocked_write_reaps, "blocked_write", false);
case!(cancel_trickled_header_reaps, "trickled_header", true);
case!(cancel_trickled_body_reaps, "trickled_body", true);
case!(cancel_stalled_frame_reaps, "stalled_frame", true);
case!(cancel_blocked_write_reaps, "blocked_write", true);
case!(timely_frame_succeeds, "healthy", false);

#[test]
fn step_cancel_flag_does_not_cancel_later_close_exchange() {
    let root = std::env::temp_dir().join(format!(
        "b7e-io-{}-{}",
        std::process::id(),
        crate::app_host::hex(&crate::app_host::random(8).unwrap())
    ));
    std::fs::create_dir(&root).unwrap();
    let socket = root.join("s");
    let listener = UnixListener::bind(&socket).unwrap();
    let phase_listener = UnixListener::bind(root.join("s.phase")).unwrap();
    let child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--ignored",
            "--exact",
            "native_compiler::worker::tests::fake_worker_process",
            "--nocapture",
        ])
        .env("B5_FAKE_SOCKET", &socket)
        .env("B5_FAKE_MODE", "healthy_twice")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    let (stream, _) = listener.accept().unwrap();
    let (_phase, _) = phase_listener.accept().unwrap();
    let mut worker = Worker::from_child(child, stream, Arc::new(AtomicBool::new(false))).unwrap();
    let deadline = Instant::now() + duration(BudgetId::ModelSelection).unwrap();
    let route = Arc::new(AtomicBool::new(false));
    worker
        .request_controlled(&json!({"command":"step"}), &[], deadline, route.clone())
        .unwrap();
    // Route teardown sets the step flag; the following close-class exchange must
    // still run under the worker's own flag.
    route.store(true, Ordering::SeqCst);
    assert_eq!(
        worker
            .request_until(&json!({"command":"close"}), &[], deadline)
            .unwrap(),
        (json!({"ok":true}), vec![7; 16])
    );
    let evidence = worker.kill_and_reap().unwrap();
    assert_eq!(evidence["worker_reaped"], true);
    std::fs::remove_dir_all(&root).unwrap();
}
/// worker.rs:147: when the link's frame-wait budget is unavailable, the
/// just-spawned child must still be killed and reaped before the error returns.
/// A child left behind would contradict the caller's "an error proves no child
/// remains" contract.
#[test]
fn unavailable_link_budget_still_kills_and_reaps_the_child() {
    let child = Command::new("/bin/cat")
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    let pid = rustix::process::Pid::from_child(&child);
    let (client, _server) = UnixStream::pair().unwrap();
    let refused = Worker::from_child_within(
        child,
        client,
        Arc::new(AtomicBool::new(false)),
        Err(Error("budget_unavailable")),
    );
    assert!(refused.is_err());
    // Already reaped by the worker: no status left to collect (ECHILD). A
    // leaked child would be collected here instead.
    let leftover = rustix::process::waitpid(Some(pid), rustix::process::WaitOptions::empty());
    assert_eq!(
        leftover.map(|s| s.map(|(p, _)| p)),
        Err(rustix::io::Errno::CHILD),
        "the child outlived a failed worker start"
    );
}

/// EP0 fake worker replies: `reset_valid` is a well-formed session reset;
/// the others are each wrong in exactly one way.
fn reset_reply(mode: &str) -> Option<Value> {
    let mut session = json!({"protocol":stage_contract::native_session::REPLY_PROTOCOL,
        "state_generation":1,"active_bytes":700,"baseline_bytes":700});
    match mode {
        "reset_valid" => {}
        "reset_residue" => session["active_bytes"] = json!(701),
        "reset_extra_field" => session["position"] = json!(0),
        "reset_wrong_protocol" => session["protocol"] = json!("mycelium.native_session_reset.v0"),
        _ => return None,
    }
    Some(session)
}
/// Drive one `reset_session` exchange against the fake worker in `mode` and
/// return its result; the fake child is always killed and reaped.
fn reset_against(mode: &str) -> Result<Value> {
    let root = std::env::temp_dir().join(format!(
        "ep0-io-{}-{}",
        std::process::id(),
        crate::app_host::hex(&crate::app_host::random(8).unwrap())
    ));
    std::fs::create_dir(&root).unwrap();
    let socket = root.join("s");
    let listener = UnixListener::bind(&socket).unwrap();
    let phase_listener = UnixListener::bind(root.join("s.phase")).unwrap();
    let child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--ignored",
            "--exact",
            "native_compiler::worker::tests::fake_worker_process",
            "--nocapture",
        ])
        .env("B5_FAKE_SOCKET", &socket)
        .env("B5_FAKE_MODE", mode)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .spawn()
        .unwrap();
    let (stream, _) = listener.accept().unwrap();
    let (_phase, _) = phase_listener.accept().unwrap();
    let mut worker = Worker::from_child(child, stream, Arc::new(AtomicBool::new(false))).unwrap();
    let result = worker.reset_session(Instant::now() + duration(BudgetId::ModelSelection).unwrap());
    assert_eq!(worker.kill_and_reap().unwrap()["worker_reaped"], true);
    std::fs::remove_dir_all(&root).unwrap();
    result
}
/// EP0: the client sends exactly `{"command":"session_reset_v1"}` (the fake
/// refuses anything else) and returns the worker's session object.
#[test]
fn session_reset_sends_the_exact_command_and_returns_the_session() {
    assert_eq!(
        reset_against("reset_valid").map_err(|e| e.0),
        Ok(reset_reply("reset_valid").unwrap())
    );
}
/// MG2 M12: reproduce the kernel outcome of natural exit after the last live
/// poll but before a successful kill call. NOWAIT observes exit without taking
/// the Child's wait ownership. This qualifies real OS evidence plus the
/// production classifier, not an injected race inside Worker or native MLX.
#[test]
fn natural_exit_after_the_last_poll_is_death_even_when_kill_succeeds() {
    use rustix::process::{Pid, WaitId, WaitIdOptions, WaitOptions, waitid, waitpid};
    use std::os::unix::process::ExitStatusExt;
    fn owned_cat() -> Worker {
        let (client, _server) = UnixStream::pair().unwrap();
        let child = Command::new("/bin/cat")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .spawn()
            .unwrap();
        // Worker owns kill/reap before any assertion or phase wait can panic.
        Worker::from_child(child, client, Arc::new(AtomicBool::new(false))).unwrap()
    }
    let worker = owned_cat();
    let pid = Pid::from_raw(worker.pid().unwrap() as i32).unwrap();
    let raced = {
        let mut slot = worker.child.lock().unwrap();
        let child = slot.as_mut().unwrap();
        assert_eq!(child.try_wait().unwrap(), None, "cat is held by its stdin");
        drop(child.stdin.take()); // EOF lets this exact child exit naturally.
        let end = Instant::now() + duration(BudgetId::ModelSelection).unwrap();
        loop {
            assert!(Instant::now() < end, "owned child did not exit after EOF");
            match waitid(
                WaitId::Pid(pid),
                WaitIdOptions::EXITED | WaitIdOptions::NOWAIT | WaitIdOptions::NOHANG,
            ) {
                Ok(Some(status)) => {
                    assert_eq!(status.exit_status(), Some(0));
                    break;
                }
                Ok(None) | Err(rustix::io::Errno::INTR) => std::thread::yield_now(),
                Err(e) => panic!("observing owned child exit failed: {e}"),
            }
        }
        child.kill().unwrap(); // Success does not prove our signal caused death.
        let status = child.wait().unwrap();
        assert_eq!(status.code(), Some(0));
        assert_eq!(status.signal(), None);
        json!({"worker_pid":pid.as_raw_pid(),"context_closed":false,
            "worker_reaped":true,"killed":true,
            "exit_code":status.code(),"exit_signal":status.signal()})
    };
    drop(worker);
    assert_eq!(
        waitpid(Some(pid), WaitOptions::NOHANG).unwrap_err(),
        rustix::io::Errno::CHILD
    );
    let control = owned_cat().kill_and_reap().unwrap();
    assert_eq!(control["killed"], true);
    assert_eq!(control["exit_signal"], libc::SIGKILL);
    let control_pid = Pid::from_raw(control["worker_pid"].as_u64().unwrap() as i32).unwrap();
    assert_eq!(
        waitpid(Some(control_pid), WaitOptions::NOHANG).unwrap_err(),
        rustix::io::Errno::CHILD
    );
    println!("MG2_M12 natural={raced} deliberate={control}");
    assert!(
        !Worker::ended_by_itself(&control),
        "deliberate live kill is not natural death"
    );
    assert!(
        Worker::ended_by_itself(&raced),
        "a successful kill call cannot hide an earlier natural exit"
    );
}

/// EP0: a reply that reports residue (more active memory than the resident
/// weights), carries an unknown field or another protocol is no reset proof.
#[test]
fn session_reset_refuses_residue_and_malformed_replies() {
    for mode in ["reset_residue", "reset_extra_field", "reset_wrong_protocol"] {
        assert_eq!(
            reset_against(mode).map_err(|e| e.0),
            Err("native_session_reset_invalid"),
            "{mode}"
        );
    }
}
