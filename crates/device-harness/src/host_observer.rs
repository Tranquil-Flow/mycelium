// SPDX-License-Identifier: AGPL-3.0-only
//! Paired host metrics command adapter; native status/mailbox remain injected.
//! Qualification here is fake-only: no subprocess, SSH or application is opened.
use crate::{
    Fault, FaultKind, Host, Result,
    effects::{Clock, CommandLimits, RetryReceipt, Transport},
    observe::{Mailbox, Metrics, Observer},
};
use serde_json::Value;

/// Native status/session and mailbox-file adapters have separate authority.
pub trait StatusMailbox {
    fn status(&mut self, host: Host) -> Result<(u16, Value)>;
    fn mailbox(&mut self, host: Host) -> Result<Mailbox>;
}
pub struct CommandObserver<T, C, S> {
    pub transport: T,
    pub clock: C,
    pub evidence: S,
    pub member_host: String,
    pub limits: CommandLimits,
    pub max_output_bytes: usize,
    pub retries: Vec<RetryReceipt>,
}
impl<T: Transport, C: Clock, S: StatusMailbox> Observer for CommandObserver<T, C, S> {
    fn status(&mut self, host: Host) -> Result<(u16, Value)> {
        self.evidence.status(host)
    }
    fn mailbox(&mut self, host: Host) -> Result<Mailbox> {
        self.evidence.mailbox(host)
    }
    fn metrics(&mut self, host: Host) -> Result<Metrics> {
        if self.max_output_bytes == 0 {
            return Err(Fault::new(
                "host_metrics_output_limit_invalid",
                FaultKind::Input,
            ));
        }
        let remote = match host {
            Host::Owner => None,
            Host::Member => Some(self.member_host.as_str()),
        };
        // Validate both fixed argv recipes before the first effect. Remote host
        // is an allow-listed SSH alias, never caller-provided shell source.
        let load =
            crate::host_tools::helper_command("/usr/sbin/sysctl", &["-n", "vm.loadavg"], remote)?;
        let cpu =
            crate::host_tools::helper_command("/bin/ps", &["-axo", "pid=,pcpu=,comm="], remote)?;
        let mut read = |command| -> Result<String> {
            let out = crate::effects::execute(
                &mut self.transport,
                &mut self.clock,
                command,
                self.limits,
                &mut self.retries,
            )?;
            if out.stdout.len() > self.max_output_bytes || out.stderr.len() > self.max_output_bytes
            {
                return Err(Fault::new(
                    "host_metrics_output_too_large",
                    FaultKind::Observation,
                ));
            }
            Ok(out.stdout)
        };
        let invalid = || Fault::new("host_metrics_unobserved", FaultKind::Observation);
        let raw = read(&load)?;
        let inner = raw
            .trim()
            .strip_prefix('{')
            .and_then(|s| s.strip_suffix('}'))
            .ok_or_else(invalid)?;
        let mut words = inner.split_whitespace();
        let mut load = [0.; 3];
        for value in &mut load {
            *value = words
                .next()
                .ok_or_else(invalid)?
                .parse::<f64>()
                .map_err(|_| invalid())?;
            if !value.is_finite() || *value < 0. {
                return Err(invalid());
            }
        }
        if words.next().is_some() {
            return Err(invalid());
        }
        let observed = crate::observe::metrics(load, &read(&cpu)?)?;
        if !observed.valid() {
            return Err(invalid());
        }
        Ok(observed)
    }
}
