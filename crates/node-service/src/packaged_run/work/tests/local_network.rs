// SPDX-License-Identifier: AGPL-3.0-only
//! Lane PHY item 2 (ledger rows 79, 80): macOS asks its Local Network question at the app's first
//! packet to another Mac, which used to be the owner's step-6 dial, after the compile. On 15 Nov a
//! fresh install lost one run on the owner's alert (`control_call_failed:control_connect_timeout`)
//! and one on the joined Mac's (`Expired`), both shown as `managed_workflow_failed`.
//!
//! The probe is behind its seam (`api::local_network::Probe`); what macOS answers was measured with
//! a probe app (`api::local_network` module doc): `EHOSTUNREACH` at once while pending or denied.
use super::*;
use api::local_network::{Answer, JOINED_MAC_SILENT, PERMISSION_REQUIRED, Probe};
use std::net::SocketAddr;

/// The seam: answers what the test says and records every datagram's address.
struct Scripted {
    answer: Mutex<Answer>,
    sent: Mutex<Vec<SocketAddr>>,
}
impl Scripted {
    fn new(answer: Answer) -> Arc<Self> {
        Arc::new(Self {
            answer: Mutex::new(answer),
            sent: Mutex::default(),
        })
    }
    fn answer(&self, answer: Answer) {
        *self.answer.lock().unwrap() = answer;
    }
    fn sent(&self) -> Vec<SocketAddr> {
        self.sent.lock().unwrap().clone()
    }
}
impl Probe for Scripted {
    fn send(&self, to: SocketAddr) -> Answer {
        self.sent.lock().unwrap().push(to);
        *self.answer.lock().unwrap()
    }
}

/// The run as it stands before its compile: the owner announced it and the joined Mac published
/// its signed work address for it (the joined Mac's agent tick); `work`'s owner key is the run's.
fn published(f: &mut Fixture, work: &Work, run: &str) -> RunState {
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    seed.exchange
        .set_owner_control(Some(OwnerControl::new(work.owner.public())));
    seed.exchange
        .announce(Announce {
            phase: "collect".into(),
            run_id: Some(run.into()),
            deployment_id: Some(format!("dep-{run}")),
            ..Default::default()
        })
        .unwrap();
    f.agent.tick(&f.member);
    let local = f
        .host
        .with(|c| c.run.contributor.as_ref().unwrap().node.clone());
    let mut state = RunState::new(run, &format!("dep-{run}"), owner_run::Reason::Start);
    state.participants = vec![local.clone(), f.remote.clone()];
    state.local_node = Some(local);
    state
}
/// The joined Mac's work addresses as the owner verifies them (signed by its enrolled key).
fn member_addresses(f: &Fixture, work: &Work, state: &RunState) -> Vec<SocketAddr> {
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    let verified = seed
        .work_members
        .get(
            &seed.coordinator,
            &f.remote,
            work.owner.public(),
            &state.run_id,
            &state.deployment_id,
            &|_| true,
        )
        .unwrap();
    verified.address().ip_addrs().copied().collect()
}
fn refusal(result: Result<(), StepError>) -> (String, String) {
    match result {
        Err(StepError::Refused(e)) => (e.code.clone(), e.message.clone().unwrap_or_default()),
        other => panic!("expected a named refusal, got {other:?}"),
    }
}
/// A Runner env whose Stop is already pressed: a step that is reached returns at once.
struct Stopping;
impl StepEnv for Stopping {
    fn progress(&mut self, _: &str) {}
    fn stopped(&self) -> bool {
        true
    }
    fn now(&mut self) -> f64 {
        100.0
    }
    fn wall(&mut self) -> f64 {
        app_host::now()
    }
    fn set_result(&mut self, _: &str, _: Value) {}
    fn record_exception(&mut self, _: &str, _: &str) {}
    fn pause(&mut self, _: f64) -> bool {
        true
    }
}

/// Proves: (a) and (b) before the compile. An early step sends ONE datagram to the joined Mac's
/// verified work address (which raises the alert) and never refuses; the compile's start refuses a
/// pending permission by name, saying which Mac and what to allow; once allowed it passes, and an
/// address already reached in this run gets no further datagram.
#[test]
fn a_pending_permission_is_asked_early_and_refuses_the_compile_by_name() {
    let mut f = Fixture::new();
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Blocked);
    ports.work.probe = probe.clone();
    let state = published(&mut f, &ports.work, "run-ln1");
    let addresses = member_addresses(&f, &ports.work, &state);
    assert!(!addresses.is_empty());
    assert!(ports.work.local_network(&state, false).is_ok());
    assert_eq!(probe.sent(), addresses, "one datagram per verified address");
    let (code, message) = refusal(ports.work.local_network(&state, true));
    assert_eq!(code, PERMISSION_REQUIRED);
    assert!(
        message.contains("THIS Mac") && message.contains("Local Network"),
        "{message}"
    );
    probe.answer(Answer::Sent);
    assert!(ports.work.local_network(&state, true).is_ok());
    let reached = probe.sent().len();
    assert!(ports.work.local_network(&state, true).is_ok());
    assert!(ports.work.local_network(&state, false).is_ok());
    assert_eq!(
        probe.sent().len(),
        reached,
        "a reached address is not probed again"
    );
}

/// Proves: (b) at Start's first step. A Start after a run whose datagram was blocked refuses at
/// once, before its first step (`model`, `machine::STEPS`) runs, by re-probing the address the earlier run verified; the
/// compile's start does the same.
#[test]
fn the_next_start_refuses_at_once_while_the_permission_is_still_pending() {
    let mut f = Fixture::new();
    let mut chain = Composed::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Blocked);
    chain.work.probe = probe.clone();
    let earlier = published(&mut f, &chain.work, "run-ln2");
    assert!(chain.work.local_network(&earlier, false).is_ok());
    let first = probe.sent();
    assert!(!first.is_empty());
    chain.new_run(
        "run-ln3",
        "dep-run-ln3",
        owner_run::Reason::Start,
        None,
        None,
    );
    let (code, _) = refusal(chain.run_step("model", &mut Stopping));
    assert_eq!(code, PERMISSION_REQUIRED);
    assert_eq!(
        probe.sent()[first.len()..],
        first[..],
        "the earlier run's address, again"
    );
    // Steps between only ask: the observe step is reached (and ends on the pressed Stop).
    let asked = probe.sent().len();
    let observed = chain.run_step("observe", &mut Stopping);
    assert!(
        !matches!(&observed, Err(StepError::Refused(e)) if e.code == PERMISSION_REQUIRED),
        "{observed:?}"
    );
    assert_eq!(
        probe.sent().len(),
        asked + first.len(),
        "the step asked again"
    );
    let (code, _) = refusal(chain.run_step("compile", &mut Stopping));
    assert_eq!(code, PERMISSION_REQUIRED);
}

/// Proves: (c). A step-6 dial that got no answer is named: by this Mac's permission when the
/// datagram to the joined Mac is blocked, else as the joined Mac's silence with its address, the
/// dial's cause and where to allow it; any other failure is left as it was, with no datagram.
#[test]
fn a_step_6_dial_without_an_answer_is_named_on_the_mac_that_must_allow_it() {
    let mut f = Fixture::new();
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Blocked);
    ports.work.probe = probe.clone();
    let state = run_state(&mut f, &ports);
    let mut env = Env { at: Instant::now() };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    let member = member_addresses(&f, &ports.work, &state);
    let timeout = "control_call_failed:control_connect_timeout";
    let named = ports.work.named_dial_failure(timeout);
    let (code, _) = refusal(Err(named.expect("named")));
    assert_eq!(code, PERMISSION_REQUIRED);
    assert!(
        member.iter().all(|a| probe.sent().contains(a)),
        "the joined Mac's address was probed: {:?}",
        probe.sent()
    );
    probe.answer(Answer::Sent);
    let named = ports.work.named_dial_failure("Expired");
    let (code, message) = refusal(Err(named.expect("named")));
    assert_eq!(code, JOINED_MAC_SILENT);
    assert!(
        message.contains(&member[0].ip().to_string())
            && message.contains("Expired")
            && message.contains("THERE"),
        "{message}"
    );
    let before = probe.sent().len();
    assert!(
        ports
            .work
            .named_dial_failure("member_consent_required")
            .is_none()
    );
    assert_eq!(probe.sent().len(), before, "no datagram for another cause");
    ports.work.teardown().unwrap();
}
