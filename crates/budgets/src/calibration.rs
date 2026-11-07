// SPDX-License-Identifier: AGPL-3.0-only
use crate::*;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PhaseStatistic<'a> {
    Missing,
    Observation {
        duration: Span,
        evidence: &'a str,
    },
    /// Upstream L2 must bind method/raw samples/device/build/load to this path.
    /// A count greater than one is necessary, not sufficient statistical qualification.
    MeasuredP99 {
        duration: Span,
        samples: Entries,
        evidence: &'a str,
    },
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PhaseProfile<'a> {
    pub phase: Phase,
    pub statistic: PhaseStatistic<'a>,
}

pub fn require_p99(budget: Span, profile: PhaseProfile<'_>) -> Result<(), Refusal> {
    // A source-site inventory does not establish a measured phase. Even a
    // p99-shaped payload cannot qualify a site whose phase is still unknown.
    if profile.phase == Phase::Unclassified {
        return Err(Refusal::BlockedNoP99(Phase::Unclassified));
    }
    match profile.statistic {
        PhaseStatistic::Missing | PhaseStatistic::Observation { .. } => {
            Err(Refusal::BlockedNoP99(profile.phase))
        }
        PhaseStatistic::MeasuredP99 {
            duration,
            samples,
            evidence,
        } => {
            if samples.get() <= 1 || evidence.trim().is_empty() || duration == Span::ZERO {
                return Err(Refusal::InvalidEvidence);
            }
            let required = duration.checked_mul(BudgetId::P99Multiplier.count()?.get())?;
            if budget < required {
                Err(Refusal::BelowTwiceP99(profile.phase))
            } else {
                Ok(())
            }
        }
    }
}

/// No observation or idle-window sum is a measured end-to-end p99.
pub fn qualify_entry(
    id: BudgetId,
    peers: Entries,
    profile: Option<PhaseProfile<'_>>,
) -> Result<(), Refusal> {
    use table::Purpose::*;
    match id.entry().purpose {
        Operational(phase) | ProgressWindow(phase) => {
            let Some(profile) = profile else {
                return Err(Refusal::BlockedNoP99(phase));
            };
            if profile.phase != phase {
                return Err(Refusal::InvalidEvidence);
            }
            require_p99(id.span(peers)?, profile)
        }
        _ => Ok(()),
    }
}
