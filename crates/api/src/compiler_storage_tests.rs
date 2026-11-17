// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded source-artifact tests. Controlled member/observation documents are
//! not authenticated hello sessions, native model execution, or BuildOutput.
use super::*;
use crate::{compiler_inputs, preferences::resource_policy};
use std::sync::atomic::{AtomicU64, Ordering};
static NEXT: AtomicU64 = AtomicU64::new(0);

fn storage_policy(root: &Path, bytes: u64) {
    resource_policy::write(
        root,
        &json!({"peer_visibility":true,"power":"ac_only",
        "storage_bytes":bytes,"network":"unmetered_only","background":false,
        "diagnostics":false,"thermal":"stop_on_serious"}),
    )
    .unwrap();
}

struct Fixture {
    root: PathBuf,
    source: crate::serving_source::Prepared,
    request: BuildRequest,
    members: BTreeMap<String, Value>,
}
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "laneB-compiler-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        let root = root.canonicalize().unwrap();
        storage_policy(&root, 4_000_000_000);
        let fixture: Value = serde_json::from_str(include_str!(
            "../../planner/tests/fixtures/source-slices.json"
        ))
        .unwrap();
        let row = &fixture["cases"][0];
        let source_root = root.join("snapshot");
        fs::create_dir(&source_root).unwrap();
        for (name, encoded) in row["source_files"].as_object().unwrap() {
            let encoded = encoded.as_str().unwrap();
            let bytes: Vec<_> = (0..encoded.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&encoded[i..i + 2], 16).unwrap())
                .collect();
            fs::write(source_root.join(name), bytes).unwrap();
        }
        let manifest = crate::local_manifest::compile_local_model_manifest(
            &crate::local_manifest::LocalModelSource {
                root: source_root.clone(),
                model_id: row["manifest"]["model_id"].as_str().unwrap().into(),
                requested_revision: "main".into(),
                resolved_commit: row["manifest"]["resolved_commit"].as_str().unwrap().into(),
                tokenizer_assets: Ok(Vec::new()),
            },
        )
        .unwrap();
        let source = crate::serving_source::Prepared {
            source_root,
            source_bytes: manifest["files"]
                .as_array()
                .unwrap()
                .iter()
                .map(|r| r["size_bytes"].as_u64().unwrap())
                .sum(),
            manifest_digest: planner::bound::manifest_digest_ref(&manifest).unwrap(),
            manifest: manifest.as_object().unwrap().clone(),
        };
        let ids: Vec<String> = vec!["node-0".into(), "node-1".into(), "node-2".into()];
        let ranges: Vec<[usize; 2]> = serde_json::from_value(row["ranges"].clone()).unwrap();
        let request = BuildRequest {
            model_id: manifest["model_id"].as_str().unwrap().into(),
            resolved_commit: manifest["resolved_commit"].as_str().unwrap().into(),
            deployment_id: "12345678-1234-5678-9234-abcdefabcdef".into(),
            route_label: "a13",
            stage_sharded: false,
            compilation_cache_key: "controlled-source-boundary-not-admitted-native".into(),
            node_ids: ids.clone(),
            managed_layer_ranges: ranges.clone(),
            layer_split: json!({"stages":ids.iter().zip(&ranges).map(|(n,r)|json!({"node_id":n,"start":r[0],"end":r[1]})).collect::<Vec<_>>()}),
            serving_context_tokens: crate::model_catalog::registered()[2].serving_context_tokens(),
        };
        let members = ids.into_iter().map(|n| (n.clone(), json!({"node_id":n,
            "generation":1,"incarnation":"controlled-incarnation","endpoint_id":format!("controlled-{n}"),
            "verification_key_digest":"controlled-not-a-key","lease_expires_at":100}))).collect();
        Self {
            root,
            source,
            request,
            members,
        }
    }
    fn emit_run(&self, run: &str) -> InputArtifact {
        self.emit_run_at(&self.root, run).unwrap()
    }
    fn emit_run_at(&self, root: &Path, run: &str) -> Result<InputArtifact> {
        let nodes: Vec<_> = self
            .request
            .node_ids
            .iter()
            .map(|n| {
                json!({"node_id":n,
            "staging_root":format!("/controlled/{n}"),"runtime_backend":"mlx"})
            })
            .collect();
        let observations: Vec<_> = self
            .request
            .node_ids
            .iter()
            .map(|n| json!({"node_id":n,"host_id":format!("controlled-{n}")}))
            .collect();
        compiler_inputs::emit(
            root,
            run,
            &self.request.deployment_id,
            0,
            &self.request,
            &self.source,
            &json!({"protocol":"mycelium.qwen_live_topology.v1","nodes":nodes}),
            &json!({"run_id":run,"deployment_id":self.request.deployment_id,"observations":observations}),
            &json!({}),
            &self.members.values().cloned().collect::<Vec<_>>(),
        )
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}

std::thread_local! {
    static WRITES_BEFORE_CRASH: std::cell::Cell<usize> = const { std::cell::Cell::new(0) };
}

fn interrupt_before_rename(skip: usize, action: impl FnOnce()) {
    WRITES_BEFORE_CRASH.with(|count| count.set(skip));
    private_state::BEFORE_RENAME.with(|hook| {
        *hook.borrow_mut() = Some(|path| {
            if path.file_name().unwrap() == STORAGE_LEDGER
                && WRITES_BEFORE_CRASH.with(|count| {
                    let remaining = count.get();
                    count.set(remaining.saturating_sub(1));
                    remaining == 0
                })
            {
                panic!("injected crash after ledger fsync before rename");
            }
        });
    });
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(action));
    private_state::BEFORE_RENAME.with(|hook| *hook.borrow_mut() = None);
    assert!(result.is_err(), "must reach the real pre-rename crash seam");
}

fn temporaries(root: &Path) -> Vec<PathBuf> {
    fs::read_dir(root.join("chain-runs"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| path.extension().is_some_and(|ext| ext == "tmp"))
        .collect()
}

#[test]
fn member_refund_interrupted_before_ledger_rename_retains_charge_and_recovers() {
    let f = Fixture::new();
    let fixture: Value = serde_json::from_str(include_str!(
        "../../planner/tests/fixtures/source-slices.json"
    ))
    .unwrap();
    let case = &fixture["cases"][0];
    let recipe = &case["node_plans"]["node-0"];
    let transfer = &case["node_files"]["node-0"][0];
    let digest = recipe["node_plan_digest"].as_str().unwrap();
    let mut receive =
        member_shard::MemberShard::begin(&f.root, "refund", "node-0", transfer, recipe, digest)
            .unwrap();
    let path = receive.path();
    interrupt_before_rename(0, || {
        receive.discard().unwrap();
    });
    assert!(!path.exists());
    let ledger: Value =
        serde_json::from_slice(&fs::read(f.root.join("chain-runs").join(STORAGE_LEDGER)).unwrap())
            .unwrap();
    assert_eq!(ledger["entries"]["refund"]["bytes"], transfer["size_bytes"]);
    drop(receive);
    let mut receive =
        member_shard::MemberShard::resume(&f.root, "refund", "node-0", transfer, recipe, digest)
            .unwrap();
    receive.discard().unwrap();
    drop(receive);
    assert!(temporaries(&f.root).is_empty());
    let ledger: Value =
        serde_json::from_slice(&fs::read(f.root.join("chain-runs").join(STORAGE_LEDGER)).unwrap())
            .unwrap();
    assert!(ledger["entries"].as_object().unwrap().is_empty());
    let mut receive =
        member_shard::MemberShard::resume(&f.root, "refund", "node-0", transfer, recipe, digest)
            .unwrap();
    receive.discard().unwrap();
}

#[test]
fn compiler_storage_accepts_roots_below_a_symlinked_ancestor() {
    let mut f = Fixture::new();
    f.request.stage_sharded = true;
    let alias = f.root.join("alias");
    std::os::unix::fs::symlink(&f.root, &alias).unwrap();
    let physical = f.root.join("app");
    fs::create_dir(&physical).unwrap();
    storage_policy(&physical, 4_000_000_000);
    let root = alias.join("app");
    f.source.source_root = alias.join("snapshot");
    assert_ne!(root.canonicalize().unwrap(), root);
    assert_ne!(
        f.source.source_root.canonicalize().unwrap(),
        f.source.source_root
    );
    let mut artifact = f.emit_run_at(&root, "linked-ancestor").unwrap();
    artifact.produce_stage_shards(&f.members, 0.0).unwrap();
    artifact.validate_stage_shards(&f.members, 0.0).unwrap();
    assert_eq!(artifact.root(), physical.join("chain-runs/linked-ancestor"));
    let ledger: Value = serde_json::from_slice(
        &fs::read(physical.join("chain-runs").join(STORAGE_LEDGER)).unwrap(),
    )
    .unwrap();
    assert_eq!(ledger["entries"]["linked-ancestor"]["phase"], "retained");
}

#[test]
fn compiler_storage_refuses_symlinks_below_trusted_roots() {
    let mut f = Fixture::new();
    f.request.stage_sharded = true;
    let foreign = f.root.join("foreign");
    directory(&foreign).unwrap();
    fs::write(foreign.join("sentinel"), b"untouched").unwrap();
    let runs = f.root.join("chain-runs");
    std::os::unix::fs::symlink(&foreign, &runs).unwrap();
    assert_eq!(
        f.emit_run_at(&f.root, "refused").unwrap_err().0,
        "compiler_directory_invalid"
    );
    assert_eq!(fs::read_dir(&foreign).unwrap().count(), 1);
    fs::remove_file(runs).unwrap();
    let mut artifact = f.emit_run("refuse-output-link");
    artifact.produce_stage_shards(&f.members, 0.0).unwrap();
    let shards = artifact.root().join("stage-shards");
    let moved = f.root.join("moved-shards");
    fs::rename(&shards, &moved).unwrap();
    std::os::unix::fs::symlink(&moved, &shards).unwrap();
    assert_eq!(
        artifact
            .validate_stage_shards(&f.members, 0.0)
            .unwrap_err()
            .0,
        "compiler_source_path_invalid"
    );
    assert_eq!(fs::read(foreign.join("sentinel")).unwrap(), b"untouched");
}

#[test]
fn headroom_counts_unwritten_promises_not_materialized_shards_twice() {
    let mut f = Fixture::new();
    f.request.stage_sharded = true;
    let mut written = f.emit_run("written");
    written.produce_stage_shards(&f.members, 0.0).unwrap();
    let ledger_path = f.root.join("chain-runs").join(STORAGE_LEDGER);
    let ledger: Value = serde_json::from_slice(&fs::read(&ledger_path).unwrap()).unwrap();
    let charge = ledger["entries"]["written"]["bytes"].as_u64().unwrap();
    let allocated: u64 = fs::read_dir(written.root().join("stage-shards"))
        .unwrap()
        .map(|e| {
            let meta = e.unwrap().metadata().unwrap();
            meta.len().min(meta.blocks() * 512)
        })
        .sum();
    assert!(allocated > 0);
    let reserve = budgets::BudgetId::PyFinalMyceliumLiveLocalPreparerArtifactDiskReserveBytesL50C0
        .bytes()
        .unwrap()
        .get();
    let next = f.emit_run("next");
    STORAGE_FREE_BYTES.with(|free| free.set(Some(reserve + charge - allocated + 300)));
    let result = next.reserve_stage_output(300);
    STORAGE_FREE_BYTES.with(|free| free.set(None));
    drop(result.expect("already allocated shard bytes must not reserve free space twice"));
    let third = f.emit_run("third");
    STORAGE_FREE_BYTES.with(|free| free.set(Some(reserve + charge - allocated + 300)));
    let result = third.reserve_stage_output(1);
    STORAGE_FREE_BYTES.with(|free| free.set(None));
    assert_eq!(
        result.unwrap_err().0,
        "compiler_storage_headroom_exceeded",
        "the unmaterialized next reservation still consumes headroom"
    );
    storage_policy(&f.root, 100_000_000);
    assert_eq!(
        third
            .reserve_stage_output(100_000_000 - charge - 300 + 1)
            .unwrap_err()
            .0,
        "compiler_storage_quota_exceeded",
        "materialized shards remain fully charged to consent"
    );
}

#[test]
fn headroom_keeps_sparse_and_partial_output_promises() {
    use std::{io::Write, os::unix::fs::OpenOptionsExt};
    let f = Fixture::new();
    let partial = f.emit_run("partial");
    let promised = 8 * 1024 * 1024;
    drop(partial.reserve_stage_output(promised).unwrap());
    let output = partial.root().join("stage-shards");
    directory(&output).unwrap();
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(output.join("partial.safetensors"))
        .unwrap();
    file.write_all(&[1; 4096]).unwrap();
    file.set_len(promised / 2).unwrap();
    file.sync_all().unwrap();
    let meta = file.metadata().unwrap();
    let allocated = meta.len().min(meta.blocks() * 512);
    assert!(allocated > 0 && allocated < promised);
    let reserve = budgets::BudgetId::PyFinalMyceliumLiveLocalPreparerArtifactDiskReserveBytesL50C0
        .bytes()
        .unwrap()
        .get();
    let next = f.emit_run("next");
    STORAGE_FREE_BYTES.with(|free| free.set(Some(reserve + promised - allocated + 299)));
    let refused = next.reserve_stage_output(300);
    STORAGE_FREE_BYTES.with(|free| free.set(None));
    assert_eq!(refused.unwrap_err().0, "compiler_storage_headroom_exceeded");
    STORAGE_FREE_BYTES.with(|free| free.set(Some(reserve + promised - allocated + 300)));
    let accepted = next.reserve_stage_output(300);
    STORAGE_FREE_BYTES.with(|free| free.set(None));
    drop(accepted.unwrap());
}

#[test]
fn ledger_first_reservation_crash_allows_fresh_constructor_retry() {
    let f = Fixture::new();
    let first = f.emit_run("first");
    interrupt_before_rename(0, || drop(first.reserve_stage_output(200).unwrap()));
    assert!(!f.root.join("chain-runs").join(STORAGE_LEDGER).exists());
    drop(first);
    drop(f.emit_run("retry").reserve_stage_output(300).unwrap());
    let ledger: Value =
        serde_json::from_slice(&fs::read(f.root.join("chain-runs").join(STORAGE_LEDGER)).unwrap())
            .unwrap();
    assert_eq!(ledger["entries"].as_object().unwrap().len(), 1);
    assert_eq!(ledger["entries"]["retry"]["bytes"], 300);
    assert!(temporaries(&f.root).is_empty());
}

#[test]
fn ledger_recovery_refuses_foreign_symlink_hardlink_and_changed_identity() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    for mutation in [
        "symlink",
        "hardlink",
        "foreign",
        "run-identity",
        "refund",
        "mode",
        "partial",
    ] {
        let f = Fixture::new();
        let old = f.emit_run("old");
        drop(old.reserve_stage_output(100).unwrap());
        let crashed = f.emit_run("crashed");
        interrupt_before_rename(0, || drop(crashed.reserve_stage_output(200).unwrap()));
        let path = temporaries(&f.root).pop().unwrap();
        let foreign = f.root.join("foreign");
        let raw = fs::read(&path).unwrap();
        match mutation {
            "symlink" | "hardlink" => {
                fs::rename(&path, &foreign).unwrap();
                if mutation == "symlink" {
                    symlink(&foreign, &path).unwrap();
                } else {
                    fs::hard_link(&foreign, &path).unwrap();
                }
            }
            "mode" => fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap(),
            "partial" => fs::write(&path, b"{").unwrap(),
            _ => {
                let mut doc: Value = serde_json::from_slice(&raw).unwrap();
                match mutation {
                    "foreign" => doc["protocol"] = json!("foreign"),
                    "run-identity" => doc["entries"]["crashed"]["run_ino"] = json!(0),
                    "refund" => doc["entries"]["old"]["bytes"] = json!(1),
                    _ => unreachable!(),
                }
                fs::write(&path, protocol::canonical::to_bytes(&doc).unwrap()).unwrap();
            }
        }
        let before = fs::read(&path).unwrap();
        let ledger = f.root.join("chain-runs").join(STORAGE_LEDGER);
        let committed = fs::read(&ledger).unwrap();
        assert_eq!(
            f.emit_run("retry").reserve_stage_output(1).unwrap_err().0,
            "compiler_storage_owner_changed",
            "{mutation}"
        );
        assert_eq!(fs::read(&path).unwrap(), before);
        assert_eq!(fs::read(&ledger).unwrap(), committed);
        if foreign.exists() {
            assert_eq!(fs::read(foreign).unwrap(), raw);
        }
    }
}

#[test]
fn ledger_pre_rename_crash_allows_restart_without_refunding_old_charges() {
    for completing in [false, true] {
        let mut f = Fixture::new();
        f.request.stage_sharded = true;
        let old = f.emit_run("old-charge");
        drop(old.reserve_stage_output(100).unwrap());
        let mut crashed = f.emit_run("crashed");
        interrupt_before_rename(usize::from(completing), || {
            if completing {
                crashed.produce_stage_shards(&f.members, 0.0).unwrap();
            } else {
                drop(crashed.reserve_stage_output(200).unwrap());
            }
        });
        let ledger_path = f.root.join("chain-runs").join(STORAGE_LEDGER);
        let prior: Value = serde_json::from_slice(&fs::read(&ledger_path).unwrap()).unwrap();
        assert_eq!(temporaries(&f.root).len(), 1);
        drop(crashed);
        drop(old);
        let restarted = f.emit_run("restarted");
        drop(
            restarted
                .reserve_stage_output(300)
                .expect("restart must reconcile owned ledger residue"),
        );
        let after: Value = serde_json::from_slice(&fs::read(ledger_path).unwrap()).unwrap();
        for (run, row) in prior["entries"].as_object().unwrap() {
            assert_eq!(
                &after["entries"][run], row,
                "never refund a committed charge"
            );
        }
        assert_eq!(after["entries"]["restarted"]["bytes"], 300);
        assert!(temporaries(&f.root).is_empty());
    }
}

// Lane PHY item 1 (ledger rows 8, 81): the owner retires the entries and stage shards of its own
// dead compile runs. Physical run 4 on 15 Nov was refused `compiler_storage_quota_exceeded` at
// step 5 because runs 2 and 3 had compiled and then died at step 6, each leaving a `retained`
// 1.5 GB entry against the 4.0 GB limit, and nothing ever removed them.

fn ledger_of(root: &Path) -> Value {
    serde_json::from_slice(&fs::read(root.join("chain-runs").join(STORAGE_LEDGER)).unwrap())
        .unwrap()
}
fn ledger_runs(root: &Path) -> Vec<String> {
    ledger_of(root)["entries"]
        .as_object()
        .unwrap()
        .keys()
        .cloned()
        .collect()
}

/// A run whose compile completed: its reservation, its stage shard (sparse, so the test spends no
/// disk) and the ledger's `retained` entry, as the producer leaves them.
fn compiled_run(f: &Fixture, run: &str, bytes: u64) -> InputArtifact {
    use std::os::unix::fs::OpenOptionsExt;
    let artifact = f.emit_run(run);
    let reservation = artifact.reserve_stage_output(bytes).unwrap();
    let output = artifact.root().join("stage-shards");
    directory(&output).unwrap();
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(output.join("model-stage-001-of-002.safetensors"))
        .unwrap()
        .set_len(bytes)
        .unwrap();
    reservation.complete().unwrap();
    assert_eq!(ledger_of(&f.root)["entries"][run]["phase"], "retained");
    artifact
}

#[test]
fn two_runs_dead_after_their_compile_are_retired_so_a_third_passes_the_reservation() {
    let f = Fixture::new();
    // Room for two compiled runs and not a third, as 2 x 1.5 GB against 4.0 GB on Mac A
    // (the resource policy's floor is 100 MB).
    storage_policy(&f.root, 100_000_000);
    let charge = 40_000_000;
    // Each run failed at step 6: its teardown ended the compile's custody.
    drop(compiled_run(&f, "dead-1", charge));
    drop(compiled_run(&f, "dead-2", charge));
    let wedged = f.emit_run("wedged");
    assert_eq!(
        wedged.reserve_stage_output(charge).unwrap_err().0,
        "compiler_storage_quota_exceeded",
        "the physical wedge of row 81, before any retirement"
    );
    drop(wedged);
    let retirement = compiler_inputs::retire_dead_runs(&f.root).unwrap();
    assert_eq!(retirement.retired, ["dead-1", "dead-2"]);
    assert!(retirement.refused.is_empty(), "{:?}", retirement.refused);
    let runs = f.root.join("chain-runs");
    for run in ["dead-1", "dead-2"] {
        assert!(!runs.join(run).join("stage-shards").exists(), "{run}");
        assert!(
            runs.join(run).join("compile-start.private.json").exists(),
            "the run's metadata is evidence and stays: {run}"
        );
    }
    let third = compiled_run(&f, "third", charge);
    assert_eq!(ledger_runs(&f.root), ["third"]);
    drop(third);
}

#[test]
fn a_crash_between_reserve_and_complete_is_retired_at_the_next_start() {
    use std::{io::Write, os::unix::fs::OpenOptionsExt};
    for written in [false, true] {
        let f = Fixture::new();
        let promised = 60_000_000;
        storage_policy(&f.root, 100_000_000);
        let crashed = f.emit_run("crashed");
        drop(crashed.reserve_stage_output(promised).unwrap());
        if written {
            let output = crashed.root().join("stage-shards");
            directory(&output).unwrap();
            let mut file = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(output.join("model-stage-001-of-003.safetensors"))
                .unwrap();
            file.write_all(&[7; 4096]).unwrap();
            file.sync_all().unwrap();
        }
        // The process is lost with the compile in flight: its custody ends with it.
        drop(crashed);
        let next = f.emit_run("next");
        assert_eq!(
            next.reserve_stage_output(promised).unwrap_err().0,
            "compiler_storage_quota_exceeded",
            "written={written}"
        );
        let retirement = compiler_inputs::retire_dead_runs(&f.root).unwrap();
        assert_eq!(retirement.retired, ["crashed"], "written={written}");
        assert!(
            !f.root.join("chain-runs/crashed/stage-shards").exists(),
            "written={written}"
        );
        drop(
            next.reserve_stage_output(promised)
                .expect("the next Start's reservation passes"),
        );
        assert_eq!(ledger_runs(&f.root), ["next"], "written={written}");
    }
}

#[test]
fn a_serving_runs_entry_survives_a_second_starts_retirement_pass() {
    let mut f = Fixture::new();
    f.request.stage_sharded = true;
    let mut serving = f.emit_run("serving");
    serving.produce_stage_shards(&f.members, 0.0).unwrap();
    let row = ledger_of(&f.root)["entries"]["serving"].clone();
    let mut dead = f.emit_run("dead");
    dead.produce_stage_shards(&f.members, 0.0).unwrap();
    drop(dead);
    let retirement = compiler_inputs::retire_dead_runs(&f.root).unwrap();
    assert_eq!(retirement.retired, ["dead"]);
    assert_eq!(ledger_of(&f.root)["entries"]["serving"], row);
    serving
        .validate_stage_shards(&f.members, 0.0)
        .expect("a serving run's shards are untouched");
    // A second Start's pass while the first run still serves.
    let again = compiler_inputs::retire_dead_runs(&f.root).unwrap();
    assert!(again.retired.is_empty(), "{again:?}");
    assert_eq!(ledger_of(&f.root)["entries"]["serving"], row);
    serving.validate_stage_shards(&f.members, 0.0).unwrap();
    // Its own end releases it.
    drop(serving);
    assert_eq!(
        compiler_inputs::retire_dead_runs(&f.root).unwrap().retired,
        ["serving"]
    );
}

/// Proves PHY-01 (lane PHY patch 2): a run whose start record has no custody marker is a base
/// build's run. A base build never takes the run directory's lock, so a free lock proves nothing
/// about it: a LIVE base process (compiling or serving it) looks exactly like a dead one. Such a
/// run is never retired; it is named with its charged bytes. A marked run (this build's) whose
/// lock is free is dead and is retired beside it.
#[test]
fn a_legacy_runs_entry_is_kept_and_named_while_a_dead_marked_run_beside_it_is_retired() {
    let f = Fixture::new();
    let charge = 40_000_000;
    // A base-format live run: the base wrote this start record and holds no run-directory lock.
    drop(compiled_run(&f, "legacy-live", charge));
    let start = f
        .root
        .join("chain-runs/legacy-live")
        .join("compile-start.private.json");
    let max = private_state::private_json_limit().unwrap();
    let written = private_state::load(&start, max).unwrap().unwrap();
    let base = json!({
        "protocol": "mycelium.chain_compile_start.v1",
        "run_id": written["run_id"],
        "deployment_id": written["deployment_id"],
        "epoch": written["epoch"],
    });
    private_state::store(&start, &base, max).unwrap();
    let row = ledger_of(&f.root)["entries"]["legacy-live"].clone();
    // This build's run, dead: its process released the lock.
    drop(compiled_run(&f, "dead-marked", charge));
    let retirement = compiler_inputs::retire_dead_runs(&f.root).unwrap();
    assert_eq!(retirement.retired, ["dead-marked"]);
    assert!(retirement.refused.is_empty(), "{:?}", retirement.refused);
    assert_eq!(retirement.legacy, [("legacy-live".to_owned(), charge)]);
    assert_eq!(ledger_of(&f.root)["entries"]["legacy-live"], row);
    assert!(
        f.root
            .join("chain-runs/legacy-live/stage-shards/model-stage-001-of-002.safetensors")
            .exists(),
        "a legacy run's shards are untouched"
    );
    assert!(!f.root.join("chain-runs/dead-marked/stage-shards").exists());
    // This build marks its start record (the retired run keeps its metadata).
    let marked = private_state::load(
        &f.root
            .join("chain-runs/dead-marked")
            .join("compile-start.private.json"),
        max,
    )
    .unwrap()
    .unwrap();
    assert_eq!(
        marked.get("custody"),
        Some(&json!(owner_retire::CUSTODY)),
        "this build marks its start record"
    );
    // Every later pass leaves it too.
    let again = compiler_inputs::retire_dead_runs(&f.root).unwrap();
    assert!(again.retired.is_empty(), "{again:?}");
    assert_eq!(again.legacy, [("legacy-live".to_owned(), charge)]);
}

#[test]
fn retirement_never_adopts_a_member_custody_or_a_foreign_file() {
    let mut f = Fixture::new();
    f.request.stage_sharded = true;
    let fixture: Value = serde_json::from_str(include_str!(
        "../../planner/tests/fixtures/source-slices.json"
    ))
    .unwrap();
    let case = &fixture["cases"][0];
    let recipe = &case["node_plans"]["node-0"];
    let transfer = &case["node_files"]["node-0"][0];
    let digest = recipe["node_plan_digest"].as_str().unwrap();
    // A joined Mac's custody in the same ledger is retired by its own discard, never by this pass.
    drop(
        member_shard::MemberShard::begin(&f.root, "member-run", "node-0", transfer, recipe, digest)
            .unwrap(),
    );
    let mut tampered = f.emit_run("tampered");
    tampered.produce_stage_shards(&f.members, 0.0).unwrap();
    let shards = tampered.root().join("stage-shards");
    drop(tampered);
    {
        use std::os::unix::fs::OpenOptionsExt;
        fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(shards.join("notes.txt"))
            .unwrap();
    }
    let before: Vec<_> = {
        let mut names: Vec<_> = fs::read_dir(&shards)
            .unwrap()
            .map(|e| e.unwrap().file_name())
            .collect();
        names.sort();
        names
    };
    let committed = fs::read(f.root.join("chain-runs").join(STORAGE_LEDGER)).unwrap();
    let retirement = compiler_inputs::retire_dead_runs(&f.root).unwrap();
    assert!(retirement.retired.is_empty(), "{retirement:?}");
    assert_eq!(
        retirement.refused,
        [(
            "tampered".to_owned(),
            "compiler_storage_retire_foreign_output"
        )]
    );
    let mut after: Vec<_> = fs::read_dir(&shards)
        .unwrap()
        .map(|e| e.unwrap().file_name())
        .collect();
    after.sort();
    assert_eq!(after, before, "nothing of a refused run is removed");
    assert_eq!(
        fs::read(f.root.join("chain-runs").join(STORAGE_LEDGER)).unwrap(),
        committed
    );
}

#[test]
fn a_crash_before_the_retirements_ledger_rename_recovers_without_refusing() {
    let mut f = Fixture::new();
    f.request.stage_sharded = true;
    let mut dead = f.emit_run("dead");
    dead.produce_stage_shards(&f.members, 0.0).unwrap();
    drop(dead);
    interrupt_before_rename(0, || {
        compiler_inputs::retire_dead_runs(&f.root).unwrap();
    });
    assert!(!f.root.join("chain-runs/dead/stage-shards").exists());
    assert_eq!(temporaries(&f.root).len(), 1);
    let next = f.emit_run("next");
    drop(
        next.reserve_stage_output(1)
            .expect("the retirement's own leftover is recognised, never `owner_changed`"),
    );
    assert!(temporaries(&f.root).is_empty());
    assert_eq!(
        compiler_inputs::retire_dead_runs(&f.root).unwrap().retired,
        ["dead"]
    );
    assert_eq!(ledger_runs(&f.root), ["next"]);
}

/// Lane A, release blocker B11 (a) (owner ruling 4 a, ledger rows 77 and 93): a storage ledger the
/// compile cannot read (here the hand-indented copy row 77 found on Mac A: 63 bytes where the
/// canonical document is 56) still answers the closed `authority_state_invalid`, and the log
/// names the file beside that code, so the next reader of an app log knows which file it was.
#[test]
fn an_unreadable_storage_ledger_is_named_in_the_log() {
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new();
    let runs = f.root.join("chain-runs");
    directory(&runs).unwrap();
    let ledger = runs.join(STORAGE_LEDGER);
    fs::write(
        &ledger,
        "{\n  \"entries\": {},\n  \"protocol\": \"mycelium.compiler_storage.v1\"\n}",
    )
    .unwrap();
    fs::set_permissions(&ledger, fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(
        storage_ledger(&runs).err().map(|e| e.0),
        Some("authority_state_invalid")
    );
    let named = format!("authority_state_invalid:{}", ledger.display());
    let lines = DIAGNOSTICS.lock().unwrap().clone();
    assert!(lines.contains(&named), "{named} not in {lines:?}");
}

/// Lane A, B12 (ledger row 96), the same mechanism at this module's other `flock`s: a holder's
/// lock description still held by a child this process forked (from fork to exec), after the
/// holder itself let go. Modelled without a close-on-exec window in the parent: the holder's
/// description is handed to `/bin/sleep 1` as its stdin, then the holder drops it.
fn held_by_a_child(path: &Path) -> std::process::Child {
    let holder = fs::File::open(path).unwrap();
    holder.try_lock().unwrap();
    let child = std::process::Command::new("/bin/sleep")
        .arg("1")
        .stdin(std::process::Stdio::from(holder.try_clone().unwrap()))
        .spawn()
        .unwrap();
    drop(holder);
    child
}

/// The storage ledger's lock (`compiler_storage_busy`, 2 of the first 30 full `api --lib` repeats
/// after the member shard fix, under load ~17).
#[test]
fn a_ledger_lock_copy_still_held_by_a_forked_child_does_not_refuse_the_next_compile() {
    let f = Fixture::new();
    let runs = f.root.join("chain-runs");
    directory(&runs).unwrap();
    drop(storage_ledger(&runs).unwrap());
    let mut child = held_by_a_child(&runs.join(STORAGE_LOCK));
    let got = storage_ledger(&runs).map(drop).map_err(|e| e.0);
    child.wait().unwrap();
    assert_eq!(
        got,
        Ok(()),
        "a lock only a forked child's copy held refused the ledger"
    );
}

/// A dead run's directory lock (the retirement pass read it as a live run: 1 of the same 30).
#[test]
fn a_dead_runs_lock_copy_still_held_by_a_forked_child_does_not_keep_it_live() {
    let mut f = Fixture::new();
    f.request.stage_sharded = true;
    let mut dead = f.emit_run("dead");
    dead.produce_stage_shards(&f.members, 0.0).unwrap();
    drop(dead);
    let mut child = held_by_a_child(&f.root.join("chain-runs").join("dead"));
    let retirement = compiler_inputs::retire_dead_runs(&f.root).map(|r| r.retired);
    child.wait().unwrap();
    assert_eq!(retirement.map_err(|e| e.0), Ok(vec!["dead".to_owned()]));
}

/// Lane A patch 2, A-01 (REVIEW-laneA-release-astra.md): a retirement pass has ONE bound for all
/// its lock waiting, not the close allowance per live run while it holds the ledger. Eight live
/// runs (the ledger's limit, `MAX_RETAINED_SHARD_RUNS`) are all kept live, and the pass answers
/// within one allowance (plus slack for I/O), not about eight allowances (32 s).
#[test]
fn a_retirement_pass_over_eight_live_runs_waits_one_allowance_in_total() {
    let f = Fixture::new();
    let live: Vec<_> = (0..MAX_RETAINED_SHARD_RUNS)
        .map(|n| compiled_run(&f, &format!("live-{n}"), 1_000_000))
        .collect();
    let allowance = budgets::BudgetId::RouterClose
        .span(budgets::Entries::ZERO)
        .unwrap()
        .millis();
    let started = std::time::Instant::now();
    let retirement = compiler_inputs::retire_dead_runs(&f.root).unwrap();
    let waited = started.elapsed();
    assert!(retirement.retired.is_empty(), "{retirement:?}");
    assert!(retirement.refused.is_empty(), "{retirement:?}");
    assert_eq!(ledger_runs(&f.root).len(), MAX_RETAINED_SHARD_RUNS);
    assert!(
        waited < std::time::Duration::from_millis(allowance + 2_000),
        "a pass over {} live runs waited {waited:?} (allowance {allowance} ms)",
        live.len()
    );
    drop(live);
}
