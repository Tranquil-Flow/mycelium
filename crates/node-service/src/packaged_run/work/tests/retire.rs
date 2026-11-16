// SPDX-License-Identifier: AGPL-3.0-only
//! Lane PHY item 1 (ledger rows 8, 81): the packaged owner retires its dead compile runs from its
//! storage ledger when a run ends (the chain's teardown: failed step, Stop, Quit) and at the next
//! Start (`Chain::new_run`, which also covers a crash: no process holds the dead run any more).
//! Physical run 4 on 15 Nov was refused `compiler_storage_quota_exceeded` because two runs that
//! had died at step 6 still held 1.5 GB each.
use super::*;
use api::preferences::private_state;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt};

/// A run that compiled and died, as a crashed or older process leaves it: its run directory with
/// the compile's start record, its stage shard and its `retained` ledger entry; nothing holds it.
fn dead_owner_run(owner: &Path, run: &str) {
    let limit = private_state::private_json_limit().unwrap();
    let runs = owner.join("chain-runs");
    match fs::DirBuilder::new().mode(0o700).create(&runs) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(e) => panic!("{e}"),
    }
    let root = runs.join(run);
    fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
    private_state::store(
        &root.join("compile-start.private.json"),
        &json!({"protocol":"mycelium.chain_compile_start.v1","run_id":run,
            "deployment_id":"dead-deployment","epoch":0}),
        limit,
    )
    .unwrap();
    let shards = root.join("stage-shards");
    fs::DirBuilder::new().mode(0o700).create(&shards).unwrap();
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(shards.join("model-stage-001-of-002.safetensors"))
        .unwrap();
    std::io::Write::write_all(&mut file, &[3; 4096]).unwrap();
    let meta = fs::symlink_metadata(&root).unwrap();
    let ledger = runs.join("compiler-storage.private.json");
    let mut document = private_state::load(&ledger, limit)
        .unwrap()
        .map(Value::Object)
        .unwrap_or_else(|| json!({"protocol":"mycelium.compiler_storage.v1","entries":{}}));
    document["entries"][run] = json!({"bytes":4096,"run_dev":meta.dev(),
        "run_ino":meta.ino(),"phase":"retained"});
    private_state::store(&ledger, &document, limit).unwrap();
}
fn entries(owner: &Path) -> Vec<String> {
    let limit = private_state::private_json_limit().unwrap();
    private_state::load(
        &owner.join("chain-runs/compiler-storage.private.json"),
        limit,
    )
    .unwrap()
    .map(|ledger| {
        ledger["entries"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect()
    })
    .unwrap_or_default()
}
struct Quiet;
impl StepEnv for Quiet {
    fn progress(&mut self, _: &str) {}
    fn stopped(&self) -> bool {
        false
    }
    fn now(&mut self) -> f64 {
        0.0
    }
    fn wall(&mut self) -> f64 {
        app_host::now()
    }
    fn set_result(&mut self, _: &str, _: Value) {}
    fn record_exception(&mut self, _: &str, _: &str) {}
    fn pause(&mut self, _: f64) -> bool {
        false
    }
}

#[test]
fn a_runs_teardown_retires_the_owners_dead_compile_runs() {
    let f = Fixture::new();
    let owner = f.root.join("owner");
    dead_owner_run(&owner, "died-at-step-6");
    dead_owner_run(&owner, "stopped");
    assert_eq!(entries(&owner), ["died-at-step-6", "stopped"]);
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let teardown = owner_run::owner_chain::ChainPorts::teardown(&mut ports, true, &mut Quiet);
    assert!(teardown.is_ok(), "{teardown:?}");
    assert!(entries(&owner).is_empty(), "{:?}", entries(&owner));
    for run in ["died-at-step-6", "stopped"] {
        assert!(
            !owner
                .join("chain-runs")
                .join(run)
                .join("stage-shards")
                .exists()
        );
    }
}

#[test]
fn the_next_start_retires_runs_a_lost_process_left_behind() {
    let f = Fixture::new();
    let owner = f.root.join("owner");
    dead_owner_run(&owner, "crashed-1");
    dead_owner_run(&owner, "crashed-2");
    let mut chain = Composed::new(&f.host, None, None);
    chain.new_run("next", "next-dep", owner_run::Reason::Start, None, None);
    assert!(entries(&owner).is_empty(), "{:?}", entries(&owner));
}
