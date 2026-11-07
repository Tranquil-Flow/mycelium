// SPDX-License-Identifier: AGPL-3.0-only
//! The adaptive host policy file (`mycelium_a13_macos/adaptive_host.py:31-200` and
//! `adaptive_owner.py` @ f4e366e9): `adaptive-host-policy.json` read, validated and written exactly as
//! the source does, the first-consent default (`apply_first_consent_choice`) and the off/error branches of
//! `adaptive_status`. The controller that acts on an enabled policy (sampler, hysteresis, publications)
//! is not here: `static_status` answers `None` for an enabled policy.
use crate::{
    preferences::os_error,
    schema::PublicError,
    wire::{self, Json, Text},
};
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value};
use std::{
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::Path,
};

pub const FILE: &str = "adaptive-host-policy.json";
const INVALID: &str = "adaptive_policy_invalid";
const UNAUTHORIZED: &str = "adaptive_toggle_unauthorized";
/// `_POLICY_KEYS` (`DEFAULT_POLICY`'s keys).
const POLICY_KEYS: [&str; 6] = [
    "schema",
    "enabled",
    "game_mode",
    "reserve_bytes",
    "cpu_saturation_fraction",
    "timings",
];
/// `DEFAULT_TIMINGS` keys, in source order, with the registry entry of each default (seconds).
const TIMINGS: [(&str, Option<BudgetId>); 11] = [
    (
        "sample_interval_s",
        Some(BudgetId::PyFinalMyceliumA13MacosAdaptiveHostSampleIntervalSL49C4),
    ),
    (
        "stale_after_s",
        Some(BudgetId::PyFinalMyceliumA13MacosAdaptiveHostStaleAfterSL50C4),
    ),
    (
        "game_drain_s",
        Some(BudgetId::PyFinalMyceliumA13MacosAdaptiveHostGameDrainSL51C4),
    ),
    (
        "pressure_drain_s",
        Some(BudgetId::PyFinalMyceliumA13MacosAdaptiveHostPressureDrainSL52C4),
    ),
    (
        "zero_capacity_s",
        Some(BudgetId::PyFinalMyceliumA13MacosAdaptiveHostZeroCapacitySL53C4),
    ),
    (
        "withdraw_s",
        Some(BudgetId::PyFinalMyceliumA13MacosAdaptiveHostWithdrawSL54C4),
    ),
    (
        "reduced_dwell_s",
        Some(BudgetId::PyFinalMyceliumA13MacosAdaptiveHostReducedDwellSL55C4),
    ),
    (
        "drain_dwell_s",
        Some(BudgetId::PyFinalMyceliumA13MacosAdaptiveHostDrainDwellSL56C4),
    ),
    (
        "recovery_s",
        Some(BudgetId::PyFinalMyceliumA13MacosAdaptiveHostRecoverySL57C4),
    ),
    (
        "cpu_sustain_s",
        Some(BudgetId::PyFinalMyceliumA13MacosAdaptiveHostCpuSustainSL58C4),
    ),
    // `"authority_observations": 2`: a count, validated as an integer (see below).
    ("authority_observations", None),
];
// SOURCE-UNREGISTERED bounds (seam SE-L10-ADAPTIVE-BOUNDS to the budget registry): adaptive_host.py:60-61
// `_TIMING_MIN = 1` / `_TIMING_MAX = 3600` seconds and adaptive_host.py:124 `2 <= authority_observations <= 10`.
const TIMING_MIN: f64 = 1.0;
const TIMING_MAX: f64 = 3600.0;
const OBSERVATIONS: std::ops::RangeInclusive<i64> = 2..=10;

/// `AdaptiveHostError(code)`, or the `OSError` a write raised.
#[derive(Debug)]
pub enum Failure {
    Adaptive(&'static str),
    Os(PublicError),
}
fn invalid() -> Failure {
    Failure::Adaptive(INVALID)
}

fn max_policy_bytes() -> u64 {
    BudgetId::PyMyceliumA13MacosAdaptiveHostMaxPolicyBytesL32
        .bytes()
        .map_or(0, |b| b.get())
}
fn max_bytes() -> i64 {
    BudgetId::PyShiftMyceliumA13MacosAdaptiveHostMaxBytesL88C0
        .bytes()
        .map_or(0, |b| i64::try_from(b.get()).unwrap_or(i64::MAX))
}
/// The default of a timing, in seconds.
fn default_seconds(id: BudgetId) -> f64 {
    id.span(Entries::new(0))
        .map_or(0.0, |s| s.millis() as f64 / 1000.0)
}

/// `DEFAULT_POLICY`.
pub fn default_policy() -> Map<String, Value> {
    let mut policy = Map::new();
    policy.insert("schema".into(), Value::from(1));
    policy.insert("enabled".into(), Value::Bool(false));
    policy.insert("game_mode".into(), Value::Bool(false));
    policy.insert("reserve_bytes".into(), Value::from(0));
    policy.insert("cpu_saturation_fraction".into(), Value::Null);
    policy.insert("timings".into(), Value::Object(Map::new()));
    policy
}

/// `_is_number`: a finite `int` or `float` that is not a `bool`.
fn number(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64().filter(|f| f.is_finite()),
        _ => None,
    }
}
fn integer(value: &Value) -> Option<i64> {
    match value {
        Value::Number(n) => n.as_i64(),
        _ => None,
    }
}

/// `validate_policy`: a normalised copy, or `adaptive_policy_invalid`.
pub fn validate(policy: &Value) -> Result<Map<String, Value>, Failure> {
    let map = policy.as_object().ok_or_else(invalid)?;
    let keys: Vec<&str> = map
        .keys()
        .map(String::as_str)
        .filter(|k| *k != "automatic")
        .collect();
    if keys.len() != POLICY_KEYS.len() || !POLICY_KEYS.iter().all(|k| keys.contains(k)) {
        return Err(invalid());
    }
    if let Some(automatic) = map.get("automatic") {
        match automatic {
            Value::Bool(on) if !*on || map["enabled"] == Value::Bool(true) => {}
            _ => return Err(invalid()),
        }
    }
    if integer(&map["schema"]) != Some(1) {
        return Err(invalid());
    }
    if !map["enabled"].is_boolean() || !map["game_mode"].is_boolean() {
        return Err(invalid());
    }
    if !integer(&map["reserve_bytes"]).is_some_and(|n| (0..=max_bytes()).contains(&n)) {
        return Err(invalid());
    }
    let fraction = &map["cpu_saturation_fraction"];
    if !fraction.is_null() && !number(fraction).is_some_and(|f| 0.0 < f && f <= 1.0) {
        return Err(invalid());
    }
    let timings = map["timings"].as_object().ok_or_else(invalid)?;
    if !timings
        .keys()
        .all(|k| TIMINGS.iter().any(|(name, _)| name == k))
    {
        return Err(invalid());
    }
    for value in timings.values() {
        if !number(value).is_some_and(|n| (TIMING_MIN..=TIMING_MAX).contains(&n)) {
            return Err(invalid());
        }
    }
    if timings
        .get("authority_observations")
        .is_some_and(|n| !integer(n).is_some_and(|n| OBSERVATIONS.contains(&n)))
    {
        return Err(invalid());
    }
    let merged = |name: &str| -> f64 {
        timings.get(name).and_then(number).unwrap_or_else(|| {
            TIMINGS
                .iter()
                .find(|(n, _)| *n == name)
                .and_then(|(_, id)| *id)
                .map_or(0.0, default_seconds)
        })
    };
    if merged("stale_after_s") < merged("sample_interval_s") {
        return Err(invalid());
    }
    let chain = [
        "game_drain_s",
        "pressure_drain_s",
        "zero_capacity_s",
        "withdraw_s",
    ];
    if chain
        .windows(2)
        .any(|pair| merged(pair[0]) > merged(pair[1]))
    {
        return Err(invalid());
    }
    Ok(map.clone())
}

/// `read_adaptive_policy`: a missing file is the migrated *off* default; an unsafe, oversized,
/// unparsable or invalid one is `adaptive_policy_invalid`.
pub fn read(root: &Path) -> Result<Map<String, Value>, Failure> {
    let path = root.join(FILE);
    let meta = match fs::symlink_metadata(&path) {
        Ok(meta) => meta,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(default_policy()),
        Err(_) => return Err(invalid()),
    };
    // O_NOFOLLOW: a symlink is an OSError (ELOOP), which the source also reports as invalid.
    if meta.file_type().is_symlink() {
        return Err(invalid());
    }
    let mut file = File::open(&path).map_err(|_| invalid())?;
    let opened = file.metadata().map_err(|_| invalid())?;
    if (opened.dev(), opened.ino()) != (meta.dev(), meta.ino())
        || !opened.file_type().is_file()
        || opened.nlink() != 1
        || opened.uid() != rustix::process::getuid().as_raw()
        || opened.mode() & 0o7777 != 0o600
        || opened.len() > max_policy_bytes()
    {
        return Err(invalid());
    }
    let mut raw = Vec::new();
    (&mut file)
        .take(max_policy_bytes() + 1)
        .read_to_end(&mut raw)
        .map_err(|_| invalid())?;
    // `json.loads(bytes)`: any detected encoding; a non-finite constant or a lone surrogate parses in
    // Python but can never be part of a valid policy (every string is a fixed key).
    let text = wire::decode_json_bytes(&raw).map_err(|_| invalid())?;
    let value: Value = serde_json::from_str(&text).map_err(|_| invalid())?;
    validate(&value)
}

/// `json.dumps(policy, sort_keys=True)` (default separators; a policy holds no strings but its keys).
fn render(policy: &Map<String, Value>) -> Vec<u8> {
    let compact = wire::dumps_sorted(&Value::Object(policy.clone()), Text::Ascii);
    let mut out = Vec::with_capacity(compact.len() * 2);
    for byte in compact {
        out.push(byte);
        if byte == b',' || byte == b':' {
            out.push(b' ');
        }
    }
    out
}

/// `write_adaptive_policy`: refuses unless the caller proves owner authority; persists the normalised
/// policy with a private temporary file, `fsync` and an atomic replace.
pub fn write(root: &Path, policy: &Value, owner_authorized: bool) -> Result<(), Failure> {
    if !owner_authorized {
        return Err(Failure::Adaptive(UNAUTHORIZED));
    }
    let raw = render(&validate(policy)?);
    let path = root.join(FILE);
    let temporary = root.join(format!(".{FILE}.{}.tmp", std::process::id()));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)
        .map_err(|e| Failure::Os(os_error(&e)))?;
    let written = file
        .write_all(&raw)
        .and_then(|()| file.flush())
        .and_then(|()| file.sync_all())
        .and_then(|()| fs::rename(&temporary, &path));
    // `finally: temporary.unlink(missing_ok=True)`.
    match fs::remove_file(&temporary) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => {
            return Err(Failure::Os(os_error(&e)));
        }
        _ => {}
    }
    written.map_err(|e| Failure::Os(os_error(&e)))
}

/// `apply_first_consent_choice`: the tick at first consent writes `{enabled, automatic}` unless a policy
/// (or anything at its path) already exists; a policy problem never fails enrolment.
pub fn apply_first_consent_choice(root: &Path, choice: Option<bool>) -> Result<(), PublicError> {
    let Some(enabled) = choice else {
        return Ok(());
    };
    if fs::symlink_metadata(root.join(FILE)).is_ok() {
        return Ok(());
    }
    let mut policy = default_policy();
    policy.insert("enabled".into(), Value::Bool(enabled));
    policy.insert("automatic".into(), Value::Bool(enabled));
    match write(root, &Value::Object(policy), true) {
        Ok(()) | Err(Failure::Adaptive(_)) => Ok(()),
        Err(Failure::Os(error)) => Err(error),
    }
}

/// The branches of `adaptive_status` that need no controller: an unreadable policy and a policy that is off.
/// `None` for an enabled policy (the runtime answers that one).
pub fn static_status(root: &Path) -> Option<Json> {
    match read(root) {
        Err(Failure::Adaptive(code)) => Some(Json::object([
            ("enabled", Json::Bool(false)),
            ("state", Json::str("error")),
            ("policy_error", Json::str(code)),
            ("new_admissions_allowed", Json::Bool(false)),
        ])),
        Err(Failure::Os(_)) => None,
        Ok(policy) if policy["enabled"] == Value::Bool(false) => Some(Json::object([
            ("enabled", Json::Bool(false)),
            ("game_mode", Json::Bool(false)),
            ("pause_sharing", Json::Bool(false)),
            ("adaptive_sharing", Json::Bool(false)),
            ("stopped", Json::Bool(false)),
            ("state", Json::str("off")),
            ("line", Json::str("Adaptive sharing: off")),
            ("reserve_bytes", Json::from(&policy["reserve_bytes"])),
        ])),
        Ok(_) => None,
    }
}
