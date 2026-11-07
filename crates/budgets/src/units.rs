// SPDX-License-Identifier: AGPL-3.0-only
use crate::Refusal;

macro_rules! unit {
    ($name:ident, $new:ident, $get:ident) => {
        #[derive(Clone, Copy, Debug, Default, Eq, PartialEq, Ord, PartialOrd)]
        pub struct $name(u64);
        impl $name {
            pub const ZERO: Self = Self(0);
            pub const fn $new(value: u64) -> Self {
                Self(value)
            }
            pub const fn $get(self) -> u64 {
                self.0
            }
            pub fn checked_add(self, rhs: Self) -> Result<Self, Refusal> {
                self.0
                    .checked_add(rhs.0)
                    .map(Self)
                    .ok_or(Refusal::ArithmeticOverflow)
            }
            pub fn checked_sub(self, rhs: Self) -> Result<Self, Refusal> {
                self.0
                    .checked_sub(rhs.0)
                    .map(Self)
                    .ok_or(Refusal::ArithmeticUnderflow)
            }
            pub fn checked_mul(self, factor: u64) -> Result<Self, Refusal> {
                self.0
                    .checked_mul(factor)
                    .map(Self)
                    .ok_or(Refusal::ArithmeticOverflow)
            }
            pub fn checked_div(self, divisor: u64) -> Result<Self, Refusal> {
                self.0
                    .checked_div(divisor)
                    .map(Self)
                    .ok_or(Refusal::DivisionByZero)
            }
        }
    };
}
unit!(Span, from_millis, millis);
unit!(Bytes, new, get);
unit!(Entries, new, get);
// Dimensionless ratio in permille (1000 = 1.0), never a duration, a byte count or an entry count.
unit!(Permille, new, get);

/// Absolute wall expiry, never a relative duration or a local monotonic tick.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd)]
pub struct WallExpiry(u64);
impl WallExpiry {
    pub const fn from_unix_millis(value: u64) -> Self {
        Self(value)
    }
    pub const fn unix_millis(self) -> u64 {
        self.0
    }
    pub fn remaining(self, now: Self) -> Result<Span, Refusal> {
        if self <= now {
            return Err(Refusal::Expired);
        }
        self.0
            .checked_sub(now.0)
            .map(Span::from_millis)
            .ok_or(Refusal::ArithmeticUnderflow)
    }
}

/// Caller assigns a distinct domain per host/boot; cross-domain comparisons refuse.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ClockDomain(pub u64);
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct MonoInstant {
    domain: ClockDomain,
    ticks: u64,
}
impl MonoInstant {
    pub const fn new(domain: ClockDomain, ticks: u64) -> Self {
        Self { domain, ticks }
    }
    pub const fn ticks(self) -> u64 {
        self.ticks
    }
    pub const fn domain(self) -> ClockDomain {
        self.domain
    }
    pub fn checked_add(self, span: Span) -> Result<Self, Refusal> {
        Ok(Self::new(
            self.domain,
            self.ticks
                .checked_add(span.millis())
                .ok_or(Refusal::ArithmeticOverflow)?,
        ))
    }
    pub fn checked_sub(self, span: Span) -> Result<Self, Refusal> {
        Ok(Self::new(
            self.domain,
            self.ticks
                .checked_sub(span.millis())
                .ok_or(Refusal::ArithmeticUnderflow)?,
        ))
    }
    pub fn elapsed_since(self, earlier: Self) -> Result<Span, Refusal> {
        self.same_domain(earlier)?;
        self.ticks
            .checked_sub(earlier.ticks)
            .map(Span::from_millis)
            .ok_or(Refusal::ClockWentBackwards)
    }
    pub fn same_domain(self, other: Self) -> Result<(), Refusal> {
        if self.domain == other.domain {
            Ok(())
        } else {
            Err(Refusal::ClockDomainMismatch)
        }
    }
    pub fn earliest(self, other: Self) -> Result<Self, Refusal> {
        self.same_domain(other)?;
        Ok(if self.ticks <= other.ticks {
            self
        } else {
            other
        })
    }
    pub fn remaining(self, now: Self) -> Result<Span, Refusal> {
        self.same_domain(now)?;
        if self.ticks <= now.ticks {
            return Err(Refusal::Expired);
        }
        self.elapsed_since(now)
    }
}
