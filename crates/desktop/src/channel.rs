// SPDX-License-Identifier: AGPL-3.0-only
pub const ORIGIN: &str = "mycelium://localhost";
pub struct Request {
    pub window: String,
    pub method: String,
    pub uri: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ChannelError {
    Window,
    Origin,
    Path,
    Closed,
    Method,
    ContentType,
    Body,
    Action,
}
/// HTTP is never bound. The window label comes from Tauri's native protocol
/// context, NOT request headers or JS. Epoch revocation closes this capability.
#[derive(Default)]
pub struct Channel {
    closed: bool,
}
impl Channel {
    pub fn authorize(&self, request: &Request) -> Result<(), ChannelError> {
        self.authorize_with_native_document(request, false)
    }
    fn authorize_with_native_document(
        &self,
        request: &Request,
        native_document: bool,
    ) -> Result<(), ChannelError> {
        if self.closed {
            return Err(ChannelError::Closed);
        }
        if request.window != "main" {
            return Err(ChannelError::Window);
        }
        let path = request
            .uri
            .strip_prefix("mycelium://localhost/")
            .ok_or(ChannelError::Path)?;
        if path.contains(['%', '?', '#', '\\', ':'])
            || path.split('/').any(|s| s == ".." || s == ".")
            || path.starts_with('/')
        {
            return Err(ChannelError::Path);
        }
        if matches!(path, "session" | "native-session") {
            return Err(ChannelError::Path);
        }
        let header = |name: &str| {
            let mut values = request
                .headers
                .iter()
                .filter(|(key, _)| key.eq_ignore_ascii_case(name));
            match (values.next(), values.next()) {
                (Some((_, v)), None) => Some(v.as_str()),
                _ => None,
            }
        };
        // Any supplied Origin is exact, including GET; absent is allowed only
        // for same-window top-level/static GETs (WebKit omits it there).
        if request
            .headers
            .iter()
            .any(|(k, _)| k.eq_ignore_ascii_case("origin"))
            && header("origin") != Some(ORIGIN)
        {
            return Err(ChannelError::Origin);
        }
        match request.method.as_str() {
            "GET" if request.body.is_empty() => Ok(()),
            "POST" => {
                if header("origin") != Some(ORIGIN) && !native_document {
                    return Err(ChannelError::Origin);
                }
                // chat.py:562-567 @ f4e366e9: Chat (not Action) plus JSON
                // on exactly these routes. No prefix exception for other POSTs.
                let required = match path {
                    "chat/api/state" | "chat/api/send" | "chat/api/events" => "x-mycelium-chat",
                    _ => "x-mycelium-action",
                };
                if header(required) != Some("1") {
                    return Err(ChannelError::Action);
                }
                if header("content-type") != Some("application/json") {
                    return Err(ChannelError::ContentType);
                }
                // app.py:31 @ oracle; byte bound, not a timeout.
                if request.body.len() > 65_536 {
                    return Err(ChannelError::Body);
                }
                Ok(())
            }
            _ => Err(ChannelError::Method),
        }
    }
    /// Only the Tauri adapter may supply this independently observed native URL;
    /// never take it from a request header, JSON field, referrer or JS argument.
    /// Frames are disabled and navigation is pinned. Missing Origin is accepted
    /// only on that capability; supplied foreign/null/duplicate origins refuse.
    pub fn authorize_native_document(
        &self,
        request: &Request,
        document_url: &str,
    ) -> Result<(), ChannelError> {
        if !navigation_allowed(document_url) {
            return Err(ChannelError::Origin);
        }
        if request
            .headers
            .iter()
            .any(|(name, _)| name.eq_ignore_ascii_case("origin"))
        {
            // A supplied header is never excused by native provenance.
            return self.authorize(request);
        }
        self.authorize_with_native_document(request, true)
    }
    pub fn revoke(&mut self) {
        self.closed = true;
    }
}
pub fn navigation_allowed(uri: &str) -> bool {
    matches!(uri, "mycelium://localhost/" | "mycelium://localhost/chat")
}

/// No inline script, remote requests, frames, popups, forms, objects or workers.
/// The legacy bytes are served verbatim, CSP delivered as a response header.
pub const CSP: &str = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; frame-src 'none'; child-src 'none'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; worker-src 'none'";
