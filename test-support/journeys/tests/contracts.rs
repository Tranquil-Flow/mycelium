// SPDX-License-Identifier: AGPL-3.0-only
use journey_tests::{compare, loopback_origin, parse_sse, project};
use serde_json::json;

#[test]
fn loopback_only_never_resolve_hosts_or_accept_url_credentials() {
    assert_eq!(loopback_origin("http://127.0.0.1:12345").unwrap(), 12345);
    for bad in [
        "https://127.0.0.1:1",
        "http://localhost:1",
        "http://127.0.0.1:1@evil",
        "http://192.0.2.1:1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:1/path",
    ] {
        assert!(loopback_origin(bad).is_err(), "accepted {bad}");
    }
}
#[test]
fn wire_projection_is_scalar_allowlist_not_recursive_redaction() {
    let input =
        json!({"authority":{"membership_state":"absent","secret":"NEVER"},"cookie":"NEVER"});
    assert_eq!(
        project(&input, &["/authority/membership_state"]).unwrap(),
        json!({"/authority/membership_state":"absent"})
    );
    assert!(project(&input, &["/authority"]).is_err());
    assert!(project(&input, &["/missing"]).is_err());
}
#[test]
fn comparator_kills_changed_status_defaults_missing_and_extra_observations() {
    let old = json!([{"id":"fresh","status":200,"fields":{"storage":4000000000u64}}]);
    assert!(compare(&old, &old).is_ok());
    for bad in [
        json!([]),
        json!([{"id":"fresh","status":401,"fields":{"storage":4000000000u64}}]),
        json!([{"id":"fresh","status":200,"fields":{"storage":2000000000u64}}]),
        json!([{}, {}]),
    ] {
        assert!(compare(&old, &bad).is_err());
    }
}
#[test]
fn sse_order_multiline_comments_crlf_and_incomplete_terminal() {
    let events = parse_sse(
        ": heartbeat\r\nevent: token\r\nid: 7\r\ndata: first\r\ndata: second\r\n\r\nevent: done\ndata: {}\n\n",
    )
    .unwrap();
    assert_eq!(
        events,
        vec![
            json!({"event":"token","id":"7","data":"first\nsecond"}),
            json!({"event":"done","id":"7","data":"{}"})
        ]
    );
    assert!(
        parse_sse(
            "event: done
data: {}"
        )
        .is_err()
    );
}
