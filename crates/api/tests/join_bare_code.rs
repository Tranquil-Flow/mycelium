// SPDX-License-Identifier: AGPL-3.0-only
//! A bare invite code names no relay. On a Mac that already knows one -- a lapse, a rejoin, or a
//! reinstall that kept its identity -- it should still join, instead of demanding the relay name the
//! owner has no way to read out to you.
use api::join_link::{is_bare_code, link_for_known_origin};
use serde_json::json;

#[test]
fn only_a_code_alone_counts_as_bare() {
    assert!(is_bare_code(&json!("ABCD-EFGH-JKMN")));
    assert!(is_bare_code(&json!("ABCDEFGHJKMN")));
    assert!(!is_bare_code(&json!("quiet-river-apple-stone ABCD-EFGH-JKMN")), "a named form names its relay");
    assert!(!is_bare_code(&json!("mycelium-join:eyJwcm90b2NvbCI6IngifQ")), "a link carries its own origin");
    assert!(!is_bare_code(&json!("ABCD-EFGH-JKM")), "a short code is not a code");
}

#[test]
fn a_bare_code_resolves_against_the_known_origin() {
    let link = link_for_known_origin("https://demo.mycelium.now", &json!("ABCD-EFGH-JKMN")).expect("link");
    assert_eq!(link.origin, "https://demo.mycelium.now");
    assert_eq!(link.window_id, "ABCD");
    assert_eq!(link.code, "EFGHJKMN");
    assert_eq!(link.seed_key_digest, None);
    assert!(link_for_known_origin("https://demo.mycelium.now", &json!("nope")).is_err());
}
