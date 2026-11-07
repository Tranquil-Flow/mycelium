// SPDX-License-Identifier: AGPL-3.0-only
use api::schema::strict_object;
use serde_json::json;
#[test]
fn nested_numbers_preserve_scalar_types_under_workspace_feature_union() {
    let v = strict_object(br#"{"a":[1.25,-0.5,1e2],"nested":{"ttl":9007199254740991}}"#).unwrap();
    assert_eq!(v["a"], json!([1.25, -0.5, 100.0]));
    assert_eq!(v["nested"]["ttl"], json!(9007199254740991u64));
}
#[test]
fn private_number_keys_remain_objects_and_nested_duplicates_remain_errors() {
    let v = strict_object(br#"{"n":{"$serde_json::private::Number":"1.25"}}"#).unwrap();
    assert_eq!(v["n"], json!({"$serde_json::private::Number":"1.25"}));
    for raw in [
        br#"{"a":[{"x":1,"x":2}]}"#.as_slice(),
        br#"{"a":{"\u0078":1,"x":2}}"#,
        br#"{"a":NaN}"#,
        br#"{"a":1} false"#,
    ] {
        assert_eq!(strict_object(raw).unwrap_err().code, "invalid_request");
    }
}
