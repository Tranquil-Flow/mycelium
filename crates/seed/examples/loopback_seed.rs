// SPDX-License-Identifier: AGPL-3.0-only
//! Interop harness (RU-027), run by an EXTERNAL probe only (`L5-evidence/round11-interop-probe.py`): a Rust seed on
//! loopback TLS over a material directory (reused if it holds a set, generated the v0.1.4 way otherwise), driven
//! over stdin/stdout so a v0.1.4 Python member can verify it and join it. Scratch state only.
//!
//!   loopback_seed <root>           prints one JSON line `{"base_url", "ca", "swarm_id", "seed_node_id"}`, then
//!   reads commands: `mint <nonce> <ttl>` -> the invite bundle (JSON line), `member <node-id>` -> the member
//!   projection (JSON line), `quit`.
use protocol::identity::Identity;
use rand_core::{OsRng, RngCore};
use seed::{Coordinator, Options, server::Server, tls};
use serde_json::{Value, json};
use std::{
    io::{BufRead, Write},
    path::Path,
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

fn uuid4() -> String {
    let mut b = [0u8; 16];
    OsRng.fill_bytes(&mut b);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    let h: String = b.iter().map(|x| format!("{x:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    )
}

fn main() -> Result<(), String> {
    let root = std::env::args()
        .nth(1)
        .ok_or("usage: loopback_seed <root>")?;
    let root = Path::new(&root);
    let material = tls::ensure_local_tls_material(&root.join("tls")).map_err(|e| e.0.to_owned())?;
    let mut secret = [0u8; 32];
    OsRng.fill_bytes(&mut secret);
    let signer = Identity::from_secret("seed-endpoint", &secret).map_err(|e| format!("{e:?}"))?;
    let options = Options::new("swarm-r11-interop", "owner-node", &uuid4())
        .map_err(|e| e.code.to_string())?;
    let clock = Box::new(|| {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0.0, |d| d.as_secs_f64())
    });
    let coordinator = Arc::new(
        Coordinator::open(
            &root.join("seed.sqlite"),
            signer,
            options,
            clock,
            Box::new(uuid4),
        )
        .map_err(|e| e.code.to_string())?,
    );
    let server =
        Server::bind_tls(coordinator.clone(), &material, None).map_err(|e| e.code.to_string())?;
    let mut out = std::io::stdout().lock();
    let hello = json!({
        "base_url": server.base_url(),
        "ca": material.ca_certificate,
        "swarm_id": coordinator.swarm_id(),
        "seed_node_id": coordinator.seed_node_id(),
    });
    writeln!(out, "{hello}").map_err(|e| e.to_string())?;
    out.flush().map_err(|e| e.to_string())?;
    for line in std::io::stdin().lock().lines() {
        let line = line.map_err(|e| e.to_string())?;
        let words: Vec<&str> = line.split_whitespace().collect();
        let answer: Value = match words.as_slice() {
            ["mint", nonce, ttl] => {
                let ttl = ttl.parse::<u64>().map_err(|e| e.to_string())?;
                coordinator
                    .mint_invite(nonce, ttl)
                    .map_err(|e| json!({"error": e.code}).to_string())?
            }
            ["member", node] => coordinator
                .member(node)
                .unwrap_or_else(|e| json!({"error": e.code})),
            ["quit"] => break,
            other => json!({"error": format!("unknown command {other:?}")}),
        };
        writeln!(out, "{answer}").map_err(|e| e.to_string())?;
        out.flush().map_err(|e| e.to_string())?;
    }
    server.close();
    Ok(())
}
