// SPDX-License-Identifier: AGPL-3.0-only
//! The rest of `ProductProjector._build` and `project` (`mycelium_product_spine/projector.py:308-1394`
//! @ f4e366e9) around the member rows of [`ProductProjector::project_members`]: the ten source states,
//! the recovery aggregate (`:201-264`), the seed-rotation incident, the route, its stages, assignments,
//! load proofs, directed links and incidents, the qualification, the readiness rows, the provenance
//! entities, the publication and the route explorer (`:363-366`).
//!
//! Behavioural parity (owner ruling 1): source documents are read the way the oracle reads JSON
//! (`int(...)` of a JSON integer or float, `str(...)` through the member projection's `py_str`); Python
//! quirks beyond that (bool as int, `1 == 1.0` in the binding check) are not reproduced. Not ported
//! (class E: no Rust source exists): the A7 workspace entity, coarse-region evidence and the
//! transport-history `internet_native` source; `internet_native` is always the oracle's own unknown
//! projection (`:45-88`), which v0.1.4 also publishes when its source is unreadable.
use super::{
    ProductProjector, ProjectionError, empty_binding, int_field, int_json, millis, py_str, raise,
    safe_code,
};
use crate::wire::{Text, dumps_sorted};
use budgets::{BudgetId, Entries};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};

/// One projection's inputs, as `project(...)` takes them (`:308-322`).
pub struct ProjectionInput<'a> {
    pub members: &'a [Map<String, Value>],
    /// `None`: no assignment source is configured (`assignment-source` is `unsupported`).
    pub assignments: Option<&'a [Map<String, Value>]>,
    pub route_status: Option<&'a Value>,
    pub qualification: Option<&'a Value>,
    pub recovery_status: Option<&'a Value>,
    pub now_unix_ms: u64,
    /// Source id to the bounded public reason a failing source reader gave.
    pub source_errors: &'a BTreeMap<String, String>,
}

/// The publication sequence and the last current recovery aggregate (`:270-280`).
#[derive(Default)]
pub(super) struct Publication {
    generation: u64,
    cursor: u64,
    last_recovery: Option<(Value, Value, Value)>,
}

const ERROR_SOURCES: [&str; 6] = [
    "membership-source",
    "assignment-source",
    "route-source",
    "qualification-source",
    "recovery-source",
    "a7-source",
];
const DEGRADING_SOURCES: [&str; 5] = [
    "membership-source",
    "assignment-source",
    "route-source",
    "qualification-source",
    "recovery-source",
];
const ENTITY_KINDS: [&str; 13] = [
    "artifact",
    "assignment",
    "device",
    "directed_link",
    "incident",
    "load_proof",
    "qualification",
    "recovery",
    "request",
    "route",
    "runtime_kv_ownership",
    "source_provenance",
    "stage",
];
const RECOVERY_PHASES: [&str; 10] = [
    "active",
    "failure_observed",
    "checkpoint_frozen",
    "successor_pending",
    "recovery_prefill",
    "cutover_pending",
    "recovered_decode",
    "cleanup",
    "terminal",
    "aborted",
];
/// `len(requests) > 4_096` refuses a recovery projection (`:209`).
const MAX_RECOVERY_REQUESTS: usize = 4_096;
/// `0 <= attempt <= 2` (`:236`).
const MAX_RECOVERY_ATTEMPT: u64 = 2;
/// `identity.split(':', 1)[1][:20]` and `snapshot_digest[:16]` (`:845`, `:1361`).
const ROUTE_ID_HEX: usize = 20;
const SNAPSHOT_ID_HEX: usize = 16;

fn code(text: &str) -> bool {
    let bytes = text.as_bytes();
    bytes.first().is_some_and(u8::is_ascii_lowercase)
        && bytes.len() <= 64
        && bytes
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_')
}

fn digest_text(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        s.strip_prefix("sha256:").is_some_and(|hex| {
            hex.len() == 64
                && hex
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
    })
}

/// `_digest` (`:27-34`): sha256 over sorted, compact, ASCII-escaped JSON.
fn digest(value: &Value) -> String {
    let hash = Sha256::digest(dumps_sorted(value, Text::Ascii));
    let hex: String = hash.iter().map(|b| format!("{b:02x}")).collect();
    format!("sha256:{hex}")
}

fn binding(
    deployment_id: Value,
    deployment_epoch: Value,
    route_id: Value,
    route_generation: Value,
    topology_version: Value,
) -> Value {
    json!({
        "deployment_id": deployment_id,
        "deployment_epoch": deployment_epoch,
        "route_id": route_id,
        "route_generation": route_generation,
        "topology_version": topology_version,
    })
}

fn freshness(status: &str, observed: Value, valid_until: Value) -> Value {
    json!({"status": status, "observed_at_unix_ms": observed, "valid_until_unix_ms": valid_until})
}

fn current(now: u64) -> Value {
    freshness("current", json!(now), json!(now))
}

fn source_state(id: &str, authority: &str, status: &str, now: u64, reason: Option<&str>) -> Value {
    let observed = if matches!(status, "current" | "replay") {
        json!(now)
    } else {
        Value::Null
    };
    json!({
        "source_id": id,
        "authority": authority,
        "status": status,
        "observed_at_unix_ms": observed,
        "valid_until_unix_ms": observed,
        "generation": null,
        "reason_code": reason,
    })
}

fn object(value: Option<&Value>) -> Option<&Map<String, Value>> {
    value.and_then(Value::as_object)
}

fn is_true(map: &Map<String, Value>, key: &str) -> bool {
    map.get(key) == Some(&Value::Bool(true))
}

/// `str(mapping.get(key, fallback))`.
fn text_or(map: &Map<String, Value>, key: &str, fallback: &str) -> Result<String, ProjectionError> {
    map.get(key).map_or_else(|| Ok(fallback.to_owned()), py_str)
}

/// `str(value)` of a lookup key, `None` included (`public_member_ids.get(str(raw_node_id))`).
fn key_text(value: Option<&Value>) -> Result<String, ProjectionError> {
    value.map_or_else(|| Ok("None".to_owned()), py_str)
}

fn invalid(message: &str) -> ProjectionError {
    raise("ValueError", message)
}

/// The oracle's unknown A8 projection (`_unknown_internet_native`, `:45-88`).
pub fn unknown_internet_native(now: u64) -> Value {
    let activation = json!({
        "protocol": "mycelium.internet_activation_observation.v1",
        "observation_id": format!("internet-unknown-{now}"),
        "connection_generation": 0,
        "connection_reuse": 0,
        "path_class": "unknown",
        "path_source": "unknown",
        "endpoint_pseudonym": null,
        "observed_at_unix_ms": now,
        "freshness": "unknown",
        "evidence_lifetime_until_unix_ms": now,
        "metrics": {
            "rtt_ms": null, "warm_rtt_ms": null, "jitter_ms": null,
            "goodput_bytes_per_second": null, "loss_ratio": null, "sample_count": null,
            "measured_zero": false,
        },
    });
    json!({
        "bootstrap_status": {
            "protocol": "mycelium.internet_bootstrap_status.v1",
            "generation": 1,
            "observed_at_unix_ms": now,
            "freshness": "unknown",
            "tls_state": "unknown",
            "canonical_origin_verified": false,
            "seed_pin_state": "unknown",
            "route_state": "unknown",
            "invitation_state": "unknown",
            "counters": {"requests": 0, "joins_accepted": 0, "joins_rejected": 0},
        },
        "activation_observation": activation.clone(),
        "activation_history": [activation],
        "relay_projection": null,
        "qualification": null,
    })
}

/// `_recovery_aggregate` (`:201-264`): the binding and the counts of a recovery runtime projection.
fn recovery_aggregate(value: &Value) -> Result<(Value, Value), ProjectionError> {
    let refuse = || invalid("recovery_projection_invalid");
    let map = value.as_object().ok_or_else(refuse)?;
    if map.get("protocol") != Some(&json!("mycelium.full_context_recovery_runtime.v1")) {
        return Err(refuse());
    }
    let binding_map = object(map.get("binding")).ok_or_else(refuse)?;
    let requests = map
        .get("requests")
        .and_then(Value::as_array)
        .filter(|r| r.len() <= MAX_RECOVERY_REQUESTS)
        .ok_or_else(refuse)?;
    let breaker = object(map.get("breaker"))
        .and_then(|b| b.get("state"))
        .and_then(Value::as_str)
        .filter(|s| matches!(*s, "closed" | "open"))
        .ok_or_else(refuse)?;
    let (mut recovering, mut terminal_count, mut blocked) = (0u64, 0u64, 0u64);
    let (mut cleanup_count, mut reconciled_count, mut max_attempt) = (0u64, 0u64, 0u64);
    for request in requests {
        let request = request.as_object().ok_or_else(refuse)?;
        let phase = request.get("phase").and_then(Value::as_str);
        let attempt = request
            .get("recovery_attempt")
            .and_then(Value::as_u64)
            .filter(|a| *a <= MAX_RECOVERY_ATTEMPT);
        let cleanup = request.get("cleanup_complete").and_then(Value::as_bool);
        let reconciled = request.get("reconciled").unwrap_or(&Value::Null);
        let terminal = request.get("terminal").unwrap_or(&Value::Null);
        let (Some(phase), Some(attempt), Some(cleanup)) = (phase, attempt, cleanup) else {
            return Err(refuse());
        };
        if !RECOVERY_PHASES.contains(&phase)
            || !(reconciled.is_null()
                || matches!(
                    reconciled.as_str(),
                    Some("resumed" | "aborted" | "already_terminal")
                ))
            || !(terminal.is_null() || terminal.is_string())
        {
            return Err(refuse());
        }
        if !matches!(phase, "active" | "terminal" | "aborted") {
            recovering += 1;
        }
        if !terminal.is_null() {
            terminal_count += 1;
        }
        if terminal.as_str() == Some("TERMINAL_BLOCKED") {
            blocked += 1;
        }
        cleanup_count += u64::from(cleanup);
        reconciled_count += u64::from(!reconciled.is_null());
        max_attempt = max_attempt.max(attempt);
    }
    let deployment_id = binding_map
        .get("deployment_id")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .ok_or_else(refuse)?;
    let epoch = binding_map
        .get("deployment_epoch")
        .and_then(Value::as_u64)
        .filter(|e| *e >= 1)
        .ok_or_else(refuse)?;
    Ok((
        binding(
            json!(deployment_id),
            json!(epoch),
            Value::Null,
            Value::Null,
            Value::Null,
        ),
        json!({
            "request_count": requests.len(),
            "recovering_count": recovering,
            "terminal_count": terminal_count,
            "terminal_blocked_count": blocked,
            "cleanup_complete_count": cleanup_count,
            "reconciled_count": reconciled_count,
            "max_recovery_attempt": max_attempt,
            "breaker_state": breaker,
        }),
    ))
}

/// `_stage_entity_id` (`:91-97`): the placement id when a stage has replicas.
fn stage_entity_id(stage: &Map<String, Value>, index: usize) -> Result<String, ProjectionError> {
    match stage.get("placement_id") {
        Some(Value::String(id)) if !id.is_empty() => Ok(id.clone()),
        _ => text_or(stage, "stage_id", &format!("stage-{index}")),
    }
}

fn notice(id: String, scope: &str, severity: &str, code: &str, source: &str) -> Value {
    json!({"notice_id": id, "scope_id": scope, "severity": severity, "code": code, "source_id": source})
}

fn relation(id: String, kind: &str, from: &str, to: &str, source: &str) -> Value {
    json!({"relation_id": id, "kind": kind, "from_entity_id": from, "to_entity_id": to, "source_id": source})
}

fn entity(
    id: &str,
    kind: &str,
    label: &str,
    source: &str,
    binding: &Value,
    fresh: Value,
    attributes: Value,
) -> Value {
    json!({
        "entity_id": id, "kind": kind, "label": label, "source_id": source,
        "binding": binding.clone(), "freshness": fresh, "attributes": attributes,
    })
}

fn source_protocol(source: &str) -> &'static str {
    match source {
        "membership-source" => "mycelium.membership.signed_message.v1",
        "assignment-source" => "mycelium.membership.assignment_offer.v1",
        "route-source" | "runtime-source" => "mycelium.live_route_status.v1",
        "qualification-source" => "mycelium.route_qualification.v1",
        "planner-source" => "mycelium.layer_planner_snapshot.v1",
        "transport-path-source" => "mycelium.transport_path_observation.v1",
        "artifact-source" => "mycelium.physical_deployment.v1",
        "request-source" => "mycelium.observatory.request_projection.v1",
        "recovery-source" => "mycelium.full_context_recovery_runtime.v1",
        _ => "mycelium.a7_product_evidence.v1",
    }
}

/// `mycelium_deadlines.COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS`: the cancel-and-cleanup budget in ms.
fn cleanup_bound_ms() -> Result<u64, ProjectionError> {
    BudgetId::CancelAndCleanup
        .span(Entries::new(0))
        .map(|span| span.millis())
        .map_err(|_| invalid("product_projection_budget_unavailable"))
}

impl ProductProjector {
    /// `restore_publication` (`:282-298`): resume a durable sequence before the first projection.
    pub fn restore_publication(&self, generation: u64, cursor: u64) -> Result<(), ProjectionError> {
        let mut state = self.lock()?;
        if state.generation != 0 || state.cursor != 0 {
            return Err(invalid("product_publication_already_started"));
        }
        state.generation = generation;
        state.cursor = cursor;
        Ok(())
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, Publication>, ProjectionError> {
        self.publication
            .lock()
            .map_err(|_| raise("RuntimeError", "product_projector_poisoned"))
    }

    /// `project` (`:308-382`): one validated snapshot with its route explorer; the sequence advances
    /// only when the snapshot validates.
    pub fn project(&self, input: &ProjectionInput<'_>) -> Result<Value, ProjectionError> {
        if input
            .source_errors
            .iter()
            .any(|(source, reason)| !ERROR_SOURCES.contains(&source.as_str()) || !code(reason))
        {
            return Err(invalid("product_projection_input_invalid"));
        }
        let mut state = self.lock()?;
        let generation = state.generation + 1;
        let cursor = state.cursor + 1;
        let mut snapshot = self.build(input, &state, generation, cursor)?;
        let checked =
            |snapshot: &Value| protocol::product::snapshot(snapshot).map_err(|e| invalid(e.0));
        checked(&snapshot)?;
        let explorer = protocol::explorer::project_from_validated_snapshot(&snapshot)
            .map_err(|e| invalid(e.0))?;
        snapshot["route_explorer"] = explorer;
        checked(&snapshot)?;
        state.generation = generation;
        state.cursor = cursor;
        if let Some(recovery) = snapshot["entities"].as_array().and_then(|entities| {
            entities.iter().find(|e| {
                e["entity_id"] == "recovery-runtime" && e["freshness"]["status"] == "current"
            })
        }) {
            state.last_recovery = Some((
                recovery["binding"].clone(),
                recovery["attributes"].clone(),
                recovery["freshness"]["observed_at_unix_ms"].clone(),
            ));
        }
        Ok(snapshot)
    }

    #[allow(clippy::too_many_lines)]
    fn build(
        &self,
        input: &ProjectionInput<'_>,
        state: &Publication,
        generation: u64,
        cursor: u64,
    ) -> Result<Value, ProjectionError> {
        let now = input.now_unix_ms;
        let errors = input.source_errors;
        let error = |id: &str| errors.get(id).map(String::as_str);
        let route = object(input.route_status)
            .filter(|r| r.get("protocol") == Some(&json!("mycelium.live_route_status.v1")));
        let route_current = route.is_some();
        let qualification = object(input.qualification)
            .filter(|q| q.get("protocol") == Some(&json!("mycelium.route_qualification.v1")));
        let qualification_bound = match (qualification, route) {
            (Some(q), Some(r)) => ["deployment_id", "model_id", "topology_version"]
                .iter()
                .all(|k| q.get(*k) == r.get(*k)),
            _ => false,
        };
        let qualification_current = qualification.is_some() && qualification_bound;
        let member_current = !input.members.is_empty();
        let recovery_error = error("recovery-source");
        let recovery_doc = input.recovery_status.filter(|v| v.is_object());
        let recovery_current = recovery_error.is_none() && recovery_doc.is_some();
        let mut recovery = match recovery_doc {
            Some(doc) if recovery_current => Some(recovery_aggregate(doc)?),
            _ => None,
        };
        let (membership_error, assignment_error) =
            (error("membership-source"), error("assignment-source"));
        let (route_error, qualification_error) =
            (error("route-source"), error("qualification-source"));
        let (qualification_status, qualification_reason) = if let Some(e) = qualification_error {
            ("missing", Some(e))
        } else if qualification.is_none() {
            ("missing", Some("qualification_source_missing"))
        } else if qualification_bound {
            ("current", None)
        } else {
            ("conflict", Some("qualification_binding_conflict"))
        };
        let assignments = input.assignments;
        let mut sources = vec![
            source_state(
                "membership-source",
                "seed_coordinator",
                if member_current && membership_error.is_none() {
                    "current"
                } else {
                    "missing"
                },
                now,
                membership_error.or((!member_current).then_some("membership_source_missing")),
            ),
            match assignments {
                None => source_state(
                    "assignment-source",
                    "physical_assignment_authority",
                    "unsupported",
                    now,
                    Some("assignment_projection_unsupported"),
                ),
                Some(rows) => source_state(
                    "assignment-source",
                    "physical_assignment_authority",
                    if !rows.is_empty() && assignment_error.is_none() {
                        "current"
                    } else {
                        "missing"
                    },
                    now,
                    assignment_error.or(rows.is_empty().then_some("assignment_source_missing")),
                ),
            },
            source_state(
                "route-source",
                "live_router_status",
                if route_current && route_error.is_none() {
                    "current"
                } else {
                    "missing"
                },
                now,
                route_error.or((!route_current).then_some("route_source_missing")),
            ),
            source_state(
                "qualification-source",
                "route_qualification_authority",
                qualification_status,
                now,
                qualification_reason,
            ),
            source_state(
                "recovery-source",
                "request_gateway_recovery",
                if recovery_current {
                    "current"
                } else {
                    "missing"
                },
                now,
                recovery_error.or((!recovery_current).then_some("recovery_source_missing")),
            ),
        ];
        for (id, authority, reason) in [
            (
                "planner-source",
                "unsupported_until_m13",
                "planner_projection_unsupported",
            ),
            (
                "transport-path-source",
                "unsupported_until_m14",
                "transport_path_unsupported",
            ),
            (
                "artifact-source",
                "artifact_inventory_adapter_pending",
                "artifact_inventory_unavailable",
            ),
            (
                "runtime-source",
                "runtime_kv_adapter_pending",
                "runtime_kv_projection_unavailable",
            ),
            (
                "request-source",
                "request_lifecycle_adapter_pending",
                "request_projection_unavailable",
            ),
        ] {
            sources.push(source_state(
                id,
                authority,
                "unsupported",
                now,
                Some(reason),
            ));
        }
        let empty = Vec::new();
        let stages: &Vec<Value> = route
            .and_then(|r| r.get("stages"))
            .and_then(Value::as_array)
            .unwrap_or(&empty);

        let mut entities: Vec<Value> = Vec::new();
        let mut recovery_fresh = current(now);
        if !recovery_current
            && recovery_error.is_some()
            && let Some((b, a, observed)) = &state.last_recovery
        {
            recovery = Some((b.clone(), a.clone()));
            recovery_fresh = freshness("stale", observed.clone(), observed.clone());
        }
        if let Some((b, a)) = &recovery {
            entities.push(entity(
                "recovery-runtime",
                "recovery",
                "Full-context replay recovery",
                "recovery-source",
                b,
                recovery_fresh,
                a.clone(),
            ));
        }
        let mut relations: Vec<Value> = Vec::new();
        let mut readiness: Vec<Value> = Vec::new();
        let mut notices: Vec<Value> = Vec::new();
        for source in &sources {
            let status = source["status"].as_str().unwrap_or_default();
            if matches!(status, "missing" | "conflict" | "stale") {
                notices.push(notice(
                    format!(
                        "source-{}-{status}",
                        source["source_id"].as_str().unwrap_or_default()
                    ),
                    "product-root",
                    if status == "conflict" {
                        "error"
                    } else {
                        "warning"
                    },
                    source["reason_code"].as_str().unwrap_or_default(),
                    source["source_id"].as_str().unwrap_or_default(),
                ));
            }
        }
        // The seed-rotation incident (`:653-689`): exactly one distinct rotation record.
        let mut rotations: Vec<(Value, String, f64)> = Vec::new();
        for member in input.members {
            if member.get("rotation_status").is_some_and(|v| !v.is_null()) {
                let record = (
                    int_field(member, "authority_generation", 1)?,
                    safe_code(member.get("rotation_status"), "absent"),
                    millis(member, "rotation_observed_at")?,
                );
                if !rotations.contains(&record) {
                    rotations.push(record);
                }
            }
        }
        if let [(authority_generation, status, observed)] = rotations.as_slice()
            && matches!(status.as_str(), "pending" | "completed")
        {
            let rotation_code = format!("seed_rotation_{status}");
            notices.push(notice(
                format!("seed-rotation-{authority_generation}"),
                "product-root",
                if status == "pending" {
                    "warning"
                } else {
                    "info"
                },
                &rotation_code,
                "membership-source",
            ));
            let observed = int_json(observed.max(0.0));
            entities.push(entity(
                &format!("incident-seed-rotation-{authority_generation}"),
                "incident",
                "Seed authority rotation",
                "membership-source",
                &empty_binding(),
                freshness("current", observed.clone(), observed.clone()),
                json!({"state": rotation_code, "reason_code": rotation_code, "observed_at_unix_ms": observed}),
            ));
        }
        let members = self.project_members(input.members, stages, now)?;
        entities.extend(members.entities);
        readiness.extend(members.readiness);
        notices.extend(members.notices);
        let public = &members.public_member_ids;
        let device = |raw: Option<&Value>| -> Result<Option<String>, ProjectionError> {
            Ok(public.get(&key_text(raw)?).cloned())
        };

        let placed: BTreeSet<&str> = stages
            .iter()
            .filter_map(|s| s.get("node_id").and_then(Value::as_str))
            .collect();
        let present = placed
            .iter()
            .all(|n| members.current_raw_members.contains(*n));
        let eligible = placed
            .iter()
            .all(|n| members.eligible_raw_members.contains(*n));
        let membership_ready = !placed.is_empty() && present && eligible;
        let mut route_id: Option<String> = None;
        let mut route_binding = empty_binding();
        let mut a4: Option<&Map<String, Value>> = None;
        let (mut concurrency_eligible, mut scoped_liveness_proven) = (false, false);
        if let Some(route) = route {
            a4 = object(route.get("concurrency_liveness_qualification"));
            concurrency_eligible = a4.is_some_and(|a| is_true(a, "eligible"));
            let fenced = a4.is_some_and(|a| is_true(a, "publisher_generation_fencing_proven"));
            scoped_liveness_proven = a4.is_some_and(|a| is_true(a, "scoped_liveness_proven"));
            let identity = match route.get("route_identity_digest") {
                Some(v) if digest_text(v) => v.as_str().unwrap_or_default().to_owned(),
                _ => digest(input.route_status.unwrap_or(&Value::Null)),
            };
            let id = format!("route-{}", &identity["sha256:".len()..][..ROUTE_ID_HEX]);
            let deployment_id = text_or(route, "deployment_id", "deployment-unknown")?;
            let topology_version = int_field(route, "topology_version", 0)?;
            let epoch = match qualification {
                Some(q) if qualification_current => int_field(q, "deployment_epoch", 0)?,
                _ => Value::Null,
            };
            route_binding = binding(
                json!(deployment_id),
                epoch,
                json!(id),
                topology_version.clone(),
                topology_version.clone(),
            );
            let decode_mode = text_or(route, "decode_mode", "complete_context_replay")?;
            let provenance = match qualification {
                Some(q) if qualification_current => {
                    text_or(q, "placement_provenance", "operator_selected")?
                }
                _ => "operator_selected".to_owned(),
            };
            let cleanup_bound = match a4 {
                Some(a) if a.contains_key("cancellation_and_cleanup_bound_ms") => {
                    int_field(a, "cancellation_and_cleanup_bound_ms", 0)?
                }
                _ => Value::from(cleanup_bound_ms()?),
            };
            entities.push(entity(
                &id,
                "route",
                "Distributed inference route",
                "route-source",
                &route_binding,
                current(now),
                json!({
                    "deployment_id": deployment_id,
                    "model_id": text_or(route, "model_id", "unknown-model")?,
                    "topology_version": topology_version,
                    "decode_mode": decode_mode,
                    "placement_provenance": provenance,
                    "route_alive": is_true(route, "route_alive"),
                    "concurrency_eligible": concurrency_eligible,
                    "cancellation_cleanup_bound_ms": cleanup_bound,
                    "publisher_generation_fenced": fenced,
                    "scoped_liveness_proven": scoped_liveness_proven,
                }),
            ));
            let mut stage_ids = BTreeSet::new();
            for (index, stage) in stages.iter().enumerate() {
                let Some(stage) = stage.as_object() else {
                    continue;
                };
                let stage_id = stage_entity_id(stage, index)?;
                stage_ids.insert(stage_id.clone());
                entities.push(entity(
                    &stage_id,
                    "stage",
                    &format!("Inference stage {}", index + 1),
                    "route-source",
                    &route_binding,
                    current(now),
                    json!({
                        "stage_index": index,
                        "start_layer": int_field(stage, "start_layer", 0)?,
                        "end_layer_exclusive": int_field(stage, "end_layer_exclusive", 1)?,
                        "component_roles": stage.get("component_roles").cloned().unwrap_or(json!([])),
                        "decode_mode": decode_mode,
                    }),
                ));
                relations.push(relation(
                    format!("stage-route-{index}"),
                    "assigned_to",
                    &stage_id,
                    &id,
                    "route-source",
                ));
                if let Some(device_id) = device(stage.get("node_id"))? {
                    relations.push(relation(
                        format!("stage-device-{index}"),
                        "placed_on",
                        &stage_id,
                        &device_id,
                        "route-source",
                    ));
                }
            }
            if let Some(rows) = assignments {
                let ready = qualification_current
                    && qualification.is_some_and(|q| is_true(q, "route_ready"));
                let (mut seen, mut seen_proofs) = (BTreeSet::new(), BTreeSet::new());
                for (index, row) in rows.iter().enumerate() {
                    let rejected = || {
                        notice(
                            format!("assignment-invalid-{index}"),
                            &id,
                            "error",
                            "assignment_record_invalid",
                            "assignment-source",
                        )
                    };
                    let assignment_id = row.get("assignment_id").and_then(Value::as_str);
                    let stage_id = row.get("stage_id").and_then(Value::as_str);
                    let device_id = device(row.get("node_id"))?;
                    let integer = |k: &str| row.get(k).is_some_and(|v| v.is_i64() || v.is_u64());
                    let (Some(assignment_id), Some(stage_id), Some(device_id)) =
                        (assignment_id, stage_id, device_id)
                    else {
                        notices.push(rejected());
                        continue;
                    };
                    let well_formed = stage_ids.contains(stage_id)
                        && !seen.contains(assignment_id)
                        && integer("membership_generation")
                        && integer("load_generation")
                        && [
                            "assignment_digest",
                            "stage_pack_digest",
                            "load_proof_digest",
                        ]
                        .iter()
                        .all(|k| row.get(*k).is_some_and(digest_text));
                    if !well_formed {
                        notices.push(rejected());
                        continue;
                    }
                    let proof_digest = row["load_proof_digest"].as_str().unwrap_or_default();
                    let proof_id = format!(
                        "load-proof-{}",
                        &proof_digest["sha256:".len().."sha256:".len() + ROUTE_ID_HEX]
                    );
                    if seen_proofs.contains(&proof_id) {
                        notices.push(rejected());
                        continue;
                    }
                    seen.insert(assignment_id.to_owned());
                    seen_proofs.insert(proof_id.clone());
                    entities.push(entity(
                        assignment_id,
                        "assignment",
                        "Validated stage assignment",
                        "assignment-source",
                        &route_binding,
                        current(now),
                        json!({
                            "device_id": device_id,
                            "stage_id": stage_id,
                            "membership_generation": row["membership_generation"],
                            "load_generation": row["load_generation"],
                            "assignment_digest": row["assignment_digest"],
                            "stage_pack_digest": row["stage_pack_digest"],
                        }),
                    ));
                    entities.push(entity(
                        &proof_id,
                        "load_proof",
                        "Qualified stage load proof",
                        "assignment-source",
                        &route_binding,
                        current(now),
                        json!({
                            "proof_digest": proof_digest,
                            "assignment_id": assignment_id,
                            "load_generation": row["load_generation"],
                            "ready": ready,
                        }),
                    ));
                    relations.push(relation(
                        format!("assignment-stage-{index}"),
                        "assigned_to",
                        assignment_id,
                        stage_id,
                        "assignment-source",
                    ));
                    relations.push(relation(
                        format!("assignment-device-{index}"),
                        "placed_on",
                        assignment_id,
                        &device_id,
                        "assignment-source",
                    ));
                    relations.push(relation(
                        format!("load-proof-assignment-{index}"),
                        "reports",
                        &proof_id,
                        assignment_id,
                        "assignment-source",
                    ));
                }
            }
            for (index, pair) in stages.windows(2).enumerate() {
                let (Some(source), Some(destination)) = (pair[0].as_object(), pair[1].as_object())
                else {
                    continue;
                };
                if source.get("stage_id").is_some_and(Value::is_string)
                    && source.get("stage_id") == destination.get("stage_id")
                {
                    continue;
                }
                let (Some(src), Some(dst)) = (
                    device(source.get("node_id"))?,
                    device(destination.get("node_id"))?,
                ) else {
                    continue;
                };
                if src == dst {
                    continue;
                }
                entities.push(entity(
                    &format!("directed-link-{index}"),
                    "directed_link",
                    "Activation-plane directed link",
                    "route-source",
                    &route_binding,
                    current(now),
                    json!({"src_device_id": src, "dst_device_id": dst, "connectivity": "unknown",
                           "measurement_digest": null}),
                ));
            }
            let incidents = route
                .get("incidents")
                .and_then(Value::as_array)
                .unwrap_or(&empty);
            for (index, incident) in incidents.iter().enumerate() {
                let Some(incident) = incident.as_object() else {
                    continue;
                };
                let observed = if incident.contains_key("observed_at_unix_ms") {
                    int_field(incident, "observed_at_unix_ms", 0)?
                } else {
                    json!(now)
                };
                entities.push(entity(
                    &format!("incident-{}", index + 1),
                    "incident",
                    "Route incident",
                    "route-source",
                    &route_binding,
                    freshness("current", observed.clone(), json!(now)),
                    json!({
                        "state": safe_code(incident.get("state"), "route_incident"),
                        "reason_code": safe_code(incident.get("reason"), "upstream_reason_redacted"),
                        "observed_at_unix_ms": observed,
                    }),
                ));
            }
            route_id = Some(id);
        }
        let qualification_ready = if let Some(q) = qualification {
            let qualification_id = text_or(q, "qualification_id", "qualification-unknown")?;
            let issued_at = int_field(q, "issued_at_unix_ms", 0)?;
            let authority_ready = is_true(q, "route_ready");
            let reasons: Vec<String> = q
                .get("reason_codes")
                .and_then(Value::as_array)
                .unwrap_or(&empty)
                .iter()
                .map(|r| safe_code(Some(r), "upstream_reason_redacted"))
                .collect();
            entities.push(entity(
                &qualification_id,
                "qualification",
                "Route qualification",
                "qualification-source",
                &route_binding,
                freshness(qualification_status, issued_at.clone(), Value::Null),
                json!({
                    "qualification_digest": digest(input.qualification.unwrap_or(&Value::Null)),
                    "route_ready": authority_ready,
                    "issued_at_unix_ms": issued_at,
                    "expires_at_unix_ms": null,
                    "reason_codes": reasons,
                }),
            ));
            if let Some(route_id) = route_id.as_deref().filter(|_| qualification_bound) {
                relations.push(relation(
                    "qualification-route".to_owned(),
                    "qualifies",
                    &qualification_id,
                    route_id,
                    "qualification-source",
                ));
            }
            authority_ready && membership_ready && route.is_some_and(|r| is_true(r, "route_alive"))
        } else {
            false
        };
        let scope = route_id.as_deref().unwrap_or("product-root");
        let row = |dimension: &str,
                   state: &str,
                   reason: Option<&str>,
                   source: &str,
                   scope: &str| {
            json!({"scope_id": scope, "dimension": dimension, "state": state, "reason_code": reason,
                   "source_id": source})
        };
        let (artifacts_state, artifacts_reason) = match assignments {
            None => ("unsupported", Some("assignment_projection_unsupported")),
            Some(rows) if !rows.is_empty() && qualification_current => ("ready", None),
            Some(_) => ("not_ready", Some("load_proof_unavailable")),
        };
        let a4_seen = route_current && a4.is_some();
        readiness.extend([
            row(
                "artifacts",
                artifacts_state,
                artifacts_reason,
                "assignment-source",
                scope,
            ),
            if membership_ready {
                row("membership", "ready", None, "membership-source", scope)
            } else if present {
                row(
                    "membership",
                    "not_ready",
                    Some("placement_member_ineligible"),
                    "membership-source",
                    scope,
                )
            } else {
                row(
                    "membership",
                    "not_ready",
                    Some("placement_member_missing"),
                    "membership-source",
                    scope,
                )
            },
            if route_current && scoped_liveness_proven {
                row("transport", "ready", None, "transport-path-source", scope)
            } else if a4_seen {
                row(
                    "transport",
                    "not_ready",
                    Some("scoped_liveness_unqualified"),
                    "transport-path-source",
                    scope,
                )
            } else {
                row(
                    "transport",
                    "unknown",
                    Some("transport_path_observation_unavailable"),
                    "transport-path-source",
                    scope,
                )
            },
            if route_current && concurrency_eligible {
                row("runtime", "ready", None, "runtime-source", scope)
            } else if a4_seen {
                row(
                    "runtime",
                    "not_ready",
                    Some("concurrency_liveness_unqualified"),
                    "runtime-source",
                    scope,
                )
            } else {
                row(
                    "runtime",
                    "unknown",
                    Some("runtime_qualification_unavailable"),
                    "runtime-source",
                    scope,
                )
            },
            if qualification_ready {
                row(
                    "qualification",
                    "ready",
                    None,
                    "qualification-source",
                    scope,
                )
            } else {
                row(
                    "qualification",
                    "not_ready",
                    Some("coherent_qualification_unavailable"),
                    "qualification-source",
                    scope,
                )
            },
            if route_current && member_current {
                row(
                    "product_source",
                    "ready",
                    None,
                    "route-source",
                    "product-root",
                )
            } else {
                row(
                    "product_source",
                    "not_ready",
                    Some("product_source_incomplete"),
                    "route-source",
                    "product-root",
                )
            },
        ]);
        for source in &sources {
            let id = source["source_id"].as_str().unwrap_or_default();
            entities.push(entity(
                &format!("provenance-{id}"),
                "source_provenance",
                "Evidence source",
                id,
                &empty_binding(),
                freshness(
                    source["status"].as_str().unwrap_or_default(),
                    source["observed_at_unix_ms"].clone(),
                    source["valid_until_unix_ms"].clone(),
                ),
                json!({
                    "authority": source["authority"],
                    "source_protocol": source_protocol(id),
                    "source_generation": source["generation"],
                    "evidence_digest": null,
                }),
            ));
        }
        let internet_native = unknown_internet_native(now);
        let material = json!({
            "generation": generation,
            "cursor": cursor,
            "source_states": sources,
            "entities": entities,
            "relations": relations,
            "readiness": readiness,
            "notices": notices,
            "internet_native": internet_native,
        });
        let snapshot_digest = digest(&material);
        let degraded = sources.iter().any(|s| {
            DEGRADING_SOURCES.contains(&s["source_id"].as_str().unwrap_or_default())
                && matches!(s["status"].as_str(), Some("missing" | "stale" | "conflict"))
        });
        let mode = if degraded { "degraded" } else { "live" };
        let Value::Object(mut material) = material else {
            return Err(invalid("product_projection_input_invalid"));
        };
        let mut take = |k: &str| material.remove(k).unwrap_or(Value::Null);
        Ok(json!({
            "protocol": "mycelium.product_snapshot.v1",
            "publication": {
                "snapshot_id": format!(
                    "snapshot-{generation}-{}",
                    &snapshot_digest["sha256:".len().."sha256:".len() + SNAPSHOT_ID_HEX]
                ),
                "generation": generation,
                "cursor": cursor,
                "published_at_unix_ms": now,
                "source_mode": mode,
            },
            "supported_entity_kinds": ENTITY_KINDS,
            "source_states": take("source_states"),
            "entities": take("entities"),
            "relations": take("relations"),
            "readiness": take("readiness"),
            "notices": take("notices"),
            "internet_native": take("internet_native"),
            "provenance": {
                "projector": "mycelium_product_spine",
                "projector_version": "m12-v1",
                "source_mode": mode,
            },
        }))
    }
}
