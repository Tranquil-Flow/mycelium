// SPDX-License-Identifier: AGPL-3.0-only
//! Lane Q: the joined route's startup challenge (v0.1.4 `_qualify_open_route`,
//! mycelium_live/supervisor.py:3787-3830 @ f4e366e9; `infer_startup_challenge`,
//! mycelium_live/route.py:4563-4590; the workflow's `_challenge_event` / `_startup_failure` /
//! `started`, mycelium_a13_macos/workflow.py:195-245, 300-306; the step,
//! mycelium_a13_macos/demo_chain.py:987-1106).
//!
//! Model-free: the owner's real epoch, carrier, request service and every member's real stage
//! server, over the CONTROLLED stage compute of the serving test members (the final stage emits
//! scripted token ids). The challenge's tokens come back through the opened route; a scripted
//! wrong token is refused by the real comparison.
use super::*;
use owner_run::chain::StepEnv;
use owner_run::steps::{
    exchange::{ClockSkew, MemberRow, ReportsPort},
    model::{ModelStepOutput, PreparedModel, PreparedSource},
    serving::run_serving_step,
};
use work::Challenge;

#[path = "owner_prepare_honesty.rs"]
mod prepare_honesty;

/// The challenge prompt (carrier test values) and the scripted output it expects.
fn challenge(expected: &[u32]) -> Challenge {
    Challenge {
        prompt: (0..4).collect(),
        expected: expected.to_vec(),
    }
}
/// Two stage scripts: the final stage emits `tokens` by session position.
fn scripts_emitting(tokens: &[u32]) -> Vec<Serving> {
    vec![
        Serving::default(),
        Serving {
            tokens: tokens.to_vec(),
            ..Serving::default()
        },
    ]
}
/// A serving work port whose plan carries `challenge`, committing to `exchange`.
fn challenged(
    s: &mut Swarm,
    challenge: Option<Challenge>,
    scripts: Vec<Serving>,
) -> (OwnerWork, Vec<Watched>, Arc<RunExchange>) {
    let mut plan = s.serving_plan();
    plan.challenge = challenge;
    let (work, members) = s.serving(plan, scripts);
    let exchange = Arc::new(RunExchange::new(Box::new(app_host::now)));
    (work.with_exchange(exchange.clone()), members, exchange)
}
/// start_serving -> every stage prepared -> activation, then the workflow's first settled state
/// after `starting` (api_running, failed or stopped).
fn activate_until_settled(work: &mut OwnerWork) -> Value {
    let proposal = json!({"max_wall_seconds":60});
    let started = work.start_serving(&proposal);
    assert!(started.is_ok(), "{started:?}");
    let staged = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(staged["state"], "awaiting_activation", "{staged}");
    let activated = ActivationPort::activate(work, &proposal);
    assert!(activated.is_ok(), "{activated:?}");
    work.wait_state(
        |s| {
            matches!(
                s["state"].as_str(),
                Some("api_running" | "failed" | "stopped")
            )
        },
        span(B::WorkerStartup) * 2,
    )
}
/// v0.1.4's started record (`run_registry_server`, supervisor.py:4416-4423) for a route whose
/// startup challenge qualified it, at the gateway's url.
fn qualified_startup(gateway: &Value) -> Value {
    json!({"protocol":"mycelium.live_registry_server_started.v1",
        "url":format!("http://{}/", gateway["addr"].as_str().unwrap()),
        "startup_qualification_complete":true,"simulated":false,"api_only":true,
        "deployment_count":1,"unavailable_deployment_count":0})
}

/// No members and no reports: the serving step's report reads are empty.
struct NoReports;
impl ReportsPort for NoReports {
    fn forget_reports(&mut self) {}
    fn announce(&mut self, _: &Announcement) -> Result<(), StepError> {
        panic!("the serving step never announces outside its fence")
    }
    fn reports(&mut self) -> Result<BTreeMap<String, Value>, StepError> {
        Ok(BTreeMap::new())
    }
    fn members(&mut self) -> Vec<MemberRow> {
        Vec::new()
    }
    fn own_node_id(&mut self) -> Option<String> {
        None
    }
    fn member_identity(&mut self, _: &str) -> Value {
        Value::Null
    }
    fn clock_skew(&mut self, _: &[String]) -> Result<Option<ClockSkew>, StepError> {
        Ok(None)
    }
    fn tick_local(&mut self) {}
}
/// The run's step environment: real time, the run's own Stop flag, the results it set.
struct RunEnv {
    began: Instant,
    stop: Arc<AtomicBool>,
    result: BTreeMap<String, Value>,
    progress: Vec<String>,
}
impl RunEnv {
    fn new(stop: Arc<AtomicBool>) -> Self {
        Self {
            began: Instant::now(),
            stop,
            result: BTreeMap::new(),
            progress: Vec::new(),
        }
    }
}
impl StepEnv for RunEnv {
    fn progress(&mut self, detail: &str) {
        self.progress.push(detail.to_owned());
    }
    fn stopped(&self) -> bool {
        self.stop.load(Ordering::SeqCst)
    }
    fn now(&mut self) -> f64 {
        self.began.elapsed().as_secs_f64()
    }
    fn wall(&mut self) -> f64 {
        app_host::now()
    }
    fn set_result(&mut self, key: &str, value: Value) {
        self.result.insert(key.to_owned(), value);
    }
    fn record_exception(&mut self, code: &str, class: &str) {
        panic!("unexpected exception {code} {class}")
    }
    /// `ctx.stop_event.wait(interval)`: the run's Stop ends the wait within one owner poll.
    fn pause(&mut self, seconds: f64) -> bool {
        let end = Instant::now() + Duration::from_secs_f64(seconds);
        while Instant::now() < end && !self.stopped() {
            std::thread::sleep(
                end.saturating_duration_since(Instant::now())
                    .min(span(B::OwnerPoll) / 4),
            );
        }
        self.stopped()
    }
}
/// The serving run's state (the work port's run ids, compiled at epoch 0, its model).
fn serving_run(run: &str) -> RunState {
    let mut state = serving_state(run);
    let manifest = protocol::canonical::digest(&json!("manifest")).unwrap();
    state.model = Some(ModelStepOutput {
        forced_slice: false,
        model_id: "Qwen/Qwen3-0.6B".into(),
        resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
        manifest_digest: manifest.clone(),
        slice_mode: true,
        choice: None,
        prepared: PreparedSource {
            model: PreparedModel {
                model_id: "Qwen/Qwen3-0.6B".into(),
                resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
                architecture: "qwen3".into(),
                label: "model-free serving fixture".into(),
                choice: None,
                shape_registered: true,
                owner_storage_need: 0,
            },
            manifest_digest: manifest,
            source_bytes: json!(0),
        },
    });
    state.work_limits = Some(json!({"max_wall_seconds":60}));
    state
}
/// Step 10 (`run_serving_step`) over `work` for `state`, with the run's Stop flag `stop`.
fn serving_step(
    work: &mut OwnerWork,
    state: &mut RunState,
    stop: Arc<AtomicBool>,
) -> (Result<(), StepError>, RunEnv) {
    let mut env = RunEnv::new(stop);
    let poll = seconds(B::OwnerPoll);
    let result = run_serving_step(&mut env, &mut NoReports, work, state, poll);
    (result, env)
}
/// The challenge request's record: it reached its terminal (released) with `terminal`.
fn released(state: &Value, terminal: &str) {
    let record = &state["startup_challenge"];
    assert_eq!(record["released"], true, "{state}");
    assert_eq!(record["terminal"], terminal, "{state}");
}

/// Proves: B-RUN-10 (lane Q, the challenge qualifies the route: v0.1.4 `_qualify_open_route`
/// runs the controller-authenticated prompt through the OPEN route for exactly
/// `len(expected)` new tokens, compares the tokens that came back with the expected ids, reports
/// each verified index while the workflow is `starting` (`challenge_progress`, workflow.py:195-216,
/// 238-244), releases the request, and only then publishes the started record with
/// `startup_qualification_complete: true` and `unavailable_deployment_count: 0`
/// (supervisor.py:3787-3830, 4416-4423; workflow.py:300-306). Step 10, the REAL
/// `run_serving_step`, then ends `serving` with `registry_origin` set to the gateway's origin,
/// and a chat after the challenge starts on a fresh session.)
#[test]
fn the_startup_challenge_runs_through_the_opened_route_and_qualifies_it() {
    let mut s = swarm(2);
    let (entered, held) = mpsc::channel();
    let (open, gate) = mpsc::channel();
    let mut scripts = scripts_emitting(&SCRIPTED);
    // Hold the final stage's third challenge step: two tokens are back by then.
    scripts[1].hold = Some((2, entered, gate));
    let (mut work, members, exchange) = challenged(&mut s, Some(challenge(&SCRIPTED)), scripts);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let proposal = json!({"max_wall_seconds":60});
    assert!(work.start_serving(&proposal).is_ok());
    let staged = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(staged["state"], "awaiting_activation", "{staged}");
    assert!(ActivationPort::activate(&mut work, &proposal).is_ok());
    assert!(
        held.recv_timeout(span(B::WorkerStartup)).is_ok(),
        "the challenge runs on the opened route"
    );
    let progress = work.wait_state(
        |s| s["challenge_progress"]["verified_token_count"] == 2,
        span(B::WorkerStartup),
    );
    assert_eq!(progress["state"], "starting", "{progress}");
    assert_eq!(
        progress["challenge_progress"],
        json!({"verified_token_count": 2}),
        "{progress}"
    );
    assert_eq!(
        progress["startup"],
        Value::Null,
        "not before the comparison"
    );
    open.send(()).unwrap();
    let serving = work.wait_state(|s| s["state"] != "starting", span(B::WorkerStartup) * 2);
    assert_eq!(serving["state"], "api_running", "{serving}");
    let gateway = work.gateway().unwrap();
    assert_eq!(serving["startup"], qualified_startup(&gateway), "{serving}");
    assert_eq!(
        serving["challenge_progress"],
        Value::Null,
        "only while starting"
    );
    released(&serving, "completed");
    for (seen, _, control) in &members {
        assert!(
            control.wait(seen, |s| count(s, "reset") >= 1, span(B::WorkerStartup)),
            "the challenge's end resets the stage session: {:?}",
            seen.lock().unwrap().compute
        );
    }
    // Step 10 over the qualified route.
    let mut state = serving_run("s4a-run");
    let (result, env) = serving_step(&mut work, &mut state, Arc::default());
    assert_eq!(result, Ok(()), "{:?}", env.progress);
    let origin = format!("http://{}", gateway["addr"].as_str().unwrap());
    assert_eq!(env.result.get("registry_origin"), Some(&json!(origin)));
    assert_eq!(phase(&exchange), "serving");
    // A chat after the challenge: a fresh session on the final stage.
    let all = chat(&gateway);
    assert_eq!(
        token_ids(&all),
        SCRIPTED.iter().map(|t| u64::from(*t)).collect::<Vec<_>>(),
        "{all:?}"
    );
    work.teardown().unwrap();
    let state = work.state();
    assert_eq!(state["state"], "stopped", "{state}");
    assert_eq!(state["cleanup_error"], Value::Null, "{state}");
    for (seen, _, _) in &members {
        let served = seen.lock().unwrap().served.clone().unwrap().unwrap();
        assert_eq!(served["requests_served"], 2, "challenge + chat: {served}");
    }
}

/// Proves: B-RUN-10 (lane Q, the comparison is exact: `result.token_ids != startup_output` ->
/// `startup_challenge_failed` (supervisor.py:3818-3819), published by `_startup_failure` before
/// the route closes (workflow.py:218-230, supervisor.py:3909-3917). One wrong token at the first,
/// a middle and the last position: the workflow fails `startup_challenge_failed`, the started
/// record is never written, the request was released (it completed and reached its terminal)
/// and step 10 ends with the workflow's code (`up()`, demo_chain.py:1016-1019).)
#[test]
fn one_wrong_token_anywhere_refuses_the_route_startup_challenge_failed() {
    for wrong in [0usize, 2, 3] {
        let mut s = swarm(2);
        let mut emitted = SCRIPTED.to_vec();
        emitted[wrong] = 7;
        let (mut work, members, exchange) = challenged(
            &mut s,
            Some(challenge(&SCRIPTED)),
            scripts_emitting(&emitted),
        );
        let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
        let ended = activate_until_settled(&mut work);
        assert_eq!(ended["state"], "failed", "token {wrong}: {ended}");
        assert_eq!(ended["error_code"], "startup_challenge_failed", "{ended}");
        assert_eq!(
            ended["startup"],
            Value::Null,
            "token {wrong}: never qualified"
        );
        assert_eq!(ended["challenge_progress"], Value::Null, "{ended}");
        let mut state = serving_run("s4a-run");
        let (result, _) = serving_step(&mut work, &mut state, Arc::default());
        assert!(
            matches!(&result, Err(StepError::Refused(e)) if e.code == "startup_challenge_failed"),
            "token {wrong}: {result:?}"
        );
        assert_eq!(phase(&exchange), "idle", "nothing announced");
        work.teardown().unwrap();
        let state = work.state();
        assert_eq!(state["state"], "failed", "{state}");
        released(&state, "completed");
        assert_eq!(state["cleanup_error"], Value::Null, "{state}");
    }
}

/// Proves: B-RUN-10 (lane Q, the challenge runs with product stop tokens disabled for that
/// request only: `infer_startup_challenge` passes `_stop_token_ids_override=frozenset()`
/// (route.py:4563-4590), so an expected output that contains a product stop token is not
/// truncated; a later chat on the same epoch still stops at it.)
#[test]
fn product_stop_tokens_do_not_truncate_the_startup_challenge() {
    const EOS: u32 = 151_645;
    let expected = [11, EOS, 13, 151_643];
    let mut s = swarm(2);
    let (mut work, members, _) = challenged(
        &mut s,
        Some(challenge(&expected)),
        scripts_emitting(&expected),
    );
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let serving = activate_until_settled(&mut work);
    assert_eq!(serving["state"], "api_running", "{serving}");
    assert_eq!(
        serving["startup"]["startup_qualification_complete"], true,
        "{serving}"
    );
    released(&serving, "completed");
    // A product request on the same epoch still stops at the product stop token, which it never
    // emits (lane CHATFIX item 2, route.py:5094-5098; this line pinned `[11, EOS]` before).
    let all = chat(&work.gateway().unwrap());
    assert_eq!(token_ids(&all), [11], "{all:?}");
    work.teardown().unwrap();
}

/// Lane CHATFIX item 2 (ledger row 112 a): a product chat ends at the run model's stop ids
/// (`<|im_end|>` 151645, `<|endoftext|>` 151643: `Qwen2PromptCodec._stop_token_ids`,
/// codec.py:135-139, set on the route by `route.set_stop_token_ids`, supervisor.py:1952-1955) and
/// the stop token itself is never emitted: v0.1.4's route stops BEFORE `sink.emit` for a stop id
/// (route.py:5094-5098; its codec decodes one to "", codec.py:177-179), so the page never shows
/// `<|im_end|>`. Model-free: the final stage scripts the ids; the challenge (stop ids off for it)
/// still sees all four.
#[test]
fn a_product_chat_ends_at_a_stop_id_and_never_emits_it() {
    for stop in api::prompt_codec::QWEN_STOP_TOKEN_IDS {
        let expected = [11, stop, 13, 14];
        let mut s = swarm(2);
        let (mut work, members, _) = challenged(
            &mut s,
            Some(challenge(&expected)),
            scripts_emitting(&expected),
        );
        let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
        let serving = activate_until_settled(&mut work);
        assert_eq!(serving["state"], "api_running", "{stop}: {serving}");
        let all = chat(&work.gateway().unwrap());
        assert_eq!(token_ids(&all), [11], "{stop}: {all:?}");
        assert!(
            all.iter().all(|e| !e["text"]
                .as_str()
                .is_some_and(|t| t.contains("<|im_end|>") || t.contains("<|endoftext|>"))),
            "{stop}: {all:?}"
        );
        assert_eq!(all.last().unwrap()["type"], "completed", "{stop}: {all:?}");
        work.teardown().unwrap();
    }
}

/// Proves: B-RUN-10 (lane Q, a member that dies mid-challenge: the route's own error ends the
/// challenge (v0.1.4: a controller error -> `startup_route_rejected:<code>`, whose public code is
/// the controller's, supervisor.py:3812-3814, 3920-3938; published by `_startup_failure`), the
/// started record is never written, the request is released, and step 10 ends with that code,
/// never `serving`.)
#[test]
fn a_member_that_dies_mid_challenge_fails_the_route_with_the_routes_code() {
    let mut s = swarm(2);
    let mut scripts = scripts_emitting(&SCRIPTED);
    scripts[1].fail_at = Some(2);
    let (mut work, members, exchange) = challenged(&mut s, Some(challenge(&SCRIPTED)), scripts);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let ended = activate_until_settled(&mut work);
    assert_eq!(ended["state"], "failed", "{ended}");
    let code = ended["error_code"].as_str().unwrap_or_default().to_owned();
    eprintln!("dead member mid-challenge: {code}");
    assert!(
        !code.is_empty() && code != "startup_challenge_failed" && code != "managed_workflow_failed",
        "the route's own code: {ended}"
    );
    assert_eq!(ended["startup"], Value::Null, "{ended}");
    let mut state = serving_run("s4a-run");
    let (result, _) = serving_step(&mut work, &mut state, Arc::default());
    assert!(
        matches!(&result, Err(StepError::Refused(e)) if e.code == code),
        "{result:?}"
    );
    assert_eq!(phase(&exchange), "idle");
    work.teardown().unwrap();
    let state = work.state();
    assert_eq!(state["startup_challenge"]["released"], true, "{state}");
}

/// Proves: B-RUN-10 (lane Q, Stop mid-challenge: the run's Stop ends step 10 `stopped` within its
/// poll while the challenge is in flight (v0.1.4: the step's wait checks `ctx.stop_event`
/// first, demo_chain.py:1023-1025); the run's teardown then stops the workflow, which cancels
/// and releases the challenge request (`finally: route.release_request`, supervisor.py:3829-3830;
/// `stop()` clears the challenge progress, workflow.py:271-275), closes the epoch before the
/// members discard and ends `stopped` within the stop allowance, with no qualification.)
#[test]
fn a_stop_mid_challenge_ends_the_step_stopped_and_releases_the_request() {
    let mut s = swarm(2);
    let (entered, held) = mpsc::channel();
    let (open, gate) = mpsc::channel();
    let mut scripts = scripts_emitting(&SCRIPTED);
    scripts[1].hold = Some((1, entered, gate));
    let (mut work, members, exchange) = challenged(&mut s, Some(challenge(&SCRIPTED)), scripts);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let proposal = json!({"max_wall_seconds":60});
    assert!(work.start_serving(&proposal).is_ok());
    let staged = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(staged["state"], "awaiting_activation", "{staged}");
    assert!(ActivationPort::activate(&mut work, &proposal).is_ok());
    assert!(
        held.recv_timeout(span(B::WorkerStartup)).is_ok(),
        "the challenge is in flight"
    );
    let run_stop = Arc::new(AtomicBool::new(false));
    let mut state = serving_run("s4a-run");
    let (result, began, ended_at) = std::thread::scope(|scope| {
        let (flag, port, run) = (run_stop.clone(), &mut work, &mut state);
        let step = scope.spawn(move || {
            let began = Instant::now();
            let (result, _) = serving_step(port, run, flag);
            (result, began, Instant::now())
        });
        run_stop.store(true, Ordering::SeqCst);
        step.join().unwrap()
    });
    assert_eq!(result, Err(StepError::Stopped));
    assert!(
        ended_at - began <= span(B::OwnerPoll) * 4,
        "the step's own Stop check ends it"
    );
    assert_eq!(phase(&exchange), "idle");
    let bound = stop_allowance(60, 2);
    let (ended, stopper) = stop(work);
    open.send(()).unwrap();
    let stopped = ended.recv_timeout(bound + span(B::WorkerStartup));
    assert!(stopped.is_ok(), "Stop mid-challenge did not end");
    let (result, state) = stopped.unwrap();
    assert_eq!(result, Ok(()));
    assert_eq!(state["state"], "stopped", "{state}");
    assert_eq!(state["startup"], Value::Null, "{state}");
    assert_eq!(state["challenge_progress"], Value::Null, "{state}");
    assert_eq!(state["startup_challenge"]["released"], true, "{state}");
    assert_eq!(state["cleanup_error"], Value::Null, "{state}");
    for (seen, _, _) in &members {
        let seen = seen.lock().unwrap();
        assert_eq!(seen.cancels, 0, "the epoch's close ended the serve");
        let closed = first(&seen, "close").or(first(&seen, "abort"));
        assert!(closed.is_some(), "{:?}", seen.compute);
        assert!(closed < first(&seen, "discard"), "{:?}", seen.compute);
    }
    drop(stopper.join().unwrap());
}

/// Proves: B-RUN-10 (lane Q, every run qualifies its own route: a second run in the same process,
/// over the same members (rollover or the next Start: a new run, a new epoch), runs its own
/// startup challenge and qualifies again (v0.1.4: each started runtime runs
/// `_qualify_open_route`, supervisor.py:3860-3875).)
#[test]
fn a_second_run_in_the_same_process_qualifies_again() {
    let mut s = swarm(2);
    let mut next = s.serving_plan();
    next.run_id = "s4a-run-next".into();
    next.challenge = Some(challenge(&SCRIPTED));
    let (mut work, members, _) = challenged(
        &mut s,
        Some(challenge(&SCRIPTED)),
        scripts_emitting(&SCRIPTED),
    );
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let first_run = activate_until_settled(&mut work);
    assert_eq!(first_run["startup"]["startup_qualification_complete"], true);
    work.teardown().unwrap();
    let back = work.take_members();
    assert_eq!(back.len(), 2);
    let (seed, _, _) = app_host::identity_in(&s.owner, app_host::SEED_IDENTITY_FILE).unwrap();
    let mut again = OwnerWork::new(
        s.seed.coordinator.clone(),
        back,
        next,
        None,
        Arc::new(Prompt),
        host_clocks(),
    )
    .unwrap()
    .with_admission(Box::new(ControlledAdmission {
        seed,
        swarm: s.seed.coordinator.swarm_id().to_owned(),
        prepared: Vec::new(),
    }));
    assert_eq!(SlicesPort::node_slice_plans(&mut again), Ok(None));
    let second = activate_until_settled(&mut again);
    assert_eq!(second["state"], "api_running", "{second}");
    assert_eq!(
        second["startup"],
        qualified_startup(&again.gateway().unwrap()),
        "{second}"
    );
    released(&second, "completed");
    again.teardown().unwrap();
}

/// Proves: B-RUN-10 (lane Q, the route's fatal counter: `route.counters().fatal is not None` ->
/// `startup_challenge_failed` even when the tokens matched (supervisor.py:3818-3819). Here the
/// final stage cannot reset its session after the challenge, so the challenge completes with the
/// expected tokens and its own teardown retires the epoch (EP1's failed reset): the route is
/// refused, never started.)
#[test]
fn a_challenge_that_retires_its_epoch_refuses_startup_challenge_failed() {
    let mut s = swarm(2);
    let mut scripts = scripts_emitting(&SCRIPTED);
    scripts[1].reset_fails = true;
    let (mut work, members, _) = challenged(&mut s, Some(challenge(&SCRIPTED)), scripts);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let ended = activate_until_settled(&mut work);
    assert_eq!(ended["state"], "failed", "{ended}");
    assert_eq!(ended["error_code"], "startup_challenge_failed", "{ended}");
    assert_eq!(ended["startup"], Value::Null, "{ended}");
    work.teardown().unwrap();
    released(&work.state(), "completed");
}

/// Proves: B-RUN-10 (lane Q, the refusal is published before the slow teardown: `_startup_failure`
/// "Publish refusal before potentially slow remote teardown, never readiness" (workflow.py:218-230,
/// called before `route.close()`, supervisor.py:3909-3917). With the final stage's compute close
/// held, a mismatched challenge already reads `failed` / `startup_challenge_failed` while the
/// epoch is still closing.)
#[test]
fn a_failed_challenge_is_published_before_the_route_has_closed() {
    let mut s = swarm(2);
    let (entered, closing) = mpsc::channel();
    let (open, gate) = mpsc::channel();
    let mut emitted = SCRIPTED.to_vec();
    emitted[1] = 7;
    let mut scripts = scripts_emitting(&emitted);
    scripts[1].close_hold = Some((entered, gate));
    let (mut work, members, _) = challenged(&mut s, Some(challenge(&SCRIPTED)), scripts);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let proposal = json!({"max_wall_seconds":60});
    assert!(work.start_serving(&proposal).is_ok());
    let staged = work.wait_state(settled, span(B::WorkerStartup));
    assert_eq!(staged["state"], "awaiting_activation", "{staged}");
    assert!(ActivationPort::activate(&mut work, &proposal).is_ok());
    let held = closing.recv_timeout(span(B::WorkerStartup) * 2);
    let state = work.state();
    open.send(()).unwrap();
    assert!(held.is_ok(), "the route closes after the refusal");
    assert_eq!(state["state"], "failed", "while the route closes: {state}");
    assert_eq!(state["error_code"], "startup_challenge_failed", "{state}");
    work.teardown().unwrap();
}
