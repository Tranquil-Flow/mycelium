// SPDX-License-Identifier: AGPL-3.0-only
//! Production pump, real authenticated SQLite commits, pinned model-free child.
use super::*;
#[test]
#[ignore = "requires pinned real model-free helper"]
fn replay_reopen_refuses_unresolved_journal_without_replacement_dispatch() {
    let f = Fixture::new();
    let mut pump = f.pump();
    f.cleanup();
    f.enqueue(1, "stop");
    fault(&f, "ack", true, "managed_control_response_timeout");
    assert_eq!(pump.step(), Ok(false));
    native_stopped(&f);
    pump.close().unwrap();
    drop(pump);
    let unresolved = state(&f);
    assert!(!unresolved["pending"].is_null());
    assert!(!unresolved["outbound"].is_null());
    let requests = f.host.requests.lock().unwrap().len();
    let failure = match f.try_pump() {
        Err(failure) => failure,
        Ok(_) => panic!("unresolved journal must not spawn a replacement child"),
    };
    assert_eq!(failure.code(), "control_restart_requires_reconciliation");
    assert!(!failure.owner_retained());
    assert_eq!(state(&f), unresolved);
    assert_eq!(f.host.requests.lock().unwrap().len(), requests);
}

#[test]
#[ignore = "requires pinned real model-free helper"]
fn replay_late_ack_authority_loss_retains_actual_native_output_and_unresolved_request() {
    for code in ["revoke_after_commit", "cancel_after_commit"] {
        let f = Fixture::new();
        let mut pump = f.pump();
        f.cleanup();
        f.enqueue(1, "stop");
        fault(&f, "ack", true, code);
        assert!(pump.step().is_err());
        native_stopped(&f);
        let old = state(&f);
        assert!(!old["outbound"].is_null());
        assert_eq!(server_state(&f), "completed");
        assert!(pump.step().is_err());
        assert_eq!(state(&f), old);
        assert_eq!(f.host.requests.lock().unwrap().len(), 2);
        pump.close().unwrap();
    }
}
#[test]
#[ignore = "requires pinned real model-free helper"]
fn replay_uncertain_native_output_is_acknowledged_not_reexecuted() {
    for code in [
        "managed_control_response_timeout",
        "managed_control_sequence_replayed",
    ] {
        let f = Fixture::new();
        let mut pump = f.pump();
        f.cleanup();
        f.enqueue(1, "stop");
        pump.process_control().cancel().unwrap(); // actual child exit, NOT a fake worker result
        fault(&f, "ack", false, code);
        assert_eq!(pump.step(), Ok(false));
        let s = state(&f);
        assert_eq!(s["pending"]["phase"], "reconciliation_required");
        assert!(s["pending"]["native_response"].is_null());
        assert!(s["pending"]["error_code"].is_string());
        if code == "managed_control_sequence_replayed" {
            assert_eq!(pump.step(), Err("control_reconciliation_required"));
            assert_eq!(f.host.requests.lock().unwrap().len(), 2);
        } else {
            assert_eq!(pump.step(), Ok(false));
            assert_eq!(server_state(&f), "reconciliation_required");
            let requests = f.host.requests.lock().unwrap();
            assert_eq!(requests.len(), 3);
            assert_eq!(requests[1], requests[2]);
            assert!(state(&f)["pending"].is_null());
        }
        pump.close().unwrap();
    }
}

#[test]
#[ignore = "requires pinned real model-free helper"]
fn replay_expired_committed_poll_hands_uncertainty_to_mailbox_without_native_dispatch() {
    let f = Fixture::new();
    let mut pump = f.pump();
    f.cleanup();
    f.enqueue(1, "stop");
    fault(&f, "poll", true, "managed_control_response_timeout");
    assert_eq!(pump.step(), Ok(false));
    assert_eq!(server_state(&f), "claimed");
    f.host.wall_advance.store(91, Ordering::Release);
    assert_eq!(pump.step(), Ok(false));
    assert_eq!(f.host.requests.lock().unwrap().len(), 1);
    assert!(state(&f)["outbound"].is_null());
    assert_eq!(pump.step(), Ok(false));
    assert_eq!(server_state(&f), "reconciliation_required");
    let s = state(&f);
    assert!(s["pending"].is_null());
    assert!(s["native_stop_receipt"].is_null());
    let requests = f.host.requests.lock().unwrap();
    assert_eq!(requests.len(), 3);
    let ack: Value = serde_json::from_slice(&requests[2]).unwrap();
    assert_eq!(ack["statement"]["disposition"], "reconciliation_required");
    assert!(ack["statement"]["native_response"].is_null());
    pump.close().unwrap();
}

fn state(f: &Fixture) -> Value {
    serde_json::from_slice(&fs::read(f.root.join("state.json")).unwrap()).unwrap()
}
fn fault(f: &Fixture, operation: &'static str, after_commit: bool, code: &'static str) {
    f.host
        .faults
        .lock()
        .unwrap()
        .push_back((operation, after_commit, code));
}
fn server_state(f: &Fixture) -> String {
    rusqlite::Connection::open(f.root.join("mailbox.sqlite"))
        .unwrap()
        .query_row(
            "SELECT state FROM managed_commands WHERE command_id='stop'",
            [],
            |r| r.get(0),
        )
        .unwrap()
}
fn native_stopped(f: &Fixture) {
    let s = state(f);
    assert_eq!(
        s["pending"]["native_response"]["result"]["state"],
        "STOPPED"
    );
    let pid = rustix::process::Pid::from_raw(s["process"]["pid"].as_i64().unwrap() as i32).unwrap();
    assert_eq!(
        rustix::process::test_kill_process(pid),
        Err(rustix::io::Errno::SRCH)
    );
    // The real helper returns immediately after its first Stop. A duplicate call
    // cannot be hidden by a fake counter or a native deduplication collaborator.
}
#[test]
#[ignore = "requires pinned real model-free helper"]
fn replay_lost_poll_and_ack_after_real_commits_reuses_exact_bytes_native_once() {
    let mut f = Fixture::new();
    let server = f.host.clone();
    f.host.carrier = Some(Arc::new(
        api::managed_carrier::LocalMailboxCarrier::start(move |bytes| {
            let envelope = serde_json::from_slice(bytes).unwrap();
            // Actual authenticated SQLite COMMIT followed by real carrier connection
            // loss, not a fabricated successful receipt or a fake native executor.
            server
                .exchange(&envelope, server.monotonic(), &TargetFence::default())
                .map(|v| protocol::canonical::to_bytes(&v).unwrap())
                .map_err(runtime::managed_owner::OwnerError)
        })
        .unwrap(),
    ));
    let mut pump = f.pump();
    f.cleanup();
    f.enqueue(1, "stop");
    fault(&f, "poll", true, "managed_control_response_timeout");
    fault(&f, "ack", true, "managed_control_unreachable");
    assert_eq!(pump.step(), Ok(false), "lost committed poll is recoverable");
    assert_eq!(server_state(&f), "claimed");
    let poll = state(&f)["outbound"].clone();
    assert!(state(&f)["pending"].is_null());
    assert_eq!(pump.step(), Ok(false), "lost committed ACK is recoverable");
    native_stopped(&f);
    assert_eq!(server_state(&f), "completed");
    let pending = state(&f)["pending"].clone();
    let ack = state(&f)["outbound"].clone();
    assert_eq!(
        pump.step(),
        Ok(true),
        "ACK replay must not call the exited child"
    );
    let requests = f.host.requests.lock().unwrap();
    assert_eq!(requests.len(), 4);
    assert_eq!(requests[0], requests[1]);
    assert_eq!(requests[2], requests[3]);
    assert_eq!(requests[0], protocol::canonical::to_bytes(&poll).unwrap());
    assert_eq!(requests[2], protocol::canonical::to_bytes(&ack).unwrap());
    assert_eq!(
        ack["statement"]["native_response"],
        pending["native_response"]
    );
    assert_eq!(poll["statement"]["sequence"], 1);
    assert_eq!(ack["statement"]["sequence"], 2);
    assert!(state(&f)["pending"].is_null());
    assert!(state(&f)["outbound"].is_null());
    pump.close().unwrap();
}
#[test]
#[ignore = "requires pinned real model-free helper"]
fn replay_unverifiable_owner_reply_keeps_claim_and_exact_request() {
    let f = Fixture::new();
    let mut pump = f.pump();
    f.cleanup();
    f.enqueue(1, "stop");
    fault(&f, "poll", true, "corrupt_response_signature");
    assert_eq!(pump.step(), Ok(false));
    assert_eq!(server_state(&f), "claimed");
    assert!(state(&f)["pending"].is_null());
    assert_eq!(pump.step(), Ok(true));
    let r = f.host.requests.lock().unwrap();
    assert_eq!(r.len(), 3);
    assert_eq!(r[0], r[1]);
    pump.close().unwrap();
}
#[test]
#[ignore = "requires pinned real model-free helper"]
fn replay_ack_unusable_codes_requeue_only_envelope_not_native_result() {
    for code in [
        "managed_control_member_stale",
        "managed_control_sequence_replayed",
        "managed_control_nonce_replayed",
        "managed_control_conflicting_retry",
        "managed_control_prepared_request_invalid",
        "managed_control_message_expired",
    ] {
        let f = Fixture::new();
        let mut pump = f.pump();
        f.cleanup();
        f.enqueue(1, "stop");
        fault(&f, "ack", false, code);
        assert_eq!(pump.step(), Ok(false), "{code}");
        native_stopped(&f);
        let pending = state(&f)["pending"].clone();
        assert!(state(&f)["outbound"].is_null());
        assert_eq!(
            pump.step(),
            Ok(true),
            "requeue {code} must not dispatch Stop twice"
        );
        let requests = f.host.requests.lock().unwrap();
        assert_eq!(requests.len(), 3);
        let a: Value = serde_json::from_slice(&requests[1]).unwrap();
        let b: Value = serde_json::from_slice(&requests[2]).unwrap();
        assert_eq!(a["statement"]["sequence"], 2);
        assert_eq!(b["statement"]["sequence"], 3);
        assert_ne!(a["statement"]["nonce"], b["statement"]["nonce"]);
        assert_eq!(
            b["statement"]["native_response"],
            pending["native_response"]
        );
        assert_eq!(server_state(&f), "completed");
        pump.close().unwrap();
    }
}
#[test]
#[ignore = "requires pinned real model-free helper"]
fn replay_signed_ack_expiry_requeues_before_exchange_without_renewing_old_lease() {
    let f = Fixture::new();
    let mut pump = f.pump();
    f.cleanup();
    f.enqueue(1, "stop");
    fault(&f, "ack", false, "managed_control_response_timeout");
    assert_eq!(pump.step(), Ok(false));
    native_stopped(&f);
    let pending = state(&f)["pending"].clone();
    let old = state(&f)["outbound"].clone();
    f.host.wall_advance.store(91, Ordering::Release);
    assert_eq!(pump.step(), Ok(false));
    assert_eq!(
        f.host.requests.lock().unwrap().len(),
        2,
        "expired bytes never sent"
    );
    assert!(state(&f)["outbound"].is_null());
    assert_eq!(state(&f)["pending"], pending);
    assert_eq!(pump.step(), Ok(true));
    let r = f.host.requests.lock().unwrap();
    let new: Value = serde_json::from_slice(&r[2]).unwrap();
    assert_eq!(
        new["statement"]["native_response"],
        old["statement"]["native_response"]
    );
    assert_ne!(
        new["statement"]["request_id"],
        old["statement"]["request_id"]
    );
    assert_eq!(server_state(&f), "completed");
    pump.close().unwrap();
}
#[test]
#[ignore = "requires pinned real model-free helper"]
fn replay_retryable_codes_keep_outbound_but_unknown_and_auth_errors_are_fatal() {
    for code in [
        "managed_control_unreachable",
        "managed_control_response_timeout",
        "managed_control_internal_error",
        "managed_control_response_invalid",
        "managed_control_response_signature_invalid",
    ] {
        let f = Fixture::new();
        let mut pump = f.pump();
        f.cleanup();
        fault(&f, "poll", false, code);
        assert_eq!(pump.step(), Ok(false), "{code}");
        let old = state(&f)["outbound"].clone();
        assert!(!old.is_null());
        assert_eq!(pump.step(), Ok(false));
        let r = f.host.requests.lock().unwrap();
        assert_eq!(r[0], r[1]);
        assert!(state(&f)["outbound"].is_null());
        pump.close().unwrap();
    }
    for code in [
        "managed_control_signature_invalid",
        "managed_control_key_pin_mismatch",
        "managed_control_run_binding_mismatch",
        "managed_control_command_interrupted",
        "unknown_failure",
    ] {
        let f = Fixture::new();
        let mut pump = f.pump();
        f.cleanup();
        fault(&f, "poll", false, code);
        assert!(pump.step().is_err(), "fatal {code}");
        assert!(!state(&f)["outbound"].is_null());
        assert!(state(&f)["pending"].is_null());
        assert_eq!(f.host.requests.lock().unwrap().len(), 1);
        pump.close().unwrap();
    }
}
#[test]
#[ignore = "requires pinned real model-free helper"]
fn replay_local_revocation_and_cancellation_never_become_requeue() {
    for cancel in [false, true] {
        let f = Fixture::new();
        let mut pump = f.pump();
        f.cleanup();
        f.enqueue(1, "stop");
        if cancel {
            f.host.cancel_after_reply.store(true, Ordering::Release);
        } else {
            f.host.revoke_after_reply.store(true, Ordering::Release);
        }
        assert!(pump.step().is_err());
        let old = state(&f);
        assert!(old["pending"].is_null());
        assert!(!old["outbound"].is_null());
        assert!(pump.step().is_err());
        assert_eq!(state(&f), old);
        assert_eq!(f.host.requests.lock().unwrap().len(), 1);
        pump.close().unwrap();
    }
}

fn command_state(f: &Fixture, mailbox: &str, command: &str) -> String {
    rusqlite::Connection::open(f.root.join(mailbox))
        .unwrap()
        .query_row(
            "SELECT state FROM managed_commands WHERE command_id=?1",
            [command],
            |r| r.get(0),
        )
        .unwrap()
}
/// Physical rv41 shape (two Macs, 17 Nov): a run's poll is interrupted after the owner
/// claimed work, the run ends, and the pump closes with phase `closed`, `pending` null and
/// the exact signed poll still in `outbound`. v0.1.4 keeps that journal per RUN
/// (`target_app.py:256` roots the pump at `managed/<run_id>/pump`): the NEXT run's pump
/// never loads, replays or refuses on it, and only the signer-wide sequence carries over
/// (`control_sequence.py`). Reopening the SAME run is still refused, as v0.1.4's concurrent
/// pump refuses a serial outbound it cannot reconcile (`concurrent_pump.py:71-76`).
#[test]
#[ignore = "requires pinned real model-free helper"]
fn replay_next_run_starts_after_interrupted_run_without_replaying_its_outbound() {
    let old = Fixture::new();
    let mut pump = old.pump();
    old.cleanup();
    old.enqueue(1, "hello");
    // The owner commits the claim, then the reply is lost to a fatal interruption.
    fault(&old, "poll", true, "managed_control_command_interrupted");
    assert_eq!(pump.step(), Err("managed_control_command_interrupted"));
    pump.record_failure("managed_control_command_interrupted")
        .unwrap();
    pump.close().unwrap();
    drop(pump);
    let interrupted = state(&old);
    assert_eq!(interrupted["phase"], "closed");
    assert!(interrupted["pending"].is_null());
    assert!(!interrupted["outbound"].is_null());
    assert_eq!(
        interrupted["failure"],
        "managed_control_command_interrupted"
    );
    assert_eq!(command_state(&old, "mailbox.sqlite", "hello"), "claimed");
    let old_requests = old.host.requests.lock().unwrap().clone();
    assert_eq!(old_requests.len(), 1);

    // Same run: unresolved journal still refused, nothing sent or rewritten.
    let same = match old.try_pump() {
        Err(failure) => failure,
        Ok(_) => panic!("same-run unresolved outbound must not be resumed blindly"),
    };
    assert_eq!(same.code(), "control_restart_requires_reconciliation");
    assert!(!same.owner_retained());
    drop(same);
    assert_eq!(state(&old), interrupted);
    assert!(!old.root.join("stage").exists());

    // The next owner run (fresh run/deployment id) on the same Mac starts.
    let next = old.next_run("run-2", "deployment-2");
    let mut pump = next
        .try_pump()
        .unwrap_or_else(|failure| panic!("next run refused: {}", failure.code()));
    let started = state(&next);
    assert_eq!(started["phase"], "running");
    assert_eq!(started["binding"], next.scope);
    assert!(started["outbound"].is_null());
    assert!(started["pending"].is_null());
    assert!(started["failure"].is_null());
    assert_eq!(started["sequence"], interrupted["sequence"]);
    next.enqueue(1, "hello");
    assert_eq!(pump.step(), Ok(false));
    let sent = next.host.requests.lock().unwrap().clone();
    let stale = protocol::canonical::to_bytes(&interrupted["outbound"]).unwrap();
    assert!(!sent.contains(&stale), "earlier run's outbound replayed");
    let first: Value = serde_json::from_slice(&sent[0]).unwrap();
    assert_eq!(first["statement"]["run_id"], "run-2");
    assert_eq!(
        first["statement"]["sequence"].as_u64(),
        interrupted["sequence"].as_u64().map(|s| s + 1)
    );
    assert_eq!(
        command_state(&next, "mailbox-run-2.sqlite", "hello"),
        "completed"
    );
    // The earlier run's interrupted claim is never executed or acknowledged.
    assert_eq!(*old.host.requests.lock().unwrap(), old_requests);
    assert_eq!(command_state(&old, "mailbox.sqlite", "hello"), "claimed");
    next.enqueue(2, "stop");
    assert!(pump.step().unwrap());
    next.cleanup();
    pump.close().unwrap();
    assert_eq!(state(&next)["phase"], "closed");
    assert!(state(&next)["outbound"].is_null());
}
