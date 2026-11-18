// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's signed managed-control plane on its seed's HTTPS server, for a JOINED member
//! (`ManagedControlMailbox.handle_http` + `ManagedOfferMailbox.handle_http`,
//! `managed_control.py:1895-1910`, `managed_offers.py:343-358 @ f4e366e9`).
//!
//! Only the TARGET side of the plane is mounted here: poll, ACK and the staging offer reads/ACKs a
//! member sends. Owner commands and results stay on the owner's local carrier. The route is
//! transport only: the bound dispatcher authenticates every envelope against the CURRENT
//! membership and the mailbox verifies its signature, exactly as for the local carrier.
use seed::{
    Coordinator, SeedError,
    http::{RouteExtension, Ticket},
};
use serde_json::Value;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicUsize, Ordering},
};

/// `BoundedSemaphore(16)` (`tls.py:435 @ f4e366e9`): the managed-control requests the seed serves
/// at once. The next one is refused at once (`acquire(blocking=False)`), before it reaches the
/// mailbox, so a LAN or relay flood cannot queue up behind the mailbox's member guard.
pub const MAX_CONCURRENT_REQUESTS: usize = 16;

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
pub struct DispatchSlot {
    dispatch: Mutex<Option<Dispatch>>,
    in_flight: AtomicUsize,
}
/// One admitted managed-control request; dropping it frees the slot.
struct Admitted(Arc<DispatchSlot>);
impl Drop for Admitted {
    fn drop(&mut self) {
        self.0.in_flight.fetch_sub(1, Ordering::SeqCst);
    }
}
impl DispatchSlot {
    pub fn bind(&self, dispatch: Dispatch) {
        *self
            .dispatch
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner) = Some(dispatch);
    }
    pub fn unbind(&self) {
        self.dispatch
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
    }
    fn current(&self) -> Option<Dispatch> {
        self.dispatch
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .clone()
    }
    /// `slots.acquire(blocking=False)`: a slot, or `None` when all are taken.
    fn admit(self: &Arc<Self>) -> Option<Admitted> {
        self.in_flight
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| {
                (n < MAX_CONCURRENT_REQUESTS).then_some(n + 1)
            })
            .ok()
            .map(|_| Admitted(self.clone()))
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
        // The request's slot was taken by `admit`, before its body was read.
        Some((|| {
            // The path names the statement it carries; nothing else crosses this route.
            if body["statement"]["protocol"] != *protocol {
                return Err(SeedError::new("managed_control_route_unknown"));
            }
            let dispatch = self.0.current().ok_or_else(|| {
                eprintln!("diag: managed seat missing for {path}");
                SeedError::new("managed_owner_unavailable")
            })?;
            let raw = protocol::canonical::to_bytes(body)
                .map_err(|_| SeedError::new("managed_control_json_invalid"))?;
            let answer = dispatch(&raw).map_err(SeedError::new)?;
            protocol::strict::object(&answer, protocol::managed::MAX_FRAME_BYTES)
                .map_err(|_| SeedError::new("managed_control_response_invalid"))
        })())
    }
    /// v0.1.4 `CombinedSeedHandler.do_POST` (`tls.py:438-441`): the slot is taken first, before the
    /// header checks and the body read; the seed's error envelope carries the code and its status
    /// map answers 503 as v0.1.4 (ruling 16).
    fn admit(&self, path: &str) -> Result<Option<Ticket>, SeedError> {
        if !TARGET_ROUTES.iter().any(|(p, _)| *p == path) {
            return Ok(None);
        }
        match self.0.admit() {
            Some(admitted) => Ok(Some(Box::new(admitted))),
            None => {
                // Log-only probe (RV42 S1 relay-502 RCA): a slot refusal, by route.
                eprintln!(
                    "l11_managed_control_capacity_exhausted path={path} slots={MAX_CONCURRENT_REQUESTS} wall_ms={}",
                    seed::probe::wall_ms()
                );
                Err(SeedError::new("managed_control_capacity_exhausted"))
            }
        }
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
    fn admit(&self, path: &str) -> Result<Option<Ticket>, SeedError> {
        for routes in &self.0 {
            if let Some(ticket) = routes.admit(path)? {
                return Ok(Some(ticket));
            }
        }
        Ok(None)
    }
}
