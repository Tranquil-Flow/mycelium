// SPDX-License-Identifier: AGPL-3.0-only
//! Real SLICE-1 host and NodeService; controlled driver, no model/GPU claim.
#![cfg(feature = "service-composition")]
use api::{requests::Service as _, schema::PublicError};
use budgets::{BudgetId, Entries, Span};
use desktop::{
    channel::{Channel, ORIGIN, Request},
    core::Core,
    lifecycle::{Cleanup, CleanupError, Poll, QuitSource, Shutdown, State},
    services::{ServiceCleanup, adapt},
};
use node_service::{
    desktop_host::DesktopServices,
    service::{Driver, Job, NodeService},
};
use serde_json::{Value, json};
use std::{
    num::NonZeroUsize,
    sync::{Arc, Mutex, mpsc},
    time::{Duration, Instant},
};
fn budget() -> Span {
    BudgetId::StageCleanup.span(Entries::ZERO).unwrap()
}
fn wait() -> Duration {
    Duration::from_millis(
        BudgetId::WorkerStartup
            .span(Entries::ZERO)
            .unwrap()
            .millis(),
    )
}
fn request(path: &str, body: Value) -> Request {
    Request {
        window: "main".into(),
        method: "POST".into(),
        uri: format!("{ORIGIN}{path}"),
        headers: vec![
            ("Content-Type".into(), "application/json".into()),
            ("X-Mycelium-Chat".into(), "1".into()),
        ],
        body: serde_json::to_vec(&body).unwrap(),
    }
}
fn call(core: &impl Core, request: Request) -> desktop::core::Reply {
    Channel::default()
        .authorize_native_document(&request, "mycelium://localhost/chat")
        .unwrap();
    let (tx, rx) = mpsc::sync_channel(1);
    core.submit(request, Box::new(move |reply| tx.send(reply).unwrap()));
    rx.recv_timeout(wait()).unwrap()
}
struct NeverRun;
impl Driver for NeverRun {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        panic!("invalid request started a driver")
    }
}
/// A fresh private state root for the packaged action host (these chat paths never write it).
fn private() -> std::path::PathBuf {
    use std::os::unix::fs::DirBuilderExt;
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "node-shutdown-private-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    root
}
fn node(driver: Arc<dyn Driver>) -> NodeService {
    NodeService::new(
        driver,
        "isolated-test-token".into(),
        node_service::native::development_qualification().unwrap(),
    )
}
#[test]
fn real_node_idle_shutdown_fences_admission_and_drains_the_callback() {
    let node = node(Arc::new(NeverRun));
    let host = Arc::new(DesktopServices::new(
        node.clone(),
        NonZeroUsize::new(2).unwrap(),
        &private(),
    ));
    let (core, mut cleanup) = adapt(host, NonZeroUsize::new(2).unwrap());
    cleanup.bind_clock(Instant::now()).unwrap();
    let state = call(&*core, request("/chat/api/state", json!({})));
    assert_eq!(state.status, 200);
    assert_eq!(
        serde_json::from_slice::<Value>(&state.body).unwrap()["available"],
        true
    );
    assert_eq!(
        call(
            &*core,
            request("/chat/api/send", json!({"prompt":true,"max_new_tokens":1}))
        )
        .status,
        400
    );
    assert_eq!(
        call(
            &*core,
            request(
                "/chat/api/events",
                json!({"request_id":"missing","after":0})
            )
        )
        .status,
        404
    );
    cleanup.begin(budget()).unwrap();
    let deadline = Instant::now() + wait();
    while cleanup.poll().unwrap() == Poll::Pending {
        assert!(Instant::now() < deadline);
        std::thread::yield_now();
    }
    assert!(node.poll_shutdown().unwrap());
    assert_eq!(
        call(
            &*core,
            request(
                "/chat/api/send",
                json!({"prompt":"forbidden","max_new_tokens":1})
            )
        )
        .status,
        503
    );
    let input = json!({"protocol":"mycelium.request_gateway.v1","prompt":"forbidden","max_new_tokens":1,"qualification":node.qualification().unwrap()});
    match node.submit(input) {
        Err(error) => assert_eq!(error.code, "gateway_capacity_exhausted"),
        Ok(_) => panic!("shutdown admitted a new driver"),
    }
}

struct HeldDriver {
    entered: mpsc::SyncSender<()>,
    release: Mutex<mpsc::Receiver<()>>,
    cancelled: mpsc::SyncSender<bool>,
    fail: bool,
}
impl Driver for HeldDriver {
    fn run(&self, job: &mut Job) -> Result<(), PublicError> {
        self.entered.send(()).unwrap();
        self.release.lock().unwrap().recv_timeout(wait()).unwrap();
        self.cancelled.send(job.cancelled()).unwrap();
        if self.fail {
            Err(PublicError::new(503, "controlled_driver_cleanup_failure"))
        } else {
            Ok(())
        }
    }
}
struct Release(Option<mpsc::SyncSender<()>>);
impl Release {
    fn now(&mut self) {
        self.0.take().unwrap().send(()).unwrap();
    }
}
impl Drop for Release {
    fn drop(&mut self) {
        if let Some(tx) = self.0.take()
            && tx.send(()).is_err()
        {
            eprintln!("held_driver_already_exited");
        }
    }
}
type Quit = Shutdown<ServiceCleanup<DesktopServices>>;
/// Quit while a held driver runs: the caller is never blocked and nothing verifies before the
/// driver ends; the release reaches a cancelled driver. Returns the Quit's first result after it.
fn quit_during_held_run(fail: bool) -> (NodeService, Quit, State) {
    let (entered_tx, entered) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    let (cancelled_tx, cancelled) = mpsc::sync_channel(1);
    let mut release = Release(Some(release_tx));
    let node = node(Arc::new(HeldDriver {
        entered: entered_tx,
        release: Mutex::new(release_rx),
        cancelled: cancelled_tx,
        fail,
    }));
    let host = Arc::new(DesktopServices::new(
        node.clone(),
        NonZeroUsize::new(2).unwrap(),
        &private(),
    ));
    let (core, mut cleanup) = adapt(host, NonZeroUsize::new(2).unwrap());
    cleanup.bind_clock(Instant::now()).unwrap();
    assert_eq!(
        call(
            &*core,
            request(
                "/chat/api/send",
                json!({"prompt":"held driver","max_new_tokens":1})
            )
        )
        .status,
        200
    );
    entered.recv_timeout(wait()).unwrap();
    let mut shutdown = Shutdown::new(cleanup, budget()).unwrap();
    // A held driver cannot block the caller: no release is sent until these return.
    shutdown.request(QuitSource::WebUi, Span::ZERO);
    assert_eq!(shutdown.tick(Span::ZERO), State::Stopping);
    assert!(!shutdown.can_exit());
    assert_eq!(
        call(&*core, request("/chat/api/state", json!({}))).status,
        503
    );
    assert!(!node.poll_shutdown().unwrap());
    release.now();
    assert!(cancelled.recv_timeout(wait()).unwrap());
    let deadline = Instant::now() + wait();
    let state = loop {
        match shutdown.tick(Span::ZERO) {
            State::Stopping => {
                assert!(Instant::now() < deadline);
                std::thread::yield_now();
            }
            state => break state,
        }
    };
    (node, shutdown, state)
}
#[test]
fn active_real_node_cancellation_never_promotes_a_returned_driver_to_cleanup_proof() {
    let (node, shutdown, state) = quit_during_held_run(false);
    assert_eq!(state, State::Failed(CleanupError::Poll));
    assert!(!shutdown.can_exit());
    assert_eq!(node.poll_shutdown().unwrap_err().code, "cleanup_unproven");
}
#[test]
fn active_real_node_run_that_fails_under_quit_verifies_like_v014() {
    // FX-SLICE1-quit-after-failure (v0.1.4 f4e366e9, executed): Quit stops and joins an active run
    // without reading its outcome (`demo_app.py:199-203`), a failed run is not active
    // (`owner_run.py:48`), and the launcher exits 3 only on a close-step error (`__main__.py:219`).
    let (node, shutdown, state) = quit_during_held_run(true);
    assert_eq!(state, State::Verified);
    assert!(shutdown.can_exit());
    assert_eq!(shutdown.exit_status(), Some(0));
    assert!(node.poll_shutdown().unwrap());
}

#[test]
fn real_host_callback_must_return_before_shutdown_verifies() {
    let node = node(Arc::new(NeverRun));
    let host = Arc::new(DesktopServices::new(
        node.clone(),
        NonZeroUsize::new(2).unwrap(),
        &private(),
    ));
    let (core, mut cleanup) = adapt(host, NonZeroUsize::new(2).unwrap());
    cleanup.bind_clock(Instant::now()).unwrap();
    let (entered_tx, entered) = mpsc::sync_channel(1);
    let (release_tx, release_rx) = mpsc::sync_channel(1);
    let mut release = Release(Some(release_tx));
    core.submit(
        request("/chat/api/state", json!({})),
        Box::new(move |reply| {
            assert_eq!(reply.status, 200);
            entered_tx.send(()).unwrap();
            release_rx.recv_timeout(wait()).unwrap();
        }),
    );
    entered.recv_timeout(wait()).unwrap();
    cleanup.begin(budget()).unwrap();
    assert_eq!(cleanup.poll().unwrap(), Poll::Pending);
    release.now();
    let deadline = Instant::now() + wait();
    while cleanup.poll().unwrap() == Poll::Pending {
        assert!(Instant::now() < deadline);
        std::thread::yield_now();
    }
    assert!(node.poll_shutdown().unwrap());
}
