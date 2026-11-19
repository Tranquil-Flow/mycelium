// SPDX-License-Identifier: AGPL-3.0-only
//! Round 2: result provenance, not new owner admission. All calls use the real signed carrier.
use super::*;

impl Member {
    fn call(&self, secret: &iroh::SecretKey, request: Value) -> Value {
        member_remote::with_owner(secret, |owner, client, deadline| {
            let command = owner
                .command(&self.address.id, request, app_host::now())
                .unwrap();
            let signed = member_remote::raw(
                client,
                &self.address,
                &serde_json::to_vec(&command).unwrap(),
                deadline(),
            )
            .unwrap();
            owner
                .open_reply(&command, &signed, Some(&self.enrolled))
                .unwrap()
        })
    }
    fn poll(&self, secret: &iroh::SecretKey, id: u64) -> Value {
        self.call(secret, json!({"command":"result","operation_id":id}))
    }
}

#[derive(Clone, Copy, Debug)]
enum Switch {
    Replacement,
    ReturnToA,
    Run,
    Deployment,
    Cleared,
    OwnerProcess,
    Refresh,
}
fn change(
    exchange: &RunExchange,
    now: f64,
    pins: &Pins,
    a: &iroh::SecretKey,
    kind: Switch,
) -> iroh::SecretKey {
    let b = iroh::SecretKey::from_bytes(&[86; 32]);
    match kind {
        Switch::Replacement => {
            repin(exchange, now, pins, &b);
            return b;
        }
        Switch::ReturnToA => repin(exchange, now, pins, &b),
        Switch::Run | Switch::Deployment => {
            exchange
                .announce(Announce {
                    phase: "collect".into(),
                    run_id: Some(
                        if matches!(kind, Switch::Run) {
                            "run-2"
                        } else {
                            "run-1"
                        }
                        .into(),
                    ),
                    deployment_id: Some(
                        if matches!(kind, Switch::Deployment) {
                            "dep-2"
                        } else {
                            "dep-1"
                        }
                        .into(),
                    ),
                    ..Default::default()
                })
                .unwrap();
        }
        Switch::Cleared => {
            exchange.set_owner_control(None);
            let client = member_client(now);
            let (response, id) = member_read(exchange, &client, now);
            assert_eq!(
                pins.repin(&VerifiedAnnouncement::from_response(&client, &response, &id).unwrap())
                    .unwrap_err()
                    .0,
                "owner_pin_missing"
            );
            assert_eq!(pins.current(), None);
        }
        Switch::OwnerProcess => {
            // A fresh real exchange supplies a new signed owner_incarnation, same run and key.
            let (replacement, _, _) = owner_exchange(&oracle());
            repin(&replacement, now, pins, a);
            return a.clone();
        }
        Switch::Refresh => {}
    }
    repin(exchange, now, pins, a);
    a.clone()
}

fn boundary(kind: Switch, late: bool) {
    let (exchange, _, now) = owner_exchange(&oracle());
    let a = iroh::SecretKey::from_bytes(&[85; 32]);
    let pins = Pins::new();
    repin(&exchange, now, &pins, &a);
    let mut member = Member::start(pins.clone());
    assert_eq!(
        member.call(&a, json!({"command":"discard","operation_id":1})),
        json!({"ok":true,"result":{"accepted":true,"operation_id":1}})
    );
    // The real admission queue is the barrier: completion cannot precede this receive,
    // and the test owns the operation until after the synchronous verified repin below.
    let operation = member.queue.recv_timeout(wait()).unwrap().unwrap();
    assert_eq!(member.dispatcher.active_id(), Some(1));
    let held = if late {
        Some(operation)
    } else {
        member.finish_discard(operation);
        None
    };
    let current = change(&exchange, now, &pins, &a, kind);
    if let Some(operation) = held {
        assert_eq!(member.poll(&current, 1)["result"]["finished"], false);
        member.finish_discard(operation);
    }
    // No earlier terminal read has consumed the old result.
    let result = member.poll(&current, 1);
    // Repinning must still admit NEW valid work; no forced Stop, no auth-policy change.
    assert_eq!(
        member.call(&current, json!({"command":"discard","operation_id":2}))["result"]["accepted"],
        true
    );
    let operation = member.queue.recv_timeout(wait()).unwrap().unwrap();
    assert_eq!(operation.id, Some(2));
    let expected = member.finish_discard(operation);
    assert_eq!(member.poll(&current, 2)["result"]["reply"], expected);
    assert_eq!(member.executions, 2);
    assert!(matches!(
        member.queue.try_recv(),
        Err(mpsc::TryRecvError::Empty)
    ));
    drop(member); // join/close before the decisive assertion, including on RED
    assert_eq!(
        result,
        json!({"ok":true,"result":{"operation_id":1,"finished":false}}),
        "previous authority result escaped: {kind:?}, late={late}"
    );
}

#[test]
fn unread_completion_is_not_returned_to_a_replacement_authority() {
    boundary(Switch::Replacement, false);
}
#[test]
fn completion_published_after_repin_keeps_its_admission_authority() {
    boundary(Switch::Replacement, true);
}
#[test]
fn returning_to_a_does_not_revive_an_unread_a_result() {
    boundary(Switch::ReturnToA, false);
}
#[test]
fn late_completion_does_not_revive_after_a_b_a() {
    boundary(Switch::ReturnToA, true);
}
#[test]
fn same_endpoint_changed_signed_run_cannot_read_previous_result() {
    boundary(Switch::Run, false);
}
#[test]
fn same_endpoint_changed_signed_deployment_cannot_read_previous_result() {
    boundary(Switch::Deployment, false);
}
#[test]
fn clearing_then_restoring_pin_does_not_revive_previous_result() {
    boundary(Switch::Cleared, true);
}
#[test]
fn new_signed_owner_process_cannot_read_previous_result() {
    boundary(Switch::OwnerProcess, false);
}

fn refreshed_retry(consumed: bool) {
    let (exchange, _, now) = owner_exchange(&oracle());
    let a = iroh::SecretKey::from_bytes(&[87; 32]);
    let pins = Pins::new();
    repin(&exchange, now, &pins, &a);
    let mut member = Member::start(pins.clone());
    assert_eq!(
        member.call(&a, json!({"command":"discard","operation_id":1}))["result"]["accepted"],
        true
    );
    let expected = member.complete_discard();
    if consumed {
        assert_eq!(member.poll(&a, 1)["result"]["reply"], expected);
    }
    change(&exchange, now, &pins, &a, Switch::Refresh);
    let result = member.poll(&a, 1);
    member.assert_once();
    drop(member);
    assert_eq!(
        result,
        json!({"ok":true,"result":{"operation_id":1,"finished":true,"reply":expected}}),
        "unchanged verified refresh lost a valid result; previously read={consumed}"
    );
}
#[test]
fn unchanged_refresh_preserves_unread_result_control() {
    refreshed_retry(false);
}
#[test]
fn unchanged_refresh_preserves_same_authority_retry() {
    refreshed_retry(true);
}

#[test]
fn replacement_service_has_no_previous_result_control() {
    let (exchange, _, now) = owner_exchange(&oracle());
    let a = iroh::SecretKey::from_bytes(&[88; 32]);
    let pins = Pins::new();
    repin(&exchange, now, &pins, &a);
    let mut old = Member::start(pins.clone());
    assert_eq!(
        old.call(&a, json!({"command":"discard","operation_id":1}))["result"]["accepted"],
        true
    );
    old.complete_discard();
    old.assert_once();
    drop(old);
    let new = Member::start(pins);
    let result = new.poll(&a, 1);
    drop(new);
    assert_eq!(
        result,
        json!({"ok":true,"result":{"operation_id":1,"finished":false}})
    );
}
