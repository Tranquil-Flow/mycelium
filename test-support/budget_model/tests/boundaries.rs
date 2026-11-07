// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{BudgetId::*, *};
fn d(id: BudgetId) -> Span {
    id.span(Entries::new(2)).unwrap()
}
fn t(raw: u64) -> MonoInstant {
    MonoInstant::new(ClockDomain(1), raw)
}

#[test]
fn forward_wall_expiry_then_rollback_cannot_revive_a_pin() {
    let expiry = WallExpiry::from_unix_millis(d(MembershipTtl).millis());
    let mut pin = LeasePin::new(
        expiry,
        WallExpiry::from_unix_millis(0),
        t(0),
        t(d(WorkerDefaultWall).millis()),
    )
    .unwrap();
    assert_eq!(
        pin.refresh(expiry, expiry, t(d(OwnerPoll).millis())),
        Err(Refusal::Expired)
    );
    assert_eq!(
        pin.refresh(
            expiry,
            WallExpiry::from_unix_millis(0),
            t(d(RelayPoll).millis())
        ),
        Err(Refusal::Expired)
    );
}
#[test]
fn reservation_all_dimensions_and_no_partial_write() {
    let mut ledger = Reservations::new(Resources {
        storage: Bytes::new(10),
        memory: Bytes::new(10),
    });
    let request = Reservation {
        staged: Bytes::new(4),
        staging_overhead: Bytes::new(6),
        resident: Bytes::new(3),
        scratch: Bytes::new(2),
        kv: Bytes::new(5),
    };
    assert_eq!(ledger.reserve(ReservationId(1), request), Ok(()));
    assert_eq!(
        ledger.used(),
        Resources {
            storage: Bytes::new(10),
            memory: Bytes::new(10)
        }
    );
    let before = ledger.clone();
    assert_eq!(
        ledger.release(ReservationId(1), CleanupProof::Unproven),
        Err(Refusal::CleanupUnproven)
    );
    assert_eq!(ledger, before);
    assert_eq!(
        ledger.reserve(ReservationId(2), request),
        Err(Refusal::StorageCap)
    );
    assert_eq!(ledger, before);
    assert_eq!(
        ledger.release(ReservationId(1), CleanupProof::Verified),
        Ok(())
    );
    assert_eq!(
        ledger.release(ReservationId(1), CleanupProof::Verified),
        Err(Refusal::UnknownReservation)
    );
    let overflow = Reservation {
        staged: Bytes::new(u64::MAX),
        staging_overhead: Bytes::new(1),
        ..request
    };
    assert_eq!(
        ledger.reserve(ReservationId(2), overflow),
        Err(Refusal::ArithmeticOverflow)
    );
    assert_eq!(ledger.used(), Resources::default());
    let memory_over = Reservation {
        kv: Bytes::new(6),
        ..request
    };
    assert_eq!(
        ledger.reserve(ReservationId(2), memory_over),
        Err(Refusal::MemoryCap)
    );
    assert_eq!(ledger.used(), Resources::default());
}
#[test]
fn p99_positive_boundary_invalid_evidence_and_wrong_phase() {
    let duration = d(ObservedStop); // Synthetic profile fixture, NOT promotion of the incident to p99.
    let profile = PhaseProfile {
        phase: Phase::Stop,
        statistic: PhaseStatistic::MeasuredP99 {
            duration,
            samples: Entries::new(256),
            evidence: "synthetic: validator boundary ONLY",
        },
    };
    assert_eq!(
        require_p99(duration.checked_mul(2).unwrap(), profile),
        Ok(())
    );
    assert_eq!(
        require_p99(duration, profile),
        Err(Refusal::BelowTwiceP99(Phase::Stop))
    );
    for (samples, evidence) in [(0, "synthetic"), (1, "synthetic"), (256, "")] {
        let invalid = PhaseProfile {
            phase: Phase::Stop,
            statistic: PhaseStatistic::MeasuredP99 {
                duration,
                samples: Entries::new(samples),
                evidence,
            },
        };
        assert_eq!(
            require_p99(d(PreviousClose), invalid),
            Err(Refusal::InvalidEvidence)
        );
    }
    assert_eq!(
        qualify_entry(PreviousClose, Entries::new(2), Some(profile)),
        Err(Refusal::InvalidEvidence)
    );
}
#[test]
fn authority_expiry_shortens_windows_and_mixed_clocks_refuse() {
    let origin = t(0);
    let window = WorkerWindow {
        origin,
        wall: d(WorkerDefaultWall),
        authorized_until: t(d(MemberChosenWall).millis()),
    };
    let result = deadlines(&[window], origin).unwrap();
    assert_eq!(result.rollover_at.ticks(), 480_000);
    assert_eq!(result.stop_by.ticks(), 590_000);
    let wrong = WorkerWindow {
        authorized_until: MonoInstant::new(ClockDomain(2), window.authorized_until.ticks()),
        ..window
    };
    assert_eq!(
        deadlines(&[wrong], origin),
        Err(Refusal::ClockDomainMismatch)
    );
    assert_eq!(deadlines(&[], origin), Err(Refusal::EmptyMembers));
    let late = WorkerWindow {
        origin: t(d(OwnerPoll).millis()),
        ..window
    };
    assert_eq!(deadlines(&[late], origin), Err(Refusal::ClockWentBackwards));
}
