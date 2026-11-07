// SPDX-License-Identifier: AGPL-3.0-only
//! Native process/listener probe: pure parsers plus exact command shapes behind a recording
//! transport, then a read-only check of THIS test process on the real OS (`ps`, `lsof` of
//! our own pid and our own loopback listener; no other process, app, device or network).
mod support;
use budgets::Span;
use device_harness::{
    Result,
    effects::{Command, CommandKind, Output, Transport},
    native::NativeTransport,
    native_os::{HostProbe, Probe, parse_listener, parse_process},
};
use std::net::TcpListener;
use support::PrivateRoot;

#[test]
fn process_parser_accepts_one_exact_ps_line() {
    let ok = parse_process(
        4242,
        "Wed 28 Oct 05:19:01 2026 /Applications/My App.app/Contents/MacOS/mycelium\n",
    )
    .unwrap();
    assert_eq!(ok.pid, 4242);
    assert_eq!(ok.birth, "Wed 28 Oct 05:19:01 2026");
    assert_eq!(
        ok.executable,
        "/Applications/My App.app/Contents/MacOS/mycelium"
    );
    let padded = parse_process(7, "Sun  4 Oct 05:19:01 2026 /bin/sh").unwrap();
    assert_eq!(padded.birth, "Sun 4 Oct 05:19:01 2026"); // one canonical spelling
    let wide = parse_process(7, "Wed 28 Oct 05:19:01 2026    /bin/bash\n").unwrap();
    assert_eq!(
        (wide.birth.as_str(), wide.executable.as_str()),
        ("Wed 28 Oct 05:19:01 2026", "/bin/bash")
    );
    for bad in [
        "",
        "\n",
        "Wed 28 Oct 05:19:01 2026",
        "Wed 28 Oct 05:19:01 2026 relative/path",
        "Wed 28 Oct 05:19:01 2026 /a\nWed 28 Oct 05:19:02 2026 /b",
        "Xyz 28 Oct 05:19:01 2026 /a",
        "Wed 28 Oct 25:19:01 2026 /a",
        "Wed 28 Oct 05:19:01 20x6 /a",
        "garbage line that is long enough to pass length /a",
        "Wed 28 Oct 05:19:01 2026 /a\0b",
    ] {
        assert_eq!(
            parse_process(1, bad).err().map(|e| e.code),
            Some("process_identity_unobserved"),
            "{bad:?}"
        );
    }
}

#[test]
fn process_parser_normalizes_both_observed_english_ps_layouts() {
    // Actual same-host ps observations: clean env/C uses month/day; en_GB uses day/month.
    for (day_first, month_first, birth) in [
        (
            "Thu 29 Oct 21:30:57 2026 /bin/sh\n",
            "Thu Oct 29 21:30:57 2026 /bin/sh\n",
            "Thu 29 Oct 21:30:57 2026",
        ),
        (
            "Sun  4 Oct 05:19:01 2026    /Applications/My App.app/Contents/MacOS/tool\n",
            "Sun Oct  4 05:19:01 2026    /Applications/My App.app/Contents/MacOS/tool\n",
            "Sun 4 Oct 05:19:01 2026",
        ),
    ] {
        let accepted = parse_process(4242, day_first).unwrap();
        assert_eq!(accepted.birth, birth);
        assert_eq!(parse_process(4242, month_first).unwrap(), accepted);
    }
}

#[test]
fn process_parser_refuses_mixed_junk_and_multiline_layouts() {
    for bad in [
        "Thu Oct Nov 21:30:57 2026 /bin/sh",
        "Thu 29 10 21:30:57 2026 /bin/sh",
        "Thu Okt 29 21:30:57 2026 /bin/sh",
        "Xxx Oct 29 21:30:57 2026 /bin/sh",
        "Thu Oct xx 21:30:57 2026 /bin/sh",
        "Thu Oct 029 21:30:57 2026 /bin/sh",
        "Thu Oct 29 24:30:57 2026 /bin/sh",
        "Thu Oct 29 21:60:57 2026 /bin/sh",
        "Thu Oct 29 21:30:61 2026 /bin/sh",
        "Thu Oct 29 1:30:57 2026 /bin/sh",
        "Thu Oct 29 21:30:57:00 2026 /bin/sh",
        "Thu Oct 29 21:30:57 20x6 /bin/sh",
        "Thu Oct 29 21:30:57 2026 relative/path",
        "Thu Oct 29 21:30:57 2026 /bin/sh\r\n",
        "Thu Oct 29 21:30:57 2026 /bin/sh\0",
        "Thu Oct 29 21:30:57 2026 /bin/sh\n\n",
        "Thu Oct 29 21:30:57 2026 /a\nThu Oct 29 21:30:57 2026 /b",
        "Thu\tOct 29 21:30:57 2026 /bin/sh",
    ] {
        assert_eq!(
            parse_process(4242, bad).unwrap_err().code,
            "process_identity_unobserved",
            "{bad:?}"
        );
    }
}

#[test]
fn listener_parser_needs_exactly_one_loopback_port() {
    assert_eq!(
        parse_listener("p1\nf15\nn127.0.0.1:49152\n").unwrap(),
        49152
    );
    // The same listener repeated (dual descriptors) is one port.
    assert_eq!(
        parse_listener("p1\nf15\nn127.0.0.1:49152\nf16\nn127.0.0.1:49152\n").unwrap(),
        49152
    );
    for (bad, code) in [
        ("", "listener_unobserved"),
        ("p1\n", "listener_unobserved"),
        ("p1\nn127.0.0.1:1\nn127.0.0.1:2\n", "listener_ambiguous"),
        ("p1\nn*:49152\n", "listener_unobserved"),
        ("p1\nn10.0.0.5:49152\n", "listener_unobserved"),
        ("p1\nn127.0.0.1:0\n", "listener_unobserved"),
        ("p1\nn127.0.0.1:70000\n", "listener_unobserved"),
        ("p1\nn127.0.0.1:x\n", "listener_unobserved"),
        ("p1\nn[::1]:49152\n", "listener_unobserved"),
    ] {
        assert_eq!(
            parse_listener(bad).err().map(|e| e.code),
            Some(code),
            "{bad:?}"
        );
    }
}

struct Recording {
    seen: Vec<(String, Vec<String>)>,
    reply: Output,
}
impl Transport for Recording {
    fn execute(&mut self, c: &Command, _: Span) -> Result<Output> {
        assert!(matches!(c.kind, CommandKind::Local));
        self.seen.push((c.program.clone(), c.args.clone()));
        Ok(Output {
            rc: self.reply.rc,
            stdout: self.reply.stdout.clone(),
            stderr: self.reply.stderr.clone(),
        })
    }
}

#[test]
fn commands_are_fixed_absolute_read_only_programs_for_one_pid() {
    let mut probe = HostProbe {
        transport: Recording {
            seen: vec![],
            reply: Output {
                rc: 0,
                stdout: "Wed 28 Oct 05:19:01 2026 /bin/sh\n".into(),
                stderr: String::new(),
            },
        },
    };
    probe.process(4242, Span::from_millis(1000)).unwrap();
    probe.transport.reply.stdout = "p4242\nf9\nn127.0.0.1:49152\n".into();
    assert_eq!(
        probe.listener_port(4242, Span::from_millis(1000)).unwrap(),
        49152
    );
    let args = |a: &[&str]| a.iter().map(ToString::to_string).collect::<Vec<_>>();
    assert_eq!(
        probe.transport.seen,
        vec![
            (
                "/bin/ps".to_string(),
                args(&["-o", "lstart=", "-o", "comm=", "-p", "4242"])
            ),
            (
                "/usr/sbin/lsof".to_string(),
                args(&[
                    "-nP",
                    "-a",
                    "-p",
                    "4242",
                    "-iTCP@127.0.0.1",
                    "-sTCP:LISTEN",
                    "-Fn"
                ])
            ),
        ]
    );
}

#[test]
fn nonzero_exit_and_non_positive_pids_never_become_an_identity() {
    let mut probe = HostProbe {
        transport: Recording {
            seen: vec![],
            reply: Output {
                rc: 1,
                stdout: "Wed 28 Oct 05:19:01 2026 /bin/sh\n".into(),
                stderr: String::new(),
            },
        },
    };
    assert_eq!(
        probe
            .process(4242, Span::from_millis(1000))
            .err()
            .unwrap()
            .code,
        "process_identity_unobserved"
    );
    assert_eq!(
        probe
            .listener_port(4242, Span::from_millis(1000))
            .err()
            .unwrap()
            .code,
        "listener_unobserved"
    );
    for pid in [0, 1] {
        assert_eq!(
            probe
                .process(pid, Span::from_millis(1000))
                .err()
                .unwrap()
                .code,
            "process_pid_invalid"
        );
        assert_eq!(
            probe
                .listener_port(pid, Span::from_millis(1000))
                .err()
                .unwrap()
                .code,
            "process_pid_invalid"
        );
    }
    assert!(
        probe.transport.seen.len() == 2,
        "only the two valid-pid calls reached the OS; pid 0 and 1 never did"
    );
}

#[test]
fn real_os_reports_this_process_executable_birth_and_its_own_listener() {
    let root = PrivateRoot::new("os-probe");
    let mut probe = HostProbe {
        transport: NativeTransport {
            scratch: root.to_path_buf(),
            uid: root.uid(),
            poll: Span::from_millis(20),
            max_output_bytes: 65_536,
            diagnostics: vec![],
        },
    };
    let pid = std::process::id();
    let id = probe.process(pid, Span::from_millis(15_000)).unwrap();
    assert_eq!(id.pid, pid);
    assert_eq!(
        std::fs::canonicalize(&id.executable).unwrap(),
        std::fs::canonicalize(std::env::current_exe().unwrap()).unwrap()
    );
    // Stable across two reads of the same live process; a different process differs.
    assert_eq!(probe.process(pid, Span::from_millis(15_000)).unwrap(), id);
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    assert_eq!(
        probe.listener_port(pid, Span::from_millis(15_000)).unwrap(),
        port
    );
}
