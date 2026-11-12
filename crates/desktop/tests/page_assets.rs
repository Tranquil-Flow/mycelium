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
