// SPDX-License-Identifier: AGPL-3.0-only
//! Closed evidence wire records, not measurement, location or authority issuers.
//! Oracle: mycelium_topology_evidence.py:90-159 and
//! mycelium_product_spine/coarse_region.py:83-103 @ 941765e8.
use crate::{ContractError, canonical, checks::*};
use serde_json::Value;
use std::cmp::Ordering;

// Inputs have already passed the unbounded nonnegative integer predicate.
fn compare_integers(a: &Value, b: &Value) -> Ordering {
    let a = a.to_string();
    let b = b.to_string();
    a.len().cmp(&b.len()).then_with(|| a.cmp(&b))
}
fn nonempty(v: &Value) -> bool {
    v.as_str().is_some_and(|s| !s.is_empty())
}

/// Validate declared measurements, with an optional caller-owned Unix-ms clock.
/// This does not authenticate endpoints or prove that any frame was exchanged.
pub fn transport_path(
    v: &Value,
    now_unix_ms: Option<&Value>,
    require_resolved: bool,
) -> Result<(), ContractError> {
    exact(
        v,
        &[
            "protocol",
            "local_node_id",
            "local_endpoint_id",
            "remote_node_id",
            "remote_endpoint_id",
            "connection_generation",
            "path_class",
            "relay_identity",
            "relay_region",
            "cold_rtt_ms",
            "warm_rtt_ms",
            "observed_goodput_Bps",
            "jitter_ms",
            "loss_ratio",
            "sample_count",
            "connections_opened",
            "frames_sent",
            "reconnect_count",
            "selected_path_changes",
            "measurement_source",
            "measured_at_unix_ms",
            "fresh_until_unix_ms",
            "exclusions",
        ],
        "transport observation shape is invalid",
    )?;
    require(
        v["protocol"] == "mycelium.transport_path_observation.v1",
        "transport observation protocol is invalid",
    )?;
    for (field, code) in [
        (
            "local_node_id",
            "transport observation local_node_id is invalid",
        ),
        (
            "remote_node_id",
            "transport observation remote_node_id is invalid",
        ),
        (
            "local_endpoint_id",
            "transport observation local_endpoint_id is invalid",
        ),
        (
            "remote_endpoint_id",
            "transport observation remote_endpoint_id is invalid",
        ),
    ] {
        require(nonempty(&v[field]), code)?;
    }
    require(
        v["local_node_id"] != v["remote_node_id"]
            && v["local_endpoint_id"] != v["remote_endpoint_id"],
        "transport observation endpoints must be distinct",
    )?;
    require(
        integer(&v["connection_generation"], 1),
        "transport observation connection_generation is invalid",
    )?;
    require(
        one_of(&v["path_class"], &["unknown", "direct", "relay"]),
        "transport observation path_class is invalid",
    )?;
    require(
        !require_resolved || v["path_class"] != "unknown",
        "transport observation path is unresolved",
    )?;
    for (field, code) in [
        (
            "relay_identity",
            "transport observation relay_identity is invalid",
        ),
        (
            "relay_region",
            "transport observation relay_region is invalid",
        ),
    ] {
        require(v[field].is_null() || v[field].is_string(), code)?;
    }
    require(
        v["path_class"] == "relay"
            || (v["relay_identity"].is_null() && v["relay_region"].is_null()),
        "non-relay observation cannot claim relay identity",
    )?;
    for (field, code) in [
        (
            "cold_rtt_ms",
            "transport observation cold_rtt_ms is invalid",
        ),
        (
            "warm_rtt_ms",
            "transport observation warm_rtt_ms is invalid",
        ),
        (
            "observed_goodput_Bps",
            "transport observation observed_goodput_Bps is invalid",
        ),
        ("jitter_ms", "transport observation jitter_ms is invalid"),
        ("loss_ratio", "transport observation loss_ratio is invalid"),
    ] {
        let n = number(&v[field], code)?;
        require(
            n >= 0.0 && (!(require_resolved && field == "observed_goodput_Bps") || n > 0.0),
            code,
        )?;
    }
    require(
        number(
            &v["loss_ratio"],
            "transport observation loss_ratio is invalid",
        )? <= 1.0,
        "transport observation loss_ratio is invalid",
    )?;
    for (field, code) in [
        (
            "sample_count",
            "transport observation sample_count is invalid",
        ),
        (
            "connections_opened",
            "transport observation connections_opened is invalid",
        ),
        (
            "frames_sent",
            "transport observation frames_sent is invalid",
        ),
        (
            "reconnect_count",
            "transport observation reconnect_count is invalid",
        ),
        (
            "selected_path_changes",
            "transport observation selected_path_changes is invalid",
        ),
        (
            "measured_at_unix_ms",
            "transport observation measured_at_unix_ms is invalid",
        ),
        (
            "fresh_until_unix_ms",
            "transport observation fresh_until_unix_ms is invalid",
        ),
    ] {
        require(integer(&v[field], 0), code)?;
    }
    require(
        compare_integers(&v["fresh_until_unix_ms"], &v["measured_at_unix_ms"]) == Ordering::Greater,
        "transport observation freshness window is invalid",
    )?;
    if let Some(now) = now_unix_ms {
        // Python's caller clock comparison admits numeric values; retain exact
        // unbounded integer ordering instead of rounding Unix-ms values to f64.
        let stale = if integer(now, 0) {
            compare_integers(now, &v["fresh_until_unix_ms"]) == Ordering::Greater
        } else if now.as_number().is_some_and(|n| {
            n.to_string()
                .strip_prefix('-')
                .is_some_and(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()))
        }) {
            false
        } else {
            let n = if let Some(b) = now.as_bool() {
                f64::from(u8::from(b))
            } else {
                number(now, "transport observation clock is invalid")?
            };
            n > number(
                &v["fresh_until_unix_ms"],
                "transport observation clock is invalid",
            )?
        };
        require(!stale, "transport observation is stale")?;
    }
    require(
        v["measurement_source"] == "iroh_activation_plane",
        "transport observation is not activation-plane evidence",
    )?;
    require(
        v["exclusions"]
            .as_array()
            .is_some_and(|a| a.iter().all(nonempty)),
        "transport observation exclusions are invalid",
    )?;
    if require_resolved {
        require(
            integer(&v["sample_count"], 3) && integer(&v["frames_sent"], 3),
            "transport observation sample window is insufficient",
        )?;
        require(
            integer(&v["connections_opened"], 1)
                && compare_integers(&v["connections_opened"], &v["frames_sent"]) == Ordering::Less,
            "transport observation does not prove connection reuse",
        )?;
    }
    Ok(())
}

/// Closed reviewed claim and canonical digest. Authority selection, expiry at a
/// caller's current time, and geography reduction remain the consumer's work.
pub fn coarse_region(v: &Value) -> Result<(), ContractError> {
    let c = "coarse_region_evidence_invalid";
    exact(
        v,
        &[
            "protocol",
            "subject_ref",
            "subject_kind",
            "source_kind",
            "region",
            "distance_class",
            "vocabulary_version",
            "model_version",
            "uncertainty",
            "issued_at_unix_ms",
            "expires_at_unix_ms",
            "authority_digest",
            "digest",
        ],
        c,
    )?;
    require(
        v["protocol"] == "mycelium.coarse_region_evidence.v1"
            && v["vocabulary_version"] == "mycelium.coarse_region.v1",
        c,
    )?;
    require(
        digest(&v["subject_ref"]) && one_of(&v["subject_kind"], &["member", "relay"]),
        c,
    )?;
    require(
        one_of(
            &v["source_kind"],
            &[
                "owner_declared",
                "reviewed_relay_metadata",
                "latency_distance_class",
            ],
        ) && one_of(
            &v["region"],
            &[
                "africa",
                "americas_north",
                "americas_south",
                "asia",
                "europe",
                "oceania",
                "polar",
                "unknown",
            ],
        ),
        c,
    )?;
    let uncertainty = match v["source_kind"].as_str() {
        Some("owner_declared") => "declared_unverified",
        Some("reviewed_relay_metadata") => "reviewed",
        Some("latency_distance_class") => "model_estimate",
        _ => return Err(ContractError(c)),
    };
    require(
        v["uncertainty"] == uncertainty && digest(&v["authority_digest"]),
        c,
    )?;
    for field in ["issued_at_unix_ms", "expires_at_unix_ms"] {
        require(v[field].as_u64().is_some_and(|n| n < (1 << 53)), c)?;
    }
    require(
        compare_integers(&v["expires_at_unix_ms"], &v["issued_at_unix_ms"]) != Ordering::Less,
        c,
    )?;
    if v["source_kind"] == "latency_distance_class" {
        require(
            v["region"] == "unknown"
                && one_of(&v["distance_class"], &["near", "mid", "far", "unknown"])
                && segment(&v["model_version"]),
            c,
        )?;
    } else {
        require(
            v["distance_class"].is_null() && v["model_version"].is_null(),
            c,
        )?;
    }
    let mut body = v.clone();
    body.as_object_mut()
        .ok_or(ContractError(c))?
        .remove("digest");
    let encoded = canonical::to_ascii_bytes(&body).map_err(|_| ContractError(c))?;
    require(v["digest"] == canonical::sha256_prefixed(&encoded), c)
}
