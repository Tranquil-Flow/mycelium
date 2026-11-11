// SPDX-License-Identifier: AGPL-3.0-only
//! Keep L10's exact chat parsing/header boundary. This is a native invocation,
//! not a synthesized browser session, Origin, cookie, or relay identity.
use super::*;
use api::chat::{Chat as Boundary, ChatError, Service};
struct Backend<'a> {
    host: &'a DesktopServices,
    callback: Mutex<Option<Completion>>,
}
fn converted(e: PublicError) -> ChatError {
    ChatError {
        error: e,
        retry_after: None,
    }
}
impl Backend<'_> {
    fn value(&self, path: &str, body: Value) -> Result<Value, ChatError> {
        let reply = self
            .host
            .dispatch(ServiceRequest {
                method: "POST".into(),
                target: path.into(),
                headers: Vec::new(),
                body: serde_json::to_vec(&body)
                    .map_err(|_| converted(err("native_json_failed")))?,
            })
            .map_err(converted)?;
        serde_json::from_slice(&reply.body).map_err(|_| converted(err("native_json_failed")))
    }
}
impl Service for Backend<'_> {
    fn is_owner(&self) -> bool {
        true
    }
    fn state(&self) -> Result<Value, ChatError> {
        // With a run composed, the shipped `ChatService` answers (lanes PH3b S5, S5F).
        if let Some(chat) = self.host.routed_chat() {
            return Ok(chat.state());
        }
        self.value("/chat/api/state", json!({}))
    }
    fn send(&self, _: &str, body: Value, via_relay: bool) -> Result<Value, ChatError> {
        if via_relay {
            return Err(ChatError::new(403, "relay_not_supported"));
        }
        if let Some(chat) = self.host.routed_chat() {
            return self.host.route_send(chat, &body);
        }
        self.value("/chat/api/send", body)
    }
    fn events(&self, body: Value) -> Result<Value, ChatError> {
        // With a run composed, every record lives in the service (it answers at once,
        // chat.py:454-474).
        if let Some(chat) = self.host.routed_chat() {
            return chat.events(&body);
        }
        let bytes = serde_json::to_vec(&body).map_err(|_| converted(err("native_json_failed")))?;
        let callback = self
            .callback
            .lock()
            .map_err(|_| converted(err("desktop_poisoned")))?
            .take()
            .ok_or_else(|| converted(err("desktop_callback_consumed")))?;
        // The native completion executes while the stream guard remains held;
        // ACK follows completion, never the construction of an undelivered reply.
        self.host.events(
            ServiceRequest {
                method: "POST".into(),
                target: "/chat/api/events".into(),
                headers: Vec::new(),
                body: bytes,
            },
            callback,
        );
        Ok(Value::Null) // callback already consumed; no second response is sent.
    }
}
pub(super) fn handle(host: &DesktopServices, request: ServiceRequest, complete: Completion) {
    let bridge = Boundary::new(Backend {
        host,
        callback: Mutex::new(Some(complete)),
    });
    let result = bridge
        .handle(
            api::http::Request {
                method: request.method,
                target: request.target,
                headers: request.headers,
                body: request.body,
            },
            false,
        )
        .map(|r| Reply {
            status: r.status,
            content_type: "application/json",
            body: r.body,
        });
    match bridge.service().callback.lock() {
        Ok(mut callback) => {
            if let Some(callback) = callback.take() {
                DesktopServices::deliver(callback, result);
            }
        }
        Err(_) => eprintln!("desktop_callback_lock_poisoned"),
    }
}
