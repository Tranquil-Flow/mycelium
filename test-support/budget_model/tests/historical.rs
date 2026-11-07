// SPDX-License-Identifier: AGPL-3.0-only
// Faithful MODEL faults, not executions of the historical Python product.
use budgets::{BudgetId::*, *};
fn d(id: BudgetId) -> Span {
    id.span(Entries::new(2)).unwrap()
}
fn t(raw: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(1), raw)
}

#[test]
fn f15b_historical_owner_fallback_is_rejected_corrected_proposal_accepted() {
    let authority = Authority::OwnerLocal {
        proposal: d(WorkerDefaultWall),
    };
    assert_eq!(
        require_effective_wall(
            authority,
            d(UnknownMemberWall),
            AuthoritySource::UnknownMemberFallback
        ),
        Err(Refusal::AuthorityMismatch)
    );
    assert_eq!(
        require_effective_wall(
            authority,
            d(WorkerDefaultWall),
            AuthoritySource::LocalProposal
        ),
        Ok(())
    );
}
#[test]
fn f15_historical_owner_only_schedule_rejected_member_schedule_accepted() {
    let owner = WorkerWindow {
        origin: t(0),
        wall: d(WorkerDefaultWall),
        authorized_until: t(d(WorkerDefaultWall).millis()),
    };
    let member = WorkerWindow {
        wall: d(MemberChosenWall),
        ..owner
    };
    // Historical fault: all worker limits are assigned the owner's proposal.
    let historical = RunDeadlines {
        rollover_at: t(1_680_000),
        ceiling_at: t(1_770_000),
        stop_by: t(1_790_000),
    };
    let corrected = RunDeadlines {
        rollover_at: t(480_000),
        ceiling_at: t(570_000),
        stop_by: t(590_000),
    };
    assert_eq!(
        require_deadlines(&[owner, member], t(0), historical),
        Err(Refusal::UnsafeSchedule)
    );
    assert_eq!(require_deadlines(&[owner, member], t(0), corrected), Ok(()));
    assert_eq!(corrected.rollover_at.ticks(), 480_000);
    assert_eq!(corrected.ceiling_at.ticks(), 570_000);
}
#[test]
fn f4_historical_roll_at_hard_worker_limit_rejected_early_roll_accepted() {
    let window = WorkerWindow {
        origin: t(0),
        wall: d(WorkerDefaultWall),
        authorized_until: t(d(WorkerDefaultWall).millis()),
    };
    let corrected = RunDeadlines {
        rollover_at: t(1_680_000),
        ceiling_at: t(1_770_000),
        stop_by: t(1_790_000),
    };
    let historical = RunDeadlines {
        rollover_at: window.authorized_until,
        ..corrected
    };
    assert_eq!(
        require_deadlines(&[window], t(0), historical),
        Err(Refusal::UnsafeSchedule)
    );
    assert_eq!(require_deadlines(&[window], t(0), corrected), Ok(()));
}
#[test]
fn f1_frozen_membership_pin_refuses_later_request_renewed_pin_accepts() {
    let old_end = t(d(MembershipTtl).millis());
    let refresh_at = old_end.checked_sub(d(LeaseRenewalInterval)).unwrap();
    let later_request = old_end.checked_add(d(OwnerPoll)).unwrap();
    let mut pin = LeasePin::new(
        WallExpiry::from_unix_millis(old_end.ticks()),
        WallExpiry::from_unix_millis(0),
        t(0),
        t(d(WorkerDefaultWall).millis()),
    )
    .unwrap();
    let new_expiry =
        WallExpiry::from_unix_millis(refresh_at.checked_add(d(MembershipTtl)).unwrap().ticks());
    let renewed = pin
        .refresh(
            new_expiry,
            WallExpiry::from_unix_millis(refresh_at.ticks()),
            refresh_at,
        )
        .unwrap();
    assert_eq!(old_end.remaining(later_request), Err(Refusal::Expired));
    assert!(renewed.remaining(later_request).is_ok());
}
#[test]
fn f13_old_hard_timer_refused_renewed_window_accepted() {
    let old_end = t(d(RelayWindow).millis());
    let renew_at = old_end.checked_sub(d(RelayRenewalLead)).unwrap();
    let request = old_end.checked_add(d(OwnerPoll)).unwrap();
    let mut relay = RelayBudget::new(t(0), d(RelayWindow)).unwrap();
    assert_eq!(
        relay.step(renew_at, ServingProbe::Serving),
        Ok(RelayAction::Renewed)
    );
    assert_eq!(old_end.remaining(request), Err(Refusal::Expired));
    assert!(relay.deadline().remaining(request).is_ok());
}
#[test]
fn f16_four_caps_and_unbounded_close_are_not_misrepresented() {
    for (wall_id, expected_stop) in [
        (UnknownMemberWall, 20_000),
        (MemberChosenWall, 110_000),
        (WorkerDefaultWall, 110_000),
        (WorkerMaximumWall, 110_000),
    ] {
        let effective = EffectiveStopAllowance
            .wall_span(d(wall_id), Entries::new(2))
            .unwrap();
        assert_eq!(effective.millis(), expected_stop);
        assert_eq!(
            require_stop(effective, CloseBound::Finite(d(ConfiguredCloseWaitSum))),
            Err(Refusal::StopBelowClose)
        );
        assert_eq!(
            require_stop(effective, CloseBound::Unbounded),
            Err(Refusal::UnboundedClose)
        );
        assert_eq!(
            require_rollover(
                EffectiveRolloverLead
                    .wall_span(d(wall_id), Entries::new(2))
                    .unwrap(),
                d(ObservedStop).checked_mul(2).unwrap(),
                d(ObservedStart).checked_mul(2).unwrap(),
                d(WorkerCleanupReserve)
            ),
            Err(Refusal::RolloverTooSmall)
        );
    }
    assert_eq!(d(StartupWaitSum).millis(), 12_580_000);
    assert_eq!(
        ConfiguredCloseWaitSum
            .span(Entries::new(8))
            .unwrap()
            .millis(),
        15_133_500
    );
    // GREEN is only the incident-sized cleanup fit, never full restart qualification.
    assert_eq!(
        require_stop(
            EffectiveStopAllowance
                .wall_span(d(MemberChosenWall), Entries::new(2))
                .unwrap(),
            CloseBound::Finite(d(ObservedStop))
        ),
        Ok(())
    );
    assert_eq!(
        CorrectedRestart.span(Entries::new(2)),
        Err(Refusal::BlockedNoP99(Phase::Start))
    );
}
