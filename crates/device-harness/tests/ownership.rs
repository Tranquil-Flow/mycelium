// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{
    Fault, FaultKind, Result, effects::Clock, ownership::*, preflight::ProcessIdentity,
};
use std::collections::VecDeque;
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
fn limits() -> StopLimits {
    StopLimits {
        grace: Span::from_millis(1000),
        poll: Span::from_millis(250),
    }
}
fn id() -> ProcessIdentity {
    ProcessIdentity {
        pid: 12345,
        birth: "birth".into(),
        executable: "/fixture/device-harness".into(),
    }
}
fn record() -> OwnedRecord {
    OwnedRecord {
        process: id(),
        attempt: "fixture".into(),
        kind: "soak".into(),
        marker: "/fixture/DEVICE.busy".into(),
        awake_record: None,
    }
}
struct Fake {
    record: OwnedRecord,
    private: bool,
    ids: VecDeque<Option<ProcessIdentity>>,
    interrupts: Vec<ProcessIdentity>,
    marker: bool,
    awake: Vec<String>,
    late_awake: bool,
    awake_fails: bool,
    reads: usize,
}
fn fake() -> Fake {
    Fake {
        record: record(),
        private: true,
        ids: [Some(id()), None].into(),
        interrupts: vec![],
        marker: false,
        awake: vec![],
        late_awake: true,
        awake_fails: false,
        reads: 0,
    }
}
impl OwnedProcessIo for Fake {
    fn record(&mut self) -> Result<RecordRead> {
        self.reads += 1;
        if self.reads > 1 && self.late_awake {
            self.record.awake_record = Some("/fixture/late-awake".into());
        }
        Ok(RecordRead {
            record: self.record.clone(),
            private_regular_owned: self.private,
        })
    }
    fn identity(&mut self, _: u32) -> Result<Option<ProcessIdentity>> {
        Ok(self.ids.pop_front().unwrap_or(None))
    }
    fn interrupt_exact(&mut self, p: &ProcessIdentity) -> Result<()> {
        self.interrupts.push(p.clone());
        Ok(())
    }
    fn marker_exists(&mut self, _: &str) -> Result<bool> {
        Ok(self.marker)
    }
    fn stop_awake_exact(&mut self, s: &str) -> Result<()> {
        self.awake.push(s.into());
        if self.awake_fails {
            Err(Fault::new("awake_cleanup_unproven", FaultKind::Cleanup))
        } else {
            Ok(())
        }
    }
}
#[test]
fn stop_only_exact_owned_pid_and_reread_late_awake_binding() {
    let mut io = fake();
    stop(&mut io, &mut Tick::default(), 99, "fixture", limits()).unwrap();
    assert_eq!(io.interrupts, vec![id()]);
    assert_eq!(io.reads, 2);
    assert_eq!(io.awake, vec!["/fixture/late-awake"]);
}
#[test]
fn stop_refuses_pid_reuse_private_record_self_pid_and_foreign_attempt_without_signal() {
    for case in 0..5 {
        let mut io = fake();
        match case {
            0 => io.ids[0].as_mut().unwrap().birth = "reused".into(),
            1 => io.private = false,
            2 => io.record.process.pid = 99,
            3 => io.record.attempt = "foreign".into(),
            _ => io.record.process.pid = 1,
        }
        assert!(stop(&mut io, &mut Tick::default(), 99, "fixture", limits()).is_err());
        assert!(io.interrupts.is_empty());
    }
}
#[test]
fn stop_timeout_marker_and_awake_failure_never_become_success_or_force_kill() {
    let mut io = fake();
    io.ids = std::iter::repeat_n(Some(id()), 10).collect();
    let mut clock = Tick::default();
    assert_eq!(
        stop(&mut io, &mut clock, 99, "fixture", limits())
            .unwrap_err()
            .code,
        "harness_stop_timeout_no_force_kill"
    );
    assert_eq!(io.interrupts.len(), 1);
    assert_eq!(clock.0, 1000);
    io = fake();
    io.marker = true;
    assert_eq!(
        stop(&mut io, &mut Tick::default(), 99, "fixture", limits())
            .unwrap_err()
            .code,
        "harness_busy_cleanup_unproven"
    );
    io = fake();
    io.awake_fails = true;
    assert_eq!(
        stop(&mut io, &mut Tick::default(), 99, "fixture", limits())
            .unwrap_err()
            .code,
        "awake_cleanup_unproven"
    );
}
struct QuitFake {
    calls: Vec<String>,
    census: VecDeque<Vec<ProcessIdentity>>,
    rc: i32,
    listener: bool,
}
impl AppQuitIo for QuitFake {
    fn apple_event_error_code(&self) -> Option<i32> {
        if self.rc == 1 { Some(-1743) } else { None }
    }
    fn census(&mut self, b: &str) -> Result<Vec<ProcessIdentity>> {
        assert_eq!(b, "/fixture/app.app");
        Ok(self.census.pop_front().unwrap_or_default())
    }
    fn apple_event_quit(&mut self, b: &str, _: Span) -> Result<i32> {
        self.calls.push(b.into());
        Ok(self.rc)
    }
    fn listener_present(&mut self, _: &str) -> Result<bool> {
        Ok(self.listener)
    }
}
#[test]
fn quit_exact_bundle_apple_event_only_and_listener_teardown_required() {
    let mut io = QuitFake {
        calls: vec![],
        census: [vec![id()], vec![]].into(),
        rc: 0,
        listener: false,
    };
    let r = quit(&mut io, &mut Tick::default(), "/fixture/app.app", limits()).unwrap();
    assert!(r.quit_clean);
    assert_eq!(r.seconds_to_exit, Some(0.));
    assert_eq!(r.processes_before, 1);
    assert_eq!(r.processes_left, 0);
    assert_eq!(io.calls, vec!["/fixture/app.app"]);
    io.census = [vec![id()], vec![]].into();
    io.listener = true;
    assert!(
        !quit(&mut io, &mut Tick::default(), "/fixture/app.app", limits())
            .unwrap()
            .quit_clean
    );
}
#[test]
fn quit_refusal_or_absent_app_cannot_escalate() {
    let mut io = QuitFake {
        calls: vec![],
        census: std::iter::repeat_n(vec![id()], 10).collect(),
        rc: 1,
        listener: true,
    };
    let mut clock = Tick::default();
    let r = quit(&mut io, &mut clock, "/fixture/app.app", limits()).unwrap();
    assert!(!r.quit_clean);
    assert_eq!(r.apple_event_rc, 1);
    assert_eq!(r.processes_left, 1);
    assert!(r.apple_event_error.as_deref().unwrap().contains("-1743"));
    assert_eq!(r.seconds_to_exit, None);
    assert_eq!(io.calls.len(), 1);
    assert_eq!(clock.0, 1000);
    io.census.clear();
    io.calls.clear();
    assert_eq!(
        quit(&mut io, &mut Tick::default(), "/fixture/app.app", limits())
            .unwrap_err()
            .code,
        "app_not_running"
    );
    assert!(io.calls.is_empty());
}
