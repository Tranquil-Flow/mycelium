// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{canonical, request};
use serde_json::Value;
#[test]
fn request_and_cancellation_event_wire_corpus() {
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../fixtures/continuation-request-vectors.json"
    ))
    .unwrap();
    for r in rows {
        let validate = if r["kind"] == "event" {
            request::event
        } else {
            request::submission
        };
        assert_eq!(validate(&r["value"]), Ok(()), "{}", r["name"]);
        assert_eq!(
            canonical::to_bytes(&r["value"]).unwrap(),
            r["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for case in r["cases"].as_array().unwrap() {
            let got = validate(&case["value"]);
            assert_eq!(
                got.is_ok(),
                case["accepted"].as_bool().unwrap(),
                "{} / {}: {got:?} oracle={}",
                r["name"],
                case["name"],
                case["error"]
            );
            if case["exception"] == "AdmissionError" {
                assert_eq!(
                    got.unwrap_err().0,
                    case["error"].as_str().unwrap(),
                    "{} / {}",
                    r["name"],
                    case["name"]
                );
            }
        }
    }
}
