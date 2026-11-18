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

/// Milliseconds since `started`, rounded to microseconds for printing.
fn ms(started: Instant) -> f64 {
    (started.elapsed().as_secs_f64() * 1_000_000.0).round() / 1000.0
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
    let before = read_snapshot(&Connection::open(&database).unwrap()).unwrap();
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
    eprintln!(
        "lane-sn real copy: open (incl. the one-time migration) {} ms",
        ms(started)
    );
    let started = Instant::now();
    let stored = store.load_table().unwrap().unwrap();
    let load_table = ms(started);
    let started = Instant::now();
    let mut table = SeedTable::restore_journaled(
        &stored.core,
        stored.renewals,
        stored.emitted,
        Box::new(String::new),
    )
    .unwrap();
    eprintln!(
        "lane-sn real copy: load_table {load_table} ms, restore_journaled {} ms",
        ms(started)
    );
    // Migration fidelity on real data: the table read back from the snapshot row and the journal rows is the
    // table the single snapshot held, row for row and in order.
    if let Some(before) = before.filter(|b| has_inline_journals(b)) {
        let started = Instant::now();
        assert_eq!(
            table.snapshot(),
            before,
            "the migrated table equals the single snapshot"
        );
        eprintln!(
            "lane-sn real copy: migrated table equals the single snapshot ({} renewals, {} emitted ids; checked in {} ms)",
            before["renewals"].as_array().map_or(0, Vec::len),
            before["emitted"].as_array().map_or(0, Vec::len),
            ms(started)
        );
    }
    let mut baseline = table.core_snapshot();
    assert_eq!(store.load_core().unwrap().unwrap(), baseline);
    let template = table.snapshot()["renewals"][0].clone();
    for round in 0..3 {
        // `durable_view`: the stored snapshot row read and parsed for the member check.
        let started = Instant::now();
        store.load_core().unwrap().unwrap();
        let load = ms(started);
        // `persist`: the snapshot row and this message's journal rows, then the commit.
        let started = Instant::now();
        let snapshot = table.core_snapshot();
        let snap = ms(started);
        let mut row = template.clone();
        row["heartbeat_message_id"] = json!(format!("lane-sn-measure-{round}"));
        let journal = Journal {
            renewals: vec![row],
            emitted: vec![format!("lane-sn-measure-{round}")],
        };
        let started = Instant::now();
        store
            .commit(&snapshot, &baseline, &journal, None, &[])
            .unwrap();
        let commit = ms(started);
        baseline = snapshot.clone();
        table.mark_journal_saved();
        // `with_current_member`: the snapshot row read, parsed and restored per call.
        let started = Instant::now();
        store
            .with_authority(|s| {
                SeedTable::restore(&s.unwrap(), Box::new(String::new)).unwrap();
                Ok(())
            })
            .unwrap();
        let guard = ms(started);
        eprintln!(
            "lane-sn real copy round {round}: durable_view load {load} ms, core_snapshot {snap} ms, commit {commit} ms \
             (per message under the lock ~{} ms); with_current_member read {guard} ms; snapshot row {} bytes",
            load + snap + commit,
            snapshot.to_string().len()
        );
    }
    fs::remove_dir_all(database.parent().unwrap()).unwrap();
}
