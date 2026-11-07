// SPDX-License-Identifier: AGPL-3.0-only
//! Round-15 owner pairing window + capability/link report against the EXECUTED oracle
//! (`L10-evidence/r15-probe-pairwin.py` -> `fixtures/oracle_pairwin.json`, v0.1.4 f4e366e9, Python 3.14):
//! 182 window operations in 14 scenarios (open validation, capacity/eviction, id collision, claim ->
//! approve -> poll, poll shapes, code lockout, submit validation order, stale re-claim, expiry per state,
//! the public HTTP surface) and the capability library (64 verify cases, 17 signed reports byte for
//! byte, 14 `measure_link` runs, 10 `link_probe_response` answers). Results are compared as the
//! oracle's own compact `json.dumps` bytes, so key order and number formatting are pinned too.
use api::{
    capability,
    pairing_windows::{PairingWindows, request_digest},
    schema::{PublicError, Raised},
    wire::{Json, Text},
};
use protocol::identity::Identity;
use serde::Deserialize;
use serde_json::{Value, value::RawValue};
use sha2::{Digest, Sha256};
use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
};

#[derive(Deserialize)]
struct Fixture {
    windows: Vec<Scenario>,
    capability: Cap,
    request_digests: Value,
    keys: Value,
    reports: Value,
}
#[derive(Deserialize)]
struct Scenario {
    name: String,
    draws: Vec<String>,
    start: f64,
    ops: Vec<Op>,
}
#[derive(Deserialize)]
struct Op {
    op: String,
    at: f64,
    origin: Option<Value>,
    pin: Option<Value>,
    ttl: Option<Value>,
    window_id: Option<Value>,
    document: Option<Box<RawValue>>,
    digest: Option<Value>,
    issue: Option<String>,
    method: Option<String>,
    path: Option<String>,
    body: Option<String>,
    /// `null` is a recorded answer (`pending_request` of a free window), not an absent key.
    #[serde(default, deserialize_with = "present")]
    ok: Option<Box<RawValue>>,
    error: Option<String>,
    exception: Option<String>,
    draws_used: Vec<usize>,
    states: std::collections::BTreeMap<String, String>,
    issue_calls: Option<Vec<Value>>,
}
#[derive(Deserialize)]
struct Cap {
    verify: Vec<VerifyCase>,
    signed: Vec<SignCase>,
    measure: Vec<MeasureCase>,
    probe_response: Vec<ProbeCase>,
}
#[derive(Deserialize)]
struct VerifyCase {
    label: String,
    report: Value,
    now: f64,
    expected_key_digest: String,
    node_id: String,
    purposes: Option<Vec<String>>,
    ok: Option<Box<RawValue>>,
    error: Option<String>,
}
#[derive(Deserialize)]
struct SignCase {
    label: String,
    signer_byte: u8,
    args: Value,
    ok: Option<Box<RawValue>>,
    error: Option<String>,
}
#[derive(Deserialize)]
struct MeasureCase {
    label: String,
    rounds: i64,
    bulk_bytes: i64,
    ticks: Vec<f64>,
    wrong: Option<String>,
    ok: Option<Box<RawValue>>,
    error: Option<String>,
    calls: Vec<[u64; 2]>,
}
#[derive(Deserialize)]
struct ProbeCase {
    download_kib: Value,
    upload_len: usize,
    ok_len: Option<usize>,
    ok_head: Option<String>,
    ok_sha256: Option<String>,
    error: Option<String>,
}

fn present<'de, D: serde::Deserializer<'de>>(d: D) -> Result<Option<Box<RawValue>>, D::Error> {
    Box::<RawValue>::deserialize(d).map(Some)
}
fn fixture() -> Fixture {
    serde_json::from_str(include_str!("fixtures/oracle_pairwin.json")).expect("fixture")
}
/// `json.dumps(indent=1)` text -> the compact `separators=(',',':')` text (whitespace outside strings).
fn compact(raw: &str) -> String {
    let (mut out, mut quoted, mut escaped) = (String::new(), false, false);
    for c in raw.chars() {
        if quoted {
            out.push(c);
            if escaped {
                escaped = false;
            } else if c == '\\' {
                escaped = true;
            } else if c == '"' {
                quoted = false;
            }
        } else if c == '"' {
            quoted = true;
            out.push(c);
        } else if !c.is_whitespace() {
            out.push(c);
        }
    }
    out
}
fn dumps(json: &Json) -> String {
    String::from_utf8(json.dumps(Text::Ascii)).expect("ascii")
}
fn signer(byte: u8) -> Identity {
    Identity::from_secret(&format!("endpoint-{byte:02x}"), &[byte; 32]).expect("identity")
}
/// All numbers as f64 (an oracle `1000` and a Rust `1000.0` are the same document here; bytes are
/// compared separately where a signature or a view pins them).
fn norm(value: &Value) -> Value {
    match value {
        Value::Number(n) => serde_json::Number::from_f64(n.as_f64().unwrap_or(f64::NAN))
            .map_or(Value::Null, Value::Number),
        Value::Array(items) => Value::Array(items.iter().map(norm).collect()),
        Value::Object(map) => {
            Value::Object(map.iter().map(|(k, v)| (k.clone(), norm(v))).collect())
        }
        other => other.clone(),
    }
}
fn unhex(text: &str) -> Vec<u8> {
    (0..text.len() / 2)
        .map(|i| u8::from_str_radix(&text[2 * i..2 * i + 2], 16).expect("hex"))
        .collect()
}

#[test]
fn the_window_state_machine_equals_the_executed_oracle_operation_by_operation() {
    let fixture = fixture();
    let mut total = 0;
    for scenario in &fixture.windows {
        let now = Arc::new(Mutex::new(scenario.start));
        let queue = Arc::new(Mutex::new(VecDeque::from(scenario.draws.clone())));
        let drawn = Arc::new(Mutex::new(Vec::<usize>::new()));
        let (clock, texts, log) = (now.clone(), queue.clone(), drawn.clone());
        let windows = PairingWindows::new(
            Box::new(move || *clock.lock().expect("clock")),
            Box::new(move |length| {
                log.lock().expect("log").push(length);
                texts
                    .lock()
                    .expect("queue")
                    .pop_front()
                    .expect("scripted text")
            }),
        );
        for (at, op) in scenario.ops.iter().enumerate() {
            total += 1;
            let label = format!("{} #{at} {}", scenario.name, op.op);
            *now.lock().expect("clock") = op.at;
            drawn.lock().expect("log").clear();
            let null = Value::Null;
            let id = op.window_id.as_ref().unwrap_or(&null);
            let document_text = op.document.as_ref().map_or("null", |d| d.get());
            let document: Value = serde_json::from_str(document_text).expect("document");
            let mut issued = Vec::<Value>::new();
            let result: Result<String, PublicError> = match op.op.as_str() {
                "open" => windows
                    .open(
                        op.origin.as_ref().unwrap_or(&null),
                        op.pin.as_ref().unwrap_or(&null),
                        op.ttl.as_ref().unwrap_or(&null),
                    )
                    .map(|v| dumps(&v)),
                "list" => windows.list().map(|v| dumps(&v)),
                "revoke" => windows.revoke(id).map(|v| dumps(&v)),
                "submit" => windows
                    .submit_bytes(id, document_text.as_bytes())
                    .map(|v| dumps(&v)),
                "poll" => windows.poll(id, &document).map(|v| dumps(&v)),
                "pending" => Ok(windows.pending_request(id).map_or_else(
                    || "null".to_owned(),
                    |r| serde_json::to_string(&r).expect("json"),
                )),
                "approve" => {
                    let mode = op.issue.clone().expect("issue mode");
                    let mut issue = |request: &Value, quota: &Value| -> Result<Json, PublicError> {
                        issued.push(serde_json::json!({
                            "request_digest": request_digest(request).expect("digest"), "quota": quota,
                        }));
                        match mode.as_str() {
                            // The stub echoes the quota in the order the probe's stub saw it.
                            "ok" => Ok(api::jobj! {
                                "stub" => "handoff", "for" => &request["node_id"],
                                "quota" => api::jobj! {
                                    "storage_bytes" => &quota["storage_bytes"],
                                    "network_bytes_per_day" => &quota["network_bytes_per_day"],
                                    "background_allowed" => &quota["background_allowed"],
                                },
                            }),
                            "non_mapping" => Ok(Json::str("text")),
                            "value_error" => Err(PublicError::new(409, "action_not_completed")
                                .raised_by(Raised::Builtin("ValueError"))),
                            m => Err(PublicError::new(
                                409,
                                match m {
                                    "raise:pairing_request_expiring" => "pairing_request_expiring",
                                    other => panic!("issue mode {other}"),
                                },
                            )
                            .raised_by(Raised::Pairing)),
                        }
                    };
                    windows
                        .approve(id, op.digest.as_ref().unwrap_or(&null), &mut issue)
                        .map(|v| dumps(&v))
                }
                "handle" => {
                    let body = unhex(op.body.as_deref().unwrap_or(""));
                    let (status, raw) = windows.handle(
                        op.method.as_deref().expect("method"),
                        op.path.as_deref().expect("path"),
                        &body,
                    );
                    Ok(serde_json::json!({"status": status, "body": String::from_utf8(raw).expect("utf8")})
                        .to_string())
                }
                other => panic!("op {other}"),
            };
            match (&op.ok, &op.error, &op.exception) {
                (Some(expected), None, None) => {
                    let text = result.unwrap_or_else(|e| panic!("{label}: {e:?}"));
                    if op.op == "handle" {
                        let want: Value = serde_json::from_str(expected.get()).expect("json");
                        let got: Value = serde_json::from_str(&text).expect("json");
                        assert_eq!(got, want, "{label}");
                    } else if op.op == "pending" {
                        let want: Value = serde_json::from_str(expected.get()).expect("json");
                        let got: Value = serde_json::from_str(&text).expect("json");
                        assert_eq!(norm(&got), norm(&want), "{label}");
                    } else {
                        assert_eq!(text, compact(expected.get()), "{label}");
                    }
                }
                (None, Some(code), None) => {
                    let error = result.expect_err(&label);
                    assert_eq!(error.code, code.as_str(), "{label}");
                    assert_eq!(error.raised, Raised::Pairing, "{label}");
                }
                (None, None, Some(exception)) => {
                    let error = result.expect_err(&label);
                    assert_eq!(error.raised, Raised::Builtin("ValueError"), "{label}");
                    assert!(exception.starts_with("ValueError:"), "{label}");
                }
                other => panic!("{label}: fixture entry {:?}", other.2),
            }
            assert_eq!(
                *drawn.lock().expect("log"),
                op.draws_used,
                "{label}: random draws"
            );
            let states: std::collections::BTreeMap<String, String> = windows
                .states()
                .into_iter()
                .map(|(k, v)| (k, v.to_owned()))
                .collect();
            assert_eq!(states, op.states, "{label}: window states");
            if let Some(calls) = &op.issue_calls {
                let wanted: Vec<Value> = calls.iter().map(norm).collect();
                let got: Vec<Value> = issued.iter().map(norm).collect();
                assert_eq!(got, wanted, "{label}: issue calls");
            }
        }
    }
    assert_eq!(total, 182, "every recorded operation was replayed");
}

#[test]
fn the_fixture_keys_and_documents_are_the_oracle_s() {
    let fixture = fixture();
    assert_eq!(
        signer(1).verification_key_digest(),
        fixture.keys["signer_1_digest"].as_str().expect("digest")
    );
    let request = &fixture.reports["default_request_1000"];
    assert_eq!(
        request_digest(request).expect("digest"),
        fixture.request_digests["default"].as_str().expect("digest")
    );
    assert_eq!(
        api::persistent::identity_fingerprint(&request["identity_key_digest"])
            .expect("fingerprint"),
        fixture.request_digests["fingerprint"]
            .as_str()
            .expect("fingerprint")
    );
}

#[test]
fn verify_capability_link_report_equals_the_executed_oracle() {
    let fixture = fixture();
    assert_eq!(fixture.capability.verify.len(), 64);
    for case in &fixture.capability.verify {
        let purposes: Vec<&str> = case.purposes.as_ref().map_or_else(
            || vec!["join", "pre_run", "periodic"],
            |p| p.iter().map(String::as_str).collect(),
        );
        let got = capability::verify_report(
            &case.report,
            case.now,
            &case.expected_key_digest,
            &case.node_id,
            &purposes,
        );
        match (&case.ok, &case.error) {
            (Some(want), None) => {
                let want: Value = serde_json::from_str(want.get()).expect("json");
                assert_eq!(
                    norm(&got.unwrap_or_else(|e| panic!("{}: {e:?}", case.label))),
                    norm(&want),
                    "{}",
                    case.label
                );
            }
            (None, Some(code)) => {
                let error = got.expect_err(&case.label);
                assert_eq!(error.code, code.as_str(), "{}", case.label);
            }
            _ => panic!("{}: malformed fixture entry", case.label),
        }
    }
}

#[test]
fn signed_capability_link_reports_are_byte_equal_to_the_oracle() {
    let fixture = fixture();
    assert_eq!(fixture.capability.signed.len(), 17);
    for case in &fixture.capability.signed {
        let a = &case.args;
        let generation = a["generation"].as_i64();
        let generation = if a["generation"].is_boolean() {
            Some(-1)
        } else {
            generation
        };
        let got = capability::signed_report(
            &signer(case.signer_byte),
            a["node_id"].as_str().expect("node"),
            a["purpose"].as_str().expect("purpose"),
            generation,
            &a["device"],
            &a["link"],
            a["now"].as_f64().expect("now"),
            a.get("ttl_seconds")
                .map_or(600, |t| t.as_i64().expect("ttl")),
            a.get("caps"),
        );
        match (&case.ok, &case.error) {
            (Some(want), None) => {
                let want: Value = serde_json::from_str(want.get()).expect("json");
                let got = got.unwrap_or_else(|e| panic!("{}: {e:?}", case.label));
                assert_eq!(norm(&got), norm(&want), "{}", case.label);
                assert_eq!(
                    protocol::canonical::to_bytes(&got).expect("bytes"),
                    protocol::canonical::to_bytes(&want).expect("bytes"),
                    "{}: canonical bytes",
                    case.label
                );
            }
            (None, Some(code)) => {
                assert_eq!(
                    got.expect_err(&case.label).code,
                    code.as_str(),
                    "{}",
                    case.label
                );
            }
            _ => panic!("{}: malformed fixture entry", case.label),
        }
    }
}

#[test]
fn measure_link_equals_the_executed_oracle() {
    let fixture = fixture();
    for case in &fixture.capability.measure {
        let mut ticks = case.ticks.clone().into_iter();
        let mut clock = || ticks.next().expect("scripted tick");
        let mut calls = Vec::<[u64; 2]>::new();
        let wrong = case.wrong.clone();
        let mut transport = |kib: u64, upload: &[u8]| -> Result<Vec<u8>, PublicError> {
            calls.push([kib, upload.len() as u64]);
            let mut received = upload.len();
            let mut filler = "A".repeat(kib as usize * 1024);
            match wrong.as_deref() {
                Some("received") => received += 1,
                Some("filler") => {
                    filler = if kib > 0 {
                        filler[..filler.len() - 1].to_owned()
                    } else {
                        "x".into()
                    }
                }
                Some("notjson") => return Ok(b"nope".to_vec()),
                Some("notdict") => return Ok(b"[]".to_vec()),
                _ => {}
            }
            Ok(format!("{{\"received_bytes\":{received},\"filler\":\"{filler}\"}}").into_bytes())
        };
        let got =
            capability::measure_link(&mut transport, case.rounds, case.bulk_bytes, &mut clock);
        match (&case.ok, &case.error) {
            (Some(want), None) => {
                let want: Value = serde_json::from_str(want.get()).expect("json");
                assert_eq!(
                    norm(&got.unwrap_or_else(|e| panic!("{}: {e:?}", case.label))),
                    norm(&want),
                    "{}",
                    case.label
                );
            }
            (None, Some(code)) => {
                assert_eq!(
                    got.expect_err(&case.label).code,
                    code.as_str(),
                    "{}",
                    case.label
                );
            }
            _ => panic!("{}: malformed fixture entry", case.label),
        }
        assert_eq!(calls, case.calls, "{}: transport calls", case.label);
    }
}

#[test]
fn link_probe_response_equals_the_executed_oracle() {
    let fixture = fixture();
    for case in &fixture.capability.probe_response {
        let got = capability::link_probe_response(&case.download_kib, case.upload_len);
        match (&case.ok_len, &case.error) {
            (Some(len), None) => {
                let raw = got.expect("answer");
                assert_eq!(raw.len(), *len, "{:?}", case.download_kib);
                assert!(
                    String::from_utf8_lossy(&raw)
                        .starts_with(case.ok_head.as_deref().expect("head"))
                );
                let hash: String = Sha256::digest(&raw)
                    .iter()
                    .map(|b| format!("{b:02x}"))
                    .collect();
                assert_eq!(Some(hash), case.ok_sha256, "{:?}", case.download_kib);
            }
            (None, Some(code)) => assert_eq!(got.expect_err("refused").code, code.as_str()),
            _ => panic!("malformed fixture entry"),
        }
    }
}
