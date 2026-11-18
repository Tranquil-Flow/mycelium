// SPDX-License-Identifier: AGPL-3.0-only
//! Model-free real signed control loop, including its mutable announced-owner boundary.
use super::*;
use budgets::{BudgetId as B, Entries};
use runtime::iroh_host::LoopbackHost;
use std::{
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread::JoinHandle,
    time::Duration,
};
use transport::Channel;

#[path = "result_retention_round2.rs"]
mod round2;

fn wait() -> Duration {
    Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis())
}
struct Member {
    address: iroh::EndpointAddr,
    dispatcher: Arc<work::Dispatcher>,
    queue: mpsc::Receiver<work::Result<work::Operation>>,
    enrolled: String,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
    executions: usize,
}
impl Member {
    fn start(pins: Pins) -> Self {
        let (dispatcher, queue) = work::Dispatcher::new().unwrap();
        let dispatch = dispatcher.clone();
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let signer = Identity::from_secret("rr-carrier-member", &[81; 32]).unwrap();
        let enrolled = signer.public_key_record()["verification_key_digest"]
            .as_str()
            .unwrap()
            .to_owned();
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        let thread = std::thread::spawn(move || {
            let rt = tokio::runtime::Builder::new_multi_thread()
                .worker_threads(1)
                .enable_all()
                .build()
                .unwrap();
            let clock = api::managed_carrier::HostClock::default();
            let mut host = LoopbackHost::bind_protocols(
                &rt,
                &clock,
                work::until(&clock, B::RouterClose).unwrap(),
                iroh::SecretKey::generate(),
                &[Channel::NodeControl],
            )
            .unwrap();
            let address = host.address().unwrap();
            ready_tx.send(address.clone()).unwrap();
            let result = work::control_loop(
                &rt,
                &clock,
                &mut host,
                &OwnerTrust::Announced(pins),
                address.id,
                &dispatch,
                &signer,
                &work::Live::new(json!({"joined":true})),
                &flag,
            );
            host.close(&clock, work::until(&clock, B::RouterClose).unwrap())
                .unwrap();
            result.unwrap();
        });
        Self {
            address: ready_rx.recv_timeout(wait()).unwrap(),
            dispatcher,
            queue,
            enrolled,
            stop,
            thread: Some(thread),
            executions: 0,
        }
    }
    fn complete_discard(&mut self) -> Value {
        let operation = self.queue.recv_timeout(wait()).unwrap().unwrap();
        assert_eq!(operation.id, Some(1));
        self.finish_discard(operation)
    }
    fn finish_discard(&mut self, operation: work::Operation) -> Value {
        assert_eq!(operation.request["command"], "discard");
        operation.check().unwrap();
        self.executions += 1;
        // Real model-free command; empty custody never accesses the path.
        let mut owned = work::Work::new(Default::default());
        let out = json!({"ok":true,"result":owned.discard().unwrap()});
        self.dispatcher
            .complete(&operation, out.clone(), |_| {
                panic!("remote result cannot be local output")
            })
            .unwrap();
        out
    }
    fn assert_once(&self) {
        assert_eq!(self.executions, 1);
        assert!(matches!(
            self.queue.try_recv(),
            Err(mpsc::TryRecvError::Empty)
        ));
        assert_eq!(self.dispatcher.active_id(), None);
    }
}
impl Drop for Member {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        self.dispatcher.cancel_active().unwrap();
        self.dispatcher.close_input().unwrap();
        self.thread.take().unwrap().join().unwrap();
    }
}
fn repin(exchange: &RunExchange, now: f64, pins: &Pins, secret: &iroh::SecretKey) {
    exchange.set_owner_control(Some(OwnerControl::new(secret.public())));
    let client = member_client(now);
    let (response, id) = member_read(exchange, &client, now);
    pins.repin(&VerifiedAnnouncement::from_response(&client, &response, &id).unwrap())
        .unwrap();
}

#[test]
fn lost_result_response_is_returned_by_the_next_signed_carrier_poll() {
    let (exchange, _, now) = owner_exchange(&oracle());
    let secret = iroh::SecretKey::from_bytes(&[82; 32]);
    let pins = Pins::new();
    repin(&exchange, now, &pins, &secret);
    let mut member = Member::start(pins);
    let (expected, retried) = member_remote::with_owner(&secret, |owner, client, deadline| {
        let call = |request| {
            let command = owner
                .command(&member.address.id, request, app_host::now())
                .unwrap();
            let signed = member_remote::raw(
                client,
                &member.address,
                &serde_json::to_vec(&command).unwrap(),
                deadline(),
            )
            .unwrap();
            let opened = owner
                .open_reply(&command, &signed, Some(&member.enrolled))
                .unwrap();
            (command, signed, opened)
        };
        let (_, _, admitted) = call(json!({"command":"discard","operation_id":1}));
        assert_eq!(
            admitted,
            json!({"ok":true,"result":{"accepted":true,"operation_id":1}})
        );
        let (_, _, pending) = call(json!({"command":"result","operation_id":1}));
        assert_eq!(pending["result"]["finished"], false);
        // Release the immutable closure borrow before running the real operation.
        let expected = member.complete_discard();
        let remote = member_control::Remote {
            addr: member.address.clone(),
            enrolled: Some(member.enrolled.clone()),
        };
        let first_command = owner
            .command(
                &member.address.id,
                json!({"command":"result","operation_id":1}),
                app_host::now(),
            )
            .unwrap();
        let first_signed = member_remote::raw(
            client,
            &member.address,
            &serde_json::to_vec(&first_command).unwrap(),
            deadline(),
        )
        .unwrap();
        let first = owner
            .open_reply(&first_command, &first_signed, Some(&member.enrolled))
            .unwrap();
        assert_eq!(first["result"]["finished"], true);
        assert_eq!(first["result"]["reply"], expected);
        // Isolated consumer-delivery fault: discard this authenticated terminal response.
        drop(first);
        let next_command = owner
            .command(
                &member.address.id,
                json!({"command":"result","operation_id":1}),
                app_host::now(),
            )
            .unwrap();
        assert!(
            next_command["message"]["sequence"].as_u64().unwrap()
                > first_command["message"]["sequence"].as_u64().unwrap()
        );
        assert_eq!(
            owner
                .open_reply(&next_command, &first_signed, Some(&member.enrolled))
                .unwrap_err()
                .code,
            "member_control_reply_mismatch"
        );
        let next_signed = member_remote::raw(
            client,
            &member.address,
            &serde_json::to_vec(&next_command).unwrap(),
            deadline(),
        )
        .unwrap();
        let retried = owner
            .open_reply(&next_command, &next_signed, Some(&member.enrolled))
            .unwrap();
        for request in [
            json!({"command":"result","operation_id":2}),
            json!({"command":"discard","operation_id":1}),
        ] {
            let reply = remote
                .call(owner, client, request.clone(), deadline(), app_host::now())
                .unwrap();
            if request["command"] == "result" {
                assert_eq!(reply["result"]["finished"], false);
                assert!(reply["result"].get("reply").is_none());
            } else {
                assert_eq!(reply["code"], "operator_operation_id_invalid");
            }
        }
        (expected, retried)
    });
    member.assert_once();
    drop(member); // stop and join before the decisive RED assertion
    assert_eq!(
        retried,
        json!({"ok":true,"result":{"operation_id":1,"finished":true,"reply":expected}}),
        "fresh signed poll lost the original completed result"
    );
}

#[test]
fn a_replacement_announced_owner_cannot_read_the_previous_owners_consumed_result() {
    let (exchange, _, now) = owner_exchange(&oracle());
    let (old, new) = (
        iroh::SecretKey::from_bytes(&[83; 32]),
        iroh::SecretKey::from_bytes(&[84; 32]),
    );
    let pins = Pins::new();
    repin(&exchange, now, &pins, &old);
    let mut member = Member::start(pins.clone());
    member_remote::with_owner(&old, |owner, client, deadline| {
        let remote = member_control::Remote {
            addr: member.address.clone(),
            enrolled: Some(member.enrolled.clone()),
        };
        let accepted = remote
            .call(
                owner,
                client,
                json!({"command":"discard","operation_id":1}),
                deadline(),
                app_host::now(),
            )
            .unwrap();
        assert_eq!(accepted["result"]["accepted"], true);
        let expected = member.complete_discard();
        let result = remote
            .call(
                owner,
                client,
                json!({"command":"result","operation_id":1}),
                deadline(),
                app_host::now(),
            )
            .unwrap();
        assert_eq!(result["result"]["reply"], expected);
    });
    // Public Announced contract permits changing the current pin while this loop lives.
    repin(&exchange, now, &pins, &new);
    let result = member_remote::with_owner(&new, |owner, client, deadline| {
        member_control::Remote {
            addr: member.address.clone(),
            enrolled: Some(member.enrolled.clone()),
        }
        .call(
            owner,
            client,
            json!({"command":"result","operation_id":1}),
            deadline(),
            app_host::now(),
        )
        .unwrap()
    });
    member.assert_once();
    drop(member);
    assert_ne!(
        result["result"]["finished"], true,
        "replacement owner obtained the old owner's result: {result}"
    );
    assert!(result["result"].get("reply").is_none());
}
