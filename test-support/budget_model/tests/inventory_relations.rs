// SPDX-License-Identifier: AGPL-3.0-only
// Obligation O:path:mycelium_deadlines.py; F16. Source f5bfb709:188-469.
// Obligation O:path:mycelium_a13_macos/public_relay.py; F13: unqualified timing stays blocked.
use budgets::{BudgetId::*, CloseBound, Entries, require_stop};

#[test]
fn unclassified_source_site_cannot_be_calibrated_by_a_generic_profile() {
    use budgets::{Phase, PhaseProfile, PhaseStatistic, Refusal, qualify_entry, require_p99};
    let peers = Entries::new(2);
    let id = PyRelayCloudflaredGrace;
    let allowance = id.span(peers).unwrap();
    // Deliberately unqualified negative-control payload, NOT measured evidence.
    let profile = PhaseProfile {
        phase: Phase::Unclassified,
        statistic: PhaseStatistic::MeasuredP99 {
            duration: allowance.checked_div(4).unwrap(),
            samples: Entries::new(64),
            evidence: "negative-control: invented profile must never qualify an unclassified site",
        },
    };
    assert_eq!(
        require_p99(allowance, profile),
        Err(Refusal::BlockedNoP99(Phase::Unclassified))
    );
    assert_eq!(
        qualify_entry(id, peers, Some(profile)),
        Err(Refusal::BlockedNoP99(Phase::Unclassified))
    );
}

#[test]
fn cleanup_formulas_follow_current_values_not_stale_python_docstrings() {
    let peers = Entries::new(2);
    assert_eq!(
        PyRouteCleanupFanout.span(peers).unwrap().millis(),
        8_000 - 3_500
    );
    assert_eq!(
        PyPeerCleanupAdvertisement.span(peers).unwrap().millis(),
        8_000 - 3_500 - 200 - 100
    );
    assert_eq!(
        PyCommandInterruptionCleanup.span(peers).unwrap().millis(),
        8_000
    );
}

#[test]
fn advertised_cleanup_is_inside_fanout_but_not_a_measured_close_guarantee() {
    let peers = Entries::new(2);
    let advertised = PyPeerCleanupAdvertisement.span(peers).unwrap();
    let fanout = PyRouteCleanupFanout.span(peers).unwrap();
    assert!(advertised < fanout);
    assert!(
        PyMyceliumDeadlinesCommandDefaultCooperativeStepMsL250
            .span(peers)
            .unwrap()
            < PyCommandInterruptionCleanup.span(peers).unwrap()
    );
    assert!(
        PyMyceliumDeadlinesRequalificationIntervalSecondsL319
            .span(peers)
            .unwrap()
            < PyMyceliumDeadlinesQualificationMaxAgeSecondsL313
                .span(peers)
                .unwrap()
    );
    assert!(require_stop(fanout, CloseBound::Unbounded).is_err());
}

#[test]
fn configured_requalification_does_not_satisfy_ttl_thirds_rule() {
    let peers = Entries::new(2);
    // Python's freshness relation is refresh < max age, NOT refresh <= age/3.
    assert!(
        budgets::require_renewal(
            PyMyceliumDeadlinesQualificationMaxAgeSecondsL313
                .span(peers)
                .unwrap(),
            PyMyceliumDeadlinesRequalificationIntervalSecondsL319
                .span(peers)
                .unwrap(),
        )
        .is_err()
    );
}
