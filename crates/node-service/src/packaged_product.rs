// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged executable's product evidence routes (PH2): v0.1.4's product gateway
//! (`GET /api/v1/product/{snapshot,export,events}`, `mycelium_ui_gateway/app.py:348-352, 591-670`
//! @ f4e366e9) and B13f's owner reset (`POST /api/v1/product/reset`), served by the same
//! `api::product_routes::Routes` the headless launcher composes, over this process's owner seed
//! (the packaged host's authority). The application exists only while the owner's seed runs; its
//! window lives under `<state>/serving/product`, bound to the swarm id.
//!
//! The routes sit behind the packaged window channel (`desktop::channel::Channel`): the window label
//! is the session, so a product GET takes exactly the rules of `/api/status` and the reset those of
//! every owner action (exact Origin, `X-Mycelium-Action`, JSON). The window's reply carries status,
//! type and body only (the shell adds `cache-control: no-store` and `nosniff`), so the gateway's
//! `Allow: GET` on a 405 is not sent (class D).
//!
//! Sources (`ProductEvidenceApplication`, `app.py:206-281`), as v0.1.4 publishes them for what this
//! process really has:
//! - members: the owner's seed (shared with the headless launcher).
//! - route status: read through the live route (`_follow`, supervisor.py:2153-2164). This process
//!   never opens a qualified live route (its serving status is `route_ready: false`,
//!   `development_unqualified`; the native slice has no `mycelium.live_route_status.v1` producer), so
//!   the read is v0.1.4's `RouteUnavailableError('route_not_open')`, which `_follow` does not type:
//!   `route_source_failed`. Only a cold-restart code (`ColdRouteRestartError`) would publish its own
//!   reason; this process has no cold restart.
use crate::service::NodeService;
use api::{
    app_host::Authority,
    desktop::ProductRoutes,
    http,
    product_app::{PATHS, RESET_PATH, SourceError},
    product_routes::{Owner, OwnerSeed, Routes},
    schema::PublicError,
};
use desktop::{core::Reply, services::ServiceRequest};
use serde_json::Value;
use std::sync::{Arc, Mutex};

/// The packaged process as the product routes' owner.
struct PackagedOwner {
    /// The packaged host's authority (`None`: its state was refused at bind, no owner seed).
    authority: Option<Arc<Mutex<Authority>>>,
    node: NodeService,
}
impl Owner for PackagedOwner {
    /// The running owner seed, `None` while there is none, for a target, or once Quit fenced the
    /// node service (a poisoned node counts as closed). The authority's lock is held only to clone.
    fn seed(&self) -> Option<OwnerSeed> {
        if self.node.closed().unwrap_or(true) {
            return None;
        }
        let authority = self.authority.as_ref()?.lock().ok()?;
        if authority.target {
            return None;
        }
        Some(OwnerSeed {
            coordinator: authority.seed.as_ref()?.coordinator.clone(),
            root: authority.root.clone(),
        })
    }
    /// `route_source = _follow('public_status')`: no qualified live route in this process.
    fn route_status(&self) -> Result<Option<Value>, SourceError> {
        Err(route_not_open())
    }
}

/// v0.1.4's read of a route that is not open (`RouteUnavailableError('route_not_open')`, supervisor.py:
/// 590-600): `_follow` re-raises it untyped, so the source publishes its generic `<source>_failed`.
fn route_not_open() -> SourceError {
    SourceError::Failed
}

/// The product routes of the packaged host over its authority and node service.
pub fn routes(
    authority: Option<Arc<Mutex<Authority>>>,
    node: NodeService,
) -> Arc<dyn ProductRoutes> {
    Arc::new(Routes::new(Arc::new(PackagedOwner { authority, node })))
}

/// The window's reply for a product answer: its status, type and body.
fn window_reply(reply: &http::Reply) -> Result<Reply, PublicError> {
    let content_type = match reply.header("content-type") {
        Some("application/json") => "application/json",
        Some("text/event-stream; charset=utf-8") => "text/event-stream; charset=utf-8",
        _ => return Err(PublicError::new(503, "product_reply_unsupported")),
    };
    Ok(Reply {
        status: reply.status,
        content_type,
        body: reply.body.clone(),
    })
}

/// The product answer to `request` (its JSON `body` already parsed), `None` when it is no product
/// route. A product path answers whatever the method (the gateway's own `405`); the reset is the
/// owner action `POST /api/v1/product/reset` with a JSON object body.
pub fn dispatch(
    product: &dyn ProductRoutes,
    request: &ServiceRequest,
    body: &Value,
) -> Option<Result<Reply, PublicError>> {
    if PATHS.contains(&request.target.as_str()) {
        let request = http::Request {
            method: request.method.clone(),
            target: request.target.clone(),
            headers: request.headers.clone(),
            body: request.body.clone(),
        };
        return Some(product.get(&request).and_then(|reply| window_reply(&reply)));
    }
    if request.method == "POST" && request.target == RESET_PATH {
        let Value::Object(body) = body else {
            return Some(Err(PublicError::new(400, "invalid_request")));
        };
        return Some(product.reset(body).and_then(|reply| window_reply(&reply)));
    }
    None
}
