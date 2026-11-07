// SPDX-License-Identifier: AGPL-3.0-only
use super::{C, Limits, cmp_int};
use crate::{ContractError, canonical, checks::*, public::fields};
use serde_json::Value;
use std::cmp::Ordering;
use std::collections::BTreeSet;
fn shape(v: &Value, required: &str, optional: &str) -> Result<(), ContractError> {
    let m = v.as_object().ok_or(ContractError(C))?;
    require(
        required.split_whitespace().all(|k| m.contains_key(k))
            && m.keys().all(|k| {
                required
                    .split_whitespace()
                    .chain(optional.split_whitespace())
                    .any(|f| f == k)
            }),
        C,
    )
}
pub(super) fn nonempty(v: &Value) -> bool {
    v.as_str().is_some_and(|s| !s.is_empty())
}
pub(super) fn hex(v: &Value, min: usize, max: usize) -> bool {
    v.as_str().is_some_and(|s| {
        s.len() >= min
            && s.len() <= max
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
pub(super) fn array(v: &Value) -> Result<&Vec<Value>, ContractError> {
    v.as_array().ok_or(ContractError(C))
}
fn privacy(v: &Value) -> Result<(), ContractError> {
    match v {
        Value::Object(m) => {
            for (k, v) in m {
                require(
                    ![
                        "api_key",
                        "auth_key",
                        "access_token",
                        "refresh_token",
                        "bearer_token",
                        "password",
                        "private_key",
                        "secret",
                        "shared_secret",
                    ]
                    .contains(&k.to_lowercase().as_str()),
                    C,
                )?;
                privacy(v)?;
            }
        }
        Value::Array(a) => {
            for v in a {
                privacy(v)?;
            }
        }
        _ => {}
    }
    Ok(())
}
fn domains(v: &Value) -> Result<(), ContractError> {
    let mut seen = BTreeSet::new();
    for d in array(v)? {
        shape(
            d,
            "memory_domain_id kind total_bytes",
            "allocatable_after_reservations_bytes committed_bytes reclaimable_bytes reservation_generation extensions",
        )?;
        require(
            segment(&d["memory_domain_id"])
                && seen.insert(d["memory_domain_id"].as_str())
                && one_of(
                    &d["kind"],
                    &["system", "unified", "vram", "accelerator", "other"],
                )
                && integer(&d["total_bytes"], 0),
            C,
        )?;
        for f in [
            "allocatable_after_reservations_bytes",
            "committed_bytes",
            "reclaimable_bytes",
            "reservation_generation",
        ] {
            if let Some(v) = d.get(f) {
                require(integer(v, 0), C)?;
            }
        }
    }
    Ok(())
}
fn endpoints(v: &Value) -> Result<(), ContractError> {
    for e in array(v)? {
        shape(
            e,
            "endpoint_id transport host port scope inbound",
            "priority extensions",
        )?;
        require(
            segment(&e["endpoint_id"])
                && segment(&e["transport"])
                && e["host"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty() && s.chars().count() <= 255)
                && integer(&e["port"], 1)
                && e["port"].as_u64().is_some_and(|n| n <= 65535)
                && one_of(
                    &e["scope"],
                    &["lan", "overlay", "manual", "relay", "loopback"],
                )
                && e["inbound"].is_boolean(),
            C,
        )?;
    }
    Ok(())
}
pub(super) fn link(v: &Value, origin: &str) -> Result<(), ContractError> {
    privacy(v)?;
    shape(
        v,
        "protocol src_node_id dst_node_id src_endpoint_id dst_endpoint_id reachable sample_count measurement_method",
        "connect_rtt_ema_ms rtt_p95_ms jitter_ms loss_ratio goodput_mbps measurement_payload_bytes connection_state quality extensions",
    )?;
    require(
        v["protocol"] == "mycelium.link_state.v1"
            && v["src_node_id"] == origin
            && [
                "src_node_id",
                "dst_node_id",
                "src_endpoint_id",
                "dst_endpoint_id",
                "measurement_method",
            ]
            .iter()
            .all(|f| segment(&v[*f]))
            && v["reachable"].is_boolean()
            && integer(&v["sample_count"], 0),
        C,
    )?;
    for f in [
        "connect_rtt_ema_ms",
        "rtt_p95_ms",
        "jitter_ms",
        "goodput_mbps",
    ] {
        if let Some(n) = v.get(f).filter(|n| !n.is_null()) {
            require(number(n, C)? >= 0.0, C)?;
        }
    }
    if let Some(n) = v.get("loss_ratio") {
        require((0.0..=1.0).contains(&number(n, C)?), C)?;
    }
    Ok(())
}
fn payload(v: &Value, kind: &str, origin: &str) -> Result<(), ContractError> {
    privacy(v)?;
    match kind {
        "profile" => {
            shape(
                v,
                "protocol node_id software_version protocol_versions platform architecture memory_domains endpoints policy",
                "cpu total_ram_bytes unified_memory accelerators backends precisions model_inventory storage extensions",
            )?;
            require(
                v["protocol"] == "mycelium.device_profile.v2"
                    && v["node_id"] == origin
                    && segment(&v["node_id"])
                    && nonempty(&v["software_version"])
                    && v["policy"].is_object(),
                C,
            )?;
            let versions = array(&v["protocol_versions"])?;
            require(!versions.is_empty() && versions.iter().all(nonempty), C)?;
            domains(&v["memory_domains"])?;
            endpoints(&v["endpoints"])?;
        }
        "status" => {
            shape(
                v,
                "protocol node_id lifecycle memory_domains queue_depth in_flight concurrency_limit",
                "available_ram_bytes available_vram_bytes available_storage_bytes utilization thermal ac_power battery_percent error_ewma performance kv_cache_headroom_bytes active_assignments ingress_mbps egress_mbps quality extensions",
            )?;
            require(
                v["protocol"] == "mycelium.device_status.v1"
                    && v["node_id"] == origin
                    && one_of(
                        &v["lifecycle"],
                        &["joining", "ready", "draining", "offline"],
                    ),
                C,
            )?;
            domains(&v["memory_domains"])?;
            for f in ["queue_depth", "in_flight", "concurrency_limit"] {
                require(integer(&v[f], 0), C)?;
            }
            for f in [
                "available_ram_bytes",
                "available_vram_bytes",
                "available_storage_bytes",
                "kv_cache_headroom_bytes",
            ] {
                if let Some(v) = v.get(f) {
                    require(integer(v, 0), C)?;
                }
            }
        }
        "link" => link(v, origin)?,
        "membership" => {
            shape(
                v,
                "protocol subject_node_id subject_incarnation state reporter_node_id reason",
                "extensions",
            )?;
            require(
                v["protocol"] == "mycelium.membership.v1"
                    && v["reporter_node_id"] == origin
                    && segment(&v["subject_node_id"])
                    && segment(&v["reporter_node_id"])
                    && integer(&v["subject_incarnation"], 0)
                    && one_of(&v["state"], &["alive", "suspect", "dead"])
                    && v["reason"]
                        .as_str()
                        .is_some_and(|s| s.chars().count() <= 256),
                C,
            )?;
        }
        "offering" => {
            shape(
                v,
                "protocol deployment_id deployment_epoch assignment_id manifest_digest resolved_commit model_id start_layer end_layer_exclusive runtime_instance_id load_generation readiness_state proof_digest inference_endpoint_id",
                "backend precision performance extensions",
            )?;
            require(
                v["protocol"] == "mycelium.runtime_offering.v1"
                    && [
                        "deployment_id",
                        "assignment_id",
                        "model_id",
                        "runtime_instance_id",
                        "inference_endpoint_id",
                    ]
                    .iter()
                    .all(|f| segment(&v[*f]))
                    && digest(&v["manifest_digest"])
                    && digest(&v["proof_digest"])
                    && hex(&v["resolved_commit"], 7, 64)
                    && [
                        "deployment_epoch",
                        "start_layer",
                        "end_layer_exclusive",
                        "load_generation",
                    ]
                    .iter()
                    .all(|f| integer(&v[*f], 0))
                    && cmp_int(&v["end_layer_exclusive"], &v["start_layer"]) == Ordering::Greater
                    && one_of(
                        &v["readiness_state"],
                        &[
                            "loading",
                            "loaded",
                            "loaded_and_probed",
                            "draining",
                            "failed",
                        ],
                    ),
                C,
            )?;
        }
        _ => return Err(ContractError(C)),
    }
    Ok(())
}
/// Record grammar and logical key. The record's own `swarm_id` is validated here; equality
/// with the enclosing bundle is a separate (later) oracle check owned by the caller.
pub(super) fn record(v: &Value, limits: Limits) -> Result<Vec<String>, ContractError> {
    fields(
        v,
        "protocol swarm_id kind origin_node_id incarnation sequence boot_id generated_at_unix_ms ttl_ms payload_hash payload",
        C,
    )?;
    require(
        v["protocol"] == "mycelium.gossip.record.v1"
            && segment(&v["swarm_id"])
            && segment(&v["origin_node_id"])
            && segment(&v["boot_id"])
            && integer(&v["incarnation"], 1)
            && integer(&v["sequence"], 0)
            && integer(&v["generated_at_unix_ms"], 0)
            && integer(&v["ttl_ms"], 1)
            && v["ttl_ms"]
                .as_u64()
                .is_some_and(|n| n <= limits.max_record_ttl_ms),
        C,
    )?;
    let kind = v["kind"].as_str().ok_or(ContractError(C))?;
    let origin = v["origin_node_id"].as_str().ok_or(ContractError(C))?;
    payload(&v["payload"], kind, origin)?;
    require(
        v["payload_hash"] == canonical::digest(&v["payload"]).map_err(|_| ContractError(C))?,
        C,
    )?;
    let mut key = vec![
        v["swarm_id"].as_str().ok_or(ContractError(C))?.to_owned(),
        origin.to_owned(),
        kind.to_owned(),
    ];
    for f in match kind {
        "link" => &["src_endpoint_id", "dst_node_id", "dst_endpoint_id"][..],
        "membership" => &["subject_node_id"][..],
        "offering" => &["deployment_id", "assignment_id", "inference_endpoint_id"][..],
        _ => &[][..],
    } {
        key.push(
            v["payload"][*f]
                .as_str()
                .ok_or(ContractError(C))?
                .to_owned(),
        );
    }
    Ok(key)
}
