// SPDX-License-Identifier: AGPL-3.0-only
//! Exact-PID, birth and executable lookup: no pgrep/pkill/killall and no app signal.
use crate::{
    Fault, FaultKind, Result,
    effects::{Command, CommandKind, Transport},
    ownership::{OwnedProcessIo, OwnedRecord, RecordRead},
    preflight::ProcessIdentity,
    private_fs::read_private,
};
use budgets::Span;
use std::path::PathBuf;
pub fn parse_ps(stdout: &str, pid: u32) -> Result<Option<ProcessIdentity>> {
    let bad = || Fault::new("process_identity_unobserved", FaultKind::Observation);
    if stdout.trim().is_empty() {
        return Ok(None);
    }
    let mut rest = stdout.trim();
    let mut fields = Vec::new();
    for _ in 0..7 {
        rest = rest.trim_start();
        let end = rest.find(char::is_whitespace).ok_or_else(bad)?;
        fields.push(&rest[..end]);
        rest = &rest[end..];
    }
    if fields[0].parse::<u32>().ok() != Some(pid)
        || rest.trim().is_empty()
        || rest.trim().contains('\n')
    {
        return Err(bad());
    }
    if fields[6].starts_with('Z') {
        return Ok(None);
    }
    Ok(Some(ProcessIdentity {
        pid,
        birth: fields[1..6].join(" "),
        executable: rest.trim().into(),
    }))
}
pub struct StopAdapter<T> {
    pub transport: T,
    pub record_path: PathBuf,
    pub expected_executable: String,
    pub uid: u32,
    pub command_bound: Span,
}
impl<T: Transport> OwnedProcessIo for StopAdapter<T> {
    fn record(&mut self) -> Result<RecordRead> {
        let bytes = read_private(&self.record_path, self.uid, 16384)?;
        let record: OwnedRecord = serde_json::from_slice(&bytes)
            .map_err(|_| Fault::new("harness_pid_record_invalid", FaultKind::Admission))?;
        if record.process.executable != self.expected_executable {
            return Err(Fault::new(
                "harness_executable_mismatch",
                FaultKind::Admission,
            ));
        }
        Ok(RecordRead {
            record,
            private_regular_owned: true,
        })
    }
    fn identity(&mut self, pid: u32) -> Result<Option<ProcessIdentity>> {
        if pid <= 1 {
            return Err(Fault::new("unsafe_pid", FaultKind::Admission));
        }
        let command = Command {
            kind: CommandKind::Local,
            program: "/bin/ps".into(),
            args: vec![
                "-p".into(),
                pid.to_string(),
                "-o".into(),
                "pid=,lstart=,stat=,comm=".into(),
            ],
        };
        let out = self.transport.execute(&command, self.command_bound)?;
        if out.rc == 1 && out.stdout.trim().is_empty() {
            return Ok(None);
        }
        if out.rc != 0 {
            return Err(Fault::new("process_lookup_failed", FaultKind::Observation));
        }
        parse_ps(&out.stdout, pid)
    }
    fn interrupt_exact(&mut self, expected: &ProcessIdentity) -> Result<()> {
        if expected.executable != self.expected_executable
            || self.identity(expected.pid)?.as_ref() != Some(expected)
        {
            return Err(Fault::new(
                "harness_pid_identity_changed",
                FaultKind::Admission,
            ));
        }
        let command = Command {
            kind: CommandKind::Local,
            program: "/bin/kill".into(),
            args: vec!["-INT".into(), "--".into(), expected.pid.to_string()],
        };
        if self.transport.execute(&command, self.command_bound)?.rc != 0 {
            return Err(Fault::new("harness_interrupt_failed", FaultKind::Cleanup));
        }
        Ok(())
    }
    fn marker_exists(&mut self, path: &str) -> Result<bool> {
        let path = std::path::Path::new(path);
        if !path.is_absolute() || path.file_name().and_then(|v| v.to_str()) != Some("DEVICE.busy") {
            return Err(Fault::new(
                "harness_marker_path_invalid",
                FaultKind::Cleanup,
            ));
        }
        match std::fs::symlink_metadata(path) {
            Ok(_) => Ok(true),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
            Err(_) => Err(Fault::new(
                "harness_marker_unobserved",
                FaultKind::Observation,
            )),
        }
    }
    fn stop_awake_exact(&mut self, _record: &str) -> Result<()> {
        // A local stop cannot validate/stop a remote lease. The paired-device
        // adapter must implement the exact-owned lease protocol before live use.
        Err(Fault::new(
            "paired_awake_adapter_required",
            FaultKind::Cleanup,
        ))
    }
}
