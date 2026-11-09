// SPDX-License-Identifier: AGPL-3.0-only
//! `ProductProjector` (`mycelium_product_spine/projector.py` @ f4e366e9): the member rows of
//! `_build` (`:690-814`), behind the projector seam [`ProductProjector::project_members`]: the device
//! entity, the membership readiness row, the revoked notice and incident, the unplaced notice, and the
//! current/eligible member sets the rest of `_build` reads.
//!
//! A member row is the `Mapping[str, Any]` `_build` copies (`:410`), JSON-shaped: `null` is `None`, an
//! integer literal an `int` (any size), any other number a `float`. Python behaviour kept on purpose:
//! - `lease_expires_at` and `last_liveness_at` are `int(float(member.get(key, 0)) * 1_000)` (`:696`,
//!   `:720`). A missing key is `0`. `None`, a list or a mapping raise `TypeError`; text `float()` refuses
//!   (empty, blank, `0x10`, ...) raises `ValueError`; NaN raises `ValueError` and an infinity
//!   `OverflowError` at `int()`; an `int` beyond the float range raises `OverflowError`. Numeric text
//!   (surrounding whitespace, `_` between digits, exponents, `inf`/`nan` spellings, Unicode decimal
//!   digits) and `bool` are read as `float()` reads them. `int()` truncates toward zero and keeps every
//!   digit: no value is capped or defaulted.
//! - A member is fresh iff that lease is `> now_unix_ms` (`:697`); equality is expired.
//! - `peer_class`, `incarnation` and `runtime_capability`'s `runtime_backend` and `transport` are
//!   `str(value)` (`:738`, `:743`, `:746-747`): text as is; `None`/`True`/`False`; an `int` with every
//!   digit (`str()` refuses more than 4300 with `ValueError`); a `float` as `float.__repr__` (`inf` beyond
//!   the float range); a list or dict as its `repr()`, items `repr()`ed recursively (strings quoted and
//!   escaped by `protocol::gossip::python_str_repr`, whose pinned printability table is CPython 3.14's,
//!   Unicode 16.0), keys in the order the row's `Map` holds them. Without serde_json's
//!   `preserve_order` (serde_json 1.0.154 `src/map.rs:38-39`) that order is sorted: a dict's insertion
//!   order is gone before this seam (declared limit; the oracle's product path only delivers
//!   key-sorted mappings there, `mycelium_seed/state.py:746-758, :778`).
//! - The first exception aborts the whole projection, in source order: lease, liveness, `str()` of
//!   `peer_class`, `generation`, `authority_generation`, then `str()` of `incarnation`,
//!   `runtime_backend` and `transport`. A row without a non-empty string `node_id` is skipped before
//!   any of them.
mod build;
pub use build::{ProjectionInput, unknown_internet_native};

use crate::schema::Raised;
use crate::wire::float_repr;
use protocol::gossip::python_str_repr;
use serde_json::{Map, Number, Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::str::FromStr;

/// A builtin exception `_build` raises on a member row: its class and its message.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProjectionError {
    pub raised: Raised,
    pub message: String,
}

/// What the member loop of `_build` contributes, in source order.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct MemberProjection {
    pub entities: Vec<Value>,
    pub readiness: Vec<Value>,
    pub notices: Vec<Value>,
    /// `public_member_ids` (`:644`, `:695`): raw node id to its pseudonym.
    pub public_member_ids: BTreeMap<String, String>,
    /// `current_raw_members` (`:645`, `:698-699`): fresh members.
    pub current_raw_members: BTreeSet<String>,
    /// `eligible_raw_members` (`:646`, `:705-706`): fresh, activation-eligible members.
    pub eligible_raw_members: BTreeSet<String>,
}

/// `ProductProjector.__init__` refuses a shorter salt (`:270-272`).
const MIN_SALT_BYTES: usize = 32;
/// `_pseudonym` keeps `hexdigest()[:20]` (`:300-306`): ten digest bytes.
const PSEUDONYM_DIGEST_BYTES: usize = 10;
/// The `* 1_000` of `:696` and `:720` (seconds to milliseconds).
const MILLIS_PER_SECOND: f64 = 1_000.0;
/// CPython's `sys.int_info.default_max_str_digits`: `int(text)` and `str(int)` refuse more digits.
const INT_MAX_STR_DIGITS: usize = 4_300;
/// `%.200R`: `int(text)` cuts the repr in its message to 200 characters.
const INT_ERROR_REPR_CHARS: usize = 200;
/// `2**63`: an integer-valued float below it in magnitude is exactly an `i64`.
const I64_LIMIT: f64 = i64::MAX as f64;
/// `2**64`: an integer-valued float in `[0, 2**64)` is exactly a `u64`.
const U64_LIMIT: f64 = u64::MAX as f64;
/// The zero of every Unicode decimal-digit (`Nd`) run beyond ASCII, Unicode 15.1 (CPython 3.13
/// `unicodedata`); each run is ten consecutive code points `0`-`9`. `int()`/`float()` read them
/// (`_PyUnicode_TransformDecimalAndSpaceToASCII`).
const DECIMAL_ZEROS: [u32; 67] = [
    0x660, 0x6F0, 0x7C0, 0x966, 0x9E6, 0xA66, 0xAE6, 0xB66, 0xBE6, 0xC66, 0xCE6, 0xD66, 0xDE6,
    0xE50, 0xED0, 0xF20, 0x1040, 0x1090, 0x17E0, 0x1810, 0x1946, 0x19D0, 0x1A80, 0x1A90, 0x1B50,
    0x1BB0, 0x1C40, 0x1C50, 0xA620, 0xA8D0, 0xA900, 0xA9D0, 0xA9F0, 0xAA50, 0xABF0, 0xFF10,
    0x104A0, 0x10D30, 0x11066, 0x110F0, 0x11136, 0x111D0, 0x112F0, 0x11450, 0x114D0, 0x11650,
    0x116C0, 0x11730, 0x118E0, 0x11950, 0x11C50, 0x11D50, 0x11DA0, 0x11F50, 0x16A60, 0x16AC0,
    0x16B50, 0x1D7CE, 0x1D7D8, 0x1D7E2, 0x1D7EC, 0x1D7F6, 0x1E140, 0x1E2F0, 0x1E4F0, 0x1E950,
    0x1FBF0,
];
const SOURCE_ID: &str = "membership-source";

fn raise(class: &'static str, message: impl Into<String>) -> ProjectionError {
    ProjectionError {
        raised: Raised::Builtin(class),
        message: message.into(),
    }
}

/// `type(value).__name__` for the JSON shapes `float()`/`int()` refuse.
fn type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// An `int` literal (Python's `json` reads any other number as a `float`).
fn int_literal(text: &str) -> bool {
    !text.contains(['.', 'e', 'E'])
}

/// The ASCII whitespace `float()`/`int()` strip (`Py_ISSPACE`: includes `\v`, unlike
/// `u8::is_ascii_whitespace`).
fn py_space(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\n' | '\x0b' | '\x0c' | '\r')
}

fn decimal_value(c: char) -> Option<u8> {
    let code = u32::from(c);
    let run = DECIMAL_ZEROS.partition_point(|zero| *zero <= code);
    let zero = DECIMAL_ZEROS[run.checked_sub(1)?];
    u8::try_from(code - zero).ok().filter(|digit| *digit < 10)
}

/// `_PyUnicode_TransformDecimalAndSpaceToASCII`: ASCII stays, non-ASCII whitespace becomes a space,
/// a non-ASCII decimal digit its ASCII digit; anything else makes the text unreadable.
fn ascii_numeric(text: &str) -> Option<String> {
    text.chars()
        .map(|c| {
            if u32::from(c) < 127 {
                Some(c)
            } else if c.is_whitespace() {
                Some(' ')
            } else {
                decimal_value(c).map(|digit| char::from(b'0' + digit))
            }
        })
        .collect()
}

/// `_Py_string_to_number_with_underscores`: every `_` sits between two digits; then drop them.
fn without_underscores(text: &str) -> Option<String> {
    let mut out = String::with_capacity(text.len());
    let mut previous: Option<char> = None;
    for c in text.chars() {
        if c == '_' {
            if !previous.is_some_and(|p| p.is_ascii_digit()) {
                return None;
            }
        } else {
            if previous == Some('_') && !c.is_ascii_digit() {
                return None;
            }
            out.push(c);
        }
        previous = Some(c);
    }
    (previous != Some('_')).then_some(out)
}

fn repr(text: &str) -> String {
    planner::py::py_repr(&Value::String(text.to_owned()))
}

/// `float(text)`. After the transforms the ASCII grammar of `PyOS_string_to_double` (sign, digits,
/// point, exponent, `inf`/`infinity`/`nan` in any case) is the grammar of `f64::from_str`; both
/// round correctly and both read an out-of-range exponent as an infinity.
fn float_text(text: &str) -> Option<f64> {
    let ascii = without_underscores(&ascii_numeric(text)?)?;
    ascii.trim_matches(py_space).parse::<f64>().ok()
}

/// Python `float(value)`.
fn py_float(value: &Value) -> Result<f64, ProjectionError> {
    match value {
        Value::Bool(flag) => Ok(if *flag { 1.0 } else { 0.0 }),
        Value::Number(number) => {
            let text = number.to_string();
            let parsed = float_text(&text).ok_or_else(|| {
                raise(
                    "ValueError",
                    format!("could not convert string to float: {}", repr(&text)),
                )
            })?;
            if int_literal(&text) && parsed.is_infinite() {
                Err(raise("OverflowError", "int too large to convert to float"))
            } else {
                Ok(parsed)
            }
        }
        Value::String(text) => float_text(text).ok_or_else(|| {
            raise(
                "ValueError",
                format!("could not convert string to float: {}", repr(text)),
            )
        }),
        other => Err(raise(
            "TypeError",
            format!(
                "float() argument must be a string or a real number, not '{}'",
                type_name(other)
            ),
        )),
    }
}

/// `int(x)` of a float, as the integer-valued float it equals exactly (`-0.0` becomes `0.0`).
fn py_trunc(x: f64) -> Result<f64, ProjectionError> {
    if x.is_nan() {
        Err(raise("ValueError", "cannot convert float NaN to integer"))
    } else if x.is_infinite() {
        Err(raise(
            "OverflowError",
            "cannot convert float infinity to integer",
        ))
    } else {
        Ok(x.trunc() + 0.0)
    }
}

/// `int(float(member.get(key, 0)) * 1_000)` (`:696`, `:720`).
fn millis(member: &Map<String, Value>, key: &str) -> Result<f64, ProjectionError> {
    let seconds = match member.get(key) {
        None => 0.0,
        Some(value) => py_float(value)?,
    };
    py_trunc(seconds * MILLIS_PER_SECOND)
}

/// `value > now` for an integer-valued float, exactly.
fn exceeds(value: f64, now: u64) -> bool {
    if value < 0.0 {
        false
    } else if value >= U64_LIMIT {
        true
    } else {
        value as u64 > now
    }
}

/// A decimal integer as a JSON number (any size).
fn integer(digits: &str) -> Value {
    Value::Number(Number::from_str(digits).expect("a decimal integer is a JSON number"))
}

/// The exact integer an integer-valued float holds, as a JSON number.
fn int_json(value: f64) -> Value {
    if value.abs() < I64_LIMIT {
        Value::from(value as i64)
    } else {
        integer(&format!("{value:.0}"))
    }
}

/// `int(text)` (base 10).
fn int_text(text: &str) -> Result<Value, ProjectionError> {
    let invalid = || {
        let shown: String = repr(text).chars().take(INT_ERROR_REPR_CHARS).collect();
        raise(
            "ValueError",
            format!("invalid literal for int() with base 10: {shown}"),
        )
    };
    let ascii = ascii_numeric(text).ok_or_else(invalid)?;
    let body = ascii.trim_matches(py_space);
    let (negative, unsigned) = match body.strip_prefix('-') {
        Some(rest) => (true, rest),
        None => (false, body.strip_prefix('+').unwrap_or(body)),
    };
    let well_formed = !unsigned.is_empty()
        && !unsigned.starts_with('_')
        && !unsigned.ends_with('_')
        && !unsigned.contains("__")
        && unsigned.bytes().all(|b| b.is_ascii_digit() || b == b'_');
    if !well_formed {
        return Err(invalid());
    }
    let digits: String = unsigned.chars().filter(|c| *c != '_').collect();
    if digits.len() > INT_MAX_STR_DIGITS {
        return Err(raise(
            "ValueError",
            format!(
                "Exceeds the limit ({INT_MAX_STR_DIGITS} digits) for integer string conversion: \
                 value has {} digits; use sys.set_int_max_str_digits() to increase the limit",
                digits.len()
            ),
        ));
    }
    let magnitude = digits.trim_start_matches('0');
    Ok(match (magnitude.is_empty(), negative) {
        (true, _) => Value::from(0),
        (false, true) => integer(&format!("-{magnitude}")),
        (false, false) => integer(magnitude),
    })
}

/// Python `int(member.get(key, 1))` (`:739-742`).
fn generation(member: &Map<String, Value>, key: &str) -> Result<Value, ProjectionError> {
    int_field(member, key, 1)
}

/// `int(mapping.get(key, default))` of a JSON value.
fn int_field(
    member: &Map<String, Value>,
    key: &str,
    default: i64,
) -> Result<Value, ProjectionError> {
    match member.get(key) {
        None => Ok(Value::from(default)),
        Some(Value::Bool(flag)) => Ok(Value::from(u8::from(*flag))),
        Some(Value::Number(number)) => {
            let text = number.to_string();
            if int_literal(&text) {
                Ok(if text == "-0" {
                    Value::from(0)
                } else {
                    Value::Number(number.clone())
                })
            } else {
                let parsed = float_text(&text).ok_or_else(|| {
                    raise(
                        "ValueError",
                        format!("invalid literal for int() with base 10: {}", repr(&text)),
                    )
                })?;
                Ok(int_json(py_trunc(parsed)?))
            }
        }
        Some(Value::String(text)) => int_text(text),
        Some(other) => Err(raise(
            "TypeError",
            format!(
                "int() argument must be a string, a bytes-like object or a real number, not '{}'",
                type_name(other)
            ),
        )),
    }
}

/// Python `repr()` of a JSON number: an integer literal is an `int` (`-0` is `0`), any other a
/// `float`, which Python's `json` reads with `float(text)`.
fn number_repr(number: &Number) -> Result<String, ProjectionError> {
    let text = number.to_string();
    if int_literal(&text) {
        if text.trim_start_matches('-').len() > INT_MAX_STR_DIGITS {
            return Err(raise(
                "ValueError",
                format!(
                    "Exceeds the limit ({INT_MAX_STR_DIGITS} digits) for integer string conversion; \
                     use sys.set_int_max_str_digits() to increase the limit"
                ),
            ));
        }
        Ok(if text == "-0" { "0".to_owned() } else { text })
    } else {
        let x = py_float(&Value::Number(number.clone()))?;
        Ok(if x.is_finite() {
            float_repr(x)
        } else if x < 0.0 {
            "-inf".to_owned()
        } else {
            "inf".to_owned()
        })
    }
}

/// Python `repr(value)`: what `str()` of a list or dict writes for each item.
fn py_repr(value: &Value) -> Result<String, ProjectionError> {
    Ok(match value {
        Value::Null => "None".to_owned(),
        Value::Bool(flag) => if *flag { "True" } else { "False" }.to_owned(),
        Value::Number(number) => number_repr(number)?,
        Value::String(text) => python_str_repr(text),
        Value::Array(items) => {
            let items = items.iter().map(py_repr).collect::<Result<Vec<_>, _>>()?;
            format!("[{}]", items.join(", "))
        }
        Value::Object(map) => {
            let items = map
                .iter()
                .map(|(key, item)| Ok(format!("{}: {}", python_str_repr(key), py_repr(item)?)))
                .collect::<Result<Vec<_>, ProjectionError>>()?;
            format!("{{{}}}", items.join(", "))
        }
    })
}

/// Python `str(value)`.
fn py_str(value: &Value) -> Result<String, ProjectionError> {
    match value {
        Value::String(text) => Ok(text.clone()),
        other => py_repr(other),
    }
}

/// `str(mapping.get(key, fallback))`.
fn str_or(map: &Map<String, Value>, key: &str, fallback: &str) -> Result<String, ProjectionError> {
    map.get(key).map_or_else(|| Ok(fallback.to_owned()), py_str)
}

/// `_safe_code(value, fallback)` (`:37-42`): `_CODE = [a-z][a-z0-9_]{0,63}\Z` over `value.lower()`.
fn safe_code(value: Option<&Value>, fallback: &str) -> String {
    if let Some(Value::String(text)) = value {
        let lower = text.to_lowercase();
        let bytes = lower.as_bytes();
        if bytes.first().is_some_and(u8::is_ascii_lowercase)
            && bytes.len() <= 64
            && bytes
                .iter()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
        {
            return lower;
        }
    }
    fallback.to_owned()
}

/// `_binding()` (`:100-113`).
fn empty_binding() -> Value {
    json!({
        "deployment_id": null,
        "deployment_epoch": null,
        "route_id": null,
        "route_generation": null,
        "topology_version": null,
    })
}

/// `ProductProjector(pseudonym_salt=...)`.
pub struct ProductProjector {
    salt: Vec<u8>,
    publication: std::sync::Mutex<build::Publication>,
}

impl ProductProjector {
    /// `ValueError("product_pseudonym_salt_invalid")` for a salt under 32 bytes (`:270-272`).
    pub fn new(pseudonym_salt: &[u8]) -> Result<Self, ProjectionError> {
        if pseudonym_salt.len() < MIN_SALT_BYTES {
            return Err(raise("ValueError", "product_pseudonym_salt_invalid"));
        }
        Ok(Self {
            salt: pseudonym_salt.to_vec(),
            publication: std::sync::Mutex::default(),
        })
    }

    /// `_pseudonym` (`:300-306`): `"device-"` and the first 20 hex digits of HMAC-SHA256.
    fn pseudonym(&self, raw_id: &str) -> String {
        let digest = runtime::sidecar_local::hmac_sha256(&self.salt, raw_id.as_bytes());
        let hex: String = digest[..PSEUDONYM_DIGEST_BYTES]
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        format!("device-{hex}")
    }

    /// The member rows of `_build` (`:690-814`). `stages` is the route's stage list `_build` places
    /// members from (`:554-558`; empty without a current route).
    pub fn project_members(
        &self,
        members: &[Map<String, Value>],
        stages: &[Value],
        now_unix_ms: u64,
    ) -> Result<MemberProjection, ProjectionError> {
        let mut placed_by_raw_node: BTreeMap<&str, Vec<&Map<String, Value>>> = BTreeMap::new();
        for stage in stages {
            if let Value::Object(stage) = stage
                && let Some(Value::String(node_id)) = stage.get("node_id")
            {
                placed_by_raw_node
                    .entry(node_id.as_str())
                    .or_default()
                    .push(stage);
            }
        }
        let mut out = MemberProjection::default();
        for member in members {
            let raw_node_id = match member.get("node_id") {
                Some(Value::String(id)) if !id.is_empty() => id,
                _ => continue,
            };
            let public_id = self.pseudonym(raw_node_id);
            out.public_member_ids
                .insert(raw_node_id.clone(), public_id.clone());
            let lease_expires_ms = millis(member, "lease_expires_at")?;
            let fresh = exceeds(lease_expires_ms, now_unix_ms);
            if fresh {
                out.current_raw_members.insert(raw_node_id.clone());
            }
            let lifecycle = safe_code(member.get("lifecycle_state"), "unknown");
            let eligible = matches!(member.get("activation_eligible"), Some(Value::Bool(true)))
                && matches!(lifecycle.as_str(), "configured" | "running");
            if eligible && fresh {
                out.eligible_raw_members.insert(raw_node_id.clone());
            }
            let empty = Map::new();
            let runtime = match member.get("runtime_capability") {
                Some(Value::Object(runtime)) => runtime,
                _ => &empty,
            };
            let placements = placed_by_raw_node
                .get(raw_node_id.as_str())
                .map_or(&[][..], Vec::as_slice);
            let placement_id = match placements {
                [only] => match only.get("placement_id") {
                    Some(Value::String(id)) => Value::String(id.clone()),
                    _ => Value::Null,
                },
                _ => Value::Null,
            };
            let observed_ms = millis(member, "last_liveness_at")?;
            let label = match member.get("peer_class") {
                Some(Value::String(class)) if class == "android_termux_iroh" => {
                    "Mobile conformance device"
                }
                _ => "Native inference device",
            };
            let observed = int_json(observed_ms.max(0.0));
            let valid_until = int_json(observed_ms.max(lease_expires_ms).max(0.0));
            // The attributes of `:737-751`, evaluated in source order.
            let peer_class = str_or(member, "peer_class", "linux_tbd")?;
            let membership_generation = generation(member, "generation")?;
            let authority_generation = generation(member, "authority_generation")?;
            let incarnation = str_or(member, "incarnation", "unknown")?;
            let runtime_backend = str_or(runtime, "runtime_backend", "tbd")?;
            let transport = str_or(runtime, "transport", "none")?;
            let lease_freshness = if fresh { "fresh" } else { "expired" };
            out.entities.push(json!({
                "entity_id": public_id,
                "kind": "device",
                "label": label,
                "source_id": SOURCE_ID,
                "binding": empty_binding(),
                "freshness": {
                    "status": if fresh { "current" } else { "stale" },
                    "observed_at_unix_ms": observed,
                    "valid_until_unix_ms": valid_until,
                },
                "attributes": {
                    "peer_class": peer_class,
                    "membership_generation": membership_generation,
                    "authority_generation": authority_generation,
                    "incarnation": incarnation,
                    "lifecycle": lifecycle,
                    "lease_freshness": lease_freshness,
                    "runtime_backend": runtime_backend,
                    "transport": transport,
                    "activation_protocol": runtime.get("activation_protocol").cloned().unwrap_or(Value::Null),
                    "activation_eligible": eligible && fresh,
                    "placement_id": placement_id,
                },
            }));
            let stopping = lifecycle == "stopping";
            let stopped = lifecycle == "stopped";
            let reason_code = if stopped {
                json!("member_revoked")
            } else if fresh && !stopping {
                Value::Null
            } else if stopping {
                json!("membership_stopping")
            } else {
                json!("membership_lease_expired")
            };
            out.readiness.push(json!({
                "scope_id": public_id,
                "dimension": "membership",
                "state": if fresh && !stopping && !stopped { "ready" } else { "not_ready" },
                "reason_code": reason_code,
                "source_id": SOURCE_ID,
            }));
            if stopped {
                out.notices.push(json!({
                    "notice_id": format!("revoked-{public_id}"),
                    "scope_id": public_id,
                    "severity": "warning",
                    "code": "member_revoked",
                    "source_id": SOURCE_ID,
                }));
                out.entities.push(json!({
                    "entity_id": format!("incident-revoked-{public_id}"),
                    "kind": "incident",
                    "label": "Member revoked",
                    "source_id": SOURCE_ID,
                    "binding": empty_binding(),
                    "freshness": {
                        "status": "current",
                        "observed_at_unix_ms": observed,
                        "valid_until_unix_ms": observed,
                    },
                    "attributes": {
                        "state": "member_revoked",
                        "reason_code": "member_revoked",
                        "observed_at_unix_ms": observed,
                    },
                }));
            }
            if placements.is_empty() {
                out.notices.push(json!({
                    "notice_id": format!("unplaced-{public_id}"),
                    "scope_id": public_id,
                    "severity": "info",
                    "code": "member_without_placement",
                    "source_id": SOURCE_ID,
                }));
            }
        }
        Ok(out)
    }
}
