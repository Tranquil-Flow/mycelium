// SPDX-License-Identifier: AGPL-3.0-only
// Measurement tool, not a test: wall time of one confined spawn (`fork` + the descriptor
// confinement closure + `exec` of /usr/bin/true) at the current RLIMIT_NOFILE. Run it
// under `ulimit -n 256|10240|1048575` to see how the confinement cost depends on the limit.
// Prints one JSON line. Timings are host- and load-dependent; they are never asserted.
use std::{
    os::fd::{AsRawFd, OwnedFd},
    process::Command,
    time::Instant,
};
fn main() {
    let rounds: usize = std::env::args()
        .nth(1)
        .and_then(|n| n.parse().ok())
        .unwrap_or(9);
    let extra: usize = std::env::args()
        .nth(2)
        .and_then(|n| n.parse().ok())
        .unwrap_or(0);
    // Inheritable-by-default decoys (cleared close-on-exec), like descriptors a library
    // has not marked yet, plus `extra` more for the many-open-descriptors case.
    let mut held: Vec<OwnedFd> = Vec::new();
    for _ in 0..(5 + extra) {
        let (r, w) = std::io::pipe().expect("pipe");
        for end in [OwnedFd::from(r), OwnedFd::from(w)] {
            rustix::io::fcntl_setfd(&end, rustix::io::FdFlags::empty()).expect("clear cloexec");
            held.push(end);
        }
    }
    let limit = rustix::process::getrlimit(rustix::process::Resource::Nofile).current;
    let mut times = Vec::new();
    for _ in 0..rounds {
        let mut command = Command::new("/usr/bin/true");
        node::process::confine_descriptors(&mut command).expect("confine");
        let start = Instant::now();
        let status = command.status().expect("spawn");
        times.push(start.elapsed().as_secs_f64());
        assert!(status.success());
    }
    times.sort_by(f64::total_cmp);
    println!(
        "{{\"nofile_soft\":{},\"open_decoys\":{},\"max_decoy_fd\":{},\"rounds\":{},\"median_s\":{:.4},\"min_s\":{:.4},\"max_s\":{:.4}}}",
        limit.map_or(-1, |l| i64::try_from(l).unwrap_or(-1)),
        held.len(),
        held.iter().map(AsRawFd::as_raw_fd).max().unwrap_or(-1),
        rounds,
        times[times.len() / 2],
        times[0],
        times[times.len() - 1]
    );
}
