// SPDX-License-Identifier: AGPL-3.0-only
use device_harness::{Fault, FaultKind, http::Chat, schedule::*, soak::*};
fn p() -> Policy {
    Policy {
        requested_ms: 2100000,
        minimum_ms: 2100000,
        chat_every_ms: 300000,
        sample_every_ms: 60000,
        mailbox_every_ms: 600000,
        visitor_every_ms: 600000,
        restart_at_ms: Some(720000),
        minimum_rollovers: 1,
        mailbox_max_bytes: Some(4096),
        restart_expectation: RestartExpectation::AutomaticRecovery,
        required_steps: Default::default(),
    }
}
fn chat() -> Chat {
    Chat {
        done: true,
        terminal: Some("completed".into()),
        n_tokens: 3,
        ..Chat::default()
    }
}
fn run(r: &str, ready: bool) -> Run {
    Run {
        run_id: Some(r.into()),
        state: if ready { "serving" } else { "running" }.into(),
        route_ready: ready,
        participants: 2,
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
fn mailbox(at: u64) -> MailboxSample {
    MailboxSample {
        at_ms: at,
        owner: Some(2048),
        member: Some(0),
        owner_storage: 2048,
        member_storage: 0,
    }
}
#[test]
fn full_scripted_soak_reaches_rollover_restart_schedules_and_final_evaluator() {
    let mut m = Machine::new(p(), &chat(), &run("r1", true)).unwrap();
    let mut restarted = false;
    for at in (0..2100000).step_by(1000) {
        for task in m.poll(at).unwrap() {
            let done = match task.job {
                Job::Sample => Completion::Sample {
                    run: run(
                        if at >= 1680000 {
                            "r3"
                        } else if restarted {
                            "r2"
                        } else {
                            "r1"
                        },
                        !(1680000..1740000).contains(&at),
                    ),
                    member_http: 200,
                    metrics: Some(metrics_sample(at)),
                    published: false,
                },
                Job::Mailbox => Completion::Mailbox(mailbox(at)),
                Job::Chat => Completion::Chat(Chat {
                    start_ms: at,
                    end_ms: at,
                    ..chat()
                }),
                Job::Visitor => panic!("unpublished must not invoke visitor"),
                Job::Restart => {
                    restarted = true;
                    Completion::Restart {
                        receipt: Restart {
                            expectation: RestartExpectation::AutomaticRecovery,
                            attempted: true,
                            recovered: true,
                            idle_observed: false,
                            explicit_start: false,
                            member_mutations: 0,
                            endpoint_changed: Some(true),
                            approval: None,
                        },
                        run: run("r2", true),
                    }
                }
            };
            m.complete(task, at, Ok(done)).unwrap();
        }
    }
    assert!(m.poll(2100000).unwrap().is_empty());
    let v = m.finish(2100000, &run("r3", true), mailbox(2100000));
    assert!(v.scenario_passed, "{:?}", v.reasons);
    assert_eq!(m.evidence.chats.len(), 7);
    assert_eq!(m.evidence.samples, 35);
    assert_eq!(m.evidence.metrics_samples, 35);
    assert_eq!(m.evidence.mailbox_samples.len(), 5);
    assert_eq!(m.evidence.rollovers.len(), 1);
    assert_eq!(m.evidence.rollovers[0].gap_ms, 60000);
    assert!(v.f3_automatic_recovery);
}
#[test]
fn observation_loss_stops_new_work_incomplete_and_stale_completion_rejected() {
    let mut m = Machine::new(p(), &chat(), &run("r1", true)).unwrap();
    let tasks = m.poll(0).unwrap();
    let t = tasks.into_iter().find(|t| t.job == Job::Sample).unwrap();
    m.complete(
        t,
        0,
        Err(Fault::new("ssh_observation_lost", FaultKind::Observation)),
    )
    .unwrap();
    assert!(m.poll(1000).unwrap().is_empty());
    assert!(
        m.complete(
            t,
            1000,
            Err(Fault::new("ssh_observation_lost", FaultKind::Observation))
        )
        .is_err()
    );
    assert_eq!(
        m.finish(1000, &run("r1", true), mailbox(1000)).outcome,
        "incomplete"
    );
}
#[test]
fn restart_waits_for_inflight_chat_and_schedule_overrun_is_not_silent() {
    let mut m = Machine::new(p(), &chat(), &run("r1", true)).unwrap();
    let tasks = m.poll(0).unwrap();
    let t = *tasks.iter().find(|t| t.job == Job::Chat).unwrap();
    assert!(
        m.poll(720000)
            .unwrap()
            .iter()
            .all(|t| t.job != Job::Restart)
    );
    assert!(
        m.evidence
            .errors
            .iter()
            .any(|s| s == "chat_schedule_overrun")
    );
    m.complete(
        t,
        720000,
        Ok(Completion::Chat(Chat {
            start_ms: 0,
            end_ms: 720000,
            ..chat()
        })),
    )
    .unwrap();
    assert!(
        m.poll(721000)
            .unwrap()
            .iter()
            .any(|t| t.job == Job::Restart)
    );
}
#[test]
fn owner_ruled_scheduler_records_actual_restart_window_with_concurrent_chats() {
    let mut policy = p();
    policy.requested_ms = 1200000;
    policy.minimum_ms = 1200000;
    policy.chat_every_ms = 150000;
    let mut m = Machine::new(policy, &chat(), &run("r1", true)).unwrap();
    let mut pending_restart = None;
    for at in (0..1200000).step_by(1000) {
        if at == 1076000 {
            m.complete(
                pending_restart.take().unwrap(),
                at,
                Ok(Completion::Restart {
                    receipt: Restart {
                        expectation: RestartExpectation::AutomaticRecovery,
                        attempted: true,
                        recovered: true,
                        idle_observed: false,
                        explicit_start: false,
                        member_mutations: 0,
                        endpoint_changed: Some(true),
                        approval: None,
                    },
                    run: run("r3", true),
                }),
            )
            .unwrap();
        }
        for task in m.poll(at).unwrap() {
            let completion = match task.job {
                Job::Sample => Completion::Sample {
                    run: run(
                        if at >= 1076000 {
                            "r3"
                        } else if at >= 420000 {
                            "r2"
                        } else {
                            "r1"
                        },
                        !(420000..540000).contains(&at),
                    ),
                    member_http: 200,
                    metrics: Some(metrics_sample(at)),
                    published: false,
                },
                Job::Mailbox => Completion::Mailbox(mailbox(at)),
                Job::Chat => Completion::Chat(if pending_restart.is_some() {
                    let mut value = serde_json::to_value(Chat {
                        error: Some("chat_submission_failed".into()),
                        ..Chat::default()
                    })
                    .unwrap();
                    value["submit_http"] = 503.into();
                    value["submit_code"] = "chat_not_ready".into();
                    serde_json::from_value(value).unwrap()
                } else {
                    chat()
                }),
                Job::Restart => {
                    pending_restart = Some(task);
                    continue;
                }
                Job::Visitor => panic!("not published"),
            };
            m.complete(task, at, Ok(completion)).unwrap();
        }
    }
    let v = m.finish(1200000, &run("r3", true), mailbox(1200000));
    assert!(v.scenario_passed, "{v:?}");
    assert_eq!(
        serde_json::to_value(&m.evidence).unwrap()["restart_window"],
        serde_json::json!({"begin_ms":720000,"end_ms":1076000})
    );
    assert_eq!(
        serde_json::to_value(v).unwrap()["owner_restart_unavailable_chats"],
        3
    );
    assert_eq!(m.evidence.chats.len(), 8);
    assert_eq!(m.evidence.samples, 20);
}
#[test]
fn clock_does_not_start_on_http_200_no_content_or_non_distributed_run() {
    assert!(Machine::new(p(), &Chat::default(), &run("r1", true)).is_err());
    let mut r = run("r1", true);
    r.participants = 1;
    assert!(Machine::new(p(), &chat(), &r).is_err());
    let mut m = Machine::new(p(), &chat(), &run("r1", true)).unwrap();
    m.poll(1000).unwrap();
    assert!(m.poll(999).is_err());
}
