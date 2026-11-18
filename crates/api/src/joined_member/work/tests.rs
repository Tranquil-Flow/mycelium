// SPDX-License-Identifier: AGPL-3.0-only
//! Result retention regressions through real admission, Work and completion. No model or I/O.
use super::*;

#[path = "provenance.rs"]
mod provenance;

struct Fixture {
    dispatcher: Arc<Dispatcher>,
    queue: mpsc::Receiver<Result<Operation>>,
    status: Status,
    work: Work,
    executions: usize,
}
impl Fixture {
    fn new() -> Self {
        let (dispatcher, queue) = Dispatcher::new().unwrap();
        Self {
            dispatcher,
            queue,
            status: Live::new(json!({"joined":true})),
            // Empty custody discard returns before accessing this path.
            work: Work::new(PathBuf::new()),
            executions: 0,
        }
    }
    fn handle(&self, request: Value) -> Value {
        handle(&self.dispatcher, &self.status, request)
    }
    fn poll(&self, id: u64) -> Value {
        self.handle(json!({"command":"result","operation_id":id}))
    }
    fn finish_discard(&mut self) -> Value {
        let operation = self.queue.try_recv().unwrap().unwrap();
        assert_eq!(operation.request["command"], "discard");
        operation.check().unwrap();
        self.executions += 1;
        let out = json!({"ok":true,"result":self.work.discard().unwrap()});
        self.work
            .record(&self.status, true, "discard", &operation, &out);
        self.dispatcher
            .complete(&operation, out.clone(), |_| {
                panic!("remote completion must not reach local output")
            })
            .unwrap();
        out
    }
    fn complete_discard(&mut self, id: u64) -> Value {
        assert_eq!(
            self.handle(json!({"command":"discard","operation_id":id})),
            json!({"ok":true,"result":{"accepted":true,"operation_id":id}})
        );
        self.finish_discard()
    }
    fn empty(&self) {
        assert!(matches!(
            self.queue.try_recv(),
            Err(mpsc::TryRecvError::Empty)
        ));
        assert_eq!(self.dispatcher.active_id(), None);
    }
}
fn pending(id: u64) -> Value {
    json!({"ok":true,"result":{"operation_id":id,"finished":false}})
}
fn terminal(id: u64, reply: &Value) -> Value {
    json!({"ok":true,"result":{"operation_id":id,"finished":true,"reply":reply}})
}

#[test]
fn lost_result_is_returned_to_a_fresh_authenticated_poll_without_reexecution() {
    let mut f = Fixture::new();
    let owner = member_control::Owner::new(&iroh::SecretKey::from_bytes(&[71; 32])).unwrap();
    let member = iroh::SecretKey::from_bytes(&[72; 32]).public();
    let signer = Identity::from_secret("result-test-member", &[73; 32]).unwrap();
    let enrolled = signer.public_key_record()["verification_key_digest"]
        .as_str()
        .unwrap()
        .to_owned();
    let trust = OwnerTrust::Operator(owner.endpoint());
    let mut last = 0;
    let mut call = |f: &Fixture, request| {
        let now = app_host::now();
        let command = owner.command(&member, request, now).unwrap();
        let (sequence, request) =
            admit_command(&trust, owner.endpoint(), &command, &member, last, now).unwrap();
        assert!(sequence > last);
        last = sequence;
        let reply = member_control::sign_reply(&signer, &command, f.handle(request)).unwrap();
        let opened = owner.open_reply(&command, &reply, Some(&enrolled)).unwrap();
        (command, reply, opened)
    };
    let (_, _, admitted) = call(&f, json!({"command":"discard","operation_id":1}));
    assert_eq!(admitted["result"]["accepted"], true);
    assert_eq!(f.poll(1), pending(1));
    let completed = f.finish_discard();
    let (first_command, first_signed, first) =
        call(&f, json!({"command":"result","operation_id":1}));
    assert_eq!(first, terminal(1, &completed));
    // Fault seam: the result was read and signed, but is never delivered to the consumer.
    drop(first);
    let (next_command, _, delivered) = call(&f, json!({"command":"result","operation_id":1}));
    assert_ne!(first_command, next_command);
    assert_eq!(
        owner
            .open_reply(&next_command, &first_signed, Some(&enrolled))
            .unwrap_err()
            .code,
        "member_control_reply_mismatch",
        "the cached body must be signed anew for the fresh request"
    );
    assert_eq!(f.executions, 1);
    f.empty();
    assert_eq!(
        delivered,
        terminal(1, &completed),
        "a lost result response must not turn a completed operation into pending"
    );
}

#[test]
fn normal_completion_unknown_ids_and_duplicate_execution_controls() {
    let mut f = Fixture::new();
    assert_eq!(f.poll(0), pending(0));
    assert_eq!(f.poll(1), pending(1));
    let completed = f.complete_discard(1);
    assert_eq!(f.poll(2), pending(2));
    assert_eq!(f.poll(u64::MAX), pending(u64::MAX));
    assert_eq!(f.poll(1), terminal(1, &completed));
    assert_eq!(
        f.handle(json!({"command":"discard","operation_id":1})),
        json!({"ok":false,"code":"operator_operation_id_invalid"})
    );
    assert_eq!(f.executions, 1);
    f.empty();
}

#[test]
fn completed_results_are_bounded_and_reads_do_not_refresh_eviction_order() {
    let mut f = Fixture::new();
    for id in 1..=KEPT_RESULTS as u64 {
        f.complete_discard(id);
    }
    // Reading the oldest must not give it a newer retention position.
    assert_eq!(f.poll(1)["result"]["finished"], true);
    let newest = KEPT_RESULTS as u64 + 1;
    f.complete_discard(newest);
    assert_eq!(f.poll(1), pending(1));
    for id in 2..=newest {
        assert_eq!(f.poll(id)["result"]["finished"], true, "id={id}");
    }
    assert!(f.dispatcher.results.lock().unwrap().len() <= KEPT_RESULTS);
    assert_eq!(f.executions, KEPT_RESULTS + 1);
    f.empty();
}

#[test]
fn independent_dispatchers_and_local_completions_do_not_share_remote_results() {
    let mut old = Fixture::new();
    let mut new = Fixture::new();
    let reply = old.complete_discard(1);
    assert_eq!(new.poll(1), pending(1));
    new.complete_discard(1);
    assert_eq!(old.poll(1), terminal(1, &reply));
    assert_eq!(new.poll(1), terminal(1, &reply));
    old.dispatcher
        .admit(json!({"command":"discard","operation_id":2}), Origin::Stdin)
        .unwrap();
    let operation = old.queue.try_recv().unwrap().unwrap();
    let local = json!({"ok":true,"result":old.work.discard().unwrap()});
    let mut delivered = None;
    old.dispatcher
        .complete(&operation, local.clone(), |value| {
            delivered = Some(value.clone());
            Ok(())
        })
        .unwrap();
    assert_eq!(delivered, Some(local));
    assert_eq!(old.poll(2), pending(2));
    old.empty();
    new.empty();
}

#[test]
fn result_polls_do_not_cancel_or_extend_an_active_operation() {
    let f = Fixture::new();
    assert_eq!(
        f.handle(json!({"command":"discard","operation_id":1}))["ok"],
        true
    );
    let operation = f.queue.try_recv().unwrap().unwrap();
    let deadline = operation.deadline;
    assert_eq!(f.poll(1), pending(1));
    assert_eq!(f.poll(2), pending(2));
    assert_eq!(operation.deadline, deadline);
    operation.check().unwrap();
    assert_eq!(
        f.dispatcher.cancel(Some(2)).unwrap()["cancel_requested"],
        false
    );
    operation.check().unwrap();
    assert_eq!(
        f.dispatcher.cancel(Some(1)).unwrap()["cancel_requested"],
        true
    );
    assert_eq!(operation.check().unwrap_err(), "member_operation_cancelled");
    f.dispatcher.close_input().unwrap();
    assert!(matches!(
        f.queue.try_recv(),
        Err(mpsc::TryRecvError::Disconnected)
    ));
}
