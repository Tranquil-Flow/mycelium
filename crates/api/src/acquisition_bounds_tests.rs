// SPDX-License-Identifier: AGPL-3.0-only
use super::*;

#[test]
fn registered_acquisition_bounds_and_timeout_precedence_match_executed_oracle() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../tests/fixtures/oracle_acquisition_bounds_r23.json"
    ))
    .unwrap();
    assert_eq!(
        max_repository_files() as u64,
        fixture["constants"]["_MAX_REPOSITORY_FILES"]
            .as_u64()
            .unwrap()
    );
    assert_eq!(
        max_path_parts() as u64,
        fixture["constants"]["_MAX_PATH_PARTS"].as_u64().unwrap()
    );
    assert_eq!(
        stream_chunk_bytes() as u64,
        fixture["constants"]["_STREAM_CHUNK_BYTES"]
            .as_u64()
            .unwrap()
    );
    for row in fixture["close"].as_array().unwrap() {
        let timeout = row["timeout"]
            .as_f64()
            .map_or_else(default_timeout, Duration::from_secs_f64);
        assert_eq!(
            close_patience(timeout).as_secs_f64(),
            row["join_seconds"].as_f64().unwrap(),
            "{row}"
        );
    }
}
