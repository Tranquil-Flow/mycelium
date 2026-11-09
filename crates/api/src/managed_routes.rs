// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's signed managed-control plane on its seed's HTTPS server, for a JOINED member
//! (`ManagedControlMailbox.handle_http` + `ManagedOfferMailbox.handle_http`,
//! `managed_control.py:1895-1910`, `managed_offers.py:343-358 @ f4e366e9`).
//!
//! Only the TARGET side of the plane is mounted here: poll, ACK and the staging offer reads/ACKs a
//! member sends. Owner commands and results stay on the owner's local carrier. The route is
//! transport only: the bound dispatcher authenticates every envelope against the CURRENT
//! membership and the mailbox verifies its signature, exactly as for the local carrier.
use seed::{Coordinator, SeedError, http::RouteExtension};
use serde_json::Value;
use std::sync::{Arc, Mutex};

/// `(path, statement protocol)` of every target-side request the seed accepts.
pub const TARGET_ROUTES: [(&str, &str); 6] = [
    (
        "/managed-control/poll",
        "mycelium.managed_control.target_poll.v1",
    ),
    (
        "/managed-control/ack",
        "mycelium.managed_control.target_ack.v1",
    ),
    (
        "/managed-control/staging/read",
        "mycelium.managed_stage_read.v1",
    ),
    (
        "/managed-control/staging/ack",
        "mycelium.managed_stage_ack.v1",
    ),
    (
        "/managed-control/staging/cleanup/read",
        "mycelium.managed_stage_cleanup_read.v1",
    ),
    (
        "/managed-control/staging/cleanup/ack",
        "mycelium.managed_stage_cleanup_ack.v1",
    ),
];

/// The route a member POSTs a signed target-side statement to; `None` for any other protocol.
pub fn path_for(statement_protocol: &str) -> Option<&'static str> {
    TARGET_ROUTES
        .iter()
        .find(|(_, protocol)| *protocol == statement_protocol)
        .map(|(path, _)| *path)
}

/// The owner's mailbox dispatcher: the exact canonical envelope bytes in, the signed answer out.
pub type Dispatch = Arc<dyn Fn(&[u8]) -> Result<Vec<u8>, &'static str> + Send + Sync>;

/// Where the owner binds its mailbox dispatcher once its managed owner exists (and unbinds it on
/// close). Unbound, every managed route answers `managed_owner_unavailable`.
#[derive(Default)]
pub struct DispatchSlot(Mutex<Option<Dispatch>>);
impl DispatchSlot {
    pub fn bind(&self, dispatch: Dispatch) {
        *self
            .0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(dispatch);
    }
    pub fn unbind(&self) {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
    }
    fn current(&self) -> Option<Dispatch> {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }
}

/// The target-side managed routes over a [`DispatchSlot`].
pub struct MailboxRoutes(pub Arc<DispatchSlot>);
impl RouteExtension for MailboxRoutes {
    fn post(
        &self,
        _coordinator: &Coordinator,
        path: &str,
        body: &Value,
    ) -> Option<Result<Value, SeedError>> {
        let (_, protocol) = TARGET_ROUTES.iter().find(|(p, _)| *p == path)?;
        Some((|| {
            // The path names the statement it carries; nothing else crosses this route.
            if body["statement"]["protocol"] != *protocol {
                return Err(SeedError::new("managed_control_route_unknown"));
            }
            let dispatch = self
                .0
                .current()
                .ok_or_else(|| SeedError::new("managed_owner_unavailable"))?;
            let raw = protocol::canonical::to_bytes(body)
                .map_err(|_| SeedError::new("managed_control_json_invalid"))?;
            let answer = dispatch(&raw).map_err(SeedError::new)?;
            protocol::strict::object(&answer, protocol::managed::MAX_FRAME_BYTES)
                .map_err(|_| SeedError::new("managed_control_response_invalid"))
        })())
    }
}

/// Several route extensions on one seed server, tried in order.
pub struct Routes(pub Vec<Arc<dyn RouteExtension>>);
impl RouteExtension for Routes {
    fn post(
        &self,
        coordinator: &Coordinator,
        path: &str,
        body: &Value,
    ) -> Option<Result<Value, SeedError>> {
        self.0
            .iter()
            .find_map(|routes| routes.post(coordinator, path, body))
    }
}
