// SPDX-License-Identifier: AGPL-3.0-only
//! Foundation policy MODEL, not a port of worker, transport or consent verification.
//! All configured values are in [`table`]. A configured allowance is not a proven
//! latency bound. In particular v0.1.4 full restart admission remains BLOCKED.
//!
//! Bytes and durations cannot be interchanged:
//! ```compile_fail
//! use budgets::{Bytes, Span};
//! Span::ZERO.checked_add(Bytes::ZERO);
//! ```
//! Absolute wall expiries cannot be used as monotonic timestamps:
//! ```compile_fail
//! use budgets::{ClockDomain, MonoInstant, WallExpiry};
//! MonoInstant::new(ClockDomain(0), 0).elapsed_since(WallExpiry::from_unix_millis(0));
//! ```
mod authority;
mod calibration;
mod relay;
mod resources;
mod units;
pub use authority::*;
pub use calibration::*;
pub use relay::*;
pub use resources::*;
#[cfg(feature = "trace-reads")]
pub mod reads;
pub mod table;
pub use table::{BudgetId, Phase};
pub use units::*;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Refusal {
    ArithmeticOverflow,
    ArithmeticUnderflow,
    DivisionByZero,
    WrongUnit(BudgetId),
    MissingBudgetContext(BudgetId),
    BlockedNoP99(Phase),
    UnboundedClose,
    InvalidEvidence,
    RenewalTooLate,
    StopBelowClose,
    RolloverTooSmall,
    BelowTwiceP99(Phase),
    ConsentTooSmall,
    MissingMemberGrant,
    InvalidWall,
    AuthorityMismatch,
    UnsafeSchedule,
    EmptyMembers,
    ClockDomainMismatch,
    ClockWentBackwards,
    Expired,
    InvalidRelayWindow,
    StorageCap,
    MemoryCap,
    MailboxCap,
    ReplayCapacity,
    ReplayHistoryMissing,
    DuplicateReservation,
    UnknownReservation,
    CleanupUnproven,
}

/// Structural coherence, separately from latency qualification.
pub fn require_renewal(ttl: Span, interval: Span) -> Result<(), Refusal> {
    let third = ttl.checked_div(BudgetId::RenewalDivisor.count()?.get())?;
    if interval == Span::ZERO || interval > third {
        Err(Refusal::RenewalTooLate)
    } else {
        Ok(())
    }
}
pub fn require_stop(stop: Span, close: CloseBound) -> Result<(), Refusal> {
    match close {
        CloseBound::Unbounded => Err(Refusal::UnboundedClose),
        CloseBound::Finite(bound) if stop < bound => Err(Refusal::StopBelowClose),
        CloseBound::Finite(_) => Ok(()),
    }
}
pub fn require_rollover(
    reserve: Span,
    stop: Span,
    start: Span,
    safety: Span,
) -> Result<(), Refusal> {
    let required = stop.checked_add(start)?.checked_add(safety)?;
    if reserve < required {
        Err(Refusal::RolloverTooSmall)
    } else {
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CloseBound {
    Finite(Span),
    Unbounded,
}
