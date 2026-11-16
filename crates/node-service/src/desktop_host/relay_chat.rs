// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's chat behind the public relay (lane CHATS item 2). v0.1.4 @ f4e366e9 binds it in
//! `_bind_public_routes` (app.py:1070-1077): `proxy.bind_chat(lambda method, path, headers, body:
//! chat.handle(self, method, path, headers, body, via_relay=True))`, so the room's `share_url`
//! (`chat.chat_status`, chat.py:521-529) reaches the SAME chat service as the owner's page:
//! `chat.handle` (chat.py:553-584) with the relay's client key (`client_key(headers,
//! via_relay=True)`, chat.py:532-550: `relay:<Cf-Connecting-Ip>`, an IPv6 address by its /64).
//!
//! Here that is the shipped boundary `api::chat::Chat` (`handle(r, via_relay=true)`: owner only,
//! the page assets, the `X-Mycelium-Chat` / JSON header check, the 16 KiB body bound, the client
//! key) over the run's route `ChatService`, which answers `chat_not_ready` / `chat_warming_up` /
//! `chat_switching_runs` itself while the run does not serve. The relay keeps its own framing,
//! header and size limits (`api::relay` `local`), and drops `Cf-Connecting-Ip` unless the edge is
//! trusted. v0.1.4's `_send` (chat.py:587-610) also passes a relay prompt through the published
//! swarm entry's guard (`admit_relay_chat`); the packaged host publishes no swarm gateway, so that
//! guard is absent, as in v0.1.4 when no swarm is published (`guard is None`).
use super::*;
use api::{
    chat::{Chat as Boundary, ChatError, Service},
    chat_service::ChatService,
    relay::{RichAnswer, RichHandler},
};

struct RelayChat {
    chat: ChatService,
    gate: Arc<Mutex<Gate>>,
    authority: Option<Arc<Mutex<crate::packaged_run::Authority>>>,
}
impl Service for RelayChat {
    /// `app.installation_role != 'owner'` -> 404 (chat.py:556-557): a joined Mac, or a host whose
    /// authority was refused at bind, is not the owner.
    fn is_owner(&self) -> bool {
        self.authority
            .as_ref()
            .is_some_and(|a| a.lock().is_ok_and(|a| !a.target))
    }
    fn state(&self) -> Result<Value, ChatError> {
        Ok(self.chat.state())
    }
    fn send(&self, client: &str, body: Value, _: bool) -> Result<Value, ChatError> {
        send_routed(&self.gate, &self.chat, client, &body)
    }
    fn events(&self, body: Value) -> Result<Value, ChatError> {
        self.chat.events(&body)
    }
}

/// The relay's chat handler over the run's chat service `chat` (what `Proxy::bind_chat` calls).
pub(super) fn handler(
    chat: ChatService,
    gate: Arc<Mutex<Gate>>,
    authority: Option<Arc<Mutex<crate::packaged_run::Authority>>>,
) -> RichHandler {
    let boundary = Arc::new(Boundary::new(RelayChat {
        chat,
        gate,
        authority,
    }));
    Arc::new(move |method, path, headers, body| {
        let reply = boundary
            .handle(
                api::http::Request {
                    method: method.to_owned(),
                    target: path.to_owned(),
                    headers: headers.to_vec(),
                    body: body.to_vec(),
                },
                true,
            )
            .map_err(|_| ())?;
        let kind = reply
            .headers
            .iter()
            .find(|(name, _)| name.eq_ignore_ascii_case("content-type"))
            .map(|(_, value)| value.clone())
            .ok_or(())?;
        Ok(RichAnswer {
            status: i64::from(reply.status),
            kind,
            raw: reply.body,
        })
    })
}
