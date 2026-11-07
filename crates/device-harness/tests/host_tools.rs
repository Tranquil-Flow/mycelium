// SPDX-License-Identifier: AGPL-3.0-only
use budgets::Span;
use device_harness::{
    Result,
    effects::{Command, Output, Transport},
    host_tools::*,
};
#[derive(Default)]
struct Fake {
    calls: Vec<Vec<String>>,
    rc: i32,
    out: String,
    err: String,
}
impl Transport for Fake {
    fn execute(&mut self, c: &Command, _: Span) -> Result<Output> {
        let mut a = vec![c.program.clone()];
        a.extend(c.args.clone());
        self.calls.push(a);
        Ok(Output {
            rc: self.rc,
            stdout: self.out.clone(),
            stderr: self.err.clone(),
        })
    }
}
#[test]
fn quit_command_targets_exact_bundle_without_signal_or_shell() {
    let mut f = Fake::default();
    let bundle = "/fixture/programs/0.1.4/Mycelium A13 Demo.app";
    let r = apple_event(&mut f, bundle, Span::from_millis(60000)).unwrap();
    assert_eq!(f.calls[0][0], "/usr/bin/osascript");
    assert_eq!(f.calls[0].last().unwrap(), bundle);
    assert!(f.calls[0].contains(&"tell application (item 1 of argv) to quit".into()));
    assert_eq!(r.rc, 0);
    let mut f = Fake {
        rc: 1,
        err: "execution error: Not authorized (-1743) PRIVATE-MATERIAL".into(),
        ..Fake::default()
    };
    let r = apple_event(&mut f, bundle, Span::from_millis(60000)).unwrap();
    assert_eq!(r.error_code, Some(-1743));
    assert!(!format!("{r:?}").contains("PRIVATE-MATERIAL"));
    assert_eq!(f.calls.len(), 1);
}
#[test]
fn os_version_and_comm_only_metrics_are_recorded() {
    let mut f = Fake {
        out: "15.6.1\n".into(),
        ..Fake::default()
    };
    assert_eq!(
        os_version(&mut f, Span::from_millis(20000)).unwrap(),
        "15.6.1"
    );
    assert_eq!(f.calls[0], ["/usr/bin/sw_vers", "-productVersion"]);
    f.out =
        "10 5 /bin/low\n20 50 /bin/worker\n30 4 /bin/x\n40 3 /bin/y\n50 2 /bin/z\n60 1 /bin/last"
            .into();
    let m = cpu_metrics(&mut f, [1., 2., 3.], Span::from_millis(20000)).unwrap();
    assert!(f.calls[1].last().unwrap().contains("comm="));
    assert!(!f.calls[1].last().unwrap().contains("command="));
    assert_eq!(m.load, [1., 2., 3.]);
    assert_eq!(m.top_cpu.len(), 5);
    assert_eq!(m.top_cpu[0].pid, 20);
    assert_eq!(m.top_cpu[0].cpu_percent, 50.);
    assert_eq!(m.top_cpu[0].command, "worker");
}
#[test]
fn helper_commands_preserve_argument_boundaries_without_python_or_environment_override() {
    let c = helper_command(
        "/fixture/device-helper",
        &["launch", "--version", "0.1.4-rc1"],
        None,
    )
    .unwrap();
    assert_eq!(c.args, ["launch", "--version", "0.1.4-rc1"]);
    let c = helper_command(
        "/fixture/member-helper",
        &["join", "/private/file with spaces"],
        Some("fixture-host"),
    )
    .unwrap();
    assert_eq!(c.program, "ssh");
    assert!(
        c.args
            .last()
            .unwrap()
            .ends_with("'/private/file with spaces'")
    );
    assert!(!c.args.join(" ").contains("--env"));
    assert!(!c.args.join(" ").contains("python"));
}
