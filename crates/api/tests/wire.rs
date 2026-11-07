// SPDX-License-Identifier: AGPL-3.0-only
//! Byte-for-byte `json.dumps` parity (integrator ruling (a)). Expected bytes come from the
//! EXECUTED v0.1.4 call (`r8-probe-wire.py`, Python 3.14): insertion key order, `\uXXXX`
//! escapes, Python float repr.
use api::wire::{Json, Text, float_repr};
use serde_json::{Number, Value};

fn tagged(v: &Value) -> Json {
    match v {
        Value::Object(o) if o.len() == 1 && o.contains_key("$o") => Json::Obj(
            o["$o"]
                .as_array()
                .unwrap()
                .iter()
                .map(|pair| (pair[0].as_str().unwrap().to_owned(), tagged(&pair[1])))
                .collect(),
        ),
        Value::Object(o) if o.len() == 1 && o.contains_key("$n") => {
            Json::Num(serde_json::from_str::<Number>(o["$n"].as_str().unwrap()).unwrap())
        }
        Value::Array(a) => Json::Arr(a.iter().map(tagged).collect()),
        other => Json::from(other.clone()),
    }
}
fn cases() -> Vec<Value> {
    let fixture: Value = serde_json::from_str(include_str!("fixtures/oracle_wire.json")).unwrap();
    assert_eq!(
        fixture["oracle"],
        "f4e366e98a1f705dfaf182897044500d290dbd7f"
    );
    fixture["cases"].as_array().unwrap().clone()
}

#[test]
fn every_oracle_case_is_byte_identical_in_both_text_modes() {
    let cases = cases();
    assert!(cases.len() > 4000);
    let mut failures = Vec::new();
    for case in &cases {
        let input = tagged(&case["input"]);
        for (mode, key) in [(Text::Ascii, "ascii"), (Text::Utf8, "utf8")] {
            let got = String::from_utf8(input.dumps(mode)).unwrap();
            if got != case[key].as_str().unwrap() {
                failures.push(format!("{} {key}: {got} != {}", case["name"], case[key]));
            }
        }
    }
    assert!(
        failures.is_empty(),
        "{} mismatches, first: {:?}",
        failures.len(),
        failures.first()
    );
}
#[test]
fn insertion_order_is_kept_and_value_conversion_sorts() {
    let ordered = Json::object([("zeta", Json::int(1)), ("alpha", Json::int(2))]);
    assert_eq!(ordered.dumps(Text::Ascii), br#"{"zeta":1,"alpha":2}"#);
    assert_eq!(ordered.keys(), ["zeta", "alpha"]);
    assert_eq!(
        Json::from(serde_json::json!({"zeta": 1, "alpha": 2})).dumps(Text::Ascii),
        br#"{"alpha":2,"zeta":1}"#
    );
}
#[test]
fn set_replaces_in_place_like_a_dict() {
    let mut value = Json::object([("a", Json::int(1)), ("b", Json::int(2))]);
    value.set("a", Json::int(9));
    value.set("c", Json::int(3));
    assert_eq!(value.dumps(Text::Ascii), br#"{"a":9,"b":2,"c":3}"#);
}
#[test]
fn float_repr_matches_cpython_edges() {
    for (value, repr) in [
        (1e16, "1e+16"),
        (1e-5, "1e-05"),
        (0.0001, "0.0001"),
        (123456789012345680.0, "1.2345678901234568e+17"),
        (100.0, "100.0"),
        (-0.0, "-0.0"),
    ] {
        assert_eq!(float_repr(value), repr);
    }
}
