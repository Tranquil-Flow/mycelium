// SPDX-License-Identifier: AGPL-3.0-only
//! SE-L10-TLS-KEYGEN-SLOW measurement tool: wall and CPU time of `tls::generate_material` (two RSA-2048 keys, two
//! signatures) in the profile this example is built with.
//!     cargo run --offline --locked -p seed --example keygen_timing -- 3
use std::time::Instant;

fn cpu_seconds() -> f64 {
    let output = std::process::Command::new("/bin/ps")
        .args(["-o", "time=", "-p", &std::process::id().to_string()])
        .output()
        .ok();
    output
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map_or(f64::NAN, |text| {
            let text = text.trim();
            let (minutes, seconds) = text.split_once(':').unwrap_or(("0", text));
            minutes.parse::<f64>().unwrap_or(f64::NAN) * 60.0
                + seconds.parse::<f64>().unwrap_or(f64::NAN)
        })
}

fn main() {
    let runs: usize = std::env::args()
        .nth(1)
        .and_then(|n| n.parse().ok())
        .unwrap_or(3);
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    for run in 0..runs {
        let (wall, cpu) = (Instant::now(), cpu_seconds());
        let bodies = seed::tls::generate_material(now).unwrap_or_else(|e| panic!("{}", e.0));
        let _ = bodies;
        println!(
            "run {run}: wall {:.2} s, cpu(cumulative) {:.2} s",
            wall.elapsed().as_secs_f64(),
            cpu_seconds() - cpu
        );
    }
}
