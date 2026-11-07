// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{BudgetId::*, *};
fn d(id: BudgetId) -> Span {
    id.span(Entries::new(2)).unwrap()
}

#[test]
fn renewal_red_and_green() {
    assert_eq!(
        require_renewal(d(MembershipTtl), d(MembershipTtl)),
        Err(Refusal::RenewalTooLate)
    );
    assert_eq!(
        require_renewal(d(MembershipTtl), d(LeaseRenewalInterval)),
        Ok(())
    );
}
#[test]
fn f16_old_stop_rejected_observed_close_fits_corrected_cap_only() {
    assert_eq!(
        require_stop(d(HistoricalOuterStop), CloseBound::Finite(d(ObservedStop))),
        Err(Refusal::StopBelowClose)
    );
    let cap = d(RolloverMargin)
        .checked_sub(d(WorkerCleanupReserve))
        .unwrap();
    assert_eq!(
        require_stop(cap, CloseBound::Finite(d(ObservedStop))),
        Ok(())
    );
    assert_eq!(
        require_stop(cap, CloseBound::Unbounded),
        Err(Refusal::UnboundedClose)
    );
}
#[test]
fn f16_full_restart_cannot_fit_and_remains_blocked() {
    assert_eq!(
        require_rollover(
            d(RolloverMargin),
            d(ObservedStop).checked_mul(2).unwrap(),
            d(ObservedStart).checked_mul(2).unwrap(),
            d(WorkerCleanupReserve)
        ),
        Err(Refusal::RolloverTooSmall)
    );
    assert_eq!(d(ConfiguredCloseWaitSum).millis(), 3_793_500);
    assert_eq!(d(ObservedRestartFloor).millis(), 396_000);
    assert_eq!(
        CorrectedRestart.span(Entries::new(2)),
        Err(Refusal::BlockedNoP99(Phase::Start))
    );
}
