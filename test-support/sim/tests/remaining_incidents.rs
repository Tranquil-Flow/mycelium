// SPDX-License-Identifier: AGPL-3.0-only
//! Section 7 model contracts. The same assertions run in both modes; historical
//! faults must fail here, not through setup errors. See INCIDENTS.md for seams.
use sim::{model::*, trace::Trace};

const INCIDENTS: [Incident; 7] = [
    Incident::F1,
    Incident::F2,
    Incident::F3,
    Incident::F5,
    Incident::F13,
    Incident::F14,
    Incident::F15b,
];
fn inputs() -> Vec<Input> {
    vec![Input {
        at_ms: 0,
        task: 1,
        command: Command::Begin,
    }]
}
fn config(id: Incident, mode: Mode) -> Config {
    Config::standard(id, mode, 1).unwrap()
}
fn contract(id: Incident) {
    let mode = if std::env::var("SIM_MODEL").as_deref() == Ok("historical") {
        Mode::Historical
    } else {
        Mode::Corrected
    };
    let c = config(id, mode);
    let o = run(&c, &inputs()).unwrap();
    println!(
        "MODEL {id:?} {mode:?}: starts={} served={} chats={} metrics={:?}",
        o.starts, o.served, o.successful_chats, o.metrics
    );
    assert!(
        violations(&c, &o).is_empty(),
        "MODEL {id:?} violates section-7 contract: {:?}; reached={:?}",
        violations(&c, &o),
        o.transitions
    );
    assert!(o.served > 0 && o.successful_chats > 0);
    for transition in id.required_transitions() {
        assert!(
            o.transitions.contains_key(*transition),
            "{id:?}: missing {transition}"
        );
    }
}
#[test]
fn section7_f1_authorized_progress_contract() {
    contract(Incident::F1);
}
#[test]
fn section7_f2_authorized_progress_contract() {
    contract(Incident::F2);
}
#[test]
fn section7_f3_authorized_progress_contract() {
    contract(Incident::F3);
}
#[test]
fn section7_f5_authorized_progress_contract() {
    contract(Incident::F5);
}
#[test]
fn section7_f13_authorized_progress_contract() {
    contract(Incident::F13);
}
#[test]
fn section7_f14_authorized_progress_contract() {
    contract(Incident::F14);
}
#[test]
fn section7_f15b_authorized_progress_contract() {
    contract(Incident::F15b);
}

#[test]
fn historical_faults_have_distinct_semantics_and_reached_actions() {
    for (id, failure) in [
        (Incident::F1, "token_pin_expired_with_valid_authority"),
        (Incident::F2, "mailbox_byte_ceiling_exceeded"),
        (Incident::F3, "owner_restart_report_missing"),
        (Incident::F5, "status_read_blocked_by_slow_work"),
        (Incident::F13, "quick_relay_expired_while_serving"),
        (Incident::F14, "owner_share_exceeds_storage_consent"),
        (Incident::F15b, "owner_received_unknown_member_wall"),
    ] {
        let c = config(id, Mode::Historical);
        let o = run(&c, &inputs()).unwrap();
        assert!(
            violations(&c, &o).contains(&failure.to_string()),
            "{id:?}: {o:?}"
        );
        for transition in id.required_transitions() {
            assert!(
                o.transitions.contains_key(*transition),
                "{id:?}: {transition}"
            );
        }
    }
}

#[test]
fn corrected_observations_match_independent_source_values() {
    for id in INCIDENTS {
        let c = config(id, Mode::Corrected);
        let o = run(&c, &inputs()).unwrap();
        let m = &o.metrics;
        match id {
            Incident::F1 => {
                assert_eq!(m["initial_pin_ttl_ms"], 300_000);
                assert_eq!(m["renew_interval_ms"], 100_000);
                assert!(m["late_requests_served"] >= 2);
                assert_eq!(m["policy_installs"], 1);
                assert_eq!(m["authority_cap_ms"], 1_800_000);
                assert_eq!(m["post_cap_refusals"], 1);
            }
            Incident::F2 => {
                assert!(m["accepted"] > 134_217_728 / 512);
                assert!(m["peak_bytes"] <= 134_217_728);
                assert!(m["retained_rows"] <= 8192);
                assert!(m["pruned"] > 0);
                assert_eq!(m["replay_refusals_after_reopen"], 3);
                assert_eq!(m["fresh_after_reopen"], 1);
                assert_eq!(m["live_retry_preserved"], 1);
                assert_eq!(m["pending_command_preserved"], 1);
                assert_eq!(m["claimed_auth_preserved"], 1);
                assert_eq!(m["old_nonce_pruned"], 1);
                assert_eq!(m["retired_command_pruned"], 1);
            }
            Incident::F3 => {
                assert_eq!(o.successful_chats, 2);
                assert_eq!(m["owner_restarts"], 1);
                assert_eq!(m["member_restarts"], 0);
                assert_eq!(m["member_reconsents"], 0);
                assert_eq!(m["member_identity_preserved"], 1);
                assert_eq!(m["stale_generation_refusals"], 2);
                assert_eq!(m["reports_delivered"], 2);
            }
            Incident::F5 => {
                assert_eq!(m["compile_hold_ms"], 40_000);
                assert_eq!(m["publish_hold_ms"], 150_000);
                assert_eq!(m["status_budget_ms"], 500);
                assert_eq!(m["reads_while_blocked"], 2);
                assert_eq!(m["phases_completed"], 2);
            }
            Incident::F13 => {
                assert_eq!(m["renewals"], 47);
                assert_eq!(m["granted_ms"], 86_400_000);
                // Early renewals consume complete windows, not 24h elapsed.
                assert_eq!(m["closed_at_ms"], 83_580_000);
                assert_eq!(m["process_starts"], 1);
                assert_eq!(m["url_changes"], 0);
                assert!(m["requests_after_first_window"] > 0);
            }
            Incident::F14 => {
                assert_eq!(m["selected_share_bytes"], 1_997_798_421);
                assert_eq!(m["reserved_bytes"], 1_997_798_421);
                assert_eq!(m["staged_bytes"], 1_997_798_421);
                assert_eq!(m["owner_storage_cap"], 2_000_000_000);
                assert_eq!(m["member_storage_cap"], 2_000_000_000);
                assert_eq!(m["fresh_default_bytes"], 4_000_000_000);
                assert_eq!(m["owner_standing_grants"], 0);
            }
            Incident::F15b => {
                assert_eq!(m["owner_worker_deadline_ms"], 1_800_000);
                assert_eq!(m["member_worker_deadline_ms"], 600_000);
                assert_eq!(m["unknown_member_wall_ms"], 60_000);
                assert_eq!(m["owner_standing_grants"], 0);
                assert_eq!(o.successful_chats, 2);
            }
            _ => unreachable!(),
        }
    }
}

#[test]
fn empty_trace_always_refuse_and_missing_permissions_cannot_pass() {
    for id in INCIDENTS {
        let c = config(id, Mode::Corrected);
        assert!(!violations(&c, &run(&c, &[]).unwrap()).is_empty());
        for mutant in [0, 1, 2] {
            let mut c = c.clone();
            match mutant {
                0 => c.mode = Mode::AlwaysRefuse,
                1 => c.owner_local_allowed = false,
                _ => c.member_granted = false,
            }
            let o = run(&c, &inputs()).unwrap();
            assert_eq!(o.successful_chats, 0);
            assert!(!violations(&c, &o).is_empty());
        }
    }
}

#[test]
fn remaining_models_are_deterministic_and_minimizer_keeps_reachability() {
    for id in INCIDENTS {
        let t = Trace::generate(
            "test-build".into(),
            "a".repeat(64),
            17,
            config(id, Mode::Historical),
        )
        .unwrap();
        let minimal = t.minimize().unwrap();
        assert_eq!(minimal.commands, inputs());
        let mut replay: Trace =
            serde_json::from_str(&serde_json::to_string(&minimal).unwrap()).unwrap();
        replay.seed = u64::MAX;
        assert_eq!(replay.replay().unwrap(), minimal.expected_outcome);
        assert_eq!(
            violations(&replay.config, &replay.replay().unwrap()),
            minimal.expected_failure
        );
    }
}

#[test]
fn remaining_models_link_real_l0_obligations() {
    let (registry, _) = obligations::checked_in().unwrap();
    for id in INCIDENTS {
        assert!(registry.escapes.iter().any(|e| e.id == format!("{id:?}")));
        assert!(
            registry
                .obligations
                .iter()
                .any(|o| o.id == id.obligation_id())
        );
    }
}
