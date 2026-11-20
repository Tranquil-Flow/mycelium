// SPDX-License-Identifier: AGPL-3.0-only
//! Identity-bound retirement, never pathname unlink of a data file. The owned
//! inode is captured with exclusive rename, checked, truncated by descriptor and
//! retained as a zero-byte audit tombstone. A swapped foreign inode is preserved.
//! Durable intent + the actual tombstone, not absence, authorize restart/refund.
use super::*;
use rustix::fs::{AtFlags, Mode, OFlags, RenameFlags, openat, renameat_with, unlinkat};
const INTENT: &str = "member-shard-cleanup.private.json";
const RETIRED: &str = "member-shard-retired.bin";
fn bad() -> Error {
    fail("member_shard_identity_changed")
}
fn open(dir: &fs::File, name: &str, flags: OFlags) -> Result<fs::File> {
    openat(
        dir,
        name,
        flags | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map(fs::File::from)
    .map_err(|_| bad())
}
/// The device of an opened directory, read now: the anchor of the identities persisted under it.
fn fd_volume(dir: &fs::File) -> Result<u64> {
    dir.metadata().map(|m| m.dev()).map_err(|_| bad())
}
fn root_fd(root: &Path, row: &Value) -> Result<fs::File> {
    let anchor =
        crate::compiler_inputs::volume_of(root.parent().ok_or(bad())?).map_err(|_| bad())?;
    let f = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(root)
        .map_err(|_| bad())?;
    let m = f.metadata().map_err(|_| bad())?;
    if !m.is_dir()
        || m.mode() & 0o7777 != 0o700
        || m.uid() != rustix::process::getuid().as_raw()
        || !crate::compiler_inputs::persisted_identity(
            &row["run_dev"],
            &row["run_ino"],
            (m.dev(), m.ino()),
            anchor,
        )
    {
        return Err(bad());
    }
    Ok(f)
}
fn expected(record: &Value, row: &Value) -> Value {
    json!({"protocol":"mycelium.member_shard_cleanup.v2","custody":record,"reservation":row,"retired_name":RETIRED})
}
pub(super) fn has_intent(root: &Path) -> Result<bool> {
    match fs::symlink_metadata(root.join(INTENT)) {
        Ok(_) => Ok(true),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(false),
        Err(_) => Err(bad()),
    }
}
fn intent(root: &Path, record: &Value, row: &Value) -> Result<()> {
    let v = private_state::load(
        &root.join(INTENT),
        private_state::private_json_limit().map_err(state)?,
    )
    .map_err(state)?
    .ok_or(bad())?;
    if Value::Object(v) != expected(record, row) {
        return Err(bad());
    }
    Ok(())
}
fn captured_file(dir: &fs::File, record: &Value) -> Result<fs::File> {
    let file = open(dir, RETIRED, OFlags::RDWR)?;
    let id = file_identity(&file)?;
    if !crate::compiler_inputs::persisted_pair(&record["file_identity"], id, fd_volume(dir)?) {
        return Err(bad());
    }
    Ok(file)
}
pub(super) fn captured(root: &Path, record: &Value, row: &Value) -> Result<()> {
    intent(root, record, row)?;
    captured_file(&root_fd(root, row)?, record)?;
    Ok(())
}
pub(super) fn proven(root: &Path, record: &Value, row: &Value) -> Result<()> {
    intent(root, record, row)?;
    let dir = root_fd(root, row)?;
    let file = captured_file(&dir, record)?;
    if file.metadata().map_err(|_| bad())?.len() != 0 {
        return Err(bad());
    }
    match rustix::fs::statat(&dir, "stage-shards", AtFlags::SYMLINK_NOFOLLOW) {
        Err(rustix::io::Errno::NOENT) => Ok(()),
        _ => Err(bad()),
    }
}
fn stage_matches(dir: &fs::File, record: &Value) -> Result<()> {
    let stage = open(dir, "stage-shards", OFlags::RDONLY | OFlags::DIRECTORY)?;
    let m = stage.metadata().map_err(|_| bad())?;
    if m.mode() & 0o7777 != 0o700
        || m.uid() != rustix::process::getuid().as_raw()
        || !crate::compiler_inputs::persisted_pair(
            &record["stage_identity"],
            (m.dev(), m.ino()),
            fd_volume(dir)?,
        )
    {
        return Err(bad());
    }
    Ok(())
}
pub(super) fn retire(
    root: &Path,
    record: &Value,
    row: &Value,
    checkpoint: &dyn Fn() -> Result<()>,
) -> Result<()> {
    let dir = root_fd(root, row)?;
    if has_intent(root)? {
        intent(root, record, row)?;
    } else {
        stage_matches(&dir, record)?;
        let stage = open(&dir, "stage-shards", OFlags::RDONLY | OFlags::DIRECTORY)?;
        let f = open(&stage, record["name"].as_str().ok_or(bad())?, OFlags::RDWR)?;
        let id = file_identity(&f)?;
        if !crate::compiler_inputs::persisted_pair(&record["file_identity"], id, fd_volume(&dir)?) {
            return Err(bad());
        }
        if !matches!(
            rustix::fs::statat(&dir, RETIRED, AtFlags::SYMLINK_NOFOLLOW),
            Err(rustix::io::Errno::NOENT)
        ) {
            return Err(bad());
        }
        private_state::store(
            &root.join(INTENT),
            &expected(record, row),
            private_state::private_json_limit().map_err(state)?,
        )
        .map_err(state)?;
        dir.sync_all().map_err(|_| bad())?;
    }
    if matches!(
        rustix::fs::statat(&dir, RETIRED, AtFlags::SYMLINK_NOFOLLOW),
        Err(rustix::io::Errno::NOENT)
    ) {
        stage_matches(&dir, record)?;
        let stage = open(&dir, "stage-shards", OFlags::RDONLY | OFlags::DIRECTORY)?;
        let name = record["name"].as_str().ok_or(bad())?;
        let f = open(&stage, name, OFlags::RDWR)?;
        let id = file_identity(&f)?;
        if !crate::compiler_inputs::persisted_pair(&record["file_identity"], id, fd_volume(&dir)?) {
            return Err(bad());
        }
        checkpoint()?;
        // Even a swap after this callback cannot redirect the relative operation
        // through a symlink. Exclusive capture cannot overwrite another file.
        renameat_with(&stage, name, &dir, RETIRED, RenameFlags::NOREPLACE).map_err(|_| bad())?;
        if captured_file(&dir, record).is_err() {
            // Restore only if vacant. If another actor occupied the old name,
            // keep the foreign bytes intact in retirement custody, with full debt.
            let _restore_result =
                renameat_with(&dir, RETIRED, &stage, name, RenameFlags::NOREPLACE);
            return Err(bad());
        }
        stage.sync_all().map_err(|_| bad())?;
        dir.sync_all().map_err(|_| bad())?;
    }
    checkpoint()?;
    // Capture alone is not authority to truncate if the stage was substituted.
    let stage_present = match rustix::fs::statat(&dir, "stage-shards", AtFlags::SYMLINK_NOFOLLOW) {
        Ok(_) => {
            stage_matches(&dir, record)?;
            true
        }
        Err(rustix::io::Errno::NOENT) => false,
        Err(_) => return Err(bad()),
    };
    let file = captured_file(&dir, record)?;
    if !stage_present && file.metadata().map_err(|_| bad())?.len() != 0 {
        return Err(bad());
    }
    file.set_len(0).map_err(|_| bad())?;
    file.sync_all().map_err(|_| bad())?;
    checkpoint()?;
    if stage_present {
        stage_matches(&dir, record)?;
        // AT_REMOVEDIR refuses symlinks/nonempty dirs. Never unlink file paths.
        unlinkat(&dir, "stage-shards", AtFlags::REMOVEDIR).map_err(|_| bad())?;
    }
    dir.sync_all().map_err(|_| bad())?;
    checkpoint()?;
    proven(root, record, row)
}
