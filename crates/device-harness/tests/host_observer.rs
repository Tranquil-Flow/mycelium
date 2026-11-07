// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{
    Fault, FaultKind, Host, Result,
    effects::{Clock, Command, CommandKind, CommandLimits, Output, Transport},
    host_observer::{CommandObserver, StatusMailbox},
    observe::{self, Mailbox, Observer},
};
use std::{cell::Cell, collections::VecDeque, rc::Rc};
const PS: &str = "12 0.125 /usr/bin/low\n21 8 /opt/worker\n20 8 /opt/worker\n30 7 /bin/other\n40 4 /bin/last\n50 0 /bin/omitted\n";
#[derive(Clone, Default)]
struct Time(Rc<Cell<u64>>);
impl Clock for Time {
    fn now(&self) -> u64 {
        self.0.get()
    }
    fn sleep(&mut self, span: Span) -> Result<()> {
        self.0.set(self.now() + span.millis());
        Ok(())
    }
}
struct Wire {
    replies: VecDeque<Output>,
    calls: Vec<(CommandKind, String, Vec<String>, u64)>,
    clock: Time,
    latency: u64,
}
impl Transport for Wire {
    fn execute(&mut self, c: &Command, bound: Span) -> Result<Output> {
        self.calls
            .push((c.kind, c.program.clone(), c.args.clone(), bound.millis()));
        self.clock.0.set(self.clock.now() + self.latency);
        Ok(self.replies.pop_front().expect("unexpected native command"))
    }
}
#[derive(Default)]
struct Evidence {
    status: Vec<Host>,
    mailbox: Vec<Host>,
}
impl StatusMailbox for Evidence {
    fn status(&mut self, host: Host) -> Result<(u16, serde_json::Value)> {
        self.status.push(host);
        if host == Host::Owner {
            Err(Fault::new("owner_unavailable", FaultKind::Observation))
        } else {
            Ok((200, serde_json::json!({"healthy":true})))
        }
    }
    fn mailbox(&mut self, host: Host) -> Result<Mailbox> {
        self.mailbox.push(host);
        observe::mailbox(host == Host::Member, true, &[Some(7), None], &[None; 4])
    }
}
fn output(rc: i32, stdout: &str, stderr: &str) -> Output {
    Output {
        rc,
        stdout: stdout.into(),
        stderr: stderr.into(),
    }
}
fn good() -> Vec<Output> {
    vec![output(0, "{ 1.25 2.5 3 }\n", ""), output(0, PS, "")]
}
fn adapter(replies: Vec<Output>) -> CommandObserver<Wire, Time, Evidence> {
    let clock = Time::default();
    CommandObserver {
        transport: Wire {
            replies: replies.into(),
            calls: vec![],
            clock: clock.clone(),
            latency: 0,
        },
        clock,
        evidence: Evidence::default(),
        member_host: "fixture-member".into(),
        limits: CommandLimits {
            attempt: Span::from_millis(25),
            retry_delay: Span::from_millis(3),
        },
        max_output_bytes: 4096,
        retries: vec![],
    }
}
#[test]
fn paired_metrics_use_fixed_tools_comm_only_and_independent_expected_values() {
    let mut io = adapter(good().into_iter().chain(good()).collect());
    for host in [Host::Owner, Host::Member] {
        let m = io.metrics(host).unwrap();
        assert_eq!(m.load, [1.25, 2.5, 3.0]);
        assert_eq!(
            m.top_cpu.iter().map(|p| p.pid).collect::<Vec<_>>(),
            [20, 21, 30, 40, 12]
        );
        assert_eq!(m.top_cpu[4].cpu_percent, 0.125);
        assert_eq!(m.top_cpu[0].command, "worker");
        assert!(m.valid());
    }
    let calls = &io.transport.calls;
    assert_eq!(calls.len(), 4);
    assert_eq!(
        calls[0],
        (
            CommandKind::Local,
            "/usr/sbin/sysctl".into(),
            vec!["-n".into(), "vm.loadavg".into()],
            25
        )
    );
    assert_eq!(
        calls[1],
        (
            CommandKind::Local,
            "/bin/ps".into(),
            vec!["-axo".into(), "pid=,pcpu=,comm=".into()],
            25
        )
    );
    for c in &calls[2..] {
        assert_eq!(c.0, CommandKind::Ssh);
        assert_eq!(c.1, "ssh");
        assert_eq!(c.2[0], "fixture-member");
        assert!(c.2[1].contains("MYCELIUM_REMOTE_EXIT="));
        assert!(!c.2[1].contains("command="));
        assert_eq!(c.3, 25);
    }
    assert!(calls[2].2[1].contains("vm.loadavg"));
    assert!(calls[3].2[1].contains("pid=,pcpu=,comm="));
    assert!(io.transport.replies.is_empty());
}
#[test]
fn metrics_refuse_malformed_nonfinite_duplicate_and_oversized_observations() {
    for load in [
        "",
        "1 2 3",
        "{ 1 2 }",
        "{ 1 2 3 4 }",
        "{ NaN 2 3 }",
        "{ inf 2 3 }",
        "{ -1 2 3 }",
        "{ 1 2 3 } extra",
    ] {
        let mut io = adapter(vec![output(0, load, "")]);
        assert_eq!(
            io.metrics(Host::Owner).unwrap_err().code,
            "host_metrics_unobserved",
            "{load}"
        );
        assert_eq!(io.transport.calls.len(), 1);
    }
    for ps in [
        "1 2 only_one",
        "1 nan invalid",
        "1 2 a\n1 3 b\n2 1 c\n3 4 d\n4 5 e",
    ] {
        let mut io = adapter(vec![output(0, "{ 1 2 3 }", ""), output(0, ps, "")]);
        assert_eq!(
            io.metrics(Host::Member).unwrap_err().code,
            "host_metrics_unobserved"
        );
    }
    let mut io = adapter(good());
    io.max_output_bytes = 4;
    assert_eq!(
        io.metrics(Host::Owner).unwrap_err().code,
        "host_metrics_output_too_large"
    );
    assert_eq!(io.transport.calls.len(), 1);
    let mut io = adapter(vec![output(0, "{ 1 2 3 }", ""), output(0, PS, "")]);
    io.max_output_bytes = 20;
    assert_eq!(
        io.metrics(Host::Owner).unwrap_err().code,
        "host_metrics_output_too_large"
    );
    assert_eq!(io.transport.calls.len(), 2);
}
#[test]
fn refused_inputs_never_start_a_native_command() {
    for host in [
        "-oProxyCommand=bad",
        "user@fixture",
        "bad;command",
        "",
        "a\nb",
    ] {
        let mut io = adapter(vec![]);
        io.member_host = host.into();
        assert_eq!(
            io.metrics(Host::Member).unwrap_err().code,
            "remote_host_invalid"
        );
        assert!(io.transport.calls.is_empty());
    }
    let mut io = adapter(vec![]);
    io.max_output_bytes = 0;
    assert_eq!(
        io.metrics(Host::Owner).unwrap_err().code,
        "host_metrics_output_limit_invalid"
    );
    assert!(io.transport.calls.is_empty());
    let mut io = adapter(vec![]);
    io.limits.attempt = Span::ZERO;
    assert_eq!(
        io.metrics(Host::Owner).unwrap_err().code,
        "invalid_command_budget"
    );
    assert!(io.transport.calls.is_empty());
}
#[test]
fn only_missing_ssh_receipt_retries_and_late_or_failed_output_stays_unobserved() {
    let mut io = adapter(
        std::iter::once(output(255, "private", "connection lost"))
            .chain(good())
            .collect(),
    );
    assert!(io.metrics(Host::Member).unwrap().valid());
    assert_eq!(io.transport.calls.len(), 3);
    assert_eq!(io.retries.len(), 1);
    assert_eq!(io.clock.now(), 3);
    for (rc, stderr) in [(255, "MYCELIUM_REMOTE_EXIT=255\n"), (1, "private")] {
        let mut io = adapter(vec![output(rc, "private", stderr)]);
        assert_eq!(io.metrics(Host::Member).unwrap_err().code, "command_failed");
        assert_eq!(io.transport.calls.len(), 1);
        assert!(io.retries.is_empty());
    }
    let mut io = adapter(vec![output(255, "private", ""), output(255, "private", "")]);
    assert_eq!(
        io.metrics(Host::Member).unwrap_err().code,
        "ssh_observation_lost"
    );
    assert_eq!(io.transport.calls.len(), 2);
    let mut io = adapter(good());
    io.transport.latency = 26;
    assert_eq!(io.metrics(Host::Owner).unwrap_err().code, "command_timeout");
    assert_eq!(io.transport.calls.len(), 1);
}
#[test]
fn owner_failures_do_not_suppress_member_metrics_or_either_mailbox() {
    let mut io = adapter(
        std::iter::once(output(1, "private", "private"))
            .chain(good())
            .collect(),
    );
    let snapshot = observe::sample(&mut io, &[]);
    assert_eq!(snapshot["owner"]["metrics"]["error"], "command_failed");
    assert_eq!(
        snapshot["member"]["metrics"]["load"],
        serde_json::json!([1.25, 2.5, 3.0])
    );
    assert_eq!(snapshot["member"]["observation"]["http_status"], 200);
    assert_eq!(io.evidence.status, [Host::Owner, Host::Member]);
    assert_eq!(io.evidence.mailbox, [Host::Owner, Host::Member]);
    assert!(!snapshot.to_string().contains("private"));
    assert_eq!(io.transport.calls.len(), 3);
}
