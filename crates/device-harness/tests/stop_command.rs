// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{
    Fault, FaultKind, Result,
    effects::{Command, Output, Transport},
    ownership::OwnedProcessIo,
    preflight::ProcessIdentity,
    stop_command::*,
};
use std::collections::VecDeque;
struct Fake {
    out: VecDeque<(i32, &'static str)>,
    calls: Vec<(String, Vec<String>)>,
}
impl Transport for Fake {
    fn execute(&mut self, c: &Command, _: Span) -> Result<Output> {
        self.calls.push((c.program.clone(), c.args.clone()));
        let (rc, s) = self
            .out
            .pop_front()
            .ok_or(Fault::new("fake_exhausted", FaultKind::Observation))?;
        Ok(Output {
            rc,
            stdout: s.into(),
            stderr: "MUST-NOT-LEAK".into(),
        })
    }
}
fn adapter(rows: Vec<(i32, &'static str)>) -> StopAdapter<Fake> {
    StopAdapter {
        transport: Fake {
            out: rows.into(),
            calls: vec![],
        },
        record_path: "unused".into(),
        expected_executable: "/private/tool".into(),
        uid: 501,
        command_bound: Span::from_millis(1000),
    }
}
fn id() -> ProcessIdentity {
    ProcessIdentity {
        pid: 123,
        birth: "Tue Oct 27 15:00:00 2026".into(),
        executable: "/private/tool".into(),
    }
}
#[test]
fn ps_parser_preserves_birth_and_exact_executable_with_spaces_and_refuses_malformed() {
    let p = parse_ps(
        " 123 Tue Oct 27 15:00:00 2026 S /private/a  path/tool\n",
        123,
    )
    .unwrap()
    .unwrap();
    assert_eq!(p.birth, id().birth);
    assert_eq!(p.executable, "/private/a  path/tool");
    assert!(parse_ps("", 123).unwrap().is_none());
    assert!(
        parse_ps("123 Tue Oct 27 15:00:00 2026 Z /private/tool", 123)
            .unwrap()
            .is_none()
    );
    assert!(parse_ps("wrong shape", 123).is_err());
    assert!(parse_ps("124 Tue Oct 27 15:00:00 2026 S /private/tool", 123).is_err());
}
#[test]
fn exact_signal_rechecks_birth_immediately_and_never_escalates() {
    let mut a = adapter(vec![
        (0, "123 Tue Oct 27 15:00:00 2026 S /private/tool"),
        (0, ""),
    ]);
    a.interrupt_exact(&id()).unwrap();
    assert_eq!(a.transport.calls[0].0, "/bin/ps");
    assert_eq!(
        a.transport.calls[1],
        (
            "/bin/kill".into(),
            vec!["-INT".into(), "--".into(), "123".into()]
        )
    );
    let mut a = adapter(vec![(0, "123 Tue Oct 27 15:01:00 2026 S /private/tool")]);
    assert!(a.interrupt_exact(&id()).is_err());
    assert_eq!(a.transport.calls.len(), 1);
}
#[test]
fn ps_observation_error_is_not_process_gone_and_remote_awake_is_not_silently_claimed() {
    let mut a = adapter(vec![(2, "private failure")]);
    assert!(a.identity(123).is_err());
    assert!(a.stop_awake_exact("remote.private").is_err());
    assert_eq!(a.transport.calls.len(), 1);
    let mut a = adapter(vec![(1, "")]);
    assert!(a.identity(123).unwrap().is_none());
}
