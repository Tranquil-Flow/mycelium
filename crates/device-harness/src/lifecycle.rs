// SPDX-License-Identifier: AGPL-3.0-only
use crate::{
    Fault, FaultKind, Host, Result, admission::Power, effects::Clock, preflight::ProcessIdentity,
};
use budgets::Span;
pub trait PowerIo: Clock {
    fn power(&mut self, host: Host) -> Result<Power>;
    fn stop_demo(&mut self) -> Result<()>;
}
#[derive(Clone, Debug, serde::Serialize)]
pub struct PowerCause {
    pub code: &'static str,
    /// Compatibility class of a harness fault, never raw external exception prose.
    pub class: &'static str,
    pub kind: FaultKind,
}
pub struct PowerWatch {
    pub samples: Vec<(Host, Power)>,
    pub percentages: [Option<u8>; 2],
    pub causes: Vec<&'static str>,
    pub cause_ledger: Vec<PowerCause>,
    interval: Span,
    next: u64,
    failure: Option<Fault>,
}
impl PowerWatch {
    pub fn new(interval: Span) -> Result<Self> {
        if interval == Span::ZERO {
            return Err(Fault::new("invalid_power_interval", FaultKind::Input));
        }
        Ok(Self {
            samples: vec![],
            percentages: [None; 2],
            causes: vec![],
            cause_ledger: vec![],
            interval,
            next: 0,
            failure: None,
        })
    }
    pub fn check<I: PowerIo>(&mut self, io: &mut I) -> Result<()> {
        if let Some(e) = self.failure {
            return Err(e);
        }
        if !self.samples.is_empty() && io.now() < self.next {
            return Ok(());
        }
        let initial = self.samples.is_empty();
        self.next = io
            .now()
            .checked_add(self.interval.millis())
            .ok_or(Fault::new("power_clock_overflow", FaultKind::Observation))?;
        for (index, host) in crate::HOSTS.into_iter().enumerate() {
            let admitted = match io.power(host) {
                Ok(p) => {
                    self.samples.push((host, p));
                    self.percentages[index] = p.percent;
                    crate::admission::admit_power(p, initial)
                }
                Err(e) => {
                    self.causes.push(e.code);
                    self.cause_ledger.push(PowerCause {
                        code: e.code,
                        class: "Failure",
                        kind: e.kind,
                    });
                    Err(if initial && host == Host::Member {
                        Fault::new("preflight_ssh_unreachable", FaultKind::Observation)
                    } else {
                        e
                    })
                }
            };
            if let Err(e) = admitted {
                self.failure.get_or_insert(e);
                if initial {
                    break;
                }
            }
        }
        if let Some(e) = self.failure {
            if !initial && let Err(cleanup) = io.stop_demo() {
                self.causes.push(cleanup.code);
                self.cause_ledger.push(PowerCause {
                    code: cleanup.code,
                    class: "Failure",
                    kind: cleanup.kind,
                });
            }
            return Err(e);
        }
        Ok(())
    }
}
pub trait AwakeIo {
    fn spawn(&mut self, argv: Vec<String>) -> Result<ProcessIdentity>;
    fn write_record(&mut self, p: &[ProcessIdentity], mode: u32) -> Result<()>;
    fn read_record(&mut self) -> Result<Vec<ProcessIdentity>>;
    fn identity(&mut self, pid: u32) -> Result<Option<ProcessIdentity>>;
    fn stop_exact(&mut self, p: &ProcessIdentity) -> Result<()>;
}
pub fn start_awake<I: AwakeIo>(io: &mut I, span: Span) -> Result<()> {
    if span == Span::ZERO || !span.millis().is_multiple_of(1000) {
        return Err(Fault::new("invalid_awake_budget", FaultKind::Input));
    }
    let sleep = io.spawn(vec![
        "/bin/sleep".into(),
        (span.millis() / 1000).to_string(),
    ])?;
    let caffeinate = match io.spawn(vec![
        "/usr/bin/caffeinate".into(),
        "-i".into(),
        "-w".into(),
        sleep.pid.to_string(),
    ]) {
        Ok(p) => p,
        Err(e) => {
            io.stop_exact(&sleep)?;
            return Err(e);
        }
    };
    let processes = vec![sleep, caffeinate];
    if let Err(e) = io.write_record(&processes, 0o600) {
        let mut failure = e;
        for p in processes.iter().rev() {
            if let Err(cleanup) = io.stop_exact(p) {
                failure = cleanup;
            }
        }
        return Err(failure);
    }
    if io.read_record()? != processes {
        return Err(Fault::new(
            "awake_record_readback_failed",
            FaultKind::Observation,
        ));
    }
    Ok(())
}
pub fn stop_awake<I: AwakeIo>(io: &mut I) -> Result<()> {
    let records = io.read_record()?;
    if records.len() != 2
        || records[0].pid <= 1
        || records[1].pid <= 1
        || records[0].pid == records[1].pid
        || records[0].executable != "/bin/sleep"
        || records[1].executable != "/usr/bin/caffeinate"
        || records.iter().any(|p| p.birth.is_empty())
    {
        return Err(Fault::new("awake_record_invalid", FaultKind::Admission));
    }
    // Inspect the full record before signalling either process, then recheck each.
    for p in &records {
        if io.identity(p.pid)?.is_some_and(|current| current != *p) {
            return Err(Fault::new("awake_identity_changed", FaultKind::Cleanup));
        }
    }
    let mut failure = None;
    for p in records.iter().rev() {
        let stopped = (|| -> Result<()> {
            if let Some(current) = io.identity(p.pid)? {
                if current != *p {
                    return Err(Fault::new("awake_identity_changed", FaultKind::Cleanup));
                }
                io.stop_exact(p)?;
            }
            if io.identity(p.pid)?.is_some() {
                return Err(Fault::new("awake_cleanup_unproven", FaultKind::Cleanup));
            }
            Ok(())
        })();
        if let Err(e) = stopped {
            failure.get_or_insert(e);
        }
    }
    if let Some(e) = failure {
        return Err(e);
    }
    Ok(())
}
pub trait TransferIo {
    fn create_local(&mut self, bytes: &[u8], mode: u32) -> Result<String>;
    fn copy(&mut self, local: &str, remote: &str) -> Result<()>;
    fn remove_local(&mut self, path: &str) -> Result<()>;
    fn remove_remote(&mut self, path: &str) -> Result<()>;
}
#[derive(Debug)]
pub struct TransferFailure {
    pub first: Fault,
    pub cleanup: Vec<Fault>,
}
pub fn with_private_transfer<I: TransferIo, T>(
    io: &mut I,
    bytes: &[u8],
    remote: &str,
    use_link: impl FnOnce(&mut I, &str) -> Result<T>,
) -> std::result::Result<T, TransferFailure> {
    let failure = |first| TransferFailure {
        first,
        cleanup: vec![],
    };
    if bytes.is_empty() || bytes.len() > 16384 || !remote.starts_with('/') || remote.contains('\0')
    {
        return Err(failure(Fault::new(
            "private_transfer_invalid",
            FaultKind::Input,
        )));
    }
    let local = io.create_local(bytes, 0o600).map_err(failure)?;
    let result = io.copy(&local, remote).and_then(|()| use_link(io, remote));
    // Even a failed copy can have created a partial remote file.
    let mut cleanup = vec![];
    if let Err(e) = io.remove_local(&local) {
        cleanup.push(e);
    }
    if let Err(e) = io.remove_remote(remote) {
        cleanup.push(e);
    }
    match result {
        Err(first) => Err(TransferFailure { first, cleanup }),
        Ok(v) if cleanup.is_empty() => Ok(v),
        Ok(_) => Err(TransferFailure {
            first: cleanup[0],
            cleanup,
        }),
    }
}
