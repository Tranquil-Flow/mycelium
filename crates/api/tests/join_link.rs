// SPDX-License-Identifier: AGPL-3.0-only
//! `pairing.parse_join_text`, `canonical_https_origin` and the relay origin/port/options
//! validators against the executed v0.1.4 oracle (`oracle_join_text.json`, probe
//! `L10-evidence/r9-probe-join-text.py`, Python 3.14 @ f4e366e9): every outcome (value, or
//! exception class/module/code) must be identical.
use api::join_link::{self, RelayOptions};
use serde_json::{Map, Value};

fn fixture() -> Value {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/oracle_join_text.json"
    );
    let raw = std::fs::read_to_string(path).unwrap();
    // The oracle holds a lone surrogate (`\ud800`); ingress turns those into U+FFFD.
    let text = api::wire::neutralise_surrogate_escapes(&raw);
    serde_json::from_str(&text).unwrap()
}

/// `{"float":"nan"|"inf"|"<repr>"}` is the probe's spelling of a Python float JSON cannot hold.
fn decode(value: &Value) -> Value {
    match value.get("float").and_then(Value::as_str) {
        Some("nan" | "inf") => Value::String("non-finite".into()),
        _ => value.clone(),
    }
}
fn now_of(value: &Value) -> f64 {
    match value.get("float").and_then(Value::as_str) {
        Some("nan") => f64::NAN,
        Some("inf") => f64::INFINITY,
        _ => value.as_f64().unwrap_or(f64::NAN),
    }
}

#[test]
fn parse_join_text_matches_the_oracle_on_every_input() {
    let doc = fixture();
    let cases = doc["join_text"].as_array().unwrap();
    assert!(cases.len() >= 190, "{} cases", cases.len());
    let mut failures = Vec::new();
    for case in cases {
        let label = format!("{} {}", case["label"], case["text"]);
        let text = decode(&case["text"]);
        // `now` of a non-number type is passed through as the JSON value itself.
        let now = if case["now"].get("float").is_some() {
            serde_json::Number::from_f64(now_of(&case["now"]))
                .map_or_else(|| Value::String("non-finite".into()), Value::Number)
        } else {
            case["now"].clone()
        };
        let got = join_link::parse_join_text(&text, &now);
        let want = case.get("ok");
        match (got, want) {
            (Ok((link, expires_at)), Some(want)) => {
                let same = want["origin"] == link.origin
                    && want["window_id"] == link.window_id
                    && want["code"] == link.code
                    && want["seed_key_digest"]
                        == link
                            .seed_key_digest
                            .as_deref()
                            .map_or(Value::Null, Value::from)
                    && match (want["expires_at"].as_f64(), expires_at) {
                        (None, None) => want["expires_at"].is_null(),
                        (Some(a), Some(b)) => a == b,
                        _ => false,
                    };
                if !same {
                    failures.push(format!("{label}: value {link:?} {expires_at:?} != {want}"));
                }
            }
            (Err(error), None) => {
                let (ty, module) = error.raised.identity();
                let err = &case["err"];
                let want_code = err["code"].as_str().unwrap_or("action_not_completed");
                if err["type"] != ty
                    || err["module"] != module
                    || want_code != error.code
                    || error.status != 409
                {
                    failures.push(format!("{label}: {error:?} ({ty}, {module}) != {err}"));
                }
            }
            (got, want) => failures.push(format!("{label}: {got:?} vs oracle {want:?}")),
        }
    }
    assert!(
        failures.is_empty(),
        "{} of {} differ:\n{}",
        failures.len(),
        cases.len(),
        failures
            .iter()
            .take(15)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}

fn verdict<T: std::fmt::Debug>(r: Result<T, join_link::Invalid>) -> Value {
    match r {
        Ok(v) => serde_json::json!({"ok": format!("{v:?}").trim_matches('"')}),
        Err(_) => serde_json::json!({"err": "ValueError"}),
    }
}

#[test]
fn origin_validators_match_the_oracle() {
    let doc = fixture();
    let cases = doc["origins"].as_array().unwrap();
    assert!(cases.len() >= 99);
    let mut failures = Vec::new();
    for case in cases {
        let value = &case["value"];
        for (name, got) in [
            (
                "canonical",
                verdict(join_link::canonical_https_origin(value)),
            ),
            (
                "external",
                verdict(join_link::validate_external_origin(value)),
            ),
        ] {
            let want = &case[name];
            let want = match want.get("ok") {
                Some(ok) => serde_json::json!({"ok": ok}),
                None => serde_json::json!({"err": want["err"]["type"]}),
            };
            if got != want {
                failures.push(format!("{name} {value}: {got} != {want}"));
            }
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn proxy_ports_match_the_oracle() {
    let doc = fixture();
    let cases = doc["ports"].as_array().unwrap();
    for case in cases {
        let value = &case["value"];
        if value.get("float").is_some() {
            continue;
        }
        let got = join_link::validate_proxy_port(value);
        match (&got, case["result"].get("ok")) {
            (Ok(port), Some(want)) => assert_eq!(want, port, "{value}"),
            (Err(_), None) => {}
            _ => panic!("port {value}: {got:?} vs {}", case["result"]),
        }
    }
}

#[test]
fn relay_options_match_the_oracle_constructor() {
    let doc = fixture();
    let cases = doc["relay"].as_array().unwrap();
    assert!(cases.len() >= 30);
    let mut failures = Vec::new();
    for case in cases {
        let spec: &Map<String, Value> = case["spec"].as_object().unwrap();
        let got = RelayOptions::external(spec);
        match (got, case.get("ok")) {
            (Ok(opts), Some(want)) => {
                let same = want["cloudflare_front"] == opts.cloudflare_front
                    && want["auto_renew"] == opts.auto_renew
                    && want["long_lived"] == opts.long_lived
                    && want["renewal_cap"].as_f64() == Some(opts.renewal_cap_seconds)
                    && want["public_origin"] == opts.public_origin
                    && want["proxy_port"] == opts.proxy_port;
                if !same {
                    failures.push(format!("{}: {opts:?} != {want}", case["label"]));
                }
            }
            (Err(code), None) => {
                if case["err"]["message"] != code {
                    failures.push(format!("{}: {code} != {}", case["label"], case["err"]));
                }
            }
            (got, want) => failures.push(format!("{}: {got:?} vs {want:?}", case["label"])),
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}
