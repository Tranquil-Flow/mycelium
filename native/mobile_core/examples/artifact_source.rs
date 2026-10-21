// SPDX-License-Identifier: AGPL-3.0-only
//! A2-over-iroh artifact source (ALPN `mycelium.artifact_chunk.v1`) serving
//! content-addressed stage objects to phones under seed grants.
//!
//! ```text
//! cargo run --release --features net --example artifact_source -- \
//!     <objects-dir> <seed-key-record.json> <key-dir> [--local]
//! ```
//!
//! `<objects-dir>` holds `<sha256 hex>` files (`python -m
//! mycelium_mobile.qwen_stage_manifest export`). `<seed-key-record.json>` is
//! the seed's public key record (grants must verify under it). `<key-dir>`
//! keeps this source's iroh and signing secrets (created 0600 on first run),
//! so its endpoint id and key digest stay stable for the owner's
//! `route_assignment issue --artifact-source ENDPOINT_ID:KEY_DIGEST`; it also
//! holds the durable admission journal (replay fence and grant budgets).
//! Prints one ready line with both, then serves until killed.

use std::io::Write;
use std::path::{Path, PathBuf};

use mycelium_mobile_core::artifact_net::ArtifactSource;
use mycelium_mobile_core::transport::Reachability;
use serde_json::{Value, json};

fn secret(path: &Path) -> [u8; 32] {
    if let Ok(raw) = std::fs::read(path) {
        return raw.try_into().expect("key file must hold 32 bytes");
    }
    let mut key = [0u8; 32];
    getrandom::getrandom(&mut key).expect("randomness");
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    std::os::unix::fs::OpenOptionsExt::mode(&mut options, 0o600);
    options.open(path).and_then(|mut f| f.write_all(&key)).expect("key file writable");
    key
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 4 {
        eprintln!("usage: artifact_source <objects-dir> <seed-key-record.json> <key-dir> [--local]");
        std::process::exit(2);
    }
    let seed_key: Value = serde_json::from_str(&std::fs::read_to_string(&args[2]).expect("seed key record")).expect("seed key JSON");
    let key_dir = PathBuf::from(&args[3]);
    std::fs::create_dir_all(&key_dir).expect("key dir");
    let reach = if args.iter().any(|a| a == "--local") { Reachability::LocalOnly } else { Reachability::Production };
    let source = ArtifactSource::bind(&secret(&key_dir.join("iroh.key")), &secret(&key_dir.join("signing.key")), seed_key, PathBuf::from(&args[1]), &key_dir, reach)
        .await
        .expect("bind");
    let accept = source.spawn_accept();
    println!(
        "{}",
        json!({"ready": true, "alpn": "mycelium.artifact_chunk.v1", "endpoint_id": source.endpoint_id(), "verification_key_digest": source.key_digest(),
               // Direct addresses as grant hints (`ip:port`).
               "direct_addrs": source.addr().addrs.iter().filter_map(|a| match a { iroh::TransportAddr::Ip(ip) => Some(ip.to_string()), _ => None }).collect::<Vec<_>>()})
    );
    let _ = accept.await;
}
