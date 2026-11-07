// SPDX-License-Identifier: AGPL-3.0-only
//! Closed v0.1.4 cancellation/liveness and privacy-reduced transport documents.
//! Shape validation is not execution, liveness, or qualification evidence.
use crate::{ContractError, checks::*};
use serde_json::Value;
use std::collections::BTreeSet;

type Result = std::result::Result<(), ContractError>;
fn fields(v: &Value, names: &str, code: &'static str) -> Result {
    exact(v, &names.split_whitespace().collect::<Vec<_>>(), code)
}
fn bounded(v: &Value, max: usize) -> bool {
    v.as_str().is_some_and(|s| !s.is_empty() && s.len() <= max)
}
fn int(v: &Value, min: u64) -> bool {
    v.as_u64().is_some_and(|n| n >= min && n <= i64::MAX as u64)
}
fn arr(v: &Value, max: usize) -> Option<&Vec<Value>> {
    v.as_array().filter(|a| a.len() <= max)
}
fn nullable_int(v: &Value) -> bool {
    v.is_null() || int(v, 0)
}
fn unique(a: &[Value]) -> bool {
    (0..a.len()).all(|i| !a[..i].contains(&a[i]))
}
fn name(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.len() <= 96
            && s.as_bytes()[0].is_ascii_lowercase()
            && s.bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
    })
}
fn names(v: &Value, max: usize) -> bool {
    arr(v, max).is_some_and(|a| !a.is_empty() && unique(a) && a.iter().all(name))
}
fn privacy(v: &Value, code: &'static str) -> Result {
    if let Some(m) = v.as_object() {
        for (k, child) in m {
            require(
                ![
                    "hostname",
                    "host",
                    "address",
                    "ip_address",
                    "url",
                    "relay_url",
                    "dns_name",
                    "endpoint_id",
                    "private_path",
                    "credential",
                    "credentials",
                    "secret",
                    "password",
                    "token",
                    "bearer",
                    "authorization",
                    "cookie",
                    "prompt",
                    "output",
                    "logit",
                    "logits",
                    "tensor",
                    "tensors",
                    "activation",
                    "activations",
                ]
                .contains(&k.as_str())
                    && !((k.starts_with("kv_") || k.starts_with("key_value"))
                        && k.bytes()
                            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')),
                code,
            )?;
            privacy(child, code)?;
        }
    }
    Ok(())
}
const SOURCES: &[&str] = &[
    "application_receipt",
    "activation_receipt",
    "signed_keepalive",
    "idle_keepalive",
    "command_deadline",
    "active_transport_failure",
    "membership_exit",
    "worker_exception",
    "deployment_fatal",
];
const FATAL: &[&str] = &[
    "immutable_authority_contradiction",
    "deployment_resource_ledger_corruption",
    "active_authority_compromise",
    "all_qualified_tracks_lost",
];
const FRESH: &[&str] = &["current", "stale", "unknown"];
/// `cleanup_max_ms` must be injected from BudgetId::CancelAndCleanup (L1).
pub fn validate(v: &Value, cleanup_max_ms: u64) -> Result {
    if let Some(p) = v["protocol"].as_str().filter(|p| {
        p.starts_with("mycelium.full_context_recovery_")
            || *p == "mycelium.private_request_replay_checkpoint.v1"
    }) {
        return crate::recovery::validate_as(p, v);
    }
    if v["protocol"] == "mycelium.product_ui.bootstrap.v1" {
        return crate::public::bootstrap(v);
    }
    if v["protocol"] == "mycelium.manual_provisioning_route.v1" {
        return crate::provisioning::manual_route(v);
    }
    if let Some(p) = v["protocol"]
        .as_str()
        .filter(|p| p.starts_with("mycelium.replica_"))
    {
        return crate::replica::validate_as(p, v);
    }
    match v["protocol"].as_str() {
        Some("mycelium.assignment_artifact_cache.v1") => crate::public_evidence::cache(v),
        Some("mycelium.assignment_materialization.v1") => {
            crate::public_evidence::materialization(v)
        }
        Some("mycelium.concurrent_request_runtime.v1") => crate::public_evidence::runtime(v),
        Some("mycelium.m13_placement_projection.v1") => crate::public_evidence::placement(v),
        Some("mycelium.m14_topology_projection.v1") => crate::public_evidence::topology(v),
        Some("mycelium.m15_plan_comparison.v1") => crate::public_evidence::comparison(v),
        Some("mycelium.route_plan.v2") => crate::route_contract::plan(v).map(|_| ()),
        Some("mycelium.route_qualification.v1") => {
            crate::route_contract::unqualified(v).map(|_| ())
        }
        Some("mycelium.m23_heterogeneous_kv_gate.v1") => crate::public_evidence::kv_gate(v),
        Some("mycelium.candidate_promotion_report.v1") => crate::promotion::parse(v).map(|_| ()),
        Some("mycelium.evidence_projection.v1") => crate::projection::validate(v),
        Some("mycelium.observatory.snapshot.v1") => crate::observatory::snapshot(v),
        Some("mycelium.observatory.event.v1") => crate::observatory::event(v),
        Some("mycelium.capacity_profile.v1") => crate::capacity::validate(v),
        Some(p) if p.starts_with("mycelium.performance_budget.") => {
            crate::performance::validate_as(p, v)
        }
        Some("mycelium.product_snapshot.v1") => crate::product::snapshot(v),
        Some("mycelium.product_event.v1") => crate::product::event(v),
        Some("mycelium.route_explorer.v1") => crate::explorer::validate(v),
        Some("mycelium.deployment_activation.v1") => crate::public::activation(v),
        Some("mycelium.product_ui.swarm.v1") => crate::public::swarm(v),
        Some("mycelium.interruptible_stage_command.v1") => command(v, cleanup_max_ms),
        Some("mycelium.traffic_liveness.v1") => liveness(v),
        Some("mycelium.scoped_runtime_incident.v1") => incident(v),
        Some("mycelium.product_concurrency_liveness_qualification.v1") => {
            qualification(v, cleanup_max_ms)
        }
        Some("mycelium.internet_bootstrap_status.v1") => bootstrap(v),
        Some("mycelium.internet_activation_observation.v1") => activation(v),
        Some("mycelium.relay_projection.v1") => relay(v),
        Some("mycelium.internet_native_qualification.v1") => internet_qualification(v),
        _ => Err(ContractError("unsupported_contract_protocol")),
    }
}
// Protocol-specific entry points also retain oracle error codes on wrong protocol.
pub fn validate_as(protocol: &str, v: &Value, cleanup_max_ms: u64) -> Result {
    if protocol.starts_with("mycelium.full_context_recovery_")
        || protocol == "mycelium.private_request_replay_checkpoint.v1"
    {
        return crate::recovery::validate_as(protocol, v);
    }
    if protocol == "mycelium.product_ui.bootstrap.v1" {
        return crate::public::bootstrap(v);
    }
    if protocol == "mycelium.manual_provisioning_route.v1" {
        return crate::provisioning::manual_route(v);
    }
    if protocol.starts_with("mycelium.replica_") {
        return crate::replica::validate_as(protocol, v);
    }
    match protocol {
        "mycelium.assignment_artifact_cache.v1" => return crate::public_evidence::cache(v),
        "mycelium.assignment_materialization.v1" => {
            return crate::public_evidence::materialization(v);
        }
        "mycelium.concurrent_request_runtime.v1" => return crate::public_evidence::runtime(v),
        "mycelium.m13_placement_projection.v1" => return crate::public_evidence::placement(v),
        "mycelium.m14_topology_projection.v1" => return crate::public_evidence::topology(v),
        "mycelium.m15_plan_comparison.v1" => return crate::public_evidence::comparison(v),
        "mycelium.route_plan.v2" => return crate::route_contract::plan(v).map(|_| ()),
        "mycelium.route_qualification.v1" => {
            return crate::route_contract::unqualified(v).map(|_| ());
        }
        "mycelium.m23_heterogeneous_kv_gate.v1" => return crate::public_evidence::kv_gate(v),
        "mycelium.candidate_promotion_report.v1" => return crate::promotion::parse(v).map(|_| ()),
        "mycelium.evidence_projection.v1" => return crate::projection::validate(v),
        "mycelium.observatory.snapshot.v1" => return crate::observatory::snapshot(v),
        "mycelium.observatory.event.v1" => return crate::observatory::event(v),
        "mycelium.capacity_profile.v1" => return crate::capacity::validate(v),
        p if p.starts_with("mycelium.performance_budget.") => {
            return crate::performance::validate_as(p, v);
        }
        "mycelium.product_snapshot.v1" => return crate::product::snapshot(v),
        "mycelium.product_event.v1" => return crate::product::event(v),
        "mycelium.route_explorer.v1" => return crate::explorer::validate(v),
        "mycelium.deployment_activation.v1" => return crate::public::activation(v),
        "mycelium.product_ui.swarm.v1" => return crate::public::swarm(v),
        _ => {}
    }
    let code = match protocol {
        "mycelium.interruptible_stage_command.v1" => "invalid_interruptible_stage_command",
        "mycelium.traffic_liveness.v1" => "invalid_traffic_liveness",
        "mycelium.scoped_runtime_incident.v1" => "invalid_scoped_runtime_incident",
        "mycelium.product_concurrency_liveness_qualification.v1" => {
            "invalid_product_concurrency_liveness_qualification"
        }
        "mycelium.internet_bootstrap_status.v1" => "internet_bootstrap_status_invalid",
        "mycelium.internet_activation_observation.v1" => "internet_activation_observation_invalid",
        "mycelium.relay_projection.v1" => "relay_projection_invalid",
        "mycelium.internet_native_qualification.v1" => "internet_native_qualification_invalid",
        _ => return Err(ContractError("unsupported_contract_protocol")),
    };
    require(v["protocol"] == protocol, code)?;
    validate(v, cleanup_max_ms)
}
fn command(v: &Value, max: u64) -> Result {
    let c = "invalid_interruptible_stage_command";
    fields(
        v,
        "protocol deployment_id deployment_epoch qualification_digest request_id request_attempt path_id path_attempt path_digest topology_generation command_id stage_id placement_id assignment_id command_kind issued_at_ms idempotency_digest cancellation_generation publisher_generation absolute_deadline_ms cooperative_step_ms cleanup_owner_id maximum_request_bytes maximum_response_bytes expected_terminal_revision",
        c,
    )?;
    for f in [
        "deployment_id",
        "request_id",
        "path_id",
        "command_id",
        "stage_id",
        "placement_id",
        "assignment_id",
        "cleanup_owner_id",
    ] {
        require(bounded(&v[f], 256), c)?;
    }
    for f in ["qualification_digest", "path_digest", "idempotency_digest"] {
        require(digest(&v[f]), c)?;
    }
    for f in [
        "deployment_epoch",
        "request_attempt",
        "topology_generation",
        "publisher_generation",
        "absolute_deadline_ms",
        "cooperative_step_ms",
        "maximum_request_bytes",
        "maximum_response_bytes",
    ] {
        require(int(&v[f], 1), c)?;
    }
    for f in [
        "path_attempt",
        "cancellation_generation",
        "issued_at_ms",
        "expected_terminal_revision",
    ] {
        require(int(&v[f], 0), c)?;
    }
    require(
        one_of(
            &v["command_kind"],
            &["prefill", "decode", "cleanup", "shutdown", "probe"],
        ) && v["issued_at_ms"].as_u64() < v["absolute_deadline_ms"].as_u64()
            && v["expected_terminal_revision"] == 0
            && v["cooperative_step_ms"].as_u64().is_some_and(|n| n <= max)
            && v["maximum_request_bytes"]
                .as_u64()
                .is_some_and(|n| n <= 1 << 30)
            && v["maximum_response_bytes"]
                .as_u64()
                .is_some_and(|n| n <= 1 << 34),
        c,
    )
}
fn liveness(v: &Value) -> Result {
    let c = "invalid_traffic_liveness";
    fields(
        v,
        "protocol deployment_id generated_at_monotonic_ms subjects incidents deployment_fatal_reason",
        c,
    )?;
    require(
        bounded(&v["deployment_id"], 256)
            && int(&v["generated_at_monotonic_ms"], 0)
            && (v["deployment_fatal_reason"].is_null()
                || one_of(&v["deployment_fatal_reason"], FATAL)),
        c,
    )?;
    let subjects = arr(&v["subjects"], 4096).ok_or(ContractError(c))?;
    let incidents = arr(&v["incidents"], 256).ok_or(ContractError(c))?;
    let mut ids = BTreeSet::new();
    for s in subjects {
        fields(
            s,
            "subject_id kind membership_generation state last_fresh_ms last_observed_ms next_keepalive_due_ms consecutive_misses last_source",
            c,
        )?;
        require(
            bounded(&s["subject_id"], 256)
                && ids.insert(s["subject_id"].as_str())
                && one_of(&s["kind"], &["edge", "placement", "peer", "deployment"])
                && int(&s["membership_generation"], 1)
                && one_of(
                    &s["state"],
                    &["fresh", "suspect", "quarantined", "failed", "recovered"],
                )
                && one_of(&s["last_source"], SOURCES),
            c,
        )?;
        for f in [
            "last_fresh_ms",
            "last_observed_ms",
            "next_keepalive_due_ms",
            "consecutive_misses",
        ] {
            require(int(&s[f], 0), c)?;
        }
        require(
            s["last_fresh_ms"].as_u64() <= s["last_observed_ms"].as_u64()
                && s["last_observed_ms"].as_u64() <= v["generated_at_monotonic_ms"].as_u64()
                && s["next_keepalive_due_ms"].as_u64() >= s["last_fresh_ms"].as_u64(),
            c,
        )?;
    }
    let mut seqs = BTreeSet::new();
    for s in incidents {
        fields(
            s,
            "sequence source scope subject_id membership_generation observed_at_ms affected_track_ids action outcome detection_latency_ms within_detection_budget",
            c,
        )?;
        require(
            int(&s["sequence"], 1)
                && seqs.insert(s["sequence"].as_u64())
                && one_of(&s["source"], SOURCES)
                && one_of(
                    &s["scope"],
                    &["request", "edge", "placement", "peer", "deployment"],
                )
                && bounded(&s["subject_id"], 256)
                && int(&s["membership_generation"], 1)
                && int(&s["observed_at_ms"], 0)
                && bounded(&s["action"], 256)
                && bounded(&s["outcome"], 256)
                && nullable_int(&s["detection_latency_ms"])
                && (s["within_detection_budget"].is_null()
                    || s["within_detection_budget"].is_boolean())
                && s["observed_at_ms"].as_u64() <= v["generated_at_monotonic_ms"].as_u64(),
            c,
        )?;
        require(
            arr(&s["affected_track_ids"], 256)
                .is_some_and(|a| unique(a) && a.iter().all(|v| bounded(v, 256))),
            c,
        )?;
    }
    Ok(())
}
fn incident(v: &Value) -> Result {
    let c = "invalid_scoped_runtime_incident";
    fields(
        v,
        "protocol incident_id deployment_id deployment_epoch qualification_digest request_id request_attempt path_id path_attempt path_digest topology_generation command_id cancellation_generation publisher_generation cleanup_owner_id subject_id scope reason fatal_requested fatal_accepted observed_at_monotonic_ms",
        c,
    )?;
    for f in [
        "incident_id",
        "deployment_id",
        "request_id",
        "path_id",
        "command_id",
        "cleanup_owner_id",
        "subject_id",
        "reason",
    ] {
        require(bounded(&v[f], 256), c)?;
    }
    for f in [
        "deployment_epoch",
        "request_attempt",
        "topology_generation",
        "publisher_generation",
    ] {
        require(int(&v[f], 1), c)?;
    }
    for f in [
        "path_attempt",
        "cancellation_generation",
        "observed_at_monotonic_ms",
    ] {
        require(int(&v[f], 0), c)?;
    }
    require(
        one_of(
            &v["scope"],
            &["request", "edge", "placement", "peer", "deployment"],
        ) && digest(&v["qualification_digest"])
            && digest(&v["path_digest"])
            && v["fatal_requested"].is_boolean()
            && v["fatal_accepted"].is_boolean()
            && (v["fatal_accepted"] != true || v["fatal_requested"] == true),
        c,
    )
}
fn qualification(v: &Value, max: u64) -> Result {
    let c = "invalid_product_concurrency_liveness_qualification";
    fields(
        v,
        "protocol deployment_id qualification_digest maximum_concurrent_requests cancellation_and_cleanup_bound_ms cooperative_interruption_proven request_scoped_cleanup_proven shared_process_termination_used publisher_generation_fencing_proven scoped_liveness_proven eligible evidence_digest",
        c,
    )?;
    require(
        digest(&v["qualification_digest"])
            && digest(&v["evidence_digest"])
            && bounded(&v["deployment_id"], 256)
            && v["maximum_concurrent_requests"]
                .as_u64()
                .is_some_and(|n| (2..=64).contains(&n)),
        c,
    )?;
    for f in [
        "cooperative_interruption_proven",
        "request_scoped_cleanup_proven",
        "shared_process_termination_used",
        "publisher_generation_fencing_proven",
        "scoped_liveness_proven",
        "eligible",
    ] {
        require(v[f].is_boolean(), c)?;
    }
    // Oracle uses numeric equality here, deliberately accepting an equal float.
    let gates = [
        "cooperative_interruption_proven",
        "request_scoped_cleanup_proven",
        "publisher_generation_fencing_proven",
        "scoped_liveness_proven",
    ]
    .iter()
    .all(|f| v[*f] == true)
        && v["shared_process_termination_used"] == false
        && v["cancellation_and_cleanup_bound_ms"].as_f64() == Some(max as f64);
    require(v["eligible"] == gates, c)
}
fn bootstrap(v: &Value) -> Result {
    let c = "internet_bootstrap_status_invalid";
    fields(
        v,
        "protocol generation observed_at_unix_ms freshness tls_state canonical_origin_verified seed_pin_state route_state invitation_state counters",
        c,
    )?;
    privacy(v, "internet_bootstrap_status_privacy")?;
    require(
        int(&v["generation"], 1)
            && int(&v["observed_at_unix_ms"], 0)
            && one_of(&v["freshness"], FRESH)
            && one_of(
                &v["tls_state"],
                &["publicly_trusted", "unverified", "unknown"],
            )
            && v["canonical_origin_verified"].is_boolean()
            && one_of(&v["seed_pin_state"], &["verified", "mismatch", "unknown"])
            && one_of(&v["route_state"], &["available", "unavailable", "unknown"])
            && one_of(
                &v["invitation_state"],
                &["pending", "accepted", "rejected", "unknown"],
            ),
        c,
    )?;
    fields(&v["counters"], "requests joins_accepted joins_rejected", c)?;
    for f in ["requests", "joins_accepted", "joins_rejected"] {
        require(int(&v["counters"][f], 0), c)?;
    }
    Ok(())
}
fn activation(v: &Value) -> Result {
    let c = "internet_activation_observation_invalid";
    fields(
        v,
        "protocol observation_id connection_generation connection_reuse path_class path_source endpoint_pseudonym observed_at_unix_ms freshness evidence_lifetime_until_unix_ms metrics",
        c,
    )?;
    privacy(v, "internet_activation_observation_privacy")?;
    require(
        bounded(&v["observation_id"], 128)
            && one_of(&v["path_class"], &["direct", "relay", "unknown"])
            && one_of(&v["path_source"], &["bound_live_connection", "unknown"])
            && (v["endpoint_pseudonym"].is_null() || digest(&v["endpoint_pseudonym"]))
            && one_of(&v["freshness"], FRESH),
        c,
    )?;
    for f in [
        "connection_generation",
        "connection_reuse",
        "observed_at_unix_ms",
        "evidence_lifetime_until_unix_ms",
    ] {
        require(int(&v[f], 0), c)?;
    }
    let m = &v["metrics"];
    fields(
        m,
        "rtt_ms warm_rtt_ms jitter_ms goodput_bytes_per_second loss_ratio sample_count measured_zero",
        c,
    )?;
    require(m["measured_zero"].is_boolean(), c)?;
    for f in [
        "rtt_ms",
        "warm_rtt_ms",
        "jitter_ms",
        "goodput_bytes_per_second",
        "sample_count",
    ] {
        require(nullable_int(&m[f]), c)?;
    }
    if !m["loss_ratio"].is_null() {
        let loss = number(&m["loss_ratio"], c)?;
        require(
            (0.0..=1.0).contains(&loss)
                && int(&m["sample_count"], 1)
                && m["measured_zero"] == (loss == 0.0),
            c,
        )?;
    } else {
        require(m["sample_count"].is_null(), c)?;
    }
    if m["sample_count"].is_null() {
        require(
            [
                "rtt_ms",
                "warm_rtt_ms",
                "jitter_ms",
                "goodput_bytes_per_second",
            ]
            .iter()
            .all(|f| m[*f].is_null()),
            c,
        )?;
    }
    if v["path_class"] != "unknown" {
        require(
            v["path_source"] == "bound_live_connection"
                && v["freshness"] == "current"
                && !v["endpoint_pseudonym"].is_null(),
            c,
        )?;
    }
    if v["freshness"] != "current" {
        require(
            v["path_class"] == "unknown" && v["path_source"] == "unknown",
            c,
        )?;
    }
    if v["path_class"] == "unknown" {
        require(
            v["path_source"] == "unknown"
                && m["measured_zero"] == false
                && [
                    "rtt_ms",
                    "warm_rtt_ms",
                    "jitter_ms",
                    "goodput_bytes_per_second",
                    "loss_ratio",
                    "sample_count",
                ]
                .iter()
                .all(|f| m[*f].is_null()),
            c,
        )?;
    }
    Ok(())
}
fn relay_ref(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        s.len() == 76
            && s.starts_with("hmac-sha256:")
            && s[12..]
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
fn relay(v: &Value) -> Result {
    let c = "relay_projection_invalid";
    fields(
        v,
        "protocol relay_reference region projection_generation stable observed_at_unix_ms",
        c,
    )?;
    privacy(v, "relay_projection_privacy")?;
    require(
        relay_ref(&v["relay_reference"])
            && text(&v["region"], 64)
            && int(&v["projection_generation"], 1)
            && v["stable"].is_boolean()
            && int(&v["observed_at_unix_ms"], 0),
        c,
    )
}
fn internet_qualification(v: &Value) -> Result {
    let c = "internet_native_qualification_invalid";
    fields(
        v,
        "protocol qualification_id gate_kind case_ids observed_at_unix_ms executed result spec_digest source_digest evidence_digests fresh_until_unix_ms projection_digest public_projection",
        c,
    )?;
    privacy(v, "internet_native_qualification_privacy")?;
    require(
        bounded(&v["qualification_id"], 128)
            && one_of(&v["gate_kind"], &["physical_positive", "physical_negative"])
            && names(&v["case_ids"], 32)
            && int(&v["observed_at_unix_ms"], 0)
            && v["executed"].is_boolean()
            && one_of(&v["result"], &["passed", "failed", "not_executed"])
            && digest(&v["spec_digest"])
            && digest(&v["source_digest"])
            && nullable_int(&v["fresh_until_unix_ms"])
            && (v["projection_digest"].is_null() || digest(&v["projection_digest"])),
        c,
    )?;
    let evidence = arr(&v["evidence_digests"], 32).ok_or(ContractError(c))?;
    require(unique(evidence) && evidence.iter().all(digest), c)?;
    if v["executed"] == false {
        require(
            v["result"] == "not_executed"
                && evidence.is_empty()
                && v["projection_digest"].is_null(),
            c,
        )?;
    } else {
        require(one_of(&v["result"], &["passed", "failed"]), c)?;
    }
    let p = &v["public_projection"];
    fields(
        p,
        "gate_case_ids outcomes relay_reference observed_at_unix_ms",
        c,
    )?;
    require(
        names(&p["gate_case_ids"], 32)
            && names(&p["outcomes"], 32)
            && (p["relay_reference"].is_null() || relay_ref(&p["relay_reference"]))
            && int(&p["observed_at_unix_ms"], 0),
        c,
    )?;
    let cases = v["case_ids"].as_array().ok_or(ContractError(c))?;
    let selected = p["gate_case_ids"].as_array().ok_or(ContractError(c))?;
    let outcomes = p["outcomes"].as_array().ok_or(ContractError(c))?;
    require(
        selected.iter().all(|i| cases.contains(i))
            && outcomes.windows(2).all(|w| w[0].as_str() <= w[1].as_str()),
        c,
    )
}
