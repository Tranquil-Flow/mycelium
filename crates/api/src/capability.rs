// SPDX-License-Identifier: AGPL-3.0-only
//! The signed capability + relay-link report of a joining member (`mycelium_a13_macos/capability.py`
//! @ f4e366e9): `signed_capability_link_report` (member side), `verify_capability_link_report`
//! (owner side, used by the pairing window's `submit`), `measure_link` (RTT and throughput over the
//! SAME relay path) and `link_probe_response` (the owner's `/link-probe/v1/<kib>` answer).
//! Every function is compared with the executed oracle in `tests/pairing_windows.rs`
//! (`fixtures/oracle_pairwin.json`, probe `r15-probe-pairwin.py`).
use crate::schema::{PublicError, Raised};
use budgets::{BudgetId, Entries};
use protocol::{canonical, identity::Identity};
use serde_json::{Map, Number, Value, json};

pub const REPORT_PROTOCOL: &str = "mycelium.a13_capability_link_report.v1";
pub const LINK_PROBE_PATH: &str = "/link-probe/v1";
pub const MEMBER_REPORT_PATH: &str = "/member-report/v1";
/// `REPORT_TTL_SECONDS` (capability.py:96): the lifetime of a signed report, from the budget registry.
pub fn report_ttl_seconds() -> i64 {
    BudgetId::PyMyceliumA13MacosCapabilityReportTtlSecondsL96
        .span(Entries::new(0))
        .map_or(0, |s| i64::try_from(s.millis() / 1000).unwrap_or(i64::MAX))
}
/// `_MAX_PROBE_KIB` (capability.py:110), KiB. SE-L10-PAIRING-BOUNDS (L1b): no registry id yet.
const MAX_PROBE_KIB: u64 = 256;
const PURPOSES: [&str; 3] = ["join", "pre_run", "periodic"];
const REPORT_FIELDS: [&str; 12] = [
    "protocol",
    "node_id",
    "identity_key_digest",
    "identity_verification_key",
    "purpose",
    "generation",
    "observed_at",
    "expires_at",
    "device",
    "link",
    "independently_qualified",
    "signature",
];
const DEVICE_FIELDS: [&str; 8] = [
    "chip",
    "memory_bytes",
    "available_storage_bytes",
    "os_version",
    "mlx",
    "power",
    "thermal",
    "network",
];
const LINK_FIELDS: [&str; 8] = [
    "path_kind",
    "rounds",
    "rtt_ms_median",
    "rtt_ms_samples",
    "upload_bytes",
    "upload_bytes_per_second",
    "download_bytes",
    "download_bytes_per_second",
];

fn fail(code: &'static str) -> PublicError {
    PublicError::new(409, code).raised_by(Raised::Pairing)
}
fn invalid() -> PublicError {
    fail("capability_report_invalid")
}

/// `type(value) is int` (never a bool, never a float).
fn int_of(value: &Value) -> Option<i128> {
    match value {
        Value::Number(n) => n.to_string().parse::<i128>().ok(),
        _ => None,
    }
}
/// `_finite_number(value)`: a finite int/float (never a bool) that is `>= 0`.
fn finite_number(value: &Value) -> bool {
    matches!(value, Value::Number(n) if n.as_f64().is_some_and(|f| f.is_finite() && f >= 0.0))
}
/// `[ -~]{1,96}\Z`.
fn text_ok(value: &Value) -> bool {
    value.as_str().is_some_and(|t| {
        (1..=96).contains(&t.len()) && t.bytes().all(|b| (0x20..=0x7e).contains(&b))
    })
}
fn keys_are(map: &Map<String, Value>, fields: &[&str]) -> bool {
    map.len() == fields.len() && fields.iter().all(|f| map.contains_key(*f))
}
fn device_valid(device: &Value) -> bool {
    let Some(d) = device.as_object().filter(|d| keys_are(d, &DEVICE_FIELDS)) else {
        return false;
    };
    let mlx_ok = d["mlx"].as_object().is_some_and(|m| {
        keys_are(m, &["available", "device"])
            && m["available"].is_boolean()
            && (m["device"].is_null() || text_ok(&m["device"]))
    });
    let one_of = |key: &str, set: &[&str]| d[key].as_str().is_some_and(|v| set.contains(&v));
    text_ok(&d["chip"])
        && text_ok(&d["os_version"])
        && int_of(&d["memory_bytes"]).is_some_and(|v| v > 0)
        && int_of(&d["available_storage_bytes"]).is_some_and(|v| v >= 0)
        && mlx_ok
        && one_of("power", &["external", "battery", "unknown"])
        && one_of(
            "thermal",
            &["nominal", "fair", "serious", "critical", "unknown"],
        )
        && one_of(
            "network",
            &["unmetered", "metered", "unavailable", "unknown"],
        )
}
fn link_valid(link: &Value) -> bool {
    let Some(l) = link.as_object().filter(|l| keys_are(l, &LINK_FIELDS)) else {
        return false;
    };
    let rounds = int_of(&l["rounds"]);
    l["path_kind"] == "public_https_relay"
        && rounds.is_some_and(|r| (1..=8).contains(&r))
        && l["rtt_ms_samples"]
            .as_array()
            .is_some_and(|s| Some(s.len() as i128) == rounds && s.iter().all(finite_number))
        && finite_number(&l["rtt_ms_median"])
        && int_of(&l["upload_bytes"]).is_some()
        && int_of(&l["download_bytes"]).is_some()
        && finite_number(&l["upload_bytes_per_second"])
        && finite_number(&l["download_bytes_per_second"])
}
/// `_caps_valid`: the member's planning caps carried in a report.
fn caps_valid(caps: &Value) -> bool {
    let Some(c) = caps.as_object().filter(|c| !c.is_empty()) else {
        return false;
    };
    c.keys()
        .all(|k| ["storage_bytes", "max_memory_bytes", "max_wall_seconds"].contains(&k.as_str()))
        && c.values()
            .all(|v| int_of(v).is_some_and(|v| 0 < v && v <= i128::from(i64::MAX)))
        && c.get("max_wall_seconds")
            .is_none_or(|v| int_of(v).is_some_and(|v| (60..=3600).contains(&v)))
}
fn node_ok(node: &str) -> bool {
    !node.is_empty()
        && node.len() <= 128
        && node.as_bytes()[0].is_ascii_alphanumeric()
        && node
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}
fn number(value: f64) -> Value {
    Number::from_f64(value).map_or(Value::Null, Value::Number)
}

/// `signed_capability_link_report` (capability.py:236-253): the member signs its device and link
/// measurement with its permanent identity key.
#[allow(clippy::too_many_arguments)]
pub fn signed_report(
    signer: &Identity,
    node_id: &str,
    purpose: &str,
    generation: Option<i64>,
    device: &Value,
    link: &Value,
    now: f64,
    ttl_seconds: i64,
    caps: Option<&Value>,
) -> Result<Value, PublicError> {
    let valid = PURPOSES.contains(&purpose)
        && node_ok(node_id)
        && generation.is_none_or(|g| g >= 0)
        && (purpose == "join" || generation.is_some())
        && device_valid(device)
        && link_valid(link)
        && now.is_finite()
        && now >= 0.0
        && caps.is_none_or(caps_valid)
        && (1..=report_ttl_seconds()).contains(&ttl_seconds);
    if !valid {
        return Err(invalid());
    }
    let mut statement = json!({
        "protocol": REPORT_PROTOCOL, "node_id": node_id,
        "identity_key_digest": signer.verification_key_digest(),
        "identity_verification_key": signer.public_key_record(), "purpose": purpose,
        "generation": generation, "observed_at": number(now),
        "expires_at": number(now + ttl_seconds as f64),
        "device": device, "link": link, "independently_qualified": false,
    });
    if let Some(caps) = caps {
        statement["caps"] = caps.clone();
    }
    let signature = signer.sign(&statement).map_err(|_| invalid())?;
    statement["signature"] = signature;
    Ok(statement)
}

/// `verify_capability_link_report` (capability.py:256-283): returns the signed statement.
pub fn verify_report(
    report: &Value,
    now: f64,
    expected_key_digest: &str,
    node_id: &str,
    purposes: &[&str],
) -> Result<Value, PublicError> {
    let Some(map) = report.as_object().filter(|m| {
        (m.len() == REPORT_FIELDS.len() && REPORT_FIELDS.iter().all(|f| m.contains_key(*f)))
            || (m.len() == REPORT_FIELDS.len() + 1
                && REPORT_FIELDS.iter().all(|f| m.contains_key(*f))
                && m.contains_key("caps"))
    }) else {
        return Err(invalid());
    };
    if map["protocol"] != REPORT_PROTOCOL {
        return Err(invalid());
    }
    let mut statement = map.clone();
    statement.remove("signature");
    let statement = Value::Object(statement);
    let record = &map["identity_verification_key"];
    let signed = record.is_object()
        && record["verification_key_digest"] == expected_key_digest
        && map["identity_key_digest"] == expected_key_digest
        && map["signature"].is_object()
        && protocol::identity::verify(&statement, &map["signature"], record).is_ok();
    if !signed || map["node_id"] != node_id {
        return Err(fail("capability_report_signature_invalid"));
    }
    let observed = &map["observed_at"];
    let expires = &map["expires_at"];
    let lifetime_ok = finite_number(observed)
        && finite_number(expires)
        && matches!(
            (observed.as_f64(), expires.as_f64()),
            (Some(o), Some(e)) if 0.0 < e - o && e - o <= report_ttl_seconds() as f64
        );
    if !map["purpose"]
        .as_str()
        .is_some_and(|p| purposes.contains(&p))
        || map["independently_qualified"] != Value::Bool(false)
        || !device_valid(&map["device"])
        || !link_valid(&map["link"])
        || map.get("caps").is_some_and(|c| !caps_valid(c))
        || !lifetime_ok
    {
        return Err(invalid());
    }
    let (observed, expires) = (
        observed.as_f64().unwrap_or(f64::NAN),
        expires.as_f64().unwrap_or(f64::NAN),
    );
    if !(now.is_finite() && now >= 0.0) || now > expires || now + 60.0 < observed {
        return Err(fail("capability_report_expired"));
    }
    Ok(statement)
}

/// `link_probe_response(download_kib, body)`: bounded, stateless, no secrets.
pub fn link_probe_response(download_kib: &Value, body_len: usize) -> Result<Vec<u8>, PublicError> {
    let probe = || fail("link_probe_invalid");
    let kib = int_of(download_kib)
        .filter(|k| (0..=i128::from(MAX_PROBE_KIB)).contains(k))
        .ok_or_else(probe)?;
    if body_len as u64 > MAX_PROBE_KIB * 1024 {
        return Err(probe());
    }
    let filler = "A".repeat(kib as usize * 1024);
    Ok(format!("{{\"received_bytes\":{body_len},\"filler\":\"{filler}\"}}").into_bytes())
}

/// One timed probe round trip: `transport(download_kib, upload)` returns the raw response.
pub type Transport<'a> = dyn FnMut(u64, &[u8]) -> Result<Vec<u8>, PublicError> + 'a;

/// `measure_link(transport, rounds=, bulk_bytes=, clock=)` (capability.py:159-186): RTT and
/// throughput over the SAME relay path the member uses to reach the owner.
pub fn measure_link(
    transport: &mut Transport<'_>,
    rounds: i64,
    bulk_bytes: i64,
    clock: &mut dyn FnMut() -> f64,
) -> Result<Value, PublicError> {
    let probe = || fail("link_probe_invalid");
    let max_bulk = (MAX_PROBE_KIB * 1024) as i64;
    if !(1..=8).contains(&rounds)
        || bulk_bytes % 1024 != 0
        || !(1024..=max_bulk).contains(&bulk_bytes)
    {
        return Err(probe());
    }
    let mut timed = |download_kib: u64, upload: &[u8]| -> Result<f64, PublicError> {
        let started = clock();
        let raw = transport(download_kib, upload)?;
        let elapsed = clock() - started;
        let value: Value = crate::wire::decode_json_bytes(&raw)
            .ok()
            .and_then(|text| crate::wire::parse_python_json(&text))
            .ok_or_else(probe)?;
        let object = value.as_object().ok_or_else(probe)?;
        let received = object
            .get("received_bytes")
            .and_then(Value::as_f64)
            .is_some_and(|r| r == upload.len() as f64);
        let filler = object
            .get("filler")
            .and_then(Value::as_str)
            .is_some_and(|f| f.chars().count() as u64 == download_kib * 1024);
        if !received || !filler || elapsed.is_nan() || elapsed <= 0.0 {
            return Err(probe());
        }
        Ok(elapsed)
    };
    let round1 = |x: f64| -> f64 { format!("{x:.1}").parse().unwrap_or(f64::NAN) };
    let mut samples = Vec::new();
    for _ in 0..rounds {
        samples.push(round1(timed(0, &[])? * 1000.0));
    }
    let upload = timed(0, &vec![0u8; bulk_bytes as usize])?;
    let download = timed(bulk_bytes as u64 / 1024, &[])?;
    let mut ordered = samples.clone();
    ordered.sort_by(f64::total_cmp);
    Ok(json!({
        "path_kind": "public_https_relay", "rounds": rounds,
        "rtt_ms_median": number(ordered[ordered.len() / 2]),
        "rtt_ms_samples": samples.iter().map(|s| number(*s)).collect::<Vec<_>>(),
        "upload_bytes": bulk_bytes, "upload_bytes_per_second": number(round1(bulk_bytes as f64 / upload)),
        "download_bytes": bulk_bytes, "download_bytes_per_second": number(round1(bulk_bytes as f64 / download)),
    }))
}

/// The canonical bytes a report is signed over (the statement without `signature`).
pub fn statement_bytes(report: &Value) -> Option<Vec<u8>> {
    let mut statement = report.as_object()?.clone();
    statement.remove("signature");
    canonical::to_bytes(&Value::Object(statement)).ok()
}
