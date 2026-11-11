// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged executable's swarm listing custody (`DemoApp.swarm_publisher`, v0.1.4
//! `swarm_publish.py` @ f4e366e9, here `api::swarm::publisher::app::AppPublisher`, as the headless
//! launcher composes it): explicit opt-in only (`/api/swarm/publish`, `/api/swarm/unpublish`), its
//! state in `swarm-publisher.sqlite3` beside the owner's private state, nothing written and no
//! network while the owner never opted in, and Quit's `swarm_publish.close` (`close(now, 5 s)`,
//! `thread.join(5)` at swarm_publish.py:453) whose failure is reported after the host's own
//! cleanup ran (the download is still closed).
//!
//! The directory is a loopback socket owned by the test that counts connections and never
//! answers (a slow directory): no test reaches the real directory.
#![cfg(feature = "desktop-shell")]
use api::resources::{Observation, Observer};
use budgets::{BudgetId as B, Entries};
use desktop::{
    channel::{ORIGIN, Request},
    core::Core,
    lifecycle::{Cleanup, CleanupError, Poll},
};
use node_service::packaged::{self, AppCleanup};
use serde_json::{Value, json};
use std::{
    net::TcpListener,
    os::unix::fs::{DirBuilderExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};

const DB_FILE: &str = api::swarm::publisher::app::DB_FILE;
const SOURCE: &str = "model-source-7ae557604adf";

fn millis(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn state_root() -> PathBuf {
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "packaged-publisher-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    root
}
fn private(state: &Path) -> PathBuf {
    state.join("v02-node/private")
}
/// This Mac as the window's consent permits it (no download runs in these tests).
struct Permissive;
impl Observer for Permissive {
    fn observe(&self) -> Observation {
        Observation {
            power: "external".into(),
            thermal: "nominal".into(),
            network: "unmetered".into(),
        }
    }
}
/// A loopback directory that accepts, counts and holds every connection without answering.
struct SilentDirectory {
    origin: String,
    accepted: Arc<AtomicUsize>,
    stop: Arc<AtomicBool>,
    server: Option<JoinHandle<()>>,
}
impl SilentDirectory {
    fn start() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let origin = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
        let (accepted, stop) = (Arc::new(AtomicUsize::new(0)), Arc::new(AtomicBool::new(false)));
        let (counting, stopping) = (accepted.clone(), stop.clone());
        let server = std::thread::spawn(move || {
            let mut held = Vec::new();
            while !stopping.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        counting.fetch_add(1, Ordering::SeqCst);
                        held.push(stream);
                    }
                    Err(_) => std::thread::sleep(millis(B::OwnerPoll) / 50),
                }
            }
        });
        Self {
            origin,
            accepted,
            stop,
            server: Some(server),
        }
    }
    fn accepted(&self) -> usize {
        self.accepted.load(Ordering::SeqCst)
    }
}
impl Drop for SilentDirectory {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(server) = self.server.take() {
            server.join().unwrap();
        }
    }
}
fn packaged_core(state: &Path) -> (Arc<dyn Core>, AppCleanup) {
    let (core, mut cleanup) = packaged::core_with(
        state,
        Arc::new(api::acquisition::HttpTransport::hugging_face(true)),
        Arc::new(Permissive),
    )
    .unwrap();
    cleanup.bind_clock(Instant::now()).unwrap();
    (core, cleanup)
}
fn call(core: &Arc<dyn Core>, path: &str, body: Value) -> (u16, Value) {
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    core.submit(
        Request {
            window: "main".into(),
            method: "POST".into(),
            uri: format!("{ORIGIN}{path}"),
            headers: vec![
                ("x-mycelium-action".into(), "1".into()),
                ("content-type".into(), "application/json".into()),
            ],
            body: serde_json::to_vec(&body).unwrap(),
        },
        Box::new(move |reply| tx.send(reply).unwrap()),
    );
    let reply = rx.recv_timeout(millis(B::WorkerStartup)).unwrap();
    (reply.status, serde_json::from_slice(&reply.body).unwrap())
}
fn create(core: &Arc<dyn Core>) {
    let consent = json!({
        "peer_visibility": true, "power": "ac_only",
        "storage_bytes": api::model_catalog::storage_consent_ceiling(),
        "network": "unmetered_only", "background": false, "diagnostics": false,
        "thermal": "stop_on_serious",
    });
    let created = call(core, "/api/create", json!({"consent": consent}));
    assert_eq!(created.0, 200, "create: {}", created.1);
}
/// Quit to its result, and how long it took from its begin.
fn quit(cleanup: &mut AppCleanup) -> (Result<Poll, CleanupError>, Duration) {
    let allowance = desktop::lifecycle::quit_allowance().unwrap();
    let began = Instant::now();
    cleanup.begin(allowance).unwrap();
    let deadline = began + Duration::from_millis(allowance.millis());
    loop {
        match cleanup.poll() {
            Ok(Poll::Pending) => {
                assert!(Instant::now() < deadline, "the packaged core did not clean up");
                std::thread::sleep(millis(B::OwnerPoll) / 5);
            }
            other => return (other, began.elapsed()),
        }
    }
}
fn acquisition_state(state: &Path) -> Value {
    let status: Value =
        serde_json::from_slice(&std::fs::read(state.join(SOURCE).join("status.json")).unwrap())
            .unwrap();
    status["state"].clone()
}

/// The owner opts in on the packaged window (`swarm_publish.action`, app mode): the routes answer
/// the custody's status, the opt-in is durable in `swarm-publisher.sqlite3` (0600) beside the
/// owner's private state, opting out is accepted, and Quit closes the custody within its budget
/// (`close(now, 5 s)`) before the process's own cleanup proof.
#[test]
fn packaged_swarm_listing_is_opt_in_and_closed_at_quit() {
    let state = state_root();
    let directory = SilentDirectory::start();
    let (core, mut cleanup) = packaged_core(&state);
    create(&core);
    // Never opted in: opting out changes nothing and writes nothing.
    assert_eq!(
        call(&core, "/api/swarm/unpublish", json!({})),
        (200, json!({"enabled": false, "mode": null}))
    );
    assert!(!private(&state).join(DB_FILE).exists());
    let published = call(
        &core,
        "/api/swarm/publish",
        json!({"directory": directory.origin, "name": "Packaged swarm",
               "access_mode": "request", "description": "A packaged owner"}),
    );
    assert_eq!(published.0, 200, "publish: {}", published.1);
    assert_eq!(published.1["enabled"], true);
    assert_eq!(published.1["mode"], "app");
    assert_eq!(published.1["directory"], directory.origin.as_str());
    let db = std::fs::metadata(private(&state).join(DB_FILE)).unwrap();
    assert_eq!(db.permissions().mode() & 0o777, 0o600);
    let withdrawn = call(&core, "/api/swarm/unpublish", json!({}));
    assert_eq!(withdrawn.0, 200, "unpublish: {}", withdrawn.1);
    assert_eq!(withdrawn.1["mode"], "app");
    let (result, took) = quit(&mut cleanup);
    assert_eq!(result, Ok(Poll::Verified));
    assert!(
        took < millis(B::PyMyceliumA13MacosSwarmPublishSwarmpublisherCloseSelfThreadJoinL453) * 2,
        "Quit took {took:?}"
    );
    assert_eq!(acquisition_state(&state), "closed");
    drop(core);
    drop(directory);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// While the owner never opted in, the custody makes no directory connection and writes no state,
/// through the window's whole life and its Quit.
#[test]
fn packaged_swarm_publisher_makes_no_network_while_listing_is_off() {
    let state = state_root();
    let directory = SilentDirectory::start();
    let (core, mut cleanup) = packaged_core(&state);
    create(&core);
    let (result, _) = quit(&mut cleanup);
    assert_eq!(result, Ok(Poll::Verified));
    assert_eq!(directory.accepted(), 0);
    assert!(!private(&state).join(DB_FILE).exists());
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

/// A listing state the custody cannot read fails every close (reconcile/laneB12f.md: keep the
/// close's result, run the host's own cleanup, then report it): Quit still closes the download
/// (`closed` persisted, as `DemoApp.close` continues after `swarm_publish.close`) and reports the
/// failure (exit 3), never a clean Quit.
#[test]
fn packaged_quit_closes_the_download_when_the_swarm_close_fails() {
    let state = state_root();
    std::fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(private(&state).join(DB_FILE))
        .unwrap();
    let (core, mut cleanup) = packaged_core(&state);
    create(&core);
    let (result, _) = quit(&mut cleanup);
    assert_eq!(
        result,
        Err(CleanupError::Poll),
        "a failed swarm close was reported as a clean Quit"
    );
    assert_eq!(acquisition_state(&state), "closed");
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}
