// SPDX-License-Identifier: AGPL-3.0-only
//! `execution_admission.checked_ceiling` and `proposal_within_ceiling` against EXECUTED v0.1.4
//! output (FX-SLICE1-ceiling, probe `r11-probe-ceiling.py`): 98 request bodies, 132 proposals.
use owner_run::ceiling;
use serde_json::Value;

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle_ceiling.json")).expect("fixture parses")
}

/// Proves: B-CONSENT-16 (a ceiling is exact: extra/missing key, bool/float/string, zero rss/disk/wall,
/// run_count != 1, any value above the 8 GiB / 1,800 s bounds is `execution_ceiling_invalid`).
#[test]
fn checked_ceiling_matches_the_executed_oracle_on_every_body() {
    let fx = fixture();
    let cases = fx["checked"].as_array().expect("cases");
    assert_eq!(cases.len(), 98);
    let mut accepted = 0;
    for case in cases {
        let text = case["json"].as_str().expect("json text");
        let value: Value = serde_json::from_str(text).expect("body parses");
        let got = ceiling::checked(&value);
        match (&case["checked"]["ok"], &case["checked"]["error"]) {
            (ok, Value::Null) => {
                accepted += 1;
                let got =
                    got.unwrap_or_else(|e| panic!("{text}: oracle accepts, port refused {e}"));
                assert_eq!(&got.to_value(), ok, "{text}: accepted value");
            }
            (_, err) => assert_eq!(got.err(), err.as_str(), "{text}: oracle refuses"),
        }
    }
    assert_eq!(
        accepted, 17,
        "the oracle accepted exactly the recorded bodies"
    );
}

/// Proves: B-CONSENT-16 (a compiled proposal outside the ceiling, or a zero resource proposal, is refused
/// before any work is published; download bytes and run_count must equal their bound).
#[test]
fn proposal_within_ceiling_matches_the_executed_oracle() {
    let fx = fixture();
    let cases = fx["proposals"].as_array().expect("cases");
    assert_eq!(cases.len(), 132);
    for case in cases {
        let p: Value = serde_json::from_str(case["proposal"].as_str().expect("p")).expect("p");
        let c: Value = serde_json::from_str(case["ceiling"].as_str().expect("c")).expect("c");
        assert_eq!(
            ceiling::proposal_within(&p, &c),
            case["within"].as_bool().expect("within"),
            "proposal {p} under ceiling {c}"
        );
    }
}

/// Proves: B-CONSENT-16 (the registered bounds are the oracle's REHEARSAL_MAX).
#[test]
fn registered_bounds_equal_the_oracle_rehearsal_max() {
    let fx = fixture();
    let max = ceiling::rehearsal_max().expect("registered");
    for (key, bound) in max {
        assert_eq!(fx["rehearsal_max"][key].as_u64(), Some(bound), "{key}");
    }
    assert_eq!(
        max.len(),
        fx["rehearsal_max"].as_object().expect("max").len()
    );
}
