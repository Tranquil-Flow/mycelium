// SPDX-License-Identifier: AGPL-3.0-only
//! Lane SN measurement (ignored, manual): the per-member-message persistence steps of the seed store timed on a
//! COPY of a real owner seed database. `LANE_SN_REAL_SEED_COPY=<path to a copy>`; the file is copied again into a
//! private scratch directory, so the given file is only read. Run in release for app-like figures:
//! `cargo test --release -p seed --lib -- --ignored --nocapture real_copy`.
use super::*;
use serde_json::json;
use std::time::Instant;

fn private_copy(source: &Path) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!("lane-sn-real-{}", std::process::id()));
    fs::DirBuilder::new()
        .mode(OWNER_DIR_MODE)
        .recursive(true)
        .create(&dir)
        .unwrap();
    let dir = fs::canonicalize(&dir).unwrap();
    fs::set_permissions(&dir, fs::Permissions::from_mode(OWNER_DIR_MODE)).unwrap();
    let target = dir.join("seed.sqlite");
    fs::copy(source, &target).unwrap();
    fs::set_permissions(&target, fs::Permissions::from_mode(OWNER_FILE_MODE)).unwrap();
    target
}

fn ms(started: Instant) -> u128 {
    started.elapsed().as_millis()
}

#[test]
#[ignore = "manual measurement on a copy of a real seed database (LANE_SN_REAL_SEED_COPY)"]
fn real_copy_per_message_persistence_cost() {
    let Ok(source) = std::env::var("LANE_SN_REAL_SEED_COPY") else {
        eprintln!("lane-sn real copy: LANE_SN_REAL_SEED_COPY unset, nothing measured");
        return;
    };
    let database = private_copy(Path::new(&source));
    let bound = read_binding(&database).unwrap();
    let started = Instant::now();
    let mut store = Store::open_with(
        &database,
        &Binding {
            swarm_id: &bound.swarm_id,
            seed_node_id: &bound.seed_node_id,
            seed_key_digest: &bound.seed_key_digest,
        },
        None,
    )
    .unwrap();
    eprintln!("lane-sn real copy: open {} ms", ms(started));
    for round in 0..3 {
        // `durable_view`: the whole snapshot read and parsed for the member check.
        let started = Instant::now();
        let durable = store.load_snapshot().unwrap().unwrap();
        let load = ms(started);
        let table = SeedTable::restore(&durable, Box::new(String::new)).unwrap();
        // `persist`: the table serialized to a document, then the commit (read + parse + compare + write).
        let started = Instant::now();
        let mut snapshot = table.snapshot();
        let snap = ms(started);
        let mut row = snapshot["renewals"][0].clone();
        row["heartbeat_message_id"] = json!(format!("lane-sn-measure-{round}"));
        snapshot["renewals"].as_array_mut().unwrap().push(row);
        snapshot["emitted"]
            .as_array_mut()
            .unwrap()
            .push(json!(format!("lane-sn-measure-{round}")));
        let started = Instant::now();
        store.commit(&snapshot, &durable, None, &[]).unwrap();
        let commit = ms(started);
        // `with_current_member`: the snapshot read, parsed and restored as a whole table per call.
        let started = Instant::now();
        store
            .with_authority(|s| {
                SeedTable::restore(&s.unwrap(), Box::new(String::new)).unwrap();
                Ok(())
            })
            .unwrap();
        let guard = ms(started);
        eprintln!(
            "lane-sn real copy round {round}: durable_view load {load} ms, table.snapshot {snap} ms, commit {commit} ms \
             (per message under the lock ~{} ms); with_current_member read {guard} ms; snapshot row {} bytes",
            load + snap + commit,
            snapshot.to_string().len()
        );
    }
    fs::remove_dir_all(database.parent().unwrap()).unwrap();
}
