// SPDX-License-Identifier: AGPL-3.0-only
//! The v0.1.4 seed sqlite schema (`mycelium_seed/state.py` @ f4e366e9, `SQLITE_SCHEMA_VERSION = 8`): the
//! initialisation and migration every open performs, and the import of its rows into the Rust store.
//!
//! - [`initialize`] is `SqliteSeedState._initialize`: one `BEGIN IMMEDIATE` transaction that creates every missing
//!   table, brings a version 1..7 database to version 8 with the same `ALTER TABLE` statements and defaults, refuses
//!   any other version (`seed_state_schema_unsupported`) and any renewal table outside the exact contract
//!   (`seed_state_corrupt`); every sqlite failure is `seed_state_unavailable` and rolls back.
//! - [`read`] decodes the rows the way `load_members` / `_decode_acceptance` do (strict types, canonical JSON) into
//!   the snapshot the Rust coordinator keeps (`protocol::seed_members::SeedTable::snapshot`) plus the exact signed
//!   answers (`join`, `resume`, `renewal` envelopes). This is a ONE-WAY import: afterwards the Rust tables
//!   (`rs_seed_table`, `rs_seed_envelopes`) are authoritative and the v0.1.4 row tables are left as they were.
//!
//! Not imported (listed in `round14-seed-state.md`): the membership-v2 tables (`_ensure_membership_v2_schema`), the
//! signature/relational re-verification of stored heartbeat rows that `_decode_heartbeat_renewal_row` performs on
//! every lookup, and the assignment columns the Rust table does not model (`result_code`, `load_proof_digest`,
//! `runtime_endpoint`).
use crate::coordinator::SeedError;
use protocol::canonical;
use rusqlite::{Connection, types::ValueRef};
use serde_json::{Map, Value, json};

pub(crate) const SCHEMA_VERSION: &str = "8";

fn unavailable() -> SeedError {
    SeedError::new("seed_state_unavailable")
}
fn corrupt() -> SeedError {
    SeedError::new("seed_state_corrupt")
}

const TABLES: &str = "
CREATE TABLE IF NOT EXISTS seed_metadata (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seed_members (
    node_id TEXT PRIMARY KEY NOT NULL,
    endpoint_id TEXT NOT NULL,
    endpoint_addrs_json TEXT NOT NULL,
    peer_class TEXT NOT NULL,
    runtime_capability_json TEXT NOT NULL,
    verification_key_digest TEXT NOT NULL,
    incarnation TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    lease_expires_at REAL NOT NULL,
    last_heartbeat_sequence INTEGER NOT NULL
        CHECK (last_heartbeat_sequence >= 0),
    last_liveness_at REAL NOT NULL,
    next_heartbeat_due_at REAL NOT NULL,
    last_activity_receipt_at REAL,
    active_requests INTEGER NOT NULL CHECK (active_requests >= 0),
    lifecycle_state TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seed_replay (
    node_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    message_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (node_id, generation, message_id),
    FOREIGN KEY (node_id) REFERENCES seed_members(node_id)
        ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seed_emitted_messages (
    message_id TEXT PRIMARY KEY NOT NULL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seed_join_acceptances (
    nonce TEXT PRIMARY KEY NOT NULL,
    invite_token_digest TEXT NOT NULL,
    request_envelope_digest TEXT NOT NULL,
    request_message_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    endpoint_id TEXT NOT NULL,
    verification_key_digest TEXT NOT NULL,
    incarnation TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    acceptance_json TEXT NOT NULL,
    FOREIGN KEY (nonce) REFERENCES consumed_invites(nonce),
    FOREIGN KEY (node_id) REFERENCES seed_members(node_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seed_heartbeat_renewals (
    node_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation >= 1),
    heartbeat_message_id TEXT NOT NULL,
    request_envelope_digest TEXT NOT NULL,
    heartbeat_sequence INTEGER NOT NULL
        CHECK (heartbeat_sequence >= 1),
    heartbeat_json TEXT NOT NULL,
    renewal_message_id TEXT NOT NULL UNIQUE,
    renewal_json TEXT NOT NULL,
    PRIMARY KEY (node_id, generation, heartbeat_message_id),
    FOREIGN KEY (node_id) REFERENCES seed_members(node_id)
        ON DELETE CASCADE,
    FOREIGN KEY (renewal_message_id)
        REFERENCES seed_emitted_messages(message_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seed_assignments (
    assignment_id TEXT PRIMARY KEY NOT NULL,
    node_id TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    deployment_epoch INTEGER NOT NULL CHECK (deployment_epoch >= 1),
    membership_generation INTEGER NOT NULL
        CHECK (membership_generation >= 1),
    accepted INTEGER CHECK (accepted IN (0, 1) OR accepted IS NULL),
    result_code TEXT,
    load_proof_digest TEXT,
    runtime_endpoint TEXT,
    FOREIGN KEY (node_id) REFERENCES seed_members(node_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seed_authority_rotations (
    authority_generation INTEGER PRIMARY KEY NOT NULL
        CHECK (authority_generation >= 2),
    previous_generation INTEGER NOT NULL
        CHECK (previous_generation >= 1),
    old_seed_key_digest TEXT NOT NULL,
    new_seed_key_digest TEXT NOT NULL,
    initiated_at REAL NOT NULL,
    effective_at REAL NOT NULL,
    overlap_expires_at REAL NOT NULL,
    reason TEXT NOT NULL,
    transition_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'COMPLETED'))
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seed_rotation_acknowledgements (
    node_id TEXT NOT NULL,
    member_generation INTEGER NOT NULL
        CHECK (member_generation >= 1),
    authority_generation INTEGER NOT NULL
        CHECK (authority_generation >= 2),
    transition_digest TEXT NOT NULL,
    message_id TEXT NOT NULL UNIQUE,
    acknowledged_at REAL NOT NULL,
    PRIMARY KEY (node_id, authority_generation),
    FOREIGN KEY (node_id) REFERENCES seed_members(node_id)
        ON DELETE CASCADE,
    FOREIGN KEY (authority_generation)
        REFERENCES seed_authority_rotations(authority_generation)
        ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS seed_resume_acceptances (
    request_message_id TEXT PRIMARY KEY NOT NULL,
    request_envelope_digest TEXT NOT NULL,
    node_id TEXT NOT NULL,
    previous_incarnation TEXT NOT NULL,
    accepted_incarnation TEXT NOT NULL,
    previous_generation INTEGER NOT NULL
        CHECK (previous_generation >= 1),
    generation INTEGER NOT NULL CHECK (generation >= 2),
    acceptance_json TEXT NOT NULL,
    FOREIGN KEY (node_id) REFERENCES seed_members(node_id)
        ON DELETE CASCADE
) WITHOUT ROWID;
";

/// The `ALTER TABLE` statements of the version 1..7 -> 8 migration, each guarded by the column's absence.
const MEMBER_COLUMNS: [(&str, &str); 7] = [
    (
        "last_heartbeat_sequence",
        "ALTER TABLE seed_members ADD COLUMN last_heartbeat_sequence INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "peer_class",
        "ALTER TABLE seed_members ADD COLUMN peer_class TEXT NOT NULL DEFAULT 'linux_tbd'",
    ),
    (
        "runtime_capability_json",
        "ALTER TABLE seed_members ADD COLUMN runtime_capability_json TEXT NOT NULL DEFAULT \
         '{\"activation_protocol\":null,\"runtime_backend\":\"tbd\",\"transport\":\"none\"}'",
    ),
    (
        "last_liveness_at",
        "ALTER TABLE seed_members ADD COLUMN last_liveness_at REAL NOT NULL DEFAULT 0",
    ),
    (
        "next_heartbeat_due_at",
        "ALTER TABLE seed_members ADD COLUMN next_heartbeat_due_at REAL NOT NULL DEFAULT 0",
    ),
    (
        "last_activity_receipt_at",
        "ALTER TABLE seed_members ADD COLUMN last_activity_receipt_at REAL",
    ),
    (
        "active_requests",
        "ALTER TABLE seed_members ADD COLUMN active_requests INTEGER NOT NULL DEFAULT 0",
    ),
];
const LIFECYCLE_COLUMN: (&str, &str) = (
    "lifecycle_state",
    "ALTER TABLE seed_members ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'NEW'",
);

/// `SqliteSeedState._connect`: the pragmas every v0.1.4 state connection sets.
pub(crate) fn pragmas(connection: &Connection) -> Result<(), SeedError> {
    connection
        .execute_batch(
            "PRAGMA foreign_keys = ON; PRAGMA trusted_schema = OFF; PRAGMA synchronous = FULL;",
        )
        .map_err(|_| unavailable())
}

/// `SqliteSeedState._initialize` over a connection that already has its busy timeout.
pub(crate) fn initialize(connection: &Connection) -> Result<(), SeedError> {
    pragmas(connection)?;
    let begun = connection.execute_batch(&format!("BEGIN IMMEDIATE;{TABLES}"));
    let outcome = begun
        .map_err(|_| unavailable())
        .and_then(|()| migrate(connection));
    match outcome {
        Ok(()) => connection
            .execute_batch("COMMIT")
            .map_err(|_| unavailable()),
        Err(error) => {
            // `connection.rollback()` is a no-op without an open transaction and raises otherwise.
            if !connection.is_autocommit() {
                connection
                    .execute_batch("ROLLBACK")
                    .map_err(|_| unavailable())?;
            }
            Err(error)
        }
    }
}

fn migrate(connection: &Connection) -> Result<(), SeedError> {
    let version: Option<String> = connection
        .query_row(
            "SELECT value FROM seed_metadata WHERE key = 'schema_version'",
            [],
            |row| row.get(0),
        )
        .map(Some)
        .or_else(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => Ok(None),
            other => Err(other),
        })
        .map_err(|_| unavailable())?;
    match version.as_deref() {
        None => {
            connection
                .execute(
                    "INSERT INTO seed_metadata (key, value) VALUES ('schema_version', ?)",
                    [SCHEMA_VERSION],
                )
                .map_err(|_| unavailable())?;
        }
        Some("1" | "2" | "3" | "4" | "5" | "6" | "7") => {
            let columns = member_columns(connection).map_err(|_| unavailable())?;
            for (name, statement) in MEMBER_COLUMNS.iter().chain([&LIFECYCLE_COLUMN]) {
                if !columns.iter().any(|c| c == name) {
                    connection
                        .execute(statement, [])
                        .map_err(|_| unavailable())?;
                }
            }
            connection
                .execute(
                    "UPDATE seed_metadata SET value = ? WHERE key = 'schema_version'",
                    [SCHEMA_VERSION],
                )
                .map_err(|_| unavailable())?;
        }
        Some(SCHEMA_VERSION) => {}
        Some(_) => return Err(SeedError::new("seed_state_schema_unsupported")),
    }
    validate_heartbeat_renewal_schema(connection)
}

fn member_columns(connection: &Connection) -> rusqlite::Result<Vec<String>> {
    let mut statement = connection.prepare("PRAGMA table_info(seed_members)")?;
    statement
        .query_map([], |row| row.get::<_, String>(1))?
        .collect()
}

type ColumnRow = (i64, String, String, i64, Option<String>, i64);

/// `_validate_heartbeat_renewal_schema`: the renewal table must be exactly the version 8 contract.
fn validate_heartbeat_renewal_schema(connection: &Connection) -> Result<(), SeedError> {
    let expected: Vec<ColumnRow> = vec![
        (0, "node_id", "TEXT", 1, None, 1),
        (1, "generation", "INTEGER", 1, None, 2),
        (2, "heartbeat_message_id", "TEXT", 1, None, 3),
        (3, "request_envelope_digest", "TEXT", 1, None, 0),
        (4, "heartbeat_sequence", "INTEGER", 1, None, 0),
        (5, "heartbeat_json", "TEXT", 1, None, 0),
        (6, "renewal_message_id", "TEXT", 1, None, 0),
        (7, "renewal_json", "TEXT", 1, None, 0),
    ]
    .into_iter()
    .map(
        |(a, b, c, d, e, f): (i64, &str, &str, i64, Option<&str>, i64)| {
            (a, b.to_owned(), c.to_owned(), d, e.map(str::to_owned), f)
        },
    )
    .collect();
    let info = |sql: &str, hidden: bool| -> Result<Vec<ColumnRow>, SeedError> {
        let mut statement = connection.prepare(sql).map_err(|_| unavailable())?;
        let rows = statement
            .query_map([], |row| {
                if hidden && row.get::<_, i64>(6)? != 0 {
                    return Ok(None);
                }
                Ok(Some((
                    row.get::<_, i64>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, i64>(3)?,
                    row.get::<_, Option<String>>(4)?,
                    row.get::<_, i64>(5)?,
                )))
            })
            .map_err(|_| unavailable())?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| unavailable())?;
        // A hidden (generated) column makes the xinfo list differ from the info list: keep them distinguishable.
        let all = rows.len();
        let kept: Vec<ColumnRow> = rows.into_iter().flatten().collect();
        if kept.len() != all {
            return Err(corrupt());
        }
        Ok(kept)
    };
    let table_info = info("PRAGMA table_info(seed_heartbeat_renewals)", false)?;
    let table_xinfo = info("PRAGMA table_xinfo(seed_heartbeat_renewals)", true)?;
    if table_info != expected || table_xinfo != expected {
        return Err(corrupt());
    }

    let mut indexes: Vec<(i64, String, i64, Vec<String>)> = Vec::new();
    let listed: Vec<(String, i64, String, i64)> = connection
        .prepare("PRAGMA index_list(seed_heartbeat_renewals)")
        .and_then(|mut s| {
            s.query_map([], |row| {
                Ok((
                    row.get::<_, String>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, i64>(4)?,
                ))
            })?
            .collect()
        })
        .map_err(|_| unavailable())?;
    for (name, unique, origin, partial) in listed {
        let quoted = name.replace('"', "\"\"");
        let columns: Vec<String> = connection
            .prepare(&format!("PRAGMA index_info(\"{quoted}\")"))
            .and_then(|mut s| s.query_map([], |row| row.get::<_, String>(2))?.collect())
            .map_err(|_| unavailable())?;
        indexes.push((unique, origin, partial, columns));
    }
    indexes.sort();
    let wanted = vec![
        (
            1,
            "pk".to_owned(),
            0,
            vec![
                "node_id".to_owned(),
                "generation".to_owned(),
                "heartbeat_message_id".to_owned(),
            ],
        ),
        (1, "u".to_owned(), 0, vec!["renewal_message_id".to_owned()]),
    ];
    let mut wanted_sorted = wanted;
    wanted_sorted.sort();
    if indexes != wanted_sorted {
        return Err(corrupt());
    }

    let mut keys: Vec<[String; 6]> = connection
        .prepare("PRAGMA foreign_key_list(seed_heartbeat_renewals)")
        .and_then(|mut s| {
            s.query_map([], |row| {
                Ok([
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, String>(6)?,
                    row.get::<_, String>(7)?,
                ])
            })?
            .collect()
        })
        .map_err(|_| unavailable())?;
    keys.sort();
    let key = |a: &str, b: &str, c: &str, d: &str, e: &str, f: &str| {
        [a, b, c, d, e, f].map(str::to_owned)
    };
    let mut wanted_keys = vec![
        key(
            "seed_members",
            "node_id",
            "node_id",
            "NO ACTION",
            "CASCADE",
            "NONE",
        ),
        key(
            "seed_emitted_messages",
            "renewal_message_id",
            "message_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
    ];
    wanted_keys.sort();
    if keys != wanted_keys {
        return Err(corrupt());
    }

    let sql: Option<String> = connection
        .query_row(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'seed_heartbeat_renewals'",
            [],
            |row| row.get(0),
        )
        .map_err(|_| corrupt())?;
    let Some(sql) = sql else {
        return Err(corrupt());
    };
    let normalized: String = sql
        .to_lowercase()
        .chars()
        .filter(|c| !is_python_regex_space(*c))
        .collect();
    let declarations = [
        "node_idtextnotnull,",
        "generationintegernotnullcheck(generation>=1),",
        "heartbeat_message_idtextnotnull,",
        "request_envelope_digesttextnotnull,",
        "heartbeat_sequenceintegernotnullcheck(heartbeat_sequence>=1),",
        "heartbeat_jsontextnotnull,",
        "renewal_message_idtextnotnullunique,",
        "renewal_jsontextnotnull,",
    ];
    if !normalized.ends_with(")withoutrowid")
        || normalized.matches("check(").count() != 2
        || !normalized.contains("check(generation>=1)")
        || !normalized.contains("check(heartbeat_sequence>=1)")
        || declarations.iter().any(|d| !normalized.contains(d))
    {
        return Err(corrupt());
    }
    Ok(())
}

/// `re.sub(r"\s+", "", ...)`: Python's Unicode `\s` for `str` patterns.
fn is_python_regex_space(c: char) -> bool {
    c.is_whitespace() || matches!(c, '\u{1c}'..='\u{1f}')
}

// ------------------------------------------------------------------------------------------------ the import

/// Everything one v0.1.4 database holds, in the shape the Rust store keeps it.
pub(crate) struct Imported {
    pub snapshot: Value,
    /// `(kind, key, signed envelope)` rows of `rs_seed_envelopes`.
    pub envelopes: Vec<(String, String, Value)>,
}

fn text(value: ValueRef<'_>) -> Result<String, SeedError> {
    match value {
        ValueRef::Text(bytes) => std::str::from_utf8(bytes)
            .map(str::to_owned)
            .map_err(|_| corrupt()),
        _ => Err(corrupt()),
    }
}
fn nonempty(value: ValueRef<'_>) -> Result<String, SeedError> {
    let value = text(value)?;
    if value.is_empty() {
        Err(corrupt())
    } else {
        Ok(value)
    }
}
fn integer(value: ValueRef<'_>) -> Result<i64, SeedError> {
    match value {
        ValueRef::Integer(i) => Ok(i),
        _ => Err(corrupt()),
    }
}
/// A finite number from an `int` or `float` cell (Python: not a bool, `math.isfinite`).
fn number(value: ValueRef<'_>) -> Result<f64, SeedError> {
    let number = match value {
        ValueRef::Integer(i) => i as f64,
        ValueRef::Real(r) => r,
        _ => return Err(corrupt()),
    };
    if number.is_finite() {
        Ok(number)
    } else {
        Err(corrupt())
    }
}
fn float(number: f64) -> Value {
    serde_json::Number::from_f64(number).map_or(Value::Null, Value::Number)
}
fn unsigned(value: i64) -> Result<u64, SeedError> {
    u64::try_from(value).map_err(|_| corrupt())
}

/// `_decode_acceptance`: a JSON object whose canonical bytes are exactly the stored text.
fn canonical_object(raw: &str) -> Result<Value, SeedError> {
    let value: Value = serde_json::from_str(raw).map_err(|_| corrupt())?;
    if !value.is_object() || canonical::to_bytes(&value).map_err(|_| corrupt())? != raw.as_bytes() {
        return Err(corrupt());
    }
    Ok(value)
}

/// A request digest as v0.1.4 stores it (64 lowercase hex digits) in the `sha256:` form the Rust table keeps.
fn prefixed_digest(stored: &str) -> Result<String, SeedError> {
    if stored.len() == 64
        && stored
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        Ok(format!("sha256:{stored}"))
    } else {
        Err(corrupt())
    }
}

fn cell_ref(cell: &rusqlite::types::Value) -> ValueRef<'_> {
    use rusqlite::types::Value as Cell;
    match cell {
        Cell::Null => ValueRef::Null,
        Cell::Integer(i) => ValueRef::Integer(*i),
        Cell::Real(r) => ValueRef::Real(*r),
        Cell::Text(t) => ValueRef::Text(t.as_bytes()),
        Cell::Blob(b) => ValueRef::Blob(b),
    }
}

fn sqlite_corrupt(_: rusqlite::Error) -> SeedError {
    corrupt()
}

fn member(row: &rusqlite::Row<'_>) -> Result<Value, SeedError> {
    let get = |i: usize| row.get_ref(i).map_err(sqlite_corrupt);
    let addresses_raw = text(get(2)?)?;
    let runtime_raw = text(get(4)?)?;
    let addresses: Value = serde_json::from_str(&addresses_raw).map_err(|_| corrupt())?;
    let runtime: Value = serde_json::from_str(&runtime_raw).map_err(|_| corrupt())?;
    let listed = addresses.as_array().ok_or_else(corrupt)?;
    if listed.is_empty()
        || !listed
            .iter()
            .all(|a| a.as_str().is_some_and(|s| !s.is_empty()))
        || canonical::to_bytes(&addresses).map_err(|_| corrupt())? != addresses_raw.as_bytes()
        || !runtime.is_object()
        || canonical::to_bytes(&runtime).map_err(|_| corrupt())? != runtime_raw.as_bytes()
    {
        return Err(corrupt());
    }
    let generation = integer(get(7)?)?;
    let sequence = integer(get(9)?)?;
    let active = integer(get(13)?)?;
    if generation < 1 || sequence < 0 || active < 0 {
        return Err(corrupt());
    }
    let receipt = match get(12)? {
        ValueRef::Null => Value::Null,
        other => float(number(other)?),
    };
    Ok(json!({
        "node_id": nonempty(get(0)?)?,
        "endpoint_id": nonempty(get(1)?)?,
        "endpoint_addrs": addresses,
        "peer_class": nonempty(get(3)?)?,
        "runtime_capability": runtime,
        "verification_key_digest": nonempty(get(5)?)?,
        "incarnation": nonempty(get(6)?)?,
        "generation": generation,
        "lease_expires_at": float(number(get(8)?)?),
        "last_heartbeat_sequence": sequence,
        "last_liveness_at": float(number(get(10)?)?),
        "next_heartbeat_due_at": float(number(get(11)?)?),
        "last_activity_receipt_at": receipt,
        "active_requests": active,
        "lifecycle_state": nonempty(get(14)?)?,
    }))
}

fn metadata(connection: &Connection, key: &str) -> Result<Option<String>, SeedError> {
    match connection.query_row(
        "SELECT value FROM seed_metadata WHERE key = ?",
        [key],
        |r| r.get::<_, String>(0),
    ) {
        Ok(value) => Ok(Some(value)),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
        Err(_) => Err(corrupt()),
    }
}

/// `authority_state()["authority_generation"]` as stored: the metadata key, `str(int(value)) == value`, `>= 1`.
pub(crate) fn authority_generation(connection: &Connection) -> Result<Option<u64>, SeedError> {
    let Some(value) = metadata(connection, "authority_generation")? else {
        return Ok(None);
    };
    match value.parse::<u64>() {
        Ok(n) if n >= 1 && n.to_string() == value => Ok(Some(n)),
        _ => Err(corrupt()),
    }
}

/// How many members the v0.1.4 tables hold (`len(load_members())`, strictly decoded).
pub(crate) fn member_count(connection: &Connection) -> Result<usize, SeedError> {
    members(connection).map(|m| m.len())
}

fn members(connection: &Connection) -> Result<Vec<Value>, SeedError> {
    let mut statement = connection
        .prepare(
            "SELECT node_id, endpoint_id, endpoint_addrs_json, peer_class, runtime_capability_json, \
             verification_key_digest, incarnation, generation, lease_expires_at, last_heartbeat_sequence, \
             last_liveness_at, next_heartbeat_due_at, last_activity_receipt_at, active_requests, \
             lifecycle_state FROM seed_members ORDER BY node_id",
        )
        .map_err(sqlite_corrupt)?;
    let mut rows = statement.query([]).map_err(sqlite_corrupt)?;
    let mut out = Vec::new();
    while let Some(row) = rows.next().map_err(sqlite_corrupt)? {
        out.push(member(row)?);
    }
    Ok(out)
}

/// Read every v0.1.4 row table. `None` when the database holds no state at all (a fresh or empty database).
pub(crate) fn read(connection: &Connection) -> Result<Option<Imported>, SeedError> {
    let members = members(connection)?;
    let query = |sql: &str| -> Result<Vec<Vec<rusqlite::types::Value>>, SeedError> {
        let mut statement = connection.prepare(sql).map_err(sqlite_corrupt)?;
        let width = statement.column_count();
        let mut rows = statement.query([]).map_err(sqlite_corrupt)?;
        let mut out = Vec::new();
        while let Some(row) = rows.next().map_err(sqlite_corrupt)? {
            out.push(
                (0..width)
                    .map(|i| {
                        row.get::<_, rusqlite::types::Value>(i)
                            .map_err(sqlite_corrupt)
                    })
                    .collect::<Result<Vec<_>, _>>()?,
            );
        }
        Ok(out)
    };
    let mut envelopes = Vec::new();

    let replay = query(
        "SELECT node_id, generation, message_id, expires_at FROM seed_replay ORDER BY node_id, generation, message_id",
    )?
    .iter()
    .map(|r| {
        Ok(json!({
            "node_id": nonempty(cell_ref(&r[0]))?,
            "generation": unsigned(integer(cell_ref(&r[1]))?)?,
            "message_id": nonempty(cell_ref(&r[2]))?,
            "expires_at": float(number(cell_ref(&r[3]))?),
        }))
    })
    .collect::<Result<Vec<_>, SeedError>>()?;

    let emitted = query("SELECT message_id FROM seed_emitted_messages ORDER BY message_id")?
        .iter()
        .map(|r| Ok(Value::String(nonempty(cell_ref(&r[0]))?)))
        .collect::<Result<Vec<_>, SeedError>>()?;

    let mut join_rows = Vec::new();
    for r in query(
        "SELECT nonce, invite_token_digest, request_envelope_digest, acceptance_json FROM seed_join_acceptances ORDER BY nonce",
    )? {
        let nonce = nonempty(cell_ref(&r[0]))?;
        let token = prefixed_digest(&text(cell_ref(&r[1]))?)?;
        let request = prefixed_digest(&text(cell_ref(&r[2]))?)?;
        envelopes.push((
            "join".to_owned(),
            nonce.clone(),
            canonical_object(&text(cell_ref(&r[3]))?)?,
        ));
        join_rows.push(json!([nonce, token, request]));
    }

    let mut resume_rows = Vec::new();
    for r in query(
        "SELECT request_message_id, request_envelope_digest, acceptance_json FROM seed_resume_acceptances ORDER BY request_message_id",
    )? {
        let id = nonempty(cell_ref(&r[0]))?;
        let request = prefixed_digest(&text(cell_ref(&r[1]))?)?;
        envelopes.push((
            "resume".to_owned(),
            id.clone(),
            canonical_object(&text(cell_ref(&r[2]))?)?,
        ));
        resume_rows.push(json!([id, request]));
    }

    let mut renewals = Vec::new();
    for r in query(
        "SELECT node_id, generation, heartbeat_message_id, request_envelope_digest, heartbeat_sequence, \
         heartbeat_json, renewal_json FROM seed_heartbeat_renewals ORDER BY node_id, generation, heartbeat_message_id",
    )? {
        let node = nonempty(cell_ref(&r[0]))?;
        let generation = unsigned(integer(cell_ref(&r[1]))?)?;
        let heartbeat = nonempty(cell_ref(&r[2]))?;
        let digest = prefixed_digest(&text(cell_ref(&r[3]))?)?;
        let sequence = unsigned(integer(cell_ref(&r[4]))?)?;
        canonical_object(&text(cell_ref(&r[5]))?)?;
        let signed = canonical_object(&text(cell_ref(&r[6]))?)?;
        if sequence < 1 || generation < 1 {
            return Err(corrupt());
        }
        renewals.push(json!({
            "node_id": node, "generation": generation, "heartbeat_message_id": heartbeat,
            "request_digest": digest, "sequence": sequence, "renewal": signed["message"].clone(),
        }));
        envelopes.push((
            "renewal".to_owned(),
            format!("{node}|{generation}|{heartbeat}"),
            signed,
        ));
    }

    let assignments = query(
        "SELECT node_id, deployment_id, deployment_epoch, membership_generation, assignment_id, accepted \
         FROM seed_assignments ORDER BY assignment_id",
    )?
    .iter()
    .map(|r| {
        let accepted = match cell_ref(&r[5]) {
            ValueRef::Null => Value::Null,
            ValueRef::Integer(0) => Value::Bool(false),
            ValueRef::Integer(1) => Value::Bool(true),
            _ => return Err(corrupt()),
        };
        Ok(json!({
            "node_id": nonempty(cell_ref(&r[0]))?, "deployment_id": nonempty(cell_ref(&r[1]))?,
            "deployment_epoch": unsigned(integer(cell_ref(&r[2]))?)?,
            "membership_generation": unsigned(integer(cell_ref(&r[3]))?)?,
            "assignment_id": nonempty(cell_ref(&r[4]))?, "accepted": accepted,
        }))
    })
    .collect::<Result<Vec<_>, SeedError>>()?;

    let rotations = query(
        "SELECT authority_generation, transition_json, status FROM seed_authority_rotations ORDER BY authority_generation",
    )?
    .iter()
    .map(|r| {
        let transition = canonical_object(&text(cell_ref(&r[1]))?)?;
        let pending = match text(cell_ref(&r[2]))?.as_str() {
            "PENDING" => true,
            "COMPLETED" => false,
            _ => return Err(corrupt()),
        };
        let digest = canonical::sha256_prefixed(&canonical::to_bytes(&transition).map_err(|_| corrupt())?);
        Ok(json!({
            "authority_generation": unsigned(integer(cell_ref(&r[0]))?)?,
            "transition_digest": digest, "pending": pending, "transition": transition,
        }))
    })
    .collect::<Result<Vec<_>, SeedError>>()?;

    let acks = query(
        "SELECT node_id, member_generation, authority_generation, transition_digest, message_id, acknowledged_at \
         FROM seed_rotation_acknowledgements ORDER BY node_id, authority_generation",
    )?
    .iter()
    .map(|r| {
        Ok(json!({
            "node_id": nonempty(cell_ref(&r[0]))?,
            "member_generation": unsigned(integer(cell_ref(&r[1]))?)?,
            "authority_generation": unsigned(integer(cell_ref(&r[2]))?)?,
            "transition_digest": nonempty(cell_ref(&r[3]))?,
            "message_id": nonempty(cell_ref(&r[4]))?,
            "acknowledged_at": float(number(cell_ref(&r[5]))?),
        }))
    })
    .collect::<Result<Vec<_>, SeedError>>()?;

    let generation = authority_generation(connection)?.unwrap_or(1);
    let empty = members.is_empty()
        && replay.is_empty()
        && emitted.is_empty()
        && join_rows.is_empty()
        && resume_rows.is_empty()
        && renewals.is_empty()
        && assignments.is_empty()
        && rotations.is_empty()
        && acks.is_empty()
        && generation == 1;
    if empty {
        return Ok(None);
    }
    let mut snapshot = Map::new();
    snapshot.insert("members".into(), Value::Array(members));
    snapshot.insert("replay".into(), Value::Array(replay));
    snapshot.insert("renewals".into(), Value::Array(renewals));
    snapshot.insert("join_rows".into(), Value::Array(join_rows));
    snapshot.insert("resume_rows".into(), Value::Array(resume_rows));
    snapshot.insert("assignments".into(), Value::Array(assignments));
    snapshot.insert("rotations".into(), Value::Array(rotations));
    snapshot.insert("acks".into(), Value::Array(acks));
    snapshot.insert("authority_generation".into(), json!(generation));
    snapshot.insert("emitted".into(), Value::Array(emitted));
    Ok(Some(Imported {
        snapshot: Value::Object(snapshot),
        envelopes,
    }))
}
