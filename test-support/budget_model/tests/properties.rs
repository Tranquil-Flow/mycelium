// SPDX-License-Identifier: AGPL-3.0-only
//! Independent oracles use integer scalar arithmetic, never a SUT budget function.
//! Scalar literals here are oracle expectations/generator bounds, not configured
//! timeout arguments. Actual duration construction takes generated data or IDs.
use budgets::{BudgetId::*, *};
use proptest::prelude::*;
use proptest::test_runner::RngSeed;
use std::collections::BTreeMap;

fn d(id: BudgetId) -> Span {
    id.span(Entries::new(2)).unwrap()
}
fn tick(raw: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(7), raw)
}
fn span(raw: u64) -> Span {
    Span::from_millis(raw)
}
fn config(cases: u32) -> ProptestConfig {
    ProptestConfig {
        cases,
        rng_seed: RngSeed::Fixed(0xB0D6_E702),
        failure_persistence: None,
        ..ProptestConfig::default()
    }
}

proptest! {
    #![proptest_config(config(256))]
    #[test]
    fn checked_arithmetic_never_wraps(a: u64, b: u64) {
        let sum = u128::from(a) + u128::from(b);
        let product = u128::from(a) * u128::from(b);
        let expected_sum = u64::try_from(sum).map(span).map_err(|_| Refusal::ArithmeticOverflow);
        let expected_product = u64::try_from(product).map(span).map_err(|_| Refusal::ArithmeticOverflow);
        prop_assert_eq!(span(a).checked_add(span(b)), expected_sum);
        prop_assert_eq!(span(a).checked_mul(b), expected_product);
        prop_assert_eq!(span(a).checked_sub(span(b)), if a >= b { Ok(span(a-b)) } else { Err(Refusal::ArithmeticUnderflow) });
        prop_assert_eq!(Bytes::new(a).checked_add(Bytes::new(b)).map(Bytes::get), u64::try_from(sum).map_err(|_| Refusal::ArithmeticOverflow));
        prop_assert_eq!(tick(a).checked_add(span(b)).map(MonoInstant::ticks), u64::try_from(sum).map_err(|_| Refusal::ArithmeticOverflow));
    }
    #[test]
    fn renewal_valid_and_impossible(ttl: u64, interval: u64) {
        let expected = if interval > 0 && u128::from(interval)*3 <= u128::from(ttl) { Ok(()) } else { Err(Refusal::RenewalTooLate) };
        prop_assert_eq!(require_renewal(span(ttl), span(interval)), expected);
    }
    #[test]
    fn rollover_checked_sums(reserve: u64, stop: u64, start: u64, safety: u64) {
        let sum = u128::from(stop)+u128::from(start)+u128::from(safety);
        let expected = if sum > u128::from(u64::MAX) { Err(Refusal::ArithmeticOverflow) }
            else if u128::from(reserve) < sum { Err(Refusal::RolloverTooSmall) } else { Ok(()) };
        prop_assert_eq!(require_rollover(span(reserve), span(stop), span(start), span(safety)), expected);
    }
    #[test]
    fn valid_rollover_and_adjacent_impossible(stop in 1u64..1_000_000, start in 1u64..1_000_000, safety in 1u64..1_000_000) {
        let sum = stop + start + safety;
        prop_assert_eq!(require_rollover(span(sum), span(stop), span(start), span(safety)), Ok(()));
        prop_assert_eq!(require_rollover(span(sum-1), span(stop), span(start), span(safety)), Err(Refusal::RolloverTooSmall));
    }
    #[test]
    fn p99_floor_is_twice_actual_input_not_observation(budget: u64, p99 in 1u64..=u64::MAX) {
        // Synthetic test-only profile. This is never exported into the registry as a measurement.
        let profile = PhaseProfile { phase: Phase::Stop, statistic: PhaseStatistic::MeasuredP99 {
            duration: span(p99), samples: Entries::new(256), evidence: "synthetic: property-test ONLY" } };
        let floor = u128::from(p99)*2;
        let expected = if floor > u128::from(u64::MAX) { Err(Refusal::ArithmeticOverflow) }
            else if u128::from(budget) < floor { Err(Refusal::BelowTwiceP99(Phase::Stop)) } else { Ok(()) };
        prop_assert_eq!(require_p99(span(budget), profile), expected);
    }
    #[test]
    fn consent_fit_and_impossible_work(proposal in 10_001u64..7_200_001, member in 60_000u64..3_600_001, needed in 0u64..7_200_001) {
        let authority = Authority::Member { proposal: span(proposal), reported_cap: Some(span(member)) };
        let chosen = effective_wall(authority).unwrap();
        let expected = proposal.min(member).min(3_600_000);
        prop_assert_eq!(chosen.limit().millis(), expected);
        prop_assert_eq!(chosen.admit(span(needed), Some(span(member))), if needed <= expected { Ok(()) } else { Err(Refusal::ConsentTooSmall) });
        prop_assert_eq!(chosen.limit().millis(), expected); // refusal did not enlarge the grant
    }
    #[test]
    fn individual_origins_not_smallest_relative_cap(rows in prop::collection::vec((0u64..100_001, 60_000u64..3_600_001), 1..7)) {
        let now = rows.iter().map(|r| r.0).max().unwrap();
        let mut expected_roll = u64::MAX;
        let mut expected_ceiling = u64::MAX;
        let mut expected_stop = u64::MAX;
        let windows: Vec<_> = rows.iter().map(|&(origin, cap)| {
            let end = origin+cap;
            expected_roll = expected_roll.min(end-cap.div_euclid(2).min(120_000));
            expected_ceiling = expected_ceiling.min(end-30_000);
            expected_stop = expected_stop.min(end-10_000);
            WorkerWindow { origin: tick(origin), wall: span(cap), authorized_until: tick(end) }
        }).collect();
        let result = deadlines(&windows, tick(now));
        if now >= expected_stop { prop_assert_eq!(result, Err(Refusal::Expired)); }
        else {
            let result = result.unwrap();
            prop_assert_eq!(result.rollover_at.ticks(), expected_roll);
            prop_assert_eq!(result.ceiling_at.ticks(), expected_ceiling);
            prop_assert_eq!(result.stop_by.ticks(), expected_stop);
        }
    }
    #[test]
    fn retention_bytes_include_never_cleared_history(signers in 1u64..25, rows in 0u64..10_000, row_bytes in 0u64..200_000_000, overhead in 0u64..200_000_000) {
        let footprint = RetentionFootprint { signers: Entries::new(signers), max_rows_per_signer: Entries::new(rows),
            retained_row_bytes: Bytes::new(row_bytes), retained_history_bytes: Bytes::new(signers*8_388_608),
            active_command_bytes: Bytes::ZERO, database_overhead: Bytes::new(overhead) };
        let total = row_bytes+overhead+signers*8_388_608;
        prop_assert_eq!(footprint.total().unwrap().get(), total);
        let expected = if rows > 8192 { Err(Refusal::ReplayCapacity) } else if total > 134_217_728 { Err(Refusal::MailboxCap) } else { Ok(()) };
        prop_assert_eq!(footprint.check(), expected);
    }
}

proptest! {
    #![proptest_config(config(64))]
    #[test]
    fn stateful_reservations_atomic_and_cleanup_bound(actions in prop::collection::vec((0u8..3, 0u64..8, 0u64..150, any::<bool>()), 1..101)) {
        let cap = Resources { storage: Bytes::new(100), memory: Bytes::new(100) };
        let mut sut = Reservations::new(cap);
        let mut model = BTreeMap::<u64,u64>::new();
        // Mandatory positive and duplicate controls prevent an always-refuse model passing.
        let first = Reservation { staged: Bytes::new(1), staging_overhead: Bytes::ZERO, resident: Bytes::new(1), scratch: Bytes::ZERO, kv: Bytes::ZERO };
        sut.reserve(ReservationId(0), first).unwrap(); model.insert(0, 1);
        for (action,id,size,proof) in actions {
            let before = sut.clone();
            let total: u64 = model.values().sum();
            if action == 0 {
                let request = Reservation { staged: Bytes::new(size), staging_overhead: Bytes::ZERO, resident: Bytes::new(size), scratch: Bytes::ZERO, kv: Bytes::ZERO };
                let expected = if model.contains_key(&id) { Err(Refusal::DuplicateReservation) }
                    else if total+size > 100 { Err(Refusal::StorageCap) } else { Ok(()) };
                let actual = sut.reserve(ReservationId(id), request);
                prop_assert_eq!(actual, expected);
                if actual.is_ok() { model.insert(id,size); } else { prop_assert_eq!(&sut,&before); }
            } else {
                let expected = if !proof { Err(Refusal::CleanupUnproven) } else if !model.contains_key(&id) { Err(Refusal::UnknownReservation) } else { Ok(()) };
                let actual = sut.release(ReservationId(id), if proof { CleanupProof::Verified } else { CleanupProof::Unproven });
                prop_assert_eq!(actual, expected);
                if actual.is_ok() { model.remove(&id); } else { prop_assert_eq!(&sut,&before); }
            }
            let expected: u64 = model.values().sum();
            prop_assert_eq!(sut.used().storage.get(), expected);
            prop_assert_eq!(sut.used().memory.get(), expected);
        }
    }
    #[test]
    fn stateful_relay_jumps_probes_and_stop(actions in prop::collection::vec((0u64..2_000_001, any::<bool>(), any::<bool>()), 1..101)) {
        let mut sut = RelayBudget::new(tick(0), d(RelayWindow)).unwrap();
        let (mut now,mut end,mut granted,mut closed,mut capped) = (0u64,1_800_000u64,1_800_000u64,false,false);
        for (jump,serving,stop) in actions {
            now += jump;
            if stop { sut.stop(); closed = true; }
            let expected = if closed || now >= end { closed = true; RelayAction::Closed }
                else if !capped && end-now <= 60_000 && serving {
                    if granted+1_800_000 > 86_400_000 { capped = true; RelayAction::CapReached }
                    else { granted += 1_800_000; end = now+1_800_000; RelayAction::Renewed }
                } else { RelayAction::Waiting };
            prop_assert_eq!(sut.step(tick(now), if serving { ServingProbe::Serving } else { ServingProbe::Failed }), Ok(expected));
            prop_assert_eq!(sut.granted().millis(), granted);
            prop_assert_eq!(sut.deadline().ticks(), end);
            prop_assert_eq!(sut.is_closed(), closed);
        }
    }
    #[test]
    fn stateful_unchanged_lease_wall_jumps_never_regrant(actions in prop::collection::vec((0u64..10_001, 0u64..700_001), 1..101)) {
        let mut sut = LeasePin::new(WallExpiry::from_unix_millis(d(MemberChosenWall).millis()),
            WallExpiry::from_unix_millis(d(MembershipTtl).millis()), tick(0), tick(d(WorkerDefaultWall).millis())).unwrap();
        let (mut now,mut end,mut expired) = (0u64,300_000u64,false);
        for (jump, wall) in actions {
            now += jump;
            let before = sut;
            let expected = if expired || now >= end || wall >= 600_000 { expired = true; Err(Refusal::Expired) }
                else { end = end.min(now+600_000-wall); Ok(tick(end)) };
            let actual = sut.refresh(WallExpiry::from_unix_millis(d(MemberChosenWall).millis()), WallExpiry::from_unix_millis(wall), tick(now));
            prop_assert_eq!(actual, expected);
            prop_assert_eq!(sut.is_expired(), expired);
            if actual.is_err() { prop_assert_eq!(sut.deadline(),before.deadline()); }
            if now > 0 {
                let saved = sut;
                prop_assert_eq!(sut.refresh(WallExpiry::from_unix_millis(d(MemberChosenWall).millis()), WallExpiry::from_unix_millis(0), tick(now-1)), Err(Refusal::ClockWentBackwards));
                prop_assert_eq!(sut,saved);
            }
        }
    }
}

#[test]
fn explicit_overflows_units_and_clock_domains() {
    assert_eq!(
        span(u64::MAX).checked_add(d(OwnerPoll)),
        Err(Refusal::ArithmeticOverflow)
    );
    assert_eq!(
        ConfiguredCloseWaitSum.span(Entries::new(u64::MAX)),
        Err(Refusal::ArithmeticOverflow)
    );
    assert_eq!(
        Span::ZERO.checked_sub(d(OwnerPoll)),
        Err(Refusal::ArithmeticUnderflow)
    );
    assert_eq!(d(OwnerPoll).checked_div(0), Err(Refusal::DivisionByZero));
    assert_eq!(
        WorkerDefaultWall.bytes(),
        Err(Refusal::WrongUnit(WorkerDefaultWall))
    );
    assert_eq!(
        tick(0).earliest(MonoInstant::new(ClockDomain(8), 0)),
        Err(Refusal::ClockDomainMismatch)
    );
    let mut relay = RelayBudget::new(tick(d(OwnerPoll).millis()), d(RelayWindow)).unwrap();
    let original = relay;
    assert_eq!(
        relay.step(tick(0), ServingProbe::Serving),
        Err(Refusal::ClockWentBackwards)
    );
    assert_eq!(relay, original);
}

#[test]
fn full_relay_cap_trace_is_reached_not_randomly_assumed() {
    let mut relay = RelayBudget::new(tick(0), d(RelayWindow)).unwrap();
    let mut renewals = 0;
    loop {
        let now = relay.deadline().checked_sub(d(RelayRenewalLead)).unwrap();
        match relay.step(now, ServingProbe::Serving).unwrap() {
            RelayAction::Renewed => renewals += 1,
            RelayAction::CapReached => break,
            other => panic!("unexpected {other:?}"),
        }
    }
    assert_eq!(renewals, 47);
    assert_eq!(relay.granted().millis(), 86_400_000);
    assert_eq!(
        relay.step(relay.deadline(), ServingProbe::Serving),
        Ok(RelayAction::Closed)
    );
}

#[test]
fn every_operational_entry_is_blocked_without_phase_p99() {
    use budgets::table::{BUDGET_TABLE, Purpose};
    let ids: std::collections::BTreeSet<_> = BUDGET_TABLE.iter().map(|e| e.id).collect();
    assert_eq!(ids.len(), BUDGET_TABLE.len());
    let mut reached = 0;
    for entry in BUDGET_TABLE {
        if let Purpose::Operational(phase) | Purpose::ProgressWindow(phase) = entry.purpose {
            reached += 1;
            assert_eq!(
                qualify_entry(entry.id, Entries::new(2), None),
                Err(Refusal::BlockedNoP99(phase))
            );
        }
    }
    assert!(reached > 0);
}
