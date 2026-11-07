// SPDX-License-Identifier: AGPL-3.0-only
//! Bind a stored physical operator plan to the current durable seed membership, byte-faithful to
//! v0.1.4 `mycelium_seed/plan_binding.py` (desktop `chain_compile.start_compile` ->
//! `workflow.prepare_serving_workflow` -> `bind_operator_plan_document`).
//!
//! The membership contract (`membership::validate`, activation eligibility) and the signature
//! (`Identity::sign`) are consumed from their owners in this crate; the TTL ceiling arrives as the
//! caller's [`TimeLimits`] from the budget registry.
use crate::identity::Identity;
use crate::{TimeLimits, membership};
use serde_json::{Map, Number, Value, json};
use std::collections::{BTreeMap, BTreeSet};

/// Source constant `now + 3_600.0` (`plan_binding.py:91`): the bound offers' validity window.
const PLAN_VALIDITY_SECONDS: f64 = 3_600.0;
const INVALID: PlanBindingError = PlanBindingError("operator_plan_binding_invalid");

/// The plan-binding code, or the membership-contract code raised through it unchanged.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct PlanBindingError(pub &'static str);

fn float_value(f: f64) -> Value {
    Number::from_f64(f).map_or(Value::Null, Value::Number)
}

/// `isinstance(value, int)` (a Python bool is an int; a JSON number with a fraction/exponent is not).
fn is_python_int(value: &Value) -> bool {
    match value {
        Value::Bool(_) => true,
        Value::Number(n) => !n
            .to_string()
            .bytes()
            .any(|c| matches!(c, b'.' | b'e' | b'E')),
        _ => false,
    }
}

/// `float(value)` for an `int`/`float`/`bool`; `None` when the value is neither.
fn python_float(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(b) => Some(f64::from(u8::from(*b))),
        Value::Number(n) => n.as_f64(),
        _ => None,
    }
}

fn non_empty_text(value: Option<&Value>) -> Option<&str> {
    value.and_then(Value::as_str).filter(|s| !s.is_empty())
}

/// The seed the offers are re-bound to: its signer and its swarm/node identity, plus the
/// membership TTL ceiling from the budget registry.
pub struct BindingSeed<'a> {
    pub signer: &'a Identity,
    pub swarm_id: &'a str,
    pub seed_node_id: &'a str,
    pub limits: TimeLimits,
}

/// `bind_operator_plan_document`: a detached plan with freshly signed coordinator-bound offers.
pub fn bind_operator_plan_document(
    plan: &Value,
    seed: &BindingSeed<'_>,
    members: &[Value],
    now: f64,
    observed_boot_ids: Option<&Value>,
) -> Result<Value, PlanBindingError> {
    let BindingSeed {
        signer,
        swarm_id,
        seed_node_id,
        limits,
    } = *seed;
    if !plan.is_object() || !now.is_finite() || now <= 0.0 {
        return Err(INVALID);
    }
    let mut result = plan.clone();
    let offers = result
        .pointer("/controller/membership_snapshot/assignment_offers")
        .cloned()
        .ok_or(INVALID)?;
    let offers = offers.as_array().filter(|o| !o.is_empty()).ok_or(INVALID)?;
    if !result["controller"]["membership_snapshot"].is_object() {
        return Err(INVALID);
    }

    let mut current: BTreeMap<&str, &Value> = BTreeMap::new();
    for member in members {
        if let Some(node) = member
            .get("node_id")
            .and_then(Value::as_str)
            .filter(|_| member.is_object())
        {
            current.insert(node, member);
        }
    }
    let mut recipients: Vec<&str> = Vec::new();
    let mut activation_endpoint: BTreeMap<&str, &str> = BTreeMap::new();
    for envelope in offers {
        let recipient = envelope
            .get("message")
            .filter(|m| m.is_object())
            .and_then(|m| m.get("recipient_node_id"))
            .and_then(Value::as_str)
            .filter(|r| !recipients.contains(r))
            .ok_or(INVALID)?;
        recipients.push(recipient);
        let records = envelope["message"]["peer_endpoint_records"]
            .as_array()
            .ok_or(INVALID)?;
        for record in records {
            if !record.is_object() {
                return Err(INVALID);
            }
            let (Some(node), Some(endpoint)) = (
                non_empty_text(record.get("node_id")),
                non_empty_text(record.get("endpoint_id")),
            ) else {
                return Err(PlanBindingError(
                    "operator_plan_activation_endpoint_invalid",
                ));
            };
            if *activation_endpoint.entry(node).or_insert(endpoint) != endpoint {
                return Err(PlanBindingError(
                    "operator_plan_activation_endpoint_conflict",
                ));
            }
        }
    }

    let ineligible = PlanBindingError("operator_plan_member_ineligible");
    let mut selected: BTreeMap<&str, &Value> = BTreeMap::new();
    for recipient in &recipients {
        let member = *current.get(recipient).ok_or(ineligible.clone())?;
        let runtime = &member["runtime_capability"];
        if !runtime.is_object() {
            return Err(ineligible);
        }
        let eligible =
            membership::peer_runtime_is_activation_eligible(&member["peer_class"], runtime)
                .map_err(|e| PlanBindingError(e.0))?;
        let lease = member.get("lease_expires_at").and_then(python_float);
        if !eligible
            || lease.is_none_or(|l| l <= now)
            || !member["endpoint_id"].is_string()
            || !is_python_int(&member["generation"])
        {
            return Err(ineligible);
        }
        selected.insert(recipient, member);
    }
    if selected.len() > 1
        && activation_endpoint.keys().collect::<BTreeSet<_>>()
            != selected.keys().collect::<BTreeSet<_>>()
    {
        return Err(PlanBindingError(
            "operator_plan_activation_endpoint_missing",
        ));
    }
    let valid_until = selected
        .values()
        .filter_map(|m| python_float(&m["lease_expires_at"]))
        .fold(now + PLAN_VALIDITY_SECONDS, f64::min);

    let stamp = (now * 1_000.0) as i128;
    let mut refreshed = Vec::new();
    for (index, envelope) in offers.iter().enumerate() {
        let mut message = envelope["message"].as_object().cloned().unwrap_or_default();
        let recipient = envelope["message"]["recipient_node_id"]
            .as_str()
            .unwrap_or_default();
        let member = selected.get(recipient).ok_or(INVALID)?;
        // `message["deployment_epoch"]` is read once per peer record (a KeyError only when a peer exists).
        let mut peer_records = Vec::new();
        for (node, peer) in selected.iter().filter(|(node, _)| **node != recipient) {
            let epoch = message
                .get("deployment_epoch")
                .cloned()
                .ok_or(PlanBindingError("membership_message_invalid"))?;
            peer_records.push(json!({
                "node_id": node,
                "endpoint_id": activation_endpoint.get(node),
                "deployment_epoch": epoch,
                "membership_generation": peer["generation"],
                "valid_from": float_value(now),
                "valid_until": float_value(valid_until),
            }));
        }
        for (key, value) in [
            (
                "message_id",
                json!(format!("bound-plan-offer-{index}-{stamp}")),
            ),
            ("swarm_id", json!(swarm_id)),
            ("sender_node_id", json!(seed_node_id)),
            ("sender_endpoint_id", json!(signer.endpoint_id())),
            ("incarnation", json!(format!("bound-seed-{stamp}"))),
            ("generation", member["generation"].clone()),
            ("issued_at", float_value(now)),
            ("expires_at", float_value(valid_until)),
            ("peer_endpoint_records", Value::Array(peer_records)),
        ] {
            message.insert(key.to_owned(), value);
        }
        let message = Value::Object(message);
        membership::validate(&message, limits).map_err(|e| PlanBindingError(e.0))?;
        let signature = signer
            .sign(&message)
            .map_err(|_| PlanBindingError("membership_message_invalid"))?;
        refreshed.push(json!({
            "protocol": membership::SIGNED_MESSAGE_PROTOCOL,
            "message": message,
            "signature": signature,
            "verification_key": signer.public_key_record(),
        }));
    }
    if let Some(snapshot) = result
        .pointer_mut("/controller/membership_snapshot")
        .and_then(Value::as_object_mut)
    {
        snapshot.insert(
            "seed_key_digest".to_owned(),
            json!(signer.verification_key_digest()),
        );
        snapshot.insert("swarm_id".to_owned(), json!(swarm_id));
        snapshot.insert("assignment_offers".to_owned(), Value::Array(refreshed));
    }
    if let Some(controller) = result.get_mut("controller").and_then(Value::as_object_mut) {
        controller.insert("now".to_owned(), float_value(now));
        rebind_boot_ids(controller, observed_boot_ids);
    }
    Ok(result)
}

/// Bind the observed `boot_id` onto the peers (only the value of an existing field, only for a node
/// with a non-empty text observation).
fn rebind_boot_ids(controller: &mut Map<String, Value>, observed: Option<&Value>) {
    let Some(observed) = observed
        .and_then(Value::as_object)
        .filter(|o| !o.is_empty())
    else {
        return;
    };
    let Some(peers) = controller.get_mut("peers").and_then(Value::as_array_mut) else {
        return;
    };
    for peer in peers.iter_mut().filter_map(Value::as_object_mut) {
        let seen = peer
            .get("node_id")
            .and_then(Value::as_str)
            .and_then(|node| observed.get(node))
            .and_then(Value::as_str)
            .filter(|b| !b.is_empty());
        if let Some(boot) = seen
            && peer.get("boot_id").and_then(Value::as_str) != Some(boot)
        {
            peer.insert("boot_id".to_owned(), json!(boot));
        }
    }
}
