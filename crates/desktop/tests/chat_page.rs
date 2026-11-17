// SPDX-License-Identifier: AGPL-3.0-only
//! Lane CHATPAGE item 1 (owner, 16 Nov 21:58-22:15): "Open the chat on this Mac" navigates the
//! packaged window to `mycelium://localhost/chat`, which was v0.1.4's `chat.html` byte for byte:
//! the old look and NO way back to the main page, so the owner could neither Stop the run nor Quit
//! from the window. The window's chat page now wears the app's look (the main page's `style.css`
//! tokens and fonts), shows a visible "Back to Mycelium" link to the main page, and the run's Stop
//! (the same request the main page's Stop sends, run.js `act('/api/demo/stop', {})`).
//!
//! The owner controls live in a window-only script (`chat-owner.js`, served at `/chat/owner.js`);
//! the shared chat script (`chat.js`) is the one the public relay's chat page serves too (item 2),
//! so it must never reach an owner action: anonymous room members must not see owner controls,
//! and the server refuses an owner action through the relay whatever a page shows
//! (`node-service` `packaged_network`).
//!
//! Static checks over the shipped bytes; the behaviour of the controls in a stub DOM is recorded
//! by the lane's probe (`qualification/laneFLK/chatpage/`), and only WKWebView on a physical run
//! shows the look itself.
use std::path::Path;

fn asset(name: &str) -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("assets")
        .join(name);
    std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{name}: {e}"))
}

/// The opening tag of the first element carrying `id="<id>"` (attributes included).
fn tag_with_id<'a>(html: &'a str, id: &str) -> Option<&'a str> {
    let at = html.find(&format!("id=\"{id}\""))?;
    let start = html[..at].rfind('<')?;
    let end = at + html[at..].find('>')?;
    Some(&html[start..=end])
}

/// The element `<a ... href="/">...</a>` whose text is the visible "Back to Mycelium".
fn back_link(html: &str) -> Option<&str> {
    let text = html.find(">Back to Mycelium</a>")?;
    let start = html[..text].rfind("<a ")?;
    Some(&html[start..text + 1])
}

#[test]
fn the_window_chat_page_wears_the_apps_look() {
    let html = asset("chat.html");
    let css = asset("chat.css");
    // The main page's own stylesheet first (its tokens, the Fraunces face, the header and button
    // rules), then the chat's layout on top of it.
    let style = html
        .find(r#"<link rel="stylesheet" href="/style.css">"#)
        .expect("the window's chat page does not load the app's style.css");
    let chat = html
        .find(r#"<link rel="stylesheet" href="/chat/chat.css">"#)
        .expect("the chat layout stylesheet is gone");
    assert!(
        style < chat,
        "the chat layout must build on the app's tokens, not precede them"
    );
    assert!(
        html.contains(r#"<img class="mark" src="/mark.svg""#),
        "the app's header mark is missing from the chat page"
    );
    assert!(
        !html.contains('✳'),
        "the chat page still shows v0.1.4's asterisk brand"
    );
    // No palette of its own: v0.1.4's chat.css declared `:root{--bg:#101b19;--panel:#192923...}`.
    assert!(
        !css.contains(":root"),
        "chat.css declares its own palette instead of the app's tokens"
    );
    for old in ["#101b19", "#192923", "#edf2e9", "#b6e8ad", "#40564a"] {
        assert!(
            !css.contains(old),
            "chat.css still carries v0.1.4's colour {old}"
        );
    }
    assert!(
        css.contains("var(--"),
        "chat.css does not use the app's tokens"
    );
}

#[test]
fn the_window_chat_page_has_a_visible_way_back_to_mycelium() {
    let html = asset("chat.html");
    let back = back_link(&html).expect("the chat page has no \"Back to Mycelium\" link");
    assert!(
        back.contains(r#"href="/""#),
        "Back does not lead to the main page: {back}"
    );
    assert!(!back.contains("hidden"), "Back is hidden: {back}");
    assert!(
        !back.contains("target="),
        "Back must stay in the pinned window: {back}"
    );
    // `/` is a pinned destination of the window (navigation is refused anywhere else).
    assert!(desktop::channel::navigation_allowed(
        "mycelium://localhost/"
    ));
}

#[test]
fn the_window_chat_page_offers_the_runs_stop_with_the_main_pages_request() {
    let html = asset("chat.html");
    let stop = tag_with_id(&html, "run-stop").expect("the chat page has no Stop control");
    assert!(stop.starts_with("<button"), "{stop}");
    assert!(stop.contains(r#"type="button""#), "{stop}");
    assert!(
        !stop.contains("hidden"),
        "Stop is hidden by default: {stop}"
    );
    assert!(
        html.contains(">Stop the run</button>"),
        "the Stop control has no visible label"
    );
    assert!(
        html.contains(r#"<script defer src="/chat/owner.js"></script>"#),
        "the window's chat page does not load its owner script"
    );
    let owner = asset("chat-owner.js");
    let run = asset("run.js");
    // The main page's Stop: run.js `act('/api/demo/stop', {})` through its `api` (POST JSON with
    // `X-Mycelium-Action: 1`, the header the window's channel requires for an action).
    assert!(
        run.contains("act('/api/demo/stop', {})"),
        "run.js's Stop moved"
    );
    assert!(
        owner.contains("api('/api/demo/stop', {})"),
        "the chat page's Stop is not the main page's request"
    );
    let headers = "{'Content-Type':'application/json','X-Mycelium-Action':'1'}";
    assert!(
        run.contains(headers) && owner.contains(headers),
        "the action headers differ"
    );
    assert!(
        owner.contains("$('run-stop').addEventListener('click'"),
        "Stop is not wired"
    );
}

/// The shared chat script is the relay page's script too (item 2): it may only call the chat's
/// own routes, never an owner action, and it knows nothing of the window's controls.
#[test]
fn the_shared_chat_script_reaches_no_owner_action() {
    let chat = asset("chat.js");
    for line in chat.lines().filter(|l| l.contains("'/")) {
        for path in line.split('\'').filter(|s| s.starts_with('/')) {
            assert!(
                path.starts_with("/chat/api/"),
                "chat.js calls {path}, outside the chat's own routes"
            );
        }
    }
    for owner in [
        "run-stop",
        "/api/demo",
        "Back to Mycelium",
        "X-Mycelium-Action",
    ] {
        assert!(
            !chat.contains(owner),
            "chat.js reaches owner control {owner}"
        );
    }
}

/// The window serves every file the chat page names (the app's stylesheet, mark and font, the
/// chat's layout, the shared script and the window-only owner script), byte for byte; the shared
/// script is the one exported for the relay ([`desktop::page::CHAT_JS`]).
#[test]
fn the_window_serves_everything_its_chat_page_loads() {
    let html = asset("chat.html");
    let mut named = Vec::new();
    for attribute in ["href=\"", "src=\""] {
        for part in html.split(attribute).skip(1) {
            named.push(part[..part.find('"').unwrap()].to_owned());
        }
    }
    assert!(named.contains(&"/chat/owner.js".to_owned()), "{named:?}");
    for path in &named {
        assert!(
            desktop::page::window(path).is_some(),
            "the chat page names {path}, which the window does not serve"
        );
    }
    let style = asset("style.css");
    assert!(style.contains("url(\"/fraunces-latin-wght-normal.woff2\")"));
    assert!(desktop::page::window("/fraunces-latin-wght-normal.woff2").is_some());
    let served = |path: &str| desktop::page::window(path).unwrap().bytes;
    assert_eq!(served("/chat/owner.js"), asset("chat-owner.js").as_bytes());
    assert_eq!(served("/chat/chat.js"), asset("chat.js").as_bytes());
    assert_eq!(desktop::page::CHAT_JS, asset("chat.js").as_bytes());
    assert_eq!(served("/chat"), html.as_bytes());
}
