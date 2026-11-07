// SPDX-License-Identifier: AGPL-3.0-only
use budgets::BudgetId as B;
use sim::{incidents::*, model::*, span};
fn ms(id: B) -> u64 {
    span(id).unwrap().millis()
}
fn bytes(id: B) -> u64 {
    id.bytes().unwrap().get()
}

#[test]
fn relay_false_failed_and_explicit_stop_cannot_renew_or_revive() {
    for window in [ms(B::RelayMinimumWindow), ms(B::RelayWindow)] {
        for probe in [ServingProbe::NotServing, ServingProbe::Failed] {
            let mut relay = Relay::new(0, window, ms(B::RelayGrantedWindowCap)).unwrap();
            let identity = (relay.process, relay.url, relay.cap_ms());
            let due = relay.renewal_due().unwrap();
            relay.tick(due, probe, Mode::Corrected).unwrap();
            assert_eq!(relay.deadline_ms, window);
            assert_eq!(relay.renewals, 0);
            relay.tick(window, probe, Mode::Corrected).unwrap();
            assert!(relay.closed);
            relay
                .tick(
                    window + ms(B::RelayPoll),
                    ServingProbe::Serving,
                    Mode::Corrected,
                )
                .unwrap();
            assert!(relay.closed);
            assert_eq!(relay.renewals, 0);
            assert_eq!((relay.process, relay.url, relay.cap_ms()), identity);
        }
        let mut relay = Relay::new(0, window, ms(B::RelayGrantedWindowCap)).unwrap();
        relay
            .tick(
                relay.renewal_due().unwrap(),
                ServingProbe::Serving,
                Mode::Corrected,
            )
            .unwrap();
        assert_eq!(relay.renewals, 1);
        relay.stop();
        relay
            .tick(
                relay.renewal_due().unwrap(),
                ServingProbe::Serving,
                Mode::Corrected,
            )
            .unwrap();
        assert!(relay.closed);
        assert_eq!(relay.renewals, 1);
    }
}

#[test]
fn relay_minimum_window_accounts_granted_not_elapsed_and_preserves_identity() {
    let mut relay = Relay::new(0, ms(B::RelayMinimumWindow), ms(B::RelayGrantedWindowCap)).unwrap();
    let identity = (relay.process, relay.url, relay.cap_ms());
    loop {
        let previous = relay.renewals;
        relay
            .tick(
                relay.renewal_due().unwrap(),
                ServingProbe::Serving,
                Mode::Corrected,
            )
            .unwrap();
        assert_eq!((relay.process, relay.url, relay.cap_ms()), identity);
        if relay.renewals == previous {
            break;
        }
    }
    assert_eq!(relay.renewals, 2879);
    assert_eq!(relay.granted_ms, 86_400_000);
    assert_eq!(relay.deadline_ms, 57_610_000);
    relay
        .tick(relay.deadline_ms, ServingProbe::Serving, Mode::Corrected)
        .unwrap();
    assert!(relay.closed);
}

#[test]
fn lease_wall_jumps_cannot_rearm_unchanged_authority() {
    let mut c = Config::standard(Incident::F1, Mode::Corrected, 1).unwrap();
    c.authority_until_ms = ms(B::MemberChosenWall) + ms(B::MembershipTtl);
    let begin = Input {
        at_ms: 0,
        task: 1,
        command: Command::Begin,
    };
    let base = run(&c, std::slice::from_ref(&begin)).unwrap();
    assert_eq!(base.successful_chats, 3);
    assert_eq!(base.metrics["max_pin_expiry_ms"], 900_000);
    assert_eq!(base.metrics["installed_policy_expiry_ms"], 900_000);
    assert_eq!(base.metrics["post_cap_refusals"], 1);
    for sign in [-1, 1] {
        let jumped = run(
            &c,
            &[
                begin.clone(),
                Input {
                    at_ms: ms(B::LeaseRenewalInterval),
                    task: 0,
                    command: Command::WallJump {
                        delta_ms: sign * i64::try_from(ms(B::RelayGrantedWindowCap)).unwrap(),
                    },
                },
            ],
        )
        .unwrap();
        assert_eq!(jumped.metrics, base.metrics);
        assert_eq!(jumped.successful_chats, base.successful_chats);
    }
}

#[test]
fn storage_saved_consent_survives_fresh_default_and_infeasible_refuses() {
    let saved = bytes(B::SavedStorageConsent);
    let fresh = storage_consent(None).unwrap();
    assert_eq!(fresh, 4_000_000_000);
    assert_eq!(storage_consent(Some(saved)).unwrap(), 2_000_000_000);
    assert_eq!(
        select_owner_share(Mode::Corrected, saved).unwrap(),
        1_997_798_421
    );
    assert_eq!(
        select_owner_share(Mode::Corrected, fresh).unwrap(),
        2_227_449_134
    );
    assert_eq!(
        select_owner_share(Mode::Corrected, bytes(B::ControlArchiveAllowance)).unwrap_err(),
        "member_storage_cap_below_share:OwnerLocalStorage"
    );
    assert_eq!(storage_consent(Some(saved)).unwrap(), saved);
}

#[test]
fn owner_local_fallback_does_not_change_unknown_joined_member_semantics() {
    let proposal = ms(B::WorkerDefaultWall);
    assert_eq!(
        wall_limit(Contributor::OwnerLocal, None, proposal, Mode::Corrected).unwrap(),
        1_800_000
    );
    assert_eq!(
        wall_limit(Contributor::Joined, None, proposal, Mode::Corrected).unwrap(),
        60_000
    );
    for role in [Contributor::OwnerLocal, Contributor::Joined] {
        assert_eq!(
            wall_limit(
                role,
                Some(ms(B::MemberChosenWall)),
                proposal,
                Mode::Corrected
            )
            .unwrap(),
            600_000
        );
    }
}
