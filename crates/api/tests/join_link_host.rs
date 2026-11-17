// SPDX-License-Identifier: AGPL-3.0-only
//! The paste field accepts a relay name and a 12-character code, or a `mycelium-join:` link.
//!
//! A named tunnel origin has dots (`demo.mycelium.now`), and the parser used to accept only
//! `[a-z0-9-]` labels while unconditionally appending `.trycloudflare.com`. So the one origin that
//! survives an owner restart -- the reason the owner chose a named tunnel -- could not be typed at
//! all, and every join had to go through a quick tunnel whose origin dies with the next launch.
use api::join_link::parse_join_text;
use serde_json::json;

#[test]
fn a_named_tunnel_host_is_accepted_as_written() {
    let (link, _) =
        parse_join_text(&json!("demo.mycelium.now ABCD-EFGH-JKMN"), &json!(0.0)).unwrap();
    assert_eq!(link.origin, "https://demo.mycelium.now");
    assert_eq!(link.window_id, "ABCD");
    assert_eq!(link.code, "EFGHJKMN");
}

#[test]
fn a_bare_quick_tunnel_name_still_gets_its_suffix() {
    let (link, _) = parse_join_text(&json!("plain-name ABCD-EFGH-JKMN"), &json!(0.0)).unwrap();
    assert_eq!(link.origin, "https://plain-name.trycloudflare.com");
}

#[test]
fn a_full_quick_tunnel_host_is_not_doubled() {
    let (link, _) = parse_join_text(
        &json!("plain-name.trycloudflare.com ABCD-EFGH-JKMN"),
        &json!(0.0),
    )
    .unwrap();
    assert_eq!(link.origin, "https://plain-name.trycloudflare.com");
}
