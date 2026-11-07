// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "iroh-loopback")]
//! Signed NodeControl sessions: L9 carries raw bytes, so signature, run binding
//! and replay refusal are this service's job and are exercised here directly.
use node_service::control::{Ids, Session};
use protocol::identity::Identity;
use serde_json::json;

fn ids() -> Ids {
    Ids {
        request_id: "request-1".into(),
        generation: 7,
    }
}
fn keys() -> (Identity, Identity) {
    (
        Identity::from_secret("owner-endpoint", &[1; 32]).unwrap(),
        Identity::from_secret("member-endpoint", &[2; 32]).unwrap(),
    )
}
/// Child registers (empty run identity), owner answers; both ends bound afterwards.
fn registered() -> (Session, Session, Identity, Identity) {
    let (owner, member) = keys();
    let mut owner_side = Session::owner(ids(), member.public_key_record());
    let mut member_side = Session::child(owner.public_key_record());
    let (seq, bytes) = member_side
        .call_frame(&member, "register", json!({"pid": 1}))
        .unwrap();
    let (got, kind, _) = owner_side.accept_call(&bytes).unwrap();
    assert_eq!((got, kind.as_str()), (0, "register"));
    let reply = owner_side
        .reply(&owner, "register", seq, json!({"ok": true}))
        .unwrap();
    member_side
        .accept_reply(&reply, seq, "register")
        .expect("signed owner reply binds the child to the run");
    assert_eq!(member_side.ids(), &ids());
    (owner_side, member_side, owner, member)
}

#[test]
fn replayed_command_is_refused() {
    let (mut owner_side, mut member_side, _, member) = registered();
    let (_, hello) = member_side.call_frame(&member, "hello", json!({})).unwrap();
    owner_side.accept_call(&hello).unwrap();
    assert_eq!(
        owner_side.accept_call(&hello).unwrap_err().to_string(),
        "control_sequence"
    );
}

#[test]
fn command_signed_by_an_unpinned_key_is_refused() {
    let (mut owner_side, mut member_side, _, _) = registered();
    let intruder = Identity::from_secret("intruder", &[9; 32]).unwrap();
    let (_, forged) = member_side
        .call_frame(&intruder, "hello", json!({}))
        .unwrap();
    assert!(owner_side.accept_call(&forged).is_err());
}

#[test]
fn tampered_body_fails_the_signature() {
    let (mut owner_side, mut member_side, _, member) = registered();
    let (_, bytes) = member_side
        .call_frame(&member, "hello", json!({"pid": 1}))
        .unwrap();
    let mut value: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
    value["body"]["pid"] = json!(2);
    assert!(
        owner_side
            .accept_call(&serde_json::to_vec(&value).unwrap())
            .is_err()
    );
}

#[test]
fn frame_from_another_run_or_generation_is_refused() {
    let (owner, member) = keys();
    let mut owner_side = Session::owner(ids(), member.public_key_record());
    // A child bound to a different run (it adopted ids from a different owner reply).
    let mut other_owner = Session::owner(
        Ids {
            request_id: "request-2".into(),
            generation: 7,
        },
        member.public_key_record(),
    );
    let mut stranger = Session::child(owner.public_key_record());
    let (seq, bytes) = stranger.call_frame(&member, "register", json!({})).unwrap();
    other_owner.accept_call(&bytes).unwrap();
    let reply = other_owner
        .reply(&owner, "register", seq, json!({}))
        .unwrap();
    stranger.accept_reply(&reply, seq, "register").unwrap();
    let (_, hello) = stranger.call_frame(&member, "hello", json!({})).unwrap();
    // The first register was never presented to `owner_side`, so sequence also fails;
    // the run binding is what must be reported for a well-ordered stranger frame.
    let (_, register) = Session::child(owner.public_key_record())
        .call_frame(&member, "register", json!({}))
        .unwrap();
    owner_side.accept_call(&register).unwrap();
    assert_eq!(
        owner_side.accept_call(&hello).unwrap_err().to_string(),
        "control_run_binding"
    );
}

#[test]
fn reply_for_a_different_call_or_kind_is_refused() {
    let (mut owner_side, mut member_side, owner, member) = registered();
    let (seq, bytes) = member_side.call_frame(&member, "hello", json!({})).unwrap();
    owner_side.accept_call(&bytes).unwrap();
    let reply = owner_side.reply(&owner, "hello", seq, json!({})).unwrap();
    assert!(member_side.accept_reply(&reply, seq + 1, "hello").is_err());
    assert!(member_side.accept_reply(&reply, seq, "plan").is_err());
    member_side.accept_reply(&reply, seq, "hello").unwrap();
}

#[test]
fn owner_reply_signed_by_a_foreign_key_does_not_bind_a_child() {
    let (owner, member) = keys();
    let mut child = Session::child(owner.public_key_record());
    let mut owner_side = Session::owner(ids(), member.public_key_record());
    let (seq, bytes) = child.call_frame(&member, "register", json!({})).unwrap();
    owner_side.accept_call(&bytes).unwrap();
    let foreign = Identity::from_secret("foreign", &[5; 32]).unwrap();
    let reply = owner_side
        .reply(&foreign, "register", seq, json!({}))
        .unwrap();
    assert!(child.accept_reply(&reply, seq, "register").is_err());
    assert_eq!(child.ids(), &Ids::default());
}
