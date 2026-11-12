// SPDX-License-Identifier: AGPL-3.0-only
//! Already-joined admission: real seed/session, no second pairing and no native work.
#[path = "support/work_member.rs"]
mod fixture;
use api::{app_host, member_control::joined};
use protocol::identity::Identity;
use serde_json::Value;
fn document(p: &fixture::Joined) -> Value {
    let carrier = Identity::from_secret(
        &iroh::SecretKey::generate().public().to_string(),
        &p.signer.secret_bytes(),
    )
    .unwrap();
    let control = iroh::EndpointAddr::new(iroh::SecretKey::generate().public())
        .with_ip_addr("127.0.0.1:12345".parse().unwrap());
    joined::announce(
        &p.session,
        &p.signer,
        &carrier,
        &control,
        &p.announced(),
        app_host::now(),
    )
    .unwrap()
}
#[test]
fn an_existing_join_produces_a_signed_current_control_binding_without_pairing_again() {
    let p = fixture::Joined::new();
    let before = p.seed.coordinator.member(p.session.node_id()).unwrap();
    let d = document(&p);
    let result = joined::verify(
        &p.seed.coordinator,
        &d,
        p.owner.public(),
        "run-s4cb2",
        "dep-s4cb2",
        &|a| a.ip().is_loopback(),
        app_host::now(),
    );
    assert!(
        result.is_ok(),
        "already-joined adoption refused: {result:?}"
    );
    let verified = result.unwrap();
    assert_eq!(verified.node_id(), p.session.node_id());
    assert_eq!(verified.registration(), &d["message"]["registration"]);
    let mut after = p.seed.coordinator.member(p.session.node_id()).unwrap();
    let mut before = before;
    // member() computes elapsed liveness age on read; it is not stored seed state.
    assert!(
        after["liveness_age_seconds"].as_f64().unwrap()
            >= before["liveness_age_seconds"].as_f64().unwrap()
    );
    after
        .as_object_mut()
        .unwrap()
        .remove("liveness_age_seconds");
    before
        .as_object_mut()
        .unwrap()
        .remove("liveness_age_seconds");
    assert_eq!(
        after, before,
        "adoption must not pair, resume or rewrite the seed"
    );
}
#[test]
fn rendezvous_reaches_owner_over_existing_seed_tls_without_pairing() {
    let p = fixture::Joined::new();
    let d = document(&p);
    let result = p.client().post(stage_contract::member_work::PATH, &d);
    assert!(
        result.is_ok(),
        "signed rendezvous is not mounted: {result:?}"
    );
    let ack = result.unwrap();
    assert_eq!(
        ack["message"]["document_digest"],
        protocol::canonical::digest(&d).unwrap()
    );
    assert_eq!(
        ack["verification_key"],
        p.seed.coordinator.signer().public_key_record()
    );
    joined::inbox::publish(&p.client(), &d).unwrap();
    let received = p.seed.work_members.get(&p.seed.coordinator, p.session.node_id(), p.owner.public(),
        "run-s4cb2", "dep-s4cb2", &|a| a.ip().is_loopback()).unwrap();
    assert_eq!(received.registration(), &d["message"]["registration"]);
}
#[test]
fn stale_generation_and_wrong_signer_are_refused_before_adoption() {
    let mut p = fixture::Joined::new();
    let d = document(&p);
    let mut forged = d.clone();
    let stranger = Identity::from_secret(p.signer.endpoint_id(), &[41; 32]).unwrap();
    forged["signature"] = stranger.sign(&forged["message"]).unwrap();
    forged["verification_key"] = stranger.public_key_record();
    let check = |p: &fixture::Joined, d: &Value| {
        joined::verify(
            &p.seed.coordinator,
            d,
            p.owner.public(),
            "run-s4cb2",
            "dep-s4cb2",
            &|a| a.ip().is_loopback(),
            app_host::now(),
        )
    };
    assert_eq!(
        check(&p, &forged).unwrap_err().code,
        "member_control_reply_unenrolled"
    );
    p.resume();
    assert_eq!(
        check(&p, &d).unwrap_err().code,
        "joined_shard_member_changed"
    );
}
