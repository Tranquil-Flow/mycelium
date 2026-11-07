// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{Fault, FaultKind, Result, effects::*};
use std::collections::VecDeque;
struct Fake {
    replies: VecDeque<Result<Output>>,
    calls: Vec<(CommandKind, Span)>,
}
impl Transport for Fake {
    fn execute(&mut self, c: &Command, b: Span) -> Result<Output> {
        self.calls.push((c.kind, b));
        self.replies
            .pop_front()
            .expect("unexpected external effect")
    }
}
#[derive(Default)]
struct FakeClock {
    at: u64,
    sleeps: Vec<Span>,
}
impl Clock for FakeClock {
    fn now(&self) -> u64 {
        self.at
    }
    fn sleep(&mut self, b: Span) -> Result<()> {
        self.at += b.millis();
        self.sleeps.push(b);
        Ok(())
    }
}
fn limits() -> CommandLimits {
    CommandLimits {
        attempt: Span::from_millis(8000),
        retry_delay: Span::from_millis(5000),
    }
}
fn command(kind: CommandKind) -> Command {
    Command {
        kind,
        program: "fixture".into(),
        args: vec!["private argument".into()],
    }
}
fn out(rc: i32, stderr: &str) -> Result<Output> {
    Ok(Output {
        rc,
        stdout: "PRIVATE".into(),
        stderr: stderr.into(),
    })
}
#[test]
fn ssh_rc255_only_one_bounded_retry_with_safe_receipt() {
    let mut io = Fake {
        replies: [out(255, "PRIVATE"), out(0, "MYCELIUM_REMOTE_EXIT=0\n")].into(),
        calls: vec![],
    };
    let mut clock = FakeClock::default();
    let mut journal = vec![];
    assert_eq!(
        execute(
            &mut io,
            &mut clock,
            &command(CommandKind::Ssh),
            limits(),
            &mut journal
        )
        .unwrap()
        .rc,
        0
    );
    assert_eq!(
        io.calls,
        vec![(CommandKind::Ssh, Span::from_millis(8000)); 2]
    );
    assert_eq!(clock.at, 5000);
    assert_eq!(
        journal,
        vec![RetryReceipt {
            attempt: 1,
            rc: 255,
            delay: Span::from_millis(5000)
        }]
    );
    assert!(!format!("{journal:?}").contains("PRIVATE"));
}
#[test]
fn second_connection_loss_is_incomplete_not_product_failure() {
    let mut io = Fake {
        replies: [out(255, ""), out(255, "")].into(),
        calls: vec![],
    };
    let err = execute(
        &mut io,
        &mut FakeClock::default(),
        &command(CommandKind::Ssh),
        limits(),
        &mut vec![],
    )
    .err()
    .unwrap();
    assert_eq!(err.code, "ssh_observation_lost");
    assert!(err.incomplete());
    assert_eq!(io.calls.len(), 2);
}
#[test]
fn remote_255_non255_scp_timeout_and_spawn_failure_never_retry() {
    for (kind, reply) in [
        (CommandKind::Ssh, out(255, "MYCELIUM_REMOTE_EXIT=255\n")),
        (CommandKind::Ssh, out(1, "MYCELIUM_REMOTE_EXIT=1\n")),
        (CommandKind::Scp, out(255, "")),
        (CommandKind::Local, out(255, "")),
        (
            CommandKind::Ssh,
            Err(Fault::new("command_timeout", FaultKind::Observation)),
        ),
        (
            CommandKind::Ssh,
            Err(Fault::new("spawn_failed", FaultKind::Observation)),
        ),
    ] {
        let mut io = Fake {
            replies: [reply].into(),
            calls: vec![],
        };
        let mut clock = FakeClock::default();
        assert!(execute(&mut io, &mut clock, &command(kind), limits(), &mut vec![]).is_err());
        assert_eq!(io.calls.len(), 1);
        assert!(clock.sleeps.is_empty());
    }
}
#[test]
fn ssh_wrapper_quotes_remote_command_and_preserves_transport_options() {
    struct Capture(Vec<Vec<String>>);
    impl Transport for Capture {
        fn execute(&mut self, c: &Command, _: Span) -> Result<Output> {
            self.0.push(c.args.clone());
            out(255, "MYCELIUM_REMOTE_EXIT=255\n")
        }
    }
    let c = Command {
        kind: CommandKind::Ssh,
        program: "ssh".into(),
        args: vec![
            "-oBatchMode=yes".into(),
            "fixture-host".into(),
            "printf 'PRIVATE'; exit 255".into(),
        ],
    };
    let mut io = Capture(vec![]);
    let err = execute(
        &mut io,
        &mut FakeClock::default(),
        &c,
        limits(),
        &mut vec![],
    )
    .err()
    .unwrap();
    assert_eq!(err.code, "command_failed");
    assert_eq!(io.0.len(), 1);
    assert_eq!(&io.0[0][..2], &c.args[..2]);
    assert_eq!(shell_quote("a'b"), "'a'\\''b'");
    let remote = &io.0[0][2];
    // The child shell keeps an explicit remote `exit 255` from skipping the marker.
    assert!(remote.starts_with("/bin/sh -c 'printf '\\''PRIVATE'\\''; exit 255'; rc=$?;"));
    assert!(remote.contains("MYCELIUM_REMOTE_EXIT=%s"));
    assert!(remote.ends_with("exit \"$rc\""));
}
#[test]
fn command_failure_retains_exact_safe_executable_and_exit_without_output() {
    for (kind, program, rc, replies) in [
        (CommandKind::Local, "/fixture/bin/fixture", 1, 1),
        (CommandKind::Scp, "/usr/bin/scp", 255, 1),
        (CommandKind::Ssh, "/usr/bin/ssh", 255, 2),
    ] {
        let mut io = Fake {
            replies: (0..replies).map(|_| out(rc, "PRIVATE-LINK")).collect(),
            calls: vec![],
        };
        let command = Command {
            program: program.into(),
            ..command(kind)
        };
        let err = execute_reported(
            &mut io,
            &mut FakeClock::default(),
            &command,
            limits(),
            &mut vec![],
        )
        .err()
        .unwrap();
        assert_eq!(
            err.to_string(),
            format!(
                "command_failed:{}:rc{rc}",
                program.rsplit('/').next().unwrap()
            )
        );
        assert!(err.fault.incomplete());
        assert_eq!(io.calls.len(), replies);
        assert!(!format!("{err:?}").contains("PRIVATE"));
    }
    let mut io = Fake {
        replies: [Err(Fault::new("command_timeout", FaultKind::Observation))].into(),
        calls: vec![],
    };
    let err = execute_reported(
        &mut io,
        &mut FakeClock::default(),
        &command(CommandKind::Ssh),
        limits(),
        &mut vec![],
    )
    .err()
    .unwrap();
    assert_eq!(err.to_string(), "command_timeout");
    assert_eq!(io.calls.len(), 1);
}
#[test]
fn outer_safety_fence_panics_are_not_candidate_refusals_for_process_or_http() {
    struct Fence;
    impl Transport for Fence {
        fn execute(&mut self, _: &Command, _: Span) -> Result<Output> {
            panic!("OUTER_SAFETY_FENCE");
        }
    }
    impl device_harness::http::Http for Fence {
        fn call(
            &mut self,
            _: device_harness::http::Request,
            _: Span,
        ) -> Result<(u16, serde_json::Value)> {
            panic!("OUTER_SAFETY_FENCE");
        }
    }
    impl device_harness::http::Guard for Fence {
        fn check(&mut self) -> Result<()> {
            Ok(())
        }
    }
    let process = std::panic::catch_unwind(|| {
        execute(
            &mut Fence,
            &mut FakeClock::default(),
            &command(CommandKind::Local),
            limits(),
            &mut vec![],
        )
        .err()
    })
    .unwrap_err();
    let http = std::panic::catch_unwind(|| {
        device_harness::http::chat(
            &mut Fence,
            &mut FakeClock::default(),
            &mut Fence,
            "fixture",
            48,
            device_harness::http::ChatLimits {
                total: Span::from_millis(240000),
                request: Span::from_millis(20000),
                poll: Span::from_millis(250),
            },
        )
        .err()
    })
    .unwrap_err();
    for payload in [process, http] {
        assert_eq!(payload.downcast_ref::<&str>(), Some(&"OUTER_SAFETY_FENCE"));
    }
}
#[test]
fn zero_budget_or_overflow_refuses_before_transport() {
    for l in [
        CommandLimits {
            attempt: Span::ZERO,
            retry_delay: Span::ZERO,
        },
        CommandLimits {
            attempt: Span::from_millis(u64::MAX),
            retry_delay: Span::from_millis(1),
        },
    ] {
        let mut io = Fake {
            replies: VecDeque::new(),
            calls: vec![],
        };
        assert!(
            execute(
                &mut io,
                &mut FakeClock::default(),
                &command(CommandKind::Ssh),
                l,
                &mut vec![]
            )
            .is_err()
        );
        assert!(io.calls.is_empty());
    }
}
