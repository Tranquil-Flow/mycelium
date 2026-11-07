// SPDX-License-Identifier: AGPL-3.0-only
//! Closed privacy-reduced product snapshot/event schemas. These validate claims,
//! not source authority, observed readiness, HTTP sessions or publication order.
use crate::{
    ContractError,
    checks::*,
    public::{fields, safe_integer},
};
use serde_json::Value;
use std::{collections::BTreeSet, net::IpAddr};
type Result = std::result::Result<(), ContractError>;
const C: &str = "product_snapshot_invalid";
const KINDS: &[&str] = &[
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
const MODES: &[&str] = &["fixture", "replay", "degraded", "live"];
const FRESH: &[&str] = &[
    "current",
    "stale",
    "missing",
    "conflict",
    "unsupported",
    "replay",
];
fn shape(v: &Value, s: &str) -> bool {
    fields(v, s, C).is_ok()
}
fn id(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.len() <= 128
            && s.as_bytes()[0].is_ascii_alphanumeric()
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._:~-".contains(&b))
    })
}
fn code(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.len() <= 64
            && s.as_bytes()[0].is_ascii_lowercase()
            && s.bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
    })
}
fn opt(v: &Value, p: fn(&Value) -> bool) -> bool {
    v.is_null() || p(v)
}
fn int(v: &Value) -> bool {
    safe_integer(v, 0)
}
fn ids(v: &Value, max: usize, p: fn(&Value) -> bool) -> bool {
    v.as_array().is_some_and(|a| {
        a.len() <= max && a.iter().all(p) && (0..a.len()).all(|i| !a[..i].contains(&a[i]))
    })
}
fn model(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        let parts: Vec<_> = s.split('/').collect();
        parts.len() <= 2
            && parts.iter().all(|p| {
                !p.is_empty()
                    && p.len() <= 128
                    && p.as_bytes()[0].is_ascii_alphanumeric()
                    && p.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            })
    })
}
pub(crate) fn credential(s: &str) -> bool {
    // Python re.IGNORECASE extends ASCII letters with these four Unicode
    // characters; boundaries still use the original Unicode word characters.
    let chars: Vec<_> = s.chars().collect();
    let folded: String = chars
        .iter()
        .map(|c| match c {
            'İ' | 'ı' => 'i',
            'ſ' => 's',
            'K' => 'k',
            _ => c.to_ascii_lowercase(),
        })
        .collect();
    for (index, (byte, _)) in folded.char_indices().enumerate() {
        let tail = &folded[byte..];
        if let Some(rest) = tail.strip_prefix("-----begin") {
            for n in 0..=48 {
                if rest
                    .get(n..)
                    .is_some_and(|x| x.starts_with("private key-----"))
                {
                    return true;
                }
                if !rest.as_bytes().get(n).is_some_and(|b| {
                    b.is_ascii_lowercase() || b.is_ascii_digit() || b" _-".contains(b)
                }) {
                    break;
                }
            }
        }
        if index > 0 && (chars[index - 1].is_alphanumeric() || chars[index - 1] == '_') {
            continue;
        }
        if tail
            .strip_prefix("bearer")
            .and_then(|s| s.chars().next())
            .is_some_and(|c| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
        {
            return true;
        }
        for (prefix, min, extra) in [
            ("sk-", 12, "_-"),
            ("github_pat_", 20, "_"),
            ("ghp_", 20, ""),
            ("gho_", 20, ""),
            ("ghu_", 20, ""),
            ("ghs_", 20, ""),
            ("ghr_", 20, ""),
        ] {
            if tail.strip_prefix(prefix).is_some_and(|rest| {
                rest.bytes()
                    .take_while(|b| {
                        b.is_ascii_lowercase() || b.is_ascii_digit() || extra.as_bytes().contains(b)
                    })
                    .count()
                    >= min
            }) {
                return true;
            }
        }
    }
    false
}
fn safe(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        let lower = s.to_lowercase();
        !s.is_empty()
            && s.chars().count() <= 256
            && !s.contains(['/', '\\'])
            && !crate::public::host_port(s)
            && !credential(s)
            && lower != "localhost"
            && !([".local", ".lan", ".internal"]
                .iter()
                .any(|suffix| lower.ends_with(suffix)))
            && s.split('%')
                .next()
                .is_some_and(|ip| ip.parse::<IpAddr>().is_err())
    })
}
fn privacy(v: &Value) -> bool {
    match v {
        Value::Object(o) => o.iter().all(|(k, x)| {
            ![
                "activation",
                "activations",
                "api_key",
                "auth_token",
                "bearer_token",
                "completion",
                "decoded_output",
                "endpoint_addr",
                "endpoint_addrs",
                "hidden_state",
                "hidden_states",
                "hostname",
                "input_ids",
                "invite_nonce",
                "ip",
                "ip_address",
                "kv_cache",
                "logits",
                "message",
                "model_weights",
                "output_ids",
                "password",
                "path",
                "private_key",
                "prompt",
                "reservation_id",
                "secret",
                "tensor",
                "tensors",
                "token",
                "token_ids",
                "tokens",
            ]
            .contains(&k.to_lowercase().as_str())
                && privacy(x)
        }),
        Value::Array(a) => a.len() <= 4096 && a.iter().all(privacy),
        Value::String(s) => !credential(s),
        Value::Number(n) => {
            !n.to_string().contains(['.', 'e', 'E']) || n.as_f64().is_some_and(f64::is_finite)
        }
        _ => true,
    }
}
fn binding(v: &Value) -> bool {
    shape(
        v,
        "deployment_id deployment_epoch route_id route_generation topology_version",
    ) && opt(&v["deployment_id"], id)
        && opt(&v["route_id"], id)
        && ["deployment_epoch", "route_generation", "topology_version"]
            .iter()
            .all(|f| opt(&v[*f], int))
}
fn freshness(v: &Value) -> bool {
    shape(v, "status observed_at_unix_ms valid_until_unix_ms")
        && one_of(&v["status"], FRESH)
        && opt(&v["observed_at_unix_ms"], int)
        && opt(&v["valid_until_unix_ms"], int)
        && (v["observed_at_unix_ms"].is_null()
            || v["valid_until_unix_ms"].is_null()
            || v["valid_until_unix_ms"].as_u64() >= v["observed_at_unix_ms"].as_u64())
}
fn decode_mode(v: &Value) -> bool {
    one_of(v, &["stage_local_kv", "complete_context_replay"])
}
fn attributes(kind: &str, v: &Value) -> bool {
    match kind {
        "device" => {
            shape(
                v,
                "peer_class membership_generation authority_generation incarnation lifecycle lease_freshness runtime_backend transport activation_protocol activation_eligible placement_id",
            ) && ["peer_class", "incarnation", "runtime_backend", "transport"]
                .iter()
                .all(|f| id(&v[*f]))
                && safe_integer(&v["membership_generation"], 1)
                && safe_integer(&v["authority_generation"], 1)
                && code(&v["lifecycle"])
                && one_of(&v["lease_freshness"], &["fresh", "stale", "expired"])
                && opt(&v["activation_protocol"], id)
                && opt(&v["placement_id"], id)
                && v["activation_eligible"].is_boolean()
        }
        "directed_link" => {
            shape(
                v,
                "src_device_id dst_device_id connectivity measurement_digest",
            ) && id(&v["src_device_id"])
                && id(&v["dst_device_id"])
                && v["src_device_id"] != v["dst_device_id"]
                && one_of(&v["connectivity"], &["unknown", "measured"])
                && opt(&v["measurement_digest"], digest)
                && (v["connectivity"] == "measured" || v["measurement_digest"].is_null())
        }
        "stage" => {
            shape(
                v,
                "stage_index start_layer end_layer_exclusive component_roles decode_mode",
            ) && int(&v["stage_index"])
                && int(&v["start_layer"])
                && safe_integer(&v["end_layer_exclusive"], 1)
                && v["end_layer_exclusive"].as_u64() > v["start_layer"].as_u64()
                && ids(&v["component_roles"], 256, id)
                && decode_mode(&v["decode_mode"])
        }
        "route" => {
            shape(
                v,
                "deployment_id model_id topology_version decode_mode placement_provenance route_alive concurrency_eligible cancellation_cleanup_bound_ms publisher_generation_fenced scoped_liveness_proven",
            ) && id(&v["deployment_id"])
                && model(&v["model_id"])
                && int(&v["topology_version"])
                && decode_mode(&v["decode_mode"])
                && one_of(
                    &v["placement_provenance"],
                    &["operator_selected", "planner_v2", "frozen_fixture"],
                )
                && [
                    "route_alive",
                    "concurrency_eligible",
                    "publisher_generation_fenced",
                    "scoped_liveness_proven",
                ]
                .iter()
                .all(|f| v[*f].is_boolean())
                && safe_integer(&v["cancellation_cleanup_bound_ms"], 1)
        }
        "assignment" => {
            shape(
                v,
                "device_id stage_id membership_generation load_generation assignment_digest stage_pack_digest",
            ) && id(&v["device_id"])
                && id(&v["stage_id"])
                && safe_integer(&v["membership_generation"], 1)
                && safe_integer(&v["load_generation"], 1)
                && digest(&v["assignment_digest"])
                && digest(&v["stage_pack_digest"])
        }
        "artifact" => {
            shape(v, "artifact_digest size_bytes cache_state")
                && digest(&v["artifact_digest"])
                && int(&v["size_bytes"])
                && one_of(
                    &v["cache_state"],
                    &["verified", "missing", "corrupt", "unknown"],
                )
        }
        "load_proof" => {
            shape(v, "proof_digest assignment_id load_generation ready")
                && digest(&v["proof_digest"])
                && id(&v["assignment_id"])
                && safe_integer(&v["load_generation"], 1)
                && v["ready"].is_boolean()
        }
        "runtime_kv_ownership" => {
            shape(
                v,
                "device_id stage_id decode_mode kv_state_count kv_byte_budget proof_digest",
            ) && id(&v["device_id"])
                && id(&v["stage_id"])
                && decode_mode(&v["decode_mode"])
                && int(&v["kv_state_count"])
                && int(&v["kv_byte_budget"])
                && opt(&v["proof_digest"], digest)
        }
        "recovery" => {
            let names = "request_count recovering_count terminal_count terminal_blocked_count cleanup_complete_count reconciled_count max_recovery_attempt breaker_state";
            shape(v, names)
                && one_of(&v["breaker_state"], &["closed", "open"])
                && names
                    .split_whitespace()
                    .filter(|f| *f != "breaker_state")
                    .all(|f| int(&v[f]))
                && [
                    "recovering_count",
                    "terminal_count",
                    "terminal_blocked_count",
                    "cleanup_complete_count",
                    "reconciled_count",
                ]
                .iter()
                .all(|f| v[*f].as_u64() <= v["request_count"].as_u64())
        }
        "request" => {
            shape(v, "state path_attempt sequence qualification_id")
                && one_of(
                    &v["state"],
                    &["accepted", "running", "completed", "failed", "cancelled"],
                )
                && safe_integer(&v["path_attempt"], 1)
                && int(&v["sequence"])
                && id(&v["qualification_id"])
        }
        "incident" => {
            shape(v, "state reason_code observed_at_unix_ms")
                && code(&v["state"])
                && code(&v["reason_code"])
                && int(&v["observed_at_unix_ms"])
        }
        "qualification" => {
            shape(
                v,
                "qualification_digest route_ready issued_at_unix_ms expires_at_unix_ms reason_codes",
            ) && digest(&v["qualification_digest"])
                && v["route_ready"].is_boolean()
                && int(&v["issued_at_unix_ms"])
                && opt(&v["expires_at_unix_ms"], int)
                && (v["expires_at_unix_ms"].is_null()
                    || v["expires_at_unix_ms"].as_u64() >= v["issued_at_unix_ms"].as_u64())
                && ids(&v["reason_codes"], 128, code)
        }
        "source_provenance" => {
            shape(
                v,
                "authority source_protocol source_generation evidence_digest",
            ) && safe(&v["authority"])
                && id(&v["source_protocol"])
                && opt(&v["source_generation"], int)
                && opt(&v["evidence_digest"], digest)
        }
        _ => false,
    }
}
const A7: &[(&str, &str)] = &[
    ("committed_token_count", "count"),
    ("sync_ack_wait_median_ms", "number"),
    ("recovery_prefill_operations", "count"),
    ("duplicate_token_count", "count"),
    ("standby_placement_fingerprint", "digest"),
    ("runtime_backend", "code"),
    ("kv_reservation_bytes", "count"),
    ("measured_free_bytes", "count"),
    ("last_checkpoint_bytes_per_sec", "number"),
    ("last_ack_rtt_ms", "number"),
    ("fence_advance_sequence", "count"),
    ("transport_rtt_to_standby_ms", "number"),
    ("entry_node_binding", "identifier"),
    ("source_node_binding", "identifier"),
    ("standby_node_binding", "identifier"),
    ("process_exited_incident_count", "count"),
    ("standby_plan_digest", "digest"),
    ("deployment_id_epoch", "identifier"),
    ("qualification_age_s", "signed_number"),
    ("parity_challenge_digest", "digest"),
    ("fence_ledger_state", "fence_ledger_state"),
    ("durable_fence_generation", "count"),
    ("parity_challenge_status", "parity_challenge_status"),
    ("qualification_ttl_remaining_s", "signed_number"),
    ("scoped_incident_count_1h", "count"),
    ("incident_reason_code", "code"),
    ("recovery_path_chosen", "recovery_path"),
    ("terminal_blocked_count", "count"),
    ("collection_window_ms", "number"),
    ("command_timeout_ms", "count"),
    ("ack_timeout_s", "number"),
    ("receipt_timeout_s", "number"),
];
fn a7(v: &Value) -> bool {
    let names: Vec<_> = A7
        .iter()
        .map(|(k, _)| *k)
        .chain(["unavailable_reasons"])
        .collect();
    if exact(v, &names, C).is_err() {
        return false;
    }
    let Some(reasons) = v["unavailable_reasons"]
        .as_object()
        .filter(|r| r.keys().all(|k| A7.iter().any(|(f, _)| k == f)))
    else {
        return false;
    };
    A7.iter().all(|(f, kind)| {
        let x = &v[*f];
        if x.is_null() {
            return reasons.get(*f).is_some_and(code);
        }
        if reasons.contains_key(*f) {
            return false;
        }
        match *kind {
            "count" => int(x),
            "number" | "signed_number" => x
                .as_f64()
                .is_some_and(|n| n.is_finite() && (*kind == "signed_number" || n >= 0.0)),
            "digest" => digest(x),
            "code" => code(x),
            "identifier" => id(x),
            "fence_ledger_state" => one_of(x, &["empty", "holding"]),
            "parity_challenge_status" => one_of(x, &["passed", "failed", "missing"]),
            "recovery_path" => one_of(
                x,
                &["fenced_kv_successor", "full_context_replay", "aborted"],
            ),
            _ => false,
        }
    })
}
fn publication(v: &Value) -> bool {
    shape(
        v,
        "snapshot_id generation cursor published_at_unix_ms source_mode",
    ) && id(&v["snapshot_id"])
        && safe_integer(&v["generation"], 1)
        && int(&v["cursor"])
        && int(&v["published_at_unix_ms"])
        && one_of(&v["source_mode"], MODES)
}
fn source(v: &Value) -> bool {
    shape(
        v,
        "source_id authority status observed_at_unix_ms valid_until_unix_ms generation reason_code",
    ) && id(&v["source_id"])
        && safe(&v["authority"])
        && one_of(&v["status"], FRESH)
        && ["observed_at_unix_ms", "valid_until_unix_ms", "generation"]
            .iter()
            .all(|f| opt(&v[*f], int))
        && if one_of(&v["status"], &["current", "replay"]) {
            v["reason_code"].is_null()
        } else {
            code(&v["reason_code"])
        }
}
fn entity(v: &Value) -> bool {
    shape(
        v,
        "entity_id kind label source_id binding freshness attributes",
    ) && id(&v["entity_id"])
        && safe(&v["label"])
        && id(&v["source_id"])
        && binding(&v["binding"])
        && freshness(&v["freshness"])
        && if v["kind"] == "recovery" && v["entity_id"] == "a7-kv-successor-runtime" {
            a7(&v["attributes"])
        } else {
            v["kind"]
                .as_str()
                .is_some_and(|kind| attributes(kind, &v["attributes"]))
        }
}
fn relation(v: &Value) -> bool {
    shape(v, "relation_id kind from_entity_id to_entity_id source_id")
        && ["relation_id", "from_entity_id", "to_entity_id", "source_id"]
            .iter()
            .all(|f| id(&v[*f]))
        && v["from_entity_id"] != v["to_entity_id"]
        && one_of(
            &v["kind"],
            &[
                "assigned_to",
                "observes",
                "owns",
                "placed_on",
                "produced_by",
                "qualifies",
                "reports",
            ],
        )
}
fn readiness(v: &Value) -> bool {
    shape(v, "scope_id dimension state reason_code source_id")
        && id(&v["scope_id"])
        && id(&v["source_id"])
        && one_of(
            &v["dimension"],
            &[
                "artifacts",
                "membership",
                "product_source",
                "qualification",
                "route_challenge",
                "runtime",
                "transport",
            ],
        )
        && one_of(
            &v["state"],
            &["ready", "not_ready", "unknown", "unsupported"],
        )
        && opt(&v["reason_code"], code)
}
fn notice(v: &Value) -> bool {
    shape(v, "notice_id scope_id severity code source_id")
        && ["notice_id", "scope_id", "source_id"]
            .iter()
            .all(|f| id(&v[*f]))
        && one_of(&v["severity"], &["info", "warning", "error"])
        && code(&v["code"])
}
fn provenance(v: &Value) -> bool {
    shape(v, "projector projector_version source_mode")
        && safe(&v["projector"])
        && id(&v["projector_version"])
        && one_of(&v["source_mode"], MODES)
}
fn collection<'a>(
    v: &'a Value,
    key: &str,
    validate: fn(&Value) -> bool,
) -> std::result::Result<&'a Vec<Value>, ContractError> {
    let a = v
        .as_array()
        .filter(|a| a.len() <= 4096)
        .ok_or(ContractError(C))?;
    let mut seen = BTreeSet::new();
    for x in a {
        require(validate(x), C)?;
        if !key.is_empty() {
            require(seen.insert(x[key].as_str().ok_or(ContractError(C))?), C)?;
        }
    }
    Ok(a)
}
fn internet(v: &Value) -> Result {
    fields(
        v,
        "bootstrap_status activation_observation activation_history relay_projection qualification",
        C,
    )?;
    let history = v["activation_history"]
        .as_array()
        .filter(|a| !a.is_empty() && a.len() <= 64)
        .ok_or(ContractError(C))?;
    for (f, p) in [
        ("bootstrap_status", "mycelium.internet_bootstrap_status.v1"),
        (
            "activation_observation",
            "mycelium.internet_activation_observation.v1",
        ),
        ("relay_projection", "mycelium.relay_projection.v1"),
        ("qualification", "mycelium.internet_native_qualification.v1"),
    ] {
        if matches!(f, "relay_projection" | "qualification") && v[f].is_null() {
            continue;
        }
        crate::schemas::validate_as(p, &v[f], 0).map_err(|_| ContractError(C))?;
    }
    for a in history {
        crate::schemas::validate_as("mycelium.internet_activation_observation.v1", a, 0)
            .map_err(|_| ContractError(C))?;
    }
    require(
        history
            .last()
            .is_some_and(|a| python_equal(a, &v["activation_observation"])),
        C,
    )
}
pub fn snapshot(v: &Value) -> Result {
    let mut names="protocol publication supported_entity_kinds source_states entities relations readiness notices internet_native provenance".to_owned();
    for f in ["route_explorer", "coarse_region_evidence"] {
        if v.get(f).is_some() {
            names.push(' ');
            names.push_str(f);
        }
    }
    fields(v, &names, C)?;
    require(
        v["protocol"] == "mycelium.product_snapshot.v1"
            && v["supported_entity_kinds"].as_array().is_some_and(|a| {
                a.len() == KINDS.len() && a.iter().zip(KINDS).all(|(x, k)| x == k)
            })
            && publication(&v["publication"]),
        C,
    )?;
    let sources = collection(&v["source_states"], "source_id", source)?;
    let entities = collection(&v["entities"], "entity_id", entity)?;
    let relations = collection(&v["relations"], "relation_id", relation)?;
    let readiness = collection(&v["readiness"], "", readiness)?;
    let notices = collection(&v["notices"], "notice_id", notice)?;
    require(
        v["internet_native"].is_object()
            && provenance(&v["provenance"])
            && v["publication"]["source_mode"] == v["provenance"]["source_mode"]
            && privacy(v),
        C,
    )?;
    if let Some(e) = v.get("coarse_region_evidence") {
        require(
            e.as_array()
                .is_some_and(|a| a.len() <= 4096 && a.iter().all(Value::is_object)),
            C,
        )?;
    }
    let sources: BTreeSet<_> = sources
        .iter()
        .filter_map(|x| x["source_id"].as_str())
        .collect();
    let entity_ids: BTreeSet<_> = entities
        .iter()
        .filter_map(|x| x["entity_id"].as_str())
        .collect();
    let source_bound = |x: &Value| x["source_id"].as_str().is_some_and(|s| sources.contains(s));
    require(
        entities.iter().all(source_bound),
        "product_snapshot_source_unbound",
    )?;
    require(
        relations.iter().all(|x| {
            source_bound(x)
                && ["from_entity_id", "to_entity_id"]
                    .iter()
                    .all(|f| x[*f].as_str().is_some_and(|id| entity_ids.contains(id)))
        }),
        "product_snapshot_relation_unbound",
    )?;
    require(
        readiness.iter().all(source_bound),
        "product_snapshot_readiness_unbound",
    )?;
    require(
        notices.iter().all(source_bound),
        "product_snapshot_notice_unbound",
    )?;
    internet(&v["internet_native"])?;
    if let Some(a) = v["coarse_region_evidence"].as_array() {
        for x in a {
            crate::evidence::coarse_region(x).map_err(|_| ContractError(C))?;
        }
    }
    if let Some(explorer) = v.get("route_explorer") {
        crate::explorer::validate(explorer).map_err(|_| ContractError(C))?;
        let projection =
            crate::explorer::project_from_validated_snapshot(v).map_err(|_| ContractError(C))?;
        require(python_equal(explorer, &projection), C)?;
    }
    Ok(())
}
pub fn event(v: &Value) -> Result {
    let c = "product_event_invalid";
    fields(v, "protocol cursor previous_cursor event_kind snapshot", c)?;
    require(
        v["protocol"] == "mycelium.product_event.v1"
            && safe_integer(&v["cursor"], 1)
            && int(&v["previous_cursor"])
            && v["previous_cursor"].as_u64().and_then(|x| x.checked_add(1)) == v["cursor"].as_u64()
            && one_of(
                &v["event_kind"],
                &[
                    "conflict",
                    "snapshot_published",
                    "source_degraded",
                    "source_recovered",
                ],
            ),
        c,
    )?;
    snapshot(&v["snapshot"])?;
    require(
        v["snapshot"]["publication"]["cursor"] == v["cursor"],
        "product_event_snapshot_unbound",
    )
}
