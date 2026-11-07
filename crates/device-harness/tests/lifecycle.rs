// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{
    Fault, FaultKind, Host, Result,
    admission::{Power, PowerSource},
    effects::Clock,
    lifecycle::*,
    preflight::ProcessIdentity,
};
use std::collections::BTreeMap;
#[derive(Default)]
struct PowerFake {
    now: u64,
    pct: u8,
    stops: usize,
    reads: Vec<Host>,
    remote_failure: bool,
}
impl Clock for PowerFake {
    fn now(&self) -> u64 {
        self.now
    }
    fn sleep(&mut self, s: Span) -> Result<()> {
        self.now += s.millis();
        Ok(())
    }
}
impl PowerIo for PowerFake {
    fn power(&mut self, h: Host) -> Result<Power> {
        self.reads.push(h);
        if h == Host::Member && self.remote_failure {
            return Err(Fault::new(
                "command_failed:ssh:rc255",
                FaultKind::Observation,
            ));
        }
        Ok(if h == Host::Owner {
            Power {
                source: PowerSource::Ac,
                percent: None,
            }
        } else {
            Power {
                source: PowerSource::Battery,
                percent: Some(self.pct),
            }
        })
    }
    fn stop_demo(&mut self) -> Result<()> {
        self.stops += 1;
        Ok(())
    }
}
#[test]
fn battery_watch_records_six_samples_and_exact_stop_percentage() {
    let mut f = PowerFake {
        pct: 35,
        ..PowerFake::default()
    };
    let mut w = PowerWatch::new(Span::from_millis(60000)).unwrap();
    w.check(&mut f).unwrap();
    f.now += 60000;
    f.pct = 25;
    w.check(&mut f).unwrap();
    f.now += 60000;
    f.pct = 24;
    assert_eq!(w.check(&mut f).unwrap_err().code, "battery_floor");
    assert_eq!(w.samples.len(), 6);
    assert_eq!(w.percentages[1], Some(24));
    assert_eq!(f.stops, 1);
    assert_eq!(w.check(&mut f).unwrap_err().kind, FaultKind::Admission);
    assert_eq!(f.stops, 1);
}
#[test]
fn initial_transport_failure_keeps_preflight_reason_and_cause() {
    let mut f = PowerFake {
        remote_failure: true,
        ..PowerFake::default()
    };
    let mut w = PowerWatch::new(Span::from_millis(60000)).unwrap();
    assert_eq!(
        w.check(&mut f).unwrap_err().code,
        "preflight_ssh_unreachable"
    );
    assert_eq!(w.causes, ["command_failed:ssh:rc255"]);
    let ledger = serde_json::to_value(&w.cause_ledger).unwrap();
    assert_eq!(
        ledger,
        serde_json::json!([{"code":"command_failed:ssh:rc255","class":"Failure","kind":"observation"}])
    );
    assert_eq!(f.reads, [Host::Owner, Host::Member]);
}
#[derive(Default)]
struct AwakeFake {
    started: Vec<Vec<String>>,
    table: BTreeMap<u32, ProcessIdentity>,
    record: Vec<ProcessIdentity>,
    killed: Vec<u32>,
    mode: u32,
}
impl AwakeIo for AwakeFake {
    fn spawn(&mut self, argv: Vec<String>) -> Result<ProcessIdentity> {
        let pid = 701 + self.started.len() as u32;
        let p = ProcessIdentity {
            pid,
            birth: format!("birth {pid}"),
            executable: argv[0].clone(),
        };
        self.started.push(argv);
        self.table.insert(pid, p.clone());
        Ok(p)
    }
    fn write_record(&mut self, p: &[ProcessIdentity], mode: u32) -> Result<()> {
        self.record = p.to_vec();
        self.mode = mode;
        Ok(())
    }
    fn read_record(&mut self) -> Result<Vec<ProcessIdentity>> {
        Ok(self.record.clone())
    }
    fn identity(&mut self, pid: u32) -> Result<Option<ProcessIdentity>> {
        Ok(self.table.get(&pid).cloned())
    }
    fn stop_exact(&mut self, p: &ProcessIdentity) -> Result<()> {
        assert_eq!(self.table.get(&p.pid), Some(p));
        self.killed.push(p.pid);
        self.table.remove(&p.pid);
        Ok(())
    }
}
#[test]
fn awake_lease_has_bounded_sleep_exact_reverse_cleanup_and_private_record() {
    let mut f = AwakeFake::default();
    start_awake(&mut f, Span::from_millis(2100000)).unwrap();
    assert_eq!(
        f.started,
        [
            vec!["/bin/sleep", "2100"],
            vec!["/usr/bin/caffeinate", "-i", "-w", "701"]
        ]
    );
    stop_awake(&mut f).unwrap();
    assert_eq!(f.killed, [702, 701]);
    assert_eq!(f.mode, 0o600);
    assert!(f.table.is_empty());
}
#[test]
fn awake_pid_reuse_refuses_before_any_signal() {
    let mut f = AwakeFake::default();
    start_awake(&mut f, Span::from_millis(2100000)).unwrap();
    f.table.get_mut(&701).unwrap().birth = "reused".into();
    assert_eq!(
        stop_awake(&mut f).unwrap_err().code,
        "awake_identity_changed"
    );
    assert!(f.killed.is_empty());
}
#[derive(Default)]
struct TransferFake {
    local: Option<Vec<u8>>,
    remote: bool,
    commands: Vec<String>,
    fail: bool,
    mode: u32,
    cleanups: Vec<&'static str>,
}
impl TransferIo for TransferFake {
    fn create_local(&mut self, bytes: &[u8], mode: u32) -> Result<String> {
        self.local = Some(bytes.to_vec());
        self.mode = mode;
        Ok("/scratch/join-one.private".into())
    }
    fn copy(&mut self, local: &str, remote: &str) -> Result<()> {
        self.commands.push(format!("scp -q -p {local} {remote}"));
        self.remote = true;
        if self.fail {
            Err(Fault::new("fixture_copy_failure", FaultKind::Observation))
        } else {
            Ok(())
        }
    }
    fn remove_local(&mut self, _: &str) -> Result<()> {
        self.cleanups.push("local");
        self.local = None;
        Ok(())
    }
    fn remove_remote(&mut self, path: &str) -> Result<()> {
        self.cleanups.push("remote");
        self.commands.push(format!("device-helper unlink {path}"));
        self.remote = false;
        Ok(())
    }
}
#[test]
fn private_transfer_cleans_both_on_copy_failure_without_link_in_argv() {
    for fail in [false, true] {
        let mut f = TransferFake {
            fail,
            ..TransferFake::default()
        };
        let r = with_private_transfer(
            &mut f,
            b"PRIVATE-LINK",
            "/fixture/remote.private",
            |io, _| {
                assert_eq!(io.local.as_deref(), Some(b"PRIVATE-LINK".as_slice()));
                assert_eq!(io.mode, 0o600);
                Ok(())
            },
        );
        assert_eq!(r.is_err(), fail);
        if let Err(e) = r {
            assert_eq!(e.first.code, "fixture_copy_failure");
        }
        assert!(f.local.is_none());
        assert!(!f.remote);
        assert_eq!(f.cleanups, ["local", "remote"]);
        assert!(!f.commands.join(" ").contains("PRIVATE-LINK"));
        assert!(f.commands.last().unwrap().contains("unlink"));
    }
}
