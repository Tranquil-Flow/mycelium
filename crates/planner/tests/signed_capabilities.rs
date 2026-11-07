// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{BudgetId as B, Entries, WallExpiry};
use planner::{canonical, discovery::*, placement::Caps};
use protocol::identity::Identity as SigningIdentity;
use serde_json::{Value, json};

fn fixture() -> (SigningIdentity, MemberIdentity, Value) {
    let key = SigningIdentity::from_secret(&"a".repeat(64), &[7; 32]).unwrap();
    let id = MemberIdentity {
        node_id: "member".into(),
        generation: 2,
        incarnation: "boot-2".into(),
        endpoint_id: key.endpoint_id().into(),
        verification_key_digest: key.verification_key_digest(),
    };
    let message = json!({"protocol":"mycelium.membership.capability_report.v1","message_id":"report-1","swarm_id":"swarm","sender_node_id":id.node_id,"sender_endpoint_id":id.endpoint_id,"recipient_node_id":"owner","incarnation":id.incarnation,"generation":id.generation,"issued_at":0,"expires_at":B::MemberChosenWall.span(Entries::ZERO).unwrap().millis()/1000,"platform":"macos","architecture":"arm64","memory_bytes":16*1024*1024*1024u64,"available_storage_bytes":8_000_000_000u64,"backends":["mlx"],"precisions":["float32"]});
    (key, id, message)
}
fn envelope(key: &SigningIdentity, m: &Value) -> Vec<u8> {
    serde_json::to_vec(&json!({"protocol":protocol::membership::SIGNED_MESSAGE_PROTOCOL,"message":m,"signature":key.sign(m).unwrap(),"verification_key":key.public_key_record()})).unwrap()
}
fn verifier() -> MembershipVerifier<'static> {
    MembershipVerifier {
        swarm_id: "swarm",
        recipient_node_id: "owner",
        caps: Caps {
            storage_bytes: Some(2_000_000_000),
            max_memory_bytes: Some(4 * 1024 * 1024 * 1024),
            max_wall_seconds: Some(600),
        },
        backend: "mlx",
        precision: "float32",
        maximum_bytes: 16_384,
    }
}
#[test]
fn real_l5_signature_and_all_current_identity_pins_are_required() {
    let (key, id, m) = fixture();
    let v = verifier();
    let report = v
        .verify(&envelope(&key, &m), &id, WallExpiry::from_unix_millis(0))
        .unwrap();
    assert_eq!(report.identity, id);
    assert_eq!(report.member.memory_bytes, 16 * 1024 * 1024 * 1024);
    assert_eq!(
        report.member.caps.unwrap().storage_bytes,
        Some(2_000_000_000)
    );
    for field in [
        "swarm_id",
        "recipient_node_id",
        "sender_node_id",
        "sender_endpoint_id",
        "incarnation",
        "generation",
        "backends",
        "precisions",
    ] {
        let mut bad = m.clone();
        bad[field] = match field {
            "generation" => json!(3),
            "backends" | "precisions" => json!(["foreign"]),
            _ => json!("foreign"),
        };
        assert!(
            v.verify(&envelope(&key, &bad), &id, WallExpiry::from_unix_millis(0))
                .is_err(),
            "{field}"
        );
    }
    let mut tampered: Value = serde_json::from_slice(&envelope(&key, &m)).unwrap();
    tampered["message"]["memory_bytes"] = json!(99);
    assert_eq!(
        v.verify(
            &serde_json::to_vec(&tampered).unwrap(),
            &id,
            WallExpiry::from_unix_millis(0)
        )
        .unwrap_err()
        .code,
        "membership_signature_invalid"
    );
    let foreign = SigningIdentity::from_secret(&"a".repeat(64), &[8; 32]).unwrap();
    assert!(
        v.verify(
            &envelope(&foreign, &m),
            &id,
            WallExpiry::from_unix_millis(0)
        )
        .is_err()
    );
    assert!(
        v.verify(
            &envelope(&key, &m),
            &id,
            WallExpiry::from_unix_millis(600_000)
        )
        .is_err()
    );
}
#[test]
fn strict_ingress_and_submillisecond_expiry_never_extend_report() {
    let (key, id, mut m) = fixture();
    let mut v = verifier();
    let raw = envelope(&key, &m);
    v.maximum_bytes = raw.len() - 1;
    assert!(
        v.verify(&raw, &id, WallExpiry::from_unix_millis(0))
            .is_err()
    );
    v.maximum_bytes = raw.len();
    assert!(v.verify(&raw, &id, WallExpiry::from_unix_millis(0)).is_ok());
    let duplicate = String::from_utf8(raw).unwrap().replacen(
        "\"generation\":2",
        "\"generation\":2,\"generation\":2",
        1,
    );
    v.maximum_bytes = duplicate.len();
    assert!(
        v.verify(duplicate.as_bytes(), &id, WallExpiry::from_unix_millis(0))
            .is_err()
    );
    v.maximum_bytes = 16_384;
    m["expires_at"] = json!(0.0019);
    assert_eq!(
        v.verify(&envelope(&key, &m), &id, WallExpiry::from_unix_millis(0))
            .unwrap()
            .expires_at
            .unix_millis(),
        1
    );
    assert!(
        v.verify(&envelope(&key, &m), &id, WallExpiry::from_unix_millis(1))
            .is_err()
    );
}
#[test]
fn capability_time_policy_reads_the_exact_l1_declarations() {
    let (key, id, m) = fixture();
    let raw = envelope(&key, &m);
    let (result, reads) =
        budgets::reads::capture(|| verifier().verify(&raw, &id, WallExpiry::from_unix_millis(0)));
    result.unwrap();
    let ids: std::collections::BTreeSet<_> = reads.iter().map(|r| r.id).collect();
    assert_eq!(
        ids,
        std::collections::BTreeSet::from([
            B::PyMyceliumMembershipContractsMaxMessageTtlSecondsL30,
            B::PyMyceliumMembershipContractsMaxClockSkewSecondsL31
        ])
    );
}
#[test]
fn shared_canonical_identity_preserves_large_integer_and_float_tokens() {
    for source in [
        "123456789012345678901234567890",
        "{\"x\":1e-7,\"u\":\"é𝄞\"}",
        "-0.0",
    ] {
        let value: Value = serde_json::from_str(source).unwrap();
        assert_eq!(
            canonical(&value, true).unwrap().as_bytes(),
            protocol::canonical::to_ascii_bytes(&value).unwrap()
        );
        assert_eq!(
            canonical(&value, false).unwrap().as_bytes(),
            protocol::canonical::to_bytes(&value).unwrap()
        );
    }
}
