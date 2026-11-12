// SPDX-License-Identifier: AGPL-3.0-only
//! Explicit legacy-callback limit and exact retained Work/helper cleanup ownership.
use super::*;
use tokenizer_helper::HelperFixture;
struct Held {
    entered: mpsc::Sender<()>,
    release: Mutex<mpsc::Receiver<()>>,
}
impl Codec for Held {
    fn encode(&self, _: &str) -> Result<Vec<u32>, PublicError> {
        Ok((0..4).collect())
    }
    fn decode(&self, token: u32) -> Result<String, PublicError> {
        self.entered.send(()).unwrap();
        self.release
            .lock()
            .unwrap()
            .recv_timeout(span(B::WorkerStartup) * 2)
            .unwrap();
        Ok(format!("<{token}>"))
    }
}
#[test]
fn ep2h_legacy_held_callback_retains_same_owner_and_never_renews_stop() {
    let (entered, ready) = mpsc::channel();
    let (release, gate) = mpsc::channel();
    let codec = Arc::new(Held {
        entered,
        release: Mutex::new(gate),
    });
    let mut swarm = swarm(2);
    let plan = swarm.serving_plan();
    let (mut work, members) = swarm.serving_with_codec(plan, stage_scripts(), codec);
    activate(&mut work);
    let gateway = work.gateway().unwrap();
    let submit = std::thread::spawn(move || post(&gateway));
    ready.recv_timeout(span(B::WorkerStartup)).unwrap();
    let repeat = work.stopper();
    let began = Instant::now();
    let allowance = stop_allowance(60, 2);
    let (done, thread) = stop(work);
    // Synchronize repeated Stop with actual healthy sibling release, not a sleep.
    let sibling_end = Instant::now() + span(B::RouterClose);
    while members
        .iter()
        .any(|m| m.0.lock().unwrap().discard_at.is_none())
        && Instant::now() < sibling_end
    {
        std::thread::yield_now();
    }
    repeat();
    let result = done.recv_timeout(allowance + span(B::OwnerPoll));
    let elapsed = began.elapsed();
    let siblings = members
        .iter()
        .all(|m| m.0.lock().unwrap().discard_at.is_some());
    // Release even a broken implementation before assertions, retaining all joins.
    if result.is_err() {
        release.send(()).unwrap();
    }
    let mut work = thread.join().unwrap();
    let pending_before_release = work.cleanup_pending();
    let replacement_blocked = work.take_members().is_empty();
    let again = Instant::now();
    let retry = work.teardown();
    let retry_elapsed = again.elapsed();
    if result.is_ok() {
        release.send(()).unwrap();
    }
    if let Err(e) = submit.join() {
        eprintln!("legacy submit closed: {e:?}");
    }
    let end = Instant::now() + span(B::WorkerStartup);
    while work.cleanup_pending() && Instant::now() < end {
        match work.teardown() {
            Ok(()) => break,
            Err(e) => assert_eq!(e, "owner_target_cleanup_unproven"),
        }
        std::thread::yield_now();
    }
    work.teardown().unwrap();
    let returned = work.take_members();
    eprintln!(
        "EP2H_LEGACY whole_stop={elapsed:?} allowance={allowance:?} repeat={retry_elapsed:?} pending={pending_before_release} blocked={replacement_blocked} siblings={siblings} returned={}",
        returned.len()
    );
    assert!(elapsed <= allowance + span(B::OwnerPoll));
    let (answer, state) = result.unwrap();
    assert_eq!(answer.unwrap_err(), "owner_target_cleanup_unproven");
    assert_eq!(state["cleanup_error"], "owner_target_cleanup_unproven");
    assert!(pending_before_release && replacement_blocked && siblings);
    assert_eq!(retry.unwrap_err(), "owner_target_cleanup_unproven");
    assert!(
        retry_elapsed < span(B::OwnerPoll),
        "same expired first end, not another allowance"
    );
    assert!(!work.cleanup_pending());
    assert_eq!(
        returned.len(),
        2,
        "same member links returned only after same worker joined"
    );
}
#[test]
fn ep2h_native_deadline_retry_never_signals_an_unrelated_helper() {
    let mut held = HelperFixture::new("read");
    let other = HelperFixture::new("normal");
    assert_eq!(other.codec.encode("control").unwrap(), [0, 1, 2, 3]);
    let other_pid = other.pid();
    let codec = held.codec.clone();
    let end = Instant::now() + span(B::RouterClose);
    let call =
        std::thread::spawn(move || codec.decode_until(11, &api::m16_route::CodecLimit::new(end)));
    assert!(held.entered());
    let pid = held.pid();
    let result = call.join().unwrap();
    let code = result.unwrap_err().code;
    let retained = held.codec.owned_pid().unwrap();
    assert!(
        retained.is_none() || retained == Some(pid),
        "only the exact Child may survive an expired reap"
    );
    let retry_end = Instant::now() + span(B::RouterClose);
    while held.codec.owned_pid().unwrap().is_some() && Instant::now() < retry_end {
        match held.codec.close_until(end) {
            Ok(()) => break,
            Err(e) => assert_eq!(e.code, "tokenizer_exit_unproven"),
        }
        std::thread::yield_now();
    }
    held.codec.close_until(end).unwrap();
    assert_eq!(held.codec.owned_pid().unwrap(), None);
    assert!(matches!(
        code,
        "tokenizer_deadline" | "tokenizer_exit_unproven"
    ));
    held.release();
    // No-child cleanup must be idempotent; wrong helper stays alive and functional.
    held.codec.close_until(end).unwrap();
    assert_eq!(other.codec.decode(11).unwrap(), "<11>");
    assert_eq!(other.codec.owned_pid().unwrap(), Some(other_pid));
    other.codec.close().unwrap();
    eprintln!(
        "EP2H_NATIVE_DEADLINE code={code} held_pid={pid} retained={retained:?} unrelated_pid={other_pid} isolated=true"
    );
}
