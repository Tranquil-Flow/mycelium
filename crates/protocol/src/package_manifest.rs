// SPDX-License-Identifier: AGPL-3.0-only
//! The closed `mycelium.installation_package_manifest.v1` of v0.1.4 (`mycelium_a13_packaging/manifest.py`
//! shared core with the Linux, Windows and macOS profiles and their `verify_*_package` entry points): the
//! envelope, the twelve frozen bindings, the embedded-material scan (no invite, seed key, owner credential,
//! model, fixed peer, fixed topology or hidden download URL), signing, and the signature / expiry / publisher /
//! key-pin / rollback-floor checks. Verification returns member-only facts; it never returns `route_ready`,
//! grants eligibility or claims a package was installed.
//!
//! One documented divergence: Python's `str.isdigit()` / `\d` accept non-ASCII decimal digits (and `int()`
//! parses the decimal ones). No Unicode digit tables are admitted, so this port requires ASCII digits where the
//! oracle checks digits (dotted versions, the Windows build, the macOS version) and refuses the rest.
use crate::identity::{self, Identity};
use crate::{canonical, checks};
use serde_json::{Map, Value, json};

pub const PROTOCOL: &str = "mycelium.installation_package_manifest.v1";
/// Exactly the frozen A13 acceptance inventory's `manifest_bindings`.
pub const BINDINGS: [&str; 12] = [
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
const ENVELOPE_FIELDS: [&str; 4] = ["protocol", "issued_at", "expires_at", "signature"];
const BUILD_INPUT_FIELDS: [&str; 2] = ["builder_id", "build_digest"];
const BASE_PUBLISHER_FIELDS: [&str; 3] = ["publisher_id", "endpoint_id", "verification_key_record"];
const SIGNATURE_FIELDS: [&str; 5] = [
    "algorithm",
    "signer_endpoint_id",
    "verification_key_digest",
    "signed_statement_digest",
    "signature",
];
/// Material a package may never carry (A13 section 2).
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
const MAX_BYTES: usize = 65536;
const MAX_TEXT_CHARS: usize = 256;
const SOURCE_REVISION_HEX: usize = 40;
const TEAM_ID_CHARS: usize = 10;

pub const AUTHENTICODE_UNSIGNED_SUBJECT: &str = "authenticode-unsigned:owner_private_media";
pub const DEVELOPER_ID_UNSIGNED: &str = "developer-id-unsigned:owner_private_media";
const MACOS_ADMITTED_VERSIONS: [&str; 2] = ["26.5.1", "15.6.1"];
const MACOS_ADMITTED_ARCHITECTURES: [&str; 1] = ["arm64"];
const LINUX_ADMITTED_ARCHITECTURES: [&str; 2] = ["x86_64", "aarch64"];
const LINUX_UBUNTU_MINIMUM: &str = "24.04";
const WINDOWS_ADMITTED_ARCHITECTURES: [&str; 1] = ["x86_64"];
const WINDOWS_MINIMUM_VERSION: &str = "10.0";
const WINDOWS_MINIMUM_BUILD: &str = "19045";

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct PackageManifestError(pub &'static str);

type E = PackageManifestError;

fn fail<T>(code: &'static str) -> Result<T, E> {
    Err(PackageManifestError(code))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Platform {
    Linux,
    Windows,
    MacOs,
}

impl Platform {
    fn platform_fields(self) -> &'static [&'static str] {
        match self {
            Self::Linux => &[
                "system",
                "machine",
                "distribution_id",
                "distribution_version",
            ],
            Self::Windows => &["system", "machine", "windows_version", "windows_build"],
            Self::MacOs => &["system", "machine", "macos_version"],
        }
    }
    fn extra_publisher_field(self) -> Option<&'static str> {
        match self {
            Self::Linux => None,
            Self::Windows => Some("authenticode_subject"),
            Self::MacOs => Some("developer_id_team"),
        }
    }
    fn descriptor_name_field(self) -> &'static str {
        match self {
            Self::Linux => "unit_name",
            Self::Windows => "service_name",
            Self::MacOs => "launchd_label",
        }
    }
    fn install_modes(self) -> &'static [&'static str] {
        match self {
            Self::Linux => &["systemd_user", "systemd_system"],
            Self::Windows => &["windows_service", "windows_service_per_user"],
            Self::MacOs => &["launchd_user"],
        }
    }
}

fn is_digest(v: &Value) -> bool {
    checks::digest(v)
}

fn is_segment(v: &Value) -> bool {
    checks::segment(v)
}

/// `is_text`: bounded, non-empty, not padded, no NUL.
fn is_text(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.trim_matches(|c: char| c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)) == s
            && s.chars().count() <= MAX_TEXT_CHARS
            && !s.contains('\0')
    })
}

/// A dotted version as digit strings with leading zeros removed (so equal numbers compare equal at any size).
type Version = Vec<String>;

fn dotted_version(v: &Value, code: &'static str) -> Result<Version, E> {
    let Some(s) = v.as_str().filter(|s| !s.is_empty()) else {
        return fail(code);
    };
    let mut parts = Vec::new();
    for part in s.split('.') {
        if part.is_empty() || !part.bytes().all(|b| b.is_ascii_digit()) {
            return fail(code);
        }
        let trimmed = part.trim_start_matches('0');
        parts.push(if trimmed.is_empty() { "0" } else { trimmed }.to_owned());
    }
    Ok(parts)
}

fn number_cmp(a: &str, b: &str) -> std::cmp::Ordering {
    a.len().cmp(&b.len()).then_with(|| a.cmp(b))
}

/// Python tuple comparison over integer components.
fn version_cmp(a: &Version, b: &Version) -> std::cmp::Ordering {
    for (x, y) in a.iter().zip(b) {
        let ord = number_cmp(x, y);
        if ord != std::cmp::Ordering::Equal {
            return ord;
        }
    }
    a.len().cmp(&b.len())
}

const ROLLBACK_CODE: &str = "package_rollback_floor_invalid";

fn walk_keys<'a>(value: &'a Value, out: &mut Vec<&'a str>) {
    match value {
        Value::Object(m) => {
            for (k, item) in m {
                out.push(k);
                walk_keys(item, out);
            }
        }
        Value::Array(a) => a.iter().for_each(|item| walk_keys(item, out)),
        _ => {}
    }
}

fn walk_strings<'a>(value: &'a Value, out: &mut Vec<&'a str>) {
    match value {
        Value::String(s) => out.push(s),
        Value::Object(m) => m.values().for_each(|item| walk_strings(item, out)),
        Value::Array(a) => a.iter().for_each(|item| walk_strings(item, out)),
        _ => {}
    }
}

/// `_URL.search(text)`: `[A-Za-z][A-Za-z0-9+.-]*://` anywhere, i.e. a `://` whose maximal run of scheme
/// characters directly before it contains a letter.
fn has_url_scheme(text: &str) -> bool {
    let bytes = text.as_bytes();
    let mut from = 0;
    while let Some(found) = text[from..].find("://") {
        let at = from + found;
        let run_start = (0..at)
            .rev()
            .take_while(|&i| bytes[i].is_ascii_alphanumeric() || b"+.-".contains(&bytes[i]))
            .last()
            .unwrap_or(at);
        if bytes[run_start..at].iter().any(u8::is_ascii_alphabetic) {
            return true;
        }
        from = at + 1;
    }
    false
}

fn check_no_embedded_material(document: &Value) -> Result<(), E> {
    let mut keys = Vec::new();
    walk_keys(document, &mut keys);
    if keys
        .iter()
        .any(|k| FORBIDDEN_MATERIAL.contains(&k.to_lowercase().as_str()))
    {
        return fail("package_embedded_secret");
    }
    let mut strings = Vec::new();
    walk_strings(document, &mut strings);
    if strings.iter().any(|s| has_url_scheme(s)) {
        return fail("package_hidden_download_url");
    }
    Ok(())
}

fn exact(m: &Map<String, Value>, fields: &[&str]) -> bool {
    m.len() == fields.len() && fields.iter().all(|f| m.contains_key(*f))
}

fn validate_platform(platform: Platform, p: &Map<String, Value>) -> Result<(), E> {
    const CODE: &str = "package_platform_invalid";
    match platform {
        Platform::Linux => {
            if ![
                "system",
                "machine",
                "distribution_id",
                "distribution_version",
            ]
            .iter()
            .all(|f| is_segment(&p[*f]))
            {
                return fail(CODE);
            }
        }
        Platform::Windows => {
            let build_ok = p["windows_build"]
                .as_str()
                .is_some_and(|b| !b.is_empty() && b.bytes().all(|c| c.is_ascii_digit()));
            if p["system"] != "windows"
                || !p["machine"]
                    .as_str()
                    .is_some_and(|m| WINDOWS_ADMITTED_ARCHITECTURES.contains(&m))
            {
                return fail(CODE);
            }
            dotted_version(&p["windows_version"], CODE)?;
            if !build_ok {
                return fail(CODE);
            }
        }
        Platform::MacOs => {
            let version_ok = p["macos_version"].as_str().is_some_and(|v| {
                let parts: Vec<&str> = v.split('.').collect();
                (2..=3).contains(&parts.len())
                    && parts
                        .iter()
                        .all(|x| !x.is_empty() && x.bytes().all(|c| c.is_ascii_digit()))
            });
            if p["system"] != "macos"
                || !p["machine"]
                    .as_str()
                    .is_some_and(|m| MACOS_ADMITTED_ARCHITECTURES.contains(&m))
                || !version_ok
            {
                return fail(CODE);
            }
        }
    }
    Ok(())
}

fn validate_publisher_extra(platform: Platform, p: &Map<String, Value>) -> Result<(), E> {
    match platform {
        Platform::Linux => Ok(()),
        Platform::Windows => {
            let subject = &p["authenticode_subject"];
            if subject != AUTHENTICODE_UNSIGNED_SUBJECT && !is_text(subject) {
                return fail("package_publisher_invalid");
            }
            Ok(())
        }
        Platform::MacOs => {
            let team = &p["developer_id_team"];
            let team_ok = team.as_str().is_some_and(|t| {
                t.len() == TEAM_ID_CHARS
                    && t.bytes()
                        .all(|c| c.is_ascii_digit() || c.is_ascii_uppercase())
            });
            if team != DEVELOPER_ID_UNSIGNED && !team_ok {
                return fail("package_publisher_invalid");
            }
            Ok(())
        }
    }
}

fn validate_bindings(d: &Map<String, Value>, platform: Platform) -> Result<(), E> {
    let revision_ok = d["source_revision"].as_str().is_some_and(|s| {
        s.len() == SOURCE_REVISION_HEX
            && s.bytes()
                .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
    });
    if !revision_ok {
        return fail("package_source_revision_invalid");
    }
    match d["build_inputs"].as_object() {
        Some(b) if exact(b, &BUILD_INPUT_FIELDS) => {
            if !is_segment(&b["builder_id"]) || !is_digest(&b["build_digest"]) {
                return fail("package_build_inputs_invalid");
            }
        }
        _ => return fail("package_build_inputs_invalid"),
    }
    match d["platform_architecture"].as_object() {
        Some(p) if exact(p, platform.platform_fields()) => validate_platform(platform, p)?,
        _ => return fail("package_platform_invalid"),
    }
    if !is_digest(&d["package_digest"]) {
        return fail("package_digest_invalid");
    }
    let mut publisher_fields: Vec<&str> = BASE_PUBLISHER_FIELDS.to_vec();
    publisher_fields.extend(platform.extra_publisher_field());
    let Some(publisher) = d["publisher_identity"]
        .as_object()
        .filter(|p| exact(p, &publisher_fields))
    else {
        return fail("package_publisher_invalid");
    };
    if !is_segment(&publisher["publisher_id"]) || !is_segment(&publisher["endpoint_id"]) {
        return fail("package_publisher_invalid");
    }
    validate_publisher_extra(platform, publisher)?;
    let record = &publisher["verification_key_record"];
    if !record.is_object() || identity::key_record_digest(record).is_err() {
        return fail("package_publisher_invalid");
    }
    if !is_segment(&d["minimum_os"]) {
        return fail("package_minimum_os_invalid");
    }
    let name_field = platform.descriptor_name_field();
    let descriptor_fields = [name_field, "sha256", "install_mode", "lifecycle_support"];
    let descriptor_ok = d["service_descriptor"].as_object().is_some_and(|s| {
        exact(s, &descriptor_fields)
            && is_segment(&s[name_field])
            && is_digest(&s["sha256"])
            && s["install_mode"]
                .as_str()
                .is_some_and(|m| platform.install_modes().contains(&m))
            && s["lifecycle_support"] == true
    });
    if !descriptor_ok {
        return fail("package_lifecycle_support_missing");
    }
    if !is_segment(&d["update_channel"]) {
        return fail("package_update_channel_invalid");
    }
    dotted_version(&d["rollback_floor"], ROLLBACK_CODE)?;
    let protocol_ok = is_segment(&d["capability_protocol"])
        && d["capability_protocol"]
            .as_str()
            .is_some_and(|s| s.starts_with("mycelium."));
    if !protocol_ok {
        return fail("package_capability_protocol_invalid");
    }
    if !is_segment(&d["privacy_notice"]) {
        return fail("package_privacy_notice_invalid");
    }
    if !is_segment(&d["expiry_revocation_policy"]) {
        return fail("package_expiry_policy_invalid");
    }
    Ok(())
}

fn validate_times(d: &Map<String, Value>) -> Result<(), E> {
    let number = |v: &Value| {
        v.as_number()
            .and_then(serde_json::Number::as_f64)
            .filter(|f| !f.is_nan())
    };
    match (number(&d["issued_at"]), number(&d["expires_at"])) {
        (Some(issued), Some(expires)) if expires > issued => Ok(()),
        _ => fail("package_manifest_shape_invalid"),
    }
}

/// `validate_manifest`: closed-shape validation of one installed-package manifest.
pub fn validate_manifest(document: &Value, platform: Platform) -> Result<Value, E> {
    let fields: Vec<&str> = BINDINGS
        .iter()
        .chain(ENVELOPE_FIELDS.iter())
        .copied()
        .collect();
    let Some(d) = document.as_object().filter(|d| exact(d, &fields)) else {
        return fail("package_manifest_shape_invalid");
    };
    let Ok(bytes) = canonical::to_bytes(document) else {
        return fail("package_manifest_shape_invalid");
    };
    if bytes.len() > MAX_BYTES {
        return fail("package_manifest_too_large");
    }
    if d["protocol"] != PROTOCOL {
        return fail("package_manifest_protocol_invalid");
    }
    check_no_embedded_material(document)?;
    validate_bindings(d, platform)?;
    validate_times(d)?;
    if !d["signature"]
        .as_object()
        .is_some_and(|s| exact(s, &SIGNATURE_FIELDS))
    {
        return fail("package_signature_invalid");
    }
    Ok(document.clone())
}

/// `build_manifest`: assemble and sign one manifest from the twelve frozen bindings.
pub fn build_manifest(
    platform: Platform,
    signer: &Identity,
    issued_at: &Value,
    expires_at: &Value,
    bindings: &Map<String, Value>,
) -> Result<Value, E> {
    if !exact(bindings, &BINDINGS) {
        return fail("package_manifest_shape_invalid");
    }
    let mut document = Map::new();
    document.insert("protocol".into(), json!(PROTOCOL));
    for name in BINDINGS {
        document.insert(name.into(), bindings[name].clone());
    }
    document.insert("issued_at".into(), issued_at.clone());
    document.insert("expires_at".into(), expires_at.clone());
    let statement = Value::Object(document);
    check_no_embedded_material(&statement)?;
    let d = statement.as_object().unwrap_or(&Map::new()).clone();
    validate_bindings(&d, platform)?;
    validate_times(&d)?;
    let signature = signer
        .sign(&statement)
        .map_err(|_| PackageManifestError("package_signature_invalid"))?;
    let mut signed = d;
    signed.insert("signature".into(), signature);
    Ok(Value::Object(signed))
}

/// The pins every platform's `verify_*_package` takes.
pub struct Expect<'a> {
    pub expected_publisher: &'a str,
    pub publisher_key_digest_pin: &'a str,
    pub installed_version: &'a str,
    pub rollback_floor_pin: &'a str,
    pub now: f64,
}

/// What each platform's `verify_*_package` additionally expects of the offered package.
pub enum PlatformExpect<'a> {
    Linux {
        architecture: &'a str,
        distribution: &'a str,
    },
    Windows {
        architecture: &'a str,
        authenticode_subject: &'a str,
        windows_version: &'a str,
        windows_build: &'a str,
    },
    MacOs {
        architecture: &'a str,
        developer_id_team: &'a str,
        observed_macos_version: &'a str,
    },
}

/// `verify_signed`: signature, time, publisher label and pinned key, in the frozen order.
fn verify_signed<'a>(value: &'a Value, expect: &Expect<'_>) -> Result<&'a Value, E> {
    let publisher = &value["publisher_identity"];
    let key = &publisher["verification_key_record"];
    if identity::key_record_digest(key).is_err() {
        return fail("package_publisher_invalid");
    }
    let mut statement = value.as_object().cloned().unwrap_or_default();
    statement.remove("signature");
    if identity::verify(&Value::Object(statement), &value["signature"], key).is_err() {
        return fail("package_signature_invalid");
    }
    let expires = value["expires_at"].as_f64().unwrap_or(f64::NAN);
    if expect.now > expires {
        return fail("package_expired");
    }
    if publisher["publisher_id"] != expect.expected_publisher {
        return fail("package_publisher_mismatch");
    }
    if key["verification_key_digest"] != expect.publisher_key_digest_pin {
        return fail("package_publisher_mismatch");
    }
    Ok(publisher)
}

fn str_version(s: &str, code: &'static str) -> Result<Version, E> {
    dotted_version(&Value::String(s.to_owned()), code)
}

fn check_rollback_floor(value: &Value, expect: &Expect<'_>) -> Result<(), E> {
    let package_floor = dotted_version(&value["rollback_floor"], ROLLBACK_CODE)?;
    let pin = str_version(expect.rollback_floor_pin, ROLLBACK_CODE)?;
    let installed = str_version(expect.installed_version, ROLLBACK_CODE)?;
    if version_cmp(&package_floor, &pin).is_lt() || version_cmp(&installed, &package_floor).is_lt()
    {
        return fail("package_downgrade_rejected");
    }
    Ok(())
}

fn member_only(facts: &mut Map<String, Value>) {
    facts.insert("identity_creation_allowed".into(), json!(true));
    facts.insert("eligibility".into(), json!("member_only"));
    facts.insert("activation".into(), json!("none"));
    facts.insert("route_ready".into(), json!(false));
}

/// `verify_{linux,windows,macos}_package`: member-only facts or a refusal.
pub fn verify_package(
    document: &Value,
    expect: &Expect<'_>,
    platform_expect: &PlatformExpect<'_>,
) -> Result<Value, E> {
    let platform = match platform_expect {
        PlatformExpect::Linux { .. } => Platform::Linux,
        PlatformExpect::Windows { .. } => Platform::Windows,
        PlatformExpect::MacOs { .. } => Platform::MacOs,
    };
    let value = validate_manifest(document, platform)?;
    let publisher = verify_signed(&value, expect)?;
    let pa = &value["platform_architecture"];
    let mut facts = Map::new();
    facts.insert("verified".into(), json!(true));
    facts.insert("protocol".into(), json!(PROTOCOL));
    facts.insert("source_revision".into(), value["source_revision"].clone());
    match platform_expect {
        PlatformExpect::Linux {
            architecture,
            distribution,
        } => {
            const UNSUPPORTED: &str = "package_platform_unsupported";
            let declared = pa["distribution_id"].as_str() == Some("ubuntu");
            let offered = str_version(
                pa["distribution_version"].as_str().unwrap_or_default(),
                ROLLBACK_CODE,
            )
            .or_else(|_| fail(UNSUPPORTED))?;
            let floor = declared
                .then(|| str_version(LINUX_UBUNTU_MINIMUM, ROLLBACK_CODE))
                .transpose()
                .or_else(|_| fail(UNSUPPORTED))?;
            let machine = pa["machine"].as_str().unwrap_or_default();
            if pa["system"] != "linux"
                || !declared
                || floor.is_none_or(|floor| version_cmp(&offered, &floor).is_lt())
                || machine != *architecture
                || !LINUX_ADMITTED_ARCHITECTURES.contains(&machine)
                || pa["distribution_id"] != *distribution
            {
                return fail(UNSUPPORTED);
            }
            check_rollback_floor(&value, expect)?;
            facts.insert("platform_architecture".into(), pa["machine"].clone());
            facts.insert("distribution_id".into(), pa["distribution_id"].clone());
        }
        PlatformExpect::Windows {
            architecture,
            authenticode_subject,
            windows_version,
            windows_build,
        } => {
            if publisher["authenticode_subject"] != *authenticode_subject {
                return fail("package_publisher_mismatch");
            }
            let floor = str_version(WINDOWS_MINIMUM_VERSION, "package_platform_invalid")?;
            let offered = dotted_version(&pa["windows_version"], "package_platform_invalid")?;
            let build = pa["windows_build"].as_str().unwrap_or_default();
            let machine = pa["machine"].as_str().unwrap_or_default();
            let floor_build = WINDOWS_MINIMUM_BUILD;
            let build_trimmed = build.trim_start_matches('0');
            if machine != *architecture
                || !WINDOWS_ADMITTED_ARCHITECTURES.contains(&machine)
                || version_cmp(&offered, &floor).is_lt()
                || pa["windows_version"] != *windows_version
                || build != *windows_build
                || number_cmp(build_trimmed, floor_build).is_lt()
            {
                return fail("package_platform_unsupported");
            }
            check_rollback_floor(&value, expect)?;
            facts.insert("platform".into(), json!("windows"));
            facts.insert("platform_architecture".into(), pa["machine"].clone());
            facts.insert("windows_version".into(), pa["windows_version"].clone());
            facts.insert("windows_build".into(), pa["windows_build"].clone());
            facts.insert(
                "authenticode_subject".into(),
                publisher["authenticode_subject"].clone(),
            );
            facts.insert("inference_eligibility".into(), json!(false));
        }
        PlatformExpect::MacOs {
            architecture,
            developer_id_team,
            observed_macos_version,
        } => {
            if publisher["developer_id_team"] != *developer_id_team {
                return fail("package_publisher_mismatch");
            }
            if pa["machine"] != *architecture
                || !MACOS_ADMITTED_VERSIONS.contains(observed_macos_version)
            {
                return fail("package_platform_unsupported");
            }
            check_rollback_floor(&value, expect)?;
            facts.insert("platform".into(), json!("macos"));
            facts.insert("platform_architecture".into(), pa["machine"].clone());
            facts.insert(
                "developer_id_team".into(),
                publisher["developer_id_team"].clone(),
            );
            facts.insert("inference_eligibility".into(), json!(false));
        }
    }
    facts.insert("package_digest".into(), value["package_digest"].clone());
    facts.insert("publisher_id".into(), publisher["publisher_id"].clone());
    facts.insert(
        "publisher_key_digest".into(),
        json!(expect.publisher_key_digest_pin),
    );
    facts.insert("minimum_os".into(), value["minimum_os"].clone());
    facts.insert(
        "service_descriptor".into(),
        value["service_descriptor"].clone(),
    );
    facts.insert("update_channel".into(), value["update_channel"].clone());
    facts.insert("rollback_floor".into(), value["rollback_floor"].clone());
    facts.insert(
        "capability_protocol".into(),
        value["capability_protocol"].clone(),
    );
    member_only(&mut facts);
    Ok(Value::Object(facts))
}
