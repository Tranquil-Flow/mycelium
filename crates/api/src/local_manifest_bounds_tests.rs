// SPDX-License-Identifier: AGPL-3.0-only
use super::*;

#[test]
fn registered_relative_path_boundaries_match_executed_oracle() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../tests/fixtures/oracle_acquisition_bounds_r23.json"
    ))
    .unwrap();
    for row in fixture["paths"].as_array().unwrap() {
        let got = safe_relative(row["value"].as_str(), "tokenizer");
        if let Some(error) = row["error"].as_str() {
            assert_eq!(got.unwrap_err().0, error, "{row}");
        } else {
            assert_eq!(
                serde_json::to_value(got.unwrap()).unwrap(),
                row["parts"],
                "{row}"
            );
        }
    }
}
