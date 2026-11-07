// SPDX-License-Identifier: AGPL-3.0-only
//! V03 characterization: lone surrogates. v0.1.4 (Python) accepts them everywhere
//! `json.loads(bytes)` does (escape `\ud800`, surrogatepass UTF-8, UTF-16/32 code units) and
//! echoes them back as `\ud800` escapes. A Rust `String` cannot hold one, so this port makes two
//! decisions, both recorded here against the EXECUTED oracle's own output for the same bytes
//! (`L10-evidence/r9-probe-surrogates.py` -> `fixtures/oracle_surrogates.json`: the handler's
//! `json.loads` line and `send_value`'s `json.dumps` line, app.py:2000-2008 and 1874):
//!
//! 1. Stored documents and join links (`resource-policy.json`, `mycelium-join:` links) replace
//!    each lone surrogate by U+FFFD before parsing. Nothing in those paths echoes the text, so
//!    every outcome equals the oracle's (see `oracle_policy_encodings.json` and
//!    `oracle_join_text.json`); only the spelling of a lone surrogate differs.
//! 2. HTTP request bodies (`schema::strict_object`) REFUSE every lone surrogate with
//!    400 `invalid_request`, where v0.1.4 accepts the body and echoes `\udXXX`: a known v0.2
//!    divergence (and the same for UTF-16/32 bodies) deliberately not "fixed" in a port. It
//!    belongs to v0.3 and this test must change with that decision.
use api::{
    schema::strict_object,
    wire::{self, Json, Text},
};
use serde_json::Value;

fn hex(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}
fn fixture() -> Value {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/oracle_surrogates.json")).unwrap();
    assert_eq!(
        fixture["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    fixture
}
/// The oracle's answer with every LONE surrogate escape replaced by `\ufffd`: a high escape
/// followed by a low one is a valid pair (kept), anything else is lone.
fn replace_lone(answer: &str) -> String {
    let unit = |at: usize| -> Option<u32> {
        let digits = answer.get(at..at + 6)?;
        digits
            .strip_prefix("\\u")
            .and_then(|h| u32::from_str_radix(h, 16).ok())
    };
    let mut out = String::new();
    let mut at = 0;
    while at < answer.len() {
        match unit(at) {
            Some(0xd800..=0xdbff)
                if unit(at + 6).is_some_and(|low| (0xdc00..=0xdfff).contains(&low)) =>
            {
                out.push_str(&answer[at..at + 12]);
                at += 12;
            }
            Some(0xd800..=0xdfff) => {
                out.push_str("\\ufffd");
                at += 6;
            }
            _ => {
                let c = answer[at..].chars().next().unwrap();
                out.push(c);
                at += c.len_utf8();
            }
        }
    }
    out
}
/// Python builds TWO separate lone code points from the six surrogatepass bytes of a pair, so
/// its (textually pair-looking) answer cannot be told from a joined pair by the answer alone.
const SEPARATE_LONE_PAIR: &str = "surrogatepass_utf8_pair_bytes";

#[test]
fn stored_documents_replace_each_lone_surrogate_with_u_fffd() {
    let fixture = fixture();
    let cases = fixture["cases"].as_array().unwrap();
    assert!(cases.len() >= 17);
    let mut failures = Vec::new();
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let answer = case["answer"]
            .as_str()
            .expect("the oracle accepts every body");
        let expected = if label == SEPARATE_LONE_PAIR {
            r#"{"k":"\ufffd\ufffd"}"#.to_owned()
        } else {
            replace_lone(answer)
        };
        let stored = wire::decode_json_bytes(&hex(case["hex"].as_str().unwrap()))
            .ok()
            .and_then(|text| wire::parse_python_json(&wire::neutralise_surrogate_escapes(&text)))
            .map(|v| String::from_utf8(Json::from(v).dumps(Text::Ascii)).unwrap());
        if stored.as_deref() != Some(expected.as_str()) {
            failures.push(format!(
                "{label}: rust {stored:?}, oracle-derived {expected}"
            ));
        }
    }
    assert!(failures.is_empty(), "{}", failures.join("\n"));
}

#[test]
fn http_bodies_refuse_what_the_oracle_accepts_and_echoes() {
    let fixture = fixture();
    let (mut equal, mut diverge) = (0, 0);
    for case in fixture["cases"].as_array().unwrap() {
        let label = case["label"].as_str().unwrap();
        let oracle = case["answer"].as_str().unwrap();
        let rust = strict_object(&hex(case["hex"].as_str().unwrap())).map(|map| {
            String::from_utf8(Json::from(Value::Object(map)).dumps(Text::Ascii)).unwrap()
        });
        let lone = case["utf8_encodable"] == false;
        match (&rust, lone) {
            (Ok(bytes), false) => {
                assert_eq!(bytes, oracle, "{label}: a body without a lone surrogate");
                equal += 1;
            }
            (Err(error), true) => {
                assert_eq!(
                    (error.status, error.code),
                    (400, "invalid_request"),
                    "{label}"
                );
                assert!(
                    oracle.contains("\\ud"),
                    "{label}: the oracle echoes its lone surrogate"
                );
                diverge += 1;
            }
            other => panic!("{label}: {other:?} (lone surrogate in the oracle: {lone})"),
        }
    }
    // escape_valid_pair and the plain U+FFFD body agree; the other 15 spellings diverge.
    assert_eq!((equal, diverge), (2, 15));
}
