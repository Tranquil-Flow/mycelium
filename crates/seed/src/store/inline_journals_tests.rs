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
