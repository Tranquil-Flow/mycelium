// SPDX-License-Identifier: AGPL-3.0-only
//! Existing assets, byte-for-byte from the immutable oracle; no frontend rewrite.
use crate::http::Reply;
fn reply(bytes: &[u8], kind: &str) -> Reply {
    Reply {
        status: 200,
        headers: vec![
            ("content-type".into(), kind.into()),
            ("cache-control".into(), "no-store".into()),
            ("referrer-policy".into(), "no-referrer".into()),
            ("x-content-type-options".into(), "nosniff".into()),
        ],
        body: bytes.into(),
    }
}
pub fn desktop(path: &str) -> Option<Reply> {
    let (bytes, kind): (&[u8], &str) = match path {
        "/" => (
            include_bytes!("../assets/desktop/index.html"),
            "text/html; charset=utf-8",
        ),
        "/app.js" => (
            include_bytes!("../assets/desktop/app.js"),
            "text/javascript; charset=utf-8",
        ),
        "/run.js" => (
            include_bytes!("../assets/desktop/run.js"),
            "text/javascript; charset=utf-8",
        ),
        "/swarm.js" => (
            include_bytes!("../assets/desktop/swarm.js"),
            "text/javascript; charset=utf-8",
        ),
        "/style.css" => (
            include_bytes!("../assets/desktop/style.css"),
            "text/css; charset=utf-8",
        ),
        _ => return None,
    };
    Some(reply(bytes, kind))
}
/// Caller must enforce the owner-only chat boundary before serving these assets.
pub fn chat(path: &str) -> Option<Reply> {
    let (bytes, kind): (&[u8], &str) = match path {
        "/chat" => (
            include_bytes!("../assets/desktop/chat.html"),
            "text/html; charset=utf-8",
        ),
        "/chat/chat.js" => (
            include_bytes!("../assets/desktop/chat.js"),
            "text/javascript; charset=utf-8",
        ),
        "/chat/chat.css" => (
            include_bytes!("../assets/desktop/chat.css"),
            "text/css; charset=utf-8",
        ),
        _ => return None,
    };
    Some(reply(bytes, kind))
}
pub fn directory(path: &str) -> Option<Reply> {
    let (bytes, kind): (&[u8], &str) = match path {
        "/swarms" | "/swarms/" => (
            include_bytes!("../assets/directory/index.html"),
            "text/html; charset=utf-8",
        ),
        "/swarms/swarms.js" => (
            include_bytes!("../assets/directory/swarms.js"),
            "text/javascript; charset=utf-8",
        ),
        "/swarms/swarms.css" => (
            include_bytes!("../assets/directory/swarms.css"),
            "text/css; charset=utf-8",
        ),
        _ => return None,
    };
    Some(reply(bytes, kind))
}
