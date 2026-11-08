// SPDX-License-Identifier: AGPL-3.0-only
//! Admission must refuse before native launch when no received shard is owned.
use api::joined_shard::native::validate_prepare;
use serde_json::json;
#[test]
fn native_prepare_requires_received_offer_binding_before_launch() {
    assert_eq!(
        validate_prepare(&json!({}), &json!({}), &json!({}))
            .unwrap_err()
            .0,
        "joined_native_prepare_invalid"
    );
}
