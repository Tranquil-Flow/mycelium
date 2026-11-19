// SPDX-License-Identifier: AGPL-3.0-only
//! Private implementation invariants supplement the pre-fix signed-carrier REDs.
use super::*;

struct Signed {
    owner: member_control::Owner,
    member: iroh::EndpointId,
    service: ResultScope,
}
impl Signed {
    fn new() -> Self {
        Self {
            owner: member_control::Owner::new(&iroh::SecretKey::from_bytes(&[91; 32])).unwrap(),
            member: iroh::SecretKey::from_bytes(&[92; 32]).public(),
            service: ResultScope::new(),
        }
    }
    fn verify(&self, request: Value) -> (Value, VerifiedCommand) {
        let now = app_host::now();
        let envelope = self.owner.command(&self.member, request, now).unwrap();
        let verified = OwnerTrust::Operator(self.owner.endpoint())
            .snapshot(&self.service)
            .unwrap()
            .verify(self.owner.endpoint(), &envelope, &self.member, 0, now)
            .unwrap();
        (envelope, verified)
    }
    fn call(&self, f: &Fixture, request: Value) -> Value {
        handle_verified(&f.dispatcher, &f.status, self.verify(request).1)
    }
}

#[test]
fn a_verified_poll_cannot_borrow_a_different_operations_scope_when_signed() {
    let mut f = Fixture::new();
    let old = Signed::new();
    let (envelope, held_poll) = old.verify(json!({"command":"result","operation_id":2}));
    // Deterministically hold a verified request across a switch. Even the same endpoint
    // cannot label this old request with the new operation's scope after verification.
    old.service.authority.store(false, Ordering::SeqCst);
    let new = Signed::new();
    assert_eq!(
        new.call(&f, json!({"command":"discard","operation_id":2}))["result"]["accepted"],
        true
    );
    let expected = f.finish_discard();
    let answer = handle_verified(&f.dispatcher, &f.status, held_poll);
    let signer = Identity::from_secret("scope-member", &[93; 32]).unwrap();
    let frame = member_control::sign_reply(&signer, &envelope, answer).unwrap();
    let opened = old
        .owner
        .open_reply(&envelope, &frame, Some(&signer.verification_key_digest()))
        .unwrap();
    assert_eq!(
        opened,
        pending(2),
        "a correctly signed old request must not read another scope"
    );
    assert_eq!(
        new.call(&f, json!({"command":"result","operation_id":2})),
        terminal(2, &expected)
    );
    f.empty();
}

#[test]
fn a_new_control_service_cannot_read_results_even_with_the_same_authority_token() {
    let mut f = Fixture::new();
    let old = Signed::new();
    assert_eq!(
        old.call(&f, json!({"command":"discard","operation_id":1}))["ok"],
        true
    );
    let expected = f.finish_discard();
    let mut new = Signed::new();
    new.service.authority = old.service.authority.clone();
    assert_eq!(
        new.call(&f, json!({"command":"result","operation_id":1})),
        pending(1)
    );
    assert_eq!(
        old.call(&f, json!({"command":"result","operation_id":1})),
        terminal(1, &expected)
    );
}

#[test]
fn unscoped_completion_cannot_acquire_the_active_operations_authority_by_id() {
    let mut f = Fixture::new();
    let signed = Signed::new();
    assert_eq!(
        signed.call(&f, json!({"command":"discard","operation_id":1}))["ok"],
        true
    );
    let operation = f.queue.try_recv().unwrap().unwrap();
    let unrelated = Operation {
        request: operation.request.clone(),
        cancelled: Arc::new(AtomicBool::new(false)),
        deadline: operation.deadline,
        origin: operation.origin,
        id: operation.id,
    };
    let out = json!({"ok":true,"result":f.work.discard().unwrap()});
    f.dispatcher
        .complete(&unrelated, out.clone(), |_| panic!("not local"))
        .unwrap();
    assert_eq!(
        signed.call(&f, json!({"command":"result","operation_id":1})),
        pending(1)
    );
    // The public low-level method retains its destructive behavior, including unscoped work.
    assert_eq!(f.dispatcher.result(1).unwrap()["reply"], out);
    assert_eq!(f.dispatcher.result(1).unwrap()["finished"], false);
}

#[test]
fn scoped_results_remain_bounded_and_reads_do_not_refresh_eviction() {
    let mut f = Fixture::new();
    let signed = Signed::new();
    for id in 1..=KEPT_RESULTS as u64 {
        assert_eq!(
            signed.call(&f, json!({"command":"discard","operation_id":id}))["ok"],
            true
        );
        f.finish_discard();
    }
    let first = signed.call(&f, json!({"command":"result","operation_id":1}));
    assert_eq!(first["result"]["finished"], true);
    assert_eq!(
        signed.call(&f, json!({"command":"result","operation_id":1})),
        first
    );
    let newest = KEPT_RESULTS as u64 + 1;
    assert_eq!(
        signed.call(&f, json!({"command":"discard","operation_id":newest}))["ok"],
        true
    );
    f.finish_discard();
    assert_eq!(
        signed.call(&f, json!({"command":"result","operation_id":1})),
        pending(1)
    );
    assert_eq!(f.dispatcher.results.lock().unwrap().len(), KEPT_RESULTS);
    for id in 2..=newest {
        assert_eq!(
            signed.call(&f, json!({"command":"result","operation_id":id}))["result"]["finished"],
            true
        );
    }
    f.empty();
}

#[test]
fn scoped_dispatch_preserves_type_errors_and_never_reads_unscoped_results() {
    let mut f = Fixture::new();
    let signed = Signed::new();
    for (request, code) in [
        (
            json!({"command":"result"}),
            "operator_operation_id_required",
        ),
        (
            json!({"command":"result","operation_id":"1"}),
            "operator_operation_id_required",
        ),
        (
            json!({"command":false,"operation_id":1}),
            "operator_command_invalid",
        ),
        (
            json!({"command":"discard","operation_id":"1"}),
            "operator_operation_id_invalid",
        ),
        (
            json!({"command":"discard"}),
            "operator_operation_id_required",
        ),
    ] {
        assert_eq!(signed.call(&f, request), json!({"ok":false,"code":code}));
    }
    let reply = f.complete_discard(1);
    assert_eq!(
        signed.call(&f, json!({"command":"result","operation_id":1})),
        pending(1)
    );
    assert_eq!(f.dispatcher.result(1).unwrap()["reply"], reply);
    assert_eq!(f.dispatcher.result(1).unwrap()["finished"], false);
    f.empty();
}
