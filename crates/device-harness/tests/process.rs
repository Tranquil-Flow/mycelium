// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{
    Fault, FaultKind, Result,
    effects::{Clock, Command, CommandKind, Output},
    process::*,
};
#[derive(Default)]
struct Tick(u64);
impl Clock for Tick {
    fn now(&self) -> u64 {
        self.0
    }
    fn sleep(&mut self, s: Span) -> Result<()> {
        self.0 += s.millis();
        Ok(())
    }
}
struct Fake {
    started: usize,
    polls: usize,
    terminate: usize,
    cleanup: usize,
    finish_after: usize,
    spawn_error: bool,
    poll_error: bool,
    cleanup_error: bool,
}
impl Default for Fake {
    fn default() -> Self {
        Self {
            started: 0,
            polls: 0,
            terminate: 0,
            cleanup: 0,
            finish_after: 2,
            spawn_error: false,
            poll_error: false,
            cleanup_error: false,
        }
    }
}
impl Process for Fake {
    fn start(&mut self, _: &Command) -> Result<()> {
        self.started += 1;
        if self.spawn_error {
            Err(Fault::new("spawn_failed", FaultKind::Observation))
        } else {
            Ok(())
        }
    }
    fn poll(&mut self) -> Result<Option<Output>> {
        self.polls += 1;
        if self.poll_error {
            return Err(Fault::new("poll_failed", FaultKind::Observation));
        }
        Ok((self.polls >= self.finish_after).then(|| Output {
            rc: 0,
            stdout: "PRIVATE".into(),
            stderr: String::new(),
        }))
    }
    fn terminate_owned(&mut self) -> Result<()> {
        self.terminate += 1;
        Ok(())
    }
    fn cleanup(&mut self) -> Result<()> {
        self.cleanup += 1;
        if self.cleanup_error {
            Err(Fault::new("capture_cleanup_failed", FaultKind::Cleanup))
        } else {
            Ok(())
        }
    }
}
fn cmd() -> Command {
    Command {
        kind: CommandKind::Local,
        program: "fixture".into(),
        args: vec![],
    }
}
fn bound() -> Span {
    Span::from_millis(1000)
}
fn poll() -> Span {
    Span::from_millis(250)
}
#[test]
fn bounded_child_success_cleans_capture_without_signal_or_secret_diagnostics() {
    let mut p = Fake::default();
    let mut c = Tick::default();
    let mut d = vec![];
    assert_eq!(
        supervise(&mut p, &mut c, &cmd(), bound(), poll(), &mut d)
            .unwrap()
            .rc,
        0
    );
    assert_eq!(p.started, 1);
    assert_eq!(p.terminate, 0);
    assert_eq!(p.cleanup, 1);
    assert_eq!(c.0, 250);
    assert!(d.is_empty());
}
#[test]
fn deadline_terminates_only_owned_child_once_and_preserves_timeout() {
    let mut p = Fake {
        finish_after: 99,
        ..Fake::default()
    };
    let mut c = Tick::default();
    let mut d = vec![];
    assert_eq!(
        supervise(&mut p, &mut c, &cmd(), bound(), poll(), &mut d)
            .err()
            .unwrap()
            .code,
        "command_timeout"
    );
    assert_eq!(p.terminate, 1);
    assert_eq!(p.cleanup, 1);
    assert_eq!(c.0, 1000);
    assert_eq!(d[0].code, "command_timeout");
}
#[test]
fn spawn_poll_and_cleanup_failures_record_specific_first_and_secondary_codes() {
    for (spawn, poll_error) in [(true, false), (false, true)] {
        let mut p = Fake {
            spawn_error: spawn,
            poll_error,
            cleanup_error: true,
            ..Fake::default()
        };
        let mut d = vec![];
        let e = supervise(
            &mut p,
            &mut Tick::default(),
            &cmd(),
            bound(),
            poll(),
            &mut d,
        )
        .err()
        .unwrap();
        assert_eq!(e.code, if spawn { "spawn_failed" } else { "poll_failed" });
        assert_eq!(d[0], e);
        assert_eq!(d.last().unwrap().code, "capture_cleanup_failed");
        assert_eq!(p.cleanup, 1);
    }
}
#[test]
fn late_success_or_empty_poll_cannot_escape_absolute_process_deadline() {
    use std::{cell::Cell, rc::Rc};
    struct Shared(Rc<Cell<u64>>);
    impl Clock for Shared {
        fn now(&self) -> u64 {
            self.0.get()
        }
        fn sleep(&mut self, s: Span) -> Result<()> {
            self.0.set(self.0.get() + s.millis());
            Ok(())
        }
    }
    struct Slow {
        at: Rc<Cell<u64>>,
        success: bool,
        terminated: bool,
        cleaned: bool,
    }
    impl Process for Slow {
        fn start(&mut self, _: &Command) -> Result<()> {
            Ok(())
        }
        fn poll(&mut self) -> Result<Option<Output>> {
            self.at.set(1001);
            Ok(self.success.then(|| Output {
                rc: 0,
                stdout: String::new(),
                stderr: String::new(),
            }))
        }
        fn terminate_owned(&mut self) -> Result<()> {
            self.terminated = true;
            Ok(())
        }
        fn cleanup(&mut self) -> Result<()> {
            self.cleaned = true;
            Ok(())
        }
    }
    for success in [true, false] {
        let at = Rc::new(Cell::new(0));
        let mut clock = Shared(at.clone());
        let mut p = Slow {
            at,
            success,
            terminated: false,
            cleaned: false,
        };
        let result = supervise(&mut p, &mut clock, &cmd(), bound(), poll(), &mut vec![]);
        assert_eq!(result.err().unwrap().code, "command_timeout");
        assert!(p.terminated && p.cleaned);
    }
}
#[test]
fn cleanup_failure_after_success_is_not_success() {
    let mut p = Fake {
        cleanup_error: true,
        ..Fake::default()
    };
    assert_eq!(
        supervise(
            &mut p,
            &mut Tick::default(),
            &cmd(),
            bound(),
            poll(),
            &mut vec![]
        )
        .err()
        .unwrap()
        .code,
        "capture_cleanup_failed"
    );
}
