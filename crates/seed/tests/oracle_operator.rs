// SPDX-License-Identifier: AGPL-3.0-only
//! Seed key rotation OPERATOR layer and acknowledgement rows against the EXECUTED oracle (FX-L5-seed-operator-r12:
//! `round12-operator-probe.py` -> `fixtures/oracle_operator.json`, v0.1.4 f4e366e9, Python 3.14.8). The probe ran the real
//! `mycelium_seed.operator.{begin,complete}_seed_key_rotation` / `seed_key_rotation_status` over a real private
//! state root (identity key files, `state.sqlite3`) with a scripted clock and key generator, real coordinator traffic
//! (join, heartbeat, rotation acknowledgements over `/seed/message`) and file-system mutations between the calls.
//! Every step is replayed here over the Rust implementation and must give the same value or the same exception
//! class and code, the same file set (type, mode, link count, size, sha256), the same durable authority state, the
//! same acknowledgement rows and member rows, the same HTTP bytes and the same ids drawn.
//!
//! Round 13 adds a second fixture (FX-L5-seed-operator-r13: `round13-operator-probe.py` ->
//! `fixtures/oracle_operator_r13.json`): the replay-window-full acknowledgement (RU-035: the row is kept, then the
//! call refuses), `seed_inventory`, `revoke_seed_member` and `backup_seed_state`. Its steps also compare the
//! replay rows per member generation and the backup directory tree.
use protocol::{canonical, identity::Identity, seed_members::SeedTable};
use seed::{
    Coordinator, Options,
    operator::{self, Arg, ErrorClass, OperatorError},
};
use serde_json::{Map, Value, json};
use std::time::Duration;
use std::{
    fs,
    io::Cursor,
    os::unix::fs::{MetadataExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
};

fn fixture() -> Value {
    let doc: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_operator.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    doc
}

fn fixture_r13() -> Value {
    let doc: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_operator_r13.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    assert_eq!(doc["views_replay_rows"], true);
    doc
}

fn fixture_r14() -> Value {
    let doc: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_state_r14.json")).unwrap();
    assert_eq!(doc["oracle"], "f4e366e98a1f705dfaf182897044500d290dbd7f");
    assert_eq!(doc["views_restores"], true);
    doc
}

fn unhex(s: &str) -> Vec<u8> {
    (0..s.len() / 2)
        .map(|i| u8::from_str_radix(&s[2 * i..2 * i + 2], 16).unwrap())
        .collect()
}

fn text(v: &Value) -> String {
    String::from_utf8(canonical::to_bytes(v).unwrap()).unwrap()
}

struct World {
    base: PathBuf,
    root: PathBuf,
    config: Value,
    clock: Arc<AtomicU64>,
    counter: Arc<Mutex<(u64, Vec<String>)>>,
    /// The round-13 views: replay rows per member generation and the backup directory tree.
    extras: bool,
    name: String,
}

impl World {
    fn new(name: &str, config: &Value, seed_byte: u8) -> Self {
        // The store refuses a symlink anywhere on the path and macOS keeps `/var` -> `/private/var`: resolve first.
        let temp = fs::canonicalize(std::env::temp_dir()).unwrap();
        let base = temp.join(format!("l5r12-op-{}-{name}", std::process::id()));
        fs::remove_dir_all(&base).unwrap_or(());
        fs::create_dir_all(&base).unwrap();
        fs::set_permissions(&base, fs::Permissions::from_mode(0o700)).unwrap();
        let root = base.join(name);
        fs::create_dir(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
        let identity = root.join("identity");
        fs::create_dir(&identity).unwrap();
        fs::set_permissions(&identity, fs::Permissions::from_mode(0o700)).unwrap();
        let world = Self {
            base,
            root,
            config: config.clone(),
            clock: Arc::new(AtomicU64::new(config["now"].as_f64().unwrap().to_bits())),
            counter: Arc::new(Mutex::new((0, Vec::new()))),
            extras: false,
            name: name.to_owned(),
        };
        world.write_file(&identity.join("seed.key"), &[seed_byte; 32], 0o600);
        world
    }

    fn write_file(&self, path: &Path, data: &[u8], mode: u32) {
        use std::io::Write;
        use std::os::unix::fs::OpenOptionsExt;
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(path)
            .unwrap();
        file.write_all(data).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
    }

    fn db(&self) -> PathBuf {
        self.root.join("state.sqlite3")
    }

    fn set_clock(&self, now: f64) {
        self.clock.store(now.to_bits(), Ordering::SeqCst);
        self.counter.lock().unwrap().1.clear();
    }

    fn drawn(&self) -> Vec<String> {
        self.counter.lock().unwrap().1.clone()
    }

    fn bound_digest(&self) -> Option<String> {
        let db = self.db();
        if !db.exists() {
            return None;
        }
        let connection =
            rusqlite::Connection::open_with_flags(&db, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
                .ok()?;
        connection
            .query_row(
                "SELECT value FROM seed_metadata WHERE key = 'seed_key_digest'",
                [],
                |row| row.get::<_, String>(0),
            )
            .ok()
    }

    /// The signer of one key file, read directly (the harness does not depend on the code under test): the
    /// endpoint id is `node-identity-<first 32 hex of the key digest>` as `Ed25519EvidenceSigner` is built there.
    fn signer_of(path: &Path) -> Option<Identity> {
        let secret: [u8; 32] = fs::read(path).ok()?.try_into().ok()?;
        let first = Identity::from_secret("derive", &secret).ok()?;
        let endpoint = format!("node-identity-{}", &first.verification_key_digest()[7..39]);
        Identity::from_secret(&endpoint, &secret).ok()
    }

    /// A fresh coordinator over the durable state, signing with the key the database is bound to.
    fn coordinator(&self) -> Coordinator {
        self.try_coordinator(None)
            .unwrap_or_else(|e| panic!("open: {}", e.code))
    }

    /// The same, answering the refusal instead of panicking; `busy` replaces v0.1.4's 30 s busy timeouts.
    fn try_coordinator(&self, busy: Option<Duration>) -> Result<Coordinator, seed::SeedError> {
        let identity = self.root.join("identity");
        let signer = match self.bound_digest() {
            Some(digest) => ["seed.key", "seed.next.key"]
                .iter()
                .filter_map(|name| Self::signer_of(&identity.join(name)))
                .find(|s| s.verification_key_digest() == digest)
                .expect("a key file matches the bound digest"),
            None => Self::signer_of(&identity.join("seed.key")).unwrap(),
        };
        let c = &self.config;
        let options = Options {
            swarm_id: c["swarm_id"].as_str().unwrap().into(),
            seed_node_id: c["seed_node_id"].as_str().unwrap().into(),
            incarnation: c["incarnation"].as_str().unwrap().into(),
            seed_url: Some(c["seed_url"].as_str().unwrap().into()),
            lease_seconds: c["lease_seconds"].as_f64().unwrap(),
            message_ttl_seconds: c["message_ttl_seconds"].as_f64().unwrap(),
            keepalive_interval_seconds: c["keepalive_interval_seconds"].as_f64().unwrap(),
            evidence_freshness_seconds: c["evidence_freshness_seconds"].as_f64().unwrap(),
        };
        let clock = self.clock.clone();
        let counter = self.counter.clone();
        let clock: seed::Clock = Box::new(move || f64::from_bits(clock.load(Ordering::SeqCst)));
        let ids: seed::IdSource = Box::new(move || {
            let mut state = counter.lock().unwrap();
            let id = format!("seed-msg-{}", state.0);
            state.0 += 1;
            state.1.push(id.clone());
            id
        });
        match busy {
            None => Coordinator::open(&self.db(), signer, options, clock, ids),
            Some(busy) => {
                Coordinator::open_with_busy_timeout(&self.db(), signer, options, clock, ids, busy)
            }
        }
    }

    /// Start from the state the oracle built (round 14): its identity files, its sqlite database (verified against
    /// the sha256 the probe recorded), optional direct edits of that database, and the id sequence it had reached.
    fn install(&self, install: &Value) {
        let identity = self.root.join("identity");
        if install["identity"]
            .as_object()
            .is_some_and(|m| !m.is_empty())
        {
            fs::remove_dir_all(&identity).unwrap();
            fs::create_dir(&identity).unwrap();
            fs::set_permissions(&identity, fs::Permissions::from_mode(0o700)).unwrap();
            for (name, hex) in install["identity"].as_object().unwrap() {
                self.write_file(&identity.join(name), &unhex(hex.as_str().unwrap()), 0o600);
            }
        }
        if let Some(file) = install["db_file"].as_str() {
            let bytes = fs::read(
                Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("fixtures/v014_state")
                    .join(file),
            )
            .unwrap();
            assert_eq!(
                canonical::sha256_prefixed(&bytes)
                    .strip_prefix("sha256:")
                    .unwrap(),
                install["db_sha256"].as_str().unwrap(),
                "the committed base database is the one the probe built"
            );
            self.write_file(&self.db(), &bytes, 0o600);
        }
        let statements = install["pre_sql"].as_array().unwrap();
        if !statements.is_empty() {
            let connection = rusqlite::Connection::open(self.db()).unwrap();
            for statement in statements {
                connection
                    .execute_batch(statement.as_str().unwrap())
                    .unwrap();
            }
            drop(connection);
            fs::set_permissions(self.db(), fs::Permissions::from_mode(0o600)).unwrap();
        }
        self.counter.lock().unwrap().0 = install["id_counter"].as_u64().unwrap();
    }

    /// The listing of one directory the way the probe records it (the sqlite files are not listed).
    fn listing(dir: &Path) -> Value {
        if fs::symlink_metadata(dir).is_err() {
            return Value::Null;
        }
        let mut names: Vec<_> = fs::read_dir(dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .filter(|n| !n.starts_with("state.sqlite3"))
            .collect();
        names.sort();
        let mut out = Map::new();
        for name in names {
            let path = dir.join(&name);
            let meta = fs::symlink_metadata(&path).unwrap();
            let kind = if meta.file_type().is_symlink() {
                "link"
            } else if meta.is_dir() {
                "dir"
            } else {
                "file"
            };
            let mut entry =
                json!({ "type": kind, "mode": meta.mode() & 0o7777, "nlink": meta.nlink() });
            if kind == "file" {
                let bytes = fs::read(&path).unwrap();
                entry["size"] = json!(bytes.len());
                entry["sha256"] = json!(
                    canonical::sha256_prefixed(&bytes)
                        .strip_prefix("sha256:")
                        .unwrap()
                );
            }
            if kind == "link" {
                entry["target"] = json!(fs::read_link(&path).unwrap().to_string_lossy());
            }
            out.insert(name, entry);
        }
        Value::Object(out)
    }

    /// The durable views of the probe, read straight from the database (independent of the key files).
    fn views(&self) -> Value {
        let mut out = Map::new();
        let db = self.db();
        if !db.exists() {
            out.insert("views_error".into(), json!("db_absent"));
        } else if fs::metadata(&self.root).unwrap().mode() & 0o7777 != 0o700 {
            out.insert("views_error".into(), json!("root_mode_skipped"));
        } else if fs::metadata(&db).unwrap().mode() & 0o7777 != 0o600 {
            out.insert("views_error".into(), json!("db_mode_skipped"));
        } else {
            let connection = rusqlite::Connection::open_with_flags(
                &db,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
            )
            .unwrap();
            let snapshot: Option<String> = connection
                .query_row("SELECT snapshot FROM rs_seed_table WHERE id = 1", [], |r| {
                    r.get(0)
                })
                .ok();
            let table = match snapshot {
                Some(s) => SeedTable::restore(
                    &serde_json::from_str(&s).unwrap(),
                    Box::new(|| unreachable!("views draw no ids")),
                )
                .unwrap(),
                None => SeedTable::new(Box::new(|| unreachable!("views draw no ids"))),
            };
            out.insert("authority_state".into(), table.authority_state());
            out.insert("binding_digest".into(), json!(self.bound_digest().unwrap()));
            out.insert(
                "acks".into(),
                json!({
                    "2": table.rotation_acknowledgements(2),
                    "3": table.rotation_acknowledgements(3),
                }),
            );
            let mut members = Map::new();
            for m in table.rows() {
                members.insert(
                    m.node_id.clone(),
                    json!({
                        "generation": m.generation,
                        "lifecycle_state": m.lifecycle_state,
                        "peer_class": m.peer_class,
                    }),
                );
            }
            out.insert("members".into(), Value::Object(members));
            if self.extras {
                out.insert("replay".into(), replay_groups(&table));
            }
        }
        if self.extras {
            let backups = self.base.join(format!("bk-{}", self.name));
            out.insert(
                "backups".into(),
                if fs::symlink_metadata(&backups).is_ok() {
                    tree(&backups)
                } else {
                    Value::Null
                },
            );
        }
        if self.extras {
            let restores = self.base.join(format!("rs-{}", self.name));
            if fs::symlink_metadata(&restores).is_ok() {
                out.insert("restores".into(), tree(&restores));
            }
        }
        out.insert("state_root".into(), Self::listing(&self.root));
        out.insert(
            "identity".into(),
            Self::listing(&self.root.join("identity")),
        );
        Value::Object(out)
    }
}

/// The replay rows of the durable table per `node/generation`: the row count and the sha256 of the sorted
/// `message_id|expires_at` lines (the probe reads the same facts straight from `seed_replay`).
fn replay_groups(table: &SeedTable) -> Value {
    let mut groups: std::collections::BTreeMap<String, Vec<(String, f64)>> = Default::default();
    for row in table.snapshot()["replay"].as_array().unwrap() {
        groups
            .entry(format!(
                "{}/{}",
                row["node_id"].as_str().unwrap(),
                row["generation"]
            ))
            .or_default()
            .push((
                row["message_id"].as_str().unwrap().to_owned(),
                row["expires_at"].as_f64().unwrap(),
            ));
    }
    let mut out = Map::new();
    for (key, mut rows) in groups {
        rows.sort_by(|a, b| a.0.cmp(&b.0));
        let lines: Vec<String> = rows.iter().map(|(id, at)| format!("{id}|{at:?}")).collect();
        out.insert(
            key,
            json!({
                "rows": rows.len(),
                "sha256": canonical::sha256_prefixed(lines.join("\n").as_bytes())
                    .strip_prefix("sha256:")
                    .unwrap(),
            }),
        );
    }
    Value::Object(out)
}

/// A backup tree the way the probe records it: names, modes, link counts; key files by size and sha256; the
/// database by `PRAGMA integrity_check`; the manifest parsed with its database digest checked against the
/// sibling `state.sqlite3`.
fn tree(path: &Path) -> Value {
    let meta = fs::symlink_metadata(path).unwrap();
    let kind = if meta.file_type().is_symlink() {
        "link"
    } else if meta.is_dir() {
        "dir"
    } else {
        "file"
    };
    let mut entry = json!({ "type": kind, "mode": meta.mode() & 0o7777, "nlink": meta.nlink() });
    let name = path.file_name().unwrap().to_string_lossy().into_owned();
    if kind == "dir" {
        let mut children = Map::new();
        let mut names: Vec<_> = fs::read_dir(path)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        for child in names {
            children.insert(child.clone(), tree(&path.join(child)));
        }
        entry["children"] = Value::Object(children);
    } else if kind == "link" {
        entry["target"] = json!(fs::read_link(path).unwrap().to_string_lossy());
    } else if name == "state.sqlite3" {
        // A database sqlite cannot read at all is `sqlite3.DatabaseError` in the probe: recorded as unreadable.
        let rows: Result<Vec<String>, rusqlite::Error> =
            rusqlite::Connection::open_with_flags(path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
                .and_then(|connection| {
                    connection
                        .prepare("PRAGMA integrity_check")?
                        .query_map([], |r| r.get::<_, String>(0))?
                        .collect()
                });
        let rows = rows.unwrap_or_else(|_| vec!["<unreadable>".to_owned()]);
        entry["integrity"] = json!(rows.iter().map(|r| vec![r.clone()]).collect::<Vec<_>>());
        entry["nonempty"] = json!(meta.len() > 0);
    } else if name == "manifest.json" || name == "restore-manifest.json" {
        let raw = fs::read(path).unwrap();
        let mut doc: Value = serde_json::from_slice(&raw).unwrap();
        let sibling = path.parent().unwrap().join("state.sqlite3");
        let actual = fs::read(&sibling)
            .ok()
            .map(|bytes| canonical::sha256_prefixed(&bytes));
        entry["canonical"] = json!(canonical::to_bytes(&doc).unwrap() == raw);
        entry["database_digest_is_sha256_of_state"] =
            json!(actual.is_some() && doc["database_digest"].as_str() == actual.as_deref());
        doc["database_digest"] = json!("<sha256 of state.sqlite3>");
        entry["manifest"] = doc;
    } else {
        let bytes = fs::read(path).unwrap();
        entry["size"] = json!(bytes.len());
        entry["sha256"] = json!(
            canonical::sha256_prefixed(&bytes)
                .strip_prefix("sha256:")
                .unwrap()
        );
    }
    entry
}

/// Split a connection's output into responses by `Content-Length` framing; `Date` headers are dropped.
fn split_responses(mut wire: &[u8]) -> Vec<(String, Vec<String>, Vec<u8>)> {
    let mut out = Vec::new();
    while !wire.is_empty() {
        let at = wire
            .windows(4)
            .position(|w| w == b"\r\n\r\n")
            .expect("response head");
        let head = String::from_utf8_lossy(&wire[..at]).into_owned();
        let mut lines = head.split("\r\n").map(str::to_owned);
        let status = lines.next().unwrap();
        let headers: Vec<String> = lines
            .filter(|l| !l.to_ascii_lowercase().starts_with("date:"))
            .collect();
        let length = headers
            .iter()
            .find_map(|h| h.strip_prefix("Content-Length: "))
            .map_or(wire.len() - at - 4, |n| n.parse().unwrap());
        out.push((status, headers, wire[at + 4..at + 4 + length].to_vec()));
        wire = &wire[at + 4 + length..];
    }
    out
}

fn arg(tagged: &Value) -> Arg {
    match tagged["t"].as_str().unwrap() {
        "none" => Arg::Null,
        "bool" => Arg::Bool(tagged["v"].as_bool().unwrap()),
        "int" => Arg::Int(tagged["v"].as_i64().unwrap()),
        "float" => match tagged["v"].as_str() {
            Some(word) => Arg::Float(word.parse().unwrap()),
            None => Arg::Float(tagged["v"].as_f64().unwrap()),
        },
        "str" => Arg::Text(tagged["v"].as_str().unwrap().to_owned()),
        other => panic!("tag {other}"),
    }
}

fn class_of(name: &str) -> ErrorClass {
    match name {
        "SeedOperatorError" => ErrorClass::Operator,
        "SeedAuthorityError" => ErrorClass::Authority,
        "NodeIdentityError" => ErrorClass::NodeIdentity,
        "ValueError" => ErrorClass::Value,
        "OverflowError" => ErrorClass::Overflow,
        "InviteError" => ErrorClass::Invite,
        other => panic!("exception class {other}"),
    }
}

fn do_fs(world: &World, step: &Value) {
    let args = &step["args"];
    let path = world.root.join(step["path"].as_str().unwrap());
    match step["action"].as_str().unwrap() {
        "write" => {
            if fs::symlink_metadata(&path).is_ok() {
                fs::remove_file(&path).unwrap();
            }
            world.write_file(
                &path,
                &unhex(args["hex"].as_str().unwrap()),
                args["mode"].as_u64().map_or(0o600, |m| m as u32),
            );
        }
        "chmod" => fs::set_permissions(
            &path,
            fs::Permissions::from_mode(args["mode"].as_u64().unwrap() as u32),
        )
        .unwrap(),
        "remove" => fs::remove_file(&path).unwrap(),
        "rename" => fs::rename(&path, world.root.join(args["to"].as_str().unwrap())).unwrap(),
        "symlink" => std::os::unix::fs::symlink(args["target"].as_str().unwrap(), &path).unwrap(),
        "hardlink" => {
            fs::hard_link(world.root.join(args["target"].as_str().unwrap()), &path).unwrap();
        }
        "mkdir" => {
            fs::create_dir(&path).unwrap();
            fs::set_permissions(
                &path,
                fs::Permissions::from_mode(args["mode"].as_u64().unwrap() as u32),
            )
            .unwrap();
        }
        "rmdir" => fs::remove_dir(&path).unwrap(),
        other => panic!("fs action {other}"),
    }
}

fn do_operator(world: &World, step: &Value, at: &str) {
    let root = step["state_root"]
        .as_str()
        .map_or_else(|| world.root.clone(), |rel| world.base.join(rel));
    let now = if step["now_nan"] == true {
        f64::NAN
    } else {
        step["now"].as_f64().unwrap()
    };
    let clock = move || now;
    let key_byte = step["next_key"].as_u64().map(|b| b as u8);
    let mut generated = 0usize;
    let mut generate = || -> Result<[u8; 32], OperatorError> {
        generated += 1;
        Ok([key_byte.expect("a key was generated that the probe never offered"); 32])
    };
    let args = &step["args"];
    let got = match step["call"].as_str().unwrap() {
        "begin" => operator::begin_seed_key_rotation(
            &root,
            &arg(&args["reason"]),
            &arg(&args["overlap_seconds"]),
            &clock,
            &mut generate,
        ),
        "complete" => {
            operator::complete_seed_key_rotation(&root, &arg(&args["authority_generation"]), &clock)
        }
        _ => operator::seed_key_rotation_status(&root),
    };
    let want = &step["result"];
    match (want["ok"] == true, got) {
        (true, Ok(value)) => assert_eq!(text(&value), text(&want["value"]), "{at}: value"),
        (false, Err(error)) => {
            assert_eq!(
                (error.class, error.code.as_str()),
                (
                    class_of(want["exc"].as_str().unwrap()),
                    want["code"].as_str().unwrap()
                ),
                "{at}: refusal"
            );
        }
        (want_ok, got) => panic!("{at}: want ok={want_ok} got {got:?}"),
    }
    let offered = key_byte.is_some();
    let unused = step["unused_generated_keys"].as_array().unwrap().len();
    assert_eq!(
        generated,
        usize::from(offered && unused == 0),
        "{at}: the next key is generated exactly when the oracle generated one"
    );
}

/// Insert replay rows straight into the durable snapshot, as the probe inserts them into `seed_replay`.
fn fill_replay(world: &World, step: &Value) {
    let connection = rusqlite::Connection::open(world.db()).unwrap();
    let text: String = connection
        .query_row("SELECT snapshot FROM rs_seed_table WHERE id = 1", [], |r| {
            r.get(0)
        })
        .unwrap();
    let mut snapshot: Value = serde_json::from_str(&text).unwrap();
    let node = step["node"].as_str().unwrap();
    let prefix = step["prefix"].as_str().unwrap();
    let rows = snapshot["replay"].as_array_mut().unwrap();
    for (count, tag, expires) in [
        (&step["live"], "live", &step["expires_at"]),
        (&step["expired"], "old", &step["expired_at"]),
    ] {
        for i in 0..count.as_u64().unwrap() {
            rows.push(json!({
                "node_id": node, "generation": step["generation"],
                "message_id": format!("{prefix}-{tag}-{i}"), "expires_at": expires,
            }));
        }
    }
    connection
        .execute(
            "UPDATE rs_seed_table SET snapshot = ? WHERE id = 1",
            [snapshot.to_string()],
        )
        .unwrap();
}

/// `seed_inventory` / `revoke_seed_member` / `backup_seed_state` against the oracle's recorded result.
fn do_operator2(world: &World, step: &Value, at: &str) {
    let root = step["state_root"]
        .as_str()
        .map_or_else(|| world.root.clone(), |rel| world.base.join(rel));
    let now = if step["now_nan"] == true {
        f64::NAN
    } else if step["now_inf"] == true {
        f64::INFINITY
    } else {
        step["now"].as_f64().unwrap()
    };
    let clock = move || now;
    let args = &step["args"];
    let got = match step["call"].as_str().unwrap() {
        "inventory" => operator::seed_inventory(&root, &clock),
        "revoke" => operator::revoke_seed_member(
            &root,
            &arg(&args["node_id"]),
            &arg(&args["expected_generation"]),
            &arg(&args["reason"]),
            &clock,
        ),
        _ => {
            let id = arg(&args["backup_id"]);
            let sub = args["sub"]
                .as_object()
                .map_or(".", |_| args["sub"]["v"].as_str().unwrap());
            operator::backup_seed_state(
                &root,
                &world.base.join(format!("bk-{}", world.name)).join(sub),
                &clock,
                &|| id.clone(),
            )
        }
    };
    let want = &step["result"];
    match (want["ok"] == true, got) {
        (true, Ok(value)) => {
            let shown = text(&value).replace(world.base.to_str().unwrap(), "<TMP>");
            assert_eq!(shown, text(&want["value"]), "{at}: value");
        }
        (false, Err(error)) => {
            let expected = want["code"]
                .as_str()
                .unwrap_or_else(|| want["message"].as_str().unwrap());
            assert_eq!(
                (error.class, error.code.as_str()),
                (class_of(want["exc"].as_str().unwrap()), expected),
                "{at}: refusal"
            );
        }
        (want_ok, got) => panic!("{at}: want ok={want_ok} got {got:?}"),
    }
}

/// A short bounded wait for a locked database (the oracle's test patches the 30 s busy timeout down to 10 ms): the
/// accept loop's poll interval is the registered short span.
fn busy() -> Duration {
    let span = budgets::BudgetId::PyFinalMyceliumSeedHttpSelfServerTimeoutL676C8
        .span(budgets::Entries::new(0))
        .unwrap();
    Duration::from_millis(span.millis())
}

fn expect_open(want: &Value, got: Result<(), seed::SeedError>, at: &str) {
    match (want["ok"] == true, got) {
        (true, Ok(())) => {}
        (false, Err(error)) => assert_eq!(error.code, want["code"].as_str().unwrap(), "{at}"),
        (want_ok, got) => panic!(
            "{at}: want ok={want_ok} got {:?}",
            got.err().map(|e| e.code)
        ),
    }
}

/// The schema facts the probe reads from sqlite after the oracle opened the state: the stored version, the
/// `seed_members` column list (name, type, not-null, default, pk) and the renewal table's columns.
fn check_meta(world: &World, step: &Value, at: &str) {
    let connection = rusqlite::Connection::open_with_flags(
        world.db(),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    let version: Option<String> = connection
        .query_row(
            "SELECT value FROM seed_metadata WHERE key = 'schema_version'",
            [],
            |r| r.get(0),
        )
        .ok();
    assert_eq!(
        json!(version.map_or_else(Vec::new, |v| vec![vec![v]])),
        step["schema_version"],
        "{at}: schema_version"
    );
    let members: Vec<Value> = connection
        .prepare("PRAGMA table_info(seed_members)")
        .unwrap()
        .query_map([], |r| {
            Ok(json!([
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, i64>(3)?,
                r.get::<_, Option<String>>(4)?,
                r.get::<_, i64>(5)?
            ]))
        })
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert_eq!(
        json!(members),
        step["member_columns"],
        "{at}: seed_members columns"
    );
    let renewals: Vec<String> = connection
        .prepare("PRAGMA table_info(seed_heartbeat_renewals)")
        .unwrap()
        .query_map([], |r| r.get::<_, String>(1))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    assert_eq!(
        json!(renewals),
        step["renewal_columns"],
        "{at}: renewal columns"
    );
}

/// What the oracle's own reads (`load_members`, `load_assignments`, `authority_state`, the acknowledgement rows)
/// say about the database it built, against what the Rust store imported from the same bytes.
fn check_imported_projection(world: &World, projection: &Value, name: &str) {
    if !projection["members"].is_array() {
        return;
    }
    drop(world.coordinator());
    let connection = rusqlite::Connection::open_with_flags(
        world.db(),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    let stored: String = connection
        .query_row("SELECT snapshot FROM rs_seed_table WHERE id = 1", [], |r| {
            r.get(0)
        })
        .unwrap();
    let snapshot: Value = serde_json::from_str(&stored).unwrap();
    let table = SeedTable::restore(&snapshot, Box::new(|| unreachable!("no ids"))).unwrap();
    let want_members = projection["members"].as_array().unwrap();
    let got_members = snapshot["members"].as_array().unwrap();
    assert_eq!(
        got_members.len(),
        want_members.len(),
        "{name}: member count"
    );
    for want in want_members {
        let node = want["node_id"].as_str().unwrap();
        let got = got_members
            .iter()
            .find(|m| m["node_id"] == node)
            .unwrap_or_else(|| panic!("{name}: member {node} was not imported"));
        assert_eq!(
            text(got),
            text(want),
            "{name}: member {node} (all fifteen fields)"
        );
    }
    let want_assignments = projection["assignments"].as_array().unwrap();
    let got_assignments = snapshot["assignments"].as_array().unwrap();
    assert_eq!(
        got_assignments.len(),
        want_assignments.len(),
        "{name}: assignment count"
    );
    for want in want_assignments {
        let got = got_assignments
            .iter()
            .find(|a| a["assignment_id"] == want["assignment_id"])
            .unwrap_or_else(|| panic!("{name}: assignment not imported"));
        for key in [
            "node_id",
            "deployment_id",
            "deployment_epoch",
            "membership_generation",
            "accepted",
        ] {
            assert_eq!(got[key], want[key], "{name}: assignment {key}");
        }
    }
    assert_eq!(
        text(&table.authority_state()),
        text(&projection["authority_state"]),
        "{name}: authority state"
    );
    for generation in [2u64, 3] {
        assert_eq!(
            text(&json!(table.rotation_acknowledgements(generation))),
            text(&projection["acks"][generation.to_string()]),
            "{name}: acknowledgement rows of generation {generation}"
        );
    }
}

fn do_fs2(world: &World, step: &Value) {
    let args = &step["args"];
    let path = world.root.join(step["path"].as_str().unwrap());
    match step["action"].as_str().unwrap() {
        "flipbyte" => {
            let mut bytes = fs::read(&path).unwrap();
            bytes[args["offset"].as_u64().unwrap() as usize] ^= 0xFF;
            fs::write(&path, bytes).unwrap();
        }
        "writedb" => fs::write(&path, unhex(args["hex"].as_str().unwrap())).unwrap(),
        "manifest" => {
            let mut doc: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
            for (key, value) in args["set"].as_object().into_iter().flatten() {
                doc[key] = value.clone();
            }
            for key in args["delete"].as_array().into_iter().flatten() {
                doc.as_object_mut().unwrap().remove(key.as_str().unwrap());
            }
            if args["resign"] == true {
                let database = fs::read(path.parent().unwrap().join("state.sqlite3")).unwrap();
                doc["database_digest"] = json!(canonical::sha256_prefixed(&database));
            }
            let body = if args["indent"] == true {
                serde_json::to_vec_pretty(&doc).unwrap()
            } else {
                canonical::to_bytes(&doc).unwrap()
            };
            fs::write(&path, body).unwrap();
        }
        other => panic!("fs2 action {other}"),
    }
}

/// `restore_seed_state` against the oracle's recorded result.
fn do_restore(world: &World, step: &Value, at: &str) {
    let restores = world.base.join(format!("rs-{}", world.name));
    if fs::symlink_metadata(&restores).is_err() {
        fs::create_dir(&restores).unwrap();
        fs::set_permissions(&restores, fs::Permissions::from_mode(0o700)).unwrap();
    }
    let backup = world
        .base
        .join(format!("bk-{}", world.name))
        .join(step["sub"].as_str().unwrap())
        .join(step["backup"].as_str().unwrap());
    let target = restores.join(step["target"].as_str().unwrap());
    let got = operator::restore_seed_state(&backup, &target);
    let want = &step["result"];
    match (want["ok"] == true, got) {
        (true, Ok(value)) => assert_eq!(text(&value), text(&want["value"]), "{at}: value"),
        (false, Err(error)) => assert_eq!(
            (error.class, error.code.as_str()),
            (
                class_of(want["exc"].as_str().unwrap()),
                want["code"].as_str().unwrap()
            ),
            "{at}: refusal"
        ),
        (want_ok, got) => panic!("{at}: want ok={want_ok} got {got:?}"),
    }
}

fn expected_ids(step: &Value) -> Vec<String> {
    step["ids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_owned())
        .collect()
}

/// Serve one recorded raw request and compare every response (status line, headers minus `Date`, body bytes).
fn check_http(coordinator: &Coordinator, step: &Value, at: &str) {
    let mut out = Vec::new();
    seed::http::serve(
        coordinator,
        Cursor::new(unhex(step["request_hex"].as_str().unwrap())),
        &mut out,
    )
    .unwrap();
    let got = split_responses(&out);
    let want = step["responses"].as_array().unwrap();
    assert_eq!(got.len(), want.len(), "{at}: response count");
    for (i, ((status, headers, body), want)) in got.iter().zip(want).enumerate() {
        assert_eq!(status, want["status_line"].as_str().unwrap(), "{at}#{i}");
        let want_headers: Vec<String> = want["headers"]
            .as_array()
            .unwrap()
            .iter()
            .map(|h| h.as_str().unwrap().to_owned())
            .collect();
        assert_eq!(headers, &want_headers, "{at}#{i}: headers");
        assert_eq!(
            body,
            &unhex(want["body_hex"].as_str().unwrap()),
            "{at}#{i}: body {}",
            String::from_utf8_lossy(body)
        );
    }
}

fn replay(scenario: &Value, config: &Value, extras: bool) -> usize {
    let name = scenario["name"].as_str().unwrap();
    let mut world = World::new(name, config, scenario["seed_byte"].as_u64().unwrap() as u8);
    world.extras = extras;
    if scenario["install"].is_object() {
        world.install(&scenario["install"]);
        check_imported_projection(&world, &scenario["install"]["oracle_projection"], name);
    }
    let mut checked = 0;
    for step in scenario["steps"].as_array().unwrap() {
        let label = step["label"].as_str().unwrap();
        let at = format!("{name}/{label}");
        let op = step["op"].as_str().unwrap();
        if op == "fs" || op == "fs2" || op == "restore" || op == "operator" || op == "operator2" {
            world.counter.lock().unwrap().1.clear();
        } else {
            world.set_clock(step["now"].as_f64().unwrap());
        }
        match op {
            "http" => {
                let coordinator = world.coordinator();
                check_http(&coordinator, step, &at);
            }
            "mint" => {
                let got = world
                    .coordinator()
                    .mint_invite(
                        step["nonce"].as_str().unwrap(),
                        step["ttl_seconds"].as_u64().unwrap(),
                    )
                    .unwrap_or_else(|e| panic!("{at}: {}", e.code));
                assert_eq!(text(&got), text(&step["bundle"]), "{at}: bundle");
            }
            "advance" => {
                let got = world.coordinator().advance_member_generation(
                    step["node"].as_str().unwrap(),
                    step["expected_generation"].as_u64().unwrap(),
                    step["lifecycle_state"].as_str().unwrap(),
                );
                let want = &step["result"];
                if want["ok"] == true {
                    let got = got.unwrap_or_else(|e| panic!("{at}: {}", e.code));
                    assert_eq!(text(&got), text(&want["projection"]), "{at}: projection");
                } else {
                    assert_eq!(
                        got.unwrap_err().code,
                        want["code"].as_str().unwrap(),
                        "{at}"
                    );
                }
            }
            "db_begin" | "db_complete" => {
                let coordinator = world.coordinator();
                let args = &step["args"];
                let got = if op == "db_begin" {
                    coordinator.begin_authority_rotation(&args["transition"])
                } else {
                    coordinator.complete_authority_rotation(
                        args["authority_generation"].as_u64().unwrap(),
                        args["new_seed_key_digest"].as_str().unwrap(),
                    )
                };
                match (&step["result"]["ok"], got) {
                    (Value::Bool(true), Ok(())) => {}
                    (Value::Bool(false), Err(e)) => {
                        assert_eq!(e.code, step["result"]["code"].as_str().unwrap(), "{at}");
                    }
                    (want, got) => panic!("{at}: want {want} got {got:?}"),
                }
            }
            "fs" => do_fs(&world, step),
            "operator" => do_operator(&world, step, &at),
            "operator2" => do_operator2(&world, step, &at),
            "fill_replay" => fill_replay(&world, step),
            "open_state" => {
                let got = world.try_coordinator(None).map(drop);
                expect_open(&step["result"], got, &at);
            }
            "meta" => check_meta(&world, step, &at),
            "busy_open" => {
                let locker = rusqlite::Connection::open(world.db()).unwrap();
                locker.execute_batch("BEGIN IMMEDIATE").unwrap();
                let got = world.try_coordinator(Some(busy())).map(drop);
                locker.execute_batch("ROLLBACK").unwrap();
                expect_open(&step["result"], got, &at);
            }
            "busy_http" => {
                let coordinator = world.try_coordinator(Some(busy())).unwrap();
                let locker = rusqlite::Connection::open(world.db()).unwrap();
                locker.execute_batch("BEGIN IMMEDIATE").unwrap();
                check_http(&coordinator, step, &at);
                locker.execute_batch("ROLLBACK").unwrap();
            }
            "inject" => {
                // The oracle arms a trigger on its own table; the Rust store keeps the signed answers in
                // `rs_seed_envelopes`, so the same fault is armed there (the observable effect is what is compared).
                drop(world.coordinator());
                let connection = rusqlite::Connection::open(world.db()).unwrap();
                if step["on"] == true {
                    let kind = match step["name"].as_str().unwrap() {
                        "heartbeat" => "renewal",
                        "join" => "join",
                        "resume" => "resume",
                        other => panic!("fault {other}"),
                    };
                    connection
                        .execute_batch(&format!(
                            "CREATE TRIGGER fail_commit BEFORE INSERT ON rs_seed_envelopes WHEN NEW.kind = '{kind}' \
                             BEGIN SELECT RAISE(ABORT, 'injected failure'); END;"
                        ))
                        .unwrap();
                } else {
                    connection
                        .execute_batch("DROP TRIGGER fail_commit;")
                        .unwrap();
                }
            }
            "api" => {
                let coordinator = world.coordinator();
                let got = match step["call"].as_str().unwrap() {
                    "join" => coordinator
                        .accept_join(step["token"].as_str().unwrap(), &step["envelope"])
                        .map(drop),
                    "resume" => coordinator.accept_resume(&step["envelope"]).map(drop),
                    "lease_renewal" => coordinator
                        .lease_renewal(step["node"].as_str().unwrap(), step["hb"].as_str().unwrap())
                        .map(drop),
                    _ => coordinator
                        .receive_member_message(
                            &step["envelope"],
                            "mycelium.membership.heartbeat.v1",
                        )
                        .map(drop),
                };
                expect_open(&step["result"], got, &at);
            }
            "pair" => {
                // Two live coordinators over ONE database: `stale` opened first, `current` second.
                let stale = world.coordinator();
                let current = world.coordinator();
                let calls = step["calls"].as_array().unwrap();
                for (call, want) in calls.iter().zip(step["results"].as_array().unwrap()) {
                    let who = if call["who"] == "stale" {
                        &stale
                    } else {
                        &current
                    };
                    let got = match call["call"].as_str().unwrap() {
                        "receive" => who
                            .receive_member_message(
                                &call["envelope"],
                                "mycelium.membership.heartbeat.v1",
                            )
                            .map(drop),
                        "resume" => who.accept_resume(&call["envelope"]).map(drop),
                        "join" => who
                            .accept_join(call["token"].as_str().unwrap(), &call["envelope"])
                            .map(drop),
                        _ => who
                            .advance_member_generation(
                                call["node"].as_str().unwrap(),
                                call["generation"].as_u64().unwrap(),
                                call["lifecycle"].as_str().unwrap(),
                            )
                            .map(drop),
                    };
                    expect_open(want, got, &format!("{at} {}", call["call"]));
                }
            }
            "reset_ids" => world.counter.lock().unwrap().0 = step["to"].as_u64().unwrap(),
            "fs2" => do_fs2(&world, step),
            "restore" => do_restore(&world, step, &at),
            other => panic!("unknown op {other}"),
        }
        if !matches!(op, "operator" | "operator2" | "fs" | "fs2" | "restore") {
            assert_eq!(world.drawn(), expected_ids(step), "{at}: ids drawn");
        }
        if !step["views"].is_null() {
            assert_eq!(
                text(&world.views()),
                text(&step["views"]),
                "{at}: durable views and files"
            );
        }
        checked += 1;
    }
    fs::remove_dir_all(&world.base).unwrap_or(());
    checked
}

fn scenario(name: &str) -> usize {
    let doc = fixture();
    let found = doc["scenarios"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["name"] == name)
        .unwrap();
    replay(found, &doc["config"], false)
}

fn scenario_r13(name: &str) -> usize {
    let doc = fixture_r13();
    let found = doc["scenarios"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["name"] == name)
        .unwrap();
    replay(found, &doc["config"], true)
}

fn scenario_r14(name: &str) -> usize {
    let doc = fixture_r14();
    let found = doc["scenarios"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["name"] == name)
        .unwrap_or_else(|| panic!("no scenario {name}"));
    replay(found, &doc["config"], true)
}

#[test]
fn acknowledgement_rows_equal_the_oracle() {
    assert_eq!(scenario("ack_rows"), 18);
}

#[test]
fn operator_flow_equals_the_oracle() {
    assert_eq!(scenario("operator_flow"), 32);
}

#[test]
fn required_members_are_selected_as_the_oracle_does() {
    assert_eq!(scenario("operator_required_members"), 14);
    assert_eq!(scenario("operator_expired_lease"), 10);
    assert_eq!(scenario("operator_stale_ack_generation"), 11);
}

#[test]
fn operator_arguments_are_refused_as_the_oracle_refuses_them() {
    assert_eq!(scenario("operator_arguments"), 26);
}

#[test]
fn state_root_identity_and_database_refusals_equal_the_oracle() {
    assert_eq!(scenario("operator_state_refusals"), 34);
    assert_eq!(scenario("operator_root_refusals"), 13);
}

#[test]
fn record_tampering_crash_window_and_cleanup_equal_the_oracle() {
    assert_eq!(scenario("operator_records"), 59);
    assert_eq!(scenario("operator_failure_cleanup"), 9);
}

#[test]
fn a_full_replay_window_keeps_the_acknowledgement_row_then_refuses_as_the_oracle_does() {
    // RU-035: `save_seed_rotation_acknowledgement` commits before `_remember` refuses `seed_replay_window_full`.
    assert_eq!(scenario_r13("ack_window_full"), 16);
    assert_eq!(scenario_r13("ack_window_full_expired_rows"), 7);
    assert_eq!(scenario_r13("ack_window_expired_frees_room"), 6);
    // A row that expires exactly now still counts (`DELETE .. WHERE expires_at < now`); a moment later it is purged.
    assert_eq!(scenario_r13("ack_window_expiry_boundary"), 7);
    assert_eq!(scenario_r13("window_full_heartbeat"), 7);
}

#[test]
fn seed_inventory_and_revoke_seed_member_equal_the_oracle() {
    assert_eq!(scenario_r13("inventory_revoke"), 52);
}

#[test]
fn backup_seed_state_equals_the_oracle() {
    assert_eq!(scenario_r13("backup"), 38);
}

#[test]
fn the_default_backup_id_is_a_fresh_valid_id() {
    // `backup-` + `secrets.token_hex(12)`: 24 lowercase hex digits, accepted by the id check (so a default backup works).
    let (a, b) = (operator::default_backup_id(), operator::default_backup_id());
    assert_ne!(a, b);
    for id in [&a, &b] {
        let tail = id.strip_prefix("backup-").unwrap();
        assert_eq!(tail.len(), 24);
        assert!(
            tail.bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        );
    }
}

// ---------------------------------------------------------------------------------------------------------------
// Round 14 (FX-L5-seed-state-r14: `round14-state-probe.py` -> `fixtures/oracle_state_r14.json` +
// `fixtures/v014_state/v8_populated.sqlite3`, v0.1.4 f4e366e9, Python 3.14.8). The probe builds a REAL v0.1.4 state
// root with the oracle (joins, heartbeats, a resume, a revocation, a pending key rotation with an acknowledgement,
// assignment rows), saves the database, and records what the oracle then does with it. Each scenario starts from
// those exact bytes. Behaviours: B-CONSENT-19 (the new version reads the earlier version's persisted membership
// state: schema v1..v8, malformed renewal table, busy database, failed commit), B-SWARM-31 (the durable state
// survives upgrades v1..v8 and a malformed table is refused `seed_state_corrupt`), B-FAIL-16 (atomic, never backwards:
// busy / failed commit, stale seed process, stored renewal after a rotation) and B-AUTH-12 (restore of a verified
// backup, and its refusals).
// ---------------------------------------------------------------------------------------------------------------

/// B-CONSENT-19, B-SWARM-31: a populated v8 database is imported; every row the oracle reads comes out equal; the exact retries
/// (join, heartbeat, resume) answer the stored bytes; new traffic continues the id sequence; the operator calls, a
/// backup of the imported root and a restore of that backup behave as the oracle's do.
#[test]
fn a_populated_v014_state_root_is_imported_and_served_as_the_oracle_does() {
    assert_eq!(scenario_r14("import_v8_populated"), 21);
}

/// B-CONSENT-19, B-FAIL-16: the emitted-message set is imported: an id source that offers an id the earlier run already
/// emitted is refused `seed_message_id_reused`, and nothing changes.
#[test]
fn the_emitted_message_ids_survive_the_import() {
    assert_eq!(scenario_r14("import_emitted_ids"), 5);
}

/// B-FAIL-16: a heartbeat retried for the first time AFTER a key rotation completed is `seed_state_corrupt` on
/// every entry point (the stored renewal was signed by the previous key and no longer verifies); new heartbeats
/// are answered under the new key.
#[test]
fn a_stored_renewal_of_the_previous_key_fails_closed_after_a_rotation() {
    assert_eq!(scenario_r14("import_rotated_renewal_retry"), 7);
}

/// B-FAIL-16: a seed process that opened its view before another process committed never undoes the newer state: a heartbeat
/// older than the stored sequence is `seed_state_member_conflict` (and the newer renewal is still answered after a
/// restart); a message for a generation the database has moved past is `seed_state_member_stale`; an operator's
/// revocation made while the seed runs is not overwritten by the seed's next write for another member.
#[test]
fn a_stale_seed_process_never_undoes_a_newer_commit() {
    assert_eq!(scenario_r14("stale_process_heartbeat"), 10);
    assert_eq!(scenario_r14("stale_process_generation"), 8);
    assert_eq!(scenario_r14("operator_revocation_vs_running_seed"), 8);
    assert_eq!(scenario_r14("stale_process_other_member"), 9);
    assert_eq!(scenario_r14("stale_process_join_resume_conflict"), 9);
}

/// B-SWARM-31: schema versions 1..7 are brought to 8 with the same columns and defaults, and the members survive.
#[test]
fn legacy_schema_versions_migrate_to_the_current_member_contract() {
    for version in 2..=7 {
        assert_eq!(scenario_r14(&format!("import_legacy_v{version}")), 6);
    }
    assert_eq!(scenario_r14("import_legacy_v1"), 6);
}

/// B-SWARM-31: a renewal table outside the exact contract, an unsupported schema version and a corrupt member
/// row are refused with the oracle's code and are never started empty.
#[test]
fn a_state_outside_the_contract_is_refused_with_the_oracles_code() {
    let doc = fixture_r14();
    let mut refused = 0;
    for scenario in doc["scenarios"].as_array().unwrap() {
        let name = scenario["name"].as_str().unwrap();
        if name.starts_with("refuse_") {
            assert_eq!(replay(scenario, &doc["config"], true), 1, "{name}");
            refused += 1;
        }
    }
    assert_eq!(refused, 26);
}

/// B-FAIL-16: a busy database is `seed_state_unavailable` (at open: the invitation registry's
/// `invite_registry_unavailable` first), never corruption, and changes nothing.
#[test]
fn a_busy_database_is_unavailable_not_corrupt() {
    assert_eq!(scenario_r14("busy_database"), 4);
}

/// B-FAIL-16: a commit that fails leaves no effect (member row, replay row, emitted id, renewal, burnt invite),
/// and the same request succeeds once the fault is gone.
#[test]
fn a_failed_commit_leaves_no_partial_effect() {
    assert_eq!(scenario_r14("commit_rollback"), 16);
}

/// B-AUTH-12: `restore_seed_state` restores a verified backup into an absent private root and refuses a tampered,
/// partial, mismatched or already-restored one with the oracle's code.
#[test]
fn restore_seed_state_equals_the_oracle() {
    assert_eq!(scenario_r14("restore_matrix"), 67);
}
