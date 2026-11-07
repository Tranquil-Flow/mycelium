// SPDX-License-Identifier: AGPL-3.0-only
//! Exact Observatory semantic documents. No wall-clock freshness qualification,
//! authenticated evidence, owner lease, durable publication or SSE is implied.
use crate::{
    ContractError,
    checks::*,
    public::{fields, safe_integer},
};
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet},
    net::IpAddr,
};
const C: &str = "observatory_document_invalid";
type Result<T = ()> = std::result::Result<T, ContractError>;
type Date = [u32; 7];
fn id(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.len() <= 128
            && s.as_bytes()[0].is_ascii_alphanumeric()
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
            && !crate::product::credential(s)
            && s.parse::<IpAddr>().is_err()
    })
}
fn timestamp(v: &Value) -> Result<Date> {
    let s = v.as_str().ok_or(ContractError(C))?;
    let b = s.as_bytes();
    require(
        (b.len() == 20 || (22..=27).contains(&b.len())) && s.is_ascii(),
        C,
    )?;
    require(
        b[4] == b'-'
            && b[7] == b'-'
            && b[10] == b'T'
            && b[13] == b':'
            && b[16] == b':'
            && b.last() == Some(&b'Z'),
        C,
    )?;
    let mut out = [0; 7];
    for (i, (start, end)) in [(0, 4), (5, 7), (8, 10), (11, 13), (14, 16), (17, 19)]
        .into_iter()
        .enumerate()
    {
        require(b[start..end].iter().all(u8::is_ascii_digit), C)?;
        out[i] = s[start..end].parse().map_err(|_| ContractError(C))?;
    }
    let [year, month, day, hour, minute, second, _] = out;
    require(
        year > 0 && (1..=12).contains(&month) && hour <= 24 && minute < 60 && second < 60,
        C,
    )?;
    let days = match month {
        2 => {
            if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) {
                29
            } else {
                28
            }
        }
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    require(day >= 1 && day <= days, C)?;
    if b.len() > 20 {
        require(
            b[19] == b'.' && b[20..b.len() - 1].iter().all(u8::is_ascii_digit),
            C,
        )?;
        let fraction = &s[20..s.len() - 1];
        let mut micros: u32 = fraction.parse().map_err(|_| ContractError(C))?;
        for _ in fraction.len()..6 {
            micros *= 10;
        }
        out[6] = micros;
    }
    // The recorded Python 3.14 datetime parser admits exact end-of-day 24:00.
    // Preserve its spelling on the wire, but compare the following civil day.
    if hour == 24 {
        require(minute == 0 && second == 0 && out[6] == 0, C)?;
        out[3] = 0;
        if day < days {
            out[2] += 1;
        } else {
            out[2] = 1;
            if month < 12 {
                out[1] += 1;
            } else {
                require(year < 9999, C)?;
                out[0] += 1;
                out[1] = 1;
            }
        }
    }
    Ok(out)
}
fn freshness(v: &Value, outer: Option<(Date, Date)>) -> Result<(Date, Date)> {
    fields(v, "observed_at valid_until", C)?;
    let a = timestamp(&v["observed_at"])?;
    let b = timestamp(&v["valid_until"])?;
    require(
        a < b && outer.is_none_or(|(start, end)| a >= start && b <= end),
        C,
    )?;
    Ok((a, b))
}
fn provenance(v: &Value, kind: &str, producer: &str) -> Result {
    fields(v, "kind producer", C)?;
    require(v["kind"] == kind && v["producer"] == producer, C)
}
fn binding(v: &Value) -> Result {
    fields(v, "deployment model route", C)?;
    let d = &v["deployment"];
    fields(d, "id epoch", C)?;
    require(id(&d["id"]) && safe_integer(&d["epoch"], 1), C)?;
    let m = &v["model"];
    fields(m, "id revision manifest_digest num_layers", C)?;
    require(
        id(&m["id"])
            && id(&m["revision"])
            && digest(&m["manifest_digest"])
            && safe_integer(&m["num_layers"], 1),
        C,
    )?;
    let r = &v["route"];
    fields(r, "id generation digest assignments", C)?;
    require(
        id(&r["id"]) && safe_integer(&r["generation"], 1) && digest(&r["digest"]),
        C,
    )?;
    let a = r["assignments"]
        .as_array()
        .filter(|a| !a.is_empty())
        .ok_or(ContractError(C))?;
    let mut next = 0;
    let mut seen = BTreeSet::new();
    for row in a {
        fields(row, "id peer_id start_layer end_layer_exclusive", C)?;
        require(
            id(&row["id"])
                && id(&row["peer_id"])
                && safe_integer(&row["start_layer"], 0)
                && safe_integer(&row["end_layer_exclusive"], 1),
            C,
        )?;
        let start = row["start_layer"].as_u64().ok_or(ContractError(C))?;
        let end = row["end_layer_exclusive"]
            .as_u64()
            .ok_or(ContractError(C))?;
        require(
            seen.insert(row["id"].as_str().ok_or(ContractError(C))?)
                && start == next
                && end > start,
            C,
        )?;
        next = end;
    }
    require(m["num_layers"].as_u64() == Some(next), C)
}
fn scope<'a>(v: &'a Value, binding: &Value, request: &Value) -> Result<&'a str> {
    fields(v, "kind id", C)?;
    require(id(&v["id"]), C)?;
    let kind = v["kind"].as_str().ok_or(ContractError(C))?;
    let bound = match kind {
        "deployment" | "model" | "route" => v["id"] == binding[kind]["id"],
        "request" => v["id"] == *request,
        "assignment" => binding["route"]["assignments"]
            .as_array()
            .is_some_and(|a| a.iter().any(|x| x["id"] == v["id"])),
        _ => false,
    };
    require(bound, C)?;
    Ok(kind)
}
/// Validate source shape, all copied bindings, bounded freshness intervals,
/// provenance pairs and exact conflict groups. A valid historical document may
/// still be stale; the live publisher owns trusted-now qualification.
pub fn snapshot(v: &Value) -> Result {
    fields(
        v,
        "protocol snapshot_id freshness binding claims conflicts route_challenge request_lifecycle provenance",
        C,
    )?;
    require(
        v["protocol"] == "mycelium.observatory.snapshot.v1" && id(&v["snapshot_id"]),
        C,
    )?;
    let window = freshness(&v["freshness"], None)?;
    binding(&v["binding"])?;
    provenance(&v["provenance"], "gateway_projection", "mycelium_gateway")?;
    let l = &v["request_lifecycle"];
    fields(
        l,
        "request_id state path_attempt freshness binding provenance",
        C,
    )?;
    require(
        id(&l["request_id"])
            && one_of(
                &l["state"],
                &[
                    "admitting",
                    "prefill",
                    "locked",
                    "decoding",
                    "completed",
                    "failed",
                    "cancelled",
                ],
            )
            && safe_integer(&l["path_attempt"], 1),
        C,
    )?;
    binding(&l["binding"])?;
    require(l["binding"] == v["binding"], C)?;
    freshness(&l["freshness"], Some(window))?;
    provenance(&l["provenance"], "router_runtime", "mycelium_router")?;
    let challenge = &v["route_challenge"];
    fields(challenge, "id status freshness binding provenance", C)?;
    require(
        id(&challenge["id"]) && one_of(&challenge["status"], &["succeeded", "failed"]),
        C,
    )?;
    binding(&challenge["binding"])?;
    require(challenge["binding"] == v["binding"], C)?;
    freshness(&challenge["freshness"], Some(window))?;
    provenance(
        &challenge["provenance"],
        "route_challenge",
        "mycelium_router",
    )?;
    let claims = v["claims"].as_array().ok_or(ContractError(C))?;
    let mut by_id = BTreeMap::new();
    let mut groups: BTreeMap<(&str, &str, &str), BTreeSet<&str>> = BTreeMap::new();
    for c in claims {
        fields(c, "id scope statement value freshness provenance", C)?;
        require(id(&c["id"]), C)?;
        let cid = c["id"].as_str().ok_or(ContractError(C))?;
        require(by_id.insert(cid, c).is_none(), C)?;
        let kind = scope(&c["scope"], &v["binding"], &l["request_id"])?;
        let (statement, pk, producer) = match kind {
            "deployment" => ("deployment_bound", "gateway_projection", "mycelium_gateway"),
            "model" => ("model_bound", "provisioning_audit", "mycelium_provisioning"),
            "route" => (
                "route_challenge_succeeded",
                "route_challenge",
                "mycelium_router",
            ),
            "assignment" => (
                "assignment_ready",
                "provisioning_audit",
                "mycelium_provisioning",
            ),
            "request" => (
                "request_lifecycle_observed",
                "router_runtime",
                "mycelium_router",
            ),
            _ => return Err(ContractError(C)),
        };
        require(
            c["statement"] == statement
                && one_of(&c["value"], &["confirmed", "rejected", "unknown"]),
            C,
        )?;
        freshness(&c["freshness"], Some(window))?;
        provenance(&c["provenance"], pk, producer)?;
        groups
            .entry((
                kind,
                c["scope"]["id"].as_str().ok_or(ContractError(C))?,
                statement,
            ))
            .or_default()
            .insert(cid);
    }
    let mut reported = BTreeSet::new();
    for c in v["conflicts"].as_array().ok_or(ContractError(C))? {
        fields(c, "claim_ids scope reason", C)?;
        let ids = c["claim_ids"]
            .as_array()
            .filter(|a| a.len() >= 2)
            .ok_or(ContractError(C))?;
        require(
            one_of(
                &c["reason"],
                &["binding_mismatch", "value_mismatch", "freshness_overlap"],
            ),
            C,
        )?;
        scope(&c["scope"], &v["binding"], &l["request_id"])?;
        let mut unique = BTreeSet::new();
        for value in ids {
            require(id(value), C)?;
            let cid = value.as_str().ok_or(ContractError(C))?;
            require(
                unique.insert(cid)
                    && by_id
                        .get(cid)
                        .is_some_and(|claim| claim["scope"] == c["scope"]),
                C,
            )?;
        }
        reported.insert(unique);
    }
    require(
        groups.values().all(|g| g.len() < 2 || reported.contains(g)),
        C,
    )
}
pub fn event(v: &Value) -> Result {
    fields(v, "protocol generation snapshot", C)?;
    require(
        v["protocol"] == "mycelium.observatory.event.v1" && safe_integer(&v["generation"], 1),
        C,
    )?;
    snapshot(&v["snapshot"])
}
