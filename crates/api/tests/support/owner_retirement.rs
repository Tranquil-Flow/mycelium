// SPDX-License-Identifier: AGPL-3.0-only
//! PP round 3: real owner worker, epoch, gateway, serving step and run status. Only native
//! computation/admission and the remote call transport are controlled; tracking is product code.
use super::*;
use owner_run::runner::{Chain, Runner, SystemClocks, Timing};
#[path = "remote_under_test.rs"]
mod driver;
use driver::remote::{
    RemoteMember,
    tracking_hooks::{self, Hook},
};

struct RemoteFixture {
    endpoint: iroh::EndpointId,
    age: Arc<Mutex<Option<Duration>>>,
    polled: Arc<(Mutex<u64>, Condvar)>,
    poll_error: Arc<AtomicBool>,
}
impl RemoteFixture {
    fn wrap(mut link: TestLink) -> (WorkRemote, Self) {
        let endpoint = iroh::SecretKey::generate().public();
        let age = Arc::new(Mutex::new(None));
        let polled = Arc::new((Mutex::new(0), Condvar::new()));
        let notice = polled.clone();
        let aged = Arc::new(AtomicBool::new(false));
        let applied = aged.clone();
        let poll_error = Arc::new(AtomicBool::new(false));
        let failing = poll_error.clone();
        let cancel = link.canceller();
        tracking_hooks::install(
            endpoint,
            Hook {
                age: age.clone(),
                command: "serve",
                aged,
                call: Box::new(move |request| {
                    if request["command"] == "result" {
                        if failing.load(Ordering::SeqCst) {
                            if applied.load(Ordering::SeqCst) {
                                *notice.0.lock().unwrap() += 1;
                                notice.1.notify_all();
                            }
                            return Err("pp_test_result_poll_unavailable".into());
                        }
                        let result = match link.finish(Duration::ZERO) {
                            Err(Refusal(code)) if code == "member_reply_timeout" => {
                                json!({"ok":true,"result":{"finished":false}})
                            }
                            reply => {
                                let reply = match reply {
                                    Ok(v) => json!({"ok":true,"result":v}),
                                    Err(Refusal(c)) => json!({"ok":false,"code":c}),
                                };
                                json!({"ok":true,"result":{"finished":true,"reply":reply}})
                            }
                        };
                        if applied.load(Ordering::SeqCst) {
                            *notice.0.lock().unwrap() += 1;
                            notice.1.notify_all();
                        }
                        Ok(result)
                    } else if request["command"] == "cancel_operation" {
                        cancel();
                        Ok(json!({"ok":true}))
                    } else {
                        link.begin(request.clone()).map_err(|e| e.0)?;
                        Ok(json!({"ok":true}))
                    }
                }),
            },
        );
        let member = RemoteMember::start(
            iroh::SecretKey::generate(),
            iroh::EndpointAddr::new(endpoint),
            None,
        )
        .unwrap();
        (
            WorkRemote(member),
            Self {
                endpoint,
                age,
                polled,
                poll_error,
            },
        )
    }
    fn advance_tracking_wall(&self) {
        let mut count = self.polled.0.lock().unwrap();
        let before = *count;
        *self.age.lock().unwrap() = Some(span(B::MemberChosenWall) + span(B::OwnerPoll));
        let waited = self
            .polled
            .1
            .wait_timeout_while(count, span(B::WorkerStartup), |n| *n <= before)
            .unwrap();
        count = waited.0;
        assert!(
            *count > before,
            "driver must actually poll after the injected tracking expiry"
        );
    }
}
impl Drop for RemoteFixture {
    fn drop(&mut self) {
        tracking_hooks::remove(self.endpoint);
    }
}
struct WorkRemote(RemoteMember);
impl MemberLink for WorkRemote {
    fn begin(&mut self, command: Value) -> Result<(), Refusal> {
        self.0.begin(command)
    }
    fn finish(&mut self, within: Duration) -> Result<Value, Refusal> {
        self.0.finish(within)
    }
}

fn tracked_work(s: &mut Swarm) -> (OwnerWork, Vec<Watched>, Vec<RemoteFixture>) {
    tracked_work_with(s, host_clocks())
}
fn tracked_work_with(
    s: &mut Swarm,
    clocks: Clocks,
) -> (OwnerWork, Vec<Watched>, Vec<RemoteFixture>) {
    let mut plan = s.serving_plan();
    // Signed lifetime is deliberately greater than the UNCHANGED production tracking budget.
    for wall in plan.worker_walls.values_mut() {
        *wall = span(B::MemberChosenWall).as_secs() * 2;
    }
    plan.challenge = Some(challenge(&SCRIPTED));
    let (seed, _, _) = app_host::identity_in(&s.owner, app_host::SEED_IDENTITY_FILE).unwrap();
    let mut taken = Vec::new();
    let mut members = Vec::new();
    let mut fixtures = Vec::new();
    for (mut j, script) in s.members.drain(..).zip(scripts_emitting(&SCRIPTED)) {
        j.member.serving = Some(script);
        taken.push((j.seen.clone(), j.expected.clone(), j.member.control.clone()));
        let (link, fixture) = RemoteFixture::wrap(TestLink::start(j.member));
        members.push(WorkMember {
            node_id: j.node_id,
            cancel: Arc::new(link.0.canceller()),
            link: Box::new(link),
            registration: j.registration,
        });
        fixtures.push(fixture);
    }
    let work = OwnerWork::new(
        s.seed.coordinator.clone(),
        members,
        plan,
        None,
        Arc::new(Prompt),
        clocks,
    )
    .unwrap()
    .with_admission(Box::new(ControlledAdmission {
        seed,
        swarm: s.seed.coordinator.swarm_id().to_owned(),
        prepared: Vec::new(),
    }))
    .with_exchange(Arc::new(RunExchange::new(Box::new(app_host::now))));
    (work, taken, fixtures)
}

/// Earlier steps are modeled; qualification and step 10 are real. No fake ready status.
struct ServingChain {
    work: Arc<Mutex<OwnerWork>>,
    state: RunState,
}
impl Chain for ServingChain {
    fn new_run(&mut self, _: &str, _: &str, _: Reason, _: Option<&Value>, _: Option<&Value>) {}
    fn run_step(&mut self, step: &str, env: &mut dyn StepEnv) -> Result<(), StepError> {
        if step == "serving" {
            run_serving_step(
                env,
                &mut NoReports,
                &mut *self.work.lock().unwrap(),
                &mut self.state,
                seconds(B::OwnerPoll),
            )
        } else {
            Ok(())
        }
    }
    fn teardown(&mut self, _: bool, _: &mut dyn StepEnv) -> Result<(), String> {
        self.work.lock().unwrap().teardown()
    }
    fn monitor(&mut self) -> MonitorProbe {
        self.work.lock().unwrap().monitor(&self.state)
    }
}
fn running(work: OwnerWork) -> (Runner, Arc<Mutex<OwnerWork>>) {
    let work = Arc::new(Mutex::new(work));
    let runner = Runner::spawn(
        Box::new(ServingChain {
            work: work.clone(),
            state: serving_run("s4a-run"),
        }),
        Box::new(|| RunIdentity {
            run_id: "s4a-run".into(),
            deployment_id: "12345678-1234-5678-9234-abcdefabcdef".into(),
        }),
        Arc::new(SystemClocks::new()),
        Timing::registered(),
    )
    .unwrap();
    runner
        .command(|run, clock| {
            run.start("start", None, clock)
                .map(|commands| (commands, ()))
        })
        .unwrap();
    let until = Instant::now() + span(B::WorkerStartup);
    while runner.view().snapshot()["state"] == "running" && Instant::now() < until {
        std::thread::yield_now();
    }
    assert_eq!(
        runner.view().snapshot()["state"],
        "serving",
        "serving step settles"
    );
    assert_eq!(runner.view().snapshot()["route_ready"], true);
    (runner, work)
}

#[test]
fn red_a_tracking_timeout_does_not_retire_a_live_serving_epoch_or_refuse_chat() {
    let mut s = swarm(2);
    let (mut work, members, fixtures) = tracked_work(&mut s);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    assert_eq!(activate_until_settled(&mut work)["state"], "api_running");
    let (runner, work) = running(work);
    let gateway = work.lock().unwrap().gateway().unwrap();
    assert_eq!(chat(&gateway).last().unwrap()["type"], "completed");
    let expires = work.lock().unwrap().state()["route_expires_at"]
        .as_f64()
        .unwrap();
    assert!(expires - app_host::now() > seconds(B::MemberChosenWall));
    fixtures[0].advance_tracking_wall();
    let state = work
        .lock()
        .unwrap()
        .wait_state(|s| !s["epoch_retired"].is_null(), span(B::OwnerPoll) * 4);
    let (status, admitted) = post(&gateway);
    // Clean up before asserting RED, including admitted requests and test-member ownership.
    runner.stop();
    assert!(runner.view().join(Instant::now() + span(B::WorkerStartup)));
    drop(runner);
    drop(work);
    assert_eq!(state["state"], "api_running");
    assert_eq!(
        state["epoch_retired"],
        Value::Null,
        "local tracking timeout is not a member end"
    );
    assert_eq!(
        status, 202,
        "subsequent chat must remain admitted: {admitted}"
    );
}

#[test]
fn successor_run_does_not_retain_old_epoch_readiness() {
    let mut run = OwnerRun::new(Box::new(|| RunIdentity {
        run_id: "readiness-run".into(),
        deployment_id: "readiness-deployment".into(),
    }));
    let clock = Clock {
        mono: 0.0,
        wall: 0.0,
    };
    run.start("start", None, clock).unwrap();
    let old = Arc::new(AtomicBool::new(true));
    let observed = old.clone();
    run.set_route_readiness(Arc::new(move || observed.load(Ordering::SeqCst)));
    for _ in owner_run::machine::STEPS {
        run.step_finished(StepOutcome::Ok);
    }
    assert!(run.snapshot().route_ready);
    old.store(false, Ordering::SeqCst);
    assert!(!run.snapshot().route_ready);
    run.stop();
    run.teardown_finished(Ok(()), clock);
    run.start("start", None, clock).unwrap();
    // The new context must not inherit the retired reader, even before its own binding.
    for _ in owner_run::machine::STEPS {
        run.step_finished(StepOutcome::Ok);
    }
    assert!(run.snapshot().route_ready);
    run.set_route_readiness(Arc::new(|| true));
    assert!(run.snapshot().route_ready);
}

#[test]
fn failed_result_polls_after_tracking_wall_keep_serve_live_until_actual_member_end() {
    let mut s = swarm(2);
    let (mut work, members, fixtures) = tracked_work(&mut s);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    assert_eq!(activate_until_settled(&mut work)["state"], "api_running");
    let (runner, work) = running(work);
    let gateway = work.lock().unwrap().gateway().unwrap();
    fixtures[0].poll_error.store(true, Ordering::SeqCst);
    fixtures[0].advance_tracking_wall();
    let live = work
        .lock()
        .unwrap()
        .wait_state(|s| !s["epoch_retired"].is_null(), span(B::OwnerPoll) * 4);
    assert_eq!(live["epoch_retired"], Value::Null);
    assert_eq!(runner.view().snapshot()["route_ready"], true);
    assert_eq!(chat(&gateway).last().unwrap()["type"], "completed");
    // Restoring observation does not re-admit serve; the SAME operation's real end is consumed.
    fixtures[0].poll_error.store(false, Ordering::SeqCst);
    members[0].2.cancel(&members[0].0);
    let retired = work
        .lock()
        .unwrap()
        .wait_state(|s| !s["epoch_retired"].is_null(), span(B::WorkerStartup));
    let after = runner.view().snapshot();
    let (status, _) = post(&gateway);
    runner.stop();
    assert!(runner.view().join(Instant::now() + span(B::WorkerStartup)));
    assert_eq!(retired["state"], "api_running");
    assert!(retired["epoch_retired"].is_string());
    assert_eq!(after["state"], "serving");
    assert_eq!(after["route_ready"], false);
    assert_ne!(status, 202);
}

#[test]
fn signed_expiry_clears_readiness_without_a_chat_or_status_tick() {
    let mut s = swarm(2);
    let now = Arc::new(AtomicU64::new(app_host::now().to_bits()));
    let observed = now.clone();
    let mut clocks = host_clocks();
    clocks.wall = Arc::new(move || f64::from_bits(observed.load(Ordering::SeqCst)));
    let (mut work, members, _fixtures) = tracked_work_with(&mut s, clocks);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    assert_eq!(activate_until_settled(&mut work)["state"], "api_running");
    let expires = work.state()["route_expires_at"].as_f64().unwrap();
    let (runner, work) = running(work);
    let before = runner.view().snapshot();
    now.store(expires.to_bits(), Ordering::SeqCst);
    let after = runner.view().snapshot();
    assert_eq!(work.lock().unwrap().state()["state"], "api_running");
    runner.stop();
    assert!(runner.view().join(Instant::now() + span(B::WorkerStartup)));
    assert_eq!(after["state"], "serving");
    assert_eq!(after["steps"], before["steps"]);
    assert_eq!(after["route_ready"], false);
}

#[test]
fn finite_operations_keep_their_existing_tracking_wall_on_successful_and_failed_polls() {
    for fails in [false, true] {
        let endpoint = iroh::SecretKey::generate().public();
        tracking_hooks::install(
            endpoint,
            Hook {
                command: "prepare_native",
                aged: Arc::new(AtomicBool::new(false)),
                age: Arc::new(Mutex::new(Some(
                    span(B::MemberChosenWall) + span(B::OwnerPoll),
                ))),
                call: Box::new(move |r| {
                    if r["command"] == "result" {
                        if fails {
                            Err("pp_test_result_poll_unavailable".into())
                        } else {
                            Ok(json!({"ok":true,"result":{"finished":false}}))
                        }
                    } else {
                        Ok(json!({"ok":true}))
                    }
                }),
            },
        );
        let mut link = RemoteMember::start(
            iroh::SecretKey::generate(),
            iroh::EndpointAddr::new(endpoint),
            None,
        )
        .unwrap();
        link.begin(json!({"command":"prepare_native"})).unwrap();
        let outcome = link.finish(span(B::WorkerStartup));
        drop(link);
        tracking_hooks::remove(endpoint);
        let expected = if fails {
            "control_call_failed:pp_test_result_poll_unavailable"
        } else {
            "member_operation_wall_exceeded"
        };
        assert_eq!(outcome.unwrap_err().0, expected);
    }
}

#[test]
fn red_b_retired_epoch_clears_run_readiness_without_ending_api_running() {
    let mut s = swarm(2);
    let mut scripts = scripts_emitting(&SCRIPTED);
    // Trigger genuine member consent exhaustion only AFTER qualification and the positive snapshot.
    let grant = Arc::new(AtomicU64::new(u64::MAX));
    scripts[1].commands = Some(grant.clone());
    let (mut work, members, _) = challenged(&mut s, Some(challenge(&SCRIPTED)), scripts);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    assert_eq!(activate_until_settled(&mut work)["state"], "api_running");
    let (runner, work) = running(work);
    let before = runner.view().snapshot();
    grant.store(0, Ordering::SeqCst);
    let retired = work
        .lock()
        .unwrap()
        .wait_state(|s| !s["epoch_retired"].is_null(), span(B::WorkerStartup));
    assert_eq!(retired["state"], "api_running");
    assert!(retired["epoch_retired"].is_string());
    let after = runner.view().snapshot(); // no live-status tick or chat required
    let gateway = work.lock().unwrap().gateway().unwrap();
    let (status, _) = post(&gateway);
    runner.stop();
    assert!(runner.view().join(Instant::now() + span(B::WorkerStartup)));
    assert_eq!(after["state"], "serving");
    assert_eq!(
        after["steps"], before["steps"],
        "historical checklist remains done"
    );
    assert_ne!(status, 202);
    assert_eq!(
        after["route_ready"], false,
        "retired {:?} is not ready",
        retired["epoch_retired"]
    );
}
