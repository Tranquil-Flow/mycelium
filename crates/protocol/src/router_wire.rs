// SPDX-License-Identifier: AGPL-3.0-only
//! Router v1 binary framing and all eighteen message bodies from the pinned
//! `mycelium_router/wire.py`. Valid wire data is NOT execution authority.
//! No socket, clock, reservation, signature or delivery ownership lives here.
use crate::{ContractError, canonical, checks::*, execution_graph};
use serde_json::{Value, json};
use std::cmp::Ordering;

pub const PROTOCOL: &str = "mycelium.router_wire.v1";
pub const MAX_HEADER_BYTES: usize = 1_048_576;
pub const MAX_PAYLOAD_BYTES: usize = 268_435_456;
const INVALID: &str = "invalid_wire_field";
#[derive(Debug, Clone)]
pub struct DecodedFrame {
    message_type: String,
    // Preserve the received constructor input, not an invented typed authority.
    body: Value,
    payload: Vec<u8>,
}
impl DecodedFrame {
    pub fn message_type(&self) -> &str {
        &self.message_type
    }
    pub fn body(&self) -> &Value {
        &self.body
    }
    pub fn payload(&self) -> &[u8] {
        &self.payload
    }
    pub fn encode(&self) -> Result<Vec<u8>, ContractError> {
        encode_frame(&self.message_type, &self.body, &self.payload)
    }
}
fn required<'a>(v: &'a Value, key: &str) -> Result<&'a Value, ContractError> {
    v.get(key).ok_or(ContractError("missing_wire_field"))
}
fn truth(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::Number(n) => n.as_f64().is_none_or(|n| n != 0.0),
    }
}
fn numeric(v: &Value) -> Result<f64, ContractError> {
    if let Some(b) = v.as_bool() {
        return Ok(f64::from(u8::from(b)));
    }
    if let Some(n) = v.as_f64() {
        return Ok(n);
    }
    if let Some(n) = v.as_number() {
        return Ok(if n.to_string().starts_with('-') {
            f64::NEG_INFINITY
        } else {
            f64::INFINITY
        });
    }
    Err(ContractError(INVALID))
}
fn is_int(v: &Value) -> bool {
    v.as_number()
        .is_some_and(|n| !n.to_string().contains(['.', 'e', 'E']))
}
fn constructor(
    v: &Value,
    required_fields: &str,
    defaults: &[(&str, Value)],
    open: bool,
) -> Result<Value, ContractError> {
    let obj = v.as_object().ok_or(ContractError(INVALID))?;
    let fields: Vec<_> = required_fields.split_whitespace().collect();
    if !open {
        require(
            obj.keys()
                .all(|k| fields.contains(&k.as_str()) || defaults.iter().any(|(d, _)| k == d)),
            INVALID,
        )?;
    }
    let mut out = serde_json::Map::new();
    for f in fields {
        out.insert(f.to_owned(), required(v, f)?.clone());
    }
    for (f, default) in defaults {
        out.insert((*f).to_owned(), v.get(*f).unwrap_or(default).clone());
    }
    Ok(Value::Object(out))
}
fn iterable(v: &Value) -> Result<Vec<Value>, ContractError> {
    match v {
        Value::Array(a) => Ok(a.clone()),
        Value::String(s) => Ok(s.chars().map(|c| Value::String(c.to_string())).collect()),
        Value::Object(m) => Ok(m.keys().map(|k| Value::String(k.clone())).collect()),
        _ => Err(ContractError(INVALID)),
    }
}
fn set(v: &Value) -> Result<Value, ContractError> {
    let mut out = Vec::new();
    for x in iterable(v)? {
        require(!x.is_array() && !x.is_object(), INVALID)?;
        if !out.iter().any(|y| python_equal(&x, y)) {
            out.push(x);
        }
    }
    Ok(Value::Array(out))
}
fn compare(a: &Value, b: &Value) -> Result<Ordering, ContractError> {
    match (a, b) {
        (Value::String(a), Value::String(b)) => Ok(a.cmp(b)),
        _ if (a.is_boolean() || a.is_number()) && (b.is_boolean() || b.is_number()) => {
            if python_equal(a, b) {
                return Ok(Ordering::Equal);
            }
            numeric(a)?
                .partial_cmp(&numeric(b)?)
                .ok_or(ContractError(INVALID))
        }
        _ => Err(ContractError(INVALID)),
    }
}
fn sort_set(v: &mut Value) -> Result<(), ContractError> {
    let a = v.as_array_mut().ok_or(ContractError(INVALID))?;
    // Fallible insertion sort preserves Python's refusal of incomparable types.
    for i in 1..a.len() {
        let mut j = i;
        while j > 0 && compare(&a[j], &a[j - 1])? == Ordering::Less {
            a.swap(j, j - 1);
            j -= 1;
        }
    }
    Ok(())
}
fn hop(v: &Value) -> Result<Value, ContractError> {
    // The source's omitted expiry defaults to infinity, which cannot be encoded
    // with allow_nan=False. Keep it absent and refuse encoding, not decoding.
    let mut h = constructor(
        v,
        "stage_id placement_id reservation_id",
        &[
            ("reservation_expires_at", Value::Null),
            ("reservation_epoch", json!(-1)),
        ],
        false,
    )?;
    if v.get("reservation_expires_at").is_none() {
        h.as_object_mut()
            .ok_or(ContractError(INVALID))?
            .remove("reservation_expires_at");
    }
    Ok(h)
}
fn hops(v: &Value) -> Result<Value, ContractError> {
    Ok(Value::Array(
        iterable(v)?.iter().map(hop).collect::<Result<_, _>>()?,
    ))
}
fn request(v: &Value) -> Result<Value, ContractError> {
    require(v.is_object(), INVALID)?;
    let ids = iterable(required(v, "prompt_token_ids")?)?;
    let mut r = constructor(
        v,
        "request_id prompt_token_ids max_new_tokens expected_new_tokens qos_class admitted_at target_ttft_ms target_tpot_ms target_tokens_per_second sampling_seed generation_config_digest",
        &[],
        false,
    )?;
    r["prompt_token_ids"] = Value::Array(ids);
    Ok(r)
}
fn graph(v: &Value) -> Result<Value, ContractError> {
    execution_graph::parse(v)
        .map(|g| g.into_value())
        .map_err(|e| {
            ContractError(if e.0.contains("missing") {
                "missing_wire_field"
            } else {
                INVALID
            })
        })
}
fn build(v: &Value) -> Result<Value, ContractError> {
    require(v.is_object(), INVALID)?;
    let mut b = constructor(
        v,
        "request graph path_id path_attempt ordered_hops excluded_placements excluded_edges excluded_devices",
        &[],
        true,
    )?;
    b["request"] = request(&b["request"])?;
    b["graph"] = graph(&b["graph"])?;
    b["ordered_hops"] = hops(&b["ordered_hops"])?;
    for f in ["excluded_placements", "excluded_edges", "excluded_devices"] {
        b[f] = set(&b[f])?;
    }
    Ok(b)
}
fn manifest(v: &Value) -> Result<Value, ContractError> {
    require(v.is_object(), INVALID)?;
    let hs = hops(required(v, "ordered_hops")?)?;
    let mut m = constructor(
        v,
        "path_id path_attempt request_id deployment_id deployment_epoch topology_version manifest_digest ordered_hops loopback_edge_id",
        &[("protocol", json!(crate::path_manifest::PROTOCOL))],
        false,
    )?;
    m["ordered_hops"] = hs;
    Ok(m)
}
fn known(kind: &str) -> bool {
    matches!(
        kind,
        "HopHeader"
            | "ManifestDelta"
            | "ManifestLocked"
            | "PathCancellation"
            | "ProgressivePrefillMessage"
            | "PrefillChunkCompleted"
            | "ReservationRequest"
            | "ReservationResult"
            | "ReservationCommitResult"
            | "TokenEvent"
            | "FailureReport"
            | "KVCheckpointShipment"
            | "KVCheckpointAck"
            | "KVSuccessorCutover"
            | "KVSuccessorCutoverReceipt"
            | "KVCheckpointCommit"
            | "KVShadowRelease"
            | "VerifyEvent"
    )
}
fn normalize(kind: &str, v: &Value) -> Result<Value, ContractError> {
    require(known(kind), "unknown_message_type")?;
    require(v.is_object(), "invalid_wire_body")?;
    let b = match kind {
        "HopHeader" => constructor(
            v,
            "request_id path_id path_attempt phase token_index hop_index source_placement_id destination_placement_id topology_version idempotency_key",
            &[("prefill_chunk_token_count", json!(0))],
            false,
        )?,
        "ManifestDelta" => {
            let h = hop(required(v, "hop")?)?;
            let mut b = constructor(
                v,
                "request_id path_id path_attempt hop_index hop",
                &[],
                false,
            )?;
            b["hop"] = h;
            b
        }
        "ManifestLocked" => {
            let mut b = constructor(
                v,
                "request_id path_id path_attempt manifest build",
                &[],
                true,
            )?;
            b["manifest"] = manifest(&b["manifest"])?;
            b["build"] = build(&b["build"])?;
            b
        }
        "ProgressivePrefillMessage" => {
            let mut b = constructor(
                v,
                "header graph request ordered_hops excluded_placements excluded_edges excluded_devices",
                &[],
                true,
            )?;
            require(b["header"].is_object(), INVALID)?;
            b["header"] = normalize("HopHeader", &b["header"])?;
            b["graph"] = graph(&b["graph"])?;
            b["request"] = request(&b["request"])?;
            b["ordered_hops"] = hops(&b["ordered_hops"])?;
            for f in ["excluded_placements", "excluded_edges", "excluded_devices"] {
                b[f] = set(&b[f])?;
            }
            b
        }
        "PathCancellation" => constructor(
            v,
            "request_id path_id path_attempt topology_version",
            &[],
            false,
        )?,
        "ReservationRequest" => constructor(
            v,
            "request_id path_id path_attempt placement_id kv_bytes deployment_epoch lease_expires_at",
            &[],
            false,
        )?,
        "ReservationResult" => constructor(
            v,
            "accepted",
            &[
                ("reservation_id", json!("")),
                ("reason", json!("")),
                ("deployment_epoch", json!(-1)),
                ("expires_at", json!(0.0)),
            ],
            false,
        )?,
        "ReservationCommitResult" => constructor(v, "accepted", &[("reason", json!(""))], false)?,
        "TokenEvent" => constructor(
            v,
            "request_id path_id path_attempt token_index token_id sampling_counter",
            &[],
            false,
        )?,
        "PrefillChunkCompleted" => constructor(
            v,
            "request_id path_id path_attempt chunk_index token_count",
            &[],
            false,
        )?,
        "FailureReport" => constructor(
            v,
            "request_id path_id path_attempt token_index scope reason",
            &[
                ("placement_id", json!("")),
                ("edge_id", json!("")),
                ("node_id", json!("")),
            ],
            false,
        )?,
        "VerifyEvent" => {
            require(required(v, "decisions")?.is_array(), "invalid_verify_event")?;
            constructor(
                v,
                "request_id path_id path_attempt token_index decisions",
                &[],
                false,
            )?
        }
        "KVCheckpointShipment" => constructor(
            v,
            "request_id path_id path_attempt standby_placement_id checkpoint_sequence byte_size",
            &[],
            false,
        )?,
        "KVCheckpointAck" => constructor(
            v,
            "request_id path_id path_attempt hop signed_ack",
            &[],
            false,
        )?,
        "KVSuccessorCutover" => constructor(
            v,
            "request_id old_path_id old_path_attempt successor_path_id successor_path_attempt fencing_generation placement_id role expected_next_position lease_expires_at",
            &[],
            false,
        )?,
        "KVSuccessorCutoverReceipt" => constructor(
            v,
            "request_id successor_path_id successor_path_attempt fencing_generation placement_id installed reason checkpoint_digest",
            &[],
            false,
        )?,
        "KVCheckpointCommit" => constructor(
            v,
            "request_id path_id path_attempt request_attempt source_placement_id committed_token_count committed_prefix_digest",
            &[],
            false,
        )?,
        "KVShadowRelease" => constructor(v, "request_id path_id path_attempt reason", &[], false)?,
        _ => return Err(ContractError("unknown_message_type")),
    };
    Ok(b)
}
fn a7text(v: &Value) -> bool {
    v.as_str().is_some_and(|s| {
        !s.is_empty()
            && s.chars().count() <= 512
            && s.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
                == s
    })
}
fn validate(kind: &str, b: &Value) -> Result<(), ContractError> {
    if kind.starts_with("KV") {
        let t = |f| a7text(&b[f]);
        let n = |f, min| integer(&b[f], min);
        let valid = match kind {
            "KVCheckpointShipment" => {
                t("request_id")
                    && t("path_id")
                    && n("path_attempt", 0)
                    && t("standby_placement_id")
                    && n("checkpoint_sequence", 0)
                    && n("byte_size", 1)
                    && numeric(&b["byte_size"])? <= 67_108_864.0
            }
            "KVCheckpointAck" => {
                t("request_id")
                    && t("path_id")
                    && n("path_attempt", 0)
                    && one_of(&b["hop"], &["standby_to_source", "source_to_entry"])
                    && b["signed_ack"]
                        .as_str()
                        .is_some_and(|s| (2..=16_384).contains(&s.len()))
            }
            "KVSuccessorCutover" => {
                t("request_id")
                    && t("old_path_id")
                    && t("successor_path_id")
                    && b["old_path_id"] != b["successor_path_id"]
                    && n("old_path_attempt", 0)
                    && n("successor_path_attempt", 1)
                    && int_greater(&b["successor_path_attempt"], &b["old_path_attempt"])
                    && n("fencing_generation", 1)
                    && t("placement_id")
                    && one_of(&b["role"], &["standby_import", "surviving_rekey"])
                    && n("expected_next_position", 1)
                    && b["lease_expires_at"].as_f64().is_some_and(f64::is_finite)
            }
            "KVSuccessorCutoverReceipt" => {
                t("request_id")
                    && t("successor_path_id")
                    && n("successor_path_attempt", 1)
                    && n("fencing_generation", 1)
                    && t("placement_id")
                    && b["installed"].is_boolean()
                    && t("reason")
                    && b["checkpoint_digest"]
                        .as_str()
                        .is_some_and(|s| s.chars().count() <= 71)
            }
            "KVCheckpointCommit" => {
                t("request_id")
                    && t("path_id")
                    && n("path_attempt", 0)
                    && n("request_attempt", 1)
                    && t("source_placement_id")
                    && n("committed_token_count", 1)
                    && b["committed_prefix_digest"]
                        .as_str()
                        .is_some_and(|s| s.chars().count() == 71 && s.starts_with("sha256:"))
            }
            "KVShadowRelease" => {
                t("request_id")
                    && t("path_id")
                    && n("path_attempt", 0)
                    && one_of(
                        &b["reason"],
                        &[
                            "completed",
                            "cancelled",
                            "failed",
                            "fenced",
                            "fallback",
                            "aborted",
                        ],
                    )
            }
            _ => false,
        };
        return require(valid, INVALID);
    }
    if kind == "PathCancellation" {
        require(
            is_int(&b["path_attempt"]) && is_int(&b["topology_version"]),
            INVALID,
        )?;
    }
    if kind == "ProgressivePrefillMessage" {
        let h = &b["header"];
        validate("HopHeader", h)?;
        let hs = b["ordered_hops"].as_array().ok_or(ContractError(INVALID))?;
        return require(
            h["phase"] == "PREFILL"
                && python_equal(&b["request"]["request_id"], &h["request_id"])
                && python_equal(&b["graph"]["topology_version"], &h["topology_version"])
                && hs.len() as f64 == numeric(&h["hop_index"])? + 1.0
                && hs.last().is_some_and(|last| {
                    python_equal(&last["placement_id"], &h["destination_placement_id"])
                }),
            "invalid_progressive_prefill",
        );
    }
    if kind == "ManifestLocked" {
        return validate_locked(b);
    }
    if matches!(
        kind,
        "HopHeader"
            | "ManifestDelta"
            | "PathCancellation"
            | "ReservationRequest"
            | "TokenEvent"
            | "PrefillChunkCompleted"
            | "FailureReport"
            | "VerifyEvent"
    ) {
        require(
            truth(&b["request_id"]) && truth(&b["path_id"]),
            "invalid_wire_identity",
        )?;
        require(numeric(&b["path_attempt"])? >= 0.0, "invalid_path_attempt")?;
    }
    match kind {
        "HopHeader" => require(
            numeric(&b["hop_index"])? >= 0.0
                && numeric(&b["topology_version"])? >= 0.0
                && truth(&b["destination_placement_id"])
                && truth(&b["idempotency_key"]),
            "invalid_hop_header",
        ),
        "ManifestDelta" => require(
            numeric(&b["hop_index"])? >= 0.0 && truth(&b["hop"]["reservation_id"]),
            "invalid_manifest_delta",
        ),
        "PathCancellation" => require(
            numeric(&b["topology_version"])? >= 0.0,
            "invalid_path_cancellation",
        ),
        "ReservationRequest" => require(
            truth(&b["placement_id"])
                && numeric(&b["kv_bytes"])? >= 0.0
                && numeric(&b["deployment_epoch"])? >= 0.0
                && numeric(&b["lease_expires_at"])? > 0.0,
            "invalid_reservation_request",
        ),
        "TokenEvent" => require(
            numeric(&b["token_index"])? >= 0.0 && numeric(&b["sampling_counter"])? >= 1.0,
            "invalid_token_event",
        ),
        "VerifyEvent" => require(
            integer(&b["token_index"], 1)
                && b["decisions"].as_array().is_some_and(|a| {
                    (2..=17).contains(&a.len()) && a.iter().all(|x| integer(x, 0))
                }),
            "invalid_verify_event",
        ),
        "PrefillChunkCompleted" => require(
            numeric(&b["chunk_index"])? >= 0.0 && numeric(&b["token_count"])? >= 1.0,
            "invalid_prefill_chunk_completed",
        ),
        "FailureReport" => require(
            truth(&b["scope"]) && truth(&b["reason"]),
            "invalid_failure_report",
        ),
        _ => Ok(()),
    }
}
fn int_greater(a: &Value, b: &Value) -> bool {
    let a = a.to_string();
    let b = b.to_string();
    a.len() > b.len() || a.len() == b.len() && a > b
}
fn validate_locked(b: &Value) -> Result<(), ContractError> {
    let m = &b["manifest"];
    let build = &b["build"];
    let g = &build["graph"];
    let code = "invalid_manifest_locked";
    require(
        truth(&b["request_id"])
            && python_equal(&b["request_id"], &m["request_id"])
            && python_equal(&b["request_id"], &build["request"]["request_id"])
            && ["path_id", "path_attempt"]
                .iter()
                .all(|f| python_equal(&b[*f], &m[*f]) && python_equal(&b[*f], &build[*f]))
            && python_equal(&m["ordered_hops"], &build["ordered_hops"]),
        code,
    )?;
    require(
        m["protocol"] == crate::path_manifest::PROTOCOL
            && [
                "deployment_id",
                "deployment_epoch",
                "topology_version",
                "manifest_digest",
            ]
            .iter()
            .all(|f| python_equal(&m[*f], &g[*f])),
        code,
    )?;
    let hs = m["ordered_hops"].as_array().ok_or(ContractError(code))?;
    let stages = g["stages"].as_array().ok_or(ContractError(code))?;
    require(hs.len() == stages.len(), code)?;
    for (s, h) in stages.iter().zip(hs) {
        require(
            python_equal(&s["stage_id"], &h["stage_id"])
                && s["placements"].as_array().is_some_and(|ps| {
                    ps.iter()
                        .any(|p| python_equal(&p["placement_id"], &h["placement_id"]))
                })
                && truth(&h["reservation_id"]),
            code,
        )?;
    }
    let edges = g["edges"].as_array().ok_or(ContractError(code))?;
    for pair in hs.windows(2) {
        require(
            edges.iter().any(|e| {
                python_equal(&e["from_placement_id"], &pair[0]["placement_id"])
                    && python_equal(&e["to_placement_id"], &pair[1]["placement_id"])
            }),
            code,
        )?;
    }
    let first = hs.first().ok_or(ContractError(code))?;
    let last = hs.last().ok_or(ContractError(code))?;
    require(
        g["loopback_edges"].as_array().is_some_and(|es| {
            es.iter().any(|e| {
                python_equal(&e["from_placement_id"], &last["placement_id"])
                    && python_equal(&e["to_placement_id"], &first["placement_id"])
                    && python_equal(&e["edge_id"], &m["loopback_edge_id"])
            })
        }),
        code,
    )
}
fn encoding_body(kind: &str, b: &mut Value) -> Result<(), ContractError> {
    if kind == "ManifestDelta" {
        require(
            b["hop"].get("reservation_expires_at").is_some(),
            "invalid_wire_message",
        )?;
    }
    let containers: &[&str] = if kind == "ProgressivePrefillMessage" {
        &[""]
    } else if kind == "ManifestLocked" {
        &["manifest", "build"]
    } else {
        &[]
    };
    for name in containers {
        let c = if name.is_empty() {
            &mut *b
        } else {
            &mut b[*name]
        };
        for h in c["ordered_hops"].as_array().ok_or(ContractError(INVALID))? {
            require(
                h.get("reservation_expires_at").is_some(),
                "invalid_wire_message",
            )?;
        }
        for f in ["excluded_placements", "excluded_edges", "excluded_devices"] {
            if let Some(s) = c.get_mut(f) {
                sort_set(s)?;
            }
        }
    }
    Ok(())
}
/// Encode a source constructor body, filling only source-defined defaults.
/// The source deliberately leaves many legacy fields untyped; this is wire
/// compatibility, not permission to execute or trust those fields.
pub fn encode_frame(kind: &str, body: &Value, payload: &[u8]) -> Result<Vec<u8>, ContractError> {
    let mut b = normalize(kind, body)?;
    validate(kind, &b)?;
    require(payload.len() <= MAX_PAYLOAD_BYTES, "payload_too_large")?;
    encoding_body(kind, &mut b)?;
    let envelope = json!({"protocol":PROTOCOL,"message_type":kind,"body":b,"payload_length":payload.len(),"payload_sha256":&canonical::sha256_prefixed(payload)[7..]});
    let header =
        canonical::to_ascii_bytes(&envelope).map_err(|_| ContractError("invalid_wire_message"))?;
    require(header.len() <= MAX_HEADER_BYTES, "header_too_large")?;
    let length = u32::try_from(header.len()).map_err(|_| ContractError("header_too_large"))?;
    let mut out = Vec::with_capacity(4 + header.len() + payload.len());
    out.extend(length.to_be_bytes());
    out.extend(header);
    out.extend(payload);
    Ok(out)
}
/// Bounded finite-JSON decoder. Duplicate keys refuse, consistent with the
/// workspace strict ingress rule (the legacy Python JSON loader was permissive).
pub fn decode_frame(frame: &[u8]) -> Result<DecodedFrame, ContractError> {
    let prefix = frame.get(..4).ok_or(ContractError("truncated_frame"))?;
    let length = u32::from_be_bytes(
        prefix
            .try_into()
            .map_err(|_| ContractError("truncated_frame"))?,
    ) as usize;
    require(length <= MAX_HEADER_BYTES, "header_too_large")?;
    let end = 4 + length;
    let raw = frame.get(4..end).ok_or(ContractError("truncated_header"))?;
    let envelope: Value =
        serde_json::from_slice(raw).map_err(|_| ContractError("invalid_wire_json"))?;
    require(envelope.is_object(), "invalid_wire_envelope")?;
    let envelope = crate::strict::object(raw, MAX_HEADER_BYTES)
        .map_err(|_| ContractError("invalid_wire_json"))?;
    require(
        required(&envelope, "protocol")? == PROTOCOL,
        "unknown_wire_protocol",
    )?;
    let kind = required(&envelope, "message_type")?
        .as_str()
        .filter(|k| known(k))
        .ok_or(ContractError("unknown_message_type"))?;
    let body = required(&envelope, "body")?;
    require(body.is_object(), "invalid_wire_body")?;
    let declared = required(&envelope, "payload_length")?;
    require(integer(declared, 0), "invalid_payload_length")?;
    require(
        numeric(declared)? <= MAX_PAYLOAD_BYTES as f64,
        "payload_too_large",
    )?;
    let payload = &frame[end..];
    require(
        declared.as_u64() == Some(payload.len() as u64),
        "payload_length_mismatch",
    )?;
    require(
        required(&envelope, "payload_sha256")? == &canonical::sha256_prefixed(payload)[7..],
        "payload_digest_mismatch",
    )?;
    let normalized = normalize(kind, body)?;
    validate(kind, &normalized)?;
    Ok(DecodedFrame {
        message_type: kind.to_owned(),
        body: body.clone(),
        payload: payload.to_vec(),
    })
}
