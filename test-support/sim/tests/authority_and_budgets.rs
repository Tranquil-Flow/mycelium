// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{BudgetId as B, Entries};
use sim::model::*;
fn begin() -> Vec<Input> {
    vec![Input {
        at_ms: 0,
        task: 1,
        command: Command::Begin,
    }]
}
#[test]
fn consent_is_immutable_and_no_unauthorized_worker_is_created() {
    for owner in [false, true] {
        for member in [false, true] {
            let mut c = Config::standard(Incident::F16, Mode::Corrected, 1).unwrap();
            c.owner_local_allowed = owner;
            c.member_granted = member;
            let o = run(&c, &begin()).unwrap();
            if owner && member {
                assert_eq!(o.starts, 3);
            } else {
                assert_eq!(o.starts, 0);
                assert!(o.diagnostics.iter().any(|d| d.contains("ConsentBound")));
            }
        }
    }
    let mut c = Config::standard(Incident::F16, Mode::Corrected, 1).unwrap();
    c.authority_until_ms = B::MemberChosenWall.span(Entries::new(2)).unwrap().millis();
    let o = run(&c, &begin()).unwrap();
    assert_eq!(o.starts, 1);
    assert_eq!(o.cleanup_proofs, 1);
    assert!(
        o.workers
            .iter()
            .all(|w| w.member_expiry_ms <= 600_000 && w.owner_expiry_ms <= 600_000)
    );
    assert!(o.diagnostics.iter().any(|d| d.contains("ConsentBound")));
}
#[test]
fn observation_floors_contradict_waits_but_cannot_be_promoted_to_p99() {
    use budgets::{Phase, PhaseProfile, PhaseStatistic, Refusal, require_p99};
    let peers = Entries::new(2);
    let old = B::HistoricalOuterStop.span(peers).unwrap();
    let stop = B::ObservedStop.span(peers).unwrap();
    assert_eq!(old.millis(), 30_000);
    assert_eq!(stop.millis(), 33_000);
    assert!(old < stop);
    let actual = B::EffectiveStopAllowance
        .wall_span(B::MemberChosenWall.span(peers).unwrap(), peers)
        .unwrap();
    assert_eq!(actual.millis(), 110_000);
    for multiplier in [2, 3] {
        assert!(stop.checked_mul(multiplier * 2).unwrap() > actual);
    }
    assert_eq!(
        require_p99(
            actual,
            PhaseProfile {
                phase: Phase::Stop,
                statistic: PhaseStatistic::Observation {
                    duration: stop,
                    evidence: "L13-ROLLSTOP.md:61"
                }
            }
        ),
        Err(Refusal::BlockedNoP99(Phase::Stop))
    );
    assert_eq!(
        B::ObservedRestartFloor.span(peers).unwrap().millis(),
        396_000
    );
    assert!(B::ObservedRestartFloor.span(peers).unwrap() > B::RolloverMargin.span(peers).unwrap());
}
