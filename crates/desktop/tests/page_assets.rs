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
/// Patch 3 (PHY-05 residual): the line says only what the membership state shows, over the full
/// matrix `peer::status_membership` projects (`crates/api/src/peer.rs`: `current`, `retained`,
/// `absent`, a lease-`expired` session, and the terminal dispositions `expired`, `revoked`,
/// `stale`, `unknown`). `renewal_error` is recorded by ANY failed renewal pass, and a reachable
/// owner can answer `revoked`/`stale`, so only an expired membership gets the restart/reconnect
/// advice (v0.2 records no confirmed-unreachable owner origin: `recovery_error` is not a reachability
/// verdict). Revoked/stale: the owner ended or replaced this Mac's membership and must add it
/// again. Retained: renewal is retrying. Unknown, and any state the page does not know: not known
/// yet. (Replaces patch 2's test, which accepted ONE message for every non-current state.)
#[test]
fn a_joined_macs_relay_line_follows_the_full_membership_state_matrix() {
    const PEER_RS: &str = include_str!("../../api/src/peer.rs");
    const MATRIX: [&str; 7] = [
        "current", "retained", "absent", "expired", "revoked", "stale", "unknown",
    ];
    // The matrix is the source's: every state is a literal of peer.rs and every terminal
    // disposition is in the matrix.
    for state in MATRIX {
        assert!(
            PEER_RS.contains(&format!("\"{state}\"")),
            "peer.rs has no `{state}`"
        );
    }
    let terminal = PEER_RS
        .lines()
        .find(|l| l.starts_with("const TERMINAL_STATES"))
        .expect("peer.rs lost TERMINAL_STATES");
    for state in terminal.split('"').skip(1).step_by(2) {
        assert!(
            MATRIX.contains(&state),
            "a terminal state the page does not map: {state}"
        );
    }
    let script = script_without_comments(APP_JS);
    let line = script
        .lines()
        .find(|l| l.contains("rung('Owner relay'"))
        .expect(
            "app.js never shows renewal_error: a joined Mac whose owner relay closed shows nothing",
        );
    assert!(
        line.contains("target") && line.contains("value.renewal_error"),
        "{line}"
    );
    const ADVICE: [&str; 4] = [
        "start the relay again",
        "reconnect",
        "restart",
        "new address",
    ];
    let advises = |m: &str| ADVICE.iter().find(|a| m.contains(*a)).copied();
    for state in MATRIX.into_iter().chain(["", "some_new_state"]) {
        let message = relay_message(&script, line, state).to_lowercase();
        match state {
            "current" => {
                assert!(
                    message.contains("retr") && message.contains("current"),
                    "{state}: {message}"
                );
                assert!(!message.contains("takes no part"), "{state}: {message}");
            }
            "retained" => {
                assert!(
                    message.contains("renewal") && message.contains("retr"),
                    "{state}: {message}"
                );
                assert!(!message.contains("takes no part"), "{state}: {message}");
            }
            "expired" => {
                assert!(
                    message.contains("expired")
                        && message.contains("start the relay again")
                        && message.contains("new address"),
                    "{state}: {message}"
                );
                continue;
            }
            "revoked" | "stale" => assert!(
                message.contains("ended or replaced") && message.contains("again"),
                "{state}: {message}"
            ),
            "absent" => assert!(message.contains("no membership"), "{state}: {message}"),
            _ => assert!(message.contains("not known yet"), "{state:?}: {message}"),
        }
        assert_eq!(
            advises(&message),
            None,
            "only an expired membership gets the restart/reconnect advice; `{state}` does: {message}"
        );
        assert!(!message.contains("cannot reach"), "{state}: {message}");
    }
}

/// The message the relay line shows for `state`: the page's state map (`relayLine` names an entry
/// of `relayAdvice`, `?? '<key>'` for any other state), or the single ternary on
/// `membership_state==='current'` the page had before patch 3.
fn relay_message(script: &str, line: &str, state: &str) -> String {
    let Some(map) = script.lines().find(|l| l.contains("const relayLine={")) else {
        let messages: Vec<&str> = line.split('`').skip(1).step_by(2).collect();
        assert!(
            line.replace(' ', "")
                .contains("a.membership_state==='current'?")
                && messages.len() == 2,
            "the relay line has neither a state map nor the current/other ternary: {line}"
        );
        return messages[if state == "current" { 0 } else { 1 }].to_owned();
    };
    let body = map
        .split_once('{')
        .and_then(|(_, rest)| rest.split_once('}'))
        .map(|(body, _)| body)
        .unwrap_or_else(|| panic!("unreadable relayLine: {map}"));
    let pairs: Vec<(&str, &str)> = body
        .split(',')
        .map(|pair| {
            let (key, value) = pair.split_once(':').unwrap_or_else(|| panic!("{pair}"));
            (key.trim(), value.trim().trim_matches('\''))
        })
        .collect();
    let fallback = line
        .split_once("?? '")
        .and_then(|(_, rest)| rest.split_once('\''))
        .map(|(key, _)| key)
        .unwrap_or_else(|| panic!("the relay line has no fallback for another state: {line}"));
    let key = pairs
        .iter()
        .find(|(k, _)| *k == state)
        .map_or(fallback, |(_, v)| *v);
    let advice = script
        .split_once("const relayAdvice={")
        .and_then(|(_, rest)| rest.split_once("};"))
        .map(|(body, _)| body)
        .expect("app.js has no relayAdvice");
    let mut parts = advice.split('`');
    while let Some(name) = parts.next() {
        let Some(text) = parts.next() else { break };
        if name
            .trim()
            .trim_start_matches(',')
            .trim()
            .trim_end_matches(':')
            == key
        {
            return text.to_owned();
        }
    }
    panic!("relayAdvice has no `{key}` (for state `{state}`)")
}

/// Lane CHATS item 3 (owner ruling 14 Nov, ledger rows 17 and 106 b): one chat path, through the
/// run's route; no one-Mac native chat in the packaged app. The page still carried v0.1.4's
/// `#inference` card ("03 · Native model-stage work", index.html:39 @ f4e366e9, with its nav link
/// at :6): a Prompt box reading "Available only after this Mac has a qualified assignment" and a
/// "Run on qualified route" button (inert: app.js only prevents the form's submit), beside the model
/// download. On rv39 the owner typed into it during a serving run and pressed "Download / resume
/// source". The one-Mac prompt and its button are gone from the shipped page, and so is the nav
/// link. The card itself is hidden: its model download is still the owner's model-prepare step
/// (owner-run `NOT_PREPARED_MESSAGE` names it, run.js points at `#acquisition` for the
/// `prepare_model` fix), so the page shows the card only when the owner's last run stopped with
/// that fix, or while a download it started is running, never during a run that serves.
#[test]
fn the_shipped_page_offers_no_one_mac_chat_and_hides_the_model_card() {
    for gone in [
        "Run on qualified route",
        r#"id="infer-form""#,
        r#"id="prompt""#,
        "Available only after this Mac has a qualified assignment",
        r##"href="#inference""##,
    ] {
        assert!(
            !INDEX_HTML.contains(gone),
            "the shipped page still has `{gone}`"
        );
    }
    let open = INDEX_HTML
        .find(r#"<section id="inference""#)
        .expect("the model card (its download is the owner's model-prepare step)");
    let tag = &INDEX_HTML[open..open + INDEX_HTML[open..].find('>').unwrap()];
    assert!(
        tag.split_whitespace()
            .any(|attribute| attribute == "hidden"),
        "the card is shown by default: {tag}"
    );
    let close = open + INDEX_HTML[open..].find("</section>").unwrap();
    for (at, _) in INDEX_HTML.match_indices("Download / resume source") {
        assert!(
            open < at && at < close,
            "a download control outside the hidden card"
        );
    }
    let script = script_without_comments(APP_JS);
    let writers: Vec<&str> = script
        .lines()
        .filter(|l| l.contains("'inference'") || l.contains("\"inference\""))
        .collect();
    assert_eq!(
        writers.len(),
        1,
        "exactly one place decides the card's visibility: {writers:?}"
    );
    let rule = writers[0].replace(' ', "");
    assert!(
        rule.contains("$('inference').hidden=!(")
            && rule.contains("prepareModel")
            && rule.contains("downloading")
            && rule.contains("!target"),
        "the card is shown only to the owner, for the run's prepare_model fix or a running \
         download: {rule}"
    );
    let prepare = script
        .lines()
        .find(|l| l.trim_start().starts_with("const prepareModel="))
        .expect("app.js lost the prepare_model reveal");
    let prepare = prepare.replace(' ', "");
    assert!(
        prepare.contains("state==='failed'") && prepare.contains("action==='prepare_model'"),
        "{prepare}"
    );
}

/// Lane XB patch 2 (REVIEW XB-05, decisions D1/D2): the packaged window's page carries ONE
/// contained network section: the owner's per-run choice between "Macs on the same network"
/// (default) and "Macs on different networks (through the Mycelium relay)", and this Mac's
/// portmapping preference (default off), both saved through the packaged action
/// `/api/network/reach`, and the ACTIVE mode and a named error read back from
/// `status.network_reach`. No dialog gates either choice. The `api` crate's copy stays the
/// pinned v0.1.4 oracle asset (`crates/api/contracts/assets.json`): it never carries it.
#[test]
fn the_page_offers_the_network_choices_in_one_contained_section() {
    const MIRROR: &str = include_str!("../../api/assets/desktop/app.js");
    assert!(
        !MIRROR.contains("/api/network/reach"),
        "the api crate's oracle copy is unchanged"
    );
    for (name, page) in [("desktop", APP_JS)] {
        let begin = "// ---- Network reach (lane XB patch 2) ----";
        let end = "// ---- end network reach ----";
        assert_eq!(page.matches(begin).count(), 1, "{name}: one section");
        let whole = &page[page.find(begin).unwrap()..page.find(end).expect("closed section")];
        // The code, not its comments, must carry every piece.
        let section = &script_without_comments(whole);
        for needed in [
            "'/api/network/reach'",
            "{reach:",
            "{portmapping:",
            "Macs on the same network",
            "Macs on different networks (through the Mycelium relay)",
            "network_reach",
            "relay_unreachable",
            ".active",
        ] {
            assert!(
                section.contains(needed),
                "{name}: the section lacks {needed}"
            );
        }
        assert!(
            !script_without_comments(section).contains("window.confirm("),
            "{name}: no dialog gates a network choice"
        );
        assert!(
            !page[..page.find(begin).unwrap()].contains("/api/network/reach"),
            "{name}: nothing outside the section posts the action"
        );
    }
}
