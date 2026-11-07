// SPDX-License-Identifier: AGPL-3.0-only
//! `mycelium.membership.capability_report.v2` of v0.1.4 `mycelium_platform_neutral.capability_report`: the
//! fresh signed observation report. A report carries seven bounded, nullable observation families; every family
//! names its own evidence source kind and one source kind can never satisfy another's gate. Missing observations
//! are `null`, never zero or healthy. A report is bound to the exact profile digest and evidence generation it
//! was observed under. Parsing is closed (exact field sets, bounded values, an `unknown` source carries nothing);
//! signing and verification are separate steps, as in the oracle.
use crate::canonical;
use crate::identity::{self, Identity};
use crate::peer_profile::{
    E, PeerProfile, PlatformNeutralError, bounded_int, canonical_checked, closed, digest,
    enum_value, float_value, nullable_bool, nullable_text, req, segment, unique_names,
};
use serde_json::{Map, Value, json};

pub const CAPABILITY_REPORT_PROTOCOL: &str = "mycelium.membership.capability_report.v2";

/// The seven observation families of the capability profile (spec §3.3), in the oracle's order.
pub const CAPABILITY_FAMILIES: [&str; 7] = [
    "memory",
    "storage",
    "limits",
    "thermal",
    "power",
    "lifecycle",
    "network",
];

const REPORT_FIELDS: [&str; 11] = [
    "protocol",
    "report_id",
    "swarm_id",
    "node_id",
    "incarnation",
    "membership_generation",
    "profile_digest",
    "evidence_generation",
    "observations",
    "issued_at",
    "expires_at",
];
const FAMILY_FIELDS: [&str; 3] = ["source_kind", "observed_at", "values"];
const ENVELOPE_FIELDS: [&str; 4] = ["protocol", "report", "signature", "verification_key"];
const SIGNATURE_FIELDS: [&str; 5] = [
    "algorithm",
    "signer_endpoint_id",
    "verification_key_digest",
    "signed_statement_digest",
    "signature",
];
const EVIDENCE_SOURCE_KINDS: [&str; 4] = [
    "native_observed",
    "synthetic_conformance",
    "operator_declared",
    "unknown",
];
const MAX_GENERATION: u64 = 1 << 40;
const MAX_BYTES: u64 = 1 << 60;
const MAX_COUNT: u64 = 1 << 40;
const MAX_SECONDS: f64 = 1.0e9;

const THERMAL_STATES: [&str; 5] = ["nominal", "fair", "serious", "critical", "unknown"];
const THERMAL_CLASSES: [&str; 3] = ["sustained", "burst", "unknown"];
const THROTTLE_BEHAVIORS: [&str; 4] = ["none", "frequency_limited", "power_limited", "unknown"];
const POWER_SOURCES: [&str; 3] = ["ac", "battery", "unknown"];
const LIFECYCLE_STATES: [&str; 5] = ["active", "foreground", "background", "suspended", "unknown"];
const SUSPEND_BEHAVIORS: [&str; 3] = ["resume_safe", "restart_required", "unknown"];
const RESTART_SEMANTICS: [&str; 3] = ["same_incarnation", "new_incarnation", "unknown"];
const NETWORK_LOSS_BEHAVIORS: [&str; 3] = ["reconnect", "drain", "unknown"];

#[derive(Clone, Copy)]
enum Kind {
    Bytes,
    Count,
    Seconds,
    Ratio,
    Bool,
    Text,
    Protocol,
    TextList,
    Enum(&'static [&'static str]),
}

/// One schema field: its name, value kind and the oracle's (family-specific) refusal code.
struct Field {
    name: &'static str,
    kind: Kind,
    code: &'static str,
}

macro_rules! field {
    ($family:literal, $name:literal, $kind:expr) => {
        Field {
            name: $name,
            kind: $kind,
            code: concat!("capability_value_invalid:", $family, ":", $name),
        }
    };
}

/// `FAMILY_SCHEMA`: family -> ordered fields (the order is the oracle's, so the first failing field's code wins).
fn schema(family: &str) -> &'static [Field] {
    use Kind::{Bool, Bytes, Count, Enum, Protocol, Ratio, Seconds, Text, TextList};
    static MEMORY: [Field; 4] = [
        field!("memory", "sustainable_bytes", Bytes),
        field!("memory", "available_bytes", Bytes),
        field!("memory", "workspace_bytes", Bytes),
        field!("memory", "kv_headroom_bytes", Bytes),
    ];
    static STORAGE: [Field; 2] = [
        field!("storage", "assignment_local_bytes", Bytes),
        field!("storage", "free_bytes", Bytes),
    ];
    static LIMITS: [Field; 7] = [
        field!("limits", "supported_context_tokens", Count),
        field!("limits", "active_requests", Count),
        field!("limits", "batch_size", Count),
        field!("limits", "prefill_tokens", Count),
        field!("limits", "decode_tokens", Count),
        field!("limits", "multi_position_verify", Count),
        field!("limits", "speculative_draft", Count),
    ];
    static THERMAL: [Field; 4] = [
        field!("thermal", "state", Enum(&THERMAL_STATES)),
        field!("thermal", "sustained_class", Enum(&THERMAL_CLASSES)),
        field!("thermal", "observation_duration_s", Seconds),
        field!("thermal", "throttle_behavior", Enum(&THROTTLE_BEHAVIORS)),
    ];
    static POWER: [Field; 6] = [
        field!("power", "source", Enum(&POWER_SOURCES)),
        field!("power", "battery_class", Text),
        field!("power", "battery_level", Ratio),
        field!("power", "low_power_state", Bool),
        field!("power", "minimum_contribution", Text),
        field!("power", "drain_deadline_s", Seconds),
    ];
    static LIFECYCLE: [Field; 5] = [
        field!("lifecycle", "state", Enum(&LIFECYCLE_STATES)),
        field!("lifecycle", "background_execution", Bool),
        field!("lifecycle", "suspend_behavior", Enum(&SUSPEND_BEHAVIORS)),
        field!("lifecycle", "restart_semantics", Enum(&RESTART_SEMANTICS)),
        field!("lifecycle", "max_background_grace_s", Seconds),
    ];
    static NETWORK: [Field; 5] = [
        field!("network", "activation_protocol", Protocol),
        field!("network", "transport_observations", TextList),
        field!(
            "network",
            "network_loss_behavior",
            Enum(&NETWORK_LOSS_BEHAVIORS)
        ),
        field!("network", "link_measurement_available", Bool),
        field!("network", "freshness_s", Seconds),
    ];
    match family {
        "memory" => &MEMORY,
        "storage" => &STORAGE,
        "limits" => &LIMITS,
        "thermal" => &THERMAL,
        "power" => &POWER,
        "lifecycle" => &LIFECYCLE,
        _ => &NETWORK,
    }
}

/// `nullable_finite(..., minimum=0.0, maximum=...)` (`finite`: an int or float, not a bool, finite, in range).
fn nullable_finite(v: &Value, code: &'static str, maximum: Option<f64>) -> Result<Option<f64>, E> {
    if v.is_null() {
        return Ok(None);
    }
    let number = v
        .as_number()
        .and_then(serde_json::Number::as_f64)
        .filter(|f| f.is_finite())
        .ok_or(PlatformNeutralError(code))?;
    req(number >= 0.0 && maximum.is_none_or(|m| number <= m), code)?;
    Ok(Some(number))
}

fn nullable_int(v: &Value, code: &'static str, maximum: u64) -> Result<Value, E> {
    if v.is_null() {
        return Ok(Value::Null);
    }
    bounded_int(v, code, 0, maximum).map(Value::from)
}

fn validate_value(field: &Field, value: &Value) -> Result<Value, E> {
    let code = field.code;
    Ok(match field.kind {
        Kind::Bytes => nullable_int(value, code, MAX_BYTES)?,
        Kind::Count => nullable_int(value, code, MAX_COUNT)?,
        Kind::Seconds => {
            nullable_finite(value, code, Some(MAX_SECONDS))?.map_or(Value::Null, float_value)
        }
        Kind::Ratio => nullable_finite(value, code, Some(1.0))?.map_or(Value::Null, float_value),
        Kind::Bool => nullable_bool(value, code)?.map_or(Value::Null, Value::Bool),
        Kind::Text => nullable_text(value, code, 256)?.map_or(Value::Null, Value::String),
        Kind::Protocol => nullable_text(value, code, 128)?.map_or(Value::Null, Value::String),
        Kind::TextList => {
            if value.is_null() {
                Value::Null
            } else {
                json!(unique_names(value, code)?)
            }
        }
        Kind::Enum(allowed) => {
            if value.is_null() {
                Value::Null
            } else {
                Value::String(enum_value(value, allowed, code)?)
            }
        }
    })
}

/// One family's observation: its own source kind, when it was observed, and its nullable values.
#[derive(Debug, Clone, PartialEq)]
pub struct FamilyObservation {
    family: &'static str,
    source_kind: String,
    observed_at: Option<f64>,
    values: Map<String, Value>,
}

impl FamilyObservation {
    fn from_mapping(family: &'static str, value: &Value) -> Result<Self, E> {
        let record = closed(value, &FAMILY_FIELDS, "capability_family_invalid")?;
        let source_kind = enum_value(
            &record["source_kind"],
            &EVIDENCE_SOURCE_KINDS,
            "capability_source_kind_invalid",
        )?;
        let observed_at = nullable_finite(
            &record["observed_at"],
            "capability_observed_at_invalid",
            None,
        )?;
        let fields = schema(family);
        let names: Vec<&str> = fields.iter().map(|f| f.name).collect();
        let raw = closed(&record["values"], &names, "capability_values_invalid")?;
        let mut values = Map::new();
        for field in fields {
            values.insert(
                field.name.to_owned(),
                validate_value(field, &raw[field.name])?,
            );
        }
        if source_kind == "unknown" {
            req(
                values.values().all(Value::is_null),
                "capability_unknown_family_must_be_null",
            )?;
        }
        Ok(Self {
            family,
            source_kind,
            observed_at,
            values,
        })
    }

    fn to_mapping(&self) -> Value {
        json!({
            "source_kind": self.source_kind,
            "observed_at": self.observed_at.map_or(Value::Null, float_value),
            "values": self.values,
        })
    }

    pub fn family(&self) -> &str {
        self.family
    }

    /// Only the exact required source kind can close a gate (`unknown` never does).
    pub fn satisfies(&self, required_source_kind: &str) -> bool {
        self.source_kind == required_source_kind && required_source_kind != "unknown"
    }

    pub fn is_unknown(&self) -> bool {
        self.source_kind == "unknown" || self.values.values().all(Value::is_null)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct CapabilityReport {
    report_id: String,
    swarm_id: String,
    node_id: String,
    incarnation: String,
    membership_generation: u64,
    profile_digest: String,
    evidence_generation: u64,
    observations: Vec<FamilyObservation>,
    issued_at: f64,
    expires_at: f64,
}

/// `finite(..., minimum=0.0)` for the report validity window.
fn instant(v: &Value, code: &'static str) -> Result<f64, E> {
    nullable_finite(v, code, None).and_then(|n| n.ok_or(PlatformNeutralError(code)))
}

impl CapabilityReport {
    pub fn from_mapping(value: &Value) -> Result<Self, E> {
        let record = closed(value, &REPORT_FIELDS, "capability_report_invalid")?;
        req(
            record["protocol"] == CAPABILITY_REPORT_PROTOCOL,
            "capability_report_protocol_invalid",
        )?;
        let observations = closed(
            &record["observations"],
            &CAPABILITY_FAMILIES,
            "capability_families_invalid",
        )?;
        let issued_at = instant(&record["issued_at"], "capability_report_issued_at_invalid")?;
        let expires_at = instant(
            &record["expires_at"],
            "capability_report_expires_at_invalid",
        )?;
        req(expires_at > issued_at, "capability_report_validity_invalid")?;
        let report_id = segment(&record["report_id"], "capability_report_id_invalid")?;
        let swarm_id = segment(&record["swarm_id"], "capability_report_swarm_id_invalid")?;
        let node_id = segment(&record["node_id"], "capability_report_node_id_invalid")?;
        let incarnation = segment(
            &record["incarnation"],
            "capability_report_incarnation_invalid",
        )?;
        let membership_generation = bounded_int(
            &record["membership_generation"],
            "capability_report_generation_invalid",
            1,
            MAX_GENERATION,
        )?;
        let profile_digest = digest(
            &record["profile_digest"],
            "capability_report_profile_digest_invalid",
        )?;
        let evidence_generation = bounded_int(
            &record["evidence_generation"],
            "capability_report_evidence_generation_invalid",
            1,
            MAX_GENERATION,
        )?;
        let mut parsed = Vec::with_capacity(CAPABILITY_FAMILIES.len());
        for family in CAPABILITY_FAMILIES {
            parsed.push(FamilyObservation::from_mapping(
                family,
                &observations[family],
            )?);
        }
        Ok(Self {
            report_id,
            swarm_id,
            node_id,
            incarnation,
            membership_generation,
            profile_digest,
            evidence_generation,
            observations: parsed,
            issued_at,
            expires_at,
        })
    }

    pub fn to_mapping(&self) -> Value {
        let mut observations = Map::new();
        for observation in &self.observations {
            observations.insert(observation.family.to_owned(), observation.to_mapping());
        }
        json!({
            "protocol": CAPABILITY_REPORT_PROTOCOL,
            "report_id": self.report_id,
            "swarm_id": self.swarm_id,
            "node_id": self.node_id,
            "incarnation": self.incarnation,
            "membership_generation": self.membership_generation,
            "profile_digest": self.profile_digest,
            "evidence_generation": self.evidence_generation,
            "observations": observations,
            "issued_at": float_value(self.issued_at),
            "expires_at": float_value(self.expires_at),
        })
    }

    pub fn canonical_bytes(&self) -> Result<Vec<u8>, E> {
        canonical_checked(&self.to_mapping(), "capability_report_noncanonical")
    }

    pub fn digest(&self) -> Result<String, E> {
        Ok(canonical::sha256_prefixed(&self.canonical_bytes()?))
    }

    /// Reject a report that is not bound to this exact profile generation.
    pub fn bound_to(&self, profile: &PeerProfile) -> Result<(), E> {
        req(
            self.profile_digest == profile.digest()?
                && self.membership_generation == profile.membership_generation
                && self.incarnation == profile.incarnation
                && self.node_id == profile.node_id,
            "capability_report_profile_mismatch",
        )
    }

    pub fn is_fresh(&self, now: f64) -> bool {
        now <= self.expires_at
    }

    pub fn family(&self, name: &str) -> Option<&FamilyObservation> {
        self.observations.iter().find(|o| o.family == name)
    }
}

/// `sign_capability_report`: sign one report with the member's durable key.
pub fn sign_capability_report(signer: &Identity, report: &CapabilityReport) -> Result<Value, E> {
    let statement = report.to_mapping();
    let signature = signer
        .sign(&statement)
        .map_err(|_| PlatformNeutralError("capability_report_noncanonical"))?;
    Ok(json!({
        "protocol": CAPABILITY_REPORT_PROTOCOL,
        "report": statement,
        "signature": signature,
        "verification_key": signer.public_key_record(),
    }))
}

/// `verify_capability_report`. A malformed verification key is a Python exception in the oracle (not a contract
/// code); here it is the refusal `capability_report_verifier_invalid`.
pub fn verify_capability_report(
    envelope: &Value,
    now: f64,
    expected_key_digest: Option<&str>,
) -> Result<CapabilityReport, E> {
    let record = closed(
        envelope,
        &ENVELOPE_FIELDS,
        "capability_report_envelope_invalid",
    )?;
    req(
        record["protocol"] == CAPABILITY_REPORT_PROTOCOL,
        "capability_report_protocol_invalid",
    )?;
    let report = CapabilityReport::from_mapping(&record["report"])?;
    let signature = closed(
        &record["signature"],
        &SIGNATURE_FIELDS,
        "capability_report_signature_invalid",
    )?;
    if let Some(pin) = expected_key_digest {
        req(
            signature["verification_key_digest"] == pin,
            "capability_report_key_mismatch",
        )?;
    }
    let key = &record["verification_key"];
    identity::key_record_digest(key)
        .map_err(|_| PlatformNeutralError("capability_report_verifier_invalid"))?;
    let statement = report.to_mapping();
    canonical_checked(&statement, "capability_report_noncanonical")?;
    req(
        identity::verify(&statement, &record["signature"], key).is_ok(),
        "capability_report_signature_invalid",
    )?;
    req(now <= report.expires_at, "capability_report_expired")?;
    Ok(report)
}
