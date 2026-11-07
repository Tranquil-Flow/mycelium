// SPDX-License-Identifier: AGPL-3.0-only
use super::*;

#[test]
fn live_capacity_refuses_new_work_without_evicting_signed_retry() {
    let mut db = Mailbox::default();
    let capacity = count(B::ReplayCapacity).unwrap();
    for seq in 1..=capacity {
        assert!(db.admit(seq, seq, seq, 0, Mode::Corrected).unwrap());
    }
    assert!(
        !db.admit(capacity + 1, capacity + 1, capacity + 1, 0, Mode::Corrected)
            .unwrap()
    );
    assert_eq!(db.rows.len(), 8192);
    assert_eq!(db.high_water, capacity);
    assert!(db.retry(1, 1, 1, 0));
    assert_eq!(db.pruned, 0);
    let image = serde_json::to_vec(&db).unwrap();
    let reopened: Mailbox = serde_json::from_slice(&image).unwrap();
    assert!(reopened.retry(1, 1, 1, 0));
    assert_eq!(reopened.high_water, capacity);
}

#[test]
fn prune_is_strictly_age_bounded_and_bloom_survives_reopen() {
    let mut db = Mailbox::default();
    let keep = count(B::ReplayKeep).unwrap();
    let batch = count(B::RetentionBatch).unwrap();
    for seq in 1..=(keep + batch) {
        assert!(db.admit(seq, seq, seq, 0, Mode::Corrected).unwrap());
    }
    let expiry = ms(B::ControlTtl).unwrap() + ms(B::ControlClockSkew).unwrap();
    assert_eq!(db.prune(expiry).unwrap(), 0);
    assert_eq!(db.prune(expiry + ms(B::OwnerPoll).unwrap()).unwrap(), 128);
    assert_eq!(db.rows.len(), 256);
    assert!(!db.nonces.contains_key(&1));
    let image = serde_json::to_vec(&db).unwrap();
    let mut reopened: Mailbox = serde_json::from_slice(&image).unwrap();
    let next = keep + batch + 1;
    assert!(
        !reopened
            .admit(next, 1, next, expiry, Mode::Corrected)
            .unwrap()
    );
    assert!(
        !reopened
            .admit(next, next, 1, expiry, Mode::Corrected)
            .unwrap()
    );
    assert!(
        reopened
            .admit(next, next, next, expiry, Mode::Corrected)
            .unwrap()
    );
    // Discriminator: clearing the history (not the high-water) admits reuse at
    // a higher sequence. This proves the old-nonce test isn't only a sequence test.
    reopened.history.fill(0);
    assert!(
        reopened
            .admit(next + 1, 1, next + 1, expiry, Mode::Corrected)
            .unwrap()
    );
}

#[test]
fn retired_command_pruning_keeps_the_declared_batch_bound() {
    let mut db = Mailbox::default();
    let batch = count(B::RetentionBatch).unwrap();
    for id in 0..=batch {
        db.commands.insert(
            id,
            CommandRow {
                terminal: true,
                retired_at: Some(0),
                updated_at: 0,
                claimed_request: None,
            },
        );
    }
    db.prune(ms(B::RetiredCommandAge).unwrap() + ms(B::OwnerPoll).unwrap())
        .unwrap();
    assert_eq!(
        db.commands.len(),
        1,
        "one transaction may retire at most 128 commands"
    );
    db.prune(ms(B::RetiredCommandAge).unwrap() + ms(B::OwnerPoll).unwrap())
        .unwrap();
    assert!(db.commands.is_empty());
}

#[test]
fn retired_age_is_not_cleanup_proof_and_claimed_auth_survives_prune() {
    let mut db = Mailbox::default();
    let rows = count(B::ReplayKeep).unwrap() + count(B::RetentionBatch).unwrap();
    for seq in 1..=rows {
        assert!(db.admit(seq, seq, seq, 0, Mode::Corrected).unwrap());
    }
    db.commands.insert(
        1,
        CommandRow {
            terminal: false,
            retired_at: Some(0),
            updated_at: 0,
            claimed_request: Some(1),
        },
    );
    db.commands.insert(
        2,
        CommandRow {
            terminal: true,
            retired_at: Some(0),
            updated_at: 0,
            claimed_request: None,
        },
    );
    let age = ms(B::RetiredCommandAge).unwrap();
    db.prune(age).unwrap();
    assert!(db.commands.contains_key(&2));
    db.prune(age + ms(B::OwnerPoll).unwrap()).unwrap();
    assert!(!db.commands.contains_key(&2));
    assert!(db.commands.contains_key(&1));
    assert!(db.rows.contains_key(&1));
}
