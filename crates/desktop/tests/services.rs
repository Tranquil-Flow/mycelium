// SPDX-License-Identifier: AGPL-3.0-only
use budgets::{BudgetId, Entries};
use desktop::{
    channel::Request,
    core::{Completion, Core, Reply},
    lifecycle::{Cleanup, CleanupError, Poll},
    services::{ServiceRequest, Services, adapt},
};
use std::{
    num::NonZeroUsize,
    sync::{Arc, Mutex, mpsc},
    time::{Duration, Instant},
};
#[derive(Default)]
struct Host {
    requests: Mutex<Vec<ServiceRequest>>,
    callbacks: Mutex<Vec<Completion>>,
    deadlines: Mutex<Vec<Instant>>,
    failure: Mutex<Option<CleanupError>>,
}
impl Services for Host {
    fn submit(&self, request: ServiceRequest, complete: Completion) {
        self.requests.lock().unwrap().push(request);
        self.callbacks.lock().unwrap().push(complete);
    }
    fn begin_shutdown(&self, deadline: Instant) -> Result<(), CleanupError> {
        self.deadlines.lock().unwrap().push(deadline);
        self.failure.lock().unwrap().map_or(Ok(()), Err)
    }
    fn poll_shutdown(&self) -> Result<Poll, CleanupError> {
        self.failure.lock().unwrap().map_or(Ok(Poll::Verified), Err)
    }
}
fn request() -> Request {
    Request {
        window: "main".into(),
        method: "POST".into(),
        uri: "mycelium://localhost/api/serving/stop".into(),
        headers: vec![
            ("x-mycelium-action".into(), "1".into()),
            ("content-type".into(), "application/json".into()),
        ],
        body: b"{}".to_vec(),
    }
}
fn callback() -> (Completion, mpsc::Receiver<Reply>) {
    let (tx, rx) = mpsc::channel();
    (
        Box::new(move |reply| {
            tx.send(reply).unwrap();
        }),
        rx,
    )
}
fn bound() -> budgets::Span {
    BudgetId::RouterClose.span(Entries::ZERO).unwrap()
}
#[test]
fn service_request_translation_is_exact_and_never_synthesizes_browser_credentials() {
    let host = Arc::new(Host::default());
    let (core, _) = adapt(host.clone(), NonZeroUsize::new(1).unwrap());
    let (done, rx) = callback();
    core.submit(request(), done);
    let requests = host.requests.lock().unwrap();
    assert_eq!(requests.len(), 1);
    let r = &requests[0];
    assert_eq!(r.method, "POST");
    assert_eq!(r.target, "/api/serving/stop");
    assert_eq!(r.headers, request().headers);
    assert_eq!(r.body, b"{}");
    assert!(rx.try_recv().is_err());
    host.callbacks.lock().unwrap().pop().unwrap()(Reply {
        status: 409,
        content_type: "application/json",
        body: b"{\"error\":\"cleanup_unproven\"}".to_vec(),
    });
    let reply = rx.try_recv().unwrap();
    assert_eq!(reply.status, 409);
    assert_eq!(reply.body, b"{\"error\":\"cleanup_unproven\"}");
}
#[test]
fn capacity_and_shutdown_fence_bound_pending_callbacks_and_suppress_late_success() {
    let host = Arc::new(Host::default());
    let (core, mut cleanup) = adapt(host.clone(), NonZeroUsize::new(1).unwrap());
    let origin = Instant::now();
    cleanup.bind_clock(origin).unwrap();
    let (done, first) = callback();
    core.submit(request(), done);
    let (done, full) = callback();
    core.submit(request(), done);
    assert_eq!(
        full.try_recv().unwrap().body,
        b"{\"error\":\"native_service_capacity\"}"
    );
    assert_eq!(host.requests.lock().unwrap().len(), 1);
    cleanup.begin(bound()).unwrap();
    assert_eq!(
        *host.deadlines.lock().unwrap(),
        vec![origin + Duration::from_millis(bound().millis())]
    );
    assert_eq!(
        cleanup.poll(),
        Ok(Poll::Pending),
        "host proof alone cannot abandon a callback"
    );
    let (done, closed) = callback();
    core.submit(request(), done);
    assert_eq!(
        closed.try_recv().unwrap().body,
        b"{\"error\":\"application_stopping\"}"
    );
    assert_eq!(host.requests.lock().unwrap().len(), 1);
    host.callbacks.lock().unwrap().pop().unwrap()(Reply {
        status: 200,
        content_type: "application/json",
        body: b"{}".to_vec(),
    });
    assert_eq!(first.try_recv().unwrap().status, 503);
    assert_eq!(cleanup.poll(), Ok(Poll::Verified));
    assert_eq!(cleanup.begin(bound()), Err(CleanupError::Begin));
    assert_eq!(host.deadlines.lock().unwrap().len(), 1);
}
#[test]
fn missing_clock_and_host_failure_never_manufacture_cleanup_proof() {
    let host = Arc::new(Host::default());
    let (_, mut cleanup) = adapt(host.clone(), NonZeroUsize::new(1).unwrap());
    assert_eq!(cleanup.begin(bound()), Err(CleanupError::Clock));
    assert!(host.deadlines.lock().unwrap().is_empty());
    let (_, mut cleanup) = adapt(host.clone(), NonZeroUsize::new(1).unwrap());
    assert_eq!(cleanup.poll(), Err(CleanupError::Poll));
    cleanup.bind_clock(Instant::now()).unwrap();
    *host.failure.lock().unwrap() = Some(CleanupError::Begin);
    assert_eq!(cleanup.begin(bound()), Err(CleanupError::Begin));
    *host.failure.lock().unwrap() = Some(CleanupError::Poll);
    assert_eq!(cleanup.poll(), Err(CleanupError::Poll));
}
#[test]
fn dropped_host_callback_is_not_counted_as_completed_cleanup() {
    let host = Arc::new(Host::default());
    let (core, mut cleanup) = adapt(host.clone(), NonZeroUsize::new(1).unwrap());
    cleanup.bind_clock(Instant::now()).unwrap();
    let (done, _rx) = callback();
    core.submit(request(), done);
    host.callbacks.lock().unwrap().clear();
    cleanup.begin(bound()).unwrap();
    assert_eq!(cleanup.poll(), Ok(Poll::Pending));
}
#[test]
fn callback_delivery_itself_is_part_of_the_drain_not_just_host_completion() {
    let host = Arc::new(Host::default());
    let (core, mut cleanup) = adapt(host.clone(), NonZeroUsize::new(1).unwrap());
    cleanup.bind_clock(Instant::now()).unwrap();
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    core.submit(
        request(),
        Box::new(move |_| {
            entered_tx.send(()).unwrap();
            release_rx
                .recv_timeout(Duration::from_millis(bound().millis()))
                .unwrap();
        }),
    );
    let done = host.callbacks.lock().unwrap().pop().unwrap();
    let worker = std::thread::spawn(move || done(Reply::unavailable()));
    entered_rx
        .recv_timeout(Duration::from_millis(bound().millis()))
        .unwrap();
    cleanup.begin(bound()).unwrap();
    assert_eq!(cleanup.poll(), Ok(Poll::Pending));
    release_tx.send(()).unwrap();
    worker.join().unwrap();
    assert_eq!(cleanup.poll(), Ok(Poll::Verified));
}
#[test]
fn concurrent_admission_never_exceeds_injected_capacity() {
    let host = Arc::new(Host::default());
    let (core, _) = adapt(host.clone(), NonZeroUsize::new(2).unwrap());
    let mut threads = Vec::new();
    for _ in 0..8 {
        let core = core.clone();
        threads.push(std::thread::spawn(move || {
            let (done, rx) = callback();
            core.submit(request(), done);
            rx
        }));
    }
    let receivers: Vec<_> = threads.into_iter().map(|t| t.join().unwrap()).collect();
    assert_eq!(host.requests.lock().unwrap().len(), 2);
    assert_eq!(
        receivers.iter().filter(|rx| rx.try_recv().is_ok()).count(),
        6
    );
    for done in host.callbacks.lock().unwrap().drain(..) {
        done(Reply::unavailable());
    }
}
#[test]
fn clock_rebinding_and_foreign_service_targets_refuse_without_host_dispatch() {
    let host = Arc::new(Host::default());
    let (core, mut cleanup) = adapt(host.clone(), NonZeroUsize::new(1).unwrap());
    cleanup.bind_clock(Instant::now()).unwrap();
    assert_eq!(cleanup.bind_clock(Instant::now()), Err(CleanupError::Clock));
    let mut foreign = request();
    foreign.uri = "https://example.org/api/quit".into();
    let (done, rx) = callback();
    core.submit(foreign, done);
    assert_eq!(rx.try_recv().unwrap().status, 400);
    assert!(host.requests.lock().unwrap().is_empty());
}
#[test]
fn dropped_callback_expires_in_the_actual_shutdown_machine_without_permission_to_exit() {
    use desktop::lifecycle::{QuitSource, Shutdown, State};
    let host = Arc::new(Host::default());
    let (core, mut cleanup) = adapt(host.clone(), NonZeroUsize::new(1).unwrap());
    cleanup.bind_clock(Instant::now()).unwrap();
    let (done, _rx) = callback();
    core.submit(request(), done);
    host.callbacks.lock().unwrap().clear();
    let mut shutdown = Shutdown::new(cleanup, bound()).unwrap();
    shutdown.request(QuitSource::WebUi, budgets::Span::ZERO);
    assert_eq!(shutdown.tick(budgets::Span::ZERO), State::Stopping);
    assert_eq!(
        shutdown.tick(bound()),
        State::Failed(CleanupError::Deadline)
    );
    assert!(!shutdown.can_exit());
}
