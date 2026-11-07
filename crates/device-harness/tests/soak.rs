// SPDX-License-Identifier: AGPL-3.0-only
use device_harness::{http::Chat, soak::*};
use std::collections::BTreeSet;
fn run(id: &str, serving: bool) -> Run {
    Run {
        run_id: Some(id.into()),
        state: if serving { "serving" } else { "running" }.into(),
        route_ready: serving,
        participants: 2,
    }
}
fn good_chat() -> Chat {
    Chat {
        done: true,
        terminal: Some("completed".into()),
        n_tokens: 3,
        ..Chat::default()
    }
}
fn policy() -> Policy {
    Policy {
        requested_ms: 2700000,
        minimum_ms: 2100000,
        chat_every_ms: 300000,
        sample_every_ms: 60000,
        mailbox_every_ms: 600000,
        visitor_every_ms: 600000,
        restart_at_ms: Some(720000),
        minimum_rollovers: 1,
        mailbox_max_bytes: Some(4096),
        restart_expectation: RestartExpectation::AutomaticRecovery,
        required_steps: BTreeSet::new(),
    }
}
fn metrics_sample(at_ms: u64) -> HostMetricsSample {
    let metrics = device_harness::observe::Metrics {
        load: [1., 2., 3.],
        top_cpu: (1..=5)
            .map(|pid| device_harness::observe::Cpu {
                pid,
                cpu_percent: 1.,
                command: "worker".into(),
            })
            .collect(),
    };
    HostMetricsSample {
        at_ms,
        owner: metrics.clone(),
        member: metrics,
    }
}
#[test]
fn metric_counter_cannot_substitute_for_both_full_host_samples() {
    let mut e = good();
    e.host_metrics.clear();
    assert!(!evaluate(&policy(), &e).scenario_passed);
    let mut e = good();
    e.host_metrics[0].member.top_cpu.pop();
    assert!(!evaluate(&policy(), &e).scenario_passed);
    let mut e = good();
    e.host_metrics[0].owner.load[1] = f64::NAN;
    assert!(!evaluate(&policy(), &e).scenario_passed);
}
fn good() -> Evidence {
    Evidence {
        duration_ms: 2700000,
        clock_started_at_distributed_output: true,
        chats: vec![good_chat(); 9],
        samples: 45,
        metrics_samples: 45,
        host_metrics: (0..45).map(|i| metrics_sample(i * 60000)).collect(),
        rollovers: vec![Rollover {
            from_run_id: "a".into(),
            to_run_id: "b".into(),
            start_ms: 1600000,
            last_serving_ms: Some(1590000),
            detected_ms: 1600000,
            end_ms: 1660000,
            gap_ms: 60000,
            gap_upper_ms: Some(70000),
        }],
        restart: Some(Restart {
            expectation: RestartExpectation::AutomaticRecovery,
            attempted: true,
            recovered: true,
            idle_observed: false,
            explicit_start: false,
            member_mutations: 0,
            endpoint_changed: Some(true),
            approval: None,
        }),
        mailbox_samples: vec![
            MailboxSample {
                at_ms: 0,
                owner: Some(1024),
                member: Some(0),
                owner_storage: 1024,
                member_storage: 0,
            },
            MailboxSample {
                at_ms: 2700000,
                owner: Some(2048),
                member: Some(0),
                owner_storage: 2048,
                member_storage: 0,
            },
        ],
        final_serving: true,
        ..Evidence::default()
    }
}
#[test]
fn rollover_sampled_lower_upper_bounds_once_and_owner_restart_excluded() {
    let mut t = RolloverTracker::default();
    for (at, r, s) in [
        (0, "a", true),
        (60000, "a", true),
        (120000, "b", false),
        (180000, "b", false),
        (240000, "b", true),
    ] {
        t.observe(at, &run(r, s)).unwrap();
    }
    assert_eq!(t.rollovers.len(), 1);
    let r = &t.rollovers[0];
    assert_eq!(r.gap_ms, 120000);
    assert_eq!(r.gap_upper_ms, Some(180000));
    assert_eq!((&*r.from_run_id, &*r.to_run_id), ("a", "b"));
    t.begin_restart();
    t.observe(300000, &run("c", true)).unwrap();
    assert_eq!(t.rollovers.len(), 1);
    t.end_restart(300000, &run("c", true));
    t.observe(360000, &run("d", true)).unwrap();
    assert_eq!(t.rollovers[1].gap_ms, 0);
    assert_eq!(t.rollovers[1].gap_upper_ms, Some(60000));
    assert!(t.observe(359999, &run("d", true)).is_err());
}
#[test]
fn legacy_verdict_transcribes_all_duration_schedule_restart_mailbox_visitor_assertions() {
    assert!(evaluate(&policy(), &good()).scenario_passed);
    for mutation in 0..14 {
        let mut e = good();
        let mut p = policy();
        match mutation {
            0 => e.duration_ms = 2099999,
            1 => e.rollovers.clear(),
            2 => e.pending_rollover = true,
            3 => e.chats.clear(),
            4 => e.chats.truncate(1),
            5 => e.samples = 44,
            6 => e.restart.as_mut().unwrap().recovered = false,
            7 => p.restart_at_ms = None,
            8 => p.mailbox_max_bytes = None,
            9 => e.mailbox_samples[0].member = None,
            10 => e.mailbox_samples[0].owner = Some(4097),
            11 => e.published = true,
            12 => e.errors.push("observation_or_schedule_errors".into()),
            _ => e.metrics_samples = 44,
        }
        assert!(!evaluate(&p, &e).scenario_passed, "mutation {mutation}");
    }
    let mut e = good();
    e.published = true;
    e.visitor_completed = vec![true];
    assert!(evaluate(&policy(), &e).scenario_passed);
    e.visitor_completed.push(false);
    assert!(!evaluate(&policy(), &e).scenario_passed);
}
#[test]
fn switching_failure_allowed_only_intersecting_observed_rollover_not_generic_error() {
    let mut e = good();
    let c = &mut e.chats[0];
    c.done = false;
    c.terminal = None;
    c.error = Some("chat_switching_runs".into());
    c.switching = true;
    c.start_ms = 1620000;
    c.end_ms = 1621000;
    assert!(evaluate(&policy(), &e).scenario_passed);
    e.chats[0].switching = false;
    assert!(!evaluate(&policy(), &e).scenario_passed);
    e.chats[0].switching = true;
    e.chats[0].start_ms = 1800000;
    e.chats[0].end_ms = 1801000;
    assert!(!evaluate(&policy(), &e).scenario_passed);
    e.chats[0].start_ms = 1620000;
    e.chats[0].end_ms = 1619999;
    assert!(!evaluate(&policy(), &e).scenario_passed);
}
#[test]
fn observation_loss_is_incomplete_and_first_product_failure_is_not_erased() {
    let mut e = good();
    e.incomplete_reason = Some("ssh_observation_lost".into());
    e.chats.clear();
    let v = evaluate(&policy(), &e);
    assert_eq!(v.outcome, "incomplete");
    assert_eq!(v.product_failed, Some(false));
    assert!(!v.scenario_passed);
    e.product_failure = Some("cleanup_unproven".into());
    let v = evaluate(&policy(), &e);
    assert_eq!(v.outcome, "failed");
    assert_eq!(v.product_failed, Some(true));
    assert!(v.reasons.contains(&"cleanup_unproven".into()));
}
#[test]
fn owner_restart_expectation_is_explicit_configurable_and_reported_not_automatic_f3() {
    let mut p = policy();
    p.restart_expectation = RestartExpectation::IdleUntilExplicitStart;
    let mut e = good();
    let r = e.restart.as_mut().unwrap();
    r.expectation = p.restart_expectation;
    r.idle_observed = true;
    r.explicit_start = true;
    assert!(!evaluate(&p, &e).scenario_passed); // owner review is still open
    e.restart.as_mut().unwrap().approval = Some("fixture-explicit-owner-selection".into());
    let v = evaluate(&p, &e);
    assert!(v.scenario_passed);
    assert!(!v.f3_automatic_recovery);
    assert_eq!(
        v.restart_expectation,
        RestartExpectation::IdleUntilExplicitStart
    );
    assert!(!v.release_qualified);
    assert!(!evaluate(&policy(), &e).scenario_passed); // cannot silently change expectation
    e.restart.as_mut().unwrap().member_mutations = 1;
    assert!(!evaluate(&p, &e).scenario_passed);
}
fn ruled_twenty_minutes() -> (Policy, serde_json::Value) {
    // Owner-ruling fixture, not an operational default: TODO(L1) named budget.
    let mut p = policy();
    p.requested_ms = 1200000;
    p.minimum_ms = 1200000;
    p.chat_every_ms = 150000;
    let mut e = good();
    e.duration_ms = 1201900;
    e.chats = (0..8)
        .map(|i| Chat {
            start_ms: i * 150000,
            end_ms: i * 150000 + 8000,
            ..good_chat()
        })
        .collect();
    e.samples = 20;
    e.metrics_samples = 20;
    e.host_metrics.truncate(20);
    e.rollovers[0].start_ms = 420000;
    e.rollovers[0].end_ms = 541000;
    e.rollovers[0].last_serving_ms = Some(361000);
    e.mailbox_samples[1].at_ms = 1201900;
    let mut value = serde_json::to_value(e).unwrap();
    value["restart_window"] = serde_json::json!({"begin_ms":721900,"end_ms":1075900});
    (p, value)
}
fn replay(p: &Policy, value: serde_json::Value) -> Verdict {
    evaluate(p, &serde_json::from_value::<Evidence>(value).unwrap())
}
fn refused(value: &mut serde_json::Value) {
    value["chats"][5] = serde_json::to_value(Chat {
        error: Some("chat_submission_failed".into()),
        start_ms: 750000,
        end_ms: 751000,
        ..Chat::default()
    })
    .unwrap();
    value["chats"][5]["submit_http"] = serde_json::json!(503);
    value["chats"][5]["submit_code"] = serde_json::json!("chat_not_ready");
}
#[test]
fn owner_ruled_twenty_minute_floor_and_longer_request_remain_distinct() {
    let (mut p, mut e) = ruled_twenty_minutes();
    assert_eq!(p.minimum_ms, 1200000); // oracle MIN_SOAK_SECONDS=1200
    assert!(replay(&p, e.clone()).scenario_passed);
    e["duration_ms"] = 1199000.into();
    assert!(
        replay(&p, e.clone())
            .reasons
            .contains(&"duration_incomplete".into())
    );
    p.requested_ms = 600000;
    e["duration_ms"] = 600000.into();
    e["chats"].as_array_mut().unwrap().truncate(4);
    e["samples"] = 10.into();
    // Keep restart inside the diagnostic short request so validation does not mask the floor.
    p.restart_at_ms = Some(300000);
    assert!(
        replay(&p, e)
            .reasons
            .contains(&"duration_incomplete".into())
    );
    let (mut p, e) = ruled_twenty_minutes();
    p.requested_ms = 2700000;
    assert!(
        replay(&p, e)
            .reasons
            .contains(&"duration_incomplete".into())
    );
}
#[test]
fn owner_ruled_restart_unavailable_requires_exact_code_and_recovered_containment() {
    for code in ["chat_not_ready", "chat_warming_up", "chat_switching_runs"] {
        let (p, mut e) = ruled_twenty_minutes();
        refused(&mut e);
        e["chats"][5]["submit_code"] = code.into();
        let v = replay(&p, e);
        assert!(v.scenario_passed, "{code}: {v:?}");
        assert_eq!(
            serde_json::to_value(v).unwrap()["owner_restart_unavailable_chats"],
            1
        );
    }
    for (http, code, start, end) in [
        (503, Some("chat_busy"), 750000, 751000),
        (502, None, 750000, 751000),
        (200, None, 750000, 751000),
        (503, Some("chat_not_ready"), 700000, 701000),
        (503, Some("chat_not_ready"), 1070000, 1080000),
        (503, Some("chat_not_ready"), 751000, 750000),
    ] {
        let (p, mut e) = ruled_twenty_minutes();
        refused(&mut e);
        e["chats"][5]["submit_http"] = http.into();
        e["chats"][5]["submit_code"] = serde_json::json!(code);
        if http == 200 {
            e["chats"][5]["error"] = "chat_timeout".into();
        }
        e["chats"][5]["start_ms"] = start.into();
        e["chats"][5]["end_ms"] = end.into();
        assert!(replay(&p, e).reasons.contains(&"chat_failed".into()));
    }
    for window in [
        serde_json::Value::Null,
        serde_json::json!({}),
        serde_json::json!({"begin_ms":721900}),
        serde_json::json!({"begin_ms":800000,"end_ms":700000}),
        serde_json::json!({"begin_ms":750000,"end_ms":750000}),
    ] {
        let (p, mut e) = ruled_twenty_minutes();
        refused(&mut e);
        e["restart_window"] = window;
        assert!(replay(&p, e).reasons.contains(&"chat_failed".into()));
    }
    for field in ["submit_http", "submit_code"] {
        let (p, mut e) = ruled_twenty_minutes();
        refused(&mut e);
        e["chats"][5].as_object_mut().unwrap().remove(field);
        assert!(replay(&p, e).reasons.contains(&"chat_failed".into()));
    }
    for key in ["attempted", "recovered"] {
        let (p, mut e) = ruled_twenty_minutes();
        refused(&mut e);
        e["restart"][key] = false.into();
        assert!(replay(&p, e).reasons.contains(&"chat_failed".into()));
    }
}
#[test]
fn restart_window_edges_counter_precedence_and_cli_receipt_are_exact() {
    for (start, end, expected) in [
        (721900, 1075900, true),
        (721899, 750000, false),
        (750000, 1075901, false),
    ] {
        let (p, mut e) = ruled_twenty_minutes();
        refused(&mut e);
        e["chats"][5]["start_ms"] = start.into();
        e["chats"][5]["end_ms"] = end.into();
        let input =
            serde_json::json!({"scenario":"soak","schema_version":1,"policy":p,"evidence":e});
        let result = device_harness::cli::evaluate(&serde_json::to_vec(&input).unwrap()).unwrap();
        assert_eq!(result["scenario_passed"], expected);
        assert_eq!(
            result["owner_restart_unavailable_chats"],
            usize::from(expected)
        );
        assert_eq!(result["evidence_boundary"], "offline_replay_not_physical");
        assert_eq!(result["release_qualified"], false);
    }
    let (p, mut e) = ruled_twenty_minutes();
    refused(&mut e);
    // Rollover switching already has its own exemption: never double count it.
    e["chats"][5]["switching"] = true.into();
    e["rollovers"][0]["end_ms"] = 800000.into();
    let v = replay(&p, e);
    assert!(v.scenario_passed);
    assert_eq!(v.owner_restart_unavailable_chats, 0);
    let (p, e) = ruled_twenty_minutes();
    assert_eq!(replay(&p, e).owner_restart_unavailable_chats, 0);
}
#[test]
fn owner_ruled_restart_json_rejects_noninteger_http_and_nonfinite_times() {
    for http in [
        serde_json::json!("503"),
        serde_json::json!(503.0),
        serde_json::json!(true),
    ] {
        let (_, mut e) = ruled_twenty_minutes();
        refused(&mut e);
        e["chats"][5]["submit_http"] = http;
        assert!(serde_json::from_value::<Evidence>(e).is_err());
    }
    for end in [serde_json::json!("NaN"), serde_json::json!(-1)] {
        let (_, mut e) = ruled_twenty_minutes();
        e["restart_window"]["end_ms"] = end;
        assert!(serde_json::from_value::<Evidence>(e).is_err());
    }
}

#[test]
fn cli_numeric_payloads_preserve_precision_and_refuse_wrong_shapes() {
    let (p, mut e) = ruled_twenty_minutes();
    e["chats"][0]["decode_tok_s"] = serde_json::json!(2.125);
    e["host_metrics"][0]["owner"]["load"][0] = serde_json::json!(1.25);
    e["host_metrics"][0]["member"]["top_cpu"][0]["cpu_percent"] = serde_json::json!(0.125);
    let input = serde_json::json!({"scenario":"soak","schema_version":1,"policy":p,"evidence":e});
    let bytes = serde_json::to_vec(&input).unwrap();
    let decoded: device_harness::cli::Input = serde_json::from_slice(&bytes).unwrap();
    let device_harness::cli::Input::Soak { evidence, .. } = decoded else {
        panic!("wrong scenario")
    };
    assert_eq!(evidence.chats[0].decode_tok_s, Some(2.125));
    assert_eq!(evidence.host_metrics[0].owner.load[0], 1.25);
    assert_eq!(
        evidence.host_metrics[0].member.top_cpu[0].cpu_percent,
        0.125
    );
    assert_eq!(
        device_harness::cli::evaluate(&bytes).unwrap()["scenario_passed"],
        true
    );
    for (pointer, invalid) in [
        ("/schema_version", serde_json::json!(1.0)),
        ("/evidence/chats/0/submit_http", serde_json::json!(503.0)),
        ("/evidence/chats/0/submit_http", serde_json::json!("503")),
        ("/evidence/chats/0/start_ms", serde_json::json!(-1)),
        ("/evidence/chats/0/start_ms", serde_json::json!(true)),
        ("/evidence/chats/0/decode_tok_s", serde_json::json!("2.125")),
        (
            "/evidence/host_metrics/0/owner/load/0",
            serde_json::json!({"$serde_json::private::Number":"1.25"}),
        ),
    ] {
        let mut bad = input.clone();
        *bad.pointer_mut(pointer).unwrap() = invalid;
        assert_eq!(
            device_harness::cli::evaluate(&serde_json::to_vec(&bad).unwrap())
                .unwrap_err()
                .code,
            "invalid_evaluation_input",
            "{pointer}"
        );
    }
    let text = String::from_utf8(bytes).unwrap();
    for bad in [
        text.replacen(
            "\"scenario\":\"soak\"",
            "\"scenario\":\"soak\",\"scenario\":\"stranger\"",
            1,
        ),
        text.replacen(
            "\"schema_version\":1",
            "\"schema_version\":1,\"schema_version\":1",
            1,
        ),
        text.replacen(
            "\"duration_ms\":1201900",
            "\"duration_ms\":1201900,\"duration_ms\":1201900",
            1,
        ),
        text.replacen("\"scenario\":\"soak\"", "\"scenario\":\"unknown\"", 1),
        text.replacen(
            "\"schema_version\":1",
            "\"schema_version\":1,\"candidate\":null",
            1,
        ),
    ] {
        assert_ne!(bad, text);
        assert_eq!(
            device_harness::cli::evaluate(bad.as_bytes())
                .unwrap_err()
                .code,
            "invalid_evaluation_input"
        );
    }
}

#[test]
fn final_and_lifecycle_requirements_cannot_inherit_legacy_35_minute_threshold() {
    let mut p = policy();
    p.minimum_ms = 2700000;
    p.minimum_rollovers = 2;
    p.required_steps = ["member_renewal".into(), "normal_chat".into(), "quit".into()].into();
    let mut e = good();
    e.duration_ms = 2100000;
    assert!(!evaluate(&p, &e).scenario_passed);
    e.duration_ms = 2700000;
    let mut second = e.rollovers[0].clone();
    second.from_run_id = "b".into();
    second.to_run_id = "c".into();
    second.start_ms = 2000000;
    second.end_ms = 2060000;
    e.rollovers.push(second);
    assert!(!evaluate(&p, &e).scenario_passed);
    e.completed_steps = p.required_steps.clone();
    assert!(evaluate(&p, &e).scenario_passed);
    e.clock_started_at_distributed_output = false;
    assert!(!evaluate(&p, &e).scenario_passed);
}
