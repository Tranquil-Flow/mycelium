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
//! - route status, assignments: the SHIPPED Mac app reads them from its deployment registry (it
//!   composes the product application through `build_registry_stack`, a13 serving.py:734 ->
//!   supervisor.py:2039-2064, :4370; there `route = registry`). With no running deployment
//!   `registry.product_assignment_records()` returns `()` (registry.py:943-946) and the projector
//!   publishes `assignment_source_missing` (projector.py:476-487). The registry's route status is a
//!   degraded `live_route_status.v1` built from a deployment record (registry.py:823-856) that this
//!   process does not have, so the route source is absent until a serving registry is composed here:
//!   `route_source_missing` (a v0.2-only code, ruled 11 Nov on REVIEW-lanePH2 F1; nothing invented).
//! - qualification: the serving root's qualification source (`Serving::qualification_record`), as
//!   `route_qualification_to_dict(qualification_source.current())` (supervisor.py:2166-2168): the
//!   development record has no `mycelium.route_qualification.v1` document (`missing`); a source
//!   that cannot be read (or a serving root that could not be built) publishes
//!   `qualification_source_failed`.
//! - recovery: none composed. The v0.1.4 Mac app builds its product application through
//!   `build_registry_stack` (supervisor.py:2060-2064, :4370), whose route (the deployment registry)
//!   has no `recovery_public_projection`, so its recovery source is `None` (supervisor.py:2148,
//!   :2202-2207) and the projector publishes `recovery_source_missing` (projector.py:510-516): the
//!   application's default here too.
use crate::{router_port::Serving, service::NodeService};
use api::{
    app_host::Authority,
    desktop::ProductRoutes,
    http,
    product_app::{PATHS, RESET_PATH, SourceError},
    product_routes::{Owner, OwnerSeed, Routes},
    schema::PublicError,
};
use desktop::{core::Reply, services::ServiceRequest};
use serde_json::{Map, Value};
use std::sync::{Arc, Mutex};

/// The packaged process as the product routes' owner.
struct PackagedOwner {
    /// The packaged host's authority (`None`: its state was refused at bind, no owner seed).
    authority: Option<Arc<Mutex<Authority>>>,
    node: NodeService,
    /// The serving root (its construction failure is kept as the services keep it).
    serving: Result<Arc<Serving>, PublicError>,
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
    /// The route source: no serving registry is composed in this process, so there is no route
    /// status to read (`route_source_missing`).
    fn route_status(&self) -> Result<Option<Value>, SourceError> {
        Ok(None)
    }
    /// `registry.product_assignment_records()` with no running deployment: `()`
    /// (`assignment_source_missing`).
    fn assignments(&self) -> Option<Result<Vec<Map<String, Value>>, SourceError>> {
        Some(Ok(Vec::new()))
    }
    /// `route_qualification_to_dict(qualification_source.current())` over the serving root's source.
    fn qualification(&self) -> Result<Option<Value>, SourceError> {
        let serving = self.serving.as_ref().map_err(|_| SourceError::Failed)?;
        serving
            .qualification_record()
            .map_err(|_| SourceError::Failed)
    }
}

/// The product routes of the packaged host over its authority, node service and serving root.
pub fn routes(
    authority: Option<Arc<Mutex<Authority>>>,
    node: NodeService,
    serving: Result<Arc<Serving>, PublicError>,
) -> Arc<dyn ProductRoutes> {
    Arc::new(Routes::new(Arc::new(PackagedOwner {
        authority,
        node,
        serving,
    })))
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
