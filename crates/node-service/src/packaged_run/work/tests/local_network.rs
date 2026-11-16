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

/// The seam: answers what the test says and records every datagram's (from, to).
struct Scripted {
    answer: Mutex<Answer>,
    sent: Mutex<Vec<(std::net::IpAddr, SocketAddr)>>,
    /// Notes, per datagram, whether the run had started work when it left.
    watch: Mutex<Option<Arc<Watch>>>,
}
impl Scripted {
    fn new(answer: Answer) -> Arc<Self> {
        Arc::new(Self {
            answer: Mutex::new(answer),
            sent: Mutex::default(),
            watch: Mutex::default(),
        })
    }
    fn answer(&self, answer: Answer) {
        *self.answer.lock().unwrap() = answer;
    }
    fn sent(&self) -> Vec<SocketAddr> {
        self.sent
            .lock()
            .unwrap()
            .iter()
            .map(|(_, to)| *to)
            .collect()
    }
    fn from(&self) -> Vec<std::net::IpAddr> {
        self.sent
            .lock()
            .unwrap()
            .iter()
            .map(|(from, _)| *from)
            .collect()
    }
}
impl Probe for Scripted {
    fn send(&self, from: std::net::IpAddr, to: SocketAddr) -> Answer {
        if let Some(watch) = self.watch.lock().unwrap().as_ref() {
            watch.record();
        }
        self.sent.lock().unwrap().push((from, to));
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

/// Proves: (a) and (b) before the compile. An early step sends ONE datagram to the joined Mac's
/// verified work address (which raises the alert) and never refuses; the compile's start refuses a
/// pending permission by name, saying which Mac and what to allow; once allowed it passes, and an
/// address already reached in this run gets no further datagram.
#[test]
fn a_pending_permission_is_asked_early_and_refuses_the_compile_by_name() {
    let mut f = Fixture::on(lan_on_loopback());
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Blocked);
    ports.work.probe = probe.clone();
    let state = published(&mut f, &ports.work, "run-ln1");
    let addresses = member_addresses(&f, &ports.work, &state);
    assert!(!addresses.is_empty());
    assert!(ports.work.local_network(&state, false).is_ok());
    assert_eq!(probe.sent(), addresses, "one datagram per verified address");
    assert!(
        probe
            .from()
            .iter()
            .all(|from| *from == std::net::IpAddr::from([127, 0, 0, 1])),
        "sent from the run's selected local address: {:?}",
        probe.from()
    );
    let (code, message) = refusal(ports.work.local_network(&state, true));
    assert_eq!(code, PERMISSION_REQUIRED);
    assert!(
        message.contains("THIS Mac") && message.contains("Local Network"),
        "{message}"
    );
    // PHY-04: the evidence, not a certainty: this Mac's permission OR no route to that Mac.
    assert!(
        message.contains("no route") && message.contains(&addresses[0].ip().to_string()),
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
    let mut f = Fixture::on(lan_on_loopback());
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

/// Proves PHY-02 residual (lane PHY patch 3): the step-6 diagnostic probes only members still
/// current for this run. A member verified at compose and revoked before its failed dial is
/// diagnosed gets no datagram, and its address cannot decide the returned error: the original
/// failure passes through unnamed.
#[test]
fn a_member_revoked_after_compose_is_not_probed_by_the_step_6_diagnostic() {
    let mut f = Fixture::on(lan_on_loopback());
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Blocked);
    ports.work.probe = probe.clone();
    let state = run_state(&mut f, &ports);
    let mut env = Env { at: Instant::now() };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    assert!(!member_addresses(&f, &ports.work, &state).is_empty());
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
    let named = ports
        .work
        .named_dial_failure("control_call_failed:control_connect_timeout");
    assert!(
        named.is_none(),
        "a revoked member decided the error: {named:?}"
    );
    assert!(
        probe.sent().is_empty(),
        "the revoked member's address was probed: {:?}",
        probe.sent()
    );
    let _ = ports.work.teardown();
}

/// PHY2-01 (lane PHY patch 3): a reading of this Mac's interfaces that answers `first` to the run's
/// first reading (its probe passes) and `then` to every later one (compose), with the count.
fn changing(
    first: api::lan_interface::Selection,
    then: api::lan_interface::Selection,
) -> (WorkNetwork, Arc<AtomicUsize>) {
    let reads = Arc::new(AtomicUsize::new(0));
    let counted = reads.clone();
    let owner = WorkNetwork::Read(
        Arc::new(move || match counted.fetch_add(1, Ordering::SeqCst) {
            0 => first.clone(),
            _ => then.clone(),
        }),
        runtime::iroh_host::LanPolicy::with_explicit(vec![LOOPBACK]).unwrap(),
    );
    (owner, reads)
}
const LOOPBACK: std::net::IpAddr = std::net::IpAddr::V4(std::net::Ipv4Addr::LOCALHOST);
/// An address this Mac's interfaces no longer carry (the probe's LAN A that vanished before compose).
const VANISHED: std::net::IpAddr = std::net::IpAddr::V4(std::net::Ipv4Addr::new(10, 0, 0, 1));
/// [`published`] with the worker walls a composed run carries.
fn published_for_work(f: &mut Fixture, work: &Work, run: &str) -> RunState {
    let mut state = published(f, work, run);
    let wall = BudgetId::MemberChosenWall
        .span(Entries::ZERO)
        .unwrap()
        .millis()
        / 1000;
    state.worker_wall_limits = state
        .participants
        .iter()
        .cloned()
        .map(|n| (n, wall))
        .collect();
    state
}
/// Every datagram's (from, to) and whether the run had started a work endpoint or composed its
/// workflow when it left (a datagram compose sends must leave before either).
fn watched(probe: &Scripted, resident: &Arc<Mutex<Resident>>) -> Arc<Watch> {
    let watch = Arc::new(Watch {
        resident: resident.clone(),
        started: Mutex::default(),
    });
    *probe.watch.lock().unwrap() = Some(watch.clone());
    watch
}
struct Watch {
    resident: Arc<Mutex<Resident>>,
    started: Mutex<Vec<bool>>,
}
impl Watch {
    fn record(&self) {
        let started = match self.resident.try_lock() {
            Ok(resident) => resident.local.is_some() || resident.work.is_some(),
            Err(_) => true,
        };
        self.started.lock().unwrap().push(started);
    }
}
/// The run's preflight record is empty: no run, nothing reached, no held resolution.
fn preflight_reset(work: &Work) -> bool {
    let seen = lock(&work.local_network);
    seen.run.is_empty() && seen.reached.is_empty() && seen.network.is_none()
}

/// Proves PHY-03 (lane PHY patch 2): a run whose reading of this Mac's interfaces finds no single
/// private network (`local_only:*`) has no admitted LAN path: its probe passes send no datagram and
/// are not refused for Local Network; its compose, reading the interfaces itself (patch 3,
/// PHY2-01), refuses it by name as at the base. (Replaces the patch-2 test that required ONE
/// reading across the probe and the compose.)
#[test]
fn a_run_whose_network_resolves_local_only_sends_no_probe() {
    use api::lan_interface::{AMBIGUOUS_PRIVATE_INTERFACES, Selection};
    let (owner, reads) = changing(
        Selection::LocalOnly(AMBIGUOUS_PRIVATE_INTERFACES),
        Selection::LocalOnly(AMBIGUOUS_PRIVATE_INTERFACES),
    );
    let mut f = Fixture::on_each(owner, lan_on_loopback());
    let ports = PackagedChainPorts::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Blocked);
    let mut work = ports.work.clone();
    work.probe = probe.clone();
    let state = published(&mut f, &work, "run-lo1");
    assert!(!member_addresses(&f, &work, &state).is_empty());
    assert!(work.local_network(&state, false).is_ok());
    assert!(work.local_network(&state, true).is_ok());
    assert!(probe.sent().is_empty(), "{:?}", probe.sent());
    assert_eq!(
        reads.load(Ordering::SeqCst),
        1,
        "the probe passes share one reading"
    );
    let mut env = Env { at: Instant::now() };
    let (code, _) = refusal(work.compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env));
    assert_eq!(code, "owner_work_network_unavailable");
    assert_eq!(
        reads.load(Ordering::SeqCst),
        2,
        "compose read the interfaces itself"
    );
    assert!(probe.sent().is_empty(), "{:?}", probe.sent());
}

/// Proves PHY2-01 (lane PHY patch 3), local-only -> LAN: the probe passes found no single private
/// network (nothing sent); by compose this Mac has one. Compose reads the interfaces afresh, binds
/// that LAN address (no stale `owner_work_network_unavailable`), and, its resolution differing from
/// the probe's, asks with one datagram from that address before it starts any work endpoint.
#[test]
fn compose_after_a_local_only_probe_uses_the_lan_it_now_finds() {
    use api::lan_interface::{AMBIGUOUS_PRIVATE_INTERFACES, Selection};
    let (owner, reads) = changing(
        Selection::LocalOnly(AMBIGUOUS_PRIVATE_INTERFACES),
        Selection::Lan(LOOPBACK),
    );
    let mut f = Fixture::on_each(owner, lan_on_loopback());
    let ports = PackagedChainPorts::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Sent);
    let mut work = ports.work.clone();
    work.probe = probe.clone();
    let watch = watched(&probe, &work.resident);
    let state = published_for_work(&mut f, &work, "run-l2l");
    let member = member_addresses(&f, &work, &state);
    assert!(work.local_network(&state, false).is_ok());
    assert!(work.local_network(&state, true).is_ok());
    assert!(probe.sent().is_empty(), "{:?}", probe.sent());
    let mut env = Env { at: Instant::now() };
    let composed = work.compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env);
    assert!(composed.is_ok(), "{composed:?}");
    assert_eq!(
        reads.load(Ordering::SeqCst),
        2,
        "compose read the interfaces itself"
    );
    let bound = lock(&work.resident).network.clone();
    assert_eq!(
        bound.and_then(|n| n.0).map(|(ip, _)| ip),
        Some(LOOPBACK),
        "the run is bound to the LAN address compose found"
    );
    assert_eq!(probe.sent(), member, "one datagram per verified address");
    assert!(
        probe.from().iter().all(|from| *from == LOOPBACK),
        "{:?}",
        probe.from()
    );
    assert!(
        watch.started.lock().unwrap().iter().all(|s| !s),
        "a datagram left after a work endpoint started"
    );
    let _ = work.teardown();
}

/// Proves PHY2-01, LAN -> local-only: the probe passes reached the joined Mac on this Mac's LAN; by
/// compose this Mac has no single private network. Compose reads the interfaces afresh and refuses
/// the run by name exactly as at the base, before any work endpoint and with no further datagram.
#[test]
fn compose_after_a_lan_probe_refuses_by_name_when_the_lan_is_gone() {
    use api::lan_interface::{AMBIGUOUS_PRIVATE_INTERFACES, Selection};
    let (owner, reads) = changing(
        Selection::Lan(LOOPBACK),
        Selection::LocalOnly(AMBIGUOUS_PRIVATE_INTERFACES),
    );
    let mut f = Fixture::on_each(owner, lan_on_loopback());
    let ports = PackagedChainPorts::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Sent);
    let mut work = ports.work.clone();
    work.probe = probe.clone();
    let state = published_for_work(&mut f, &work, "run-l2o");
    let member = member_addresses(&f, &work, &state);
    assert!(work.local_network(&state, true).is_ok());
    assert_eq!(probe.sent(), member);
    let mut env = Env { at: Instant::now() };
    let (code, message) =
        refusal(work.compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env));
    assert_eq!(code, "owner_work_network_unavailable", "{message}");
    assert!(message.contains(AMBIGUOUS_PRIVATE_INTERFACES), "{message}");
    assert_eq!(
        reads.load(Ordering::SeqCst),
        2,
        "compose read the interfaces itself"
    );
    assert_eq!(probe.sent(), member, "no datagram on a local-only compose");
    let resident = lock(&work.resident);
    assert!(resident.local.is_none() && resident.work.is_none() && resident.network.is_none());
}

/// Proves PHY2-01, LAN A -> LAN B: the probe passes reached the joined Mac from address A; by
/// compose A is gone and this Mac's network is B. Compose binds B, drops what A reached and asks
/// again from B before any work endpoint or dial: reached, the run composes on B; blocked, it is
/// refused by name (`local_network_permission_required`) with nothing started.
#[test]
fn compose_on_a_new_lan_address_binds_it_and_asks_again_from_it_first() {
    use api::lan_interface::Selection;
    for at_compose in [Answer::Sent, Answer::Blocked] {
        let (owner, reads) = changing(Selection::Lan(VANISHED), Selection::Lan(LOOPBACK));
        let mut f = Fixture::on_each(owner, lan_on_loopback());
        let ports = PackagedChainPorts::new(&f.host, None, None);
        let probe = Scripted::new(Answer::Sent);
        let mut work = ports.work.clone();
        work.probe = probe.clone();
        let watch = watched(&probe, &work.resident);
        let state = published_for_work(&mut f, &work, "run-a2b");
        let member = member_addresses(&f, &work, &state);
        assert!(work.local_network(&state, false).is_ok());
        assert!(work.local_network(&state, true).is_ok());
        assert_eq!(probe.sent(), member, "reached once from A");
        assert!(probe.from().iter().all(|from| *from == VANISHED));
        probe.answer(at_compose);
        let mut env = Env { at: Instant::now() };
        let composed = work.compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env);
        assert_eq!(
            reads.load(Ordering::SeqCst),
            2,
            "compose read the interfaces itself"
        );
        let again: Vec<_> = probe.sent.lock().unwrap()[member.len()..].to_vec();
        assert_eq!(
            again,
            member.iter().map(|to| (LOOPBACK, *to)).collect::<Vec<_>>(),
            "{at_compose:?}: asked again from B, to the same verified addresses"
        );
        assert!(
            watch.started.lock().unwrap().iter().all(|s| !s),
            "{at_compose:?}: a datagram left after a work endpoint started"
        );
        match at_compose {
            Answer::Sent => {
                assert!(composed.is_ok(), "{composed:?}");
                let bound = lock(&work.resident).network.clone();
                assert_eq!(bound.and_then(|n| n.0).map(|(ip, _)| ip), Some(LOOPBACK));
            }
            _ => {
                let (code, message) = refusal(composed);
                assert_eq!(code, PERMISSION_REQUIRED, "{message}");
                let resident = lock(&work.resident);
                assert!(resident.local.is_none() && resident.work.is_none());
            }
        }
        let _ = work.teardown();
    }
}

/// Proves PHY2-01: a changed resolution re-asks only this run's CURRENT verified members. The joined
/// Mac was reached from A, then revoked; by compose this Mac is on B. Compose reads afresh, sends
/// nothing (no verified target), and is not refused for Local Network.
#[test]
fn a_changed_resolution_with_no_verified_member_sends_nothing() {
    use api::lan_interface::Selection;
    let (owner, reads) = changing(Selection::Lan(VANISHED), Selection::Lan(LOOPBACK));
    let mut f = Fixture::on_each(owner, lan_on_loopback());
    let ports = PackagedChainPorts::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Blocked);
    let mut work = ports.work.clone();
    work.probe = probe.clone();
    let state = published_for_work(&mut f, &work, "run-nv");
    assert!(work.local_network(&state, false).is_ok());
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
    let mut env = Env { at: Instant::now() };
    let composed = work.compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env);
    assert_eq!(
        reads.load(Ordering::SeqCst),
        2,
        "compose read the interfaces itself"
    );
    assert_eq!(probe.sent(), first, "no datagram without a verified member");
    if let Err(StepError::Refused(e)) = &composed {
        assert_ne!(e.code, PERMISSION_REQUIRED, "{composed:?}");
    }
    let _ = work.teardown();
}

/// Proves PHY2-01 lifecycle: Stop (the chain's teardown) between the probe passes and compose
/// drops the run's preflight record, so the next pass reads the interfaces again and asks again.
#[test]
fn stop_between_the_probe_and_compose_drops_the_preflight_record() {
    use api::lan_interface::Selection;
    let (owner, reads) = changing(Selection::Lan(LOOPBACK), Selection::Lan(LOOPBACK));
    let mut f = Fixture::on_each(owner, lan_on_loopback());
    let mut chain = Composed::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Sent);
    chain.work.probe = probe.clone();
    let state = published(&mut f, &chain.work, "run-stop");
    assert!(chain.work.local_network(&state, false).is_ok());
    let first = probe.sent();
    assert!(!first.is_empty());
    assert!(!preflight_reset(&chain.work));
    let mut env = Env { at: Instant::now() };
    let _ = chain.teardown(false, &mut env);
    assert!(
        preflight_reset(&chain.work),
        "teardown kept the run's preflight record"
    );
    // Teardown withdrew the run's owner control; announce it again so its member verifies.
    let state = published(&mut f, &chain.work, "run-stop");
    assert!(chain.work.local_network(&state, false).is_ok());
    assert_eq!(
        reads.load(Ordering::SeqCst),
        2,
        "the pass after Stop read the interfaces again"
    );
    assert_eq!(
        probe.sent().len(),
        2 * first.len(),
        "asked again after Stop"
    );
}

/// Proves PHY2-01 lifecycle: a second run starts with an empty preflight record (`new_run`), reads
/// the interfaces itself for its probe pass, and its compose reads them again.
#[test]
fn a_second_run_reads_the_interfaces_afresh() {
    use api::lan_interface::Selection;
    let (owner, reads) = changing(Selection::Lan(LOOPBACK), Selection::Lan(LOOPBACK));
    let mut f = Fixture::on_each(owner, lan_on_loopback());
    let mut chain = Composed::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Sent);
    chain.work.probe = probe.clone();
    let earlier = published(&mut f, &chain.work, "run-one");
    assert!(chain.work.local_network(&earlier, false).is_ok());
    let first = probe.sent().len();
    assert!(first > 0);
    chain.new_run(
        "run-two",
        "dep-run-two",
        owner_run::Reason::Start,
        None,
        None,
    );
    assert!(
        preflight_reset(&chain.work),
        "new_run kept the earlier run's preflight record"
    );
    let second = published_for_work(&mut f, &chain.work, "run-two");
    // The joined Mac's first tick after the run changed closes its earlier run's work service
    // (`member_work_publication:member_work_run_changed`); its next one publishes for this run.
    f.agent.tick(&f.member);
    assert!(chain.work.local_network(&second, false).is_ok());
    assert_eq!(
        reads.load(Ordering::SeqCst),
        2,
        "the second run read the interfaces"
    );
    assert_eq!(
        probe.sent().len(),
        2 * first,
        "the second run asked its member"
    );
    let mut env = Env { at: Instant::now() };
    let composed = chain
        .work
        .compose(&second, f.plan(&second), Arc::new(NoCodec), &mut env);
    assert!(composed.is_ok(), "{composed:?}");
    assert_eq!(
        reads.load(Ordering::SeqCst),
        3,
        "its compose read them again"
    );
    assert_eq!(
        probe.sent().len(),
        2 * first,
        "an unchanged resolution is not asked again"
    );
    let _ = chain.work.teardown();
}

/// Proves PHY-03: a signed member address the run's transport would not dial is not probed: an
/// address of the other family than the run's bound address, and one outside an exact-host policy.
#[test]
fn a_signed_address_the_runs_admission_refuses_is_not_probed() {
    let loopback = std::net::IpAddr::from([127, 0, 0, 1]);
    let v6 = std::net::IpAddr::from(std::net::Ipv6Addr::LOCALHOST);
    let other = std::net::IpAddr::from([127, 0, 0, 2]);
    for (label, choice) in [
        (
            "wrong family",
            NetworkChoice(Some((
                v6,
                runtime::iroh_host::LanPolicy::with_explicit(vec![v6]).unwrap(),
            ))),
        ),
        (
            "outside the exact hosts",
            NetworkChoice(Some((
                loopback,
                runtime::iroh_host::LanPolicy::with_explicit(vec![other]).unwrap(),
            ))),
        ),
    ] {
        let mut f = Fixture::on_each(WorkNetwork::Fixed(choice.clone()), lan_on_loopback());
        let ports = PackagedChainPorts::new(&f.host, None, None);
        let probe = Scripted::new(Answer::Blocked);
        let mut work = ports.work.clone();
        work.probe = probe.clone();
        let state = published(&mut f, &work, "run-adm");
        let signed = member_addresses(&f, &work, &state);
        assert!(!signed.is_empty(), "{label}");
        assert!(
            signed.iter().all(|a| !choice.allows(*a)),
            "{label}: {signed:?}"
        );
        assert!(work.local_network(&state, true).is_ok(), "{label}");
        assert!(probe.sent().is_empty(), "{label}: {:?}", probe.sent());
    }
}

/// What the slices step's shared half answers in [`the_slices_step_names_an_unanswered_dial_through_the_composed_wrapper`].
enum Next {
    Refuse,
    Workflow,
    Stop,
    Raise,
}

/// Proves PHY-06 (lane PHY patch 2): the step-6 rename is protected through the production wrapper,
/// `Composed::run_step("slices")`, with a REAL failed workflow: the joined Mac's work service is
/// gone, so the workflow's dial gets no answer and the workflow fails with that open cause. The
/// returned refusal is `local_network_permission_required` when the datagram to the joined Mac is
/// blocked and `joined_mac_did_not_answer` (with its address) when it is reached; a refusal with no
/// failed workflow, Stop and an exception pass unchanged with no datagram; teardown still runs.
/// The seam stands for `Work::prepare` (the real `compose`) and the shared chain's slices step (the
/// real `SlicesPort::node_slice_plans`), never for the rename.
#[test]
fn the_slices_step_names_an_unanswered_dial_through_the_composed_wrapper() {
    let mut f = Fixture::on(lan_on_loopback());
    let mut chain = Composed::new(&f.host, None, None);
    let probe = Scripted::new(Answer::Blocked);
    chain.work.probe = probe.clone();
    let mut state = published(&mut f, &chain.work, "run-s6");
    let wall = BudgetId::MemberChosenWall
        .span(Entries::ZERO)
        .unwrap()
        .millis()
        / 1000;
    state.worker_wall_limits = state
        .participants
        .iter()
        .cloned()
        .map(|n| (n, wall))
        .collect();
    let member = member_addresses(&f, &chain.work, &state);
    let mut plan = Some(f.plan(&state));
    let composed = state.clone();
    let next = Arc::new(Mutex::new(Next::Refuse));
    let script = next.clone();
    chain.slices_seam = Some(SlicesSeam {
        prepare: Box::new(move |work, env| match plan.take() {
            Some(plan) => work.compose(&composed, plan, Arc::new(NoCodec), env),
            None => Ok(()),
        }),
        step: Box::new(move |work, _| match *script.lock().unwrap() {
            Next::Refuse => Err(refused("member_consent_required")),
            Next::Workflow => SlicesPort::node_slice_plans(&mut work.clone()).map(|_| ()),
            Next::Stop => Err(StepError::Stopped),
            Next::Raise => Err(StepError::Exception {
                class: "TestRaised".into(),
            }),
        }),
    });
    let mut env = Env { at: Instant::now() };
    // Another refusal with no failed workflow: unchanged, nothing probed.
    let (code, _) = refusal(chain.run_step("slices", &mut env));
    assert_eq!(code, "member_consent_required");
    assert!(probe.sent().is_empty(), "{:?}", probe.sent());
    // The joined Mac's work service is gone: the real workflow's dial gets no answer.
    f.network.close_work();
    *next.lock().unwrap() = Next::Workflow;
    let (code, message) = refusal(chain.run_step("slices", &mut env));
    let cause = lock(&chain.work.resident)
        .work
        .as_ref()
        .and_then(|work| work.failure_cause());
    assert!(
        cause
            .as_deref()
            .is_some_and(api::local_network::dial_unanswered),
        "the workflow failed on a dial without an answer: {cause:?}"
    );
    assert_eq!(code, PERMISSION_REQUIRED, "{message}");
    assert!(
        member.iter().all(|a| probe.sent().contains(a)),
        "the joined Mac's address was probed: {:?}",
        probe.sent()
    );
    // Reached: the joined Mac's silence, named with its address.
    probe.answer(Answer::Sent);
    let (code, message) = refusal(chain.run_step("slices", &mut env));
    assert_eq!(code, JOINED_MAC_SILENT, "{message}");
    assert!(message.contains(&member[0].ip().to_string()), "{message}");
    // Stop and an exception pass unchanged, with no datagram.
    let before = probe.sent().len();
    *next.lock().unwrap() = Next::Stop;
    let stopped = chain.run_step("slices", &mut env);
    assert!(matches!(stopped, Err(StepError::Stopped)), "{stopped:?}");
    *next.lock().unwrap() = Next::Raise;
    let raised = chain.run_step("slices", &mut env);
    assert!(
        matches!(&raised, Err(StepError::Exception { class }) if class == "TestRaised"),
        "{raised:?}"
    );
    assert_eq!(
        probe.sent().len(),
        before,
        "Stop and an exception probe nothing"
    );
    // Teardown still runs: the failed workflow leaves the run (closed, or held as debt).
    let closed = chain.teardown(true, &mut env);
    assert!(
        lock(&chain.work.resident).work.is_none(),
        "teardown did not take the workflow: {closed:?}"
    );
}
