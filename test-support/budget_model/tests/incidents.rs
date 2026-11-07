// SPDX-License-Identifier: AGPL-3.0-only
// Independent historical relation expectations. Numeric assertion RHS values
// are observations/oracles, not configured timeout arguments.
use budgets::{BudgetId::*, *};
fn d(id: BudgetId) -> Span {
    id.span(Entries::new(2)).unwrap()
}
fn t(ticks: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(1), ticks)
}
fn b(id: BudgetId) -> Bytes {
    id.bytes().unwrap()
}

#[test]
fn f14_storage_red_green() {
    let cap = Resources {
        storage: b(SavedStorageConsent),
        memory: Bytes::ZERO,
    };
    let historical = Resources {
        storage: b(HistoricalOwnerShare),
        memory: Bytes::ZERO,
    };
    assert_eq!(historical.require_within(cap), Err(Refusal::StorageCap));
    let corrected = Resources {
        storage: b(CorrectedOwnerShare),
        memory: Bytes::ZERO,
    };
    assert_eq!(corrected.require_within(cap), Ok(()));
    assert_eq!(historical.storage.get(), 2_227_449_134);
    assert_eq!(cap.storage.get(), 2_000_000_000);
    assert_eq!(b(FreshStorageConsent).get(), 4_000_000_000);
}
#[test]
fn f15b_genuine_owner_and_unknown_member_are_distinct() {
    let proposal = d(WorkerDefaultWall);
    let owner = effective_wall(Authority::OwnerLocal { proposal }).unwrap();
    assert_eq!(owner.limit().millis(), 1_800_000);
    assert_eq!(owner.source(), AuthoritySource::LocalProposal);
    assert_eq!(owner.admit(proposal, None), Ok(()));
    let member = effective_wall(Authority::Member {
        proposal,
        reported_cap: None,
    })
    .unwrap();
    assert_eq!(member.limit().millis(), 60_000);
    assert_eq!(member.source(), AuthoritySource::UnknownMemberFallback);
    assert_eq!(
        member.admit(d(UnknownMemberWall), None),
        Err(Refusal::MissingMemberGrant)
    );
    assert_eq!(
        member.admit(d(UnknownMemberWall), Some(d(UnknownMemberWall))),
        Ok(())
    );
    assert_eq!(
        member.admit(proposal, Some(proposal)),
        Err(Refusal::ConsentTooSmall)
    );
}
#[test]
fn consent_never_expands_to_fit_work() {
    let member = effective_wall(Authority::Member {
        proposal: d(WorkerDefaultWall),
        reported_cap: Some(d(MemberChosenWall)),
    })
    .unwrap();
    assert_eq!(member.limit().millis(), 600_000);
    assert_eq!(
        member.admit(d(WorkerDefaultWall), Some(d(MemberChosenWall))),
        Err(Refusal::ConsentTooSmall)
    );
    assert_eq!(
        member.admit(d(MemberChosenWall), Some(d(MemberChosenWall))),
        Ok(())
    );
}
#[test]
fn f15_absolute_member_deadline_red_green_and_late_join() {
    let owner = WorkerWindow {
        origin: t(0),
        wall: d(WorkerDefaultWall),
        authorized_until: t(d(WorkerDefaultWall).millis()),
    };
    let member = WorkerWindow {
        origin: t(0),
        wall: d(MemberChosenWall),
        authorized_until: owner.authorized_until,
    };
    let corrected = deadlines(&[owner, member], t(0)).unwrap();
    assert_eq!(corrected.rollover_at.ticks(), 480_000);
    assert_eq!(corrected.ceiling_at.ticks(), 570_000);
    assert_eq!(corrected.stop_by.ticks(), 590_000);
    // An owner-only schedule (the historical fault) misses the member deadline.
    assert!(deadlines(&[owner], t(0)).unwrap().rollover_at.ticks() > corrected.stop_by.ticks());
    let late = WorkerWindow {
        origin: t(d(MembershipTtl).millis()),
        ..member
    };
    let joined = deadlines(&[owner, member, late], late.origin).unwrap();
    assert_eq!(joined, corrected); // a late join cannot reset another worker's origin
    assert_eq!(
        deadlines(&[member], member.authorized_until),
        Err(Refusal::Expired)
    );
}
#[test]
fn f1_lease_pin_refresh_rollback_and_forward_expiry() {
    let ttl = d(MembershipTtl).millis();
    let authority = t(d(WorkerDefaultWall).millis());
    let mut pin = LeasePin::new(
        WallExpiry::from_unix_millis(ttl * 2),
        WallExpiry::from_unix_millis(ttl),
        t(0),
        authority,
    )
    .unwrap();
    let original = pin.deadline();
    pin.refresh(
        WallExpiry::from_unix_millis(ttl * 2),
        WallExpiry::from_unix_millis(0),
        t(ttl / 3),
    )
    .unwrap();
    assert_eq!(pin.deadline(), original); // historical retranslating model extends here
    pin.refresh(
        WallExpiry::from_unix_millis(ttl * 3),
        WallExpiry::from_unix_millis(ttl * 2 - ttl / 3),
        t(ttl - ttl / 3),
    )
    .unwrap();
    assert!(pin.deadline().ticks() > original.ticks());
    assert!(pin.deadline().ticks() <= authority.ticks());
    assert_eq!(
        pin.refresh(
            WallExpiry::from_unix_millis(ttl * 3),
            WallExpiry::from_unix_millis(ttl * 3),
            t(ttl)
        ),
        Err(Refusal::Expired)
    );
}
#[test]
fn f13_quick_relay_crosses_original_hard_close_without_regrant() {
    let window = d(RelayWindow);
    let lead = d(RelayRenewalLead);
    let mut relay = RelayBudget::new(t(0), window).unwrap();
    assert_eq!(
        relay.step(
            t(window.checked_sub(lead).unwrap().millis()),
            ServingProbe::Serving
        ),
        Ok(RelayAction::Renewed)
    );
    assert_eq!(
        relay.step(t(window.millis()), ServingProbe::Serving),
        Ok(RelayAction::Waiting)
    );
    assert!(!relay.is_closed()); // historical hard timer closes here
    let deadline = relay.deadline();
    assert_eq!(
        relay.step(deadline.checked_sub(lead).unwrap(), ServingProbe::Failed),
        Ok(RelayAction::Waiting)
    );
    assert_eq!(
        relay.step(deadline, ServingProbe::Failed),
        Ok(RelayAction::Closed)
    );
    assert_eq!(
        relay.step(deadline, ServingProbe::Serving),
        Ok(RelayAction::Closed)
    );
}
#[test]
fn f4_worker_not_extended_rollover_precedes_worker_expiry() {
    let window = WorkerWindow {
        origin: t(0),
        wall: d(WorkerDefaultWall),
        authorized_until: t(d(WorkerDefaultWall).millis()),
    };
    let plan = deadlines(&[window], t(0)).unwrap();
    assert_eq!(plan.rollover_at.ticks(), 1_680_000);
    assert_eq!(plan.stop_by.ticks(), 1_790_000);
    assert!(plan.rollover_at.ticks() < plan.stop_by.ticks());
    assert_eq!(window.wall.millis(), 1_800_000);
}
#[test]
fn f2_retention_red_green_keeps_replay_history_accounted() {
    let history = b(ReplayHistoryPerSigner);
    let good = RetentionFootprint {
        signers: Entries::new(1),
        max_rows_per_signer: ReplayKeep.count().unwrap(),
        retained_row_bytes: b(ReplayPage)
            .checked_mul(ReplayKeep.count().unwrap().get())
            .unwrap(),
        retained_history_bytes: history,
        active_command_bytes: Bytes::ZERO,
        database_overhead: Bytes::ZERO,
    };
    assert_eq!(good.check(), Ok(()));
    assert_eq!(b(MailboxCeiling).get(), 134_217_728);
    let unpruned = RetentionFootprint {
        retained_row_bytes: b(MailboxCeiling),
        ..good
    };
    assert_eq!(unpruned.check(), Err(Refusal::MailboxCap));
    let discarded_history = RetentionFootprint {
        retained_history_bytes: Bytes::ZERO,
        ..good
    };
    assert_eq!(
        discarded_history.check(),
        Err(Refusal::ReplayHistoryMissing)
    );
    let pruned = RetentionFootprint {
        retained_row_bytes: Bytes::ZERO,
        ..good
    };
    assert_eq!(pruned.total().unwrap(), history);
    assert_eq!(pruned.check(), Ok(()));
}
#[test]
fn f16_observation_is_not_p99_and_missing_profile_blocks() {
    let profile = PhaseProfile {
        phase: Phase::Stop,
        statistic: PhaseStatistic::Observation {
            duration: d(ObservedStop),
            evidence: "L13 single observation",
        },
    };
    assert_eq!(
        require_p99(d(PreviousClose), profile),
        Err(Refusal::BlockedNoP99(Phase::Stop))
    );
    let missing = PhaseProfile {
        phase: Phase::Stop,
        statistic: PhaseStatistic::Missing,
    };
    assert_eq!(
        require_p99(d(PreviousClose), missing),
        Err(Refusal::BlockedNoP99(Phase::Stop))
    );
}
