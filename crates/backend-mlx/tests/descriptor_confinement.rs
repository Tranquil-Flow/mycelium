// SPDX-License-Identifier: AGPL-3.0-only
//! A supervised leaf receives exactly stdin/stdout/stderr. v0.1.4 spawned children through Python
//! `subprocess` (`close_fds=True`); `std::process::Command` does not, and on macOS a descriptor
//! another thread or library never marks close-on-exec would otherwise outlive the parent's close.
use backend_mlx::{
    process::{OwnedWorker, ProcessLimits},
    resources::ResourcePool,
};
use budgets::{BudgetId, Bytes, Entries, Reservation, Resources, Span};
use rustix::io::{FdFlags, fcntl_setfd};
use std::{
    net::TcpListener,
    os::fd::AsRawFd,
    process::Command,
    time::{Duration, Instant},
};

const LEAKED: i32 = 17;

fn span(id: BudgetId) -> Span {
    id.span(Entries::ZERO).unwrap()
}
fn limits() -> ProcessLimits {
    ProcessLimits {
        deadline: Instant::now() + Duration::from_millis(span(BudgetId::WorkerStartup).millis()),
        close: span(BudgetId::RouterClose),
        reap: span(BudgetId::WorkerCleanupReserve),
        poll: span(BudgetId::RegistryPoll),
    }
}
fn amount() -> Reservation {
    Reservation {
        staged: Bytes::ZERO,
        staging_overhead: Bytes::ZERO,
        resident: Bytes::new(1),
        scratch: Bytes::ZERO,
        kv: Bytes::ZERO,
    }
}
/// A listener whose close-on-exec flag is cleared, as one a library left inheritable would be.
fn inheritable_listener() -> TcpListener {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    fcntl_setfd(&listener, FdFlags::empty()).unwrap();
    listener
}
/// Exits `LEAKED` when descriptor `fd` of the parent is open in the child.
fn probe(fd: i32) -> Command {
    let mut command = Command::new("/bin/sh");
    command.args([
        "-c",
        &format!("if [ -e /dev/fd/{fd} ]; then exit {LEAKED}; fi; exit 0"),
    ]);
    command
}

#[test]
fn control_plain_spawn_inherits_a_non_cloexec_descriptor() {
    let listener = inheritable_listener();
    let status = probe(listener.as_raw_fd()).status().unwrap();
    assert_eq!(
        status.code(),
        Some(LEAKED),
        "the probe must detect a leak or the confinement test proves nothing"
    );
}

#[test]
fn owned_worker_never_leaks_parent_descriptors_to_the_child() {
    let listener = inheritable_listener();
    let pool = ResourcePool::new(Resources {
        storage: Bytes::ZERO,
        memory: Bytes::new(1),
    });
    let mut worker = OwnedWorker::spawn(
        probe(listener.as_raw_fd()),
        pool.clone(),
        amount(),
        limits(),
    )
    .unwrap();
    let report = worker.wait().unwrap();
    assert!(report.cleanup_proven, "{:?}", report.errors);
    assert_eq!(
        report.status.and_then(|status| status.code()),
        Some(0),
        "the child inherited the parent's listener"
    );
    assert_eq!(pool.used().unwrap(), Resources::default());
}
