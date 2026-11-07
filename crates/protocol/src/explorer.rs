// SPDX-License-Identifier: AGPL-3.0-only
//! Read-only route-explorer schema and source-bound projection. No routing,
//! location inference, transport observation or authority is created here.
use crate::{ContractError, canonical, checks::*, public::safe_integer};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
type Result<T = ()> = std::result::Result<T, ContractError>;
const C: &str = "route_explorer_invalid";
pub const PROTOCOL: &str = "mycelium.route_explorer.v1";
const REGIONS: &[&str] = &[
    "africa",
    "americas_north",
    "americas_south",
    "asia",
    "europe",
    "oceania",
    "polar",
    "unknown",
];
const SOURCES: &[&str] = &[
    "unavailable",
    "owner_declared",
    "reviewed_relay_metadata",
    "latency_distance_class",
];
const METRICS: &str =
    "rtt_ms warm_rtt_ms jitter_ms goodput_bytes_per_second loss_ratio sample_count measured_zero";
fn fields(v: &Value, s: &str) -> Result {
    exact(v, &s.split_whitespace().collect::<Vec<_>>(), C)
}
fn array(v: &Value, max: usize) -> Result<&Vec<Value>> {
    v.as_array()
        .filter(|a| a.len() <= max)
        .ok_or(ContractError(C))
}
fn optional_int(v: &Value) -> bool {
    v.is_null() || safe_integer(v, 0)
}
fn optional_hash(v: &Value) -> bool {
    v.is_null() || digest(v)
}
pub(crate) fn hash(v: &Value) -> Result<String> {
    Ok(canonical::sha256_prefixed(
        &canonical::to_ascii_bytes(v).map_err(|_| ContractError(C))?,
    ))
}
fn without(v: &Value, key: &str) -> Result<Value> {
    let mut out = v.clone();
    out.as_object_mut().ok_or(ContractError(C))?.remove(key);
    Ok(out)
}
pub fn validate(v: &Value) -> Result {
    let mut names =
        "protocol generation source_mode source_digest nodes segments overlays digest".to_owned();
    if v.get("regions").is_some() {
        names.push_str(" regions");
    }
    fields(v, &names)?;
    require(
        v["protocol"] == PROTOCOL
            && safe_integer(&v["generation"], 0)
            && one_of(
                &v["source_mode"],
                &["live", "fixture", "replay", "degraded"],
            )
            && v["overlays"] == "unavailable"
            && digest(&v["source_digest"]),
        C,
    )?;
    let nodes = array(&v["nodes"], 4096)?;
    let segments = array(&v["segments"], 4096)?;
    let mut ids = BTreeSet::new();
    for n in nodes {
        fields(n, "id kind region region_source")?;
        require(
            digest(&n["id"])
                && ids.insert(n["id"].as_str().ok_or(ContractError(C))?)
                && one_of(&n["kind"], &["member", "endpoint"])
                && one_of(&n["region"], REGIONS)
                && one_of(&n["region_source"], SOURCES),
            C,
        )?;
    }
    if let Some(regions) = v.get("regions") {
        let rows = array(regions, 4096)?;
        require(rows.len() == nodes.len(), C)?;
        let mut seen = BTreeSet::new();
        for r in rows {
            fields(r, "node region region_source region_blocker distance_class")?;
            let id = r["node"].as_str().ok_or(ContractError(C))?;
            require(
                digest(&r["node"])
                    && ids.contains(id)
                    && seen.insert(id)
                    && one_of(&r["region"], REGIONS)
                    && one_of(&r["region_source"], SOURCES)
                    && (r["region_blocker"].is_null()
                        || one_of(
                            &r["region_blocker"],
                            &["missing", "expired", "conflicting", "unauthorized"],
                        ))
                    && (r["distance_class"].is_null()
                        || one_of(&r["distance_class"], &["near", "mid", "far", "unknown"])),
                C,
            )?;
            let n = nodes
                .iter()
                .find(|n| n["id"] == r["node"])
                .ok_or(ContractError(C))?;
            require(
                n["region"] == r["region"] && n["region_source"] == r["region_source"],
                C,
            )?;
        }
        require(
            rows.windows(2)
                .all(|a| a[0]["node"].as_str() <= a[1]["node"].as_str()),
            C,
        )?;
    }
    let mut seen = BTreeSet::new();
    for s in segments {
        fields(
            s,
            "id source destination binding connection_generation issued_at expires_at metrics authority_digest provenance history edge_kind phase path_status role transport freshness",
        )?;
        require(
            digest(&s["id"])
                && seen.insert(s["id"].as_str().ok_or(ContractError(C))?)
                && digest(&s["authority_digest"]),
            C,
        )?;
        for (f, values) in [
            (
                "edge_kind",
                &[
                    "logical_execution",
                    "physical_membership",
                    "transport_observation",
                ][..],
            ),
            (
                "phase",
                &[
                    "prefill_forward",
                    "decode_forward",
                    "decode_closure",
                    "control_probe",
                    "unknown",
                ][..],
            ),
            (
                "path_status",
                &[
                    "proposed",
                    "selected",
                    "observed",
                    "failed",
                    "recovered",
                    "historical",
                    "unknown",
                ][..],
            ),
            (
                "role",
                &[
                    "primary",
                    "alternative",
                    "replica",
                    "recovery_predecessor",
                    "recovery_successor",
                    "draft",
                    "unknown",
                ][..],
            ),
            ("transport", &["direct", "relay", "unknown"][..]),
            ("freshness", &["current", "stale", "unknown"][..]),
        ] {
            require(one_of(&s[f], values), C)?;
        }
        require(
            ["source", "destination"].iter().all(|f| {
                s[*f].is_null()
                    || digest(&s[*f]) && s[*f].as_str().is_some_and(|id| ids.contains(id))
            }) && s["provenance"] == v["source_mode"]
                && s["history"].is_boolean(),
            C,
        )?;
        require(
            ["connection_generation", "issued_at", "expires_at"]
                .iter()
                .all(|f| optional_int(&s[*f]))
                && (s["issued_at"].is_null()
                    || s["expires_at"].is_null()
                    || s["expires_at"].as_u64() >= s["issued_at"].as_u64()),
            C,
        )?;
        let b = &s["binding"];
        fields(b, "deployment request attempt track path_generation")?;
        require(
            ["deployment", "request", "attempt", "track"]
                .iter()
                .all(|f| optional_hash(&b[*f]))
                && optional_int(&b["path_generation"]),
            C,
        )?;
        let m = &s["metrics"];
        fields(m, METRICS)?;
        for f in METRICS.split_whitespace().filter(|f| *f != "measured_zero") {
            require(
                m[f].is_null()
                    || m[f]
                        .as_f64()
                        .is_some_and(|n| (0.0..=9_007_199_254_740_991.0).contains(&n)),
                C,
            )?;
        }
        require(
            m["measured_zero"].is_boolean()
                && optional_int(&m["sample_count"])
                && (m["loss_ratio"].is_null()
                    || m["loss_ratio"].as_f64().is_some_and(|n| n <= 1.0))
                && (!python_equal(&m["loss_ratio"], &json!(0))
                    || m["measured_zero"] == true
                        && m["sample_count"].as_u64().is_some_and(|n| n > 0)),
            C,
        )?;
        if s["transport"] != "unknown" {
            require(
                s["edge_kind"] == "transport_observation"
                    && !s["destination"].is_null()
                    && ["connection_generation", "issued_at", "expires_at"]
                        .iter()
                        .all(|f| !s[*f].is_null()),
                C,
            )?;
        } else {
            require(
                METRICS
                    .split_whitespace()
                    .filter(|f| *f != "measured_zero")
                    .all(|f| m[f].is_null())
                    && m["measured_zero"] == false,
                C,
            )?;
        }
    }
    require(
        nodes
            .windows(2)
            .all(|a| a[0]["id"].as_str() <= a[1]["id"].as_str())
            && segments
                .windows(2)
                .all(|a| segment_key(&a[0]) <= segment_key(&a[1]))
            && v["digest"] == hash(&without(v, "digest")?)?,
        C,
    )
}
fn segment_key(v: &Value) -> (u64, &str) {
    (
        v["issued_at"].as_u64().unwrap_or(0),
        v["id"].as_str().unwrap_or(""),
    )
}
fn node(nodes: &mut BTreeMap<String, Value>, public_id: &Value, kind: &str) -> Result<String> {
    let id = hash(&json!([kind, public_id]))?;
    nodes.insert(
        id.clone(),
        json!({"id":id,"kind":kind,"region":"unknown","region_source":"unavailable"}),
    );
    Ok(id)
}
fn empty_metrics() -> Value {
    json!({"rtt_ms":null,"warm_rtt_ms":null,"jitter_ms":null,"goodput_bytes_per_second":null,"loss_ratio":null,"sample_count":null,"measured_zero":false})
}
fn segment(authority: &Value, mode: &Value) -> Result<Value> {
    Ok(
        json!({"source":null,"destination":null,"binding":{"deployment":null,"request":null,"attempt":null,"track":null,"path_generation":null},"connection_generation":null,"issued_at":null,"expires_at":null,"metrics":empty_metrics(),"authority_digest":hash(authority)?,"provenance":mode,"history":false,"edge_kind":"logical_execution","phase":"unknown","path_status":"unknown","role":"unknown","transport":"unknown","freshness":"unknown"}),
    )
}
fn insert_segment(segments: &mut BTreeMap<String, Value>, mut s: Value) -> Result {
    let id = hash(&json!([s["authority_digest"], s["edge_kind"]]))?;
    s["id"] = json!(id);
    segments.insert(id, s);
    Ok(())
}
/// Requires an independently validated product snapshot. Pure projection only;
/// the publisher still owns source authority, clock and publication sequencing.
pub fn project_from_validated_snapshot(snapshot: &Value) -> Result<Value> {
    let mode = &snapshot["publication"]["source_mode"];
    let material = without(snapshot, "route_explorer")?;
    let mut nodes = BTreeMap::new();
    let mut segments = BTreeMap::new();
    let mut members = BTreeMap::new();
    let entities = array(&snapshot["entities"], 4096)?;
    for e in entities.iter().filter(|e| e["kind"] == "device") {
        let id = e["entity_id"].as_str().ok_or(ContractError(C))?;
        members.insert(id, node(&mut nodes, &e["entity_id"], "member")?);
    }
    for e in entities.iter().filter(|e| e["kind"] == "directed_link") {
        let a = &e["attributes"];
        let b = &e["binding"];
        let src = a["src_device_id"].as_str().and_then(|id| members.get(id));
        let dst = a["dst_device_id"].as_str().and_then(|id| members.get(id));
        if let (Some(src), Some(dst)) = (src, dst) {
            let mut s = segment(&json!({"binding":b,"attributes":a}), mode)?;
            s["source"] = json!(src);
            s["destination"] = json!(dst);
            s["path_status"] = json!("selected");
            s["binding"]["deployment"] = if b["deployment_id"].is_null() {
                Value::Null
            } else {
                json!(hash(&b["deployment_id"])?)
            };
            s["binding"]["path_generation"] = b["route_generation"].clone();
            insert_segment(&mut segments, s)?;
        }
    }
    let history = array(&snapshot["internet_native"]["activation_history"], 64)?;
    let mut latest = BTreeMap::new();
    for (i, a) in history.iter().enumerate() {
        if let Some(id) = a["endpoint_pseudonym"].as_str() {
            latest.insert(id, i);
        }
    }
    for (i, a) in history.iter().enumerate() {
        let Some(id) = a["endpoint_pseudonym"].as_str() else {
            continue;
        };
        let resolved = a["path_source"] == "bound_live_connection" && a["path_class"] != "unknown";
        let mut s = segment(&without(a, "freshness")?, mode)?;
        s["destination"] = json!(node(&mut nodes, &a["endpoint_pseudonym"], "endpoint")?);
        s["edge_kind"] = json!("transport_observation");
        s["path_status"] = json!(if resolved { "observed" } else { "unknown" });
        s["transport"] = if resolved {
            a["path_class"].clone()
        } else {
            json!("unknown")
        };
        s["freshness"] = a["freshness"].clone();
        s["history"] = json!(latest.get(id) != Some(&i));
        s["connection_generation"] = a["connection_generation"].clone();
        s["issued_at"] = a["observed_at_unix_ms"].clone();
        s["expires_at"] = a["evidence_lifetime_until_unix_ms"].clone();
        s["metrics"] = if resolved {
            a["metrics"].clone()
        } else {
            empty_metrics()
        };
        insert_segment(&mut segments, s)?;
    }
    let mut ordered: Vec<_> = nodes.into_values().collect();
    let mut segments: Vec<_> = segments.into_values().collect();
    segments.sort_by(|a, b| segment_key(a).cmp(&segment_key(b)));
    let mut out = json!({"protocol":PROTOCOL,"generation":snapshot["publication"]["generation"],"source_mode":mode,"source_digest":hash(&material)?,"nodes":[],"segments":segments,"overlays":"unavailable"});
    if let Some(evidence) = snapshot["coarse_region_evidence"]
        .as_array()
        .filter(|a| !a.is_empty())
    {
        let now = snapshot["publication"]["published_at_unix_ms"]
            .as_u64()
            .ok_or(ContractError(C))?;
        let rows = reduce_regions(&ordered, evidence, now)?;
        for (n, r) in ordered.iter_mut().zip(&rows) {
            n["region"] = r["region"].clone();
            n["region_source"] = r["region_source"].clone();
        }
        out["regions"] = json!(rows);
    }
    out["nodes"] = json!(ordered);
    out["digest"] = json!(hash(&out)?);
    validate(&out)?;
    Ok(out)
}
fn reduce_regions(nodes: &[Value], evidence: &[Value], now: u64) -> Result<Vec<Value>> {
    for e in evidence {
        crate::evidence::coarse_region(e)?;
    }
    let mut rows = Vec::new();
    for n in nodes {
        let kind = if n["kind"] == "member" {
            "member"
        } else {
            "relay"
        };
        let candidates: Vec<_> = evidence
            .iter()
            .filter(|r| r["subject_ref"] == n["id"] && r["subject_kind"] == kind)
            .collect();
        let geographic: Vec<_> = candidates
            .iter()
            .copied()
            .filter(|r| r["source_kind"] != "latency_distance_class")
            .collect();
        let current: Vec<_> = geographic
            .iter()
            .copied()
            .filter(|r| {
                r["expires_at_unix_ms"]
                    .as_u64()
                    .is_some_and(|expiry| now <= expiry)
            })
            .collect();
        let mut row = json!({"node":n["id"],"region":"unknown","region_source":"unavailable","region_blocker":null,"distance_class":null});
        if candidates.is_empty() {
            row["region_blocker"] = json!("missing");
        } else if !geographic.is_empty() && current.is_empty() {
            row["region_blocker"] = json!("expired");
        } else if !current.is_empty() {
            let codes: BTreeSet<_> = current
                .iter()
                .map(|r| r["region"].as_str().ok_or(ContractError(C)))
                .collect::<Result<_>>()?;
            if codes.len() != 1 {
                row["region_blocker"] = json!("conflicting");
            } else {
                row["region"] = current[0]["region"].clone();
                row["region_source"] = json!(if current
                    .iter()
                    .any(|r| r["source_kind"] == "owner_declared")
                {
                    "owner_declared"
                } else {
                    "reviewed_relay_metadata"
                });
            }
        } else {
            let latency: Vec<_> = candidates
                .iter()
                .copied()
                .filter(|r| {
                    r["source_kind"] == "latency_distance_class"
                        && r["expires_at_unix_ms"]
                            .as_u64()
                            .is_some_and(|expiry| now <= expiry)
                })
                .collect();
            if latency.is_empty() {
                row["region_blocker"] = json!("expired");
            } else {
                let classes: BTreeSet<_> = latency
                    .iter()
                    .map(|r| r["distance_class"].as_str().ok_or(ContractError(C)))
                    .collect::<Result<_>>()?;
                if classes.len() != 1 {
                    row["region_blocker"] = json!("conflicting");
                } else {
                    row["region_source"] = json!("latency_distance_class");
                    row["distance_class"] = latency[0]["distance_class"].clone();
                }
            }
        }
        rows.push(row);
    }
    Ok(rows)
}
