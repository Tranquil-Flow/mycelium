// SPDX-License-Identifier: AGPL-3.0-only
//! Storage boundary only: real producer bytes, not joined/native serving proof.
use api::{compiler_inputs::member_shard::MemberShard, preferences::resource_policy};
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};
#[path = "support/member_shard_carrier.rs"]
mod carrier;

static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture {
    root: PathBuf,
    recipe: Value,
    transfer: Value,
    bytes: Vec<u8>,
}
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "b7-receive-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        resource_policy::write(&root, &json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"})).unwrap();
        // Independently recorded source-slice corpus; not model or resident proof.
        let fixture: Value = serde_json::from_str(include_str!(
            "../../planner/tests/fixtures/source-slices.json"
        ))
        .unwrap();
        let case = &fixture["cases"][0];
        let recipe = case["node_plans"]["node-0"].clone();
        let transfer = case["node_files"]["node-0"][0].clone();
        let name = transfer["path"]
            .as_str()
            .unwrap()
            .strip_prefix("deployment/")
            .unwrap();
        let encoded = case["shard_files"][name].as_str().unwrap();
        let bytes = (0..encoded.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&encoded[i..i + 2], 16).unwrap())
            .collect();
        Self {
            root,
            recipe,
            transfer,
            bytes,
        }
    }
    fn begin(&self, run: &str) -> MemberShard {
        MemberShard::begin(
            &self.root,
            run,
            "node-0",
            &self.transfer,
            &self.recipe,
            self.recipe["node_plan_digest"].as_str().unwrap(),
        )
        .unwrap()
    }
    fn begin_checked(
        &self,
        run: &str,
        checkpoint: &dyn Fn() -> Result<(), api::compiler_inputs::Error>,
    ) -> Result<MemberShard, api::compiler_inputs::Error> {
        MemberShard::begin_checked(
            &self.root,
            run,
            "node-0",
            &self.transfer,
            &self.recipe,
            self.recipe["node_plan_digest"].as_str().unwrap(),
            checkpoint,
        )
    }
    fn resume(&self, run: &str) -> Result<MemberShard, api::compiler_inputs::Error> {
        MemberShard::resume(
            &self.root,
            run,
            "node-0",
            &self.transfer,
            &self.recipe,
            self.recipe["node_plan_digest"].as_str().unwrap(),
        )
    }
    fn charge(&self) -> u64 {
        let v: Value = serde_json::from_slice(
            &fs::read(self.root.join("chain-runs/compiler-storage.private.json")).unwrap(),
        )
        .unwrap();
        v["entries"]
            .as_object()
            .unwrap()
            .values()
            .map(|v| v["bytes"].as_u64().unwrap())
            .sum()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}
#[test]
fn received_shard_retries_verify_and_refund_only_after_discard() {
    let f = Fixture::new();
    let mut receive = f.begin("receive");
    assert_eq!(f.charge(), f.bytes.len() as u64);
    let split = f.bytes.len() / 2;
    receive.write_chunk(0, &f.bytes[..split]).unwrap();
    receive.write_chunk(0, &f.bytes[..split]).unwrap();
    assert_eq!(receive.received_bytes().unwrap(), split as u64);
    receive
        .write_chunk(split as u64, &f.bytes[split..])
        .unwrap();
    receive.verify(&|| Ok(())).unwrap();
    assert_eq!(fs::read(receive.path()).unwrap(), f.bytes);
    let path = receive.path().to_owned();
    receive.discard().unwrap();
    assert!(!path.exists());
    assert_eq!(f.charge(), 0);
}
#[test]
fn truncated_tampered_and_conflicting_retry_do_not_become_retained() {
    let f = Fixture::new();
    let mut receive = f.begin("truncated");
    receive.write_chunk(0, &f.bytes[..1]).unwrap();
    assert_eq!(
        receive.verify(&|| Ok(())).unwrap_err().0,
        "member_shard_length_mismatch"
    );
    assert_eq!(
        receive.write_chunk(0, &[f.bytes[0] ^ 1]).unwrap_err().0,
        "member_shard_retry_mismatch"
    );
    receive.discard().unwrap();
    let mut receive = f.begin("tampered");
    let mut bad = f.bytes.clone();
    bad[0] ^= 1;
    receive.write_chunk(0, &bad).unwrap();
    assert_eq!(
        receive.verify(&|| Ok(())).unwrap_err().0,
        "member_shard_hash_mismatch"
    );
    assert_eq!(f.charge(), f.bytes.len() as u64);
    receive.discard().unwrap();
}
#[test]
fn recipe_pins_cancel_gaps_and_replaced_files_refuse_without_refund() {
    use api::compiler_inputs::Error;
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new();
    let digest = f.recipe["node_plan_digest"].as_str().unwrap();
    assert_eq!(
        MemberShard::begin(
            &f.root,
            "wrong-node",
            "node-1",
            &f.transfer,
            &f.recipe,
            digest
        )
        .unwrap_err()
        .0,
        "member_shard_recipe_invalid"
    );
    let mut receive = f.begin("negative");
    assert_eq!(
        receive.write_chunk(1, &[0]).unwrap_err().0,
        "member_shard_chunk_invalid"
    );
    receive.write_chunk(0, &f.bytes).unwrap();
    assert_eq!(
        receive
            .verify(&|| Err(Error("cancelled_by_owner")))
            .unwrap_err()
            .0,
        "cancelled_by_owner"
    );
    assert_eq!(f.charge(), f.bytes.len() as u64);
    let path = receive.path();
    let original = f.root.join("held-original");
    fs::rename(&path, &original).unwrap();
    fs::write(&path, &f.bytes).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    assert_eq!(
        receive.discard().unwrap_err().0,
        "member_shard_identity_changed"
    );
    drop(receive);
    assert_eq!(
        MemberShard::resume(
            &f.root,
            "negative",
            "node-0",
            &f.transfer,
            &f.recipe,
            digest
        )
        .unwrap_err()
        .0,
        "member_shard_identity_changed"
    );
    assert_eq!(fs::read(&path).unwrap(), f.bytes);
    fs::remove_file(&path).unwrap();
    fs::rename(&original, &path).unwrap();
    let mut receive = MemberShard::resume(
        &f.root,
        "negative",
        "node-0",
        &f.transfer,
        &f.recipe,
        digest,
    )
    .unwrap();
    receive.discard().unwrap();
    assert_eq!(f.charge(), 0);
}

#[test]
fn pinned_but_wrong_recipe_ranges_refuse_even_when_transfer_matches() {
    let mut f = Fixture::new();
    let file = f.recipe["files"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|r| r["path"] == f.transfer["path"])
        .unwrap();
    // A real content digest of different bytes, not a fabricated native proof.
    file["segments"][0]["sha256"] =
        json!(protocol::artifact_grant::range_digest(b"different bytes"));
    f.recipe.as_object_mut().unwrap().remove("node_plan_digest");
    f.recipe["node_plan_digest"] = json!(protocol::canonical::digest(&f.recipe).unwrap());
    let mut receive = f.begin("bad-recipe");
    receive.write_chunk(0, &f.bytes).unwrap();
    assert_eq!(
        receive.verify(&|| Ok(())).unwrap_err().0,
        "member_shard_recipe_mismatch"
    );
    receive.discard().unwrap();
}

#[test]
fn replacing_stage_directory_with_symlink_never_deletes_foreign_bytes() {
    let f = Fixture::new();
    let mut receive = f.begin("link-attack");
    receive.write_chunk(0, &f.bytes).unwrap();
    let stages = receive.path().parent().unwrap().to_owned();
    let moved = f.root.join("foreign-shards");
    fs::rename(&stages, &moved).unwrap();
    std::os::unix::fs::symlink(&moved, &stages).unwrap();
    let result = receive.discard();
    assert_eq!(result.unwrap_err().0, "member_shard_identity_changed");
    assert_eq!(
        fs::read(moved.join(receive.path().file_name().unwrap())).unwrap(),
        f.bytes
    );
    fs::remove_file(&stages).unwrap();
    fs::rename(&moved, &stages).unwrap();
    receive.discard().unwrap();
}

#[test]
fn restart_cannot_refund_a_moved_file_or_directory() {
    for directory in [false, true] {
        let f = Fixture::new();
        let mut shard = f.begin("moved");
        shard.write_chunk(0, &f.bytes).unwrap();
        let path = shard.path();
        let original = if directory {
            path.parent().unwrap().to_owned()
        } else {
            path.clone()
        };
        let moved = f.root.join("moved-owned");
        drop(shard);
        fs::rename(&original, &moved).unwrap();
        let result = f.resume("moved").and_then(|mut s| s.discard());
        assert!(result.is_err(), "absence is not cleanup evidence");
        assert_eq!(f.charge(), f.bytes.len() as u64);
        assert_eq!(
            fs::read(if directory {
                moved.join(path.file_name().unwrap())
            } else {
                moved.clone()
            })
            .unwrap(),
            f.bytes
        );
    }
}
#[test]
fn cleanup_checkpoint_directory_and_final_file_swap_preserve_foreign_bytes() {
    use std::{cell::Cell, os::unix::fs::PermissionsExt};
    for directory in [false, true] {
        let f = Fixture::new();
        let mut shard = f.begin("racing");
        shard.write_chunk(0, &f.bytes).unwrap();
        let path = shard.path();
        let stages = path.parent().unwrap().to_owned();
        let foreign = f.root.join("foreign");
        fs::create_dir(&foreign).unwrap();
        let foreign_file = foreign.join(path.file_name().unwrap());
        fs::write(&foreign_file, b"foreign sentinel").unwrap();
        fs::set_permissions(&foreign_file, fs::Permissions::from_mode(0o600)).unwrap();
        let moved = f.root.join("moved");
        let once = Cell::new(false);
        let result = shard.discard_with_checkpoint(&|| {
            if !once.replace(true) {
                if directory {
                    fs::rename(&stages, &moved).unwrap();
                    std::os::unix::fs::symlink(&foreign, &stages).unwrap();
                } else {
                    fs::rename(&path, &moved).unwrap();
                    fs::rename(&foreign_file, &path).unwrap();
                }
            }
            Ok(())
        });
        assert!(result.is_err());
        if directory {
            assert_eq!(fs::read(&foreign_file).unwrap(), b"foreign sentinel");
        } else {
            assert!(
                fs::read(&path).is_ok_and(|b| b == b"foreign sentinel")
                    || fs::read(stages.parent().unwrap().join("member-shard-retired.bin"))
                        .is_ok_and(|b| b == b"foreign sentinel")
            );
        }
        assert_eq!(f.charge(), f.bytes.len() as u64);
    }
}
#[test]
fn interrupted_retirement_resumes_from_durable_inode_evidence() {
    use std::cell::Cell;
    for stop_at in 1..=4 {
        let f = Fixture::new();
        let mut shard = f.begin("interrupted");
        shard.write_chunk(0, &f.bytes).unwrap();
        let path = shard.path();
        let count = Cell::new(0);
        assert_eq!(
            shard
                .discard_with_checkpoint(&|| {
                    count.set(count.get() + 1);
                    if count.get() == stop_at {
                        Err(api::compiler_inputs::Error("injected_crash"))
                    } else {
                        Ok(())
                    }
                })
                .unwrap_err()
                .0,
            "injected_crash"
        );
        assert_eq!(f.charge(), f.bytes.len() as u64);
        drop(shard);
        let mut resumed = f.resume("interrupted").unwrap();
        assert!(resumed.write_chunk(0, &f.bytes[..1]).is_err());
        resumed.discard().unwrap();
        assert!(!path.exists());
        assert_eq!(f.charge(), 0);
        drop(resumed);
        f.resume("interrupted").unwrap().discard().unwrap();
        let retired = path
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .join("member-shard-retired.bin");
        assert_eq!(fs::metadata(retired).unwrap().len(), 0);
    }
}
#[test]
fn completed_retirement_recovers_refund_ledger_interruption_but_not_replaced_tombstone() {
    use std::os::unix::fs::PermissionsExt;
    let f = Fixture::new();
    let mut shard = f.begin("ledger-interrupted");
    shard.write_chunk(0, &f.bytes).unwrap();
    let ledger = f.root.join("chain-runs/compiler-storage.private.json");
    let charged = fs::read(&ledger).unwrap();
    let owner = shard.path().parent().unwrap().parent().unwrap().to_owned();
    shard.discard().unwrap();
    drop(shard);
    let replacement = f
        .root
        .join("chain-runs/.compiler-storage.private.json.0123456789abcdef0123456789abcdef.tmp");
    fs::copy(&ledger, &replacement).unwrap();
    fs::write(&ledger, &charged).unwrap();
    let mut resumed = f.resume("ledger-interrupted").unwrap();
    assert!(!replacement.exists());
    assert_eq!(f.charge(), f.bytes.len() as u64);
    resumed.discard().unwrap();
    drop(resumed);
    assert_eq!(f.charge(), 0);
    // Even a previously real refund marker cannot authenticate another inode.
    let retired = owner.join("member-shard-retired.bin");
    fs::rename(&retired, owner.join("moved-retired")).unwrap();
    fs::write(&retired, b"").unwrap();
    fs::set_permissions(&retired, fs::Permissions::from_mode(0o600)).unwrap();
    fs::write(&ledger, &charged).unwrap();
    assert!(f.resume("ledger-interrupted").is_err());
    assert_eq!(f.charge(), f.bytes.len() as u64);
}
#[test]
fn drop_keeps_debt_and_exclusive_custody_can_resume() {
    let f = Fixture::new();
    let mut receive = f.begin("resume");
    receive.write_chunk(0, &f.bytes[..1]).unwrap();
    assert_eq!(
        MemberShard::resume(
            &f.root,
            "resume",
            "node-0",
            &f.transfer,
            &f.recipe,
            f.recipe["node_plan_digest"].as_str().unwrap()
        )
        .unwrap_err()
        .0,
        "member_shard_busy"
    );
    drop(receive);
    assert_eq!(f.charge(), f.bytes.len() as u64);
    let mut receive = MemberShard::resume(
        &f.root,
        "resume",
        "node-0",
        &f.transfer,
        &f.recipe,
        f.recipe["node_plan_digest"].as_str().unwrap(),
    )
    .unwrap();
    assert_eq!(receive.received_bytes().unwrap(), 1);
    receive.write_chunk(1, &f.bytes[1..]).unwrap();
    receive.verify(&|| Ok(())).unwrap();
    receive.discard().unwrap();
    assert_eq!(f.charge(), 0);
}

/// Lane A, release blocker B12 (ledger row 96): the api lib test
/// `member_refund_interrupted_before_ledger_rename_retains_charge_and_recovers` failed now and then
/// with `member_shard_busy` at a resume after the previous holder was dropped. Mechanism: the
/// custody lock is a `flock` on the lock file's open description; a child this process forks
/// (every confined worker spawn runs a pre-exec loop over the whole descriptor table) holds a
/// copy of EVERY descriptor from fork to exec, so a holder dropped in that window leaves its
/// lock held by the child until the child execs. Deterministic model of that window: a child
/// holding a copy of the dropped holder's lock description for a short while (`/bin/sleep 1`
/// inherits it). The next holder must not be refused for a lock nobody in this process holds.
#[test]
fn a_lock_copy_still_held_by_a_forked_child_does_not_refuse_the_next_holder() {
    use std::os::unix::fs::MetadataExt;
    let f = Fixture::new();
    let receive = f.begin("forked");
    let lock = fs::canonicalize(f.root.join("chain-runs/forked/member-shard.lock")).unwrap();
    let inode = fs::metadata(&lock).map(|m| m.ino()).unwrap();
    // The holder's own descriptor of the lock (the description its flock lives on); opening
    // `/dev/fd/N` duplicates that descriptor, as a fork does. (`/dev/fd` reports its own device
    // and the file's inode.)
    let held = (3..4096)
        .find(|n| fs::metadata(format!("/dev/fd/{n}")).is_ok_and(|m| m.ino() == inode))
        .expect("the holder's lock descriptor");
    let copy = fs::File::open(format!("/dev/fd/{held}")).unwrap();
    rustix::io::fcntl_setfd(&copy, rustix::io::FdFlags::empty()).unwrap();
    let mut child = std::process::Command::new("/bin/sleep")
        .arg("1")
        .spawn()
        .unwrap();
    drop(copy);
    drop(receive);
    let resumed = f.resume("forked").map(drop).map_err(|e| e.0);
    child.wait().unwrap();
    assert_eq!(
        resumed,
        Ok(()),
        "a lock only a forked child's inherited copy still held refused the next holder"
    );
}

/// Lane A patch 2, A-01 (REVIEW-laneA-release-astra.md): verification's last checkpoint can pass
/// just before the operation's deadline; `complete` then waits for a busy storage ledger. The
/// wait is bounded by the operation (its checkpoint), not by a fresh close allowance: an
/// operation whose deadline passes while the ledger is still held is refused by its own name at
/// about its deadline, and nothing is committed (the run stays `cleanup_required`, charged).
fn held_ledger(f: &Fixture) -> fs::File {
    let lock = fs::File::open(f.root.join("chain-runs/compiler-storage.lock")).unwrap();
    lock.try_lock().unwrap();
    lock
}
/// The owner's poll (500 ms): this file's waits are multiples of it (no literal durations).
fn poll() -> std::time::Duration {
    std::time::Duration::from_millis(
        budgets::BudgetId::OwnerPoll
            .span(budgets::Entries::ZERO)
            .unwrap()
            .millis(),
    )
}
fn phase(f: &Fixture, run: &str) -> Value {
    let v: Value = serde_json::from_slice(
        &fs::read(f.root.join("chain-runs/compiler-storage.private.json")).unwrap(),
    )
    .unwrap();
    v["entries"][run]["phase"].clone()
}
#[test]
fn a_ledger_wait_ends_at_the_operations_deadline_without_committing() {
    use std::time::Instant;
    let f = Fixture::new();
    let mut receive = f.begin("deadline");
    receive.write_chunk(0, &f.bytes).unwrap();
    let ledger = held_ledger(&f);
    let started = Instant::now();
    let deadline = started + poll() / 2;
    // The other holder lets go well after the deadline, inside a fresh close allowance.
    let holder = std::thread::spawn(move || {
        std::thread::sleep(poll() * 3);
        drop(ledger);
    });
    let got = receive.verify(&|| {
        if Instant::now() >= deadline {
            Err(api::compiler_inputs::Error("member_operation_deadline"))
        } else {
            Ok(())
        }
    });
    let waited = started.elapsed();
    holder.join().unwrap();
    assert_eq!(got.map_err(|e| e.0), Err("member_operation_deadline"));
    assert!(
        waited < poll() * 2,
        "the ledger wait outlived the operation's deadline: {waited:?}"
    );
    assert_eq!(phase(&f, "deadline"), "cleanup_required");
    assert_eq!(f.charge(), f.bytes.len() as u64);
}
/// A-01, commit after the deadline: the ledger frees inside verification's wait for it, and the
/// operation's deadline passes after the wait's last checkpoint but before the commit; the
/// re-check after the acquisition refuses it, nothing committed.
///
/// Lane A patch 3, A2-02 (REVIEW-laneA-p2-astra.md): deterministic, one thread, no timing. A
/// control verification on a free ledger counts the checkpoint calls before the ledger (`reads`:
/// all but its last, the pre-commit re-check). The witness holds the ledger itself: its calls
/// 1..=reads are verification's reads; call reads + 1 is then the busy wait (the ledger is still
/// held by this test), where the test lets the ledger go and the operation is still current;
/// the next try takes the lock without a checkpoint, so call reads + 2 is the re-check after the
/// acquisition, where the deadline has passed. Without that re-check the commit happens.
#[test]
fn a_ledger_freed_after_the_deadline_is_not_committed() {
    use std::cell::{Cell, RefCell};
    let f = Fixture::new();
    let mut control = f.begin("control");
    control.write_chunk(0, &f.bytes).unwrap();
    let count = Cell::new(0);
    control
        .verify(&|| {
            count.set(count.get() + 1);
            Ok(())
        })
        .unwrap();
    let reads = count.get() - 1;
    assert!(
        reads >= 1,
        "verification checks its operation before it reads"
    );
    let mut receive = f.begin("late");
    receive.write_chunk(0, &f.bytes).unwrap();
    let held = RefCell::new(Some(held_ledger(&f)));
    let calls = Cell::new(0);
    let released_at = Cell::new(None);
    let got = receive.verify(&|| {
        let call = calls.get() + 1;
        calls.set(call);
        if call <= reads {
            return Ok(());
        }
        match held.borrow_mut().take() {
            Some(ledger) => {
                released_at.set(Some(call));
                drop(ledger);
                Ok(())
            }
            None => Err(api::compiler_inputs::Error("member_operation_deadline")),
        }
    });
    let (calls, released_at) = (calls.get(), released_at.get());
    assert_eq!(
        released_at,
        Some(reads + 1),
        "the ledger was let go in the busy wait, after verification's reads"
    );
    assert_eq!(got.map_err(|e| e.0), Err("member_operation_deadline"));
    assert_eq!(
        calls,
        reads + 2,
        "the refusal came from the re-check after the acquisition"
    );
    assert_eq!(phase(&f, "late"), "cleanup_required");
    assert_eq!(phase(&f, "control"), "retained");
}
/// Lane A patch 3, A2-01 (REVIEW-laneA-p2-astra.md): taking custody reserves storage under the
/// storage ledger's lock. A busy ledger is waited for under the RECEIVING OPERATION's own bound:
/// an operation whose deadline passes while another holder keeps the ledger is refused by its own
/// name at about its deadline, and no reservation is written (a fresh close allowance would let it
/// commit a `cleanup_required` charge after its operation ended).
#[test]
fn a_reservation_waiting_on_a_busy_ledger_ends_at_the_operations_deadline_unreserved() {
    use std::time::Instant;
    let f = Fixture::new();
    drop(f.begin("first"));
    let ledger = held_ledger(&f);
    let started = Instant::now();
    let deadline = started + poll() / 2;
    // The other holder lets go well after the deadline, inside a fresh close allowance.
    let holder = std::thread::spawn(move || {
        std::thread::sleep(poll() * 3);
        drop(ledger);
    });
    let got = f.begin_checked("deadline", &|| {
        if Instant::now() >= deadline {
            Err(api::compiler_inputs::Error("member_operation_deadline"))
        } else {
            Ok(())
        }
    });
    let waited = started.elapsed();
    holder.join().unwrap();
    assert_eq!(
        got.map(drop).map_err(|e| e.0),
        Err("member_operation_deadline")
    );
    assert!(
        waited < poll() * 2,
        "the reservation's ledger wait outlived the operation's deadline: {waited:?}"
    );
    assert_eq!(phase(&f, "deadline"), Value::Null, "no reservation written");
    assert_eq!(
        f.charge(),
        f.bytes.len() as u64,
        "only the first run is charged"
    );
}
/// A2-01, the ledger freed inside the reservation's wait: the operation is re-checked after the
/// lock is taken and immediately before the reservation is written. Deterministic, one thread:
/// this test holds the ledger, so the checkpoint's FIRST call is the reservation's busy wait (the
/// operation still current: the test lets the ledger go there); the operation ends before the
/// next call, which can only be the re-check after the acquisition.
#[test]
fn a_ledger_freed_inside_the_reservation_wait_is_rechecked_before_reserving() {
    let f = Fixture::new();
    drop(f.begin("first"));
    let held = std::cell::RefCell::new(Some(held_ledger(&f)));
    let calls = std::cell::Cell::new(0);
    let got = f.begin_checked("cancelled", &|| {
        calls.set(calls.get() + 1);
        match held.borrow_mut().take() {
            Some(ledger) => {
                drop(ledger);
                Ok(())
            }
            None => Err(api::compiler_inputs::Error("member_operation_cancelled")),
        }
    });
    let calls = calls.get();
    assert_eq!(
        got.map(drop).map_err(|e| e.0),
        Err("member_operation_cancelled")
    );
    assert_eq!(
        calls, 2,
        "one call in the busy wait (ledger held), then the re-check after acquiring it"
    );
    assert_eq!(
        phase(&f, "cancelled"),
        Value::Null,
        "no reservation written"
    );
    assert_eq!(f.charge(), f.bytes.len() as u64);
}

/// Proves: L13 restart survival (ledger 295, rv50 LAN s2). A joined Mac's retained shard recorded
/// before a restart, when macOS renumbers the volume (`st_dev`; inodes unchanged), still resumes,
/// is no obstacle to the next run's reservation, and is discarded and refunded normally.
#[test]
fn a_restart_that_renumbers_the_volume_keeps_retained_custody_usable() {
    let f = Fixture::new();
    let mut kept = f.begin("kept");
    kept.write_chunk(0, &f.bytes).unwrap();
    kept.verify(&|| Ok(())).unwrap();
    drop(kept);
    let runs = f.root.join("chain-runs");
    let renumber = |path: PathBuf, edit: &dyn Fn(&mut Value)| {
        let mut v: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        edit(&mut v);
        fs::write(&path, protocol::canonical::to_bytes(&v).unwrap()).unwrap();
    };
    let shift = |v: &mut Value| *v = json!(v.as_u64().unwrap() ^ 0x5a5a);
    renumber(runs.join("compiler-storage.private.json"), &|v| {
        shift(&mut v["entries"]["kept"]["run_dev"])
    });
    renumber(runs.join("kept/member-shard.private.json"), &|v| {
        shift(&mut v["file_identity"][0]);
        shift(&mut v["stage_identity"][0]);
    });
    // The field failure: the next run's slice fetch refused `compiler_storage_owner_changed`.
    let mut next = f.begin("next");
    next.discard().unwrap();
    drop(next);
    let mut resumed = f.resume("kept").unwrap();
    assert_eq!(resumed.received_bytes().unwrap(), f.bytes.len() as u64);
    resumed.discard().unwrap();
    drop(resumed);
    assert_eq!(f.charge(), 0);
}
