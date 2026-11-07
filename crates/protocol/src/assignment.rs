// SPDX-License-Identifier: AGPL-3.0-only
//! Pinned layer_assignment.py identity and weight_provisioning.py evidence boundaries.
//! These pure functions neither acquire files nor attest that tensors were loaded.
//! Assignment, report and route are independent inputs, never inferred from each other.
use crate::{
    canonical,
    checks::{digest, integer, python_equal},
    execution_graph::{numeric_cmp, range_count},
};
use serde_json::{Value, json};
use sha1::{Digest, Sha1};
use std::{
    cmp::Ordering,
    collections::{BTreeMap, BTreeSet},
};
const ID_FIELDS: &str = "protocol deployment_id deployment_epoch node_id manifest_digest model_id resolved_commit range components component_tensor_keys component_aliases expected_tensor_prefixes expected_tensor_keys files artifact_cache_root runtime";
type Result<T> = std::result::Result<T, String>;
fn check(ok: bool, error: impl Into<String>) -> Result<()> {
    if ok { Ok(()) } else { Err(error.into()) }
}
fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
        Value::Number(_) => {
            if signed_integer(v) {
                "int"
            } else {
                "float"
            }
        }
    }
}
fn object(v: &Value) -> Result<()> {
    check(
        v.is_object(),
        format!("'{}' object has no attribute 'get'", type_name(v)),
    )
}
fn array(v: &Value) -> Result<&[Value]> {
    if let Some(a) = v.as_array() {
        return Ok(a);
    }
    if v.as_str() == Some("") || v.as_object().is_some_and(|m| m.is_empty()) {
        return Ok(&[]);
    }
    if v.is_string() || v.is_object() {
        return Err("'str' object has no attribute 'get'".into());
    }
    Err(format!("'{}' object is not iterable", type_name(v)))
}
fn uuid(v: &Value) -> Option<[u8; 16]> {
    let s = v.as_str()?.replace("urn:", "").replace("uuid:", "");
    let raw = s.trim_matches(['{', '}']).replace('-', "");
    let b = hex::decode(raw).ok()?;
    b.try_into().ok()
}
fn uuid_string(b: &[u8]) -> String {
    let h = hex::encode(b);
    format!(
        "{}-{}-{}-{}-{}",
        &h[..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..]
    )
}
/// UUIDv5 uses SHA-1 solely as the source identity algorithm, never as authentication.
pub fn id_for(v: &Value) -> Result<String> {
    let mut identity = serde_json::Map::new();
    for f in ID_FIELDS.split_whitespace() {
        identity.insert(
            f.into(),
            v.get(f)
                .ok_or_else(|| format!("assignment identity missing field: {f}"))?
                .clone(),
        );
    }
    if !v["control_plane_binding"].is_null() {
        identity.insert(
            "control_plane_binding".into(),
            v["control_plane_binding"].clone(),
        );
    }
    let ns = uuid(&v["deployment_id"]).ok_or("assignment deployment_id must be a UUID")?;
    check(
        v["deployment_id"] == uuid_string(&ns),
        "assignment deployment_id must be canonical",
    )?;
    let bytes = canonical::to_bytes(&Value::Object(identity)).map_err(|e| e.to_string())?;
    let mut hash = Sha1::new();
    hash.update(ns);
    hash.update(bytes);
    let mut id: [u8; 16] = hash.finalize()[..16]
        .try_into()
        .expect("SHA-1 output length");
    id[6] = (id[6] & 0x0f) | 0x50;
    id[8] = (id[8] & 0x3f) | 0x80;
    Ok(uuid_string(&id))
}
pub fn validate_identity(v: &Value) -> Result<()> {
    let expected = id_for(v)?;
    let actual = uuid(&v["assignment_id"]).ok_or("assignment_id must be a canonical UUID")?;
    check(
        v["assignment_id"] == uuid_string(&actual) && v["assignment_id"] == expected,
        "assignment_id does not bind assignment semantic identity",
    )?;
    check(
        v["route_ready"] == false,
        "assignment route_ready must be false until runtime activation",
    )
}
fn display(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Array(a) => format!("[{}]", a.iter().map(repr).collect::<Vec<_>>().join(", ")),
        Value::Object(m) => format!(
            "{{{}}}",
            m.iter()
                .map(|(k, v)| format!("{}: {}", repr(&json!(k)), repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        _ => v.to_string(),
    }
}
fn repr(v: &Value) -> String {
    v.as_str()
        .map(crate::gossip::repr::string)
        .unwrap_or_else(|| display(v))
}
fn node(v: &Value, default: &str) -> String {
    v.get("node_id")
        .map(display)
        .unwrap_or_else(|| default.into())
}
/// Target path syntax only; no filesystem resolution on this host.
pub fn validate_cache_root(v: &Value, node: &str) -> Result<()> {
    let prefix = format!("artifact cache root for {node}");
    let s = v
        .as_str()
        .filter(|s| !s.is_empty() && !s.contains('\0'))
        .ok_or_else(|| format!("{prefix} must be an absolute path"))?;
    let windows =
        s.len() >= 3 && s.as_bytes()[0].is_ascii_alphabetic() && &s.as_bytes()[1..3] == b":\\";
    if windows {
        let rest = &s[3..];
        let valid = rest.is_empty()
            || rest.split('\\').all(|p| {
                let stem = p
                    .split('.')
                    .next()
                    .unwrap_or("")
                    .trim_end_matches(' ')
                    .to_ascii_uppercase();
                !p.is_empty()
                    && p != "."
                    && p != ".."
                    && !p.ends_with(['.', ' '])
                    && !p
                        .chars()
                        .any(|c| "<>:\"/\\|?*".contains(c) || ('\u{1}'..='\u{1f}').contains(&c))
                    && !["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"].contains(&stem.as_str())
                    && !(stem.len() == 4
                        && (stem.starts_with("COM") || stem.starts_with("LPT"))
                        && matches!(stem.as_bytes()[3], b'1'..=b'9'))
            });
        return check(valid, format!("{prefix} must be canonical"));
    }
    check(
        !s.starts_with('\\'),
        format!("{prefix} must be absolute on a local drive"),
    )?;
    check(s.starts_with('/'), format!("{prefix} must be absolute"))?;
    let rest = if s.starts_with("//") && !s.starts_with("///") {
        &s[2..]
    } else {
        &s[1..]
    };
    check(
        rest.is_empty()
            || rest
                .split('/')
                .all(|p| !p.is_empty() && p != "." && p != ".."),
        format!("{prefix} must be canonical"),
    )
}
fn safe_path(v: &Value) -> Result<&str> {
    let error = format!("unsafe artifact path: {}", repr(v));
    let s = v
        .as_str()
        .filter(|s| {
            !s.is_empty()
                && s.len() <= 1024
                && s.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._/-".contains(&b))
        })
        .ok_or_else(|| error.clone())?;
    let parts: Vec<_> = s
        .split('/')
        .filter(|p| !p.is_empty() && *p != ".")
        .collect();
    check(
        !s.starts_with('/') && parts.len() <= 16 && !parts.contains(&".."),
        error,
    )?;
    Ok(s)
}
pub fn validate(v: &Value) -> Result<()> {
    object(v)?;
    check(
        v["protocol"] == "mycelium.layer_assignment.v2",
        "expected mycelium.layer_assignment.v2",
    )?;
    validate_identity(v)?;
    check(
        v["resolved_commit"].as_str().is_some_and(|s| {
            s.len() == 40
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        }),
        "assignment resolved_commit must be immutable 40-hex",
    )?;
    check(
        digest(&v["manifest_digest"]),
        "assignment manifest_digest is invalid",
    )?;
    validate_cache_root(&v["artifact_cache_root"], &node(v, "peer"))?;
    let r = &v["range"];
    if crate::execution_graph::truthy(r) {
        object(r)?;
    }
    check(
        ["start_layer", "end_layer_exclusive", "layer_count"]
            .iter()
            .all(|f| signed_integer(&r[*f])),
        "assignment range fields must be integers",
    )?;
    check(
        integer(&r["start_layer"], 0)
            && numeric_cmp(&r["end_layer_exclusive"], &r["start_layer"]) == Some(Ordering::Greater)
            && range_count(
                &r["start_layer"],
                &r["end_layer_exclusive"],
                &r["layer_count"],
            )
            .unwrap_or(false),
        "assignment range is invalid",
    )?;
    let files = v["files"]
        .as_array()
        .filter(|a| !a.is_empty())
        .ok_or("assignment requires at least one artifact file")?;
    let mut seen = BTreeSet::new();
    for f in files {
        object(f)?;
        let p = safe_path(&f["path"])?;
        check(seen.insert(p), format!("duplicate artifact path: {p}"))?;
        check(
            integer(&f["size_bytes"], 1),
            format!("invalid artifact size: {p}"),
        )?;
        check(
            digest(&f["content_digest"]),
            format!("invalid artifact digest: {p}"),
        )?;
    }
    for f in ["expected_tensor_keys", "expected_tensor_prefixes"] {
        check(
            v[f].as_array()
                .is_some_and(|a| !a.is_empty() && a.iter().all(Value::is_string)),
            format!("assignment requires {f}"),
        )?;
    }
    Ok(())
}
fn signed_integer(v: &Value) -> bool {
    v.as_number().is_some_and(|n| {
        let s = n.to_string();
        s.trim_start_matches('-')
            .bytes()
            .all(|b| b.is_ascii_digit())
    })
}
fn add(a: &Value, b: &Value) -> Value {
    let a = a.to_string();
    let b = b.to_string();
    let mut ia = a.bytes().rev();
    let mut ib = b.bytes().rev();
    let mut out = Vec::new();
    let mut carry = 0;
    loop {
        let x = ia.next();
        let y = ib.next();
        if x.is_none() && y.is_none() && carry == 0 {
            break;
        }
        let n = x.map_or(0, |x| x - b'0') + y.map_or(0, |x| x - b'0') + carry;
        out.push(n % 10 + b'0');
        carry = n / 10;
    }
    out.reverse();
    serde_json::from_slice(&out).expect("decimal addition")
}
pub fn report_errors(a: &Value, r: &Value) -> Result<Vec<String>> {
    object(a)?;
    let n = node(a, "<unknown>");
    let mut errors = Vec::new();
    if let Err(e) = validate(a) {
        if e.ends_with("has no attribute 'get'") {
            return Err(e);
        }
        errors.push(format!("invalid assignment: {e}"));
    }
    if !r.is_object() {
        errors.push("report must be a JSON object".into());
        return Ok(errors.into_iter().map(|e| format!("{n}: {e}")).collect());
    }
    for f in [
        "deployment_epoch",
        "verified_tensor_count",
        "expected_bytes",
    ] {
        if !integer(&r[f], 0) {
            errors.push(format!("invalid report field type: {f}"));
        }
    }
    if r["protocol"] != "mycelium.artifact_verification_report.v1" {
        errors.push("wrong report protocol".into());
    }
    let stage = [
        "stage_pack",
        "stage_pack_manifest",
        "stage_pack_verification",
        "stage_pack_digest",
        "stage_pack_verification_digest",
    ];
    let count = stage.iter().filter(|f| r.get(**f).is_some()).count();
    if count > 0 && count != stage.len() {
        errors.push("stage-pack evidence must be complete".into());
    }
    for f in &stage[..3] {
        if r.get(*f).is_some() && !r[*f].is_object() {
            errors.push(format!("invalid report field type: {f}"));
        }
    }
    for f in &stage[3..] {
        if r.get(*f).is_some() && !digest(&r[*f]) {
            errors.push(format!("invalid report field: {f}"));
        }
    }
    for f in [
        "deployment_id",
        "deployment_epoch",
        "assignment_id",
        "node_id",
        "manifest_digest",
        "resolved_commit",
        "range",
        "artifact_cache_root",
    ] {
        if !python_equal(&r[f], &a[f]) {
            errors.push(format!("report mismatch: {f}"));
        }
    }
    if r["ready_for_load"] != true {
        errors.push("report is not ready_for_load".into());
    }
    if r["route_ready"] != false {
        errors.push("artifact report must keep route_ready false".into());
    }
    let mut assigned = Vec::<(&str, &Value)>::new();
    if let Some(files) = a["files"].as_array().filter(|a| !a.is_empty()) {
        for f in files {
            if let Some(p) = f["path"].as_str().filter(|_| f.is_object()) {
                if let Some(entry) = assigned.iter_mut().find(|e| e.0 == p) {
                    errors.push(format!("duplicate assigned file: {p}"));
                    entry.1 = f;
                } else {
                    assigned.push((p, f));
                }
            } else {
                errors.push("invalid assigned file record".into());
            }
        }
    } else {
        errors.push("assignment lacks artifact files".into());
    }
    let mut verified = BTreeMap::new();
    if let Some(files) = r["verified_files"].as_array() {
        for f in files {
            if let Some(p) = f["path"].as_str().filter(|_| f.is_object()) {
                if verified.insert(p, f).is_some() {
                    errors.push(format!("duplicate verified file: {p}"));
                }
            } else {
                errors.push("invalid verified file record".into());
            }
        }
    } else {
        errors.push("report lacks verified_files".into());
    }
    if assigned.iter().map(|(p, _)| *p).collect::<BTreeSet<_>>()
        != verified.keys().copied().collect()
    {
        errors.push("verified files do not match assignment".into());
    }
    for (p, f) in &assigned {
        if let Some(actual) = verified.get(p) {
            if !integer(&actual["size_bytes"], 0) {
                errors.push(format!(
                    "invalid report field type: verified file size for {p}"
                ));
            } else if !python_equal(&actual["size_bytes"], &f["size_bytes"]) {
                errors.push(format!("verified size mismatch: {p}"));
            }
            if !python_equal(&actual["content_digest"], &f["content_digest"]) {
                errors.push(format!("verified digest mismatch: {p}"));
            }
        }
    }
    if a["expected_tensor_prefixes"]
        .as_array()
        .is_none_or(|a| a.is_empty())
    {
        errors.push("assignment lacks expected tensor prefixes".into());
    } else if !python_equal(
        &r["verified_tensor_prefixes"],
        &a["expected_tensor_prefixes"],
    ) {
        errors.push("verified tensor prefixes do not match assignment".into());
    }
    if let Some(keys) = a["expected_tensor_keys"]
        .as_array()
        .filter(|a| !a.is_empty())
    {
        let mut unique = Vec::new();
        for k in keys {
            check(
                !k.is_array() && !k.is_object(),
                format!(
                    "cannot use '{}' as a set element (unhashable type: '{}')",
                    type_name(k),
                    type_name(k)
                ),
            )?;
            if !unique.iter().any(|x| python_equal(x, k)) {
                unique.push(k.clone());
            }
        }
        if !python_equal(&r["verified_tensor_count"], &json!(unique.len())) {
            errors.push("verified tensor count does not match assignment".into());
        }
    } else {
        errors.push("assignment lacks expected tensor keys".into());
    }
    let valid_sizes = assigned.iter().all(|(_, f)| integer(&f["size_bytes"], 0));
    let mut total = json!(0);
    if !valid_sizes {
        errors.push("assignment has invalid artifact byte count".into());
    } else {
        for (_, f) in &assigned {
            total = add(&total, &f["size_bytes"]);
        }
        if !python_equal(&r["expected_bytes"], &total) {
            errors.push("expected byte count does not match assignment".into());
        }
    }
    if !valid_sizes
        || !integer(&r["network_download_bytes"], 0)
        || !integer(&r["cache_hit_bytes"], 0)
        || !python_equal(
            &add(&r["network_download_bytes"], &r["cache_hit_bytes"]),
            &total,
        )
    {
        errors.push("download byte accounting does not match assignment".into());
    }
    Ok(errors.into_iter().map(|e| format!("{n}: {e}")).collect())
}
pub(crate) fn manual_route(v: &Value) -> Result<()> {
    check(
        v.is_object() && v["protocol"] == "mycelium.manual_provisioning_route.v1",
        "expected mycelium.manual_provisioning_route.v1",
    )?;
    check(v["ok"] == true, "route plan is not successful")?;
    let m = &v["model"];
    check(m.is_object(), "model must be an object")?;
    check(
        m["model_id"].as_str().is_some_and(|s| !s.is_empty()),
        "model_id required",
    )?;
    check(integer(&m["num_layers"], 1), "num_layers must be positive")?;
    check(
        digest(&m["manifest_digest"]),
        "manifest_digest must be sha256:<64 lowercase hex>",
    )?;
    check(
        m["resolved_commit"].as_str().is_some_and(|s| {
            s.len() == 40
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        }),
        "resolved_commit must be immutable 40-hex",
    )?;
    check(
        v["claim_boundary"].as_str().is_some_and(|s| {
            !s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
                .is_empty()
        }),
        "claim_boundary must be a non-empty string",
    )?;
    let stages = v["route"]
        .as_array()
        .filter(|a| !a.is_empty())
        .ok_or("route must contain at least one stage")?;
    let mut cursor = json!(0);
    let mut nodes = Vec::new();
    for stage in stages {
        object(stage)?;
        let n = stage["node_id"]
            .as_str()
            .filter(|s| !s.is_empty())
            .ok_or("every route stage requires node_id")?;
        check(
            !nodes.contains(&json!(n)),
            format!("duplicate primary route node: {n}"),
        )?;
        nodes.push(json!(n));
        let r = &stage["range"];
        check(r.is_object(), format!("route stage {n} requires range"))?;
        check(
            ["start_layer", "end_layer_exclusive", "layer_count"]
                .iter()
                .all(|f| signed_integer(&r[*f])),
            "layer range fields must be integers",
        )?;
        check(
            integer(&r["start_layer"], 0),
            "start_layer must be non-negative",
        )?;
        check(
            numeric_cmp(&r["end_layer_exclusive"], &r["start_layer"]) == Some(Ordering::Greater),
            "end_layer_exclusive must be greater than start_layer",
        )?;
        check(
            numeric_cmp(&r["end_layer_exclusive"], &m["num_layers"]) != Some(Ordering::Greater),
            "end_layer_exclusive exceeds num_layers",
        )?;
        check(
            range_count(
                &r["start_layer"],
                &r["end_layer_exclusive"],
                &r["layer_count"],
            )
            .unwrap_or(false),
            "layer_count does not match half-open range",
        )?;
        check(
            numeric_cmp(&r["start_layer"], &cursor) != Some(Ordering::Greater),
            format!("route gap before layer {}", r["start_layer"]),
        )?;
        check(
            numeric_cmp(&r["start_layer"], &cursor) != Some(Ordering::Less),
            format!("route overlap at layer {}", r["start_layer"]),
        )?;
        cursor = r["end_layer_exclusive"].clone();
    }
    let mut digits = cursor.to_string().into_bytes();
    for n in digits.iter_mut().rev() {
        if *n > b'0' {
            *n -= 1;
            break;
        }
        *n = b'9';
    }
    let last = String::from_utf8(digits).expect("decimal");
    let last = last.trim_start_matches('0');
    let last = if last.is_empty() { "0" } else { last };
    check(
        numeric_cmp(&cursor, &m["num_layers"]) != Some(Ordering::Less),
        format!("route gap after layer {last}"),
    )?;
    check(
        numeric_cmp(&cursor, &m["num_layers"]) != Some(Ordering::Greater),
        "route exceeds model layer count",
    )?;
    check(
        v["node_order"].is_null() || python_equal(&v["node_order"], &json!(nodes)),
        "node_order does not match route",
    )
}
pub fn audit(
    route: &Value,
    assignments: &Value,
    reports: &Value,
    timestamp: &str,
) -> Result<Value> {
    manual_route(route)?;
    let stages = array(&route["route"])?;
    let assignments = array(assignments)?;
    let reports = array(reports)?;
    let mut errors = Vec::new();
    let nodes: BTreeSet<_> = stages
        .iter()
        .map(|s| s["node_id"].as_str().expect("validated route"))
        .collect();
    let mut index = |items: &[Value], kind: &str| -> Result<BTreeMap<String, Value>> {
        let mut out = BTreeMap::new();
        for item in items {
            object(item)?;
            let Some(n) = item["node_id"].as_str().filter(|n| !n.is_empty()) else {
                errors.push(format!("{kind} lacks node_id"));
                continue;
            };
            if out.insert(n.into(), item.clone()).is_some() {
                errors.push(format!("duplicate {kind} for {n}"));
            }
            if !nodes.contains(n) {
                errors.push(format!("unexpected {kind} for {n}"));
            }
        }
        Ok(out)
    };
    let aa = index(assignments, "assignment")?;
    let rr = index(reports, "report")?;
    let mut identity = None;
    let model = &route["model"];
    for a in assignments {
        let n = node(a, "<unknown>");
        if a["protocol"] != "mycelium.layer_assignment.v2" {
            errors.push(format!("{n} wrong assignment protocol"));
        }
        for f in ["model_id", "manifest_digest", "resolved_commit"] {
            if !python_equal(&a[f], &model[f]) {
                errors.push(format!("{n} assignment {f} does not match route"));
            }
        }
        let id = json!([
            a["deployment_id"],
            a["deployment_epoch"],
            a["manifest_digest"],
            a["resolved_commit"]
        ]);
        if let Some(old) = &identity {
            if !python_equal(old, &id) {
                errors.push(format!("{n} assignment deployment identity mismatch"));
            }
        } else {
            identity = Some(id);
        }
    }
    let mut verified = Vec::new();
    let mut bindings = Vec::new();
    for stage in stages {
        let n = stage["node_id"].as_str().expect("validated route");
        let Some(a) = aa.get(n) else {
            errors.push(format!("missing assignment for {n}"));
            continue;
        };
        bindings
            .push(json!({"node_id":n,"assignment_id":a["assignment_id"],"range":stage["range"]}));
        let Some(r) = rr.get(n) else {
            errors.push(format!("missing report for {n}"));
            continue;
        };
        let before = errors.len();
        errors.extend(report_errors(a, r)?);
        if !python_equal(&a["range"], &stage["range"]) {
            errors.push(format!("{n} assignment range does not match route"));
        }
        if before == errors.len() {
            verified.push(n);
        }
    }
    let good = errors.is_empty() && verified.len() == stages.len();
    let id = identity.unwrap_or(json!([null, null]));
    Ok(
        json!({"protocol":"mycelium.provisioning_audit.v1","model":model,"deployment_id":id[0],"deployment_epoch":id[1],"assignment_bindings":bindings,"all_assignments_verified":good,"ready_for_runtime_load":good,"route_ready":false,"verified_nodes":verified,"errors":errors,"claim_boundary":"artifact provisioning only; runtime layer load, stage probe, and route challenge remain required","timestamp":timestamp}),
    )
}
pub fn audit_errors(
    route: &Value,
    assignments: &Value,
    reports: &Value,
    actual: &Value,
) -> Result<Vec<String>> {
    let Some(fields) = actual.as_object() else {
        return Ok(vec!["provisioning audit must be an object".into()]);
    };
    let expected = match audit(route, assignments, reports, "") {
        Ok(x) => x,
        Err(e) => {
            return Ok(vec![format!(
                "cannot validate provisioning audit inputs: {e}"
            )]);
        }
    };
    let expected = expected.as_object().expect("audit object");
    let mut errors = Vec::new();
    for k in expected.keys() {
        if !fields.contains_key(k) {
            errors.push(format!("provisioning audit missing field: {k}"));
        }
    }
    for k in fields.keys() {
        if !expected.contains_key(k) {
            errors.push(format!("provisioning audit has unexpected field: {k}"));
        }
    }
    for (k, v) in expected {
        if k != "timestamp" && !python_equal(&actual[k], v) {
            errors.push(format!(
                "provisioning audit {k} does not match evidence tranche"
            ));
        }
    }
    if !actual["timestamp"].as_str().is_some_and(|s| {
        !s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
            .is_empty()
    }) {
        errors.push("provisioning audit timestamp must be a non-empty string".into());
    }
    Ok(errors)
}
