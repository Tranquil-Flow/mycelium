// SPDX-License-Identifier: AGPL-3.0-only
//! Interop check against a LIVE v0.1.4 loopback TLS seed (kept alive by `L10-evidence/r11-probe-live-seed.py`
//! while this test runs; ignored unless `L10_LIVE_SEED_DIR` names its scratch directory). The Rust member
//! uses the system clock, the recorded CA and the seed's real `127.0.0.1` port: a real TLS handshake with
//! Python's `ssl` server and the real `http.server` framing. Run by hand:
//! `L10_LIVE_SEED_DIR=<dir> cargo test -p api --test live_seed -- --ignored`.
use api::{
    peer::{JoinEnv, accept_handoff},
    seed::SeedOptions,
};
use protocol::identity::Identity;
use serde_json::Value;
use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf, time::SystemTime};

#[test]
#[ignore = "needs the live oracle seed of r11-probe-live-seed.py"]
fn the_rust_member_joins_a_live_python_seed_over_real_tls() {
    let dir = PathBuf::from(std::env::var("L10_LIVE_SEED_DIR").expect("L10_LIVE_SEED_DIR"));
    let ready: Value = serde_json::from_slice(&fs::read(dir.join("ready.json")).unwrap()).unwrap();
    let handoff: Value =
        serde_json::from_slice(&fs::read(dir.join("handoff.json")).unwrap()).unwrap();
    let hex = ready["signer_secret"].as_str().unwrap();
    let mut secret = [0u8; 32];
    for (i, b) in secret.iter_mut().enumerate() {
        *b = u8::from_str_radix(&hex[2 * i..2 * i + 2], 16).unwrap();
    }
    let signer =
        Identity::from_secret(ready["signer_endpoint"].as_str().unwrap(), &secret).unwrap();
    // The member's own state (its pending request and consent) is the Python member's, copied verbatim.
    let state = dir.join("rust-member");
    fs::remove_dir_all(&state).unwrap_or(());
    fs::create_dir_all(&state).unwrap();
    fs::set_permissions(&state, fs::Permissions::from_mode(0o700)).unwrap();
    for entry in fs::read_dir(dir.join("member")).unwrap() {
        let path = entry.unwrap().path();
        if path.is_file() {
            let target = state.join(path.file_name().unwrap());
            fs::copy(&path, &target).unwrap();
            fs::set_permissions(&target, fs::Permissions::from_mode(0o600)).unwrap();
        }
    }
    let options = SeedOptions {
        ca_pem: Some(fs::read(dir.join("ca.pem")).unwrap()),
        connect_to: None,
        server_name: None,
    };
    let clock = || {
        SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .unwrap()
            .as_secs_f64()
    };
    let random = |n: usize| {
        (0..n)
            .map(|i| format!("{:02x}", (i * 7 + 3) % 251))
            .collect::<String>()
    };
    let env = JoinEnv {
        signer: &signer,
        clock: &clock,
        random_hex: &random,
        seed: &options,
    };
    let result = accept_handoff(&state, true, &handoff, &env);
    let answer = match &result {
        Ok(accepted) => accepted.answer.to_value(),
        Err(e) => serde_json::json!({"error": e.code}),
    };
    fs::write(dir.join("rust-result.json"), answer.to_string()).unwrap();
    let accepted = result.expect("a live join");
    // The expected answer is the oracle's own: the Python member's `accept_handoff` on an identical live seed.
    let oracle: Value =
        serde_json::from_slice(&fs::read(dir.join("oracle-answer.json")).unwrap()).unwrap();
    assert_eq!(accepted.answer.to_value(), oracle);
    assert!(accepted.session.is_some());
}
