// SPDX-License-Identifier: AGPL-3.0-only
use budgets::WallExpiry;
use node_service::{enrollment::*, receipts, service};
use serde_json::json;
use std::os::unix::fs::PermissionsExt;
fn root() -> std::path::PathBuf {
    let p = std::env::temp_dir()
        .canonicalize()
        .unwrap()
        .join(format!("slice-identity-{}", service::random_id().unwrap()));
    std::fs::create_dir(&p).unwrap();
    std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o700)).unwrap();
    p
}
#[test]
fn persistent_identity_is_v014_raw_key_and_never_rotates_on_reopen() {
    let root = root();
    let a = identity(&root).unwrap();
    let b = identity(&root).unwrap();
    assert_eq!(a.public_key_record(), b.public_key_record());
    assert_eq!(
        a.endpoint_id(),
        format!("node-identity-{}", &a.verification_key_digest()[7..39])
    );
    assert_eq!(
        std::fs::read(root.join("node.key")).unwrap(),
        a.secret_bytes().as_slice()
    );
    assert_eq!(
        std::fs::metadata(root.join("node.key"))
            .unwrap()
            .permissions()
            .mode()
            & 0o777,
        0o600
    );
    std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn identity_refuses_symlinks_corruption_and_public_permissions() {
    let root = root();
    std::fs::write(root.join("foreign"), b"not-a-key").unwrap();
    std::os::unix::fs::symlink(root.join("foreign"), root.join("node.key")).unwrap();
    assert!(identity(&root).is_err());
    std::fs::remove_file(root.join("node.key")).unwrap();
    std::fs::write(root.join("node.key"), b"short").unwrap();
    std::fs::set_permissions(
        root.join("node.key"),
        std::fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    assert!(identity(&root).is_err());
    std::fs::remove_file(root.join("node.key")).unwrap();
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o755)).unwrap();
    assert!(identity(&root).is_err());
    std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn local_consent_is_explicit_persisted_and_acceptance_is_request_bound() {
    let root = root();
    let owner = receipts::identity("owner").unwrap();
    let member = identity(&root).unwrap();
    let invitation = Invitation {
        swarm: "swarm".into(),
        owner_node: "owner".into(),
        owner_endpoint: owner.endpoint_id().into(),
        owner_key: owner.public_key_record(),
        member_node: "member".into(),
        member_key: member.public_key_record(),
        nonce: "invite-nonce".into(),
    };
    let caps = node::Caps {
        storage_bytes: 1,
        memory_bytes: 1,
        wall_ms: 1,
        commands: 1,
    };
    let now = WallExpiry::from_unix_millis(0);
    assert!(LocalMembership::load(&root).is_err());
    assert!(LocalMembership::approve(&root, invitation.clone(), caps.clone(), false, now).is_err());
    assert!(!root.join("membership-choice.json").exists());
    let recorded =
        LocalMembership::approve(&root, invitation.clone(), caps.clone(), true, now).unwrap();
    let loaded = LocalMembership::load(&root).unwrap();
    assert_eq!(loaded.consent.caps, recorded.consent.caps);
    assert!(LocalMembership::approve(&root, invitation.clone(), caps, true, now).is_err());
    let request = join(&member, &invitation, "boot", "127.0.0.1:12345", now).unwrap();
    let acceptance = accept(&owner, &invitation, &request, 1, now).unwrap();
    loaded
        .verify_acceptance(&request, &acceptance, now)
        .unwrap();
    for field in [
        "request_message_id",
        "accepted_incarnation",
        "accepted_node_id",
    ] {
        let mut bad = acceptance["message"].clone();
        bad[field] = json!("wrong");
        assert!(
            loaded
                .verify_acceptance(&request, &signed(&owner, bad).unwrap(), now)
                .is_err(),
            "accepted wrong {field}"
        );
    }
    std::fs::remove_dir_all(root).unwrap();
}
#[test]
fn owner_verifies_join_signature_nonce_and_audience_before_acceptance() {
    let owner = receipts::identity("owner").unwrap();
    let member = receipts::identity("member").unwrap();
    let invite = Invitation {
        swarm: "swarm".into(),
        owner_node: "owner".into(),
        owner_endpoint: owner.endpoint_id().into(),
        owner_key: owner.public_key_record(),
        member_node: "member".into(),
        member_key: member.public_key_record(),
        nonce: "invite-nonce".into(),
    };
    let now = WallExpiry::from_unix_millis(0);
    let request = join(&member, &invite, "boot", "127.0.0.1:12345", now).unwrap();
    let accepted = accept(&owner, &invite, &request, 1, now).unwrap();
    assert_eq!(
        accepted["message"]["request_message_id"],
        request["message"]["message_id"]
    );
    for field in ["invite_nonce", "recipient_node_id", "incarnation"] {
        let mut bad = request.clone();
        bad["message"][field] = json!("tampered");
        assert!(
            accept(&owner, &invite, &bad, 1, now).is_err(),
            "accepted tampered {field}"
        );
    }
    let mut wrong_nonce = request["message"].clone();
    wrong_nonce["invite_nonce"] = json!("other");
    assert!(
        accept(
            &owner,
            &invite,
            &signed(&member, wrong_nonce).unwrap(),
            1,
            now
        )
        .is_err()
    );
    // v0.1.4 joins are self-attested (executed: SeedCoordinator.accept_join accepts a join signed by a
    // key the invitation never named, binding that key): a stranger's validly signed join with the
    // right swarm, recipient and nonce is accepted, and the acceptance is addressed to it.
    let stranger = receipts::identity("stranger").unwrap();
    let theirs = join(&stranger, &invite, "boot", "127.0.0.1:12345", now).unwrap();
    let accepted = accept(&owner, &invite, &theirs, 1, now).unwrap();
    assert_eq!(
        accepted["message"]["request_message_id"],
        theirs["message"]["message_id"]
    );
    assert_eq!(
        accepted["message"]["accepted_node_id"],
        theirs["message"]["sender_node_id"]
    );
    assert_eq!(
        accepted["message"]["recipient_node_id"],
        theirs["message"]["sender_node_id"]
    );
}
