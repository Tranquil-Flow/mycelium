// SPDX-License-Identifier: AGPL-3.0-only
#![cfg(feature = "iroh-adapter")]
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use node::{Supervisor, Ticket, WorkerAdapter, process::NativeChild};
use runtime::iroh_host::{LoopbackHost, close_endpoint_and_native};
use std::{
    net::UdpSocket,
    path::PathBuf,
    process::Command,
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant},
};
use transport::Clock;
struct Time(Instant);
impl Clock for Time {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(ClockDomain(22), self.0.elapsed().as_millis() as u64)
    }
    fn wall_seconds(&self) -> f64 {
        panic!("no wall-clock cleanup")
    }
}
impl Time {
    fn deadline(&self) -> MonoInstant {
        self.monotonic()
            .checked_add(B::RouterClose.span(Entries::ZERO).unwrap())
            .unwrap()
    }
}
static NEXT: AtomicU64 = AtomicU64::new(0);
// S23-class boundary: native child spawn, endpoint socket creation and the
// immediate release probe are ordered; every network exchange stays parallel.
include!("support/fork_lock.rs");
// `spawn()` returns when std's exec-status pipe closes, but exec closes the
// close-on-exec descriptors from the highest number down, so the child can still
// hold lower inherited copies (an endpoint socket) after that (B20: modelled
// deterministically with a 20k-descriptor block). The boundary therefore ends
// only once the child runs its new image and creates `started`. That narrows
// but does not close the window: the kernel can finish releasing an inherited
// socket shortly after the child starts (B20, 50k-descriptor model under load).
fn spawn_native(
    command: &mut Command,
    root: &std::path::Path,
    started: &std::path::Path,
) -> NativeChild {
    let _boundary = fixture_spawn_boundary();
    let mut child = NativeChild::spawn(command.env("L9_CLOSE_STARTED", started), root).unwrap();
    let end = Instant::now()
        + Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis());
    while !started.exists() {
        assert!(
            child.observe().is_ok_and(|residue| !residue.process_gone),
            "fixture child ended before it started"
        );
        assert!(Instant::now() < end, "fixture child did not start");
        std::thread::yield_now();
    }
    child
}
fn bind_host<'a>(runtime: &'a tokio::runtime::Runtime, clock: &Time) -> LoopbackHost<'a> {
    let _boundary = fixture_spawn_boundary();
    LoopbackHost::bind(runtime, clock, clock.deadline()).unwrap()
}
fn assert_released(address: &iroh::EndpointAddr, what: &str) {
    let _boundary = fixture_spawn_boundary();
    for address in address.ip_addrs() {
        assert!(UdpSocket::bind(*address).is_ok(), "{what}");
    }
}
struct Owned {
    supervisor: Supervisor<NativeChild>,
    root: PathBuf,
    started: PathBuf,
}
impl Owned {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "v02-l9-close-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        // Beside the owned root, never in it: the root must be empty for the
        // native adapter's own cleanup proof.
        let started = root.with_extension("started");
        let mut cmd = Command::new(std::env::current_exe().unwrap());
        cmd.args(["--exact", "native_close_child_fixture"])
            .env("L9_CLOSE_FIXTURE", "park");
        let child = spawn_native(&mut cmd, &root, &started);
        let ticket = Ticket {
            epoch: 1,
            run: 1,
            node: "local".into(),
            incarnation: "fixture".into(),
        };
        Self {
            supervisor: Supervisor::new(ticket, child),
            root,
            started,
        }
    }
}
impl Drop for Owned {
    fn drop(&mut self) {
        self.supervisor
            .request_stop()
            .expect("stop exact owned native PID");
        let end = Instant::now()
            + Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis());
        loop {
            match self.supervisor.worker_mut().observe() {
                Ok(r) if r.process_gone => break,
                Ok(_) => assert!(Instant::now() < end, "owned worker leaked"),
                // NativeChild observes/reaps before removing a nonempty owned
                // directory. Retain that explicit error, then remove OUR sentinel.
                Err(error) => {
                    eprintln!("owned_fixture_cleanup: {error:?}");
                    break;
                }
            }
            std::thread::yield_now();
        }
        if self.root.exists() {
            std::fs::remove_dir_all(&self.root).unwrap();
        }
        std::fs::remove_file(&self.started).unwrap();
    }
}
#[test]
fn native_close_child_fixture() {
    if std::env::var_os("L9_CLOSE_FIXTURE").is_some() {
        if let Some(started) = std::env::var_os("L9_CLOSE_STARTED") {
            std::fs::File::create_new(started).unwrap();
        }
        loop {
            std::thread::park();
        }
    }
}
#[test]
fn close_proves_exact_native_child_and_endpoint_then_releases_socket() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let clock = Time(Instant::now());
    let mut host = bind_host(&rt, &clock);
    let address = host.address().unwrap();
    let mut owned = Owned::new();
    assert!(!owned.supervisor.observe_cleanup().unwrap().proven());
    let report =
        close_endpoint_and_native(&mut host, &mut owned.supervisor, &clock, clock.deadline());
    assert!(report.proven(), "{report:?}");
    let proof = report.into_native_proof().unwrap();
    assert_eq!(proof.ticket().node, "local");
    assert!(proof.proven());
    assert!(!owned.root.exists());
    assert!(host.address().is_err());
    drop(host);
    drop(rt);
    assert_released(
        &address,
        "closed native-close endpoint must release its sockets",
    );
}
#[test]
fn expired_endpoint_close_still_stops_native_and_never_releases_unproven_resources() {
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let clock = Time(Instant::now());
    let mut host = bind_host(&rt, &clock);
    let mut owned = Owned::new();
    let sentinel = owned.root.join("retained-resource");
    std::fs::write(&sentinel, b"keep").unwrap();
    let report =
        close_endpoint_and_native(&mut host, &mut owned.supervisor, &clock, clock.monotonic());
    assert!(!report.proven());
    assert!(report.into_native_proof().is_err());
    assert!(host.address().is_err());
    let report =
        close_endpoint_and_native(&mut host, &mut owned.supervisor, &clock, clock.deadline());
    assert!(
        !report.proven(),
        "endpoint close alone must not prove native residue"
    );
    assert!(report.observation_error.is_some());
    assert_eq!(std::fs::read(&sentinel).unwrap(), b"keep");
    std::fs::remove_file(sentinel).unwrap();
    let fresh =
        close_endpoint_and_native(&mut host, &mut owned.supervisor, &clock, clock.deadline());
    assert!(fresh.proven(), "{fresh:?}");
    assert!(fresh.into_native_proof().unwrap().proven());
}
#[test]
fn endpoint_release_is_ordered_against_concurrent_native_spawns() {
    // S23-class control for this file: while other threads spawn exact-owned
    // native children, an endpoint that was closed must release its UDP sockets
    // for an immediate rebind. Bounded by completed bind/close cycles.
    struct Stop<'a>(&'a std::sync::atomic::AtomicBool);
    impl Drop for Stop<'_> {
        fn drop(&mut self) {
            self.0.store(true, Ordering::SeqCst);
        }
    }
    let stop = std::sync::atomic::AtomicBool::new(false);
    std::thread::scope(|scope| {
        let _stop = Stop(&stop);
        for _ in 0..3 {
            scope.spawn(|| {
                while !stop.load(Ordering::SeqCst) {
                    let mut owned = Owned::new();
                    owned.supervisor.request_stop().unwrap();
                    drop(owned);
                }
            });
        }
        for _ in 0..30 {
            let rt = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            let clock = Time(Instant::now());
            let host = bind_host(&rt, &clock);
            let address = host.address().unwrap();
            // Spawns overlap the endpoint's whole life; the close and the
            // immediate rebind are ordered against them. A spawn boundary now
            // lasts until the child runs, so waiting for one between close and
            // rebind would leave the freed ephemeral port open to other
            // processes that long (B20: another test binary bound it).
            let _boundary = fixture_spawn_boundary();
            drop(host);
            drop(rt);
            for socket in address.ip_addrs() {
                assert!(
                    UdpSocket::bind(*socket).is_ok(),
                    "closed endpoint must release sockets under native spawn pressure"
                );
            }
        }
    });
}
