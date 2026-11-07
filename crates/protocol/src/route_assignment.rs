// SPDX-License-Identifier: AGPL-3.0-only
//! `RouteAssignmentService.answer` of v0.1.4 `mycelium_seed/route_assignment.py`: the seed's answer to one
//! enrolled member's signed route-assignment request (`POST /seed/message`). The member signs the request with
//! its enrolled key and current membership generation; the seed answers with a statement bound to the member's
//! node id, endpoint id, current generation and the request's message id, carrying the one assignment the owner
//! recorded for that member (or none). Request validity, the member checks, the replay record shared with every
//! other member message, row selection and the delivered / applied bookkeeping are modelled over a caller-held
//! [`SeedTable`] and [`RouteTable`]; the caller signs the returned statement. Beside a seed-signed v2 stage pack
//! travels the seed-signed A2 artifact grant for the objects its manifest pins (`_artifact_grant`), issued by the
//! seed's [`GrantAuthority`] through [`answer_with_grant`]; [`answer`], which holds none, refuses a v2 pack with
//! `route_assignment_grant_authority_required`.
//!
//! Not covered: `issue_route_assignment` (owner CLI, needs the pack verifiers and the planner-owned preparation
//! identity) and the gate-role helpers.
use crate::artifact_grant::{
    DEFAULT_GRANT_TTL_MS, GrantBinding, GrantError, GrantLimits, IssueGrantError, issue_grant,
};
use crate::identity;
use crate::identity::Identity;
use crate::qwen_stage_manifest::is_signed_stage_manifest;
use crate::seed_coordinator::{Member, SeedCoordinatorError};
use crate::seed_members::{SeedTable, activation_eligible};
use serde_json::{Value, json};

pub const ROUTE_ASSIGNMENT_REQUEST_PROTOCOL: &str = "mycelium.seed.route_assignment_request.v1";
pub const ROUTE_ASSIGNMENT_PROTOCOL: &str = "mycelium.seed.route_assignment.v1";
pub const SIGNED_MESSAGE_PROTOCOL: &str = "mycelium.membership.signed_message.v1";
const MAX_REQUEST_TTL_SECONDS: f64 = 120.0;
const MAX_CLOCK_SKEW_SECONDS: f64 = 30.0;
const DELIVERY_TTL_SECONDS: f64 = 60.0;
/// At most 16 distinct controllers, each an iroh endpoint id.
const MAX_ASSIGNMENT_CONTROLLERS: usize = 16;
const ENDPOINT_HEX_CHARS: usize = 64;
const REQUEST_FIELDS: [&str; 11] = [
    "protocol",
    "message_id",
    "swarm_id",
    "sender_node_id",
    "sender_endpoint_id",
    "recipient_node_id",
    "incarnation",
    "generation",
    "issued_at",
    "expires_at",
    "applied_assignment_id",
];
const ENVELOPE_FIELDS: [&str; 4] = ["protocol", "message", "signature", "verification_key"];

type Refusal = SeedCoordinatorError;

fn refuse<T>(code: &'static str) -> Result<T, Refusal> {
    Err(SeedCoordinatorError(code))
}

/// `_SEGMENT_RE`: `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}` (this module's segment allows `:`).
fn is_segment(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.len() <= 128
            && s.as_bytes()[0].is_ascii_alphanumeric()
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
    })
}

fn float_value(f: f64) -> Value {
    serde_json::Number::from_f64(f).map_or(Value::Null, Value::Number)
}

/// `_number`: an int or float, not a bool, finite.
fn number(v: &Value) -> Option<f64> {
    v.as_number()
        .and_then(serde_json::Number::as_f64)
        .filter(|f| f.is_finite())
}

/// One row of `seed_route_assignments` (one assignment per member, keyed by node id).
#[derive(Debug, Clone)]
pub struct RouteRow {
    pub node_id: String,
    pub assignment_id: String,
    pub endpoint_id: String,
    pub verification_key_digest: String,
    pub stage_pack: Value,
    pub stage_pack_digest: String,
    pub peer_endpoint_id: String,
    pub route_generation: u64,
    pub forward_to: bool,
    pub issued_at: f64,
    pub expires_at: f64,
    /// The persisted JSON text, re-validated on every delivery.
    pub controller_endpoint_ids_json: String,
    pub withdrawn: bool,
    pub delivered_at: Option<f64>,
    pub delivered_generation: Option<u64>,
    pub applied_at: Option<f64>,
    pub applied_generation: Option<u64>,
    /// The row's `seed_route_assignment_artifacts.sources_json`, as `json.loads` reads it; `None` when the owner
    /// recorded no artifact sources for this assignment (no artifacts row).
    pub artifact_sources: Option<Value>,
}

#[derive(Default)]
pub struct RouteTable {
    rows: Vec<RouteRow>,
}

impl RouteTable {
    /// `INSERT OR REPLACE` of the member's single assignment.
    pub fn insert_replace(&mut self, row: RouteRow) {
        match self.rows.iter().position(|r| r.node_id == row.node_id) {
            Some(i) => self.rows[i] = row,
            None => self.rows.push(row),
        }
    }

    /// `withdraw_route_assignment`.
    pub fn withdraw(&mut self, node_id: &str) -> Result<(), Refusal> {
        match self.rows.iter_mut().find(|r| r.node_id == node_id) {
            Some(row) => {
                row.withdrawn = true;
                Ok(())
            }
            None => refuse("seed_route_assignment_unknown"),
        }
    }

    /// The bookkeeping columns of one member's row.
    pub fn row_state(&self, node_id: &str) -> Option<Value> {
        let row = self.rows.iter().find(|r| r.node_id == node_id)?;
        Some(json!({
            "assignment_id": row.assignment_id,
            "withdrawn": i32::from(row.withdrawn),
            "delivered_at": row.delivered_at.map_or(Value::Null, float_value),
            "delivered_generation": row.delivered_generation,
            "applied_at": row.applied_at.map_or(Value::Null, float_value),
            "applied_generation": row.applied_generation,
        }))
    }
}

/// What the service knows about itself.
pub struct RouteService<'a> {
    pub swarm_id: &'a str,
    pub seed_node_id: &'a str,
    pub seed_endpoint_id: &'a str,
    pub incarnation: &'a str,
}

/// The seed's v2 artifact-grant authority (`RouteAssignmentService.signer` and `.grant_id_source` of v0.1.4): the
/// key that signs a grant, the source of its id and the limits `_check_grant_shape` applies.
pub struct GrantAuthority<'a> {
    pub signer: &'a Identity,
    pub grant_ids: &'a mut dyn FnMut() -> String,
    pub limits: GrantLimits,
}

/// Python `int(seconds * 1000)` (`route_assignment.py:826-827`): exact for a finite float, saturated past `i64`
/// (where the difference still decides `min` the same way and `_check_grant_shape` refuses an out-of-range time);
/// `None` for ±inf / NaN, where `int()` raises.
fn unix_ms(seconds: f64) -> Option<i64> {
    let ms = (seconds * 1000.0).trunc();
    ms.is_finite().then_some(ms as i64)
}

/// `seed_route_assignment_grant_invalid:<code>` (`route_assignment.py:843-844`) for the `ArtifactGrantError` codes
/// `issue_grant` raises.
fn grant_invalid(error: &GrantError) -> Refusal {
    SeedCoordinatorError(match error.0 {
        "artifact_grant_invalid" => "seed_route_assignment_grant_invalid:artifact_grant_invalid",
        "artifact_signing_failed" => "seed_route_assignment_grant_invalid:artifact_signing_failed",
        // issue_grant raises no other code; an unknown one still refuses under the oracle's prefix.
        _ => "seed_route_assignment_grant_invalid",
    })
}

/// `RouteAssignmentService._artifact_grant` (`route_assignment.py:811-846`): the grant for exactly the objects
/// the delivered row's v2 manifest pins, bound to this member's node, endpoint and enrolled key.
fn artifact_grant(
    row: &RouteRow,
    member: &Member,
    manifest: &Value,
    now: f64,
    service: &RouteService<'_>,
    grants: &mut GrantAuthority<'_>,
) -> Result<Value, Refusal> {
    // :824-825
    let Some(sources) = &row.artifact_sources else {
        return refuse("seed_route_assignment_artifact_sources_required");
    };
    // :826-827: ttl_ms = min(DEFAULT_GRANT_TTL_MS, int(expires_at * 1000) - now_ms).
    let (Some(now_ms), Some(expires_ms)) = (unix_ms(now), unix_ms(row.expires_at)) else {
        return Err(grant_invalid(&GrantError("artifact_grant_invalid")));
    };
    let ttl_ms = DEFAULT_GRANT_TTL_MS.min(expires_ms.saturating_sub(now_ms));
    // :831: the grant id is drawn before the manifest is verified.
    let grant_id = (grants.grant_ids)();
    let binding = GrantBinding {
        grant_id: &grant_id,
        swarm_id: service.swarm_id,
        seed_node_id: service.seed_node_id,
        recipient_node_id: &row.node_id,
        recipient_endpoint_id: &member.endpoint_id,
        recipient_key_digest: &member.verification_key_digest,
        assignment_id: &row.assignment_id,
    };
    issue_grant(
        grants.signer,
        &binding,
        manifest,
        sources,
        now_ms,
        ttl_ms,
        grants.limits,
    )
    .map_err(|error| match error {
        // Not an ArtifactGrantError: the manifest's own code propagates unchanged (`artifact_grant.py:217`).
        IssueGrantError::Manifest(manifest_error) => SeedCoordinatorError(manifest_error.0),
        IssueGrantError::Grant(grant_error) => grant_invalid(&grant_error),
    })
}

/// `_controller_ids`: the persisted controller set, validated as strictly as the issuing boundary.
fn controller_ids(raw: &str) -> Result<Vec<String>, Refusal> {
    const CODE: &str = "seed_route_assignment_controller_invalid";
    let parsed: Value = serde_json::from_str(raw).map_err(|_| SeedCoordinatorError(CODE))?;
    let Some(list) = parsed
        .as_array()
        .filter(|l| l.len() <= MAX_ASSIGNMENT_CONTROLLERS)
    else {
        return refuse(CODE);
    };
    let mut ids: Vec<String> = Vec::new();
    for item in list {
        let id = item.as_str().filter(|s| {
            s.len() == ENDPOINT_HEX_CHARS
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        });
        match id {
            Some(id) if !ids.iter().any(|seen| seen == id) => ids.push(id.to_owned()),
            _ => return refuse(CODE),
        }
    }
    ids.sort();
    Ok(ids)
}

/// `RouteAssignmentService.answer`: the unsigned delivery (`statement` + `stage_pack`) for a valid request.
/// `id_source` is asked for the delivery message id once, after the request has been committed.
/// This caller holds no grant authority, so a delivered seed-signed v2 pack refuses with
/// `route_assignment_grant_authority_required` (after the bookkeeping, as every grant refusal).
pub fn answer(
    table: &mut SeedTable,
    routes: &mut RouteTable,
    service: &RouteService<'_>,
    envelope: &Value,
    now: f64,
    id_source: &mut dyn FnMut() -> String,
) -> Result<Value, Refusal> {
    respond(table, routes, service, None, envelope, now, id_source)
}

/// [`answer`] for a seed holding its grant authority: a delivered seed-signed v2 pack travels with its
/// `artifact_grant` (`route_assignment.py:773-774, 805-808`). A grant refusal (missing sources, the manifest's own
/// code, `seed_route_assignment_grant_invalid:<code>`) is returned after the request and the row's delivery
/// bookkeeping were recorded, as v0.1.4 commits them first (:746).
pub fn answer_with_grant(
    table: &mut SeedTable,
    routes: &mut RouteTable,
    service: &RouteService<'_>,
    grants: &mut GrantAuthority<'_>,
    envelope: &Value,
    now: f64,
    id_source: &mut dyn FnMut() -> String,
) -> Result<Value, Refusal> {
    respond(
        table,
        routes,
        service,
        Some(grants),
        envelope,
        now,
        id_source,
    )
}

fn respond(
    table: &mut SeedTable,
    routes: &mut RouteTable,
    service: &RouteService<'_>,
    grants: Option<&mut GrantAuthority<'_>>,
    envelope: &Value,
    now: f64,
    id_source: &mut dyn FnMut() -> String,
) -> Result<Value, Refusal> {
    const MALFORMED: &str = "seed_message_malformed";
    let Some(e) = envelope.as_object().filter(|e| {
        e.len() == ENVELOPE_FIELDS.len() && ENVELOPE_FIELDS.iter().all(|f| e.contains_key(*f))
    }) else {
        return refuse(MALFORMED);
    };
    let (message, signature, record) = (&e["message"], &e["signature"], &e["verification_key"]);
    let request = message.as_object().filter(|m| {
        e["protocol"] == SIGNED_MESSAGE_PROTOCOL
            && signature.is_object()
            && record.is_object()
            && m.len() == REQUEST_FIELDS.len()
            && REQUEST_FIELDS.iter().all(|f| m.contains_key(*f))
            && m["protocol"] == ROUTE_ASSIGNMENT_REQUEST_PROTOCOL
    });
    let Some(request) = request else {
        return refuse(MALFORMED);
    };
    for field in [
        "message_id",
        "sender_node_id",
        "sender_endpoint_id",
        "recipient_node_id",
        "incarnation",
    ] {
        if !is_segment(&request[field]) {
            return refuse(MALFORMED);
        }
    }
    let applied = &request["applied_assignment_id"];
    if !applied.is_null() && !is_segment(applied) {
        return refuse(MALFORMED);
    }
    let generation = request["generation"].as_u64().filter(|g| *g >= 1);
    let (issued_at, expires_at) = (
        number(&request["issued_at"]),
        number(&request["expires_at"]),
    );
    let (Some(generation), Some(issued_at), Some(expires_at)) = (generation, issued_at, expires_at)
    else {
        return refuse(MALFORMED);
    };
    let ttl = expires_at - issued_at;
    if !(ttl > 0.0 && ttl <= MAX_REQUEST_TTL_SECONDS) {
        return refuse(MALFORMED);
    }
    if request["swarm_id"] != service.swarm_id
        || request["recipient_node_id"] != service.seed_node_id
    {
        return refuse("seed_message_mismatch");
    }
    if now > expires_at {
        return refuse("membership_message_expired");
    }
    if now + MAX_CLOCK_SKEW_SECONDS < issued_at {
        return refuse("membership_message_from_future");
    }
    let node_id = request["sender_node_id"].as_str().unwrap_or_default();
    let Some(tracked) = table.member_row(node_id) else {
        return refuse("seed_member_unknown");
    };
    let member = tracked.member.clone();
    if record["verification_key_digest"] != member.verification_key_digest.as_str() {
        return refuse("membership_key_pin_mismatch");
    }
    if identity::verify(message, signature, record).is_err() {
        return refuse("membership_signature_invalid");
    }
    if request["sender_endpoint_id"] != member.endpoint_id.as_str()
        || request["incarnation"] != member.incarnation.as_str()
        || generation != member.generation
    {
        return refuse("seed_state_member_stale");
    }
    if member.lifecycle_state == "STOPPED" {
        return refuse("seed_member_revoked");
    }
    if now >= member.lease_expires_at {
        return refuse("seed_member_lease_expired");
    }
    table.remember_id(
        node_id,
        generation,
        request["message_id"].as_str().unwrap_or_default(),
        expires_at,
        now,
    )?;
    let eligible = activation_eligible(&member);
    let mut delivered: Option<RouteRow> = None;
    if let Some(row) = routes.rows.iter_mut().find(|r| {
        r.node_id == node_id
            && !r.withdrawn
            && r.expires_at > now
            && r.endpoint_id == member.endpoint_id
            && r.verification_key_digest == member.verification_key_digest
    }) && eligible
    {
        // The member's own word on what it runs: a node that has not re-applied the assignment is not
        // counted as applied.
        let applied_now = applied == row.assignment_id.as_str();
        row.applied_at = applied_now.then_some(now);
        row.applied_generation = applied_now.then_some(generation);
        row.delivered_at = Some(now);
        row.delivered_generation = Some(generation);
        delivered = Some(row.clone());
    }
    let mut assignment = Value::Null;
    let mut stage_pack = Value::Null;
    let mut statement_expires = now + DELIVERY_TTL_SECONDS;
    let mut grant: Option<Value> = None;
    if let Some(row) = &delivered {
        assignment = json!({
            "assignment_id": row.assignment_id,
            "stage_pack_digest": row.stage_pack_digest,
            "peer_endpoint_id": row.peer_endpoint_id,
            "route_generation": row.route_generation,
            "forward_to": row.forward_to,
            "expires_at": float_value(row.expires_at),
            "controller_endpoint_ids": controller_ids(&row.controller_endpoint_ids_json)?,
        });
        stage_pack = row.stage_pack.clone();
        statement_expires = statement_expires.min(row.expires_at);
        // :773-774: a seed-signed v2 pack travels with the A2 grant for its manifest's objects.
        if is_signed_stage_manifest(&stage_pack) {
            let Some(grants) = grants else {
                return refuse("route_assignment_grant_authority_required");
            };
            grant = Some(artifact_grant(
                row,
                &member,
                &stage_pack["message"],
                now,
                service,
                grants,
            )?);
        }
    }
    let statement = json!({
        "protocol": ROUTE_ASSIGNMENT_PROTOCOL,
        "message_id": id_source(),
        "swarm_id": service.swarm_id,
        "sender_node_id": service.seed_node_id,
        "sender_endpoint_id": service.seed_endpoint_id,
        "recipient_node_id": node_id,
        "recipient_endpoint_id": member.endpoint_id,
        "incarnation": service.incarnation,
        "generation": generation,
        "request_message_id": request["message_id"],
        "issued_at": float_value(now),
        "expires_at": float_value(statement_expires),
        "assignment": assignment,
        "route_ready": false,
    });
    let mut delivery = json!({ "statement": statement, "stage_pack": stage_pack });
    if let Some(grant) = grant {
        // :805-808: beside the pack.
        delivery["artifact_grant"] = grant;
    }
    Ok(delivery)
}
