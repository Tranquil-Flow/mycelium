// SPDX-License-Identifier: AGPL-3.0-only
//! Byte custody below member admission. Callers must authenticate the independent
//! transfer/recipe pins and current membership before every carrier operation.
//! This type grants no worker execution, membership, resident proof or readiness.
//! Discard is only for inert bytes: a serving owner must first prove worker reap.
use super::*;
use base64::Engine;
use sha2::{Digest, Sha256};
use std::{
    io::{Read, Seek, SeekFrom, Write},
    os::unix::fs::OpenOptionsExt,
};
mod cleanup;
const RECORD: &str = "member-shard.private.json";
const REFUND: &str = "member-shard-refund.private.json";

#[derive(Debug)]
pub struct MemberShard {
    root: PathBuf,
    identity: (u64, u64),
    _lock: fs::File,
    record: Value,
    file: Option<fs::File>,
    file_identity: Option<(u64, u64)>,
    row: Value,
    retained: bool,
    refunded: bool,
}
fn fail(code: &'static str) -> Error {
    Error(code)
}
fn pins(node: &str, transfer: &Value, recipe: &Value, digest: &str) -> Result<Value> {
    let mut body = recipe.clone();
    body.as_object_mut()
        .ok_or(fail("member_shard_recipe_invalid"))?
        .remove("node_plan_digest");
    if recipe["protocol"] != "mycelium.a13_node_slice_plan.v1"
        || recipe["node_id"] != node
        || recipe["node_plan_digest"] != digest
        || protocol::canonical::digest(&body).map_err(|_| fail("member_shard_recipe_invalid"))?
            != digest
    {
        return Err(fail("member_shard_recipe_invalid"));
    }
    let name = transfer["path"]
        .as_str()
        .and_then(|s| s.strip_prefix("deployment/"))
        .filter(|s| segment(s) && !s.starts_with('.'))
        .ok_or(fail("member_shard_path_invalid"))?;
    let size = transfer["size_bytes"]
        .as_u64()
        .filter(|n| *n > 0)
        .ok_or(fail("member_shard_length_mismatch"))?;
    let hash = transfer["content_digest"]
        .as_str()
        .and_then(|s| s.strip_prefix("sha256:"))
        .filter(|s| {
            s.len() == 64
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
        .ok_or(fail("member_shard_hash_invalid"))?;
    let files = recipe["files"]
        .as_array()
        .ok_or(fail("member_shard_recipe_invalid"))?;
    let mut matching = files.iter().filter(|r| r["path"] == transfer["path"]);
    let row = matching.next().ok_or(fail("member_shard_recipe_invalid"))?;
    if matching.next().is_some()
        || row["size_bytes"] != size
        || row["content_digest"] != format!("sha256:{hash}")
    {
        return Err(fail("member_shard_recipe_invalid"));
    }
    let segments = row["segments"]
        .as_array()
        .filter(|s| !s.is_empty() && s.len() <= 16384)
        .ok_or(fail("member_shard_recipe_invalid"))?;
    let mut end = 0u64;
    for s in segments {
        let length = s["length"]
            .as_u64()
            .filter(|n| *n > 0)
            .ok_or(fail("member_shard_recipe_invalid"))?;
        if s["offset"].as_u64() != Some(end) {
            return Err(fail("member_shard_recipe_invalid"));
        }
        end = end
            .checked_add(length)
            .filter(|n| *n <= size)
            .ok_or(fail("member_shard_recipe_invalid"))?;
    }
    if end != size {
        return Err(fail("member_shard_recipe_invalid"));
    }
    Ok(
        json!({"protocol":"mycelium.member_shard_custody.v1", "node_id":node,
        "name":name,"transfer":transfer,"recipe":recipe,"recipe_digest":digest}),
    )
}
fn lock(root: &Path) -> Result<fs::File> {
    let f = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(root.join("member-shard.lock"))
        .map_err(|_| fail("member_shard_lock_failed"))?;
    file_identity(&f)?;
    f.try_lock().map_err(|_| fail("member_shard_busy"))?;
    Ok(f)
}
fn file_identity(file: &fs::File) -> Result<(u64, u64)> {
    let m = file
        .metadata()
        .map_err(|_| fail("member_shard_identity_changed"))?;
    if !m.is_file()
        || m.nlink() != 1
        || m.mode() & 0o7777 != 0o600
        || m.uid() != rustix::process::getuid().as_raw()
    {
        return Err(fail("member_shard_identity_changed"));
    }
    Ok((m.dev(), m.ino()))
}
fn sync_dir(path: &Path) -> Result<()> {
    fs::File::open(path)
        .and_then(|f| f.sync_all())
        .map_err(|_| fail("member_shard_sync_failed"))
}
impl MemberShard {
    /// Reserve before writing. Pins are supplied by the admission owner, NOT
    /// inferred from arriving bytes. No source checkpoint or InputArtifact needed.
    pub fn begin(
        host: &Path,
        run: &str,
        node: &str,
        transfer: &Value,
        recipe: &Value,
        digest: &str,
    ) -> Result<Self> {
        let mut record = pins(node, transfer, recipe, digest)?;
        if !segment(run) {
            return Err(fail("member_shard_path_invalid"));
        }
        let host = fs::canonicalize(host).map_err(|_| fail("compiler_directory_invalid"))?;
        let runs = host.join("chain-runs");
        directory(&runs)?;
        let root = runs.join(run);
        fs::DirBuilder::new()
            .mode(0o700)
            .create(&root)
            .map_err(|_| fail("member_shard_run_reused"))?;
        let identity = directory(&root)?;
        let lock = lock(&root)?;
        let reservation = reserve_output(
            &runs,
            run,
            identity,
            transfer["size_bytes"].as_u64().unwrap(),
        )?;
        private_state::store(
            &root.join(RECORD),
            &record,
            private_state::private_json_limit().map_err(state)?,
        )
        .map_err(state)?;
        let stage_id = directory(&root.join("stage-shards"))?;
        record["stage_identity"] = json!([stage_id.0, stage_id.1]);
        let file = fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(
                root.join("stage-shards")
                    .join(record["name"].as_str().unwrap()),
            )
            .map_err(|_| fail("member_shard_create_failed"))?;
        let id = file_identity(&file)?;
        record["file_identity"] = json!([id.0, id.1]);
        private_state::store(
            &root.join(RECORD),
            &record,
            private_state::private_json_limit().map_err(state)?,
        )
        .map_err(state)?;
        let file_identity = Some(id);
        sync_dir(&root.join("stage-shards"))?;
        sync_dir(&root)?;
        Ok(Self {
            root,
            identity,
            _lock: lock,
            record,
            file: Some(file),
            file_identity,
            row: reservation.row,
            retained: false,
            refunded: false,
        })
    }
    /// Resume inert custody, never a worker. New authenticated pins must equal the
    /// durable record. A dropped handle leaves the full charge and partial bytes.
    pub fn resume(
        host: &Path,
        run: &str,
        node: &str,
        transfer: &Value,
        recipe: &Value,
        digest: &str,
    ) -> Result<Self> {
        let mut record = pins(node, transfer, recipe, digest)?;
        if !segment(run) {
            return Err(fail("member_shard_path_invalid"));
        }
        let host = fs::canonicalize(host).map_err(|_| fail("compiler_directory_invalid"))?;
        let runs = host.join("chain-runs");
        directory(&runs)?;
        let root = runs.join(run);
        // Reopen only; never create a missing owner while reconciling debt.
        let m = fs::symlink_metadata(&root).map_err(|_| fail("member_shard_owner_missing"))?;
        let identity = directory(&root)?;
        if (m.dev(), m.ino()) != identity {
            return Err(fail("member_shard_identity_changed"));
        }
        let lock = lock(&root)?;
        let (_, ledger) = storage_ledger(&runs)?;
        storage_inventory(
            &runs,
            ledger["entries"]
                .as_object()
                .ok_or(fail("compiler_storage_ledger_invalid"))?,
        )?;
        let mut row = ledger["entries"][run].clone();
        let refunded = row.is_null();
        if refunded {
            let saved = private_state::load(
                &root.join(REFUND),
                private_state::private_json_limit().map_err(state)?,
            )
            .map_err(|e| named_state(&root.join(REFUND), e))?
            .ok_or(fail("compiler_storage_reservation_changed"))?;
            row = saved
                .get("reservation")
                .cloned()
                .ok_or(fail("compiler_storage_reservation_changed"))?;
            if !refund_matches(&runs, run, &row) {
                return Err(fail("compiler_storage_reservation_changed"));
            }
        }
        if row["run_dev"] != identity.0
            || row["run_ino"] != identity.1
            || row["bytes"] != transfer["size_bytes"]
        {
            return Err(fail("member_shard_identity_changed"));
        }
        let saved = private_state::load(
            &root.join(RECORD),
            private_state::private_json_limit().map_err(state)?,
        )
        .map_err(|e| named_state(&root.join(RECORD), e))?;
        let saved = Value::Object(saved.ok_or(fail("member_shard_pins_changed"))?);
        record["file_identity"] = saved["file_identity"].clone();
        record["stage_identity"] = saved["stage_identity"].clone();
        if saved != record {
            return Err(fail("member_shard_pins_changed"));
        }
        let expected_id = (
            record["file_identity"][0]
                .as_u64()
                .ok_or(fail("member_shard_identity_changed"))?,
            record["file_identity"][1]
                .as_u64()
                .ok_or(fail("member_shard_identity_changed"))?,
        );
        let path = root
            .join("stage-shards")
            .join(record["name"].as_str().unwrap());
        let file = match fs::symlink_metadata(root.join("stage-shards")) {
            Ok(_) => {
                let id = directory(&root.join("stage-shards"))?;
                if record["stage_identity"] != json!([id.0, id.1]) {
                    return Err(fail("member_shard_identity_changed"));
                }
                match fs::OpenOptions::new()
                    .read(true)
                    .write(true)
                    .custom_flags(libc::O_NOFOLLOW)
                    .open(path)
                {
                    Ok(file) => Some(file),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => None, // retry unlink-before-directory-sync
                    Err(_) => return Err(fail("member_shard_read_failed")),
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => None, // discard retry after directory removal
            Err(_) => return Err(fail("member_shard_read_failed")),
        };
        // Absence alone is never evidence of deletion. Only a captured inode
        // with a matching durable cleanup intent may resume cleanup-only custody.
        if file.is_none() {
            cleanup::captured(&root, &record, &row)?;
        }
        let file_identity = file.as_ref().map(file_identity).transpose()?;
        if file_identity.is_some_and(|id| id != expected_id) {
            return Err(fail("member_shard_identity_changed"));
        }
        let retained = row["phase"] == "retained";
        Ok(Self {
            root,
            identity,
            _lock: lock,
            record,
            file,
            file_identity,
            row,
            retained,
            refunded,
        })
    }
    pub fn path(&self) -> PathBuf {
        self.root
            .join("stage-shards")
            .join(self.record["name"].as_str().unwrap())
    }
    fn check(&self) -> Result<()> {
        if cleanup::has_intent(&self.root)? {
            return Err(fail("member_shard_discarding"));
        }
        if self.refunded
            || fs::canonicalize(&self.root).map_err(|_| fail("member_shard_identity_changed"))?
                != self.root
            || directory(&self.root)? != self.identity
        {
            return Err(fail("member_shard_identity_changed"));
        }
        match fs::symlink_metadata(self.root.join("stage-shards")) {
            Ok(m) => {
                if !m.is_dir()
                    || m.mode() & 0o7777 != 0o700
                    || m.uid() != rustix::process::getuid().as_raw()
                    || self.record["stage_identity"] != json!([m.dev(), m.ino()])
                {
                    return Err(fail("member_shard_identity_changed"));
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound && self.file.is_none() => {
                return Ok(());
            }
            Err(_) => return Err(fail("member_shard_identity_changed")),
        }
        if let Some(f) = &self.file {
            let id = file_identity(f)?;
            let path = fs::symlink_metadata(self.path())
                .map_err(|_| fail("member_shard_identity_changed"))?;
            if Some(id) != self.file_identity || !path.is_file() || (path.dev(), path.ino()) != id {
                return Err(fail("member_shard_identity_changed"));
            }
        }
        Ok(())
    }
    pub fn received_bytes(&self) -> Result<u64> {
        self.check()?;
        self.file
            .as_ref()
            .ok_or(fail("member_shard_discarding"))?
            .metadata()
            .map(|m| m.len())
            .map_err(|_| fail("member_shard_read_failed"))
    }
    /// Fetch through the existing signed artifact carrier, bind its object to
    /// the independently pinned transfer, then durably append. Recheck current
    /// admission before and after I/O; a revoked result is never written.
    #[allow(clippy::too_many_arguments)]
    pub fn fetch_chunk<C: transport::Clock>(
        &mut self,
        session: &mut runtime::iroh_artifact::ChunkSession<'_, C>,
        envelope: &Value,
        request: &protocol::artifact_grant::ChunkRequest,
        grant: &protocol::artifact_grant::Grant,
        deadline: budgets::MonoInstant,
        checkpoint: &dyn Fn() -> Result<()>,
    ) -> std::result::Result<(), runtime::iroh_artifact::ArtifactError> {
        self.fetch_chunk_inner(
            session, envelope, request, grant, deadline, checkpoint, false,
        )
    }
    /// Opt in to checking cancellation/current admission during a stalled read.
    #[allow(clippy::too_many_arguments)]
    pub fn fetch_chunk_checked<C: transport::Clock>(
        &mut self,
        session: &mut runtime::iroh_artifact::ChunkSession<'_, C>,
        envelope: &Value,
        request: &protocol::artifact_grant::ChunkRequest,
        grant: &protocol::artifact_grant::Grant,
        deadline: budgets::MonoInstant,
        checkpoint: &dyn Fn() -> Result<()>,
    ) -> std::result::Result<(), runtime::iroh_artifact::ArtifactError> {
        self.fetch_chunk_inner(
            session, envelope, request, grant, deadline, checkpoint, true,
        )
    }
    #[allow(clippy::too_many_arguments)]
    fn fetch_chunk_inner<C: transport::Clock>(
        &mut self,
        session: &mut runtime::iroh_artifact::ChunkSession<'_, C>,
        envelope: &Value,
        request: &protocol::artifact_grant::ChunkRequest,
        grant: &protocol::artifact_grant::Grant,
        deadline: budgets::MonoInstant,
        checkpoint: &dyn Fn() -> Result<()>,
        responsive: bool,
    ) -> std::result::Result<(), runtime::iroh_artifact::ArtifactError> {
        use runtime::iroh_artifact::ArtifactError;
        let local = |e: Error| ArtifactError(e.0.into());
        checkpoint().map_err(local)?;
        self.check().map_err(local)?;
        let size = self.record["transfer"]["size_bytes"].as_u64().unwrap();
        let cap = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63
            .bytes()
            .map_err(|_| ArtifactError("budget_unavailable".into()))?
            .get();
        if self.record["transfer"]["content_digest"] != request.object_digest
            || grant.recipient_node_id != self.record["node_id"]
            || request.length_bytes == 0
            || request.length_bytes > cap
            || request
                .offset_bytes
                .checked_add(request.length_bytes)
                .is_none_or(|end| end > size)
        {
            return Err(ArtifactError("member_shard_request_mismatch".into()));
        }
        let bytes = if responsive {
            session.fetch_checked(envelope, request, grant, deadline, &|| {
                checkpoint().map_err(local)
            })?
        } else {
            session.fetch(envelope, request, grant, deadline)?
        };
        checkpoint().map_err(local)?;
        self.write_chunk(request.offset_bytes, &bytes)
            .map_err(local)
    }

    /// Bounded append or exact duplicate retry. Never sparse-write, overwrite,
    /// truncate or acknowledge bytes before fsync. Caller owns transport deadlines.
    pub fn write_chunk(&mut self, offset: u64, bytes: &[u8]) -> Result<()> {
        self.check()?;
        if self.retained {
            return Err(fail("member_shard_already_verified"));
        }
        let cap = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63
            .bytes()
            .map_err(|_| fail("budget_unavailable"))?
            .get();
        let end = offset
            .checked_add(bytes.len() as u64)
            .ok_or(fail("member_shard_chunk_invalid"))?;
        let current = self.received_bytes()?;
        if bytes.is_empty()
            || bytes.len() as u64 > cap
            || end > self.record["transfer"]["size_bytes"].as_u64().unwrap()
            || offset > current
        {
            return Err(fail("member_shard_chunk_invalid"));
        }
        let f = self.file.as_mut().ok_or(fail("member_shard_discarding"))?;
        f.seek(SeekFrom::Start(offset))
            .map_err(|_| fail("member_shard_write_failed"))?;
        if offset < current {
            if end > current {
                return Err(fail("member_shard_retry_mismatch"));
            }
            let mut prior = vec![0; bytes.len()];
            f.read_exact(&mut prior)
                .map_err(|_| fail("member_shard_read_failed"))?;
            if prior != bytes {
                return Err(fail("member_shard_retry_mismatch"));
            }
        } else {
            f.write_all(bytes)
                .map_err(|_| fail("member_shard_write_failed"))?;
        }
        f.sync_all().map_err(|_| fail("member_shard_sync_failed"))?;
        self.check()
    }
    /// Verify transfer and each recipe range without source access. The checkpoint
    /// is called per bounded read: hosts must use it for cancellation, absolute
    /// deadlines and current admission. This is not native tensor qualification.
    pub fn verify(&mut self, checkpoint: &dyn Fn() -> Result<()>) -> Result<()> {
        checkpoint()?;
        let size = self.record["transfer"]["size_bytes"].as_u64().unwrap();
        if self.received_bytes()? != size {
            return Err(fail("member_shard_length_mismatch"));
        }
        let path = self.record["transfer"]["path"].clone();
        let row = self.record["recipe"]["files"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["path"] == path)
            .unwrap()
            .clone();
        let f = self.file.as_mut().ok_or(fail("member_shard_discarding"))?;
        if hash_range(f, 0, size, checkpoint)? != self.record["transfer"]["content_digest"] {
            return Err(fail("member_shard_hash_mismatch"));
        }
        for s in row["segments"].as_array().unwrap() {
            let offset = s["offset"].as_u64().unwrap();
            let len = s["length"].as_u64().unwrap();
            if hash_range(f, offset, len, checkpoint)? != s["sha256"] {
                return Err(fail("member_shard_recipe_mismatch"));
            }
            if let Some(encoded) = s.get("inline_base64") {
                let cap = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxHeaderBytesL67
                    .bytes()
                    .map_err(|_| fail("budget_unavailable"))?
                    .get();
                if len > cap {
                    return Err(fail("member_shard_recipe_mismatch"));
                }
                let raw = base64::engine::general_purpose::STANDARD
                    .decode(
                        encoded
                            .as_str()
                            .ok_or(fail("member_shard_recipe_mismatch"))?,
                    )
                    .map_err(|_| fail("member_shard_recipe_mismatch"))?;
                if raw.len() as u64 != len {
                    return Err(fail("member_shard_recipe_mismatch"));
                }
                f.seek(SeekFrom::Start(offset))
                    .map_err(|_| fail("member_shard_read_failed"))?;
                let mut actual = vec![0; raw.len()];
                f.read_exact(&mut actual)
                    .map_err(|_| fail("member_shard_read_failed"))?;
                if actual != raw {
                    return Err(fail("member_shard_recipe_mismatch"));
                }
            }
        }
        checkpoint()?;
        self.check()?;
        if !self.retained {
            ShardReservation {
                runs: self.root.parent().unwrap().into(),
                run: self.root.file_name().unwrap().to_str().unwrap().into(),
                row: self.row.clone(),
            }
            .complete()?;
            self.row["phase"] = json!("retained");
            self.retained = true;
        }
        Ok(())
    }
    /// Explicit inert-byte cleanup; no refund on Drop. The handle retains the
    /// run lock through inode retirement, fsync, durable refund marker and ledger
    /// commit. Failed cleanup leaves the charge and is retryable.
    pub fn discard(&mut self) -> Result<()> {
        self.discard_with_checkpoint(&|| Ok(()))
    }
    /// Inert cleanup with cancellation checkpoints; debt survives any refusal.
    pub fn discard_with_checkpoint(&mut self, checkpoint: &dyn Fn() -> Result<()>) -> Result<()> {
        if self.refunded {
            return Ok(());
        }
        if !cleanup::has_intent(&self.root)? {
            self.check()?;
        }
        let runs = self.root.parent().unwrap();
        let run = self.root.file_name().unwrap().to_str().unwrap();
        let (_lock, mut ledger) = storage_ledger(runs)?;
        if ledger["entries"][run].is_null() && refund_matches(runs, run, &self.row) {
            sync_dir(runs)?;
            self.refunded = true;
            return Ok(());
        }
        if ledger["entries"][run] != self.row {
            return Err(fail("compiler_storage_reservation_changed"));
        }
        cleanup::retire(&self.root, &self.record, &self.row, checkpoint)?;
        self.file = None;
        self.file_identity = None;
        sync_dir(&self.root)?;
        private_state::store(
            &self.root.join(REFUND),
            &json!({"protocol":"mycelium.member_shard_refund.v2","reservation":self.row}),
            private_state::private_json_limit().map_err(state)?,
        )
        .map_err(state)?;
        ledger["entries"]
            .as_object_mut()
            .ok_or(fail("compiler_storage_ledger_invalid"))?
            .remove(run);
        store_storage_ledger(runs, &ledger)?;
        self.refunded = true;
        Ok(())
    }
}
fn hash_range(
    file: &mut fs::File,
    offset: u64,
    length: u64,
    checkpoint: &dyn Fn() -> Result<()>,
) -> Result<String> {
    file.seek(SeekFrom::Start(offset))
        .map_err(|_| fail("member_shard_read_failed"))?;
    let cap = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63
        .bytes()
        .map_err(|_| fail("budget_unavailable"))?
        .get();
    let mut buffer = vec![
        0;
        usize::try_from(cap.min(length))
            .map_err(|_| fail("member_shard_length_mismatch"))?
    ];
    let mut left = length;
    let mut hash = Sha256::new();
    while left != 0 {
        checkpoint()?;
        let n = left.min(buffer.len() as u64) as usize;
        file.read_exact(&mut buffer[..n])
            .map_err(|_| fail("member_shard_read_failed"))?;
        hash.update(&buffer[..n]);
        left -= n as u64;
    }
    Ok(format!("sha256:{:x}", hash.finalize()))
}
// A discarded private replacement ledger may omit a committed row ONLY when
// this exact owner has durably proven its output directory gone. No refund is
// inferred from an absent file, Drop or arbitrary candidate-ledger bytes.
pub(super) fn refund_matches(runs: &Path, run: &str, row: &Value) -> bool {
    let root = runs.join(run);
    if !matches!(fs::symlink_metadata(root.join("stage-shards")), Err(e) if e.kind() == std::io::ErrorKind::NotFound)
    {
        return false;
    }
    let Ok(max) = private_state::private_json_limit() else {
        return false;
    };
    let Ok(Some(record)) = private_state::load(&root.join(RECORD), max) else {
        return false;
    };
    cleanup::proven(&root, &Value::Object(record), row).is_ok()
        && matches!(private_state::load(&root.join(REFUND), max), Ok(Some(v)) if v.get("protocol") == Some(&json!("mycelium.member_shard_refund.v2")) && v.get("reservation") == Some(row))
}
