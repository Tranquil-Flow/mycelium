// SPDX-License-Identifier: AGPL-3.0-only
//! `installation_descriptor.installation_descriptor` (`installation_descriptor.py:15-52 @ f4e366e9`): the consented
//! installation facts a Mac signs for one run, as the owner's local contributor (and, later, a joined Mac) reports
//! them to the owner. The signature is the managed-control envelope over the canonical statement; the statement's
//! paths disclose where this install keeps its stage and cache, they never authorise a command.
use budgets::{BudgetId, Entries};
use protocol::{canonical, identity::Identity, managed::SIGNED_ENVELOPE_PROTOCOL};
use serde_json::{Value, json};

pub const PROTOCOL: &str = "mycelium.a13.installation_descriptor.v1";

/// Authenticate installation metadata, never native hardware or execution authority.
/// Check order and refusals: `installation_descriptor.py:77-120 @ f4e366e9`.
pub fn verify_installation_descriptor(
    card: &Value,
    member: &Value,
    swarm: &str,
    run: &str,
    deployment: &str,
    now: f64,
) -> Result<Value, Failure> {
    use base64::Engine;
    // Match Python b64decode(validate=True), as the shared identity verifier does: unused pad bits
    // need not be zero. The signature and digest have already authenticated the decoded key.
    let decoder = base64::engine::GeneralPurpose::new(
        &base64::alphabet::STANDARD,
        base64::engine::GeneralPurposeConfig::new().with_decode_allow_trailing_bits(true),
    );
    let fail = |code: &str| Failure::new("InstallationDescriptorError", code);
    let malformed = || fail("installation_descriptor_malformed");
    let check = |ok: bool, code: &str| if ok { Ok(()) } else { Err(fail(code)) };
    // Python json.dumps defaults (ASCII escaping and spaced separators), not compact UTF-8 bytes.
    check(
        crate::preferences::dumps_sorted(card).len() <= 16_384,
        "installation_descriptor_malformed",
    )?;
    let key = card
        .get("verification_key")
        .filter(|v| v.is_object())
        .ok_or_else(malformed)?;
    let digest = member
        .get("verification_key_digest")
        .ok_or_else(malformed)?;
    check(
        key.get("verification_key_digest").ok_or_else(malformed)? == digest,
        "installation_descriptor_key_mismatch",
    )?;
    let lease = member
        .get("lease_expires_at")
        .and_then(Value::as_f64)
        .ok_or_else(malformed)?;
    check(now < lease, "installation_descriptor_member_lease_expired")?;
    let endpoint = member
        .get("endpoint_id")
        .and_then(Value::as_str)
        .ok_or_else(malformed)?;
    let seconds = |id: BudgetId| {
        id.span(Entries::ZERO)
            .map(|v| v.millis() as f64 / 1000.0)
            .map_err(|_| malformed())
    };
    let statement = protocol::managed::verify(
        card,
        PROTOCOL,
        key,
        endpoint,
        now,
        protocol::TimeLimits {
            max_ttl_seconds: seconds(BudgetId::ControlTtl)?,
            clock_skew_seconds: seconds(BudgetId::ControlClockSkew)?,
        },
    )
    .map_err(|error| {
        fail(match error.0 {
            "managed_control_message_expired" => "installation_descriptor_expired",
            "managed_control_message_from_future" => "installation_descriptor_from_future",
            "managed_control_key_pin_mismatch" => "installation_descriptor_key_mismatch",
            "managed_control_signature_invalid" => "installation_descriptor_signature_invalid",
            _ => "installation_descriptor_malformed",
        })
    })?;
    let node = member
        .get("node_id")
        .and_then(Value::as_str)
        .ok_or_else(malformed)?;
    let incarnation = member
        .get("incarnation")
        .and_then(Value::as_str)
        .ok_or_else(malformed)?;
    let generation = member
        .get("generation")
        .and_then(Value::as_u64)
        .ok_or_else(malformed)?;
    check(
        is_segment(node)
            && is_segment(endpoint)
            && is_segment(incarnation)
            && is_segment(run)
            && is_segment(deployment)
            && generation > 0
            && digest.as_str().is_some_and(is_digest),
        "installation_descriptor_malformed",
    )?;
    let binding = json!({"target_node_id":node,"target_endpoint_id":endpoint,
        "target_key_digest":digest,"membership_generation":generation,
        "member_incarnation":incarnation,"run_id":run,"deployment_id":deployment});
    let fields = ["protocol", "swarm_id", "issued_at", "expires_at", "facts"];
    let object = statement.as_object().ok_or_else(malformed)?;
    check(
        object.len() == binding.as_object().ok_or_else(malformed)?.len() + fields.len()
            && fields.iter().all(|k| object.contains_key(*k))
            && binding
                .as_object()
                .ok_or_else(malformed)?
                .keys()
                .all(|k| object.contains_key(k)),
        "installation_descriptor_malformed",
    )?;
    check(
        statement["membership_generation"] == binding["membership_generation"]
            && statement["member_incarnation"] == binding["member_incarnation"],
        "installation_descriptor_generation_mismatch",
    )?;
    check(
        statement["swarm_id"] == swarm
            && binding
                .as_object()
                .ok_or_else(malformed)?
                .iter()
                .all(|(k, v)| &statement[k] == v),
        "installation_descriptor_binding_mismatch",
    )?;
    let mut facts = statement["facts"]
        .as_object()
        .cloned()
        .ok_or_else(|| fail("installation_descriptor_facts_invalid"))?;
    let fact_fields = [
        "node_id",
        "process_transport",
        "ssh_target",
        "ssh_identity_file",
        "staging_root",
        "python_executable",
        "sidecar_binary",
        "endpoint_secret_file",
        "endpoint_id",
        "membership_generation",
        "runtime_backend",
        "source_root",
        "manifest_digest",
        "route_ready",
    ];
    check(
        facts.len() == fact_fields.len() && fact_fields.iter().all(|k| facts.contains_key(*k)),
        "installation_descriptor_facts_invalid",
    )?;
    check(
        facts["membership_generation"] == generation,
        "installation_descriptor_generation_mismatch",
    )?;
    check(
        facts["node_id"] == node && facts["endpoint_id"] == endpoint,
        "installation_descriptor_binding_mismatch",
    )?;
    let backend = match member.get("peer_class").and_then(Value::as_str) {
        None if member.get("peer_class").is_none() => "mlx",
        Some("mac_mlx_iroh") => "mlx",
        Some("windows_native_iroh" | "linux_native_iroh") => "numpy",
        _ => return Err(malformed()),
    };
    check(
        facts["runtime_backend"] == backend
            && facts["process_transport"] == "managed_https"
            && facts["ssh_target"].is_null()
            && facts["ssh_identity_file"].is_null()
            && facts["route_ready"] == Value::Bool(false),
        "installation_descriptor_facts_invalid",
    )?;
    for field in [
        "staging_root",
        "python_executable",
        "sidecar_binary",
        "endpoint_secret_file",
        "source_root",
    ] {
        check(
            facts[field].as_str().is_some_and(|p| {
                p.starts_with('/')
                    && !p.split('/').any(|part| part == "..")
                    && !p.chars().any(|c| c < ' ')
            }),
            "installation_descriptor_facts_invalid",
        )?;
    }
    let activation = decoder
        .decode(key["verification_key"].as_str().ok_or_else(malformed)?)
        .map_err(|_| malformed())?;
    check(activation.len() == 32, "installation_descriptor_malformed")?;
    let hex: String = activation.iter().map(|b| format!("{b:02x}")).collect();
    facts.insert("activation_endpoint_id".into(), Value::String(hex));
    Ok(Value::Object(facts))
}

/// A Python exception as the agent sees it: its class and text (`.code`/`str(error)`), nothing else survives.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Failure {
    pub class: &'static str,
    pub code: String,
}
impl Failure {
    pub fn new(class: &'static str, code: &str) -> Self {
        Self {
            class,
            code: code.to_owned(),
        }
    }
}
fn value_error(code: &str) -> Failure {
    Failure::new("ValueError", code)
}
fn managed(code: &str) -> Failure {
    Failure::new("ManagedControlError", code)
}

/// `target.staging_control_snapshot()`: the member's current binding.
#[derive(Clone, Debug)]
pub struct Member {
    pub node_id: String,
    pub endpoint_id: String,
    pub verification_key_digest: String,
    pub generation: u64,
    pub incarnation: String,
}

/// Where the model bytes the descriptor names live.
#[derive(Clone, Debug)]
pub enum Source {
    /// `prepared_source()`: the owner's whole snapshot.
    Whole {
        source_root: String,
        manifest_digest: String,
    },
    /// `slice_source=`: the member's empty per-run slice root (slice mode).
    Slice {
        source_root: String,
        manifest_digest: String,
    },
}

/// The paths this Mac discloses.
#[derive(Clone, Debug)]
pub struct Paths {
    /// `actor.peer.state_root`.
    pub state_root: String,
    /// `actor._endpoint_identity_file(member)`.
    pub endpoint_secret_file: String,
    /// `str(Path(sys.executable).resolve())`: this install's interpreter; the Rust host names its own executable.
    pub python_executable: String,
    /// `installed_sidecar()`.
    pub sidecar_binary: String,
}

/// `_SEGMENT_RE = ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`.
pub fn is_segment(text: &str) -> bool {
    let mut chars = text.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && text.len() <= 128
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-'))
}
/// `_DIGEST_RE = ^sha256:[0-9a-f]{64}$`.
fn is_digest(text: &str) -> bool {
    text.strip_prefix("sha256:").is_some_and(|hex| {
        hex.len() == 64 && hex.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
    })
}
fn absolute(path: &str) -> bool {
    path.starts_with('/')
}

fn ttl_seconds() -> f64 {
    BudgetId::PyQuantityMyceliumA13MacosInstallationDescriptorL59C56
        .span(Entries::ZERO)
        .map_or(f64::NAN, |s| s.millis() as f64 / 1000.0)
}

/// The slice source must be an absolute root and a `sha256:` manifest digest (`installation_slice_source_invalid`).
fn checked_source(source: &Source) -> Result<(String, String), Failure> {
    match source {
        Source::Whole {
            source_root,
            manifest_digest,
        } => Ok((source_root.clone(), manifest_digest.clone())),
        Source::Slice {
            source_root,
            manifest_digest,
        } => {
            if !absolute(source_root) || !manifest_digest.starts_with("sha256:") {
                return Err(value_error("installation_slice_source_invalid"));
            }
            Ok((source_root.clone(), manifest_digest.clone()))
        }
    }
}

/// Builds and signs the descriptor. Check order is the oracle's: consent, the two ids, the source, the binding.
#[allow(clippy::too_many_arguments)]
pub fn installation_descriptor(
    signer: &Identity,
    member: &Member,
    swarm_id: &str,
    run_id: &str,
    deployment_id: &str,
    acknowledged: bool,
    source: &Source,
    paths: &Paths,
    now: f64,
) -> Result<Value, Failure> {
    if !acknowledged {
        return Err(value_error("installation_disclosure_consent_required"));
    }
    if !is_segment(run_id) || !is_segment(deployment_id) {
        return Err(managed("managed_control_identifier_invalid"));
    }
    let (source_root, manifest_digest) = checked_source(source)?;
    // `_binding_from_member` then `_validate_binding`.
    let binding_invalid = || managed("managed_control_member_binding_invalid");
    if !is_segment(&member.node_id)
        || !is_segment(&member.endpoint_id)
        || !is_segment(&member.incarnation)
        || !is_digest(&member.verification_key_digest)
        || member.generation < 1
    {
        return Err(binding_invalid());
    }
    let statement = json!({
        "protocol": PROTOCOL,
        "target_node_id": member.node_id,
        "target_endpoint_id": member.endpoint_id,
        "target_key_digest": member.verification_key_digest,
        "membership_generation": member.generation,
        "member_incarnation": member.incarnation,
        "run_id": run_id,
        "deployment_id": deployment_id,
        "swarm_id": swarm_id,
        "issued_at": now,
        "expires_at": now + ttl_seconds(),
        "facts": {
            "node_id": member.node_id,
            "endpoint_id": member.endpoint_id,
            "membership_generation": member.generation,
            "staging_root": format!("{}/managed/{run_id}/mycelium-stage", paths.state_root),
            "python_executable": paths.python_executable,
            "sidecar_binary": paths.sidecar_binary,
            "endpoint_secret_file": paths.endpoint_secret_file,
            "process_transport": "managed_https",
            "ssh_target": Value::Null,
            "ssh_identity_file": Value::Null,
            "runtime_backend": "mlx",
            "source_root": source_root,
            "manifest_digest": manifest_digest,
            "route_ready": false,
        },
    });
    // `_detached`: the canonical round trip (also what the signature covers).
    let detached: Value = canonical::to_bytes(&statement)
        .ok()
        .and_then(|raw| serde_json::from_slice(&raw).ok())
        .ok_or_else(|| managed("managed_control_json_invalid"))?;
    let signature = signer
        .sign(&detached)
        .map_err(|_| managed("managed_control_signer_invalid"))?;
    Ok(json!({
        "protocol": SIGNED_ENVELOPE_PROTOCOL,
        "statement": detached,
        "signature": signature,
        "verification_key": signer.public_key_record(),
    }))
}
