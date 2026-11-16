// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged page must not gate an action on a JavaScript dialog.
//!
//! The packaged webview is WKWebView, which implements none of `window.confirm`, `window.alert` or
//! `window.prompt`: each call returns immediately without drawing anything, and `confirm` returns
//! `false`. Five actions were gated on `window.confirm` - creating a swarm with the public relay,
//! starting the public relay, using your own relay address, revoking a member, and withdrawing
//! standing consent - so on a real install every one of them silently did nothing and showed no
//! error. That left the entire two-Mac join path unreachable: the relay could never be started, so
//! no join code was ever redeemable. They ask through the in-page `<dialog id="ask-dialog">` now.

const APP_JS: &str = include_str!("../assets/app.js");
const INDEX_HTML: &str = include_str!("../assets/index.html");

/// Comment lines in `app.js` legitimately name `window.confirm` to explain why it is banned.
fn script_without_comments(source: &str) -> String {
    source
        .lines()
        .filter(|line| !line.trim_start().starts_with("//"))
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn no_page_action_is_gated_on_a_javascript_dialog() {
    let script = script_without_comments(APP_JS);
    for forbidden in ["window.confirm(", "window.alert(", "window.prompt("] {
        assert!(
            !script.contains(forbidden),
            "app.js calls {forbidden}: the packaged webview implements no JavaScript dialogs, so \
             the call returns false without drawing anything and the action silently never runs"
        );
    }
}

#[test]
fn the_page_owns_the_dialog_the_scripts_await() {
    assert!(
        INDEX_HTML.contains(r#"id="ask-dialog""#),
        "index.html lost the ask dialog the handlers await"
    );
    assert!(
        INDEX_HTML.contains(r#"id="ask-text""#),
        "index.html lost the ask dialog's message element"
    );
    assert!(
        APP_JS.contains("showModal()"),
        "app.js no longer opens the in-page dialog"
    );
}

/// Lane PHY item 3 (ledger row 74 c): a stopped relay keeps its old origin in the status (v0.2
/// `api::relay` status, `crates/api/src/relay.rs:1246-1262`; v0.1.4 `public_relay.py:418-440`, whose
/// `public_origin` attribute survives `close()`), and the page hid `Start public relay` whenever an
/// origin was present (v0.1.4 `web/app.js:503-504`, ported unchanged). So once the temporary relay's
/// 30-minute window closed, the owner could not start one again without restarting the app, although
/// the backend reopens a stopped relay on the same Start (app.py:695-697; `persistent.rs`
/// `RelayState::Stopped`). A dead end the oracle has too: fixed, not ported.
#[test]
fn after_the_relay_window_closes_the_owner_can_start_a_relay_again_from_the_page() {
    let script = script_without_comments(APP_JS);
    let up = script
        .lines()
        .find(|l| l.trim_start().starts_with("const relayUp="))
        .expect("app.js lost the relay visibility rule");
    assert!(up.contains("public_origin"), "{up}");
    assert!(
        up.contains("state!=='stopped'"),
        "a stopped relay's origin stays in the status, so a rule on the origin alone hides \
         `Start public relay` for good once the window closes: {up}"
    );
    for id in ["relay-start", "own-address", "relay-order-note"] {
        assert!(
            script.contains(&format!("$('{id}').hidden=!owner || relayUp;")),
            "{id} no longer follows relayUp"
        );
    }
    let stopped = script
        .lines()
        .find(|l| l.contains("'Temporary relay stopped."))
        .expect("app.js lost the stopped relay line");
    assert!(
        stopped.contains("Start public relay"),
        "the stopped line does not say how to open a new relay: {stopped}"
    );
}

/// Lane PHY item 3 (row 74 c, joined side): after the owner's relay closed or was started again
/// at a new address, a joined Mac's renewals fail (`renewal_error: renewal_unavailable`, app.py:925-927)
/// and v0.1.4's page read that field nowhere: the joined Mac's page showed nothing while every poll
/// got HTTP 530. The page now names the case on the joined Mac.
///
/// Patch 2 (PHY-05): `renewal_error` is recorded by ANY failed renewal pass
/// (`api::app_status::Recorded::renewal`), including a transient or local one that keeps the live
/// Session and its work service (`api/src/app_host.rs`). So the line says only what the state
/// shows: while this Mac's membership is still current it reads "renewal failed, retrying", with
/// no claim that this Mac takes no part and no restart/reconnect advice; only a membership that is
/// no longer current gets the instruction to have the owner start the relay again and reconnect.
#[test]
fn a_joined_macs_relay_line_says_only_what_its_membership_shows() {
    let script = script_without_comments(APP_JS);
    let line = script
        .lines()
        .find(|l| l.contains("rung('Owner relay'"))
        .expect(
            "app.js never shows renewal_error: a joined Mac whose owner relay closed shows nothing",
        );
    let (condition, _) = line
        .split_once('`')
        .unwrap_or_else(|| panic!("the line has no message: {line}"));
    assert!(
        condition.contains("target") && condition.contains("value.renewal_error"),
        "{line}"
    );
    assert!(
        condition
            .replace(' ', "")
            .ends_with("a.membership_state==='current'?"),
        "the relay line must depend on whether this Mac's membership is still current: {line}"
    );
    let messages: Vec<&str> = line.split('`').skip(1).step_by(2).collect();
    assert_eq!(
        messages.len(),
        2,
        "one message per membership state: {line}"
    );
    let (retrying, lapsed) = (messages[0].to_lowercase(), messages[1].to_lowercase());
    assert!(
        retrying.contains("renewal") && retrying.contains("retr"),
        "{retrying}"
    );
    for claim in [
        "takes no part",
        "start the relay again",
        "reconnect",
        "restart",
    ] {
        assert!(
            !retrying.contains(claim),
            "a single failed renewal with a current membership claims `{claim}`: {retrying}"
        );
    }
    assert!(
        lapsed.contains("no longer current")
            && lapsed.contains("start the relay again")
            && lapsed.contains("new address"),
        "{lapsed}"
    );
}
