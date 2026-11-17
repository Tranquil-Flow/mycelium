// SPDX-License-Identifier: AGPL-3.0-only
//! The page assets the packaged window serves on `mycelium://localhost` (the native custom
//! protocol, `shell`), kept outside the Tauri feature so the table itself is testable and the
//! public relay can serve the SAME chat script (lane CHATPAGE item 2: one source of truth for the
//! chat page's script, so a room member reads the refusal messages the owner reads).
//!
//! The window's chat page (`/chat`, `chat.html`) is the app's own page: the main page's
//! `style.css`, a link back to `/` and the run's Stop, wired by the window-only `/chat/owner.js`.
//! The relay never serves `chat.html`, `chat.css` or `owner.js`; it serves only [`CHAT_JS`].

/// One asset: its content type and its bytes, as shipped.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Asset {
    pub content_type: &'static str,
    pub bytes: &'static [u8],
}

/// The chat page's script, byte for byte the one the window serves at `/chat/chat.js`. It calls
/// only the chat's own routes (`/chat/api/*`); the window's owner controls live in `owner.js`.
pub const CHAT_JS: &[u8] = include_bytes!("../assets/chat.js");

/// The window's asset at `path`, if it serves one.
pub fn window(path: &str) -> Option<Asset> {
    let (content_type, bytes): (&'static str, &'static [u8]) = match path {
        "/" => (
            "text/html; charset=utf-8",
            include_bytes!("../assets/index.html"),
        ),
        "/app.js" => ("text/javascript", include_bytes!("../assets/app.js")),
        "/run.js" => ("text/javascript", include_bytes!("../assets/run.js")),
        "/swarm.js" => ("text/javascript", include_bytes!("../assets/swarm.js")),
        "/style.css" => ("text/css", include_bytes!("../assets/style.css")),
        "/mark.svg" => ("image/svg+xml", include_bytes!("../assets/mark.svg")),
        "/mycelium-boot.js" => (
            "text/javascript",
            include_bytes!("../assets/mycelium-boot.js"),
        ),
        "/mycelium-geometry.js" => (
            "text/javascript",
            include_bytes!("../assets/mycelium-geometry.js"),
        ),
        "/mycelium-hero.js" => (
            "text/javascript",
            include_bytes!("../assets/mycelium-hero.js"),
        ),
        "/mycelium-field.js" => (
            "text/javascript",
            include_bytes!("../assets/mycelium-field.js"),
        ),
        "/fraunces-latin-wght-normal.woff2" => (
            "font/woff2",
            include_bytes!("../assets/fraunces-latin-wght-normal.woff2"),
        ),
        "/chat" => (
            "text/html; charset=utf-8",
            include_bytes!("../assets/chat.html"),
        ),
        "/chat/chat.js" => ("text/javascript", CHAT_JS),
        "/chat/chat.css" => ("text/css", include_bytes!("../assets/chat.css")),
        "/chat/owner.js" => ("text/javascript", include_bytes!("../assets/chat-owner.js")),
        _ => return None,
    };
    Some(Asset {
        content_type,
        bytes,
    })
}
