// SPDX-License-Identifier: AGPL-3.0-only
//! The remaining owner-only operator calls of `mycelium_seed/operator.py` @ f4e366e9: `seed_inventory`,
//! `revoke_seed_member` and `backup_seed_state`, over the same private state root as the rotation calls
//! (`_load_authority`: lease, `state.sqlite3`, the one key file bound to the stored digest).
//!
//! Python behaviour kept on purpose (executed-oracle vectors, `FX-L5-seed-operator-r13`):
//! - `revoke_seed_member` validates `reason` first, then loads the authority, and only then the node id and the
//!   generation (`ValueError` escapes the `SeedCoordinatorError` handler); the revocation is durable BEFORE the
//!   clock is read for the answer, so a non-finite clock raises after the member was fenced.
//! - `backup_seed_state` assigns `created` before `mkdir`: a backup id that already exists makes `mkdir` fail and
//!   the cleanup then deletes the PRE-EXISTING directory. Exceptions that are not `SeedOperatorError`, `OSError`,
//!   `sqlite3.Error` or `ValueError` (`SeedAuthorityError`, `NodeIdentityError`, `OverflowError`) leave the
//!   half-written backup directory in place.
use super::{
    ACTIVE_KEY, Arg, ErrorClass, Held, IDENTITY_DIR, Lease, NEXT_KEY, OWNER_DIR, OWNER_FILE,
    OperatorError, PSEUDONYM_FILE, ensure_pseudonym, load_authority, load_node_signer,
    load_pseudonym, open_lease_with, operator, overflow_error, owner_file, py_equal, reason_valid,
    revalidate, sync_directory, value_error, write_private,
};
use crate::{coordinator::is_segment, store};
use budgets::{BudgetId, Entries};
use protocol::seed_authority;
use protocol::{canonical, membership};
use rand_core::{OsRng, RngCore};
use rusqlite::{
    Connection, OpenFlags,
    backup::{Backup, StepResult},
};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::{DirBuilderExt, PermissionsExt},
    path::{Path, PathBuf},
    time::Duration,
};

const INVENTORY_PROTOCOL: &str = "mycelium.seed_operator_inventory.v1";
const REVOCATION_PROTOCOL: &str = "mycelium.seed_operator_revocation.v1";
const BACKUP_PROTOCOL: &str = "mycelium.seed_backup.v1";
const BACKUP_ID_PREFIX: &str = "backup-";
const BACKUP_ID_MAX_CHARS: usize = 64;
const BACKUP_ID_CHARACTERS: &str = "abcdefghijklmnopqrstuvwxyz0123456789-";

/// `int(float(now) * 1000)`: `ValueError` for NaN, `OverflowError` for an infinity.
fn python_millis(instant: f64) -> Result<i64, OperatorError> {
    let scaled = instant * 1000.0;
    if scaled.is_nan() {
        Err(value_error("cannot convert float NaN to integer"))
    } else if scaled.is_infinite() {
        Err(overflow_error("cannot convert float infinity to integer"))
    } else {
        Ok(scaled as i64)
    }
}

/// `seed_inventory`: a privacy-reduced inventory of the durable coordinator store.
pub fn seed_inventory(state_root: &Path, now: &dyn Fn() -> f64) -> Result<Value, OperatorError> {
    let loaded = load_authority(state_root, "seed-operator-inventory")?;
    let observed_at = now();
    let members = loaded
        .coordinator
        .members(None)
        .map_err(|e| operator(&e.code))?;
    let invalid = || operator("seed_operator_inventory_invalid");
    let mut rows = Vec::new();
    for member in &members {
        let lifecycle = member["lifecycle_state"].as_str().ok_or_else(invalid)?;
        let lease_fresh = member["lease_expires_at"].as_f64().ok_or_else(invalid)? > observed_at;
        let policy_eligible = membership::peer_runtime_is_activation_eligible(
            &member["peer_class"],
            &member["runtime_capability"],
        )
        .unwrap_or(false);
        rows.push(json!({
            "node_id": member["node_id"],
            "peer_class": member["peer_class"],
            "generation": member["generation"],
            "incarnation": member["incarnation"],
            "lifecycle_state": lifecycle,
            "lease_freshness": if lease_fresh { "fresh" } else { "expired" },
            "activation_eligible": policy_eligible
                && lease_fresh
                && matches!(lifecycle, "CONFIGURED" | "RUNNING"),
            "revocation_state": if lifecycle == "STOPPED" { "revoked" } else { "active" },
        }));
    }
    let observed_ms = match python_millis(observed_at) {
        Err(e) if e.class == ErrorClass::Value => return Err(invalid()),
        other => other?,
    };
    Ok(json!({
        "protocol": INVENTORY_PROTOCOL,
        "swarm_id": loaded.swarm_id,
        "seed_node_id": loaded.seed_node_id,
        "seed_key_digest": loaded.seed_key_digest,
        "observed_at_unix_ms": observed_ms,
        "members": rows,
        "route_ready": false,
    }))
}

/// `revoke_seed_member`: fence one member generation and persist the terminal lifecycle state.
pub fn revoke_seed_member(
    state_root: &Path,
    node_id: &Arg,
    expected_generation: &Arg,
    reason: &Arg,
    now: &dyn Fn() -> f64,
) -> Result<Value, OperatorError> {
    if !reason_valid(reason) {
        return Err(operator("seed_operator_reason_invalid"));
    }
    let Arg::Text(reason) = reason else {
        return Err(operator("seed_operator_reason_invalid"));
    };
    let loaded = load_authority(state_root, "seed-operator-revoke")?;
    let node = match node_id {
        Arg::Text(node) if is_segment(node) => node,
        _ => return Err(value_error("node_id is invalid")),
    };
    let generation = match expected_generation {
        Arg::Int(g) if *g >= 1 => *g as u64,
        _ => return Err(value_error("expected_generation is invalid")),
    };
    let member = loaded
        .coordinator
        .advance_member_generation(node, generation, "STOPPED")
        .map_err(|e| operator(&e.code))?;
    Ok(json!({
        "protocol": REVOCATION_PROTOCOL,
        "swarm_id": loaded.swarm_id,
        "seed_key_digest": loaded.seed_key_digest,
        "node_id": member["node_id"],
        "previous_generation": generation,
        "generation": member["generation"],
        "lifecycle_state": member["lifecycle_state"],
        "reason": reason,
        "revoked_at_unix_ms": python_millis(now())?,
        "route_ready": false,
    }))
}

fn random_backup_id() -> Arg {
    let mut raw = [0u8; 12];
    OsRng.fill_bytes(&mut raw);
    Arg::Text(format!(
        "{BACKUP_ID_PREFIX}{}",
        raw.iter().map(|b| format!("{b:02x}")).collect::<String>()
    ))
}

/// A fresh random backup id (`backup-` + 24 hex digits), the default of `backup_seed_state`.
pub fn default_backup_id() -> String {
    match random_backup_id() {
        Arg::Text(id) => id,
        _ => String::new(),
    }
}

/// How a `backup_seed_state` step failed, as the Python `try`/`except` ladder sorts it.
enum Fail {
    /// `SeedOperatorError`: clean up, re-raise.
    Operator(OperatorError),
    /// `OSError`, `sqlite3.Error` or `ValueError`: clean up, `seed_operator_backup_failed`.
    Os,
    /// Any other exception: it escapes with the directory left in place.
    Escapes(OperatorError),
}

impl From<OperatorError> for Fail {
    fn from(error: OperatorError) -> Self {
        match error.class {
            ErrorClass::Operator => Self::Operator(error),
            ErrorClass::Value => Self::Os,
            _ => Self::Escapes(error),
        }
    }
}

fn io<T>(result: std::io::Result<T>) -> Result<T, Fail> {
    result.map_err(|_| Fail::Os)
}

fn sqlite_busy() -> Result<Duration, Fail> {
    BudgetId::PyMyceliumSeedOperatorBackupSeedStateTimeoutL758
        .span(Entries::ZERO)
        .map(|s| Duration::from_millis(s.millis()))
        .map_err(|_| Fail::Os)
}

fn backup_id_valid(id: &Arg) -> Option<&str> {
    let Arg::Text(id) = id else { return None };
    (id.starts_with(BACKUP_ID_PREFIX)
        && id.chars().count() <= BACKUP_ID_MAX_CHARS
        && id.chars().all(|c| BACKUP_ID_CHARACTERS.contains(c)))
    .then_some(id.as_str())
}

/// `backup_seed_state`: capture the key and a transactionally consistent database generation.
pub fn backup_seed_state(
    state_root: &Path,
    output_root: &Path,
    now: &dyn Fn() -> f64,
    backup_id_source: &dyn Fn() -> Arg,
) -> Result<Value, OperatorError> {
    let loaded = load_authority(state_root, "seed-operator-backup")?;
    let mut created: Option<PathBuf> = None;
    let result = capture(&loaded, output_root, now, backup_id_source, &mut created);
    match result {
        Ok(value) => Ok(value),
        Err(Fail::Escapes(error)) => Err(error),
        Err(failure) => {
            if let Some(directory) = &created {
                // `shutil.rmtree(..., ignore_errors=True)`: a symbolic link is not removed.
                if fs::symlink_metadata(directory).is_ok_and(|m| !m.file_type().is_symlink()) {
                    fs::remove_dir_all(directory).unwrap_or(());
                }
            }
            match failure {
                Fail::Operator(error) => Err(error),
                _ => Err(operator("seed_operator_backup_failed")),
            }
        }
    }
}

fn capture(
    loaded: &super::Loaded,
    output_root: &Path,
    now: &dyn Fn() -> f64,
    backup_id_source: &dyn Fn() -> Arg,
    created: &mut Option<PathBuf>,
) -> Result<Value, Fail> {
    let output: Held = match open_lease_with(output_root, true) {
        Ok(Lease::Held(held)) => held,
        _ => return Err(Fail::Os),
    };
    revalidate(&output).map_err(|()| Fail::Os)?;
    let id = backup_id_source();
    let backup_id = backup_id_valid(&id)
        .ok_or_else(|| Fail::Operator(operator("seed_operator_backup_id_invalid")))?
        .to_owned();
    let directory = output.path.join(&backup_id);
    *created = Some(directory.clone());
    io(fs::DirBuilder::new().mode(OWNER_DIR).create(&directory))?;
    io(fs::set_permissions(
        &directory,
        fs::Permissions::from_mode(OWNER_DIR),
    ))?;
    let identity_dir = directory.join(IDENTITY_DIR);
    io(fs::DirBuilder::new().mode(OWNER_DIR).create(&identity_dir))?;
    io(fs::set_permissions(
        &identity_dir,
        fs::Permissions::from_mode(OWNER_DIR),
    ))?;

    let state_identity = loaded.lease.path.join(IDENTITY_DIR);
    ensure_pseudonym(
        &state_identity,
        loaded.coordinator.signer(),
        &loaded.swarm_id,
    )?;
    let salt = load_pseudonym(&state_identity.join(PSEUDONYM_FILE))?;

    let mut source_key = state_identity.join(ACTIVE_KEY);
    if !source_key.exists()
        || load_node_signer(&source_key)?.verification_key_digest() != loaded.seed_key_digest
    {
        source_key = state_identity.join(NEXT_KEY);
    }
    owner_file(&source_key, "seed_operator_identity_invalid")?;
    if load_node_signer(&source_key)?.verification_key_digest() != loaded.seed_key_digest {
        return Err(operator("seed_operator_identity_mismatch").into());
    }
    let secret: Vec<u8> = io(fs::read(&source_key))?;
    write_private(&identity_dir.join(ACTIVE_KEY), &secret)?;
    write_private(&identity_dir.join(PSEUDONYM_FILE), &salt)?;

    let database_target = directory.join("state.sqlite3");
    {
        let busy = sqlite_busy()?;
        let source = Connection::open_with_flags(
            loaded.lease.path.join("state.sqlite3"),
            OpenFlags::SQLITE_OPEN_READ_ONLY,
        )
        .map_err(|_| Fail::Os)?;
        source.busy_timeout(busy).map_err(|_| Fail::Os)?;
        let mut target = Connection::open(&database_target).map_err(|_| Fail::Os)?;
        target.busy_timeout(busy).map_err(|_| Fail::Os)?;
        let copy = Backup::new(&source, &mut target).map_err(|_| Fail::Os)?;
        // `Connection.backup()` of Python copies every page in one step (`pages=-1`).
        let step = copy.step(-1).map_err(|_| Fail::Os)?;
        if step != StepResult::Done {
            return Err(Fail::Os);
        }
        drop(copy);
        target
            .execute_batch("PRAGMA synchronous = FULL")
            .map_err(|_| Fail::Os)?;
    }
    io(fs::set_permissions(
        &database_target,
        fs::Permissions::from_mode(OWNER_FILE),
    ))?;
    owner_file(&database_target, "seed_operator_backup_invalid")?;

    let members = loaded
        .coordinator
        .members(None)
        .map_err(|e| Fail::Operator(operator(&e.code)))?;
    let authority_generation = store::read_authority_generation(&database_target)
        .map_err(|_| Fail::Operator(operator("seed_operator_state_invalid")))?;
    let created_at = python_millis(now())?;
    let database_bytes = fs::read(&database_target)
        .map_err(|_| Fail::Operator(operator("seed_operator_backup_invalid")))?;
    let manifest = json!({
        "protocol": BACKUP_PROTOCOL,
        "backup_id": backup_id,
        "created_at_unix_ms": created_at,
        "swarm_id": loaded.swarm_id,
        "seed_node_id": loaded.seed_node_id,
        "seed_key_digest": loaded.seed_key_digest,
        "authority_generation": authority_generation,
        "product_pseudonym_digest": canonical::sha256_prefixed(&salt),
        "database_digest": canonical::sha256_prefixed(&database_bytes),
        "member_count": members.len(),
    });
    let body = canonical::to_bytes(&manifest).map_err(|_| Fail::Os)?;
    write_private(&directory.join("manifest.json"), &body)?;
    sync_directory(&identity_dir)?;
    sync_directory(&directory)?;
    sync_directory(&output.path)?;
    Ok(json!({
        "protocol": BACKUP_PROTOCOL,
        "backup_id": backup_id,
        "seed_key_digest": loaded.seed_key_digest,
        "authority_generation": authority_generation,
        "member_count": members.len(),
        "backup_directory": directory.to_string_lossy(),
        "route_ready": false,
    }))
}

// ---------------------------------------------------------------------------------------------- restore

const RESTORE_MANIFEST: &str = "restore-manifest.json";
const MANIFEST: &str = "manifest.json";
const MANIFEST_KEYS: [&str; 9] = [
    "protocol",
    "backup_id",
    "created_at_unix_ms",
    "swarm_id",
    "seed_node_id",
    "seed_key_digest",
    "authority_generation",
    "database_digest",
    "member_count",
];
const PSEUDONYM_DIGEST_KEY: &str = "product_pseudonym_digest";

/// `_file_digest`: `sha256:` + the hex digest of a file; an unreadable file is `seed_operator_backup_invalid`.
fn file_digest(path: &Path) -> Result<String, Fail> {
    let bytes =
        fs::read(path).map_err(|_| Fail::Operator(operator("seed_operator_backup_invalid")))?;
    Ok(canonical::sha256_prefixed(&bytes))
}

/// `_verify_database_integrity`: a read-only `PRAGMA integrity_check` that answers exactly `ok`.
fn verify_database_integrity(path: &Path) -> Result<(), Fail> {
    let corrupt = || Fail::Operator(operator("seed_operator_backup_database_corrupt"));
    let connection = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|_| corrupt())?;
    let rows: Vec<String> = connection
        .prepare("PRAGMA integrity_check")
        .and_then(|mut statement| {
            statement
                .query_map([], |row| row.get::<_, String>(0))?
                .collect()
        })
        .map_err(|_| corrupt())?;
    if rows == ["ok"] {
        Ok(())
    } else {
        Err(corrupt())
    }
}

/// `_backup_manifest`: the exact, canonical manifest of one backup directory.
fn backup_manifest(backup_root: &Path) -> Result<Value, Fail> {
    let invalid = || Fail::Operator(operator("seed_operator_backup_invalid"));
    let path = backup_root.join(MANIFEST);
    owner_file(&path, "seed_operator_backup_invalid")?;
    let raw = fs::read(&path).map_err(|_| invalid())?;
    let document: Value = serde_json::from_slice(&raw).map_err(|_| invalid())?;
    let Some(object) = document.as_object() else {
        return Err(invalid());
    };
    let keys_ok = object.len() == MANIFEST_KEYS.len()
        && MANIFEST_KEYS.iter().all(|k| object.contains_key(*k))
        || object.len() == MANIFEST_KEYS.len() + 1
            && MANIFEST_KEYS.iter().all(|k| object.contains_key(*k))
            && object.contains_key(PSEUDONYM_DIGEST_KEY);
    if !keys_ok
        || document["protocol"] != BACKUP_PROTOCOL
        || canonical::to_bytes(&document).map_err(|_| invalid())? != raw
    {
        return Err(invalid());
    }
    Ok(document)
}

/// `restore_seed_state`: restore one complete verified generation into an absent private root.
pub fn restore_seed_state(backup_root: &Path, target_root: &Path) -> Result<Value, OperatorError> {
    let mut created = false;
    match restoring(backup_root, target_root, &mut created) {
        Ok(value) => Ok(value),
        // Any other exception leaves the half-written target in place (it escapes the `except` ladder).
        Err(Fail::Escapes(error)) => Err(error),
        Err(failure) => {
            if created {
                // `shutil.rmtree(target_root, ignore_errors=True)`: a symbolic link is not removed.
                if fs::symlink_metadata(target_root).is_ok_and(|m| !m.file_type().is_symlink()) {
                    fs::remove_dir_all(target_root).unwrap_or(());
                }
            }
            match failure {
                Fail::Operator(error) => Err(error),
                _ => Err(operator("seed_operator_restore_failed")),
            }
        }
    }
}

/// A failure of `SqliteSeedState(...)`: `SeedStateError` is part of the `except` ladder, `InviteError` is not.
fn state_failure(error: &crate::SeedError) -> Fail {
    if error.code.starts_with("invite_") {
        Fail::Escapes(OperatorError {
            code: error.code.clone(),
            class: ErrorClass::Invite,
        })
    } else {
        Fail::Os
    }
}

fn restoring(backup_root: &Path, target_root: &Path, created: &mut bool) -> Result<Value, Fail> {
    if fs::symlink_metadata(target_root).is_ok() {
        return Err(Fail::Operator(operator(
            "seed_operator_restore_target_exists",
        )));
    }
    let backup: Held = match open_lease_with(backup_root, false) {
        Ok(Lease::Held(held)) => held,
        Ok(Lease::Missing) => {
            return Err(Fail::Operator(operator("seed_operator_backup_invalid")));
        }
        Err(()) => return Err(Fail::Os),
    };
    revalidate(&backup).map_err(|()| Fail::Os)?;
    let manifest = backup_manifest(&backup.path)?;
    let key_source = backup.path.join(IDENTITY_DIR).join(ACTIVE_KEY);
    let pseudonym_source = backup.path.join(IDENTITY_DIR).join(PSEUDONYM_FILE);
    let database_source = backup.path.join("state.sqlite3");
    let has_pseudonym_digest = manifest.get(PSEUDONYM_DIGEST_KEY).is_some();
    owner_file(&key_source, "seed_operator_backup_invalid")?;
    if has_pseudonym_digest {
        owner_file(&pseudonym_source, "seed_operator_backup_invalid")?;
    }
    owner_file(&database_source, "seed_operator_backup_invalid")?;
    let signer = load_node_signer(&key_source)?;
    if manifest["seed_key_digest"].as_str() != Some(signer.verification_key_digest().as_str()) {
        return Err(Fail::Operator(operator(
            "seed_operator_backup_identity_mismatch",
        )));
    }
    if manifest["database_digest"].as_str() != Some(file_digest(&database_source)?.as_str()) {
        return Err(Fail::Operator(operator(
            "seed_operator_backup_digest_mismatch",
        )));
    }
    verify_database_integrity(&database_source)?;
    let described = store::describe(&database_source).map_err(|e| state_failure(&e))?;
    let same_binding = manifest["swarm_id"].as_str() == Some(described.binding.swarm_id.as_str())
        && manifest["seed_node_id"].as_str() == Some(described.binding.seed_node_id.as_str())
        && manifest["seed_key_digest"].as_str() == Some(described.binding.seed_key_digest.as_str());
    if !same_binding || !py_equal(&json!(described.member_count), &manifest["member_count"]) {
        return Err(Fail::Operator(operator(
            "seed_operator_backup_identity_mismatch",
        )));
    }
    if !py_equal(
        &json!(described.authority_generation),
        &manifest["authority_generation"],
    ) {
        return Err(Fail::Operator(operator(
            "seed_operator_backup_generation_mismatch",
        )));
    }
    let salt: Vec<u8> = if has_pseudonym_digest {
        let salt = fs::read(&pseudonym_source).map_err(|_| Fail::Os)?;
        if salt.len() != 32
            || manifest[PSEUDONYM_DIGEST_KEY].as_str()
                != Some(canonical::sha256_prefixed(&salt).as_str())
        {
            return Err(Fail::Operator(operator(
                "seed_operator_backup_pseudonym_mismatch",
            )));
        }
        salt
    } else {
        let swarm = manifest["swarm_id"].as_str().ok_or(Fail::Os)?;
        seed_authority::derive_product_pseudonym_salt(&signer, swarm)
            .map_err(|_| Fail::Os)?
            .to_vec()
    };

    let target: Held = match open_lease_with(target_root, true) {
        Ok(Lease::Held(held)) => held,
        _ => return Err(Fail::Os),
    };
    *created = true;
    let identity_target = target.path.join(IDENTITY_DIR);
    io(fs::DirBuilder::new()
        .mode(OWNER_DIR)
        .create(&identity_target))?;
    io(fs::set_permissions(
        &identity_target,
        fs::Permissions::from_mode(OWNER_DIR),
    ))?;
    let key_bytes = io(fs::read(&key_source))?;
    write_private(&identity_target.join(ACTIVE_KEY), &key_bytes)?;
    write_private(&identity_target.join(PSEUDONYM_FILE), &salt)?;
    let database_bytes = io(fs::read(&database_source))?;
    write_private(&target.path.join("state.sqlite3"), &database_bytes)?;
    let manifest_body = canonical::to_bytes(&manifest).map_err(|_| Fail::Os)?;
    write_private(&target.path.join(RESTORE_MANIFEST), &manifest_body)?;
    sync_directory(&identity_target)?;
    sync_directory(&target.path)?;
    revalidate(&target).map_err(|()| Fail::Os)?;
    let restored_signer = load_node_signer(&identity_target.join(ACTIVE_KEY))?;
    let restored =
        store::describe(&target.path.join("state.sqlite3")).map_err(|e| state_failure(&e))?;
    if restored_signer.verification_key_digest()
        != manifest["seed_key_digest"].as_str().unwrap_or_default()
        || restored.binding.swarm_id != described.binding.swarm_id
        || restored.binding.seed_node_id != described.binding.seed_node_id
        || restored.binding.seed_key_digest != described.binding.seed_key_digest
    {
        return Err(Fail::Operator(operator(
            "seed_operator_restore_verification_failed",
        )));
    }
    Ok(json!({
        "protocol": BACKUP_PROTOCOL,
        "event": "seed_restored",
        "backup_id": manifest["backup_id"],
        "seed_key_digest": manifest["seed_key_digest"],
        "authority_generation": manifest["authority_generation"],
        "member_count": manifest["member_count"],
        "route_ready": false,
    }))
}
