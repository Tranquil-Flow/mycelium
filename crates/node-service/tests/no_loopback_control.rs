// SPDX-License-Identifier: AGPL-3.0-only
//! Round 4 RED gate: the composed owner/member/leaf slice must carry control,
//! provisioning and renewal exclusively over L9's signed Iroh NodeControl (and
//! artifact) carriers. The only TCP the slice may keep is the public HTTP/SSE
//! gateway used by the qualification client, which lives in `native/verify.rs`.
use std::{fs, path::PathBuf};

fn source(relative: &str) -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(relative);
    fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

fn composed_sources() -> Vec<(String, String)> {
    ["src/native.rs", "src/native/lifecycle_owner.rs"]
        .into_iter()
        .map(|p| (p.to_owned(), source(p)))
        .collect()
}

#[test]
fn composed_slice_has_no_tcp_sockets_for_control() {
    for (name, text) in composed_sources() {
        for forbidden in ["TcpListener", "TcpStream", "127.0.0.1:0"] {
            assert!(
                !text.contains(forbidden),
                "{name} still uses {forbidden}: control must ride Iroh NodeControl"
            );
        }
    }
}

#[test]
fn composed_slice_has_no_length_prefixed_json_rpc() {
    for (name, text) in composed_sources() {
        for forbidden in [
            "loopback::send",
            "loopback::receive",
            "send_json",
            "receive_json",
            "loopback::configure",
        ] {
            assert!(
                !text.contains(forbidden),
                "{name} still calls {forbidden}: loopback JSON RPC must be gone"
            );
        }
    }
}

#[test]
fn loopback_module_no_longer_exports_a_control_framing() {
    let text = source("src/loopback.rs");
    for forbidden in [
        "pub fn send",
        "pub fn receive",
        "pub fn send_json",
        "pub fn receive_json",
    ] {
        assert!(
            !text.contains(forbidden),
            "loopback.rs still exports `{forbidden}`"
        );
    }
}

#[test]
fn native_binds_the_signed_carrier_protocols_and_has_no_tcp_fallback() {
    let native = source("src/native.rs");
    assert!(
        !native.contains("cfg(not(feature = \"iroh-loopback\"))"),
        "no TCP development fallback may remain behind a missing feature"
    );
    let frames = source("src/iroh_frames.rs");
    assert!(frames.contains("bind_protocols"));
    assert!(frames.contains("Channel::NodeControl"));
    assert!(frames.contains("Channel::ArtifactChunk"));
    let manifest = source("Cargo.toml");
    assert!(
        manifest.contains("metal = [\"backend-mlx/metal\", \"dep:tokenizers\", \"iroh-loopback\"]"),
        "the native (metal) slice must always carry the Iroh carriers"
    );
}

#[test]
fn every_composed_spawn_goes_through_the_confining_module() {
    // Round 5: no process is built outside `spawn`, which registers L6's descriptor
    // confinement last. `native.rs` may not spawn or capture a child directly.
    for (name, text) in composed_sources() {
        for forbidden in [
            "OwnedWorker::spawn",
            ".output()",
            ".spawn()",
            "Command::new",
        ] {
            let allowed = if forbidden == "Command::new" {
                // Workers are configured as a Command and handed to spawn_worker.
                text.matches("Command::new").count() == text.matches("spawn::spawn_worker").count()
            } else {
                !text.contains(forbidden)
            };
            assert!(allowed, "{name} uses {forbidden} outside the spawn module");
        }
    }
}
