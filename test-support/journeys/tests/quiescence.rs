// SPDX-License-Identifier: AGPL-3.0-only
use journey_tests::scenario::quiescent;
use serde_json::json;
#[test]
fn recovered_closed_acquisition_is_inert_but_active_or_bytes_are_forbidden() {
    // acquisition.py:905-914,1524-1548 @ f5bfb709: close persists closed; restart retains it.
    for state in ["idle", "closed"] {
        assert!(quiescent(
            &json!({"acquisition":{"state":state,"downloaded_bytes":0,"total_bytes":0,"error_code":null},"serving":{"route_ready":false},"network":{"state":"private"},"swarm":{"enabled":false}})
        ));
    }
    for state in [
        "starting",
        "resolving_metadata",
        "checking_capacity",
        "downloading",
        "verifying",
        "compiling",
        "prepared",
        "failed",
        "cancelled",
        "unknown",
    ] {
        assert!(!quiescent(
            &json!({"acquisition":{"state":state,"downloaded_bytes":0},"serving":{"route_ready":false}})
        ));
    }
    assert!(!quiescent(
        &json!({"acquisition":{"state":"closed","downloaded_bytes":1},"serving":{"route_ready":false}})
    ));
    assert!(!quiescent(&json!({})));
}
