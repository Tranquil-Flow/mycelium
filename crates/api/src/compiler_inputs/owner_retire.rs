// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's retirement of its own dead compile runs from its storage ledger (lane PHY item 1,
//! ledger rows 8 and 81).
//!
//! v0.1.4 (f4e366e9) has no storage ledger: its chain teardown removes a retired run's
//! `stage-shards`, `temporary` and `compiled` after a proven route stop and keeps the run's logs
//! and metadata (`compile_cache.discard_retired_artifacts`, compile_cache.py:71-99, called from
//! demo_chain.py:1416-1423). v0.2 charges every compiled run to a bounded ledger
//! (`reserve_output`) and had no way to release a charge, so two runs that died after their
//! compile refused every later compile `compiler_storage_quota_exceeded` (physical run 4, 15 Nov).
//!
//! Liveness is the run directory's own lock: the compile's [`InputArtifact`] holds an exclusive
//! `flock(2)` on its run directory from `emit` for as long as a process keeps the run (a compile
//! in flight, a serving run, a failed close's work debt). A pass takes the same lock without
//! waiting, and a busy lock is a live run, skipped. A lost process releases its locks with it, so
//! the next Start's pass retires what a crash left. `reserve_output` itself still never refunds.
//!
//! A free lock proves a run dead only when its process would have held it (PHY-01, lane PHY patch
//! 2). A build before this one (base `3d7d89be` and earlier) writes the same start record
//! (`mycelium.chain_compile_start.v1`) and never takes the lock, so a live older process compiling
//! or serving its run looks exactly like a crashed one. Every build that takes the lock writes
//! [`CUSTODY`] in its start record (`emit_mode`), before any stage reservation; only such a run is
//! retired. A run without the marker is a legacy run: never retired automatically, only reported
//! with its charged bytes ([`Retirement::legacy`]) so the owner can release it by hand once no
//! older Mycelium runs on this Mac.
//!
//! Only the owner's own runs are retired: an entry whose run directory still has the identity the
//! ledger bound (dev, ino; private mode, this user), with the compile's start record and no member
//! custody record (a joined Mac's custody is retired by its own discard,
//! `member_shard::MemberShard::discard`), and whose `stage-shards` holds only the producer's own
//! `model-stage-NNN-of-MMM.safetensors` files (regular, private, one link, this user) within the
//! charged bytes. Anything else is refused by name, left charged and untouched. Every file is
//! removed relative to a directory descriptor opened without following links; every removal is
//! validated first, all of it. The refund is recorded in the run directory before the ledger drops
//! the entry, so a loss between the two is recognised by `reconcile_storage_temporaries` instead
//! of refusing every later compile `compiler_storage_owner_changed`.
//!
//! An absent `stage-shards` in a run directory of the bound identity is accepted as nothing left
//! to remove (unlike the member custody, which keeps a tombstone): the owner's charge is the bytes
//! its producer writes below that one directory, and both the inventory (`storage_inventory`) and
//! the reservation already read an absent output as nothing materialized.
use super::*;
use rustix::fs::{AtFlags, Dir, FileType, Mode, OFlags, openat, statat, unlinkat};
use std::os::unix::fs::OpenOptionsExt;

const REFUND: &str = "compiler-storage-refund.private.json";
const REFUND_PROTOCOL: &str = "mycelium.compiler_storage_refund.v1";
const START: &str = "compile-start.private.json";
const MEMBER_RECORD: &str = "member-shard.private.json";
const STAGE: &str = "stage-shards";
/// A `stage-shards` entry the producer did not write, or a file beyond the charged bytes.
pub const FOREIGN_OUTPUT: &str = "compiler_storage_retire_foreign_output";
/// The run directory no longer has the identity, mode or owner the ledger bound.
pub const OWNER_CHANGED: &str = "compiler_storage_retire_owner_changed";
const BUSY: &str = "compiler_run_busy";
/// The start record's custody marker (lane PHY patch 2, PHY-01): written by every build that
/// holds the run directory's lock for as long as it keeps the run.
pub const CUSTODY: &str = "mycelium.compile_run_flock.v1";

/// What one retirement pass did: the runs whose entries it removed, in ledger order, the runs it
/// left charged with the reason (never silently), and the legacy runs it left charged with their
/// charged bytes.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct Retirement {
    pub retired: Vec<String>,
    pub refused: Vec<(String, &'static str)>,
    pub legacy: Vec<(String, u64)>,
}

fn open_directory(path: &Path) -> std::io::Result<fs::File> {
    fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)
}

/// The run's custody: its run directory opened without following links, of exactly `identity`,
/// and exclusively locked for as long as the handle lives (see the module doc).
pub(super) fn custody(root: &Path, identity: (u64, u64)) -> Result<fs::File> {
    let directory = open_directory(root).map_err(|_| Error("compiler_directory_invalid"))?;
    let meta = directory
        .metadata()
        .map_err(|_| Error("compiler_directory_invalid"))?;
    if !meta.is_dir() || (meta.dev(), meta.ino()) != identity {
        return Err(Error("compiler_directory_invalid"));
    }
    directory.try_lock().map_err(|_| Error(BUSY))?;
    Ok(directory)
}

/// The producer's own stage file names (`planner::source_plan::producer::emit_stage_shards`).
fn stage_name(name: &str) -> bool {
    let Some(rest) = name
        .strip_prefix("model-stage-")
        .and_then(|s| s.strip_suffix(".safetensors"))
    else {
        return false;
    };
    let bytes = rest.as_bytes();
    bytes.len() == 10
        && &bytes[3..7] == b"-of-"
        && bytes[..3].iter().chain(&bytes[7..]).all(u8::is_ascii_digit)
}

enum Outcome {
    /// Not the owner's compile run (a joined Mac's custody, or no compile record).
    NotOwner,
    /// Another holder keeps the run.
    Live,
    /// A run whose start record has no custody marker: its process never took the lock, so a free
    /// lock proves nothing (its charged bytes).
    Legacy(u64),
    /// Stage shards gone and refund recorded; the lock is held until the ledger commits.
    Retired(fs::File),
}

/// Retire every ledger entry of the owner's own compile runs that no process holds any more, and
/// remove their stage shards. Under the ledger's lock; the ledger is committed after each run.
pub fn retire_dead_runs(host: &Path) -> Result<Retirement> {
    let host = fs::canonicalize(host).map_err(|_| Error("compiler_directory_invalid"))?;
    let runs = host.join("chain-runs");
    match fs::symlink_metadata(&runs) {
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Retirement::default()),
        _ => {}
    }
    let (_lock, mut ledger) = storage_ledger(&runs)?;
    let rows = ledger["entries"]
        .as_object()
        .ok_or(Error("compiler_storage_ledger_invalid"))?
        .clone();
    let mut done = Retirement::default();
    for (run, row) in &rows {
        match retire_one(&runs, run, row) {
            Ok(Outcome::NotOwner | Outcome::Live) => {}
            Ok(Outcome::Legacy(bytes)) => done.legacy.push((run.clone(), bytes)),
            Ok(Outcome::Retired(held)) => {
                ledger["entries"]
                    .as_object_mut()
                    .ok_or(Error("compiler_storage_ledger_invalid"))?
                    .remove(run);
                store_storage_ledger(&runs, &ledger)?;
                drop(held);
                done.retired.push(run.clone());
            }
            Err(error) => done.refused.push((run.clone(), error.0)),
        }
    }
    Ok(done)
}

fn retire_one(runs: &Path, run: &str, row: &Value) -> Result<Outcome> {
    let changed = || Error(OWNER_CHANGED);
    let root = runs.join(run);
    let directory = open_directory(&root).map_err(|_| changed())?;
    let meta = directory.metadata().map_err(|_| changed())?;
    if !meta.is_dir()
        || meta.mode() & 0o7777 != 0o700
        || meta.uid() != rustix::process::getuid().as_raw()
        || row["run_dev"] != meta.dev()
        || row["run_ino"] != meta.ino()
    {
        return Err(changed());
    }
    match statat(&directory, MEMBER_RECORD, AtFlags::SYMLINK_NOFOLLOW) {
        Err(rustix::io::Errno::NOENT) => {}
        Ok(_) => return Ok(Outcome::NotOwner),
        Err(_) => return Err(changed()),
    }
    let max = private_state::private_json_limit().map_err(state)?;
    match private_state::load(&root.join(START), max) {
        Ok(Some(start)) if start.get("run_id") == Some(&json!(run)) => {
            if start.get("custody") != Some(&json!(CUSTODY)) {
                return Ok(Outcome::Legacy(row["bytes"].as_u64().unwrap_or(0)));
            }
        }
        Ok(None) => return Ok(Outcome::NotOwner),
        _ => return Err(changed()),
    }
    match directory.try_lock() {
        Ok(()) => {}
        Err(fs::TryLockError::WouldBlock) => return Ok(Outcome::Live),
        Err(fs::TryLockError::Error(_)) => return Err(changed()),
    }
    // Recheck the path still names the locked directory before anything below it is removed.
    let named = fs::symlink_metadata(&root).map_err(|_| changed())?;
    if (named.dev(), named.ino()) != (meta.dev(), meta.ino()) {
        return Err(changed());
    }
    match statat(&directory, STAGE, AtFlags::SYMLINK_NOFOLLOW) {
        Err(rustix::io::Errno::NOENT) => {}
        Ok(_) => remove_stage(&directory, row)?,
        Err(_) => return Err(changed()),
    }
    private_state::store(
        &root.join(REFUND),
        &json!({"protocol":REFUND_PROTOCOL,"reservation":row}),
        max,
    )
    .map_err(state)?;
    Ok(Outcome::Retired(directory))
}

/// Validate every entry of the run's `stage-shards`, then remove them and the directory, all
/// relative to descriptors opened without following links.
fn remove_stage(run: &fs::File, row: &Value) -> Result<()> {
    let foreign = || Error(FOREIGN_OUTPUT);
    let flags = OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    let stage = fs::File::from(openat(run, STAGE, flags, Mode::empty()).map_err(|_| foreign())?);
    let meta = stage.metadata().map_err(|_| foreign())?;
    let uid = rustix::process::getuid().as_raw();
    if !meta.is_dir() || meta.mode() & 0o7777 != 0o700 || meta.uid() != uid {
        return Err(foreign());
    }
    let charged = row["bytes"].as_u64().ok_or(foreign())?;
    let mut names = Vec::new();
    let mut total = 0u64;
    let mut listing = Dir::new(openat(&stage, c".", flags, Mode::empty()).map_err(|_| foreign())?)
        .map_err(|_| foreign())?;
    while let Some(entry) = listing.read() {
        let entry = entry.map_err(|_| foreign())?;
        let name = entry.file_name();
        if name == c"." || name == c".." {
            continue;
        }
        if names.len() >= MAX_HOSTS || !name.to_str().is_ok_and(stage_name) {
            return Err(foreign());
        }
        let stat = statat(&stage, name, AtFlags::SYMLINK_NOFOLLOW).map_err(|_| foreign())?;
        if FileType::from_raw_mode(stat.st_mode) != FileType::RegularFile
            || stat.st_nlink != 1
            || u32::from(stat.st_mode) & 0o7777 != 0o600
            || stat.st_uid != uid
        {
            return Err(foreign());
        }
        total = u64::try_from(stat.st_size)
            .ok()
            .and_then(|size| total.checked_add(size))
            .ok_or(foreign())?;
        names.push(name.to_owned());
    }
    if total > charged {
        return Err(foreign());
    }
    let failed = || Error("compiler_storage_retire_failed");
    for name in &names {
        unlinkat(&stage, name.as_c_str(), AtFlags::empty()).map_err(|_| failed())?;
    }
    stage.sync_all().map_err(|_| failed())?;
    unlinkat(run, STAGE, AtFlags::REMOVEDIR).map_err(|_| failed())?;
    run.sync_all().map_err(|_| failed())
}

/// A discarded replacement ledger may omit a committed owner row only when this exact run
/// directory recorded the refund of exactly that row and has no stage shards left.
pub(super) fn refund_matches(runs: &Path, run: &str, row: &Value) -> bool {
    let root = runs.join(run);
    let Ok(meta) = fs::symlink_metadata(&root) else {
        return false;
    };
    if !meta.is_dir() || row["run_dev"] != meta.dev() || row["run_ino"] != meta.ino() {
        return false;
    }
    if !matches!(fs::symlink_metadata(root.join(STAGE)), Err(e) if e.kind() == std::io::ErrorKind::NotFound)
    {
        return false;
    }
    let Ok(max) = private_state::private_json_limit() else {
        return false;
    };
    matches!(private_state::load(&root.join(START), max), Ok(Some(v)) if v.get("run_id") == Some(&json!(run)))
        && matches!(private_state::load(&root.join(REFUND), max),
            Ok(Some(v)) if v.get("protocol") == Some(&json!(REFUND_PROTOCOL))
                && v.get("reservation") == Some(row))
}

#[cfg(test)]
mod tests {
    #[test]
    fn only_the_producers_stage_names_are_its_own() {
        for name in [
            "model-stage-001-of-002.safetensors",
            "model-stage-123-of-999.safetensors",
        ] {
            assert!(super::stage_name(name), "{name}");
        }
        for name in [
            "notes.txt",
            "model-stage-1-of-2.safetensors",
            "model-stage-001-of-002.safetensors.tmp",
            ".model-stage-001-of-002.safetensors",
            "model-stage-00a-of-002.safetensors",
            "model-stage-001_of-002.safetensors",
        ] {
            assert!(!super::stage_name(name), "{name}");
        }
    }
}
