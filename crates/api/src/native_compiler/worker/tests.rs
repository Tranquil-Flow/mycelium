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
