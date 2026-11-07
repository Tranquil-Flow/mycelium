// SPDX-License-Identifier: AGPL-3.0-only
// O:path:mycelium_a13_macos/managed_control.py; F1/F2/F3/F4
use control_runtime::{
    Store,
    mailbox::{Limits, Mailbox},
};
use protocol::identity::Identity;
use serde_json::{Value, json};
use std::path::PathBuf;
#[test]
fn migrated_python_schema_dispatches_and_reopens_existing_claims() {
    let t = Temp::new();
    let db = rusqlite::Connection::open(t.0.join("state.db")).unwrap();
    db.execute_batch(include_str!("fixtures/v014-mailbox.sql"))
        .unwrap();
    for (k, v) in [
        ("swarm_id", "swarm-fixture".to_owned()),
        ("owner_node_id", "node-owner".into()),
        ("owner_endpoint_id", owner().endpoint_id().into()),
        ("owner_key_digest", owner().verification_key_digest()),
    ] {
        db.execute(
            "UPDATE managed_metadata SET value=?1 WHERE key=?2",
            rusqlite::params![v, k],
        )
        .unwrap();
    }
    drop(db);
    let store =
        Store::open_with_backup(&t.0.join("state.db"), Some(&t.0.join("backup.db"))).unwrap();
    let mut m = Mailbox::new(
        store,
        owner(),
        "swarm-fixture",
        "node-owner",
        Limits {
            pending: budgets::Entries::new(128),
            active_work: budgets::Entries::new(1),
            active_control: budgets::Entries::new(2),
        },
    )
    .unwrap();
    m.bind_member(&binding(), &member().public_key_record(), 1300.0)
        .unwrap();
    let e = enqueue(1, "legacy-command");
    dispatch(&mut m, &e);
    assert_eq!(
        body(&dispatch(&mut m, &request("target_poll.v1", 1, json!({}))))["delivery_kind"],
        "command"
    );
    drop(m);
    let db = rusqlite::Connection::open(t.0.join("state.db")).unwrap();
    // Delete only Rust's extension row to exercise restoration from the original
    // Python-format columns, not a newly invented JSON state document.
    db.execute("DELETE FROM control_dispatch", []).unwrap();
    drop(db);
    let mut m = open(&t);
    assert_eq!(
        body(&dispatch(&mut m, &request("target_poll.v1", 2, json!({}))))["delivery_kind"],
        "reconciliation_required"
    );
}

#[test]
fn concurrent_cleanup_priority_and_signed_receipts_match_l5_reader() {
    let t = Temp::new();
    let mut m = open(&t);
    m.bind_member(&binding(), &member().public_key_record(), 1300.0)
        .unwrap();
    let e = enqueue(1, "hello");
    let receipt = dispatch(&mut m, &e);
    let env: Value = serde_json::from_slice(&e).unwrap();
    let response: Value = serde_json::from_slice(&receipt).unwrap();
    protocol::managed_receipt::verify(
        &response,
        &protocol::managed_receipt::ExpectedReceipt {
            kind: protocol::managed_receipt::ReceiptKind::Enqueue,
            owner_key: &owner().public_key_record(),
            request_id: env["statement"]["request_id"].as_str().unwrap(),
            swarm_id: "swarm-fixture",
            owner_node_id: "node-owner",
            binding: &binding(),
            native_command: &env["statement"]["native_command"],
            now: 1000.0,
        },
        control::managed_time_limits().unwrap(),
    )
    .unwrap();
    let cleanup = request(
        "owner_command.v1",
        2,
        json!({"owner_node_id":"node-owner","native_command":{"protocol":"mycelium.physical_node_control.v1","command":"stop","command_id":"cleanup","run_id":"run-1","deployment_id":"deployment-1","payload":{}}}),
    );
    dispatch(&mut m, &cleanup);
    let p = request("target_poll.v2", 1, json!({"active_claims":{}}));
    let delivered = dispatch(&mut m, &p);
    assert_eq!(body(&delivered)["command_id"], "cleanup");
    let native = json!({"protocol":"mycelium.physical_node_control.v1","command_id":"cleanup","node_id":"node-member","ok":true,"route_ready":false,"result":{}});
    let a = request(
        "target_ack.v1",
        2,
        json!({"command_id":"cleanup","claim_token":"claim-fixture","disposition":"completed","native_response":native}),
    );
    let acknowledged = dispatch(&mut m, &a);
    assert_eq!(body(&acknowledged)["state"], "completed");
    let receipt: Value = serde_json::from_slice(&acknowledged).unwrap();
    protocol::managed_receipt::verify(&receipt,&protocol::managed_receipt::ExpectedReceipt{kind:protocol::managed_receipt::ReceiptKind::Ack,owner_key:&owner().public_key_record(),request_id:"req-target_ack.v1-2",swarm_id:"swarm-fixture",owner_node_id:"node-owner",binding:&binding(),native_command:&serde_json::from_slice::<Value>(&cleanup).unwrap()["statement"]["native_command"],now:1000.0},control::managed_time_limits().unwrap()).unwrap();
}

#[test]
fn owner_result_is_a_fresh_bound_observation_without_claim_capability() {
    let t = Temp::new();
    let mut m = open(&t);
    m.bind_member(&binding(), &member().public_key_record(), 1300.0)
        .unwrap();
    let e = enqueue(1, "result-command");
    let env: Value = serde_json::from_slice(&e).unwrap();
    dispatch(&mut m, &e);
    let observation = request(
        "owner_result.v1",
        2,
        json!({"owner_node_id":"node-owner","native_command":env["statement"]["native_command"]}),
    );
    let result = dispatch(&mut m, &observation);
    assert_eq!(body(&result)["state"], "pending");
    assert!(body(&result).get("claim_token").is_none());
    dispatch(&mut m, &request("target_poll.v1", 1, json!({})));
    assert_eq!(dispatch(&mut m, &observation), result);
    let fresh = request(
        "owner_result.v1",
        3,
        json!({"owner_node_id":"node-owner","native_command":env["statement"]["native_command"]}),
    );
    assert_eq!(body(&dispatch(&mut m, &fresh))["state"], "claimed");
    let native = json!({"protocol":"mycelium.physical_node_control.v1","command_id":"result-command","node_id":"node-member","ok":true,"route_ready":false,"result":{}});
    dispatch(
        &mut m,
        &request(
            "target_ack.v1",
            2,
            json!({"command_id":"result-command","claim_token":"claim-fixture","disposition":"completed","native_response":native}),
        ),
    );
    let request4 = request(
        "owner_result.v1",
        4,
        json!({"owner_node_id":"node-owner","native_command":env["statement"]["native_command"]}),
    );
    let final_response = dispatch(&mut m, &request4);
    let final_doc: Value = serde_json::from_slice(&final_response).unwrap();
    protocol::managed_receipt::verify(
        &final_doc,
        &protocol::managed_receipt::ExpectedReceipt {
            kind: protocol::managed_receipt::ReceiptKind::Result,
            owner_key: &owner().public_key_record(),
            request_id: "req-owner_result.v1-4",
            swarm_id: "swarm-fixture",
            owner_node_id: "node-owner",
            binding: &binding(),
            native_command: &env["statement"]["native_command"],
            now: 1000.0,
        },
        control::managed_time_limits().unwrap(),
    )
    .unwrap();
    assert_eq!(body(&final_response)["state"], "completed");
    let mut corrupt = native;
    corrupt["result"] = json!({"corrupt":true});
    let db = rusqlite::Connection::open(t.0.join("state.db")).unwrap();
    db.execute(
        "UPDATE control_dispatch SET native=?1 WHERE command_id='result-command'",
        [serde_json::to_vec(&corrupt).unwrap()],
    )
    .unwrap();
    assert!(m.dispatch(&request("owner_result.v1",5,json!({"owner_node_id":"node-owner","native_command":env["statement"]["native_command"]})),||1000.0,||panic!("observation must not mint claims")).is_err());
}

#[test]
fn delivery_wakeup_and_held_poll_do_not_delay_live_claims() {
    use budgets::{BudgetId as B, Entries};
    use std::time::{Duration, Instant};
    let t = Temp::new();
    // Inject a fixture wait for simultaneous real connections. The development
    // opener is deliberately nonblocking, not a production SQLite policy.
    let fixture_wait = B::OwnerPoll.span(Entries::ZERO).unwrap();
    let mut m = open_wait(&t, fixture_wait);
    m.bind_member(&binding(), &member().public_key_record(), 1300.0)
        .unwrap();
    let wake = m.delivery_wake();
    let generation = wake.generation().unwrap();
    let mut writer = open_wait(&t, fixture_wait).with_delivery_wake(wake.clone());
    dispatch(&mut writer, &enqueue(1, "wake"));
    let limit = || {
        Instant::now()
            + Duration::from_millis(B::WorkerStartup.span(Entries::ZERO).unwrap().millis())
    };
    assert!(wake.wait_until(generation, limit()).unwrap());
    let poll = request("target_poll.v2", 1, json!({"active_claims":{}}));
    dispatch(&mut m, &poll);
    let owing = request(
        "target_poll.v2",
        2,
        json!({"active_claims":{"wake":"claim-fixture"}}),
    );
    let mut calls = 0;
    let live_deadline =
        Instant::now() + Duration::from_millis(B::RelayPoll.span(Entries::ZERO).unwrap().millis());
    let empty = m
        .poll_held(
            &owing,
            live_deadline,
            B::OwnerPoll.span(Entries::ZERO).unwrap(),
            || {
                calls += 1;
                1000.0
            },
            || Ok("held-claim".into()),
        )
        .unwrap();
    assert_eq!(calls, 1);
    assert!(
        Instant::now() < live_deadline,
        "live acknowledgement was parked"
    );
    assert_eq!(body(&empty)["delivery_kind"], "empty");
    let native = json!({"protocol":"mycelium.physical_node_control.v1","command_id":"wake","node_id":"node-member","ok":true,"route_ready":false,"result":{}});
    dispatch(
        &mut m,
        &request(
            "target_ack.v1",
            3,
            json!({"command_id":"wake","claim_token":"claim-fixture","disposition":"completed","native_response":native}),
        ),
    );
    let idle = request("target_poll.v2", 4, json!({"active_claims":{}}));
    let mut attempts = 0;
    let delivered = std::thread::scope(|scope| {
        let writer_thread=scope.spawn(||{let db=rusqlite::Connection::open(t.0.join("state.db")).unwrap();let end=limit();
            while !db.query_row("SELECT EXISTS(SELECT 1 FROM managed_auth_requests WHERE request_id='req-target_poll.v2-4')",[],|r|r.get::<_,bool>(0)).unwrap() {assert!(Instant::now()<end);std::thread::sleep(Duration::from_millis(B::OwnerPoll.span(Entries::ZERO).unwrap().millis()));}
            dispatch(&mut writer,&enqueue(2,"later"));});
        let response = m
            .poll_held(
                &idle,
                limit(),
                B::OwnerPoll.span(Entries::ZERO).unwrap(),
                || {
                    attempts += 1;
                    1000.0
                },
                || Ok("held-claim".into()),
            )
            .unwrap();
        writer_thread.join().unwrap();
        response
    });
    assert!(attempts >= 2);
    assert_eq!(body(&delivered)["command_id"], "later");
    assert_eq!(dispatch(&mut m, &idle), delivered);
}

#[test]
fn held_poll_expired_parent_deadline_cannot_claim_queued_work() {
    let t = Temp::new();
    let mut m = open(&t);
    m.bind_member(&binding(), &member().public_key_record(), 1300.0)
        .unwrap();
    dispatch(&mut m, &enqueue(1, "late"));
    let p = request("target_poll.v2", 1, json!({"active_claims":{}}));
    assert!(
        m.poll_held(
            &p,
            std::time::Instant::now(),
            budgets::BudgetId::OwnerPoll
                .span(budgets::Entries::ZERO)
                .unwrap(),
            || 1000.0,
            || Ok("too-late".into())
        )
        .is_err()
    );
    assert_eq!(body(&dispatch(&mut m, &p))["claim_token"], "claim-fixture");
}

struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "v02-l6-mailbox-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        std::fs::create_dir(&p).unwrap();
        Self(p)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
#[test]
fn shared_signer_preserves_owned_wire_reopen_and_revocation() {
    use std::sync::Arc;
    let t = Temp::new();
    let signer = Arc::new(owner());
    let shared_open = || {
        Mailbox::new_shared(
            Store::open(&t.0.join("shared.db")).unwrap(),
            Arc::clone(&signer),
            "swarm-fixture",
            "node-owner",
            Limits {
                pending: budgets::Entries::new(128),
                active_work: budgets::Entries::new(1),
                active_control: budgets::Entries::new(2),
            },
        )
        .unwrap()
    };
    let mut owned = open(&t);
    let mut shared = shared_open();
    // The mailbox retains the supplied allocation, not reconstructed key material.
    assert_eq!(Arc::strong_count(&signer), 2);
    for mailbox in [&mut owned, &mut shared] {
        mailbox
            .bind_member(&binding(), &member().public_key_record(), 1300.0)
            .unwrap();
    }
    let command = enqueue(1, "shared-command");
    let original = dispatch(&mut owned, &command);
    assert_eq!(dispatch(&mut shared, &command), original);
    let poll = request("target_poll.v1", 1, json!({}));
    assert_eq!(dispatch(&mut shared, &poll), dispatch(&mut owned, &poll));
    drop(shared);
    assert_eq!(Arc::strong_count(&signer), 1);
    let mut shared = shared_open();
    assert_eq!(dispatch(&mut shared, &command), original);
    assert_eq!(dispatch(&mut shared, &poll), dispatch(&mut owned, &poll));
    let conflict = enqueue(1, "different-command");
    assert!(shared.dispatch(&conflict, || 1000.0, || panic!()).is_err());
    assert!(owned.dispatch(&conflict, || 1000.0, || panic!()).is_err());
    shared.revoke_member("node-member").unwrap();
    assert!(shared.dispatch(&command, || 1000.0, || panic!()).is_err());
    // Retaining a signing allocation does not restore revoked membership.
    assert_eq!(Arc::strong_count(&signer), 2);
}

#[test]
fn shared_constructor_keeps_configuration_and_original_database_binding() {
    use std::sync::Arc;
    let t = Temp::new();
    drop(open(&t));
    let limits = Limits {
        pending: budgets::Entries::new(128),
        active_work: budgets::Entries::new(1),
        active_control: budgets::Entries::new(2),
    };
    let attempt = |signer, swarm, node, limits| {
        Mailbox::new_shared(
            Store::open(&t.0.join("state.db")).unwrap(),
            signer,
            swarm,
            node,
            limits,
        )
    };
    assert!(attempt(Arc::new(owner()), "swarm-fixture", "node-owner", limits).is_ok());
    for (signer, swarm, node) in [
        (member(), "swarm-fixture", "node-owner"),
        (owner(), "other-swarm", "node-owner"),
        (owner(), "swarm-fixture", "other-owner"),
    ] {
        assert!(matches!(
            attempt(Arc::new(signer), swarm, node, limits),
            Err(control_runtime::Error::Refused(
                "managed_control_identity_binding_mismatch"
            ))
        ));
    }
    for (swarm, node, limits) in [
        ("invalid swarm", "node-owner", limits),
        ("swarm-fixture", "invalid node", limits),
        (
            "swarm-fixture",
            "node-owner",
            Limits {
                pending: budgets::Entries::ZERO,
                ..limits
            },
        ),
        (
            "swarm-fixture",
            "node-owner",
            Limits {
                active_work: budgets::Entries::ZERO,
                ..limits
            },
        ),
        (
            "swarm-fixture",
            "node-owner",
            Limits {
                active_control: budgets::Entries::ZERO,
                ..limits
            },
        ),
    ] {
        assert!(matches!(
            attempt(Arc::new(owner()), swarm, node, limits),
            Err(control_runtime::Error::Refused(
                "managed_control_configuration_invalid"
            ))
        ));
    }
    // Failed attempts have not replaced the stored original identity.
    drop(open(&t));
}

fn owner() -> Identity {
    Identity::from_secret("endpoint-owner", &[0x11; 32]).unwrap()
}
fn member() -> Identity {
    Identity::from_secret("endpoint-member", &[0x22; 32]).unwrap()
}
fn binding() -> Value {
    json!({"target_node_id":"node-member","target_endpoint_id":"endpoint-member","target_key_digest":member().verification_key_digest(),"membership_generation":2,"member_incarnation":"incarnation-2","run_id":"run-1","deployment_id":"deployment-1"})
}
fn open(t: &Temp) -> Mailbox {
    open_wait(t, budgets::Span::ZERO)
}
fn open_wait(t: &Temp, wait: budgets::Span) -> Mailbox {
    Mailbox::new(
        Store::open_with_policy(&t.0.join("state.db"), None, wait).unwrap(),
        owner(),
        "swarm-fixture",
        "node-owner",
        Limits {
            pending: budgets::Entries::new(128),
            active_work: budgets::Entries::new(1),
            active_control: budgets::Entries::new(2),
        },
    )
    .unwrap()
}
fn request(kind: &str, seq: u64, extra: Value) -> Vec<u8> {
    let mut s = binding();
    for (k,v) in json!({"protocol":format!("mycelium.managed_control.{kind}"),"request_id":format!("req-{kind}-{seq}"),"nonce":format!("nonce-{kind}-{seq}"),"sequence":seq,"swarm_id":"swarm-fixture","issued_at":1000.0,"expires_at":1100.0}).as_object().unwrap(){s[k]=v.clone();}
    for (k, v) in extra.as_object().unwrap() {
        s[k] = v.clone();
    }
    let signer = if kind.starts_with("owner_") {
        owner()
    } else {
        member()
    };
    serde_json::to_vec(&json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL,"verification_key":signer.public_key_record(),"signature":signer.sign(&s).unwrap(),"statement":s})).unwrap()
}
fn enqueue(seq: u64, id: &str) -> Vec<u8> {
    request(
        "owner_command.v1",
        seq,
        json!({"owner_node_id":"node-owner","native_command":{"protocol":"mycelium.physical_node_control.v1","command":"hello","command_id":id,"run_id":"run-1","deployment_id":"deployment-1","payload":{}}}),
    )
}
fn dispatch(m: &mut Mailbox, bytes: &[u8]) -> Vec<u8> {
    m.dispatch(bytes, || 1000.0, || Ok("claim-fixture".into()))
        .unwrap()
}
fn body(bytes: &[u8]) -> Value {
    serde_json::from_slice::<Value>(bytes).unwrap()["statement"].clone()
}
#[test]
fn signed_dispatch_replays_claim_and_ack_across_reopen_without_reexecution() {
    let t = Temp::new();
    let mut m = open(&t);
    m.bind_member(&binding(), &member().public_key_record(), 1300.0)
        .unwrap();
    let e = enqueue(1, "command-1");
    let receipt = dispatch(&mut m, &e);
    assert_eq!(body(&receipt)["state"], "pending");
    assert_eq!(dispatch(&mut m, &e), receipt);
    let poll = request("target_poll.v1", 1, json!({}));
    let claimed = dispatch(&mut m, &poll);
    assert_eq!(body(&claimed)["delivery_kind"], "command");
    drop(m);
    let mut m = open(&t);
    assert_eq!(dispatch(&mut m, &poll), claimed);
    let reconcile = dispatch(&mut m, &request("target_poll.v1", 2, json!({})));
    assert_eq!(body(&reconcile)["delivery_kind"], "reconciliation_required");
    assert!(body(&reconcile)["command_envelope"].is_null());
    let ack = request(
        "target_ack.v1",
        3,
        json!({"command_id":"command-1","claim_token":"claim-fixture","disposition":"reconciliation_required","native_response":null}),
    );
    let acked = dispatch(&mut m, &ack);
    assert_eq!(body(&acked)["state"], "reconciliation_required");
    assert_eq!(dispatch(&mut m, &ack), acked);
    assert_eq!(
        body(&dispatch(&mut m, &request("target_poll.v1", 4, json!({}))))["delivery_kind"],
        "empty"
    );
    // A command receipt is not proof that its worker or reservations are gone.
    let db = rusqlite::Connection::open(t.0.join("state.db")).unwrap();
    assert_eq!(
        db.query_row(
            "SELECT retired_at FROM managed_commands WHERE command_id='command-1'",
            [],
            |r| r.get::<_, Option<f64>>(0)
        )
        .unwrap(),
        None
    );
}
#[test]
fn empty_poll_can_deliver_later_without_new_nonce_and_stale_member_cannot_retry() {
    let t = Temp::new();
    let mut m = open(&t);
    m.bind_member(&binding(), &member().public_key_record(), 1300.0)
        .unwrap();
    let p = request("target_poll.v2", 1, json!({"active_claims":{}}));
    assert_eq!(body(&dispatch(&mut m, &p))["delivery_kind"], "empty");
    dispatch(&mut m, &enqueue(1, "command-1"));
    let delivered = dispatch(&mut m, &p);
    assert_eq!(body(&delivered)["delivery_kind"], "command");
    assert_eq!(dispatch(&mut m, &p), delivered);
    let mut next = binding();
    next["membership_generation"] = 3.into();
    m.bind_member(&next, &member().public_key_record(), 1300.0)
        .unwrap();
    assert!(
        m.dispatch(&p, || 1000.0, || panic!("must not claim"))
            .is_err()
    );
}
#[test]
fn dispatch_effects_are_atomic_bounded_and_replayed_without_republication() {
    let t = Temp::new();
    let mut m = open(&t);
    m.bind_member(&binding(), &member().public_key_record(), 1300.0)
        .unwrap();
    let db = rusqlite::Connection::open(t.0.join("state.db")).unwrap();
    let request = enqueue(1, "transaction-command");
    let mut sampled = 0;
    let response = m
        .dispatch_with_effects(
            &request,
            || {
                sampled += 1;
                db.busy_timeout(std::time::Duration::ZERO).unwrap();
                assert!(db.execute_batch("BEGIN IMMEDIATE").is_err());
                1001.0
            },
            || panic!("enqueue cannot claim"),
            |effect| {
                assert_eq!(effect.now(), 1001.0);
                assert_eq!(
                    effect.statement()["native_command"]["command_id"],
                    "transaction-command"
                );
                effect.publish("forwarding-fixture", None, &json!({"pending":true}))?;
                effect.enqueue(
                    "effect-1",
                    &json!({"action":"forward","command_id":"transaction-command"}),
                    budgets::Entries::new(1),
                )?;
                Ok(())
            },
        )
        .unwrap();
    assert_eq!(sampled, 1);
    assert_eq!(body(&response)["issued_at"], 1001.0);
    assert_eq!(
        m.dispatch_with_effects(
            &request,
            || 1002.0,
            || panic!(),
            |_| panic!("exact retry cannot republish")
        )
        .unwrap(),
        response
    );
    drop(m);
    let mut m = open(&t);
    assert_eq!(
        m.pending_effects(budgets::Entries::new(1)).unwrap()[0].id,
        "effect-1"
    );
    // Outbox exhaustion rolls back mailbox, state CAS, and replay high water together.
    let next = enqueue(2, "capacity-command");
    assert!(
        m.dispatch_with_effects(
            &next,
            || 1003.0,
            || panic!(),
            |effect| {
                effect.publish("forwarding-fixture", Some(1), &json!({"pending":false}))?;
                effect.enqueue(
                    "effect-2",
                    &json!({"action":"forward"}),
                    budgets::Entries::new(1),
                )?;
                Ok(())
            }
        )
        .is_err()
    );
    assert_eq!(
        db.query_row(
            "SELECT revision FROM control_state WHERE name='forwarding-fixture'",
            [],
            |r| r.get::<_, u64>(0)
        )
        .unwrap(),
        1
    );
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM managed_commands WHERE command_id='capacity-command'",
            [],
            |r| r.get::<_, u64>(0)
        )
        .unwrap(),
        0
    );
    let claimed = m.claim_effect("effect-1", "attempt-1", || 1004.0).unwrap();
    assert_eq!(claimed.document["command_id"], "transaction-command");
    assert!(m.claim_effect("effect-1", "attempt-2", || 1005.0).is_err());
    assert!(
        m.pending_effects(budgets::Entries::new(1))
            .unwrap()
            .is_empty()
    );
    assert!(
        m.complete_effect("effect-1", "foreign", &json!({"done":true}))
            .is_err()
    );
    m.complete_effect("effect-1", "attempt-1", &json!({"done":true}))
        .unwrap();
    m.complete_effect("effect-1", "attempt-1", &json!({"done":true}))
        .unwrap();
    assert!(
        m.complete_effect("effect-1", "attempt-1", &json!({"done":false}))
            .is_err()
    );
    assert_eq!(m.effect("effect-1").unwrap().unwrap().state, "completed");
    // Completed entries are still retained until an independently authorized retirement.
    assert!(
        m.dispatch_with_effects(
            &next,
            || 1006.0,
            || panic!(),
            |e| e.enqueue("effect-2", &json!({}), budgets::Entries::new(1))
        )
        .is_err()
    );
}

#[test]
fn dispatch_effect_commit_failure_has_no_partial_publication_or_replay() {
    let t = Temp::new();
    let mut m = open(&t);
    m.bind_member(&binding(), &member().public_key_record(), 1300.0)
        .unwrap();
    let db = rusqlite::Connection::open(t.0.join("state.db")).unwrap();
    // DEFERRABLE failure happens at COMMIT, after callback and replay insert.
    db.execute_batch("CREATE TABLE fault_parent(id INTEGER PRIMARY KEY); CREATE TABLE fault_child(id INTEGER REFERENCES fault_parent(id) DEFERRABLE INITIALLY DEFERRED); CREATE TRIGGER fail_effect AFTER INSERT ON control_effects BEGIN INSERT INTO fault_child VALUES (99); END;").unwrap();
    let req = enqueue(1, "commit-command");
    assert!(
        m.dispatch_with_effects(
            &req,
            || 1000.0,
            || panic!(),
            |e| {
                e.publish("forwarding-fixture", None, &json!({"pending":true}))?;
                e.enqueue(
                    "effect-1",
                    &json!({"action":"stop_reader"}),
                    budgets::Entries::new(1),
                )
            }
        )
        .is_err()
    );
    for table in [
        "managed_commands",
        "managed_auth_requests",
        "control_high_water",
        "control_state",
        "control_effects",
    ] {
        assert_eq!(
            db.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r
                .get::<_, u64>(0))
                .unwrap(),
            0,
            "partial write in {table}"
        );
    }
    db.execute_batch("DROP TRIGGER fail_effect").unwrap();
    m.dispatch_with_effects(
        &req,
        || 1000.0,
        || panic!(),
        |e| {
            e.enqueue(
                "effect-1",
                &json!({"action":"stop_reader"}),
                budgets::Entries::new(1),
            )
        },
    )
    .unwrap();
    assert_eq!(
        m.pending_effects(budgets::Entries::new(1)).unwrap().len(),
        1
    );
}

#[test]
fn run_retirement_is_atomic_origin_preserving_and_not_cleanup_or_terminality() {
    let t = Temp::new();
    let mut m = open(&t);
    m.bind_member(&binding(), &member().public_key_record(), 1300.0)
        .unwrap();
    dispatch(&mut m, &enqueue(1, "retire-one"));
    let db = rusqlite::Connection::open(t.0.join("state.db")).unwrap();
    let read = || {
        db.query_row(
            "SELECT retired_at FROM managed_commands WHERE command_id='retire-one'",
            [],
            |r| r.get::<_, Option<f64>>(0),
        )
        .unwrap()
    };
    let next = enqueue(2, "retire-two");
    assert!(
        m.dispatch_with_effects(
            &next,
            || 1001.0,
            || panic!(),
            |e| {
                e.finish_run("run-1", "deployment-1")?;
                Err(control_runtime::Error::Refused("injected_after_retirement"))
            }
        )
        .is_err()
    );
    assert_eq!(read(), None);
    m.dispatch_with_effects(
        &next,
        || 1002.0,
        || panic!(),
        |e| {
            assert_eq!(e.response()["state"], "pending");
            assert_eq!(e.finish_run("run-1", "deployment-1")?, 2);
            e.enqueue(
                "retirement-effect",
                &json!({"retired":true}),
                budgets::Entries::new(1),
            )
        },
    )
    .unwrap();
    assert_eq!(read(), Some(1002.0));
    m.finish_run("run-1", "deployment-1", || 1003.0).unwrap();
    assert_eq!(read(), Some(1002.0));
    assert_eq!(
        db.query_row(
            "SELECT state FROM managed_commands WHERE command_id='retire-one'",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        "pending"
    );
    assert_eq!(
        m.finish_run("run-other", "deployment-1", || 1004.0)
            .unwrap(),
        0
    );
}
#[test]
fn transaction_failure_rolls_back_claim_and_replay_and_clock_is_under_lock() {
    let t = Temp::new();
    let mut m = open(&t);
    m.bind_member(&binding(), &member().public_key_record(), 1300.0)
        .unwrap();
    dispatch(&mut m, &enqueue(1, "command-1"));
    let db = rusqlite::Connection::open(t.0.join("state.db")).unwrap();
    db.execute_batch("CREATE TRIGGER fail_receipt BEFORE INSERT ON managed_auth_requests WHEN NEW.protocol='mycelium.managed_control.target_poll.v1' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    let p = request("target_poll.v1", 1, json!({}));
    assert!(
        m.dispatch(&p, || 1000.0, || Ok("claim-fixture".into()))
            .is_err()
    );
    assert_eq!(
        db.query_row("SELECT state FROM managed_commands", [], |r| r
            .get::<_, String>(0))
            .unwrap(),
        "pending"
    );
    db.execute_batch("DROP TRIGGER fail_receipt").unwrap();
    let reply = m
        .dispatch(
            &p,
            || {
                db.busy_timeout(std::time::Duration::ZERO).unwrap();
                assert!(db.execute_batch("BEGIN IMMEDIATE").is_err());
                1000.0
            },
            || Ok("claim-fixture".into()),
        )
        .unwrap();
    assert_eq!(body(&reply)["delivery_kind"], "command");
    let bad = request(
        "target_ack.v1",
        2,
        json!({"command_id":"command-1","claim_token":"foreign","disposition":"reconciliation_required","native_response":null}),
    );
    assert!(m.dispatch(&bad, || 1000.0, || panic!()).is_err());
}
