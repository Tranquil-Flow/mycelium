// SPDX-License-Identifier: AGPL-3.0-only
use crate::*;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Authority {
    /// A genuine local proposal; absence of a standing member grant is normal.
    OwnerLocal { proposal: Span },
    /// A report is a planning bound, NOT authenticated permission to execute.
    Member {
        proposal: Span,
        reported_cap: Option<Span>,
    },
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum AuthoritySource {
    LocalProposal,
    MemberReport,
    UnknownMemberFallback,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EffectiveWall {
    limit: Span,
    source: AuthoritySource,
}

pub fn effective_wall(authority: Authority) -> Result<EffectiveWall, Refusal> {
    let maximum = BudgetId::WorkerMaximumWall.span(Entries::ZERO)?;
    let minimum = BudgetId::UnknownMemberWall.span(Entries::ZERO)?;
    let reserve = BudgetId::WorkerCleanupReserve.span(Entries::ZERO)?;
    let (proposal, cap, source) = match authority {
        Authority::OwnerLocal { proposal } => (proposal, proposal, AuthoritySource::LocalProposal),
        Authority::Member {
            proposal,
            reported_cap: None,
        } => (proposal, minimum, AuthoritySource::UnknownMemberFallback),
        Authority::Member {
            proposal,
            reported_cap: Some(cap),
        } => {
            if cap < minimum || cap > maximum {
                return Err(Refusal::InvalidWall);
            }
            (proposal, cap, AuthoritySource::MemberReport)
        }
    };
    let limit = proposal.min(cap).min(maximum);
    if limit <= reserve {
        return Err(Refusal::InvalidWall);
    }
    Ok(EffectiveWall { limit, source })
}
impl EffectiveWall {
    pub fn limit(self) -> Span {
        self.limit
    }
    pub fn source(self) -> AuthoritySource {
        self.source
    }
    /// In this pure model, grant inputs have already been authenticated by L5.
    /// Fallback discovery never creates a grant; refusal does not mutate consent.
    pub fn admit(self, needed: Span, member_grant: Option<Span>) -> Result<(), Refusal> {
        let cap = match self.source {
            AuthoritySource::LocalProposal => self.limit,
            _ => self
                .limit
                .min(member_grant.ok_or(Refusal::MissingMemberGrant)?),
        };
        if needed > cap {
            Err(Refusal::ConsentTooSmall)
        } else {
            Ok(())
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct WorkerWindow {
    pub origin: MonoInstant,
    pub wall: Span,
    pub authorized_until: MonoInstant,
}
pub fn require_effective_wall(
    authority: Authority,
    actual_limit: Span,
    actual_source: AuthoritySource,
) -> Result<(), Refusal> {
    let expected = effective_wall(authority)?;
    if expected.limit == actual_limit && expected.source == actual_source {
        Ok(())
    } else {
        Err(Refusal::AuthorityMismatch)
    }
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RunDeadlines {
    pub rollover_at: MonoInstant,
    pub ceiling_at: MonoInstant,
    pub stop_by: MonoInstant,
}
pub fn require_deadlines(
    windows: &[WorkerWindow],
    now: MonoInstant,
    actual: RunDeadlines,
) -> Result<(), Refusal> {
    let expected = deadlines(windows, now)?;
    actual.rollover_at.same_domain(expected.rollover_at)?;
    actual.ceiling_at.same_domain(expected.ceiling_at)?;
    actual.stop_by.same_domain(expected.stop_by)?;
    if actual == expected {
        Ok(())
    } else {
        Err(Refusal::UnsafeSchedule)
    }
}

/// Origins must already be translated into the same local host/boot domain.
pub fn deadlines(windows: &[WorkerWindow], now: MonoInstant) -> Result<RunDeadlines, Refusal> {
    let ceiling = BudgetId::ExecutionCeilingLead.span(Entries::ZERO)?;
    let safety = BudgetId::WorkerCleanupReserve.span(Entries::ZERO)?;

    let mut result: Option<RunDeadlines> = None;
    for window in windows {
        now.elapsed_since(window.origin)?;
        if window.wall <= safety {
            return Err(Refusal::InvalidWall);
        }
        let end = window
            .origin
            .checked_add(window.wall)?
            .earliest(window.authorized_until)?;
        let authorized_wall = end.elapsed_since(window.origin)?;
        if authorized_wall <= safety || authorized_wall < ceiling {
            return Err(Refusal::InvalidWall);
        }
        let lead = BudgetId::EffectiveRolloverLead.wall_span(authorized_wall, Entries::ZERO)?;
        let next = RunDeadlines {
            rollover_at: end.checked_sub(lead)?,
            ceiling_at: end.checked_sub(ceiling)?,
            stop_by: end.checked_sub(safety)?,
        };
        next.stop_by.remaining(now)?;
        result = Some(match result {
            None => next,
            Some(previous) => RunDeadlines {
                rollover_at: previous.rollover_at.earliest(next.rollover_at)?,
                ceiling_at: previous.ceiling_at.earliest(next.ceiling_at)?,
                stop_by: previous.stop_by.earliest(next.stop_by)?,
            },
        });
    }
    result.ok_or(Refusal::EmptyMembers)
}

/// Pin a signed wall expiry once; an unchanged lease cannot gain time on rollback.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct LeasePin {
    expiry: WallExpiry,
    deadline: MonoInstant,
    authority: MonoInstant,
    last: MonoInstant,
    expired: bool,
}
impl LeasePin {
    pub fn new(
        expiry: WallExpiry,
        wall_now: WallExpiry,
        mono_now: MonoInstant,
        authority: MonoInstant,
    ) -> Result<Self, Refusal> {
        let deadline = mono_now
            .checked_add(expiry.remaining(wall_now)?)?
            .earliest(authority)?;
        deadline.remaining(mono_now)?;
        Ok(Self {
            expiry,
            deadline,
            authority,
            last: mono_now,
            expired: false,
        })
    }
    pub fn deadline(self) -> MonoInstant {
        self.deadline
    }
    pub fn is_expired(self) -> bool {
        self.expired
    }
    /// Observing expiry is terminal, even if a subsequent wall sample rolls back.
    /// Arithmetic/domain errors do not mutate state; expiry records retirement.
    pub fn refresh(
        &mut self,
        expiry: WallExpiry,
        wall_now: WallExpiry,
        now: MonoInstant,
    ) -> Result<MonoInstant, Refusal> {
        now.elapsed_since(self.last)?;
        let checked = (|| {
            if self.expired {
                return Err(Refusal::Expired);
            }
            self.deadline.remaining(now)?;
            let remaining = expiry.remaining(wall_now)?;
            let translated = now.checked_add(remaining)?.earliest(self.authority)?;
            let deadline = if expiry > self.expiry {
                translated
            } else {
                self.deadline.earliest(translated)?
            };
            deadline.remaining(now)?;
            Ok(deadline)
        })();
        let deadline = match checked {
            Ok(deadline) => deadline,
            Err(Refusal::Expired) => {
                self.expired = true;
                self.last = now;
                return Err(Refusal::Expired);
            }
            Err(error) => return Err(error),
        };
        self.deadline = deadline;
        self.expiry = expiry;
        self.last = now;
        Ok(self.deadline)
    }
}
