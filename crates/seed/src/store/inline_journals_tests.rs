// SPDX-License-Identifier: AGPL-3.0-only
//! Lane SN (review SN-01): [`Store::load_table`] reads the journal rows and the snapshot row in one read
//! transaction, and adds the journals a marked snapshot row carries inline (an rv44 writer committed between this
//! build's migration and its load) by the same union rule as [`install_journals`].
use super::*;
use serde_json::json;

fn renewal(id: &str, digest: &str) -> Value {
    json!({
        "node_id": "node-a",
        "generation": 1,
        "heartbeat_message_id": id,
        "request_digest": digest,
        "sequence": 1,
        "renewal": {"heartbeat_message_id": id},
    })
}

fn open(db: &Path) -> Store {
    Store::open_with(
        db,
        &Binding {
            swarm_id: "swarm-fixture",
            seed_node_id: "seed-node",
            seed_key_digest: "sha256:seed-key",
        },
        None,
    )
    .unwrap()
}

fn write_row(store: &Store, document: &Value) {
    store
        .connection
        .execute(
            "INSERT OR REPLACE INTO rs_seed_table (id, snapshot) VALUES (1, ?)",
            [document.to_string()],
        )
        .unwrap();
}

/// Exercise the storage boundary with a real signed fixture envelope. Unrelated envelopes must not authorize
/// a conflict; the exact (kind, node, generation, heartbeat) key must. Readers never normalize on disk.
fn committed_row_wins(mode: &str) {
    let dir = std::env::temp_dir().join(format!("lane-sn-committed-{mode}-{}", std::process::id()));
    if dir.exists() {
        fs::remove_dir_all(&dir).unwrap();
    }
    fs::DirBuilder::new()
        .mode(OWNER_DIR_MODE)
        .create(&dir)
        .unwrap();
    let dir = fs::canonicalize(&dir).unwrap();
    let mut store = open(&dir.join("seed.sqlite"));
    let message: Value = serde_json::from_str(include_str!(
        "../../../protocol/fixtures/contracts/membership-lease-renewal-v1.json"
    ))
    .unwrap();
    let signed = protocol::membership::sign(
        &protocol::identity::Identity::from_secret("endpoint-seed", &[9; 32]).unwrap(),
        &message,
        crate::limits().unwrap(),
    )
    .unwrap();
    let mut rn = renewal("heartbeat-fixture", "same-request");
    rn["renewal"] = message;
    let mut ro = rn.clone();
    ro["renewal"]["message_id"] = json!("provisional-id");
    let core = SeedTable::new(Box::new(String::new)).core_snapshot();
    let saved = Journal {
        renewals: vec![rn.clone()],
        emitted: vec!["lease_renewal-fixture".into()],
    };
    store.commit(&core, &core, &saved, None, &[]).unwrap();
    let mut hybrid = core.clone();
    // A row before the conflict catches a non-atomic migration or rotation (partial writes must roll back).
    hybrid["renewals"] = json!([renewal("new-inline", "new-request"), ro]);
    hybrid["emitted"] = json!(["provisional-id"]);
    write_row(&store, &hybrid);
    let empty = Journal {
        renewals: Vec::new(),
        emitted: Vec::new(),
    };
    let read_state = |store: &Store| {
        let snapshot = read_snapshot(&store.connection).unwrap();
        let rows: Vec<String> = store
            .connection
            .prepare("SELECT row FROM rs_seed_renewals ORDER BY ordinal")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .map(Result::unwrap)
            .collect();
        let emitted: Vec<String> = store
            .connection
            .prepare("SELECT message_id FROM rs_seed_emitted ORDER BY message_id")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .map(Result::unwrap)
            .collect();
        (
            snapshot,
            rows,
            emitted,
            store.seed_key_digest().unwrap(),
            v014::authority_generation(&store.connection).unwrap(),
        )
    };
    for (kind, key) in [
        ("join", "node-a|1|heartbeat-fixture"),
        ("renewal", "node-b|1|heartbeat-fixture"),
        ("renewal", "node-a|2|heartbeat-fixture"),
        ("renewal", "node-a|1|other-heartbeat"),
    ] {
        store
            .connection
            .execute(
                "INSERT INTO rs_seed_envelopes (kind, key, envelope) VALUES (?, ?, ?)",
                params![kind, key, signed.to_string()],
            )
            .unwrap();
    }
    let before = read_state(&store);
    let refused = match mode {
        "load" => store.load_table().map(|_| ()),
        "rotation" => store.commit_rotation_completion(&core, &empty, "new-key", 2),
        "commit" => store.commit(&core, &core, &empty, None, &[]).map(|_| ()),
        _ => panic!("unknown mode"),
    };
    assert_eq!(refused.unwrap_err().code, "seed_state_corrupt");
    assert_eq!(
        read_state(&store),
        before,
        "no exact committed envelope: nothing written"
    );
    store.connection.execute(
        "INSERT INTO rs_seed_envelopes (kind, key, envelope) VALUES ('renewal', 'node-a|1|heartbeat-fixture', ?)",
        [signed.to_string()],
    ).unwrap();
    if mode == "rotation" {
        store
            .commit_rotation_completion(&core, &empty, "new-key", 2)
            .expect("rotation normalization keeps the committed row");
        assert_eq!(store.seed_key_digest().unwrap(), "new-key");
        assert_eq!(
            v014::authority_generation(&store.connection).unwrap(),
            Some(2)
        );
    } else if mode == "commit" {
        store
            .commit(&core, &core, &empty, None, &[])
            .expect("commit normalization keeps the committed row");
    }
    let table = store
        .load_table()
        .expect("the in-memory union keeps the committed row")
        .unwrap();
    assert_eq!(table.core, core);
    assert_eq!(table.renewals, [rn, renewal("new-inline", "new-request")]);
    let mut emitted = table.emitted;
    emitted.sort();
    assert_eq!(emitted, ["lease_renewal-fixture", "provisional-id"]);
    if mode == "load" {
        assert_eq!(
            read_state(&store),
            before,
            "the in-memory union makes no writes"
        );
    } else {
        assert_eq!(read_snapshot(&store.connection).unwrap(), Some(core));
        assert_eq!(
            read_state(&store).1[0],
            before.1[0],
            "stored Rn bytes unchanged"
        );
    }
    assert_eq!(
        store
            .load_envelope("renewal", "node-a|1|heartbeat-fixture")
            .unwrap(),
        Some(signed)
    );
    drop(store);
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn load_table_keeps_the_committed_row_only_with_its_exact_envelope() {
    committed_row_wins("load");
}

#[test]
fn rotation_keeps_the_committed_row_only_with_its_exact_envelope() {
    committed_row_wins("rotation");
}

#[test]
fn commit_keeps_the_committed_row_only_with_its_exact_envelope() {
    committed_row_wins("commit");
}

#[test]
fn load_table_adds_the_inline_rows_of_a_marked_row_as_a_union() {
    let dir = std::env::temp_dir().join(format!("lane-sn-inline-{}", std::process::id()));
    if dir.exists() {
        fs::remove_dir_all(&dir).unwrap();
    }
    fs::DirBuilder::new()
        .mode(OWNER_DIR_MODE)
        .create(&dir)
        .unwrap();
    let dir = fs::canonicalize(&dir).unwrap();
    let db = dir.join("seed.sqlite");
    let store = open(&db);
    let mut legacy = SeedTable::new(Box::new(String::new)).snapshot();
    legacy["renewals"] = json!([renewal("h1", "d1"), renewal("h2", "d2")]);
    legacy["emitted"] = json!(["a", "b"]);
    write_row(&store, &legacy);
    drop(store);
    let store = open(&db);
    let core = store.load_core().unwrap().unwrap();
    assert_eq!(core[JOURNALS_KEY], JOURNALS_ROWS_V1, "migrated at open");

    let mut hybrid = core.clone();
    hybrid["renewals"] = json!([renewal("h2", "d2"), renewal("h3", "d3")]);
    hybrid["emitted"] = json!(["b", "c"]);
    write_row(&store, &hybrid);
    assert_eq!(
        store.load_core().unwrap().unwrap(),
        core,
        "core readers take the row without the inline journals"
    );
    let table = store.load_table().unwrap().unwrap();
    assert_eq!(table.core, core);
    assert_eq!(
        table.renewals,
        [
            renewal("h1", "d1"),
            renewal("h2", "d2"),
            renewal("h3", "d3")
        ]
    );
    let mut emitted = table.emitted.clone();
    emitted.sort();
    assert_eq!(emitted, ["a", "b", "c"]);

    hybrid["renewals"] = json!([renewal("h2", "another digest")]);
    write_row(&store, &hybrid);
    assert_eq!(
        store.load_table().err().map(|e| e.code),
        Some("seed_state_corrupt".to_owned())
    );
    drop(store);
    fs::remove_dir_all(&dir).unwrap();
}
