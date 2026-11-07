// SPDX-License-Identifier: AGPL-3.0-only
//! Round 5 RED gate: a process spawned by node-service must hold no parent UDP/TCP/endpoint
//! descriptor. std/mio/iroh cannot create close-on-exec sockets atomically on macOS, so a
//! socket created in that gap would otherwise live in the child for the child's whole life
//! (a closed Iroh endpoint then keeps its UDP port bound). Every child runs `lsof` on its
//! own pid and reports the network descriptors it actually holds; no assertion is made on
//! the code path, only on what the real child sees.
use backend_mlx::{process::ProcessLimits, resources::ResourcePool};
use budgets::{BudgetId as B, Bytes, Entries, Reservation};
use node_service::spawn;
use rustix::io::{FdFlags, fcntl_setfd};
use std::{
    net::{TcpListener, UdpSocket},
    path::{Path, PathBuf},
    process::Command,
    sync::{
        Mutex, MutexGuard, PoisonError,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};
// Forks from a concurrent test thread would hold the gap descriptors until their own exec.
static SERIAL: Mutex<()> = Mutex::new(());
static NEXT: AtomicU64 = AtomicU64::new(0);
fn serial() -> MutexGuard<'static, ()> {
    SERIAL.lock().unwrap_or_else(PoisonError::into_inner)
}
fn duration(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
/// Parent descriptors a careless or concurrent creator leaves inheritable.
struct Gap {
    udp: UdpSocket,
    listener: TcpListener,
}
impl Gap {
    fn new() -> Self {
        let udp = UdpSocket::bind("127.0.0.1:0").unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        fcntl_setfd(&udp, FdFlags::empty()).unwrap();
        fcntl_setfd(&listener, FdFlags::empty()).unwrap();
        Self { udp, listener }
    }
    fn ports(&self) -> [u16; 2] {
        [
            self.udp.local_addr().unwrap().port(),
            self.listener.local_addr().unwrap().port(),
        ]
    }
}
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "v02-slice-confine-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).unwrap();
    }
}
/// The child asks lsof for the network descriptors of its own (the shell's) pid.
const LSOF_SELF: &str = "/usr/sbin/lsof -nP -a -p $$ -i -Fn";
fn network_names(output: &str) -> Vec<String> {
    output
        .lines()
        .filter(|line| line.starts_with('n'))
        .map(str::to_owned)
        .collect()
}
fn names_a_port(names: &[String], gap: &Gap) -> bool {
    names.iter().any(|name| {
        gap.ports()
            .iter()
            .any(|port| name.contains(&format!(":{port}")))
    })
}
#[test]
fn control_a_plain_child_inherits_the_gap_descriptors() {
    // Without this the confinement assertions below could pass vacuously.
    let _serial = serial();
    let gap = Gap::new();
    let out = Command::new("/bin/sh")
        .args(["-c", &format!("{LSOF_SELF}; true")])
        .output()
        .unwrap();
    let names = network_names(&String::from_utf8(out.stdout).unwrap());
    assert!(
        names_a_port(&names, &gap),
        "the fixture must expose the gap to an unconfined child: {names:?}"
    );
}
#[test]
fn host_probe_child_holds_no_parent_network_descriptor() {
    let _serial = serial();
    let gap = Gap::new();
    let out = spawn::confined_output("/bin/sh", &["-c", &format!("{LSOF_SELF}; true")]).unwrap();
    assert!(out.status.success());
    let names = network_names(&String::from_utf8(out.stdout).unwrap());
    assert!(
        names.is_empty(),
        "host probe child holds network descriptors {names:?} (parent ports {:?})",
        gap.ports()
    );
}
fn wait_for(path: &Path) -> String {
    let end = Instant::now() + duration(B::WorkerStartup);
    while !path.exists() {
        assert!(Instant::now() < end, "worker never reported");
        std::thread::sleep(duration(B::OwnerPoll));
    }
    std::fs::read_to_string(path).unwrap()
}
#[test]
fn spawned_worker_holds_no_parent_network_descriptor() {
    let _serial = serial();
    let temp = Temp::new();
    let gap = Gap::new();
    let report = temp.0.join("report");
    let partial = temp.0.join("partial");
    // Report, then stay alive until the owner cancels (stdin is the worker's pipe).
    let script = format!(
        "{LSOF_SELF} > '{p}'; mv '{p}' '{r}'; exec cat",
        p = partial.display(),
        r = report.display()
    );
    let mut command = Command::new("/bin/sh");
    command.args(["-c", &script]);
    let reservation = Reservation {
        resident: Bytes::new(1),
        staged: Bytes::ZERO,
        staging_overhead: Bytes::ZERO,
        scratch: Bytes::ZERO,
        kv: Bytes::ZERO,
    };
    let pool = ResourcePool::new(reservation.total().unwrap());
    let mut worker = spawn::spawn_worker(
        command,
        pool,
        reservation,
        ProcessLimits {
            deadline: Instant::now() + duration(B::MemberChosenWall),
            close: B::RouterClose.span(Entries::ZERO).unwrap(),
            reap: B::WorkerCleanupReserve.span(Entries::ZERO).unwrap(),
            poll: B::RegistryPoll.span(Entries::ZERO).unwrap(),
        },
    )
    .unwrap();
    let names = network_names(&wait_for(&report));
    worker.cancel().unwrap();
    let proven = worker.wait().unwrap().cleanup_proven;
    assert!(proven, "exact worker was not reaped");
    assert!(
        names.is_empty(),
        "worker holds network descriptors {names:?} (parent ports {:?})",
        gap.ports()
    );
}
