// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{TimeLimits, canonical, membership, strict};
use serde_json::Value;

#[test]
fn oracle_membership_adversarial_shape_corpus() {
    let rows: Value =
        serde_json::from_str(include_str!("../fixtures/membership-vectors.json")).unwrap();
    let cases: Value =
        serde_json::from_str(include_str!("../fixtures/membership-shape-cases.json")).unwrap();
    let expected_cases: usize = rows
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["envelope"]["message"].as_object().unwrap().len() * 11)
        .sum();
    assert_eq!(cases.as_array().unwrap().len(), expected_cases);
    assert!(expected_cases > 0);
    let mut differences = Vec::new();
    for case in cases.as_array().unwrap() {
        let mut m = rows
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["envelope"]["message"]["protocol"] == case["protocol"])
            .unwrap()["envelope"]["message"]
            .clone();
        let field = case["field"].as_str().unwrap();
        if case["remove"] == true {
            m.as_object_mut().unwrap().remove(field);
        } else {
            m[field] = case["value"].clone();
        }
        let result = membership::validate(
            &m,
            TimeLimits {
                max_ttl_seconds: 3600.0,
                clock_skew_seconds: 30.0,
            },
        );
        let actual = result.err().map(|e| e.0).unwrap_or("ok");
        if actual != case["expected"].as_str().unwrap() {
            differences.push(format!(
                "{} {}={} expected={} actual={actual}",
                case["protocol"], field, case["value"], case["expected"]
            ));
        }
    }
    assert!(
        differences.is_empty(),
        "{} divergences: {}",
        differences.len(),
        differences.join("\n")
    );
}

#[test]
fn float_rendering_matches_1024_external_python_bit_patterns() {
    let cases: Value =
        serde_json::from_str(include_str!("../fixtures/float-vectors.json")).unwrap();
    assert_eq!(cases.as_array().unwrap().len(), 1024);
    for case in cases.as_array().unwrap() {
        let bits = u64::from_str_radix(case["bits"].as_str().unwrap(), 16).unwrap();
        assert_eq!(
            canonical::python_float_repr(f64::from_bits(bits)).unwrap(),
            case["expected"].as_str().unwrap(),
            "{:x}",
            bits
        );
    }
}

#[test]
fn number_tokens_and_reserved_object_keys_are_not_conflated() {
    for raw in [
        br#"{"n":1208925819614629174706176}"#.as_slice(),
        br#"{"$serde_json::private::Number":"1"}"#,
        br#"{"n":1.0}"#,
    ] {
        let v = strict::canonical_object(raw, raw.len()).unwrap();
        assert_eq!(canonical::to_bytes(&v).unwrap(), raw);
    }
    let deeply_nested = format!("{{\"n\":{}0{}}}", "[".repeat(130), "]".repeat(130));
    assert!(strict::object(deeply_nested.as_bytes(), deeply_nested.len()).is_err());
}
