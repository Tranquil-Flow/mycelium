// SPDX-License-Identifier: AGPL-3.0-only
//! S12/S13: bounded native adapter, independent of browser session constructors.
use crate::{
    channel::Request,
    core::{Completion, Core, Reply},
    lifecycle::{Cleanup, CleanupError, Poll},
};
use budgets::Span;
use std::{
    num::NonZeroUsize,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize},
    },
    time::Instant,
};

/// Same owned field shape as api::http::Request, but a service invocation, NOT
/// a browser-authenticated HTTP request. Never invent cookie/Origin/Host values.
pub struct ServiceRequest {
    pub method: String,
    pub target: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}
/// Composition-root adapter for L10 service dispatch and L6/L9 cleanup ownership.
/// All methods MUST be nonblocking; enqueue onto the existing host executor.
/// `begin_shutdown` must synchronously fence admission, then enqueue cancellation,
/// drain/worker/reservation proof. It shares that fence with `submit` so a racing
/// submission cannot commit effects after shutdown. Desktop does not create a
/// second executor, poll thread, session credential or speculative receipt.
pub trait Services: Send + Sync + 'static {
    fn submit(&self, request: ServiceRequest, complete: Completion);
    fn begin_shutdown(&self, deadline: Instant) -> Result<(), CleanupError>;
    /// Verified means the REAL composed owner has proved resource cleanup, not
    /// that stop was queued, a worker exited, or a counter happened to be zero.
    fn poll_shutdown(&self) -> Result<Poll, CleanupError>;
}
struct Shared<S> {
    service: Arc<S>,
    capacity: NonZeroUsize,
    pending: AtomicUsize,
    closed: AtomicBool,
}
pub struct ServiceCore<S> {
    shared: Arc<Shared<S>>,
}
pub struct ServiceCleanup<S> {
    shared: Arc<Shared<S>>,
    origin: Option<Instant>,
    begun: bool,
}
pub fn adapt<S: Services>(
    service: Arc<S>,
    capacity: NonZeroUsize,
) -> (Arc<ServiceCore<S>>, ServiceCleanup<S>) {
    let shared = Arc::new(Shared {
        service,
        capacity,
        pending: AtomicUsize::new(0),
        closed: AtomicBool::new(false),
    });
    (
        Arc::new(ServiceCore {
            shared: shared.clone(),
        }),
        ServiceCleanup {
            shared,
            origin: None,
            begun: false,
        },
    )
}
impl<S: Services> Core for ServiceCore<S> {
    fn submit(&self, request: Request, complete: Completion) {
        use std::sync::atomic::Ordering;
        let shared = &self.shared;
        if shared.closed.load(Ordering::Acquire) {
            complete(Reply::error(503, "application_stopping"));
            return;
        }
        let Some(target) = request
            .uri
            .strip_prefix(crate::channel::ORIGIN)
            .filter(|path| path.starts_with('/'))
        else {
            complete(Reply::error(400, "native_service_target"));
            return;
        };
        if shared
            .pending
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |count| {
                if count < shared.capacity.get() {
                    Some(count + 1)
                } else {
                    None
                }
            })
            .is_err()
        {
            complete(Reply::error(503, "native_service_capacity"));
            return;
        }
        if shared.closed.load(Ordering::Acquire) {
            complete(Reply::error(503, "application_stopping"));
            shared.pending.fetch_sub(1, Ordering::AcqRel);
            return;
        }
        let request = ServiceRequest {
            method: request.method,
            target: target.to_owned(),
            headers: request.headers,
            body: request.body,
        };
        let finished = shared.clone();
        shared.service.submit(
            request,
            Box::new(move |reply| {
                // Refuse a completion that observes local revocation. The
                // host's shared submit/shutdown fence remains effect authority.
                let reply = if finished.closed.load(Ordering::Acquire) {
                    Reply::error(503, "application_stopping")
                } else {
                    reply
                };
                complete(reply);
                // Only an actually completed callback releases its slot. A dropped
                // or panicking callback intentionally leaves cleanup unproven.
                finished.pending.fetch_sub(1, Ordering::AcqRel);
            }),
        );
    }
}
impl<S: Services> Cleanup for ServiceCleanup<S> {
    fn bind_clock(&mut self, origin: Instant) -> Result<(), CleanupError> {
        if self.origin.is_some()
            || self
                .shared
                .closed
                .load(std::sync::atomic::Ordering::Acquire)
        {
            return Err(CleanupError::Clock);
        }
        self.origin = Some(origin);
        Ok(())
    }
    fn begin(&mut self, deadline: Span) -> Result<(), CleanupError> {
        if self
            .shared
            .closed
            .swap(true, std::sync::atomic::Ordering::AcqRel)
        {
            return Err(CleanupError::Begin);
        }
        let deadline = self
            .origin
            .ok_or(CleanupError::Clock)?
            .checked_add(std::time::Duration::from_millis(deadline.millis()))
            .ok_or(CleanupError::Clock)?;
        self.shared.service.begin_shutdown(deadline)?;
        self.begun = true;
        Ok(())
    }
    fn poll(&mut self) -> Result<Poll, CleanupError> {
        if !self.begun {
            return Err(CleanupError::Poll);
        }
        match self.shared.service.poll_shutdown()? {
            Poll::Verified
                if self
                    .shared
                    .pending
                    .load(std::sync::atomic::Ordering::Acquire)
                    == 0 =>
            {
                Ok(Poll::Verified)
            }
            _ => Ok(Poll::Pending),
        }
    }
}
