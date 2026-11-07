// SPDX-License-Identifier: AGPL-3.0-only
//! Signed node observations and exact cancellation receipt predicates.
//! The caller owns current process/member/lease authority and participant quorum.
use crate::{ContractError, checks::*, identity};
use serde_json::Value;
/// Independently pinned context, never copied out of an untrusted receipt.
pub struct ExpectedObservation<'a> {
    pub event: &'a str,
    pub node_id: &'a str,
    pub host_id: &'a str,
    pub process_id: &'a Value,
    pub run_id: &'a str,
    pub deployment_id: &'a str,
    pub endpoint_id: &'a str,
    pub key: &'a Value,
}
/// Only successful signature and binding verification can construct this value.
pub struct VerifiedObservation {
    observation: Value,
    signed_bytes: Vec<u8>,
    digest: String,
}
/// One authenticated node assertion, bound to the caller's exact subject.
/// This record is sealable evidence, not aggregate cleanup/release authority.
#[derive(Debug)]
pub struct VerifiedCancelReceipt {
    subject: Value,
    signed_observation_bytes: Vec<u8>,
    observation_digest: String,
}
impl VerifiedCancelReceipt {
    pub fn subject(&self) -> &Value {
        &self.subject
    }
    pub fn signed_observation_bytes(&self) -> &[u8] {
        &self.signed_observation_bytes
    }
    pub fn observation_digest(&self) -> &str {
        &self.observation_digest
    }
}
impl VerifiedObservation {
    pub fn cancel_receipt(&self, subject: &Value) -> Result<VerifiedCancelReceipt, ContractError> {
        self.require_cancel_receipt(subject)?;
        Ok(VerifiedCancelReceipt {
            subject: subject.clone(),
            signed_observation_bytes: self.signed_bytes.clone(),
            observation_digest: self.digest.clone(),
        })
    }
    /// Mirrors phone_stage_session.require_cancel_receipt. This proves only one
    /// authenticated node's assertion for a caller-owned exact subject. It does
    /// not observe runtime residue, aggregate participants or authorize release.
    pub fn require_cancel_receipt(&self, subject: &Value) -> Result<(), ContractError> {
        let c = "external_node_cancel_receipt_unsupported";
        let expected = subject.as_object().ok_or(ContractError(c))?;
        let receipt = &self.observation["details"]["request_cleanup"];
        require(
            receipt.is_object()
                && expected.iter().all(|(k, v)| receipt.get(k) == Some(v))
                && [
                    "runtime_clean",
                    "transport_clean",
                    "cancellation_worker_complete",
                    "complete",
                ]
                .iter()
                .all(|k| receipt[*k].as_bool() == Some(true)),
            c,
        )
    }
}
/// What v0.1.4's `ControllerError` carries beyond the code: the persisted operator-facing
/// `diagnostic=` string and the node's own rejection code. `str(exc)` stays `code`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ObservationRefusal {
    pub code: &'static str,
    pub remote_code: Option<String>,
    pub diagnostic: Option<String>,
}

/// Python `isinstance(x, int) and not bool and x >= 0` over a JSON value (`-0` is the int 0).
fn py_nonneg_int(v: &Value) -> bool {
    py_int(v).is_some_and(|(negative, zero)| !negative || zero)
}
/// `(negative, all-zero)` for a JSON integer token, `None` for anything Python would not call `int`.
fn py_int(v: &Value) -> Option<(bool, bool)> {
    let s = v.as_number()?.to_string();
    let digits = s.strip_prefix('-').unwrap_or(&s);
    (!digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit()))
        .then(|| (s.starts_with('-'), digits.bytes().all(|b| b == b'0')))
}

/// v0.1.4 `_observation_diagnostic`: the fixed predicate labels that failed, in the oracle's order.
fn observation_failures(
    observation: &Value,
    signature: &Value,
    key: &Value,
    expected: &ExpectedObservation<'_>,
) -> Vec<&'static str> {
    const FIELDS: [&str; 13] = [
        "protocol",
        "event",
        "monotonic_ns",
        "run_id",
        "deployment_id",
        "node_id",
        "host_id",
        "process_id",
        "endpoint_id",
        "peer_generation",
        "state",
        "route_ready",
        "details",
    ];
    let mut failures = Vec::new();
    match observation.as_object() {
        None => failures.push("observation_shape"),
        Some(o) => {
            if o.len() != FIELDS.len() || !FIELDS.iter().all(|f| o.contains_key(*f)) {
                failures.push("observation_fields");
            }
            let get = |name: &str| o.get(name).unwrap_or(&Value::Null);
            let text = |s: &str| Value::String(s.to_owned());
            for (name, want) in [
                ("protocol", text("mycelium.physical_node_observation.v1")),
                ("event", text(expected.event)),
                ("run_id", text(expected.run_id)),
                ("deployment_id", text(expected.deployment_id)),
                ("node_id", text(expected.node_id)),
                ("host_id", text(expected.host_id)),
                ("process_id", expected.process_id.clone()),
                ("endpoint_id", text(expected.endpoint_id)),
            ] {
                if !python_equal(get(name), &want) {
                    failures.push(name);
                }
            }
            if !py_nonneg_int(get("monotonic_ns")) {
                failures.push("monotonic_ns");
            }
            if get("route_ready").as_bool() != Some(false) {
                failures.push("route_ready");
            }
            if py_int(get("peer_generation")).is_none() {
                failures.push("peer_generation");
            }
            if !get("state").is_string() {
                failures.push("state");
            }
            if !get("details").is_object() {
                failures.push("details");
            }
        }
    }
    match signature.as_object() {
        None => failures.push("signature_shape"),
        Some(s) => {
            if !python_equal(
                s.get("signer_endpoint_id").unwrap_or(&Value::Null),
                &Value::String(expected.endpoint_id.to_owned()),
            ) {
                failures.push("signer_endpoint_id");
            }
        }
    }
    if !key.is_object() {
        failures.push("verification_key_shape");
    } else if !python_equal(key, expected.key) {
        failures.push("verification_key_mismatch");
    }
    failures
}

/// `_verified_observation` with everything v0.1.4's `ControllerError` carries: the code, the node's
/// own (segment-shaped) rejection code, and the persisted `diagnostic=` string.
pub fn verify_detailed(
    response: &Value,
    expected: &ExpectedObservation<'_>,
) -> Result<VerifiedObservation, ObservationRefusal> {
    let refuse = |code: &'static str, diagnostic: Option<String>| ObservationRefusal {
        code,
        remote_code: None,
        diagnostic,
    };
    if response["ok"].as_bool() != Some(true) || !response["result"].is_object() {
        let remote_code = response["error"]["code"]
            .as_str()
            .filter(|_| response["error"].is_object())
            .filter(|_| segment(&response["error"]["code"]))
            .map(str::to_owned);
        return Err(ObservationRefusal {
            code: "node_command_rejected",
            remote_code,
            diagnostic: None,
        });
    }
    let signed = &response["result"];
    let invalid = "node_observation_invalid";
    if exact(
        signed,
        &["observation", "signature", "verification_key"],
        invalid,
    )
    .is_err()
    {
        return Err(refuse(
            invalid,
            Some("observation_mismatch:envelope_fields".to_owned()),
        ));
    }
    let o = &signed["observation"];
    let signature = &signed["signature"];
    let key = &signed["verification_key"];
    let failures = observation_failures(o, signature, key, expected);
    if !failures.is_empty() {
        return Err(refuse(
            invalid,
            Some(format!("observation_mismatch:{}", failures.join(","))),
        ));
    }
    identity::verify(o, signature, key)
        .map_err(|_| refuse("node_observation_signature_invalid", None))?;
    Ok(VerifiedObservation {
        observation: o.clone(),
        signed_bytes: crate::canonical::to_bytes(signed).map_err(|_| refuse(invalid, None))?,
        digest: crate::canonical::digest(o).map_err(|_| refuse(invalid, None))?,
    })
}

/// The plain verifier: the code only (`str(exc)`), as every existing consumer uses it.
pub fn verify(
    response: &Value,
    expected: &ExpectedObservation<'_>,
) -> Result<VerifiedObservation, ContractError> {
    verify_detailed(response, expected).map_err(|r| ContractError(r.code))
}
