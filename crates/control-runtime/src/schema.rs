// SPDX-License-Identifier: AGPL-3.0-only
use crate::{Error, Result};
use rusqlite::Connection;
use std::path::Path;

pub(crate) fn initialize(db: &mut Connection, backup: Option<&Path>) -> Result<()> {
    let version: u32 = db.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    if version > 2 {
        return Err(Error::Refused("control_schema_future"));
    }
    let legacy: bool=db.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_auth_requests')",[],|r|r.get(0))?;
    if version == 1 || (version == 0 && legacy) {
        let backup = backup.ok_or(Error::Refused("control_migration_backup_required"))?;
        // Retain an exclusive SQLite lock across the snapshot and schema migration.
        db.execute_batch("PRAGMA locking_mode=EXCLUSIVE; BEGIN EXCLUSIVE; COMMIT;")?;
        backup_verified(db, backup)?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        for (table, column) in [
            ("managed_auth_requests", "valid_until"),
            ("managed_commands", "retired_at"),
        ] {
            let mut query = tx.prepare(&format!("PRAGMA table_info({table})"))?;
            let names = query
                .query_map([], |r| r.get::<_, String>(1))?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            if !names.iter().any(|name| name == column) {
                tx.execute_batch(&format!("ALTER TABLE {table} ADD COLUMN {column} REAL;"))?;
            }
        }
        tx.execute_batch(
            "CREATE INDEX IF NOT EXISTS managed_auth_sequence ON managed_auth_requests(signer_key_digest,sequence);
            CREATE INDEX IF NOT EXISTS managed_claim ON managed_commands(target_key_digest,claimed_request_id)
                WHERE state IN ('claimed','reconciliation_required');",
        )?;
        additions(&tx)?;
        tx.commit()?;
        db.execute_batch("PRAGMA locking_mode=NORMAL;")?;
        return Ok(());
    }
    if version == 2 {
        return Ok(());
    }
    let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    tx.execute_batch("CREATE TABLE managed_auth_requests (
        signer_key_digest TEXT NOT NULL, request_id TEXT NOT NULL, request_digest TEXT NOT NULL,
        sequence INTEGER NOT NULL, nonce TEXT NOT NULL, protocol TEXT NOT NULL, response_json BLOB NOT NULL,
        valid_until REAL, PRIMARY KEY(signer_key_digest,request_id), UNIQUE(signer_key_digest,nonce)
        ) WITHOUT ROWID;
        CREATE INDEX managed_auth_sequence ON managed_auth_requests(signer_key_digest,sequence);
        CREATE TABLE managed_commands (command_id TEXT PRIMARY KEY, target_key_digest TEXT NOT NULL,
        claimed_request_id TEXT, state TEXT NOT NULL, run_id TEXT NOT NULL, deployment_id TEXT NOT NULL,
        updated_at REAL NOT NULL, retired_at REAL);
        CREATE INDEX managed_claim ON managed_commands(target_key_digest,claimed_request_id)
          WHERE state IN ('claimed','reconciliation_required');")?;
    additions(&tx)?;
    tx.commit()?;
    Ok(())
}
fn additions(db: &Connection) -> Result<()> {
    db.execute_batch("CREATE TABLE IF NOT EXISTS managed_replay_history (
        signer_key_digest TEXT NOT NULL,page INTEGER NOT NULL,bits BLOB NOT NULL,
        PRIMARY KEY(signer_key_digest,page)) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS managed_command_retired ON managed_commands(retired_at,updated_at)
            WHERE retired_at IS NOT NULL AND state IN ('completed','failed','expired');
        CREATE TABLE control_high_water (signer_key_digest TEXT PRIMARY KEY,sequence INTEGER NOT NULL) WITHOUT ROWID;
        INSERT INTO control_high_water SELECT signer_key_digest,MAX(sequence) FROM managed_auth_requests GROUP BY signer_key_digest;
        CREATE TABLE control_state (name TEXT PRIMARY KEY, revision INTEGER NOT NULL, document BLOB NOT NULL) WITHOUT ROWID;
        PRAGMA user_version=2;")?;
    Ok(())
}
/// Offline operation: caller owns the database and must quiesce other writers.
/// Destination must be new; failed backups remain inspectable, never overwrite a rollback.
pub fn backup_verified(source: &Connection, path: &Path) -> Result<()> {
    use std::os::unix::fs::OpenOptionsExt;
    let file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    file.sync_all()?;
    let before = fingerprint(source)?;
    source.backup(rusqlite::DatabaseName::Main, path, None)?;
    let copied = Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let check: String = copied.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
    if check != "ok" || before != fingerprint(&copied)? || before != fingerprint(source)? {
        return Err(Error::Refused("control_backup_verification_failed"));
    }
    std::fs::File::open(path)?.sync_all()?;
    use std::io::Write;
    let mut receipt = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(receipt_path(path))?;
    receipt.write_all(hex_digest(&before).as_bytes())?;
    receipt.sync_all()?;
    if let Some(parent) = path.parent() {
        std::fs::File::open(parent)?.sync_all()?;
    }
    Ok(())
}
/// Recover into a NEW path. The installer owns selecting it after all live handles close.
pub fn rollback_verified(backup: &Path, destination: &Path) -> Result<()> {
    let source = Connection::open_with_flags(backup, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let check: String = source.query_row("PRAGMA integrity_check", [], |r| r.get(0))?;
    if check != "ok" {
        return Err(Error::Refused("control_backup_corrupt"));
    }
    use std::io::Read;
    let expected = hex_digest(&fingerprint(&source)?);
    let receipt = std::fs::File::open(receipt_path(backup))?;
    let mut actual = Vec::new();
    receipt
        .take(expected.len() as u64 + 1)
        .read_to_end(&mut actual)?;
    if actual != expected.as_bytes() {
        return Err(Error::Refused("control_backup_digest_mismatch"));
    }
    backup_verified(&source, destination)
}
fn receipt_path(path: &Path) -> std::path::PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(".sha256");
    value.into()
}
fn hex_digest(digest: &[u8]) -> String {
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}
fn fingerprint(db: &Connection) -> Result<Vec<u8>> {
    use rusqlite::types::ValueRef;
    use sha2::{Digest, Sha256};
    let mut hash = Sha256::new();
    let version: i64 = db.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    hash.update(version.to_be_bytes());
    let mut query =
        db.prepare("SELECT name,type,coalesce(sql,'') FROM sqlite_master ORDER BY name,type")?;
    let objects = query
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
            ))
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;
    for (name, kind, sql) in objects {
        for s in [&name, &kind, &sql] {
            hash.update(s.len().to_be_bytes());
            hash.update(s.as_bytes());
        }
        if kind != "table" {
            continue;
        }
        let table = name.replace('"', "\"\"");
        let mut rows_query = db.prepare(&format!("SELECT * FROM \"{table}\""))?;
        let cols = rows_query.column_count();
        let mut rows = rows_query.query([])?;
        let mut encoded = Vec::new();
        while let Some(row) = rows.next()? {
            let mut h = Sha256::new();
            for col in 0..cols {
                match row.get_ref(col)? {
                    ValueRef::Null => h.update([0]),
                    ValueRef::Integer(n) => {
                        h.update([1]);
                        h.update(n.to_be_bytes());
                    }
                    ValueRef::Real(n) => {
                        h.update([2]);
                        h.update(n.to_bits().to_be_bytes());
                    }
                    ValueRef::Text(s) => {
                        h.update([3]);
                        h.update(s.len().to_be_bytes());
                        h.update(s);
                    }
                    ValueRef::Blob(s) => {
                        h.update([4]);
                        h.update(s.len().to_be_bytes());
                        h.update(s);
                    }
                }
            }
            encoded.push(h.finalize().to_vec());
        }
        encoded.sort();
        hash.update(encoded.len().to_be_bytes());
        for row in encoded {
            hash.update(row);
        }
    }
    Ok(hash.finalize().to_vec())
}
