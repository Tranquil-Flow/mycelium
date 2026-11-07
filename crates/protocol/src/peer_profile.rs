// SPDX-License-Identifier: AGPL-3.0-only
//! `mycelium.membership.peer_profile.v2`: the closed platform-neutral declaration, byte-faithful to v0.1.4
//! `mycelium_platform_neutral/peer_profile.py` (+ the fail-closed primitives of `primitives.py` it uses).
//! A profile is the member's signed *declaration* of identity kind, platform family, architecture, software
//! build, installed transports and runtime backends; a declaration is never proof that a capability works
//! (evidence, class qualification and eligibility are separate authorities). The record is closed: unknown
//! fields and enum values are refused, `unknown` is a registered sentinel that stays visible and ineligible.
use crate::identity::{self, Identity};
use crate::{canonical, checks};
use serde_json::{Map, Value, json};

pub const PEER_PROFILE_PROTOCOL: &str = "mycelium.membership.peer_profile.v2";

const PROFILE_FIELDS: [&str; 16] = [
    "protocol",
    "profile_id",
    "swarm_id",
    "node_id",
    "incarnation",
    "membership_generation",
    "identity_kind",
    "platform_family",
    "platform_variant",
    "architecture",
    "software_build",
    "transports",
    "runtimes",
    "lease_expires_at",
    "issued_at",
    "expires_at",
];
const BUILD_FIELDS: [&str; 2] = ["version", "digest"];
const TRANSPORT_FIELDS: [&str; 11] = [
    "family",
    "build_digest",
    "wire_protocol_versions",
    "endpoint_id_digest",
    "connection_roles",
    "direct_observed",
    "relay_observed",
    "persistent_reuse",
    "lifecycle_availability",
    "max_frame_bytes",
    "max_concurrency",
];
const RUNTIME_FIELDS: [&str; 12] = [
    "backend_id",
    "build_digest",
    "model_families",
    "representations",
    "precisions",
    "decode_operations",
    "max_context_tokens",
    "max_concurrent_requests",
    "kv_schema",
    "artifact_formats",
    "cancellation_supported",
    "cleanup_supported",
];
const ENVELOPE_FIELDS: [&str; 4] = ["protocol", "profile", "signature", "verification_key"];
const SIGNATURE_FIELDS: [&str; 5] = [
    "algorithm",
    "signer_endpoint_id",
    "verification_key_digest",
    "signed_statement_digest",
    "signature",
];
const CONNECTION_ROLES: [&str; 2] = ["initiator", "responder"];
const IDENTITY_KINDS: [&str; 4] = [
    "native_peer",
    "browser_peer",
    "artifact_source",
    "probe_only",
];
const PLATFORM_FAMILIES: [&str; 8] = [
    "macos", "linux", "windows", "android", "ios", "ipados", "browser", "unknown",
];
const ARCHITECTURES: [&str; 4] = ["arm64", "x86_64", "wasm32", "unknown"];
const TRANSPORT_FAMILIES: [&str; 4] = ["iroh", "https", "browser_https", "unknown"];
const LIFECYCLE_AVAILABILITY: [&str; 4] = [
    "foreground_only",
    "foreground_and_background",
    "service_managed",
    "unknown",
];
/// The closed runtime backend registry (A9 `runtime_backend_registry`); `unknown` is the sentinel.
const RUNTIME_BACKENDS: [&str; 6] = [
    "mlx",
    "numpy",
    "llama_http",
    "openrouter_http",
    "windows_native_provider",
    "unknown",
];
const MAX_TRANSPORTS: usize = 4;
const MAX_RUNTIMES: usize = 8;
/// `primitives.py`: one canonical record may not exceed 64 KiB; lists hold at most 64 items; strings 256 chars.
const MAX_RECORD_BYTES: usize = 64 * 1024;
const MAX_LIST_ITEMS: usize = 64;
const MAX_STRING_CHARS: usize = 256;
const MAX_VARIANT_CHARS: usize = 64;
const MAX_GENERATION: u64 = 1 << 40;
const MAX_FRAME_BYTES: u64 = 1 << 30;
const MAX_CONCURRENCY: u64 = 1 << 20;
const MAX_CONTEXT_TOKENS: u64 = 1 << 30;
const MAX_CONCURRENT_REQUESTS: u64 = 1 << 20;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct PlatformNeutralError(pub &'static str);

pub(crate) type E = PlatformNeutralError;

pub(crate) fn req(ok: bool, code: &'static str) -> Result<(), E> {
    if ok {
        Ok(())
    } else {
        Err(PlatformNeutralError(code))
    }
}

pub(crate) fn closed<'a>(
    v: &'a Value,
    fields: &[&str],
    code: &'static str,
) -> Result<&'a Map<String, Value>, E> {
    let m = v.as_object().ok_or(PlatformNeutralError(code))?;
    req(
        m.len() == fields.len() && fields.iter().all(|f| m.contains_key(*f)),
        code,
    )?;
    Ok(m)
}

pub(crate) fn segment(v: &Value, code: &'static str) -> Result<String, E> {
    req(checks::segment(v), code)?;
    Ok(v.as_str().unwrap_or_default().to_owned())
}

pub(crate) fn digest(v: &Value, code: &'static str) -> Result<String, E> {
    req(checks::digest(v), code)?;
    Ok(v.as_str().unwrap_or_default().to_owned())
}

/// `str.isspace()`: Unicode White_Space plus the C0 separators U+001C..U+001F.
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\x1c'..='\x1f').contains(&c)
}

pub(crate) fn text(v: &Value, code: &'static str, maximum: usize) -> Result<String, E> {
    let s = v.as_str().ok_or(PlatformNeutralError(code))?;
    req(
        !s.is_empty() && s.trim_matches(py_space) == s && s.chars().count() <= maximum,
        code,
    )?;
    Ok(s.to_owned())
}

pub(crate) fn nullable_text(
    v: &Value,
    code: &'static str,
    maximum: usize,
) -> Result<Option<String>, E> {
    if v.is_null() {
        return Ok(None);
    }
    text(v, code, maximum).map(Some)
}

pub(crate) fn enum_value(v: &Value, allowed: &[&str], code: &'static str) -> Result<String, E> {
    match v.as_str() {
        Some(s) if allowed.contains(&s) => Ok(s.to_owned()),
        _ => Err(PlatformNeutralError(code)),
    }
}

pub(crate) fn bounded_int(
    v: &Value,
    code: &'static str,
    minimum: u64,
    maximum: u64,
) -> Result<u64, E> {
    match v.as_u64() {
        Some(n) if (minimum..=maximum).contains(&n) => Ok(n),
        _ => Err(PlatformNeutralError(code)),
    }
}

fn finite(v: &Value, code: &'static str, minimum: f64) -> Result<f64, E> {
    let number = v
        .as_number()
        .and_then(serde_json::Number::as_f64)
        .filter(|f| f.is_finite())
        .ok_or(PlatformNeutralError(code))?;
    req(number >= minimum, code)?;
    Ok(number)
}

pub(crate) fn nullable_bool(v: &Value, code: &'static str) -> Result<Option<bool>, E> {
    match v {
        Value::Null => Ok(None),
        Value::Bool(b) => Ok(Some(*b)),
        _ => Err(PlatformNeutralError(code)),
    }
}

fn require_bool(v: &Value, code: &'static str) -> Result<bool, E> {
    v.as_bool().ok_or(PlatformNeutralError(code))
}

fn bounded_list<'a>(v: &'a Value, code: &'static str, maximum: usize) -> Result<&'a Vec<Value>, E> {
    match v.as_array() {
        Some(a) if a.len() <= maximum => Ok(a),
        _ => Err(PlatformNeutralError(code)),
    }
}

pub(crate) fn unique_names(v: &Value, code: &'static str) -> Result<Vec<String>, E> {
    let mut out: Vec<String> = Vec::new();
    for item in bounded_list(v, code, MAX_LIST_ITEMS)? {
        let name = text(item, code, MAX_STRING_CHARS)?;
        req(!out.contains(&name), code)?;
        out.push(name);
    }
    Ok(out)
}

pub(crate) fn float_value(f: f64) -> Value {
    serde_json::Number::from_f64(f).map_or(Value::Null, Value::Number)
}

pub(crate) fn canonical_checked(value: &Value, code: &'static str) -> Result<Vec<u8>, E> {
    let bytes = canonical::to_bytes(value).map_err(|_| PlatformNeutralError(code))?;
    req(bytes.len() <= MAX_RECORD_BYTES, code)?;
    Ok(bytes)
}

#[derive(Debug, Clone, PartialEq)]
struct Transport {
    family: String,
    build_digest: String,
    wire_protocol_versions: Vec<String>,
    endpoint_id_digest: Option<String>,
    connection_roles: Vec<String>,
    direct_observed: Option<bool>,
    relay_observed: Option<bool>,
    persistent_reuse: Option<bool>,
    lifecycle_availability: String,
    max_frame_bytes: u64,
    max_concurrency: u64,
}

impl Transport {
    fn from_mapping(value: &Value) -> Result<Self, E> {
        let r = closed(value, &TRANSPORT_FIELDS, "transport_declaration_invalid")?;
        let family = enum_value(
            &r["family"],
            &TRANSPORT_FAMILIES,
            "transport_family_invalid",
        )?;
        let build_digest = digest(&r["build_digest"], "transport_build_invalid")?;
        let wire_protocol_versions = unique_names(
            &r["wire_protocol_versions"],
            "transport_wire_versions_invalid",
        )?;
        let endpoint_id_digest = if r["endpoint_id_digest"].is_null() {
            None
        } else {
            Some(digest(
                &r["endpoint_id_digest"],
                "transport_endpoint_digest_invalid",
            )?)
        };
        let mut connection_roles = Vec::new();
        for role in unique_names(&r["connection_roles"], "transport_roles_invalid")? {
            connection_roles.push(enum_value(
                &json!(role),
                &CONNECTION_ROLES,
                "transport_role_invalid",
            )?);
        }
        connection_roles.sort();
        Ok(Self {
            family,
            build_digest,
            wire_protocol_versions,
            endpoint_id_digest,
            connection_roles,
            direct_observed: nullable_bool(
                &r["direct_observed"],
                "transport_direct_observed_invalid",
            )?,
            relay_observed: nullable_bool(
                &r["relay_observed"],
                "transport_relay_observed_invalid",
            )?,
            persistent_reuse: nullable_bool(
                &r["persistent_reuse"],
                "transport_persistent_reuse_invalid",
            )?,
            lifecycle_availability: enum_value(
                &r["lifecycle_availability"],
                &LIFECYCLE_AVAILABILITY,
                "transport_lifecycle_invalid",
            )?,
            max_frame_bytes: bounded_int(
                &r["max_frame_bytes"],
                "transport_frame_bound_invalid",
                1,
                MAX_FRAME_BYTES,
            )?,
            max_concurrency: bounded_int(
                &r["max_concurrency"],
                "transport_concurrency_bound_invalid",
                1,
                MAX_CONCURRENCY,
            )?,
        })
    }

    fn to_mapping(&self) -> Value {
        json!({
            "family": self.family,
            "build_digest": self.build_digest,
            "wire_protocol_versions": self.wire_protocol_versions,
            "endpoint_id_digest": self.endpoint_id_digest,
            "connection_roles": self.connection_roles,
            "direct_observed": self.direct_observed,
            "relay_observed": self.relay_observed,
            "persistent_reuse": self.persistent_reuse,
            "lifecycle_availability": self.lifecycle_availability,
            "max_frame_bytes": self.max_frame_bytes,
            "max_concurrency": self.max_concurrency,
        })
    }
}

#[derive(Debug, Clone, PartialEq)]
struct Runtime {
    backend_id: String,
    build_digest: String,
    model_families: Vec<String>,
    representations: Vec<String>,
    precisions: Vec<String>,
    decode_operations: Vec<String>,
    max_context_tokens: u64,
    max_concurrent_requests: u64,
    kv_schema: String,
    artifact_formats: Vec<String>,
    cancellation_supported: bool,
    cleanup_supported: bool,
}

impl Runtime {
    fn from_mapping(value: &Value) -> Result<Self, E> {
        let r = closed(value, &RUNTIME_FIELDS, "runtime_declaration_invalid")?;
        let backend_id = match r["backend_id"].as_str() {
            Some(b) if RUNTIME_BACKENDS.contains(&b) => b.to_owned(),
            _ => return Err(PlatformNeutralError("runtime_backend_not_registered")),
        };
        Ok(Self {
            backend_id,
            build_digest: digest(&r["build_digest"], "runtime_build_invalid")?,
            model_families: unique_names(&r["model_families"], "runtime_model_families_invalid")?,
            representations: unique_names(
                &r["representations"],
                "runtime_representations_invalid",
            )?,
            precisions: unique_names(&r["precisions"], "runtime_precisions_invalid")?,
            decode_operations: unique_names(
                &r["decode_operations"],
                "runtime_decode_operations_invalid",
            )?,
            max_context_tokens: bounded_int(
                &r["max_context_tokens"],
                "runtime_context_bound_invalid",
                1,
                MAX_CONTEXT_TOKENS,
            )?,
            max_concurrent_requests: bounded_int(
                &r["max_concurrent_requests"],
                "runtime_concurrency_bound_invalid",
                1,
                MAX_CONCURRENT_REQUESTS,
            )?,
            kv_schema: text(
                &r["kv_schema"],
                "runtime_kv_schema_invalid",
                MAX_STRING_CHARS,
            )?,
            artifact_formats: unique_names(
                &r["artifact_formats"],
                "runtime_artifact_formats_invalid",
            )?,
            cancellation_supported: require_bool(
                &r["cancellation_supported"],
                "runtime_cancellation_invalid",
            )?,
            cleanup_supported: require_bool(&r["cleanup_supported"], "runtime_cleanup_invalid")?,
        })
    }

    fn to_mapping(&self) -> Value {
        json!({
            "backend_id": self.backend_id,
            "build_digest": self.build_digest,
            "model_families": self.model_families,
            "representations": self.representations,
            "precisions": self.precisions,
            "decode_operations": self.decode_operations,
            "max_context_tokens": self.max_context_tokens,
            "max_concurrent_requests": self.max_concurrent_requests,
            "kv_schema": self.kv_schema,
            "artifact_formats": self.artifact_formats,
            "cancellation_supported": self.cancellation_supported,
            "cleanup_supported": self.cleanup_supported,
        })
    }
}

/// A verified-shape `PeerProfile` (parsing is closed; signing and verification are separate).
#[derive(Debug, Clone, PartialEq)]
pub struct PeerProfile {
    pub(crate) profile_id: String,
    pub(crate) swarm_id: String,
    pub(crate) node_id: String,
    pub(crate) incarnation: String,
    pub(crate) membership_generation: u64,
    identity_kind: String,
    platform_family: String,
    platform_variant: Option<String>,
    architecture: String,
    software_build_version: String,
    software_build_digest: String,
    transports: Vec<Transport>,
    runtimes: Vec<Runtime>,
    lease_expires_at: f64,
    issued_at: f64,
    expires_at: f64,
}

fn require_distinct(values: &[&str], code: &'static str) -> Result<(), E> {
    let mut seen: Vec<&str> = Vec::new();
    for v in values {
        req(!seen.contains(v), code)?;
        seen.push(v);
    }
    Ok(())
}

impl PeerProfile {
    /// `PeerProfile.from_mapping`.
    pub fn from_mapping(value: &Value) -> Result<Self, E> {
        let r = closed(value, &PROFILE_FIELDS, "peer_profile_invalid")?;
        req(
            r["protocol"] == PEER_PROFILE_PROTOCOL,
            "peer_profile_protocol_invalid",
        )?;
        let build = closed(
            &r["software_build"],
            &BUILD_FIELDS,
            "peer_profile_build_invalid",
        )?;
        let platform_family = enum_value(
            &r["platform_family"],
            &PLATFORM_FAMILIES,
            "peer_profile_platform_invalid",
        )?;
        let platform_variant = nullable_text(
            &r["platform_variant"],
            "peer_profile_platform_variant_invalid",
            MAX_VARIANT_CHARS,
        )?;
        req(
            platform_variant.is_none() || platform_family == "unknown",
            "peer_profile_platform_variant_requires_unknown_family",
        )?;
        let transports = bounded_list(
            &r["transports"],
            "peer_profile_transports_invalid",
            MAX_TRANSPORTS,
        )?
        .iter()
        .map(Transport::from_mapping)
        .collect::<Result<Vec<_>, _>>()?;
        require_distinct(
            &transports
                .iter()
                .map(|t| t.family.as_str())
                .collect::<Vec<_>>(),
            "peer_profile_transports_duplicate",
        )?;
        let runtimes = bounded_list(
            &r["runtimes"],
            "peer_profile_runtimes_invalid",
            MAX_RUNTIMES,
        )?
        .iter()
        .map(Runtime::from_mapping)
        .collect::<Result<Vec<_>, _>>()?;
        require_distinct(
            &runtimes
                .iter()
                .map(|t| t.backend_id.as_str())
                .collect::<Vec<_>>(),
            "peer_profile_runtimes_duplicate",
        )?;
        let issued_at = finite(&r["issued_at"], "peer_profile_issued_at_invalid", 0.0)?;
        let expires_at = finite(&r["expires_at"], "peer_profile_expires_at_invalid", 0.0)?;
        req(expires_at > issued_at, "peer_profile_validity_invalid")?;
        Ok(Self {
            profile_id: segment(&r["profile_id"], "peer_profile_id_invalid")?,
            swarm_id: segment(&r["swarm_id"], "peer_profile_swarm_id_invalid")?,
            node_id: segment(&r["node_id"], "peer_profile_node_id_invalid")?,
            incarnation: segment(&r["incarnation"], "peer_profile_incarnation_invalid")?,
            membership_generation: bounded_int(
                &r["membership_generation"],
                "peer_profile_generation_invalid",
                1,
                MAX_GENERATION,
            )?,
            identity_kind: enum_value(
                &r["identity_kind"],
                &IDENTITY_KINDS,
                "peer_profile_identity_kind_invalid",
            )?,
            platform_family,
            platform_variant,
            architecture: enum_value(
                &r["architecture"],
                &ARCHITECTURES,
                "peer_profile_architecture_invalid",
            )?,
            software_build_version: text(
                &build["version"],
                "peer_profile_build_version_invalid",
                MAX_STRING_CHARS,
            )?,
            software_build_digest: digest(&build["digest"], "peer_profile_build_digest_invalid")?,
            transports,
            runtimes,
            lease_expires_at: finite(&r["lease_expires_at"], "peer_profile_lease_invalid", 0.0)?,
            issued_at,
            expires_at,
        })
    }

    pub fn to_mapping(&self) -> Value {
        json!({
            "protocol": PEER_PROFILE_PROTOCOL,
            "profile_id": self.profile_id,
            "swarm_id": self.swarm_id,
            "node_id": self.node_id,
            "incarnation": self.incarnation,
            "membership_generation": self.membership_generation,
            "identity_kind": self.identity_kind,
            "platform_family": self.platform_family,
            "platform_variant": self.platform_variant,
            "architecture": self.architecture,
            "software_build": {
                "version": self.software_build_version,
                "digest": self.software_build_digest,
            },
            "transports": self.transports.iter().map(Transport::to_mapping).collect::<Vec<_>>(),
            "runtimes": self.runtimes.iter().map(Runtime::to_mapping).collect::<Vec<_>>(),
            "lease_expires_at": float_value(self.lease_expires_at),
            "issued_at": float_value(self.issued_at),
            "expires_at": float_value(self.expires_at),
        })
    }

    pub fn canonical_bytes(&self) -> Result<Vec<u8>, E> {
        canonical_checked(&self.to_mapping(), "peer_profile_noncanonical")
    }

    /// The profile digest bound by evidence, qualification and eligibility.
    pub fn digest(&self) -> Result<String, E> {
        Ok(canonical::sha256_prefixed(&self.canonical_bytes()?))
    }

    pub fn is_unknown_platform(&self) -> bool {
        self.platform_family == "unknown" || self.architecture == "unknown"
    }

    pub fn node_id(&self) -> &str {
        &self.node_id
    }

    pub fn expires_at(&self) -> f64 {
        self.expires_at
    }

    pub fn runtime_ids(&self) -> Vec<&str> {
        self.runtimes
            .iter()
            .map(|r| r.backend_id.as_str())
            .collect()
    }
}

/// `sign_peer_profile`: sign one profile with the member's durable key.
pub fn sign_peer_profile(signer: &Identity, profile: &PeerProfile) -> Result<Value, E> {
    let statement = profile.to_mapping();
    let signature = signer
        .sign(&statement)
        .map_err(|_| PlatformNeutralError("peer_profile_noncanonical"))?;
    Ok(json!({
        "protocol": PEER_PROFILE_PROTOCOL,
        "profile": statement,
        "signature": signature,
        "verification_key": signer.public_key_record(),
    }))
}

/// `verify_peer_profile`: a declaration proves only that it was declared. A malformed verification key is a
/// Python exception in the oracle (not a contract code); here it is the refusal `peer_profile_verifier_invalid`.
pub fn verify_peer_profile(
    envelope: &Value,
    now: f64,
    expected_key_digest: Option<&str>,
    expected_node_id: Option<&str>,
) -> Result<PeerProfile, E> {
    let record = closed(envelope, &ENVELOPE_FIELDS, "peer_profile_envelope_invalid")?;
    req(
        record["protocol"] == PEER_PROFILE_PROTOCOL,
        "peer_profile_protocol_invalid",
    )?;
    let profile = PeerProfile::from_mapping(&record["profile"])?;
    let signature = closed(
        &record["signature"],
        &SIGNATURE_FIELDS,
        "peer_profile_signature_invalid",
    )?;
    let key = &record["verification_key"];
    let detached_ok = canonical_checked(key, "peer_profile_key_invalid").is_ok() && key.is_object();
    req(detached_ok, "peer_profile_key_invalid")?;
    req(
        signature["verification_key_digest"] == key["verification_key_digest"],
        "peer_profile_key_digest_mismatch",
    )?;
    if let Some(pin) = expected_key_digest {
        req(
            signature["verification_key_digest"] == pin,
            "peer_profile_key_continuity_mismatch",
        )?;
    }
    if let Some(node) = expected_node_id {
        req(profile.node_id == node, "peer_profile_node_mismatch")?;
    }
    identity::key_record_digest(key)
        .map_err(|_| PlatformNeutralError("peer_profile_verifier_invalid"))?;
    let statement = profile.to_mapping();
    canonical_checked(&statement, "peer_profile_noncanonical")?;
    req(
        identity::verify(&statement, &record["signature"], key).is_ok(),
        "peer_profile_signature_invalid",
    )?;
    req(now.is_finite() && now >= 0.0, "peer_profile_now_invalid")?;
    req(now <= profile.expires_at, "peer_profile_expired")?;
    Ok(profile)
}
