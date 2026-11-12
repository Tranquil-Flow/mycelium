// SPDX-License-Identifier: AGPL-3.0-only
//! The signed package documents of a macOS release bundle, verified exactly as the v0.1.4
//! installer does (`mycelium_a13_packaging/manifest.py`, `install_source.py`,
//! `mycelium_a13_macos/package_manifest.py`, `install_source.py`, `installer.py::verify_documents`
//! and `mycelium_qualification/signing.py` at f4e366e9): the same closed shapes, the same checks
//! in the same order, the same refusal codes, the same Ed25519 signature domain, the same
//! publisher-key pin and rollback floor. A bundle carries two documents:
//! `installation-package-manifest.json` (Ed25519-signed, binds `package_digest`) and
//! `install-source.json` (binds `artifact_digest`, the artifact name that yields the version).
//!
//! The signing half (`Signer`, `finalize`) exists for local fixtures and the owner's later release
//! tool; it takes a key FILE path and never prints the key. Tests use throw-away test keys only.
//!
//! Known, fail-closed divergences from the Python oracle (none accepts more than v0.1.4 does):
//! Python `\d`/`isdigit` accept non-ASCII digits, this port accepts ASCII digits only; a value that
//! Python hashes as an unhashable type (a list/object where a name is expected) raises a
//! `TypeError` there (public code `installer_failed`) and is reported with the same code here.
use crate::install::{Observed, Refusal, Result};
use ed25519_dalek::{Signature, Signer as _, SigningKey, Verifier as _, VerifyingKey};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{fs, io::Read, os::unix::fs::PermissionsExt, path::Path};

pub const MANIFEST_NAME: &str = "installation-package-manifest.json";
pub const SOURCE_NAME: &str = "install-source.json";
pub const MANIFEST_PROTOCOL: &str = "mycelium.installation_package_manifest.v1";
pub const SOURCE_PROTOCOL: &str = "mycelium.a13_macos_install_source.v1";
pub const PUBLISHER_ID: &str = "mycelium-release";
pub const SIGNER_ENDPOINT_ID: &str = "mycelium-macos-release";
pub const DEVELOPER_ID_UNSIGNED: &str = "developer-id-unsigned:owner_private_media";
pub const FIRST_USE_PIN: &str = "first_use_owner_private_media";
/// The exact compatibility targets the app's preflight admits (`ADMITTED_VERSIONS`).
pub const ADMITTED_VERSIONS: [&str; 2] = ["26.5.1", "15.6.1"];
const SIGNATURE_DOMAIN: &str = "mycelium.ed25519_evidence_signature.v1";
const MAX_MANIFEST_CANONICAL: usize = 65536;
const MAX_DOCUMENT_FILE: u64 = 16 * 1024 * 1024;
const MAX_JSON_DEPTH: usize = 48;
const MAX_JSON_NODES: usize = 100_000;
const BINDINGS: [&str; 12] = [
    "source_revision",
    "build_inputs",
    "platform_architecture",
    "package_digest",
    "publisher_identity",
    "minimum_os",
    "service_descriptor",
    "update_channel",
    "rollback_floor",
    "capability_protocol",
    "privacy_notice",
    "expiry_revocation_policy",
];
const FORBIDDEN_MATERIAL: [&str; 15] = [
    "invite",
    "invite_bundle",
    "invite_token",
    "swarm_secret",
    "seed_private_key",
    "private_key",
    "owner_credential",
    "credential",
    "api_key",
    "model",
    "model_bytes",
    "model_weights",
    "fixed_peer",
    "fixed_topology",
    "topology",
];
const SIGNATURE_FIELDS: [&str; 5] = [
    "algorithm",
    "signer_endpoint_id",
    "verification_key_digest",
    "signed_statement_digest",
    "signature",
];
const KEY_FIELDS: [&str; 4] = [
    "algorithm",
    "encoding",
    "verification_key",
    "verification_key_digest",
];
const SOURCE_FIELDS: [&str; 14] = [
    "protocol",
    "channel",
    "package_class",
    "artifact_name",
    "artifact_digest",
    "publisher_identity",
    "minimum_os",
    "platform_architecture",
    "macos_version",
    "service_manager",
    "install_mode",
    "rollback_floor",
    "lifecycle_support",
    "expert_tools_required",
];

fn refuse<T>(code: &'static str) -> Result<T> {
    Err(Refusal(code))
}

// ---- small oracle-compatible primitives ----------------------------------------------------------

/// Python `repr(float)`: shortest round-trip digits, exponent form below 1e-4 or from 1e16.
fn python_float(value: f64) -> String {
    if value == 0.0 {
        return if value.is_sign_negative() {
            "-0.0"
        } else {
            "0.0"
        }
        .into();
    }
    let sci = format!("{:e}", value.abs());
    let (mantissa, exponent) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    let digits: String = mantissa.chars().filter(|c| *c != '.').collect();
    let decpt = exponent + 1;
    let sign = if value < 0.0 { "-" } else { "" };
    let body = if (-3..=16).contains(&decpt) {
        if decpt <= 0 {
            format!("0.{}{}", "0".repeat((-decpt) as usize), digits)
        } else if digits.len() as i32 <= decpt {
            format!(
                "{}{}.0",
                digits,
                "0".repeat((decpt - digits.len() as i32) as usize)
            )
        } else {
            format!(
                "{}.{}",
                &digits[..decpt as usize],
                &digits[decpt as usize..]
            )
        }
    } else {
        let tail = &digits[1..];
        let sign_e = if exponent < 0 { '-' } else { '+' };
        let rest = if tail.is_empty() {
            String::new()
        } else {
            format!(".{tail}")
        };
        format!("{}{}e{}{:02}", &digits[..1], rest, sign_e, exponent.abs())
    };
    format!("{sign}{body}")
}

fn python_string(text: &str, out: &mut Vec<u8>) {
    out.push(b'"');
    for c in text.chars() {
        match c {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\r' => out.extend_from_slice(b"\\r"),
            '\t' => out.extend_from_slice(b"\\t"),
            '\u{8}' => out.extend_from_slice(b"\\b"),
            '\u{c}' => out.extend_from_slice(b"\\f"),
            c if (c as u32) < 0x20 => {
                out.extend_from_slice(format!("\\u{:04x}", c as u32).as_bytes())
            }
            c => {
                let mut buffer = [0u8; 4];
                out.extend_from_slice(c.encode_utf8(&mut buffer).as_bytes());
            }
        }
    }
    out.push(b'"');
}

fn write_canonical(value: &Value, out: &mut Vec<u8>) {
    match value {
        Value::Null => out.extend_from_slice(b"null"),
        Value::Bool(true) => out.extend_from_slice(b"true"),
        Value::Bool(false) => out.extend_from_slice(b"false"),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                out.extend_from_slice(i.to_string().as_bytes());
            } else if let Some(u) = n.as_u64() {
                out.extend_from_slice(u.to_string().as_bytes());
            } else {
                out.extend_from_slice(python_float(n.as_f64().unwrap_or(0.0)).as_bytes());
            }
        }
        Value::String(s) => python_string(s, out),
        Value::Array(items) => {
            out.push(b'[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                write_canonical(item, out);
            }
            out.push(b']');
        }
        Value::Object(map) => {
            out.push(b'{');
            // serde_json's map is ordered by key: bytewise UTF-8 order equals code-point order.
            for (index, (key, item)) in map.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                python_string(key, out);
                out.push(b':');
                write_canonical(item, out);
            }
            out.push(b'}');
        }
    }
}

/// `canonical_json_bytes`: sorted keys, no spaces, UTF-8; refuses over-deep or over-large input.
pub fn canonical_json(value: &Value) -> Option<Vec<u8>> {
    let mut stack = vec![(value, 0usize)];
    let mut nodes = 0usize;
    while let Some((current, depth)) = stack.pop() {
        if depth > MAX_JSON_DEPTH {
            return None;
        }
        nodes += 1;
        if nodes > MAX_JSON_NODES {
            return None;
        }
        match current {
            Value::Object(map) => stack.extend(map.values().map(|v| (v, depth + 1))),
            Value::Array(items) => stack.extend(items.iter().map(|v| (v, depth + 1))),
            _ => {}
        }
    }
    let mut out = Vec::new();
    write_canonical(value, &mut out);
    Some(out)
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
fn digest_of(bytes: &[u8]) -> String {
    format!("sha256:{}", sha256_hex(bytes))
}
fn is_digest(value: &Value) -> bool {
    value.as_str().is_some_and(|text| {
        text.strip_prefix("sha256:").is_some_and(|h| {
            h.len() == 64 && h.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
        })
    })
}
fn segment_text(text: &str) -> bool {
    let bytes = text.as_bytes();
    (1..=128).contains(&bytes.len())
        && bytes[0].is_ascii_alphanumeric()
        && bytes[1..]
            .iter()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
}
fn is_segment(value: &Value) -> bool {
    value.as_str().is_some_and(segment_text)
}
/// Python `str(value)` for the cases whose result can pass a name pattern.
fn python_str(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => n.as_i64().map_or_else(
            || {
                n.as_u64().map_or_else(
                    || python_float(n.as_f64().unwrap_or(0.0)),
                    |u| u.to_string(),
                )
            },
            |i| i.to_string(),
        ),
        Value::Array(_) => "[".into(),
        Value::Object(_) => "{".into(),
    }
}
fn team_id(text: &str) -> bool {
    text.len() == 10
        && text
            .bytes()
            .all(|b| b.is_ascii_uppercase() || b.is_ascii_digit())
}
fn dotted(value: &Value, code: &'static str) -> Result<Vec<u64>> {
    let Some(text) = value.as_str().filter(|t| !t.is_empty()) else {
        return refuse(code);
    };
    text.split('.')
        .map(|part| {
            if !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit()) {
                part.parse().map_err(|_| Refusal(code))
            } else {
                Err(Refusal(code))
            }
        })
        .collect()
}
fn dotted_str(text: &str, code: &'static str) -> Result<Vec<u64>> {
    dotted(&Value::String(text.into()), code)
}
/// `re.search(r"[A-Za-z][A-Za-z0-9+.\-]*://", text)`.
fn contains_url(text: &str) -> bool {
    let bytes = text.as_bytes();
    let mut from = 0;
    while let Some(found) = text[from..].find("://") {
        let at = from + found;
        let run = bytes[..at]
            .iter()
            .rev()
            .take_while(|b| b.is_ascii_alphanumeric() || matches!(b, b'+' | b'.' | b'-'))
            .collect::<Vec<_>>();
        if run.iter().any(|b| b.is_ascii_alphabetic()) {
            return true;
        }
        from = at + 3;
    }
    false
}
fn any_string(value: &Value, test: &dyn Fn(&str) -> bool) -> bool {
    match value {
        Value::String(s) => test(s),
        Value::Object(map) => map.values().any(|v| any_string(v, test)),
        Value::Array(items) => items.iter().any(|v| any_string(v, test)),
        _ => false,
    }
}
fn any_key(value: &Value, test: &dyn Fn(&str) -> bool) -> bool {
    match value {
        Value::Object(map) => map.iter().any(|(k, v)| test(k) || any_key(v, test)),
        Value::Array(items) => items.iter().any(|v| any_key(v, test)),
        _ => false,
    }
}
fn exact_keys(map: &Map<String, Value>, names: &[&str]) -> bool {
    map.len() == names.len() && names.iter().all(|n| map.contains_key(*n))
}
/// Python `x in frozenset(...)`: an unhashable value raises `TypeError` there (`installer_failed`).
fn member(value: &Value, set: &[&str]) -> Result<bool> {
    match value {
        Value::Array(_) | Value::Object(_) => refuse("installer_failed"),
        Value::String(s) => Ok(set.contains(&s.as_str())),
        _ => Ok(false),
    }
}

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
pub fn base64_encode(bytes: &[u8]) -> String {
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let n = chunk
            .iter()
            .enumerate()
            .fold(0u32, |a, (i, b)| a | (u32::from(*b) << (16 - 8 * i)));
        for i in 0..4 {
            if i <= chunk.len() {
                out.push(B64[((n >> (18 - 6 * i)) & 63) as usize] as char);
            } else {
                out.push('=');
            }
        }
    }
    out
}
/// Python `base64.b64decode(value, validate=True)`: standard alphabet, padding required.
fn base64_decode(text: &str) -> Option<Vec<u8>> {
    let bytes = text.as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return None;
    }
    let pad = bytes.iter().rev().take_while(|b| **b == b'=').count();
    if pad > 2 {
        return None;
    }
    let body = &bytes[..bytes.len() - pad];
    let mut out = Vec::new();
    let (mut acc, mut bits) = (0u32, 0u32);
    for b in body {
        let v = B64.iter().position(|c| c == b)? as u32;
        acc = (acc << 6) | v;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((acc >> bits) as u8);
            acc &= (1 << bits) - 1;
        }
    }
    Some(out)
}

// ---- key records and signatures ------------------------------------------------------------------

/// `build_ed25519_verifier([record])`: closed record, base64 key of 32 bytes, matching digest. A key
/// the curve library cannot decode is kept as `None` and fails at verification (as OpenSSL would).
struct Keyring {
    digest: String,
    key: Option<VerifyingKey>,
}
fn load_key(record: &Value) -> Option<Keyring> {
    let record = record.as_object()?;
    if !exact_keys(record, &KEY_FIELDS)
        || record["algorithm"] != "ed25519"
        || record["encoding"] != "base64"
    {
        return None;
    }
    let bytes = base64_decode(record["verification_key"].as_str()?)?;
    let array: [u8; 32] = bytes.as_slice().try_into().ok()?;
    let digest = digest_of(&array);
    if record["verification_key_digest"] != digest.as_str() {
        return None;
    }
    Some(Keyring {
        digest,
        key: VerifyingKey::from_bytes(&array).ok(),
    })
}
fn signature_payload(endpoint: &str, key_digest: &str, statement_digest: &str) -> Vec<u8> {
    canonical_json(&json!({
        "algorithm": "ed25519",
        "protocol": SIGNATURE_DOMAIN,
        "signed_statement_digest": statement_digest,
        "signer_endpoint_id": endpoint,
        "verification_key_digest": key_digest,
    }))
    .unwrap_or_default()
}
fn verify_statement(ring: &Keyring, statement: &[u8], signature: &Map<String, Value>) -> bool {
    if !exact_keys(signature, &SIGNATURE_FIELDS) || signature["algorithm"] != "ed25519" {
        return false;
    }
    let (Some(endpoint), Some(key_digest), Some(claimed), Some(encoded)) = (
        signature["signer_endpoint_id"]
            .as_str()
            .filter(|s| !s.is_empty()),
        signature["verification_key_digest"].as_str(),
        signature["signed_statement_digest"].as_str(),
        signature["signature"].as_str(),
    ) else {
        return false;
    };
    if key_digest != ring.digest || claimed != digest_of(statement) {
        return false;
    }
    let Some(raw) = base64_decode(encoded).and_then(|b| <[u8; 64]>::try_from(b).ok()) else {
        return false;
    };
    let Some(key) = &ring.key else {
        return false;
    };
    key.verify(
        &signature_payload(endpoint, key_digest, claimed),
        &Signature::from_bytes(&raw),
    )
    .is_ok()
}

// ---- the signed manifest -----------------------------------------------------------------------------

fn validate_bindings(document: &Map<String, Value>) -> Result<()> {
    let revision = &document["source_revision"];
    if !revision
        .as_str()
        .is_some_and(|r| r.len() == 40 && r.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f')))
    {
        return refuse("package_source_revision_invalid");
    }
    let Some(build) = document["build_inputs"]
        .as_object()
        .filter(|m| exact_keys(m, &["builder_id", "build_digest"]))
    else {
        return refuse("package_build_inputs_invalid");
    };
    if !is_segment(&build["builder_id"]) || !is_digest(&build["build_digest"]) {
        return refuse("package_build_inputs_invalid");
    }
    let Some(platform) = document["platform_architecture"]
        .as_object()
        .filter(|m| exact_keys(m, &["system", "machine", "macos_version"]))
    else {
        return refuse("package_platform_invalid");
    };
    let version_ok = platform["macos_version"].as_str().is_some_and(|v| {
        let parts: Vec<_> = v.split('.').collect();
        (2..=3).contains(&parts.len())
            && parts
                .iter()
                .all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()))
    });
    if platform["system"] != "macos" || platform["machine"] != "arm64" || !version_ok {
        return refuse("package_platform_invalid");
    }
    if !is_digest(&document["package_digest"]) {
        return refuse("package_digest_invalid");
    }
    let Some(publisher) = document["publisher_identity"].as_object().filter(|m| {
        exact_keys(
            m,
            &[
                "publisher_id",
                "endpoint_id",
                "verification_key_record",
                "developer_id_team",
            ],
        )
    }) else {
        return refuse("package_publisher_invalid");
    };
    if !is_segment(&publisher["publisher_id"]) || !is_segment(&publisher["endpoint_id"]) {
        return refuse("package_publisher_invalid");
    }
    let team = &publisher["developer_id_team"];
    if team != DEVELOPER_ID_UNSIGNED && !team.as_str().is_some_and(team_id) {
        return refuse("package_publisher_invalid");
    }
    if load_key(&publisher["verification_key_record"]).is_none() {
        return refuse("package_publisher_invalid");
    }
    if !is_segment(&document["minimum_os"]) {
        return refuse("package_minimum_os_invalid");
    }
    if !document["service_descriptor"].as_object().is_some_and(|d| {
        exact_keys(
            d,
            &[
                "launchd_label",
                "sha256",
                "install_mode",
                "lifecycle_support",
            ],
        ) && is_segment(&d["launchd_label"])
            && is_digest(&d["sha256"])
            && d["install_mode"] == "launchd_user"
            && d["lifecycle_support"] == true
    }) {
        return refuse("package_lifecycle_support_missing");
    }
    if !is_segment(&document["update_channel"]) {
        return refuse("package_update_channel_invalid");
    }
    dotted(
        &document["rollback_floor"],
        "package_rollback_floor_invalid",
    )?;
    if !document["capability_protocol"]
        .as_str()
        .is_some_and(|c| segment_text(c) && c.starts_with("mycelium."))
    {
        return refuse("package_capability_protocol_invalid");
    }
    if !is_segment(&document["privacy_notice"]) {
        return refuse("package_privacy_notice_invalid");
    }
    if !is_segment(&document["expiry_revocation_policy"]) {
        return refuse("package_expiry_policy_invalid");
    }
    Ok(())
}
fn number(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64(),
        _ => None,
    }
}
/// `validate_manifest`: closed shape, size, protocol, embedded-material scan, bindings, times, signature shape.
fn validate_manifest(document: &Value) -> Result<&Map<String, Value>> {
    let map = document
        .as_object()
        .filter(|m| {
            m.len() == BINDINGS.len() + 4
                && BINDINGS
                    .iter()
                    .chain(&["protocol", "issued_at", "expires_at", "signature"])
                    .all(|name| m.contains_key(*name))
        })
        .ok_or(Refusal("package_manifest_shape_invalid"))?;
    let canonical = canonical_json(document).ok_or(Refusal("package_manifest_shape_invalid"))?;
    if canonical.len() > MAX_MANIFEST_CANONICAL {
        return refuse("package_manifest_too_large");
    }
    if map["protocol"] != MANIFEST_PROTOCOL {
        return refuse("package_manifest_protocol_invalid");
    }
    if any_key(document, &|k| {
        FORBIDDEN_MATERIAL.contains(&k.to_lowercase().as_str())
    }) {
        return refuse("package_embedded_secret");
    }
    if any_string(document, &contains_url) {
        return refuse("package_hidden_download_url");
    }
    validate_bindings(map)?;
    match (number(&map["issued_at"]), number(&map["expires_at"])) {
        (Some(issued), Some(expires)) if expires > issued => {}
        _ => return refuse("package_manifest_shape_invalid"),
    }
    if !map["signature"]
        .as_object()
        .is_some_and(|s| exact_keys(s, &SIGNATURE_FIELDS))
    {
        return refuse("package_signature_invalid");
    }
    Ok(map)
}

pub struct PublisherPin<'a> {
    pub key_digest: &'a str,
    pub rollback_floor: &'a str,
}
struct Context<'a> {
    pin: &'a str,
    floor_pin: &'a str,
    observed: &'a Observed,
    now: f64,
}
fn admitted(observed: &Observed) -> bool {
    ADMITTED_VERSIONS.contains(&observed.macos_version.as_str())
}
/// `verify_macos_package`: signature, expiry, publisher, pinned key, platform, rollback floor.
fn verify_manifest<'a>(document: &'a Value, ctx: &Context) -> Result<&'a Map<String, Value>> {
    let map = validate_manifest(document)?;
    let publisher = map["publisher_identity"]
        .as_object()
        .ok_or(Refusal("package_publisher_invalid"))?;
    let ring = load_key(&publisher["verification_key_record"])
        .ok_or(Refusal("package_publisher_invalid"))?;
    let mut statement = map.clone();
    statement.remove("signature");
    let statement_bytes =
        canonical_json(&Value::Object(statement)).ok_or(Refusal("package_signature_invalid"))?;
    if !map["signature"]
        .as_object()
        .is_some_and(|s| verify_statement(&ring, &statement_bytes, s))
    {
        return refuse("package_signature_invalid");
    }
    if ctx.now > number(&map["expires_at"]).unwrap_or(0.0) {
        return refuse("package_expired");
    }
    if publisher["publisher_id"] != PUBLISHER_ID {
        return refuse("package_publisher_mismatch");
    }
    if ring.digest != ctx.pin {
        return refuse("package_publisher_mismatch");
    }
    // The installer passes the manifest's own team as the expected team: the comparison cannot fail.
    if map["platform_architecture"]["machine"] != "arm64" || !admitted(ctx.observed) {
        return refuse("package_platform_unsupported");
    }
    let package_floor = dotted(&map["rollback_floor"], "package_rollback_floor_invalid")?;
    let pin = dotted_str(ctx.floor_pin, "package_rollback_floor_invalid")?;
    // `installed_version` is the manifest's own floor: only the pin can reject.
    if package_floor < pin {
        return refuse("package_downgrade_rejected");
    }
    Ok(map)
}

// ---- the install source ------------------------------------------------------------------------------

fn validate_source(source: &Value) -> Result<&Map<String, Value>> {
    let map = source
        .as_object()
        .filter(|m| exact_keys(m, &SOURCE_FIELDS))
        .ok_or(Refusal("install_source_shape_invalid"))?;
    if map["protocol"] != SOURCE_PROTOCOL {
        return refuse("install_source_protocol_invalid");
    }
    if any_string(source, &contains_url) {
        return refuse("install_source_hidden_url");
    }
    if !member(
        &map["channel"],
        &["owner_private_media", "signed_update_channel"],
    )? {
        return refuse("install_source_channel_invalid");
    }
    if map["package_class"] != "signed_macos_package" {
        return refuse("install_source_package_class_invalid");
    }
    if !map["artifact_name"]
        .as_str()
        .is_some_and(|n| segment_text(n) && !n.contains('/'))
    {
        return refuse("install_source_artifact_invalid");
    }
    if !is_digest(&map["artifact_digest"]) {
        return refuse("install_source_digest_invalid");
    }
    let Some(publisher) = map["publisher_identity"].as_object().filter(|m| {
        exact_keys(
            m,
            &[
                "publisher_id",
                "endpoint_id",
                "verification_key_record",
                "developer_id_team",
            ],
        )
    }) else {
        return refuse("install_source_publisher_invalid");
    };
    if !segment_text(&python_str(&publisher["publisher_id"]))
        || !segment_text(&python_str(&publisher["endpoint_id"]))
    {
        return refuse("install_source_publisher_invalid");
    }
    let team = &publisher["developer_id_team"];
    let team_ok = if map["channel"] == "owner_private_media" {
        team == DEVELOPER_ID_UNSIGNED
    } else {
        team.as_str().is_some_and(team_id)
    };
    if !team_ok {
        return refuse("install_source_publisher_invalid");
    }
    let record_ok = publisher["verification_key_record"]
        .as_object()
        .is_some_and(|r| {
            r.get("algorithm").is_some_and(|a| a == "ed25519")
                && is_digest(&Value::String(python_str(
                    r.get("verification_key_digest").unwrap_or(&Value::Null),
                )))
        });
    if !record_ok {
        return refuse("install_source_publisher_invalid");
    }
    if !segment_text(&python_str(&map["minimum_os"])) {
        return refuse("install_source_minimum_os_invalid");
    }
    if !member(&map["platform_architecture"], &["arm64"])? {
        return refuse("install_source_platform_unsupported");
    }
    if map["service_manager"] != "launchd" {
        return refuse("install_source_lifecycle_support_missing");
    }
    if !member(&map["install_mode"], &["launchd_user"])? {
        return refuse("install_source_install_mode_invalid");
    }
    dotted(
        &map["rollback_floor"],
        "install_source_rollback_floor_invalid",
    )?;
    if map["lifecycle_support"] != true {
        return refuse("install_source_lifecycle_support_missing");
    }
    if !map["expert_tools_required"]
        .as_array()
        .is_some_and(Vec::is_empty)
    {
        return refuse("install_source_expert_tools_required");
    }
    Ok(map)
}
fn verify_source<'a>(
    source: &'a Value,
    team: &Value,
    manifest_floor: &str,
    ctx: &Context,
) -> Result<&'a Map<String, Value>> {
    let map = validate_source(source)?;
    let publisher = &map["publisher_identity"];
    if publisher["publisher_id"] != PUBLISHER_ID {
        return refuse("install_source_publisher_mismatch");
    }
    if &publisher["developer_id_team"] != team {
        return refuse("install_source_publisher_mismatch");
    }
    if map["platform_architecture"] != "arm64" || !admitted(ctx.observed) {
        return refuse("install_source_platform_unsupported");
    }
    let package_floor = dotted(
        &map["rollback_floor"],
        "install_source_rollback_floor_invalid",
    )?;
    let pin = dotted_str(ctx.floor_pin, "install_source_rollback_floor_invalid")?;
    let installed = dotted_str(manifest_floor, "install_source_rollback_floor_invalid")?;
    if package_floor < pin || installed < package_floor {
        return refuse("install_source_downgrade_rejected");
    }
    Ok(map)
}

// ---- verify_documents --------------------------------------------------------------------------------

#[derive(Debug)]
pub struct Verified {
    pub version: String,
    /// `package_digest` and `artifact_digest`: the payload digest must equal both.
    pub expected_digests: [String; 2],
    pub rollback_floor: String,
    pub publisher_key_digest: String,
    pub publisher_pin_source: String,
    pub source_revision: String,
    pub developer_id_team: String,
}
fn read_document(bundle: &Path, name: &str) -> Result<Value> {
    let path = bundle.join(name);
    let meta = fs::symlink_metadata(&path).map_err(|_| Refusal("bundle_incomplete"))?;
    if meta.file_type().is_symlink() || !meta.file_type().is_file() {
        return refuse("bundle_incomplete");
    }
    let mut bytes = Vec::new();
    fs::File::open(&path)
        .and_then(|f| f.take(MAX_DOCUMENT_FILE + 1).read_to_end(&mut bytes))
        .map_err(|_| Refusal("bundle_incomplete"))?;
    if bytes.len() as u64 > MAX_DOCUMENT_FILE {
        return refuse("bundle_incomplete");
    }
    match serde_json::from_slice::<Value>(&bytes) {
        Ok(value @ Value::Object(_)) => Ok(value),
        _ => refuse("bundle_incomplete"),
    }
}
/// `artifact_name` -> version: `^mycelium-(\d+\.\d+\.\d+(?:-[a-z0-9.]+)?)-macos-arm64$`.
fn artifact_version(name: &str) -> Option<&str> {
    let version = name
        .strip_prefix("mycelium-")?
        .strip_suffix("-macos-arm64")?;
    let (head, suffix) = version
        .split_once('-')
        .map_or((version, None), |(h, s)| (h, Some(s)));
    let parts: Vec<_> = head.split('.').collect();
    let numeric = parts.len() == 3
        && parts
            .iter()
            .all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_digit()));
    let suffix_ok = suffix.is_none_or(|s| {
        !s.is_empty()
            && s.bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'.')
    });
    (numeric && suffix_ok).then_some(version)
}
/// The platform checks of the v0.1.4 macOS installer, no writes. `installed_pin` is the installed
/// record's publisher key digest, pin source and rollback floor, when there is one.
pub fn verify_documents(
    bundle: &Path,
    observed: &Observed,
    installed: Option<(&str, &str, &str)>,
    now: f64,
) -> Result<Verified> {
    let manifest = read_document(bundle, MANIFEST_NAME)?;
    let source = read_document(bundle, SOURCE_NAME)?;
    if observed.system != "Darwin" || observed.machine != "arm64" {
        return refuse("platform_unsupported");
    }
    let record = manifest
        .get("publisher_identity")
        .and_then(|p| p.get("verification_key_record"))
        .filter(|r| r.is_object());
    let offered = record.and_then(|r| r.get("verification_key_digest"));
    let (pin, pin_source, floor_pin) = match installed {
        Some((key, source, floor)) => (key.to_string(), source.to_string(), floor.to_string()),
        None => match offered.and_then(Value::as_str) {
            Some(key) => (
                key.to_string(),
                FIRST_USE_PIN.to_string(),
                "0.0.0".to_string(),
            ),
            None => return refuse("package_publisher_invalid"),
        },
    };
    let ctx = Context {
        pin: &pin,
        floor_pin: &floor_pin,
        observed,
        now,
    };
    let checked = verify_manifest(&manifest, &ctx)?;
    let team = checked["publisher_identity"]["developer_id_team"].clone();
    let manifest_floor = checked["rollback_floor"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    let checked_source = verify_source(&source, &team, &manifest_floor, &ctx)?;
    let artifact_name = checked_source["artifact_name"].as_str().unwrap_or_default();
    let version = artifact_version(artifact_name).ok_or(Refusal("artifact_name_invalid"))?;
    Ok(Verified {
        version: version.to_string(),
        expected_digests: [
            checked["package_digest"]
                .as_str()
                .unwrap_or_default()
                .to_string(),
            checked_source["artifact_digest"]
                .as_str()
                .unwrap_or_default()
                .to_string(),
        ],
        rollback_floor: manifest_floor,
        publisher_key_digest: pin,
        publisher_pin_source: pin_source,
        source_revision: checked["source_revision"]
            .as_str()
            .unwrap_or_default()
            .to_string(),
        developer_id_team: team.as_str().unwrap_or_default().to_string(),
    })
}

// ---- signing and finalizing (local fixtures; the production key is never used here) ------------------

pub struct Signer {
    key: SigningKey,
    endpoint_id: String,
}
impl Signer {
    pub fn from_seed(seed: [u8; 32], endpoint_id: &str) -> Self {
        Self {
            key: SigningKey::from_bytes(&seed),
            endpoint_id: endpoint_id.into(),
        }
    }
    /// 32 raw private-key bytes, exactly as `load_pinned_signer`; the bytes are never echoed.
    pub fn from_key_file(path: &Path, endpoint_id: &str) -> Result<Self> {
        let bytes = fs::read(path).map_err(|_| Refusal("signing_key_file_unreadable"))?;
        let seed: [u8; 32] = bytes
            .as_slice()
            .try_into()
            .map_err(|_| Refusal("signing_key_file_must_hold_32_raw_private_bytes"))?;
        Ok(Self::from_seed(seed, endpoint_id))
    }
    pub fn verification_key_digest(&self) -> String {
        digest_of(self.key.verifying_key().as_bytes())
    }
    pub fn public_key_record(&self) -> Value {
        json!({
            "algorithm": "ed25519", "encoding": "base64",
            "verification_key": base64_encode(self.key.verifying_key().as_bytes()),
            "verification_key_digest": self.verification_key_digest(),
        })
    }
    pub fn sign(&self, statement: &Value) -> Result<Value> {
        let bytes = canonical_json(statement).ok_or(Refusal("package_manifest_shape_invalid"))?;
        let statement_digest = digest_of(&bytes);
        let key_digest = self.verification_key_digest();
        let payload = signature_payload(&self.endpoint_id, &key_digest, &statement_digest);
        let signature = self.key.sign(&payload);
        Ok(json!({
            "algorithm": "ed25519", "signer_endpoint_id": self.endpoint_id,
            "verification_key_digest": key_digest, "signed_statement_digest": statement_digest,
            "signature": base64_encode(&signature.to_bytes()),
        }))
    }
}

/// v0.2's own launchd user-agent label (ruling 25; v0.1.4 declares `org.mycelium.managed-host`,
/// `finalize_a13_macos.py:45`): declared in the signed manifest, never installed by this tranche.
pub const LAUNCHD_LABEL: &str = "org.mycelium.app.managed-host";
pub struct FinalizeSpec<'a> {
    pub version: &'a str,
    pub rollback_floor: &'a str,
    pub source_revision: &'a str,
    pub issued_at: f64,
    pub minimum_os: &'a str,
}
/// The signed manifest and install source of `payload_digest`, as `finalize_macos_bundle` builds them.
/// The launchd descriptor is declared, not installed: its digest is that of this declaration text.
pub fn build_documents(
    signer: &Signer,
    spec: &FinalizeSpec,
    payload_digest: &str,
) -> Result<(Value, Value)> {
    let publisher = json!({
        "publisher_id": PUBLISHER_ID, "developer_id_team": DEVELOPER_ID_UNSIGNED,
        "endpoint_id": signer.endpoint_id, "verification_key_record": signer.public_key_record(),
    });
    let minimum = format!("macos-{}", spec.minimum_os);
    let source = json!({
        "protocol": SOURCE_PROTOCOL, "channel": "owner_private_media", "package_class": "signed_macos_package",
        "artifact_name": format!("mycelium-{}-macos-arm64", spec.version), "artifact_digest": payload_digest,
        "publisher_identity": publisher, "minimum_os": minimum, "platform_architecture": "arm64",
        "macos_version": spec.minimum_os, "service_manager": "launchd", "install_mode": "launchd_user",
        "rollback_floor": spec.rollback_floor, "lifecycle_support": true, "expert_tools_required": [],
    });
    let declaration = "launchd user agent org.mycelium.app.managed-host (declared, not installed by this tranche)";
    let statement = json!({
        "protocol": MANIFEST_PROTOCOL,
        "source_revision": spec.source_revision,
        "build_inputs": {"builder_id": "mycelium-ci", "build_digest": payload_digest},
        "platform_architecture": {"system": "macos", "machine": "arm64", "macos_version": spec.minimum_os},
        "package_digest": payload_digest, "publisher_identity": publisher, "minimum_os": minimum,
        "service_descriptor": {
            "launchd_label": LAUNCHD_LABEL, "sha256": digest_of(declaration.as_bytes()),
            "install_mode": "launchd_user", "lifecycle_support": true,
        },
        "update_channel": "owner_private_media", "rollback_floor": spec.rollback_floor,
        "capability_protocol": "mycelium.capability_report.v1", "privacy_notice": "mycelium-a13-privacy-v1",
        "expiry_revocation_policy": "expire_after_90d_or_revocation_list",
        "issued_at": spec.issued_at, "expires_at": spec.issued_at + 90.0 * 86400.0,
    });
    let mut manifest = statement.as_object().cloned().unwrap_or_default();
    manifest.insert("signature".into(), signer.sign(&statement)?);
    let manifest = Value::Object(manifest);
    // Never write what the verifier would refuse.
    validate_manifest(&manifest)?;
    validate_source(&source)?;
    Ok((manifest, source))
}

pub const INSTALL_COMMAND: &str = include_str!("../package-assets/install-mycelium.command");
pub const README: &str = include_str!("../package-assets/README-PILOT.txt");
/// The Gatekeeper/install walkthrough `README-PILOT.txt` points at. It was referenced but never
/// shipped; it is written into the bundle by [`finalize`] like the README, so the reference resolves
/// in the folder the reader actually has. Static text: no behaviour, no signature over it.
pub const QUICKSTART: &str = include_str!("../package-assets/QUICKSTART-MACOS.md");
pub const QUICKSTART_NAME: &str = "QUICKSTART-MACOS.md";
pub const COMMAND_NAME: &str = "Install Mycelium.command";

/// Write the finalize outputs next to `<bundle>/Mycelium.app`: signed manifest, install
/// source, `APP-FILES.sha256`, the install script, README, artifact identity and `SHA256SUMS`.
pub fn finalize(bundle: &Path, signer: &Signer, spec: &FinalizeSpec) -> Result<String> {
    let app = bundle.join(crate::install::PAYLOAD_DIRECTORY);
    let digest = crate::install::bundle_digest(&app)?;
    let (manifest, source) = build_documents(signer, spec, &digest)?;
    let write = |name: &str, bytes: &[u8]| {
        fs::write(bundle.join(name), bytes).map_err(|_| Refusal("install_io_error"))
    };
    let pretty = |value: &Value| {
        serde_json::to_vec_pretty(value)
            .map(|mut b| {
                b.push(b'\n');
                b
            })
            .map_err(|_| Refusal("installer_failed"))
    };
    write(SOURCE_NAME, &pretty(&source)?)?;
    write(MANIFEST_NAME, &pretty(&manifest)?)?;
    write(COMMAND_NAME, INSTALL_COMMAND.as_bytes())?;
    fs::set_permissions(bundle.join(COMMAND_NAME), fs::Permissions::from_mode(0o755))
        .map_err(|_| Refusal("install_io_error"))?;
    write("README-PILOT.txt", README.as_bytes())?;
    write(QUICKSTART_NAME, QUICKSTART.as_bytes())?;
    let listing =
        crate::bundle::app_files_listing(&app).map_err(|_| Refusal("payload_special_file"))?;
    write("APP-FILES.sha256", listing.as_bytes())?;
    let identity = json!({
        "platform": "macos-arm64", "channel": "owner_private_media",
        "signing": "Ed25519-signed manifest; Apple: ad-hoc only, no Developer ID, NOT notarized",
        "developer_id_team": DEVELOPER_ID_UNSIGNED, "version": spec.version,
        "artifact_name": format!("mycelium-{}-macos-arm64", spec.version), "bundle_digest": digest,
        "install_mode": "launchd_user", "source_revision": spec.source_revision,
        "manifest_publisher_key_digest": signer.verification_key_digest(),
    });
    write("artifact-identity.json", &pretty(&identity)?)?;
    let mut names: Vec<String> = fs::read_dir(bundle)
        .map_err(|_| Refusal("install_io_error"))?
        .filter_map(std::result::Result::ok)
        .filter(|e| e.file_type().is_ok_and(|t| t.is_file()))
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n != "SHA256SUMS")
        .collect();
    names.sort();
    let mut sums = String::new();
    for name in names {
        let bytes = fs::read(bundle.join(&name)).map_err(|_| Refusal("install_io_error"))?;
        sums += &format!("{}  {name}\n", sha256_hex(&bytes));
    }
    write("SHA256SUMS", sums.as_bytes())?;
    Ok(digest)
}

/// Public so tests can assert exact oracle values.
pub fn canonical_float(value: f64) -> String {
    python_float(value)
}
