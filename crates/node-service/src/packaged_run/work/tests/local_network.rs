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

/// The owner's and the joined Mac's work transports on a LAN-shaped choice: bound to 127.0.0.1
/// under an explicit-peer policy that admits only 127.0.0.1, so the run has an admitted LAN path
/// (`NetworkChoice(Some(..))`) and every address `allows` accepts is on this Mac.
fn lan_on_loopback() -> WorkNetwork {
    let loopback = std::net::IpAddr::from([127, 0, 0, 1]);
    WorkNetwork::Fixed(NetworkChoice(Some((
        loopback,
        runtime::iroh_host::LanPolicy::with_explicit(vec![loopback]).unwrap(),
    ))))
}

/// Proves PHY-02 (lane PHY patch 2): a new run takes its probe targets from ITS verified members
/// only. An earlier run's blocked address is not probed into a new run whose joined Mac has not
/// published (and so is not verified) for it: Start's first step and the compile's start send no
/// datagram and never refuse `local_network_permission_required`. (Replaces the patch-1 test that
/// required the earlier run's address to be replayed into the fresh run.)
#[test]
fn a_new_run_whose_members_are_not_verified_yet_sends_nothing_and_is_never_refused() {
    let mut f = Fixture::on(lan_on_loopback());
    let mut chain = Composed::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Blocked);
    chain.work.probe = probe.clone();
    let earlier = published(&mut f, &chain.work, "run-ln2");
    assert!(chain.work.local_network(&earlier, false).is_ok());
    let first = probe.sent();
    assert!(!first.is_empty(), "the earlier run's member was probed");
    chain.new_run(
        "run-ln3",
        "dep-run-ln3",
        owner_run::Reason::Start,
        None,
        None,
    );
    // The new run as the chain holds it at Start (`model`) and at the compile's start: the probe
    // pass of both refusing steps (`Composed::run_step` hands it this state).
    let current = chain.inner.state().cloned().expect("the new run");
    assert!(chain.work.local_network(&current, true).is_ok());
    // The new run with the joined Mac named but not yet verified for it (it has not published).
    let mut fresh = RunState::new("run-ln3", "dep-run-ln3", owner_run::Reason::Start);
    fresh.participants = earlier.participants.clone();
    fresh.local_node = earlier.local_node.clone();
    assert!(chain.work.local_network(&fresh, true).is_ok());
    assert_eq!(probe.sent(), first, "no datagram into the new run");
}

/// Proves PHY-02: a member the seed revoked is not a current verified member, so its address is
/// not probed again, even in the run it was verified for, and it cannot veto the run.
#[test]
fn a_revoked_members_address_is_not_probed() {
    let mut f = Fixture::on(lan_on_loopback());
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Blocked);
    ports.work.probe = probe.clone();
    let state = published(&mut f, &ports.work, "run-rv1");
    assert!(ports.work.local_network(&state, false).is_ok());
    let first = probe.sent();
    assert!(!first.is_empty());
    let seed = f.host.with(|c| c.authority.seed.clone().unwrap());
    let generation = lock(&f.member_authority)
        .session
        .as_ref()
        .unwrap()
        .generation()
        .unwrap();
    seed.coordinator
        .advance_member_generation(&f.remote, generation, "STOPPED")
        .unwrap();
    assert!(ports.work.local_network(&state, true).is_ok());
    assert_eq!(
        probe.sent(),
        first,
        "the revoked member's address was probed"
    );
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
