// SPDX-License-Identifier: AGPL-3.0-only
//! Host-side driver for NodeCore (same code path the app runs):
//! `cargo run --features net --example node_cli -- <data_dir> <invite_qr|resume> [local] [--fast] [--watch <s>]`
//!
//! `--fast` shortens the membership timing for short-lease test seeds; `--watch`
//! keeps the node alive for that many seconds, printing one `status` line per
//! change (the lease-recovery tests read them). `--platform-sample k=v,...`
//! stands in for the app shell's readings (battery, charging, temperatures),
//! re-sent on every status tick, for hosts without them (the artifact gate fails
//! closed on unknown readings).

use std::path::Path;
use std::time::{Duration, Instant};

use mycelium_mobile_core::node::{NodeCore, NodeTiming};
use mycelium_mobile_core::transport::Reachability;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let flag = |name: &str| args.iter().any(|a| a == name);
    let reach = if flag("local") { Reachability::LocalOnly } else { Reachability::Production };
    let timing = if flag("--fast") {
        NodeTiming {
            heartbeat_interval: Duration::from_millis(500),
            http_timeout: Duration::from_secs(2),
            retry_min: Duration::from_millis(200),
            retry_max: Duration::from_secs(1),
        }
    } else {
        NodeTiming::default()
    };
    let watch = args
        .iter()
        .position(|a| a == "--watch")
        .and_then(|i| args.get(i + 1))
        .and_then(|v| v.parse::<f64>().ok());
    let sample: Option<mycelium_mobile_core::evidence::Metrics> = args
        .iter()
        .position(|a| a == "--platform-sample")
        .and_then(|i| args.get(i + 1))
        .map(|spec| {
            spec.split(',')
                .filter_map(|kv| kv.split_once('='))
                .map(|(k, v)| (k.to_owned(), v.parse::<f64>().ok()))
                .collect()
        });
    let core = NodeCore::open_with_timing(Path::new(&args[1]), reach, timing).expect("open");
    if let Some(sample) = &sample {
        core.set_platform_metrics(sample.clone());
    }
    let show = |label: &str, core: &NodeCore| {
        println!("{label} {}", serde_json::to_string(&core.status()).unwrap());
    };
    show("opened", &core);
    if args[2] == "resume" {
        match core.resume() {
            Ok(_) => show("resuming", &core),
            Err(e) => println!("resume_error {e}"),
        }
    } else {
        match core.pair_with_channel_qr(&args[2], "host-cli-1") {
            Ok(_) => show("paired", &core),
            Err(e) => println!("pair_error {e}"),
        }
        match core.join() {
            Ok(_) => show("joined", &core),
            Err(e) => println!("join_error {e}"),
        }
    }
    match watch {
        Some(seconds) => {
            let until = Instant::now() + Duration::from_secs_f64(seconds);
            let mut last = String::new();
            while Instant::now() < until {
                let status = core.status();
                if let Some(sample) = &sample {
                    core.set_platform_metrics(sample.clone());
                }
                let key = format!(
                    "{}|{}|{:?}|{:?}|{:?}|{:?}|{:?}|{:?}",
                    status.phase,
                    status.lease_fresh,
                    status.member_generation,
                    status.last_error,
                    status.route_assignment,
                    status.route_assignment_error,
                    status.artifact_state,
                    status.stage_loaded
                );
                if key != last {
                    last = key;
                    show("status", &core);
                }
                std::thread::sleep(Duration::from_millis(100));
            }
        }
        None => std::thread::sleep(Duration::from_secs(3)),
    }
    show("final", &core);
    core.shutdown();
}
