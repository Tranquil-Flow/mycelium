// SPDX-License-Identifier: AGPL-3.0-only
//! Private next-run preference files of the desktop app (fast mode, run-model choice):
//! authority.py:147-270 `_atomic_private_json`/`_load_private_json`, fast_mode.py:25-62 and
//! run_model.py:60-82 @ f4e366e9, checked against the executed oracle
//! (`L10-evidence/r6-oracle-prefs.txt`). The caller supplies the environment value and the
//! run-activity fact; nothing here reads `os.environ`, starts work or grants authority.
use crate::{model_catalog, schema::PublicError, wire::Json};
use serde_json::{Map, Value};
use std::path::Path;

pub mod private_state {
    use super::*;
    use sha2::{Digest, Sha256};
    use std::{
        fs::{self, File, OpenOptions},
        io::{Read, Write},
        os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
        sync::atomic::{AtomicU64, Ordering},
    };
    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    pub struct StateError(pub &'static str);
    const CEILING: u64 = 4 * 1024 * 1024;
    /// `authority._MAX_PRIVATE_JSON_BYTES` (authority.py:96): the default `max_bytes` of
    /// `_load_private_json`/`_atomic_private_json`, from the budget registry. Every host and
    /// peer read of authority-private state uses this one cap.
    pub fn private_json_limit() -> Result<u64, StateError> {
        budgets::BudgetId::PyMyceliumA13MacosAuthorityMaxPrivateJsonBytesL96
            .bytes()
            .map(|b| b.get())
            .map_err(|_| StateError("authority_state_size_limit_invalid"))
    }
    fn limit(max_bytes: u64) -> Result<(), StateError> {
        if max_bytes == 0 || max_bytes > CEILING {
            return Err(StateError("authority_state_size_limit_invalid"));
        }
        Ok(())
    }
    /// `_load_private_json`: absent => None; anything but a private (0600), single-link, owned,
    /// regular, canonical JSON object within the limit is `authority_state_invalid`.
    pub fn load(path: &Path, max_bytes: u64) -> Result<Option<Map<String, Value>>, StateError> {
        limit(max_bytes)?;
        let invalid = StateError("authority_state_invalid");
        let meta = match fs::symlink_metadata(path) {
            Ok(m) => m,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(_) => return Err(invalid),
        };
        if !meta.file_type().is_file()
            || meta.uid() != rustix::process::getuid().as_raw()
            || meta.nlink() != 1
            || meta.mode() & 0o7777 != 0o600
            || meta.len() == 0
            || meta.len() > max_bytes
        {
            return Err(invalid);
        }
        let mut file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW)
            .open(path)
            .map_err(|_| invalid)?;
        let opened = file.metadata().map_err(|_| invalid)?;
        if (opened.dev(), opened.ino()) != (meta.dev(), meta.ino()) {
            return Err(invalid);
        }
        let mut raw = Vec::new();
        (&mut file)
            .take(max_bytes + 1)
            .read_to_end(&mut raw)
            .map_err(|_| invalid)?;
        if raw.is_empty() || raw.len() as u64 > max_bytes {
            return Err(invalid);
        }
        match serde_json::from_slice::<Value>(&raw) {
            Ok(Value::Object(map))
                if protocol::canonical::to_bytes(&Value::Object(map.clone()))
                    .ok()
                    .as_deref()
                    == Some(raw.as_slice()) =>
            {
                Ok(Some(map))
            }
            _ => Err(invalid),
        }
    }
    pub(super) fn unique() -> String {
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |d| d.as_nanos());
        let seed = format!(
            "{now}:{}:{}",
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::SeqCst)
        );
        Sha256::digest(seed.as_bytes())[..16]
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect()
    }
    #[cfg(test)]
    std::thread_local! {
        // Thread-local to keep parallel tests isolated. Panicking here models a
        // process loss after durable temp write, bypassing ordinary IO cleanup.
        pub(crate) static BEFORE_RENAME: std::cell::RefCell<Option<fn(&Path)>> = const { std::cell::RefCell::new(None) };
    }
    /// `_atomic_private_json`: canonical bytes to an exclusive 0600 temporary file, fsync,
    /// rename over `path`, fsync the directory. A failure removes the temporary file.
    pub fn store(path: &Path, document: &Value, max_bytes: u64) -> Result<(), StateError> {
        limit(max_bytes)?;
        let failed = StateError("authority_state_write_failed");
        let raw = protocol::canonical::to_bytes(document).map_err(|_| failed)?;
        if raw.is_empty() || raw.len() as u64 > max_bytes {
            return Err(failed);
        }
        let parent = path.parent().ok_or(failed)?;
        let name = path.file_name().and_then(|n| n.to_str()).ok_or(failed)?;
        let temporary = parent.join(format!(".{name}.{}.tmp", unique()));
        let written = (|| -> std::io::Result<()> {
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&temporary)?;
            file.set_permissions(fs::Permissions::from_mode(0o600))?;
            file.write_all(&raw)?;
            file.sync_all()?;
            drop(file);
            #[cfg(test)]
            BEFORE_RENAME.with(|hook| {
                if let Some(hook) = hook.borrow().as_ref() {
                    hook(path);
                }
            });
            fs::rename(&temporary, path)?;
            File::open(parent)?.sync_all()
        })();
        if written.is_err() {
            // Either way the write failed; a cleanup error cannot change the public code.
            return Err(fs::remove_file(&temporary).map_or(failed, |()| failed));
        }
        Ok(())
    }
}
fn demo(code: &'static str) -> PublicError {
    PublicError::new(400, code)
}
/// A builtin `ValueError` in the source reaches the client as `action_not_completed` (409).
fn builtin() -> PublicError {
    builtin_of("ValueError")
}
fn builtin_of(class: &'static str) -> PublicError {
    PublicError::new(409, "action_not_completed").raised_by(crate::schema::Raised::Builtin(class))
}
fn json_decode() -> PublicError {
    PublicError::new(409, "action_not_completed").raised_by(crate::schema::Raised::JsonDecode)
}
/// The Python `OSError` subclass `OSError(errno, ...)` builds for this failure (macOS errno
/// numbers), as a builtin 409.
pub fn os_error(error: &std::io::Error) -> PublicError {
    builtin_of(match error.raw_os_error() {
        Some(1 | 13) => "PermissionError",
        Some(2) => "FileNotFoundError",
        Some(3) => "ProcessLookupError",
        Some(4) => "InterruptedError",
        Some(10) => "ChildProcessError",
        Some(17) => "FileExistsError",
        Some(20) => "NotADirectoryError",
        Some(21) => "IsADirectoryError",
        Some(32) => "BrokenPipeError",
        Some(35..=37) => "BlockingIOError",
        Some(53) => "ConnectionAbortedError",
        Some(54) => "ConnectionResetError",
        Some(60) => "TimeoutError",
        Some(61) => "ConnectionRefusedError",
        _ => "OSError",
    })
}
pub mod fast_mode {
    use super::*;
    pub const PROTOCOL: &str = "mycelium.a13_fast_mode.v1";
    pub const FILE: &str = "fast-mode.json";
    const MAX: u64 = 4096;
    /// `document(root)` in the source's dict order (`enabled`, `source`, `locked`); `env` is the
    /// value of `MYCELIUM_TOKEN_PATH` if set. Errors are the source's `ValueError` messages.
    pub fn document_json(root: &Path, env: Option<&str>) -> Result<Json, &'static str> {
        let doc = |enabled: bool, source: &str, locked: bool| {
            crate::jobj! {"enabled" => enabled, "source" => source, "locked" => locked}
        };
        if let Some(value) = env {
            if value != "on" && value != "off" {
                return Err("token_path_activation_invalid");
            }
            return Ok(doc(value == "on", "environment", true));
        }
        let invalid = "fast_mode_state_invalid";
        let saved = private_state::load(&root.join(FILE), MAX).map_err(|_| invalid)?;
        match saved {
            None => Ok(doc(false, "default", false)),
            Some(map) => {
                let enabled = map.get("enabled").and_then(Value::as_bool);
                if map.len() != 2 || map.get("protocol") != Some(&Value::from(PROTOCOL)) {
                    return Err(invalid);
                }
                Ok(doc(enabled.ok_or(invalid)?, "saved", false))
            }
        }
    }
    pub fn document(root: &Path, env: Option<&str>) -> Result<Value, &'static str> {
        document_json(root, env).map(|j| j.to_value())
    }
    /// `choose(app, body)` under the app's action guard: owner role, body shape, run activity and
    /// the environment lock are checked in that order before anything is written.
    pub fn choose(
        root: &Path,
        owner: bool,
        run_active: bool,
        env: Option<&str>,
        body: &Map<String, Value>,
    ) -> Result<Value, PublicError> {
        choose_then(root, owner, run_active, env, body, &mut || {})
    }
    /// `choose` with the source's `_refresh_fast_mode_process()` between the write and the
    /// returned document.
    pub fn choose_then(
        root: &Path,
        owner: bool,
        run_active: bool,
        env: Option<&str>,
        body: &Map<String, Value>,
        refresh: &mut dyn FnMut(),
    ) -> Result<Value, PublicError> {
        choose_then_json(root, owner, run_active, env, body, refresh).map(|j| j.to_value())
    }
    pub fn choose_then_json(
        root: &Path,
        owner: bool,
        run_active: bool,
        env: Option<&str>,
        body: &Map<String, Value>,
        refresh: &mut dyn FnMut(),
    ) -> Result<Json, PublicError> {
        if !owner {
            return Err(demo("owner_role_required"));
        }
        if body.len() != 1 || !body.get("enabled").is_some_and(Value::is_boolean) {
            return Err(demo("invalid_request"));
        }
        if run_active {
            return Err(demo("fast_mode_run_active"));
        }
        if document_json(root, env)
            .map_err(|_| builtin())?
            .get("locked")
            == Some(&Json::Bool(true))
        {
            return Err(demo("fast_mode_locked_by_environment"));
        }
        let record = serde_json::json!({"protocol": PROTOCOL, "enabled": body["enabled"]});
        private_state::store(&root.join(FILE), &record, MAX)
            .map_err(|e| PublicError::new(409, e.0))?;
        refresh();
        document_json(root, env).map_err(|_| builtin())
    }
}
pub mod run_model {
    use super::*;
    pub const PROTOCOL: &str = "mycelium.a13_run_model_choice.v1";
    pub const FILE: &str = "run-model.json";
    pub const CHOICES: [&str; 4] = ["a9-route", "qwen3-0.6b", "qwen3-8b", "qwen3-14b"];
    const MAX: u64 = 4096;
    /// The source raises a builtin `ValueError(code)`; the handler maps any non-`mycelium_*`
    /// exception to `public_failure_code` = `action_not_completed` (409), so the message must
    /// never reach the wire (integrator ruling (b)). `Reason` keeps it for diagnostics only.
    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    pub struct Reason(pub &'static str);
    impl From<Reason> for PublicError {
        fn from(_: Reason) -> Self {
            builtin()
        }
    }
    pub fn try_choice(name: &str) -> Result<&'static str, Reason> {
        CHOICES
            .iter()
            .find(|c| **c == name)
            .copied()
            .ok_or(Reason("run_model_choice_invalid"))
    }
    pub fn choice(name: &str) -> Result<&'static str, PublicError> {
        Ok(try_choice(name)?)
    }
    pub fn try_load_choice(root: &Path) -> Result<Option<&'static str>, Reason> {
        let invalid = Reason("run_model_state_invalid");
        let Some(map) = private_state::load(&root.join(FILE), MAX).map_err(|_| invalid)? else {
            return Ok(None);
        };
        if map.len() != 2 || map.get("protocol") != Some(&Value::from(PROTOCOL)) {
            return Err(invalid);
        }
        let name = map.get("choice").and_then(Value::as_str).ok_or(invalid)?;
        CHOICES
            .iter()
            .find(|c| **c == name)
            .copied()
            .map(Some)
            .ok_or(invalid)
    }
    pub fn load_choice(root: &Path) -> Result<Option<&'static str>, PublicError> {
        Ok(try_load_choice(root)?)
    }
    /// A write failure is a library (`mycelium_*`) error: its closed code is public.
    pub fn save_choice(root: &Path, name: &str) -> Result<(), PublicError> {
        let name = choice(name)?;
        let record = serde_json::json!({"protocol": PROTOCOL, "choice": name});
        private_state::store(&root.join(FILE), &record, MAX).map_err(|e| PublicError::new(409, e.0))
    }
    /// Run-model labels, pinned snapshot and int8 resident sizes of the four picker choices
    /// (run_model.CHOICES).
    const CHOICE_ROWS: [(&str, &str, u64, u64); 4] = [
        (
            "a9-route",
            "Qwen2.5-0.5B-Instruct \u{2014} tested starter (tested on two Macs; ~1 GB download if not cached)",
            999_604_126,
            500_000_000,
        ),
        (
            "qwen3-0.6b",
            "Qwen3-0.6B (not yet tested on two Macs)",
            1_519_209_243,
            750_000_000,
        ),
        (
            "qwen3-8b",
            "Qwen3-8B (not yet tested on two Macs)",
            16_397_461_266,
            8_200_000_000,
        ),
        (
            "qwen3-14b",
            "Qwen3-14B (not yet tested on two Macs)",
            29_552_613_776,
            14_800_000_000,
        ),
    ];
    /// `MYCELIUM_A13_RUN_MODEL`: the operator override of the picker.
    pub const ENV: &str = "MYCELIUM_A13_RUN_MODEL";
    /// Run-time facts the caller owns (os.environ, app flags, host memory).
    #[derive(Clone, Copy, Debug, Default)]
    pub struct Facts<'a> {
        pub env: Option<&'a str>,
        pub acquisition_active: bool,
        pub owner_run_active: bool,
        pub device_memory_bytes: Option<u64>,
    }
    fn picker_model(name: &str) -> Option<&'static model_catalog::Model> {
        let index = CHOICES.iter().position(|c| *c == name)?;
        model_catalog::registered().get(index + 1)
    }
    fn demo_default_choice() -> Option<&'static str> {
        let starter = picker_model("a9-route")?;
        (model_catalog::owner_storage_requirement(starter)
            <= model_catalog::storage_consent_ceiling())
        .then_some("a9-route")
    }
    /// `app_run_model`: `(name, source, model)`. A bad environment value or unreadable state is
    /// a builtin `ValueError` in the source, hence the collapsed 409.
    fn app_run_model(
        root: &Path,
        env: Option<&str>,
    ) -> Result<(String, &'static str, &'static model_catalog::Model), PublicError> {
        if let Some(value) = env {
            let model = model_catalog::by_run_model_name(value).ok_or_else(builtin)?;
            return Ok((value.to_string(), "environment", model));
        }
        if let Some(saved) = load_choice(root).map_err(|_| builtin())? {
            return Ok((
                saved.to_string(),
                "saved",
                picker_model(saved).ok_or_else(builtin)?,
            ));
        }
        let name = demo_default_choice().ok_or_else(builtin)?;
        Ok((
            name.to_string(),
            "demo_default",
            picker_model(name).ok_or_else(builtin)?,
        ))
    }
    /// `app_run_model(root)[2]`: the model this installation's runs pin (picker choice, environment override).
    pub fn selected_model(
        root: &Path,
        env: Option<&str>,
    ) -> Result<&'static model_catalog::Model, PublicError> {
        app_run_model(root, env).map(|(_, _, model)| model)
    }
    /// `run_model.document(root)`: the picker state shown by the UI, in the source's dict order.
    pub fn document_json(
        root: &Path,
        env: Option<&str>,
        device_memory_bytes: Option<u64>,
    ) -> Result<Json, PublicError> {
        let (name, source, model) = app_run_model(root, env)?;
        let ceiling = model_catalog::storage_consent_ceiling();
        let choices: Vec<Json> = CHOICE_ROWS
            .iter()
            .filter_map(|(choice, label, snapshot, int8)| {
                let m = picker_model(choice)?;
                let owner = model_catalog::owner_storage_requirement(m);
                Some(crate::jobj! {
                    "name" => *choice, "label" => *label, "model_id" => m.model_id,
                    "resolved_commit" => m.resolved_commit, "snapshot_bytes" => *snapshot,
                    "int8_weight_bytes" => *int8, "owner_storage_bytes" => owner,
                    "fits_storage_ceiling" => owner <= ceiling,
                })
            })
            .collect();
        Ok(crate::jobj! {
            "selected" => name, "source" => source, "locked" => source == "environment",
            "model_id" => model.model_id, "resolved_commit" => model.resolved_commit,
            "demo_default" => demo_default_choice(), "storage_ceiling_bytes" => ceiling,
            "resources" => model_catalog::consent_document_json(
                Some((model.model_id, model.resolved_commit)),
                device_memory_bytes,
            ),
            "choices" => choices,
        })
    }
    pub fn document(
        root: &Path,
        env: Option<&str>,
        device_memory_bytes: Option<u64>,
    ) -> Result<Value, PublicError> {
        document_json(root, env, device_memory_bytes).map(|j| j.to_value())
    }
    /// `DemoApp._choose_run_model` under the app guard: shape, choice, environment lock,
    /// acquisition and owner-run activity are checked in that order before anything is written.
    /// `open_acquisition` is the host's reset of `acquisition_policy_error` + `_open_acquisition`;
    /// its failure is the answer (raised after the choice was saved, app.py:866-867).
    pub fn choose(
        root: &Path,
        facts: &Facts<'_>,
        body: &Map<String, Value>,
        open_acquisition: &mut dyn FnMut() -> Result<(), PublicError>,
    ) -> Result<Value, PublicError> {
        choose_json(root, facts, body, open_acquisition).map(|j| j.to_value())
    }
    pub fn choose_json(
        root: &Path,
        facts: &Facts<'_>,
        body: &Map<String, Value>,
        open_acquisition: &mut dyn FnMut() -> Result<(), PublicError>,
    ) -> Result<Json, PublicError> {
        if body.len() != 1 || !body.contains_key("choice") {
            return Err(demo("invalid_request"));
        }
        let picked = body["choice"]
            .as_str()
            .and_then(|name| CHOICES.iter().find(|c| **c == name))
            .ok_or_else(|| demo("run_model_choice_invalid"))?;
        if app_run_model(root, facts.env)?.1 == "environment" {
            return Err(demo("run_model_locked_by_environment"));
        }
        if facts.acquisition_active {
            return Err(demo("acquisition_already_active"));
        }
        if facts.owner_run_active {
            return Err(demo("owner_run_active"));
        }
        save_choice(root, picked)?;
        open_acquisition()?;
        document_json(root, facts.env, facts.device_memory_bytes)
    }
}

/// `resource-policy.json`: the owner's/target's consent form (app.py `consent_valid`,
/// `read_resource_policy`, `private_write` @ f4e366e9).
/// `json.dumps(value, sort_keys=True)`: `", "` and `": "` separators, ASCII escapes.
pub fn dumps_sorted(value: &Value) -> String {
    let mut out = String::new();
    resource_policy::dumps(value, &mut out);
    out
}
/// `app.private_write(root / name, raw)`: exclusive 0600 temporary file, fsync, rename,
/// directory fsync, temporary removed (app.py:216-234). Any failure is a builtin `OSError`
/// (409 `action_not_completed`).
pub fn private_write(root: &Path, name: &str, raw: &[u8]) -> Result<(), PublicError> {
    use std::{fs, io::Write, os::unix::fs::OpenOptionsExt};
    let temporary = root.join(format!(".{name}.{}", &private_state::unique()[..16]));
    let written = (|| -> std::io::Result<()> {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(raw)?;
        file.flush()?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, root.join(name))?;
        fs::File::open(root)?.sync_all()
    })();
    if let Err(error) = written {
        // `finally: temp.unlink(missing_ok=True)`; a cleanup error cannot change the verdict.
        let verdict = os_error(&error);
        return Err(fs::remove_file(&temporary).map_or(verdict, |()| verdict));
    }
    Ok(())
}
pub mod resource_policy {
    use super::*;
    use std::{
        fs::{self, File},
        io::Read,
        os::unix::fs::MetadataExt,
    };
    pub const FILE: &str = "resource-policy.json";
    /// `info.st_size > 4096` / `os.read(fd, 4097)`.
    const MAX: u64 = 4096;
    /// The seven keys of a consent form.
    const KEYS: [&str; 7] = [
        "peer_visibility",
        "power",
        "storage_bytes",
        "network",
        "background",
        "diagnostics",
        "thermal",
    ];
    /// `consent_valid`: exactly the seven keys, the offered literals, and a storage figure that is
    /// a JSON integer within `[100_000_000, storage_consent_ceiling]`.
    pub fn consent_valid(consent: &Value) -> bool {
        let Some(c) = consent.as_object() else {
            return false;
        };
        c.len() == KEYS.len()
            && KEYS.iter().all(|k| c.contains_key(*k))
            && c["peer_visibility"] == Value::Bool(true)
            && matches!(c["power"].as_str(), Some("ac_only" | "battery_allowed"))
            && c["storage_bytes"].as_u64().is_some_and(|v| {
                (100_000_000..=model_catalog::storage_consent_ceiling()).contains(&v)
            })
            && matches!(
                c["network"].as_str(),
                Some("unmetered_only" | "metered_allowed")
            )
            && c["background"] == Value::Bool(false)
            && c["diagnostics"] == Value::Bool(false)
            && c["thermal"].as_str() == Some("stop_on_serious")
    }
    fn invalid() -> PublicError {
        demo("resource_policy_invalid")
    }
    /// Python's `json.loads(bytes)`: any `json.detect_encoding` encoding (utf-8/-sig, utf-16,
    /// utf-32; integrator ruling (c)), and the `NaN`/`Infinity` constants (which can never be a
    /// valid consent).
    fn parse(raw: &[u8]) -> Result<Value, PublicError> {
        let text = crate::wire::decode_json_bytes(raw).map_err(|error| match error {
            crate::wire::DecodeError::Unicode => builtin_of("UnicodeDecodeError"),
            crate::wire::DecodeError::LeadingBom => json_decode(),
        })?;
        let text = crate::wire::neutralise_surrogate_escapes(&text);
        let text = text.as_ref();
        if let Ok(v) = serde_json::from_str::<Value>(text) {
            return Ok(v);
        }
        let mut cleaned = String::with_capacity(text.len());
        let (mut in_string, mut escaped, mut rest) = (false, false, text);
        while let Some(c) = rest.chars().next() {
            let tail = &rest[c.len_utf8()..];
            if in_string {
                in_string = c != '"' || escaped;
                escaped = c == '\\' && !escaped;
            } else if c == '"' {
                in_string = true;
            } else if let Some(after) = ["-Infinity", "Infinity", "NaN"]
                .iter()
                .find_map(|t| rest.strip_prefix(t))
            {
                cleaned.push_str("null");
                rest = after;
                continue;
            }
            cleaned.push(c);
            rest = tail;
        }
        // A non-finite constant parses in Python but is never a valid consent value.
        serde_json::from_str::<Value>(&cleaned).map_err(|_| json_decode())
    }
    /// `read_resource_policy`: absent => `None`; unsafe file or invalid form => 400
    /// `resource_policy_invalid`; an unreadable/unparsable file is a builtin failure (409).
    pub fn read(root: &Path) -> Result<Option<Value>, PublicError> {
        let path = root.join(FILE);
        let meta = match fs::symlink_metadata(&path) {
            Ok(m) => m,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(os_error(&e)),
        };
        // O_NOFOLLOW: a symlink is an OSError (ELOOP), not a policy verdict.
        if meta.file_type().is_symlink() {
            return Err(builtin_of("OSError"));
        }
        let mut file = File::open(&path).map_err(|e| os_error(&e))?;
        let opened = file.metadata().map_err(|e| os_error(&e))?;
        if (opened.dev(), opened.ino()) != (meta.dev(), meta.ino()) {
            return Err(builtin_of("OSError"));
        }
        if !opened.file_type().is_file()
            || opened.nlink() != 1
            || opened.uid() != rustix::process::getuid().as_raw()
            || opened.mode() & 0o7777 != 0o600
            || opened.len() > MAX
        {
            return Err(invalid());
        }
        let mut raw = Vec::new();
        (&mut file)
            .take(MAX + 1)
            .read_to_end(&mut raw)
            .map_err(|e| os_error(&e))?;
        let value = parse(&raw)?;
        if consent_valid(&value) {
            Ok(Some(value))
        } else {
            Err(invalid())
        }
    }
    fn ascii_string(s: &str) -> String {
        let mut out = String::from("\"");
        for c in s.chars() {
            match c {
                '"' => out.push_str("\\\""),
                '\\' => out.push_str("\\\\"),
                '\n' => out.push_str("\\n"),
                '\r' => out.push_str("\\r"),
                '\t' => out.push_str("\\t"),
                '\u{8}' => out.push_str("\\b"),
                '\u{c}' => out.push_str("\\f"),
                c if (' '..='~').contains(&c) => out.push(c),
                c => {
                    let mut units = [0u16; 2];
                    for unit in c.encode_utf16(&mut units) {
                        out.push_str(&format!("\\u{unit:04x}"));
                    }
                }
            }
        }
        out.push('"');
        out
    }
    /// `json.dumps(value, sort_keys=True)`: `", "` and `": "` separators, ASCII escapes.
    pub(super) fn dumps(value: &Value, out: &mut String) {
        match value {
            Value::Object(map) => {
                out.push('{');
                let mut keys: Vec<&String> = map.keys().collect();
                keys.sort();
                for (i, key) in keys.into_iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    out.push_str(&ascii_string(key));
                    out.push_str(": ");
                    dumps(&map[key.as_str()], out);
                }
                out.push('}');
            }
            Value::Array(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ");
                    }
                    dumps(item, out);
                }
                out.push(']');
            }
            Value::String(s) => out.push_str(&ascii_string(s)),
            other => out.push_str(&other.to_string()),
        }
    }
    /// `private_write(root / 'resource-policy.json', json.dumps(consent, sort_keys=True))`:
    /// exclusive 0600 temporary file, fsync, rename, directory fsync, temporary removed.
    pub fn write(root: &Path, consent: &Value) -> Result<(), PublicError> {
        let mut raw = String::new();
        dumps(consent, &mut raw);
        private_write(root, FILE, raw.as_bytes())
    }
}
/// `standing-consent.json` (standing_consent.py @ f4e366e9): the one explicit grant a joining
/// Mac records, its binding to the accepted membership, and its withdrawal.
pub mod standing_consent {
    use super::*;
    /// The automatic acceptor of owner work offers under this grant (`StandingAcceptor`).
    pub use crate::standing_acceptor::{
        AcceptFailure, StandingAcceptor, WorkAcceptance, check_work_preview, standing_limits,
    };
    use sha2::{Digest, Sha256};
    pub const FILE: &str = "standing-consent.json";
    pub const PROTOCOL: &str = "mycelium.a13_standing_consent.v1";
    pub const DISCLOSURE_TEXT: &str = "While this Mac is a member of the owner's swarm and this app is open, it will automatically: accept work the owner assigns to it; download and store only the model slice assigned to it; load that slice with MLX and compute it for requests routed through the swarm; and send the owner a capability report (chip, memory, free disk, MLX availability, power and thermal state) and a link measurement over the relay on join and before each run. Limits: at most the disk, memory, run time and command budget you choose below, only on the power and network policy you chose, and it stops on serious thermal pressure. Cooperative workers observe the activations they compute, so this is not confidential inference. You can withdraw this consent at any time from this app; automatic acceptance stops immediately.";
    /// `"sha256:" + sha256(DISCLOSURE_TEXT)`; `disclosure_digest()` recomputes it for the test.
    pub const DISCLOSURE_DIGEST: &str =
        "sha256:f22a00621bc11a4379ad40a508974f7f8325b3623a30ae2b2e674bd945915e19";
    pub fn disclosure_digest() -> String {
        let hex: String = Sha256::digest(DISCLOSURE_TEXT.as_bytes())
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect();
        format!("sha256:{hex}")
    }
    /// A `StandingConsentError` or the authority's `DemoAuthorityError`: a closed code.
    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    pub struct StandingError(pub &'static str);
    const CAP_FIELDS: [&str; 4] = [
        "storage_bytes",
        "max_memory_bytes",
        "max_wall_seconds",
        "max_commands",
    ];
    const BINDING_FIELDS: [&str; 3] = ["swarm_id", "node_id", "seed_key_digest"];
    const FIELDS: [&str; 8] = [
        "protocol",
        "disclosure_digest",
        "caps",
        "granted_at",
        "binding",
        "bound_at",
        "withdrawn_at",
        "state",
    ];
    fn has_exactly(map: &Map<String, Value>, keys: &[&str]) -> bool {
        map.len() == keys.len() && keys.iter().all(|k| map.contains_key(*k))
    }
    fn limit() -> Result<u64, StandingError> {
        private_state::private_json_limit().map_err(|e| StandingError(e.0))
    }
    fn write(root: &Path, document: &Map<String, Value>) -> Result<(), StandingError> {
        private_state::store(&root.join(FILE), &Value::Object(document.clone()), limit()?)
            .map_err(|e| StandingError(e.0))
    }
    /// `load_standing_consent`.
    pub fn load(root: &Path) -> Result<Option<Map<String, Value>>, StandingError> {
        let Some(value) =
            private_state::load(&root.join(FILE), limit()?).map_err(|e| StandingError(e.0))?
        else {
            return Ok(None);
        };
        let valid = has_exactly(&value, &FIELDS)
            && value["protocol"] == PROTOCOL
            && matches!(
                value["state"].as_str(),
                Some("granted_unbound" | "granted" | "withdrawn")
            )
            && value["caps"]
                .as_object()
                .is_some_and(|c| has_exactly(c, &CAP_FIELDS))
            && (value["binding"].is_null()
                || value["binding"]
                    .as_object()
                    .is_some_and(|b| has_exactly(b, &BINDING_FIELDS)));
        if valid {
            Ok(Some(value))
        } else {
            Err(StandingError("standing_consent_state_invalid"))
        }
    }
    /// The stored grant, usable only for `binding`.
    ///
    /// A grant bound to another swarm, node or seed key is a leftover from an earlier membership.
    /// Treating it as a grant is what made a freshly joined Mac silently do nothing: the agent's
    /// gate dropped every pass, wrote no card and logged nothing, so the owner's run only ever said
    /// "1 of 2 Macs reported".
    pub fn for_binding(
        root: &Path,
        binding: &Value,
    ) -> Result<Option<Map<String, Value>>, StandingError> {
        let Some(record) = load(root)? else {
            return Ok(None);
        };
        let usable = record["state"] == "granted" && record["binding"] == *binding;
        Ok(usable.then_some(record))
    }
    /// Drop any *bound* grant before a new join.
    ///
    /// `grant` answers with the stored record whenever it is not `withdrawn`, so a Mac that had
    /// already granted consent to an earlier swarm kept that record through every later join -- and
    /// its binding could never match the new membership. The member then passed no gate, wrote no
    /// card and logged nothing. A join always starts from a fresh, unbound grant.
    pub fn clear_bound(root: &Path) -> Result<bool, StandingError> {
        let Some(record) = load(root)? else {
            return Ok(false);
        };
        if record["binding"].is_null() {
            return Ok(false);
        }
        std::fs::remove_file(root.join(FILE))
            .map_err(|_| StandingError("standing_consent_state_invalid"))?;
        Ok(true)
    }
    /// Drop a grant another membership bound, so a new join cannot inherit it. Fail-closed: the
    /// member then has to grant again instead of quietly running on a stranger's consent.
    pub fn clear_if_stale(root: &Path, binding: &Value) -> Result<bool, StandingError> {
        let Some(record) = load(root)? else {
            return Ok(false);
        };
        let stale = record["state"] == "granted"
            && !record["binding"].is_null()
            && record["binding"] != *binding;
        if !stale {
            return Ok(false);
        }
        std::fs::remove_file(root.join(FILE))
            .map_err(|_| StandingError("standing_consent_state_invalid"))?;
        Ok(true)
    }
    /// The caps a grant would record, checked without touching any state: the
    /// acknowledged disclosure and `_caps(caps, policy, device_memory_bytes)`.
    /// v0.1.4 runs exactly this before any role change (`app.py:1751-1757`).
    pub fn decision_caps(
        body: &Value,
        policy: &Value,
        device_memory_bytes: Option<u64>,
    ) -> Result<Value, StandingError> {
        let required = StandingError("standing_consent_required");
        let b = body.as_object().ok_or(required)?;
        if !has_exactly(b, &["acknowledged", "disclosure_digest", "caps"])
            || b["acknowledged"] != Value::Bool(true)
            || b["disclosure_digest"] != DISCLOSURE_DIGEST
        {
            return Err(required);
        }
        let memory = device_memory_bytes.map_or(Value::Null, Value::from);
        protocol::consent::caps(&b["caps"], policy, &memory).map_err(|e| StandingError(e.0))
    }
    /// `grant_standing_consent`: the one explicit grant shown at join (unbound until membership).
    pub fn grant(
        root: &Path,
        body: &Value,
        policy: &Value,
        now: f64,
        device_memory_bytes: Option<u64>,
    ) -> Result<Map<String, Value>, StandingError> {
        let caps = decision_caps(body, policy, device_memory_bytes)?;
        if let Some(existing) = load(root)?
            && existing["state"] != "withdrawn"
        {
            if existing["caps"] != caps {
                return Err(StandingError("standing_consent_changed"));
            }
            return Ok(existing);
        }
        if !now.is_finite() {
            return Err(StandingError("standing_consent_time_invalid"));
        }
        let document = serde_json::json!({
            "protocol": PROTOCOL, "disclosure_digest": DISCLOSURE_DIGEST, "caps": caps,
            "granted_at": now, "binding": null, "bound_at": null, "withdrawn_at": null,
            "state": "granted_unbound",
        });
        let document = document.as_object().cloned().unwrap_or_default();
        write(root, &document)?;
        Ok(document)
    }
    /// `bind_standing_consent`: a different owner never inherits the grant.
    pub fn bind(
        root: &Path,
        binding: &Value,
        now: f64,
    ) -> Result<Map<String, Value>, StandingError> {
        let invalid = StandingError("standing_consent_binding_invalid");
        let b = binding.as_object().ok_or(invalid)?;
        if !has_exactly(b, &BINDING_FIELDS)
            || !BINDING_FIELDS
                .iter()
                .all(|k| b[*k].as_str().is_some_and(|s| !s.is_empty()))
        {
            return Err(invalid);
        }
        let mut existing = load(root)?.ok_or(StandingError("standing_consent_missing"))?;
        if existing["state"] == "withdrawn" {
            return Err(StandingError("standing_consent_withdrawn"));
        }
        let wanted = Value::Object(b.clone());
        if !existing["binding"].is_null() {
            if existing["binding"] != wanted {
                return Err(StandingError("standing_consent_binding_changed"));
            }
            return Ok(existing);
        }
        existing.insert("binding".into(), wanted);
        existing.insert("bound_at".into(), serde_json::json!(now));
        existing.insert("state".into(), "granted".into());
        write(root, &existing)?;
        Ok(existing)
    }
    /// `withdraw_standing_consent`: absent or already withdrawn is returned unchanged.
    pub fn withdraw(root: &Path, now: f64) -> Result<Option<Map<String, Value>>, StandingError> {
        let Some(mut existing) = load(root)? else {
            return Ok(None);
        };
        if existing["state"] == "withdrawn" {
            return Ok(Some(existing));
        }
        existing.insert("withdrawn_at".into(), serde_json::json!(now));
        existing.insert("state".into(), "withdrawn".into());
        write(root, &existing)?;
        Ok(Some(existing))
    }
}
