// SPDX-License-Identifier: AGPL-3.0-only
use crate::{
    Fault, FaultKind, Result,
    effects::{Command, Transport},
    observe::Metrics,
};
use budgets::Span;
#[derive(Debug)]
pub struct AppleEvent {
    pub rc: i32,
    pub error_code: Option<i32>,
}
fn local(program: &str, args: &[&str]) -> Command {
    Command {
        kind: crate::effects::CommandKind::Local,
        program: program.into(),
        args: args.iter().map(|s| s.to_string()).collect(),
    }
}
pub fn apple_event<T: Transport>(io: &mut T, bundle: &str, bound: Span) -> Result<AppleEvent> {
    if !bundle.starts_with('/') || !bundle.ends_with(".app") {
        return Err(Fault::new("bundle_path_invalid", FaultKind::Input));
    }
    let out = io.execute(
        &local(
            "/usr/bin/osascript",
            &[
                "-e",
                "on run argv",
                "-e",
                "tell application (item 1 of argv) to quit",
                "-e",
                "end run",
                bundle,
            ],
        ),
        bound,
    )?;
    // Preserve the known Apple Event refusal code, never arbitrary stderr prose.
    let error_code = if out.stderr.contains("(-1743)") {
        Some(-1743)
    } else {
        None
    };
    Ok(AppleEvent {
        rc: out.rc,
        error_code,
    })
}
pub fn os_version<T: Transport>(io: &mut T, bound: Span) -> Result<String> {
    let out = io.execute(&local("/usr/bin/sw_vers", &["-productVersion"]), bound)?;
    let s = out.stdout.trim();
    if out.rc != 0
        || s.is_empty()
        || !s
            .split('.')
            .all(|p| !p.is_empty() && p.bytes().all(|c| c.is_ascii_digit()))
    {
        return Err(Fault::new("os_version_unobserved", FaultKind::Observation));
    }
    Ok(s.into())
}
pub fn cpu_metrics<T: Transport>(io: &mut T, load: [f64; 3], bound: Span) -> Result<Metrics> {
    let out = io.execute(&local("/bin/ps", &["-axo", "pid=,pcpu=,comm="]), bound)?;
    if out.rc != 0 {
        return Err(Fault::new("cpu_observation_failed", FaultKind::Observation));
    }
    crate::observe::metrics(load, &out.stdout)
}
pub fn helper_command(helper: &str, args: &[&str], remote: Option<&str>) -> Result<Command> {
    if !helper.starts_with('/') || helper.contains('\0') || args.iter().any(|s| s.contains('\0')) {
        return Err(Fault::new("helper_arguments_invalid", FaultKind::Input));
    }
    if let Some(host) = remote {
        if host.is_empty()
            || host.starts_with('-')
            || !host
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || b".-_".contains(&c))
        {
            return Err(Fault::new("remote_host_invalid", FaultKind::Input));
        }
        let command = std::iter::once(helper)
            .chain(args.iter().copied())
            .map(crate::effects::shell_quote)
            .collect::<Vec<_>>()
            .join(" ");
        Ok(Command {
            kind: crate::effects::CommandKind::Ssh,
            program: "ssh".into(),
            args: vec![host.into(), command],
        })
    } else {
        Ok(local(helper, args))
    }
}
