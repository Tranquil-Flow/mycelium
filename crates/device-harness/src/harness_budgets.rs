// SPDX-License-Identifier: AGPL-3.0-only
//! Semantic L1 requests, never numeric fallbacks. Missing entries block live use.
use crate::native_io::FileLimits;
use crate::{Fault, FaultKind, Result};
use budgets::{
    BudgetId, Bytes, Entries, Span,
    table::{Provenance, Value},
};

pub trait Registry {
    fn span(&self, name: &str) -> Result<Span>;
    fn count(&self, name: &str) -> Result<u64>;
    fn bytes(&self, name: &str) -> Result<Bytes>;
    /// A byte limit that may be registered as "v0.1.4 ships no ceiling" (RU-014).
    /// The default reads a registered `Storage` value as [`Ceiling::Bounded`].
    fn ceiling(&self, name: &str) -> Result<Ceiling> {
        self.bytes(name).map(Ceiling::Bounded)
    }
}
/// What the registry says about a byte limit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ceiling {
    Bounded(Bytes),
    /// Registered `Blocked` with provenance "v0.1.4 ships no ceiling ... V03" (RU-014): read
    /// exactly as v0.1.4 does, with no limit of the harness's own.
    SourceUnbounded,
}
/// Tag of the deferred ceiling design for the three S25 harness limits (owner ruling
/// 2026-10-28 option a: ceilings for source-unbounded values are designed in v0.3).
pub const V03_TAG: &str = "V03-L12-1";
/// The read limit meaning "no ceiling of our own": v0.1.4's `read_text()` stops only at EOF.
pub const SOURCE_UNBOUNDED_READ: Bytes = Bytes::new(u64::MAX);
const NO_CEILING_PREFIX: &str = "v0.1.4 ships no ceiling";
pub struct L1;
impl Registry for L1 {
    fn span(&self, name: &str) -> Result<Span> {
        BudgetId::from_name(name)
            .ok_or(Fault::new(
                "harness_budget_unregistered",
                FaultKind::Admission,
            ))?
            .span(Entries::ZERO)
            .map_err(|_| Fault::new("harness_budget_wrong_unit", FaultKind::Input))
    }
    fn count(&self, name: &str) -> Result<u64> {
        BudgetId::from_name(name)
            .ok_or(Fault::new(
                "harness_budget_unregistered",
                FaultKind::Admission,
            ))?
            .count()
            .map(|n| n.get())
            .map_err(|_| Fault::new("harness_budget_wrong_unit", FaultKind::Input))
    }
    fn bytes(&self, name: &str) -> Result<Bytes> {
        BudgetId::from_name(name)
            .ok_or(Fault::new(
                "harness_budget_unregistered",
                FaultKind::Admission,
            ))?
            .bytes()
            .map_err(|_| Fault::new("harness_budget_wrong_unit", FaultKind::Input))
    }
    fn ceiling(&self, name: &str) -> Result<Ceiling> {
        let entry = BudgetId::from_name(name)
            .ok_or(Fault::new(
                "harness_budget_unregistered",
                FaultKind::Admission,
            ))?
            .entry();
        match (entry.value, entry.provenance) {
            (Value::Storage(bytes), _) => Ok(Ceiling::Bounded(bytes)),
            (Value::Blocked, Provenance::Blocked(text))
                if text.starts_with(NO_CEILING_PREFIX) && text.contains("V03") =>
            {
                Ok(Ceiling::SourceUnbounded)
            }
            // Blocked for another reason (no p99, no design): not an invitation to read unbounded.
            (Value::Blocked, _) => Err(Fault::new("harness_budget_blocked", FaultKind::Admission)),
            _ => Err(Fault::new("harness_budget_wrong_unit", FaultKind::Input)),
        }
    }
}
pub fn minimum_soak(registry: &impl Registry) -> Result<Span> {
    let value = registry.span("HarnessMinimumSoak")?;
    if value == Span::ZERO {
        return Err(Fault::new("harness_budget_invalid", FaultKind::Input));
    }
    Ok(value)
}
/// Production policy construction must use L1. Offline replay retains its
/// explicit fixture policy and never issues device/release acceptance.
pub fn bind_soak(registry: &impl Registry, policy: &mut crate::soak::Policy) -> Result<()> {
    let minimum = minimum_soak(registry)?;
    policy.validate()?;
    policy.minimum_ms = policy.minimum_ms.max(minimum.millis());
    Ok(())
}
#[derive(Clone, Copy, Debug)]
pub struct KeeperLimits {
    pub recovery_window: Span,
    pub attempts: u64,
    pub rollover_grace: Span,
    pub startup_grace: Span,
    pub member_silence: Span,
}
impl KeeperLimits {
    pub fn from_registry(r: &impl Registry) -> Result<Self> {
        let limits = Self {
            recovery_window: r.span("KeeperRecoveryWindow")?,
            attempts: r.count("KeeperRecoveryAttempts")?,
            rollover_grace: r.span("KeeperRolloverGrace")?,
            startup_grace: r.span("KeeperStartupGrace")?,
            member_silence: r.span("KeeperMemberSilence")?,
        };
        if limits.attempts == 0
            || [
                limits.recovery_window,
                limits.rollover_grace,
                limits.startup_grace,
                limits.member_silence,
            ]
            .contains(&Span::ZERO)
        {
            return Err(Fault::new("harness_budget_invalid", FaultKind::Input));
        }
        Ok(limits)
    }
}

/// v0.1.4 `reopen_native` reads `native-control.json` with `max_bytes=4096`
/// (mycelium_a13_macos/app.py:306 @ ef9ae61d): registered by L1b.
pub const CONTROL_FILE_BYTES: &str = "PyFinalMyceliumA13MacosAppMaxBytesL306C64";
/// Requested from L1b in S25; v0.1.4 never read the launch file back, so there is no
/// oracle literal to reuse. Absent until registered: fail closed.
pub const LAUNCH_FILE_BYTES: &str = "HarnessNativeLaunchFileMaxBytes";
/// Requested from L1b in S25 (harness-owned candidate-bound session record).
pub const SESSION_RECORD_BYTES: &str = "HarnessNativeSessionRecordMaxBytes";
/// Requested from L1b in S25 (largest loopback HTTP response the native wire reads).
pub const LOOPBACK_RESPONSE_BYTES: &str = "HarnessLoopbackResponseMaxBytes";

fn nonzero(bytes: Bytes) -> Result<Bytes> {
    if bytes == Bytes::ZERO {
        return Err(Fault::new("harness_budget_invalid", FaultKind::Input));
    }
    Ok(bytes)
}
/// A registered v0.1.4 ceiling is never loosened: only ids v0.1.4 never bounded may be
/// source-unbounded (RU-014); everything else must resolve to a nonzero `Storage` value.
fn limit(r: &impl Registry, name: &str, may_be_unbounded: bool) -> Result<Bytes> {
    match r.ceiling(name)? {
        Ceiling::Bounded(bytes) => nonzero(bytes),
        Ceiling::SourceUnbounded if may_be_unbounded => Ok(SOURCE_UNBOUNDED_READ),
        Ceiling::SourceUnbounded => Err(Fault::new("harness_budget_invalid", FaultKind::Input)),
    }
}
/// The only constructor a native provider may use for [`FileLimits`]: every cap is an
/// exact registry id; a missing id is an error, never a synthetic default. The launch and
/// session ids have no v0.1.4 ceiling and may be registered source-unbounded (RU-014,
/// [`V03_TAG`]); the control file keeps v0.1.4's 4096-byte ceiling.
pub fn file_limits(r: &impl Registry) -> Result<FileLimits> {
    Ok(FileLimits {
        control: limit(r, CONTROL_FILE_BYTES, false)?,
        launch: limit(r, LAUNCH_FILE_BYTES, true)?,
        session: limit(r, SESSION_RECORD_BYTES, true)?,
    })
}
pub fn loopback_response_limit(r: &impl Registry) -> Result<Bytes> {
    limit(r, LOOPBACK_RESPONSE_BYTES, true)
}
