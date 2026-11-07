// SPDX-License-Identifier: AGPL-3.0-only
//! Pinned gossip evidence reconstruction. No registry, clock, liveness, replay or
//! network ownership. Derived eligibility is evidence, not admission authority.
pub(crate) mod repr;
mod schema;
mod views;
use crate::{
    ContractError, canonical, capacity, checks::*, execution_graph::truthy, identity,
    public::fields,
};
/// Python `repr(str)` under the pinned Unicode printability table.
pub use repr::string as python_str_repr;
use schema::{array, nonempty};
use serde_json::{Value, json};
use std::cmp::Ordering;
const C: &str = "gossip_evidence_invalid";
pub const BUNDLE_PROTOCOL: &str = "mycelium.gossip.evidence_bundle.v1";
pub const SIGNED_PROTOCOL: &str = "mycelium.gossip.signed_evidence_bundle.v1";
/// Caller-owned source wire hard cap. TODO(L1): register the gossip record cap;
/// this is not a lease or operational expiry default.
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    pub max_record_ttl_ms: u64,
}
fn cmp_int(a: &Value, b: &Value) -> Ordering {
    let a = a.to_string();
    let b = b.to_string();
    a.len().cmp(&b.len()).then_with(|| a.cmp(&b))
}
fn add_int(a: &Value, b: &Value) -> Result<Value, ContractError> {
    require(integer(a, 0) && integer(b, 0), C)?;
    let a = a.to_string();
    let b = b.to_string();
    let mut a = a.bytes().rev();
    let mut b = b.bytes().rev();
    let mut digits = Vec::new();
    let mut carry = 0;
    loop {
        let x = a.next();
        let y = b.next();
        if x.is_none() && y.is_none() && carry == 0 {
            break;
        }
        let n = x.map_or(0, |n| n - b'0') + y.map_or(0, |n| n - b'0') + carry;
        digits.push(b'0' + n % 10);
        carry = n / 10;
    }
    digits.reverse();
    serde_json::from_slice(&digits).map_err(|_| ContractError(C))
}
/// Oracle `EvidenceBundleError` texts (`mycelium_gossip/evidence_bundle.py`), one per
/// failed field, so callers that surface the source refusal observe the same message.
const M_KEYS: &str = "evidence bundle fields are incomplete or unexpected";
const M_PROTOCOL: &str = "expected mycelium.gossip.evidence_bundle.v1";
const M_SWARM: &str = "swarm_id must be non-empty";
const M_GENERATION: &str = "snapshot_generation must be a non-negative integer";
const M_DEPLOYMENT: &str = "deployment must contain deployment_id and deployment_epoch";
const M_UUID: &str = "deployment_id must be a canonical UUID";
const M_EPOCH: &str = "deployment_epoch must be a non-negative integer";
const M_MODEL: &str = "model identity fields are incomplete";
const M_MODEL_ID: &str = "model_id must be non-empty";
const M_LAYERS: &str = "num_layers must be a positive integer";
const M_MANIFEST: &str = "manifest_digest must be sha256:<64 lowercase hex>";
const M_COMMIT: &str = "resolved_commit must be immutable 40-hex";
const M_RECORDS: &str = "records must be an array";
const M_RECORD_OBJECT: &str = "record must be an object";
const M_RECORD: &str = "record evidence is invalid";
const M_RECORD_SWARM: &str = "record swarm_id does not match bundle";
const M_RECORD_DUPLICATE: &str = "duplicate logical record in evidence bundle";
const M_RECORD_ORDER: &str = "records must be sorted by logical key";
const M_PEERS: &str = "peer_states must be an array";
const M_PEER_SHAPE: &str = "peer state shape is invalid";
const M_PEER_IDENTITY: &str = "peer identity is invalid";
const M_PEER_INCARNATION: &str = "peer incarnation is invalid";
const M_PEER_LIVENESS: &str = "peer liveness_present must be boolean";
const M_PEER_STATE: &str = "peer state is invalid";
const M_PEER_ORDER: &str = "peer_states must be unique and sorted";
const M_QUARANTINES: &str = "quarantines must be an array";
const M_QUARANTINE_SHAPE: &str = "quarantine shape is invalid";
const M_QUARANTINE_KEY: &str = "quarantine key is invalid";
const M_QUARANTINE_OBSERVATION: &str = "quarantine observation is invalid";
const M_QUARANTINE_SCOPE: &str = "quarantine scope does not match observation";
const M_QUARANTINE_ORDER: &str = "quarantines must be unique and sorted";
const M_ROUTER: &str = "router_view is not derived from bound evidence";
const M_ALLOCATOR: &str = "allocator_view is not derived from bound evidence";
const M_DIGEST: &str = "evidence_bundle_digest mismatch";
const OBSERVATION_REQUIRED: &str = "route_id route_generation src_node_id src_endpoint_id dst_node_id dst_endpoint_id failure_kind scope probe_correlation_id";
/// `FailureObservation(...)` construction from the wire object (KeyError/TypeError/ValueError
/// all become one refusal); `offering_id` alone is optional (`.get`).
fn observation_constructible(q_scope: &Value, o: &Value) -> bool {
    let scopes = ["peer", "offering", "edge"];
    let Some(m) = o.as_object() else { return false };
    if !OBSERVATION_REQUIRED
        .split_whitespace()
        .all(|f| m.contains_key(f))
        || !one_of(q_scope, &scopes)
        || !one_of(&o["scope"], &scopes)
    {
        return false;
    }
    let nonnegative = match &o["route_generation"] {
        Value::Bool(_) => true,
        n => n.as_f64().is_some_and(|n| n >= 0.0),
    };
    nonnegative
        && [
            "route_id",
            "src_node_id",
            "dst_node_id",
            "failure_kind",
            "probe_correlation_id",
        ]
        .iter()
        .all(|f| truthy(&o[*f]))
        && (o["scope"] != "edge"
            || (truthy(&o["src_endpoint_id"]) && truthy(&o["dst_endpoint_id"])))
        && (o["scope"] != "offering" || truthy(&o["offering_id"]))
}
fn validate_input(v: &Value, limits: Limits) -> Result<(), ContractError> {
    require(limits.max_record_ttl_ms > 0, C)?;
    fields(
        v,
        "protocol swarm_id deployment model snapshot_generation records peer_states quarantines",
        M_KEYS,
    )?;
    require(v["protocol"] == BUNDLE_PROTOCOL, M_PROTOCOL)?;
    require(nonempty(&v["swarm_id"]), M_SWARM)?;
    require(integer(&v["snapshot_generation"], 0), M_GENERATION)?;
    fields(
        &v["deployment"],
        "deployment_id deployment_epoch",
        M_DEPLOYMENT,
    )?;
    let id = v["deployment"]["deployment_id"].as_str().unwrap_or("");
    require(
        id.len() == 36
            && id.bytes().enumerate().all(|(i, b)| {
                if [8, 13, 18, 23].contains(&i) {
                    b == b'-'
                } else {
                    b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
                }
            }),
        M_UUID,
    )?;
    require(integer(&v["deployment"]["deployment_epoch"], 0), M_EPOCH)?;
    fields(
        &v["model"],
        "model_id num_layers manifest_digest resolved_commit",
        M_MODEL,
    )?;
    require(nonempty(&v["model"]["model_id"]), M_MODEL_ID)?;
    require(integer(&v["model"]["num_layers"], 1), M_LAYERS)?;
    require(digest(&v["model"]["manifest_digest"]), M_MANIFEST)?;
    require(
        schema::hex(&v["model"]["resolved_commit"], 40, 40),
        M_COMMIT,
    )?;
    let records = v["records"].as_array().ok_or(ContractError(M_RECORDS))?;
    let mut keys: Vec<Vec<String>> = Vec::new();
    for r in records {
        require(r.is_object(), M_RECORD_OBJECT)?;
        let key = schema::record(r, limits).map_err(|_| ContractError(M_RECORD))?;
        require(r["swarm_id"] == v["swarm_id"], M_RECORD_SWARM)?;
        require(!keys.contains(&key), M_RECORD_DUPLICATE)?;
        keys.push(key);
    }
    require(keys.windows(2).all(|w| w[0] < w[1]), M_RECORD_ORDER)?;
    let peers = v["peer_states"].as_array().ok_or(ContractError(M_PEERS))?;
    for p in peers {
        fields(
            p,
            "node_id incarnation boot_id state liveness_present",
            M_PEER_SHAPE,
        )?;
        require(
            nonempty(&p["node_id"]) && nonempty(&p["boot_id"]),
            M_PEER_IDENTITY,
        )?;
        require(integer(&p["incarnation"], 1), M_PEER_INCARNATION)?;
        require(p["liveness_present"].is_boolean(), M_PEER_LIVENESS)?;
        require(
            one_of(
                &p["state"],
                &["alive", "suspect", "dead", "identity_conflict"],
            ),
            M_PEER_STATE,
        )?;
    }
    let ids: Vec<&str> = peers.iter().filter_map(|p| p["node_id"].as_str()).collect();
    require(ids.windows(2).all(|w| w[0] < w[1]), M_PEER_ORDER)?;
    let quarantines = v["quarantines"]
        .as_array()
        .ok_or(ContractError(M_QUARANTINES))?;
    let mut keys: Vec<Vec<&str>> = Vec::new();
    // Wire-shape drift that the oracle only detects when it re-serializes the entries.
    let mut noncanonical = false;
    for q in quarantines {
        fields(q, "key scope observation", M_QUARANTINE_SHAPE)?;
        let key = q["key"]
            .as_array()
            .filter(|k| k.iter().all(nonempty))
            .ok_or(ContractError(M_QUARANTINE_KEY))?;
        keys.push(key.iter().filter_map(Value::as_str).collect());
        let o = &q["observation"];
        require(o.is_object(), M_QUARANTINE_OBSERVATION)?;
        require(
            observation_constructible(&q["scope"], o),
            M_QUARANTINE_OBSERVATION,
        )?;
        require(q["scope"] == o["scope"], M_QUARANTINE_SCOPE)?;
        noncanonical |= o
            .as_object()
            .is_none_or(|m| m.len() != 10 || !m.contains_key("offering_id"));
    }
    require(
        !noncanonical && keys.windows(2).all(|w| w[0] < w[1]),
        M_QUARANTINE_ORDER,
    )?;
    Ok(())
}
/// Reconstruct both registered views and the bundle digest from independently
/// supplied sorted record/peer/quarantine evidence. Input has the eight bundle
/// source fields, excluding `router_view`, `allocator_view`, and its digest.
pub fn build_bundle(input: &Value, limits: Limits) -> Result<Value, ContractError> {
    validate_input(input, limits)?;
    let (router, allocator) = views::derive(input)?;
    let mut out = input.clone();
    out["router_view"] = router;
    out["allocator_view"] = allocator;
    out["evidence_bundle_digest"] = canonical::digest(&out)
        .map_err(|_| ContractError(C))?
        .into();
    Ok(out)
}
/// Validate complete bound evidence, not only the top-level self-digest. Returned
/// views are freshly derived; numeric equality follows the source comparisons.
pub fn parse_bundle(v: &Value, limits: Limits) -> Result<Value, ContractError> {
    fields(
        v,
        "protocol swarm_id deployment model snapshot_generation records peer_states quarantines router_view allocator_view evidence_bundle_digest",
        M_KEYS,
    )?;
    let mut source = v.clone();
    let m = source.as_object_mut().ok_or(ContractError(C))?;
    for f in ["router_view", "allocator_view", "evidence_bundle_digest"] {
        m.remove(f).ok_or(ContractError(C))?;
    }
    let mut expected = build_bundle(&source, limits)?;
    require(
        python_equal(&v["router_view"], &expected["router_view"]),
        M_ROUTER,
    )?;
    require(
        python_equal(&v["allocator_view"], &expected["allocator_view"]),
        M_ALLOCATOR,
    )?;
    let mut unsigned = v.clone();
    unsigned
        .as_object_mut()
        .ok_or(ContractError(C))?
        .remove("evidence_bundle_digest")
        .ok_or(ContractError(C))?;
    require(
        v["evidence_bundle_digest"]
            == canonical::digest(&unsigned).map_err(|_| ContractError(C))?,
        M_DIGEST,
    )?;
    expected["evidence_bundle_digest"] = v["evidence_bundle_digest"].clone();
    Ok(expected)
}
/// Full link payload grammar and recursive secret-field exclusion; the origin is
/// independently supplied by the record/transport owner.
pub fn validate_link(v: &Value, origin: &str) -> Result<(), ContractError> {
    schema::link(v, origin)
}
/// Authenticated immutable evidence. Does not create a current-membership or
/// authority-generation capability: consumers must bind their own state.
#[derive(Clone, Debug)]
pub struct VerifiedEvidence {
    value: Value,
}
impl VerifiedEvidence {
    pub fn to_value(&self) -> Value {
        self.value.clone()
    }
    pub fn bundle(&self) -> &Value {
        &self.value["bundle"]
    }
    pub fn statement(&self) -> &Value {
        &self.value["statement"]
    }
}
fn capacity_documents(v: &Value) -> Result<Value, ContractError> {
    let values = v.as_object().ok_or(ContractError(C))?;
    require(!values.is_empty(), C)?;
    let mut profiles = json!({});
    for (id, p) in values {
        require(!id.is_empty() && id.chars().count() <= 128, C)?;
        profiles[id] = capacity::parse(&canonical::to_bytes(p).map_err(|_| ContractError(C))?)
            .map_err(|_| ContractError(C))?;
    }
    Ok(profiles)
}
/// Seal already validated source evidence using the shared identity signer. All
/// times and lifetime are caller supplied; this does not grant signing authority.
pub fn seal_bundle(
    bundle: &Value,
    signer: &identity::Identity,
    captured: &Value,
    lifetime: &Value,
    generation: &Value,
    capacity_profiles: &Value,
    limits: Limits,
) -> Result<Value, ContractError> {
    let bundle = parse_bundle(bundle, limits)?;
    require(
        integer(captured, 0) && integer(lifetime, 1) && integer(generation, 1),
        C,
    )?;
    let until = add_int(captured, lifetime)?;
    require(
        cmp_int(&until, &json!(9_007_199_254_740_991u64)) != Ordering::Greater,
        C,
    )?;
    let profiles = capacity_documents(capacity_profiles)?;
    let statement = json!({"kind":"mycelium.gossip.evidence_bundle_statement.v1","swarm_id":bundle["swarm_id"],"deployment_id":bundle["deployment"]["deployment_id"],"deployment_epoch":bundle["deployment"]["deployment_epoch"],"snapshot_generation":bundle["snapshot_generation"],"evidence_bundle_digest":bundle["evidence_bundle_digest"],"capacity_profiles_digest":canonical::digest(&profiles).map_err(|_|ContractError(C))?,"captured_at_unix_ms":captured,"valid_until_unix_ms":until,"authority_generation":generation,"signer_endpoint_id":signer.endpoint_id()});
    Ok(
        json!({"protocol":SIGNED_PROTOCOL,"signature":signer.sign(&statement).map_err(|_|ContractError(C))?,"verification_key":signer.public_key_record(),"statement":statement,"evidence_bundle":bundle,"capacity_profiles":profiles}),
    )
}
/// Pin trust independently; never pass the key digest read from the untrusted
/// envelope as the expected digest. Freshness is [captured, valid_until).
pub fn verify_signed(
    v: &Value,
    expected_key_digest: &str,
    now: &Value,
    limits: Limits,
) -> Result<VerifiedEvidence, ContractError> {
    // Each refusal text is the exact `SignedEvidenceBundleError` the v0.1.4 validator
    // raises, in its check order (it reaches the operator's CLI stderr).
    let refuse = |text: &'static str| ContractError(text);
    fields(
        v,
        "protocol statement evidence_bundle capacity_profiles signature verification_key",
        C,
    )
    .map_err(|_| refuse("signed evidence bundle shape is invalid"))?;
    require(
        v["protocol"] == SIGNED_PROTOCOL,
        "signed evidence bundle protocol is invalid",
    )?;
    let s = &v["statement"];
    fields(
        s,
        "kind swarm_id deployment_id deployment_epoch snapshot_generation evidence_bundle_digest capacity_profiles_digest captured_at_unix_ms valid_until_unix_ms authority_generation signer_endpoint_id",
        C,
    )
    .map_err(|_| refuse("signed evidence statement shape is invalid"))?;
    require(
        s["kind"] == "mycelium.gossip.evidence_bundle_statement.v1",
        "signed evidence statement kind is invalid",
    )?;
    require(
        integer(&s["captured_at_unix_ms"], 0),
        "captured_at_unix_ms must be an exact non-negative integer",
    )?;
    require(
        integer(&s["valid_until_unix_ms"], 1),
        "valid_until_unix_ms must be an exact positive integer",
    )?;
    require(
        integer(&s["authority_generation"], 1),
        "authority_generation must be an exact positive integer",
    )?;
    require(
        integer(now, 0),
        "now_unix_ms must be an exact non-negative integer",
    )?;
    require(
        cmp_int(&s["valid_until_unix_ms"], &s["captured_at_unix_ms"]) == Ordering::Greater
            && cmp_int(now, &s["captured_at_unix_ms"]) != Ordering::Less
            && cmp_int(now, &s["valid_until_unix_ms"]) == Ordering::Less,
        "signed evidence bundle is not current",
    )?;
    require(
        v["verification_key"].is_object() && v["signature"].is_object(),
        "signed evidence authority is invalid",
    )?;
    require(
        v["verification_key"]["verification_key_digest"] == expected_key_digest,
        "signed evidence authority is not trusted",
    )?;
    require(
        v["signature"]["verification_key_digest"] == expected_key_digest
            && v["signature"]["signer_endpoint_id"] == s["signer_endpoint_id"],
        "signed evidence source binding is invalid",
    )?;
    identity::verify(s, &v["signature"], &v["verification_key"])
        .map_err(|_| refuse("signed evidence signature is invalid"))?;
    let bundle = parse_bundle(&v["evidence_bundle"], limits)
        .map_err(|_| refuse("signed evidence bundle payload is invalid"))?;
    let profiles = capacity_documents(&v["capacity_profiles"])
        .map_err(|_| refuse("signed capacity profiles are invalid"))?;
    require(
        python_equal(&profiles, &v["capacity_profiles"]),
        "signed capacity profiles are not canonical",
    )?;
    let digest = canonical::digest(&profiles).map_err(|_| ContractError(C))?;
    let expected = json!({"kind":"mycelium.gossip.evidence_bundle_statement.v1","swarm_id":bundle["swarm_id"],"deployment_id":bundle["deployment"]["deployment_id"],"deployment_epoch":bundle["deployment"]["deployment_epoch"],"snapshot_generation":bundle["snapshot_generation"],"evidence_bundle_digest":bundle["evidence_bundle_digest"],"capacity_profiles_digest":digest,"captured_at_unix_ms":s["captured_at_unix_ms"],"valid_until_unix_ms":s["valid_until_unix_ms"],"authority_generation":s["authority_generation"],"signer_endpoint_id":s["signer_endpoint_id"]});
    require(
        python_equal(s, &expected),
        "signed evidence statement does not bind its bundle",
    )?;
    for r in array(&bundle["records"])? {
        require(
            cmp_int(&r["generated_at_unix_ms"], &s["captured_at_unix_ms"]) != Ordering::Greater
                && cmp_int(
                    &s["captured_at_unix_ms"],
                    &add_int(&r["generated_at_unix_ms"], &r["ttl_ms"])?,
                ) == Ordering::Less,
            "signed evidence bundle contains stale records",
        )?;
    }
    Ok(VerifiedEvidence {
        value: json!({"bundle":bundle,"capacity_profiles":profiles,"statement":s,"verification_key":v["verification_key"]}),
    })
}
