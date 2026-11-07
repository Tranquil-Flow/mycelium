// SPDX-License-Identifier: AGPL-3.0-only
//! Authenticated token-forwarding decisions at the f4e366e9 boundary.
//! Retirement is NOT receipt acceptance, a native ACK, terminality or cleanup.
//! Mailbox transactions, current membership, replay and worker IO belong to L6.
use crate::{ContractError, TimeLimits, canonical, checks::*};
use serde_json::{Value, json};
pub const ACK: &str = "mycelium.managed_control.token_progress_ack.v1";
pub const CANCELLED: &str = "mycelium.managed_control.token_progress_cancelled.v1";
const BINDING: &[&str] = &[
    "target_node_id",
    "target_endpoint_id",
    "target_key_digest",
    "membership_generation",
    "member_incarnation",
    "run_id",
    "deployment_id",
];
pub struct ExpectedProgress<'a> {
    pub owner_key: &'a Value,
    pub request_id: &'a str,
    pub swarm_id: &'a str,
    pub owner_node_id: &'a str,
    /// Independently pinned current managed-member binding.
    pub binding: &'a Value,
    pub command_id: &'a str,
    /// Exact pending receipt already verified by the native progress cursor.
    pub receipt: &'a Value,
    pub now: f64,
}
/// Constructible only by owner signature/binding verification. Cannot be turned
/// into a VerifiedReceipt or a native eviction ACK when it denotes retirement.
#[derive(Debug)]
pub struct VerifiedDecision {
    cancelled: bool,
    command_id: String,
    receipt_bytes: Vec<u8>,
    envelope_bytes: Vec<u8>,
    envelope: Value,
}
impl VerifiedDecision {
    pub fn is_cancelled(&self) -> bool {
        self.cancelled
    }
    pub fn envelope_bytes(&self) -> &[u8] {
        &self.envelope_bytes
    }
}
pub fn parse_response(raw: &[u8]) -> Result<Value, ContractError> {
    crate::strict::object(raw, crate::managed::MAX_FRAME_BYTES)
        .map_err(|_| ContractError("managed_control_response_invalid"))
}
fn encoded(v: &Value, code: &'static str) -> Result<Vec<u8>, ContractError> {
    canonical::to_bytes(v).map_err(|_| ContractError(code))
}
fn snapshot(v: &Value) -> Result<Vec<u8>, ContractError> {
    let c = "token_progress_frame_invalid";
    exact(v, &["statement", "signature"], c)?;
    let b = encoded(v, c)?;
    require(b.len() <= crate::native::MAX_RECEIPT_BYTES, c)?;
    Ok(b)
}
fn any_integer(v: &Value) -> bool {
    v.as_number().is_some_and(|n| {
        let s = n.to_string();
        let digits = s.strip_prefix('-').unwrap_or(&s);
        !digits.is_empty() && digits.bytes().all(|b| b.is_ascii_digit())
    })
}
pub fn verify_response(
    env: &Value,
    e: &ExpectedProgress<'_>,
    limits: TimeLimits,
) -> Result<VerifiedDecision, ContractError> {
    let receipt_bytes = snapshot(e.receipt)?;
    let cancelled = env["statement"]["protocol"] == CANCELLED;
    let protocol = if cancelled { CANCELLED } else { ACK };
    let endpoint = env["statement"]["owner_endpoint_id"].as_str().unwrap_or("");
    let body = crate::managed::verify(env, protocol, e.owner_key, endpoint, e.now, limits)
        .map_err(|err| match err.0 {
            "managed_control_signature_invalid" | "managed_control_key_pin_mismatch" => {
                ContractError("managed_control_response_signature_invalid")
            }
            _ => err,
        })?;
    let c = "managed_control_response_invalid";
    let mut fields = vec![
        "protocol",
        "request_id",
        "swarm_id",
        "owner_node_id",
        "owner_endpoint_id",
        "issued_at",
        "expires_at",
        "command_id",
        "grant_digest",
        "receipt_sequence",
        "receipt_digest",
    ];
    fields.extend(BINDING);
    exact(&body, &fields, c)?;
    require(
        body["request_id"] == e.request_id
            && body["swarm_id"] == e.swarm_id
            && body["owner_node_id"] == e.owner_node_id
            && body["owner_endpoint_id"] == env["signature"]["signer_endpoint_id"]
            && BINDING
                .iter()
                .all(|f| python_equal(&body[*f], &e.binding[*f])),
        c,
    )?;
    require(
        body["command_id"] == e.command_id
            && body["grant_digest"] == e.receipt["statement"]["grant_digest"]
            && any_integer(&body["receipt_sequence"])
            && body["receipt_sequence"] == e.receipt["statement"]["sequence"]
            && body["receipt_digest"] == e.receipt["signature"]["signed_statement_digest"],
        c,
    )?;
    Ok(VerifiedDecision {
        cancelled,
        command_id: e.command_id.to_owned(),
        receipt_bytes,
        envelope_bytes: encoded(env, c)?,
        envelope: env.clone(),
    })
}
/// Pure forwarding record. Caller must start with a cursor-verified receipt;
/// this type does not verify or accept tokens. Persistence is injected and must
/// complete before caller closes the reader/clears its durable outbound packet.
/// No restore, native execution, journal eviction or cleanup is performed here.
pub struct PendingForward {
    command_id: String,
    row: Value,
}
impl PendingForward {
    pub fn new(command_id: &str, receipt: &Value) -> Result<Self, ContractError> {
        snapshot(receipt)?;
        Ok(Self {
            command_id: command_id.to_owned(),
            row: json!({"pending":receipt,"owner_ack":null,"terminal":false,"error":null}),
        })
    }
    pub fn snapshot(&self) -> &Value {
        &self.row
    }
    pub fn record(
        &mut self,
        decision: VerifiedDecision,
        persist: impl FnOnce(&Value) -> Result<(), ContractError>,
    ) -> Result<(), ContractError> {
        require(
            decision.command_id == self.command_id
                && decision.receipt_bytes == snapshot(&self.row["pending"])?
                && (self.row["error"].is_null()
                    || (self.row["error"] == "token_progress_cancelled"
                        && decision.cancelled
                        && self.row["owner_ack"] == decision.envelope)),
            "token_progress_state_invalid",
        )?;
        let mut next = self.row.clone();
        next["owner_ack"] = decision.envelope;
        if decision.cancelled {
            next["error"] = json!("token_progress_cancelled");
        }
        persist(&next)?;
        self.row = next;
        Ok(())
    }
    /// A cancellation can unblock ordinary command delivery only after a
    /// separately validated native result or reconciliation disposition exists.
    pub fn ready_to_ack(&self, claim_phase: Option<&str>) -> bool {
        self.row["error"] == "token_progress_cancelled"
            && matches!(claim_phase, Some("executed" | "reconciliation_required"))
    }
    pub fn native_ack(&self) -> Option<&Value> {
        if self.row["error"].is_null() && !self.row["owner_ack"].is_null() {
            Some(&self.row["owner_ack"])
        } else {
            None
        }
    }
}
/// Host-supplied snapshot under the mailbox/member transaction locks. These are
/// NOT wire inputs or capabilities: authenticate the outer progress request and
/// recheck live member/claim plus replay store before signing the returned fields.
/// `cancelled` MUST come only from the trusted owner's cancellation predicate;
/// ordinary close, HTTP refusal, grant expiry and restart cannot set it.
pub struct RetirementContext<'a> {
    pub cancelled: bool,
    pub closed: bool,
    pub command_id: &'a str,
    pub grant_digest: &'a str,
    pub target_endpoint_id: &'a str,
    pub claim: &'a Value,
    pub native_command: &'a Value,
}
/// Pure subject predicate from `_cancelled_token_progress`, not authentication
/// or a mailbox. Does not consume/seal receipts, renew grants or mutate claims.
/// No inner receipt signature check is intended: only forwarding is retired.
pub fn retirement_subject(
    c: &RetirementContext<'_>,
    statement: &Value,
) -> Result<Option<Value>, ContractError> {
    if !c.cancelled || statement["command_id"] != c.command_id {
        return Ok(None);
    }
    require(!c.closed, "managed_control_closed")?;
    require(c.claim["state"] == "claimed", "token_progress_claim_stale")?;
    require(
        BINDING.iter().all(|f| c.claim[*f] == statement[*f]),
        "managed_control_command_binding_mismatch",
    )?;
    require(
        c.claim["claim_token"] == statement["claim_token"],
        "managed_control_claim_token_mismatch",
    )?;
    let receipt = &statement["receipt"];
    snapshot(receipt)?;
    let body = &receipt["statement"];
    let signature = &receipt["signature"];
    let code = "token_progress_subject_mismatch";
    require(body.is_object() && signature.is_object(), code)?;
    let grant = &c.native_command["payload"]["token_grant"]["statement"];
    let scope = &grant["scope"];
    require(
        c.native_command["command"] == "infer_start"
            && canonical::sha256_prefixed(&encoded(grant, code)?) == c.grant_digest
            && scope["entry_node_id"] == statement["target_node_id"]
            && statement["target_endpoint_id"] == c.target_endpoint_id
            && scope["entry_key_digest"] == statement["target_key_digest"]
            && scope["run_id"] == statement["run_id"]
            && scope["control"]["deployment_id"] == statement["deployment_id"]
            && body["grant_digest"] == c.grant_digest
            && any_integer(&body["sequence"])
            && signature["signed_statement_digest"]
                == canonical::sha256_prefixed(&encoded(body, code)?),
        code,
    )?;
    let mut fields = json!({"protocol":CANCELLED,"request_id":statement["request_id"],"command_id":c.command_id,
        "grant_digest":c.grant_digest,"receipt_sequence":body["sequence"],"receipt_digest":signature["signed_statement_digest"]});
    for f in BINDING {
        fields[*f] = statement[*f].clone();
    }
    Ok(Some(fields))
}
