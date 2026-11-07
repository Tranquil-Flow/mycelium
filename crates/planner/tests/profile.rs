// SPDX-License-Identifier: AGPL-3.0-only
use serde_json::Value;
#[test]
fn bounded_header_reader_never_reads_tensor_payload() {
    use std::io::{Cursor, Seek};
    let body = br#"{"tensor":{"data_offsets":[0,2]}}"#;
    let mut bytes = (body.len() as u64).to_le_bytes().to_vec();
    bytes.extend(body);
    bytes.extend(b"unread tensor payload");
    let mut reader = Cursor::new(bytes);
    assert_eq!(
        planner::profile::read_header(&mut reader).unwrap(),
        serde_json::from_slice::<Value>(body).unwrap()
    );
    assert_eq!(reader.stream_position().unwrap(), 8 + body.len() as u64);
    for data in [
        vec![],
        vec![0; 7],
        (104_857_601u64).to_le_bytes().to_vec(),
        [2u64.to_le_bytes().as_slice(), b"{"].concat(),
        [2u64.to_le_bytes().as_slice(), b"[]"].concat(),
    ] {
        assert_eq!(
            planner::profile::read_header(&mut Cursor::new(data))
                .unwrap_err()
                .code,
            "model_header_invalid"
        );
    }
}
#[test]
fn duplicate_shard_tensor_refuses_before_double_accounting() {
    let rows: Value = serde_json::from_str(include_str!("fixtures/model-headers.json")).unwrap();
    let header = rows[0]["headers"][0].clone();
    assert_eq!(
        planner::profile::from_headers(&rows[0]["config"], &[header.clone(), header])
            .unwrap_err()
            .code,
        "model_header_invalid"
    );
}
#[test]
fn exact_header_profile_and_unsupported_architecture_refusals() {
    let rows: Value = serde_json::from_str(include_str!("fixtures/model-headers.json")).unwrap();
    for row in rows.as_array().unwrap() {
        let result =
            planner::profile::from_headers(&row["config"], row["headers"].as_array().unwrap());
        if let Some(expected) = row.get("expected") {
            assert_eq!(
                &result
                    .unwrap_or_else(|e| panic!("{}: {e}", row["name"]))
                    .document(),
                expected,
                "{}",
                row["name"]
            );
        } else {
            assert_eq!(result.unwrap_err().code, row["error"], "{}", row["name"]);
        }
    }
}
