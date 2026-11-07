// SPDX-License-Identifier: AGPL-3.0-only
//! Scripted signed receipt IO, not native hello/stop or a real server mailbox.
use super::*;
use runtime::managed_owner::session::{CallFence, SessionHost, SharedManagedOwner};
use std::sync::{Arc, mpsc};
#[path = "managed_owner_factory.rs"]
mod factory;
#[path = "managed_owner_factory_inputs.rs"]
mod factory_inputs;
#[path = "managed_owner_fixture_budgets.rs"]
mod fixture_budgets;
struct Gate {
    entered: mpsc::Sender<()>,
    release: Mutex<mpsc::Receiver<()>>,
}
impl Gate {
    fn release_pending(&self) -> bool {
        match self.release.lock().unwrap().try_recv() {
            Ok(()) | Err(mpsc::TryRecvError::Disconnected) => false,
            Err(mpsc::TryRecvError::Empty) => true,
        }
    }
}
#[test]
fn held_gate_distinguishes_pending_delivery_and_disconnection() {
    let (entered, _entered_rx) = mpsc::channel();
    let (tx, rx) = mpsc::channel();
    let gate = Gate {
        entered,
        release: Mutex::new(rx),
    };
    assert!(gate.release_pending(), "empty channel must keep IO held");
    tx.send(()).unwrap();
    assert!(!gate.release_pending(), "delivery releases IO");
    assert!(
        gate.release_pending(),
        "consumed release is not a persistent signal"
    );
    drop(tx);
    assert!(
        !gate.release_pending(),
        "sender teardown releases IO for scope join"
    );
}
struct SessionTestHost {
    inner: Arc<Mutex<Host>>,
    identity: Identity,
    hold: Option<Arc<Gate>>,
    hold_wait: bool,
}
impl transport::Clock for SessionTestHost {
    fn wall_seconds(&self) -> f64 {
        self.inner.lock().unwrap().wall_seconds()
    }
    fn monotonic(&self) -> MonoInstant {
        self.inner.lock().unwrap().monotonic()
    }
}
impl OwnerHost for SessionTestHost {
    fn signer(&self) -> &Identity {
        &self.identity
    }
    fn current_member(&mut self, node: &str) -> Result<Value, OwnerError> {
        self.inner.lock().unwrap().current_member(node)
    }
    fn nonce(&mut self) -> Result<String, OwnerError> {
        self.inner.lock().unwrap().nonce()
    }
    fn exchange(&mut self, p: &Value, d: MonoInstant) -> Result<Value, OwnerError> {
        self.inner.lock().unwrap().exchange(p, d)
    }
    fn wait(&mut self, a: Span, d: MonoInstant) -> Result<(), OwnerError> {
        self.inner.lock().unwrap().wait(a, d)
    }
}
impl SessionTestHost {
    fn held(&self, fence: &CallFence) -> Result<(), OwnerError> {
        if let Some(gate) = &self.hold {
            gate.entered.send(()).unwrap();
            // release_pending drops the receiver lock before the fence wait.
            while gate.release_pending() {
                fence.wait_for(Span::from_millis(5))?;
            }
        }
        Ok(())
    }
}
impl SessionHost for SessionTestHost {
    fn exchange_fenced(
        &mut self,
        p: &Value,
        d: MonoInstant,
        fence: &CallFence,
    ) -> Result<Value, OwnerError> {
        fence.check()?;
        let answer = self.exchange(p, d)?;
        if !self.hold_wait
            && p["operation"] == "result"
            && p["envelope"]["statement"]["native_command"]["command_id"] == "held"
        {
            self.held(fence)?;
        }
        fence.check()?;
        Ok(answer)
    }
    fn wait_fenced(
        &mut self,
        a: Span,
        d: MonoInstant,
        fence: &CallFence,
    ) -> Result<(), OwnerError> {
        fence.check()?;
        if self.hold_wait {
            self.held(fence)?;
        }
        self.wait(a, d)?;
        fence.check()
    }
}
fn host_factory(
    inner: Arc<Mutex<Host>>,
    hold: Option<Arc<Gate>>,
    hold_wait: bool,
) -> impl Fn() -> Result<SessionTestHost, OwnerError> {
    move || {
        Ok(SessionTestHost {
            inner: inner.clone(),
            identity: signer(),
            hold: hold.clone(),
            hold_wait,
        })
    }
}
#[test]
fn same_stop_id_is_persisted_before_io_retried_and_logical_only() {
    let r = Root::new();
    let inner = Arc::new(Mutex::new(Host::new(&r)));
    let expected = case("same_stop");
    let initial = expected["stop_id"]
        .as_str()
        .unwrap()
        .strip_prefix("managed-stop-")
        .unwrap();
    {
        let mut h = inner.lock().unwrap();
        h.nonce = u64::from_str_radix(initial, 16).unwrap() - 1;
        h.lose = true;
    }
    let owner = SharedManagedOwner::new(open(&r), host_factory(inner.clone(), None, false));
    let session = owner
        .session("member", "run", "deployment", Span::from_millis(30000))
        .unwrap();
    assert!(session.close().is_err());
    let id = session.stop_command_id().unwrap();
    assert_eq!(id, expected["stop_id"]);
    assert_eq!(session.returncode(), None);
    assert!(session.cleanup_error().is_some());
    inner.lock().unwrap().lose = false;
    session.close().unwrap();
    assert_eq!(session.stop_command_id().as_deref(), Some(id.as_str()));
    assert_eq!(session.returncode(), Some(0));
    assert_eq!(session.stderr(), b"");
    assert_eq!(disk(&r), expected["journal"]);
    {
        let h = inner.lock().unwrap();
        assert_eq!(h.calls[0], h.calls[1]);
    }
    assert_eq!(
        session.send("later", "health", json!({})).unwrap_err().0,
        "managed_session_closed"
    );
}
#[test]
fn pre_interrupt_has_no_io_and_original_absolute_deadline_is_kept() {
    let r = Root::new();
    let inner = Arc::new(Mutex::new(Host::new(&r)));
    let owner = SharedManagedOwner::new(open(&r), host_factory(inner.clone(), None, false));
    let session = owner
        .session("member", "run", "deployment", Span::from_millis(30000))
        .unwrap();
    session
        .interrupt_command("never", "cancelled_locally")
        .unwrap();
    assert_eq!(
        session.send("never", "hello", json!({})).unwrap_err().0,
        "cancelled_locally"
    );
    assert!(inner.lock().unwrap().calls.is_empty());
    assert_eq!(disk(&r)["write_intents"], 0);
    let now = MonoInstant::new(ClockDomain(1), 100000);
    assert_eq!(
        session
            .send_before("deadline", "hello", json!({}), now)
            .unwrap_err()
            .0,
        "command_deadline_exceeded"
    );
    assert!(inner.lock().unwrap().calls.is_empty());
    owner.close().unwrap();
    assert_eq!(
        session.send("closed", "hello", json!({})).unwrap_err().0,
        "managed_owner_closed"
    );
    drop(open(&r)); // close releases actual local lock, even while session handles survive.
}
fn captured(name: &str) -> Value {
    let f: Value = serde_json::from_str(include_str!(
        "../fixtures/managed-owner-session-oracle.json"
    ))
    .unwrap();
    f["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == name)
        .unwrap()
        .clone()
}
fn calibrate(inner: &Arc<Mutex<Host>>, expected: &Value) {
    let prepared =
        &expected["journal"]["commands"]["held"]["enqueue_request"]["envelope"]["statement"];
    inner.lock().unwrap().nonce =
        u64::from_str_radix(prepared["nonce"].as_str().unwrap(), 16).unwrap() - 1;
}
struct Release(mpsc::Sender<()>);
#[derive(Debug, PartialEq, Eq)]
enum ReleaseOutcome {
    Delivered,
    ReceiverAlreadyGone,
}
impl Release {
    fn signal(&self) -> ReleaseOutcome {
        match self.0.send(()) {
            Ok(()) => ReleaseOutcome::Delivered,
            // SendError<()> has exactly this meaning; no other error is swallowed.
            Err(mpsc::SendError(())) => ReleaseOutcome::ReceiverAlreadyGone,
        }
    }
}
impl Drop for Release {
    fn drop(&mut self) {
        self.signal(); // Both outcomes release our obligation; never panic on unwind.
    }
}
#[test]
fn release_outcome_distinguishes_delivery_from_receiver_absence() {
    let (tx, rx) = mpsc::channel();
    let guard = Release(tx);
    assert_eq!(guard.signal(), ReleaseOutcome::Delivered);
    assert_eq!(rx.try_recv(), Ok(()));
    drop(rx);
    assert_eq!(guard.signal(), ReleaseOutcome::ReceiverAlreadyGone);
}
#[test]
fn release_guard_delivers_or_accepts_closed_receiver_even_during_unwind() {
    let (tx, rx) = mpsc::channel();
    drop(Release(tx));
    assert_eq!(rx.try_recv(), Ok(()));
    assert_eq!(rx.try_recv(), Err(mpsc::TryRecvError::Disconnected));

    let (tx, rx) = mpsc::channel();
    drop(rx);
    drop(Release(tx)); // SendError<()> means only that the held receiver is gone.

    let (tx, rx) = mpsc::channel();
    let unwind = std::panic::catch_unwind(|| {
        let _release = Release(tx);
        panic!("controlled assertion unwind");
    });
    assert!(unwind.is_err());
    assert_eq!(
        rx.try_recv(),
        Ok(()),
        "unwind must release before scope joins"
    );
}
#[test]
fn held_result_interrupt_is_responsive_and_fences_publication() {
    let r = Root::new();
    let inner = Arc::new(Mutex::new(Host::new(&r)));
    let (tx, rx) = mpsc::channel();
    let (release, proceed) = mpsc::channel();
    let expected = captured("held_interrupt");
    calibrate(&inner, &expected);
    let gate = Arc::new(Gate {
        entered: tx,
        release: Mutex::new(proceed),
    });
    let owner = SharedManagedOwner::new(open(&r), host_factory(inner.clone(), Some(gate), false));
    let session = owner
        .session(
            "member",
            "run",
            "deployment",
            fixture_budgets::session_timeout(),
        )
        .unwrap();
    std::thread::scope(|scope| {
        let running = scope.spawn(|| session.send("held", "hello", json!({})));
        let guard = Release(release);
        rx.recv_timeout(fixture_budgets::barrier_wait()).unwrap();
        session
            .interrupt_command("held", "cancelled_locally")
            .unwrap();
        assert_eq!(running.join().unwrap().unwrap_err().0, "cancelled_locally");
        drop(guard);
    });
    assert_eq!(session.stop_response(), None);
    assert_eq!(session.returncode(), None);
    assert_eq!(disk(&r), expected["journal"]);
    assert_eq!(
        inner
            .lock()
            .unwrap()
            .calls
            .iter()
            .filter(|p| p["operation"] == "enqueue")
            .count(),
        1
    );
}
#[test]
fn pending_wait_releases_owner_so_another_session_can_progress() {
    let r = Root::new();
    let inner = Arc::new(Mutex::new(Host::new(&r)));
    inner.lock().unwrap().states.push_back("pending");
    let (tx, rx) = mpsc::channel();
    let (release, proceed) = mpsc::channel();
    let gate = Arc::new(Gate {
        entered: tx,
        release: Mutex::new(proceed),
    });
    let owner = SharedManagedOwner::new(open(&r), host_factory(inner.clone(), Some(gate), true));
    let first = owner
        .session(
            "member",
            "run",
            "deployment",
            fixture_budgets::session_timeout(),
        )
        .unwrap();
    let second = owner
        .session(
            "member",
            "run",
            "deployment",
            fixture_budgets::session_timeout(),
        )
        .unwrap();
    std::thread::scope(|scope| {
        let running = scope.spawn(|| first.send("held", "hello", json!({})));
        let guard = Release(release);
        rx.recv_timeout(fixture_budgets::barrier_wait()).unwrap();
        assert_eq!(
            second.send("second", "hello", json!({})).unwrap()["ok"],
            true
        );
        first
            .interrupt_command("held", "cancelled_locally")
            .unwrap();
        assert_eq!(running.join().unwrap().unwrap_err().0, "cancelled_locally");
        drop(guard);
    });
}

#[test]
fn stop_retires_held_local_waiter_then_persists_its_own_intent() {
    let r = Root::new();
    let inner = Arc::new(Mutex::new(Host::new(&r)));
    let expected = captured("stop_interrupt");
    calibrate(&inner, &expected);
    let (tx, rx) = mpsc::channel();
    let (release, proceed) = mpsc::channel();
    let gate = Arc::new(Gate {
        entered: tx,
        release: Mutex::new(proceed),
    });
    let owner = SharedManagedOwner::new(open(&r), host_factory(inner.clone(), Some(gate), false));
    let session = owner
        .session(
            "member",
            "run",
            "deployment",
            fixture_budgets::session_timeout(),
        )
        .unwrap();
    std::thread::scope(|scope| {
        let running = scope.spawn(|| session.send("held", "hello", json!({})));
        let guard = Release(release);
        rx.recv_timeout(fixture_budgets::barrier_wait()).unwrap();
        session.close().unwrap();
        assert_eq!(
            running.join().unwrap().unwrap_err().0,
            expected["answers"][0]["error"]
        );
        drop(guard);
    });
    assert_eq!(disk(&r), expected["journal"]);
    assert_eq!(session.returncode(), Some(0));
    let trace: Vec<Value> = inner
        .lock()
        .unwrap()
        .calls
        .iter()
        .map(|v| {
            json!([
                v["operation"],
                v["envelope"]["statement"]["native_command"]["command_id"],
                v["envelope"]["statement"]["sequence"]
            ])
        })
        .collect();
    assert_eq!(json!(trace), expected["calls"]);
}
#[test]
fn completion_linearizes_before_late_interrupt_and_owner_close_is_local() {
    let r = Root::new();
    let inner = Arc::new(Mutex::new(Host::new(&r)));
    let expected = captured("late_interrupt");
    calibrate(&inner, &expected);
    let owner = SharedManagedOwner::new(open(&r), host_factory(inner.clone(), None, false));
    let session = owner
        .session("member", "run", "deployment", Span::from_millis(30000))
        .unwrap();
    assert_eq!(
        session.send("held", "hello", json!({})).unwrap()["ok"],
        true
    );
    session
        .interrupt_command("held", "cancelled_locally")
        .unwrap();
    assert_eq!(
        session.send("held", "hello", json!({})).unwrap()["ok"],
        true
    );
    assert_eq!(disk(&r), expected["journal"]);
    let before = inner.lock().unwrap().calls.len();
    owner.close().unwrap();
    assert_eq!(inner.lock().unwrap().calls.len(), before);
    assert_eq!(session.returncode(), None);
}
#[test]
fn owner_close_wakes_held_io_without_remote_stop_or_exit_claim() {
    let r = Root::new();
    let inner = Arc::new(Mutex::new(Host::new(&r)));
    let (tx, rx) = mpsc::channel();
    let (release, proceed) = mpsc::channel();
    let gate = Arc::new(Gate {
        entered: tx,
        release: Mutex::new(proceed),
    });
    let owner = SharedManagedOwner::new(open(&r), host_factory(inner.clone(), Some(gate), false));
    let session = owner
        .session(
            "member",
            "run",
            "deployment",
            fixture_budgets::session_timeout(),
        )
        .unwrap();
    std::thread::scope(|scope| {
        let running = scope.spawn(|| session.send("held", "hello", json!({})));
        let guard = Release(release);
        rx.recv_timeout(fixture_budgets::barrier_wait()).unwrap();
        owner.close().unwrap();
        assert_eq!(
            running.join().unwrap().unwrap_err().0,
            "managed_owner_closed"
        );
        drop(guard);
    });
    assert_eq!(session.returncode(), None);
    assert!(
        inner
            .lock()
            .unwrap()
            .calls
            .iter()
            .all(|c| c["envelope"]["statement"]["native_command"]["command"] != "stop")
    );
    drop(open(&r));
}

#[test]
fn binding_adopted_before_begin_error_cannot_roll_back_on_retry() {
    binding_error_case("begin");
}
#[test]
fn binding_adopted_before_poll_error_cannot_roll_back_on_retry() {
    binding_error_case("poll");
}
fn session_status<T>(result: runtime::managed_owner::session::SessionResult<T>) -> Value {
    match result {
        Ok(_) => json!({"ok": true}),
        Err(error) => json!({"error": error.0}),
    }
}
fn binding_error_case(seam: &str) {
    let fixture: Value = serde_json::from_str(include_str!(
        "../fixtures/managed-owner-binding-error-oracle.json"
    ))
    .unwrap();
    let expected = fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["name"] == seam)
        .unwrap();
    let r = Root::new();
    let inner = Arc::new(Mutex::new(Host::new(&r)));
    let owner = SharedManagedOwner::new(open(&r), host_factory(inner.clone(), None, false));
    let session = owner
        .session("member", "run", "deployment", Span::from_millis(30000))
        .unwrap();
    {
        let mut h = inner.lock().unwrap();
        if seam == "begin" {
            h.member["generation"] = json!(2);
            h.member["incarnation"] = json!("inc-2");
            h.lose = true;
        } else {
            h.advance_after = Some(("enqueue", true));
            h.fail_result = true;
        }
    }
    let first = session_status(session.send("hello-1", "hello", json!({})));
    let before = {
        let mut h = inner.lock().unwrap();
        h.member["generation"] = json!(1);
        h.member["incarnation"] = json!("inc-1");
        h.lose = false;
        h.fail_result = false;
        h.advance_after = None;
        h.calls.len()
    };
    let second = session_status(session.send("after-error", "hello", json!({})));
    // Collect before asserting: a successful stale retry must expose its extra IO,
    // rather than panic at unwrap_err before the no-IO assertion can be reached.
    let (calls, second_io_count) = {
        let h = inner.lock().unwrap();
        (
            h.calls
                .iter()
                .map(|p| p["operation"].clone())
                .collect::<Vec<_>>(),
            h.calls.len() - before,
        )
    };
    assert_eq!(
        json!({"first":first,"second":second,"calls":calls,"second_io_count":second_io_count}),
        json!({"first":expected["first"],"second":expected["second"],"calls":expected["calls"],"second_io_count":expected["second_io_count"]}),
        "{seam}: adopted binding must survive the error; older retry must refuse before IO"
    );
}
