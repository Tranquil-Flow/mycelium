// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant, WallExpiry};
use node_service::{
    lifecycle::{self, Admission},
    receipts,
};
use serde_json::json;
fn admission() -> (Admission, node::Now) {
    let identity = receipts::identity("owner-endpoint").unwrap();
    let peer = receipts::identity("member-endpoint").unwrap();
    let now = node::Now {
        mono: MonoInstant::new(ClockDomain(1), 0),
        wall: WallExpiry::from_unix_millis(0),
    };
    let caps = node::Caps {
        storage_bytes: B::FreshStorageConsent.bytes().unwrap().get(),
        memory_bytes: B::FreshStorageConsent.bytes().unwrap().get(),
        wall_ms: B::MemberChosenWall.span(Entries::ZERO).unwrap().millis(),
        commands: 1,
    };
    let owner = node::Binding {
        swarm_id: "test-swarm".into(),
        node_id: "owner".into(),
        seed_key_digest: identity.verification_key_digest(),
    };
    let member = control::MemberBinding {
        target_node_id: "member".into(),
        target_endpoint_id: peer.endpoint_id().into(),
        target_key_digest: peer.verification_key_digest(),
        membership_generation: "1".into(),
        member_incarnation: "member-app".into(),
        run_id: "test".into(),
        deployment_id: "test".into(),
    };
    let statement = json!({"protocol":"mycelium.membership.join_acceptance.v1","message_id":"accept-1","swarm_id":"test-swarm","sender_node_id":"owner","sender_endpoint_id":identity.endpoint_id(),"recipient_node_id":"member","incarnation":"owner-app","generation":1,"issued_at":0.,"expires_at":B::MembershipTtl.span(Entries::ZERO).unwrap().millis() as f64/1000.,"request_message_id":"join-1","accepted_node_id":"member","accepted_incarnation":"member-app","membership_generation":1,"lease_expires_at":B::MembershipTtl.span(Entries::ZERO).unwrap().millis() as f64/1000.});
    let acceptance = json!({"protocol":protocol::membership::SIGNED_MESSAGE_PROTOCOL,"message":statement,"signature":identity.sign(&statement).unwrap(),"verification_key":identity.public_key_record()});
    (
        Admission {
            consent: Some(node::Consent {
                disclosure_digest: node::disclosure_digest(),
                caps: caps.clone(),
                binding: node::Binding {
                    node_id: "member".into(),
                    ..owner.clone()
                },
                granted_at: 0,
                withdrawn_at: None,
            }),
            owner,
            policy: caps,
            member,
            owner_key: identity.public_key_record(),
            acceptance,
        },
        now,
    )
}
#[test]
fn signed_owner_admission_and_explicit_consent_reach_real_l6_state() {
    let root = std::env::temp_dir().join(format!(
        "slice-core-{}-{}",
        std::process::id(),
        receipts::wall_ms().unwrap()
    ));
    std::fs::create_dir(&root).unwrap();
    let (setup, now) = admission();
    let core = lifecycle::open(&root.join("node.sqlite"), setup, now).unwrap();
    assert_eq!(core.state().phase, node::RunPhase::Idle);
    let member = &core.state().members["member"];
    assert!(member.consent.is_some());
    assert!(member.lease.is_some());
    assert_eq!(member.report_epoch, Some(core.state().epoch));
    drop(core);
    std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn no_consent_or_bad_signature_creates_no_owner_state() {
    let root = std::env::temp_dir().join(format!(
        "slice-refusal-{}-{}",
        std::process::id(),
        receipts::wall_ms().unwrap()
    ));
    std::fs::create_dir(&root).unwrap();
    for consent in [false, true] {
        let (mut setup, now) = admission();
        if !consent {
            setup.consent = None;
        }
        if consent {
            setup.acceptance["message"]["accepted_incarnation"] = json!("substituted");
        }
        let path = root.join(if consent {
            "bad-signature.sqlite"
        } else {
            "no-consent.sqlite"
        });
        assert!(lifecycle::open(&path, setup, now).is_err());
        assert!(!path.exists());
    }
    std::fs::remove_dir_all(root).unwrap();
}
