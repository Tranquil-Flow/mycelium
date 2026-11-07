// SPDX-License-Identifier: AGPL-3.0-only
//! Native OS observations for the S14 session seam: the identity of one exact process and
//! its loopback listener, read from the operating system (`ps`, `lsof`), never from the
//! status endpoint attesting itself. Read-only, fixed absolute programs, one numeric pid;
//! commands go through the confined [`Transport`], so no helper keeps a parent descriptor.
//! This is the identity probe only: it does not admit a package, launch or stop anything.
use crate::{
    Fault, FaultKind, Result,
    effects::{Command, CommandKind, Transport},
    preflight::ProcessIdentity,
};
use budgets::Span;

fn fault(code: &'static str) -> Fault {
    Fault::new(code, FaultKind::Observation)
}

/// Independent process facts for the exact pid a candidate binding names.
pub trait Probe {
    /// The pid's start time and executable path as the OS reports them now.
    fn process(&mut self, pid: u32, bound: Span) -> Result<ProcessIdentity>;
    /// The single `127.0.0.1` TCP port that pid is listening on.
    fn listener_port(&mut self, pid: u32, bound: Span) -> Result<u16>;
}

pub struct HostProbe<T> {
    pub transport: T,
}

fn local(program: &str, args: &[&str]) -> Command {
    Command {
        kind: CommandKind::Local,
        program: program.into(),
        args: args.iter().map(ToString::to_string).collect(),
    }
}
fn pid_valid(pid: u32) -> Result<()> {
    // pid 0/1 are the kernel and launchd: never a candidate.
    if pid <= 1 || i32::try_from(pid).is_err() {
        return Err(Fault::new("process_pid_invalid", FaultKind::Input));
    }
    Ok(())
}

impl<T: Transport> Probe for HostProbe<T> {
    fn process(&mut self, pid: u32, bound: Span) -> Result<ProcessIdentity> {
        pid_valid(pid)?;
        let pid_text = pid.to_string();
        let out = self.transport.execute(
            &local(
                "/bin/ps",
                &["-o", "lstart=", "-o", "comm=", "-p", &pid_text],
            ),
            bound,
        )?;
        if out.rc != 0 {
            return Err(fault("process_identity_unobserved"));
        }
        parse_process(pid, &out.stdout)
    }
    fn listener_port(&mut self, pid: u32, bound: Span) -> Result<u16> {
        pid_valid(pid)?;
        let pid_text = pid.to_string();
        let out = self.transport.execute(
            &local(
                "/usr/sbin/lsof",
                &[
                    "-nP",
                    "-a",
                    "-p",
                    &pid_text,
                    "-iTCP@127.0.0.1",
                    "-sTCP:LISTEN",
                    "-Fn",
                ],
            ),
            bound,
        )?;
        if out.rc != 0 {
            return Err(fault("listener_unobserved"));
        }
        parse_listener(&out.stdout)
    }
}

/// English `ps -o lstart= -o comm=` on macOS emits weekday/day/month or weekday/month/day
/// depending on locale, then time/year and the absolute executable path. Exactly one
/// line. Preserve the accepted day/month birth spelling with single spaces in both cases.
pub fn parse_process(pid: u32, text: &str) -> Result<ProcessIdentity> {
    let bad = || fault("process_identity_unobserved");
    let line = text.strip_suffix('\n').unwrap_or(text);
    if line.contains(['\n', '\0', '\r']) {
        return Err(bad());
    }
    let mut rest = line.trim_start_matches(' ');
    let mut fields = Vec::with_capacity(5);
    for _ in 0..5 {
        let (field, tail) = rest.split_once(' ').ok_or_else(bad)?;
        fields.push(field);
        rest = tail.trim_start_matches(' ');
    }
    let executable = rest;
    let [weekday, first, second, clock, year] = fields[..] else {
        return Err(bad());
    };
    const DAYS: [&str; 7] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    const MONTHS: [&str; 12] = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ];
    let digits = |s: &str, n: std::ops::RangeInclusive<usize>| {
        n.contains(&s.len()) && s.bytes().all(|b| b.is_ascii_digit())
    };
    let (day, month) = if digits(first, 1..=2) && MONTHS.contains(&second) {
        (first, second)
    } else if MONTHS.contains(&first) && digits(second, 1..=2) {
        (second, first)
    } else {
        return Err(bad());
    };
    let mut hms = clock.split(':');
    let time_ok = matches!(
        (hms.next(), hms.next(), hms.next(), hms.next()),
        (Some(h), Some(m), Some(s), None)
            if digits(h, 2..=2) && digits(m, 2..=2) && digits(s, 2..=2)
                && h < "24" && m < "60" && s < "61"
    );
    if !DAYS.contains(&weekday)
        || !MONTHS.contains(&month)
        || !digits(day, 1..=2)
        || !time_ok
        || !digits(year, 4..=4)
        || !executable.starts_with('/')
    {
        return Err(bad());
    }
    Ok(ProcessIdentity {
        pid,
        birth: [weekday, day, month, clock, year].join(" "),
        executable: executable.to_owned(),
    })
}

/// `lsof -Fn` field output: `n127.0.0.1:<port>` lines. Exactly one distinct loopback port.
pub fn parse_listener(text: &str) -> Result<u16> {
    let mut ports = std::collections::BTreeSet::new();
    for name in text.lines().filter_map(|l| l.strip_prefix('n')) {
        let port = name
            .strip_prefix("127.0.0.1:")
            .filter(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()))
            .and_then(|p| p.parse::<u16>().ok())
            .filter(|p| *p != 0)
            .ok_or_else(|| fault("listener_unobserved"))?;
        ports.insert(port);
    }
    let mut it = ports.into_iter();
    match (it.next(), it.next()) {
        (Some(port), None) => Ok(port),
        (None, _) => Err(fault("listener_unobserved")),
        _ => Err(fault("listener_ambiguous")),
    }
}
