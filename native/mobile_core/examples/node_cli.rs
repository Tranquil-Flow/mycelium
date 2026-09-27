// SPDX-License-Identifier: AGPL-3.0-or-later
//! Host-side driver for NodeCore (same code path the app runs):
//! `cargo run --features net --example node_cli -- <data_dir> <invite_qr> [local]`

use std::path::Path;

use mycelium_mobile_core::node::NodeCore;
use mycelium_mobile_core::transport::Reachability;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let reach = if args.get(3).map(String::as_str) == Some("local") {
        Reachability::LocalOnly
    } else {
        Reachability::Production
    };
    let core = NodeCore::open(Path::new(&args[1]), reach).expect("open");
    let show = |label: &str, core: &NodeCore| {
        println!("{label} {}", serde_json::to_string(&core.status()).unwrap());
    };
    show("opened", &core);
    if args[2] == "resume" {
        match core.resume() {
            Ok(_) => show("resumed", &core),
            Err(e) => println!("resume_error {e}"),
        }
        std::thread::sleep(std::time::Duration::from_secs(3));
        show("final", &core);
        core.shutdown();
        return;
    }
    match core.pair_with_channel_qr(&args[2], "host-cli-1") {
        Ok(_) => show("paired", &core),
        Err(e) => println!("pair_error {e}"),
    }
    match core.join() {
        Ok(_) => show("joined", &core),
        Err(e) => println!("join_error {e}"),
    }
    std::thread::sleep(std::time::Duration::from_secs(3));
    show("final", &core);
    core.shutdown();
}
